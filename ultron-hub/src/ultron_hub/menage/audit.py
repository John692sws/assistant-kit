"""Bimonthly audit of the tidy-up map (todo #127): Claude re-reads config/menage.yaml against the disk and the docs.

The audit only PROPOSES changes to the YAML (written to var/menage/audit-<date>.md); nothing is applied.
Everything sent to Claude is data collected here, best effort: an unreadable source becomes a line of `warnings`.
No secret path is ever opened or listed (names from the YAML `secrets` and `exclusions`, plus ~/forum and ~/.ssh).
"""

import fnmatch
import json
import os
import pwd
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.core.job import JobRequest
from ultron_hub.core.runner import run_job

log = get_logger("menage.audit")

STATE_FILE = "audit.json"
EVERY_DAYS = 60
YAML_MAX = 12 * 1024
DOC_MAX = 4 * 1024
DOC_READ_MAX = 256 * 1024
TREE_TOP_MAX = 80
TREE_CHILD_MAX = 40
SUMMARY_MAX = 300
LINE_MAX = 200
SNIPPET_MAX = 600
PROPOSALS_MAX = 8
ALWAYS_SKIPPED = ("~/forum", "~/.ssh")
DOCS = (("serveur.md", "Architecture Workspace"), ("charte.md", ""))
DASHES = re.compile(r"[—–]")


# --- state ----------------------------------------------------------------------------------------------------------

def due(state_dir: Path, now: float, every_days: int = EVERY_DAYS) -> bool:
    """True when the last audit is older than `every_days` days; no state or unreadable state = due."""
    try:
        last = float(json.loads((Path(state_dir) / STATE_FILE).read_text(encoding="utf-8"))["last_ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    if not 0 <= last <= now:
        return True
    return now - last >= every_days * 86400


def _write_private(path: Path, text: str) -> None:
    """Atomic replacement, file 0600, folder 0700 when created."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def mark_done(state_dir: Path, now: float) -> None:
    _write_private(Path(state_dir) / STATE_FILE, json.dumps({"last_ts": float(now)}) + "\n")


# --- collection -----------------------------------------------------------------------------------------------------

def _expand(pattern: str, home: str) -> str:
    if pattern == "~" or pattern.startswith("~/"):
        pattern = home + pattern[1:]
    return pattern.replace("**/", "*/", 1) if pattern.startswith("**/") else pattern


def _protected_patterns(yaml_text: str, home: str, warnings: list[str]) -> list[str]:
    names: list[str] = list(ALWAYS_SKIPPED)
    try:
        data = yaml.safe_load(yaml_text) or {}
        for key in ("secrets", "exclusions"):
            names += [n for n in (data.get(key) or []) if isinstance(n, str)]
    except (yaml.YAMLError, AttributeError):
        warnings.append("carte illisible : seuls ~/forum et ~/.ssh sont exclus")
    return [_expand(n, home) for n in names]


def _is_protected(path: str, patterns: list[str], home: str) -> bool:
    """The path, or one of its parents below home, matches a secret/excluded pattern."""
    current = path
    while current and current != home and current != "/":
        if any(fnmatch.fnmatchcase(current, p) for p in patterns):
            return True
        current = os.path.dirname(current)
    return False


def _entries(path: str) -> list[os.DirEntry]:
    with os.scandir(path) as it:
        return sorted(it, key=lambda e: e.name)


def _count(path: str) -> int | None:
    try:
        with os.scandir(path) as it:
            return sum(1 for _ in it)
    except OSError:
        return None


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _tree(home: str, patterns: list[str], warnings: list[str]) -> list[dict[str, Any]]:
    """Two levels of `home`: names and number of entries, links never followed, protected paths left out."""
    try:
        top = _entries(home)
    except OSError:
        warnings.append("dossier personnel illisible")
        return []
    out: list[dict[str, Any]] = []
    for entry in top[:TREE_TOP_MAX]:
        if _is_protected(entry.path, patterns, home):
            continue
        item: dict[str, Any] = {"name": entry.name}
        if entry.is_symlink():
            item["type"] = "link"
        elif _is_dir(entry):
            item["type"] = "dir"
            item["entries"] = _count(entry.path)
            try:
                children = [c for c in _entries(entry.path) if not _is_protected(c.path, patterns, home)]
            except OSError:
                warnings.append(f"{entry.name} illisible")
                children = []
            item["children"] = [
                {"name": c.name, "type": "link" if c.is_symlink() else "dir" if _is_dir(c) else "file",
                 **({"entries": _count(c.path)} if not c.is_symlink() and _is_dir(c) else {})}
                for c in children[:TREE_CHILD_MAX]]
            if len(children) > TREE_CHILD_MAX:
                item["more"] = len(children) - TREE_CHILD_MAX
        else:
            item["type"] = "file"
        out.append(item)
    if len(top) > TREE_TOP_MAX:
        out.append({"name": "...", "type": "more", "entries": len(top) - TREE_TOP_MAX})
    return out


def _section(text: str, title: str) -> str:
    """The Markdown section whose heading contains `title`, up to the next heading of the same or higher level."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"(#+)\s", line)
        if m and title.lower() in line.lower():
            level = len(m.group(1))
            end = len(lines)
            for j in range(i + 1, len(lines)):
                n = re.match(r"(#+)\s", lines[j])
                if n and len(n.group(1)) <= level:
                    end = j
                    break
            return "\n".join(lines[i:end])
    return ""


def _read_text(path: Path, limit: int) -> str:
    """Regular file only (no link), at most `limit` bytes."""
    if path.is_symlink() or not path.is_file():
        raise OSError("not a regular file")
    with open(path, "rb") as fh:
        return fh.read(limit).decode("utf-8", errors="replace")


def _doc(path: Path, patterns: list[str], home: str, warnings: list[str]) -> str | None:
    if _is_protected(str(path), patterns, home):
        return None
    try:
        text = _read_text(path, DOC_READ_MAX)
    except OSError:
        warnings.append(f"{path.name} illisible")
        return None
    title = dict(DOCS).get(path.name, "")
    if title:
        part = _section(text, title)
        if not part:
            warnings.append(f"{path.name} : section « {title} » absente")
        text = part or text
    return text[:DOC_MAX]


def collect(home: str, config_path: Path, docs: list[Path]) -> dict[str, Any]:
    """What Claude gets: the raw map, the tree of `home` on two levels and short excerpts of the docs."""
    warnings: list[str] = []
    try:
        yaml_text = _read_text(Path(config_path), YAML_MAX + 1)
    except OSError:
        yaml_text = ""
        warnings.append("carte illisible")
    if len(yaml_text) > YAML_MAX:
        yaml_text = yaml_text[:YAML_MAX]
        warnings.append("carte coupée à 12 Ko")
    patterns = _protected_patterns(yaml_text, home, warnings)
    excerpts = {}
    for path in docs:
        text = _doc(Path(path), patterns, home, warnings)
        if text is not None:
            excerpts[Path(path).name] = text
    return {"yaml": yaml_text, "tree": _tree(home, patterns, warnings), "docs": excerpts, "warnings": warnings}


# --- run ------------------------------------------------------------------------------------------------------------

def _clean(value: Any, limit: int) -> str:
    return DASHES.sub("-", str(value)).strip()[:limit]


def _check(data: Any, sections: set[str]) -> tuple[str, list[dict[str, str]], int]:
    """Validated output: (summary, proposals, number dropped). Unknown section or unparsable YAML = dropped."""
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) \
            or not isinstance(data.get("proposals"), list):
        raise UltronHubError("menage-audit: missing or invalid structured_output")
    proposals: list[dict[str, str]] = []
    dropped = 0
    for raw in data["proposals"][:PROPOSALS_MAX]:
        if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str)
                                                for k in ("section", "changement", "raison", "yaml")):
            dropped += 1
            continue
        try:
            yaml.safe_load(raw["yaml"])
        except yaml.YAMLError:
            dropped += 1
            continue
        if sections and raw["section"] not in sections:
            dropped += 1
            continue
        proposals.append({"section": raw["section"][:60], "changement": _clean(raw["changement"], LINE_MAX),
                          "raison": _clean(raw["raison"], LINE_MAX), "yaml": raw["yaml"].strip()[:SNIPPET_MAX]})
    return _clean(data["summary"], SUMMARY_MAX), proposals, dropped


