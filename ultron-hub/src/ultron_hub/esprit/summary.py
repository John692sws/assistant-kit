"""One-line journal summary of a conversation (Claude Code session or Hermes session), by tasks/journal-resume.

Claude Code: the user's SessionEnd hook pipes its JSON ({transcript_path, cwd, ...}) to
`ultron-hub journal summarize --claude-hook`. Hermes: the plugin pipes a plain transcript on its
on_session_finalize hook. ultron-hub's own `claude -p` jobs are never summarized (JOB_ENV): the summary
itself is a `claude -p`, its own SessionEnd would start another summary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.core.job import JobRequest, new_job_id
from ultron_hub.core.runner import run_job
from ultron_hub.esprit import store
from ultron_hub.executors.claude_cli import JOB_ENV

log = get_logger("esprit.summary")

ACTION = "journal-resume"
TRANSCRIPT_MAX = 12_000     # chars sent to the model: the end of the conversation matters most
PART_MAX = 600              # chars kept from one message
PENDING_DIR = "journal-pending"


MIND_ABSENT = "esprit absent"


def skip_reason(settings: Settings, cwd: str | None = None) -> str | None:
    if os.environ.get(JOB_ENV):
        return "session d'un job ultron-hub"
    if cwd:
        path = Path(cwd).resolve()
        for inside in (settings.runs_dir, settings.missions_dir):
            if inside and (path == inside or inside in path.parents):
                return "session d'un job ultron-hub"
    if not store.root(settings).is_dir():
        return MIND_ABSENT
    return None


def _clip(text: str, limit: int = PART_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _tool_line(block: dict) -> str:
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    hint = next((str(args[k]) for k in ("file_path", "command", "path", "pattern", "url", "description")
                 if args.get(k)), "")
    return f"[outil {block.get('name')}{': ' + _clip(hint, 120) if hint else ''}]"


def claude_transcript(path: Path) -> str:
    """Compact text of a Claude Code session file (.jsonl): user and assistant text, tool calls named."""
    lines = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") not in ("user", "assistant") or row.get("isMeta"):
            continue
        content = (row.get("message") or {}).get("content")
        who = "Utilisateur" if row["type"] == "user" else "Assistant"
        if isinstance(content, str):
            if content.strip() and not content.lstrip().startswith("<"):  # <command-name>, <local-command…>
                lines.append(f"{who} : {_clip(content)}")
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                lines.append(f"{who} : {_clip(block['text'])}")
            elif block.get("type") == "tool_use":
                lines.append(_tool_line(block))
    return "\n".join(lines)


def claude_hook_input(settings: Settings, raw: str) -> tuple[str | None, str]:
    """(transcript text, skip reason) from the SessionEnd hook's JSON."""
    try:
        hook = json.loads(raw or "{}")
    except ValueError as exc:
        raise InvalidRequestError(f"entrée du hook illisible : {exc}") from None
    if not isinstance(hook, dict):
        raise InvalidRequestError("entrée du hook : objet JSON attendu")
    reason = skip_reason(settings, hook.get("cwd"))
    if reason:
        return None, reason
    path = Path(str(hook.get("transcript_path") or "")).expanduser()
    projects = Path.home() / ".claude" / "projects"
    try:
        real = path.resolve(strict=True)
    except OSError:
        return None, "transcript introuvable"
    if real.suffix != ".jsonl" or projects.resolve() not in real.parents:
        raise InvalidRequestError("transcript hors de ~/.claude/projects refusé")
    return claude_transcript(real), ""


def worth_summarizing(text: str) -> bool:
    return "Utilisateur :" in text and ("Assistant :" in text or "[outil" in text)


def summarize(settings: Settings, text: str, source: str, run=None) -> dict[str, Any]:
    """Runs the model, then records its line; nothing when the conversation produced nothing."""
    run = run or run_job
    if not worth_summarizing(text):
        return {"recorded": False, "reason": "conversation vide"}
    excerpt = text[-TRANSCRIPT_MAX:]
    request = JobRequest(action=ACTION, params={"source": source, "transcript": excerpt},
                         requested_by=f"journal:{source}")
    result = run(request, settings)
    if not result.ok or not isinstance(result.data, dict):
        raise InvalidRequestError(f"résumé impossible : {result.error or result.summary}")
    data = result.data
    if not data.get("action") or not str(data.get("resume") or "").strip():
        return {"recorded": False, "reason": "rien de fait"}
    todo = data.get("todo") if isinstance(data.get("todo"), int) and data.get("todo") > 0 else None
    entry = store.add(settings, source, "resume", data["resume"], todo)
    return {"recorded": True, "entry": entry}


def detach(settings: Settings, text: str, source: str, spawn=None) -> dict[str, Any]:
    """Hooks have a short time budget: keep the text in var/ and summarize it in a detached process."""
    if not worth_summarizing(text):
        return {"recorded": False, "reason": "conversation vide"}
    pending = settings.var_dir / PENDING_DIR
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{new_job_id()}.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text[-TRANSCRIPT_MAX:])
    cli = settings.root / ".venv" / "bin" / "ultron-hub"
    cmd = [str(cli if cli.exists() else Path(sys.argv[0]).resolve()), "journal", "summarize",
           "--pending", str(path), "--source", source]
    (spawn or _spawn)(cmd)
    return {"recorded": False, "detached": True, "pending": str(path)}


def _spawn(cmd: list[str]) -> None:
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def read_pending(settings: Settings, raw_path: str) -> str:
    """Text of a pending file, removed once read; only files of var/journal-pending/."""
    pending = (settings.var_dir / PENDING_DIR).resolve()
    path = Path(raw_path).resolve()
    if path.parent != pending or path.suffix != ".txt":
        raise InvalidRequestError("fichier en attente hors de var/journal-pending refusé")
    try:
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)
