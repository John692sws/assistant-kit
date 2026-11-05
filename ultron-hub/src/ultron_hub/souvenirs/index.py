"""Search index of old conversations (todo #133): SQLite FTS5, standard library only, no AI.

Sources, read only: text sources (notes, journal, knowledge sheets, memory, todo #260) and Claude Code sessions (`~/.claude/projects/**/*.jsonl`) and Hermes sessions (`~/.hermes/state.db`,
tables `sessions` and `messages`). Only what the user typed and what the assistant answered in plain text is kept:
never a tool call, a tool result, a file content or a reasoning block. Every line that looks like a secret is
dropped BEFORE it reaches the index. The sessions of ultron-hub jobs (cwd under var/runs or var/missions) and the
scheduled / sub-agent sessions of Hermes are left out. The index lives in var/souvenirs/ (0600), is rebuilt by
`ultron-hub souvenirs indexer` and is never committed (var/ is ignored).
"""

import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ultron_hub.common.settings import Settings

CLAUDE_CODE = "Claude Code"
HERMES = "Hermes"
NOTES = "Notes"
JOURNAL = "Journal"
SAVOIR = "Savoir"
MEMOIRE = "Mémoire"
SOURCE_NAMES = {"claude": CLAUDE_CODE, "hermes": HERMES, "notes": NOTES, "journal": JOURNAL, "savoir": SAVOIR,
                "memoire": MEMOIRE}
FILE_MAX_BYTES = 2_000_000       # a text file bigger than this is skipped
SENSITIVE_NAME_MARKERS = (".env", "auth", "credentials", "secret")
SESSION_TEXT_MAX = 400_000       # characters kept per session
EXCERPT_MAX = 1200
RESULT_MAX = 5
QUERY_WORDS_MAX = 8
REFRESH_AFTER_S = 3600
REFRESH_BUDGET_S = 25.0
HERMES_SKIPPED_SOURCES = ("cron", "subagent")  # scheduled tasks and sub-agents: not conversations
JOB_MARKERS = ("/var/runs", "/var/missions")
JOB_DIR_MARKERS = ("-var-runs-", "-var-missions-")  # Claude Code names a project folder after its cwd

# --- secrets: a line that looks like one is removed whole, before indexing ------------------------------------------

_KEYWORD = r"(?:password|passwd|pwd|mot\s+de\s+passe|mdp|token|secret|api[_ -]?key|cl[ée]\s+api|passphrase)"
SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),                        # OpenAI / Anthropic style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}|\bxapp-[A-Za-z0-9-]{8,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{12,}"),                  # AWS access keys
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),                      # Google API keys
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I),
    re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}"),              # Telegram bot token
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),     # JWT
    re.compile(rf"\b{_KEYWORD}\b[\"']?\s*(?:[:=]|\best\b|\bis\b|->)\s*[\"']?\S{{3,}}", re.I),  # « password: x »
    re.compile(rf"\b{_KEYWORD}\b[\"']?\s*[:=]?\s*[\"'`][^\"'`\s]{{3,}}[\"'`]", re.I),          # « token "x" »
    re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])"),  # long hex (hashes, keys)
)
_LONG_B64 = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE|PGP)[A-Z ]*-----")
_PEM_END = re.compile(r"-----END [A-Z ]*-----")
_SYSTEM_BLOCK = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+|task-notification)\b.*?</\1>",
                           re.S)


def looks_secret(line: str) -> bool:
    if any(p.search(line) for p in SECRET_PATTERNS):
        return True
    for word in _LONG_B64.findall(line):  # a long mixed-case token with digits (a path is lower case)
        if any(c.isupper() for c in word) and any(c.islower() for c in word) and any(c.isdigit() for c in word):
            return True
    return False


def scrub(text: str) -> str:
    """The text without any line that looks like a secret (PEM blocks are dropped whole)."""
    kept: list[str] = []
    in_pem = False
    for line in _SYSTEM_BLOCK.sub("", text).splitlines():
        if _PEM_BEGIN.search(line):
            in_pem = True
            continue
        if in_pem:
            in_pem = not _PEM_END.search(line)
            continue
        if line.strip() and not looks_secret(line):
            kept.append(line.strip())
    return "\n".join(kept)


# --- sources --------------------------------------------------------------------------------------------------------

def is_job_cwd(cwd: str | None) -> bool:
    return bool(cwd) and any(m in cwd for m in JOB_MARKERS)


