"""Signed closing (todo #19): only the Telegram button or a human at a terminal closes a todo."""

import asyncio
import io
import json
import os
import re
import time

import pytest

from hermes_plugin import close_key
from ultron_hub import branches, cli
from ultron_hub.todo import closing
from ultron_hub.todo.store import TodoStore

from test_branches import branch, no_bwrap, repo, sh, status  # noqa: F401  (repo, no_bwrap: fixtures)


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture
def todos(project, capsys):
    for title in ("un", "deux"):
        run_cli(capsys, "todo", "add", "--title", title)
    return project


def press(monkeypatch, action, todo_id, **kw):
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token(action, todo_id, **kw))


def history(project, todo_id=1):
    with TodoStore(project / "var" / "ultron.db") as store:
        return [h for h in store.history(todo_id) if h["action"] == "status"]


def task_status(project, todo_id=1):
    with TodoStore(project / "var" / "ultron.db") as store:
        return store.get(todo_id).status


# --- CLI --------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["done", "cancel"])
def test_unsigned_close_is_refused(todos, capsys, cmd):
    code, out = run_cli(capsys, "todo", cmd, "1")
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "--i-am-human" in out["error"]
    assert task_status(todos) == "todo" and history(todos) == []


def test_signed_close_is_accepted_and_traced(todos, capsys, monkeypatch, close_key_path):
    press(monkeypatch, "done", 1)
    code, out = run_cli(capsys, "todo", "done", "1")
    assert code == 0 and out["data"]["status"] == "done"
    [event] = history(todos)
    assert event["details"]["by"] == "telegram-button" and event["details"]["from"] == "todo"
    assert re.fullmatch(r"[0-9a-f]{16}", event["details"]["proof"])
    assert os.stat(close_key_path).st_mode & 0o777 == 0o600  # created by the plugin side, 0600


@pytest.mark.parametrize("forge", [
    lambda: "pas-un-jeton",
    lambda: f"{int(time.time())}.{'0' * 64}",                         # wrong signature
    lambda: close_key.token("done", 1, key=b"x" * 64),                # another key
    lambda: close_key.token("cancel", 1),                             # another action
    lambda: close_key.token("done", 2),                               # another todo
    lambda: close_key.token("done", 1, now=time.time() - 3600),       # expired
    lambda: close_key.token("done", 1, now=time.time() + 600),        # from the future
    lambda: close_key.token("done", 1) + "\n",                        # fullmatch
])
def test_bad_tokens_are_refused(todos, capsys, monkeypatch, forge):
    close_key.load_or_create()
    monkeypatch.setenv(closing.TOKEN_ENV, forge())
    code, out = run_cli(capsys, "todo", "done", "1")
    assert code == 1 and "jeton" in out["error"] and task_status(todos) == "todo"


def test_token_cannot_be_replayed(todos, capsys, monkeypatch):
    token = close_key.token("done", 1)
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    assert run_cli(capsys, "todo", "done", "1")[0] == 0
    press(monkeypatch, "reopen", 1, now=time.time() - 5)
    assert run_cli(capsys, "todo", "reopen", "1")[0] == 0
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    code, out = run_cli(capsys, "todo", "done", "1")
    assert code == 1 and "déjà utilisé" in out["error"] and task_status(todos) == "todo"


def test_reopen_unsigned_is_allowed_but_traced(todos, capsys, monkeypatch):
    press(monkeypatch, "cancel", 1)
    assert run_cli(capsys, "todo", "cancel", "1")[0] == 0
    monkeypatch.delenv(closing.TOKEN_ENV)
    assert run_cli(capsys, "todo", "reopen", "1")[0] == 0
    assert [e["details"]["by"] for e in history(todos)] == ["telegram-button", "cli"]
    monkeypatch.setenv(closing.TOKEN_ENV, "faux")  # a token, if given, must be valid
    assert run_cli(capsys, "todo", "reopen", "1")[0] == 1


def test_key_must_be_private(todos, capsys, monkeypatch, close_key_path):
    token = close_key.token("done", 1)  # creates the key
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    os.chmod(close_key_path, 0o644)
    code, out = run_cli(capsys, "todo", "done", "1")
    assert code == 1 and "0600" in out["error"]
    os.chmod(close_key_path, 0o600)
    real = close_key_path.with_name("real.key")
    close_key_path.rename(real)
    close_key_path.symlink_to(real)
    assert run_cli(capsys, "todo", "done", "1")[0] == 1  # O_NOFOLLOW
    close_key_path.unlink()
    code, out = run_cli(capsys, "todo", "done", "1")
    assert code == 1 and "absente" in out["error"] and task_status(todos) == "todo"


