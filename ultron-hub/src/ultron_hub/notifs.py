"""History of the notifications Ultron sent (todo #276): one JSON line each, newest last, in `var/notifs/historique.jsonl`.

`noter` is called by `envoi.envoyer` after Telegram and never raises. Title and body come from `push.summary` (paths and
secrets masked, MEDIA lines dropped); `complet` is the whole text after the title, cleaned the same way. Todo #289 (the « fil »): each
`MEDIA:<path>` attachment is copied to `var/notifs/pj/` (hard-linked instead when the source is a 0600 file under `var/`, same filesystem; timestamped name, 0600, size cap, never a secret file) and served
by name only (`PJ_RE`, `pj_path`). Rows written before that have no `pj`/`complet` and stay readable. Read state (`vu.json`): a notification is read when `at <= vu_jusqu_a` (« tout lu ») or when its `at` is in `lues`
(todo #301: opened one by one, purged with the old notifications). A row may carry a `cible` (todo or session, derived from
the text by `noter` when not given); without one the page shows no « Ouvrir » button.
Todo #343: each row carries a `genre` (GENRES), given by the sender (`send --genre`) or deduced by `deduire_genre`;
older rows get it deduced when read. Todo #344: `liste` also returns a `titre` that says something (`titre_sens`: the
first sentence of the message when the stored title is empty or « Ultron ») and a short `resume`; computed on read, after
the masking. Todo #348: a successful « Le bot redémarré (fusion de #N) » row is folded into the merge row before it
(`etiquettes: ["bot redémarré"]`, see `replier_redemarrages`); a folded row is read when its merge row is. CLI: `ultron-hub notifs liste [--n N]`, `notifs vu` and `notifs lire <at>`.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultron_hub.common.logging import get_logger

log = get_logger(__name__)

HISTORY_FILE = "historique.jsonl"
SEEN_FILE = "vu.json"
KEEP = 300
ROTATE_AT = 400
N_DEFAULT = 50
N_MAX = 200
PJ_DIR = "pj"
PJ_MAX_BYTES = 20 * 1024 * 1024
PJ_MAX_PER_MESSAGE = 5
COMPLET_MAX = 4000
PJ_RE = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{6}-[A-Za-z0-9._-]{1,60}")
IMAGES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
# Todo #373: what the in-page reader opens. Text is served as text/plain (never rendered by the browser), html as a page
# that only runs sandboxed (server._pj), audio inline with Range; anything else stays a download.
TEXTES = {"md": "text/plain; charset=utf-8", "txt": "text/plain; charset=utf-8"}
HTMLS = {"html": "text/html; charset=utf-8", "htm": "text/html; charset=utf-8"}
AUDIOS = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav", "ogg": "audio/ogg", "oga": "audio/ogg",
          "opus": "audio/ogg", "flac": "audio/flac"}
PJ_URL = "/api/pj/"
ORIGINE_TOI = "toi"  # todo #353: marks the entries made of the user's own sends
GENRES = ("feu-vert", "question", "alerte", "fait", "bilan", "fichier")
_FEU_VERT_RE = re.compile(r"prête|🔀", re.IGNORECASE)
_QUESTION_RE = re.compile(r"tu décides", re.IGNORECASE)
_ALERTE_RE = re.compile(r"🚨|❌")
_BILAN_RE = re.compile(r"bilan|en clair", re.IGNORECASE)
TITRE_MAX = 60
RESUME_MAX = 140
_TETE_RE = re.compile(r"^(?:#[0-9]+\b[\s:\-–—]*|Ultron\s*:\s*|[^\w#«\"'(\[]+)+")  # leading emoji, symbols, « #340 », « Ultron : »
_FIN_PHRASE_RE = re.compile(r"[.!?](?=\s|$)")
AT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\+00:00)?")
TODO_REF_RE = re.compile(r"(?<![\w#])#([1-9][0-9]{0,8})(?!\w)")
SESSION_REF_RE = re.compile(r"(?<![\w-])(ultron-(?:[a-z0-9][a-z0-9-]{0,39}-)?[0-9]{2}-[0-9]{2}-[0-9]{2}h[0-9]{2}(?:-[0-9]{1,3})?)(?![\w-])")
# Never copied into the fil, whatever a MEDIA line says (the fil is readable from the phone).
SECRET_PARTS = {".ssh", ".gnupg", ".aws", ".sudo_pass", ".env", "auth.json", ".credentials.json", "close.key", ".netrc"}
SECRET_TREES = (("~", ".config", "gh"), ("~", ".hermes"), ("~", ".config", "ultron-hub"))


def dossier(settings=None) -> Path:
    from ultron_hub.common.settings import project_root
    var = getattr(settings, "var_dir", None) or (project_root() / "var")
    return Path(var) / "notifs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("at"), str):
            rows.append(row)
    return rows


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _prepare(settings) -> Path:
    base = dossier(settings)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    return base


def pj_dir(settings=None) -> Path:
    return dossier(settings) / PJ_DIR


def genre(nom: str) -> str:
    ext = nom.rsplit(".", 1)[-1].lower()
    for g, table in (("image", IMAGES), ("texte", TEXTES), ("audio", AUDIOS), ("html", HTMLS)):
        if ext in table:
            return g
    return "fichier"


def pj_path(nom: str, settings=None) -> Path | None:
    """The file of an attachment of the fil, from its stored name only: never a free path. None when refused."""
    if not isinstance(nom, str) or not PJ_RE.fullmatch(nom):
        return None
    path = pj_dir(settings) / nom
    try:
        if path.is_symlink() or not path.is_file():
            return None
    except OSError:
        return None
    return path


def content_type(nom: str) -> str:
    ext = nom.rsplit(".", 1)[-1].lower()
    return (IMAGES | TEXTES | HTMLS | AUDIOS).get(ext, "application/octet-stream")


def _secret(path: Path) -> bool:
    home = Path.home()
    if any(part in SECRET_PARTS for part in path.parts):
        return True
    for tree in SECRET_TREES:
        root = home.joinpath(*tree[1:])
        if path == root or root in path.parents:
            return True
    return False


def _lier(src: Path, st: os.stat_result, base: Path, dest: Path) -> bool:
    """Hard-link `src` as `dest` instead of copying (todo #461): only a regular file under `var/` (the parent of
    the notifs folder), on the same filesystem, already 0600 or stricter (a link shares the mode, never chmod the source)."""
    try:
        if base.parent not in src.parents or st.st_dev != base.stat().st_dev or st.st_mode & 0o077:
            return False
        os.link(src, dest, follow_symlinks=False)
        now = os.stat(dest, follow_symlinks=False)
        if (now.st_ino, now.st_dev) != (st.st_ino, st.st_dev):  # swapped under us: undo, copy instead
            os.unlink(dest)
            return False
        return True
    except OSError:
        return False


def _copier(source: str, base: Path, stamp: str) -> dict[str, str] | None:
    """Keep one attachment in `pj/` (hard link of a private file under var/, else a copy); None when it is refused (missing, not a file, too big, a secret)."""
    try:
        src = Path(source).expanduser().resolve(strict=True)
        if _secret(src) or base in src.parents:
            return None
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fin:
            st = os.fstat(fin.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > PJ_MAX_BYTES:
                return None
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.name).lstrip(".")[-60:] or "fichier"
            nom = f"{stamp}-{secrets.token_hex(3)}-{safe}"
            pj = base / PJ_DIR
            pj.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not _lier(src, st, base, pj / nom):
                out = os.open(pj / nom, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(out, "wb") as fout:
                    shutil.copyfileobj(fin, fout, 1 << 20)
        return {"nom": nom, "orig": src.name, "genre": genre(nom)}
    except (OSError, ValueError, RuntimeError):
        return None


def _complet(texte: str) -> str:
    from ultron_hub.dashboard import push
    lignes = [push._clean(ln) for ln in texte.splitlines()]  # noqa: SLF001 - same masking as the summary
    lignes = [ln for ln in lignes if ln]
    out = "\n".join(lignes[1:] if len(lignes) > 1 else lignes)  # the title is shown apart (as in push.summary)
    return out if len(out) <= COMPLET_MAX else out[:COMPLET_MAX - 1].rstrip() + "…"


def _menage_pj(base: Path, rows: list[dict]) -> None:
    gardees = {p.get("nom") for r in rows for p in r.get("pj", []) if isinstance(p, dict)}
    try:
        for f in (base / PJ_DIR).iterdir():
            if f.name not in gardees and not f.name.endswith("-lecture.wav"):  # readings: montage.lecture.clean()
                f.unlink(missing_ok=True)
    except OSError:
        pass


def cible_depuis(*textes: str) -> dict[str, str] | None:
    """The precise target a text names: « #<id> » = a todo, an `ultron-…` session name = a session. None when it names none."""
    for t in textes:
        m = TODO_REF_RE.search(t or "")
        if m:
            return {"genre": "todo", "ref": m.group(1)}
    for t in textes:
        m = SESSION_REF_RE.search(t or "")
        if m:
            return {"genre": "session", "ref": m.group(1)}
    return None


