"""Tokens, estimated cost, duration and delivered todos of one interactive Claude Code transcript (+ its subagents)."""

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultron_hub.cout.usage_jsonl import TOKEN_KEYS, poids, usage_of

# `usage_jsonl.poids` is the existing price table (equivalent tokens: input 1, output 5, cache write 1.25, cache read 0.1,
# times a model factor); one sonnet input token is priced at this many USD (order of magnitude, not an invoice).
USD_PAR_UNITE = 3e-6
TODO_RE = re.compile(r"\(todo #(\d+)\)")


def _parse(stamp: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


def transcript_files(transcript: Path) -> list[Path]:
    """The transcript plus `<id>/subagents/*.jsonl` next to it."""
    return [transcript, *sorted((transcript.parent / transcript.stem / "subagents").glob("*.jsonl"))]


def read_session(transcript: Path) -> dict[str, Any]:
    """One count per message id (a message is written several times while streaming: the last line wins)."""
    messages: dict[str, tuple[dict[str, int], float]] = {}
    stamps: list[datetime] = []
    for path in transcript_files(transcript):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            when = _parse(event.get("timestamp")) if event.get("timestamp") else None
            if when:
                stamps.append(when)
            message = event.get("message")
            if event.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            key = message.get("id") or f"{path.name}:{len(messages)}"
            messages[key] = (usage_of(usage), poids(usage, message.get("model")))
    tokens = {k: sum(t[k] for t, _ in messages.values()) for k in TOKEN_KEYS}
    units = sum(p for _, p in messages.values())
    return {**tokens, "messages": len(messages), "cout_usd": round(units * USD_PAR_UNITE, 4),
            "debut": min(stamps) if stamps else None, "fin": max(stamps) if stamps else None}


def delivered_todos(repo: Path, debut: datetime | None, fin: datetime | None) -> list[int]:
    """Todo numbers of the commits `(todo #N)` of the repo (all branches) inside the window."""
    if not debut or not fin:
        return []
    cmd = ["git", "-C", str(repo), "log", "--all", "--format=%s", f"--since={debut.isoformat()}",
           f"--until={fin.isoformat()}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({int(n) for n in TODO_RE.findall(out)})


def measure(transcript: Path, repo: Path) -> dict[str, Any]:
    data = read_session(transcript)
    debut, fin = data.pop("debut"), data.pop("fin")
    duree = round((fin - debut).total_seconds()) if debut and fin else 0
    todos = delivered_todos(repo, debut, fin)
    iso = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None  # noqa: E731
    cout = data["cout_usd"]
    return {**data, "debut": iso(debut), "fin": iso(fin), "duree_s": duree, "todos_livrees": todos,
            "cout_par_todo": round(cout / len(todos), 4) if todos else None}
