"""Todo #103: `chantier` (the subject a todo belongs to, base v3), its migration and the way back to v2."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ultron_hub import cli
from ultron_hub.common.errors import InvalidRequestError, UltronHubError
from ultron_hub.common.settings import load_settings
from ultron_hub.esprit import store as journal
from ultron_hub.todo import service
from ultron_hub.todo.store import TodoStore, downgrade_to_v2
from test_nature import make_v1

SRC = str(Path(__file__).resolve().parents[1] / "src")


def make_v2(path: Path) -> None:
    make_v1(path)
    TodoStore(path).close()  # v1 -> v2 -> v3 ...
    downgrade_to_v2(path)    # ... and back to a true v2
    for leftover in path.parent.glob(path.name + ".bak-v3*"):
        leftover.unlink()
    (path.parent / (path.name + ".bak-v2")).unlink(missing_ok=True)


def version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def columns(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    finally:
        conn.close()


@pytest.fixture
def subjects(prompts_dir):
    (prompts_dir / "termines").mkdir(parents=True)
    (prompts_dir / "al-kitab.md").write_text("# Al Kitab\n")
    (prompts_dir / "_socle.md").write_text("socle\n")
    (prompts_dir / "termines" / "vieux.md").write_text("# Vieux\n")
    return prompts_dir


# --- migration v2 -> v3 --------------------------------------------------------------

def test_migration_v2_to_v3_backs_up_and_keeps_rows(tmp_path):
    db = tmp_path / "ultron.db"
    make_v2(db)
    assert version(db) == 2 and "chantier" not in columns(db)
    with TodoStore(db) as s:
        assert [(t.title, t.chantier) for t in s.list(status=None)] == [("old 1", None), ("old 2", None),
                                                                        ("old 3", None)]
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == 3
    backup = tmp_path / "ultron.db.bak-v2"
    assert version(backup) == 2 and "chantier" not in columns(backup)
    conn = sqlite3.connect(backup)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3
    conn.close()


def test_migration_v1_goes_straight_to_v3(tmp_path):
    db = tmp_path / "ultron.db"
    make_v1(db)
    TodoStore(db).close()
    assert version(db) == 3 and "chantier" in columns(db)
    assert version(tmp_path / "ultron.db.bak-v1") == 1


def test_migration_v3_is_idempotent_and_keeps_the_first_backup(tmp_path):
    db = tmp_path / "ultron.db"
    make_v2(db)
    TodoStore(db).close()
    backup = tmp_path / "ultron.db.bak-v2"
    first = backup.read_bytes()
    with TodoStore(db) as s:
        s.add("after", "perso", chantier="x")
    with TodoStore(db) as s:
        assert len(s.list(status=None)) == 4
    assert backup.read_bytes() == first


def test_migration_never_overwrites_an_existing_backup(tmp_path):
    db = tmp_path / "ultron.db"
    make_v2(db)
    (tmp_path / "ultron.db.bak-v2").write_bytes(b"precious")
    TodoStore(db).close()
    assert (tmp_path / "ultron.db.bak-v2").read_bytes() == b"precious"


def test_migration_resumes_after_a_half_done_attempt(tmp_path):
    db = tmp_path / "ultron.db"
    make_v2(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE tasks ADD COLUMN chantier TEXT")
    conn.commit()
    conn.close()
    with TodoStore(db) as s:
        assert s.get(1).chantier is None and s._conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_fresh_database_is_v3_without_backup(tmp_path):
    TodoStore(tmp_path / "new.db").close()
    assert version(tmp_path / "new.db") == 3 and "chantier" in columns(tmp_path / "new.db")
    assert not (tmp_path / "new.db.bak-v2").exists()


def test_v3_constraint_holds_without_the_api(tmp_path):
    make_v2(tmp_path / "ultron.db")
    TodoStore(tmp_path / "ultron.db").close()
    conn = sqlite3.connect(tmp_path / "ultron.db")  # a migrated table keeps its CHECK
    for value in ("", "x" * 41):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO tasks (title, category, created_at, updated_at, chantier)"
                         " VALUES ('t', 'perso', 'n', 'n', ?)", (value,))
    conn.close()


def test_two_processes_migrate_at_the_same_time(tmp_path):
    db = tmp_path / "ultron.db"
    make_v2(db)
    start = time.time() + 1.5
    code = ("import sys, time\nfrom ultron_hub.todo.store import TodoStore\n"
            f"time.sleep(max(0, {start} - time.time()))\n"
            "s = TodoStore(sys.argv[1]); print(len(s.list(status=None)))\n")
    env = {**os.environ, "PYTHONPATH": SRC}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(db)], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = [p.communicate(timeout=60) + (p.returncode,) for p in procs]
    assert [(r[2], r[0].strip()) for r in results] == [(0, "3"), (0, "3")], results
    assert version(db) == 3 and columns(db).count("chantier") == 1
    assert (tmp_path / "ultron.db.bak-v2").exists()


# --- way back to v2 ------------------------------------------------------------------

@pytest.mark.parametrize("force_copy", [False, True])
def test_downgrade_to_v2_then_the_v2_code_can_use_the_base(tmp_path, force_copy):
    db = tmp_path / "ultron.db"
    make_v1(db)
    with TodoStore(db) as s:
        s.add("filed", "perso", nature="chantier", chantier="al-kitab")
        s.update(2, by="cli", chantier="fba")
        s._conn.execute("DELETE FROM events WHERE task_id = 4")
        s._conn.execute("DELETE FROM tasks WHERE id = 4")  # the counter must survive: ids are never reused
    result = downgrade_to_v2(db, force_copy=force_copy)
    assert result["changed"] and result["from"] == 3 and result["to"] == 2
    assert result["method"] == ("table-copy" if force_copy or sqlite3.sqlite_version_info < (3, 35, 0)
                                else "drop-column")
    assert result["sqlite_version"] == sqlite3.sqlite_version
    # What the v2 code sees: user_version 2, no chantier column, its own queries work.
    assert version(db) == 2 and "chantier" not in columns(db)
    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute("SELECT title FROM tasks ORDER BY id")] == ["old 1", "old 2", "old 3"]
    conn.execute("INSERT INTO tasks (title, category, created_at, updated_at, nature, clair)"
                 " VALUES ('v2 insert', 'perso', 'n', 'n', 'fond', '')")
    assert conn.execute("SELECT max(id) FROM tasks").fetchone()[0] == 5  # 4 was used, not reused
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tasks (title, category, created_at, updated_at, nature) VALUES ('t','c','n','n','zz')")
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] >= 3
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.execute("DELETE FROM tasks WHERE id = 5")
    conn.commit()
    conn.close()
    # The backup holds the v3 state, chantiers included.
    backup = tmp_path / "ultron.db.bak-v3"
    conn = sqlite3.connect(backup)
    assert version(backup) == 3
    assert conn.execute("SELECT chantier FROM tasks WHERE id = 2").fetchone()[0] == "fba"
    conn.close()
    # The index of the open tasks is still there.
    conn = sqlite3.connect(db)
    assert "idx_tasks_open" in [r[1] for r in conn.execute("PRAGMA index_list(tasks)")]
    conn.close()
    # Running the new code again migrates forward once more, with the chantier column back and empty.
    with TodoStore(db) as s:
        assert version(db) == 3 and all(t.chantier is None for t in s.list(status=None))


def test_downgrade_keeps_earlier_backups_and_is_a_noop_on_v2(tmp_path):
    db = tmp_path / "ultron.db"
    TodoStore(db).close()
    assert downgrade_to_v2(db)["backup"].endswith(".bak-v3")
    assert downgrade_to_v2(db) == {"sqlite_version": sqlite3.sqlite_version, "backup": None, "changed": False,
                                   "from": 2, "to": 2, "method": None}
    TodoStore(db).close()
    assert downgrade_to_v2(db)["backup"].endswith(".bak-v3.1")
    assert (tmp_path / "ultron.db.bak-v3").exists()


def test_downgrade_refuses_unknown_and_missing_bases(tmp_path):
    with pytest.raises(UltronHubError):
        downgrade_to_v2(tmp_path / "absent.db")
    db = tmp_path / "odd.db"
    TodoStore(db).close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 9")
    conn.close()
    with pytest.raises(UltronHubError, match="v9"):
        downgrade_to_v2(db)


# --- store ---------------------------------------------------------------------------

def test_store_add_edit_list_and_events(tmp_path):
    with TodoStore(tmp_path / "t.db") as s:
        s.add("a", "perso", chantier="al-kitab")
        s.add("b", "perso")
        assert s.get(1).chantier == "al-kitab" and s.get(2).chantier is None
        assert [t.title for t in s.list(chantier="al-kitab")] == ["a"]
        assert [t.title for t in s.list(hors_chantier=True)] == ["b"]
        s.update(2, by="telegram-button", chantier="fba")
        assert s.history(2)[-1] == {"at": s.history(2)[-1]["at"], "action": "chantier",
                                    "details": {"from": None, "to": "fba", "by": "telegram-button"}}
        s.update(2, chantier="fba")  # unchanged: no event
        s.update(2, chantier=None, title="b2")  # an edit and a chantier event
        assert [e["action"] for e in s.history(2)] == ["add", "chantier", "edit", "chantier"]
        assert s.get(2).chantier is None
        with pytest.raises(InvalidRequestError):
            s.update(1, chantier="x" * 41)
        with pytest.raises(InvalidRequestError):
            s.add("c", "perso", chantier="")


# --- CLI -----------------------------------------------------------------------------

def run_todo(capsys, *argv):
    code = cli.main(["todo", *argv])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    return code, json.loads(lines[0])


def test_cli_refuses_an_unknown_subject(project, subjects, capsys):
    code, out = run_todo(capsys, "add", "--title", "x", "--chantier", "al-kitba")
    assert code == 1 and "chantier inconnu" in out["error"] and "al-kitba" in out["error"]
    assert run_todo(capsys, "list", "--status", "all")[1]["data"]["tasks"] == []  # nothing was created
    run_todo(capsys, "add", "--title", "x")
    code, out = run_todo(capsys, "edit", "1", "--chantier", "inconnu")
    assert code == 1 and "chantier inconnu" in out["error"]
    for bad in ("_socle", "../x", "a/b"):
        assert run_todo(capsys, "edit", "1", "--chantier", bad)[0] == 1
    _, shown = run_todo(capsys, "show", "1")
    assert shown["data"]["chantier"] is None


def test_cli_refuses_everything_without_a_prompts_folder(project, capsys):
    run_todo(capsys, "add", "--title", "x")
    assert run_todo(capsys, "edit", "1", "--chantier", "al-kitab")[0] == 1


def test_cli_add_edit_list_show(project, subjects, esprit_dir, capsys):
    esprit_dir.mkdir()
    _, out = run_todo(capsys, "add", "--title", "Dans un chantier", "--chantier", "al-kitab")
    assert out["data"]["chantier"] == "al-kitab"
    run_todo(capsys, "add", "--title", "Libre")
    run_todo(capsys, "add", "--title", "Archivé", "--chantier", "vieux")  # an archived subject is allowed
    _, out = run_todo(capsys, "list", "--chantier", "al-kitab")
    assert [t["title"] for t in out["data"]["tasks"]] == ["Dans un chantier"]
    _, out = run_todo(capsys, "list", "--hors-chantier")
    assert [t["title"] for t in out["data"]["tasks"]] == ["Libre"]
    assert all("chantier" in t for t in run_todo(capsys, "list")[1]["data"]["tasks"])
    assert run_todo(capsys, "list", "--chantier", "a", "--hors-chantier")[0] == 1  # exclusive

    _, out = run_todo(capsys, "edit", "2", "--chantier", "al-kitab", "--by", "telegram-button")
    assert out["data"]["chantier"] == "al-kitab"
    _, shown = run_todo(capsys, "show", "2")
    assert shown["data"]["chantier"] == "al-kitab"
    assert shown["data"]["history"][-1]["details"] == {"from": None, "to": "al-kitab", "by": "telegram-button"}
    assert shown["data"]["history"][-1]["action"] == "chantier"
    _, out = run_todo(capsys, "edit", "2", "--sans-chantier")
    assert out["data"]["chantier"] is None
    assert run_todo(capsys, "edit", "2", "--chantier", "al-kitab", "--sans-chantier")[0] == 1
    run_todo(capsys, "edit", "2", "--sans-chantier")  # unchanged: no second event or line
    texts = [e["texte"] for e in journal.tail(load_settings())]
    assert "Todo rattachée au chantier al-kitab : Libre" in texts
    assert "Todo sortie de son chantier : Libre" in texts
    assert sum("sortie de son chantier" in t for t in texts) == 1
    # filtering is lenient: the subject of a prompt that has been deleted can still be looked up
    assert run_todo(capsys, "list", "--chantier", "disparu")[1]["data"]["tasks"] == []


def test_cli_add_inside_a_squad_files_the_todo_in_its_chantier(project, subjects, monkeypatch, capsys):
    monkeypatch.setenv("ULTRON_ESCOUADE_SUJET", "al-kitab")
    _, out = run_todo(capsys, "add", "--title", "Créée par une escouade")
    assert out["data"]["chantier"] == "al-kitab" and out["data"]["nature"] == "chantier"
    _, out = run_todo(capsys, "add", "--title", "Ailleurs", "--chantier", "vieux", "--nature", "fond")  # explicit wins
    assert out["data"]["chantier"] == "vieux" and out["data"]["nature"] == "fond"
    monkeypatch.setenv("ULTRON_ESCOUADE_SUJET", "inconnu")  # best effort: never blocks the creation
    code, out = run_todo(capsys, "add", "--title", "Sujet inconnu")
    assert code == 0 and out["data"]["chantier"] is None and out["data"]["nature"] == "fond"
    monkeypatch.delenv("ULTRON_ESCOUADE_SUJET")
    _, out = run_todo(capsys, "add", "--title", "Hors escouade")
    assert out["data"]["chantier"] is None and out["data"]["nature"] == "fond"


def test_cli_board_filters_by_chantier(project, subjects, capsys):
    run_todo(capsys, "add", "--title", "dedans", "--nature", "chantier", "--chantier", "al-kitab")
    run_todo(capsys, "add", "--title", "dehors", "--nature", "chantier")
    _, out = run_todo(capsys, "board", "--chantier", "al-kitab")
    assert [t["title"] for t in out["data"]["tasks"]] == ["dedans"] and out["data"]["tasks"][0]["chantier"] == "al-kitab"
    assert out["data"]["open"] == 2
    _, out = run_todo(capsys, "board", "--hors-chantier")
    assert [t["title"] for t in out["data"]["tasks"]] == ["dehors"]


def test_cli_board_of_a_chantier_lists_the_fond_too(project, subjects, capsys):
    """The screen /chantiers (todo #109) shows every open todo of a chantier, "en tête" or fond."""
    run_todo(capsys, "add", "--title", "fond", "--nature", "fond", "--chantier", "al-kitab")
    run_todo(capsys, "add", "--title", "tete", "--nature", "chantier", "--chantier", "al-kitab")
    run_todo(capsys, "add", "--title", "libre", "--nature", "fond")
    _, out = run_todo(capsys, "board", "--chantier", "al-kitab")
    assert sorted(t["title"] for t in out["data"]["tasks"]) == ["fond", "tete"]
    _, out = run_todo(capsys, "board", "--hors-chantier")
    assert [t["title"] for t in out["data"]["tasks"]] == ["libre"]
    _, out = run_todo(capsys, "board")
    assert [t["title"] for t in out["data"]["tasks"]] == ["tete"]


def test_cli_schema_retour(project, subjects, esprit_dir, capsys):
    esprit_dir.mkdir()
    run_todo(capsys, "add", "--title", "x", "--chantier", "al-kitab")
    code, out = run_todo(capsys, "schema-retour", "--vers", "2")
    assert code == 0 and out["data"]["changed"] and out["data"]["sqlite_version"] == sqlite3.sqlite_version
    assert out["data"]["method"] in ("drop-column", "table-copy")
    assert version(load_settings().todo_db) == 2
    code, out = run_todo(capsys, "schema-retour", "--vers", "2")
    assert code == 0 and not out["data"]["changed"] and version(load_settings().todo_db) == 2
    assert any("Base des todos" in e["texte"] for e in journal.tail(load_settings()))
    assert run_todo(capsys, "schema-retour", "--vers", "1")[0] != 0
    # the next ordinary command migrates forward again
    assert run_todo(capsys, "list")[1]["data"]["tasks"][0]["chantier"] is None
    assert version(load_settings().todo_db) == 3


def test_known_chantiers_lists_active_and_archived_without_socle(subjects):
    assert service.known_chantiers() == ["al-kitab", "vieux"]


def test_parse_prompt_domaine():
    from ultron_hub.chantier.core import parse_prompt
    base = "# Prompt de départ : chantier « X »\n\nSession précédente : aucune.\n{}\nProjet : /tmp\n\n---\n"
    assert parse_prompt(base.format("Domaine : business"), "x")["domaine"] == "business"
    assert parse_prompt(base.format("Domaine : business"), "x")["domaine_icone"] == "💼"
    for line in ("", "Domaine : inconnu"):
        got = parse_prompt(base.format(line), "x")
        assert (got["domaine"], got["domaine_icone"], got["domaine_titre"]) == ("", "❔", "À ranger")
