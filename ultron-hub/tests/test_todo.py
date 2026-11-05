import json
import sqlite3
import sys
from datetime import date

import pytest

from hermes_plugin import close_key
from ultron_hub import cli
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.todo import service
from ultron_hub.todo.store import TaskNotFound, TodoStore

ENVELOPE_KEYS = {"status", "command", "summary", "data", "error"}
SAT = date(2026, 10, 10)  # a Saturday


# --- store -------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    with TodoStore(tmp_path / "todo.db") as s:
        yield s


def test_add_get_and_history(store):
    t = store.add("  Sécuriser   SSH ", category="serveur", priority="high", due_date="2026-10-12")
    assert t.id == 1 and t.title == "Sécuriser SSH" and t.status == "todo"
    assert store.get(1) == t
    assert [e["action"] for e in store.history(1)] == ["add"]


def test_status_changes_are_logged(store):
    store.add("a", category="perso")
    done = store.set_status(1, "done")
    assert done.status == "done" and done.done_at
    reopened = store.set_status(1, "todo")
    assert reopened.done_at is None
    assert [e["action"] for e in store.history(1)] == ["add", "status", "status"]


def test_list_orders_by_due_then_priority(store):
    store.add("no due low", category="perso", priority="low")
    store.add("later", nature="chantier", category="perso", due_date="2026-10-20")
    store.add("sooner", category="perso", due_date="2026-10-11")
    store.add("no due high", category="perso", priority="high")
    store.add("finished", category="perso")
    store.set_status(5, "done")
    assert [t.title for t in store.list()] == ["sooner", "later", "no due high", "no due low"]
    assert [t.title for t in store.list(status="done")] == ["finished"]
    assert len(store.list(status=None)) == 5


def test_validation(store):
    for bad in ("", "   ", "x" * 201):
        with pytest.raises(InvalidRequestError):
            store.add(bad, category="perso")
    with pytest.raises(InvalidRequestError):
        store.add("a", category="perso", priority="urgent")
    with pytest.raises(TaskNotFound):
        store.get(42)
    store.add("a", category="perso")
    with pytest.raises(InvalidRequestError):
        store.update(1, status="done")  # status goes through set_status only


def test_update_records_before_and_after(store):
    store.add("a", category="perso")
    store.update(1, title="b", priority="high")
    edit = store.history(1)[-1]
    assert edit["action"] == "edit" and edit["details"]["title"] == {"from": "a", "to": "b"}


def test_database_constraints_hold_even_without_the_api(tmp_path):
    TodoStore(tmp_path / "t.db").close()
    conn = sqlite3.connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tasks (title, priority, category, created_at, updated_at)"
                     " VALUES ('x', 'urgent', 'perso', 'now', 'now')")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


# --- service -----------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, None), ("none", None), ("aucune", None),
    ("today", "2026-10-10"), ("aujourd'hui", "2026-10-10"),
    ("tomorrow", "2026-10-11"), ("demain", "2026-10-11"),
    ("week", "2026-10-11"), ("+3d", "2026-10-13"), ("2026-12-01", "2026-12-01"),
    ("20/10", "2026-10-20"), ("10/10", "2026-10-10"), ("1/1", "2027-01-01"), ("20.10.2028", "2028-10-20"),
])
def test_parse_due(value, expected):
    assert service.parse_due(value, SAT) == expected


def test_parse_due_week_from_monday():
    assert service.parse_due("week", date(2026, 10, 5)) == "2026-10-11"


@pytest.mark.parametrize("bad", ["next friday", "2026-13-01", "+d", "31/02", "20/13", "20/10/26"])
def test_parse_due_rejects(bad):
    with pytest.raises(InvalidRequestError):
        service.parse_due(bad, SAT)


def test_parse_priority_and_category():
    assert service.parse_priority("Haute") == "high"
    with pytest.raises(InvalidRequestError):
        service.parse_priority("urgent")
    assert service.parse_category("Serveur", ("perso", "serveur")) == "serveur"
    with pytest.raises(InvalidRequestError):
        service.parse_category("divers", ("perso", "serveur"))


def test_digest_groups(store):
    store.add("late", nature="chantier", category="perso", due_date="2026-10-08")
    store.add("now", nature="chantier", category="perso", due_date="2026-10-10")
    store.add("big", nature="chantier", category="perso", priority="high")
    store.add("later", nature="chantier", category="perso", due_date="2026-10-30")
    d = service.build_digest(store.list(), SAT)
    assert (d["overdue"], d["today"], d["high_undated"], d["open"]) == (1, 1, 1, 4)
    assert "samedi 10/10" in d["text"] and "#1 late · 08/10" in d["text"]


