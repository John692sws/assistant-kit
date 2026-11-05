"""`ultron-hub bot run` (todo #193): the plugin's `_wire` on a fake PTB Application, no network, the token never shown."""

import asyncio
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from ultron_hub import cli
from ultron_hub.bot import run as bot

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456:SECRET-test-token-xyz"


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def fake_ptb(monkeypatch):
    """Fake `telegram` / `telegram.ext`: the builder and run_polling record their arguments, post_init runs once."""
    seen = SimpleNamespace(token=None, polling=None, apps=[], users=[])

    class _Filter:
        def __and__(self, other):
            return self

        __or__ = __and__

        def __invert__(self):
            return self

    class _MessageFilter(_Filter):
        pass

    def _user(user_id):
        seen.users.append(list(user_id))
        return _Filter()

    class _Handler:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    class FakeApp:
        running = False  # the plugin's background tasks wait for it, then give up when the loop closes

        def __init__(self, post_init, post_shutdown):
            self.post_init, self.post_shutdown = post_init, post_shutdown
            self.handlers, self.error_handlers = [], []
            self.bot = SimpleNamespace()

        def add_handler(self, handler, group=0):
            self.handlers.append((group, handler))

        def add_error_handler(self, fn):
            self.error_handlers.append(fn)

        def run_polling(self, **kwargs):
            seen.polling = kwargs

            async def go():
                await self.post_init(self)
                await self.post_shutdown(self)
            asyncio.run(go())

    class Builder:
        def __init__(self):
            self._post_init = self._post_shutdown = None

        def token(self, value):
            seen.token = value
            return self

        def post_init(self, fn):
            self._post_init = fn
            return self

        def post_shutdown(self, fn):
            self._post_shutdown = fn
            return self

        def build(self):
            app = FakeApp(self._post_init, self._post_shutdown)
            seen.apps.append(app)
            return app

    ext = types.ModuleType("telegram.ext")
    ext.Application = SimpleNamespace(builder=Builder)
    ext.CommandHandler = ext.MessageHandler = ext.CallbackQueryHandler = ext.TypeHandler = _Handler
    ext.filters = SimpleNamespace(MessageFilter=_MessageFilter, TEXT=_Filter(), COMMAND=_Filter(), VOICE=_Filter(),
                                  AUDIO=_Filter(), PHOTO=_Filter(), Document=SimpleNamespace(IMAGE=_Filter(), ALL=_Filter()),
                                  User=_user)
    tg = types.ModuleType("telegram")
    tg.InlineKeyboardButton = tg.InlineKeyboardMarkup = object
    tg.Update = SimpleNamespace(ALL_TYPES=["message", "callback_query"])
    tg.ext = ext
    monkeypatch.setitem(sys.modules, "telegram", tg)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    import hermes_plugin
    monkeypatch.setattr(hermes_plugin, "hermes_will_announce", lambda: True)
    return seen


def test_missing_token_is_a_clear_refusal(fake_ptb):
    out = bot.run(environ={"TELEGRAM_ALLOWED_USERS": "7"})
    assert out["status"] == "error" and out["error"] == bot.NO_TOKEN and out["exit_code"] == bot.EX_CONFIG
    assert "TELEGRAM_BOT_TOKEN" in out["error"] and fake_ptb.token is None and not fake_ptb.apps


def test_cli_without_token_prints_json_and_exits_78(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    code = cli.main(["bot", "run"])
    payload = json.loads(capsys.readouterr().out)
    assert code == bot.EX_CONFIG and payload["command"] == "bot.run" and payload["error"] == bot.NO_TOKEN
    assert "exit_code" not in payload


def test_blank_token_counts_as_missing():
    assert bot.read_token({"TELEGRAM_BOT_TOKEN": "  \n"}) is None


def test_wire_hooks_onto_a_fake_application(fake_ptb, capsys, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "7, 42")  # read by the plugin's allowed_user_ids(), as under Hermes
    out = bot.run(environ={"TELEGRAM_BOT_TOKEN": TOKEN})
    assert out["status"] == "ok" and out["exit_code"] == 0
    assert fake_ptb.token == TOKEN
    assert fake_ptb.polling["bootstrap_retries"] == -1 and fake_ptb.polling["allowed_updates"]
    app = fake_ptb.apps[0]
    assert len(app.handlers) == 12 and app.error_handlers  # the rescue flows of the plugin (todo #463)
    assert fake_ptb.users and all(sorted(u) == [7, 42] for u in fake_ptb.users)  # same allowlist as the plugin
    import hermes_plugin
    assert hermes_plugin.hermes_will_announce() is False  # Hermes off Telegram: we announce the reconnection
    err = capsys.readouterr().err
    assert "flux du plugin branchés" in err
    assert TOKEN not in err and TOKEN not in json.dumps(out)


def test_empty_allowlist_refuses_to_start(fake_ptb, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
    out = bot.run(environ={"TELEGRAM_BOT_TOKEN": TOKEN})
    assert out["error"] == bot.NO_ALLOWED and out["exit_code"] == bot.EX_CONFIG and not fake_ptb.apps


def test_missing_ptb_is_a_clear_refusal(monkeypatch):
    monkeypatch.setitem(sys.modules, "telegram.ext", None)  # import raises ImportError
    out = bot.run(environ={"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_USERS": "7"})
    assert out["error"] == bot.NO_PTB and out["exit_code"] == bot.EX_CONFIG
    assert TOKEN not in json.dumps(out)


def test_token_is_masked_in_every_log_record(capsys):
    bot.setup_logging(TOKEN)
    logging.getLogger("httpx").warning("HTTP Request: POST https://api.telegram.org/bot%s/getUpdates", TOKEN)
    try:
        raise RuntimeError(f"bad url /bot{TOKEN}/x")
    except RuntimeError:
        logging.getLogger("telegram.ext").exception("boom")
    err = capsys.readouterr().err
    assert TOKEN not in err and err.count(bot.MASK) == 2
    assert logging.getLogger("httpx").level == logging.WARNING  # its INFO lines carry the token in the URL


def test_context_accepts_hermes_only_registrations():
    ctx = bot.BotContext()
    ctx.register_command("todo", handler=lambda raw: "", description="Ma todo")
    ctx.register_hook("pre_gateway_dispatch", lambda *a: None)
    ctx.register_system_prompt_section("profil", "texte", max_chars=10)
    ctx.register_something_new("x")
    assert ctx.commands == [("todo", "Ma todo")]
    assert ctx.ignored == ["hook:pre_gateway_dispatch", "section:profil", "register_something_new"]
    with pytest.raises(AttributeError):
        ctx.other


def test_unit_takes_the_token_from_systemd_and_is_not_a_loop():
    unit = (ROOT / "deploy" / "systemd" / "ultron-bot.service").read_text()
    assert "EnvironmentFile=%h/.hermes/.env" in unit
    assert "ExecStart=" in unit and "/.venv/bin/ultron-hub bot run" in unit
    assert "RestartPreventExitStatus=78" in unit and "Conflicts=hermes-gateway.service" in unit
    assert "TELEGRAM_BOT_TOKEN=" not in unit


def test_source_never_reads_the_env_file():
    source = (ROOT / "src" / "ultron_hub" / "bot" / "run.py").read_text()
    assert "open(" not in source and "read_text" not in source and "dotenv" not in source


def test_bot_context_declares_itself_standalone():
    """todo #196 : le plugin branche le texte libre seulement si le ctx le dit ; Hermes n'a pas cet attribut."""
    from ultron_hub.bot.run import BotContext

    assert BotContext().standalone is True
