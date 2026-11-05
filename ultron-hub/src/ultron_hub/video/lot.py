"""✅ Tout ranger (todo #442): every proposal of a video (actions.json, todos #93/#441) filed on ONE signed press.

`video lot <id>` (read only, no AI) lists the proposals still pending, each by the test of its own mechanism:
    fiche            -> its draft is valid and its fingerprint is not in the notebook's ledger (savoir.ledger) ;
    todo, recherche  -> no todo whose source is `video:<id>` has this title (whatever its status: never twice) ;
    profil           -> no fact proposed with source `video:<id in lower case>` (the profile's rule) has this text.
`empreinte` = 16 hex of the SHA-256 of the video id and one JSON line per pending proposal (sorted keys, the fiche by
the fingerprint of its draft as it is now), null when nothing is pending.

`video ranger-lot <id> --empreinte X` recomputes the lot (different fingerprint = refused, nothing done), needs the
`video-lot` token signed on "<id>:<fingerprint>" (or `--i-am-human`), never replayed (proofs in var/video/lots.jsonl,
spent before anything runs), then files each proposal by the code of its unit ✅: `savoir.rank` on the fingerprint
checked (trace `via: video-lot`, the lot's proof as `lot_proof`), `todo add --source video:<id>` (same arguments as the
proposal card), `profil propose --source video:<id in lower case>` (validated later in Profil). Every proposal is tried; the result
is one line each.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ultron_hub import proposals
from ultron_hub.common.errors import InvalidRequestError, UltronHubError
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import savoir
from ultron_hub.esprit import store as esprit
from ultron_hub.todo import closing
from ultron_hub.todo.store import TodoStore
from ultron_hub.video import collect as collector

ACTION = "video-lot"
EMPREINTE_RE = re.compile(r"[0-9a-f]{16}")
TITLE_MAX = 40  # the title the proposal card gave `todo add` (proposals.TITLE_MAX)
ICONES = {"fiche": "📚 Fiche", "todo": "➕ Todo", "recherche": "🔎 Recherche", "profil": "👤 Fait pour ton profil"}


def ledger_path(settings: Settings) -> Path:
    return settings.var_dir / "video" / "lots.jsonl"


def source_of(video_id: str) -> str:
    return f"video:{video_id}"


def fact_source(video_id: str) -> str:
    """The profile's sources are lower case (esprit.store._SOURCE_RE): « video:20261019t120000z-0a1b2c3d »."""
    return source_of(video_id).lower()


