import json
import os
import re
import sys
import textwrap
from pathlib import Path

import pytest

PING_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok", "warn", "error"]}, "summary": {"type": "string"}},
    "required": ["status", "summary"],
}


def _tests_needing_missing_hermes_plugin() -> list[str]:
    """Kit export (todo #391): hermes_plugin/ and deploy/user/ are not exported, so the tests that use them cannot run;
    ignore them. Includes the tests that import (`from test_x import …`) one of those, until nothing changes."""
    here = Path(__file__).parent
    if (here.parent / "hermes_plugin").is_dir() and (here.parent / "deploy" / "user").is_dir():
        return []
    texts = {p.stem: p.read_text(encoding="utf-8") for p in here.glob("test_*.py")}
    ignored = {name for name, text in texts.items() if "hermes_plugin" in text or "user_actions" in text}
    grew = True
    while grew:
        grew = False
        for name, text in texts.items():
            if name not in ignored and any(re.search(rf"^\s*(from|import) (tests\.|\.)?{other}\b", text, re.M) for other in ignored):
                ignored.add(name)
                grew = True
    return sorted(f"{name}.py" for name in ignored)


collect_ignore = _tests_needing_missing_hermes_plugin()

# Kit export: these tests read a file that is not exported (deploy/systemd units of other services, deploy/navigateur,
# config/contexte.yaml, config/dashboard.yaml...). Skipped there with that reason; they all run in the full repository.
KIT_SKIPS = {
    "test_cap_bilan.py::test_timer_and_after_merge", "test_cap_send.py::test_timer_files_and_after_merge",
    "test_capturer_sans_navigateur.py::test_capturer_runs_with_fake_playwright",
    "test_compte_budget.py::test_units_and_after_merge_file", "test_contexte.py::test_engine_copies_config",
    "test_dashboard.py::test_devices_file", "test_dashboard.py::test_service_and_after_merge_file",
    "test_dashboard_push.py::test_page_registers_worker_and_after_merge_restarts_dashboard",
    "test_health_alertes.py::test_service_runs_alertes_after_collect_and_action_is_declared",
    "test_veille.py::test_demo_veille_reports_a_moved_main", "test_veille.py::test_timer_and_after_merge_declared",
    "test_veille.py::test_real_sandbox_probe_demo",
}


def pytest_collection_modifyitems(config, items):
    if (Path(__file__).parent.parent / "hermes_plugin").is_dir():
        return
    skip = pytest.mark.skip(reason="reads a file that the kit export does not contain")
    for item in items:
        if item.nodeid.removeprefix("tests/") in KIT_SKIPS:
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_send: use the real todo.service.send_message (its subprocess is mocked)")
    config.addinivalue_line("markers", "sends_telegram: the test may call the default `hermes` binary (see no_real_hermes)")


@pytest.fixture(autouse=True)
def _telegram_actif(monkeypatch):
    """Telegram is off by default (todo #290): the existing tests of the sending code run with it on."""
    monkeypatch.setenv("ULTRON_TELEGRAM", "on")


@pytest.fixture(autouse=True)
def no_real_hermes(request, tmp_path_factory, monkeypatch):
    """Control of todo #393 (a test must never send a real Telegram message): the default `hermes` binary (PATH and
    ULTRON_HUB_HERMES_BIN) is a fake that records its calls, the bot token is removed, and the test FAILS at teardown
    if the fake was called. Tests that pass their own fake binary are not affected; mark `sends_telegram` to exempt."""
    bin_dir = tmp_path_factory.mktemp("no-real-hermes")  # not tmp_path: some tests list its content
    calls = bin_dir / "calls.log"
    fake = bin_dir / "hermes"
    fake.write_text(f"#!/bin/sh\necho \"$*\" >> {calls}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("ULTRON_HUB_HERMES_BIN", str(fake))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    yield calls
    if calls.exists() and not request.node.get_closest_marker("sends_telegram"):
        pytest.fail(f"test sent a real Telegram message (hermes called: {calls.read_text()[:200]!r})", pytrace=False)


@pytest.fixture
def make_fake_claude(tmp_path):
    """Build an executable fake `claude` that records its argv and stdin, then runs `body`.

    `body` is Python code; it can use `out(obj)` to print JSON on stdout.
    """
    def factory(body: str, name: str = "claude") -> Path:
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / name
        argv_file = bin_dir / "argv.json"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            f"open({str(argv_file)!r}, 'w').write(json.dumps({{'argv': sys.argv, 'cwd': os.getcwd(), "
            "'env': dict(os.environ), 'stdin': sys.stdin.read()}))\n"
            "def out(obj):\n"
            "    sys.stdout.write(json.dumps(obj))\n"
            + textwrap.dedent(body)
        )
        script.chmod(0o755)
        return script
    return factory