def _cible_publique(cible: Any) -> dict[str, str] | None:
    """A stored target for the page, with the words of its button; anything malformed is dropped."""
    if not isinstance(cible, dict) or not isinstance(cible.get("ref"), str):
        return None
    ref, g = cible["ref"], cible.get("genre")
    if g == "todo" and re.fullmatch(r"[1-9][0-9]{0,8}", ref):
        return {"genre": g, "ref": ref, "libelle": f"Voir la todo #{ref}"}
    if g == "session" and SESSION_REF_RE.fullmatch(ref):
        return {"genre": g, "ref": ref, "libelle": f"Ouvrir la session {ref}"}
    return None


def deduire_genre(texte: str) -> str:
    """Pure script (no AI): the genre of a notification text, first match wins; « fait » when nothing matches."""
    texte = texte or ""
    sans_media = "\n".join(ln for ln in texte.splitlines() if not ln.strip().startswith("MEDIA:")).strip()
    if _FEU_VERT_RE.search(sans_media):
        return "feu-vert"
    if _QUESTION_RE.search(sans_media) or sans_media.endswith("?"):
        return "question"
    if _ALERTE_RE.search(sans_media):
        return "alerte"
    if "MEDIA:" in texte:
        return "fichier"
    if _BILAN_RE.search(sans_media):
        return "bilan"
    return "fait"


