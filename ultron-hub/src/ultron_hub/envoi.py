"""Point d'envoi Telegram commun (todo #194, spec ~/notes/spec-porte-directe.md §3 étape 2).

Tous les modules qui écrivent à l'utilisateur passent par `envoyer` : un réglage (`envoi.mode` dans
config/settings.yaml, ou la variable ULTRON_ENVOI_MODE) choisit `hermes` (défaut : `hermes send`, comportement
d'origine) ou `direct` (Bot API en HTTPS, sans Hermes). Jamais d'exception pour l'appelant : un échec est un
`Resultat` de statut `error`. Le jeton (TELEGRAM_BOT_TOKEN) vient de l'environnement et n'est jamais journalisé.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from datetime import datetime
import re
import shutil
import unicodedata
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("ultron_hub.envoi")

MODES = ("hermes", "direct")
MODE_ENV = "ULTRON_ENVOI_MODE"
MODE_FILE = "var/envoi-mode"
TELEGRAM_ENV = "ULTRON_TELEGRAM"   # on|off, wins over the file (tests, emergencies)
TELEGRAM_FILE = "telegram-etat"    # in var/: "on" or "off"; absent = off (Telegram is a fallback since todo #290)
SUJETS_TOUJOURS = ("alertes",)     # always sent, even when Telegram is off (with any message that has a cle_alerte)
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENVS = ("ULTRON_TELEGRAM_CHAT_ID", "TELEGRAM_HOME_CHANNEL")
API = "https://api.telegram.org"
MAX_MESSAGE = 4096
MEDIA_PREFIX = "MEDIA:"
_PHOTO = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO = {".mp4", ".mov", ".webm", ".mkv"}
_VOICE = {".ogg", ".oga", ".opus"}
_AUDIO = {".mp3", ".m4a", ".wav", ".flac"}


SUJET_LIGNE = re.compile(r"^(\s*)([a-z0-9_-]+):\s*(-?\d+)\s*$")
SECTION_LIGNE = re.compile(r"^([a-z_]+):\s*$")


def _cle_sujet(nom: str) -> str:
    """« Revue de cap » / « Système » -> `revue-de-cap` / `systeme` (clés de config/groupe.yaml)."""
    nom = unicodedata.normalize("NFD", str(nom)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]+", "-", nom).strip("-")


def groupe_path() -> Path:
    from ultron_hub.common.settings import project_root
    return project_root() / "config" / "groupe.yaml"


def lire_groupe(path: Path | None = None) -> dict:
    """{'chat_id': str | None, 'sujets': {clé: id}, 'chantiers': {nom: id}} ; fichier absent ou illisible = vide."""
    out: dict = {"chat_id": None, "sujets": {}, "chantiers": {}}
    section = "sujets"  # un ancien fichier sans en-tête de section : tout est sujet
    try:
        lignes = (path or groupe_path()).read_text().splitlines()
    except OSError:
        return out
    for ligne in lignes:
        h = SECTION_LIGNE.match(ligne)
        if h:
            section = h.group(1)
            continue
        m = SUJET_LIGNE.match(ligne)
        if not m:
            continue
        if m.group(1) == "" and m.group(2) == "chat_id":
            out["chat_id"] = m.group(3)
        elif m.group(1) and section in ("sujets", "chantiers"):
            out[section][m.group(2)] = int(m.group(3))
    return out


def destination_sujet(sujet: str | None, path: Path | None = None, chantier: str | None = None) -> tuple[str, int] | None:
    """(chat_id du groupe, id du sujet) ou None : sujet vide/inconnu, fichier absent ou illisible = conversation privée.

    Avec `chantier` : le sujet du chantier s'il existe, sinon celui du domaine (`sujet`)."""
    if not sujet and not chantier:
        return None
    try:
        etat = lire_groupe(path)
        chat = etat["chat_id"]
        thread = etat["chantiers"].get(str(chantier).strip().lower()) if chantier else None
        if not thread and sujet:
            thread = etat["sujets"].get(_cle_sujet(sujet))
        return (chat, thread) if chat and thread else None
    except Exception:  # noqa: BLE001 - jamais d'erreur : repli sur la conversation privée
        return None


