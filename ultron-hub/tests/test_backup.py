import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ultron_hub.backup import commands, hc, heartbeat
from ultron_hub.backup import run as backup

HC = "https://hc-ping.com/1234-abcd"


class Pinger:
    def __init__(self, error: str | None = None):
        self.calls, self.error = [], error

    def __call__(self, url, suffix="", body=""):
        self.calls.append((url, suffix, body))
        return self.error


class Restic:
    """Fake restic: answers by subcommand, records every call."""

    def __init__(self, **codes):
        self.calls, self.codes = [], {"cat": 0, "init": 0, "backup": 0, "forget": 0, "check": 0, **codes}

    def __call__(self, argv, env, timeout):
        self.calls.append((argv, env))
        sub = argv[5]
        code = self.codes[sub]
        stdout = ""
        if sub == "backup" and code in (0, 3):
            stdout = "\n".join([json.dumps({"message_type": "status", "percent_done": 0.5}),
                                json.dumps({"message_type": "summary", "snapshot_id": "abc123", "files_new": 4,
                                            "files_changed": 1, "data_added": 2048,
                                            "total_bytes_processed": 9999})])
        return subprocess.CompletedProcess(argv, code, stdout, f"{sub} stderr")

    def subs(self):
        return [argv[5] for argv, _ in self.calls]


