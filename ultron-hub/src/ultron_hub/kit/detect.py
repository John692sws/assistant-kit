"""Personal data detector for the public kit export (todo #386). Standard library only, read only.

`scan(root, patterns)` walks text files under root and reports where personal data sits. A Finding holds the path,
the line and the rule name, never the matched value: the report itself must not leak what it finds.
The personal forbidden list lives outside the repository (INTERDITS_PATH below); without it we refuse to scan.
"""

import fnmatch
import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path

# Not derived from $HOME, same reasoning as todo/closing.py KEY_PATH.
INTERDITS_PATH = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "ultron-hub" / "kit-interdits.txt"
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache"}
FORBIDDEN_DIR = "var"
FORBIDDEN_NAMES = ("*.db", "*.key", "*.pem", "*.pass", ".env", "auth.json", "credentials*")
FORBIDDEN_SUFFIXES = ("config/dashboard.yaml", "config/groupe.yaml")
CGNAT_RANGE = "100.64.0.0/10"  # the documented range itself is not an address

RULES = {
    "home-en-dur": re.compile(r"/home/[A-Za-z0-9_.-]+"),
    "ip-tailscale": re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    "tailnet-ts-net": re.compile(r"[A-Za-z0-9-]+\.ts\.net\b"),
    "adresse-mail": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    "jeton-telegram": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"),
}
# Explicit allow lists for fictitious test data (todo #396). A match is tolerated only when it is exactly one of these.
FICTIVE_IPS = frozenset(f"100.64.0.{n}" for n in range(1, 10))
FICTIVE_TAILNETS = frozenset({"exemple.ts.net"})
FICTIVE_MAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net", "exemple.fr"})
FICTIVE_HOMES = re.compile(r"/home/exemple\d*")
FICTIVE_TOKEN_PREFIX = "123456789:"
URL_USERINFO = re.compile(r"://[^/\s@]*@")  # user:pw@host in a URL is not a mail address


def _fictive(rule: str, value: str) -> bool:
    if rule == "ip-tailscale":
        return value in FICTIVE_IPS
    if rule == "tailnet-ts-net":
        return value.lower() in FICTIVE_TAILNETS
    if rule == "adresse-mail":
        return value.rsplit("@", 1)[1].lower() in FICTIVE_MAIL_DOMAINS
    if rule == "home-en-dur":
        return FICTIVE_HOMES.fullmatch(value) is not None
    if rule == "jeton-telegram":
        return value.startswith(FICTIVE_TOKEN_PREFIX)
    return False


class RefusedError(Exception):
    """The forbidden list is missing or unreadable: scanning blind is refused."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int  # 0 for a forbidden path name
    rule: str


def load_interdits(path: Path | None = None) -> list[str]:
    path = path or INTERDITS_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RefusedError(f"forbidden list unreadable: {path} ({type(exc).__name__})") from exc
    patterns = [ln.strip().lower() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not patterns:
        raise RefusedError(f"forbidden list empty: {path}")
    return patterns


def _forbidden_name(rel: Path, is_dir: bool) -> str | None:
    if is_dir:
        return "nom-interdit" if rel.name == FORBIDDEN_DIR else None
    name = rel.name
    if any(fnmatch.fnmatch(name, pat) for pat in FORBIDDEN_NAMES):
        return "nom-interdit"
    if rel.as_posix().endswith(FORBIDDEN_SUFFIXES) and rel.parent.name == "config":
        return "nom-interdit"
    return None


def _is_text(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return None if b"\0" in data[:8192] else data


def scan(root: Path, patterns: list[str]) -> list[Finding]:
    root = Path(root)
    lowered = [p.lower() for p in patterns if p]
    findings: list[Finding] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if d in SKIP_DIRS:
                continue
            rel = (base / d).relative_to(root)
            if _forbidden_name(rel, True):
                findings.append(Finding(rel.as_posix(), 0, "nom-interdit"))
                continue  # contents of a forbidden folder are not scanned
            keep.append(d)
        dirnames[:] = keep
        for fname in sorted(filenames):
            full = base / fname
            rel = full.relative_to(root)
            if full.is_symlink():
                continue
            if _forbidden_name(rel, False):
                findings.append(Finding(rel.as_posix(), 0, "nom-interdit"))
                continue
            if any(p in rel.as_posix().lower() for p in lowered):
                findings.append(Finding(rel.as_posix(), 0, "interdit-perso"))
            data = _is_text(full)
            if data is not None:
                findings.extend(_scan_text(rel.as_posix(), data.decode("utf-8", errors="replace"), lowered))
    return findings


def _scan_text(rel: str, text: str, lowered: list[str]) -> list[Finding]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if any(p in low for p in lowered):
            out.append(Finding(rel, n, "interdit-perso"))
        checked = line.replace(CGNAT_RANGE, "")
        for rule, rx in RULES.items():
            text = URL_USERINFO.sub("://", checked) if rule == "adresse-mail" else checked
            if any(not _fictive(rule, m.group(0)) for m in rx.finditer(text)):
                out.append(Finding(rel, n, rule))
    return out
