"""Nightly encrypted backup to Google Drive: restic over rclone (spec: ~/notes/chantier-2-spec.md).

- SQLite databases are copied with the sqlite3 backup API into a staging dir first (never a hot file copy);
  their live files are excluded.
- Secrets the agents may not read are excluded on purpose (re-created by logging in again).
- The state of the last run lives in var/backup/state.json and is read by `backup status` and /sante.
- Every run pings HC_SAUVEGARDE_URL: /start, then success or /fail with the reason.
"""

import fcntl
import hashlib
import json
import secrets
import tempfile
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ultron_hub.backup import hc

REPOSITORY = "rclone:gdrive:ultron-backup"
KEEP = ("--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6")
CHECK_SUBSET = "5%"
SENTINEL_NAME = "sentinelle.txt"  # written in the staging dir by every backup (todo #162)
RESTIC_TIMEOUT_S = 4 * 3600
SHORT_TIMEOUT_S = 300
EXIT_NO_REPOSITORY = 10  # restic >= 0.17: repository does not exist
EXIT_PARTIAL = 3  # snapshot created, but some source files could not be read
# restic's default is "serve restic --stdio --b2-hard-delete"; the throttle keeps Google Drive from answering
# rateLimitExceeded / 500 on the big nightly upload (todo #456)
RCLONE_ARGS = "serve restic --stdio --b2-hard-delete --tpslimit 4 --drive-pacer-min-sleep 200ms"
RETRY_DELAY_S = 600  # one deferred retry of `restic backup` after a transient error
TRANSIENT_MARKERS = ("ratelimitexceeded", "500 internal server error", "internal server error", "503",
                     "connection reset", "connection refused", "timeout", "timed out", "temporary failure",
                     "no such host", "network is unreachable", "unexpected eof", "tls handshake")
DATA_ADDED_WARN_BYTES = 300 * 1024 * 1024  # a night adding more than this is suspicious (junk not excluded?)

Runner = Callable[..., subprocess.CompletedProcess]


def default_run(argv: list[str], env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout,
                          stdin=subprocess.DEVNULL, check=False)


class Layout:
    """Every path the backup touches, derived from one home directory (tests use a fake one)."""

    def __init__(self, home: Path, var_dir: Path, todo_db: Path | None):
        self.home = home
        self.var_dir = var_dir
        self.restic = home / ".local" / "bin" / "restic"
        self.rclone = home / ".local" / "bin" / "rclone"
        self.password_file = home / ".config" / "ultron-hub" / "restic.pass"
        self.staging = home / ".cache" / "ultron-hub" / "backup-staging"
        self.state_file = var_dir / "backup" / "state.json"
        self.lock_file = var_dir / "backup" / "run.lock"
        hermes, omniroute = home / ".hermes", home / ".omniroute"
        # staged name -> live database
        self.databases = {
            "hermes/state.db": hermes / "state.db",
            "hermes/kanban.db": hermes / "kanban.db",
            "hermes/shared-state.db": hermes / "shared-state.db",
            "omniroute/storage.sqlite": omniroute / "storage.sqlite",
        }
        if todo_db:
            self.databases["ultron-hub/ultron.db"] = todo_db

    def sources(self) -> list[Path]:
        h = self.home
        # ~/ultron = shared mind (esprit/, archives included) and knowledge notebook (savoir/), todo #63
        return [h / ".hermes", h / ".claude", h / "notes", h / "workspace", h / ".omniroute", h / "ultron",
                h / ".config" / "systemd" / "user", h / ".config" / "ultron-hub", self.staging]

    def excludes(self) -> list[str]:
        h = self.home
        reinstallable = [h / ".hermes" / d for d in
                         ("tools", "installs", "hermes-agent", "cache", "lsp", "audio_cache", "image_cache")]
        caches = [h / ".claude" / d for d in ("cache", "paste-cache", "shell-snapshots", "telemetry", "session-env")]
        secrets = [h / ".ssh", h / ".sudo_pass", h / ".hermes" / ".env", h / ".claude" / ".credentials.json",
                   h / ".config" / "gh", self.password_file]  # the password inside its own repository is useless
        patterns = [str(p) for p in reinstallable + caches + secrets]
        hub = h / "workspace" / "projects" / "active" / "ultron-hub"
        patterns += ["auth.json", ".venv", "venv", "node_modules", "*.worktrees",
                     str(h / ".omniroute" / "call_logs"), str(h / ".omniroute" / "*.bak-*"),
                     str(hub / "var" / "missions" / "*" / "work"), str(hub / "var" / "worktrees"),
                     str(hub / "var" / "captures"), str(h / "notes" / "montage-video" / "references"),
                     str(h / ".claude" / "projects" / "-tmp-*")]
        for live in self.databases.values():
            patterns += [str(live) + suffix for suffix in ("", "-wal", "-shm", "-journal")]
        return patterns


