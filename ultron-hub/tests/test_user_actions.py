"""ultron-user (todo #53): server actions without sudo declared for after 🔀,
run only after the signed "✅ Exécuter" button. Nothing real is ever executed here."""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_branches import branch, commit, no_bwrap, repo  # noqa: F401  (fixtures)

from hermes_plugin import close_key
from ultron_hub import after_merge, branch_commands, branches, cli, user_actions
from ultron_hub.common.errors import InvalidRequestError, UltronHubError
from ultron_hub.todo import closing

spec = user_actions._spec()
CHAT = 1185120603
SIGNED = {"by": "telegram-button", "proof": "0123456789abcdef"}
SECRETS = (".env", "auth.json", "close.key", "restic.pass", ".ssh", ".sudo_pass", "sudo")


@pytest.fixture(autouse=True)
def nothing_runs(monkeypatch):
    """Nothing real runs in this file: neither the tool's commands nor ultron-hub's call to the tool
    (bound as a default argument of the decide functions)."""
    def refuse(*a, **kw):
        raise AssertionError(f"real execution attempted: {a}")
    monkeypatch.setattr(spec, "_run", refuse)
    monkeypatch.setattr(after_merge.decide, "__defaults__", (refuse, None))


# --- ultron-user (the boundary) --------------------------------------------------------------

@pytest.mark.parametrize("action,args,expected", [
    ("user_unit_restart", ["hermes-gateway"],
     ["/usr/bin/systemd-run", "--user", "--collect", "--quiet", "--on-active=5",
      "/usr/bin/systemctl", "--user", "restart", "hermes-gateway.service"]),
    ("user_unit_restart", ["ultron-hub-health.timer"],
     ["/usr/bin/systemctl", "--user", "restart", "--no-block", "ultron-hub-health.timer"]),
    ("user_unit_restart", ["ultron-hub-backup.service"],
     ["/usr/bin/systemctl", "--user", "restart", "--no-block", "ultron-hub-backup.service"]),
    ("hermes_config_set", ["stt.local.model", "small"], [spec.HERMES, "config", "set", "stt.local.model", "small"]),
    ("hermes_config_set", ["display.language", "fr"], [spec.HERMES, "config", "set", "display.language", "fr"]),
])
def test_tool_accepts(action, args, expected):
    assert spec.build_command(action, args) == expected
    assert spec.describe(action, args) == [" ".join(expected)]


@pytest.mark.parametrize("action,args", [
    # hermes_menu_add: commands of our plugin only
    ("hermes_menu_add", []),
    ("hermes_menu_add", ["new"]),  # a Hermes command
    ("hermes_menu_add", ["model"]),
    ("hermes_menu_add", ["todo", "sante"]),
    ("hermes_menu_add", ["todo\n"]),
    ("hermes_menu_add", ["Todo"]),
    ("hermes_menu_add", ["../todo"]),
    # user_unit_enable: timers of deploy/systemd only
    ("user_unit_enable", ["ultron-hub-health.service"]),  # services are started by their timer (but the dashboard)
    ("user_unit_enable", ["ultron-hub-nope.timer"]),  # not in the repository
    ("user_unit_enable", ["hermes-gateway.timer"]),
    ("user_unit_enable", ["omniroute.service"]),
    ("user_unit_enable", ["ultron-hub-health.timer\n"]),
    ("user_unit_enable", ["../ultron-hub-health.timer"]),
    ("user_unit_enable", ["/tmp/ultron-hub-health.timer"]),
    ("user_unit_enable", ["ultron-hub-health.timer", "--global"]),
    ("user_unit_enable", []),
    # user_unit_restart: hermes-gateway or ultron-hub-* of deploy/systemd only
    ("user_unit_restart", ["ssh"]),
    ("user_unit_restart", ["omniroute"]),
    ("user_unit_restart", ["paid-model-watch.service"]),
    ("user_unit_restart", ["hermes-gateway.service"]),
    ("user_unit_restart", ["hermes-gateway;reboot"]),
    ("user_unit_restart", ["hermes-gateway", "ultron-hub-health.timer"]),
    ("user_unit_restart", ["ultron-hub-nope.service"]),
    ("user_unit_restart", ["ultron-hub-health"]),
    ("user_unit_restart", ["--all"]),
    ("user_unit_restart", []),
    # hermes_pip_install: pinned packages of a closed list (empty today)
    ("hermes_pip_install", ["hermes-video==0.1.0"]),
    ("hermes_pip_install", ["requests"]),
    ("hermes_pip_install", ["requests>=2"]),
    ("hermes_pip_install", ["-e", "git+https://example.com/x"]),
    ("hermes_pip_install", ["--index-url=https://evil", "x==1"]),
    ("hermes_pip_install", ["x==1\n"]),
    ("hermes_pip_install", ["/tmp/x.whl"]),
    ("hermes_pip_install", []),
    # hermes_config_set: closed keys, closed values; never approvals, guard, secrets, platforms, models
    ("hermes_config_set", ["approvals.mode", "off"]),
    ("hermes_config_set", ["model.default", "x"]),
    ("hermes_config_set", ["model.provider", "x"]),
    ("hermes_config_set", ["platforms.telegram.extra.command_menu.priority", "[]"]),
    ("hermes_config_set", ["HERMES_YOLO_MODE", "1"]),  # UPPER_SNAKE goes to ~/.hermes/.env
    ("hermes_config_set", ["TELEGRAM_BOT_TOKEN", "x"]),
    ("hermes_config_set", ["dashboard.host", "0.0.0.0"]),
    ("hermes_config_set", ["plugins.enabled", "x"]),
    ("hermes_config_set", ["hooks.ultron-guard", "off"]),
    ("hermes_config_set", ["terminal.backend", "local"]),
    ("hermes_config_set", ["display.language", "de"]),
    ("hermes_config_set", ["display.language", "fr\n"]),
    ("hermes_config_set", ["display.language", "--force"]),
    ("hermes_config_set", ["stt.local.model", "large-v3"]),
    ("hermes_config_set", ["display.language"]),
    ("hermes_config_set", ["display.language", "fr", "--force"]),
    # montage_install: no argument at all (the steps are fixed, in the repository's montage/remotion)
    ("montage_install", ["x"]),
    ("montage_install", ["--prefix=/"]),
    ("montage_install", ["", ""]),
    # veille_lecture: an existing veille and a repository of ~/workspace/projects/active, nothing else
    ("veille_lecture", []),
    ("veille_lecture", ["ultron-hub-main"]),
    ("veille_lecture", ["inconnue-xyz", "ultron-hub"]),
    ("veille_lecture", ["../x", "ultron-hub"]),
    ("veille_lecture", ["ultron-hub-main", "../../.ssh"]),
    ("veille_lecture", ["ultron-hub-main", "/home"]),
    ("veille_lecture", ["ultron-hub-main", "ultron-hub\n"]),
    ("veille_lecture", ["ultron-hub-main", "--sans-lecture"]),
    ("veille_lecture", ["ultron-hub-main", "pas-un-depot-xyz"]),
    ("veille_lecture", ["ultron-hub-main", "ultron-hub", "x"]),
    ("ultron_poser", ["x"]),
    ("ultron_poser", ["--essai"]),
    # claude_hooks_sync: no argument at all (fixed list in ultron-user, todo #175)
    ("claude_hooks_sync", ["PostToolUse"]),
    ("claude_hooks_sync", ["PostToolUse", "/bin/sh -c id"]),
    ("claude_hooks_sync", [""]),
    # anything else
    ("sudo", ["id"]),
    ("shell", ["id"]),
    ("hermes_config_get", ["model"]),
    ("", []),
])
def test_tool_refuses(action, args):
    with pytest.raises(spec.Refused):
        spec.describe(action, args)


