"""`ultron-hub usage`: Claude plan usage, without Claude.

Prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "usage", "summary": str, "data": obj|null, "error": str|null}
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.usage import text
from ultron_hub.usage.collect import SQUAD_ENV, UsageError, squad_quota, usage


def envelope(status: str = "ok", summary: str = "", data: Any = None, error: str | None = None) -> dict[str, Any]:
    return {"status": status, "command": "usage", "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    sub.add_parser("usage", help="Claude plan usage: 5 h session and week (OAuth endpoint, no Claude)")


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    folder = os.environ.get(SQUAD_ENV)
    result = squad_quota(Path(folder), now) if folder else None  # squad session: no credentials here
    try:
        result = result or usage(settings.var_dir / "cache" / "usage.json", now)
    except UsageError as exc:
        return envelope(status="error", summary="usage illisible", error=str(exc),
                        data={"text": f"📊 Usage Claude\n\n❌ {exc}"})
    return envelope(summary=text.summary_line(result), data={**result, "text": text.usage_text(result, now)})
