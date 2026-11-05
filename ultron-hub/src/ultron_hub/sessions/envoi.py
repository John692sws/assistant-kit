"""Writing into an interactive ultron session (todo #265): text, Escape, or a key answering a choice question.

Only through `tmux send-keys`, argv as a list, never a shell. The session name is checked by `journal.NAME_RE`
(`ultron-JJ-MM-HHhMM`) and must exist (`tmux has-session`); the target is `=<name>:` (exact name, never a prefix match).
The text goes with `-l --` (literal: never read as key names or options), then Enter is sent on its own.
Writing and choosing are signed (`closing` action `session`, target `<name>:<sha16 of the text>` / `<name>:<key>`);
`interrompre` (Escape) is not (like ⏹ Arrêter of a squad); the dashboard's Pilote (todo #283) sends Escape as a signed key. The trace keeps the session, the length and who sent it: NEVER the text.
State (spent proofs, last text fingerprint per session, never the text) lives in `var/sessions/envois.json`.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ultron_hub.common.logging import get_logger
from ultron_hub.escouade.mcp_ultron import value_hash
from ultron_hub.sessions import ecran
from ultron_hub.sessions.journal import NAME_RE, TMUX_TIMEOUT_S, Runner, SessionError
from ultron_hub.todo import closing

TEXT_MAX = 4000
PAUSE_S = 0.3  # between the typed text and its Enter: sent at once, Claude Code reads them as one paste and Enter becomes a newline
DEDUP_S = 10  # the same text to the same session within this delay = a double send, refused
PROOF_KEEP_S = closing.TOKEN_TTL_S + closing.FUTURE_SKEW_S + 10  # a spent proof is remembered while its token could live
SIGNED_ACTION = "session"
# Keys of Claude Code's terminal: digits pick an option, arrows move, Enter validates; since todo #283 (the Pilote) also
# Escape (stops the turn, closes a menu), Tab (completion) and Ctrl-C (clears the input; twice in a row quits Claude Code).
KEYS: dict[str, tuple[str, ...]] = {**{str(d): ("-l", "--", str(d)) for d in range(1, 10)},
                                    "haut": ("Up",), "bas": ("Down",), "gauche": ("Left",), "droite": ("Right",),
                                    "entree": ("Enter",), "echap": ("Escape",), "tab": ("Tab",), "ctrl-c": ("C-c",),
                                    # todo #419 (terminal shortcuts): send-keys goes straight to the pane, not through tmux's
                                    # Ctrl-B prefix, so ONE C-b reaches Claude Code (background); C-x C-s = send now
                                    "ctrl-b": ("C-b",), "ctrl-x-s": ("C-x", "C-s")}
log = get_logger("sessions")


def check_name(nom: str) -> str:
    if not isinstance(nom, str) or NAME_RE.fullmatch(nom) is None:
        raise SessionError("nom de session refusé (ultron-JJ-MM-HHhMM seulement)")
    return nom


def check_text(texte: str) -> str:
    if not texte or not texte.strip():
        raise SessionError("texte vide")
    if len(texte) > TEXT_MAX:
        raise SessionError(f"texte trop long ({TEXT_MAX} caractères au plus)")
    if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in texte):
        raise SessionError("caractère de contrôle refusé (retour à la ligne, tabulation, échappement…)")
    return texte


def check_key(touche: str) -> str:
    if touche not in KEYS:
        raise SessionError(f"touche refusée ({', '.join(KEYS)})")
    return touche


def _tmux(args: list[str], run: Runner | None) -> bool:
    try:
        proc = (run or subprocess.run)(["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S, check=False,
                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionError(f"tmux indisponible ({type(exc).__name__})") from None
    return proc.returncode == 0


def _exists(nom: str, run: Runner | None) -> None:
    if not _tmux(["has-session", "-t", f"={nom}"], run):
        raise SessionError(f"session {nom} absente")


def _reach_text_field(nom: str, run: Runner | None) -> None:
    """Todo #308. A choice question on screen reads every key as a menu key (digits pick an option, letters are lost): the cursor
    must sit on its free-text option BEFORE the text goes. A menu without one, or a cursor that did not arrive: refused, nothing typed."""
    try:
        gap = ecran.free_text_gap(_visible(nom, run))
    except ValueError:  # no menu: the plain input prompt
        return
    except SessionError:  # screen unreadable: same as before #308
        return
    if gap is None:
        raise SessionError("la session pose une question à choix sans champ libre : réponds avec les boutons, rien n'a été tapé")
    if gap:
        _send(nom, ("Down" if gap > 0 else "Up",) * abs(gap), run)
        time.sleep(PAUSE_S)
        try:
            arrived = ecran.free_text_gap(_visible(nom, run)) == 0
        except ValueError:
            arrived = False
        if not arrived:
            raise SessionError("le curseur n'a pas atteint le champ libre de la question : rien n'a été tapé")


def _visible(nom: str, run: Runner | None) -> list[str]:
    data = ecran.capture(nom, run)
    return data["lignes"][len(data["lignes"]) - data["visibles"]:] if data["visibles"] else []


def _send(nom: str, keys: tuple[str, ...], run: Runner | None) -> None:
    if not _tmux(["send-keys", "-t", f"={nom}:", *keys], run):
        raise SessionError(f"envoi à la session {nom} refusé par tmux")


# -- state: spent proofs and last sends (fingerprints only) ---------------------------------------------------------------

@contextmanager
def _state(var_dir: Path, now: float) -> Iterator[dict[str, Any]]:
    folder = var_dir / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "envois.json"
    with open(folder / ".envois.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        proofs = {k: v for k, v in (data.get("preuves") or {}).items() if isinstance(v, (int, float)) and now - v < PROOF_KEEP_S}
        sent = {k: v for k, v in (data.get("envois") or {}).items()
                if isinstance(v, list) and len(v) == 2 and isinstance(v[1], (int, float)) and now - v[1] < DEDUP_S}
        state = {"preuves": proofs, "envois": sent}
        try:
            yield state
        finally:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, path)


def _authorize(state: dict[str, Any], target: str, what: str, human: bool, now: float, env: dict | None) -> str:
    details = closing.authorize(SIGNED_ACTION, target, human=human, what=what, env=env,
                                used=lambda proof: proof in state["preuves"])
    if details.get("proof"):
        state["preuves"][details["proof"]] = now
    return str(details.get("by") or "cli")


# -- the three commands ----------------------------------------------------------------------------------------------

def ecrire(nom: str, texte: str, var_dir: Path, *, human: bool = False, run: Runner | None = None,
           env: dict | None = None, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    check_name(nom)
    check_text(texte)
    empreinte, now = value_hash(texte), clock()
    with _state(var_dir, now) as state:
        last = state["envois"].get(nom)
        if last and last[0] == empreinte:
            raise SessionError(f"même texte déjà envoyé à {nom} il y a {int(now - last[1])} s : rien n'a été renvoyé")
        by = _authorize(state, f"{nom}:{empreinte}", f"Écrire {len(texte)} caractères dans la session {nom}", human, now, env)
        _exists(nom, run)
        _reach_text_field(nom, run)
        _send(nom, ("-l", "--", texte), run)
        time.sleep(PAUSE_S)
        _send(nom, ("Enter",), run)
        state["envois"][nom] = [empreinte, now]
    log.info("session ecrire session=%s longueur=%d by=%s", nom, len(texte), by)
    return {"session": nom, "longueur": len(texte), "by": by}


def choisir(nom: str, touche: str, var_dir: Path, *, human: bool = False, run: Runner | None = None,
            env: dict | None = None, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    check_name(nom)
    check_key(touche)
    now = clock()
    with _state(var_dir, now) as state:
        by = _authorize(state, f"{nom}:{touche}", f"Appuyer sur {touche} dans la session {nom}", human, now, env)
        _exists(nom, run)
        _send(nom, KEYS[touche], run)
    log.info("session choisir session=%s touche=%s by=%s", nom, touche, by)
    return {"session": nom, "touche": touche, "by": by}


def interrompre(nom: str, *, run: Runner | None = None) -> dict[str, Any]:
    check_name(nom)
    _exists(nom, run)
    _send(nom, ("Escape",), run)
    log.info("session interrompre session=%s", nom)
    return {"session": nom}
