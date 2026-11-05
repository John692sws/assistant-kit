"""Todo #509: lance les routines actives échues (`ultron-hub routine lancer [--essai]`, et le heartbeat de 5 min).

Un lancement au plus par passage. L'état (`var/routines/lancements.json`) est écrit sous le verrou `var/routines/lock`
AVANT le lancement : si le heartbeat repasse, la routine n'est jamais lancée deux fois. L'échéance suivante se calcule
depuis le dernier lancement (ou `active_le`) : une échéance ratée de plusieurs tours = un seul lancement.
tmux, ultron et usage sont injectables (tests sans vrai tmux ni claude).
"""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Callable

from ultron_hub import objectif, reprise, routines
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings

PREFIXE = "routine : "


def _etat_path(settings: Settings):
    return routines.var(settings) / "lancements.json"


def lire_etat(settings: Settings) -> dict[str, Any]:
    return _etat_disque(settings)


def _etat_disque(settings: Settings) -> dict[str, Any]:
    try:
        data = json.loads(_etat_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ecrire_etat(settings: Settings, etat: dict[str, Any]) -> None:
    path = _etat_path(settings)
    tmp = path.with_name(".lancements.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(etat, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _date(texte: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(texte))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.astimezone()


def echeance(r: dict[str, Any], dernier: str | None) -> datetime | None:
    """Prochaine échéance depuis le dernier lancement (ou l'activation) ; None si la routine est illisible."""
    base = _date(dernier) or _date(r.get("active_le"))
    if base is None:
        return None
    try:
        return routines.prochain(str(r.get("cadence")), base, base)
    except InvalidRequestError:
        return None


DOSSIER_MAX = 500  # entries read in a watched folder


def service_reel(unite: str) -> str:
    """`systemctl --user is-active <unité>`: argument list, no shell; '' when unreadable."""
    try:
        res = subprocess.run(["systemctl", "--user", "is-active", unite], capture_output=True, text=True, timeout=10,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (res.stdout or "").strip()


def signature_dossier(chemin: str) -> str | None:
    """SHA-256 over the regular files of the folder (name, size, mtime_ns), read only; None when empty or absent."""
    h = hashlib.sha256()
    n = 0
    try:
        with os.scandir(chemin) as it:
            for e in sorted(it, key=lambda x: x.name)[:DOSSIER_MAX]:
                try:
                    if e.is_symlink() or not e.is_file():
                        continue
                    st = e.stat()
                except OSError:
                    continue
                h.update(f"{e.name}\x00{st.st_size}:{st.st_mtime_ns}\n".encode())
                n += 1
    except OSError:
        return None
    return h.hexdigest() if n else None


def evenement(r: dict[str, Any], service: Callable[[str], str]) -> tuple[str | None, str]:
    """(signature of the event or None when nothing happens, readable text) for a routine with a `quand`."""
    q = r.get("quand") if isinstance(r.get("quand"), dict) else {}
    if isinstance(q.get("dossier"), str):
        return signature_dossier(q["dossier"]), f"fichier nouveau ou modifié dans {q['dossier']}"
    if isinstance(q.get("service"), str) and routines.UNITE_RE.fullmatch(q["service"]):
        etat = service(q["service"])
        return ("tombe" if etat in ("inactive", "failed") else None), f"service {q['service']} tombé"
    return None, ""


def commande(r: dict[str, Any], ident: int | str) -> list[str]:
    msg = f"Objectif {ident} (ultron-hub objectif show {ident}), routine {r['nom']} : {r['but']}"
    modele = routines.MODES.get(r.get("mode") or routines.MODE_DEFAUT, routines.MODES[routines.MODE_DEFAUT])
    return [reprise.ULTRON, "--modele", modele, "--chantier", r["chantier"], msg]


def _actives() -> list[dict[str, Any]]:
    out = []
    for p in routines._fichiers(routines.ACTIVES):
        try:
            r = routines._lire(p)
        except InvalidRequestError:
            continue
        if r is not None and r.get("nom") == p.stem:
            out.append(r)
    return out


def lancer(settings: Settings, essai: bool = False, *, tmux: Callable[[list[str]], tuple[int, str]] = reprise.tmux_reel,
           lanceur: Callable[[list[str]], tuple[int, str]] = reprise.ultron_reel,
           usage: Callable[[], dict[str, Any]] | None = None, now: datetime | None = None,
           service: Callable[[str], str] = service_reel) -> dict[str, Any]:
    """Plan (essai) ou exécution. `plan` : une entrée par routine active échue."""
    now = now or datetime.now().astimezone()
    usage = usage or (lambda: reprise.usage_reel(settings))
    plan: list[dict[str, Any]] = []
    quota: list[tuple[bool, str]] = []
    lance = False
    if not essai:
        _surveiller(settings, tmux, now)
    etat = lire_etat(settings)
    for r in _actives():
        nom = r["nom"]
        prec = etat.get(nom) if isinstance(etat.get(nom), dict) else {}
        signature, texte_evt = None, ""
        if isinstance(r.get("quand"), dict):
            signature, texte_evt = evenement(r, service)
            if signature is None:
                if prec.get("evenement") and not essai and "service" in r["quand"]:
                    _oublier_evenement(settings, nom)  # the service is back: a later fall counts again
                continue
            if prec.get("evenement") == signature:
                continue  # event already seen
            ech = now
        else:
            ech = echeance(r, prec.get("dernier"))
            if ech is None or ech > now:
                continue
        entree: dict[str, Any] = {"routine": nom, "echeance": ech.isoformat(timespec="minutes"),
                                  "commande": commande(r, "<id>"), "action": "lancement", "raison": ""}
        if texte_evt:
            entree["evenement"] = texte_evt
        raison = ""
        porte = r.get("porte") if isinstance(r.get("porte"), dict) else None
        if porte and prec.get("porte") == routines.porte_empreinte(porte):
            entree.update(action="saute", raison="porte : rien n'a changé")
            if not essai:
                _sauter(settings, r, porte, now)
            plan.append(entree)
            continue
        arret = prec.get("arret") if isinstance(prec.get("arret"), dict) else None
        if arret and arret.get("active_le") == r.get("active_le"):
            raison = (f"arrêtée : la session {arret.get('session')} est morte dès le lancement "
                      "(retire la routine ou réactive-la par un nouveau ✅)")
        elif lance:
            raison = "un seul lancement par passage"
        elif prec.get("session"):
            rc, _ = tmux(["has-session", "-t", f"={prec['session']}"])
            if rc == 0:
                raison = f"la session précédente {prec['session']} existe encore"
            elif rc == 127:
                raison = "tmux introuvable ou illisible"
        if not raison and reprise._chantier({"chantier": r["chantier"]}) is None:
            raison = f"chantier « {r['chantier']} » inconnu"
        if not raison:
            if not quota:
                try:
                    quota.append(reprise._quota_revenu(usage()))
                except Exception as exc:  # noqa: BLE001 - usage illisible = rien
                    quota.append((False, f"usage illisible ({type(exc).__name__})"))
            if not quota[0][0]:
                raison = quota[0][1]
        if raison:
            entree.update(action="rien", raison=raison)
        elif not essai:
            lance = True
            _lancer(settings, r, entree, lanceur, tmux, now, signature)
        else:
            lance = True
        plan.append(entree)
    faits = [e for e in plan if e["action"] in ("lancement", "echec")]
    sautees = [e for e in plan if e["action"] == "saute"]
    verbe = "lancerait" if essai else "lancée(s)"
    resume = f"{len(faits)} routine(s) {verbe} sur {len(plan)} échue(s)" if plan else "aucune routine échue"
    if sautees:
        resume += f", {len(sautees)} sautée(s) (porte : rien n'a changé)"
    return {"essai": essai, "plan": plan, "resume": resume}


ATTENTE_S = 15  # pause after a launch before checking the new session is still there (tests set 0)
MORTE_TOT_S = 120  # todo #539: a session gone before this many seconds is a failed launch, not a finished job


def _morte_tot(settings: Settings, prec: dict[str, Any], now: datetime) -> bool:
    """Session gone less than 2 min after the launch (proved by the passage time) with the objective still open."""
    dernier = _date(prec.get("dernier"))
    if dernier is None or (now - dernier).total_seconds() >= MORTE_TOT_S:
        return False
    try:
        obj = objectif.lire(settings, int(prec["objectif"]))
    except (InvalidRequestError, KeyError, TypeError, ValueError):
        return False
    return obj.get("statut") == "ouvert"


def _surveiller(settings: Settings, tmux: Callable, now: datetime) -> None:
    """Every passage, due or not: note a session seen alive, stop a routine whose session died within 2 min of its launch."""
    etat = _etat_disque(settings)
    for r in _actives():
        prec = etat.get(r["nom"]) if isinstance(etat.get(r["nom"]), dict) else {}
        if not prec.get("session") or prec.get("arret"):
            continue
        rc, _ = tmux(["has-session", "-t", f"={prec['session']}"])
        if rc == 0:
            _vue_vivante(settings, r, now)
        elif rc != 127 and _morte_tot(settings, prec, now):
            _arreter(settings, r, prec)


def _vue_vivante(settings: Settings, r: dict[str, Any], now: datetime) -> None:
    with routines._verrou(settings):
        etat = lire_etat(settings)
        prec = etat.get(r["nom"])
        if isinstance(prec, dict) and prec.get("session"):
            prec["vue_vivante"] = now.isoformat(timespec="seconds")
            _ecrire_etat(settings, etat)


def _arreter(settings: Settings, r: dict[str, Any], prec: dict[str, Any]) -> None:
    """Remember the stop (tied to this activation) and leave the proof in the objective."""
    nom = r["nom"]
    with routines._verrou(settings):
        etat = lire_etat(settings)
        cur = etat.get(nom) if isinstance(etat.get(nom), dict) else {}
        cur["arret"] = {"active_le": r.get("active_le"), "session": prec.get("session"),
                        "le": datetime.now().astimezone().isoformat(timespec="seconds")}
        etat[nom] = cur
        _ecrire_etat(settings, etat)
    texte = (f"{PREFIXE}{nom} arrêtée : session {prec.get('session')} morte moins de {MORTE_TOT_S // 60} min après le lancement ; "
             "pas de relance avant `ultron-hub routine retirer` ou un nouveau ✅")
    objectif._modifier(settings, int(prec["objectif"]), lambda o: o["preuves"].append({"le": objectif._now(), "texte": texte}))


def _evites(prec: dict[str, Any]) -> int:
    v = prec.get("evites")
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _sauter(settings: Settings, r: dict[str, Any], porte: dict[str, Any], now: datetime) -> None:
    """Gate closed: no objective, no session, no usage; the next check waits for the cadence."""
    nom = r["nom"]
    with routines._verrou(settings):
        etat = lire_etat(settings)
        prec = etat.get(nom) if isinstance(etat.get(nom), dict) else {}
        ech = echeance(r, prec.get("dernier"))
        if ech is None or ech > now or prec.get("porte") != routines.porte_empreinte(porte):
            return
        etat[nom] = {**prec, "dernier": now.isoformat(timespec="seconds"), "evites": _evites(prec) + 1}
        _ecrire_etat(settings, etat)


def _oublier_evenement(settings: Settings, nom: str) -> None:
    with routines._verrou(settings):
        etat = lire_etat(settings)
        prec = etat.get(nom)
        if isinstance(prec, dict) and "evenement" in prec:
            etat[nom] = {k: v for k, v in prec.items() if k != "evenement"}
            _ecrire_etat(settings, etat)


def _lancer(settings: Settings, r: dict[str, Any], entree: dict[str, Any], lanceur: Callable, tmux: Callable,
            now: datetime, signature: str | None = None) -> None:
    nom = r["nom"]
    with routines._verrou(settings):  # revérifié et écrit AVANT le lancement : pas de double lancement
        etat = lire_etat(settings)
        prec = etat.get(nom) if isinstance(etat.get(nom), dict) else {}
        ech = echeance(r, prec.get("dernier")) if signature is None else now
        if ech is None or ech > now or (signature is not None and prec.get("evenement") == signature):
            entree.update(action="rien", raison="déjà lancée par un autre passage")
            return
        obj = objectif.creer(settings, r["but"], list(r["critere_fin"]), f"{r['plafond']} points", r["chantier"])
        etat[nom] = {"dernier": now.isoformat(timespec="seconds"), "objectif": obj["id"], "session": None,
                     "evites": _evites(prec)}
        if signature is not None:
            etat[nom]["evenement"] = signature
        if isinstance(r.get("porte"), dict):
            etat[nom]["porte"] = routines.porte_empreinte(r["porte"])
        _ecrire_etat(settings, etat)
    objectif._modifier(settings, obj["id"], lambda o: o["preuves"].append({"le": objectif._now(),
                                                                          "texte": f"{PREFIXE}{nom}"}))
    argv = commande(r, obj["id"])
    entree.update(objectif=obj["id"], commande=argv)
    avant = reprise._noms(tmux)
    rc, sortie = lanceur(argv[1:])
    m = reprise.NOM_RE.search(sortie or "")
    nouvelle = m.group(1) if m else None
    if nouvelle is None:
        nouvelle = next((n for n in sorted(reprise._noms(tmux) - avant) if n.startswith("ultron-")), None)
    if nouvelle is None or not objectif.SESSION_RE.fullmatch(nouvelle):
        texte = f"{PREFIXE}échec du lancement de {nom} (rc {rc})"
        objectif._modifier(settings, obj["id"], lambda o: o["preuves"].append({"le": objectif._now(), "texte": texte}))
        entree.update(action="echec", raison=f"lancement sans nouvelle session (rc {rc})")
        return
    objectif.lier(settings, obj["id"], nouvelle)
    with routines._verrou(settings):
        etat = lire_etat(settings)
        etat.setdefault(nom, {})["session"] = nouvelle
        _ecrire_etat(settings, etat)
    entree["nouvelle_session"] = nouvelle
    time.sleep(ATTENTE_S)
    rc2, _ = tmux(["has-session", "-t", f"={nouvelle}"])
    if rc2 == 1:  # gone seconds after the launch: no relaunch before a removal or a new ✅
        _arreter(settings, r, {"session": nouvelle, "objectif": obj["id"]})
        entree["raison"] = f"session {nouvelle} morte dès le lancement : routine arrêtée"


def lanceur_auto(settings: Settings) -> None:
    """Appel du heartbeat : une fois par passage, toute exception est écrite sur stderr sans casser le heartbeat."""
    try:
        res = lancer(settings)
        if res["plan"]:
            print(f"routine lancer : {res['resume']}", file=sys.stderr)
    except BaseException as exc:  # noqa: BLE001
        print(f"routine lancer : {type(exc).__name__}: {exc}", file=sys.stderr)