GENRES_FIL = ("fait", "bilan", "alerte", "fichier")  # the filters of the Journal (todo #346)
_FIL_ALERTE_RE = re.compile(r"panne|échec|echec", re.IGNORECASE)
_FIL_BILAN_RE = re.compile(r"bilan|\bpoint\b|passation", re.IGNORECASE)


def genre_fil(titre: str, pj: Any) -> str:
    """Pure script: the Journal filter of a notification (todo #346). Files first, then alerts, then bilans; « fait » otherwise."""
    titre = str(titre or "").strip()
    if pj:
        return "fichier"
    if titre.startswith(("⚠", "🔴", "❌", "🚨")) or _FIL_ALERTE_RE.search(titre):
        return "alerte"
    if _FIL_BILAN_RE.search(titre):
        return "bilan"
    return "fait"


def _lignes_sens(texte: str) -> list[str]:
    """Lines of a message for a title or summary: MEDIA lines dropped, paths and secrets masked, leading emoji/#id removed."""
    from ultron_hub.dashboard import push
    out = []
    for ln in str(texte or "").splitlines():
        if ln.strip().startswith("MEDIA:"):
            continue
        ln = _TETE_RE.sub("", push._clean(ln)).strip()  # noqa: SLF001 - same masking as the summary
        if ln:
            out.append(ln)
    return out


def _couper(texte: str, taille: int) -> str:
    """Cut at a word boundary, « … » appended, at most `taille` characters."""
    if len(texte) <= taille:
        return texte
    coupe = texte[:taille - 1]
    if " " in coupe and texte[taille - 1] != " ":
        coupe = coupe.rsplit(" ", 1)[0]
    return coupe.rstrip(" ,;:-–—") + "…"


