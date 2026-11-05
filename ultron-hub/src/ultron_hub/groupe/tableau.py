"""One pinned board per domain topic (Système, Business, Perso) of the Telegram group (todo #202).

Sent once, pinned, then edited in place. The message ids live in var/groupe/tableau.json (not in config/groupe.yaml,
which only /groupe writes). Direct sending mode only: in `hermes` mode, or without a group, nothing happens.
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultron_hub import envoi
from ultron_hub.chantier import core as chantier_core
from ultron_hub.common.settings import Settings

log = logging.getLogger("ultron_hub.groupe.tableau")

MAX_TODOS = 12
# Base topic names of the group (the plugin /groupe command is retired, todo #463).
BASES = {"systeme": "🛠 Système", "business": "💼 Business", "perso": "🏠 Perso"}
RENAME_EVERY = 600  # seconds between two editForumTopic calls on one topic
MAX_NAME = 128
NOT_FOUND = ("message to edit not found", "message can't be edited", "message_id_invalid", "message to pin not found")


def state_path(settings: Settings) -> Path:
    return Path(settings.var_dir) / "groupe" / "tableau.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _domaines(settings: Settings) -> dict[str, str]:
    """{chantier subject: domain key} read from the subject files."""
    src = chantier_core.sources()
    out = {}
    for name in chantier_core.subjects(src):
        try:
            out[name] = chantier_core.parse_prompt((src.prompts / f"{name}.md").read_text(encoding="utf-8"), name)["domaine"]
        except OSError:
            continue
    return out


def _quota_line(settings: Settings) -> str:
    try:
        from ultron_hub.usage import text as usage_text
        from ultron_hub.usage.collect import usage
        result = usage(Path(settings.var_dir) / "cache" / "usage.json", datetime.now(timezone.utc))
        return "📊 " + usage_text.summary_line(result).removeprefix("usage : ")
    except Exception:  # noqa: BLE001 - the board must not depend on the usage endpoint
        return "📊 quota indisponible"


def build_texts(settings: Settings, now: datetime | None = None) -> dict[str, str]:
    """{domain key: board text}; reads the todo database and the git branches, never the network."""
    return build_all(settings, now)[0]


def topic_name(cle: str, pretes: int) -> str:
    base = BASES[cle]
    return (f"{base} · {pretes} prête{'s' if pretes > 1 else ''}" if pretes > 0 else base)[:MAX_NAME]


def build_all(settings: Settings, now: datetime | None = None) -> tuple[dict[str, str], dict[str, int]]:
    """(board texts, number of ready branches) per domain key."""
    todos, has, _ = chantier_core.open_todos(settings)
    domaine_de = _domaines(settings) if has else {}
    try:
        from ultron_hub import branches
        pretes = branches.branch_map(settings)
    except Exception:  # noqa: BLE001 - "ready branches" is a bonus
        pretes = {}
    quota = _quota_line(settings)
    heure = (now or datetime.now()).strftime("%Hh%M")
    textes, nombres = {}, {}
    for cle, icone, titre in chantier_core.DOMAINES:
        siennes = [t for t in todos if domaine_de.get(t["chantier"] or "") == cle]
        lignes = [f"{icone} {titre} · {len(siennes)} todo(s) ouverte(s)", ""]
        for t in siennes[:MAX_TODOS]:
            lignes.append(f"• #{t['id']} {t['title']}")
        if len(siennes) > MAX_TODOS:
            lignes.append(f"… et {len(siennes) - MAX_TODOS} autre(s)")
        if not siennes:
            lignes.append("Rien d'ouvert.")
        pr = [t for t in siennes if pretes.get(t["id"], {}).get("ahead", 0) > 0]
        if pr:
            lignes += ["", "🌿 Branches prêtes : " + ", ".join(f"#{t['id']}" for t in pr)]
        lignes += ["", quota, f"🕐 mis à jour à {heure}"]
        textes[cle] = "\n".join(lignes)[:envoi.MAX_MESSAGE]
        nombres[cle] = len(pr)
    return textes, nombres


def _nom(chat: str, thread: int, voulu: str, entree: dict[str, Any], appel, maintenant: float) -> None:
    """Rename the topic when the wanted name differs from the last one set, at most every RENAME_EVERY seconds."""
    dernier = entree.get("nom_at")
    if entree.get("nom") == voulu or (dernier is not None and maintenant - float(dernier) < RENAME_EVERY):
        return
    try:
        appel("editForumTopic", {"chat_id": chat, "message_thread_id": thread, "name": voulu})
    except RuntimeError as exc:
        if "topic_not_modified" not in str(exc).lower():
            raise
    entree.update(nom=voulu, nom_at=maintenant)


def _un(chat: str, thread: int, texte: str, entree: dict[str, Any], appel) -> str:
    """Create, pin or edit one board; returns what was done."""
    empreinte = hashlib.sha256(texte.rsplit("\n", 1)[0].encode()).hexdigest()[:16]
    mid = entree.get("message_id")
    fait = ""
    if mid and entree.get("empreinte") != empreinte:
        try:
            appel("editMessageText", {"chat_id": chat, "message_id": mid, "text": texte})
            fait = "modifié"
        except RuntimeError as exc:
            erreur = str(exc).lower()
            if "message is not modified" in erreur:
                fait = "inchangé"
            elif any(m in erreur for m in NOT_FOUND):
                mid = None
            else:
                raise
    elif mid:
        fait = "inchangé"
    if not mid:
        rep = appel("sendMessage", {"chat_id": chat, "message_thread_id": thread, "text": texte})
        mid = int(rep["result"]["message_id"])
        entree["epingle"] = False
        fait = "créé"
    entree.update(message_id=mid, empreinte=empreinte)
    if not entree.get("epingle"):
        appel("pinChatMessage", {"chat_id": chat, "message_id": mid, "disable_notification": True})
        entree["epingle"] = True
        fait += " + épinglé"
    return fait


def rafraichir(settings: Settings, appel=None, textes: dict[str, str] | None = None,
               nombres: dict[str, int] | None = None, clock=time.time) -> dict[str, Any]:
    """Refresh the three boards and topic names. Without `nombres` (tests), topics are not renamed. Never raises. status: ok | skipped (hermes mode, no group) | error."""
    appel = appel or envoi.appeler
    try:
        if envoi.mode(settings) != "direct":
            return {"status": "skipped", "summary": "tableaux : mode hermes, rien à faire", "sujets": {}}
        cibles = {cle: envoi.destination_sujet(titre) for cle, _, titre in chantier_core.DOMAINES}
        if not any(cibles.values()):
            return {"status": "skipped", "summary": "tableaux : groupe absent (/groupe), rien à faire", "sujets": {}}
        if not textes:
            textes, nombres = build_all(settings)
        path = state_path(settings)
        state = _load(path)
        faits, erreurs = {}, []
        for cle, dest in cibles.items():
            if not dest:
                faits[cle] = "sujet absent"
                continue
            entree = dict(state.get(cle) or {})
            try:
                faits[cle] = _un(dest[0], dest[1], textes[cle], entree, appel)
            except Exception as exc:  # noqa: BLE001
                faits[cle] = "erreur"
                erreurs.append(f"{cle} : {exc}"[:200])
            if nombres is not None and cle in nombres:
                try:
                    _nom(dest[0], dest[1], topic_name(cle, nombres[cle]), entree, appel, clock())
                except Exception as exc:  # noqa: BLE001
                    erreurs.append(f"{cle} nom : {exc}"[:200])
            state[cle] = entree
        _save(path, state)
        if erreurs:
            return {"status": "error", "summary": "tableaux : " + " ; ".join(erreurs), "sujets": faits}
        return {"status": "ok", "summary": "tableaux : " + ", ".join(f"{k} {v}" for k, v in faits.items()), "sujets": faits}
    except Exception as exc:  # noqa: BLE001
        log.warning("tableaux en erreur : %s", exc)
        return {"status": "error", "summary": f"tableaux : {type(exc).__name__}: {exc}"[:300], "sujets": {}}
