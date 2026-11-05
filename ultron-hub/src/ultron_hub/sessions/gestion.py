"""Managing ultron sessions (todo #266): open a new one, relaunch one. Both go through `~/.local/bin/ultron` (the script that
names the tmux session and starts Claude), argv as a list, never a shell, stdin closed.

The script ends with `exec tmux attach-session`, which fails without a terminal: its exit code does NOT count. Success =
a line `Session tmux : <name>` on stdout (name checked by `journal.NAME_RE`) and the session found by `tmux has-session`.
Opening is signed (`closing` action `session`, target `nouvelle:<sha16 of the instruction>`, same proofs as envoi.py);
relaunching is not (like ⏹ Interrompre) and works in the SAME tmux session (todo #291): rebuild the prompt, `/clear` + Enter,
a short wait, then the start message (`ULTRON_MSG`, read from the tmux environment) + Enter. `/clear` keeps the process, so
sub-agents at work go on: no refusal for a dirty worktree any more. If the pane no longer runs Claude (shell prompt), or the
message is gone: old way, a new session is opened (same chantier, `ultron <chantier>`, todo #284), checked, then the old one killed.
The log keeps the length of the instruction, never the text.
"""

from __future__ import annotations

import os
import pwd
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings, project_root
from ultron_hub import branches, repos
from ultron_hub.escouade import core as escouade_core
from ultron_hub.escouade.mcp_ultron import value_hash
from ultron_hub.esprit import store
from ultron_hub.sessions import etat
from ultron_hub.sessions.envoi import _authorize, _exists, _send, _state, _tmux, check_name, check_text
from ultron_hub.sessions.journal import NAME_RE, list_sessions, TMUX_TIMEOUT_S, Runner, SessionError, chantier_of

CONSIGNE_MAX = 500
LAUNCH_TIMEOUT_S = 60
# `ultron <word>` with ONE argument is a subcommand of the script (relance, liste), or a session number.
RESERVED = frozenset({"relance", "liste", "list"})
HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)  # from pwd, not $HOME
ULTRON_BIN = HOME / ".local/bin/ultron"
CREATED_RE = re.compile(r"^Session tmux : (\S+)$", re.MULTILINE)
log = get_logger("sessions")


def check_consigne(consigne: str) -> str:
    if not isinstance(consigne, str):
        raise SessionError("consigne refusée")
    if not consigne.strip():
        return ""
    check_text(consigne)
    if len(consigne) > CONSIGNE_MAX:
        raise SessionError(f"consigne trop longue ({CONSIGNE_MAX} caractères au plus)")
    if consigne in RESERVED or consigne.isascii() and consigne.isdigit():
        raise SessionError("consigne refusée : seule, elle serait lue comme une commande de ultron (reformule)")
    return consigne


def chantiers(folder: Path | None = None) -> list[str]:
    """Chantiers one can open a session on: sheets ~/notes/prompt/<name>.md (as `ultron` reads them: no `_*`, no `reprise-*`)."""
    found = escouade_core.list_subjects(folder or escouade_core.prompts_dir())
    return [c for c in found if escouade_core.NAME_RE.fullmatch(c) and c not in RESERVED and not c.startswith("reprise-")]


def check_chantier(chantier: str, folder: Path | None = None) -> str:
    if not isinstance(chantier, str):
        raise SessionError("chantier refusé")
    if chantier and chantier not in chantiers(folder):
        raise SessionError(f"chantier {chantier} inconnu (fiche ~/notes/prompt/{chantier}.md ?)")
    return chantier


