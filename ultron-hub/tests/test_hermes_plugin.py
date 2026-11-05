import asyncio
import sys

import pytest

from hermes_plugin import allowed_user_ids
from hermes_plugin.hub_client import HubClient
from hermes_plugin.todo_flow import TodoFlow, cb
from cli_contract import check_argv

CHAT = 1185120603


def ok(data=None, summary=""):
    return {"status": "ok", "summary": summary, "data": data, "error": None}


def task(i, **kw):
    return {"id": i, "title": f"Tâche {i}", "priority": "normal", "due_date": None, "status": "todo",
            "badges": "", "overdue": False, **kw}


class FakeClient:
    def __init__(self, tasks=None, fail=False):
        self.calls = []
        self.fail = fail
        self.tasks = [task(4, title="Sécuriser SSH", priority="high")] if tasks is None else tasks

    async def call(self, *args, env=None):
        check_argv(args)
        self.calls.append(args)
        if self.fail:
            return {"status": "error", "summary": "", "data": None, "error": "boom"}
        assert args[:2] == ("todo", "board"), args
        return ok({"tasks": self.tasks, "open": len(self.tasks), "overdue": 1, "category": None,
                   "filter": args[3], "fond": 0, "categories": ["perso", "serveur"]})


def run(coro):
    return asyncio.run(coro)


def callbacks(screen):
    return [data for row in screen.buttons for _, data in row]


# -- /todo of the rescue bot: a short read-only list (todo #463) -------------------------------

def test_todo_lists_open_tasks_as_text_and_points_to_the_dashboard():
    client = FakeClient()
    screen = run(TodoFlow(client).on_command(CHAT))
    assert "1 ouverte(s)" in screen.text and "#4 Sécuriser SSH" in screen.text and "dashboard" in screen.text
    assert client.calls == [("todo", "board", "--filter", "open")]
    assert callbacks(screen) == ["hub:home"]  # no card, no form, no action button


def test_list_pagination():
    flow = TodoFlow(FakeClient([task(i) for i in range(1, 26)]))
    first = run(flow.on_command(CHAT))
    assert "page 1/3" in first.text and cb("list", "1") in callbacks(first) and cb("list", "-1") not in callbacks(first)
    last = run(flow.on_callback(CHAT, cb("list", "2")))
    assert "page 3/3" in last.text and cb("list", "1") in callbacks(last)
    assert run(flow.on_callback(CHAT, cb("list", "99"))).text.count("#") == 5  # clamped to the last page


def test_list_text_fits_a_message():
    screen = run(TodoFlow(FakeClient([task(i, title="x" * 200) for i in range(1, 12)])).on_command(CHAT))
    assert len(screen.text) <= 4096


def test_list_empty_and_failure():
    assert "Rien ici" in run(TodoFlow(FakeClient([])).on_command(CHAT)).text
    screen = run(TodoFlow(FakeClient(fail=True)).on_command(CHAT))
    assert "boom" in screen.text and ("🔄 Réessayer", cb("list", "0")) in screen.buttons[0]


@pytest.mark.parametrize("data", ["hub:todo:done:4:a.0", "hub:todo:add", "hub:todo:list:x", "hub:merge:4", ""])
def test_old_callbacks_only_show_the_list(data):
    screen = run(TodoFlow(FakeClient()).on_callback(CHAT, data))
    assert "Mes tâches" in screen.text and callbacks(screen) == ["hub:home"]


def test_callback_data_fits_telegram_limit():
    assert len(cb("list", "9999").encode()) <= 64
    with pytest.raises(AssertionError):
        cb("list", "x" * 64)


def test_allowed_user_ids(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1185120603, 42,abc")
    assert allowed_user_ids() == {1185120603, 42}
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
    assert allowed_user_ids() == frozenset()


@pytest.mark.parametrize("raw,expected", [("7, 42 # moi", {7, 42}), ("7 #x", {7}), ("7,42", {7, 42}), ("", set()),
                                          ("# seul", set())])
def test_allowed_user_ids_comment(monkeypatch, raw, expected):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", raw)
    assert allowed_user_ids() == expected


# --- HubClient ---------------------------------------------------------------

def fake_cli(tmp_path, body):
    path = tmp_path / "ultron-hub"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n{body}\n")
    path.chmod(0o755)
    return path


def test_client_ok_and_minimal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    cli = fake_cli(tmp_path, "print(json.dumps({'status': 'ok', 'data': {'argv': sys.argv[1:], "
                             "'env': sorted(os.environ)}}))")
    out = run(HubClient(cli).call("todo", "list"))
    assert out["status"] == "ok" and out["data"]["argv"] == ["todo", "list"]
    assert "TELEGRAM_BOT_TOKEN" not in out["data"]["env"]


def test_client_errors(tmp_path):
    out = run(HubClient(tmp_path / "missing").call("todo", "list"))
    assert out["status"] == "error" and "introuvable" in out["error"]
    out = run(HubClient(fake_cli(tmp_path, "print('not json')")).call("todo", "list"))
    assert out["status"] == "error" and "illisible" in out["error"]
    out = run(HubClient(fake_cli(tmp_path, "time.sleep(5)"), timeout_s=0.5).call("todo", "list"))
    assert out["status"] == "error" and "délai" in out["error"]



# --- plugin isolation: Hermes runs its own Python, without ultron_hub ------------------------

def test_plugin_imports_without_ultron_hub(tmp_path):
    """-S drops site-packages (where ultron_hub is installed), like Hermes' interpreter."""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import hermes_plugin, hermes_plugin.todo_flow, hermes_plugin.health_flow\n"
            "import hermes_plugin.usage_flow, hermes_plugin.atoi_flow, hermes_plugin.close_key\n"
            "assert 'ultron_hub' not in sys.modules\n") % str(root)
    proc = subprocess.run([sys.executable, "-I", "-S", "-c", code], cwd=tmp_path,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr




class FakeApp:
    def __init__(self, running_after=0):
        self.checks, self.running_after, self.sent = 0, running_after, []
        app = self

        class Bot:
            async def send_message(self, chat_id, text):
                app.sent.append((chat_id, text))
                if chat_id == 2:
                    raise RuntimeError("chat not found")

        self.bot = Bot()

    @property
    def running(self):
        self.checks += 1
        return self.checks > self.running_after


def test_reconnect_message_once_when_running(monkeypatch):
    import hermes_plugin
    monkeypatch.setitem(hermes_plugin._announce, "sent", False)
    app = FakeApp(running_after=3)
    assert run(hermes_plugin.announce_ready(app, {1, 2}, step_s=0))
    assert app.sent == [(1, "✅ Bot reconnecté."), (2, "✅ Bot reconnecté.")]  # a failed chat does not stop
    again = FakeApp()
    assert not run(hermes_plugin.announce_ready(again, {1}, step_s=0)) and again.sent == []


def test_reconnect_message_gives_up_on_a_dead_client(monkeypatch):
    import hermes_plugin
    monkeypatch.setitem(hermes_plugin._announce, "sent", False)
    dead = FakeApp(running_after=10 ** 6)
    assert not run(hermes_plugin.announce_ready(dead, {1}, wait_s=0.05, step_s=0.01)) and dead.sent == []
    assert not hermes_plugin._announce["sent"]  # the rebuilt client can still announce


def test_silent_when_hermes_announces_itself(tmp_path, monkeypatch):
    import hermes_plugin
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert not hermes_plugin.hermes_will_announce()
    (tmp_path / ".restart_pending.json").write_text("{}")
    assert hermes_plugin.hermes_will_announce()