def _sens(titre: str, texte: str) -> tuple[str, str]:
    """(title, summary): the stored title is kept unless empty or « Ultron »; then the first sentence of the text is the
    title and the summary is what follows it. With a kept title the summary starts at the text itself."""
    lignes = _lignes_sens(texte)
    garde = " ".join(str(titre or "").split())  # a real title is kept as stored (already masked), emoji included
    garde_titre = garde if garde and garde.lower() != "ultron" else ""
    reste = ""
    if garde_titre:
        titre_final = _couper(garde_titre, TITRE_MAX)
        reste = " ".join(lignes)
    elif lignes:
        premiere = lignes[0]
        m = _FIN_PHRASE_RE.search(premiere)
        phrase = premiere[:m.start()] if m else premiere
        titre_final = _couper(phrase.strip() or premiere, TITRE_MAX)
        reste = " ".join([premiere[m.end():].strip() if m else ""] + lignes[1:]).strip()
        if len(phrase) > TITRE_MAX:  # the title was cut: the summary starts from the whole line instead
            reste = " ".join([premiere] + lignes[1:])
    else:
        titre_final = str(titre or "").strip()
    return titre_final, _couper(" ".join(reste.split()), RESUME_MAX)


def titre_sens(titre: str, texte: str) -> str:
    """Pure script: a title that says something. Kept as is unless empty or « Ultron »."""
    return _sens(titre, texte)[0]


def resume_sens(titre: str, texte: str) -> str:
    """Pure script: about two lines (RESUME_MAX characters) taken after the title sentence."""
    return _sens(titre, texte)[1]


