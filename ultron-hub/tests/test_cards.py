"""Todo #61, lot 4: « écran remplacé » (R8) on the commands of the rescue bot."""

import asyncio
import sys
import types
from datetime import date
from types import SimpleNamespace

import pytest

from hermes_plugin import ui
from tests.test_ui import CHAT, WideClient

TODAY = date(2026, 10, 11)


def run(coro):
    return asyncio.run(coro)


def cbs(screen):
    return [d for row in screen.buttons for _, d in row]


class FakeBot:
    def __init__(self, broken=()):
        self.edits, self.broken, self.next_id = [], set(broken), 100

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        if message_id in self.broken:
            raise RuntimeError("Message can't be edited")
        self.edits.append((chat_id, message_id, text, reply_markup))


def test_screen_memory_replaces_the_previous_screen_of_the_same_chat():
    memory, bot = ui.ScreenMemory(), FakeBot()
    run(memory.opened(bot, 1, 10))
    assert bot.edits == []
    run(memory.opened(bot, 1, 11))
    run(memory.opened(bot, 2, 50))  # another chat: untouched
    run(memory.opened(bot, 1, 11))  # same message again: nothing to replace
    run(memory.opened(bot, 1, 12))
    assert bot.edits == [(1, 10, "(écran remplacé)", None), (1, 11, "(écran remplacé)", None)]


def test_a_failed_edit_is_silent_and_the_new_screen_is_still_remembered():
    memory, bot = ui.ScreenMemory(), FakeBot(broken={10})
    run(memory.opened(bot, 1, 10))
    run(memory.opened(bot, 1, 11))  # editing 10 fails: no exception
    run(memory.opened(bot, 1, 12))
    assert [e[1] for e in bot.edits] == [11]


def wire_plugin(monkeypatch):
    handlers = {}

    class _Filter:
        def __and__(self, other):
            return self

        def __or__(self, other):
            return self

        def __invert__(self):
            return self

    class _MessageFilter(_Filter):
        def __init__(self):
            pass

    class _Command:
        def __init__(self, name, fn, **kw):
            handlers[name] = fn

    class _Other:
        def __init__(self, *args, **kw):
            pass

    ext = types.ModuleType("telegram.ext")
    ext.CommandHandler, ext.MessageHandler, ext.CallbackQueryHandler = _Command, _Other, _Other
    ext.filters = SimpleNamespace(MessageFilter=_MessageFilter, TEXT=_Filter(), COMMAND=_Filter(), VOICE=_Filter(), AUDIO=_Filter(), PHOTO=_Filter(),
                                        Document=SimpleNamespace(IMAGE=_Filter(), ALL=_Filter()),
                                  User=lambda user_id: _Filter())
    tg = types.ModuleType("telegram")
    tg.InlineKeyboardButton = lambda label, callback_data: (label, callback_data)
    tg.InlineKeyboardMarkup = lambda rows: rows
    tg.ext = ext
    monkeypatch.setitem(sys.modules, "telegram", tg)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "7")
    import hermes_plugin
    monkeypatch.setattr(hermes_plugin, "hermes_will_announce", lambda: True)
    monkeypatch.setattr(hermes_plugin, "HubClient", WideClient)
    wires = []
    hermes_plugin.register(SimpleNamespace(register_platform_handler=lambda name, fn: wires.append(fn),
                                           register_command=lambda *a, **k: None))
    wires[0](SimpleNamespace(add_handler=lambda handler: None), None)
    return handlers


class FakeMessage:
    counter = 100

    def __init__(self, chat_id, sent):
        self.chat_id, self.sent = chat_id, sent

    async def reply_text(self, text, reply_markup=None):
        FakeMessage.counter += 1
        self.sent.append((FakeMessage.counter, text, reply_markup))
        return SimpleNamespace(message_id=FakeMessage.counter)


def update_for(chat_id, sent):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), effective_message=FakeMessage(chat_id, sent))


def test_every_command_replaces_the_previous_command_screen(monkeypatch):
    handlers = wire_plugin(monkeypatch)
    assert set(handlers) == {"todo", "sante", "usage", "atoi", "plus", "start", "telegram"}
    bot, sent = FakeBot(), []
    ctx = SimpleNamespace(bot=bot)
    order = ["todo", "sante", "usage", "atoi", "plus", "start"]
    for name in order:
        run(handlers[name](update_for(CHAT, sent), ctx))
    ids = [m[0] for m in sent]
    assert [e[1] for e in bot.edits] == ids[:-1]  # each command replaced the one before it, in order
    assert all(e[0] == CHAT and e[2] == "(écran remplacé)" and e[3] is None for e in bot.edits)
    run(handlers["todo"](update_for(99, sent), ctx))  # another chat: nothing replaced
    assert len(bot.edits) == len(ids) - 1


def test_a_notification_is_never_replaced(monkeypatch):
    """A notification is sent by Hermes or the hub, not by a command: it is never remembered, so never edited."""
    handlers = wire_plugin(monkeypatch)
    bot, sent = FakeBot(), []
    ctx = SimpleNamespace(bot=bot)
    run(handlers["todo"](update_for(CHAT, sent), ctx))
    notification_id = 5  # e.g. an alert sent in between
    run(handlers["sante"](update_for(CHAT, sent), ctx))
    assert notification_id not in [e[1] for e in bot.edits]
    assert [e[1] for e in bot.edits] == [sent[0][0]]