def _launch(extra: list[str], run: Runner | None) -> str:
    """Runs the script, returns the name of the session it created (checked to exist), or SessionError."""
    # TMUX: else the script would switch a client, not just create; the spent proof never reaches the new session.
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "ULTRON_HUB_CLOSE_TOKEN")}
    try:
        proc = (run or subprocess.run)([str(ULTRON_BIN), *extra], capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S,
                                       check=False, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionError(f"ultron indisponible ({type(exc).__name__})") from None
    found = CREATED_RE.search(proc.stdout or "")
    if found is None or NAME_RE.fullmatch(found.group(1)) is None:
        raise SessionError("la nouvelle session n'a pas été créée (aucun nom rendu par ultron)")
    _exists(found.group(1), run)
    return found.group(1)


def nouvelle(consigne: str, var_dir: Path, *, chantier: str = "", human: bool = False, run: Runner | None = None,
             env: dict | None = None, clock: Callable[[], float] = time.time,
             folder: Path | None = None) -> dict[str, Any]:
    """Opens a session. `chantier` ("" = Main session) and the instruction both go to the script (`--chantier <name>`
    then the instruction); the signed target covers both: `nouvelle:<chantier>:<sha16 of the instruction>`."""
    consigne = check_consigne(consigne)
    chantier = check_chantier(chantier, folder)
    now = clock()
    with _state(var_dir, now) as state:
        by = _authorize(state, f"nouvelle:{chantier}:{value_hash(consigne)}", "Ouvrir une nouvelle session ultron", human, now, env)
        nom = _launch([*(["--chantier", chantier] if chantier else []), *([consigne] if consigne else [])], run)
        if (chantier_of(nom) or "") != chantier:  # an older `ultron` script read the option as an instruction: undo, say so
            _tmux(["kill-session", "-t", f"={nom}"], run)
            raise SessionError("la session ne porte pas le chantier demandé : la commande ultron n'est pas à jour "
                               "(deploy/bin/poser.sh), la session créée a été fermée")
    log.info("session nouvelle session=%s chantier=%s consigne=%d by=%s", nom, chantier or "-", len(consigne), by)
    return {"session": nom, "id": nom, "chantier": chantier or None, "consigne": bool(consigne), "by": by}


def travail_non_commite(settings: Settings) -> list[str]:
    """Names of the todo-* worktrees holding uncommitted work (an information for the closing sheet, never a refusal)."""
    out: list[str] = []
    for path in repos.list_repos(settings).values():
        try:
            names = branches._todo_refs(path)
            out += [Path(wt).name for wt in branches.dirty_worktrees(path, names)]
        except Exception:  # noqa: BLE001  a repository that git cannot read must not block closing a session
            continue
    return sorted(set(out))


def fermeture(nom: str, settings: Settings, *, run: Runner | None = None,
              digest: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """What the closing sheet shows: sub-agents at work (same count as the Pilote and the list, None = unknown) and dirty worktrees."""
    check_name(nom)
    _exists(nom, run)
    return {"session": nom, "sous_agents": (digest or etat.digest)(nom)["sous_agents"], "travail_non_commite": travail_non_commite(settings)}


def duree_hmm(secondes: float) -> str:
    """`h:mm` of a duration in seconds (0:07, 2:35)."""
    minutes = max(0, int(secondes)) // 60
    return f"{minutes // 60}:{minutes % 60:02d}"


def tracer_fin(nom: str, settings: Settings | None, run: Runner | None, now: float | None = None) -> None:
    """Todo #298: the journal line `fin <nom> durée=<h:mm>` for a session that is about to be killed. Never raises.

    Only called when Claude still runs in the pane: if it already ended, the launch command wrote the line itself."""
    try:
        cree = next((s["cree"] for s in list_sessions(run or subprocess.run) if s["nom"] == nom), None)
        duree = f" durée={duree_hmm((now or time.time()) - cree)}" if cree else ""
        store.record(settings or Settings.load(), "ultron", "session", f"fin {nom}{duree}")
    except Exception:  # noqa: BLE001  a journal problem never blocks closing
        log.warning("session fin: not journaled (%s)", nom, exc_info=True)


def fermer(nom: str, var_dir: Path, settings: Settings, *, human: bool = False, run: Runner | None = None,
           env: dict | None = None, clock: Callable[[], float] = time.time,
           digest: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Closes one session: `tmux kill-session -t =<nom>` (never forced), refused while sub-agents work in it; checked after."""
    check_name(nom)
    now = clock()
    with _state(var_dir, now) as state:
        by = _authorize(state, f"fermer:{nom}", f"Fermer la session {nom}", human, now, env)
        _exists(nom, run)
        agents = (digest or etat.digest)(nom)["sous_agents"]
        if agents:
            raise SessionError(f"fermeture refusée : {agents} sous-agent(s) travaillent dans {nom} (attends leur fin)")
        if _claude_runs(nom, run):
            tracer_fin(nom, settings, run, now)
        if not _tmux(["kill-session", "-t", f"={nom}"], run):
            raise SessionError(f"tmux n'a pas fermé la session {nom}")
        if _tmux(["has-session", "-t", f"={nom}"], run):
            raise SessionError(f"la session {nom} est toujours ouverte après la fermeture")
    try:  # todo #538 : fermée à la main = plus de reprise d'objectif ; ne fait jamais échouer la fermeture
        from ultron_hub import objectif
        objectif.delier_fermee(settings, nom)
    except Exception as exc:  # noqa: BLE001
        log.warning("session fermer: objectif non délié session=%s (%s)", nom, type(exc).__name__)
    dirty = travail_non_commite(settings)
    log.info("session fermer session=%s by=%s travail_non_commite=%d", nom, by, len(dirty))
    return {"session": nom, "fermee": True, "verifiee": True, "by": by, "travail_non_commite": dirty}


SHELLS = frozenset({"bash", "sh", "zsh", "fish", "dash", "ksh"})
CLEAR_WAIT_S = 2.0  # after `/clear`, before the message: short and bounded (no screen scraping)
HUB_BIN = project_root() / ".venv/bin/ultron-hub"
PROMPTS_DIR = project_root() / "var/prompts"


def _tmux_out(args: list[str], run: Runner | None) -> str | None:
    """stdout of a tmux command, None when tmux fails."""
    try:
        proc = (run or subprocess.run)(["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S, check=False,
                                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionError(f"tmux indisponible ({type(exc).__name__})") from None
    return proc.stdout if proc.returncode == 0 else None


def _claude_runs(nom: str, run: Runner | None) -> bool:
    """False when the pane is back at a shell prompt (Claude ended): `/clear` would be typed into the shell."""
    command = (_tmux_out(["display-message", "-p", "-t", f"={nom}:", "#{pane_current_command}"], run) or "").strip()
    return bool(command) and command not in SHELLS


def _start_message(nom: str, run: Runner | None) -> str | None:
    """The start message kept in the tmux environment of the session (never pasted into a shell), or None."""
    out = _tmux_out(["show-environment", "-t", f"={nom}", "ULTRON_MSG"], run) or ""
    msg = out.rstrip("\n").removeprefix("ULTRON_MSG=") if out.startswith("ULTRON_MSG=") else ""
    try:
        return check_text(msg) if msg else None
    except SessionError:
        return None


def _reassemble(chantier: str | None, run: Runner | None) -> None:
    """Rebuilds the prompt file as `ultron` does at launch; the chantier one is mandatory (else it is not that session)."""
    target = PROMPTS_DIR / (f"interactif-{chantier}.md" if chantier else "interactif.md")
    try:
        proc = (run or subprocess.run)([str(HUB_BIN), "prompt", "interactif", *([chantier] if chantier else []),
                                        "--sortie", str(target)], capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S,
                                       check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        proc = None
    if (proc is None or proc.returncode != 0) and chantier:
        raise SessionError(f"relance refusée : le chantier {chantier} n'est plus connu (fiche ~/notes/prompt/{chantier}.md ?)")


def _relancer_nouvelle(nom: str, run: Runner | None, settings: Settings | None = None) -> dict[str, Any]:
    """Old way: open a new session, then close this one (Claude no longer runs in the pane)."""
    chantier = chantier_of(nom)
    nouveau = _launch([chantier] if chantier else [], run)
    if chantier_of(nouveau) != chantier:  # chantier unknown now: the script made a plain session with a consigne
        _tmux(["kill-session", "-t", f"={nouveau}"], run)
        raise SessionError(f"relance refusée : le chantier {chantier} n'est plus connu (fiche ~/notes/prompt/{chantier}.md ?)")
    if _claude_runs(nom, run):  # else the launch command already wrote the end line
        tracer_fin(nom, settings, run)
    if not _tmux(["kill-session", "-t", f"={nom}"], run):
        raise SessionError(f"nouvelle session {nouveau} ouverte, mais {nom} n'a pas pu être fermée")
    log.info("session relancer ancienne=%s session=%s mode=nouvelle", nom, nouveau)
    return {"session": nouveau, "ancienne": nom, "mode": "nouvelle"}


def relancer(nom: str, *, run: Runner | None = None, settings: Settings | None = None, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    check_name(nom)
    _exists(nom, run)
    if not _claude_runs(nom, run):
        return _relancer_nouvelle(nom, run, settings)
    message = _start_message(nom, run)
    if message is None:
        return _relancer_nouvelle(nom, run, settings)
    _reassemble(chantier_of(nom), run)
    # Same session, same process: `/clear` keeps the sub-agents at work (no dirty-worktree refusal any more).
    _send(nom, ("-l", "--", "/clear"), run)
    _send(nom, ("Enter",), run)
    sleep(CLEAR_WAIT_S)
    _send(nom, ("-l", "--", message), run)
    _send(nom, ("Enter",), run)
    log.info("session relancer session=%s mode=clear message=%d", nom, len(message))
    return {"session": nom, "ancienne": nom, "mode": "clear"}
