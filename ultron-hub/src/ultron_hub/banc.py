"""Todo #513: historique du banc de 10 objectifs (chemin A'), une ligne JSON par passe dans `var/banc/historique.jsonl`.

`banc noter --reussis N --sur 10 [--cout P] [--note T] [--date AAAA-MM-JJ]` ajoute une ligne (écriture atomique en ajout) ;
`banc historique` relit tout (lecture seule) et rend la tendance (« 7/10 puis 8/10 »).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings

SUR_MAX = 50
NOTE_MAX = 200
COUT_MAX = 1_000_000


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"banc.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    top = sub.add_parser("banc", help="historique du banc de 10 objectifs: noter, historique")
    cmds = top.add_subparsers(dest="banc_command", required=True, parser_class=parser_class)
    no = cmds.add_parser("noter", help="noter une passe du banc")
    no.add_argument("--reussis", type=int, required=True)
    no.add_argument("--sur", type=int, required=True)
    no.add_argument("--cout", type=int, help="coût en points")
    no.add_argument("--note", default="")
    no.add_argument("--date", help="AAAA-MM-JJ (défaut : aujourd'hui)")
    cmds.add_parser("historique", help="les passes notées et la tendance (lecture seule)")


def historique_path(settings: Settings) -> Path:
    return settings.var_dir / "banc" / "historique.jsonl"


def valider(reussis: int, sur: int, cout: int | None, note: str, jour: str | None) -> dict[str, Any]:
    if not 0 <= reussis <= sur <= SUR_MAX or sur < 1:
        raise InvalidRequestError(f"réussis/sur refusé : il faut 0 <= réussis <= sur <= {SUR_MAX} (et sur >= 1)")
    if cout is not None and not 0 <= cout <= COUT_MAX:
        raise InvalidRequestError(f"coût refusé : il faut 0 <= coût <= {COUT_MAX}")
    note = (note or "").strip()
    if len(note) > NOTE_MAX or any(ord(c) < 32 for c in note):
        raise InvalidRequestError(f"note refusée : {NOTE_MAX} caractères au plus, sans retour à la ligne")
    if jour is None:
        jour = date.today().isoformat()
    else:
        try:
            if len(jour) != 10 or datetime.strptime(jour, "%Y-%m-%d").date().isoformat() != jour:
                raise ValueError
        except ValueError:
            raise InvalidRequestError("date refusée : format AAAA-MM-JJ attendu") from None
    return {"date": jour, "reussis": reussis, "sur": sur, "cout": cout, "note": note}


def noter(settings: Settings, reussis: int, sur: int, cout: int | None = None, note: str = "",
          jour: str | None = None) -> dict[str, Any]:
    ligne = valider(reussis, sur, cout, note, jour)
    path = historique_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(ligne, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, data)  # une seule écriture en ajout : une ligne entière ou rien
        os.fsync(fd)
    finally:
        os.close(fd)
    return ligne


def lire(settings: Settings) -> list[dict[str, Any]]:
    path = historique_path(settings)
    if not path.exists():
        return []
    passes = []
    for brut in path.read_text(encoding="utf-8").splitlines():
        try:
            p = json.loads(brut)
            valider(p["reussis"], p["sur"], p.get("cout"), p.get("note", ""), p["date"])
        except (ValueError, KeyError, TypeError, InvalidRequestError):
            continue  # ligne abîmée : ignorée, jamais fatale
        passes.append({"date": p["date"], "reussis": p["reussis"], "sur": p["sur"], "cout": p.get("cout"),
                       "note": p.get("note", "")})
    passes.sort(key=lambda p: p["date"])  # tri stable : l'ordre d'ajout départage un même jour
    return passes


def _jj_mm(d: str) -> str:
    return f"{d[8:10]}/{d[5:7]}"


def texte(passes: list[dict[str, Any]]) -> str:
    out = []
    for p in passes:
        s = f"{_jj_mm(p['date'])} : {p['reussis']}/{p['sur']}"
        if p["cout"] is not None:
            s += f", {p['cout']} pts"
        if p["note"]:
            s += f" ({p['note']})"
        out.append(s)
    return " ; ".join(out)


def tendance(passes: list[dict[str, Any]]) -> str:
    return " puis ".join(f"{p['reussis']}/{p['sur']}" for p in passes)


def historique(settings: Settings) -> dict[str, Any]:
    passes = lire(settings)
    return {"passes": passes, "texte": texte(passes)}


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.banc_command
    try:
        if cmd == "noter":
            p = noter(settings, args.reussis, args.sur, args.cout, args.note, args.date)
            return envelope(cmd, summary=f"passe du {_jj_mm(p['date'])} notée : {p['reussis']}/{p['sur']}", data=p)
        if cmd == "historique":
            d = historique(settings)
            return envelope(cmd, summary=tendance(d["passes"]) or "aucune passe notée", data=d)
        return envelope(cmd, status="error", summary="banc failed", error=f"commande inconnue : {cmd}")
    except InvalidRequestError as exc:
        return envelope(cmd, status="error", summary="banc refusé", error=str(exc))
