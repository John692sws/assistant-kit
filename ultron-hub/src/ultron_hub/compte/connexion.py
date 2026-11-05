"""Logging back into a Claude account from the dashboard (todo #336): `compte connexion demarrer|code|abandonner`.

After a switch, Claude Code may ask for a new login (the copy's refresh token was already spent): until now only over ssh.
`demarrer` opens ONE dedicated tmux session `ultron-connexion` running `claude auth login --claudeai` (argv as a list through
`env -i` with a minimal environment, never a shell), waits for the link it prints (joined again when it is cut over several
lines) and gives it back only if it points at claude.ai or console.anthropic.com. `code` pastes the code of the page
into that session (a tmux buffer fed on stdin: never in argv), waits for the login to end, checks `claude auth status
--json`, then registers the new active file into the slot asked (`store.enregistrer --remplacer`) after checking, by the
account behind the token, that it IS that slot's account.
Never `claude auth logout` (it revokes the token at Anthropic). The link and the code are never logged, journaled nor written
to disk (state = the slot, the start time, a digest of the active file before, spent proofs). Both steps are signed
(`closing` action `compte`, targets `login:<a|b>` and `code:<sha16 of the code>`); `abandonner` is not.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ultron_hub.common.logging import get_logger
from ultron_hub.compte import store
from ultron_hub.compte.store import CompteError
from ultron_hub.escouade.mcp_ultron import value_hash
from ultron_hub.todo import closing

SESSION = "ultron-connexion"
TARGET = f"={SESSION}:"  # exact name, never a prefix match
BUFFER = "ultron-connexion-code"  # tmux buffer of the code, deleted by the paste (-d)
LINK_WAIT_S = 20
END_WAIT_S = 60
POLL_S = 0.5
PAUSE_S = 0.3  # between the typed code and its Enter (see sessions/envoi.PAUSE_S)
STATE_TTL_S = 15 * 60  # a link left unanswered longer than this is forgotten
TMUX_TIMEOUT_S = 5
STATUS_TIMEOUT_S = 20
PANE_WIDTH = 500  # wide pane: the link fits on one line most of the time
CODE_MAX = 500
# The code of the login page (`<code>#<state>`, base64url): no space, no control character, never a leading « - » or « . ».
CODE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_#.~=%%-]{3,%d}" % (CODE_MAX - 1))
LINK_MAX = 2000
LINK_PREFIXES = ("https://claude.ai/", "https://claude.com/", "https://console.anthropic.com/")  # claude.com/cai/ since 10/2026
URL_PIECE_RE = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
PROOFS_MAX = 200
log = get_logger("compte")

# Looked up at call time: the tests replace them (no real tmux, no real claude, no real waiting).
run_cmd = subprocess.run
sleep = time.sleep
clock = time.monotonic


def claude_bin() -> str:
    found = shutil.which("claude", path=minimal_env()["PATH"])
    if not found:
        raise CompteError("commande claude introuvable : rien n'a été lancé")
    return found


def minimal_env() -> dict[str, str]:
    home = str(store.HOME)
    return {"HOME": home, "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": os.environ.get("LANG") or "C.UTF-8", "TERM": "xterm-256color"}


# -- tmux ------------------------------------------------------------------------------------------------------------------

def _tmux(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return run_cmd(["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S, check=False,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompteError(f"tmux indisponible ({type(exc).__name__}) : rien n'a été lancé") from None


def exists() -> bool:
    return _tmux(["has-session", "-t", f"={SESSION}"]).returncode == 0


def close() -> bool:
    """Kill the dedicated session; True if there was one."""
    return exists() and _tmux(["kill-session", "-t", f"={SESSION}"]).returncode == 0


def screen() -> list[str]:
    proc = _tmux(["capture-pane", "-p", "-J", "-t", TARGET, "-S", "-200"])
    return proc.stdout.split("\n") if proc.returncode == 0 else []


def ended() -> tuple[bool, int | None]:
    """(the login process ended, its exit status). A session gone (remain-on-exit not set) = ended, status unknown."""
    proc = _tmux(["display-message", "-p", "-t", TARGET, "#{pane_dead} #{pane_dead_status}"])
    if proc.returncode != 0:
        return True, None
    parts = proc.stdout.split()
    if not parts or parts[0] != "1":
        return False, None
    return True, int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None


def _send(keys: tuple[str, ...]) -> None:
    if _tmux(["send-keys", "-t", TARGET, *keys]).returncode != 0:
        raise CompteError("envoi à la session de connexion refusé par tmux : recommence avec 🔑")


def _paste(value: str) -> None:
    """Put `value` into the pane through a named tmux buffer fed on stdin: it never appears in any argv (`ps`)."""
    try:
        loaded = run_cmd(["tmux", "load-buffer", "-b", BUFFER, "-"], input=value, capture_output=True, text=True,
                         timeout=TMUX_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompteError(f"tmux indisponible ({type(exc).__name__}) : rien n'a été envoyé") from None
    if loaded.returncode != 0:
        raise CompteError("tmux n'a pas pris le code : rien n'a été envoyé, recommence avec 🔑")
    if _tmux(["paste-buffer", "-d", "-b", BUFFER, "-t", TARGET]).returncode != 0:
        _tmux(["delete-buffer", "-b", BUFFER])  # never leave the code in a tmux buffer
        raise CompteError("envoi à la session de connexion refusé par tmux : recommence avec 🔑")


def extract_link(lines: list[str]) -> str | None:
    """The first https link on screen, glued back when it runs over the next lines (each one URL characters only)."""
    for i, line in enumerate(lines):
        at = line.find("https://")
        if at < 0:
            continue
        found = URL_PIECE_RE.match(line, at)
        link = found.group(0) if found else ""
        if found and found.end() == len(line.rstrip()):  # the link reaches the end of its line: it may go on below
            for nxt in lines[i + 1:]:
                piece = nxt.strip()
                if not piece or URL_PIECE_RE.fullmatch(piece) is None or piece.startswith("https://"):
                    break
                link += piece
        return link[:LINK_MAX + 1]
    return None


def check_link(link: str) -> str:
    if len(link) > LINK_MAX or not link.startswith(LINK_PREFIXES) or URL_PIECE_RE.fullmatch(link) is None:
        raise CompteError("lien de connexion inattendu (ni claude.ai, ni claude.com, ni console.anthropic.com) : refusé, "
                          "rien n'a changé")
    return link


def check_code(code: str) -> str:
    if not isinstance(code, str) or CODE_RE.fullmatch(code) is None:  # the message never repeats the code
        raise CompteError(f"code refusé (vide, avec espaces ou caractères inattendus, ou plus de {CODE_MAX} caractères) : rien n'a été envoyé")
    return code


def auth_status() -> bool:
    """`claude auth status --json` says logged in (its other fields, e-mail included, are dropped here)."""
    try:
        proc = run_cmd([claude_bin(), "auth", "status", "--json"], capture_output=True, text=True, timeout=STATUS_TIMEOUT_S,
                       check=False, stdin=subprocess.DEVNULL, env=minimal_env())
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return isinstance(data, dict) and data.get("loggedIn") is True


# -- state: the slot of the login in progress and spent proofs (never the link, never the code) -------------------------------

@contextmanager
def _state(var_dir: Path, wall: float) -> Iterator[dict[str, Any]]:
    folder = var_dir / "compte"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "connexion.json"
    with open(folder / ".connexion.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CompteError("une connexion est déjà en cours de traitement : attends sa fin") from None
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        current = data.get("connexion") if isinstance(data.get("connexion"), dict) else None
        if current and not (isinstance(current.get("debut"), (int, float)) and wall - current["debut"] < STATE_TTL_S):
            current = None
        state = {"connexion": current, "preuves": [p for p in data.get("preuves") or [] if isinstance(p, str)][-PROOFS_MAX:]}
        try:
            yield state
        finally:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, path)


def _authorize(state: dict[str, Any], target: str, what: str, human: bool, env: dict | None) -> str:
    details = closing.authorize("compte", target, human=human, what=what, env=env, used=lambda proof: proof in state["preuves"])
    if details.get("proof"):  # spent at once, whatever comes next
        state["preuves"] = (state["preuves"] + [details["proof"]])[-PROOFS_MAX:]
    return str(details.get("by") or "cli")


def _active_digest() -> str | None:
    data = store.read_file(store.ACTIVE)
    return store.digest(data) if data is not None else None


def _wait(deadline_s: float, done) -> Any:
    end = clock() + deadline_s
    while True:
        out = done()
        if out or clock() >= end:
            return out
        sleep(POLL_S)


# -- the three commands ----------------------------------------------------------------------------------------------------

def demarrer(slot: str, var_dir: Path, *, human: bool = False, env: dict | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    store.slot_path(slot)
    now = now or datetime.now(timezone.utc)
    with _state(var_dir, now.timestamp()) as state:
        by = _authorize(state, f"login:{slot}", f"Se reconnecter au compte Claude {slot.upper()} ?", human, env)
        binary = claude_bin()
        replaced = close()  # an earlier login left open: one at a time
        state["connexion"] = None
        saved = store.sauver_actif(now)  # the newest tokens of the account in use are kept before the login replaces them
        before = _active_digest()
        argv = ["new-session", "-d", "-s", SESSION, "-x", str(PANE_WIDTH), "-y", "50", "--", "/usr/bin/env", "-i",
                *(f"{k}={v}" for k, v in minimal_env().items()), binary, "auth", "login", "--claudeai"]
        if _tmux(argv).returncode != 0:
            raise CompteError("tmux n'a pas pu ouvrir la session de connexion : rien n'a changé")
        _tmux(["set-option", "-w", "-t", TARGET, "remain-on-exit", "on"])  # keep the exit status readable
        link = _wait(LINK_WAIT_S, lambda: extract_link(screen()) or (ended()[0] and "fin"))
        if not link or link == "fin":
            close()
            raise CompteError(f"claude n'a pas affiché de lien de connexion en {LINK_WAIT_S} s : rien n'a changé, réessaie")
        try:
            check_link(link)
        except CompteError:
            close()
            raise
        state["connexion"] = {"compte": slot, "debut": now.timestamp(), "avant": before}
    log.info("compte connexion demarrer compte=%s by=%s remplace=%s sauve=%s", slot, by, replaced, saved)
    rescue = (f" Fichier actif inconnu (ni A ni B) gardé dans {store.DIR}/{saved}."
              if saved and saved.startswith("inconnu-") else "")
    who = store.quel(slot, store.adresses_of(store.load_meta()))
    return {"compte": slot, "adresse": store.adresses_of(store.load_meta())[slot], "lien": link, "by": by, "sauve": saved,
            "text": f"🔑 Lien de connexion au compte {who.removeprefix('compte ')} prêt : ouvre-le, vérifie que tu es sur le bon compte "
                    f"claude.ai, puis colle le code affiché.{rescue}"}


def code(value: str, var_dir: Path, cache_dir: Path, *, slot: str | None = None, human: bool = False,
         env: dict | None = None, now: datetime | None = None) -> dict[str, Any]:
    check_code(value)
    now = now or datetime.now(timezone.utc)
    with _state(var_dir, now.timestamp()) as state:
        current = state["connexion"]
        if not current or current.get("compte") not in store.SLOTS:
            raise CompteError("aucune connexion en cours (ou lien de plus de 15 min) : appuie d'abord sur 🔑 Se reconnecter")
        x = current["compte"]
        if slot is not None and slot != x:
            raise CompteError(f"la connexion en cours est pour le compte {x.upper()}, pas {slot.upper()} : rien n'a été envoyé")
        by = _authorize(state, f"code:{value_hash(value)}", f"Coller le code de connexion du compte {x.upper()} ?", human, env)
        if not exists():
            state["connexion"] = None
            raise CompteError("la session de connexion n'existe plus : recommence avec 🔑 Se reconnecter")
        if ended()[0]:
            close()
            state["connexion"] = None
            raise CompteError("claude auth login s'est arrêté avant le code : recommence avec 🔑 Se reconnecter")
        store.sauver_actif(now)
        state["connexion"] = None  # one code per link, whatever happens next
        try:
            _paste(value)
            sleep(PAUSE_S)
            _send(("Enter",))
            finished = _wait(END_WAIT_S, lambda: ended()[0])
            status = ended()[1] if finished else None
        finally:
            close()
        if not finished:
            raise CompteError(f"la connexion ne s'est pas terminée en {END_WAIT_S} s : rien n'a été enregistré, "
                              "recommence avec 🔑")
        if status not in (0, None):
            raise CompteError("Claude a refusé le code (faux, expiré ou d'une autre demande) : rien n'a été enregistré, "
                              "recommence avec 🔑")
        if not auth_status():
            raise CompteError("`claude auth status` ne dit pas « connecté » : rien n'a été enregistré, recommence avec 🔑")
        if _active_digest() in (None, current.get("avant")):
            raise CompteError("connexion annoncée mais le fichier de connexion n'a pas changé : rien n'a été enregistré")
        result = adopter(x, cache_dir, now)
    log.info("compte connexion code compte=%s by=%s verifie=%s", x, by, result["verifie"])
    return {**result, "by": by}


def adopter(slot: str, cache_dir: Path, now: datetime) -> dict[str, Any]:
    """Register the fresh login as `slot` (same code as `compte enregistrer <slot> --remplacer`), refused when the account
    behind the token is the other slot's (or a third one); `verifie` = the account was recognised and `etat` names `slot`."""
    data = store.read_file(store.ACTIVE)
    if data is None:
        raise CompteError("aucun fichier de connexion actif après la connexion : rien n'a été enregistré")
    store.oauth_of(data)
    meta = store.load_meta()
    ident = store.account_id(data, meta, now)
    seen = meta.get("_adresses", {}).get(ident) if ident else None  # todo #339: the address just read, passed on below
    o = store.other(slot)
    names = store.adresses_of(meta)
    expected, other_id = (meta["comptes"].get(slot) or {}).get("compte"), (meta["comptes"].get(o) or {}).get("compte")
    if ident and ident == other_id:
        raise CompteError(f"tu t'es connecté avec l'autre compte ({store.quel(o, names)}) : rien n'a été enregistré dans "
                          f"{store.quel(slot, names)}. Sur claude.ai, passe sur le compte {store.quel(slot, names)} puis "
                          f"recommence 🔑 Se reconnecter à {store.quel(slot, names)} "
                          f"(Claude Code est maintenant connecté à {store.quel(o, names)}).")
    if ident and expected and ident != expected:
        raise CompteError(f"ce n'est pas le compte {store.quel(slot, names).removeprefix('compte ')} enregistré jusqu'ici (un autre compte claude.ai) : rien n'a "
                          f"été enregistré ; vérifie le compte sur claude.ai, ou `ultron-hub compte enregistrer {slot} "
                          "--remplacer` en terminal pour l'adopter")
    # Not proven the same account (profile unreachable, or no id recorded) yet overwritten: the old copy is kept aside.
    kept = None if ident and ident == expected else store.garder_avant_connexion(slot)
    store.enregistrer(slot, cache_dir, remplacer=True, now=now, identite=(store.digest(data), ident, seen))
    etat = store.etat(cache_dir, now)
    verifie = bool(ident) and etat["actif"] == slot
    names = store.adresses_of(store.load_meta())
    text = (f"🔑 {names.get(slot) or 'Compte ' + slot.upper()} reconnecté et enregistré."
            + ("" if verifie else " ⚠️ Compte non vérifié (profil injoignable) : regarde `compte etat`.")
            + (f" L'ancienne copie de {store.quel(slot, names)} est gardée dans {store.DIR}/{kept}." if kept else "")
            + " Les sessions Claude déjà ouvertes gardent l'ancienne connexion : relance-les (`ultron relance`).")
    return {"compte": slot, "adresse": names.get(slot), "verifie": verifie, "actif": etat["actif"], "sauvegarde": kept, "text": text}


def abandonner(var_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    with _state(var_dir, now.timestamp()) as state:
        closed = close()
        state["connexion"] = None
    log.info("compte connexion abandonner ferme=%s", closed)
    return {"ferme": closed, "text": "✖ Connexion abandonnée." if closed else "Aucune connexion en cours."}
