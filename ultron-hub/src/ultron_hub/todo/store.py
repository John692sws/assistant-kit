"""SQLite storage for todo tasks.

The database is the single source of truth: humans and agents go through
`ultron-hub todo ...`, never edit the file directly. Every change is written
in a transaction and recorded in the `events` table.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import InvalidRequestError, UltronHubError

PRIORITIES = ("high", "normal", "low")
STATUSES = ("todo", "done", "cancelled")
TITLE_MAX = 200
NATURES = ("chantier", "fond")
CLAIR_MAX = 140
CHANTIER_MAX = 40
SCHEMA_VERSION = 3

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND {TITLE_MAX}),
    notes       TEXT NOT NULL DEFAULT '',
    priority    TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN {PRIORITIES}),
    category    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'todo' CHECK (status IN {STATUSES}),
    due_date    TEXT,
    source      TEXT NOT NULL DEFAULT 'cli',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    done_at     TEXT,
    nature      TEXT NOT NULL DEFAULT 'chantier' CHECK (nature IN {NATURES}),
    clair       TEXT NOT NULL DEFAULT '' CHECK (length(clair) <= {CLAIR_MAX}),
    chantier    TEXT CHECK (chantier IS NULL OR length(chantier) BETWEEN 1 AND {CHANTIER_MAX})
);
CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks (status, due_date);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER NOT NULL REFERENCES tasks (id),
    at       TEXT NOT NULL,
    action   TEXT NOT NULL,
    details  TEXT NOT NULL DEFAULT '{{}}'
);
"""

# v1 -> v2 (todo #52): same definitions as in _SCHEMA; existing rows become "chantier".
_V2_COLUMNS = {
    "nature": f"TEXT NOT NULL DEFAULT 'chantier' CHECK (nature IN {NATURES})",
    "clair": f"TEXT NOT NULL DEFAULT '' CHECK (length(clair) <= {CLAIR_MAX})",
}

# v2 -> v3 (todo #103): the subject (chantier) of the task; existing rows stay NULL (= "Hors chantier").
_V3_COLUMNS = {
    "chantier": f"TEXT CHECK (chantier IS NULL OR length(chantier) BETWEEN 1 AND {CHANTIER_MAX})",
}

_EDITABLE = ("title", "notes", "priority", "category", "due_date", "clair")


class TaskNotFound(UltronHubError):
    """No task with this id."""


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    notes: str
    priority: str
    category: str
    status: str
    due_date: str | None
    source: str
    created_at: str
    updated_at: str
    done_at: str | None
    nature: str = "chantier"
    clair: str = ""
    chantier: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_title(title: Any) -> str:
    if not isinstance(title, str) or not title.strip():
        raise InvalidRequestError("title must be a non-empty string")
    title = " ".join(title.split())
    if len(title) > TITLE_MAX:
        raise InvalidRequestError(f"title is longer than {TITLE_MAX} characters")
    return title


def validate_clair(clair: Any) -> str:
    """The one-sentence "En clair" text: empty (not written yet) or one line of at most CLAIR_MAX characters."""
    if not isinstance(clair, str):
        raise InvalidRequestError("clair must be a string")
    clair = clair.strip()
    if "\n" in clair or "\r" in clair:
        raise InvalidRequestError("clair must be a single line")
    if len(clair) > CLAIR_MAX:
        raise InvalidRequestError(f"clair is longer than {CLAIR_MAX} characters")
    return clair


def validate_nature(nature: Any) -> str:
    if nature not in NATURES:
        raise InvalidRequestError(f"nature must be one of {', '.join(NATURES)}")
    return nature


def validate_chantier(chantier: Any) -> str | None:
    """None (Hors chantier) or a subject name of 1 to CHANTIER_MAX characters. Whether it exists is the service's job."""
    if chantier is None:
        return None
    if not isinstance(chantier, str) or not 1 <= len(chantier) <= CHANTIER_MAX or chantier != chantier.strip():
        raise InvalidRequestError(f"chantier must be a name of 1 to {CHANTIER_MAX} characters")
    return chantier


class TodoStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise UltronHubError(f"{self.db_path} has schema v{version}, this code knows v{SCHEMA_VERSION}")
        if version < 1:
            # executescript commits on its own; the schema is idempotent (IF NOT EXISTS).
            self._conn.executescript(_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
        else:
            if version < 2:
                self._migrate_v1_to_v2()
            if version < 3:
                self._migrate_v2_to_v3()

    def _migrate_v1_to_v2(self) -> None:
        # Safety copy first, never overwriting an earlier one (it holds the true v1 state).
        backup = self.db_path.with_name(self.db_path.name + ".bak-v1")
        if not backup.exists():
            dest = sqlite3.connect(backup)
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        with self._tx() as conn:
            # Another process may have migrated while we waited for the write lock.
            if conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
                return
            have = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            for name, definition in _V2_COLUMNS.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            conn.execute("PRAGMA user_version = 2")

    def _migrate_v2_to_v3(self) -> None:
        # Same recipe: safety copy first (never overwritten), then the column, then the version, under the write lock.
        backup = self.db_path.with_name(self.db_path.name + ".bak-v2")
        if not backup.exists():
            dest = sqlite3.connect(backup)
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        with self._tx() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] >= 3:
                return
            have = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            for name, definition in _V3_COLUMNS.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TodoStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    class _Tx:
        def __init__(self, conn: sqlite3.Connection):
            self.conn = conn

        def __enter__(self):
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, *_):
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")

    def _tx(self) -> "_Tx":
        return self._Tx(self._conn)

    def _event(self, conn: sqlite3.Connection, task_id: int, action: str, details: dict) -> None:
        conn.execute("INSERT INTO events (task_id, at, action, details) VALUES (?, ?, ?, ?)",
                     (task_id, _now(), action, json.dumps(details, ensure_ascii=False)))

    def _row(self, task_id: int) -> Task:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(f"task #{task_id} not found")
        return Task(**dict(row))

    def add(self, title: str, category: str, priority: str = "normal", due_date: str | None = None,
            notes: str = "", source: str = "cli", nature: str = "fond", clair: str = "",
            chantier: str | None = None) -> Task:
        title = validate_title(title)
        validate_nature(nature)
        chantier = validate_chantier(chantier)
        clair = validate_clair(clair)
        if priority not in PRIORITIES:
            raise InvalidRequestError(f"priority must be one of {', '.join(PRIORITIES)}")
        now = _now()
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (title, notes, priority, category, due_date, source, created_at, updated_at,"
                " nature, clair, chantier) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, notes, priority, category, due_date, source, now, now, nature, clair, chantier))
            task_id = cur.lastrowid
            self._event(conn, task_id, "add", {"title": title, "source": source, "nature": nature,
                                                  **({"chantier": chantier} if chantier else {})})
        return self._row(task_id)

    def get(self, task_id: int) -> Task:
        return self._row(task_id)

    def list(self, status: str | None = "todo", category: str | None = None, limit: int = 50,
             nature: str | None = None, chantier: str | None = None, hors_chantier: bool = False) -> list[Task]:
        if status is not None and status not in STATUSES:
            raise InvalidRequestError(f"status must be one of {', '.join(STATUSES)} or all")
        if nature is not None:
            validate_nature(nature)
        clauses, args = [], []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if category:
            clauses.append("category = ?")
            args.append(category)
        if nature:
            clauses.append("nature = ?")
            args.append(nature)
        if hors_chantier:
            clauses.append("chantier IS NULL")
        elif chantier:
            clauses.append("chantier = ?")
            args.append(validate_chantier(chantier))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # Overdue/soonest first, then priority, then oldest.
        sql = (f"SELECT * FROM tasks {where} ORDER BY due_date IS NULL, due_date, "
               "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id LIMIT ?")
        rows = self._conn.execute(sql, (*args, max(1, int(limit)))).fetchall()
        return [Task(**dict(r)) for r in rows]

    def set_status(self, task_id: int, status: str, by: dict[str, Any] | None = None) -> Task:
        """`by` (who asked: {"by": actor, ...}) is merged into the event details."""
        if status not in STATUSES:
            raise InvalidRequestError(f"status must be one of {', '.join(STATUSES)}")
        before = self._row(task_id)
        now = _now()
        with self._tx() as conn:
            conn.execute("UPDATE tasks SET status = ?, updated_at = ?, done_at = ? WHERE id = ?",
                         (status, now, now if status == "done" else None, task_id))
            self._event(conn, task_id, "status", {"from": before.status, "to": status, **(by or {})})
        return self._row(task_id)

    def update(self, task_id: int, by: str | None = None, **fields: Any) -> Task:
        """Edit fields. A nature or chantier change is its own event (with `by`: who asked), not an `edit`.

        `chantier=None` takes the task out of its chantier.
        """
        unknown = set(fields) - set(_EDITABLE) - {"nature", "chantier"}
        if unknown:
            raise InvalidRequestError(f"cannot edit: {', '.join(sorted(unknown))}")
        if "title" in fields:
            fields["title"] = validate_title(fields["title"])
        if "priority" in fields and fields["priority"] not in PRIORITIES:
            raise InvalidRequestError(f"priority must be one of {', '.join(PRIORITIES)}")
        if "clair" in fields:
            fields["clair"] = validate_clair(fields["clair"])
        if "nature" in fields:
            validate_nature(fields["nature"])
        if "chantier" in fields:
            validate_chantier(fields["chantier"])
        before = self._row(task_id)
        nature = fields.pop("nature", before.nature)
        chantier = fields.pop("chantier", before.chantier)
        if not fields and nature == before.nature and chantier == before.chantier:
            return before
        sets = ", ".join(f"{k} = ?" for k in (*fields, "nature", "chantier"))
        with self._tx() as conn:
            conn.execute(f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?",
                         (*fields.values(), nature, chantier, _now(), task_id))
            if fields:
                self._event(conn, task_id, "edit",
                            {k: {"from": getattr(before, k), "to": v} for k, v in fields.items()})
            if nature != before.nature:
                self._event(conn, task_id, "nature", {"from": before.nature, "to": nature, "by": by or "cli"})
            if chantier != before.chantier:
                self._event(conn, task_id, "chantier", {"from": before.chantier, "to": chantier, "by": by or "cli"})
        return self._row(task_id)

    def mark_delivered(self, task_id: int, by: str, note: str = "") -> Task:
        """Todo #153: an unsigned `livree` event on an open todo. It closes nothing and leaves `updated_at` alone."""
        task = self._row(task_id)
        if task.status != "todo":
            raise InvalidRequestError(f"task #{task_id} is not open ({task.status})")
        with self._tx() as conn:
            self._event(conn, task_id, "livree", {"by": by, "note": note})
        return task

    def mark_merge_reverted(self, task_id: int, details: dict) -> None:
        """Todo #254: an automatic merge of this todo was reverted (↩️). Like any later event, it ends `livree`."""
        self._row(task_id)
        with self._tx() as conn:
            self._event(conn, task_id, "fusion-annulee", details)

    def proof_used(self, task_id: int, proof: str) -> bool:
        """True if a status change of this task already spent this button token."""
        rows = self._conn.execute("SELECT details FROM events WHERE task_id = ? AND action = 'status'",
                                  (task_id,)).fetchall()
        return any(json.loads(r["details"]).get("proof") == proof for r in rows)

    def history(self, task_id: int) -> list[dict[str, Any]]:
        self._row(task_id)
        rows = self._conn.execute("SELECT at, action, details FROM events WHERE task_id = ? ORDER BY id",
                                  (task_id,)).fetchall()
        return [{"at": r["at"], "action": r["action"], "details": json.loads(r["details"])} for r in rows]


_V2_TASKS_SQL = f"""
CREATE TABLE tasks_v2 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND {TITLE_MAX}),
    notes       TEXT NOT NULL DEFAULT '',
    priority    TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN {PRIORITIES}),
    category    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'todo' CHECK (status IN {STATUSES}),
    due_date    TEXT,
    source      TEXT NOT NULL DEFAULT 'cli',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    done_at     TEXT,
    nature      TEXT NOT NULL DEFAULT 'chantier' CHECK (nature IN {NATURES}),
    clair       TEXT NOT NULL DEFAULT '' CHECK (length(clair) <= {CLAIR_MAX})
)"""
_V2_COLUMN_NAMES = "id, title, notes, priority, category, status, due_date, source, created_at, updated_at, done_at, nature, clair"


def downgrade_to_v2(db_path: Path, force_copy: bool = False) -> dict[str, Any]:
    """Bring a v3 database back to v2 so that the v2 code can open it again (`todo schema-retour --vers 2`).

    Copies the database to `<db>.bak-v3` first (an earlier copy is kept: the next free `.bak-v3.N` is used), then drops
    the `chantier` column (ALTER TABLE ... DROP COLUMN, SQLite 3.35+; otherwise, or with `force_copy`, the table is
    rebuilt and its rows copied) and sets `user_version = 2`. The chantier of each task is lost; the backup keeps it.
    Does nothing on a database that is already v2.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise UltronHubError(f"{db_path} does not exist")
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=OFF")  # the rebuild drops the table that events point to
        result: dict[str, Any] = {"sqlite_version": sqlite3.sqlite_version, "backup": None}
        # Deferred transaction holding a read lock: backup() spins forever inside a write transaction of the same
        # connection. The first write below upgrades the lock (a concurrent writer gives a clear error).
        conn.execute("BEGIN")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            if version == 2:
                conn.execute("ROLLBACK")
                return {**result, "changed": False, "from": 2, "to": 2, "method": None}
            if version != 3:
                raise UltronHubError(f"{db_path} has schema v{version}, schema-retour only goes from v3 to v2")
            backup = db_path.with_name(db_path.name + ".bak-v3")
            n = 0
            while backup.exists():
                n += 1
                backup = db_path.with_name(f"{db_path.name}.bak-v3.{n}")
            dest = sqlite3.connect(backup)
            try:
                conn.backup(dest)  # under our read lock: the copy is the exact v3 state
            finally:
                dest.close()
            result["backup"] = str(backup)
            if sqlite3.sqlite_version_info >= (3, 35, 0) and not force_copy:
                method = "drop-column"
                conn.execute("ALTER TABLE tasks DROP COLUMN chantier")
            else:
                method = "table-copy"
                row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'tasks'").fetchone()
                conn.execute(_V2_TASKS_SQL)
                conn.execute(f"INSERT INTO tasks_v2 ({_V2_COLUMN_NAMES}) SELECT {_V2_COLUMN_NAMES} FROM tasks")
                conn.execute("DROP TABLE tasks")
                conn.execute("ALTER TABLE tasks_v2 RENAME TO tasks")
                conn.execute("CREATE INDEX idx_tasks_open ON tasks (status, due_date)")
                if row is not None:  # keep the counter: an id is never reused
                    conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'tasks'", (row[0],))
            conn.execute("PRAGMA user_version = 2")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return {**result, "changed": True, "from": 3, "to": 2, "method": method}
    finally:
        conn.close()
