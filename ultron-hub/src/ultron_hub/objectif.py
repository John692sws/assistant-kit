"""Todo #496: objectif stocké hors session, un fichier JSON `var/objectifs/<id>.json` par objectif.

Un objectif survit à la session : `objectif add|show|list|etape|preuve`. Écriture atomique (fichier temporaire puis
`os.replace`, droits 0600). Pas de suppression. `critere_fin` = commandes texte, exécutées plus tard (todo #497).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ultron_hub import repos
from ultron_hub.core.registry import FORBIDDEN_PATHS, _is_relative_to
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings

BUT_MAX = 2000
ID_RE = re.compile(r"[0-9]+\.json")
TIMEOUT_DEFAUT_S = 120
SORTIE_MAX = 4000
SESSION_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
RELANCES_MAX = 3
ACTION_MAX = 500
INTERROMPUE_S = 30 * 60


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"objectif.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    top = sub.add_parser("objectif", help="objectifs stockés hors session: add, show, list, etape, preuve")
    cmds = top.add_subparsers(dest="objectif_command", required=True, parser_class=parser_class)
    add = cmds.add_parser("add", help="créer un objectif")
    add.add_argument("--but", required=True)
    add.add_argument("--critere", action="append", default=[], help="commande de fin (répétable)")
    add.add_argument("--budget")
    add.add_argument("--chantier")
    show = cmds.add_parser("show", help="un objectif")
    show.add_argument("id", type=int)
    lst = cmds.add_parser("list", help="les objectifs")
    lst.add_argument("--statut")
    et = cmds.add_parser("etape", help="changer l'étape (reprise après coupure)")
    et.add_argument("id", type=int)
    et.add_argument("--etape", required=True)
    pr = cmds.add_parser("preuve", help="ajouter une preuve horodatée")
    pr.add_argument("id", type=int)
    pr.add_argument("--texte", required=True)
    li = cmds.add_parser("lier", help="lier l'objectif à une session tmux (frein Stop, todo #498) ; --session \"\" délie")
    li.add_argument("id", type=int)
    li.add_argument("--session", required=True)
    rp = cmds.add_parser("reprendre", help="relancer les objectifs ouverts dont la session est coupée ou bloquée par le quota (todo #501)")
    rp.add_argument("--essai", action="store_true", help="ne rien lancer, rendre le plan")
    it = cmds.add_parser("intention", help="noter une intention AVANT une action sensible (n'exécute rien, todo #514)")
    it.add_argument("id", type=int)
    it.add_argument("--texte", required=True)
    it.add_argument("--commande")
    rs = cmds.add_parser("resultat", help="noter le résultat d'une action notée par `intention` (todo #514)")
    rs.add_argument("id", type=int)
    rs.add_argument("--n", type=int, required=True)
    rs.add_argument("--statut", required=True, choices=["ok", "echec"])
    rs.add_argument("--texte")
    cmds.add_parser("stop-hook", help="hook Stop de Claude Code : lit le JSON du hook sur stdin (todo #498)")
    ve = cmds.add_parser("verifier", help="exécuter les commandes de critere_fin (bac à sable) : OK/KO (todo #497)")
    ve.add_argument("id", type=int)
    ve.add_argument("--dossier", help="dossier de travail absolu (défaut : racine du projet)")
    ve.add_argument("--timeout", type=float, default=TIMEOUT_DEFAUT_S, help="secondes par commande (défaut 120)")


def objectifs_dir(settings: Settings) -> Path:
    return settings.var_dir / "objectifs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


def _path(settings: Settings, ident: int) -> Path:
    return objectifs_dir(settings) / f"{ident}.json"


def _ecrire(path: Path, obj: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".objectif-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _Verrou:
    def __init__(self, settings: Settings) -> None:
        d = objectifs_dir(settings)
        d.mkdir(parents=True, exist_ok=True)
        self.fh = open(d / ".verrou", "a")

    def __enter__(self) -> "_Verrou":
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.fh.close()


def lire(settings: Settings, ident: int) -> dict[str, Any]:
    path = _path(settings, ident)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise InvalidRequestError(f"objectif {ident} inconnu") from None


def lister(settings: Settings, statut: str | None = None) -> list[dict[str, Any]]:
    d = objectifs_dir(settings)
    if not d.is_dir():
        return []
    items = []
    for p in sorted((p for p in d.iterdir() if ID_RE.fullmatch(p.name)), key=lambda p: int(p.stem)):
        obj = json.loads(p.read_text(encoding="utf-8"))
        if statut is None or obj.get("statut") == statut:
            items.append(obj)
    return items


def creer(settings: Settings, but: str, criteres: list[str] | None = None, budget: str | None = None,
          chantier: str | None = None) -> dict[str, Any]:
    but = (but or "").strip()
    if not but:
        raise InvalidRequestError("le but ne peut pas être vide")
    if len(but) > BUT_MAX:
        raise InvalidRequestError(f"le but dépasse {BUT_MAX} caractères ({len(but)})")
    with _Verrou(settings):
        ids = [int(p.stem) for p in objectifs_dir(settings).iterdir() if ID_RE.fullmatch(p.name)]
        now = _now()
        obj = {"id": max(ids, default=0) + 1, "but": but, "critere_fin": [c for c in (criteres or []) if c.strip()],
               "budget": budget, "chantier": chantier, "etape": "cadrage", "statut": "ouvert", "preuves": [],
               "cree_le": now, "maj_le": now}
        _ecrire(_path(settings, obj["id"]), obj)
    return obj


def _modifier(settings: Settings, ident: int, change: Any) -> dict[str, Any]:
    with _Verrou(settings):
        obj = lire(settings, ident)
        change(obj)
        obj["maj_le"] = _now()
        _ecrire(_path(settings, ident), obj)
    return obj


def _texte_court(nom: str, valeur: str | None, obligatoire: bool = False) -> str | None:
    valeur = (valeur or "").strip()
    if obligatoire and not valeur:
        raise InvalidRequestError(f"{nom} ne peut pas être vide")
    if len(valeur) > ACTION_MAX:
        raise InvalidRequestError(f"{nom} dépasse {ACTION_MAX} caractères ({len(valeur)})")
    return valeur or None


def intention(settings: Settings, ident: int, texte: str, commande: str | None = None) -> int:
    """Todo #514 : note l'intention avant l'action (rien n'est exécuté). Rend le numéro n de l'action."""
    texte = _texte_court("le texte", texte, True)
    commande = _texte_court("la commande", commande)
    numero: list[int] = []

    def change(o: dict[str, Any]) -> None:
        actions = o.setdefault("actions", [])
        n = max((a["n"] for a in actions), default=0) + 1
        actions.append({"n": n, "le": _now(), "texte": texte, "commande": commande, "statut": "en_cours"})
        numero.append(n)

    _modifier(settings, ident, change)
    return numero[0]