def test_pip_needs_exact_pinned_version(monkeypatch, tmp_path):
    python = tmp_path / ".hermes" / "tools" / "python-3.14.7+20260901-linux-x64" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.setattr(spec, "HOME", tmp_path)
    monkeypatch.setattr(spec, "PIP_ALLOWED", {"hermes-video": "1.2.0"})
    assert spec.build_command("hermes_pip_install", ["hermes-video==1.2.0"]) == [
        str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
        "--only-binary=:all:", "hermes-video==1.2.0"]
    for bad in ("hermes-video==1.2.1", "hermes-video==1.2.0.post1", "hermes_video==1.2.0"):
        with pytest.raises(spec.Refused):
            spec.build_command("hermes_pip_install", [bad])
    (tmp_path / ".hermes" / "tools" / "python-3.15.0" / "bin").mkdir(parents=True)
    (tmp_path / ".hermes" / "tools" / "python-3.15.0" / "bin" / "python3").write_text("")
    with pytest.raises(spec.Refused, match="unambiguously"):  # two Pythons: never guess
        spec.build_command("hermes_pip_install", ["hermes-video==1.2.0"])


def test_veille_lecture_builds_fixed_command(monkeypatch, tmp_path):
    (tmp_path / "veilles" / "demo").mkdir(parents=True)
    (tmp_path / "veilles" / "demo" / "veille.json").write_text("{}")
    (tmp_path / "depots" / "projet" / ".git").mkdir(parents=True)
    (tmp_path / "depots" / "lien").symlink_to(tmp_path / "depots" / "projet")
    cli = tmp_path / "repo" / ".venv" / "bin" / "ultron-hub"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    monkeypatch.setattr(spec, "VEILLES_DIR", tmp_path / "veilles")
    monkeypatch.setattr(spec, "REPOS_DIR", tmp_path / "depots")
    monkeypatch.setattr(spec, "REPO", tmp_path / "repo")
    assert spec.describe("veille_lecture", ["demo", "projet"])[0] == (
        f"{cli} veille acces demo --lecture {tmp_path / 'depots' / 'projet'}")
    for bad in (["demo", "lien"], ["demo", "absent"], ["autre", "projet"]):
        with pytest.raises(spec.Refused):
            spec.describe("veille_lecture", bad)


def test_pip_list_is_empty_until_reviewed():
    assert spec.PIP_ALLOWED == {}


def test_config_keys_never_sensitive():
    sensitive = r"approval|guard|secret|token|key|password|auth|platform|model\.|provider|dashboard|plugin|terminal|hook"
    for key, values in spec.CONFIG_KEYS.items():
        assert key == key.lower() and not re.search(sensitive, key), key
        assert values and all(re.fullmatch(r"[a-z0-9]+", v) for v in values), key


def test_menu_procedure_describes_exactly():
    lines = spec.describe("hermes_menu_add", ["atoi"])
    assert lines[0] == f"{spec.HERMES} config get --json platforms.telegram.extra.command_menu"
    assert any("priority <liste actuelle + atoi>" in line for line in lines)


def test_unit_enable_procedure_describes_exactly():
    lines = spec.describe("user_unit_enable", ["ultron-hub-health.timer"])
    assert lines[-1] == "/usr/bin/systemctl --user enable --now ultron-hub-health.timer"
    assert lines[0].startswith(f"/usr/bin/systemctl --user link {spec.UNITS_DIR}/ultron-hub-health.service")


def test_unit_enable_accepts_only_the_dashboard_service():
    # todo #130: the single permanent service; every other .service stays refused (a timer starts it)
    lines = spec.describe("user_unit_enable", ["ultron-hub-dashboard.service"])
    assert lines[-1] == "/usr/bin/systemctl --user enable --now ultron-hub-dashboard.service"
    assert [line for line in lines if " link " in line] == [
        f"/usr/bin/systemctl --user link {spec.UNITS_DIR}/ultron-hub-dashboard.service (si pas déjà lié)"]
    for p in spec.UNITS_DIR.glob("*.service"):
        if p.name != "ultron-hub-dashboard.service":
            with pytest.raises(spec.Refused):
                spec.build_command("user_unit_enable", [p.name])
    for name in ("ultron-hub-dashboard.timer", "ultron-hub-dashboard.service.d", "ultron-hub-dashboard2.service",
                 "../ultron-hub-dashboard.service", "ultron-hub-dashboard.service\n"):
        with pytest.raises(spec.Refused):
            spec.build_command("user_unit_enable", [name])


def test_plugin_commands_match_the_plugin():
    source = (Path(user_actions.repo_tool()).parents[2] / "hermes_plugin" / "__init__.py").read_text()
    registered = set(re.findall(r'register_command\(\s*"([a-z_]+)"', source))
    # "start" is a Hermes built-in that the plugin answers itself (CommandHandler "start"): allowed in the menu (todo #151)
    assert registered | {"start"} == set(spec.PLUGIN_COMMANDS)
    assert 'CommandHandler("start"' in source


def spec_source():
    return user_actions.repo_tool().read_text()


def test_tool_is_standalone_and_never_uses_a_shell():
    source = spec_source()
    assert source.startswith("#!/usr/bin/python3 -I")
    assert "ultron_hub" not in source and "shell=True" not in source and "os.system" not in source
    assert "os.environ" not in source  # the caller's environment is never used


def every_argv():
    yield spec.build_command("user_unit_restart", ["hermes-gateway"])
    for unit in sorted(p.name for p in spec.UNITS_DIR.iterdir() if spec.UNIT_RE.fullmatch(p.name)):
        yield spec.build_command("user_unit_restart", [unit])
    for key, values in spec.CONFIG_KEYS.items():
        for value in values:
            yield spec.build_command("hermes_config_set", [key, value])


def test_gateway_restart_goes_to_ultron_bot_in_direct_mode(tmp_path, monkeypatch):
    # todo #197 : var/envoi-mode = direct -> ultron-bot (qui charge hermes_plugin) à la place de hermes-gateway
    monkeypatch.setattr(spec, "ENVOI_MODE_FILE", tmp_path / "envoi-mode")
    assert spec.build_command("user_unit_restart", ["hermes-gateway"])[-1] == "hermes-gateway.service"
    (tmp_path / "envoi-mode").write_text("direct\n")
    argv = spec.build_command("user_unit_restart", ["hermes-gateway"])
    assert argv[-1] == "ultron-bot.service" and "hermes-gateway.service" not in argv
    assert spec.describe("user_unit_restart", ["hermes-gateway"]) == [" ".join(argv)]
    (tmp_path / "envoi-mode").write_text("hermes\n")
    assert spec.build_command("user_unit_restart", ["hermes-gateway"])[-1] == "hermes-gateway.service"


def test_bot_unit_is_out_of_reach():
    # ultron-bot.service (todo #193) has Conflicts=hermes-gateway: restarting it would cut Hermes off Telegram.
    # The switch is its own prepared command (todo #197), never a ultron-user action.
    for action in ("user_unit_restart", "user_unit_enable"):
        with pytest.raises(spec.Refused):
            spec.build_command(action, ["ultron-bot.service"])


