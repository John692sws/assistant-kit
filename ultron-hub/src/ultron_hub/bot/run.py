"""`ultron-hub bot run` : le bot Telegram à nous, sans Hermes (todo #193, spec ~/notes/spec-porte-directe.md).

Construit l'`Application` python-telegram-bot (long polling), appelle le même `register(ctx)` que Hermes avec un ctx
minimal (`BotContext`), puis le `_wire(application, adapter)` du plugin dans `post_init` (boucle déjà lancée : les
tâches du plugin, « ✅ Bot reconnecté. », démarrent comme sous Hermes).

Jeton : seulement la variable d'environnement TELEGRAM_BOT_TOKEN (systemd `EnvironmentFile=%h/.hermes/.env`). Il n'est
jamais écrit ni journalisé : httpx (qui journalise les URL `…/bot<jeton>/…`) est réduit à WARNING et un filtre masque le
jeton dans tout enregistrement. Autorisés : `hermes_plugin.allowed_user_ids()` (TELEGRAM_ALLOWED_USERS), comme le plugin.
"""

import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

from ultron_hub.bot import voice

log = logging.getLogger("ultron_hub.bot")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
EX_CONFIG = 78  # systemd: RestartPreventExitStatus=78, une config absente ne relance pas en boucle
MASK = "<jeton masqué>"

NO_TOKEN = (f"{TOKEN_ENV} absent de l'environnement : le bot ne démarre pas. Il vient de systemd "
            "(EnvironmentFile=%h/.hermes/.env dans ultron-bot.service) ; aucun fichier n'est lu par ultron-hub.")
NO_ALLOWED = ("TELEGRAM_ALLOWED_USERS vide ou absent : aucun utilisateur autorisé, le bot ne démarre pas "
              "(même liste que le plugin Hermes).")
NO_PTB = ("python-telegram-bot absent du .venv d'ultron-hub : installer `.venv/bin/pip install -e '.[bot]'` "
          "(même version majeure que Hermes, 22.x).")


class BotContext:
    """Le ctx minimal que `hermes_plugin.register` attend de Hermes.

    Utilisés : `register_platform_handler` (le `_wire` Telegram) ; `register_command` (replis texte hors Telegram) est
    seulement noté : le menu `/` reste celui que Hermes a posé chez Telegram (start, plus). Acceptés et ignorés, propres à Hermes : `register_hook`,
    `register_system_prompt_section` et tout autre `register_*`.
    """

    standalone = True  # sans Hermes (secours strict, todo #463 : le plugin ne branche plus de texte libre)

    def __init__(self, voice_transcriber: Callable | None = None):
        # Sa présence dit au plugin qu'il tourne sans Hermes (todo #195) : vocaux transcrits ici, 💬 Parler -> 🚀 Demande.
        self.voice_transcriber = voice_transcriber
        self.platform_handlers: dict[str, Callable] = {}
        self.commands: list[tuple[str, str]] = []
        self.ignored: list[str] = []

    def register_platform_handler(self, platform: str, handler: Callable) -> None:
        self.platform_handlers[platform] = handler

    def register_command(self, name: str, handler: Callable | None = None, description: str = "", **_: Any) -> None:
        self.commands.append((name, description))

    def register_hook(self, name: str, *_: Any, **__: Any) -> None:
        self.ignored.append(f"hook:{name}")

    def register_system_prompt_section(self, name: str, *_: Any, **__: Any) -> None:
        self.ignored.append(f"section:{name}")

    def __getattr__(self, name: str):  # un register_* ajouté plus tard au plugin ne doit pas casser le bot
        if name.startswith("register_"):
            return lambda *a, **k: self.ignored.append(name)
        raise AttributeError(name)


class TokenRedactor(logging.Filter):
    """Remplace le jeton par MASK dans tout enregistrement (message et arguments formatés, trace d'exception)."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.token:
            return True
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        if record.stack_info:
            message += "\n" + record.stack_info
            record.stack_info = None
        record.msg, record.args = message.replace(self.token, MASK), None
        return True


def setup_logging(token: str, stream=None) -> logging.Handler:
    """Journal sur stderr (journald sous systemd), jeton masqué partout."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(TokenRedactor(token))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore"):  # httpx journalise chaque URL, jeton compris, au niveau INFO
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return handler


def read_token(environ=os.environ) -> str | None:
    token = (environ.get(TOKEN_ENV) or "").strip()
    return token or None


def load_plugin():
    """Importe hermes_plugin/ depuis la racine du projet (il n'est pas dans src/)."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    return importlib.import_module("hermes_plugin")


def build_application(token: str, plugin, ctx: BotContext):
    """L'Application PTB, le plugin enregistré ; `_wire` est appelé dans post_init (boucle lancée)."""
    from telegram.ext import Application

    # Sous ultron-bot, Hermes ne tient pas Telegram : ses marqueurs de redémarrage (HERMES_HOME) ne doivent pas faire
    # taire « ✅ Bot reconnecté. », c'est nous qui l'envoyons.
    plugin.hermes_will_announce = lambda: False
    plugin.register(ctx)
    wire = ctx.platform_handlers.get("telegram")
    if wire is None:
        raise RuntimeError("le plugin n'a pas déclaré de handler Telegram")

    async def post_init(application) -> None:
        wire(application, None)  # adapter : propre à Hermes, inutilisé par le plugin
        log.info("bot: flux du plugin branchés (%d commandes, ignoré hors Hermes : %s)",
                 len(ctx.commands), ", ".join(ctx.ignored) or "rien")

    async def post_shutdown(application) -> None:
        log.info("bot: arrêté proprement")

    async def on_error(update, context) -> None:  # le type seulement : jamais le contenu d'un message
        log.error("bot: erreur dans un handler (%s)", type(context.error).__name__)

    application = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_error_handler(on_error)
    return application


def run(environ=os.environ) -> dict[str, Any]:
    """Lance le bot jusqu'à SIGTERM/SIGINT. Rend une enveloppe JSON ; `exit_code` pour la CLI."""
    token = read_token(environ)
    if token is None:
        return _envelope("error", NO_TOKEN, EX_CONFIG)
    setup_logging(token)
    try:
        importlib.import_module("telegram.ext")
    except ImportError:
        log.error("bot: %s", NO_PTB)
        return _envelope("error", NO_PTB, EX_CONFIG)
    plugin = load_plugin()
    allowed = plugin.allowed_user_ids()
    if not allowed:
        log.error("bot: %s", NO_ALLOWED)
        return _envelope("error", NO_ALLOWED, EX_CONFIG)
    from telegram import Update

    application = build_application(token, plugin, BotContext(voice.Transcriber()))
    log.info("bot: démarrage du long polling pour %d utilisateur(s) autorisé(s)", len(allowed))
    # bootstrap_retries=-1 : au démarrage, réessaie sans fin si le réseau manque ; ensuite l'Updater de PTB
    # réessaie seul après une coupure. SIGTERM (systemctl stop) / SIGINT : arrêt propre (stop, shutdown).
    application.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1, close_loop=False)
    return _envelope("ok", "bot arrêté", 0)


def _envelope(status: str, message: str, exit_code: int) -> dict[str, Any]:
    error = None if status == "ok" else message
    return {"status": status, "command": "bot.run", "summary": message if status == "ok" else "bot non démarré",
            "data": None, "error": error, "exit_code": exit_code}
