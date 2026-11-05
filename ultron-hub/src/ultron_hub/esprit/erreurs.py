"""Errors met and their fix (todo #506): one small fiche per symptom, found again by the words of the symptom.

    <savoir_dir>/erreurs/AAAA-MM-JJ-<symptome-court>.md   YAML header + « Symptôme / Cause / Remède » (+ Occurrences)

These are facts (like the journal), so they are written directly, without a signed ✅. Text goes through
`scrub` (secret lines masked) and `filter_instructions` (imperative sentences addressed to an AI removed).
The same normalised symptom is never filed twice: a new occurrence is added to the existing fiche.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import savoir, store
from ultron_hub.esprit.savoir import SavoirError
from ultron_hub.souvenirs.index import looks_secret

DIR = "erreurs"
SYMPTOME_MAX = 200
CAUSE_MAX = 300
REMEDE_MAX = 300
MASK = "[masqué]"
OCC_HEADING = "## Occurrences"
HEADER_KEYS = ("symptome", "date", "projet", "todos", "auteur", "occurrences")


def _mask(text: str) -> str:
    """Secrets masked: a token that looks secret becomes [masqué]; a secret line with no such token, whole."""
    out = []
    for line in text.split("\n"):
        if looks_secret(line):
            words = [MASK if looks_secret(w) else w for w in line.split()]
            line = MASK if MASK not in words else " ".join(words)
        out.append(line)
    return "\n".join(out)


def _field(value: Any, name: str, limit: int) -> str:
    if value is None or not isinstance(value, str) or not value.strip():
        raise SavoirError(f"champ « {name} » manquant")
    text = " ".join(savoir._clean(value).split())
    if len(text) > limit:
        raise SavoirError(f"champ « {name} » trop long ({len(text)} > {limit} caractères)")
    kept, _ = savoir.filter_instructions(_mask(text))
    kept = " ".join(kept.split())
    if not kept:
        raise SavoirError(f"champ « {name} » refusé : il ne contient qu'une consigne")
    return kept


def normalize(symptome: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", savoir._fold(symptome)))


def _load(path: Path) -> dict[str, Any] | None:
    if savoir.FICHE_ID_RE.fullmatch(path.stem) is None:
        return None
    try:
        text = savoir._decode(savoir._read_bounded(path, savoir.DRAFT_MAX_BYTES), path)
        fields, body = savoir.parse(text)
    except (SavoirError, InvalidRequestError):
        store.log.warning("erreurs: fiche ignored (%s)", path.name)
        return None
    if not isinstance(fields.get("symptome"), str) or not isinstance(fields.get("date"), str):
        return None
    return {"id": path.stem, "fields": fields, "body": body, "text": text}


def _all(settings: Settings) -> list[dict[str, Any]]:
    return _all_in(savoir.root(settings))


def _all_in(base: Path) -> list[dict[str, Any]]:
    folder = base / DIR
    if not folder.is_dir():
        return []
    rows = [r for p in folder.glob("*.md") if (r := _load(p)) is not None]
    return sorted(rows, key=lambda r: (r["fields"]["date"], r["id"]), reverse=True)


def _remede(body: str) -> str:
    match = re.search(r"## Remède\n(.*?)(?=\n## |\Z)", body, re.S)
    return " ".join(match.group(1).split()) if match else ""


def _summary(row: dict) -> dict[str, Any]:
    f = row["fields"]
    return {"id": row["id"], "type": "erreur", "date": f["date"], "projet": f.get("projet", "autre"),
            "titre": f["symptome"], "retenir": store.one_line(_remede(row["body"]), savoir.RETENIR_MAX),
            "occurrences": f.get("occurrences", 1)}


def _render(fields: dict, symptome: str, cause: str, remede: str, occurrences: list[str]) -> str:
    """The fiche, oldest occurrences dropped until it fits FICHE_MAX_BYTES (the count stays in the header)."""
    occ = list(occurrences)
    while True:
        body = f"## Symptôme\n{symptome}\n\n## Cause\n{cause}\n\n## Remède\n{remede}"
        if occ:
            body += f"\n\n{OCC_HEADING}\n" + "\n".join(occ)
        text = savoir.render(fields, body)
        if len(text.encode("utf-8")) <= savoir.FICHE_MAX_BYTES:
            return text
        if not occ:
            raise SavoirError("fiche d'erreur trop longue")
        occ.pop(0)


def _section(body: str, title: str) -> str:
    match = re.search(rf"## {title}\n(.*?)(?=\n\n## |\Z)", body, re.S)
    return match.group(1).strip() if match else ""


def add(settings: Settings, symptome: str, cause: str, remede: str, projet: str | None = None,
        todo: int | None = None, par: str | None = None) -> dict[str, Any]:
    """Files an error (or one more occurrence of a known one). Returns {id, path, nouvelle, occurrences, ...}."""
    symptome = _field(symptome, "symptome", SYMPTOME_MAX)
    cause = _field(cause, "cause", CAUSE_MAX)
    remede = _field(remede, "remede", REMEDE_MAX)
    projet = savoir._choice(projet or "autre", "projet", savoir.PROJETS)
    par = savoir._line(par, "par", savoir.AUTHOR_MAX)
    if todo is not None and (isinstance(todo, bool) or not isinstance(todo, int) or todo <= 0):
        raise SavoirError(f"numéro de todo invalide : {todo!r}")
    key = normalize(symptome)
    if not key:
        raise SavoirError("champ « symptome » sans mot exploitable")
    base = savoir._ensure_dirs(settings)
    folder = base / DIR
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink() or not folder.is_dir():
        raise SavoirError(f"dossier des erreurs invalide : {folder}")
    date = savoir.today(settings)
    with store._locked(base):
        existing = next((r for r in _all(settings) if normalize(r["fields"]["symptome"]) == key), None)
        if existing is None:
            fields = {"symptome": symptome, "date": date, "projet": projet,
                      "todos": [todo] if todo else [], "auteur": par, "occurrences": 1}
            text = _render(fields, symptome, cause, remede, [])
            fiche_id = savoir._free_id(folder, f"{date}-{savoir.slug(symptome)}")
        else:
            fiche_id = existing["id"]
            fields = {k: existing["fields"].get(k) for k in HEADER_KEYS if k in existing["fields"]}
            fields["occurrences"] = int(fields.get("occurrences") or 1) + 1
            todos = list(fields.get("todos") or [])
            if todo and todo not in todos and len(todos) < savoir.TODOS_MAX:
                todos.append(todo)
            fields["todos"] = todos
            body = existing["body"]
            old_remede = _section(body, "Remède")
            occ = [ln for ln in _section(body, "Occurrences").split("\n") if ln.strip()]
            occ.append(f"- {date}" + (f" : {remede}" if remede != old_remede else ""))
            text = _render(fields, _section(body, "Symptôme") or symptome, _section(body, "Cause") or cause,
                           old_remede or remede, occ)
        store._write_atomic(folder / f"{fiche_id}.md", text)
        savoir._write_index(base)
    store.record(settings, "ultron-hub", "erreur", f"Erreur notée : {store.one_line(symptome, 80)}", todo)
    return {"id": fiche_id, "path": str(folder / f"{fiche_id}.md"), "nouvelle": existing is None,
            "occurrences": fields["occurrences"], "size": len(text.encode("utf-8")), "symptome": symptome}


def entries_in(base: Path) -> list[dict[str, Any]]:
    return [_summary(r) for r in _all_in(base)]


def entries(settings: Settings, limit: int | None = None) -> list[dict[str, Any]]:
    rows = entries_in(savoir.root(settings))
    return rows if limit is None else rows[:max(1, int(limit))]


def search_rows(settings: Settings, query: str) -> list[dict[str, Any]]:
    """Every error fiche containing all the words (case and accents ignored), with matching lines."""
    words = [w for w in (savoir._fold(w) for w in (query or "").split()) if w]
    if not words:
        raise SavoirError("recherche vide")
    hits = []
    for row in _all(settings):
        if all(w in savoir._fold(row["text"]) for w in words):
            lines = [store.one_line(ln, 200) for ln in row["text"].split("\n")
                     if ln.strip() and ln.strip() != "---" and any(w in savoir._fold(ln) for w in words)]
            hits.append({**_summary(row), "lignes": lines[:savoir.SEARCH_LINES]})
    return hits


def search(settings: Settings, query: str, limit: int = savoir.SEARCH_MAX) -> list[dict[str, Any]]:
    return search_rows(settings, query)[:max(1, min(int(limit), savoir.SEARCH_MAX))]
