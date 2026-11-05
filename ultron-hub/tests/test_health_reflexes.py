"""Todo #161: reflexes of the watcher (known breakdown → diagnosis → signed button). No network, no real restic,
no real Telegram: restic answers come from a fake, the token from the throwaway close key (conftest)."""

import asyncio
import fcntl
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cli_contract import check_argv

from hermes_plugin import close_key
from hermes_plugin.health_flow import HealthFlow, cb
from ultron_hub import cli, user_actions
from ultron_hub.backup import run as backup_run
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import load_settings
from ultron_hub.health import alertes, reflexes, seuils
from ultron_hub.todo import closing

REPO = Path(__file__).resolve().parents[1]
spec = user_actions._spec()
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
CFG = seuils.charger(REPO / "config" / "sante.yaml")
LOCKED = ("Fatal: unable to create lock in backend: repository is already locked by PID 4242 on serveur-test "
          "by ultron (UID 1000, GID 1000)")
ID1, ID2 = "a" * 64, "b" * 64
HOST = socket.gethostname()


def lock(age_h: float, host: str = HOST, pid: int = 4242) -> dict:
    at = (datetime.now(timezone.utc) - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%S.123456789+00:00")  # restic's
    return {"time": at, "exclusive": False, "hostname": host, "username": "ultron", "pid": pid, "uid": 1000}


class FakeRestic:
    """ultron-user's _restic_run: answers `list locks`, `cat lock <id>` and `unlock` from a dict of locks."""

    def __init__(self, locks: dict[str, dict]):
        self.locks, self.calls = dict(locks), []

    def __call__(self, args, log):
        self.calls.append(list(args))
        if args[-2:] == ["list", "locks"]:
            return 0, "".join(f"{i}\n" for i in self.locks)
        if args[-3:-1] == ["cat", "lock"]:
            return 0, json.dumps(self.locks[args[-1]])
        if args == ["unlock"]:
            self.locks = {}
            return 0, "successfully removed 1 locks\n"
        raise AssertionError(f"unexpected restic call: {args}")

    @property
    def unlocked(self) -> bool:
        return ["unlock"] in self.calls


@pytest.fixture
def tool(tmp_path, monkeypatch):
    """ultron-user pointed at a throwaway backup state, its PIDs all gone unless a test says otherwise."""
    state = tmp_path / "var" / "backup" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"last_run": {"status": "error", "error": "restic forget a échoué (code 11) : " + LOCKED}}))
    monkeypatch.setattr(spec, "BACKUP_STATE", state)
    monkeypatch.setattr(spec, "BACKUP_RUN_LOCK", tmp_path / "var" / "backup" / "run.lock")
    monkeypatch.setattr(spec, "_pid_gone", lambda pid: True)
    return state


def use(monkeypatch, locks):
    fake = FakeRestic(locks)
    monkeypatch.setattr(spec, "_restic_run", fake)
    return fake


# --- ultron-user: the boundary ---------------------------------------------------------------------------

def test_stale_lock_found(tool, monkeypatch):
    use(monkeypatch, {ID1: lock(20)})
    found = spec.inspect_locks(6, [])
    assert found["ok"] and found["fingerprint"] == spec.lock_fingerprint([ID1]) and found["locks"][0]["age_h"] >= 19


@pytest.mark.parametrize("locks,why", [
    ({ID1: lock(2)}, "récent"),
    ({ID1: lock(20), ID2: lock(1)}, "récent"),  # one recent lock is enough to refuse: a backup may be running
    ({ID1: lock(20, host="autre-machine")}, "autre machine"),
    ({}, "aucun verrou"),
])
def test_lock_conditions_refused(tool, monkeypatch, locks, why):
    use(monkeypatch, locks)
    found = spec.inspect_locks(6, [])
    assert not found["ok"] and why in found["reason"]


def test_living_pid_refused(tool, monkeypatch):
    monkeypatch.setattr(spec, "_pid_gone", lambda pid: False)
    use(monkeypatch, {ID1: lock(20)})
    assert "existe encore" in spec.inspect_locks(6, [])["reason"]


def test_pid_gone_is_real():
    import os
    assert spec._pid_gone(os.getpid()) is False
    assert spec._pid_gone(int(Path("/proc/sys/kernel/pid_max").read_text())) is True  # never a valid PID
    assert spec._pid_gone(0) is False and spec._pid_gone(None) is False and spec._pid_gone(True) is False


