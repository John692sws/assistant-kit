"""Load and validate `config/menage.yaml`, plus the read-only helpers the checks share.

Nothing in this package changes the disk: the scan only reads names, sizes, dates (and file contents
for duplicate / plugin comparison, never for a secret).
"""

import fnmatch
import os
import pwd
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ultron_hub.common.errors import ConfigError

MAX_ENTRIES = 1_500_000  # directory entries looked at in one scan
MAX_SECONDS = 60.0  # per check

REQUIRED = {
    "exclusions": list, "secrets": list, "ignore_dirs": list, "structure": dict, "worktrees": dict,
    "disposable": dict, "space": dict, "code": dict,
}


def user_home() -> str:
    """Home directory from the password database, never from $HOME."""
    return pwd.getpwuid(os.getuid()).pw_dir


def expand(value: str, home: str) -> str:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home + value[1:]
    return value


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"menage.yaml illisible : {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ConfigError("menage.yaml : un objet est attendu")
    if data.get("version") != 1:
        raise ConfigError("menage.yaml : version 1 attendue")
    for key, kind in REQUIRED.items():
        if not isinstance(data.get(key), kind):
            raise ConfigError(f"menage.yaml : '{key}' manquant ou de mauvais type")
    return data


@dataclass
class Scan:
    """One scan: configuration, expanded home, warnings, and the budget shared by every walk."""

    cfg: dict[str, Any]
    home: str
    now: float = field(default_factory=time.time)
    warnings: list[str] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)
    entries: int = 0
    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_SECONDS)
    exhausted: bool = False
    todo_db: Path | None = None
    history_path: Path | None = None
    probe: Callable[[], list[dict[str, Any]]] | None = None
    size_cache: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.exclusions = [self.p(x) for x in self.cfg["exclusions"]]
        self.secrets = [self.p(x) if not x.startswith("*") else x for x in self.cfg["secrets"]]
        self.ignore_dirs = set(self.cfg["ignore_dirs"])

    def p(self, value: str) -> str:
        return os.path.normpath(expand(str(value), self.home))

    def reset_budget(self) -> None:
        self.entries = 0
        self.exhausted = False
        self.deadline = time.monotonic() + MAX_SECONDS

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def is_excluded(self, path: str) -> bool:
        return any(path == x or path.startswith(x + "/") for x in self.exclusions)

    def is_secret(self, path: str) -> bool:
        for pat in self.secrets:
            if path == pat or path.startswith(pat + "/") or fnmatch.fnmatch(path, pat):
                return True
        return False

    def tick(self) -> bool:
        """Count one entry; False once the budget (entries or time) is spent."""
        if self.exhausted:
            return False
        self.entries += 1
        if self.entries > MAX_ENTRIES or (self.entries % 2000 == 0 and time.monotonic() > self.deadline):
            self.exhausted = True
            self.warn("budget du scan épuisé : résultats partiels")
            return False
        return True

    def age_days(self, mtime: float) -> float:
        return (self.now - mtime) / 86400.0


def scandir(scan: Scan, path: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError as exc:
        scan.warn(f"lecture impossible : {path} ({type(exc).__name__})")
        return []


def iter_tree(scan: Scan, root: str, ignore: set[str] | None = None,
              descend: Callable[[str], bool] | None = None) -> Iterator[os.DirEntry]:
    """Every non-symlink entry under `root`. Excluded paths are never entered; symlinks are never followed
    (and not yielded); directories named in `ignore` are yielded but not entered."""
    ignore = scan.ignore_dirs if ignore is None else ignore
    if scan.is_excluded(root) or not os.path.isdir(root) or os.path.islink(root):
        return
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in scandir(scan, current):
            if not scan.tick():
                return
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if scan.is_excluded(entry.path):
                continue
            yield entry
            if is_dir and entry.name not in ignore and (descend is None or descend(entry.path)):
                stack.append(entry.path)


class BudgetExceeded(Exception):
    pass


def tree_size(scan: Scan, path: str, cache: dict[str, int]) -> int:
    """Apparent size in bytes of `path` (like `du --apparent-size`): symlinks not followed, exclusions skipped."""
    if path in cache:
        return cache[path]
    total = 0
    for entry in scandir(scan, path):
        if not scan.tick():
            raise BudgetExceeded(path)
        try:
            if entry.is_symlink() or scan.is_excluded(entry.path):
                continue
            if entry.is_dir(follow_symlinks=False):
                total += tree_size(scan, entry.path, cache)
            else:
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    cache[path] = total
    return total