def resultat(settings: Settings, ident: int, n: int, statut: str, texte: str | None = None) -> dict[str, Any]:
    """Todo #514 : complète l'action n (ok|echec, fini_le). Numéro inconnu = refus."""
    if statut not in ("ok", "echec"):
        raise InvalidRequestError(f"statut inconnu : {statut!r} (ok ou echec)")
    texte = _texte_court("le texte", texte)

    def change(o: dict[str, Any]) -> None:
        for a in o.get("actions") or []:
            if a["n"] == n:
                a.update(statut=statut, fini_le=_now(), resultat=texte)
                return
        raise InvalidRequestError(f"action {n} inconnue dans l'objectif {ident}")

    return _modifier(settings, ident, change)


@contextlib.contextmanager
def action(settings: Settings, ident: int, texte: str, commande: str | None = None) -> Iterator[int]:
    """Intention écrite AVANT d'entrer, résultat ok/echec en sortie. Si le processus meurt, l'intention reste en_cours."""
    n = intention(settings, ident, texte, commande)
    try:
        yield n
    except Exception as exc:
        resultat(settings, ident, n, "echec", f"{type(exc).__name__}: {exc}"[:ACTION_MAX])
        raise
    except BaseException:
        # arrêt brutal (KeyboardInterrupt, SystemExit...) : on laisse la trace en_cours, c'est le but
        raise
    else:
        resultat(settings, ident, n, "ok")


def interrompues(obj: dict[str, Any], maintenant: datetime | None = None) -> list[dict[str, Any]]:
    """Actions en_cours depuis plus de 30 min (règle seule, la session liée n'est pas consultée)."""
    ref = maintenant or datetime.now(timezone.utc)
    sortie = []
    for a in obj.get("actions") or []:
        if a.get("statut") == "en_cours" and (ref - datetime.fromisoformat(a["le"])).total_seconds() > INTERROMPUE_S:
            sortie.append(a)
    return sortie