def test_unlock_runs_plain_unlock_when_everything_holds(tool, monkeypatch):
    fake = use(monkeypatch, {ID1: lock(20)})
    log: list[str] = []
    assert spec._restic_unlock_stale(["6", spec.lock_fingerprint([ID1])], log) is None
    assert fake.unlocked and not any("--remove-all" in a for c in fake.calls for a in c)
    assert "verrous restants : 0" in log[-1]


@pytest.mark.parametrize("change,expected", [
    ("recent", "récent"),
    ("pid", "existe encore"),
    ("fingerprint", "conditions changées"),
    ("motif", "ne signale plus"),
    ("running", "sauvegarde tourne"),
])
def test_conditions_changed_before_the_press(tool, monkeypatch, tmp_path, change, expected):
    fingerprint = spec.lock_fingerprint([ID1])
    fake = use(monkeypatch, {ID1: lock(20)})
    if change == "recent":
        fake.locks = {ID1: lock(1)}
    elif change == "pid":
        monkeypatch.setattr(spec, "_pid_gone", lambda pid: False)
    elif change == "fingerprint":
        fake.locks = {ID2: lock(20)}
    elif change == "motif":
        tool.write_text(json.dumps({"last_run": {"status": "ok"}}))
    held = None
    if change == "running":
        spec.BACKUP_RUN_LOCK.write_text("")
        held = open(spec.BACKUP_RUN_LOCK)
        fcntl.flock(held, fcntl.LOCK_EX)
    try:
        error = spec._restic_unlock_stale(["6", fingerprint], [])
    finally:
        if held:
            held.close()
    assert error and expected in error and "rien fait" in error and not fake.unlocked


@pytest.mark.parametrize("args", [["5", "0" * 12], ["721", "0" * 12], ["6"], ["6", "XYZ"], ["6", "0" * 13],
                                  ["six", "0" * 12], ["6", "0" * 12, "--remove-all"], ["-6", "0" * 12]])
def test_unlock_arguments_refused(args):
    with pytest.raises(spec.Refused):
        spec.describe("restic_unlock_stale", args)


def test_unlock_is_listed_and_described_without_secrets():
    assert "restic_unlock_stale" in spec.ACTIONS and "restic_unlock_stale" in user_actions.describe_actions()
    lines = spec.describe("restic_unlock_stale", ["6", "0" * 12])
    assert any(line.endswith("jamais --remove-all), puis recompte les verrous") for line in lines)
    assert not any(s in line for line in lines for s in ("restic.pass", ".env", "auth.json", "sudo"))


def test_tool_uses_the_backup_repository(tmp_path):
    layout = backup_run.Layout(spec.HOME, tmp_path, None)
    assert spec.RESTIC_REPOSITORY == backup_run.REPOSITORY
    assert (spec.RESTIC, spec.RCLONE, spec.RESTIC_PASSWORD_FILE) == (layout.restic, layout.rclone, layout.password_file)
    assert spec.LOCKED_MOTIF == reflexes.DEFAUTS[0]["motif"]
    assert spec.LOCK_MIN_H_FLOOR == reflexes.VERROU_MIN_H


# --- the table and the diagnosis -------------------------------------------------------------------------

def test_table_in_config_is_the_default():
    assert reflexes.charger(REPO / "config" / "sante.yaml") == reflexes.DEFAUTS


def test_table_validation(tmp_path):
    cfg = tmp_path / "s.yaml"
    assert reflexes.charger(tmp_path / "absent.yaml") == reflexes.DEFAUTS
    cfg.write_text("reflexes: []\n")
    assert reflexes.charger(cfg) == []
    cfg.write_text("reflexes:\n"
                   "  - {nom: a, motif: x, diagnostic: d, action: z, verification: rm_rf}\n"   # unknown check
                   "  - {nom: 'B C', motif: x, diagnostic: d, action: z}\n"                     # bad name
                   "  - {nom: c, motif: x, diagnostic: d, action: z, verification: verrou_restic, verrou_min_h: 1}\n")
    assert [(r["nom"], r["verrou_min_h"]) for r in reflexes.charger(cfg)] == [("c", 6)]  # never under the floor


