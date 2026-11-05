"""var/docs/<id>/: the batch of documents of one /docs presentation (0700 folder).

    files/       the files received (1 to 3 in total with the items), named <n>-<name>.<ext>
    items.json   [{"kind": "link", "url": ...}, {"kind": "text", "text": ...}] in the order received
    collected.json    what the collector extracted (written by the collector, read by `docs present`)
    presentation.md   the full presentation text
"""

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.docs.errors import DocsError

DOCS_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
FILES, ITEMS, COLLECTED, PRESENTATION = "files", "items.json", "collected.json", "presentation.md"
MAX_ITEMS = 3
MAX_URL_CHARS = 2000   # longest accepted link
MAX_TEXT_CHARS = 200_000   # = docs.collect.MAX_TEXT_CHARS
LINK_RE = re.compile(r"(?i:https?)://[^\s\x00-\x1f\x7f]+")
PURGE_DAYS = 7


def valid_id(docs_id: Any) -> bool:
    return isinstance(docs_id, str) and DOCS_ID_RE.fullmatch(docs_id) is not None


def root(settings: Settings) -> Path:
    return settings.var_dir / "docs"


def folder(settings: Settings, docs_id: Any) -> Path:
    if not valid_id(docs_id):
        raise DocsError("Identifiant de lot invalide.")
    return root(settings) / docs_id


def create(settings: Settings, docs_id: str) -> Path:
    path = folder(settings, docs_id)
    root(settings).mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    (path / FILES).mkdir(mode=0o700)
    return path


def write_items(path: Path, items: list[dict[str, Any]]) -> None:
    """items.json (0600), as the Telegram flow writes it."""
    target = path / ITEMS
    fd = os.open(str(target) + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False)
    os.replace(str(target) + ".tmp", target)


def read_items(path: Path) -> list[dict[str, Any]]:
    target = path / ITEMS
    if not target.exists():
        return []
    if target.is_symlink() or not target.is_file():
        raise DocsError("La liste des éléments est illisible.")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DocsError("La liste des éléments est illisible.") from None
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        raise DocsError("La liste des éléments est illisible.")
    return data


def purge(settings: Settings, days: int = PURGE_DAYS) -> int:
    """Removes the batches older than `days` (best effort)."""
    removed = 0
    limit = time.time() - days * 86400
    base = root(settings)
    for item in base.iterdir() if base.is_dir() else []:
        try:
            if valid_id(item.name) and item.is_dir() and not item.is_symlink() and item.stat().st_mtime < limit:
                shutil.rmtree(item)
                removed += 1
        except OSError:
            continue
    return removed