def _dossier_sur(settings: Settings, brut: str | None) -> Path:
    if brut is None:
        return settings.root
    if not brut.startswith("/"):
        raise InvalidRequestError(f"dossier non absolu : {brut!r}")
    path = Path(brut).resolve()
    if not path.is_dir():
        raise InvalidRequestError(f"dossier introuvable : {brut}")
    if path == Path("/") or path == Path.home().resolve():
        raise InvalidRequestError(f"dossier refusé : {path}")
    for interdit in FORBIDDEN_PATHS:
        if _is_relative_to(path, interdit) or _is_relative_to(interdit, path):
            raise InvalidRequestError(f"dossier refusé : {path} exposerait {interdit}")
    return path


def _lancer(commande: str, dossier: Path, timeout: float) -> dict[str, Any]:
    debut = time.monotonic()
    try:
        argv = shlex.split(commande)
        if not argv:
            raise ValueError("commande vide")
    except ValueError as exc:
        return {"commande": commande, "rc": None, "sortie": f"commande illisible : {exc}", "duree": 0.0}
    try:
        proc = subprocess.run(repos.sandbox_argv(argv, tree=dossier, read_only=[]), cwd=dossier,
                              capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
        rc, sortie = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        rc, sortie = None, f"timeout (> {timeout:g} s)"
    except OSError as exc:
        rc, sortie = None, f"lancement impossible : {exc}"
    if len(sortie) > SORTIE_MAX:
        sortie = sortie[:SORTIE_MAX] + "…[tronqué]"
    return {"commande": commande, "rc": rc, "sortie": sortie, "duree": round(time.monotonic() - debut, 2)}


def verifier(settings: Settings, ident: int, dossier: str | None = None,
             timeout: float = TIMEOUT_DEFAUT_S) -> dict[str, Any]:
    if timeout <= 0:
        raise InvalidRequestError("le timeout doit être positif")
    obj = lire(settings, ident)
    cwd = _dossier_sur(settings, dossier)
    criteres = obj.get("critere_fin") or []
    if not criteres:
        resultats: list[dict[str, Any]] = []
        texte = "verifier : KO contrat vide"
        ok = False
    else:
        resultats = [_lancer(c, cwd, timeout) for c in criteres]
        mauvais = [r for r in resultats if r["rc"] != 0]
        ok = not mauvais
        texte = (f"verifier : OK {len(resultats)}/{len(resultats)}" if ok else
                 f"verifier : KO, {len(mauvais)}/{len(resultats)} en échec, dont {mauvais[0]['commande']}")
    _modifier(settings, ident, lambda o: o["preuves"].append({"le": _now(), "texte": texte}))
    return {"ok": ok, "resultats": resultats, "texte": texte}


def lier(settings: Settings, ident: int, session: str) -> dict[str, Any]:
    if session and not SESSION_RE.fullmatch(session):
        raise InvalidRequestError(f"nom de session invalide : {session!r}")

    def change(o: dict[str, Any]) -> None:
        if session:
            for autre in lister(settings, "ouvert"):
                if autre["id"] != ident and autre.get("session") == session:
                    raise InvalidRequestError(f"session {session} déjà liée à l'objectif {autre['id']}")
        o["session"] = session or None
        o["relances_stop"] = 0

    return _modifier(settings, ident, change)


def delier_fermee(settings: Settings, session: str) -> list[int]:
    """Session fermée par l'utilisateur (todo #538) : délie les objectifs ouverts qui lui sont liés, avec une preuve
    « fermée : par l'utilisateur » (préfixe distinct de « reprise : », hors plafond des reprises)."""
    ids = []
    for o in lister(settings, "ouvert"):
        if o.get("session") == session:
            lier(settings, o["id"], "")
            _modifier(settings, o["id"], lambda x: x["preuves"].append(
                {"le": _now(), "texte": f"fermée : par l'utilisateur ({session})"}))
            ids.append(o["id"])
    return ids


def _session_tmux() -> str | None:
    """Nom de la session tmux courante, None hors tmux ou en cas d'échec."""
    if not os.environ.get("TMUX"):
        return None
    try:
        proc = subprocess.run(["tmux", "display-message", "-p", "#S"], capture_output=True, text=True, timeout=5,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    nom = proc.stdout.strip()
    return nom if proc.returncode == 0 and SESSION_RE.fullmatch(nom) else None


def stop_hook(settings: Settings) -> str:
    """Frein du hook Stop (todo #498). Renvoie le texte de stdout : JSON `block` ou chaîne vide (arrêt permis).
    N'exécute jamais verifier ; toute exception = arrêt permis."""
    try:
        nom = _session_tmux()
        if not nom:
            return ""
        lies = [o for o in lister(settings, "ouvert") if o.get("session") == nom]
        if not lies:
            return ""
        ident = lies[0]["id"]
        decision: list[str] = []

        def change(o: dict[str, Any]) -> None:
            preuves = o["preuves"]
            debut = min(int(o.get("etape_pos") or 0), len(preuves))
            pos = next((i for i in range(len(preuves) - 1, -1, -1)
                        if preuves[i].get("texte", "").startswith("verifier : ")), None)
            if pos is not None and preuves[pos]["texte"].startswith("verifier : OK") and pos >= debut:
                o["relances_stop"] = 0
                return
            n = int(o.get("relances_stop") or 0)
            if n < RELANCES_MAX:
                o["relances_stop"] = n + 1
                preuves.append({"le": _now(), "texte": f"frein : relance {n + 1}/{RELANCES_MAX}"})
                decision.append(f"Objectif {ident} pas prouvé fini : lance `ultron-hub objectif verifier {ident} "
                                f"--dossier <d>` ; KO = continue le travail ; bloqué = dis-le à l’utilisateur.")
            else:
                preuves.append({"le": _now(), "texte": f"frein : arrêt permis après {RELANCES_MAX} relances"})

        _modifier(settings, ident, change)
        if decision:
            return json.dumps({"decision": "block", "reason": decision[0]}, ensure_ascii=False)
        return ""
    except BaseException as exc:  # noqa: BLE001 - le frein ne doit jamais coincer une session
        import sys
        print(f"objectif stop-hook : {type(exc).__name__}: {exc}", file=sys.stderr)
        return ""


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.objectif_command
    try:
        if cmd == "add":
            obj = creer(settings, args.but, args.critere, args.budget, args.chantier)
            return envelope(cmd, summary=f"objectif {obj['id']} créé", data=obj)
        if cmd == "show":
            obj = lire(settings, args.id)
            resume = f"objectif {obj['id']} : {obj['statut']}, étape {obj['etape']}"
            if obj.get("actions"):
                obj["interrompues"] = [a["n"] for a in interrompues(obj, _maintenant())]
                for n in obj["interrompues"]:
                    resume += f" ; action interrompue {n}"
            return envelope(cmd, summary=resume, data=obj)
        if cmd == "intention":
            n = intention(settings, args.id, args.texte, args.commande)
            return envelope(cmd, summary=f"objectif {args.id} : intention {n} notée", data={"n": n})
        if cmd == "resultat":
            obj = resultat(settings, args.id, args.n, args.statut, args.texte)
            return envelope(cmd, summary=f"objectif {args.id} : action {args.n} {args.statut}", data=obj)
        if cmd == "list":
            items = lister(settings, args.statut)
            return envelope(cmd, summary=f"{len(items)} objectif(s)", data={"objectifs": items})
        if cmd == "etape":
            if not args.etape.strip():
                raise InvalidRequestError("l'étape ne peut pas être vide")
            obj = _modifier(settings, args.id, lambda o: o.update(etape=args.etape.strip(), etape_pos=len(o["preuves"])))
            return envelope(cmd, summary=f"objectif {obj['id']} : étape {obj['etape']}", data=obj)
        if cmd == "preuve":
            if not args.texte.strip():
                raise InvalidRequestError("la preuve ne peut pas être vide")
            obj = _modifier(settings, args.id, lambda o: o["preuves"].append({"le": _now(), "texte": args.texte.strip()}))
            return envelope(cmd, summary=f"objectif {obj['id']} : {len(obj['preuves'])} preuve(s)", data=obj)
        if cmd == "lier":
            obj = lier(settings, args.id, args.session)
            return envelope(cmd, summary=f"objectif {obj['id']} : session {obj.get('session') or 'déliée'}", data=obj)
        if cmd == "reprendre":
            from ultron_hub import reprise
            res = reprise.reprendre(settings, essai=args.essai)
            return envelope(cmd, summary=res["resume"], data=res)
        if cmd == "verifier":
            res = verifier(settings, args.id, args.dossier, args.timeout)
            return envelope(cmd, status="ok" if res["ok"] else "ko", summary=res["texte"],
                            data={"ok": res["ok"], "resultats": res["resultats"]})
    except InvalidRequestError as exc:
        return envelope(cmd, status="error", summary="objectif refusé", error=str(exc))
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
