#!/usr/bin/env python3
"""Fill the {{MARKER}} placeholders of the kit templates (standard library only).

Usage: remplir.py <answers.json> <folder>
Replaces every {{NAME}} found in the text files of <folder> by the answer for NAME. Nothing is written unless every
marker found has an answer and every answer is a non-empty string; otherwise exit code 1 with the list on stdout.
Stdout is one JSON object.
"""

import json
import re
import sys
from pathlib import Path

MARKER = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache"}


def text_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink() and not (SKIP_DIRS & set(p.relative_to(root).parts)):
            try:
                yield p, p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue  # binary file


def fill(answers: dict, root: Path) -> dict:
    empty = sorted(k for k, v in answers.items() if not isinstance(v, str) or not v.strip())
    pending: dict[Path, str] = {}
    unknown: dict[str, list[str]] = {}
    for path, text in text_files(root):
        for name in set(MARKER.findall(text)):
            if name not in answers or name in empty:
                unknown.setdefault(name, []).append(str(path.relative_to(root)))
        if MARKER.search(text):
            pending[path] = MARKER.sub(lambda m: str(answers.get(m.group(1), m.group(0))), text)
    if empty or unknown:
        return {"status": "error", "empty_answers": empty,
                "unfilled": {k: sorted(v) for k, v in sorted(unknown.items())}}
    for path, new in pending.items():
        path.write_bytes(new.encode("utf-8"))
    return {"status": "ok", "files": sorted(str(p.relative_to(root)) for p in pending)}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"status": "error", "error": "usage: remplir.py <answers.json> <folder>"}))
        return 2
    try:
        answers = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        if not isinstance(answers, dict):
            raise ValueError("answers must be a JSON object")
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": f"answers unreadable: {type(exc).__name__}"}))
        return 2
    root = Path(argv[1])
    if not root.is_dir():
        print(json.dumps({"status": "error", "error": f"not a directory: {root}"}))
        return 2
    res = fill(answers, root)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
