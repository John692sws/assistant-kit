"""`ultron-hub video analyse <id>` (todos #92, #441): the analysis of a collected video, by the user's intention.

The collection (todo #91, var/video/<id>/collected.json + images/) is read by Claude Opus (task `video-analyse`) in
batches of BATCH_FRAMES images, each batch with the transcript lines and the sound measures of its time range. Every
batch answers with a NEUTRAL timestamped timeline (said / shown / sound) and what it could not read. One last call,
without images, writes the overview and the digest, then GUESSES what the user is looking for (todo #441: the chantier
the video was sent from, then his sentence, then the subject) and fills the grid of that case (`cas`: inspiration for
Ultron, info, methode, donnees) with, as data, the intention, the active chantiers and an inventory of what Ultron has
(skills, docs of ultron-hub, titles of the carnet) so that « face à Ultron » cites a real file or says « non vérifié ».
Todo #443 (right on any subject): every batch also lists what its images SHOW of the know-how (`technique`, with the
moment), given to the synthesis as its own block; « comment il a fait » is split into what is said and what is shown,
a gap between the two is a point to check (`ecarts`), and the synthesis reads the video through the « But » of the
fiche of the chantier it was sent from (`lentille`).
Its `rangement` (fiches and todos) is made ready in one batch through `proposals.build_actions` and kept in
actions.json, like `video actions` (the ✅ stay one per proposal).

Outputs in var/video/<id>/ (0600): `analyse.md` (guess, grid, overview, moments, then the chronology as an annex, sent
as an attachment), `analyse.json` (what the next step reads, see `load_analysis`), `actions.json`. The text is short
(guess + the essentials of the grid, 20 lines at most).

Everything from the video (transcript, title, description, on-screen text) is untrusted data: it sits between tags
with `<` `>` neutralised, and Claude's answers are cleaned (see video/text.py) (control characters, em dashes,
sentences that look like an instruction to an AI are dropped and counted as a gap). A failed batch never fails the
analysis: its range becomes a gap and its transcript is kept. All batches failing is an error.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.core.job import JobRequest, new_job_id
from ultron_hub.core.runner import run_job
from ultron_hub import proposals
from ultron_hub.esprit import savoir
from ultron_hub.esprit import store as esprit
from ultron_hub.video import collect as collector
from ultron_hub.video import commands
from ultron_hub.video.text import CTRL_RE, cut, cut_prose, neutral, timed_link

log = get_logger("video")

TASK = "video-analyse"            # the synthesis (no image): opus
TASK_LOT = "video-analyse-lot"    # the image batches: sonnet (todo #470), same prompt and schema (symlinks)
APPLICABLE = ("oui", "en_partie", "non")
APPLICABLE_TEXTE = {"oui": "applicable chez nous", "en_partie": "applicable en partie", "non": "pas applicable chez nous"}
BATCH_FRAMES = 20
NO_FRAME_CHUNK_S = 180        # a video whose images failed is still read, by time chunks
DEADLINE_S = 30 * 60          # no new batch after this (the rest becomes a gap)
DIGEST_MIN, DIGEST_MAX = 3, 10   # message = title, meta, overview, digest, gaps, attachment: 15 lines at most
FIELD_CHARS = 700
SYNTH_CHARS = 120_000         # chronology given to the last call; fields are shortened above it
TRANSCRIPT_CHARS = 30_000
ANALYSE_MD = "analyse.md"
ANALYSE_JSON = "analyse.json"
FRAME_RE = re.compile(r"[0-9]{3}_t[0-9]{5}\.jpg")
# The grid of the synthesis (todo #441)
CAS = ("inspiration", "info", "methode", "donnees")
CAS_TITRES = {"inspiration": "Inspiration pour Ultron", "info": "Info : faits et opinions", "methode": "Méthode",
              "donnees": "Données à rechercher"}
ETATS = ("existe", "en_partie", "non")
ETAT_TEXTE = {"existe": "existe", "en_partie": "en partie", "non": "non"}
RANGEMENT_KINDS = ("fiche", "todo")
NOT_CHECKED = "non vérifié"
GRID_CHARS = 400
INTENTION_CHARS = 1000
CHANTIERS_CHARS = 3000
CHANTIERS_MAX = 40
SKILLS_CHARS, DOCS_CHARS, SAVOIR_CHARS = 3500, 2500, 3500
SKILLS_DIR = Path.home() / ".claude" / "skills"
MESSAGE_MAX_LINES = 20
# Know-how shown by the images, per batch then for the whole video (todo #443)
TECHNIQUE_ITEMS, TECHNIQUE_TOTAL, TECHNIQUE_CHARS, TECHNIQUE_BLOCK_CHARS = 12, 120, 300, 15_000
# The lens: the « But » (or equivalent) of the fiche of the chantier the video was sent from
LENS_CHARS = 1500
LENS_HEADINGS = ("but", "objectif", "objectifs", "vision", "besoin")
NO_LENS = "(aucune)"
CHANTIER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class AnalyseError(UltronHubError):
    """A failure explained to the user in one sentence."""


def clean(value: Any, limit: int = FIELD_CHARS) -> tuple[str, int]:
    """Text written by Claude from untrusted material: (one paragraph, sentences dropped as instructions)."""
    text = " ".join(CTRL_RE.sub(" ", str(value or "")).replace("—", ",").replace("–", ",").split())
    kept, removed = savoir.filter_instructions(text.replace(" ,", ","))
    return " ".join(kept.split())[:limit], removed


def _number(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return round(min(max(number, low), high), 1)


def duration_of(collected: dict[str, Any]) -> float:
    for value in (collected.get("duration_s"), (collected.get("metadata") or {}).get("duration")):
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    seen = [f.get("t") for f in collected.get("frames") or [] if isinstance(f, dict)]
    seen += [s.get("end") for s in collected.get("transcript_segments") or [] if isinstance(s, dict)]
    return float(max([x for x in seen if isinstance(x, (int, float))] or [0])) + 10.0


def valid_frames(collected: dict[str, Any], images: Path) -> list[dict[str, Any]]:
    """The frames whose file really is one of ours (generated name, regular file, not a link), by time."""
    out = []
    for f in collected.get("frames") or []:
        name = f.get("file") if isinstance(f, dict) else None
        if isinstance(name, str) and FRAME_RE.fullmatch(name) and isinstance(f.get("t"), (int, float)):
            path = images / name
            if path.is_file() and not path.is_symlink():
                out.append({"file": name, "t": float(f["t"]), "kind": str(f.get("kind") or "")})
    return sorted(out, key=lambda f: f["t"])


def plan_batches(frames: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    """Consecutive time ranges, each with at most BATCH_FRAMES images; together they cover [0, duration]."""
    if not frames:
        count = max(1, int(-(-duration // NO_FRAME_CHUNK_S)))
        return [{"start": i * NO_FRAME_CHUNK_S, "end": min(duration, (i + 1) * NO_FRAME_CHUNK_S), "frames": []}
                for i in range(count)]
    groups = [frames[i:i + BATCH_FRAMES] for i in range(0, len(frames), BATCH_FRAMES)]
    batches = []
    for i, group in enumerate(groups):
        start = 0.0 if i == 0 else group[0]["t"]
        end = groups[i + 1][0]["t"] if i + 1 < len(groups) else max(duration, group[-1]["t"])
        batches.append({"start": start, "end": end, "frames": group})
    return batches


def _overlaps(item: Any, start: float, end: float) -> bool:
    return isinstance(item, dict) and item.get("start", 0) < end and item.get("end", 0) > start


def sound_slice(audio: Any, start: float, end: float) -> str:
    """The measures of the script for one range, as JSON (Claude hears nothing itself)."""
    if not isinstance(audio, dict):
        return "Pas de piste audio exploitable (vidéo muette ou analyse du son impossible)."
    windows = [w for w in audio.get("volume_per_window") or [] if isinstance(w, dict)
               and start - (audio.get("window_s") or 10) < w.get("start", 0) < end]
    return json.dumps({
        "loudness_lufs_whole_video": audio.get("loudness_lufs"), "peak_dbfs_whole_video": audio.get("peak_dbfs"),
        "silence_threshold_db": audio.get("silence_threshold_db"),
        "silences": [s for s in audio.get("silences") or [] if _overlaps(s, start, end)],
        "sound_without_speech": [s for s in audio.get("sound_without_speech") or [] if _overlaps(s, start, end)],
        "volume_per_window": windows, "window_s": audio.get("window_s")}, ensure_ascii=False)


def transcript_slice(segments: Any, start: float, end: float, last: bool) -> str:
    lines, size = [], 0
    for seg in segments if isinstance(segments, list) else []:
        if not isinstance(seg, dict) or not (start <= seg.get("start", 0) < end or (last and seg.get("start", 0) >= end)):
            continue
        line = f"[{collector.timestamp(seg['start'])}] {seg.get('text', '')}"
        size += len(line) + 1
        if size > TRANSCRIPT_CHARS:
            lines.append("[… transcription de cette plage coupée ici]")
            break
        lines.append(line)
    return neutral("\n".join(lines)) if lines else "(aucune parole détectée dans cette plage)"


def batch_params(collected: dict[str, Any], batch: dict[str, Any], index: int, count: int, images: Path,
                 last: bool) -> dict[str, Any]:
    meta = dict(collected.get("metadata") or {})
    if index:
        meta.pop("description", None)  # read once, in the first batch
    frames = "\n".join(f"- {images / f['file']}  t={collector.timestamp(f['t'])}  ({f['kind'] or 'image'})"
                       for f in batch["frames"]) or "(aucune image pour cette plage)"
    return {"etape": f"« lot » {index + 1} sur {count} : lis TOUTES les images listées, rends `timeline`, "
                     "`unreadable` et `overview` ; `digest` reste vide.",
            "contexte": neutral(json.dumps({"url": collected.get("url"), "duration": collected.get("duration"), **meta},
                                           ensure_ascii=False)),
            "plage": f"de {collector.timestamp(batch['start'])} à {collector.timestamp(batch['end'])} "
                     f"(durée totale {collected.get('duration') or '?'})",
            "images": frames,
            "transcription": transcript_slice(collected.get("transcript_segments"), batch["start"], batch["end"], last),
            "son": sound_slice(collected.get("audio"), batch["start"], batch["end"]),
            "chronologie": "(sans objet pour un lot)", "intention": "", "chantiers": "", "ultron": "",
            "technique": "", "lentille": ""}


def clean_entries(raw: Any, low: float, high: float) -> tuple[list[dict[str, Any]], int]:
    """The timeline entries of a batch, bounded, sorted, with texts cleaned; second value = sentences dropped."""
    entries, dropped = [], 0
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        start = _number(item.get("start"), low, high)
        entry = {"start": start, "end": _number(item.get("end"), start, high)}
        for key in ("said", "shown", "sound"):
            entry[key], removed = clean(item.get(key))
            dropped += removed
        if entry["said"] or entry["shown"] or entry["sound"]:
            entries.append(entry)
    return sorted(entries, key=lambda e: e["start"]), dropped


def clean_technique(raw: Any, low: float, high: float) -> tuple[list[dict[str, Any]], int]:
    """The know-how shown by the images of a batch (todo #443): bounded, moment inside the range, texts cleaned."""
    items, dropped = [], 0
    for item in raw[:TECHNIQUE_ITEMS] if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text, removed = clean(item.get("text"), TECHNIQUE_CHARS)
        dropped += removed
        if text:
            items.append({"t": _number(item.get("t"), low, high), "text": text})
    return sorted(items, key=lambda x: x["t"]), dropped


def technique_text(items: list[dict[str, Any]]) -> str:
    lines = [f"[{collector.timestamp(x['t'])}] {x['text']}" for x in items]
    return neutral(_bounded(lines, TECHNIQUE_BLOCK_CHARS)) if lines else "(rien de noté)"


def uncovered(frames: list[dict[str, Any]], entries: list[dict[str, Any]]) -> list[float]:
    """Moments of images that no entry covers: Claude skipped them (a gap, said out loud)."""
    return [f["t"] for f in frames if not any(e["start"] - 1 <= f["t"] <= max(e["end"], e["start"]) + 1
                                              for e in entries)]


def chronology(entries: list[dict[str, Any]], limit: int) -> str:
    return "\n".join(f"[{collector.timestamp(e['start'])} à {collector.timestamp(e['end'])}] "
                     f"DIT : {e['said'][:limit] or '-'} | MONTRÉ : {e['shown'][:limit] or '-'} | "
                     f"SON : {e['sound'][:limit] or '-'}" for e in entries)


def fallback_digest(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evenly spread entries when the last call failed: what they show, or what is said."""
    pool = [e for e in entries if e["shown"] or e["said"]]
    step = max(1, -(-len(pool) // DIGEST_MAX))
    return [{"time": e["start"], "text": (e["shown"] or e["said"])[:160]} for e in pool[::step][:DIGEST_MAX]]


def clean_digest(raw: Any, entries: list[dict[str, Any]], duration: float) -> tuple[list[dict[str, Any]], int]:
    """Digest lines with their time snapped to the nearest entry (never an invented moment)."""
    starts = [e["start"] for e in entries]
    out, dropped = [], 0
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not starts:
            continue
        text, removed = clean(item.get("text"), 200)
        dropped += removed
        if text:
            wanted = _number(item.get("time"), 0, duration)
            out.append({"time": min(starts, key=lambda s: abs(s - wanted)), "text": text})
    return sorted(out, key=lambda d: d["time"])[:DIGEST_MAX], dropped


# --- the grid of the synthesis (todo #441) ----------------------------------------------------------

def _bounded(lines: list[str], budget: int) -> str:
    """Whole lines while they fit in `budget` characters, then « … (n de plus) »."""
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > budget - 20:
            out.append(f"… ({len(lines) - i} de plus)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def known_chantiers() -> list[tuple[str, str]]:
    """(name, one line) of the active chantiers (~/notes/prompt/<name>.md, chantier/core.py); [] when unreadable."""
    from ultron_hub.chantier import core as chantier_core  # here: the chantier module pulls the todo store
    try:
        src = chantier_core.sources()
        names = chantier_core.subjects(src)[:CHANTIERS_MAX]
    except Exception as exc:  # noqa: BLE001  best effort: the grid then has no chantier
        log.warning("video: chantiers unreadable (%s)", type(exc).__name__)
        return []
    out = []
    for name in names:
        try:
            info = chantier_core.parse_prompt((src.prompts / f"{name}.md").read_text(encoding="utf-8")[:20_000], name)
            line = " ".join(f"{info['titre']} : {info['ou_en_est']}".split()).strip(" :")
        except Exception:  # noqa: BLE001
            line = ""
        out.append((name, line[:160]))
    return out


def chantiers_text(chantiers: list[tuple[str, str]]) -> str:
    lines = [f"- {name}" + (f" : {line}" if line else "") for name, line in chantiers]
    return neutral(_bounded(lines, CHANTIERS_CHARS)) if lines else "(aucun chantier actif connu)"


def lens_section(text: str) -> str:
    """The « But » of a fiche (or « Objectif », « Vision », « Besoin », in that order, numbering ignored); without
    one, its title and introduction (before the first section)."""
    sections = re.split(r"^## +", text, flags=re.MULTILINE)
    found: dict[str, str] = {}
    for part in sections[1:]:
        heading, _, body = part.partition("\n")
        word = re.sub(r"^[0-9]+(\s*bis)?[.)]?\s*", "", heading.strip().lower())
        word = re.split(r"[^a-zà-ÿ]", word, maxsplit=1)[0]
        if word in LENS_HEADINGS and body.strip():
            found.setdefault(word, body)
    for word in LENS_HEADINGS:
        if word in found:
            return found[word]
    return sections[0].lstrip("# ")


def lens_text(chantier: Any) -> str:
    """What the chantier of origin is after, as data for the synthesis (todo #443): the lens section of its fiche
    (the path given by its prompt, which must stay inside the notes folder: the parent of the prompts folder),
    cleaned, bounded and neutralised; NO_LENS without a chantier or when the fiche is unreadable."""
    if not isinstance(chantier, str) or not CHANTIER_NAME_RE.fullmatch(chantier):
        return NO_LENS
    from ultron_hub.chantier import core as chantier_core  # here: the chantier module pulls the todo store
    try:
        src = chantier_core.sources()
        info = chantier_core.parse_prompt((src.prompts / f"{chantier}.md").read_text(encoding="utf-8")[:20_000],
                                          chantier)
        path = Path(info["fiche"]).expanduser()
        notes = src.prompts.expanduser().resolve().parent
        if path.is_symlink() or path.suffix != ".md" or not path.resolve().is_relative_to(notes) \
                or not path.is_file():
            log.warning("video: fiche of chantier %s refused or missing", chantier)
            return NO_LENS
        text, _ = clean(lens_section(path.read_text(encoding="utf-8", errors="replace")[:200_000]), LENS_CHARS)
    except Exception as exc:  # noqa: BLE001  best effort: the synthesis then has no lens
        log.warning("video: lens of chantier %s unreadable (%s)", chantier, type(exc).__name__)
        return NO_LENS
    return neutral(f"Chantier {chantier} : {text}") if text else NO_LENS


def intention_text(intention: dict[str, Any]) -> str:
    view = {"origine": _origin(intention), "session": intention.get("session"), "chantier": intention.get("chantier"),
            "phrase": " ".join((intention.get("phrase") or "").split()) or None}
    return neutral(json.dumps(view, ensure_ascii=False))[:INTENTION_CHARS]


def _frontmatter(path: Path) -> dict[str, str]:
    """`name` and `description` of a SKILL.md header (simple `key: value` lines, quotes removed)."""
    text = path.read_text(encoding="utf-8", errors="replace")[:4000]
    if not text.startswith("---"):
        return {}
    out = {}
    for line in text[3:].split("\n---", 1)[0].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("name", "description"):
            out[key.strip()] = value.strip().strip("\"'")
    return out


def skills_lines(folder: Path | None = None) -> list[str]:
    folder = SKILLS_DIR if folder is None else folder
    lines = []
    for path in sorted(folder.glob("*/SKILL.md")) if folder.is_dir() else []:
        try:
            head = _frontmatter(path)
        except OSError:
            continue
        desc, _ = clean(head.get("description"), 220)
        lines.append(f"- skill {clean(head.get('name') or path.parent.name, 60)[0]} "
                     f"(`~/.claude/skills/{path.parent.name}/SKILL.md`) : {desc}")
    return lines


def docs_lines(settings: Settings) -> list[str]:
    """The « Carte des docs » of ultron-hub's CLAUDE.md, paths prefixed with the project."""
    try:
        text = (settings.root / "CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        return []
    section = text.split("## Carte des docs", 1)[1].split("\n## ", 1)[0] if "## Carte des docs" in text else ""
    return [clean(line.replace("- `docs/", "- `ultron-hub/docs/", 1), 200)[0]
            for line in section.splitlines() if line.startswith("- `docs/")]


def savoir_lines(settings: Settings) -> list[str]:
    base = savoir.root(settings) / savoir.FICHES
    lines = []
    for row in savoir.entries(settings):
        title, removed = clean(row.get("titre"), 160)
        if title and not removed:
            lines.append(f"- fiche « {title} » (`{base / (str(row.get('id')) + '.md')}`)")
    return lines


def ultron_text(settings: Settings) -> str:
    """What exists in Ultron, compact and bounded: skills, docs of ultron-hub, titles of the carnet (data)."""
    blocks = []
    for label, read, budget in (("Skills (~/.claude/skills)", lambda: skills_lines(), SKILLS_CHARS),
                                ("Docs du projet ultron-hub (carte du CLAUDE.md)", lambda: docs_lines(settings), DOCS_CHARS),
                                ("Carnet de savoir (titres des fiches)", lambda: savoir_lines(settings), SAVOIR_CHARS)):
        try:
            lines = read()
        except Exception as exc:  # noqa: BLE001  an unreadable source says so, the rest is still given
            log.warning("video: inventory %s unreadable (%s)", label, type(exc).__name__)
            lines = ["(illisible)"]
        blocks.append(f"{label} :\n" + (_bounded(lines, budget) if lines else "(rien)"))
    return neutral("\n\n".join(blocks))


def _texts(raw: Any, count: int, limit: int = GRID_CHARS) -> tuple[list[str], int]:
    out, dropped = [], 0
    for item in raw[:count] if isinstance(raw, list) else []:
        text, removed = clean(item, limit)
        dropped += removed
        if text:
            out.append(text)
    return out, dropped


def empty_grid() -> dict[str, Any]:
    return {"cas": "", "cas_secondaires": [], "devine": {"phrase": "", "pourquoi": ""},
            "inspiration": {"sujet": "", "analogie": "", "comment_dit": [], "comment_montre": [], "non_montre": [],
                            "face_ultron": [], "pour_nous": []},
            "info": {"faits": [], "opinions": []}, "methode": [], "donnees": [], "ecarts": [], "a_verifier": [],
            "avis": {"credible": "", "vente": "", "applicable": "", "pourquoi": ""}}


def grid_view(stored: Any) -> dict[str, Any]:
    """A saved grid with every field of today's shape: an analysis made before todo #443 has `inspiration.comment`
    (one list, what was said) and none of the new fields."""
    grid = empty_grid()
    if not isinstance(stored, dict):
        return grid
    for key, value in stored.items():
        if key in grid and isinstance(value, type(grid[key])):
            grid[key] = {**grid[key], **value} if isinstance(value, dict) else value
    insp = stored.get("inspiration") if isinstance(stored.get("inspiration"), dict) else {}
    if isinstance(insp.get("comment"), list) and not insp.get("comment_dit"):
        grid["inspiration"]["comment_dit"] = [str(c) for c in insp["comment"]]
    grid["inspiration"].pop("comment", None)
    grid["avis"]["applicable"] = grid["avis"]["applicable"] if grid["avis"]["applicable"] in APPLICABLE else ""
    grid["cas"] = grid["cas"] if grid["cas"] in CAS else ""
    grid["cas_secondaires"] = [c for c in grid["cas_secondaires"] if c in CAS and c != grid["cas"]]
    return grid


def grid_is_empty(grid: dict[str, Any]) -> bool:
    insp, info = grid["inspiration"], grid["info"]
    return not (grid["devine"]["phrase"] or any(insp.values()) or any(info.values()) or grid["methode"]
                or grid["donnees"] or grid["ecarts"] or grid["a_verifier"] or any(grid["avis"].values()))


def clean_grid(data: dict[str, Any], chantiers: frozenset[str],
               instants: list[float] | None = None) -> tuple[dict[str, Any], int]:
    """The grid written by Claude, cleaned: texts (`clean`), bounded lists, enums checked, a chantier outside the
    known list emptied, a shown step's moment snapped to one of `instants` (timeline and know-how, never an
    invented moment). Second value = sentences dropped as instructions."""
    grid, dropped = empty_grid(), 0

    def text(value: Any, limit: int = GRID_CHARS) -> str:
        nonlocal dropped
        got, removed = clean(value, limit)
        dropped += removed
        return got

    def texts(value: Any, count: int, limit: int = GRID_CHARS) -> list[str]:
        nonlocal dropped
        got, removed = _texts(value, count, limit)
        dropped += removed
        return got

    def obj(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    grid["cas"] = data.get("cas") if data.get("cas") in CAS else ""
    others = data.get("cas_secondaires") if isinstance(data.get("cas_secondaires"), list) else []
    grid["cas_secondaires"] = [c for c in dict.fromkeys(x for x in others if isinstance(x, str))
                               if c in CAS and c != grid["cas"]][:3]
    devine = obj(data.get("devine"))
    grid["devine"] = {"phrase": text(devine.get("phrase"), 300), "pourquoi": text(devine.get("pourquoi"), 300)}
    insp = obj(data.get("inspiration"))
    face = []
    for item in insp.get("face_ultron")[:6] if isinstance(insp.get("face_ultron"), list) else []:
        item = obj(item)
        idee = text(item.get("idee"), 300)
        if idee and item.get("etat") in ETATS:
            face.append({"idee": idee, "etat": item["etat"], "fichier": text(item.get("fichier"), 200) or NOT_CHECKED})
    nous = []
    for item in insp.get("pour_nous")[:3] if isinstance(insp.get("pour_nous"), list) else []:
        item = obj(item)
        application = text(item.get("application"))
        if application:
            chantier = item.get("chantier")
            nous.append({"application": application,
                         "chantier": chantier if isinstance(chantier, str) and chantier in chantiers else "",
                         "dossier_dabord": text(item.get("dossier_dabord"), 300)})
    shown = []
    for item in insp.get("comment_montre")[:8] if isinstance(insp.get("comment_montre"), list) else []:
        item = obj(item)
        what = text(item.get("text"))
        if what:
            wanted = _number(item.get("t"), 0, 86_400)
            shown.append({"t": min(instants, key=lambda s: abs(s - wanted)) if instants else wanted, "text": what})
    grid["inspiration"] = {"sujet": text(insp.get("sujet")), "analogie": text(insp.get("analogie")),
                           "comment_dit": texts(insp.get("comment_dit"), 8), "comment_montre": shown,
                           "non_montre": texts(insp.get("non_montre"), 6, 300), "face_ultron": face, "pour_nous": nous}
    info = obj(data.get("info"))
    grid["info"] = {"faits": texts(info.get("faits"), 10, 300), "opinions": texts(info.get("opinions"), 6, 300)}
    grid["methode"] = texts(data.get("methode"), 12, 300)
    grid["donnees"] = texts(data.get("donnees"), 12, 300)
    grid["ecarts"] = texts(data.get("ecarts"), 6, 300)
    grid["a_verifier"] = texts(data.get("a_verifier"), 10, 300)
    avis = obj(data.get("avis"))  # todo #470: Ultron's opinion (credible? selling? applicable here?)
    grid["avis"] = {"credible": text(avis.get("credible"), 400), "vente": text(avis.get("vente"), 400),
                    "applicable": avis.get("applicable") if avis.get("applicable") in APPLICABLE else "",
                    "pourquoi": text(avis.get("pourquoi"), 400)}
    return grid, dropped


def rangement_of(raw: Any) -> list[dict[str, Any]]:
    """The batch to file: fiches and todos only (cleaned later by proposals.build_actions)."""
    return [r for r in raw if isinstance(r, dict) and r.get("type") in RANGEMENT_KINDS][:proposals.MAX_ACTIONS] \
        if isinstance(raw, list) else []


def _run(settings: Settings, params: dict[str, Any], requested_by: str | None, read_dir: Path | None,
         task: str = TASK):
    request = JobRequest.from_dict({"job_id": new_job_id(), "action": task, "params": params,
                                    "requested_by": requested_by})
    return run_job(request, settings, read_dir=read_dir)


def analyse_video(settings: Settings, video_id: str, collected: dict[str, Any],
                  requested_by: str | None) -> dict[str, Any]:
    """The whole analysis; returns the content of analyse.json (nothing is written here)."""
    images = collector.video_dir(video_id) / collector.IMAGES_DIR
    duration = duration_of(collected)
    frames = valid_frames(collected, images)
    batches = plan_batches(frames, duration)
    started = time.monotonic()
    entries: list[dict[str, Any]] = []
    gaps: list[str] = []
    overviews: list[str] = []
    techniques: list[dict[str, Any]] = []
    cost, dropped, failed = 0.0, 0, 0
    for i, batch in enumerate(batches):
        label = f"{collector.timestamp(batch['start'])} à {collector.timestamp(batch['end'])}"
        if time.monotonic() - started > DEADLINE_S:
            result = None
            reason = "délai total de l'analyse dépassé"
        else:
            params = batch_params(collected, batch, i, len(batches), images, last=i == len(batches) - 1)
            result = _run(settings, params, requested_by, images if batch["frames"] else None, TASK_LOT)
            reason = result.error or result.summary or "échec de Claude"
            cost += result.cost_usd or 0.0
        if result is None or not result.ok or not isinstance(result.data, dict):
            failed += 1
            log.warning("video=%s batch %d/%d failed: %s", video_id, i + 1, len(batches), reason)
            gaps.append(f"Passage {label} : images non analysées ({reason[:120]}). Seule la parole est gardée.")
            speech, _ = clean(transcript_slice(collected.get("transcript_segments"), batch["start"], batch["end"],
                                               i == len(batches) - 1), 2000)
            if speech and not speech.startswith("(aucune"):
                entries.append({"start": round(batch["start"], 1), "end": round(batch["end"], 1), "said": speech,
                                "shown": "", "sound": ""})
            continue
        got, removed = clean_entries(result.data.get("timeline"), batch["start"], max(batch["end"], batch["start"]))
        dropped += removed
        entries += got
        shown, removed = clean_technique(result.data.get("technique"), batch["start"],
                                         max(batch["end"], batch["start"]))
        dropped += removed
        techniques += shown
        overview, removed = clean(result.data.get("overview"), 500)
        dropped += removed
        overviews.append(overview)
        for note in result.data.get("unreadable") or []:
            text, removed = clean(note, 300)
            dropped += removed
            if text:
                gaps.append(text)
        missed = uncovered(batch["frames"], got)
        if missed:
            gaps.append("Images sans description : " + ", ".join(collector.timestamp(t) for t in missed[:20]) + ".")
    if failed == len(batches):
        raise AnalyseError("L'analyse a échoué sur toute la vidéo. Réessaie dans quelques minutes.")
    entries.sort(key=lambda e: e["start"])
    if len(techniques) > TECHNIQUE_TOTAL:
        gaps.append(f"Savoir-faire montré : {len(techniques) - TECHNIQUE_TOTAL} élément(s) au-delà de "
                    f"{TECHNIQUE_TOTAL} non transmis à la synthèse (ils restent dans la chronologie).")
    techniques = sorted(techniques, key=lambda x: x["t"])[:TECHNIQUE_TOTAL]
    overview, digest = "", []
    intention = _intention(collected)
    chantiers = known_chantiers()
    names = frozenset(name for name, _ in chantiers)
    summary_params = {"etape": "« synthèse » : pas d'images ; rends `overview`, `digest` et la grille (`cas`, "
                               "`devine`, la grille du cas, `avis`, `rangement`) ; `timeline` et `unreadable` restent vides.",
                      "contexte": neutral(json.dumps({"url": collected.get("url"), "duration": collected.get("duration"),
                                                      **(collected.get("metadata") or {})}, ensure_ascii=False)),
                      "plage": f"toute la vidéo (durée {collected.get('duration') or '?'})",
                      "images": "(aucune : pas d'images à cette étape)", "transcription": "(voir la chronologie)",
                      "son": "(voir la chronologie)",
                      "chronologie": neutral(chronology(entries, FIELD_CHARS)),
                      "intention": intention_text(intention), "chantiers": chantiers_text(chantiers),
                      "ultron": ultron_text(settings), "technique": technique_text(techniques),
                      "lentille": lens_text(intention.get("chantier"))}
    if len(summary_params["chronologie"]) > SYNTH_CHARS:
        summary_params["chronologie"] = neutral(chronology(entries, 250))[:SYNTH_CHARS]
    result = _run(settings, summary_params, requested_by, None)
    cost += result.cost_usd or 0.0
    if result.ok and isinstance(result.data, dict):
        overview, removed = clean(result.data.get("overview"), 800)
        dropped += removed
        digest, removed = clean_digest(result.data.get("digest"), entries, duration)
        dropped += removed
        grid, removed = clean_grid(result.data, names,
                                   sorted({e["start"] for e in entries} | {x["t"] for x in techniques}))
        dropped += removed
        rangement = rangement_of(result.data.get("rangement"))
    else:
        grid, rangement = empty_grid(), []
    if grid_is_empty(grid):
        log.warning("video=%s no grid from the synthesis", video_id)
        gaps.append("La grille (ce que je pense que tu cherches) n'a pas pu être faite : seule la chronologie est "
                    "disponible. Dis-moi en une phrase ce que tu cherchais.")
    if len(digest) < DIGEST_MIN:
        log.warning("video=%s synthesis unusable, digest built from the timeline", video_id)
        digest = fallback_digest(entries)
        gaps.append("La synthèse finale a échoué : le résumé ci-dessus est tiré de la chronologie, sans relecture.")
    overview = overview or " ".join(o for o in overviews if o)[:800]
    if dropped:
        gaps.append(f"{dropped} phrase(s) écartée(s) : elles ressemblaient à une consigne adressée à une IA "
                    "(texte de la vidéo ou de son affichage).")
    for warning in (collected.get("full_warnings") or []) + (collected.get("warnings") or []):
        gaps.append(f"Collecte : {str(warning)[:200]}.")
    if collected.get("transcript_truncated"):
        gaps.append("La transcription gardée dans la collecte était coupée, mais cette analyse a lu les segments complets.")
    if not collected.get("transcript_segments"):
        gaps.append("Aucune parole détectée dans la vidéo.")
    return {"id": video_id, "url": collected.get("url"), "title": collected.get("title") or "Vidéo",
            "author": collected.get("author") or "", "duration": collected.get("duration") or "?",
            "duration_s": duration, "overview": overview, "digest": digest, "timeline": entries,
            "technique": techniques,
            "unreadable": list(dict.fromkeys(gaps)), "images_read": len(frames), "batches": len(batches), "failed_batches": failed,
            "cost_usd": round(cost, 4), "metadata": collected.get("metadata") or {},
            "audio": collected.get("audio"), "intention": intention, "grille": grid,
            "rangement": rangement, "chantiers_connus": sorted(names),
            "analysed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _intention(collected: dict[str, Any]) -> dict[str, Any]:
    """Where the video was sent from and the user's sentence (todo #440); an old collection has none."""
    found = collected.get("intention")
    found = found if isinstance(found, dict) else {}
    return {k: found.get(k) if isinstance(found.get(k), str) else None for k in ("session", "chantier", "phrase")}


# --- rendering ---------------------------------------------------------------------------------

def _grid_lines(grid: dict[str, Any], cas: str, short: bool) -> list[str]:
    """The lines of one case of the grid (markdown bullets when not `short`, text lines for the message)."""
    insp, info, out = grid["inspiration"], grid["info"], []
    bullet = "" if short else "- "
    if cas == "inspiration":
        said = insp["comment_dit"][:2] if short else insp["comment_dit"]
        shown = insp["comment_montre"][:2] if short else insp["comment_montre"]
        face = insp["face_ultron"][:2] if short else insp["face_ultron"]
        if insp["sujet"]:
            out.append(f"Sujet : {insp['sujet']}" if short else f"**Sujet** : {insp['sujet']}")
        if said:
            out += (["Il dit faire :"] + [f"{i}. {c}" for i, c in enumerate(said, 1)]) if short else \
                ["", "**Comment il a fait : ce qu'il dit**", ""] + [f"{i}. {c}" for i, c in enumerate(said, 1)]
        if shown:
            out += (["On le voit faire :"] if short else ["", "**Comment il a fait : ce qu'on le voit faire**", ""]) \
                + [f"{bullet or '- '}[{collector.timestamp(x['t'])}] {x['text']}" for x in shown]
        if insp["analogie"] and not short:
            out += ["", f"En image : {insp['analogie']}"]
        if insp["non_montre"] and not short:
            out += ["", "**Ce qui n'est pas montré (non vérifié)**", ""] + [f"- {x}" for x in insp["non_montre"]]
        if face:
            if not short:
                out += ["", "**Face à Ultron**", ""]
            out += [f"{bullet}{'Face à Ultron : ' if short else ''}{f['idee']} : {ETAT_TEXTE[f['etat']]} "
                    f"({f['fichier']})" for f in face]
        if insp["pour_nous"]:
            if not short:
                out += ["", "**Pour nous**", ""]
            for x in insp["pour_nous"]:
                where = f" (chantier {x['chantier']})" if x["chantier"] else ""
                line = f"{bullet}{'Pour nous : ' if short else ''}{x['application']}{where}"
                if x["dossier_dabord"] and not short:
                    line += f". Dossier d'abord : {x['dossier_dabord']}"
                out.append(line)
    elif cas == "info":
        faits, opinions = (info["faits"][:4], info["opinions"][:2]) if short else (info["faits"], info["opinions"])
        if faits:
            out += (["Faits :"] if short else ["**Faits**", ""]) + [f"- {x}" for x in faits]
        if opinions:
            out += (["Opinions :"] if short else ["", "**Opinions**", ""]) + [f"- {x}" for x in opinions]
    elif cas == "methode":
        out += [f"{i}. {x}" for i, x in enumerate(grid[cas][:6] if short else grid[cas], 1)]
    elif cas == "donnees":
        out += [f"- {x}" for x in (grid[cas][:6] if short else grid[cas])]
    return out


def _filled(grid: dict[str, Any]) -> list[str]:
    """The cases with content: the guessed one, then the secondary ones, then the others."""
    lines = {c: _grid_lines(grid, c, short=False) for c in CAS}
    first = [c for c in dict.fromkeys([grid["cas"], *grid["cas_secondaires"]]) if c in CAS and lines[c]]
    return first + [c for c in CAS if c not in first and lines[c]]


def render_message(analysis: dict[str, Any]) -> str:
    """The text (Telegram, dashboard): the guess + the essentials of the grid, 20 lines at most; without a grid the
    overview and the moments as before."""
    meta = " · ".join(x for x in (analysis["author"], analysis["duration"], f"{analysis['images_read']} images") if x)
    lines = [f"🔬 Analyse : {analysis['title']}", meta]
    grid = grid_view(analysis.get("grille"))
    tail = []
    actions = analysis.get("rangement") or []
    if actions:
        tail.append(f"📥 {len(actions)} proposition(s) à ranger (fiche ou todo), détail en pièce jointe.")
    checks = len(grid["ecarts"]) + len(grid["a_verifier"])
    if checks:
        tail.append(f"🔎 {checks} point(s) à vérifier (écart dit/montré, affirmation), détail en pièce jointe.")
    avis = grid["avis"]
    if avis["applicable"]:
        tail.append(f"⚖️ Avis d'Ultron : {APPLICABLE_TEXTE[avis['applicable']]}. {cut_prose(avis['pourquoi'], 120)}".strip())
    gaps = analysis["unreadable"]
    if gaps:
        tail.append(f"⚠️ {len(gaps)} point(s) non lu(s) ou à vérifier, détail en pièce jointe.")
    tail.append("📎 Analyse complète en pièce jointe (chronologie en annexe).")
    if grid_is_empty(grid):
        if analysis["overview"]:
            lines.append(analysis["overview"])
        lines += [f"{collector.timestamp(d['time'])}  {d['text']}" for d in analysis["digest"]]
        return "\n".join((lines + tail)[:MESSAGE_MAX_LINES + 2])
    if grid["devine"]["phrase"]:
        lines.append(f"🎯 Je pense que tu cherches : {grid['devine']['phrase']}")
    room = MESSAGE_MAX_LINES - len(lines) - len(tail) - 1
    body = []
    for cas in _filled(grid)[:1]:
        body = _grid_lines(grid, cas, short=True)
    if not body and analysis["overview"]:
        body = [analysis["overview"]]
    lines += body[:room]
    lines += tail
    lines.append("Ce n'est pas ça ? Réponds en une phrase (« non, c'était pour … »).")
    return "\n".join(lines)


def _avis_lines(avis: dict[str, str]) -> list[str]:
    """« Avis d'Ultron » section of analyse.md (todo #470); nothing when the synthesis gave none."""
    if not any(avis.values()):
        return []
    lines = ["", "## Avis d'Ultron", ""]
    lines += [f"- **Crédible ?** {avis['credible']}"] if avis["credible"] else []
    lines += [f"- **De la vente ?** {avis['vente']}"] if avis["vente"] else []
    lines.append(f"- **Applicable chez nous ?** {APPLICABLE_TEXTE.get(avis['applicable'], 'non jugé')}"
                 + (f" : {avis['pourquoi']}" if avis["pourquoi"] else ""))
    return lines


def _origin(intention: dict[str, Any]) -> str:
    if intention.get("chantier"):
        return f"chantier {intention['chantier']} (session {intention.get('session')})"
    return "Main" if intention.get("session") else "inconnu"


def render_markdown(analysis: dict[str, Any]) -> str:
    url = analysis["url"] or ""
    meta = analysis["metadata"]
    grid = grid_view(analysis.get("grille"))
    lines = [f"# Analyse : {analysis['title']}", "",
             f"- Lien : {url}", f"- Auteur : {analysis['author'] or 'inconnu'}",
             f"- Durée : {analysis['duration']}"]
    lines.append(f"- Envoyée depuis : {_origin(analysis.get('intention') or {})}")
    if (analysis.get("intention") or {}).get("phrase"):
        lines.append(f"- Ta phrase : {neutral(' '.join(analysis['intention']['phrase'].split()))}")
    if meta.get("upload_date"):
        d = meta["upload_date"]
        lines.append(f"- Publiée le : {d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else f"- Publiée le : {d}")
    stats = [f"{label} {meta[key]}" for key, label in (("view_count", "vues"), ("like_count", "mentions j'aime"),
                                                       ("comment_count", "commentaires")) if meta.get(key) is not None]
    if stats:
        lines.append("- Plateforme : " + ", ".join(stats))
    lines += [f"- Images lues : {analysis['images_read']}", f"- Analysée le : {analysis['analysed_at']}", "",
              "> Les faits viennent de la vidéo (donnée non vérifiée). « Ce que je pense que tu cherches » est ma "
              "devinette : si elle est fausse, corrige-la en une phrase et je refais les propositions.", "",
              "## Ce que je pense que tu cherches", ""]
    if grid["devine"]["phrase"]:
        lines.append(grid["devine"]["phrase"])
        if grid["devine"]["pourquoi"]:
            lines += ["", f"Pourquoi : {grid['devine']['pourquoi']}"]
    else:
        lines.append("(je n'ai pas su deviner : la grille n'a pas pu être faite, voir « Ce que je n'ai pas pu lire »)")
    if grid["cas"]:
        others = ", ".join(CAS_TITRES[c] for c in grid["cas_secondaires"])
        lines += ["", f"Cas : {CAS_TITRES[grid['cas']]}" + (f" ; aussi : {others}" if others else "")]
    avis_lines = _avis_lines(grid["avis"])
    for i, cas in enumerate(_filled(grid)):
        lines += ["", f"## {CAS_TITRES[cas]}", ""] + _grid_lines(grid, cas, short=False)
        if i == 0:  # right after the guessed case
            lines += avis_lines
    if avis_lines and not _filled(grid):
        lines += avis_lines
    if grid["ecarts"]:
        lines += ["", "## Écarts entre ce qui est dit et ce qui est montré (à vérifier)", ""]
        lines += [f"- {x}" for x in grid["ecarts"]]
    if grid["a_verifier"]:
        lines += ["", "## À vérifier en ligne", ""] + [f"- {x}" for x in grid["a_verifier"]]
    actions = analysis.get("rangement") or []
    if actions:
        lines += ["", "## Ce que je propose de ranger (✅ une par une)", ""]
        lines += [f"- {a['n']}. {'Fiche' if a['type'] == 'fiche' else 'Todo'} : {a['titre']}"
                  + (f" (chantier {a['chantier']})" if a.get("chantier") else "")
                  + (f" : {a['resume']}" if a.get("resume") else "") for a in actions]
    lines += ["", "## Vue d'ensemble", "", analysis["overview"] or "(indisponible)", "", "## Moments clés", ""]
    lines += [f"- [{collector.timestamp(d['time'])}]({timed_link(url, int(d['time']))}) {d['text']}"
              for d in analysis["digest"]]
    if meta.get("chapters"):
        lines += ["", "## Chapitres de la plateforme", ""]
        lines += [f"- {collector.timestamp(c.get('start'))} {str(c.get('title') or '')}" for c in meta["chapters"]
                  if isinstance(c, dict)]
    lines += ["", "## Ce que je n'ai pas pu lire", ""]
    lines += [f"- {g}" for g in analysis["unreadable"]] or ["- Rien à signaler : toutes les images ont été lues."]
    lines += ["", "## Annexe : chronologie", "", "> Description neutre de ce qui est dit, montré et mesuré.", ""]
    for e in analysis["timeline"]:
        link = timed_link(url, int(e["start"]))
        lines += [f"### [{collector.timestamp(e['start'])}]({link}) à {collector.timestamp(e['end'])}", ""]
        lines += [f"- Dit : {e['said']}" if e["said"] else "- Dit : (rien)",
                  f"- Montré : {e['shown']}" if e["shown"] else "- Montré : (rien de noté)",
                  f"- Son : {e['sound']}" if e["sound"] else "- Son : (rien de noté)", ""]
    if analysis.get("technique"):
        lines += ["## Annexe : savoir-faire montré (relevé dans les images)", ""]
        lines += [f"- [{collector.timestamp(x['t'])}]({timed_link(url, int(x['t']))}) {x['text']}"
                  for x in analysis["technique"] if isinstance(x, dict)] + [""]
    audio = analysis.get("audio")
    lines += ["## Annexe : mesures du son (script)", ""]
    if isinstance(audio, dict):
        lines += [f"- Volume moyen : {audio.get('loudness_lufs')} LUFS, crête {audio.get('peak_dbfs')} dBFS",
                  f"- Silences (plus d'1 s sous {audio.get('silence_threshold_db')} dB) : "
                  f"{len(audio.get('silences') or [])}",
                  f"- Passages de son sans parole (musique, ambiance ou bruit) : "
                  f"{len(audio.get('sound_without_speech') or [])}"]
    else:
        lines.append("- Pas de piste audio exploitable.")
    if meta.get("description"):
        quoted = "\n".join("> " + ln for ln in str(meta["description"])[:1500].splitlines())
        lines += ["", "## Annexe : description de la plateforme (texte de l'auteur, non vérifié)", "", quoted]
    return "\n".join(lines) + "\n"


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def load_analysis(video_id: str) -> dict[str, Any]:
    """The saved analysis of a video (hook of todo #93: the user's interpretation is read against it)."""
    path = collector.video_dir(video_id) / ANALYSE_JSON
    if not path.is_file() or path.is_symlink():
        raise AnalyseError("Cette vidéo n'a pas encore été analysée.")
    return collector.read_report(path)


def file_batch(settings: Settings, video_id: str, analysis: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The `rangement` of the synthesis made ready in ONE batch (todo #441) by the mechanism of `video actions`
    (proposals.build_actions: fiche drafts, todos with their chantier) and kept in actions.json with the same fields,
    so that the existing ✅ work. `analysis["rangement"]` becomes the short view of what was proposed."""
    from ultron_hub.video import actions as vactions  # here: actions imports this module
    raw = analysis.pop("rangement", None) or []
    names = frozenset(analysis.pop("chantiers_connus", None) or [])
    reponse, _ = clean(analysis["grille"]["devine"]["phrase"], 400)
    try:
        items = proposals.build_actions(settings, vactions.video_source(video_id, analysis), raw,
                                        str(analysis.get("author") or ""),
                                        (analysis.get("intention") or {}).get("phrase") or "", names)
    except UltronHubError as exc:
        log.warning("video=%s batch of proposals unusable: %s", video_id, exc)
        items = []
    proposals.write_private(collector.video_dir(video_id) / proposals.ACTIONS_JSON,
                            json.dumps({"reponse": reponse, "actions": items}, ensure_ascii=False))
    analysis["rangement"] = [{k: a[k] for k in ("n", "type", "titre", "resume", "chantier") if k in a} for a in items]
    return reponse, items


def do_analyse(settings: Settings, video_id: str, requested_by: str | None) -> dict[str, Any]:
    """`video analyse <id>` (the video lock is held by the caller). Returns an envelope."""
    command = "video.analyse"
    try:
        collected = collector.load_collected(video_id)
        if not isinstance(collected.get("frames"), list) or "metadata" not in collected:
            raise AnalyseError("Cette collecte est ancienne (sans images ni son). Renvoie le lien avec /video.")
        analysis = analyse_video(settings, video_id, collected, requested_by)
    except UltronHubError as exc:
        return commands.envelope(status="error", summary="analyse impossible", error=str(exc), command=command)
    folder = collector.video_dir(video_id)
    reponse, actions = file_batch(settings, video_id, analysis)
    md = folder / ANALYSE_MD
    _write_private(md, render_markdown(analysis))
    _write_private(folder / ANALYSE_JSON, json.dumps(analysis, ensure_ascii=False))
    esprit.record(settings, "ultron-hub", "video", f"Vidéo analysée : {analysis['title']}")
    log.info("video=%s analysed: %d entries, %d gaps, %d/%d batches failed, cost %s", video_id,
             len(analysis["timeline"]), len(analysis["unreadable"]), analysis["failed_batches"], analysis["batches"],
             analysis["cost_usd"])
    text, truncated = cut(render_message(analysis))
    return commands.envelope(summary=f"Vidéo analysée : {analysis['title']}", command=command,
                             data={"id": video_id, "text": text, "truncated": truncated, "path": str(md),
                                   "title": analysis["title"], "cost_usd": analysis["cost_usd"],
                                   "gaps": len(analysis["unreadable"]), "batches": analysis["batches"],
                                   "failed_batches": analysis["failed_batches"], "next": "interpretation",
                                   "cas": analysis["grille"]["cas"], "reponse": reponse, "actions": actions})
