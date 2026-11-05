"""Tableaux épinglés des sujets du groupe (todo #202) : aucun réseau, `envoi._urlopen` simulé."""
import io
import json
import urllib.error

import pytest

from tests.test_send import Rep, TOKEN
from ultron_hub import envoi
from ultron_hub.common.settings import load_settings
from ultron_hub.groupe import tableau
from ultron_hub.todo.store import TodoStore

TEXTES = {"systeme": "S\n🕐 10h00", "business": "B\n🕐 10h00", "perso": "P\n🕐 10h00"}


@pytest.fixture
def groupe(project, monkeypatch):
    (project / "config" / "groupe.yaml").write_text(
        "chat_id: -100\nsujets:\n  systeme: 11\n  business: 12\n  perso: 13\n  alertes: 14\n")
    monkeypatch.setenv(envoi.MODE_ENV, "direct")
    monkeypatch.setenv(envoi.TOKEN_ENV, TOKEN)
    calls, plan = [], {}

    def fake(req, timeout):
        body = json.loads(req.data)
        methode = req.full_url.rsplit("/", 1)[1]
        calls.append((methode, body))
        err = plan.get(methode)
        if err:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                         io.BytesIO(json.dumps({"ok": False, "description": err}).encode()))
        return Rep(json.dumps({"ok": True, "result": {"message_id": 100 + len(calls)}}).encode())
    monkeypatch.setattr(envoi, "_urlopen", fake)
    return calls, plan, load_settings()


def methodes(calls):
    return [m for m, _ in calls]


def test_first_run_sends_and_pins(groupe):
    calls, _, settings = groupe
    res = tableau.rafraichir(settings, textes=TEXTES)
    assert res["status"] == "ok"
    assert methodes(calls) == ["sendMessage", "pinChatMessage"] * 3
    assert calls[0][1]["message_thread_id"] == 11 and calls[0][1]["chat_id"] == "-100"
    assert calls[1][1]["disable_notification"] is True
    state = json.loads((settings.var_dir / "groupe" / "tableau.json").read_text())
    assert state["systeme"]["message_id"] and state["systeme"]["epingle"]


def test_second_run_edits_in_place_then_nothing(groupe):
    calls, _, settings = groupe
    tableau.rafraichir(settings, textes=TEXTES)
    calls.clear()
    tableau.rafraichir(settings, textes={**TEXTES, "perso": "P2\n🕐 10h05"})
    assert methodes(calls) == ["editMessageText"]
    calls.clear()
    tableau.rafraichir(settings, textes={**TEXTES, "perso": "P2\n🕐 10h09"})  # only the clock changed
    assert calls == []


def test_message_gone_is_recreated_and_repinned(groupe):
    calls, plan, settings = groupe
    tableau.rafraichir(settings, textes=TEXTES)
    calls.clear()
    plan["editMessageText"] = "Bad Request: message to edit not found"
    res = tableau.rafraichir(settings, textes={k: "!" + v for k, v in TEXTES.items()})
    assert res["status"] == "ok"
    assert methodes(calls) == ["editMessageText", "sendMessage", "pinChatMessage"] * 3


def test_not_modified_is_ok(groupe):
    calls, plan, settings = groupe
    tableau.rafraichir(settings, textes=TEXTES)
    calls.clear()
    plan["editMessageText"] = "Bad Request: message is not modified"
    res = tableau.rafraichir(settings, textes={k: "!" + v for k, v in TEXTES.items()})
    assert res["status"] == "ok" and methodes(calls) == ["editMessageText"] * 3


def test_other_error_keeps_the_id_and_hides_the_token(groupe):
    calls, plan, settings = groupe
    tableau.rafraichir(settings, textes=TEXTES)
    before = (settings.var_dir / "groupe" / "tableau.json").read_text()
    plan["editMessageText"] = f"Too Many Requests {TOKEN}"
    res = tableau.rafraichir(settings, textes={k: "!" + v for k, v in TEXTES.items()})
    assert res["status"] == "error" and TOKEN not in json.dumps(res)
    assert json.loads(before)["systeme"]["message_id"] == json.loads(
        (settings.var_dir / "groupe" / "tableau.json").read_text())["systeme"]["message_id"]


def test_pin_failure_is_retried(groupe):
    calls, plan, settings = groupe
    plan["pinChatMessage"] = "Bad Request: not enough rights to pin a message"
    assert tableau.rafraichir(settings, textes=TEXTES)["status"] == "error"
    plan.clear()
    calls.clear()
    assert tableau.rafraichir(settings, textes=TEXTES)["status"] == "ok"
    assert methodes(calls) == ["pinChatMessage"] * 3


