"""Read-only view of an interactive `ultron` session (todo #264): tmux session -> Claude Code JSONL journal -> bubbles.

Security: the only files ever opened are `~/.claude/sessions/<pid>.json` (3 fields kept) and `~/.claude/projects/<project>/<uuid>.jsonl` (resolved path, depth exactly 2, no
link leaving the folder). Nothing else of `~/.claude` is read. Tool OUTPUTS are never returned, only a short description.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECTS = Path.home() / ".claude" / "projects"
SESSIONS = Path.home() / ".claude" / "sessions"  # <pid>.json per claude process (the .key files there are never opened)
SESSION_FILE_MAX = 16 * 1024
# ~/.local/bin/ultron (deploy/bin/ultron): ultron-main-JJ-MM-HHhMM (session Main; the older ultron-JJ-MM-HHhMM stays valid), or ultron-<chantier>-JJ-MM-HHhMM for `ultron <chantier>` (todo #284)
NAME_RE = re.compile(r"ultron-(?:(?P<chantier>[a-z0-9][a-z0-9-]{0,39})-)?[0-9]{2}-[0-9]{2}-[0-9]{2}h[0-9]{2}(?:-[0-9]{1,3})?")
MAIN = "main"  # the session without chantier; refused as a chantier name (escouade.core.check_name)
JOURNAL_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl")
TAIL_BYTES = 4 * 1024 * 1024  # a long session is read from its end only
N_DEFAULT, N_MAX = 60, 200
TEXT_MAX = 4000
TOTAL_MAX = 150_000  # stays under the dashboard payload limit (DONNEES_MAX); newest entries are kept
DETAIL_MAX = 160
TMUX_TIMEOUT_S = 5
AGENT_TOOLS = ("Agent", "Task")
CONTEXT_WINDOW = 200_000  # `claude --model opus` (deploy/bin/ultron): the standard window; a 1M model is not told by the journal
SKIPPED_PREFIXES = ("<local-command", "<command-", "Caveat:", "<system-reminder", "[Request interrupted",
                    "<bash-input>", "<bash-stdout>", "<bash-stderr>", "<user-prompt-submit-hook")
Runner = Callable[..., subprocess.CompletedProcess]


class SessionError(Exception):
    pass


def _tmux(args: list[str], run: Runner) -> str:
    try:
        proc = run(["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S, check=False,
                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionError(f"tmux indisponible ({type(exc).__name__})") from None
    return proc.stdout if proc.returncode == 0 else ""


def chantier_of(name: str) -> str | None:
    """The chantier of a session name (`ultron-dashboard-20-10-09h12` -> `dashboard`), None for the Main session.

    Main is `ultron-main-…` (or the older `ultron-JJ-MM-…`): "main" is never a chantier name."""
    found = NAME_RE.fullmatch(name or "")
    chantier = found.group("chantier") if found else None
    return None if chantier == MAIN else chantier


def list_sessions(run: Runner = subprocess.run) -> list[dict[str, Any]]:
    """Open ultron sessions (Main and chantier ones), oldest first."""
    out = _tmux(["list-sessions", "-F", "#{session_created} #{session_name} #{session_attached}"], run)
    found = []
    for line in out.splitlines():
        parts = line.split(" ")
        if len(parts) == 3 and parts[0].isdigit() and NAME_RE.fullmatch(parts[1]):
            found.append({"nom": parts[1], "cree": int(parts[0]), "ouverte": parts[2] != "0",
                          "chantier": chantier_of(parts[1])})
    return sorted(found, key=lambda s: s["cree"])


# -- journal lookup ---------------------------------------------------------------------------------------------------------

def safe_journal(path: Path | str, projects: Path = PROJECTS) -> Path:
    """The resolved journal, or SessionError: only `<projects>/<project>/<uuid>.jsonl`, links leaving the folder refused."""
    try:
        base = projects.resolve(strict=True)
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise SessionError("journal refusé") from None
    if real.parent.parent != base or not JOURNAL_RE.fullmatch(real.name) or not real.is_file():
        raise SessionError("journal refusé : seuls ~/.claude/projects/<dossier>/<uuid>.jsonl se lisent")
    return real


def _children(proc: Path) -> dict[int, list[int]]:
    tree: dict[int, list[int]] = {}
    for stat in proc.glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            tree.setdefault(int(fields[1]), []).append(int(stat.parent.name))
        except (OSError, ValueError, IndexError):
            continue
    return tree


def _descendants(pid: int, proc: Path) -> list[int]:
    tree, found, todo = _children(proc), [], [pid]
    while todo:
        cur = todo.pop()
        found.append(cur)
        todo.extend(tree.get(cur, []))
    return found


def _claimed_journal(pid: int, sessions: Path, projects: Path) -> Path | None:
    """Journal named by `<sessions>/<pid>.json` (written by Claude Code per process): only pid, sessionId and cwd are read."""
    try:
        with (sessions / f"{pid}.json").open("rb") as fh:
            data = json.loads(fh.read(SESSION_FILE_MAX))
        sid, cwd = data["sessionId"], data["cwd"]
        if data.get("pid") != pid or not isinstance(sid, str) or not isinstance(cwd, str):
            return None
        return safe_journal(projects / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{sid}.jsonl", projects)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, SessionError):
        return None


def find_journal(name: str, run: Runner = subprocess.run, projects: Path = PROJECTS, proc: Path = Path("/proc"),
                 sessions: Path = SESSIONS) -> Path:
    """Journal of a session (todo #316): the `claude` process of its tmux pane declares it in `<sessions>/<pid>.json`
    (sessionId follows a /clear). No such file: the session has not started, never another session's journal."""
    if not NAME_RE.fullmatch(name):
        raise SessionError("nom de session refusé")
    if not any(s["nom"] == name for s in list_sessions(run)):
        raise SessionError("session introuvable (voir : ultron liste)")
    pids = [int(p) for p in _tmux(["list-panes", "-t", f"={name}", "-F", "#{pane_pid}"], run).split() if p.isdigit()]
    for root in pids:
        for pid in _descendants(root, proc):
            found = _claimed_journal(pid, sessions, projects)
            if found is not None:
                return found
    raise SessionError("session pas encore démarrée : aucun fil")


# -- bubbles ----------------------------------------------------------------------------------------------------------------

def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _tool_detail(block: dict[str, Any]) -> str:
    """What a tool call was FOR: its description, else a path or pattern. Never a command line, never an output."""
    args = block.get("input")
    if not isinstance(args, dict):
        return ""
    for key in ("description", "file_path", "path", "pattern", "url", "query"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _cut(" ".join(value.split()), DETAIL_MAX)
    return ""


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else ""


def _tail_lines(path: Path) -> list[str]:
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - TAIL_BYTES))
        raw = fh.read()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > TAIL_BYTES else lines