@dataclass
class Resultat:
    status: str = "ok"            # "ok" | "error"
    detail: str = ""              # message d'erreur (jeton masqué) ou ""
    envoyes: int = 0              # messages / pièces jointes partis
    erreurs: list[str] = field(default_factory=list)
    message_id: int | None = None  # mode direct : id du dernier sendMessage (todo #208), sinon None
    chat: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail, "sent": self.envoyes}


def _erreur(detail: str, envoyes: int = 0) -> Resultat:
    return Resultat("error", detail, envoyes, [detail])


def _mode_fichier() -> str:
    """Contenu de `var/envoi-mode` (racine du projet), vide si absent ou illisible."""
    try:
        from ultron_hub.common.settings import project_root
        return (project_root() / MODE_FILE).read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""


def mode(settings=None) -> str:
    """`hermes` (défaut) ou `direct` : variable d'environnement, sinon `var/envoi-mode` (hors git, posé par la bascule),
    sinon config ; toute valeur inconnue = hermes."""
    value = os.environ.get(MODE_ENV) or _mode_fichier()
    if not value:
        try:
            if settings is None:
                from ultron_hub.common.settings import load_settings
                settings = load_settings()
            value = getattr(settings, "envoi_mode", "hermes")
        except Exception:  # noqa: BLE001 - un réglage illisible ne casse jamais l'envoi
            value = "hermes"
    value = str(value or "hermes").strip().lower()
    return value if value in MODES else "hermes"


def decouper(texte: str, taille: int = MAX_MESSAGE) -> list[str]:
    """Morceaux de `taille` caractères au plus, coupés sur une fin de ligne (une ligne trop longue est coupée net)."""
    morceaux: list[str] = []
    cour = ""
    for ligne in texte.splitlines(keepends=True):
        while len(ligne) > taille:
            if cour:
                morceaux.append(cour)
                cour = ""
            morceaux.append(ligne[:taille])
            ligne = ligne[taille:]
        if len(cour) + len(ligne) > taille:
            morceaux.append(cour)
            cour = ""
        cour += ligne
    if cour:
        morceaux.append(cour)
    return [m for m in morceaux if m.strip()]


def separer(texte: str) -> tuple[str, list[str]]:
    """(texte sans les lignes `MEDIA:<chemin>`, chemins des pièces jointes)."""
    lignes, medias = [], []
    for ligne in texte.splitlines():
        if ligne.strip().startswith(MEDIA_PREFIX) and ligne.strip()[len(MEDIA_PREFIX):].strip():
            medias.append(ligne.strip()[len(MEDIA_PREFIX):].strip())
        else:
            lignes.append(ligne)
    return "\n".join(lignes).strip(), medias


def _masquer(texte: str, jeton: str) -> str:
    texte = texte.replace(jeton, "***") if jeton else texte
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", texte)


def _urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - URL https fixe de l'API Telegram


def _chat(target: str, settings) -> str | None:
    if target.startswith("telegram:") and target[9:].strip():
        return target[9:].strip()
    if re.fullmatch(r"-?\d+", target):
        return target
    if target not in ("", "telegram"):
        return None
    configured = getattr(settings, "envoi_chat_id", None) if settings is not None else None
    if not configured and settings is None:
        try:
            from ultron_hub.common.settings import load_settings
            configured = load_settings().envoi_chat_id
        except Exception:  # noqa: BLE001
            configured = None
    for value in (configured, *(os.environ.get(name) for name in CHAT_ENVS)):
        if value and str(value).strip():
            return str(value).strip()
    return None