def test_no_command_touches_secrets_or_sudo():
    lines = [" ".join(argv) for argv in every_argv()]
    lines += [line for cmd in spec.PLUGIN_COMMANDS for line in spec.describe("hermes_menu_add", [cmd])]
    lines += [line for p in spec.UNITS_DIR.glob("*.timer") for line in spec.describe("user_unit_enable", [p.name])]
    for line in lines:
        assert not any(s in line for s in SECRETS), line
    assert all(argv[0].startswith("/") for argv in every_argv())


class FakeRun:
    """Stands in for ultron-user's _run: (prefix, answer) pairs; answer = (code, output) or a callable."""
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd, log):
        self.calls.append(cmd)
        for prefix, answer in self.answers:
            if cmd[:len(prefix)] == prefix:
                return answer() if callable(answer) else answer
        return 0, ""


def menu_answers(menus):
    menus = iter(menus)
    return [([spec.HERMES, "config", "get"], lambda: (0, json.dumps(next(menus))))]


MENU = {"priority_mode": "replace", "priority": ["todo", "sante", "usage", "escouade", "actifs", "new"],
        "max_commands": 6}


def test_menu_add_appends_and_raises_the_limit(monkeypatch):
    after = {**MENU, "priority": MENU["priority"] + ["atoi"], "max_commands": 7}
    fake = FakeRun(menu_answers([MENU, after]))
    monkeypatch.setattr(spec, "_run", fake)
    assert spec._hermes_menu_add(["atoi"], []) is None
    key = "platforms.telegram.extra.command_menu"
    assert [spec.HERMES, "config", "set", f"{key}.priority", json.dumps(after["priority"])] in fake.calls
    assert [spec.HERMES, "config", "set", f"{key}.max_commands", "7"] in fake.calls


def test_menu_add_is_idempotent_and_keeps_a_larger_limit(monkeypatch):
    fake = FakeRun(menu_answers([{**MENU, "priority": MENU["priority"] + ["atoi"], "max_commands": 7}]))
    monkeypatch.setattr(spec, "_run", fake)
    assert spec._hermes_menu_add(["atoi"], []) is None and not any("set" in c for c in fake.calls)
    roomy = {**MENU, "max_commands": 10}
    fake = FakeRun(menu_answers([roomy, {**roomy, "priority": roomy["priority"] + ["atoi"]}]))
    monkeypatch.setattr(spec, "_run", fake)
    assert spec._hermes_menu_add(["atoi"], []) is None
    assert not any("max_commands" in " ".join(c) for c in fake.calls if "set" in c)


@pytest.mark.parametrize("menu", [
    None,  # not configured: never created by the tool
    {"priority": "todo", "max_commands": 6},
    {"priority": ["todo", "x; rm"], "max_commands": 6},
    {"priority": ["todo"], "max_commands": True},
    {"priority": ["todo"], "max_commands": 999},
])
def test_menu_add_refuses_unexpected_settings(monkeypatch, menu):
    fake = FakeRun(menu_answers([menu]))
    monkeypatch.setattr(spec, "_run", fake)
    with pytest.raises(spec.Refused):
        spec._hermes_menu_add(["atoi"], [])
    assert not any("set" in c for c in fake.calls)


@pytest.mark.parametrize("args", [
    [],  # empty list
    ["new"],  # a Hermes command
    ["start", "todo", "model"],
    ["start", "todo", "plus", "new"],
    ["start", "start"],  # duplicate
    ["start", "todo", "start"],
    ["atoi", "start", "plus", "todo", "sante", "usage", "actifs"],  # more than 6
    ["Start"], ["VIDEO", "plus"],  # capitals
    ["start\n"], ["start video"], ["start;rm"], ["../todo"], ["$(id)"], ["vi-deo"], [""], [" start"],
    ["start", 3], [None],  # not strings
    "start",  # not a list
])
def test_menu_set_refuses(monkeypatch, args):
    fake = FakeRun(menu_answers([MENU]))
    monkeypatch.setattr(spec, "_run", fake)
    with pytest.raises(spec.Refused):
        spec.describe("hermes_menu_set", args)
    with pytest.raises(spec.Refused):
        spec._hermes_menu_set(args, [])
    assert fake.calls == []  # refused before reading or writing anything


def test_menu_set_writes_priority_and_its_size_only(monkeypatch):
    # Hermes fills the menu with its own commands up to max_commands: the limit must be the list's size
    after = {**MENU, "priority": ["start", "todo", "plus"], "max_commands": 3}
    fake = FakeRun(menu_answers([MENU, after]))
    monkeypatch.setattr(spec, "_run", fake)
    assert spec._hermes_menu_set(["start", "todo", "plus"], []) is None
    key = "platforms.telegram.extra.command_menu"
    writes = [c for c in fake.calls if c[:3] == [spec.HERMES, "config", "set"]]
    assert writes == [[spec.HERMES, "config", "set", f"{key}.priority", '["start", "todo", "plus"]'],
                      [spec.HERMES, "config", "set", f"{key}.max_commands", "3"]]
    assert all(c[:3] in ([spec.HERMES, "config", "get"], [spec.HERMES, "config", "set"]) for c in fake.calls)


def test_menu_set_accepts_one_to_six_plugin_commands():
    for args in (["start"], ["atoi", "todo", "plus"], ["todo", "sante", "usage", "atoi", "plus", "start"]):
        assert spec.describe("hermes_menu_set", args)[0].endswith("config get --json platforms.telegram.extra.command_menu")


def test_menu_set_is_idempotent(monkeypatch):
    fake = FakeRun(menu_answers([{**MENU, "priority": ["start", "todo", "plus"], "max_commands": 3}]))
    monkeypatch.setattr(spec, "_run", fake)
    assert spec._hermes_menu_set(["start", "todo", "plus"], []) is None
    assert not any(c[:3] == [spec.HERMES, "config", "set"] for c in fake.calls)


@pytest.mark.parametrize("menu", [
    None,  # not configured: never created by the tool
    {"priority": "todo", "max_commands": 6},
    {"priority": ["todo", "x; rm"], "max_commands": 6},
    {"priority": ["todo"], "max_commands": True},
    {"priority": ["todo"], "max_commands": 999},
])
def test_menu_set_refuses_unexpected_settings(monkeypatch, menu):
    fake = FakeRun(menu_answers([menu]))
    monkeypatch.setattr(spec, "_run", fake)
    with pytest.raises(spec.Refused):
        spec._hermes_menu_set(["start"], [])
    assert not any(c[:3] == [spec.HERMES, "config", "set"] for c in fake.calls)


def test_menu_set_reports_failures(monkeypatch):
    monkeypatch.setattr(spec, "_run", FakeRun(menu_answers([MENU, MENU])))
    assert "not updated" in spec._hermes_menu_set(["start"], [])
    failing = FakeRun([([spec.HERMES, "config", "set"], (1, "boom"))] + menu_answers([MENU]))
    monkeypatch.setattr(spec, "_run", failing)
    assert "failed" in spec._hermes_menu_set(["start"], [])


def test_menu_add_reports_a_write_that_did_not_stick(monkeypatch):
    monkeypatch.setattr(spec, "_run", FakeRun(menu_answers([MENU, MENU])))
    assert "not updated" in spec._hermes_menu_add(["atoi"], [])