def test_digest_empty():
    assert "Rien en retard" in service.build_digest([], SAT)["text"]


def test_digest_atoi_line():
    d = service.build_digest([], SAT, atoi_counts={"🔀": 2, "✅": 1, "❓": 0})
    lines = d["text"].splitlines()
    assert lines[2] == "📥 À toi : 3 choses attendent ton ✅ (🔀 2 · ✅ 1) → /start" and d["atoi"] == 3
    assert "1 chose attend ton ✅ (❓ 1)" in service.build_digest([], SAT, atoi_counts={"❓": 1})["text"]
    for counts in (None, {}, {"🔀": 0}):
        assert "À toi" not in service.build_digest([], SAT, atoi_counts=counts)["text"]


def test_cli_digest_counts_atoi_by_icon(todo_project, monkeypatch, capsys):
    from ultron_hub.chantier import core as chantier_core
    items = [{"kind": "fusionner"}, {"kind": "fusionner"}, {"kind": "livree"}, {"kind": "livree"}, {"kind": "question"}]
    monkeypatch.setattr(chantier_core, "atoi", lambda settings, src, chantier=None: {"items": items})
    code, out = run_todo(capsys, "digest")
    assert code == 0 and out["data"]["atoi"] == 5
    assert "📥 À toi : 5 choses attendent ton ✅ (❓ 1 · 🔀 2 · ✅ 2) → /start" in out["data"]["text"]


def test_cli_digest_survives_unreadable_atoi(todo_project, monkeypatch, capsys):
    from ultron_hub.chantier import core as chantier_core

    def boom(*args, **kwargs):
        raise OSError("illisible")
    monkeypatch.setattr(chantier_core, "atoi", boom)
    code, out = run_todo(capsys, "digest")
    assert code == 0 and out["data"]["atoi"] == 0 and "À toi" not in out["data"]["text"]


# --- CLI ---------------------------------------------------------------------

@pytest.fixture
def todo_project(project, monkeypatch):
    monkeypatch.delenv("ULTRON_HUB_HERMES_BIN", raising=False)
    return project


def run_todo(capsys, *argv):
    code = cli.main(["todo", *argv])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == ENVELOPE_KEYS
    return code, payload


def press(monkeypatch, action, todo_id):
    """What the plugin does on a Telegram button: a fresh signed token in the CLI's environment."""
    monkeypatch.setenv(close_key.TOKEN_ENV, close_key.token(action, todo_id))


def test_cli_add_list_done(todo_project, capsys, monkeypatch):
    code, out = run_todo(capsys, "add", "--title", "Sécuriser SSH", "--priority", "haute",
                         "--category", "serveur", "--due", "2026-10-12", "--source", "telegram")
    assert code == 0 and out["data"]["id"] == 1 and out["data"]["priority"] == "high"
    assert out["data"]["source"] == "telegram"
    assert (todo_project / "var" / "ultron.db").is_file()

    code, out = run_todo(capsys, "list")
    assert code == 0 and [t["title"] for t in out["data"]["tasks"]] == ["Sécuriser SSH"]

    press(monkeypatch, "done", 1)
    code, out = run_todo(capsys, "done", "1")
    assert code == 0 and out["data"]["status"] == "done"
    code, out = run_todo(capsys, "list")
    assert out["data"]["tasks"] == []


def test_cli_default_category_and_meta(todo_project, capsys):
    _, out = run_todo(capsys, "add", "--title", "x")
    assert out["data"]["category"] == "perso"
    _, meta = run_todo(capsys, "meta")
    assert "serveur" in meta["data"]["categories"] and meta["data"]["timezone"] == "Europe/Paris"


def test_cli_errors_are_json(todo_project, capsys):
    code, out = run_todo(capsys, "add", "--title", "x", "--category", "divers")
    assert code == 1 and out["status"] == "error" and "category" in out["error"]
    code, out = run_todo(capsys, "done", "99")
    assert code == 1 and "not found" in out["error"]


def test_cli_edit_and_show(todo_project, capsys):
    run_todo(capsys, "add", "--title", "x")
    code, out = run_todo(capsys, "edit", "1", "--due", "none", "--priority", "low", "--title", "y")
    assert code == 0 and out["data"]["title"] == "y" and out["data"]["due_date"] is None
    _, out = run_todo(capsys, "show", "1")
    assert [h["action"] for h in out["data"]["history"]] == ["add", "edit"]


def _fake_hermes(tmp_path, exit_code=0):
    path = tmp_path / "fakebin" / "hermes"
    path.parent.mkdir(exist_ok=True)
    record = tmp_path / "fakebin" / "hermes_call.json"
    path.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv, 'stdin': sys.stdin.read()}}))\n"
        f"sys.stderr.write('boom')\nsys.exit({exit_code})\n")
    path.chmod(0o755)
    return path, record


