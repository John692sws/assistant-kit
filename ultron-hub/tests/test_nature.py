"""Todo #52: `nature` (chantier / fond) and `clair` ("En clair" line) of the todos."""

import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from ultron_hub import cli
from ultron_hub.common.errors import InvalidRequestError, UltronHubError
from ultron_hub.common.settings import load_settings
from ultron_hub.esprit import store as journal
from ultron_hub.todo import service, summarizer
from ultron_hub.todo.store import CLAIR_MAX, TodoStore
# Imported before the autouse `no_real_clair` fixture patches the module attribute.
from ultron_hub.todo.summarizer import llm_clair as real_llm_clair

SAT = date(2026, 10, 10)
REPO = Path(__file__).resolve().parents[1]

# The v1 schema exactly as the first version created it (no nature, no clair).
V1_SCHEMA = """
CREATE TABLE tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    notes       TEXT NOT NULL DEFAULT '',
    priority    TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('high', 'normal', 'low')),
    category    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'done', 'cancelled')),
    due_date    TEXT,
    source      TEXT NOT NULL DEFAULT 'cli',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    done_at     TEXT
);
CREATE INDEX idx_tasks_open ON tasks (status, due_date);
CREATE TABLE events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  INTEGER NOT NULL REFERENCES tasks (id),
    at       TEXT NOT NULL,
    action   TEXT NOT NULL,
    details  TEXT NOT NULL DEFAULT '{}'
);
PRAGMA user_version = 1;
"""


