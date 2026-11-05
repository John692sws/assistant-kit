"""🧹 Ménage proposé (todo #362, outside the read-only `menage` package): an agent prepares a tidy-up, the user applies all of it with ONE signed ✅.

`menage proposer --cancel ID:raison … --archiver nom …` writes var/menage/proposition.json (it replaces the previous one):
open todos to give up (a one-line reason each) and open chantiers to archive. Nothing changes before the user's ✅.

`menage proposition` (read only) shows it with its fingerprint (`empreinte`): 16 hex of the SHA-256 of one line per item,
in the order of the file: `cancel:<id>:<title json>:<reason json>` then `archiver:<name>`. `menage appliquer --empreinte …`
needs the `menage-lot` token signed on that fingerprint (or `--i-am-human`), never replayed (proofs kept in
var/menage/propositions-appliquees.jsonl). It recomputes everything first: a todo no longer open, a chantier no longer open,
or any other fingerprint = refused, nothing applied, the proposal stays. Then the todos are cancelled (status event `by` =
the signer + `via: menage-lot`, `empreinte`; notes untouched, ↩️ reopen gives them back), then the chantiers are archived
(`chantier archiver`'s own refusals: open todo, running squad). The first refusal stops: the rest is reported, never applied.
Once applying has started the proposal is consumed (moved to the jsonl with its result), whatever the outcome.

Todo #474: `--dossier CHEMIN:raison` adds a folder flagged by tidy-up check n° 10 (`disposable.aged_dirs` of
config/menage.yaml): only a direct entry of one of those folders (or `<entry>/<sous_dossier>` when the key is set), absolute,
normalised, no `..`, no symlink anywhere below the configured folder, never the project root. Its identity
(device:inode:mtime_ns) is stored with the proposal and enters the fingerprint; a folder gone or changed = refused, nothing
applied. Applied last, it is MOVED to var/.trash/<name>-<date> (never deleted), like the skills.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ultron_hub.chantier import commands as chantier_commands
from ultron_hub.chantier import core as chantier_core
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings
from ultron_hub.escouade import core as escouade_core
from ultron_hub.esprit import store as journal
from ultron_hub.common.errors import ConfigError
from ultron_hub.menage import rules as menage_rules
from ultron_hub.menage import skills as skills_usage
from ultron_hub.todo import closing
from ultron_hub.todo.store import TaskNotFound, TodoStore

ACTION = "menage-lot"
EMPREINTE_RE = re.compile(r"[0-9a-f]{16}")
CANCEL_RE = re.compile(r"([1-9][0-9]{0,8}):(.+)", re.S)
RAISON_MAX = 200
CANCEL_MAX = 50
ARCHIVER_MAX = 20
PAR_RE = re.compile(r"[a-z0-9:_-]{1,40}")
CORBEILLE_MAX = 20
CORBEILLE_RE = re.compile(r"([^:]+):(.+)", re.S)
DOSSIER_MAX = 50
DOSSIER_RE = re.compile(r"([^:]+):(.+)", re.S)


def parse_dossier(text: str) -> tuple[str, str]:
    m = DOSSIER_RE.fullmatch(text or "")
    raison = " ".join(m.group(2).split()) if m else ""
    if not m or not raison or len(raison) > RAISON_MAX or "\n" in m.group(2):
        raise InvalidRequestError(f"--dossier refusé : {text!r} (attendu CHEMIN:raison, chemin absolu sans « : », "
                                  f"raison sur une ligne, {RAISON_MAX} caractères au plus)")
    return m.group(1), raison


def dossier_roots(settings: Settings) -> list[tuple[str, str | None]]:
    """(folder, sous_dossier or None) of every `disposable.aged_dirs` entry: the only places a folder may be taken from."""
    cfg = menage_rules.load_config(settings.root / "config" / "menage.yaml")
    home = menage_rules.user_home()
    out = []
    for item in cfg["disposable"].get("aged_dirs") or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            sub = item.get("sous_dossier")
            out.append((os.path.normpath(menage_rules.expand(item["path"], home)), sub if isinstance(sub, str) else None))
    return out


def _check_dossier(settings: Settings, chemin: str, ident: str | None = None) -> tuple[str | None, str | None]:
    """(why this folder cannot go to var/.trash or None, its identity `dev:ino:mtime_ns`).

    With `ident` (the one stored at proposal time) a folder replaced or modified since is refused too."""
    if (not isinstance(chemin, str) or not chemin or "\x00" in chemin or "\n" in chemin or not os.path.isabs(chemin)
            or ".." in chemin.split("/") or os.path.normpath(chemin) != chemin):
        return f"chemin refusé : {chemin!r} (absolu, normalisé, sans « .. »)", None
    root = os.path.normpath(str(settings.root))
    if chemin == root or root.startswith(chemin.rstrip("/") + "/"):
        return f"chemin refusé : {chemin} (racine du projet ou au-dessus)", None
    if os.path.basename(chemin).startswith("."):
        return f"chemin refusé : {chemin} (dossier caché)", None
    try:
        roots = dossier_roots(settings)
    except ConfigError as exc:
        return f"configuration du ménage illisible ({exc})", None
    base = None
    for top, sub in roots:
        parent = os.path.dirname(chemin)
        if sub is None and parent == top or sub is not None and os.path.basename(chemin) == sub and os.path.dirname(parent) == top:
            if not os.path.basename(parent if sub else chemin).startswith("."):
                base = top
                break
    if base is None:
        return f"chemin refusé : {chemin} (hors des dossiers du ménage n° 10)", None
    try:
        st = os.lstat(chemin)
    except OSError:
        return f"dossier {chemin} introuvable", None
    if stat.S_ISLNK(st.st_mode):
        return f"chemin refusé : {chemin} (lien symbolique)", None
    if not stat.S_ISDIR(st.st_mode):
        return f"dossier {chemin} introuvable (pas un dossier)", None
    if os.path.realpath(chemin) != os.path.join(os.path.realpath(base), os.path.relpath(chemin, base)):
        return f"chemin refusé : {chemin} (lien symbolique sur le chemin)", None
    now = f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}"
    if ident is not None and ident != now:
        return f"dossier {chemin} a changé depuis la proposition", now
    return None, now


def skills_dir() -> Path:
    return skills_usage.default_skills_dir()  # follows $HOME, so tests use a throwaway one


def parse_corbeille(text: str) -> tuple[str, str]:
    m = CORBEILLE_RE.fullmatch(text or "")
    raison = " ".join(m.group(2).split()) if m else ""
    if not m or not raison or len(raison) > RAISON_MAX or "\n" in m.group(2):
        raise InvalidRequestError(f"--corbeille refusé : {text!r} (attendu NOM:raison, raison sur une ligne, "
                                  f"{RAISON_MAX} caractères au plus)")
    return m.group(1), raison


def _check_skill(nom: str) -> str | None:
    """Why this skill cannot go to the trash, None when it can."""
    if not nom or "/" in nom or ".." in nom or nom.startswith(".") or nom in skills_usage.SKIPPED:
        return f"nom de skill refusé : {nom!r}"
    folder_ = skills_dir() / nom
    if folder_.is_symlink() or not (folder_ / "SKILL.md").is_file():
        return f"skill {nom} introuvable"
    return None


def folder(settings: Settings) -> Path:
    return settings.var_dir / "menage"


def path(settings: Settings) -> Path:
    return folder(settings) / "proposition.json"


def applied_path(settings: Settings) -> Path:
    return folder(settings) / "propositions-appliquees.jsonl"


def parse_cancel(text: str) -> tuple[int, str]:
    m = CANCEL_RE.fullmatch(text or "")
    raison = " ".join(m.group(2).split()) if m else ""
    if not m or not raison or len(raison) > RAISON_MAX or "\n" in m.group(2):
        raise InvalidRequestError(f"--cancel refusé : {text!r} (attendu ID:raison, raison sur une ligne, "
                                  f"{RAISON_MAX} caractères au plus)")
    return int(m.group(1)), raison


def _open_chantier(src: chantier_core.Sources, sujet: str) -> str | None:
    """Why this chantier cannot be archived by a proposal, None when it is an open one."""
    if not escouade_core.NAME_RE.fullmatch(sujet or "") or sujet == escouade_core.MAIN_NAME:
        return f"nom de chantier refusé : {sujet!r}"
    if not (src.prompts / f"{sujet}.md").is_file():
        return f"chantier {sujet} introuvable ou déjà archivé"
    return None


def _check_todo(store: TodoStore, tid: int) -> tuple[str | None, str]:
    """(why it cannot be cancelled or None, title)."""
    try:
        task = store.get(tid)
    except TaskNotFound:
        return f"todo #{tid} introuvable", ""
    if task.status != "todo":
        return f"todo #{tid} n'est plus ouverte ({task.status})", task.title
    return None, task.title


def proposer(settings: Settings, cancel: list[str], archiver: list[str], par: str = "agent",
             src: chantier_core.Sources | None = None, corbeille: list[str] | None = None,
             dossiers: list[str] | None = None) -> dict[str, Any]:
    """Write the proposal (replacing the previous one) after checking every item; returns it as `voir` shows it."""
    if PAR_RE.fullmatch(par or "") is None:
        raise InvalidRequestError("--par refusé (a-z, 0-9, « : _ - », 40 caractères au plus)")
    items = [parse_cancel(c) for c in cancel]
    bins = [parse_corbeille(c) for c in corbeille or []]
    dirs = [parse_dossier(d) for d in dossiers or []]
    ids = [i for i, _ in items]
    if (len(set(ids)) != len(ids) or len(set(archiver)) != len(archiver) or len({n for n, _ in bins}) != len(bins)
            or len({c for c, _ in dirs}) != len(dirs)):
        raise InvalidRequestError("proposition refusée : une todo, un chantier, une skill ou un dossier apparaît deux fois")
    if not items and not archiver and not bins and not dirs:
        raise InvalidRequestError("proposition vide : donne au moins un --cancel, un --archiver, un --corbeille ou un --dossier")
    if len(items) > CANCEL_MAX or len(archiver) > ARCHIVER_MAX or len(bins) > CORBEILLE_MAX or len(dirs) > DOSSIER_MAX:
        raise InvalidRequestError(f"proposition trop longue ({CANCEL_MAX} todos, {ARCHIVER_MAX} chantiers au plus)")
    src = src or chantier_core.sources()
    with TodoStore(settings.todo_db) as store:
        for tid, _ in items:
            why, _ = _check_todo(store, tid)
            if why:
                raise InvalidRequestError(f"proposition refusée : {why}")
    for sujet in archiver:
        why = _open_chantier(src, sujet)
        if why:
            raise InvalidRequestError(f"proposition refusée : {why}")
    for nom, _ in bins:
        why = _check_skill(nom)
        if why:
            raise InvalidRequestError(f"proposition refusée : {why}")
    folders = []
    for chemin, raison in dirs:
        if any(chemin.startswith(other + "/") for other, _ in dirs):
            raise InvalidRequestError(f"proposition refusée : {chemin} est dans un autre dossier proposé")
        why, ident = _check_dossier(settings, chemin)
        if why:
            raise InvalidRequestError(f"proposition refusée : {why}")
        folders.append({"chemin": chemin, "raison": raison, "ident": ident})
    data = {"cree_a": datetime.now(timezone.utc).isoformat(timespec="seconds"), "par": par,
            "cancel": [{"id": i, "raison": r} for i, r in items], "archiver": list(archiver),
            "corbeille": [{"nom": n, "raison": r} for n, r in bins], "dossiers": folders}
    folder(settings).mkdir(parents=True, exist_ok=True)
    tmp = path(settings).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path(settings))
    journal.record(settings, "ultron-hub", "menage",
                   f"Ménage proposé par {par} : {len(items)} todo(s) à abandonner, {len(archiver)} chantier(s) à archiver, {len(bins)} skill(s) à la corbeille, {len(folders)} dossier(s) à var/.trash")
    return voir(settings, src)


def _load(settings: Settings) -> dict[str, Any] | None:
    try:
        raw = json.loads(path(settings).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise InvalidRequestError(f"proposition de ménage illisible ({type(exc).__name__})") from exc
    if (not isinstance(raw, dict) or not isinstance(raw.get("cancel"), list) or not isinstance(raw.get("archiver"), list)
            or not all(isinstance(c, dict) and isinstance(c.get("id"), int) and isinstance(c.get("raison"), str)
                       for c in raw["cancel"])
            or not all(isinstance(s, str) for s in raw["archiver"])
            or not isinstance(raw.setdefault("corbeille", []), list)
            or not all(isinstance(c, dict) and isinstance(c.get("nom"), str) and isinstance(c.get("raison"), str)
                       for c in raw["corbeille"])
            or not isinstance(raw.setdefault("dossiers", []), list)
            or not all(isinstance(d, dict) and isinstance(d.get("chemin"), str) and isinstance(d.get("raison"), str)
                       and isinstance(d.get("ident"), str) for d in raw["dossiers"])):
        raise InvalidRequestError("proposition de ménage mal formée")
    return raw


def empreinte(cancel: list[dict[str, Any]], archiver: list[dict[str, Any]],
              corbeille: list[dict[str, Any]] | None = None, dossiers: list[dict[str, Any]] | None = None) -> str | None:
    lines = [f"cancel:{int(c['id'])}:{json.dumps(c['titre'])}:{json.dumps(c['raison'])}" for c in cancel]
    lines += [f"archiver:{json.dumps(a['sujet'])}" for a in archiver]
    lines += [f"corbeille:{json.dumps(c['nom'])}:{json.dumps(c['raison'])}" for c in corbeille or []]
    lines += [f"dossier:{json.dumps(d['chemin'])}:{json.dumps(d['raison'])}:{json.dumps(d['ident'])}" for d in dossiers or []]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16] if lines else None


def voir(settings: Settings, src: chantier_core.Sources | None = None) -> dict[str, Any]:
    """The proposal as it is now: each item with its title and `refus` (why it could not be applied, None if fine)."""
    raw = _load(settings)
    if raw is None:
        return {"n": 0, "empreinte": None, "cancel": [], "archiver": [], "corbeille": [], "dossiers": [], "cree_a": None,
                "par": None, "bloquee": None}
    src = src or chantier_core.sources()
    cancel = []
    with TodoStore(settings.todo_db) as store:
        for c in raw["cancel"]:
            why, title = _check_todo(store, c["id"])
            cancel.append({"id": c["id"], "titre": title, "raison": c["raison"], "refus": why})
    archiver = [{"sujet": s, "refus": _open_chantier(src, s)} for s in raw["archiver"]]
    corbeille = [{"nom": c["nom"], "raison": c["raison"], "refus": _check_skill(c["nom"])} for c in raw["corbeille"]]
    dossiers = [{"chemin": d["chemin"], "raison": d["raison"], "ident": d["ident"],
                 "refus": _check_dossier(settings, d["chemin"], d["ident"])[0]} for d in raw["dossiers"]]
    bloquee = next((i["refus"] for i in cancel + archiver + corbeille + dossiers if i["refus"]), None)
    return {"n": len(cancel) + len(archiver) + len(corbeille) + len(dossiers),
            "empreinte": empreinte(cancel, archiver, corbeille, dossiers), "cancel": cancel, "archiver": archiver,
            "corbeille": corbeille, "dossiers": dossiers, "cree_a": raw.get("cree_a"), "par": raw.get("par"), "bloquee": bloquee}


def _refuse(reason: str) -> None:
    raise InvalidRequestError(f"❌ Ménage refusé : {reason}\n\nRien n'a été appliqué, la proposition reste. "
                              "À faire : rouvre « 🧹 Ménage proposé » pour voir la proposition à jour.")


def proof_used(settings: Settings, proof: str) -> bool:
    try:
        lines = applied_path(settings).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        try:
            if json.loads(line).get("proof") == proof:
                return True
        except ValueError:
            continue
    return False


def appliquer(settings: Settings, expected: str, authorize: Callable[[dict[str, Any]], dict[str, Any]],
              src: chantier_core.Sources | None = None,
              garde: Callable[[dict[str, Any]], str | None] | None = None) -> dict[str, Any]:
    """Check, sign (`authorize(view)` = the event details of who asked), apply in order, stop at the first refusal."""
    if EMPREINTE_RE.fullmatch(expected or "") is None:
        raise InvalidRequestError("empreinte invalide (16 caractères hexadécimaux)")
    src = src or chantier_core.sources()
    folder(settings).mkdir(parents=True, exist_ok=True)
    with open(folder(settings) / "proposition.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two presses at once: the second sees the proposal consumed
        view = voir(settings, src)
        if not view["n"]:
            _refuse("aucune proposition de ménage en attente (déjà appliquée ?)")
        if view["bloquee"]:
            _refuse(view["bloquee"])
        if view["empreinte"] != expected:
            _refuse("la proposition a changé depuis son affichage")
        why = garde(view) if garde else None  # todo #535: the automatic path's own refusals, under the same lock
        if why:
            _refuse(why)
        by = authorize(view)
        if by.get("proof") and proof_used(settings, by["proof"]):
            raise closing.CloseRefused("jeton de ménage déjà utilisé")
        trace = {**by, "via": ACTION, "empreinte": expected}
        consumed = path(settings).with_suffix(".en-cours")
        os.replace(path(settings), consumed)  # from here the proposal is consumed, whatever happens
        done: list[str] = []
        stopped: dict[str, Any] | None = None
        steps = [("cancel", c) for c in view["cancel"]] + [("archiver", a) for a in view["archiver"]]
        steps += [("corbeille", c) for c in view["corbeille"]] + [("dossier", d) for d in view["dossiers"]]
        for i, (kind, item) in enumerate(steps):
            label = _label(kind, item)
            try:
                if kind == "cancel":
                    _cancel(settings, item, trace)
                elif kind == "corbeille":
                    _corbeille(settings, item, trace)
                elif kind == "dossier":
                    _dossier(settings, item, trace)
                else:
                    chantier_commands.archive(settings, src, item["sujet"])
            except Exception as exc:  # noqa: BLE001 - reported, the rest is not applied
                stopped = {"item": label, "raison": str(exc)[:300]}
                remaining = [_label(k, x) for k, x in steps[i + 1:]]
                break
            done.append(label)
        else:
            remaining = []
        result = {"empreinte": expected, "faits": done, "arret": stopped, "restants": remaining}
        with applied_path(settings).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 "by": by.get("by"), "proof": by.get("proof"), "proposition": view, **result},
                                ensure_ascii=False) + "\n")
        consumed.unlink(missing_ok=True)
    journal.record(settings, "ultron-hub", "menage", f"Ménage proposé appliqué ({by.get('by')}) : {len(done)}/{len(steps)}"
                   + (f", arrêt à {stopped['item']}" if stopped else ""))
    _tableaux(settings)
    return {**result, "text": texte(result, len(steps))}


AUTO_BY = "auto:bilan"  # todo #535: who an unattended application is recorded as (listed by `bilan soir`)
JOB_ENVS = ("ULTRON_HUB_JOB", "ULTRON_ESCOUADE_DIR")
KNOWN_KEYS = {"cree_a", "par", "cancel", "archiver", "corbeille", "dossiers"}


def suppression_reelle(settings: Settings) -> str | None:
    """Why this proposal is NOT only reversible (todo #535), None when every item is bounded.

    Bounded = todo cancelled (↩️ reopen), chantier archived, skill or folder MOVED to a `.trash`. Anything else in the file
    (a key this code does not know, e.g. a real deletion) is not."""
    raw = _load(settings)
    if raw is None:
        return None
    unknown = sorted(k for k in raw if k not in KNOWN_KEYS)
    if unknown:
        return f"la proposition contient autre chose que des actions bornées ({', '.join(unknown)})"
    return None


def appliquer_auto(settings: Settings, src: chantier_core.Sources | None = None,
                   env: dict[str, str] | None = None) -> dict[str, Any]:
    """Apply the pending proposal WITHOUT the user's ✅ when it deletes nothing for real (todo #535), by `auto:bilan`.

    Own path (heartbeat or the Sunday timer), never a signed token. Refused, nothing changed: from a job or a squad, when
    `suppression_reelle` says so, when the proposal is blocked, or when its fingerprint changed between this read and the
    application. Returns {"statut": "rien" | "refuse" | "applique", ...}; a refused proposal stays for the ✅ as before."""
    env = os.environ if env is None else env
    job = next((n for n in JOB_ENVS if env.get(n)), None)
    if job:
        return {"statut": "refuse", "raison": f"application automatique interdite depuis un job ({job})"}
    try:
        view = voir(settings, src)
        if not view["n"]:
            return {"statut": "rien"}
        why = view["bloquee"] or suppression_reelle(settings)
        if why:
            return {"statut": "refuse", "raison": why, "empreinte": view["empreinte"]}
        res = appliquer(settings, view["empreinte"], lambda _v: {"by": AUTO_BY}, src,
                        garde=lambda _v: suppression_reelle(settings))
    except InvalidRequestError as exc:
        return {"statut": "refuse", "raison": str(exc).splitlines()[0][:300]}
    return {"statut": "applique", **res}


def _label(kind: str, item: dict[str, Any]) -> str:
    return {"cancel": f"#{item.get('id')}", "corbeille": f"skill {item.get('nom')}",
            "dossier": f"dossier {item.get('chemin')}"}.get(kind, f"chantier {item.get('sujet')}")


def _to_trash(source: Path, trash: Path, name: str) -> Path:
    """Move `source` to trash/<name>-<date>[-k] (never a deletion; moving it back restores it)."""
    trash.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    target = trash / f"{name}-{stamp}"
    k = 1
    while target.exists() or target.is_symlink():
        k += 1
        target = trash / f"{name}-{stamp}-{k}"
    shutil.move(str(source), str(target))
    return target


def trash_dir(settings: Settings) -> Path:
    return settings.var_dir / ".trash"


def _dossier(settings: Settings, item: dict[str, Any], trace: dict[str, Any]) -> None:
    """Check the folder again (same identity as proposed), then move it to var/.trash."""
    why, _ = _check_dossier(settings, item["chemin"], item["ident"])
    if why:
        raise InvalidRequestError(why)
    name = "-".join(Path(item["chemin"]).parts[-3:])  # e.g. missions-<id>-work or var-missions-<id>
    target = _to_trash(Path(item["chemin"]), trash_dir(settings), name)
    journal.record(settings, "ultron-hub", "menage",
                   f"Dossier à var/.trash : {item['chemin']} → {target.name} ({trace.get('by')}, ménage : {item['raison']})")


def _corbeille(settings: Settings, item: dict[str, Any], trace: dict[str, Any]) -> None:
    """Move the skill folder to .trash/<name>-<date> (never a deletion; moving it back restores it)."""
    why = _check_skill(item["nom"])
    if why:
        raise InvalidRequestError(why)
    target = _to_trash(skills_dir() / item["nom"], skills_dir() / ".trash", item["nom"])
    journal.record(settings, "ultron-hub", "menage",
                   f"Skill à la corbeille : {item['nom']} → {target.name} ({trace.get('by')}, ménage : {item['raison']})")


def _cancel(settings: Settings, item: dict[str, Any], trace: dict[str, Any]) -> None:
    with TodoStore(settings.todo_db) as store:
        why, _ = _check_todo(store, item["id"])
        if why:
            raise InvalidRequestError(why)
        task = store.set_status(item["id"], "cancelled", by=trace)
    journal.record(settings, "ultron-hub", "todo",
                   f"Todo annulée : {task.title} ({trace.get('by')}, ménage : {item['raison']})", task.id)


def _tableaux(settings: Settings) -> None:
    try:  # pinned boards of the group topics, as `todo cancel`: never blocks
        from ultron_hub.groupe import tableau
        tableau.rafraichir(settings)
    except Exception:  # noqa: BLE001
        pass


def texte(result: dict[str, Any], total: int) -> str:
    lines = [f"🧹 Ménage appliqué : {len(result['faits'])}/{total}"]
    if result["faits"]:
        lines.append("Fait : " + ", ".join(result["faits"]))
    if result["arret"]:
        lines.append(f"❌ Arrêt à {result['arret']['item']} : {result['arret']['raison']}")
    if result["restants"]:
        lines.append("Pas appliqué : " + ", ".join(result["restants"]) + " (à reproposer)")
    lines.append("Les notes des todos restent ; ↩️ Rouvrir rend une todo abandonnée.")
    return "\n".join(lines)