def noter(texte: str, onglet: str, telegram_ok: bool, settings=None, now: str | None = None,
          cible: dict[str, str] | None = None, genre: str | None = None, origine: str | None = None) -> bool:
    """Append one notification to the history. Never raises (False when nothing was written).

    `origine` (todo #353): "toi" = the entry records something the USER sent (📎 joindre); a data field, never deduced from the text."""
    try:
        from ultron_hub.dashboard import push
        from ultron_hub.envoi import separer
        titre, corps = push.summary(texte)
        row = {"at": now or _now(), "titre": titre, "corps": corps, "onglet": str(onglet), "telegram": bool(telegram_ok),
               "genre": genre if genre in GENRES else deduire_genre(texte)}
        if origine == ORIGINE_TOI:
            row["origine"] = ORIGINE_TOI
        base = _prepare(settings)
        sans_media, medias = separer(texte)
        row["complet"] = _complet(sans_media)
        cible = cible or cible_depuis(titre, corps, row["complet"])
        if _cible_publique(cible):
            row["cible"] = cible
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        pj = [c for c in (_copier(m, base, stamp) for m in medias[:PJ_MAX_PER_MESSAGE]) if c]
        if pj:
            row["pj"] = pj
        path = base / HISTORY_FILE
        with open(base / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            os.chmod(path, 0o600)
            rows = _read(path)
            if len(rows) > ROTATE_AT:
                rows = rows[-KEEP:]
                _write_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
                _menage_pj(base, rows)
        return True
    except Exception as exc:  # noqa: BLE001 - the history never breaks a send
        log.warning("notification non gardée : %s", type(exc).__name__)
        return False


def _seen(base: Path) -> tuple[str, set[str]]:
    """(date up to which everything is read, set of the `at` read one by one)."""
    try:
        data = json.loads((base / SEEN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", set()
    if not isinstance(data, dict):
        return "", set()
    value, lues = data.get("vu_jusqu_a"), data.get("lues")
    return (value if isinstance(value, str) else ""), {a for a in lues if isinstance(a, str)} if isinstance(lues, list) else set()


def _seen_until(base: Path) -> str:
    return _seen(base)[0]


def _pj_publiques(pj: Any) -> list[dict[str, Any]]:
    """Attachments of a row for the page: name, original name, kind, URL, size in bytes (todo #350; 0 when the file is gone). Anything malformed is dropped."""
    out = []
    for p in pj if isinstance(pj, list) else []:
        nom = p.get("nom") if isinstance(p, dict) else None
        if isinstance(nom, str) and PJ_RE.fullmatch(nom):
            kept = pj_path(nom)
            try:
                taille = kept.stat().st_size if kept else 0
            except OSError:
                taille = 0
            out.append({"nom": nom, "orig": str(p.get("orig") or nom), "genre": genre(nom), "url": PJ_URL + nom, "taille": taille})
    return out


def _texte_ancien(r: dict) -> str:
    """Text to deduce the genre of a row written without one (attachments count as MEDIA lines)."""
    media = "".join(f"\nMEDIA:{p.get('nom')}" for p in r.get("pj") or [] if isinstance(p, dict))
    return f"{r.get('titre', '')}\n{r.get('complet') or r.get('corps', '')}{media}"


REDEMARRAGE_RE = re.compile(r"redémarré\s*\(fusion de ((?:#\d+[,\s]*(?:et\s+)?)+)\)", re.I)
ETIQUETTE_REDEMARRAGE = "bot redémarré"
FENETRE_FUSION_S = 30 * 60
FENETRE_CITANT_S = 24 * 3600


def redemarrage_todos(r: dict) -> list[str] | None:
    """Pure: the todo numbers cited by a successful bot-restart row, or None when the row is not one (a failure never is)."""
    texte = f"{r.get('titre', '')}\n{r.get('complet') or r.get('corps', '')}"
    if "❌" in texte or re.search(r"échec|echec", texte, re.I):
        return None
    m = REDEMARRAGE_RE.search(texte)
    return re.findall(r"#(\d+)", m.group(1)) if m else None


def _epoch(at: str) -> float:
    try:
        return datetime.fromisoformat(at).timestamp()
    except ValueError:
        return 0.0


def replier_redemarrages(rows: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Pure. `rows` oldest first. Returns (rows without the folded restarts, {restart at: merge at}).
    A restart goes to the closest earlier merge row citing one of its todos, else the closest merge row within 30 min;
    none found = the restart stays a row."""
    gardes, replies = [], {}
    for r in rows:
        refs = redemarrage_todos(r)
        cible = None
        if refs is not None:
            t = _epoch(str(r.get("at", "")))
            fusions = [f for f in gardes if redemarrage_todos(f) is None and "fusion" in
                       f"{f.get('titre', '')}\n{f.get('complet') or f.get('corps', '')}".lower()
                       and 0 <= t - _epoch(str(f.get("at", ""))) <= FENETRE_CITANT_S]
            citant = [f for f in fusions if any(re.search(rf"#{x}\b", f"{f.get('titre', '')}\n{f.get('complet') or f.get('corps', '')}")
                                                for x in refs)]
            proches = [f for f in fusions if t - _epoch(str(f.get("at", ""))) <= FENETRE_FUSION_S]
            cible = (citant or proches or [None])[-1]
        if cible is None:
            gardes.append(r)
        else:
            replies[str(r.get("at"))] = str(cible.get("at"))
    return gardes, replies


_TECH_RE = re.compile(r"(?<![\w/-])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w-])|\brc=-?\d+|\btodo-\d+/|\b\d+ (?:passed|failed)\b|(?:^|\s)/(?:home|tmp|var|etc|usr)/\S+")


def separer_technique(texte: str) -> tuple[str, list[str]]:
    """Pure. todo #351: (readable text, technical lines). A line with a commit sha (7-40 hex), `rc=N`, a `todo-<id>/…` branch,
    « N passed » or an absolute path goes to the technical list; if nothing readable remains, the text is kept whole."""
    lignes = str(texte).split("\n")
    tech = [l.strip() for l in lignes if _TECH_RE.search(l)]
    reste = [l for l in lignes if not _TECH_RE.search(l)]
    if not tech or not "".join(reste).strip():
        return str(texte), []
    return "\n".join(reste).strip("\n"), tech


def liste(n: int = N_DEFAULT, settings=None) -> dict[str, Any]:
    """The `n` most recent notifications, newest first, and the number of unread ones among all kept."""
    base = dossier(settings)
    rows = _read(base / HISTORY_FILE)
    seen, lues = _seen(base)
    rows, replies = replier_redemarrages(rows)
    out = [{"at": r["at"], "titre": _sens(str(r.get("titre", "")), str(r.get("complet") or r.get("corps", "")))[0],
            "resume": _sens(str(r.get("titre", "")), str(r.get("complet") or r.get("corps", "")))[1], "corps": str(r.get("corps", "")),
            "complet": separer_technique(str(r.get("complet") or r.get("corps", "")))[0],
            "details": separer_technique(str(r.get("complet") or r.get("corps", "")))[1], "pj": _pj_publiques(r.get("pj")),
            "onglet": str(r.get("onglet", "briefing")), "lue": r["at"] <= seen or r["at"] in lues,
            "cible": _cible_publique(r.get("cible") or cible_depuis(r.get("titre", ""), r.get("corps", ""))),
            "genre": r["genre"] if r.get("genre") in GENRES else deduire_genre(_texte_ancien(r)),
            "origine": ORIGINE_TOI if r.get("origine") == ORIGINE_TOI else "ultron"} for r in reversed(rows)]
    for r, raw in zip(out, reversed(rows)):  # the raw title (often « Ultron ») and the shown one both count
        r["genre_fil"] = genre_fil(str(raw.get("titre", "")), r["pj"])
        if r["genre_fil"] == "fait":
            r["genre_fil"] = genre_fil(r["titre"], r["pj"])
    for r in out:  # todo #347: the grouping key of the page (the id of the todo aimed at, else null); the page decides nothing
        c = r["cible"]
        r["todo"] = c["ref"] if c and c["genre"] == "todo" else None
    portent = set(replies.values())  # a folded restart counts as read with its merge row: no hidden unread
    for r in out:
        r["etiquettes"] = [ETIQUETTE_REDEMARRAGE] if r["at"] in portent else []
    return {"notifs": out[:n], "non_lues": sum(1 for r in out if not r["lue"])}


def vu(settings=None) -> dict[str, Any]:
    """Mark everything read: the date of the most recent notification goes to `vu.json`."""
    base = _prepare(settings)
    rows = _read(base / HISTORY_FILE)
    latest = max((r["at"] for r in rows), default=_seen_until(base))
    _write_atomic(base / SEEN_FILE, json.dumps({"vu_jusqu_a": latest, "lues": []}))
    return {"vu_jusqu_a": latest}


def lire(at: str, settings=None) -> dict[str, Any]:
    """Mark ONE notification read (todo #301). `at` must be a date of the fil; the set keeps only dates still in the history."""
    if not isinstance(at, str) or not AT_RE.fullmatch(at):
        raise ValueError("format de date refusé")
    base = _prepare(settings)
    with open(base / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ats = {r["at"] for r in _read(base / HISTORY_FILE)}
        seen, lues = _seen(base)
        if at in ats and at > seen:
            lues.add(at)
        lues &= ats
        _write_atomic(base / SEEN_FILE, json.dumps({"vu_jusqu_a": seen, "lues": sorted(lues)}))
    return {"at": at, "lue": at in lues or at <= seen}


# -- CLI -----------------------------------------------------------------------------------------------------------------
def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None, error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"notifs.{command}", "summary": summary, "data": data, "error": error}


def _n(value: str) -> int:
    n = int(value)
    if not 1 <= n <= N_MAX:
        raise argparse.ArgumentTypeError(f"--n must be between 1 and {N_MAX}")
    return n


def _at(value: str) -> str:
    if not AT_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("date refusée")
    return value


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    grp = sub.add_parser("notifs", help="history of the notifications sent by Ultron")
    cmds = grp.add_subparsers(dest="notifs_command", required=True, parser_class=parser_class)
    one = cmds.add_parser("liste", help="recent notifications, newest first, read or not")
    one.add_argument("--n", type=_n, default=N_DEFAULT, help=f"how many (1 to {N_MAX}, default {N_DEFAULT})")
    cmds.add_parser("vu", help="mark every notification as read")
    one = cmds.add_parser("lire", help="mark one notification as read")
    one.add_argument("at", type=_at, help="its date, as in `liste` (2026-10-20T13:32:05+00:00)")


def handle(args: argparse.Namespace, settings) -> dict[str, Any]:
    cmd = args.notifs_command
    if cmd == "liste":
        data = liste(args.n, settings)
        return envelope(cmd, summary=f"{len(data['notifs'])} notification(s), {data['non_lues']} non lue(s)", data=data)
    if cmd == "lire":
        return envelope(cmd, summary="notification marquée lue", data=lire(args.at, settings))
    data = vu(settings)
    return envelope(cmd, summary="notifications marquées lues", data=data)