def make_v1(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    for i, status in enumerate(("todo", "todo", "done"), 1):
        conn.execute("INSERT INTO tasks (title, category, status, created_at, updated_at)"
                     " VALUES (?, 'perso', ?, 'now', 'now')", (f"old {i}", status))
        conn.execute("INSERT INTO events (task_id, at, action) VALUES (?, 'now', 'add')", (i,))
    conn.commit()
    conn.close()


@pytest.fixture
def store(tmp_path):
    with TodoStore(tmp_path / "todo.db") as s:
        yield s


# --- migration v1 -> v2 --------------------------------------------------------------

def test_migration_v1_to_v2_backs_up_and_keeps_rows(tmp_path):
    db = tmp_path / "ultron.db"
    make_v1(db)
    with TodoStore(db) as s:
        tasks = s.list(status=None)
        assert [(t.title, t.nature, t.clair) for t in tasks] == [("old 1", "chantier", ""),
                                                                 ("old 2", "chantier", ""),
                                                                 ("old 3", "chantier", "")]
        assert [e["action"] for e in s.history(1)] == ["add"]
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == 3  # v1 goes on to v3 (todo #103)
    backup = tmp_path / "ultron.db.bak-v1"
    conn = sqlite3.connect(backup)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3
    assert "nature" not in [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    conn.close()


def test_migration_is_idempotent_and_keeps_the_first_backup(tmp_path):
    db = tmp_path / "ultron.db"
    make_v1(db)
    TodoStore(db).close()
    with TodoStore(db) as s:
        s.add("after", "perso")
    backup = tmp_path / "ultron.db.bak-v1"
    before = backup.read_bytes()
    with TodoStore(db) as s:  # third opening: nothing to do
        assert len(s.list(status=None)) == 4
    assert backup.read_bytes() == before


def test_migration_never_overwrites_an_existing_backup(tmp_path):
    db = tmp_path / "ultron.db"
    make_v1(db)
    (tmp_path / "ultron.db.bak-v1").write_bytes(b"precious")
    TodoStore(db).close()
    assert (tmp_path / "ultron.db.bak-v1").read_bytes() == b"precious"


def test_migration_resumes_after_a_half_done_attempt(tmp_path):
    db = tmp_path / "ultron.db"
    make_v1(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE tasks ADD COLUMN nature TEXT NOT NULL DEFAULT 'chantier'")
    conn.commit()
    conn.close()
    with TodoStore(db) as s:
        assert s.get(1).clair == "" and s._conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_fresh_database_is_v2_without_backup(tmp_path):
    TodoStore(tmp_path / "new.db").close()
    assert not (tmp_path / "new.db.bak-v1").exists()


def test_v2_constraints_hold_without_the_api(tmp_path):
    make_v1(tmp_path / "ultron.db")
    TodoStore(tmp_path / "ultron.db").close()
    conn = sqlite3.connect(tmp_path / "ultron.db")  # a migrated table keeps its CHECKs
    for column, value in (("nature", "autre"), ("clair", "x" * (CLAIR_MAX + 1))):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO tasks (title, category, created_at, updated_at, {column})"
                         " VALUES ('t', 'perso', 'n', 'n', ?)", (value,))


# --- defaults and validation ---------------------------------------------------------

def test_new_todo_is_fond_and_old_ones_are_chantier(store):
    assert store.add("new", "perso").nature == "fond"
    assert store.add("deliberate", "perso", nature="chantier").nature == "chantier"
    assert store.get(1).clair == ""


def test_clair_validation(store):
    assert store.add("a", "perso", clair="  Une phrase.  ").clair == "Une phrase."
    assert store.add("b", "perso", clair="é" * CLAIR_MAX).clair == "é" * CLAIR_MAX
    for bad in ("x" * (CLAIR_MAX + 1), "deux\nlignes", "a\rb", 3):
        with pytest.raises(InvalidRequestError):
            store.add("c", "perso", clair=bad)
        with pytest.raises(InvalidRequestError):
            store.update(1, clair=bad)
    with pytest.raises(InvalidRequestError):
        store.add("c", "perso", nature="urgent")
    assert store.update(1, clair="").clair == ""


def test_nature_change_is_an_event_with_by(store):
    store.add("a", "perso")
    t = store.update(1, by="telegram-button", nature="chantier")
    assert t.nature == "chantier"
    event = store.history(1)[-1]
    assert event["action"] == "nature"
    assert event["details"] == {"from": "fond", "to": "chantier", "by": "telegram-button"}
    store.update(1, nature="chantier")  # unchanged: no event
    assert [e["action"] for e in store.history(1)] == ["add", "nature"]
    store.update(1, nature="fond", title="b")  # both: an edit and a nature event, one transaction
    assert [e["action"] for e in store.history(1)][-2:] == ["edit", "nature"]
    assert store.history(1)[-1]["details"]["by"] == "cli"


def test_list_filters_by_nature(store):
    store.add("f1", "perso")
    store.add("c1", "perso", nature="chantier")
    store.add("f2", "perso")
    assert [t.title for t in store.list(nature="fond")] == ["f1", "f2"]
    assert [t.title for t in store.list(nature="chantier")] == ["c1"]
    assert len(store.list()) == 3
    with pytest.raises(InvalidRequestError):
        store.list(nature="x")


# --- CLI -----------------------------------------------------------------------------

def run_todo(capsys, *argv):
    code = cli.main(["todo", *argv])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    return code, json.loads(lines[0])


@pytest.fixture
def todo_project(project):
    return project


def test_cli_add_default_and_options(todo_project, capsys):
    _, out = run_todo(capsys, "add", "--title", "Idée")
    assert out["data"]["nature"] == "fond" and out["data"]["clair"] == ""  # generation disabled in tests
    _, out = run_todo(capsys, "add", "--title", "Cadrée", "--nature", "chantier", "--clair", "Faire X pour Y.")
    assert out["data"]["nature"] == "chantier" and out["data"]["clair"] == "Faire X pour Y."
    code, out = run_todo(capsys, "add", "--title", "Mauvaise", "--nature", "bof")
    assert code == 1 and "nature" in out["error"]
    code, out = run_todo(capsys, "add", "--title", "Longue", "--clair", "x" * 141)
    assert code == 1 and "140" in out["error"]
    _, out = run_todo(capsys, "list")
    assert len(out["data"]["tasks"]) == 2


def test_cli_list_nature_filter(todo_project, capsys):
    run_todo(capsys, "add", "--title", "fond one")
    run_todo(capsys, "add", "--title", "chantier one", "--nature", "chantier")
    _, out = run_todo(capsys, "list", "--nature", "fond")
    assert [t["title"] for t in out["data"]["tasks"]] == ["fond one"]
    _, out = run_todo(capsys, "list", "--nature", "chantier")
    assert [t["title"] for t in out["data"]["tasks"]] == ["chantier one"]
    assert run_todo(capsys, "list", "--nature", "x")[0] == 1


def test_cli_edit_nature_writes_event_and_journal(todo_project, esprit_dir, capsys):
    esprit_dir.mkdir()
    run_todo(capsys, "add", "--title", "Grosse idée")
    _, out = run_todo(capsys, "edit", "1", "--nature", "chantier", "--by", "telegram-button")
    assert out["status"] == "ok" and out["data"]["nature"] == "chantier"
    _, shown = run_todo(capsys, "show", "1")
    event = shown["data"]["history"][-1]
    assert event["action"] == "nature"
    assert event["details"] == {"from": "fond", "to": "chantier", "by": "telegram-button"}
    run_todo(capsys, "edit", "1", "--nature", "fond")
    texts = [e["texte"] for e in journal.tail(load_settings())]
    assert "Todo passée en chantier : Grosse idée" in texts
    assert "Todo mise en fond : Grosse idée" in texts
    # an unchanged nature adds neither event nor journal line
    run_todo(capsys, "edit", "1", "--nature", "fond")
    assert sum("fond :" in t for t in [e["texte"] for e in journal.tail(load_settings())]) == 1
    assert run_todo(capsys, "edit", "1", "--by", "Bad Actor!")[0] == 1


def test_cli_edit_nature_without_journal_folder_is_fine(todo_project, esprit_dir, capsys):
    assert not esprit_dir.exists()
    run_todo(capsys, "add", "--title", "x")
    assert run_todo(capsys, "edit", "1", "--nature", "chantier")[0] == 0


def test_cli_edit_clair(todo_project, capsys):
    run_todo(capsys, "add", "--title", "x")
    _, out = run_todo(capsys, "edit", "1", "--clair", "Pour savoir ce que c'est.")
    assert out["data"]["clair"] == "Pour savoir ce que c'est."
    code, out = run_todo(capsys, "edit", "1", "--clair", "a\nb")
    assert code == 1 and "single line" in out["error"]


def test_board_lists_chantiers_then_counts_fond(todo_project, capsys):
    run_todo(capsys, "add", "--title", "fond one")
    run_todo(capsys, "add", "--title", "fond two", "--category", "serveur")
    run_todo(capsys, "add", "--title", "chantier one", "--nature", "chantier", "--category", "serveur")
    _, out = run_todo(capsys, "board")
    d = out["data"]
    assert [t["title"] for t in d["tasks"]] == ["chantier one"]
    assert (d["open"], d["chantiers"], d["fond"]) == (3, 1, 2)
    _, out = run_todo(capsys, "board", "--filter", "fond")
    assert [t["title"] for t in out["data"]["tasks"]] == ["fond one", "fond two"]
    assert out["data"]["tasks"][0]["nature"] == "fond"
    # a category view shows both natures: it is a lookup, not the chantier list
    _, out = run_todo(capsys, "board", "--category", "serveur")
    assert sorted(t["title"] for t in out["data"]["tasks"]) == ["chantier one", "fond two"]


# --- generation of "En clair" ----------------------------------------------------------

CLAIR_OK = """
out({"is_error": False, "result": "", "structured_output": {"clair": "  Mettre au clair\\nce que fait la tâche. "},
     "total_cost_usd": 0.001, "permission_denials": []})
"""


@pytest.fixture
def clair_project(project, make_fake_claude, monkeypatch):
    shutil.copytree(REPO / "tasks" / "todo-clair", project / "tasks" / "todo-clair")
    monkeypatch.setenv("ULTRON_HUB_CLAUDE_BIN", str(make_fake_claude(CLAIR_OK)))
    return project


def test_todo_clair_task_is_haiku_without_tools():
    import yaml
    spec = yaml.safe_load((REPO / "tasks" / "todo-clair" / "task.yaml").read_text())
    assert spec["model"] == "haiku" and spec["tools"] == []
    schema = json.loads((REPO / "tasks" / "todo-clair" / "output.schema.json").read_text())
    assert schema["properties"]["clair"]["maxLength"] == CLAIR_MAX
    prompt = (REPO / "tasks" / "todo-clair" / "prompt.md").read_text()
    assert "{title}" in prompt and "{notes}" in prompt and "{max_len}" in prompt


def test_llm_clair_with_the_fake_claude(clair_project, read_argv):
    assert real_llm_clair("Réparer le skill watch", "paquet manquant") == "Mettre au clair ce que fait la tâche."
    seen = read_argv()
    assert seen["argv"][seen["argv"].index("--model") + 1] == "haiku"
    assert "Réparer le skill watch" in seen["stdin"] and "paquet manquant" in seen["stdin"]
    assert "Réparer le skill watch" not in " ".join(seen["argv"])  # prompt on stdin, never in argv


def test_llm_clair_failure_raises(project, make_fake_claude, monkeypatch):
    shutil.copytree(REPO / "tasks" / "todo-clair", project / "tasks" / "todo-clair")
    monkeypatch.setenv("ULTRON_HUB_CLAUDE_BIN", str(make_fake_claude(
        'out({"is_error": True, "result": "Not logged in"})\n')))
    with pytest.raises(UltronHubError):
        real_llm_clair("x")


def test_add_generates_clair_when_absent(clair_project, monkeypatch, capsys):
    monkeypatch.setattr(summarizer, "llm_clair", real_llm_clair)
    _, out = run_todo(capsys, "add", "--title", "Nettoyer les logs")
    assert out["data"]["clair"] == "Mettre au clair ce que fait la tâche."
    # an explicit clair is never replaced, and an empty one means "none wanted"
    _, out = run_todo(capsys, "add", "--title", "B", "--clair", "Mon texte.")
    assert out["data"]["clair"] == "Mon texte."
    _, out = run_todo(capsys, "add", "--title", "C", "--clair", "")
    assert out["data"]["clair"] == ""


def test_add_survives_a_failing_generation(todo_project, monkeypatch, capsys):
    def boom(title, notes=""):
        raise UltronHubError("haiku down")
    monkeypatch.setattr(summarizer, "llm_clair", boom)
    code, out = run_todo(capsys, "add", "--title", "Quand même créée")
    assert code == 0 and out["data"]["clair"] == "" and out["data"]["id"] == 1


def test_fill_clair_is_capped_and_best_effort(store):
    for i in range(8):
        store.add(f"t{i}", "perso", clair="déjà" if i == 0 else "")
    store.set_status(2, "done")
    calls = []

    def generate(task):
        calls.append(task.id)
        if task.id == 3:
            raise UltronHubError("boom")
        return f"clair {task.id}"
    filled = service.fill_clair(store, store.list(status=None), generate)
    assert len(filled) == service.CLAIR_PER_PASS == 5
    assert 1 not in calls and 2 not in calls  # already written / closed
    assert 3 in calls and 3 not in filled      # a failure is skipped, not fatal
    assert store.get(3).clair == "" and store.get(4).clair == "clair 4"
    assert store.get(1).clair == "déjà"


def test_fill_clair_cleans_and_cuts_the_answer(store):
    store.add("a", "perso")
    service.fill_clair(store, store.list(), lambda t: ("mot " * 60))
    assert 0 < len(store.get(1).clair) <= CLAIR_MAX
    store.add("b", "perso")
    service.fill_clair(store, store.list(), lambda t: "   ")
    assert store.get(2).clair == ""


def test_fill_clair_stops_when_the_time_budget_is_spent(store):
    store.add("a", "perso")
    assert service.fill_clair(store, store.list(), lambda t: "x", budget_s=-1) == []


# --- 8 a.m. digest --------------------------------------------------------------------

def test_digest_lists_chantiers_and_counts_fond(store):
    store.add("late chantier", "perso", nature="chantier", due_date="2026-10-08")
    store.add("late fond", "perso", due_date="2026-10-08")
    store.add("idée", "perso")
    d = service.build_digest(store.list(), SAT)
    assert "late chantier" in d["text"] and "late fond" not in d["text"] and "idée" not in d["text"]
    assert "🌱 2 tâches de fond" in d["text"] and d["fond"] == 2 and d["open"] == 3
    one = service.build_digest([store.get(1), store.get(2)], SAT)
    assert "🌱 1 tâche de fond" in one["text"]
    assert "de fond" not in service.build_digest([store.get(1)], SAT)["text"]


def test_cli_digest_completes_at_most_five_clair(todo_project, monkeypatch, capsys):
    for i in range(7):
        run_todo(capsys, "add", "--title", f"t{i}")
    asked = []

    def fake(title, notes=""):
        asked.append(title)
        return f"clair de {title}"
    monkeypatch.setattr(summarizer, "llm_clair", fake)
    _, out = run_todo(capsys, "digest")
    assert out["data"]["clair_filled"] == [1, 2, 3, 4, 5] and len(asked) == 5
    assert "🌱 7 tâches de fond" in out["data"]["text"]
    _, out = run_todo(capsys, "digest")  # next pass takes the two left
    assert out["data"]["clair_filled"] == [6, 7]
    _, shown = run_todo(capsys, "show", "1")
    assert shown["data"]["clair"] == "clair de t0"


def test_cli_digest_ignores_a_failing_generation(todo_project, capsys):
    run_todo(capsys, "add", "--title", "t")
    code, out = run_todo(capsys, "digest")  # llm_clair is disabled in tests: raises
    assert code == 0 and out["data"]["clair_filled"] == []