def metrics(error: str | None = LOCKED, where: str = "run") -> dict:
    backup = {"configured": True, "last_success_age_h": 40.0, "last_run_status": "ok", "last_run_error": None,
              "last_check_at": NOW.isoformat(), "last_check_status": "ok", "last_check_error": None}
    if error:
        backup[f"last_{where}_status"], backup[f"last_{where}_error"] = "error", error
    return {"services": {"failed_system": [], "failed_user": []}, "disks": [], "backup": backup, "errors": {}}


def ok_inspect(seen=None):
    def inspect(min_h):
        if seen is not None:
            seen.append(min_h)
        return {"ok": True, "reason": "1 verrou(s) de plus de 6 h, processus absent(s)", "fingerprint": "0123456789ab"}
    return inspect


def test_reflex_found_with_its_button():
    seen: list[int] = []
    diags = reflexes.diagnostiquer(metrics(), reflexes.DEFAUTS, ok_inspect(seen))
    assert seen == [6] and len(diags) == 1
    assert diags[0]["diagnostic"] == "Verrou de sauvegarde périmé (machine coupée pendant la sauvegarde)"
    assert diags[0]["bouton"] == {"label": "🔓 Retirer le verrou", "cible": "verrou-restic:0123456789ab",
                                  "action": "restic_unlock_stale", "args": ["6", "0123456789ab"]}
    assert reflexes.diagnostiquer(metrics(where="check"), reflexes.DEFAUTS, ok_inspect())  # backup check too


def test_no_pattern_no_inspection():
    def never(min_h):
        raise AssertionError("restic inspected without the pattern")
    assert reflexes.diagnostiquer(metrics(None), reflexes.DEFAUTS, never) == []
    assert reflexes.diagnostiquer(metrics("disk full"), reflexes.DEFAUTS, never) == []


@pytest.mark.parametrize("reason", ["verrou abcd récent (2.0 h, seuil 6 h)", "verrou abcd : le processus 4242 existe encore"])
def test_recent_lock_or_living_pid_gives_no_diagnosis(reason):
    inspect = lambda h: {"ok": False, "reason": reason, "fingerprint": None}  # noqa: E731
    assert reflexes.diagnostiquer(metrics(), reflexes.DEFAUTS, inspect) == []


def test_default_inspection_is_ultron_users_own(monkeypatch, tool):
    assert reflexes.inspecter_verrous(6)["ok"] is False  # conftest: any real restic call fails, caught here
    use(monkeypatch, {ID1: lock(20)})
    assert reflexes.inspecter_verrous(6)["fingerprint"] == spec.lock_fingerprint([ID1])
    monkeypatch.setattr(spec, "_restic_run", lambda a, log: (_ for _ in ()).throw(TimeoutError()))
    assert reflexes.inspecter_verrous(6)["ok"] is False


# --- the alert message (#160) ----------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path):
    return load_settings(tmp_path)


def test_diagnosis_is_added_to_the_alert(settings):
    sent: list[str] = []
    diagnose = lambda m: reflexes.diagnostiquer(m, reflexes.DEFAUTS, ok_inspect())  # noqa: E731
    res = alertes.run_alertes(settings, metrics=metrics(), cfg=CFG, now=NOW, send=sent.append, diagnose=diagnose)
    assert res["sent"] and "reflexe:verrou-restic" in res["actives"]
    assert "la dernière sauvegarde est en erreur" in sent[0]
    assert "🩺 diagnostic : Verrou de sauvegarde périmé (machine coupée pendant la sauvegarde)" in sent[0]
    assert "🔓 Retirer le verrou" in sent[0]
    assert reflexes.lire(settings.root)[0]["bouton"]["cible"] == "verrou-restic:0123456789ab"
    # same state: silent, as every alert
    alertes.run_alertes(settings, metrics=metrics(), cfg=CFG, now=NOW, send=sent.append, diagnose=diagnose)
    assert len(sent) == 1


def test_failing_reflex_never_stops_the_alert(settings):
    def broken(m):
        raise RuntimeError("boom")
    sent: list[str] = []
    res = alertes.run_alertes(settings, metrics=metrics(), cfg=CFG, now=NOW, send=sent.append, diagnose=broken)
    assert res["sent"] and "diagnostic" not in sent[0] and reflexes.lire(settings.root) == []


# --- the signed button -----------------------------------------------------------------------------------

TARGET = "verrou-restic:0123456789ab"


@pytest.fixture
def diagnosed(settings):
    reflexes.ecrire(settings.root, reflexes.diagnostiquer(metrics(), reflexes.DEFAUTS, ok_inspect()))
    return settings.root


