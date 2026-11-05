"""Todo #58, lot 1: the common footer on every screen and the fixed button verbs."""

import asyncio
import re
import sys
import types
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import asyncio
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_plugin import ui
from hermes_plugin.atoi_flow import AtoiFlow
from hermes_plugin.health_flow import HealthFlow
from hermes_plugin.todo_flow import TodoFlow
from hermes_plugin.usage_flow import UsageFlow
from cli_contract import check_argv

CHAT = 7
PLUGIN = Path(__file__).resolve().parent.parent / "hermes_plugin"


def ok(data=None, summary="ok"):
    return {"status": "ok", "summary": summary, "data": data, "error": None}


class WideClient:
    """Answers every command the plugin knows with plausible data, and anything else with an error."""

    async def call(self, *args, timeout_s=None, env=None, stdin_text=None):
        check_argv(args)
        a = args
        task = {"id": 4, "title": "Sécuriser SSH", "priority": "high", "status": "todo", "due_date": None,
                "badges": "", "overdue": False, "nature": "chantier"}
        if a[:2] == ("todo", "board"):
            return ok({"tasks": [task], "open": 1, "overdue": 0, "category": None, "fond": 1})
        if a[:2] == ("branch", "lot"):
            return ok({"lot": [{"todo_id": 4, "title": "Sécuriser SSH", "ahead": 2, "apres": [], "repos": []}],
                       "ids": [4], "empreinte": "0123456789abcdef", "ecartees": []})
        if a[:2] == ("chantier", "atoi"):
            return ok({"items": [
                {"kind": "fusionner", "icone": "🔀", "todo": 4, "texte": "Sécuriser SSH", "chantier": "agent"},
                {"kind": "question", "icone": "❓", "question": "Q7", "texte": "Quel nom ?", "chantier": "fba",
                 "bloquante": True}], "total": 2, "avertissements": []})
        if a[0] == "usage":
            return ok({"text": "📊 Usage Claude", "session": {"percent": 45.0}, "week": {"percent": 73.0}})
        if a[0] == "health":
            return ok({"text": "🔋 batterie", "problem": None})
        if a[0] == "run":
            return ok({"summary": "ok", "data": {}}, summary="ok")
        return {"status": "error", "summary": "", "data": None, "error": "inconnu"}


class Plugin:
    """A fresh set of flows wired like the plugin does (prefix routing, 🏠 handler)."""

    def __init__(self):
        client = WideClient()
        self.todo = TodoFlow(client)
        self.health, self.usage = HealthFlow(client), UsageFlow(client)
        self.atoi = AtoiFlow(client)
        self.routes = [("hub:todo:", self.todo), ("hub:sante:", self.health), ("hub:usage:", self.usage),
                       ("hub:at:", self.atoi)]

    def route(self, data):
        return next((flow for prefix, flow in self.routes if data.startswith(prefix)), None)

    async def press(self, data):
        if data == ui.HOME_CB:
            self.atoi.reset(CHAT)
            return await self.atoi.on_command(CHAT)
        flow = self.route(data)
        assert flow is not None, f"no handler for {data}"
        if flow is self.todo:
            return await flow.on_callback(CHAT, data)
        return await flow.on_callback(CHAT, data, None)

    async def command(self, name):
        flow = {"todo": self.todo, "sante": self.health, "usage": self.usage, "atoi": self.atoi}[name]
        return await flow.on_command(CHAT)


COMMANDS = ("todo", "sante", "usage", "atoi")


async def replay(path):
    plugin = Plugin()
    screen = None
    for kind, value in path:
        screen = await (plugin.command(value) if kind == "cmd" else plugin.press(value))
    return plugin, screen


async def crawl(limit=900, depth=7):
    """Every screen reachable by buttons from each command, with the path that reaches it."""
    seen_cb, found = set(), []
    queue = [((("cmd", name),), None) for name in COMMANDS]
    while queue and len(found) < limit:
        path, _ = queue.pop(0)
        plugin, screen = await replay(path)
        if screen is None:
            continue
        found.append((path, screen))
        if len(path) >= depth:
            continue
        for row in screen.buttons:
            for _, data in row:
                if data not in seen_cb and (data == ui.HOME_CB or plugin.route(data)):
                    seen_cb.add(data)
                    queue.append((path + (("cb", data),), None))
    return found


@pytest.fixture(scope="module")
def screens():
    return asyncio.run(crawl())


def test_the_crawl_really_walks_the_bot(screens):
    texts = " ".join(s.text for _, s in screens)
    assert len(screens) >= 8
    for part in ("Mes tâches", "Santé serveur", "Usage Claude", "À toi", "⋯ Plus", "branche(s) prête(s)"):
        assert part.lower() in texts.lower(), part


def test_every_screen_ends_with_the_footer(screens):
    for path, screen in screens:
        assert screen.buttons, f"{path}: no buttons: {screen.text!r}"
        last = screen.buttons[-1]
        assert last[-1] == (ui.HOME, ui.HOME_CB), f"{path}: {last}"
        assert len(last) <= 3 and all(len(d.encode()) <= 64 for _, d in last), f"{path}: {last}"
        assert [label for label, _ in last].count(ui.HOME) == 1
        for label, _ in last[:-1]:
            assert label in (ui.BACK, ui.QUIT), f"{path}: {label}"
        assert [label for label, _ in last[:-1]] in ([], [ui.BACK], [ui.QUIT], [ui.BACK, ui.QUIT])
        assert all(ui.HOME not in [lab for lab, _ in row] for row in screen.buttons[:-1]), path