def test_unit_enable_links_then_enables(monkeypatch, tmp_path):
    monkeypatch.setattr(spec, "USER_UNIT_DIR", tmp_path)
    (tmp_path / "ultron-hub-health.service").symlink_to(spec.UNITS_DIR / "ultron-hub-health.service")
    fake = FakeRun([])
    monkeypatch.setattr(spec, "_run", fake)
    assert spec._user_unit_enable(["ultron-hub-health.timer"], []) is None
    assert fake.calls == [  # the service was already linked
        ["/usr/bin/systemctl", "--user", "link", str(spec.UNITS_DIR / "ultron-hub-health.timer")],
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "enable", "--now", "ultron-hub-health.timer"]]
    monkeypatch.setattr(spec, "_run", FakeRun([(["/usr/bin/systemctl", "--user", "link"], (1, "File exists"))]))
    assert "link" in spec._user_unit_enable(["ultron-hub-health.timer"], [])


# --- user_unit_disable (todo #460): only an ultron-hub-*.timer linked from deploy/systemd of this repository ---------

@pytest.fixture
def unit_dir(monkeypatch, tmp_path):
    units = tmp_path / "units"
    monkeypatch.setattr(spec, "USER_UNIT_DIR", units)
    (units / "timers.target.wants").mkdir(parents=True)
    return units


def link_repo(units, name, wanted=False):
    (units / name).symlink_to(spec.UNITS_DIR / name)  # the repository file may not exist (removed by the merge)
    if wanted:
        (units / "timers.target.wants" / name).symlink_to(units / name)


@pytest.mark.parametrize("args", [
    [], ["ultron-hub-old.timer", "--global"], ["ultron-hub-old.service"], ["hermes-gateway.timer"], ["omniroute.timer"],
    ["ultron-hub-old.timer\n"], ["../ultron-hub-old.timer"], ["/tmp/ultron-hub-old.timer"], ["ultron-hub-old"],
    ["ultron-hub-dashboard.service"], ["ultron-bot.timer"], ["ultron-hub-old.timer;reboot"], ["--all"],
])
def test_unit_disable_refuses_names(unit_dir, args):
    with pytest.raises(spec.Refused):
        spec.describe("user_unit_disable", args)


def test_unit_disable_refuses_a_link_elsewhere_or_a_file(unit_dir, tmp_path):
    (unit_dir / "ultron-hub-old.timer").symlink_to(tmp_path / "elsewhere" / "ultron-hub-old.timer")
    (unit_dir / "ultron-hub-real.timer").write_text("[Timer]\n")
    (unit_dir / "ultron-hub-rel.timer").symlink_to("ultron-hub-old.timer")  # relative: not the repository's path
    (unit_dir / "ultron-hub-dots.timer").symlink_to(spec.UNITS_DIR / ".." / "systemd" / "ultron-hub-dots.timer")
    for name in ("ultron-hub-old.timer", "ultron-hub-real.timer", "ultron-hub-rel.timer", "ultron-hub-dots.timer"):
        with pytest.raises(spec.Refused):
            spec.describe("user_unit_disable", [name])
        with pytest.raises(spec.Refused):
            spec._user_unit_disable([name], [])
    assert (unit_dir / "ultron-hub-real.timer").is_file() and (unit_dir / "ultron-hub-old.timer").is_symlink()


def test_unit_disable_describes_exactly(unit_dir):
    link_repo(unit_dir, "ultron-hub-old.timer")
    assert spec.describe("user_unit_disable", ["ultron-hub-old.timer"]) == [
        "/usr/bin/systemctl --user disable --now ultron-hub-old.timer",
        f"supprime les liens {unit_dir}/ultron-hub-old.timer et {unit_dir}/ultron-hub-old.service s'ils pointent vers "
        f"{spec.UNITS_DIR} (un autre lien ou un fichier est gardé)",
        "/usr/bin/systemctl --user daemon-reload",
        f"retire {unit_dir}/timers.target.wants/ultron-hub-old.timer s'il est un lien cassé vers {spec.UNITS_DIR} "
        "(fichier déjà supprimé par la fusion)",
        f"vérifie que {unit_dir}/timers.target.wants/ultron-hub-old.timer n'existe plus ; rien n'est fait si "
        "ultron-hub-old.timer n'est déjà plus lié",
        "/usr/bin/systemctl --user reset-failed ultron-hub-old.timer (efface l'état « en échec » d'une minuterie supprimée)"]


def test_unit_disable_disables_then_unlinks_the_repository_links_only(unit_dir, tmp_path, monkeypatch):
    link_repo(unit_dir, "ultron-hub-old.timer", wanted=True)
    link_repo(unit_dir, "ultron-hub-old.service")
    def disable(cmd, log):
        (unit_dir / "timers.target.wants" / "ultron-hub-old.timer").unlink()  # what systemctl disable does
        return 0, ""
    fake = FakeRun([(["/usr/bin/systemctl", "--user", "disable"], disable)])
    monkeypatch.setattr(spec, "_run", lambda cmd, log: disable(cmd, log) if cmd[2] == "disable" else fake(cmd, log))
    log = []
    assert spec._user_unit_disable(["ultron-hub-old.timer"], log) is None
    assert not os.path.lexists(unit_dir / "ultron-hub-old.timer") and not os.path.lexists(unit_dir / "ultron-hub-old.service")
    assert fake.calls == [["/usr/bin/systemctl", "--user", "daemon-reload"],
                          ["/usr/bin/systemctl", "--user", "reset-failed", "ultron-hub-old.timer"]]


def test_unit_disable_keeps_a_foreign_service_and_reports_a_timer_still_enabled(unit_dir, tmp_path, monkeypatch):
    link_repo(unit_dir, "ultron-hub-old.timer", wanted=True)
    (unit_dir / "ultron-hub-old.service").symlink_to(tmp_path / "other.service")
    fake = FakeRun([(["/usr/bin/systemctl", "--user", "disable"], (1, "boom"))])
    monkeypatch.setattr(spec, "_run", fake)
    error = spec._user_unit_disable(["ultron-hub-old.timer"], [])
    assert "still enabled" in error and "disable --now failed" in error
    assert (unit_dir / "ultron-hub-old.service").is_symlink() and not os.path.lexists(unit_dir / "ultron-hub-old.timer")
    assert fake.calls == [["/usr/bin/systemctl", "--user", "disable", "--now", "ultron-hub-old.timer"],
                          ["/usr/bin/systemctl", "--user", "daemon-reload"]]


def test_unit_disable_does_nothing_when_already_removed(unit_dir, monkeypatch):
    fake = FakeRun([])
    monkeypatch.setattr(spec, "_run", fake)
    log = []
    assert spec._user_unit_disable(["ultron-hub-old.timer"], log) is None and fake.calls == [] and "déjà retiré" in log[0]


def wanted_repo(units, name):
    """The real layout of 29/10: timers.target.wants links straight to deploy/systemd (file deleted by the merge)."""
    (units / "timers.target.wants" / name).symlink_to(spec.UNITS_DIR / name)


def test_unit_disable_drops_the_broken_wants_link_when_the_file_is_gone(unit_dir, monkeypatch):
    # todo #473: systemctl disable fails ("Unit file ... does not exist") once the merge deleted the file
    link_repo(unit_dir, "ultron-hub-old.timer")
    wanted_repo(unit_dir, "ultron-hub-old.timer")
    fake = FakeRun([(["/usr/bin/systemctl", "--user", "disable"], (1, "Unit file does not exist."))])
    monkeypatch.setattr(spec, "_run", fake)
    log = []
    assert spec._user_unit_disable(["ultron-hub-old.timer"], log) is None
    assert not os.path.lexists(unit_dir / "timers.target.wants" / "ultron-hub-old.timer")
    assert any("lien cassé retiré" in line for line in log)