def test_hermes_mode_does_nothing(groupe, monkeypatch):
    calls, _, settings = groupe
    monkeypatch.setenv(envoi.MODE_ENV, "hermes")
    res = tableau.rafraichir(settings, textes=TEXTES)
    assert res["status"] == "skipped" and calls == []


def test_no_group_does_nothing(groupe):
    calls, _, settings = groupe
    (settings.root / "config" / "groupe.yaml").unlink()
    res = tableau.rafraichir(settings, textes=TEXTES)
    assert res["status"] == "skipped" and calls == []


def test_build_texts_by_domain(project, prompts_dir, monkeypatch):
    prompts_dir.mkdir()
    (prompts_dir / "hub.md").write_text("# Hub\n\nDomaine : systeme\n")
    (prompts_dir / "fba.md").write_text("# FBA\n\nDomaine : business\n")
    settings = load_settings()
    with TodoStore(settings.todo_db) as store:
        store.add("Réparer le hub", category="general", chantier="hub")
        store.add("Trouver un produit", category="general", chantier="fba")
        store.add("Sans chantier", category="general")
    monkeypatch.setattr(tableau, "_quota_line", lambda s: "📊 session 1 %")
    textes = tableau.build_texts(settings)
    assert "Réparer le hub" in textes["systeme"] and "Trouver" not in textes["systeme"]
    assert "Trouver un produit" in textes["business"] and "Sans chantier" not in textes["business"]
    assert "Rien d'ouvert" in textes["perso"] and "📊 session 1 %" in textes["perso"]


def _renames(calls):
    return [b for m, b in calls if m == "editForumTopic"]


def _n(systeme):
    return {"systeme": systeme, "business": 0, "perso": 0}


def test_topic_name():
    assert tableau.topic_name("systeme", 0) == "🛠 Système"
    assert tableau.topic_name("systeme", 1) == "🛠 Système · 1 prête"
    assert tableau.topic_name("systeme", 2) == "🛠 Système · 2 prêtes"




def test_rename_when_ready_count_changes_and_not_when_unchanged(groupe):
    calls, _, settings = groupe
    now = [1000.0]
    kw = dict(textes=TEXTES, clock=lambda: now[0])
    tableau.rafraichir(settings, nombres=_n(2), **kw)
    assert [b["name"] for b in _renames(calls)] == ["🛠 Système · 2 prêtes", "💼 Business", "🏠 Perso"]
    assert _renames(calls)[0]["message_thread_id"] == 11 and _renames(calls)[0]["chat_id"] == "-100"
    calls.clear()
    now[0] += 5000
    tableau.rafraichir(settings, nombres=_n(2), **kw)
    assert _renames(calls) == []


def test_rename_waits_ten_minutes(groupe):
    calls, _, settings = groupe
    now = [1000.0]
    kw = dict(textes=TEXTES, clock=lambda: now[0])
    tableau.rafraichir(settings, nombres=_n(1), **kw)
    calls.clear()
    now[0] += 599
    tableau.rafraichir(settings, nombres=_n(0), **kw)
    assert _renames(calls) == []
    now[0] += 1
    tableau.rafraichir(settings, nombres=_n(0), **kw)
    assert [b["name"] for b in _renames(calls)] == ["🛠 Système"]


def test_topic_not_modified_is_ok_and_remembered(groupe):
    calls, plan, settings = groupe
    plan["editForumTopic"] = "Bad Request: TOPIC_NOT_MODIFIED"
    res = tableau.rafraichir(settings, textes=TEXTES, nombres=_n(1), clock=lambda: 50.0)
    assert res["status"] == "ok"
    state = json.loads((settings.var_dir / "groupe" / "tableau.json").read_text())
    assert state["systeme"]["nom"] == "🛠 Système · 1 prête" and state["systeme"]["nom_at"] == 50.0


def test_rename_error_is_isolated_and_retried(groupe):
    calls, plan, settings = groupe
    plan["editForumTopic"] = "Too Many Requests: retry after 5"
    res = tableau.rafraichir(settings, textes=TEXTES, nombres=_n(1), clock=lambda: 50.0)
    assert res["status"] == "error" and len(_renames(calls)) == 3  # every topic tried, boards still posted
    assert methodes(calls).count("sendMessage") == 3
    state = json.loads((settings.var_dir / "groupe" / "tableau.json").read_text())
    assert "nom" not in state["systeme"] and state["systeme"]["message_id"]
    plan.clear()
    calls.clear()
    res = tableau.rafraichir(settings, textes=TEXTES, nombres=_n(1), clock=lambda: 51.0)
    assert res["status"] == "ok" and len(_renames(calls)) == 3