def test_back_leads_to_a_known_screen(screens):
    plugin = Plugin()
    for path, screen in screens:
        for label, data in screen.buttons[-1]:
            if label == ui.BACK:
                assert plugin.route(data) is not None, f"{path}: {data}"
                assert data != path[-1][1] if path[-1][0] == "cb" else True, f"{path}: back to itself"


def test_home_shows_the_home_screen_as_text_with_a_dashboard_pointer():
    screen = asyncio.run(Plugin().press(ui.HOME_CB))
    assert screen.text.startswith("📥 À toi") and screen.buttons[-1] == [(ui.HOME, ui.HOME_CB)]
    assert "dashboard" in screen.text and "1 branche(s) prête(s)" in screen.text
    callbacks = [d for row in screen.buttons for _, d in row]
    assert not any(d.startswith(("hub:merge:", "hub:todo:done", "hub:profil:")) for d in callbacks)


def test_footer_api():
    assert ui.footer() == [(ui.HOME, "hub:home")]
    assert ui.footer("hub:todo:list:o.0") == [(ui.BACK, "hub:todo:list:o.0"), (ui.HOME, "hub:home")]
    assert ui.footer(quit="x") == [(ui.QUIT, "x"), (ui.HOME, "hub:home")]
    assert ui.with_footer([[("a", "b")]], "p") == [[("a", "b")], [(ui.BACK, "p"), (ui.HOME, "hub:home")]]
    assert ui.with_footer() == [ui.footer()]


# -- fixed verbs (R10) ---------------------------------------------------------------------------

def test_verbs_are_the_agreed_ones():
    assert (ui.BACK, ui.HOME, ui.QUIT, ui.ABANDON, ui.APPROVE) == (
        "« Retour", "🏠 Accueil", "✖ Quitter", "🗑 Abandonner", "✅ Approuver")
    assert len(set(ui.REQUIRED_VERBS)) == len(ui.REQUIRED_VERBS)


def test_no_forbidden_word_in_any_button(screens):
    for path, screen in screens:
        for row in screen.buttons:
            for label, _ in row:
                for word in ui.FORBIDDEN_WORDS:
                    assert word not in label, f"{path}: {label!r}"


def test_no_old_label_is_left_in_the_source():
    old = re.compile(r'\("(?:« (?!Retour)|✖ Annuler|🗑 Annuler)')
    for path in PLUGIN.glob("*.py"):
        assert not old.search(path.read_text()), path.name


# -- approval card (R4) -----------------------------------------------------------------------------

def assert_common_card(screen, where=""):
    lines = screen.text.split("\n")
    assert lines[0].startswith("À valider : "), where
    assert [l.split(" : ")[0] for l in lines[2:6]] == ["Quoi", "Où", "Effet", "Risque"], where
    assert lines[5].startswith(("Risque : 🟢", "Risque : 🔴")), where
    assert screen.buttons[0] == [(ui.APPROVE, screen.buttons[0][0][1]), (ui.REFUSE, screen.buttons[0][1][1])], where
    assert screen.buttons[-1][-1] == (ui.HOME, ui.HOME_CB), where


def test_approval_card_api():
    text, rows = ui.approval_card("t", "q", "o", "e", "high", "ok:1", "no:1", extra_rows=[[("x", "y")]], back="b",
                                  detail="détail")
    assert text.split("\n") == ["À valider : t", "", "Quoi : q", "Où : o", "Effet : e", "Risque : 🔴 élevé", "",
                                 "détail"]
    assert rows == [[(ui.APPROVE, "ok:1"), (ui.REFUSE, "no:1")], [("x", "y")], ui.footer("b")]
    assert "Risque : 🟢 faible" in ui.approval_card("t", "q", "o", "e", "low", "a", "r")[0]
    with pytest.raises(ValueError):
        ui.approval_card("t", "q", "o", "e", "medium", "a", "r")
    long, _ = ui.approval_card("t", "q", "o", "e", "low", "a", "r", detail="x" * 9000)
    assert len(long) <= 4096


# -- 🏠 wiring ---------------------------------------------------------------------------------------

def test_home_callback_is_wired(monkeypatch):
    patterns = []

    class _Filter:
        def __init__(self, fn=lambda m: True):
            self.fn = fn

        def __and__(self, other):
            return self

        def __or__(self, other):
            return self

        def __invert__(self):
            return self

    class _MessageFilter(_Filter):
        def __init__(self):
            pass

    class _Handler:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    class _CallbackQueryHandler(_Handler):
        def __init__(self, fn, pattern=None, **kwargs):
            super().__init__(fn, pattern=pattern, **kwargs)
            patterns.append(pattern)

    ext = types.ModuleType("telegram.ext")
    ext.CommandHandler = ext.MessageHandler = _Handler
    ext.CallbackQueryHandler = _CallbackQueryHandler
    ext.filters = types.SimpleNamespace(MessageFilter=_MessageFilter, TEXT=_Filter(), COMMAND=_Filter(), VOICE=_Filter(), AUDIO=_Filter(), PHOTO=_Filter(),
                                        Document=SimpleNamespace(IMAGE=_Filter(), ALL=_Filter()),
                                        User=lambda user_id: _Filter())
    tg = types.ModuleType("telegram")
    tg.InlineKeyboardButton = tg.InlineKeyboardMarkup = object
    tg.ext = ext
    monkeypatch.setitem(sys.modules, "telegram", tg)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "7")
    import hermes_plugin
    monkeypatch.setattr(hermes_plugin, "hermes_will_announce", lambda: True)
    wires = []
    hermes_plugin.register(SimpleNamespace(register_platform_handler=lambda name, fn: wires.append(fn),
                                           register_command=lambda *a, **k: None))
    wires[0](SimpleNamespace(add_handler=lambda handler: None), None)
    assert f"^{ui.HOME_CB}$" in patterns