def _blocks_text(content: Any) -> str:
    """Only the `text` blocks (a bare string is a text); tool_use, tool_result, thinking and images are ignored."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b["text"] for b in content
                     if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))


def read_claude_session(path: Path) -> dict[str, Any] | None:
    """{session, source, date, texts} of one jsonl, or None (job, empty, unreadable)."""
    if any(m in path.parent.name for m in JOB_DIR_MARKERS):
        return None
    texts: list[str] = []
    first_ts = ""
    session = path.stem
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") not in ("user", "assistant"):
                    continue
                if is_job_cwd(obj.get("cwd")):
                    return None
                if obj.get("isMeta") or obj.get("isSidechain"):
                    continue
                message = obj.get("message")
                if not isinstance(message, dict) or message.get("role") != obj["type"]:
                    continue
                clean = scrub(_blocks_text(message.get("content")))
                if clean:
                    texts.append(clean)
                    first_ts = first_ts or str(obj.get("timestamp") or "")
    except OSError:
        return None
    if not texts:
        return None
    return {"session": session, "source": CLAUDE_CODE, "date": first_ts[:10] or _mtime_date(path), "texts": texts}


def _mtime_date(path: Path) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(path.stat().st_mtime))


def claude_sessions(projects_dir: Path) -> Iterator[dict[str, Any]]:
    for path in sorted(projects_dir.glob("*/*.jsonl")):  # one folder per project; sub-agent files are deeper
        session = read_claude_session(path)
        if session:
            yield session


def hermes_sessions(state_db: Path) -> Iterator[dict[str, Any]]:
    """Hermes keeps its sessions in the SQLite file ~/.hermes/state.db; opened read only."""
    if not state_db.is_file():
        return
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return
    try:
        marks = ",".join("?" for _ in HERMES_SKIPPED_SOURCES)
        rows = con.execute(f"SELECT id, started_at, cwd FROM sessions WHERE source NOT IN ({marks}) "
                           "ORDER BY started_at", HERMES_SKIPPED_SOURCES).fetchall()
        for sid, started, cwd in rows:
            if is_job_cwd(cwd):
                continue
            texts = []
            for role, content in con.execute(
                    "SELECT role, content FROM messages WHERE session_id = ? AND role IN ('user', 'assistant') "
                    "AND active = 1 ORDER BY timestamp, id", (sid,)):
                clean = scrub(content) if isinstance(content, str) else ""
                if clean:
                    texts.append(clean)
            if texts:
                yield {"session": str(sid), "source": HERMES, "texts": texts,
                       "date": time.strftime("%Y-%m-%d", time.gmtime(started)) if started else ""}
    except sqlite3.Error:
        return
    finally:
        con.close()


def _sensitive(path: Path, root: Path) -> bool:
    """A path under the root whose own parts name a secret store is never read."""
    parts = path.relative_to(root).parts
    return any(m in part.lower() for part in parts for m in SENSITIVE_NAME_MARKERS)


def markdown_documents(root: Path, source: str, pattern: str) -> Iterator[dict[str, Any]]:
    """One document per markdown file under root (session = file path, date = modification date)."""
    if not root.is_dir():
        return
    for path in sorted(root.glob(pattern)):
        try:
            if not path.is_file() or path.is_symlink() or _sensitive(path, root) or path.stat().st_size > FILE_MAX_BYTES:
                continue
            text = scrub(path.read_text(encoding="utf-8", errors="replace"))
            date = _mtime_date(path)
        except OSError:
            continue
        if text:
            yield {"session": str(path), "source": source, "date": date, "texts": [text]}


def journal_documents(journal: Path) -> Iterator[dict[str, Any]]:
    """The journal jsonl, one document per day (the `texte` of each entry, prefixed with its time)."""
    days: dict[str, list[str]] = {}
    try:
        if not journal.is_file() or journal.stat().st_size > 10 * FILE_MAX_BYTES:
            return
        with journal.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or not isinstance(obj.get("texte"), str):
                    continue
                stamp = str(obj.get("date") or "")
                clean = scrub(obj["texte"])
                if stamp[:10] and clean:
                    days.setdefault(stamp[:10], []).append(f"{stamp[11:16]} {clean}".strip())
    except OSError:
        return
    for day, texts in sorted(days.items()):
        yield {"session": f"journal:{day}", "source": JOURNAL, "date": day, "texts": texts}


def text_documents(sources: dict[str, Path]) -> Iterator[dict[str, Any]]:
    """`sources` maps notes / journal / savoir / memoire to a path (a missing key is skipped)."""
    if "notes" in sources:
        yield from markdown_documents(sources["notes"], NOTES, "**/*.md")
    if "journal" in sources:
        yield from journal_documents(sources["journal"])
    if "savoir" in sources:
        yield from markdown_documents(sources["savoir"], SAVOIR, "**/*.md")
    if "memoire" in sources:
        yield from markdown_documents(sources["memoire"], MEMOIRE, "*.md")


# --- index ----------------------------------------------------------------------------------------------------------

def index_path(settings: Settings) -> Path:
    return settings.var_dir / "souvenirs" / "index.db"


def default_sources() -> tuple[Path, Path]:
    home = Path.home()
    return home / ".claude" / "projects", home / ".hermes" / "state.db"


def default_text_sources() -> dict[str, Path]:
    home = Path.home()
    return {"notes": home / "notes", "journal": home / "ultron" / "esprit" / "journal.jsonl",
            "savoir": home / "ultron" / "savoir", "memoire": home / ".claude" / "projects" / "-home-ultron" / "memory"}


def build(db_path: Path, claude_dir: Path, hermes_db: Path, budget_s: float | None = None,
          text_sources: dict[str, Path] | None = None) -> dict[str, Any]:
    """Rebuilds the index in a private temporary file, then replaces the old one. On a timeout nothing is replaced."""
    started = time.monotonic()
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(db_path.parent, 0o700)
    tmp = db_path.with_name(db_path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    os.close(os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
    con = sqlite3.connect(tmp)
    counts = dict.fromkeys((CLAUDE_CODE, HERMES, NOTES, JOURNAL, SAVOIR, MEMOIRE), 0)
    try:
        con.execute("PRAGMA journal_mode = OFF")
        con.execute("PRAGMA synchronous = OFF")
        con.execute("CREATE VIRTUAL TABLE souvenirs USING fts5(texte, session UNINDEXED, source UNINDEXED, "
                    "date UNINDEXED, tokenize = 'unicode61 remove_diacritics 2')")
        for stream in (claude_sessions(claude_dir), hermes_sessions(hermes_db), text_documents(text_sources or {})):
            for s in stream:
                if budget_s is not None and time.monotonic() - started > budget_s:
                    raise TimeoutError("budget exceeded")
                text = "\n".join(s["texts"])[:SESSION_TEXT_MAX]
                con.execute("INSERT INTO souvenirs(texte, session, source, date) VALUES (?, ?, ?, ?)",
                            (text, s["session"], s["source"], s["date"]))
                counts[s["source"]] += 1
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta VALUES ('built_at', ?)", (str(time.time()),))
        con.commit()
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    con.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, db_path)
    return {"sessions": sum(counts.values()), "claude_code": counts[CLAUDE_CODE], "hermes": counts[HERMES],
            "notes": counts[NOTES], "journal": counts[JOURNAL], "savoir": counts[SAVOIR], "memoire": counts[MEMOIRE],
            "seconds": round(time.monotonic() - started, 2), "path": str(db_path)}


def age_s(db_path: Path) -> float | None:
    try:
        return time.time() - db_path.stat().st_mtime
    except OSError:
        return None


def fts_query(words: list[str]) -> str:
    """Only quoted plain words reach MATCH: no operator, column filter, NEAR or quote from the user can get through."""
    terms = re.findall(r"\w+", " ".join(words), re.U)[:QUERY_WORDS_MAX]
    return " ".join(f'"{t}"' for t in terms)


def neutralize(text: str) -> str:
    return text.replace("<", "‹").replace(">", "›")


def excerpt(text: str, terms: list[str]) -> str:
    """≤ 1200 characters around the first occurrence of a word, `<` and `>` neutralized."""
    low = text.lower()
    hits = [i for i in (low.find(t.lower()) for t in terms) if i >= 0]
    start = max(0, min(hits) - EXCERPT_MAX // 3) if hits else 0
    chunk = text[start:start + EXCERPT_MAX]
    return neutralize(("…" if start else "") + chunk)[:EXCERPT_MAX]


def search(db_path: Path, words: list[str], limit: int = RESULT_MAX,
           sources: list[str] | None = None) -> list[dict[str, Any]]:
    """`sources`: keys of SOURCE_NAMES to keep (None or empty = all)."""
    names = [SOURCE_NAMES[k] for k in sources or [] if k in SOURCE_NAMES]
    where = f" AND source IN ({','.join('?' for _ in names)})" if names else ''
    match = fts_query(words)
    if not match or not db_path.is_file():
        return []
    terms = re.findall(r"\w+", " ".join(words), re.U)[:QUERY_WORDS_MAX]
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for expr in (match, match.replace('" "', '" OR "')):  # all the words first, else any of them
            rows = con.execute("SELECT session, source, date, texte FROM souvenirs WHERE souvenirs MATCH ?" + where
                               + " ORDER BY bm25(souvenirs) LIMIT ?", (expr, *names, limit)).fetchall()
            if rows:
                break
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [{"session": sid, "outil": source, "date": date, "extrait": excerpt(text, terms)}
            for sid, source, date, text in rows]