class FakeTool:
    def __init__(self, result=None):
        self.calls, self.result = [], result or {"status": "ok", "output": "verrous restants : 0"}

    def __call__(self, action, args):
        self.calls.append((action, args))
        return self.result


def signed(target=TARGET, action="reflexe"):
    return close_key.signed_env(action, target)


def test_signed_press_runs_the_tool(diagnosed, monkeypatch):
    monkeypatch.setenv(closing.TOKEN_ENV, signed()[closing.TOKEN_ENV])
    tool_run = FakeTool()
    res = reflexes.agir(diagnosed, TARGET, run=tool_run)
    assert res["ok"] and res["by"]["by"] == "telegram-button"
    assert tool_run.calls == [("restic_unlock_stale", ["6", "0123456789ab"])]
    assert reflexes.lire(diagnosed) == []  # done: the button goes away


def test_replayed_token_refused(diagnosed, monkeypatch):
    monkeypatch.setenv(closing.TOKEN_ENV, signed()[closing.TOKEN_ENV])
    tool_run = FakeTool({"status": "error", "error": "conditions plus réunies : aucun verrou dans le dépôt, rien fait"})
    reflexes.agir(diagnosed, TARGET, run=tool_run)
    with pytest.raises(closing.CloseRefused, match="déjà utilisé"):
        reflexes.agir(diagnosed, TARGET, run=tool_run)
    assert len(tool_run.calls) == 1


@pytest.mark.parametrize("env", [None, "other-action", "other-target"])
def test_unsigned_or_wrongly_signed_press_refused(diagnosed, monkeypatch, env):
    if env == "other-action":
        monkeypatch.setenv(closing.TOKEN_ENV, signed(action="user")[closing.TOKEN_ENV])
    elif env == "other-target":
        monkeypatch.setenv(closing.TOKEN_ENV, signed(target="verrou-restic:ffffffffffff")[closing.TOKEN_ENV])
    tool_run = FakeTool()
    with pytest.raises(closing.CloseRefused):
        reflexes.agir(diagnosed, TARGET, run=tool_run)
    assert tool_run.calls == []


def test_conditions_changed_tool_refuses_and_nothing_is_cleared(diagnosed, monkeypatch):
    monkeypatch.setenv(closing.TOKEN_ENV, signed()[closing.TOKEN_ENV])
    tool_run = FakeTool({"status": "error", "error": "conditions plus réunies : verrou aaaa récent (1.0 h, seuil 6 h), rien fait"})
    res = reflexes.agir(diagnosed, TARGET, run=tool_run)
    assert not res["ok"] and "récent" in res["message"] and res["message"].startswith("Rien fait")
    assert reflexes.lire(diagnosed)  # still diagnosed: the next alert run decides


def test_stale_diagnosis_does_not_call_the_tool(settings, monkeypatch):
    monkeypatch.setenv(closing.TOKEN_ENV, signed()[closing.TOKEN_ENV])
    tool_run = FakeTool()
    res = reflexes.agir(settings.root, TARGET, run=tool_run)
    assert not res["ok"] and "plus d'actualité" in res["message"] and tool_run.calls == []
    with pytest.raises(InvalidRequestError):
        reflexes.agir(settings.root, "verrou-restic:../../x", run=tool_run)


