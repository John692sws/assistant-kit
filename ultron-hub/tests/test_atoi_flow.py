"""/atoi (📥 À toi) of the rescue bot: text only, pointing to the dashboard (todo #463)."""

import asyncio
from pathlib import Path

from cli_contract import check_argv
from hermes_plugin import ui
from hermes_plugin.atoi_flow import AtoiFlow
from hermes_plugin.atoi_flow import PLUS_CB, PLUS_DOORS

CHAT = 5
PLUGIN = Path(__file__).resolve().parent.parent / "hermes_plugin"


def run(coro):
    return asyncio.run(coro)


def ok(data=None, summary="ok"):
    return {"status": "ok", "summary": summary, "data": data, "error": None}


def item(i, kind="fusionner", chantier="al-kitab"):
    if kind == "question":
        return {"kind": kind, "icone": "❓", "question": f"Q{i}", "texte": f"Question {i}", "chantier": chantier}
    return {"kind": kind, "icone": "🔀", "todo": i, "texte": f"Todo {i}", "chantier": chantier}


def question(qid="Q7", **kw):
    q = {"id": qid, "chantier": "fba", "escouade": "fba-1", "session": 2, "type": "choix", "bloquante": True,
         "texte": "Quel nom pour le module ?", "choix": ["alpha", "beta", "gamma"], "conseille": 2, "commande": None,
         "echeance": None, "statut": "ouverte", "repondue": False}
    q.update(kw)
    return q


class Client:
    def __init__(self, items=None, questions=None):
        self.items = [item(1), item(2, "question")] if items is None else items
        self.questions = [question()] if questions is None else questions
        self.calls = []
        self.answers = []  # (args, env, stdin_text)

    async def call(self, *args, timeout_s=None, env=None, stdin_text=None):
        check_argv(args)
        self.calls.append(args)
        if args[:2] == ("chantier", "atoi"):
            items = self.items
            if "--chantier" in args:
                name = args[args.index("--chantier") + 1]
                items = [i for i in items if i["chantier"] == name]
            return ok({"items": items, "total": len(items), "avertissements": []})
        if args[:2] == ("escouade", "questions"):
            return ok({"questions": self.questions})
        if args[:2] == ("escouade", "repondre"):
            self.answers.append((args, env, stdin_text))
            return ok({"id": args[2]})
        if args[:2] == ("todo", "show"):
            return ok({"id": int(args[2]), "title": "Une todo", "priority": "normal", "status": "todo",
                       "due_date": None, "badges": "", "nature": "fond", "missions": [], "branch": None, "notes": "n"})
        return {"status": "error", "summary": "", "data": None, "error": "inconnu"}


def buttons(screen):
    return [b for row in screen.buttons for b in row]


def check_screen(screen):
    assert screen.buttons[-1][-1] == (ui.HOME, ui.HOME_CB)
    for label, data in buttons(screen):
        assert len(data.encode()) <= 64, data
        assert not any(word in label for word in ui.FORBIDDEN_WORDS), label


# -- list ---------------------------------------------------------------------------------------------

def test_empty_list():
    screen = run(AtoiFlow(Client(items=[])).on_command(CHAT))
    check_screen(screen)
    assert "Rien ne t'attend." in screen.text and "dashboard" in screen.text
    assert screen.buttons[-2] == [("⋯ Plus", "hub:at:plus")]
    assert screen.buttons[-1] == [(ui.HOME, ui.HOME_CB)]  # no parent: no « Retour »


def test_list_is_text_only_and_points_to_the_dashboard():
    items = [item(1), item(2, "question"), item(3, chantier="-")]
    screen = run(AtoiFlow(Client(items=items)).on_callback(CHAT, "hub:at:list"))
    check_screen(screen)
    assert screen.text.startswith("📥 À toi (3)")
    assert "🔀 al-kitab · #1 Todo 1" in screen.text and "❓ al-kitab · Question 2" in screen.text
    assert "🔀 Hors chantier · #3 Todo 3" in screen.text
    assert "dashboard" in screen.text
    data = [d for _, d in buttons(screen)]
    assert data == ["hub:at:plus", ui.HOME_CB]  # no merge, answer or card button: the dashboard does it


def test_ready_branches_are_a_text_line_not_a_button():
    class Ready(Client):
        async def call(self, *args, **kw):
            if args[:2] == ("branch", "lot"):
                return ok({"lot": [{"todo_id": 1}, {"todo_id": 2}]})
            return await super().call(*args, **kw)
    screen = run(AtoiFlow(Ready()).on_callback(CHAT, "hub:at:list"))
    assert "2 branche(s) prête(s)" in screen.text
    assert not any("fusionner" in label.lower() for label, _ in buttons(screen))


def test_list_is_paginated():
    flow = AtoiFlow(Client(items=[item(i) for i in range(1, 21)]))
    first = run(flow.on_callback(CHAT, "hub:at:list"))
    check_screen(first)
    assert ("▶ Suite", "hub:at:p:1") in buttons(first) and not any(label == "◀ Avant" for label, _ in buttons(first))
    last = run(flow.on_callback(CHAT, "hub:at:p:2"))
    check_screen(last)
    assert ("◀ Avant", "hub:at:p:1") in buttons(last) and not any(label == "▶ Suite" for label, _ in buttons(last))
    assert "page 3/3" in last.text


def test_unknown_action_is_refused():
    for data in ("hub:at:c:fba", "hub:at:cp:fba:1", "hub:at:zz", "hub:at:p:x"):
        screen = run(AtoiFlow(Client()).on_callback(CHAT, data))
        assert screen.text == "Action inconnue."


def test_one_call_at_a_time_per_chat():
    class Slow(Client):
        async def call(self, *args, **kw):
            await asyncio.sleep(0)
            return await super().call(*args, **kw)

    async def go():
        flow = AtoiFlow(Slow())
        return await asyncio.gather(flow.on_command(CHAT), flow.on_command(CHAT))
    first, second = run(go())
    assert "📥 À toi" in first.text and "Un instant" in second.text


def test_cli_failure_has_a_footer():
    class Broken(Client):
        async def call(self, *args, **kw):
            return {"status": "error", "summary": "", "data": None, "error": "boum"}
    screen = run(AtoiFlow(Broken()).on_command(CHAT))
    assert "boum" in screen.text and screen.buttons[-1][-1] == (ui.HOME, ui.HOME_CB)


def test_plus_screen_has_only_the_rescue_doors():
    assert [label for label, _ in PLUS_DOORS] == ["🎯 Toutes les todos", "🩺 Santé", "📊 Usage"]
    screen = run(AtoiFlow(Client()).on_callback(CHAT, PLUS_CB))
    check_screen(screen)
    assert PLUS_CB == "hub:at:plus" and (ui.BACK, "hub:at:list") in screen.buttons[-1]


def test_commands_are_wired():
    source = (PLUGIN / "__init__.py").read_text(encoding="utf-8")
    assert 'register_command("atoi"' in source and "ATOI_PREFIX" in source
    assert 'register_command("plus"' in source and 'CommandHandler("plus", on_plus_command' in source