def render_markdown(day: str, summary: str, proposals: list[dict[str, str]]) -> str:
    lines = [f"# Audit de la carte de rangement du {day}", "", summary, "",
             "Rien n'est appliqué : modifier `config/menage.yaml` = une todo, relue avant fusion.", ""]
    if not proposals:
        lines.append("Aucune modification proposée.")
    for n, p in enumerate(proposals, 1):
        lines += [f"## {n}. {p['section']} : {p['changement']}", "", f"Raison : {p['raison']}", "",
                  "```yaml", p["yaml"], "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def default_home() -> str:
    return pwd.getpwuid(os.getuid()).pw_dir  # from the account, never from $HOME


def run(settings, home: str | None = None, now: float | None = None,
        job: Callable[..., Any] = run_job) -> dict[str, Any]:
    """Collect, ask Claude, write var/menage/audit-<date>.md, remember the date. Returns {proposals, path, summary}."""
    home = home or default_home()
    now = time.time() if now is None else now
    config_path = Path(settings.root) / "config" / "menage.yaml"
    docs = [Path(home) / "notes" / "serveur.md", Path(home) / "ultron" / "esprit" / "charte.md"]
    metrics = collect(home, config_path, docs)
    try:
        sections = set((yaml.safe_load(metrics["yaml"]) or {}).keys())
    except (yaml.YAMLError, AttributeError):
        sections = set()
    result = job(JobRequest(action="menage-audit", params={"metrics": json.dumps(metrics, ensure_ascii=False)}),
                 settings)
    if not result.ok:
        raise UltronHubError(f"menage-audit failed: {result.error}")
    summary, proposals, dropped = _check(result.data, {s for s in sections if isinstance(s, str)})
    if dropped:
        log.warning("menage-audit: %d proposal(s) dropped (invalid section or YAML)", dropped)
    day = datetime.fromtimestamp(now, ZoneInfo(settings.todo_timezone)).strftime("%Y-%m-%d")
    state_dir = Path(settings.var_dir) / "menage"
    path = state_dir / f"audit-{day}.md"
    _write_private(path, render_markdown(day, summary, proposals))
    mark_done(state_dir, now)
    return {"proposals": proposals, "path": str(path), "summary": summary}