@pytest.mark.real_send
def test_cli_digest_send(todo_project, tmp_path, monkeypatch, capsys):
    hermes, record = _fake_hermes(tmp_path)
    monkeypatch.setenv("ULTRON_HUB_HERMES_BIN", str(hermes))
    run_todo(capsys, "add", "--title", "Rédiger le rapport", "--due", "today", "--nature", "chantier")
    code, out = run_todo(capsys, "digest", "--send")
    assert code == 0 and out["data"]["sent"] is True and out["data"]["today"] == 1
    call = json.loads(record.read_text())
    assert call["argv"][1:] == ["send", "--to", "telegram", "--quiet", "--file", "-"]
    assert "Rédiger le rapport" in call["stdin"]


@pytest.mark.real_send
def test_cli_digest_send_failure_is_reported(todo_project, tmp_path, monkeypatch, capsys):
    hermes, _ = _fake_hermes(tmp_path, exit_code=3)
    monkeypatch.setenv("ULTRON_HUB_HERMES_BIN", str(hermes))
    code, out = run_todo(capsys, "digest", "--send")
    assert code == 1 and "exit 3" in out["error"] and "boom" in out["error"]


# --- board (Telegram list) ---------------------------------------------------

def test_board_order_filters_and_counts(todo_project, capsys, monkeypatch):
    monkeypatch.setattr(service, "today_in", lambda tz: SAT)
    for args in (("plus tard", "low", "2026-10-20"), ("haute sans date", "high", "none"),
                 ("en retard", "low", "2026-10-08"), ("aujourd'hui", "normal", "2026-10-10"),
                 ("normale", "normal", "none")):
        run_todo(capsys, "add", "--title", args[0], "--priority", args[1], "--due", args[2],
                 "--nature", "chantier")
    run_todo(capsys, "add", "--title", "finie", "--category", "serveur")
    press(monkeypatch, "done", 6)
    run_todo(capsys, "done", "6")
    code, out = run_todo(capsys, "board")
    d = out["data"]
    assert code == 0 and d["open"] == 5 and d["overdue"] == 1
    assert (d["today"], d["high"], d["branches"]) == (1, 1, 0) and "missions_active" not in d
    assert [t["title"] for t in d["tasks"]] == ["en retard", "aujourd'hui", "haute sans date", "plus tard", "normale"]
    assert d["tasks"][0]["overdue"] and d["tasks"][1]["today"] and d["tasks"][0]["badges"] == ""
    _, out = run_todo(capsys, "board", "--filter", "urgent")
    assert [t["title"] for t in out["data"]["tasks"]] == ["en retard", "aujourd'hui"]
    _, out = run_todo(capsys, "board", "--filter", "closed")
    assert [t["title"] for t in out["data"]["tasks"]] == ["finie"] and not out["data"]["tasks"][0]["overdue"]
    from datetime import datetime  # done_at is the real clock: "today" too for this filter
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(service, "today_in", lambda tz: datetime.now(ZoneInfo(tz)).date())
    _, out = run_todo(capsys, "board", "--filter", "done_today")  # todo #270: the briefing's « Fait aujourd'hui »
    assert [t["title"] for t in out["data"]["tasks"]] == ["finie"] and out["data"]["tasks"][0]["done_at"]
    press(monkeypatch, "cancel", 5)
    run_todo(capsys, "cancel", "5")
    _, out = run_todo(capsys, "board", "--filter", "closed", "--category", "serveur")
    assert [t["title"] for t in out["data"]["tasks"]] == ["finie"]
    assert cli.main(["todo", "board", "--filter", "bogus"]) == 1  # argparse error, JSON on stdout
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_show_lists_the_branch(todo_project, capsys, monkeypatch):
    from ultron_hub.todo import links
    run_todo(capsys, "add", "--title", "x", "--nature", "chantier")
    monkeypatch.setattr(links, "branch_map", lambda s: {1: {"branch": "todo-1/x", "ahead": 2}})
    _, out = run_todo(capsys, "show", "1")
    assert "missions" not in out["data"] and out["data"]["branch"]["ahead"] == 2
    _, out = run_todo(capsys, "board")
    assert out["data"]["tasks"][0]["badges"] == "🌿"


def test_links_are_best_effort(todo_project, capsys):
    run_todo(capsys, "add", "--title", "x")  # project dir is not a git repo
    _, out = run_todo(capsys, "show", "1")
    assert out["data"]["branch"] is None


def test_badges():
    from ultron_hub.todo.links import badges
    assert badges(None) == ""
    assert badges({"ahead": 0}) == ""
    assert badges({"ahead": 1}) == "🌿"
