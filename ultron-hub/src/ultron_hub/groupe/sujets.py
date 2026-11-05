"""`ultron-hub groupe creer`: the topics of the Telegram group, from the dashboard (todo #246), idempotent.

The job of the retired plugin `/groupe` (domain topics, then one topic per chantier not
archived), without the Telegram message: the group's chat id must already be noted in config/groupe.yaml
(by hand). Direct sending mode only. A Telegram error never raises: it comes back in `status`.
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ultron_hub import envoi
from ultron_hub.chantier import core as chantier_core
from ultron_hub.groupe import chantiers

log = logging.getLogger("ultron_hub.groupe.sujets")

TOPICS = (  # (key, title) in creation order
    ("systeme", "🛠 Système"),
    ("business", "💼 Business"),
    ("perso", "🏠 Perso"),
    ("missions", "🚀 Missions"),
    ("alertes", "🚨 Alertes"),
    ("revue-de-cap", "🔭 Revue de cap"),
    ("entree", "📥 Entrée"),
)
HEADER = "# Groupe Telegram à sujets (écrit par /groupe, todo #198). Pas un secret.\n"
SOCLE = "_socle"
NOM_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def _ecrire(chat_id: str, sujets: dict[str, int], chants: dict[str, int], path: Path) -> None:
    """Same file as the former plugin save (atomic)."""
    corps = HEADER + f"chat_id: {chat_id}\nsujets:\n"
    corps += "".join(f"  {cle}: {sujets[cle]}\n" for cle, _ in TOPICS if cle in sujets)
    if chants:
        corps += "chantiers:\n" + "".join(f"  {k}: {v}\n" for k, v in sorted(chants.items()))
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


def _chantiers_ouverts(prompts: Path) -> list[str]:
    try:
        return sorted(f.stem for f in prompts.glob("*.md") if f.is_file() and f.stem != SOCLE and NOM_RE.fullmatch(f.stem))
    except OSError:
        return []


def creer(settings, appel=None, path: Path | None = None, prompts: Path | None = None) -> dict[str, Any]:
    """Create the missing domain and chantier topics. status: ok | skipped (nothing possible, `summary` says why) | error."""
    appel = appel or envoi.appeler
    path = path or envoi.groupe_path()
    if envoi.mode(settings) != "direct":
        return {"status": "skipped", "summary": "mode hermes : les sujets se créent avec /groupe tapé dans le groupe"}
    etat = envoi.lire_groupe(path)
    if not etat["chat_id"]:
        return {"status": "skipped", "summary": "groupe inconnu : tape /groupe une fois dans le groupe pour le noter"}
    chat, sujets, creees, echecs = etat["chat_id"], dict(etat["sujets"]), [], []
    for cle, titre in TOPICS:
        if cle in sujets:
            continue
        try:
            rep = appel("createForumTopic", {"chat_id": chat, "name": titre})
            sujets[cle] = int(rep["result"]["message_thread_id"])
            _ecrire(chat, sujets, etat["chantiers"], path)  # after each topic: a stop never loses an id
            creees.append(titre)
        except Exception as exc:  # noqa: BLE001
            log.warning("sujet %s : %s", titre, exc)
            return {"status": "error", "creees": creees,
                    "summary": f"sujet « {titre} » non créé ({type(exc).__name__}: {exc})"[:300]}
    prompts = prompts or chantier_core.sources().prompts
    for nom in _chantiers_ouverts(prompts):
        if nom in envoi.lire_groupe(path)["chantiers"]:
            continue
        res = chantiers.creer(settings, nom, chantiers.domaine_de(nom, prompts), appel=appel, path=path)
        (creees if res["status"] == "ok" else echecs).append(nom if res["status"] == "ok" else f"{nom} ({res['summary']})")
    if echecs:
        return {"status": "error", "creees": creees, "summary": ("sujets non créés : " + " ; ".join(echecs))[:300]}
    n = len(creees)
    return {"status": "ok", "creees": creees,
            "summary": f"{n} sujet(s) créé(s)" if n else "tous les sujets existent déjà"}
