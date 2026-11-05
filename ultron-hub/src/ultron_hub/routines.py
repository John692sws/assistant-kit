"""🔁 Routine proposée (todo #503): a routine made of one sentence of the user, activated by a signed ✅ on its exact content.

A routine is a JSON file `~/ultron/routines/proposees/<nom>.json`, moved to `actives/<nom>.json` after the ✅ (signed
`routine` on "<nom>:<empreinte>") or to `refusees/` on ❌; nothing is ever deleted. Launching at the hour is `routines_lanceur.py` (todo #509): this module only proposes, lists, shows, activates and refuses.

- Fields: nom (`[a-z0-9][a-z0-9-]{0,39}`), phrase (the user's sentence), cadence, but, critere_fin (1 to 3 commands, as
  `objectif add --critere`), plafond (quota points per launch, 1 to 20), chantier, cree_le.
- Cadence, a closed format parsed with fullmatch: « toutes les <N> h » (N >= 1: one hour at least, by construction),
  « chaque jour <HH:MM> », « chaque <lundi..dimanche> <HH:MM> ». Anything else is refused. 50 ACTIVE routines at most.
- `empreinte` = 16 hex of the SHA-256 of the canonical JSON of the stored routine (sorted keys, no spaces).
- `prochain` = the next due time (local time, ISO): from the activation for « toutes les N h », else the next HH:MM.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings
from ultron_hub.esprit import store as journal
from ultron_hub.todo import closing

ACTION = "routine"
RACINE = Path.home() / "ultron" / "routines"  # tests swap it (fixture), never an environment variable
PROPOSEES, ACTIVES, REFUSEES = "proposees", "actives", "refusees"
RETIREES = "retirees"  # todo #539
NOM_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # signed target "<nom>:<empreinte>" <= 57 characters
CHANTIER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
EMPREINTE_RE = re.compile(r"[0-9a-f]{16}")
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_HEURE = r"([01][0-9]|2[0-3]):([0-5][0-9])"
CADENCE_HEURES = re.compile(r"toutes les ([1-9][0-9]{0,3}) h")
CADENCE_JOUR = re.compile(r"chaque jour " + _HEURE)
CADENCE_SEMAINE = re.compile(r"chaque (" + "|".join(JOURS) + r") " + _HEURE)
ACTIVES_MAX = 50
CRITERES_MAX = 3
PLAFOND_MIN, PLAFOND_MAX = 1, 20
MODES = {"tri": "haiku", "travail": "sonnet", "decider": "opus"}  # mode of a routine -> model of its session (todo #517)
MODE_DEFAUT = "decider"
PORTE_MAX = 10  # files read by the gate of a routine (todo #510)
SENSIBLES = (".ssh", ".hermes", ".config/gh", ".claude", ".sudo_pass", "auth.json")
CADENCE_EVT = "sur évènement"  # cadence of a routine carrying a `quand` trigger (todo #512)
UNITE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,80}\.service")  # user systemd unit, fullmatch, no shell
TEXTE_MAX = 400  # phrase, but, each command: one line of the sheet (dashboard/actions.py: 500 characters at most)


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"routine.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    top = sub.add_parser("routine", help="routines in one sentence: propose, list, show, activate (signed ✅), refuse")
    cmds = top.add_subparsers(dest="routine_command", required=True, parser_class=parser_class)
    pro = cmds.add_parser("propose", help="write a proposed routine (nothing runs)")
    pro.add_argument("--nom", required=True)
    pro.add_argument("--phrase", required=True, help="the user's sentence, as said")
    pro.add_argument("--cadence", default=None,
                     help="« toutes les N h », « chaque jour HH:MM » or « chaque <lundi..dimanche> HH:MM » "
                          "(omitted with --quand-*)")
    pro.add_argument("--quand-dossier", default=None, dest="quand_dossier",
                     help="event trigger: launch when a file is new or changed in this absolute folder under $HOME")
    pro.add_argument("--quand-service", default=None, dest="quand_service",
                     help="event trigger: launch when this user systemd unit (<nom>.service) is down")
    pro.add_argument("--but", required=True)
    pro.add_argument("--critere", action="append", default=[], help="end command (repeatable, 1 to 3)")
    pro.add_argument("--plafond", required=True, type=int, help="quota points per launch (1 to 20)")
    pro.add_argument("--chantier", required=True)
    pro.add_argument("--porte-fichier", action="append", default=[], dest="porte_fichier",
                     help="gate without AI: launch only if this absolute file changed (repeatable, 1 to 10)")
    pro.add_argument("--mode", default=MODE_DEFAUT, choices=list(MODES),
                     help="model of the session: tri (haiku), travail (sonnet) or decider (opus, default)")
    cmds.add_parser("liste", help="proposed and active routines with fingerprint and next due time (read only)")
    voir = cmds.add_parser("voir", help="one routine (read only)")
    voir.add_argument("--nom", required=True)
    act = cmds.add_parser("activer", help="activate the proposed routine (signed routine on <nom>:<empreinte>)")
    act.add_argument("--nom", required=True)
    act.add_argument("--empreinte", required=True, help="fingerprint shown by `routine liste`")
    act.add_argument("--i-am-human", dest="human", action="store_true",
                     help="confirm at a real terminal (typed code) instead of a signed button")
    lan = cmds.add_parser("lancer", help="launch the active routines that are due (todo #509; the heartbeat does it too)")
    lan.add_argument("--essai", action="store_true", help="launch and write nothing, print the plan")
    ref = cmds.add_parser("refuser", help="move the proposed routine to refusees/ (nothing deleted)")
    ref.add_argument("--nom", required=True)
    ret = cmds.add_parser("retirer", help="move the ACTIVE routine to retirees/ (nothing deleted, not signed: it removes work)")
    ret.add_argument("--nom", required=True)


def var(settings: Settings) -> Path:
    return settings.var_dir / "routines"


def _refuse(reason: str) -> None:
    raise InvalidRequestError(f"❌ Routine refusée : {reason}\n\nRien n'a été activé ni lancé. "
                              "À faire : rouvre la carte « Routine proposée » (onglet À toi) pour voir l'état à jour.")


# -- cadence ----------------------------------------------------------------------------------------------------------

def parse_cadence(cadence: str) -> dict[str, Any]:
    """{"type": "heures", "n"} | {"type": "jour", "h", "m"} | {"type": "semaine", "jour" (0 = lundi), "h", "m"}, or refused."""
    c = cadence if isinstance(cadence, str) else ""
    if (m := CADENCE_HEURES.fullmatch(c)) is not None:
        return {"type": "heures", "n": int(m.group(1))}
    if (m := CADENCE_JOUR.fullmatch(c)) is not None:
        return {"type": "jour", "h": int(m.group(1)), "m": int(m.group(2))}
    if (m := CADENCE_SEMAINE.fullmatch(c)) is not None:
        return {"type": "semaine", "jour": JOURS.index(m.group(1)), "h": int(m.group(2)), "m": int(m.group(3))}
    if c == CADENCE_EVT:
        return {"type": "evenement"}
    _refuse(f"cadence {cadence!r} inconnue : « toutes les <N> h » (N ≥ 1), « chaque jour <HH:MM> » "
            "ou « chaque <lundi..dimanche> <HH:MM> »")
    raise AssertionError  # unreachable


def prochain(cadence: str, now: datetime, depuis: datetime | None = None) -> datetime:
    """The next due time strictly after `now` (local time). `depuis` = the activation, for « toutes les N h »."""
    p = parse_cadence(cadence)
    if p["type"] == "evenement":
        _refuse("pas d'échéance : routine sur évènement")
    if p["type"] == "heures":
        pas = timedelta(hours=p["n"])
        base = depuis if depuis is not None and depuis <= now else now
        k = int((now - base) / pas) + 1
        return base + k * pas
    jour = now.replace(hour=p["h"], minute=p["m"], second=0, microsecond=0)
    if p["type"] == "jour":
        return jour if jour > now else jour + timedelta(days=1)
    jour += timedelta(days=(p["jour"] - now.weekday()) % 7)
    return jour if jour > now else jour + timedelta(days=7)


# -- files ------------------------------------------------------------------------------------------------------------

def empreinte(routine: dict[str, Any]) -> str:
    canon = json.dumps(routine, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _nom(nom: str) -> str:
    if NOM_RE.fullmatch(nom or "") is None:
        _refuse(f"nom invalide ({nom!r} : lettres minuscules, chiffres et tirets, 40 caractères au plus)")
    return nom


def _dir(sous: str) -> Path:
    d = RACINE / sous
    if d.is_symlink():
        _refuse(f"{sous}/ est un lien symbolique")
    return d


def _lire(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        _refuse(f"{path.name} est un lien symbolique")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        _refuse(f"{path.name} n'est pas un JSON lisible")
    if not isinstance(data, dict):
        _refuse(f"{path.name} n'est pas une routine")
    return data


def _ecrire(path: Path, routine: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(routine, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _sensible(rel: str) -> bool:
    parts = rel.split("/")
    for s in SENSIBLES:
        k = s.split("/")
        if "/" in s:
            if parts[:len(k)] == k:
                return True
        elif s in parts:
            return True
    return False


def _porte(porte: Any) -> dict[str, Any] | None:
    """The gate to store (files only, absolute, under $HOME, never a sensitive path), None when absent."""
    if porte is None:
        return None
    fichiers = porte.get("fichiers") if isinstance(porte, dict) and set(porte) == {"fichiers"} else None
    if not isinstance(fichiers, list) or not 1 <= len(fichiers) <= PORTE_MAX:
        _refuse(f"porte : 1 à {PORTE_MAX} chemins de fichiers")
    home = Path.home()
    out = []
    for f in fichiers:
        if not isinstance(f, str) or not f.startswith("/") or "\x00" in f or len(f) > TEXTE_MAX:
            _refuse(f"porte : chemin absolu attendu ({f!r})")
        for chemin, base in ((os.path.normpath(f), home), (os.path.realpath(f), Path(os.path.realpath(home)))):
            try:
                rel = Path(chemin).relative_to(base).as_posix()
            except ValueError:
                _refuse(f"porte : {f} n'est pas sous le dossier personnel")
            if rel == "." or _sensible(rel):
                _refuse(f"porte : {f} est un chemin sensible ou un dossier")
        out.append(os.path.normpath(f))
    return {"fichiers": sorted(set(out))}


def _quand(quand: Any) -> dict[str, str] | None:
    """The event trigger to store: {"dossier": abs path} or {"service": unit}; same forbidden paths as the gate."""
    if quand is None:
        return None
    cle = next(iter(quand)) if isinstance(quand, dict) and len(quand) == 1 else None
    val = quand.get(cle) if cle else None
    if cle not in ("dossier", "service") or not isinstance(val, str):
        _refuse("quand : un seul déclencheur, « dossier » ou « service »")
    if cle == "service":
        if UNITE_RE.fullmatch(val) is None:
            _refuse(f"quand : unité systemd invalide ({val!r}), forme <nom>.service")
        return {"service": val}
    d = _porte({"fichiers": [val]})["fichiers"][0]  # absolute, under $HOME, not sensitive, not $HOME itself
    return {"dossier": d}


def porte_empreinte(porte: dict[str, Any]) -> str:
    """SHA-256 over the sorted paths with (size, mtime_ns) or absent: read only, no shell."""
    h = hashlib.sha256()
    for f in sorted(porte.get("fichiers") or []):
        try:
            st = os.stat(f)
            etat = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            etat = "absent"
        h.update(f"{f}\x00{etat}\n".encode())
    return h.hexdigest()


def _texte(nom: str, valeur: str) -> str:
    v = " ".join((valeur or "").split())
    if not v:
        _refuse(f"{nom} vide")
    if len(v) > TEXTE_MAX:
        _refuse(f"{nom} trop long ({len(v)} caractères, {TEXTE_MAX} au plus)")
    return v


def valider(nom: str, phrase: str, cadence: str, but: str, criteres: list[str], plafond: int, chantier: str,
            porte: Any = None, quand: Any = None, mode: str = MODE_DEFAUT) -> dict[str, Any]:
    """The routine to store (without cree_le), or refused."""
    _nom(nom)
    if not isinstance(mode, str) or mode not in MODES:
        _refuse(f"mode invalide ({mode!r}) : {', '.join(MODES)}")
    q = _quand(quand)
    if q is not None and porte is not None:
        _refuse("un déclencheur « quand » et une porte ne vont pas ensemble")
    cadence = CADENCE_EVT if q is not None else " ".join((cadence or "").split())
    if q is None and cadence == CADENCE_EVT:
        _refuse("la cadence « sur évènement » demande un déclencheur « quand »")
    parse_cadence(cadence)
    crit = [c.strip() for c in criteres or [] if c and c.strip()]
    if not 1 <= len(crit) <= CRITERES_MAX:
        _refuse(f"critère de fin : 1 à {CRITERES_MAX} commandes ({len(crit)} données)")
    for c in crit:
        if "\n" in c or "\r" in c or len(c) > TEXTE_MAX:
            _refuse(f"critère de fin sur une ligne, {TEXTE_MAX} caractères au plus")
    if isinstance(plafond, bool) or not isinstance(plafond, int) or not PLAFOND_MIN <= plafond <= PLAFOND_MAX:
        _refuse(f"plafond de {PLAFOND_MIN} à {PLAFOND_MAX} points de quota par lancement ({plafond!r})")
    if CHANTIER_RE.fullmatch(chantier or "") is None:
        _refuse(f"chantier invalide ({chantier!r})")
    r = {"nom": nom, "phrase": _texte("phrase", phrase), "cadence": cadence, "but": _texte("but", but),
         "critere_fin": crit, "plafond": plafond, "chantier": chantier, "mode": mode}
    p = _porte(porte)
    if p is not None:
        r["porte"] = p
    if q is not None:
        r["quand"] = q
    return r


def lignes(r: dict[str, Any]) -> list[str]:
    """What the sheet of the ✅ shows, as is: every field that gets signed."""
    out = [f"Routine « {r.get('nom')} »", f"Phrase : {r.get('phrase')}", f"Cadence : {r.get('cadence')}",
           f"But : {r.get('but')}"]
    out += [f"Fin quand : {c}" for c in r.get("critere_fin") or []]
    q = r.get("quand")
    if isinstance(q, dict) and "dossier" in q:
        out.append(f"Quand : un fichier nouveau ou modifié dans le dossier {q['dossier']}")
    elif isinstance(q, dict) and "service" in q:
        out.append(f"Quand : le service {q['service']} est tombé")
    if isinstance(r.get("porte"), dict):
        out.append("Porte : lance seulement si changé : " + ", ".join(map(str, r["porte"].get("fichiers") or [])))
    mode = r.get("mode") or MODE_DEFAUT
    out.append(f"Mode : {mode} (modèle {MODES.get(mode, '?')})")
    out += [f"Plafond : {r.get('plafond')} point(s) de quota par lancement", f"Chantier : {r.get('chantier')}",
            f"Proposée le : {r.get('cree_le')}"]
    return out


def _evites(settings: Settings | None, nom: str) -> int:
    try:
        v = json.loads((var(settings) / "lancements.json").read_text(encoding="utf-8"))[nom]["evites"]
    except (AttributeError, OSError, ValueError, KeyError, TypeError):
        return 0
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _vue(r: dict[str, Any], statut: str, now: datetime, settings: Settings | None = None) -> dict[str, Any]:
    out = {**r, "statut": statut, "empreinte": empreinte(r), "lignes": lignes(r), "prochain": None}
    if statut == "active":
        out["evites"] = _evites(settings, str(r.get("nom"))) if settings is not None else 0
    try:
        depuis = datetime.fromisoformat(r["active_le"]) if r.get("active_le") else None
        out["prochain"] = prochain(str(r.get("cadence")), now, depuis).isoformat(timespec="minutes")
    except (InvalidRequestError, ValueError, TypeError):
        pass  # a file edited by hand: shown, never launched
    return out


def _now() -> datetime:
    return datetime.now().astimezone()


def _fichiers(sous: str) -> list[Path]:
    d = _dir(sous)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix == ".json" and not p.is_symlink() and p.is_file()
                  and NOM_RE.fullmatch(p.stem) is not None)


def liste(settings: Settings | None = None) -> dict[str, list[dict[str, Any]]]:
    now, out = _now(), {}
    for sous, statut in ((PROPOSEES, "proposee"), (ACTIVES, "active")):
        items = []
        for p in _fichiers(sous):
            try:
                r = _lire(p)
            except InvalidRequestError:
                continue
            if r is not None:
                items.append(_vue(r, statut, now, settings))
        out[sous] = items
    return out


def voir(nom: str, settings: Settings | None = None) -> dict[str, Any]:
    _nom(nom)
    for sous, statut in ((PROPOSEES, "proposee"), (ACTIVES, "active")):
        r = _lire(_dir(sous) / f"{nom}.json")
        if r is not None:
            return _vue(r, statut, _now(), settings)
    _refuse(f"routine « {nom} » introuvable (déjà activée ou refusée ?)")
    raise AssertionError  # unreachable


def proof_used(settings: Settings, proof: str) -> bool:
    try:
        lines = (var(settings) / "activees.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        try:
            if json.loads(line).get("proof") == proof:
                return True
        except ValueError:
            continue
    return False


def _verrou(settings: Settings):
    var(settings).mkdir(parents=True, exist_ok=True)
    lock = open(var(settings) / "lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def proposer(settings: Settings, nom: str, phrase: str, cadence: str, but: str, criteres: list[str], plafond: int,
             chantier: str, porte: Any = None, quand: Any = None, mode: str = MODE_DEFAUT) -> dict[str, Any]:
    r = valider(nom, phrase, cadence, but, criteres, plafond, chantier, porte, quand, mode)
    r["cree_le"] = _now().isoformat(timespec="seconds")
    with _verrou(settings):
        for sous in (PROPOSEES, ACTIVES):
            p = _dir(sous) / f"{nom}.json"
            if p.exists() or p.is_symlink():
                _refuse(f"une routine « {nom} » existe déjà ({sous}/)")
        _ecrire(_dir(PROPOSEES) / f"{nom}.json", r)
    vue = _vue(r, "proposee", _now())
    journal.record(settings, "ultron-hub", "routine", f"🔁 Routine « {nom} » proposée ({r['cadence']}) : {r['phrase']}")
    return vue


def activer(settings: Settings, nom: str, expected: str, authorize: Callable[[dict[str, Any]], dict[str, Any]]
            ) -> dict[str, Any]:
    _nom(nom)
    if EMPREINTE_RE.fullmatch(expected or "") is None:
        _refuse("empreinte invalide (16 caractères hexadécimaux)")
    with _verrou(settings):
        src = _dir(PROPOSEES) / f"{nom}.json"
        r = _lire(src)
        if r is None:
            _refuse(f"routine « {nom} » pas en attente (déjà activée ou refusée ?)")
        assert r is not None
        if empreinte(r) != expected:
            _refuse("la routine a changé depuis son affichage")
        stored = {k: r.get(k) for k in ("phrase", "cadence", "but", "critere_fin", "plafond", "chantier")}
        if r.get("nom") != nom:
            _refuse("le nom dans le fichier ne correspond pas")
        if r.get("porte") is not None:
            stored["porte"] = r["porte"]
        if r.get("quand") is not None:
            stored["quand"] = r["quand"]
        if r.get("mode") is not None:
            stored["mode"] = r["mode"]
        valider(nom, criteres=stored.pop("critere_fin"), **stored)  # a file edited by hand never gets active
        dest = _dir(ACTIVES) / f"{nom}.json"
        if dest.exists() or dest.is_symlink():
            _refuse(f"une routine active « {nom} » existe déjà")
        if len(_fichiers(ACTIVES)) >= ACTIVES_MAX:
            _refuse(f"déjà {ACTIVES_MAX} routines actives (refuse ou retire-en une d'abord)")
        by = authorize(r)
        if by.get("proof") and proof_used(settings, by["proof"]):
            raise closing.CloseRefused("jeton de la routine déjà utilisé")
        now = _now().isoformat(timespec="seconds")
        with (var(settings) / "activees.jsonl").open("a", encoding="utf-8") as fh:  # spent before anything moves
            fh.write(json.dumps({"at": now, "by": by.get("by"), "proof": by.get("proof"), "nom": nom,
                                 "empreinte": expected}, ensure_ascii=False) + "\n")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dest)  # moved, never deleted
        _ecrire(dest, {**r, "active_le": now, "active_par": by.get("by")})
    text = f"🔁 Routine « {nom} » activée ({r['cadence']}) : le heartbeat la lancera à l'échéance"
    journal.record(settings, "ultron-hub", "routine", f"{text} ({by.get('by')}, empreinte {expected})")
    return {"nom": nom, "empreinte": expected, "chemin": str(dest), "text": text,
            "prochain": prochain(r["cadence"], _now(), datetime.fromisoformat(now)).isoformat(timespec="minutes")}


def refuser(settings: Settings, nom: str) -> dict[str, Any]:
    _nom(nom)
    with _verrou(settings):
        src = _dir(PROPOSEES) / f"{nom}.json"
        if _lire(src) is None:
            _refuse(f"routine « {nom} » pas en attente (déjà activée ou refusée ?)")
        d = _dir(REFUSEES)
        d.mkdir(parents=True, exist_ok=True)
        dest, n = d / f"{nom}.json", 2
        while dest.exists() or dest.is_symlink():
            dest, n = d / f"{nom}-{n}.json", n + 1
        os.rename(src, dest)
    text = f"🗑 Routine « {nom} » refusée (gardée dans refusees/)"
    journal.record(settings, "ultron-hub", "routine", text)
    return {"nom": nom, "chemin": str(dest), "text": text}


def retirer(settings: Settings, nom: str) -> dict[str, Any]:
    """Todo #539: an active routine goes to retirees/ (same free-name rule as `refuser`); no signature, it removes work."""
    _nom(nom)
    with _verrou(settings):
        src = _dir(ACTIVES) / f"{nom}.json"
        if _lire(src) is None:
            _refuse(f"routine « {nom} » pas active (déjà retirée ?)")
        d = _dir(RETIREES)
        d.mkdir(parents=True, exist_ok=True)
        dest, n = d / f"{nom}.json", 2
        while dest.exists() or dest.is_symlink():
            dest, n = d / f"{nom}-{n}.json", n + 1
        os.rename(src, dest)
    text = f"🛑 Routine « {nom} » retirée (gardée dans retirees/) : le lanceur ne la lancera plus"
    journal.record(settings, "ultron-hub", "routine", text)
    return {"nom": nom, "chemin": str(dest), "text": text}


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.routine_command
    if cmd == "propose":
        if args.quand_dossier and args.quand_service:
            _refuse("un seul déclencheur : --quand-dossier ou --quand-service")
        quand = ({"dossier": args.quand_dossier} if args.quand_dossier
                 else {"service": args.quand_service} if args.quand_service else None)
        data = proposer(settings, args.nom, args.phrase, args.cadence, args.but, args.critere, args.plafond, args.chantier,
                         {"fichiers": args.porte_fichier} if args.porte_fichier else None, quand, args.mode)
        return envelope(cmd, summary=f"🔁 routine « {data['nom']} » proposée, en attente du ✅", data=data)
    if cmd == "liste":
        data = liste(settings)
        n, a = len(data[PROPOSEES]), len(data[ACTIVES])
        return envelope(cmd, summary=f"{n} routine(s) proposée(s), {a} active(s)", data=data)
    if cmd == "voir":
        data = voir(args.nom, settings)
        return envelope(cmd, summary=f"{data['statut']} : {data['nom']} ({data['cadence']})", data=data)
    if cmd == "activer":
        def authorize(r: dict[str, Any]) -> dict[str, Any]:
            what = "Activer cette routine ?\n\n" + "\n".join(lignes(r))
            return closing.authorize(ACTION, f"{args.nom}:{args.empreinte}", human=args.human, what=what,
                                     used=lambda proof: proof_used(settings, proof))
        data = activer(settings, args.nom, args.empreinte, authorize)
        return envelope(cmd, summary=data["text"], data=data)
    if cmd == "lancer":
        from ultron_hub import routines_lanceur
        data = routines_lanceur.lancer(settings, essai=args.essai)
        return envelope(cmd, summary=data["resume"], data=data)
    if cmd == "refuser":
        data = refuser(settings, args.nom)
        return envelope(cmd, summary=data["text"], data=data)
    if cmd == "retirer":
        data = retirer(settings, args.nom)
        return envelope(cmd, summary=data["text"], data=data)
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