def make_db(path: Path, value: str = "hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (value,))
    con.commit()
    con.close()


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home"
    for exe in (".local/bin/restic", ".local/bin/rclone"):
        (home / exe).parent.mkdir(parents=True, exist_ok=True)
        (home / exe).write_text("#!/bin/sh\n")
    (home / ".config/ultron-hub").mkdir(parents=True)
    (home / ".config/ultron-hub/restic.pass").write_text("pw\n")
    (home / "notes").mkdir()
    make_db(home / ".hermes/state.db")
    make_db(tmp_path / "var/ultron.db", "todo")
    return backup.Layout(home, tmp_path / "var", tmp_path / "var/ultron.db")


# -- healthchecks ----------------------------------------------------------------------------

def test_load_urls_keeps_valid_known_keys(tmp_path):
    env = tmp_path / "hc.env"
    env.write_text(f"# comment\nHC_VIVANT_URL='{HC}/'\nHC_SAUVEGARDE_URL=http://insecure/x\n"
                   f"OTHER={HC}\nHC_VIVANT_URL_X={HC}\n")
    assert hc.load_urls(env) == {"HC_VIVANT_URL": HC}
    assert hc.load_urls(tmp_path / "absent.env") == {}


def test_ping_never_raises():
    assert hc.ping("https://127.0.0.1:9/x", timeout=1) is not None
    assert hc.ping(HC, "bogus") == "suffixe inconnu : bogus"


def test_env_path_ignores_home_override(monkeypatch, tmp_path):
    monkeypatch.undo()  # the real real_home (only a path is computed, nothing is read)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not str(hc.env_path()).startswith(str(tmp_path))


# -- heartbeat -------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def pouls_ok(monkeypatch):
    """The heartbeat tests never read the real var/health: the pulse is fine unless a test says otherwise."""
    monkeypatch.setattr(heartbeat, "pouls_problem", lambda: None)


def test_heartbeat_not_configured_is_ok():
    pinger = Pinger()
    assert heartbeat.beat({}, pinger)["configured"] is False
    assert pinger.calls == []


def test_heartbeat_pings_ok_when_pulse_fresh():
    pinger = Pinger()
    result = heartbeat.beat({"HC_VIVANT_URL": HC}, pinger)
    assert result == {"configured": True, "pinged": "ok", "ping_error": None, "pouls": None}
    assert pinger.calls == [(HC, "", "")]


def test_heartbeat_ignores_hermes_gateway():
    # todo #460: hermes-gateway is disabled since 18/10; it must never make a beat fail (nor be asked about)
    assert "gateway" not in heartbeat.beat({"HC_VIVANT_URL": HC}, Pinger())


def test_heartbeat_pings_fail_when_pulse_stale():
    pinger = Pinger(error="URLError: down")
    result = heartbeat.beat({"HC_VIVANT_URL": HC}, pinger, pouls=lambda: "veilleur : 3 h")
    assert result["pinged"] == "fail" and result["pouls"] == "veilleur : 3 h" and result["ping_error"] == "URLError: down"
    assert pinger.calls == [(HC, "fail", "veilleur : 3 h")]


def test_heartbeat_unreadable_pulse_is_a_failure():
    def boom():
        raise OSError("x")
    pinger = Pinger()
    assert heartbeat.beat({"HC_VIVANT_URL": HC}, pinger, pouls=boom)["pinged"] == "fail"


def test_pouls_problem_uses_threshold_and_history(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from ultron_hub.health import collect as hcollect
    monkeypatch.undo()
    hist = tmp_path / "history.jsonl"
    monkeypatch.setattr(hcollect, "history_path", lambda root=None: hist)
    monkeypatch.setattr(heartbeat.seuils, "config_path", lambda root=None: tmp_path / "absent.yaml")
    assert "aucune collecte" in heartbeat.pouls_problem()
    for hours, ok in ((1.5, True), (2.5, False)):
        at = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        hist.write_text(json.dumps({"at": "2020-01-01T00:00:00+00:00"}) + "\n" + json.dumps({"at": at}) + "\n")
        assert (heartbeat.pouls_problem() is None) is ok
    cfg = tmp_path / "sante.yaml"
    cfg.write_text("pouls:\n  collecte_max_h: 4\n")
    monkeypatch.setattr(heartbeat.seuils, "config_path", lambda root=None: cfg)
    assert heartbeat.pouls_problem() is None


def test_seuils_fall_back_on_bad_config(tmp_path):
    from ultron_hub.health import seuils
    bad = tmp_path / "s.yaml"
    for text in ("pouls: [1]", "pouls:\n  collecte_max_h: -3\n", ":::", "pouls:\n  collecte_max_h: true\n"):
        bad.write_text(text)
        assert seuils.charger(bad)["pouls"]["collecte_max_h"] == 2
    assert seuils.charger(tmp_path / "none.yaml")["pouls"]["collecte_max_h"] == 2


def test_real_sante_yaml_is_valid():
    from ultron_hub.health import seuils
    assert seuils.charger()["pouls"]["collecte_max_h"] == 2


def test_heartbeat_command_summary(monkeypatch):
    monkeypatch.setattr(heartbeat, "beat", lambda: {"configured": False, "pinged": None, "ping_error": None})
    out = commands.handle_heartbeat(None)
    assert out["status"] == "ok" and "non configuré" in out["summary"]


# -- backup ----------------------------------------------------------------------------------

def test_excludes_cover_secrets_and_live_databases(layout):
    excludes = layout.excludes()
    home = layout.home
    for secret in (".ssh", ".sudo_pass", ".hermes/.env", ".claude/.credentials.json", ".config/gh"):
        assert str(home / secret) in excludes
    assert "auth.json" in excludes and str(layout.password_file) in excludes
    for suffix in ("", "-wal", "-shm"):
        assert str(home / ".hermes/state.db") + suffix in excludes
    assert str(home / ".hermes/tools") in excludes and str(home / ".omniroute/call_logs") in excludes
    assert layout.staging in layout.sources()


def test_stage_databases_copies_consistently(layout):
    report = backup.stage_databases(layout)
    assert report["hermes/state.db"] == "ok" and report["ultron-hub/ultron.db"] == "ok"
    assert report["hermes/kanban.db"] == "absente"
    con = sqlite3.connect(layout.staging / "ultron-hub/ultron.db")
    assert con.execute("SELECT v FROM t").fetchone() == ("todo",)
    con.close()


def test_run_backup_success(layout):
    restic, pinger = Restic(), Pinger()
    result = backup.run_backup(layout, restic, pinger, {"HC_SAUVEGARDE_URL": HC})
    assert result["status"] == "ok" and result["snapshot_id"] == "abc123" and result["data_added"] == 2048
    assert restic.subs() == ["cat", "backup", "forget"]
    argv, env = restic.calls[1]
    assert env["RESTIC_REPOSITORY"] == "rclone:gdrive:ultron-backup"
    assert env["RESTIC_PASSWORD_FILE"] == str(layout.password_file)
    assert f"rclone.program={layout.rclone}" in argv
    assert str(layout.home / "notes") in argv and str(layout.home / ".claude") not in argv  # absent sources
    forget = restic.calls[2][0]
    assert forget[-7:] == ["--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6", "--prune"]
    assert [c[:2] for c in pinger.calls] == [(HC, "start"), (HC, "")]
    assert not layout.staging.exists()
    info = backup.status(layout)
    assert info["configured"] and info["last_run"]["status"] == "ok" and info["last_success_age_h"] == 0


def test_run_backup_initialises_missing_repository(layout):
    restic = Restic(cat=10)
    assert backup.run_backup(layout, restic, Pinger(), {})["status"] == "ok"
    assert restic.subs() == ["cat", "init", "backup", "forget"]


def test_run_backup_unreadable_repository_fails_and_pings(layout):
    restic, pinger = Restic(cat=1), Pinger()
    result = backup.run_backup(layout, restic, pinger, {"HC_SAUVEGARDE_URL": HC})
    assert result["status"] == "error" and "dépôt illisible" in result["error"]
    assert restic.subs() == ["cat"] and pinger.calls[-1][1] == "fail"
    assert backup.status(layout)["last_success_at"] is None


def test_run_backup_failure_keeps_previous_success(layout):
    assert backup.run_backup(layout, Restic(), Pinger(), {})["status"] == "ok"
    first = backup.status(layout)["last_success_at"]
    pinger = Pinger()
    result = backup.run_backup(layout, Restic(backup=1), pinger, {"HC_SAUVEGARDE_URL": HC})
    assert result["status"] == "error" and "backup stderr" in result["error"]
    assert pinger.calls[-1][1] == "fail" and json.loads(pinger.calls[-1][2])["status"] == "error"
    info = backup.status(layout)
    assert info["last_success_at"] == first and info["last_run"]["status"] == "error"


def test_run_backup_partial_counts_as_snapshot(layout):
    result = backup.run_backup(layout, Restic(backup=3), Pinger(), {})
    assert result["status"] == "partial" and result["warnings"]
    assert backup.status(layout)["last_success_at"] == result["at"]


def test_run_backup_forget_failure_is_an_error(layout):
    result = backup.run_backup(layout, Restic(forget=1), Pinger(), {})
    assert result["status"] == "error" and "forget" in result["error"]
    assert backup.status(layout)["last_success_at"] == result["at"]  # the snapshot exists


def test_run_backup_not_configured(layout):
    layout.password_file.unlink()
    restic = Restic()
    result = backup.run_backup(layout, restic, Pinger(), {})
    assert result["status"] == "error" and "restic.pass" in result["error"] and restic.calls == []


def test_run_backup_refuses_concurrent_run(layout):
    import fcntl
    layout.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(layout.lock_file, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = backup.run_backup(layout, Restic(), Pinger(), {})
    assert result == {"status": "error", "error": "une sauvegarde est déjà en cours"}


def test_run_backup_unexpected_exception_is_recorded(layout):
    def boom(argv, env, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    pinger = Pinger()
    result = backup.run_backup(layout, boom, pinger, {"HC_SAUVEGARDE_URL": HC})
    assert result["status"] == "error" and "TimeoutExpired" in result["error"]
    assert pinger.calls[-1][1] == "fail"


def test_run_check(layout):
    pinger = Pinger()
    assert backup.run_check(layout, Restic(), pinger, {"HC_SAUVEGARDE_URL": HC})["status"] == "ok"
    assert pinger.calls == []
    result = backup.run_check(layout, Restic(check=1), pinger, {"HC_SAUVEGARDE_URL": HC})
    assert result["status"] == "error" and pinger.calls[0][1] == "fail"
    assert backup.status(layout)["last_check"]["status"] == "error"


def test_status_age(layout):
    past = datetime.now(timezone.utc) - timedelta(hours=30)
    backup.write_state(layout, {"last_success_at": past.isoformat(timespec="seconds")})
    assert 29.9 <= backup.status(layout)["last_success_age_h"] <= 30.1


def test_parse_summary_ignores_noise():
    assert backup.parse_summary("garbage\n{\"message_type\": \"status\"}") == {}


def test_cli_backup_status_and_heartbeat(project, monkeypatch, tmp_path, capsys):
    from ultron_hub import cli
    monkeypatch.setattr(hc, "real_home", lambda: tmp_path / "nohome")
    assert cli.main(["heartbeat"]) == 0
    assert "non configuré" in json.loads(capsys.readouterr().out)["summary"]
    assert cli.main(["backup", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "backup.status" and out["data"]["configured"] is False
    assert "jamais" in out["summary"]


def test_main_tree_follows_worktree_link(tmp_path):
    main = tmp_path / "proj"
    (main / ".git/worktrees/todo-46").mkdir(parents=True)
    wt = tmp_path / "proj.worktrees/todo-46"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/todo-46\n")
    assert backup.main_tree(wt) == main
    assert backup.main_tree(main) == main
    (wt / ".git").write_text("gitdir: /elsewhere/modules/x\n")
    assert backup.main_tree(wt) == wt


def test_cli_backup_from_worktree_uses_live_tree(tmp_path, monkeypatch):
    main = tmp_path / "proj"
    (main / ".git/worktrees/wt").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    seen = {}
    monkeypatch.setattr(backup, "status", lambda layout: seen.setdefault("layout", layout) and {
        "configured": True, "last_success_age_h": None})
    commands.handle_backup(type("A", (), {"backup_command": "status"})(), load_settings_for(wt))
    assert seen["layout"].databases["ultron-hub/ultron.db"] == main / "var/ultron.db"
    assert seen["layout"].state_file == main / "var/backup/state.json"


def load_settings_for(root):
    from ultron_hub.common.settings import load_settings
    return load_settings(root)


# -- restore test (todo #162) ----------------------------------------------------------------

class SnapshotRestic(Restic):
    """Fake restic that remembers the staging files at `backup` time and re-creates them on `restore`."""

    def __init__(self, layout, corrupt=None, **codes):
        super().__init__(**codes)
        self.layout, self.snapshot, self.corrupt = layout, {}, corrupt

    def __call__(self, argv, env, timeout):
        sub = argv[5]
        if sub == "backup":
            self.snapshot = {str(p): p.read_bytes() for p in self.layout.staging.rglob("*") if p.is_file()}
        if sub == "restore":
            self.calls.append((argv, env))
            target = Path(argv[argv.index("--target") + 1])
            wanted = [argv[i + 1] for i, a in enumerate(argv) if a == "--include"]
            for path in wanted:
                data = self.snapshot.get(path)
                if data is None:
                    continue
                if self.corrupt and path.endswith(self.corrupt):
                    data = data[: len(data) // 2] if self.corrupt.endswith(".db") else b"autre"
                out = target / path.lstrip("/")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
            self.target = target
            return subprocess.CompletedProcess(argv, self.codes.get("restore", 0), "", "restore stderr")
        return super().__call__(argv, env, timeout)


def backed_up(layout, **kw):
    restic = SnapshotRestic(layout, **kw)
    result = backup.run_backup(layout, restic, Pinger(), {})
    return restic, result


def test_backup_writes_sentinel_and_keeps_its_sha(layout):
    restic, result = backed_up(layout)
    sentinel = [d for p, d in restic.snapshot.items() if p.endswith("sentinelle.txt")]
    assert len(sentinel) == 1 and b"sentinelle de sauvegarde" in sentinel[0]
    import hashlib
    assert result["sentinelle_sha256"] == hashlib.sha256(sentinel[0]).hexdigest()
    assert backup.read_state(layout)["last_run"]["sentinelle_sha256"] == result["sentinelle_sha256"]


def test_restore_test_ok_and_folder_deleted(layout):
    restic, _ = backed_up(layout)
    res = backup.run_restore_test(layout, restic)
    assert res["status"] == "ok" and res["database"] == "ultron-hub/ultron.db"
    assert not restic.target.exists()
    assert backup.read_state(layout)["last_restore_test"]["status"] == "ok"
    assert backup.status(layout)["last_restore_test"]["status"] == "ok"


def test_restore_test_detects_wrong_fingerprint_and_still_cleans(layout):
    restic, _ = backed_up(layout, corrupt="sentinelle.txt")
    res = backup.run_restore_test(layout, restic)
    assert res["status"] == "error" and "sha256" in res["error"]
    assert not restic.target.exists()


def test_restore_test_detects_corrupt_database(layout):
    restic, _ = backed_up(layout, corrupt="ultron.db")
    res = backup.run_restore_test(layout, restic)
    assert res["status"] == "error" and "ultron.db" in res["error"]


def test_restore_test_restic_failure_and_no_backup_yet(layout):
    restic, _ = backed_up(layout, restore=1)
    assert "code 1" in backup.run_restore_test(layout, restic)["error"]
    other = SnapshotRestic(layout)
    layout.state_file.unlink()
    assert "aucune sauvegarde" in backup.run_restore_test(layout, other)["error"]
    assert other.calls == []



# -- todo #456 : lighter and slower nightly backup -----------------------------------------

def test_excludes_cover_junk_directories(layout):
    excludes, home = layout.excludes(), layout.home
    hub = home / "workspace/projects/active/ultron-hub"
    for pattern in ("venv", ".venv", str(hub / "var/missions/*/work"), str(hub / "var/worktrees"),
                    str(hub / "var/captures"), str(home / "notes/montage-video/references"),
                    str(home / ".claude/projects/-tmp-*")):
        assert pattern in excludes


def test_restic_slows_rclone_and_keeps_program(layout):
    argv = backup.restic_argv(layout, "backup")
    assert argv[1:3] == ["-o", f"rclone.program={layout.rclone}"]
    assert argv[3:5] == ["-o", f"rclone.args={backup.RCLONE_ARGS}"]
    assert "--tpslimit 4" in backup.RCLONE_ARGS and "--drive-pacer-min-sleep 200ms" in backup.RCLONE_ARGS
    assert backup.RCLONE_ARGS.startswith("serve restic --stdio --b2-hard-delete")


class FlakyRestic(Restic):
    def __init__(self, stderr, first=1, **codes):
        super().__init__(**codes)
        self.stderr, self.first, self.tries = stderr, first, 0

    def __call__(self, argv, env, timeout):
        if argv[5] == "backup":
            self.tries += 1
            if self.tries == 1:
                self.calls.append((argv, env))
                return subprocess.CompletedProcess(argv, self.first, "", self.stderr)
        return super().__call__(argv, env, timeout)


def test_backup_retries_once_after_transient_error(layout):
    waits = []
    restic = FlakyRestic("googleapi: Error 403: rateLimitExceeded")
    result = backup.run_backup(layout, restic, Pinger(), {}, retry_delay_s=600, sleep=waits.append)
    assert result["status"] == "ok" and result["retried"] is True and waits == [600]
    assert restic.subs() == ["cat", "backup", "backup", "forget"]


def test_backup_retry_is_single_and_only_for_transient_errors(layout):
    waits = []
    always = Restic(backup=1)  # stderr "backup stderr": not transient
    assert backup.run_backup(layout, always, Pinger(), {}, sleep=waits.append)["status"] == "error"
    assert waits == [] and always.subs().count("backup") == 1

    class Down(Restic):
        def __call__(self, argv, env, timeout):
            self.calls.append((argv, env))
            if argv[5] == "backup":
                return subprocess.CompletedProcess(argv, 1, "", "500 Internal Server Error")
            return subprocess.CompletedProcess(argv, 0, "", "")
    down = Down()
    result = backup.run_backup(layout, down, Pinger(), {}, retry_delay_s=1, sleep=waits.append)
    assert result["status"] == "error" and waits == [1] and down.subs().count("backup") == 2


def test_big_data_added_sets_warning_visible_in_status(layout, monkeypatch):
    assert "warning" not in backup.run_backup(layout, Restic(), Pinger(), {})
    assert backup.status(layout)["warning"] is None
    monkeypatch.setattr(backup, "DATA_ADDED_WARN_BYTES", 1024)
    result = backup.run_backup(layout, Restic(), Pinger(), {})
    assert result["status"] == "ok" and "Mo ajoutés" in result["warning"]
    assert backup.status(layout)["warning"] == result["warning"]