def test_cli_reflexes_and_signed_press(diagnosed, monkeypatch, capsys):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(diagnosed))
    assert cli.main(["health", "reflexes"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["diagnostics"][0]["bouton"]["cible"] == TARGET
    calls = []
    monkeypatch.setattr(user_actions, "run", lambda a, args: calls.append((a, args)) or {"status": "ok"})
    cli.main(["health", "reflexe-agir", TARGET])  # no token: refused
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and out["summary"].startswith("Refusé") and calls == []
    monkeypatch.setenv(closing.TOKEN_ENV, signed()[closing.TOKEN_ENV])
    cli.main(["health", "reflexe-agir", TARGET])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and calls == [("restic_unlock_stale", ["6", "0123456789ab"])]


# --- /sante on Telegram ----------------------------------------------------------------------------------

class Client:
    def __init__(self, diags):
        self.diags, self.calls = diags, []

    async def call(self, *args, timeout_s=None, env=None):
        check_argv(args)
        self.calls.append((args, env))
        if args[:2] == ("health", "guard"):
            return {"status": "ok", "data": {"ok": True, "problem": None}}
        if args[:2] == ("health", "reflexes"):
            return {"status": "ok", "data": {"diagnostics": self.diags}}
        if args[:2] == ("health", "reflexe-agir"):
            return {"status": "ok", "summary": "✅ 🔓 Retirer le verrou : fait."}
        raise AssertionError(args)


def test_sante_shows_the_button_then_card_then_signed_call(diagnosed):
    flow = HealthFlow(Client(reflexes.lire(diagnosed)), signer=lambda a, t: {"TOKEN": f"{a}|{t}"})
    menu = asyncio.run(flow.on_command(1))
    assert "Verrou de sauvegarde périmé" in menu.text
    assert ("🔓 Retirer le verrou", cb(f"rx:{TARGET}")) in [b for row in menu.buttons for b in row]
    assert all(len(data.encode()) <= 64 for row in menu.buttons for _, data in row)
    card = asyncio.run(flow.on_callback(1, cb(f"rx:{TARGET}")))
    assert card.text.startswith("À valider") and "plus de 6 h" in card.text
    assert card.buttons[0][0][1] == cb(f"rxok:{TARGET}")
    done = asyncio.run(flow.on_callback(1, cb(f"rxok:{TARGET}")))
    args, env = flow.client.calls[-1]
    assert args == ("health", "reflexe-agir", TARGET) and env == {"TOKEN": f"reflexe|{TARGET}"}
    assert "fait" in done.text


def test_sante_without_diagnosis_has_no_button_and_old_card_is_refused():
    flow = HealthFlow(Client([]))
    menu = asyncio.run(flow.on_command(1))
    assert not any(d.startswith(cb("rx")) for row in menu.buttons for _, d in row)
    assert "plus d'actualité" in asyncio.run(flow.on_callback(1, cb(f"rx:{TARGET}"))).text
    assert asyncio.run(flow.on_callback(1, cb("rxok:../x"))).text == "Action inconnue."


# -- todo #354: tmux server living inside an ultron service -------------------------------------------------------------

def _proc(tmp_path, procs):
    for pid, (comm, cmdline, cgroup) in procs.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_text(cmdline.replace(" ", "\0"))
        (d / "cgroup").write_text(cgroup + "\n")
    (tmp_path / "self").mkdir()  # non-numeric entries are ignored
    return tmp_path


def test_tmux_in_a_service_cgroup_alerts(tmp_path):
    proc = _proc(tmp_path, {10: ("bash", "bash", "0::/user.slice/x.scope"),
                            11: ("tmux: server", "tmux new-session -d", "0::/user.slice/app.slice/ultron-hub-dashboard.service")})
    res = reflexes.inspecter_tmux(proc)
    assert res["ok"] and res["in_service"] and "11" in res["reason"]
    diags = reflexes.diagnostiquer({}, inspect_dashboard=lambda: {"ok": True, "stale": False}, inspect_tmux=lambda: res)
    assert [d["nom"] for d in diags] == ["tmux-hors-service"] and diags[0]["bouton"] is None
    assert "une fusion les fermera" in diags[0]["diagnostic"] and diags[0]["action"] == "fermer et rouvrir les sessions ultron"


def test_tmux_in_its_own_scope_does_not_alert(tmp_path):
    proc = _proc(tmp_path, {11: ("tmux: server", "tmux", "0::/user.slice/app.slice/ultron-tmux-x.scope")})
    res = reflexes.inspecter_tmux(proc)
    assert res["ok"] and not res["in_service"]
    ok = lambda: {"ok": True, "stale": False}  # noqa: E731
    assert reflexes.diagnostiquer({}, inspect_dashboard=ok, inspect_tmux=lambda: res) == []


def test_no_tmux_at_all_does_not_alert(tmp_path):
    proc = _proc(tmp_path, {10: ("bash", "bash", "0::/system.slice/ultron-hub-dashboard.service")})
    assert reflexes.inspecter_tmux(proc) == {"ok": True, "in_service": False, "reason": "serveur tmux hors service ou absent"}
    assert reflexes.inspecter_tmux(tmp_path / "absent")["ok"] is False


def test_tmux_control_is_configured():
    assert any(r["nom"] == "tmux-hors-service" and r["verification"] == "tmux_hors_service" for r in reflexes.charger())
    assert any(r["nom"] == "tmux-hors-service" for r in reflexes.DEFAUTS)