def _proposed(video_id: str) -> list[dict[str, Any]]:
    path = collector.video_dir(video_id) / proposals.ACTIONS_JSON
    if not path.is_file() or path.is_symlink():
        raise InvalidRequestError("aucune proposition pour cette vidéo (pas encore analysée ?)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise InvalidRequestError("propositions illisibles") from None
    items = data.get("actions") if isinstance(data, dict) else None
    return [a for a in items or [] if isinstance(a, dict) and a.get("type") in proposals.KINDS]


def _fact_text(fait: str) -> str:
    return esprit.one_line(esprit.strip_invisible(fait)[0], esprit.FACT_MAX)


def voir(settings: Settings, video_id: str) -> dict[str, Any]:
    """The pending proposals of a video and the lot's fingerprint (read only)."""
    items = _proposed(video_id)
    source = source_of(video_id)
    with TodoStore(settings.todo_db) as store:
        titles = {t.title for t in store.list(status=None, limit=1_000_000) if t.source == source}
    try:
        facts = {r.get("texte") for r in esprit.proposals(settings, status=None) if r.get("source") == fact_source(video_id)}
    except (OSError, UltronHubError):
        facts = set()
    filed = {r.get("empreinte") for r in savoir.ledger(settings)}
    actions, faites, ignorees = [], [], []
    for a in items:
        kind, n, titre = a["type"], a.get("n"), str(a.get("titre") or "")
        line = {"n": n, "type": kind, "titre": titre, "texte": f"{ICONES[kind]} {n}. {titre}"}
        if kind == "fiche":
            try:
                draft = savoir.check_draft(Path(str(a.get("path") or "")), settings)
            except (OSError, UltronHubError) as exc:
                ignorees.append({**line, "raison": str(exc)[:200]})
                continue
            if draft["empreinte"] in filed:
                faites.append(line)
                continue
            actions.append({**line, "path": str(a.get("path")), "empreinte": draft["empreinte"]})
        elif kind in ("todo", "recherche"):
            if titre[:TITLE_MAX] in titles:
                faites.append(line)
                continue
            actions.append({**line, "notes": str(a.get("notes") or ""), "clair": str(a.get("clair") or ""),
                            "categorie": a.get("categorie"), "chantier": a.get("chantier")})
        else:
            fait = str(a.get("fait") or "")
            if _fact_text(fait) in facts:
                faites.append(line)
                continue
            actions.append({**line, "fait": fait})
    return {"id": video_id, "actions": actions, "faites": faites, "ignorees": ignorees,
            "empreinte": empreinte(video_id, actions)}


def empreinte(video_id: str, actions: list[dict[str, Any]]) -> str | None:
    if not actions:
        return None
    lines = [video_id] + [json.dumps({k: v for k, v in a.items() if k != "texte"}, sort_keys=True, ensure_ascii=False)
                          for a in actions]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def proof_used(settings: Settings, proof: str) -> bool:
    try:
        lines = ledger_path(settings).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        try:
            if json.loads(line).get("proof") == proof:
                return True
        except ValueError:
            continue
    return False


def _refuse(reason: str) -> None:
    raise InvalidRequestError(f"❌ Rangement refusé : {reason}\n\nRien n'a été fait. "
                              "À faire : rouvre les propositions de la vidéo pour voir l'état à jour.")


def _fiche(settings: Settings, a: dict[str, Any], trace: dict[str, Any]) -> str:
    row = savoir.rank(settings, Path(a["path"]), trace, expected=a["empreinte"])
    return f"fiche rangée ({row['id']})"


def _todo(settings: Settings, video_id: str, a: dict[str, Any]) -> str:
    from ultron_hub.todo import commands as todo_commands  # the same code (and checks) as `todo add`
    args = argparse.Namespace(todo_command="add", title=a["titre"][:TITLE_MAX], priority="normal",
                              category=a.get("categorie") or None, due=None, notes=a["notes"],
                              source=source_of(video_id), nature=None, chantier=a.get("chantier") or None,
                              clair=a["clair"] or None, title_mode=None)
    out = todo_commands.handle(args, settings)
    if out.get("status") != "ok":
        raise InvalidRequestError(out.get("error") or out.get("summary") or "todo add a échoué")
    return f"todo #{out['data']['id']} créée"


def _profil(settings: Settings, video_id: str, a: dict[str, Any]) -> str:
    from ultron_hub.esprit import commands as esprit_commands  # the same code as `profil propose`
    row = esprit_commands.propose_fact(settings, a["fait"], fact_source(video_id))
    return f"proposé (#{row['id']}), à valider dans Profil"


def appliquer(settings: Settings, video_id: str, expected: str,
              authorize: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    if EMPREINTE_RE.fullmatch(expected or "") is None:
        raise InvalidRequestError("empreinte invalide (16 caractères hexadécimaux)")
    folder = collector.video_dir(video_id)
    if not folder.is_dir():
        raise InvalidRequestError("vidéo inconnue")
    with open(folder / "lot.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two presses at once: the second sees the lot filed and another fingerprint
        view = voir(settings, video_id)
        if not view["actions"]:
            _refuse("aucune proposition en attente (déjà rangées ?)")
        if view["empreinte"] != expected:
            _refuse("les propositions ont changé depuis leur affichage")
        by = authorize(view)
        if by.get("proof") and proof_used(settings, by["proof"]):
            raise closing.CloseRefused("jeton du rangement déjà utilisé")
        ledger_path(settings).parent.mkdir(parents=True, exist_ok=True)
        with ledger_path(settings).open("a", encoding="utf-8") as fh:  # spent before anything runs
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "id": video_id,
                                 "by": by.get("by"), "proof": by.get("proof"), "empreinte": expected,
                                 "actions": [{"n": a["n"], "type": a["type"], "titre": a["titre"]} for a in view["actions"]]},
                                ensure_ascii=False) + "\n")
        # one proof for several fiches: the notebook's own replay check sees `lot_proof`, never the same `proof` twice
        trace = {k: v for k, v in by.items() if k != "proof"} | {"via": ACTION, "lot_proof": by.get("proof"),
                                                                 "empreinte_lot": expected}
        bilan = []
        for a in view["actions"]:
            try:
                if a["type"] == "fiche":
                    detail = _fiche(settings, a, trace)
                elif a["type"] in ("todo", "recherche"):
                    detail = _todo(settings, video_id, a)
                else:
                    detail = _profil(settings, video_id, a)
                bilan.append({"action": a["texte"], "ok": True, "detail": detail})
            except Exception as exc:  # noqa: BLE001 - reported on its line, the other proposals still run
                bilan.append({"action": a["texte"], "ok": False, "detail": str(exc)[:300]})
    ok = sum(b["ok"] for b in bilan)
    esprit.record(settings, "ultron-hub", "video", f"Propositions de la vidéo rangées en lot ({by.get('by')}) : {ok}/{len(bilan)}")
    return {"id": video_id, "empreinte": expected, "bilan": bilan, "text": texte(bilan)}


def texte(bilan: list[dict[str, Any]]) -> str:
    lines = [f"📥 Propositions rangées : {sum(b['ok'] for b in bilan)}/{len(bilan)}"]
    lines += [f"{'✅' if b['ok'] else '❌'} {b['action']} : {b['detail']}" for b in bilan]
    return "\n".join(lines)


def handle(args: argparse.Namespace, settings: Settings, envelope: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """`video lot <id>` and `video ranger-lot <id> --empreinte X` (the id is checked by the caller)."""
    command = f"video.{args.video_command}"
    if args.video_command == "lot":
        try:
            data = voir(settings, args.id)
        except UltronHubError as exc:
            return envelope(status="error", summary="propositions illisibles", error=str(exc), command=command)
        n = len(data["actions"])
        return envelope(summary=f"{n} proposition(s) à ranger" if n else "rien à ranger", data=data, command=command)

    def authorize(view: dict[str, Any]) -> dict[str, Any]:
        what = "Ranger :\n" + "\n".join(f"- {a['texte']}" for a in view["actions"]) + "\n?"
        return closing.authorize(ACTION, f"{args.id}:{args.empreinte}", human=args.human, what=what,
                                 used=lambda proof: proof_used(settings, proof))
    data = appliquer(settings, args.id, args.empreinte, authorize)
    failed = [b for b in data["bilan"] if not b["ok"]]
    return envelope(status="error" if failed else "ok", summary=data["text"].splitlines()[0], data=data, command=command,
                    error="; ".join(f"{b['action']} : {b['detail']}" for b in failed) or None)
