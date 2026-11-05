"""Kit export (todo #387): copy the allowlisted files of a git ref into <out>/ultron-hub, then run the detector.

Files come from the committed tree of the ref (`git ls-tree` + `git archive`), never from the working tree or var/.
Classification: exclude wins, then include; a file in neither list is reported as unclassified and not copied.
"""

import fnmatch
import io
import posixpath
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from ultron_hub.common.settings import DEFAULT_ROOT as PROJECT_ROOT
from ultron_hub.kit import detect

TARGET_NAME = "ultron-hub"
# Kit folders of the repository, copied next to the code (<out>/gabarits, <out>/setup) instead of inside it.
EXTRA_DIRS = {"kit/gabarits/": "gabarits/", "kit/setup/": "setup/"}
# Kit files copied at the root of <out> (todo #389): the README for the user, the CLAUDE.md for their Claude Code.
EXTRA_FILES = {"kit/README.md": "README.md", "kit/CLAUDE.md": "CLAUDE.md"}
DEFAULT_REF = "main"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "kit.yaml"


class ExportError(Exception):
    pass


@dataclass
class Plan:
    kept: list[str]
    excluded: list[str]
    unclassified: list[str]


def load_rules(path: Path) -> tuple[list[str], list[str]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ExportError(f"kit config unreadable: {path} ({type(exc).__name__})") from exc
    include, exclude = raw.get("include") or [], raw.get("exclude") or []
    if not include or not all(isinstance(p, str) for p in include + exclude):
        raise ExportError(f"kit config invalid (need a non-empty include list of strings): {path}")
    return include, exclude


def load_replacements(path: Path) -> dict[str, str]:
    """The `remplacer:` table of the kit config: exact literal strings, applied in file order (longest first)."""
    try:
        raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("remplacer") or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ExportError(f"kit config unreadable: {path} ({type(exc).__name__})") from exc
    if not isinstance(raw, dict) or not all(isinstance(k, str) and k and isinstance(v, str) for k, v in raw.items()):
        raise ExportError(f"kit config invalid (remplacer must map non-empty strings to strings): {path}")
    return raw


def apply_replacements(target: Path, table: dict[str, str]) -> int:
    """Replace the literal keys of `table` in every UTF-8 text file under `target`; returns the files changed."""
    pairs = sorted(table.items(), key=lambda kv: -len(kv[0]))
    changed = 0
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        new = text
        for old, rep in pairs:
            new = new.replace(old, rep)
        if new != text:
            path.write_bytes(new.encode("utf-8"))
            changed += 1
    return changed


def _git(repo: Path, *args: str) -> bytes:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if res.returncode != 0:
        raise ExportError(f"git {args[0]} failed: {res.stderr.decode(errors='replace').strip()[:200]}")
    return res.stdout


def classify(paths: list[str], include: list[str], exclude: list[str]) -> Plan:
    plan = Plan([], [], [])
    for p in sorted(paths):
        if any(fnmatch.fnmatchcase(p, g) for g in exclude):
            plan.excluded.append(p)
        elif any(fnmatch.fnmatchcase(p, g) for g in include):
            plan.kept.append(p)
        else:
            plan.unclassified.append(p)
    return plan


def plan_export(repo: Path, ref: str, config: Path) -> Plan:
    include, exclude = load_rules(config)
    out = _git(repo, "ls-tree", "-r", "-z", "--name-only", ref)
    return classify([p for p in out.decode("utf-8").split("\0") if p], include, exclude)


def write_tree(repo: Path, ref: str, plan: Plan, target: Path) -> int:
    """Extract the kept files from `git archive <ref>`. A kept symlink whose target is a kept regular file of the
    same tree is written as a copy of it (todo #484: tasks/video-analyse-lot links to video-analyse); other
    symlinks and special entries are skipped."""
    wanted = set(plan.kept)
    count = 0
    archive = _git(repo, "archive", "--format=tar", ref)
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        members = tar.getmembers()
        regular = {m.name: m for m in members if m.isreg()}
        for member in members:
            if member.issym() and member.name in wanted:
                linked = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
                if linked in wanted and linked in regular and not member.linkname.startswith("/"):
                    dest = target / member.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(tar.extractfile(regular[linked]).read())  # type: ignore[union-attr]
                    dest.chmod(0o755 if regular[linked].mode & 0o111 else 0o644)
                    count += 1
                continue
            if not member.isreg():
                continue
            extra = EXTRA_FILES.get(member.name) or next(
                (new + member.name[len(old):] for old, new in EXTRA_DIRS.items() if member.name.startswith(old)), None)
            if member.name not in wanted and extra is None:
                continue
            dest = target.parent / extra if extra else target / member.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(tar.extractfile(member).read())  # type: ignore[union-attr]
            dest.chmod(0o755 if member.mode & 0o111 else 0o644)
            count += 1
    return count


def check_target(out: Path) -> Path:
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ExportError(f"output folder exists and is not empty: {out}")
    return out / TARGET_NAME
