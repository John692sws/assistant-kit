"""Todo #153: `todo livree` (unsigned event), its rule, the À toi item and the one-press ✅ card."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

import test_ui
from hermes_plugin import ui
from ultron_hub import cli
from ultron_hub.chantier import core
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.todo import service
from ultron_hub.todo.store import TodoStore

CHAT = test_ui.CHAT


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --- rule (service) ---------------------------------------------------------------------------------

def test_is_delivered_rule():
    assert not service.is_delivered([])
    assert not service.is_delivered([("add", {})])
    assert service.is_delivered([("add", {}), ("livree", {"by": "cli"})])
    assert not service.is_delivered([("livree", {}), ("status", {"to": "todo"})])  # reopened
    assert not service.is_delivered([("livree", {}), ("edit", {"notes": {}})])  # edited
    assert not service.is_delivered([("livree", {}), ("chantier", {})])
    assert service.is_delivered([("livree", {}), ("edit", {"clair": {}})])  # filled by the digest: still delivered
    assert service.is_delivered([("livree", {}), ("edit", {"clair": {}}), ("livree", {})])


def test_note_limit():
    assert service.parse_livree_note("  a   b ") == "a b"
    assert service.parse_livree_note(None) == ""
    with pytest.raises(InvalidRequestError):
        service.parse_livree_note("x" * 201)


# --- CLI + store ------------------------------------------------------------------------------------

def test_livree_writes_one_event_and_closes_nothing(project, capsys):
    run_cli(capsys, "todo", "add", "--title", "Changer le prompt")
    code, out = run_cli(capsys, "todo", "livree", "1", "--note", "fait dans le prompt")
    assert code == 0 and out["status"] == "ok" and out["data"]["livree"] is True
    with TodoStore(project / "var" / "ultron.db") as store:
        task = store.get(1)
        assert task.status == "todo" and task.done_at is None
        last = store.history(1)[-1]
    assert last["action"] == "livree" and last["details"] == {"by": "cli", "note": "fait dans le prompt"}


def test_livree_refuses_long_note_and_closed_todo(project, capsys):
    run_cli(capsys, "todo", "add", "--title", "T")
    code, out = run_cli(capsys, "todo", "livree", "1", "--note", "x" * 201)
    assert code != 0 and out["status"] == "error"
    with TodoStore(project / "var" / "ultron.db") as store:
        store.set_status(1, "done")
        assert [h["action"] for h in store.history(1)].count("livree") == 0
    code, out = run_cli(capsys, "todo", "livree", "1")
    assert code != 0 and out["status"] == "error"


# --- chantier atoi ----------------------------------------------------------------------------------

@pytest.fixture
def atoi_env(project, tmp_path, monkeypatch):
    db = project / "var" / "ultron.db"
    with TodoStore(db) as store:
        for title in ("livrée", "pas livrée", "livrée puis modifiée", "livrée puis rouverte"):
            store.add(title, "perso")
        store.mark_delivered(1, "cli", "ok")
        store.mark_delivered(3, "cli")
        store.update(3, title="autre titre")
        store.mark_delivered(4, "cli")
        store.set_status(4, "done")
        store.set_status(4, "todo")
    prompts = tmp_path / "prompt"
    prompts.mkdir()
    monkeypatch.setattr(core.links, "branch_map", lambda s: {})
    src = core.sources(str(prompts), str(tmp_path / "boites"))
    return SimpleNamespace(todo_db=db), src


def test_atoi_lists_only_delivered_open_todos(atoi_env):
    settings, src = atoi_env
    items = core.atoi(settings, src)["items"]
    assert [(i["todo"], i["kind"], i["icone"]) for i in items] == [(1, "livree", "✅")]


def test_atoi_without_event_is_absent_and_closed_delivered_disappears(atoi_env):
    settings, src = atoi_env
    with TodoStore(settings.todo_db) as store:
        store.mark_delivered(2, "cli")
        store.set_status(1, "done")
    assert [i["todo"] for i in core.atoi(settings, src)["items"]] == [2]


# --- Telegram: a delivered todo is a text line, decided on the dashboard (todo #463) -----------------------------

class DeliveredClient(test_ui.WideClient):
    async def call(self, *args, **kw):
        if args[:2] == ("chantier", "atoi"):
            return test_ui.ok({"items": [{"kind": "livree", "icone": "✅", "todo": 9, "texte": "Changer le prompt",
                                          "chantier": "-"}], "total": 1, "avertissements": []})
        return await super().call(*args, **kw)


def test_delivered_item_is_listed_as_text_without_a_decision_button(monkeypatch):
    monkeypatch.setattr(test_ui, "WideClient", DeliveredClient)
    home = asyncio.run(test_ui.Plugin().press(ui.HOME_CB))
    assert "✅ Hors chantier · #9 Changer le prompt" in home.text and "dashboard" in home.text
    assert not any(d.startswith("hub:todo:") for row in home.buttons for _, d in row)