def test_unit_disable_replay_cleans_a_broken_wants_link_left_by_a_failed_run(unit_dir, monkeypatch):
    # the run of 29/10 removed the main links, then failed: a replay must not say "rien à faire"
    wanted_repo(unit_dir, "ultron-hub-old.timer")
    fake = FakeRun([])
    monkeypatch.setattr(spec, "_run", fake)
    log = []
    assert spec._user_unit_disable(["ultron-hub-old.timer"], log) is None
    assert not os.path.lexists(unit_dir / "timers.target.wants" / "ultron-hub-old.timer")
    assert fake.calls == [["/usr/bin/systemctl", "--user", "daemon-reload"],
                          ["/usr/bin/systemctl", "--user", "reset-failed", "ultron-hub-old.timer"]]


def test_unit_disable_keeps_a_working_or_foreign_wants_link(unit_dir, tmp_path, monkeypatch):
    target = tmp_path / "ultron-hub-live.timer"
    target.write_text("[Timer]\n")
    (unit_dir / "timers.target.wants" / "ultron-hub-live.timer").symlink_to(target)  # works: not ours to drop
    (unit_dir / "timers.target.wants" / "ultron-hub-far.timer").symlink_to(tmp_path / "gone" / "ultron-hub-far.timer")
    monkeypatch.setattr(spec, "_run", FakeRun([]))
    for name in ("ultron-hub-live.timer", "ultron-hub-far.timer"):
        assert spec._user_unit_disable([name], []) is None
        assert (unit_dir / "timers.target.wants" / name).is_symlink()


def test_after_merge_460_declares_the_sunday_timer_and_the_disables():
    root = Path(__file__).resolve().parents[1]
    plan = json.loads((root / "deploy/user/apres-fusion/todo-460.json").read_text(encoding="utf-8"))["actions"]
    assert len(plan) <= after_merge.MAX_ACTIONS
    assert [(a["action"], a["args"]) for a in plan] == [
        ("user_unit_enable", ["ultron-hub-dimanche.timer"]),
        *(("user_unit_disable", [f"ultron-hub-{n}.timer"]) for n in ("focus", "menage", "cap", "todo-digest"))]
    assert spec.describe("user_unit_enable", ["ultron-hub-dimanche.timer"])[-1].endswith("enable --now ultron-hub-dimanche.timer")
    for name in (a["args"][0] for a in plan[1:]):
        assert not (spec.UNITS_DIR / name).exists() and not (spec.UNITS_DIR / name.replace(".timer", ".service")).exists()


