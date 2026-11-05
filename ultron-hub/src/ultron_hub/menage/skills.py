"""Skill usage count (todo #451, no AI, read only): which skills of ~/.claude/skills/ are never or no longer used.

A use = a `Skill` tool_use whose `input.skill` is the name (or `plugin:name`), a `Read` of `.../skills/<name>/SKILL.md`, or a
`Bash` command mentioning `skills/<name>/SKILL.md`, found line by line in ~/.claude/projects/**/*.jsonl (sub-agents included;
invalid lines are skipped). Date = the line's `timestamp`. Statuses: `corbeille` = no use for 30 days AND at least 30 days old,
`a_revoir` = never used and at least 14 days old, else `ok`. Skills live in folders holding a SKILL.md (`.trash`, `synced` excluded).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REVIEW_AFTER_DAYS = 14
TRASH_AFTER_DAYS = 30
SKIPPED = {".trash", "synced"}
_BASH_RE = re.compile(r"skills/([^/\s'\"\\]+)/SKILL\.md")


def default_skills_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def default_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def list_skills(skills_dir: Path) -> dict[str, float]:
    """name -> creation time (oldest mtime of the folder and its SKILL.md); links are not followed."""
    found: dict[str, float] = {}
    try:
        entries = sorted(skills_dir.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            if entry.name in SKIPPED or entry.is_symlink() or not entry.is_dir() or not (entry / "SKILL.md").is_file():
                continue
            found[entry.name] = min(entry.stat().st_mtime, (entry / "SKILL.md").stat().st_mtime)
        except OSError:
            continue
    return found


def _when(text: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _names_in(block: dict[str, Any], names: set[str]) -> set[str]:
    """Skills a tool_use block uses."""
    tool, params = block.get("name"), block.get("input")
    if not isinstance(params, dict):
        return set()
    if tool == "Skill":
        skill = params.get("skill")
        if isinstance(skill, str):
            return {skill.strip().lstrip("/").rsplit(":", 1)[-1]} & names
    elif tool == "Read":
        path = params.get("file_path")
        if isinstance(path, str) and path.endswith("/SKILL.md") and "/skills/" in path:
            return {path.rsplit("/", 2)[-2]} & names
    elif tool == "Bash":
        command = params.get("command")
        if isinstance(command, str):
            return set(_BASH_RE.findall(command)) & names
    return set()


def count_uses(projects_dir: Path, names: set[str]) -> dict[str, tuple[int, datetime | None]]:
    """name -> (uses, last use) over every transcript under projects_dir."""
    uses: dict[str, list[Any]] = {n: [0, None] for n in names}
    seen: set[str] = set()  # a resumed session repeats earlier lines in a new file
    try:
        files = sorted(projects_dir.rglob("*.jsonl"))
    except OSError:
        files = []
    for path in files:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"tool_use"' not in line or ("Skill" not in line and "SKILL.md" not in line):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                message = obj.get("message") if isinstance(obj, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                moment = _when(obj.get("timestamp"))
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    hit = _names_in(block, names)
                    if not hit:
                        continue
                    ident = block.get("id")
                    if isinstance(ident, str):
                        if ident in seen:
                            continue
                        seen.add(ident)
                    for name in hit:
                        uses[name][0] += 1
                        if moment and (uses[name][1] is None or moment > uses[name][1]):
                            uses[name][1] = moment
    return {n: (v[0], v[1]) for n, v in uses.items()}


def status_of(n: int, last: datetime | None, age_days: int, now: datetime) -> str:
    idle = (now - last).days if last else None
    if age_days >= TRASH_AFTER_DAYS and (idle is None or idle >= TRASH_AFTER_DAYS):
        return "corbeille"
    if n == 0 and age_days >= REVIEW_AFTER_DAYS:
        return "a_revoir"
    return "ok"


def usage(skills_dir: Path | None = None, projects_dir: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Per skill: nom, n, dernier (ISO or null), cree (ISO), age_jours, statut; plus the names to review / trash."""
    skills_dir = skills_dir or default_skills_dir()
    projects_dir = projects_dir or default_projects_dir()
    clock = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    created = list_skills(skills_dir)
    counted = count_uses(projects_dir, set(created))
    rows = []
    for name, made in created.items():
        n, last = counted[name]
        born = datetime.fromtimestamp(made, timezone.utc)
        age = max(0, (clock - born).days)
        rows.append({"nom": name, "n": n, "dernier": last.isoformat(timespec="seconds") if last else None,
                     "cree": born.isoformat(timespec="seconds"), "age_jours": age, "statut": status_of(n, last, age, clock)})
    return {"skills": rows, "a_revoir": [r["nom"] for r in rows if r["statut"] == "a_revoir"],
            "corbeille": [r["nom"] for r in rows if r["statut"] == "corbeille"]}


def line(data: dict[str, Any]) -> str:
    names = data["a_revoir"] + data["corbeille"]
    return (f"🧩 Skills : {len(data['a_revoir'])} à revoir, {len(data['corbeille'])} à la corbeille"
            + (f" ({', '.join(names[:8])}{'…' if len(names) > 8 else ''})" if names else ""))