def _context_tokens(usage: Any) -> int:
    """Tokens the model read for one reply = the context in use (todo #283); 0 when the journal does not say."""
    if not isinstance(usage, dict):
        return 0
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return sum(v for k in keys if isinstance(v := usage.get(k), int) and not isinstance(v, bool) and v > 0)


def _end_agent(agents: dict[str, dict[str, Any]], entries: list[dict[str, Any]], text: str, ts: str) -> None:
    """Apply a <task-notification> (seen as a user line and/or a queued_command attachment); only the first transition counts."""
    ag = agents.get(_tag(text, "tool-use-id"))
    status = _tag(text, "status")
    if ag and ag["etat"] == "en_cours" and status in ("completed", "failed", "killed", "stopped"):
        ag["etat"] = "fini" if status == "completed" else status
        entries.append({"k": "agent_fini", "ts": ts, "texte": ag["description"], "modele": ag["model"], "etat": ag["etat"]})


def read_entries(path: Path, n: int = N_DEFAULT) -> dict[str, Any]:
    """Last `n` bubbles of a journal plus the sub-agents still running (computed on the whole tail, not only on `n`)."""
    n = max(1, min(int(n), N_MAX))
    entries: list[dict[str, Any]] = []
    agents: dict[str, dict[str, Any]] = {}
    context = 0
    for line in _tail_lines(path):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "attachment" and not obj.get("isSidechain"):
            att = obj.get("attachment")  # a background sub-agent's end is journaled as a queued_command attachment
            prompt = att.get("prompt") if isinstance(att, dict) and att.get("type") == "queued_command" else None
            if isinstance(prompt, str) and prompt.lstrip().startswith("<task-notification>"):
                _end_agent(agents, entries, prompt.lstrip(), obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else "")
            continue
        if not isinstance(obj, dict) or obj.get("isSidechain") or obj.get("type") not in ("user", "assistant"):
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""
        if obj["type"] == "assistant" and isinstance(content, list):
            context = _context_tokens(msg.get("usage")) or context
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    entries.append({"k": "reponse", "ts": ts, "texte": _cut(block["text"], TEXT_MAX)})
                elif block.get("type") == "tool_use":
                    name = str(block.get("name", "?"))[:40]
                    if name in AGENT_TOOLS:
                        args = block.get("input") if isinstance(block.get("input"), dict) else {}
                        desc = _cut(str(args.get("description") or "sous-agent"), DETAIL_MAX)
                        model = str(args.get("model") or "")[:20]
                        agents[str(block.get("id"))] = {"description": desc, "model": model, "depuis": ts, "etat": "en_cours",
                                                        "fond": bool(args.get("run_in_background"))}
                        entries.append({"k": "agent_lance", "ts": ts, "texte": desc, "modele": model})
                    else:
                        entries.append({"k": "outil", "ts": ts, "outil": name, "texte": _tool_detail(block)})
        elif obj["type"] == "user" and not obj.get("isMeta") and not obj.get("isCompactSummary"):
            if isinstance(content, list):  # a synchronous sub-agent is finished when its tool_result arrives
                for block in content:
                    ag = agents.get(str(block.get("tool_use_id"))) if isinstance(block, dict) and block.get("type") == "tool_result" else None
                    result = obj.get("toolUseResult")
                    if ag and not (isinstance(result, dict) and result.get("status") == "async_launched"):
                        ag["etat"] = "fini"
                        entries.append({"k": "agent_fini", "ts": ts, "texte": ag["description"], "modele": ag["model"]})
            text = _user_text(content).strip()
            if text.startswith("<task-notification>"):
                _end_agent(agents, entries, text, ts)
            elif text and not text.startswith(SKIPPED_PREFIXES):
                entries.append({"k": "utilisateur", "ts": ts, "texte": _cut(text, TEXT_MAX)})
    running = [{"description": a["description"], "model": a["model"], "depuis": a["depuis"]}
               for a in agents.values() if a["etat"] == "en_cours"]
    kept, budget = [], TOTAL_MAX
    for e in reversed(entries[-n:]):
        budget -= len(e.get("texte", "")) + 200
        if budget < 0 and kept:
            break
        kept.append(e)
    kept.reverse()
    return {"entrees": kept, "total": len(entries), "tronque": len(entries) > len(kept), "sous_agents": running,
            "sous_agents_finis": sum(1 for a in agents.values() if a["etat"] != "en_cours"),
            "contexte": round(100 * context / CONTEXT_WINDOW) if 0 < context <= CONTEXT_WINDOW else None,
            "maj": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