@pytest.fixture
def read_argv(tmp_path):
    def reader() -> dict:
        return json.loads((tmp_path / "fakebin" / "argv.json").read_text())
    return reader


def write_task(tasks_dir: Path, name: str, task_yaml: str, prompt: str = "Say hi", schema: dict | None = PING_SCHEMA):
    d = tasks_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.yaml").write_text(textwrap.dedent(task_yaml))
    (d / "prompt.md").write_text(prompt)
    if schema is not None:
        (d / "output.schema.json").write_text(json.dumps(schema))
    return d


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Throwaway project root with settings.yaml and a ping task; env points the CLI at it."""
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "config" / "settings.yaml").write_text("default_model: sonnet\ndefault_timeout_s: 30\n")
    write_task(root / "tasks", "ping", "name: ping\nmodel: haiku\ntimeout_s: 10\n")
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(root))
    monkeypatch.delenv("ULTRON_HUB_CLAUDE_BIN", raising=False)
    from ultron_hub import envoi
    monkeypatch.setattr(envoi, "MODE_FILE", "var/envoi-mode")  # the throwaway root holds its own file
    return root


@pytest.fixture(autouse=True)
def hook_commit_installe(monkeypatch):
    """The hook-commit reflex (todo #415) never reads the real git config: the hook counts as installed."""
    from ultron_hub.health import reflexes
    monkeypatch.setattr(reflexes, "inspecter_hook", lambda root, run=None: {"ok": True, "missing": False, "reason": "installé"})
    monkeypatch.setattr(reflexes, "inspecter_hooks",
                        lambda root, repos=None, run=None: {"ok": True, "missing": False, "reason": "installé"})


@pytest.fixture(autouse=True)
def video_skill_propre(monkeypatch):
    """The video-skill reflex (todo #476) never reads the real hub log: no recent failure."""
    from ultron_hub.health import reflexes
    monkeypatch.setattr(reflexes, "inspecter_video", lambda root, now=None: {"ok": True, "stale": False, "reason": "ok"})


@pytest.fixture(autouse=True)
def session_sans_claude_propre(monkeypatch):
    """The no-Claude-session reflex (todo #537) never reads the real common journal: no empty session."""
    from ultron_hub.health import reflexes
    monkeypatch.setattr(reflexes, "inspecter_session_sans_claude",
                        lambda settings=None, now=None, rows=None: {"ok": True, "stale": False, "reason": "ok"})


@pytest.fixture(autouse=True)
def merge_check_propre(monkeypatch):
    """The merge-check-orphelin reflex (todo #395) never reads the real var/merge-check: nothing is orphaned."""
    from ultron_hub.health import reflexes
    monkeypatch.setattr(reflexes, "inspecter_merge_check",
                        lambda root, run=None, now=None: {"ok": True, "stale": False, "reason": "aucun"})


@pytest.fixture(autouse=True)
def close_key_path(tmp_path, monkeypatch):
    """Never touch the real ~/.config/ultron-hub/close.key: the CLI and the plugin use a throwaway one."""
    from ultron_hub.todo import closing
    path = tmp_path / "close-key" / "close.key"
    monkeypatch.setattr(closing, "KEY_PATH", path)
    try:
        from hermes_plugin import close_key
    except ModuleNotFoundError:  # kit export: the plugin is not part of it
        close_key = None
    if close_key is not None:
        monkeypatch.setattr(close_key, "KEY_PATH", path)
    monkeypatch.delenv(closing.TOKEN_ENV, raising=False)
    return path


@pytest.fixture(autouse=True)
def envoi_mode_file(tmp_path, monkeypatch):
    """Never read the real var/envoi-mode (untracked, 'direct' once the bot is live): envoi.py and ultron-user
    look at an absent file in tmp_path. A test wanting 'direct' writes the returned path."""
    from ultron_hub import envoi, user_actions
    path = tmp_path / "envoi-mode-absent" / "envoi-mode"
    monkeypatch.setattr(envoi, "MODE_FILE", str(path))  # absolute: project_root() / absolute = absolute
    try:
        monkeypatch.setattr(user_actions._spec(), "ENVOI_MODE_FILE", path)
    except FileNotFoundError:  # kit export: deploy/user/ultron-user is not part of it
        pass
    return path


@pytest.fixture(autouse=True)
def esprit_dir(tmp_path, monkeypatch):
    """Never touch the real ~/ultron/esprit: the folder is a throwaway one (absent until a test creates it)."""
    path = tmp_path / "esprit"
    monkeypatch.setenv("ULTRON_ESPRIT_DIR", str(path))
    monkeypatch.delenv("ULTRON_HUB_JOB", raising=False)
    return path


@pytest.fixture(autouse=True)
def savoir_dir(tmp_path, monkeypatch):
    """Never touch the real ~/ultron/savoir: the notebook is a throwaway folder (absent until a fiche is filed)."""
    path = tmp_path / "savoir"
    monkeypatch.setenv("ULTRON_SAVOIR_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def prompts_dir(tmp_path, monkeypatch):
    """Never read the real ~/notes/prompt (the subjects a todo may be filed under): a throwaway folder (absent until a test uses it)."""
    path = tmp_path / "prompts-chantiers"
    monkeypatch.setenv("ULTRON_PROMPTS_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def escouades_dir(tmp_path, monkeypatch):
    """Never read or write the real ~/.local/state/ultron-hub/escouades: a throwaway folder (absent until a test uses it)."""
    path = tmp_path / "escouades-etat"
    monkeypatch.setenv("ULTRON_ESCOUADES_DIR", str(path))
    # pytest run from a squad's session: its subject must not file every test todo in a chantier (todo #105)
    monkeypatch.delenv("ULTRON_ESCOUADE_SUJET", raising=False)
    return path


@pytest.fixture(autouse=True)
def no_real_hook_telegram(monkeypatch):
    """The squad context gauge (hooks.send_telegram) never runs a real `hermes send` from the suite (todo #175)."""
    from ultron_hub.escouade import hooks
    sent = []
    monkeypatch.setattr(hooks, "send_telegram", lambda text: sent.append(text) or True)
    return sent


@pytest.fixture(autouse=True)
def no_real_send(request, monkeypatch):
    """No test sends a real `hermes send` (a profile proposal reached the user's Telegram, todo #89):
    `todo.service.send_message` records instead. Mark `real_send` to test that function itself."""
    if request.node.get_closest_marker("real_send"):
        return []
    from ultron_hub.todo import service
    sent = []
    monkeypatch.setattr(service, "send_message", lambda text, target, binary, timeout_s=60, sujet=None, boutons=None,
                        chantier=None, thread=None: sent.append(text))
    return sent


@pytest.fixture(autouse=True)
def no_real_suite(request, monkeypatch):
    """The test-maintenance employee (todo #144) never starts the real suite from the suite: tests give it a fake runner."""
    from ultron_hub.menage import tests_audit

    def refuse(*args, **kwargs):
        raise AssertionError("the real test suite must not be started from a test: pass a fake runner")

    monkeypatch.setattr(tests_audit, "run_pytest", refuse)


@pytest.fixture(autouse=True)
def no_real_backup_home(tmp_path, monkeypatch):
    """Never read the real healthchecks URLs or restic password: no test pings healthchecks.io for real."""
    from ultron_hub.backup import hc
    monkeypatch.setattr(hc, "real_home", lambda: tmp_path / "backup-home")


@pytest.fixture(autouse=True)
def no_real_restic(monkeypatch):
    """No test runs the real restic (todo #161): ultron-user's restic calls raise (the reflexes' lock inspection
    turns that into "no diagnosis"); a test gives its own fake."""
    from ultron_hub import user_actions

    def refuse(*args, **kwargs):
        raise AssertionError("real restic call in a test")
    try:
        monkeypatch.setattr(user_actions._spec(), "_restic_run", refuse)
    except FileNotFoundError:  # kit export: deploy/user/ultron-user is not part of it
        pass


@pytest.fixture(autouse=True)
def no_real_pexels(tmp_path, monkeypatch):
    """Never read the real Pexels key nor call the network: stock.KEY_PATH points to a throwaway (absent) file and
    any real HTTP call fails the test."""
    from ultron_hub.montage import stock
    monkeypatch.setattr(stock, "KEY_PATH", tmp_path / "pexels-home" / "pexels.key")

    def no_network(*args, **kwargs):
        raise AssertionError("real network call in a test")
    monkeypatch.setattr(stock._OPENER, "open", no_network)


@pytest.fixture(autouse=True)
def no_real_clair(monkeypatch):
    """`todo add` asks haiku for the "En clair" line and the `--long` title: never the real claude in the suite.

    Tests of the generation itself use the function imported at module level (before this patch).
    """
    from ultron_hub.common.errors import UltronHubError
    from ultron_hub.todo import summarizer

    def refuse(title, notes=""):
        raise UltronHubError("llm_clair is disabled in tests")
    monkeypatch.setattr(summarizer, "llm_clair", refuse)

    def refuse_title(text):
        raise UltronHubError("llm_title is disabled in tests")
    monkeypatch.setattr(summarizer, "llm_title", refuse_title)


@pytest.fixture(autouse=True)
def no_real_direct_send(monkeypatch):
    """No test reaches the real Telegram Bot API (todo #194): the direct sending point's HTTP call fails the test,
    the token and the sending mode are cleared; a test gives its own fake `envoi._urlopen`."""
    from ultron_hub import envoi

    def no_network(*args, **kwargs):
        raise AssertionError("real Telegram call in a test")
    monkeypatch.setattr(envoi, "_urlopen", no_network)
    for name in (envoi.TOKEN_ENV, envoi.MODE_ENV, *envoi.CHAT_ENVS):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_real_push(tmp_path, monkeypatch):
    """No test touches the real VAPID key or subscriptions, nor reaches a push service (todo #240): both paths go to
    tmp_path, and any push HTTP call is recorded then refused. A test gives its own fake `push._urlopen`."""
    from ultron_hub.dashboard import push
    calls = []
    monkeypatch.setattr(push, "KEY_PATH", tmp_path / "vapid.key")
    monkeypatch.setattr(push, "STORE_DIR", tmp_path / "dashboard-push")

    def no_network(req, timeout):
        calls.append(req.full_url)
        raise AssertionError("real push call in a test")
    monkeypatch.setattr(push, "_urlopen", no_network)
    return calls


@pytest.fixture(autouse=True)
def notifs_dir(tmp_path, monkeypatch):
    """Never write the real var/notifs (todo #276): every `envoi.envoyer` of the suite would file a notification there."""
    from ultron_hub import notifs
    path = tmp_path / "notifs-etat"
    monkeypatch.setattr(notifs, "dossier", lambda settings=None: path)
    return path


@pytest.fixture(autouse=True)
def recus_dir(tmp_path, monkeypatch):
    """Never write the real var/recus (todo #293): the files sent by 📎 joindre land in a throwaway folder."""
    from ultron_hub import recus
    path = tmp_path / "recus-etat"
    monkeypatch.setattr(recus, "dossier", lambda settings=None: path)
    return path


@pytest.fixture(autouse=True)
def comptes_paths(tmp_path, monkeypatch):
    """Never read or write the real ~/.claude/.credentials.json nor ~/.config/ultron-hub/comptes (todo #263): both are
    throwaway paths (absent until a test writes them), and the usage and profile calls of `compte` fail the test."""
    from ultron_hub.compte import store
    active = tmp_path / "claude-home" / ".credentials.json"
    monkeypatch.setattr(store, "ACTIVE", active)
    monkeypatch.setattr(store, "DIR", tmp_path / "comptes")

    def no_network(token):
        raise AssertionError("real Anthropic call in a test")
    monkeypatch.setattr(store, "fetch_usage", no_network)
    monkeypatch.setattr(store, "fetch_profile", no_network)
    return active


@pytest.fixture(autouse=True)
def no_real_dashboard_session_key(tmp_path, monkeypatch):
    """Never create nor read the real ~/.config/ultron-hub/dashboard-session.key (todo #267): a throwaway path."""
    from ultron_hub.dashboard import session
    path = tmp_path / "dashboard-session" / "dashboard-session.key"
    monkeypatch.setattr(session, "KEY_PATH", path)
    return path


@pytest.fixture(autouse=True)
def no_real_background_unit(monkeypatch):
    """A background action of the dashboard (todo #267) never starts a real systemd-run unit: the job runs at once,
    in-process, with the same Actions (fake CLI, journal), as the runner would. Launched jobs are recorded."""
    from ultron_hub.dashboard import actions, fond
    from ultron_hub.todo import closing
    launched = []

    def run_now(self, settings, job, extra, delay_s):
        launched.append(job)
        fond.run_job(settings, job, self, extra.get(closing.TOKEN_ENV))
    monkeypatch.setattr(actions.Actions, "_launch", run_now)
    return launched


@pytest.fixture(autouse=True)
def _no_real_proc_for_tmux(tmp_path_factory, monkeypatch):
    """Todo #354: the tmux health control never reads the real /proc in tests (an empty one: no tmux server)."""
    from ultron_hub.health import reflexes
    monkeypatch.setattr(reflexes, "PROC_DIR", tmp_path_factory.mktemp("proc"))


@pytest.fixture(autouse=True)
def doc_hermes_propre(monkeypatch):
    """The doc-hermes reflex (todo #257) never reads the real systemd state nor the real docs: nothing conflicts."""
    from ultron_hub.health import reflexes
    monkeypatch.setattr(reflexes, "inspecter_doc_hermes",
                        lambda root, run=None, files=None: {"ok": True, "stale": False, "reason": "aucune"})