def _appel(jeton: str, methode: str, champs: dict, fichier: Path | None, champ_fichier: str, timeout: float) -> dict:
    url = f"{API}/bot{jeton}/{methode}"
    if fichier is None:
        corps = json.dumps(champs).encode("utf-8")
        entetes = {"Content-Type": "application/json"}
    else:
        frontiere = uuid.uuid4().hex
        morceaux = []
        for nom, valeur in champs.items():
            morceaux.append(f'--{frontiere}\r\nContent-Disposition: form-data; name="{nom}"\r\n\r\n{valeur}\r\n'.encode())
        ctype = mimetypes.guess_type(fichier.name)[0] or "application/octet-stream"
        morceaux.append((f'--{frontiere}\r\nContent-Disposition: form-data; name="{champ_fichier}"; '
                         f'filename="{fichier.name}"\r\nContent-Type: {ctype}\r\n\r\n').encode())
        morceaux.append(fichier.read_bytes())
        morceaux.append(f"\r\n--{frontiere}--\r\n".encode())
        corps = b"".join(morceaux)
        entetes = {"Content-Type": f"multipart/form-data; boundary={frontiere}"}
    req = urllib.request.Request(url, data=corps, headers=entetes, method="POST")
    try:
        with _urlopen(req, timeout) as rep:
            data = json.loads(rep.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            data = {}
        raise RuntimeError(f"HTTP {exc.code}: {data.get('description') or exc.reason}") from None
    if not (isinstance(data, dict) and data.get("ok")):
        raise RuntimeError(str((data or {}).get("description") or "réponse Telegram invalide") if isinstance(data, dict)
                           else "réponse Telegram invalide")
    return data


def _methode(fichier: Path) -> tuple[str, str]:
    ext = fichier.suffix.lower()
    if ext in _PHOTO:
        return "sendPhoto", "photo"
    if ext in _VIDEO:
        return "sendVideo", "video"
    if ext in _VOICE:
        return "sendVoice", "voice"
    if ext in _AUDIO:
        return "sendAudio", "audio"
    return "sendDocument", "document"


CALLBACK_MAX = 64  # octets d'un callback_data (limite Telegram)


def clavier(boutons: list[list[tuple[str, str]]]) -> dict:
    """`reply_markup` d'un clavier en ligne (todo #201). `boutons` = lignes de (libellé, callback_data), comme les
    écrans du plugin (`hermes_plugin/ui.Button`). Les callback_data viennent tels quels des fabriques du plugin : ce
    module n'en invente aucun. Un bouton mal formé lève ValueError (rien n'est envoyé)."""
    lignes = []
    for ligne in boutons:
        rang = []
        for libelle, data in ligne:
            if not (isinstance(libelle, str) and libelle.strip() and isinstance(data, str) and data
                    and len(data.encode("utf-8")) <= CALLBACK_MAX):
                raise ValueError(f"bouton invalide : {libelle!r}")
            rang.append({"text": libelle, "callback_data": data})
        if rang:
            lignes.append(rang)
    if not lignes:
        raise ValueError("clavier vide")
    return {"inline_keyboard": lignes}


def _direct(texte: str, target: str, settings, timeout_s: float, sujet: str | None = None,
            boutons: list | None = None, chantier: str | None = None, thread: int | None = None) -> Resultat:
    jeton = os.environ.get(TOKEN_ENV, "").strip()
    if not jeton:
        return _erreur(f"envoi direct impossible : {TOKEN_ENV} absent de l'environnement")
    dest = destination_sujet(sujet, chantier=chantier)
    if thread:  # explicit topic id (todo #206: topic of a mission), only if the group is configured
        groupe = lire_groupe().get("chat_id")
        dest = (groupe, int(thread)) if groupe else dest
    chat, thread = (dest[0], dest[1]) if dest else (_chat(target, settings), None)
    if not chat:
        return _erreur("envoi direct impossible : destination inconnue (envoi.chat_id ou ULTRON_TELEGRAM_CHAT_ID)")
    extra = {"message_thread_id": thread} if thread else {}
    corps, medias = separer(texte)
    res = Resultat()
    taches = [("sendMessage", {"chat_id": chat, "text": m, **extra}, None, "") for m in decouper(corps)]
    if boutons and taches:  # le clavier va sur le dernier morceau de texte (jamais sur une pièce jointe)
        taches[-1][1]["reply_markup"] = clavier(boutons)
    for chemin in medias:
        fichier = Path(chemin).expanduser()
        if not fichier.is_file():
            res.erreurs.append(f"pièce jointe introuvable : {chemin}")
            continue
        methode, champ = _methode(fichier)
        taches.append((methode, {"chat_id": chat, **extra}, fichier, champ))
    for methode, champs, fichier, champ in taches:
        try:
            retour = _appel(jeton, methode, champs, fichier, champ, timeout_s)
            res.envoyes += 1
            msg = retour.get("result") if isinstance(retour.get("result"), dict) else {}
            if methode == "sendMessage" and isinstance(msg.get("message_id"), int):
                res.message_id, res.chat = msg["message_id"], str(chat)
        except Exception as exc:  # noqa: BLE001 - jamais d'exception pour l'appelant
            res.erreurs.append(_masquer(f"{methode} : {type(exc).__name__}: {exc}"[:300], jeton))
    if not taches and not res.erreurs:
        res.erreurs.append("message vide")
    if res.erreurs:
        res.status, res.detail = "error", " ; ".join(res.erreurs)
        log.warning("envoi direct en erreur : %s", res.detail)
    return res


def _hermes(texte: str, target: str, binary: str | None, style: str, timeout_s: float) -> Resultat:
    binary = binary or shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
    if style == "stdin":
        cmd, entree = [binary, "send", "--to", target, "--quiet", "--file", "-"], texte
    else:
        cmd, entree = [binary, "send", "-t", target, texte], None
    try:
        proc = subprocess.run(cmd, input=entree, text=True, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return _erreur(f"hermes send timed out after {timeout_s:.0f}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return _erreur(f"hermes send impossible : {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return _erreur(f"hermes send failed (exit {proc.returncode}): {detail}")
    return Resultat(envoyes=1)


def envoyer(texte: str, *, target: str = "telegram", binary: str | None = None, style: str = "arg",
            timeout_s: float = 60, settings=None, sujet: str | None = None,
            boutons: list[list[tuple[str, str]]] | None = None,
            chantier: str | None = None, thread: int | None = None,
            cle_alerte: str | None = None, genre: str | None = None, fil_seul: bool = False) -> Resultat:
    """Envoie `texte` (les lignes `MEDIA:<chemin>` sont des pièces jointes en mode direct). Ne lève jamais.

    `sujet` (ex. `missions`, `alertes`, `revue-de-cap`) range le message dans le sujet du groupe (config/groupe.yaml) en mode
    direct ; en mode `hermes`, ou si le groupe / le sujet est inconnu, le message part en conversation privée.
    `chantier` (nom du chantier, todo #203) : le sujet du chantier s'il existe, sinon celui du domaine `sujet`.
    `thread` (todo #206) : id d'un sujet précis du groupe (celui d'une mission), prioritaire sur `sujet` et `chantier`.

    `boutons` (todo #201) : lignes de (libellé, callback_data) en clavier en ligne sous le dernier morceau de texte, en
    mode direct seulement ; le mode `hermes` envoie le texte seul. Un bouton mal formé = statut `error`, rien envoyé.

    `cle_alerte` (todo #219) : clé stable d'une alerte ; en mode direct, l'id du message est gardé (`enregistrer`) pour que
    `marquer_reglee(cle)` le change en « ✅ réglée à HHhMM » quand l'alerte est réglée.

    `fil_seul` (todo #393) : rien ne part sur Telegram, même pour un sujet toujours actif ; push et historique du Fil
    sont gardés (veille à canal « fil »).

    `genre` (todo #343) : genre de la notification dans le Fil (`notifs.GENRES`) ; absent = déduit du texte par script.

    `binary` et `style` (`arg` = texte en argument, `stdin` = `--file -`) ne servent qu'au mode `hermes`, et au repli du
    mode direct quand le jeton manque dans l'environnement (todo #215).

    Web Push (todo #240) : APRÈS Telegram, un résumé nettoyé part aussi vers le dashboard installé sur le téléphone
    (`dashboard.push.notify`) s'il y est abonné ; un échec push ne change jamais le résultat Telegram.
    """
    if not fil_seul and (telegram_actif(settings) or cle_alerte or sujet in SUJETS_TOUJOURS):
        res = _envoyer_telegram(texte, target, binary, style, timeout_s, settings, sujet, boutons, chantier, thread, cle_alerte)
        _pousser(texte, sujet, chantier, boutons, res.ok, settings, genre)
        return res
    # Telegram is off: push and history of the Fil are kept, nothing leaves for Telegram (todo #290). The detail says
    # where it went, never « coupé »: sessions read it as a failure (todo #447).
    pousses = _pousser(texte, sujet, chantier, boutons, True, settings, genre)
    push = "push absent" if pousses is None else f"push : {pousses} appareil(s)"
    return Resultat("ok", f"envoyé au Fil ({push})", 0)


def telegram_path(settings=None) -> Path:
    from ultron_hub.common.settings import project_root
    var = getattr(settings, "var_dir", None) or (project_root() / "var")
    return Path(var) / TELEGRAM_FILE


def telegram_actif(settings=None) -> bool:
    """Telegram notifications on? Default OFF: variable ULTRON_TELEGRAM, else `var/telegram-etat`; unreadable = off."""
    value = os.environ.get(TELEGRAM_ENV, "").strip().lower()
    if not value:
        try:
            value = telegram_path(settings).read_text(encoding="utf-8").strip().lower()
        except Exception:  # noqa: BLE001
            value = ""
    return value == "on"


def regler_telegram(actif: bool, settings=None) -> Path:
    path = telegram_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("on\n" if actif else "off\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _pousser(texte: str, sujet: str | None, chantier: str | None, boutons, telegram_ok: bool = True, settings=None,
             genre: str | None = None) -> int | None:
    """Web Push, then the history of notifications (todo #276): neither ever changes the result of the send.
    Returns how many devices accepted the push, None if it failed."""
    tab = "briefing"
    pousses = None
    try:
        from ultron_hub.dashboard import push
        tab = push.tab_for(texte, sujet, chantier, boutons)
        pousses = push.notify(texte, tab=tab)
    except Exception as exc:  # noqa: BLE001 - le push ne casse jamais l'envoi
        log.warning("push impossible : %s", type(exc).__name__)
    try:
        from ultron_hub import notifs
        notifs.noter(texte, tab, telegram_ok, settings, genre=genre)
    except Exception as exc:  # noqa: BLE001
        log.warning("historique des notifications impossible : %s", type(exc).__name__)
    return pousses


def _envoyer_telegram(texte, target, binary, style, timeout_s, settings, sujet, boutons, chantier, thread,
                      cle_alerte) -> Resultat:
    try:
        if mode(settings) == "direct":
            if os.environ.get(TOKEN_ENV, "").strip():
                res = _direct(texte, target, settings, timeout_s, sujet, boutons, chantier, thread)
                if cle_alerte and res.ok:
                    enregistrer(cle_alerte, res, texte, settings)
                return res
            # Seul ultron-bot reçoit le jeton (EnvironmentFile) : timers, missions et sessions se replient sur
            # `hermes send`, qui marche sans le gateway (todo #215, panne du 18/10). Texte seul, conversation privée.
            log.info("envoi direct sans %s : repli sur hermes send", TOKEN_ENV)
            res = _hermes(texte, target, binary, style, timeout_s)
            if not res.ok:
                res.detail = f"envoi direct impossible ({TOKEN_ENV} absent de l'environnement), repli hermes : {res.detail}"
            return res
        return _hermes(texte, target, binary, style, timeout_s)
    except Exception as exc:  # noqa: BLE001
        return _erreur(_masquer(f"envoi impossible : {type(exc).__name__}: {exc}"[:300], os.environ.get(TOKEN_ENV, "")))


def appeler(methode: str, champs: dict, timeout_s: float = 15) -> dict:
    """Appel Bot API direct (pin, edit...) : renvoie la réponse Telegram `{"ok": true, "result": ...}`.

    Lève RuntimeError (jeton masqué) si le jeton manque ou si Telegram refuse ; utilisé par `groupe.tableau`.
    """
    jeton = os.environ.get(TOKEN_ENV, "").strip()
    if not jeton:
        raise RuntimeError(f"{TOKEN_ENV} absent de l'environnement")
    try:
        return _appel(jeton, methode, champs, None, "", timeout_s)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(_masquer(f"{methode} : {exc}"[:300], jeton)) from None


# --- Alerte réglée sur place (todo #219) -----------------------------------------------------------------------------

ALERTES_FILE = "alertes.json"
INTROUVABLE = ("message to edit not found", "message can't be edited", "message_id_invalid")
TITRE_MAX = 200


def alertes_path(settings=None) -> Path:
    from ultron_hub.common.settings import project_root
    var = getattr(settings, "var_dir", None) or (project_root() / "var")
    return Path(var) / "envoi" / ALERTES_FILE


def _lire_alertes(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ecrire_alertes(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def enregistrer(cle: str, res: Resultat, texte: str, settings=None, now: datetime | None = None) -> bool:
    """Garde le message d'une alerte envoyée (mode direct, `res.message_id` connu) sous sa clé. Ne lève jamais."""
    try:
        if mode(settings) != "direct" or res.message_id is None or not res.chat:
            return False
        lignes = separer(texte)[0].splitlines()
        path = alertes_path(settings)
        data = _lire_alertes(path)
        data[cle] = {"chat_id": res.chat, "message_id": res.message_id, "texte": (lignes[0] if lignes else "")[:TITRE_MAX],
                     "envoyee_a": (now or datetime.now().astimezone()).isoformat(timespec="seconds")}
        _ecrire_alertes(path, data)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("alerte %s non enregistrée : %s", cle, exc)
        return False


def oublier(cle: str, settings=None) -> bool:
    """Oublie la clé d'une alerte sans toucher au message (sa source a disparu, ex. veille retirée). Ne lève jamais."""
    try:
        path = alertes_path(settings)
        data = _lire_alertes(path)
        if data.pop(cle, None) is None:
            return False
        _ecrire_alertes(path, data)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("alerte %s non oubliée : %s", cle, exc)
        return False


def marquer_reglee(cle: str, settings=None, now: datetime | None = None, appel=None) -> str:
    """Change le message de l'alerte `cle` en « ✅ réglée à HHhMM » puis oublie la clé. Ne lève jamais.

    Rend `inconnue` (rien de gardé, ou mode hermes), `modifiee`, `oubliee` (message introuvable ou non modifiable) ou
    `erreur` (journalisée, la clé reste pour un nouvel essai)."""
    try:
        if mode(settings) != "direct":
            return "inconnue"
        path = alertes_path(settings)
        data = _lire_alertes(path)
        info = data.get(cle)
        if not isinstance(info, dict):
            return "inconnue"
        heure = (now or datetime.now().astimezone()).astimezone()
        texte = f"✅ réglée à {heure:%Hh%M}" + (f"\n\n{info['texte']}" if info.get("texte") else "")
        try:
            (appel or appeler)("editMessageText", {"chat_id": info["chat_id"], "message_id": info["message_id"],
                                                    "text": texte[:MAX_MESSAGE]})
            statut = "modifiee"
        except Exception as exc:  # noqa: BLE001
            if not any(m in str(exc).lower() for m in INTROUVABLE):
                log.warning("alerte %s : editMessageText en échec (%s)", cle, exc)
                return "erreur"
            statut = "oubliee"
        data.pop(cle, None)
        _ecrire_alertes(path, data)
        return statut
    except Exception as exc:  # noqa: BLE001
        log.warning("alerte %s : marquer_reglee en échec (%s)", cle, exc)
        return "erreur"
