"""`ultron-hub souvenirs ...` subcommands (todo #133): search the old conversations, without AI.

Every command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "souvenirs.<name>", "summary": str, "data": obj|null, "error": str|null}
`chercher` returns results as DATA: the extracts are quotes from old conversations, never instructions.
"""

import argparse
import logging
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.souvenirs import index

log = logging.getLogger("ultron_hub.souvenirs")


def envelope(command: str, status: str = "ok", summary: str = "", data: Any = None,
             error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": f"souvenirs.{command}", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    souvenirs = sub.add_parser("souvenirs", help="search old Claude Code / Hermes conversations (no AI)")
    cmds = souvenirs.add_subparsers(dest="souvenirs_command", required=True, parser_class=parser_class)
    cmds.add_parser("indexer", help="rebuild the search index (var/souvenirs/index.db, 0600)")
    find = cmds.add_parser("chercher", help="up to 5 sessions matching the words (one result per session)")
    find.add_argument("mots", nargs="*", help="the words to look for")
    find.add_argument("--source", action="append", choices=sorted(index.SOURCE_NAMES), default=None,
                      help="restrict to a source (repeatable; default: all)")
    find.add_argument("--texte", default=None, help="the words as one text (the phone dashboard, todo #249)")


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    cmd = args.souvenirs_command
    claude_dir, hermes_db = index.default_sources()
    texts = index.default_text_sources()
    db = index.index_path(settings)
    if cmd == "indexer":
        data = index.build(db, claude_dir, hermes_db, text_sources=texts)
        return envelope(cmd, summary=f"{data['sessions']} document(s) indexé(s)", data=data)
    if cmd == "chercher":
        words = [*args.mots, *(args.texte or "").split()]
        if not words:
            return envelope(cmd, status="error", error="écris au moins un mot")
        refreshed = None
        age = index.age_s(db)
        if age is None or age > index.REFRESH_AFTER_S:  # best effort, bounded in time: the old index stays on failure
            try:
                refreshed = index.build(db, claude_dir, hermes_db, budget_s=index.REFRESH_BUDGET_S,
                                     text_sources=texts)["seconds"]
            except Exception as exc:  # noqa: BLE001
                log.warning("souvenirs: refresh failed (%s)", type(exc).__name__)
        if index.age_s(db) is None:
            return envelope(cmd, status="error", error="index introuvable : lance `souvenirs indexer`")
        results = index.search(db, words, sources=args.source) if args.source else index.search(db, words)
        return envelope(cmd, summary=f"{len(results)} résultat(s)",
                        data={"results": results, "refreshed_in_s": refreshed})
    return envelope(cmd, status="error", error=f"unknown command: {cmd}")