def test_main_refuses_root_and_prints_json(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(spec, "LOG_FILE", tmp_path / "ultron-user.log")
    monkeypatch.setattr(spec.os, "geteuid", lambda: 0)
    assert spec.main(["user_unit_restart", "hermes-gateway"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "refused" and "root" in out["error"]
    assert json.loads((tmp_path / "ultron-user.log").read_text())["status"] == "refused"


def test_main_runs_the_validated_command_only(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(spec, "LOG_FILE", tmp_path / "ultron-user.log")
    fake = FakeRun([])
    monkeypatch.setattr(spec, "_run", fake)
    assert spec.main(["hermes_config_set", "display.language", "fr"]) == 0
    assert fake.calls == [[spec.HERMES, "config", "set", "display.language", "fr"]]
    assert spec.main(["hermes_config_set", "approvals.mode", "off"]) == 1
    assert len(fake.calls) == 1
    outs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [o["status"] for o in outs] == ["ok", "refused"]


def test_script_refuses_unknown_action_as_json():
    proc = subprocess.run([sys.executable, "-I", str(user_actions.repo_tool()), "sudo", "id"],
                          capture_output=True, text=True, timeout=30)
    out = json.loads(proc.stdout)
    assert proc.returncode == 1 and out["status"] == "refused" and "unknown action" in out["error"]


# --- ultron-hub side --------------------------------------------------------------------------

def test_run_calls_the_repo_tool_without_sudo(monkeypatch):
    seen = {}

    def fake(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"status": "ok", "output": "ok"}) + "\n", "")
    monkeypatch.setattr(user_actions.subprocess, "run", fake)
    assert user_actions.run("user_unit_restart", ["hermes-gateway"])["status"] == "ok"
    assert seen["cmd"] == ["/usr/bin/python3", "-I", str(user_actions.repo_tool()), "user_unit_restart",
                           "hermes-gateway"]
    assert "sudo" not in seen["cmd"] and set(seen["env"]) == {"PATH", "LANG"}
    seen.clear()
    with pytest.raises(InvalidRequestError):
        user_actions.run("hermes_config_set", ["approvals.mode", "off"])
    assert not seen  # refused before running anything


def test_describe_actions_lists_the_tool():
    described = user_actions.describe_actions()
    assert all(a in described for a in spec.ACTIONS) and all(c in described for c in spec.PLUGIN_COMMANDS)
    assert all(k in described for k in spec.CONFIG_KEYS) and "aucun paquet" in described


def test_cli_user_preview(capsys):
    assert cli.main(["priv", "user-preview", "user_unit_restart", "hermes-gateway"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["command"][0].startswith("/usr/bin/systemd-run")
    assert cli.main(["priv", "user-preview", "hermes_config_set", "approvals.mode", "off"]) == 1
    capsys.readouterr()


def run(coro):
    return asyncio.run(coro)


def callbacks(screen):
    return [d for row in screen.buttons for _, d in row]


def test_action_text_shows_exact_commands():
    req = {"n": 1, "action": "hermes_config_set", "args": ["display.language", "fr"], "reason": "besoin",
           "status": "pending", "command": [f"{spec.HERMES} config set display.language fr"], "error": None,
           "output": ""}
    out = user_actions.action_text(req)
    assert f"{spec.HERMES} config set display.language fr" in out and "sans sudo" in out and "en attente" in out


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def press(monkeypatch, action, target, **kw):
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token(action, target, **kw))


# --- après fusion -----------------------------------------------------------------------------

DECLARATION = json.dumps({"actions": [
    {"action": "hermes_menu_add", "args": ["atoi"], "reason": "nouvelle commande"},
    {"action": "hermes_config_set", "args": ["approvals.mode", "off"], "reason": "tentative"},
    {"action": "user_unit_restart", "args": ["hermes-gateway"], "reason": "charger le menu"},
]})


def test_parse_validates_each_action():
    entries = after_merge.parse(DECLARATION)
    assert [e["status"] for e in entries] == ["pending", "invalid", "pending"]
    assert entries[0]["command"] == spec.describe("hermes_menu_add", ["atoi"])
    for bad in ("[]", "{", '{"actions": "x"}'):
        with pytest.raises(InvalidRequestError):
            after_merge.parse(bad)
    many = json.dumps({"actions": [{"action": "user_unit_restart", "args": ["hermes-gateway"]}] * 9})
    assert len(after_merge.parse(many)) == after_merge.MAX_ACTIONS


def test_merge_records_declared_actions_from_main(repo):
    branch(repo, "todo-5/feat", after_merge.declaration_path(5), DECLARATION)
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    assert r["closed"] and [a["status"] for a in r["after_merge"]] == ["pending", "invalid", "pending"]
    message = branch_commands.merge_text(r)
    assert "2 action(s) serveur prévue(s) après la fusion" in message and "hors liste blanche" in message
    assert after_merge.load(repo, 5)["actions"] == r["after_merge"]
    calls = []
    with pytest.raises(InvalidRequestError, match="sans preuve"):
        after_merge.decide(repo, 5, 1, approve=True, run_user=lambda a, b: calls.append(a))
    req = after_merge.decide(repo, 5, 1, approve=True, by=SIGNED,
                             run_user=lambda a, b: calls.append((a, b)) or {"status": "ok", "output": "ok"})
    assert req["status"] == "done" and calls == [("hermes_menu_add", ["atoi"])]
    with pytest.raises(InvalidRequestError, match="déjà traitée"):
        after_merge.decide(repo, 5, 1, approve=True, by=SIGNED, run_user=lambda a, b: calls.append(a))
    with pytest.raises(InvalidRequestError, match="déjà traitée"):  # invalid: never runs
        after_merge.decide(repo, 5, 2, approve=True, by={"by": "cli-human"}, run_user=lambda a, b: calls.append(a))
    with pytest.raises(InvalidRequestError, match="déjà utilisé"):  # same proof on another action
        after_merge.decide(repo, 5, 3, approve=True, by=SIGNED, run_user=lambda a, b: calls.append(a))
    assert len(calls) == 1 and after_merge.decide(repo, 5, 3, approve=False)["status"] == "refused"


def test_merge_without_declaration_has_no_actions(repo):
    branch(repo, "todo-5/feat")
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    assert r["closed"] and r["after_merge"] == [] and "après la fusion" not in branch_commands.merge_text(r)


def test_uncommitted_declaration_is_ignored(repo):
    """Only main counts: a file lying in the tree (never reviewed, never merged) is not read."""
    branch(repo, "todo-5/feat")
    loose = repo.root / after_merge.declaration_path(5)
    loose.parent.mkdir(parents=True)
    loose.write_text(DECLARATION)
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    assert r["closed"] and r["after_merge"] == []


def test_record_is_done_once(repo):
    first = after_merge.record(repo, 7, lambda path: DECLARATION)
    after_merge.decide(repo, 7, 1, approve=False)
    again = after_merge.record(repo, 7, lambda path: json.dumps({"actions": []}))
    assert again[0]["status"] == "refused" and len(again) == len(first)


def test_after_run_cli_signing(repo, monkeypatch, capsys):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    after_merge.record(repo, 5, lambda path: DECLARATION)
    monkeypatch.setattr(after_merge.decide, "__defaults__", (lambda a, b: {"status": "ok", "output": "ok"}, None))
    monkeypatch.setattr(after_merge, "launch_unit", lambda s, t, n: after_merge.execute(
        s, t, n, run_user=lambda a, b: {"status": "ok", "output": "ok"}))  # no real systemd-run
    code, out = run_cli(capsys, "branch", "after-run", "5", "1")
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "after-run" in out["error"]
    press(monkeypatch, "user", "M1:1")  # a token for another target
    code, out = run_cli(capsys, "branch", "after-run", "5", "1")
    assert code == 1 and after_merge.load(repo, 5)["actions"][0]["status"] == "pending"
    press(monkeypatch, "user", "todo-5:1")
    code, out = run_cli(capsys, "branch", "after-run", "5", "1")
    assert code == 0 and out["data"]["status"] == "done"
    assert out["data"]["approval"]["by"] == "telegram-button"
    code, out = run_cli(capsys, "branch", "after", "5")
    assert code == 0 and [r["status"] for r in out["data"]["requests"]] == ["done", "invalid", "pending"]
    state = after_merge.load(repo, 5)  # replay: even if the action were pending again, the token is spent
    state["actions"][0]["status"] = "pending"
    after_merge._save(repo, state)
    code, out = run_cli(capsys, "branch", "after-run", "5", "1")
    assert code == 1 and "déjà utilisé" in out["error"]






# --- todo #79: after-merge actions survive a gateway restart ----------------------------------

def _running(repo, todo=7, age_s=3600):
    """An approved action whose process vanished (the 14/10 failure): status running, decided long ago."""
    after_merge.record(repo, todo, lambda path: DECLARATION)
    state = after_merge.load(repo, todo)
    state["actions"][0].update(status="running", decided_at=datetime.fromtimestamp(time.time() - age_s, timezone.utc).isoformat())
    after_merge._save(repo, state)


def test_orphan_running_action_becomes_interrupted_and_replayable(repo):
    _running(repo)
    state = after_merge.reconcile(repo, 7, alive=lambda unit: False)
    req = state["actions"][0]
    assert req["status"] == "interrupted" and "redémarrage" in req["error"] and "🧰" in req["error"]
    assert after_merge.pending(state["actions"])[0]["n"] == 1
    assert "interrompue" in branch_commands.after_text(req, 7)
    calls = []
    done = after_merge.decide(repo, 7, 1, approve=True, by=SIGNED,
                              run_user=lambda a, b: calls.append(a) or {"status": "ok", "output": "ok"})
    assert done["status"] == "done" and done["error"] is None and calls == ["hermes_menu_add"]


def test_live_or_fresh_running_action_is_left_alone(repo):
    _running(repo)
    assert after_merge.reconcile(repo, 7, alive=lambda unit: True)["actions"][0]["status"] == "running"
    _running(repo, age_s=1)  # unit not visible yet: grace period
    assert after_merge.reconcile(repo, 7, alive=lambda unit: False)["actions"][0]["status"] == "running"
    with pytest.raises(InvalidRequestError, match="déjà traitée"):  # still running: no second run
        after_merge.decide(repo, 7, 1, approve=True, by=SIGNED, run_user=lambda a, b: None)


def test_gateway_restart_during_action_is_survived(repo):
    """The caller (inside hermes-gateway) dies while the unit runs: the unit records the result by itself."""
    after_merge.record(repo, 7, lambda path: DECLARATION)
    seen = []

    def launch(settings, todo_id, n):
        seen.append(after_merge.unit_name(todo_id, n))
        after_merge.execute(settings, todo_id, n, run_user=lambda a, b: {"status": "ok", "output": "npm ok"})

    req = after_merge.decide(repo, 7, 1, approve=True, by=SIGNED, launch=launch)
    assert seen == ["ultron-after-merge-7-1"] and req["status"] == "done" and req["output"] == "npm ok"
    # caller killed before it could read the result: state is already final, nothing is "running" for ever
    assert after_merge.load(repo, 7)["actions"][0]["status"] == "done"


def test_unit_dying_without_result_is_interrupted_and_start_failure_is_failed(repo):
    after_merge.record(repo, 7, lambda path: DECLARATION)
    req = after_merge.decide(repo, 7, 1, approve=True, by=SIGNED, launch=lambda s, t, n: None)  # unit gone, no result
    assert req["status"] == "interrupted"
    boom = lambda s, t, n: (_ for _ in ()).throw(UltronHubError("systemd-run a échoué : pas de bus"))
    req = after_merge.decide(repo, 7, 1, approve=True, by={"by": "telegram-button", "proof": "feedfeedfeedfeed"}, launch=boom)
    assert req["status"] == "failed" and "pas de bus" in req["error"]


def test_execute_refuses_an_action_that_was_not_approved(repo):
    after_merge.record(repo, 7, lambda path: DECLARATION)
    with pytest.raises(InvalidRequestError, match="pas en cours"):
        after_merge.execute(repo, 7, 1, run_user=lambda a, b: pytest.fail("never"))


def test_launch_unit_is_a_transient_user_unit_outside_the_gateway(repo, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(after_merge.subprocess, "run", fake_run)
    after_merge.launch_unit(repo, 7, 2)
    cmd = seen["cmd"]
    assert cmd[:3] == ["systemd-run", "--user", "--unit"] and "ultron-after-merge-7-2" in cmd
    assert "--collect" in cmd and "--wait" in cmd and cmd[-4:] == ["branch", "after-exec", "7", "2"]




# --- claude_hooks_sync (todo #175): fixed list of Claude Code hooks ------------------------------

@pytest.fixture
def claude_settings(monkeypatch, tmp_path):
    """A fake ~/.claude/settings.json: the real one is never read or written here."""
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    monkeypatch.setattr(spec, "CLAUDE_SETTINGS", path)
    monkeypatch.setattr(spec, "LOG_FILE", tmp_path / "ultron-user.log")
    return path


def hook_commands(path):
    hooks = json.loads(path.read_text())["hooks"]
    return {event: [h["command"] for g in groups for h in g["hooks"]] for event, groups in hooks.items()}


def test_claude_hooks_list_is_fixed():
    cli = f"{spec.REPO}/.venv/bin/ultron-hub"
    assert spec.claude_hooks_wanted() == [
        ("SessionStart", f"{cli} esprit context --claude-hook"),
        ("SessionEnd", f"{cli} journal summarize --claude-hook --detach"),
        ("PostToolUse", f"{cli} esprit contexte --claude-hook"),
        ("Stop", f"{cli} objectif stop-hook"),
        ("PreToolUse", f"{spec.REPO}/.venv/bin/python -I {spec.REPO}/deploy/hooks/garde.py")]
    lines = spec.describe("claude_hooks_sync", [])
    assert all(any(c in line for line in lines) for _, c in spec.claude_hooks_wanted())


def test_claude_hooks_sync_without_argument_creates_a_missing_file(claude_settings, capsys):
    assert not claude_settings.exists()
    assert spec.main(["claude_hooks_sync"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and "PostToolUse : ajouté" in out["output"]
    assert hook_commands(claude_settings) == {e: [c] for e, c in spec.claude_hooks_wanted()}
    assert json.loads(claude_settings.read_text())["hooks"]["PostToolUse"][0]["hooks"][0] == {
        "type": "command", "command": spec.claude_hooks_wanted()[2][1], "timeout": 30}
    assert list(claude_settings.parent.glob("settings.json.*")) == []  # nothing to back up, no temporary left


def test_claude_hooks_sync_refuses_arguments(claude_settings, capsys):
    claude_settings.write_text("{}")
    assert spec.main(["claude_hooks_sync", "PostToolUse", "/bin/sh -c id"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "refused" and "no argument" in out["error"]
    assert claude_settings.read_text() == "{}"


def test_claude_hooks_sync_is_idempotent(claude_settings):
    log = []
    assert spec._claude_hooks_sync([], log) is None
    first = claude_settings.read_text()
    backups = set(claude_settings.parent.glob("settings.json.bak-*"))
    log = []
    assert spec._claude_hooks_sync([], log) is None
    assert claude_settings.read_text() == first and set(claude_settings.parent.glob("settings.json.bak-*")) == backups
    assert log[-1] == "rien à écrire" and all("déjà présent" in line for line in log[:-2])
    assert log[-2] == "permissions.deny : déjà complet"


def test_claude_hooks_sync_keeps_everything_else_and_backs_up(claude_settings):
    start_cmd = spec.claude_hooks_wanted()[0][1]
    before = {
        "model": "opus", "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": start_cmd, "timeout": 30}]}],
            "PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "/usr/bin/true"}]}],
            "Notification": [{"hooks": [{"type": "command", "command": "/usr/bin/echo fin"}]}],
        },
    }
    claude_settings.write_text(json.dumps(before))
    claude_settings.chmod(0o640)
    log = []
    assert spec._claude_hooks_sync([], log) is None
    after = json.loads(claude_settings.read_text())
    assert after["model"] == "opus" and after["permissions"]["allow"] == before["permissions"]["allow"]
    assert after["permissions"]["deny"] == spec.claude_deny_wanted()  # todo #485
    assert after["hooks"]["Notification"] == before["hooks"]["Notification"]
    assert after["hooks"]["SessionStart"] == before["hooks"]["SessionStart"]  # already there: not duplicated
    assert after["hooks"]["PostToolUse"][0] == before["hooks"]["PostToolUse"][0]  # the other hook stays first
    assert hook_commands(claude_settings)["PostToolUse"] == ["/usr/bin/true", spec.claude_hooks_wanted()[2][1]]
    assert hook_commands(claude_settings)["SessionEnd"] == [spec.claude_hooks_wanted()[1][1]]
    (backup,) = claude_settings.parent.glob("settings.json.bak-*")
    assert json.loads(backup.read_text()) == before
    assert claude_settings.stat().st_mode & 0o777 == 0o640
    assert "SessionStart : déjà présent" in log and any(line.startswith("sauvegarde : ") for line in log)


@pytest.mark.parametrize("content", ["{pas du json", "[]", '{"hooks": []}', '{"hooks": {"PostToolUse": {}}}'])
def test_claude_hooks_sync_never_rewrites_an_unexpected_file(claude_settings, content):
    claude_settings.write_text(content)
    with pytest.raises(spec.Refused):
        spec._claude_hooks_sync([], [])
    assert claude_settings.read_text() == content


def test_claude_hooks_sync_refuses_a_symlink_or_a_missing_directory(monkeypatch, tmp_path):
    real = tmp_path / "ailleurs.json"
    real.write_text("{}")
    link = tmp_path / ".claude" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(spec, "CLAUDE_SETTINGS", link)
    with pytest.raises(spec.Refused, match="symlink"):
        spec._claude_hooks_sync([], [])
    assert real.read_text() == "{}"
    monkeypatch.setattr(spec, "CLAUDE_SETTINGS", tmp_path / "absent" / "settings.json")
    with pytest.raises(spec.Refused):
        spec._claude_hooks_sync([], [])
    assert not (tmp_path / "absent").exists()


# --- permissions.deny (todo #485): sessions never read the secrets -----------------------------------

SECRET_PATTERNS = {"~/.config/ultron-hub/**", "~/.claude/.credentials.json", "~/.ssh/**", "~/.sudo_pass",
                   "~/.hermes/.env", "~/.hermes/google_*", "~/.config/gh/**", "//**/auth.json", "~/.config/supertool/**"}


def test_claude_deny_list_is_fixed_and_covers_every_secret():
    rules = spec.claude_deny_wanted()
    assert len(rules) == len(set(rules))
    assert {p for p, _ in spec.CLAUDE_DENY_PATHS} == SECRET_PATTERNS
    for pattern, fragment in spec.CLAUDE_DENY_PATHS:
        assert f"Read({pattern})" in rules and f"Edit({pattern})" in rules
        assert fragment in pattern.replace("~", "").replace("//**/", "")
        for cmd in ("cat", "head", "tail", "less", "cp", "base64", "grep", "sh", "bash"):
            assert f"Bash({cmd} *{fragment}*)" in rules
    # Write(path) never matches (a Read deny already blocks Write); no rule without a path, no allow rule
    assert not any(r.startswith(("Write(", "NotebookEdit(")) for r in rules)
    assert all(r.endswith(")") and "(" in r and "()" not in r for r in rules)
    assert any("permissions.deny" in line for line in spec.describe("claude_hooks_sync", []))


def bash_rule_matches(rule, command):
    """Claude Code's matching of a Bash(...) deny rule: '*' = any text (code.claude.com/docs/en/permissions)."""
    import fnmatch
    return fnmatch.fnmatchcase(command, rule[len("Bash("):-1])


@pytest.mark.parametrize("command", [
    "cat ~/.config/ultron-hub/close.key", "head -c 40 {{HOME}}/.config/ultron-hub/comptes/a.json",
    "base64 ~/.claude/.credentials.json", "cp ~/.ssh/id_ed25519 /tmp/x", "less ~/.sudo_pass",
    "grep TOKEN ~/.hermes/.env", "tail ~/.hermes/google_token.json", "cat ~/.config/gh/hosts.yml",
    "cat ~/.codex/auth.json", "sh -c 'cat ~/.config/ultron-hub/restic.pass'", "cat ~/.config/supertool/sp-api.json",
    "grep KEEPA {{HOME}}/.config/supertool/keepa.json"])
def test_claude_deny_bash_rules_catch_the_common_reads(command):
    assert any(bash_rule_matches(r, command) for r in spec.claude_deny_wanted() if r.startswith("Bash("))


@pytest.mark.parametrize("command", ["cat README.md", "grep -rn todo src", "ultron-hub todo show 485", "git status",
                                     "cat ~/workspace/projects/active/supertool/README.md",
                                     "cat ~/.config/systemd/user/x.service"])
def test_claude_deny_bash_rules_leave_ordinary_work_alone(command):
    assert not any(bash_rule_matches(r, command) for r in spec.claude_deny_wanted() if r.startswith("Bash("))


def test_claude_hooks_sync_adds_the_deny_list_and_keeps_other_rules(claude_settings):
    mine = ["Bash(rm -rf /*)", spec.claude_deny_wanted()[0]]
    before = {"permissions": {"allow": ["Bash(ls *)"], "deny": list(mine), "defaultMode": "default"}}
    claude_settings.write_text(json.dumps(before))
    log = []
    assert spec._claude_hooks_sync([], log) is None
    after = json.loads(claude_settings.read_text())["permissions"]
    assert after["allow"] == ["Bash(ls *)"] and after["defaultMode"] == "default"
    assert after["deny"][:2] == mine  # kept first, in place, not duplicated
    assert after["deny"][2:] == spec.claude_deny_wanted()[1:]
    assert f"permissions.deny : {len(spec.claude_deny_wanted()) - 1} règle(s) ajoutée(s)" in log
    log = []
    assert spec._claude_hooks_sync([], log) is None
    assert "permissions.deny : déjà complet" in log and log[-1] == "rien à écrire"


def test_claude_hooks_sync_writes_the_deny_list_even_when_hooks_are_there(claude_settings):
    hooks = {e: [{"hooks": [{"type": "command", "command": c, "timeout": 30}]}] for e, c in spec.claude_hooks_wanted()}
    claude_settings.write_text(json.dumps({"hooks": hooks}))
    assert spec._claude_hooks_sync([], []) is None
    data = json.loads(claude_settings.read_text())
    assert data["hooks"] == hooks and data["permissions"]["deny"] == spec.claude_deny_wanted()
    assert len(list(claude_settings.parent.glob("settings.json.bak-*"))) == 1


@pytest.mark.parametrize("content", ['{"permissions": []}', '{"permissions": {"deny": "Read(x)"}}'])
def test_claude_hooks_sync_never_rewrites_unexpected_permissions(claude_settings, content):
    claude_settings.write_text(content)
    with pytest.raises(spec.Refused, match="permissions"):
        spec._claude_hooks_sync([], [])
    assert claude_settings.read_text() == content


def test_todo_485_installs_the_deny_list_after_merge():
    plan = json.loads((Path(user_actions.repo_tool()).parents[2] / "deploy/user/apres-fusion/todo-485.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("claude_hooks_sync", [])]


def test_install_esprit_uses_the_same_hook_list():
    script = (Path(user_actions.repo_tool()).parents[2] / "scripts" / "install-esprit.sh").read_text()
    assert 'deploy/user/ultron-user" claude_hooks_sync' in script
    assert "--claude-hook" not in script  # no second list of hooks to keep in line


def test_after_merge_file_of_todo_175_is_valid():
    declared = json.loads((Path(user_actions.repo_tool()).parents[0] / "apres-fusion" / "todo-175.json").read_text())
    assert [(a["action"], a["args"]) for a in declared["actions"]] == [("claude_hooks_sync", [])]
    for a in declared["actions"]:
        assert spec.describe(a["action"], a["args"])


def test_spec_reloaded_when_tool_changes(tmp_path, monkeypatch):
    """Todo #181: two merges in one process, the tool gains a command in between: the 2nd validation accepts it."""
    tool = tmp_path / "ultron-user"
    tool.write_text("ACTIONS = ('a',)\n")
    monkeypatch.setattr(user_actions, "repo_tool", lambda: tool)
    first = user_actions._spec()
    assert user_actions._spec() is first  # unchanged file: cache kept
    assert user_actions.actions() == ("a",)
    tool.write_text("ACTIONS = ('a', 'b')\n")
    assert user_actions.actions() == ("a", "b")


def test_ultron_poser_runs_the_fixed_script_without_argument(monkeypatch, tmp_path, capsys):
    """Todo #356: the posing of ~/.local/bin/ultron after merge is an action, not a manual step."""
    script = tmp_path / "poser.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setattr(spec, "POSER_ULTRON", script)
    monkeypatch.setattr(spec, "LOG_FILE", tmp_path / "ultron-user.log")
    seen = []
    monkeypatch.setattr(spec, "_run", lambda cmd, log, cwd="/": (seen.append(cmd), (0, "pose-ok"))[1])
    assert spec.describe("ultron_poser", [])[0] == f"/bin/bash {script}"
    assert spec.main(["ultron_poser"]) == 0
    assert seen == [["/bin/bash", str(script)]]
    assert spec.main(["ultron_poser", "--essai"]) == 1 and len(seen) == 1


def test_after_merge_473_disables_the_five_timers_removed_by_460():
    root = Path(__file__).resolve().parents[1]
    plan = json.loads((root / "deploy/user/apres-fusion/todo-473.json").read_text(encoding="utf-8"))["actions"]
    assert len(plan) <= after_merge.MAX_ACTIONS
    assert [(a["action"], a["args"]) for a in plan] == [
        ("user_unit_disable", [f"ultron-hub-{n}.timer"]) for n in ("focus", "menage", "cap", "todo-digest", "bilan")]
    for name in (a["args"][0] for a in plan):
        assert not (spec.UNITS_DIR / name).exists()


def test_every_timer_removed_from_the_repository_is_disabled_after_merge():
    # Todo #473: #460 removed ultron-hub-bilan.timer without declaring its disable; it stayed "failed" on the dashboard.
    root = Path(__file__).resolve().parents[1]
    try:
        out = subprocess.run(["git", "-C", str(root), "log", "--diff-filter=D", "--name-only", "--format=",
                              "--", "deploy/systemd/*.timer"], check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git history unavailable")
    removed = {Path(line).name for line in out.split()} - {p.name for p in spec.UNITS_DIR.glob("*.timer")}
    declared = {a["args"][0] for f in (root / "deploy/user/apres-fusion").glob("*.json")
                for a in json.loads(f.read_text(encoding="utf-8"))["actions"] if a["action"] == "user_unit_disable"}
    assert removed <= declared, f"déclarer user_unit_disable dans deploy/user/apres-fusion/ pour : {sorted(removed - declared)}"