def restic_env(layout: Layout) -> dict[str, str]:
    return {"HOME": str(layout.home), "PATH": f"{layout.rclone.parent}:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8", "RESTIC_REPOSITORY": REPOSITORY,
            "RESTIC_PASSWORD_FILE": str(layout.password_file)}


def restic_argv(layout: Layout, *args: str) -> list[str]:
    return [str(layout.restic), "-o", f"rclone.program={layout.rclone}", "-o", f"rclone.args={RCLONE_ARGS}", *args]


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text[-limit:] if text else ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- state -----------------------------------------------------------------------

def read_state(layout: Layout) -> dict[str, Any]:
    try:
        state = json.loads(layout.state_file.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(layout: Layout, state: dict[str, Any]) -> None:
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = layout.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(layout.state_file)


def status(layout: Layout, now: datetime | None = None) -> dict[str, Any]:
    """Read-only digest for `backup status` and /sante (no restic call)."""
    now = now or _now()
    state = read_state(layout)
    age_h = None
    if state.get("last_success_at"):
        try:
            age_h = round((now - datetime.fromisoformat(state["last_success_at"])).total_seconds() / 3600, 1)
        except ValueError:
            age_h = None
    return {
        "configured": layout.password_file.exists() and layout.restic.exists() and layout.rclone.exists(),
        "last_success_at": state.get("last_success_at"),
        "last_success_age_h": age_h,
        "last_run": state.get("last_run"),
        "warning": (state.get("last_run") or {}).get("warning"),
        "last_check": state.get("last_check"),
        "last_restore_test": state.get("last_restore_test"),
    }


# -- staging ----------------------------------------------------------------------

def stage_databases(layout: Layout) -> dict[str, str]:
    """Consistent copies of the live databases; returns {name: "ok" | "absente" | error}."""
    if layout.staging.exists():
        shutil.rmtree(layout.staging)
    layout.staging.mkdir(parents=True, mode=0o700)
    report = {}
    for name, live in layout.databases.items():
        if not live.exists():
            report[name] = "absente"
            continue
        target = layout.staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            src = sqlite3.connect(f"file:{live}?mode=ro", uri=True, timeout=30)
            try:
                dst = sqlite3.connect(target)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            target.chmod(0o600)
            report[name] = "ok"
        except sqlite3.Error as exc:
            report[name] = f"{type(exc).__name__}: {exc}"
    return report


def write_sentinel(layout: Layout, now: datetime | None = None) -> str:
    """Random text + date in the staging dir; returns its sha256 (kept in last_run for `restore-test`)."""
    text = f"sentinelle de sauvegarde {(now or _now()).isoformat(timespec='seconds')} {secrets.token_hex(16)}\n"
    path = layout.staging / SENTINEL_NAME
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clear_staging(layout: Layout) -> None:
    shutil.rmtree(layout.staging, ignore_errors=True)


def parse_summary(stdout: str) -> dict[str, Any]:
    """Last `summary` message of `restic backup --json`."""
    for line in reversed((stdout or "").splitlines()):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("message_type") == "summary":
            keys = ("snapshot_id", "files_new", "files_changed", "data_added", "total_bytes_processed")
            return {k: message.get(k) for k in keys}
    return {}


# -- commands ---------------------------------------------------------------------

def _missing(layout: Layout) -> list[str]:
    return [str(p) for p in (layout.restic, layout.rclone, layout.password_file) if not p.exists()]


def ensure_repository(layout: Layout, run: Runner) -> str | None:
    """None when the repository exists (created if needed), else an error text."""
    env = restic_env(layout)
    probe = run(restic_argv(layout, "cat", "config"), env, SHORT_TIMEOUT_S)
    if probe.returncode == 0:
        return None
    if probe.returncode != EXIT_NO_REPOSITORY:
        return f"dépôt illisible (code {probe.returncode}) : {_tail(probe.stderr)}"
    init = run(restic_argv(layout, "init"), env, SHORT_TIMEOUT_S)
    return None if init.returncode == 0 else f"init impossible (code {init.returncode}) : {_tail(init.stderr)}"


def is_transient(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in TRANSIENT_MARKERS)


def run_backup(layout: Layout, run: Runner = default_run, pinger: Callable[..., str | None] = hc.ping,
               urls: dict[str, str] | None = None, retry_delay_s: float = RETRY_DELAY_S,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    urls = hc.load_urls() if urls is None else urls
    hc_url = urls.get("HC_SAUVEGARDE_URL")
    layout.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(layout.lock_file, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "error", "error": "une sauvegarde est déjà en cours"}
        started = time.monotonic()
        if hc_url:
            pinger(hc_url, "start")
        try:
            result = _backup(layout, run, retry_delay_s, sleep)
        except Exception as exc:  # recorded and pinged like any other failure
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            clear_staging(layout)
        result["at"] = _now().isoformat(timespec="seconds")
        result["duration_s"] = round(time.monotonic() - started)

        state = read_state(layout)
        state["last_run"] = result
        if result.get("snapshot_id"):
            state["last_success_at"] = result["at"]
        write_state(layout, state)

        if hc_url:
            body = json.dumps(result, ensure_ascii=False)
            result["ping_error"] = pinger(hc_url, "fail" if result["status"] == "error" else "", body)
        return result


def _backup(layout: Layout, run: Runner, retry_delay_s: float = RETRY_DELAY_S,
            sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    missing = _missing(layout)
    if missing:
        return {"status": "error", "error": "non configuré, absent : " + ", ".join(missing)}
    error = ensure_repository(layout, run)
    if error:
        return {"status": "error", "error": error}

    databases = stage_databases(layout)
    sentinel_sha = write_sentinel(layout)
    sources = [str(p) for p in layout.sources() if p.exists()]
    argv = restic_argv(layout, "backup", "--json", "--tag", "ultron", *sources)
    for pattern in layout.excludes():
        argv += ["--exclude", pattern]
    env = restic_env(layout)
    done = run(argv, env, RESTIC_TIMEOUT_S)
    summary = parse_summary(done.stdout)
    retried = False
    if done.returncode not in (0, EXIT_PARTIAL) and is_transient(done.stderr):
        sleep(retry_delay_s)  # one deferred retry; restic resumes from the packs already uploaded
        retried = True
        done = run(argv, env, RESTIC_TIMEOUT_S)
        summary = parse_summary(done.stdout)
    result: dict[str, Any] = {"databases": databases, "sentinelle_sha256": sentinel_sha, **summary}
    if retried:
        result["retried"] = True
    added = summary.get("data_added")
    if isinstance(added, (int, float)) and added > DATA_ADDED_WARN_BYTES:
        result["warning"] = (f"{added / 1024 / 1024:.0f} Mo ajoutés (seuil {DATA_ADDED_WARN_BYTES // 1024 // 1024} Mo) : "
                             "des fichiers inutiles sont peut-être sauvegardés, voir Layout.excludes")
    if done.returncode not in (0, EXIT_PARTIAL) or not summary.get("snapshot_id"):
        return {**result, "status": "error",
                "error": f"restic backup a échoué (code {done.returncode}) : {_tail(done.stderr)}"}

    problems = [f"{name} : {state}" for name, state in databases.items() if state not in ("ok", "absente")]
    if done.returncode == EXIT_PARTIAL:
        problems.append(f"fichiers illisibles : {_tail(done.stderr, 300)}")
    forget = run(restic_argv(layout, "forget", "--tag", "ultron", *KEEP, "--prune"), env, RESTIC_TIMEOUT_S)
    if forget.returncode != 0:
        return {**result, "status": "error",
                "error": f"snapshot créé, mais forget/prune a échoué (code {forget.returncode}) : "
                         f"{_tail(forget.stderr)}"}
    return {**result, "status": "partial" if problems else "ok", "warnings": problems}


def run_check(layout: Layout, run: Runner = default_run, pinger: Callable[..., str | None] = hc.ping,
              urls: dict[str, str] | None = None) -> dict[str, Any]:
    """Weekly integrity check on a 5 % data subset; a failure pings /fail (a success pings nothing)."""
    urls = hc.load_urls() if urls is None else urls
    missing = _missing(layout)
    if missing:
        result = {"status": "error", "error": "non configuré, absent : " + ", ".join(missing)}
    else:
        done = run(restic_argv(layout, "check", f"--read-data-subset={CHECK_SUBSET}"),
                   restic_env(layout), RESTIC_TIMEOUT_S)
        result = ({"status": "ok"} if done.returncode == 0 else
                  {"status": "error", "error": f"restic check a échoué (code {done.returncode}) : {_tail(done.stderr)}"})
    result["at"] = _now().isoformat(timespec="seconds")
    state = read_state(layout)
    state["last_check"] = result
    write_state(layout, state)
    if result["status"] == "error" and urls.get("HC_SAUVEGARDE_URL"):
        result["ping_error"] = pinger(urls["HC_SAUVEGARDE_URL"], "fail",
                                      "vérification : " + result["error"])
    return result


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_restore_test(layout: Layout, run: Runner = default_run) -> dict[str, Any]:
    """Monthly proof that the backup can be restored (todo #162): sentinel + one SQLite database of the last
    snapshot go to a throwaway folder, the sentinel's sha256 is compared and `PRAGMA integrity_check` is run.
    The folder is always deleted. The result is stored in state.json (`last_restore_test`); a failure is
    turned into an alert by `health alertes`."""
    result = _restore_test(layout, run)
    result["at"] = _now().isoformat(timespec="seconds")
    state = read_state(layout)
    state["last_restore_test"] = result
    write_state(layout, state)
    return result


def _restore_test(layout: Layout, run: Runner) -> dict[str, Any]:
    missing = _missing(layout)
    if missing:
        return {"status": "error", "error": "non configuré, absent : " + ", ".join(missing)}
    last = read_state(layout).get("last_run") or {}
    snapshot, expected = last.get("snapshot_id"), last.get("sentinelle_sha256")
    if not snapshot or not expected:
        return {"status": "error", "error": "aucune sauvegarde récente avec sentinelle dans last_run"}
    db_name = next((n for n in ("ultron-hub/ultron.db", *(last.get("databases") or {}))
                    if (last.get("databases") or {}).get(n) == "ok"), None)
    wanted = [layout.staging / SENTINEL_NAME] + ([layout.staging / db_name] if db_name else [])
    result: dict[str, Any] = {"snapshot_id": snapshot, "database": db_name}
    with tempfile.TemporaryDirectory(prefix="restore-test-") as tmp:
        argv = restic_argv(layout, "restore", str(snapshot), "--target", tmp)
        for path in wanted:
            argv += ["--include", str(path)]
        try:
            done = run(argv, restic_env(layout), RESTIC_TIMEOUT_S)
        except Exception as exc:  # timeout, missing binary...
            return {**result, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
        if done.returncode != 0:
            return {**result, "status": "error",
                    "error": f"restic restore a échoué (code {done.returncode}) : {_tail(done.stderr)}"}
        root = Path(tmp)
        sentinel = root / str(layout.staging / SENTINEL_NAME).lstrip("/")
        if not sentinel.is_file():
            return {**result, "status": "error", "error": "sentinelle absente du snapshot restauré"}
        if _sha256_file(sentinel) != expected:
            return {**result, "status": "error", "error": "empreinte de la sentinelle différente (sha256)"}
        if db_name:
            restored = root / str((layout.staging / db_name)).lstrip("/")
            if not restored.is_file():
                return {**result, "status": "error", "error": f"base {db_name} absente du snapshot restauré"}
            try:
                con = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
                try:
                    verdict = [row[0] for row in con.execute("PRAGMA integrity_check")]
                finally:
                    con.close()
            except sqlite3.Error as exc:
                return {**result, "status": "error", "error": f"base {db_name} illisible : {exc}"}
            if verdict != ["ok"]:
                return {**result, "status": "error",
                        "error": f"integrity_check de {db_name} : {'; '.join(map(str, verdict))[:200]}"}
    return {**result, "status": "ok"}


def main_tree(root: Path) -> Path:
    """The main checkout of `root`: a linked worktree points to it through its `.git` file.

    The backup must stage the live database and write the state /sante reads, even when it is
    started from a worktree (todo-<id>/... branches).
    """
    git = root / ".git"
    if not git.is_file():
        return root
    text = git.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return root
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = (root / gitdir).resolve()
    # <main>/.git/worktrees/<name>
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return root


def default_layout(var_dir: Path, todo_db: Path | None) -> Layout:
    return Layout(hc.real_home(), var_dir, todo_db)