def test_key_path_ignores_home(tmp_path):
    """`HOME=/tmp/x ultron-hub ...` must not point the CLI (or the plugin) at a key an agent wrote."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = ("import sys; sys.path[:0] = [%r, %r]\n"
            "from hermes_plugin import close_key; from ultron_hub.todo import closing\n"
            "print(close_key.KEY_PATH, closing.KEY_PATH)") % (str(root), str(root / "src"))
    out = subprocess.run([sys.executable, "-I", "-c", code], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
                         capture_output=True, text=True, check=True).stdout.split()
    assert len(out) == 2 and out[0] == out[1] and not out[0].startswith(str(tmp_path))
    assert out[0].endswith("/.config/ultron-hub/close.key")


# --- terminal path ------------------------------------------------------------------------------

class Tty(io.StringIO):
    def isatty(self):
        return True


def human(monkeypatch, answer=None, ancestors=("-bash", "sshd: ultron@pts/0")):
    monkeypatch.setattr("sys.stdin", Tty())
    monkeypatch.setattr(closing, "_ancestors", lambda: list(ancestors))
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return answer if answer is not None else re.search(r"Tape (\d{4})", prompt)[1]
    monkeypatch.setattr(closing, "_ask", ask)
    return prompts


def test_human_needs_a_tty(todos, capsys):
    code, out = run_cli(capsys, "todo", "done", "1", "--i-am-human")  # pytest's stdin is not a TTY
    assert code == 1 and "TTY" in out["error"] and task_status(todos) == "todo"


def test_human_confirms_with_the_code(todos, capsys, monkeypatch):
    prompts = human(monkeypatch)
    code, out = run_cli(capsys, "todo", "done", "1", "--i-am-human")
    assert code == 0 and out["data"]["status"] == "done" and "#1 un" in prompts[0]
    assert history(todos)[0]["details"]["by"] == "cli-human"


def test_human_wrong_code_is_refused(todos, capsys, monkeypatch):
    human(monkeypatch, answer="oui")
    code, out = run_cli(capsys, "todo", "cancel", "1", "--i-am-human")
    assert code == 1 and "incorrecte" in out["error"] and task_status(todos) == "todo"


@pytest.mark.parametrize("parent", [
    "/home/exemple/.local/bin/claude --model opus",
    "claude",
    "/home/exemple/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run",
    "/home/exemple/.local/bin/hermes chat",
])
def test_human_refused_under_an_agent(todos, capsys, monkeypatch, parent):
    prompts = human(monkeypatch, ancestors=("bash", parent, "systemd --user"))
    code, out = run_cli(capsys, "todo", "done", "1", "--i-am-human")
    assert code == 1 and "agent" in out["error"] and prompts == [] and task_status(todos) == "todo"


def test_agent_pattern_spares_ordinary_paths():
    for line in ("-bash", "/usr/bin/python3 /home/exemple/.claude/x.py", "tmux new -s hermesish", "vim claudette.txt"):
        assert not closing.AGENT_PROCESS_RE.search(line), line


# --- plugin signing ---------------------------------------------------------------------------









def test_hub_client_passes_extra_env(tmp_path):
    from hermes_plugin.hub_client import HubClient
    cli_path = tmp_path / "ultron-hub"
    cli_path.write_text("#!/usr/bin/env python3\nimport json, os\n"
                        f"print(json.dumps({{'status': 'ok', 'data': os.environ.get({closing.TOKEN_ENV!r})}}))\n")
    cli_path.chmod(0o755)
    out = asyncio.run(HubClient(cli_path).call("todo", "done", "4", env={closing.TOKEN_ENV: "t"}))
    assert out["data"] == "t"
    assert asyncio.run(HubClient(cli_path).call("todo", "list"))["data"] is None


def test_plugin_and_cli_sign_alike():
    key = b"k" * 64
    assert close_key.token("merge", 7, key=key, now=1000) == closing.sign(key, "merge", 7, 1000)
    assert close_key.TOKEN_ENV == closing.TOKEN_ENV


# --- branch merge ---------------------------------------------------------------------------

def test_branch_merge_requires_the_button(repo, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    branch(repo, "todo-5/feat")
    before = sh(repo.root, "rev-parse", "main")
    code, out = run_cli(capsys, "branch", "merge", "5")
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "branch merge 5" in out["error"]
    press(monkeypatch, "done", 5)  # a "done" token is not a "merge" token
    assert run_cli(capsys, "branch", "merge", "5")[0] == 1
    assert sh(repo.root, "rev-parse", "main") == before and status(repo) == "todo"

    real = branches.merge_and_close
    monkeypatch.setattr(branches, "merge_and_close", lambda *a, **kw: real(*a, restart=lambda: None, **kw))
    press(monkeypatch, "merge", 5)
    code, out = run_cli(capsys, "branch", "merge", "5")
    assert code == 0 and out["data"]["merged"] and status(repo) == "done"
    with TodoStore(repo.todo_db) as todos:
        [event] = [h for h in todos.history(5) if h["action"] == "status"]
    assert event["details"]["by"] == "telegram-merge"
