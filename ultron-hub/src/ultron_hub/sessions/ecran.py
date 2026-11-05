"""The raw tmux screen of an ultron session (todo #283, the « Pilote »), and the menu it shows, read by a script (no AI).

`capture` = `tmux capture-pane -p` of the session's pane (plain text, no escape sequence), the last HISTORY lines of scrollback
plus the visible screen, argv as a list, exact target `=<name>:`. Nothing is written to the session here.
`menu` reads the VISIBLE screen only: a numbered choice list of Claude Code (permission prompt, AskUserQuestion) is a block of
lines `1. …`, `2. …` (consecutive numbers from 1) where one line carries the cursor `❯`, with no input prompt below it.
The dashboard turns its first options into quick replies (keys 1-9 of `session choisir`).
Limit: the screen shows what the terminal shows, tool outputs included (the journal view hides them): authorized devices only.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from typing import Any

from ultron_hub.sessions.journal import NAME_RE, TMUX_TIMEOUT_S, Runner, SessionError

HISTORY = 1000  # scrollback lines kept above the visible screen
LINE_MAX = 400  # characters per line (a pane wider than this is cut)
LABEL_MAX = 80
OPTION_RE = re.compile(r"^[\s│┃|]*(?P<cur>[❯›▶>]\s*)?(?P<n>[1-9])[.)]\s+(?P<lib>\S.*?)[\s│┃|]*$")
PROMPT_RE = re.compile(r"^[\s│┃|]*[❯>](?:\s.*)?$")  # Claude Code's input line (empty or with typed text)
FREE_RE = re.compile(r"(type something|other\b|autre\b)", re.I)  # the option of AskUserQuestion that takes typed text
GAP_MAX = 4  # lines between two options (an option's description, a blank line)


def _tmux(args: list[str], run: Runner | None) -> subprocess.CompletedProcess:
    try:
        return (run or subprocess.run)(["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S, check=False,
                                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionError(f"tmux indisponible ({type(exc).__name__})") from None


def _clean(line: str) -> str:
    line = "".join(ch for ch in line if unicodedata.category(ch) != "Cc")
    return line.rstrip()[:LINE_MAX]


def capture(nom: str, run: Runner | None = None) -> dict[str, Any]:
    """The pane as plain lines: {lignes, largeur, hauteur, visibles} (visibles = how many of the last lines are on screen)."""
    if not isinstance(nom, str) or NAME_RE.fullmatch(nom) is None:
        raise SessionError("nom de session refusé")
    size = _tmux(["display-message", "-p", "-t", f"={nom}:", "#{pane_width} #{pane_height}"], run)
    parts = size.stdout.split() if size.returncode == 0 else []
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise SessionError(f"session {nom} absente")
    largeur, hauteur = int(parts[0]), int(parts[1])
    proc = _tmux(["capture-pane", "-p", "-t", f"={nom}:", "-S", f"-{HISTORY}"], run)
    if proc.returncode != 0:
        raise SessionError(f"écran de la session {nom} illisible")
    lines = [_clean(ln) for ln in proc.stdout.split("\n")]
    if lines and lines[-1] == "":  # the final newline of tmux's output
        lines.pop()
    visibles = min(hauteur, len(lines))
    while lines and not lines[-1].strip():  # blank bottom of the screen
        lines.pop()
        visibles = max(0, visibles - 1)
    return {"lignes": lines, "largeur": largeur, "hauteur": hauteur, "visibles": visibles}


def _menu_options(lines: list[str]) -> list[tuple[int, str, bool]]:
    """The raw options (number, label, carries the cursor) of the menu at the bottom of the screen, [] when there is none."""
    best: list[tuple[int, str, bool]] | None = None
    current: list[tuple[int, str, bool]] | None = None
    last_at = 0
    for i, line in enumerate(lines):
        found = OPTION_RE.match(line)
        if found:
            option = (int(found.group("n")), found.group("lib"), bool(found.group("cur")))
            if option[0] == 1:
                current = best = [option]
            elif current and option[0] == current[-1][0] + 1 and i - last_at <= GAP_MAX:
                current.append(option)
            else:
                current = None
            last_at = i
        elif PROMPT_RE.match(line):  # an input prompt below a list: that list is text, not a menu
            current = best = None
    if not best or len(best) < 2 or not any(cur for _, _, cur in best):
        return []
    return best


def menu(lines: list[str]) -> list[dict[str, str]]:
    """The numbered options of the menu shown at the bottom of the screen, [] when there is none."""
    return [{"touche": str(n), "libelle": " ".join(lib.split())[:LABEL_MAX]} for n, lib, _ in _menu_options(lines)]


def free_text_gap(lines: list[str]) -> int | None:
    """Todo #308. Rows to move the cursor (+ down, - up) to reach the free-text option of the choice question on screen
    (« Type something. » / « Other »); 0 = already there; None = a menu with no such option; ValueError = no menu at all."""
    options = _menu_options(lines)
    if not options:
        raise ValueError("no menu")
    cursor = next(i for i, (_, _, cur) in enumerate(options) if cur)
    free = next((i for i, (_, lib, _) in enumerate(options) if FREE_RE.match(lib)), None)
    return None if free is None else free - cursor


def lire(nom: str, run: Runner | None = None) -> dict[str, Any]:
    """`session ecran`: the lines, the size and the menu of the visible screen."""
    data = capture(nom, run)
    visible = data["lignes"][len(data["lignes"]) - data["visibles"]:] if data["visibles"] else []
    return {"session": nom, "lignes": data["lignes"], "largeur": data["largeur"], "hauteur": data["hauteur"],
            "menu": menu(visible)}


def menu_of(nom: str, run: Runner | None = None) -> list[dict[str, str]]:
    """The menu only (for `session lire`): [] when the screen cannot be read."""
    try:
        return lire(nom, run)["menu"]
    except SessionError:
        return []
