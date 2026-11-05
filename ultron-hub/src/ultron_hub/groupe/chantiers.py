"""One forum topic per chantier in the Telegram group (todo #203, spec ~/notes/spec-groupe-sujets.md §6 point 2).

`creer` at the creation (or reopening) of a chantier: createForumTopic named « <domain icon> <chantier> », id noted in
config/groupe.yaml under `chantiers:`. `fermer` at the archiving: closeForumTopic (the id is kept, so a reopening
reopens the same topic). Direct sending mode and a configured group only; otherwise nothing happens. A Telegram error
never raises: it comes back in the result (`status: error`) and never breaks the chantier command.
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ultron_hub import envoi
from ultron_hub.chantier import core as chantier_core

log = logging.getLogger("ultron_hub.groupe.chantiers")

_DOMAINE = re.compile(r"^Domaine\s*:\s*`?([A-Za-z0-9_-]+)`?", re.MULTILINE)


def nom_sujet(sujet: str, domaine: str | None) -> str:
    """« 🛠 sujet » : the icon of the domain, « ❔ » when the chantier has none (yet)."""
    icone = next((i for cle, i, _ in chantier_core.DOMAINES if cle == domaine), chantier_core.A_RANGER[1])
    return f"{icone} {sujet}"


def domaine_de(sujet: str, prompts: Path | None = None) -> str | None:
    try:
        dossier = prompts or chantier_core.sources().prompts
        m = _DOMAINE.search((dossier / f"{sujet}.md").read_text(encoding="utf-8"))
        return m.group(1) if m else None
    except OSError:
        return None


def ecrire(chantiers: dict[str, int], path: Path | None = None) -> None:
    """Rewrite only the `chantiers:` section of groupe.yaml (atomic); the rest of the file is kept as is."""
    path = path or envoi.groupe_path()
    gardees: list[str] = []
    dedans = False
    for ligne in path.read_text().splitlines():
        if ligne.startswith("chantiers:"):
            dedans = True
        elif dedans and ligne[:1] in (" ", "\t", "#") or dedans and not ligne.strip():
            continue
        else:
            dedans = False
            gardees.append(ligne)
    while gardees and not gardees[-1].strip():
        gardees.pop()
    corps = "\n".join(gardees) + "\n"
    if chantiers:
        corps += "chantiers:\n" + "".join(f"  {k}: {v}\n" for k, v in sorted(chantiers.items()))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".groupe.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(corps)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _contexte(settings, path: Path | None) -> tuple[dict, str] | str:
    """(group state, chat id) when the topic calls can be made, else the reason to skip."""
    if envoi.mode(settings) != "direct":
        return "mode hermes, rien à faire"
    etat = envoi.lire_groupe(path)
    if not etat["chat_id"]:
        return "groupe absent (/groupe), rien à faire"
    return etat, etat["chat_id"]


def creer(settings, sujet: str, domaine: str | None = None, appel=None, path: Path | None = None) -> dict[str, Any]:
    """Create (or reopen, when its id is already known) the topic of one chantier. Never raises."""
    appel = appel or envoi.appeler
    try:
        ctx = _contexte(settings, path)
        if isinstance(ctx, str):
            return {"status": "skipped", "summary": f"sujet de chantier : {ctx}"}
        etat, chat = ctx
        thread = etat["chantiers"].get(sujet)
        if thread:
            appel("reopenForumTopic", {"chat_id": chat, "message_thread_id": thread})
            return {"status": "ok", "summary": f"sujet de {sujet} rouvert", "thread": thread}
        rep = appel("createForumTopic", {"chat_id": chat, "name": nom_sujet(sujet, domaine)[:128]})
        thread = int(rep["result"]["message_thread_id"])
        ecrire({**etat["chantiers"], sujet: thread}, path)
        return {"status": "ok", "summary": f"sujet de {sujet} créé", "thread": thread}
    except Exception as exc:  # noqa: BLE001
        log.warning("sujet de chantier %s : %s", sujet, exc)
        msg = str(exc).lower()
        if "topic_not_modified" in msg:  # already open
            return {"status": "ok", "summary": f"sujet de {sujet} déjà ouvert"}
        return {"status": "error", "summary": f"sujet de chantier {sujet} : {type(exc).__name__}: {exc}"[:300]}


def fermer(settings, sujet: str, appel=None, path: Path | None = None) -> dict[str, Any]:
    """Close the topic of an archived chantier (id kept in groupe.yaml). Never raises."""
    appel = appel or envoi.appeler
    try:
        ctx = _contexte(settings, path)
        if isinstance(ctx, str):
            return {"status": "skipped", "summary": f"sujet de chantier : {ctx}"}
        etat, chat = ctx
        thread = etat["chantiers"].get(sujet)
        if not thread:
            return {"status": "skipped", "summary": f"sujet de chantier : {sujet} n'a pas de sujet"}
        appel("closeForumTopic", {"chat_id": chat, "message_thread_id": thread})
        return {"status": "ok", "summary": f"sujet de {sujet} fermé", "thread": thread}
    except Exception as exc:  # noqa: BLE001
        log.warning("fermeture du sujet de %s : %s", sujet, exc)
        if "topic_not_modified" in str(exc).lower():  # already closed
            return {"status": "ok", "summary": f"sujet de {sujet} déjà fermé"}
        return {"status": "error", "summary": f"sujet de chantier {sujet} : {type(exc).__name__}: {exc}"[:300]}
