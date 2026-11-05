"""Todo #104: `todo rattacher --proposer|--appliquer` and the 🗂 Ranger les todos card of /todo."""

import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from ultron_hub import cli
from ultron_hub.common.settings import load_settings
from ultron_hub.todo import service
from ultron_hub.todo.store import TodoStore
from cli_contract import check_argv

TABLE = {1: "al-kitab", 2: "al-kitab", 3: "video-neutre", 4: None, 5: "inconnu"}


@pytest.fixture
def table(prompts_dir, monkeypatch):
    prompts_dir.mkdir(parents=True, exist_ok=True)
    for name in ("al-kitab", "video-neutre"):
        (prompts_dir / f"{name}.md").write_text(f"# {name}\n")
    monkeypatch.setattr(service, "RATTACHEMENT", TABLE)


def run_todo(capsys, *argv):
    code = cli.main(["todo", *argv])
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    return code, json.loads(lines[0])


def seed(capsys, count=5):
    for i in range(count):
        run_todo(capsys, "add", "--title", f"Todo {i + 1}", "--clair", "x")


def chantiers(capsys):
    tasks = run_todo(capsys, "list", "--status", "all")[1]["data"]["tasks"]
    return {t["id"]: t["chantier"] for t in tasks}


def test_real_table_is_consistent():
    ids = list(service.RATTACHEMENT)
    assert len(ids) == len(set(ids)) == 25
    assert sum(1 for v in service.RATTACHEMENT.values() if v) == 18
    assert {v for v in service.RATTACHEMENT.values() if v} == {
        "agent-ultron", "al-kitab", "video-neutre", "escouade-autonome", "chantier-4", "montage-video"}


def test_proposal_lists_only_changes_towards_known_subjects(project, table, capsys):
    seed(capsys)
    code, out = run_todo(capsys, "rattacher", "--proposer")
    assert code == 0
    data = out["data"]
    assert [(l["id"], l["de"], l["vers"]) for l in data["lines"]] == [
        (1, None, "al-kitab"), (2, None, "al-kitab"), (3, None, "video-neutre")]  # #4 already out, #5 unknown
    assert data["chantiers"] == 2 and len(data["empreinte"]) == 8
    assert run_todo(capsys, "rattacher", "--proposer")[1]["data"]["empreinte"] == data["empreinte"]
    assert chantiers(capsys) == dict.fromkeys(range(1, 6))  # a proposal changes nothing


def test_filed_and_closed_todos_are_ignored(project, table, capsys):
    seed(capsys)
    run_todo(capsys, "edit", "2", "--chantier", "al-kitab")
    with TodoStore(load_settings().todo_db) as store:
        store.set_status(3, "cancelled")
    lines = run_todo(capsys, "rattacher", "--proposer")[1]["data"]["lines"]
    assert [l["id"] for l in lines] == [1]  # #2 is already there, #3 is closed


def test_apply_then_sans_chantier_undoes(project, table, esprit_dir, capsys):
    esprit_dir.mkdir()
    seed(capsys)
    h8 = run_todo(capsys, "rattacher", "--proposer")[1]["data"]["empreinte"]
    code, out = run_todo(capsys, "rattacher", "--appliquer", h8, "--by", "telegram-button")
    assert code == 0 and "3 todo(s) rangée(s) dans 2 chantier(s)" in out["summary"]
    assert chantiers(capsys) == {1: "al-kitab", 2: "al-kitab", 3: "video-neutre", 4: None, 5: None}
    shown = run_todo(capsys, "show", "3")[1]["data"]["history"][-1]
    assert shown["action"] == "chantier"
    assert shown["details"] == {"from": None, "to": "video-neutre", "by": "telegram-button"}
    assert run_todo(capsys, "rattacher", "--proposer")[1]["data"]["lines"] == []  # nothing left to do
    assert run_todo(capsys, "edit", "3", "--sans-chantier")[1]["data"]["chantier"] is None
    assert chantiers(capsys)[3] is None
    again = run_todo(capsys, "rattacher", "--proposer")[1]["data"]
    assert [l["id"] for l in again["lines"]] == [3] and again["empreinte"] != h8


def test_apply_with_a_changed_fingerprint_changes_nothing(project, table, capsys):
    seed(capsys)
    h8 = run_todo(capsys, "rattacher", "--proposer")[1]["data"]["empreinte"]
    run_todo(capsys, "edit", "1", "--chantier", "video-neutre")  # the state moved since the card was drawn
    code, out = run_todo(capsys, "rattacher", "--appliquer", h8)
    assert code == 1 and "a changé" in out["error"]
    assert chantiers(capsys) == {1: "video-neutre", 2: None, 3: None, 4: None, 5: None}
    assert run_todo(capsys, "rattacher", "--appliquer", "zzzzzzzz")[0] == 1
    assert cli.main(["todo", "rattacher"]) != 0  # one of the two modes is required
    capsys.readouterr()
