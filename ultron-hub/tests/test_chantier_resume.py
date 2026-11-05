"""State of the chantiers (todo #108): fake subjects, nothing real is read."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from ultron_hub.chantier import core


@pytest.fixture
def world_env(tmp_path, monkeypatch):
    prompts = tmp_path / "prompt"
    prompts.mkdir()
    (prompts / "_socle.md").write_text("socle\n")
    (prompts / "alpha.md").write_text(
        "# Chantier « Alpha test »\nProjet : /tmp\n"
        "Session précédente : fini le 12/10/2026, reste la suite.\n"
        f"Fiche du chantier : `{tmp_path}/fiche.md`\n## 1. But\n")
    (prompts / "beta.md").write_text("pas d'en-tête du tout\n")
    (tmp_path / "fiche.md").write_text("# F\n## REPRISE\n- vieille\n- derniere\n  suite\n\n## Autre\n- x\n")
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, status TEXT, chantier TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [(1, "a1", "todo", "alpha"), (2, "libre", "todo", None),
                                                           (3, "fait", "done", "alpha")])
    conn.commit()
    conn.close()
    monkeypatch.setattr(core.links, "branch_map", lambda s: {1: {"branch": "todo-1/a", "ahead": 1}})
    settings = SimpleNamespace(todo_db=db)
    src = core.sources(str(prompts), str(tmp_path / "boites"))
    return settings, src, tmp_path


def test_tableau_reads_subjects_and_todos(world_env):
    settings, src, _ = world_env
    data = core.tableau(settings, src)
    names = [c["sujet"] for c in data["chantiers"]]
    assert sorted(names) == ["alpha", "beta"]
    alpha = next(c for c in data["chantiers"] if c["sujet"] == "alpha")
    assert alpha["titre"] == "Alpha test" and alpha["date"] == "2026-10-12"
    assert alpha["groupe"] == "📥" and [t["id"] for t in alpha["todos"]] == [1]
    assert alpha["a_toi"][0]["kind"] == "fusionner"
    assert [t["id"] for t in data["hors_chantier"]["todos"]] == [2]


def test_prompt_without_header_gives_defaults(world_env):
    settings, src, _ = world_env
    beta = next(c for c in core.tableau(settings, src)["chantiers"] if c["sujet"] == "beta")
    assert beta["titre"] == "beta" and beta["ou_en_est"] == "" and beta["fiche"].endswith("chantier-beta.md")
    assert beta["groupe"] == "💤"


def test_reprise_last_bullet_and_missing_fiche(world_env):
    settings, src, tmp = world_env
    assert core.reprise(str(tmp / "fiche.md")) == ["- derniere", "  suite"]
    assert core.reprise(str(tmp / "absente.md")) == []


def test_mailbox_questions_and_remarks(world_env):
    settings, src, tmp = world_env
    assert core.questions_open(src, "alpha") == []  # absent mailbox
    box = tmp / "boites" / "alpha"
    (box / "questions").mkdir(parents=True)
    (box / "questions" / "q1.json").write_text(json.dumps({"id": "q1", "texte": "ok ?", "statut": "ouverte"}))
    (box / "questions" / "q2.json").write_text(json.dumps({"id": "q2", "statut": "close"}))
    (box / "questions" / "q3.json").write_text("{pas du json")
    (box / "remarques").mkdir()
    (box / "remarques" / "r.json").write_text("{}")
    assert [q["id"] for q in core.questions_open(src, "alpha")] == ["q1"]
    assert src.warnings
    assert core.remarques(src, "alpha")["en_attente"] == 1
    assert core.atoi(settings, src, "alpha")["items"][0]["kind"] == "question"


def test_missing_database_is_a_warning(world_env, tmp_path):
    settings, src, _ = world_env
    settings.todo_db = tmp_path / "nope.db"
    data = core.tableau(settings, src)
    assert data["avertissements"] and len(data["chantiers"]) == 2


@pytest.mark.parametrize("status,shown", [("pending", True), ("interrupted", True), ("done", False),
                                          ("invalid", False), ("refused", False)])
def test_closed_todo_with_after_merge_action_in_atoi(world_env, status, shown):
    settings, src, tmp = world_env
    var = tmp / "var"
    (var / "after-merge").mkdir(parents=True)
    (var / "after-merge" / "todo-3.json").write_text(json.dumps(
        {"todo_id": 3, "actions": [{"n": 1, "status": status}], "proofs": []}))
    settings.var_dir = var
    items = [it for it in core.atoi(settings, src)["items"] if it["kind"] == "apres_fusion"]
    if shown:
        assert items == [{"kind": "apres_fusion", "icone": "🧰", "todo": 3, "texte": "fait", "chantier": "alpha"}]
    else:
        assert items == []
