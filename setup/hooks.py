#!/usr/bin/env python3
"""Merge the assistant's Claude Code hooks into ~/.claude/settings.json (standard library only).

Usage: hooks.py <ultron-hub folder> [--essai]
Adds the missing hooks (SessionStart, SessionEnd, PostToolUse) that call <folder>/.venv/bin/ultron-hub.
Never overwrites the file: every other key and hook stays as it is. Before the first write, the original file is
copied once to settings.json.avant-ultron. Idempotent: a second run writes nothing. --essai writes nothing.
Refused (exit 1): settings.json is a symlink, not valid JSON, not an object, or has an unexpected "hooks".
Stdout is one JSON object.
"""

import json
import os
import shutil
import sys
from pathlib import Path

HOOKS = (
    ("SessionStart", "esprit context --claude-hook"),
    ("SessionEnd", "journal summarize --claude-hook --detach"),
    ("PostToolUse", "esprit contexte --claude-hook"),
)
TIMEOUT_S = 30
BACKUP_SUFFIX = ".avant-ultron"


class Refused(Exception):
    pass


def wanted(hub: Path) -> list[tuple[str, str]]:
    cli = hub / ".venv" / "bin" / "ultron-hub"
    return [(event, f"{cli} {args}") for event, args in HOOKS]


def read_settings(path: Path) -> dict:
    if path.is_symlink():
        raise Refused(f"{path} is a symlink")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused(f"{path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise Refused(f"{path} is not a JSON object")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or not all(isinstance(g, list) for g in hooks.values()):
        raise Refused(f'unexpected "hooks" in {path}')
    return data


def has_hook(groups: list, command: str) -> bool:
    return any(isinstance(g, dict) and isinstance(g.get("hooks"), list)
               and any(isinstance(h, dict) and h.get("command") == command for h in g["hooks"]) for g in groups)


def merge(path: Path, hub: Path, essai: bool) -> dict:
    data = read_settings(path)
    hooks = data.setdefault("hooks", {})
    added = []
    for event, command in wanted(hub):
        groups = hooks.setdefault(event, [])
        if not has_hook(groups, command):
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": TIMEOUT_S}]})
            added.append(event)
    res = {"status": "ok", "settings": str(path), "added": added, "backup": None, "essai": essai}
    if not added or essai:
        return res
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    if backup.exists():
        res["backup"] = str(backup)
    tmp = path.with_name(path.name + ".tmp-ultron")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)
    written = read_settings(path).get("hooks", {})
    missing = [e for e, c in wanted(hub) if not has_hook(written.get(e, []), c)]
    if missing:
        raise Refused(f"hooks missing after writing: {', '.join(missing)}")
    return res


def main(argv: list[str]) -> int:
    essai = "--essai" in argv
    rest = [a for a in argv if a != "--essai"]
    if len(rest) != 1:
        print(json.dumps({"status": "error", "error": "usage: hooks.py <ultron-hub folder> [--essai]"}))
        return 2
    hub = Path(rest[0]).expanduser().resolve()
    path = Path.home() / ".claude" / "settings.json"
    try:
        res = merge(path, hub, essai)
    except Refused as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
