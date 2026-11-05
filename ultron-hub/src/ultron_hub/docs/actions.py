"""`ultron-hub docs actions <id>` (todo #156): the user's reply to a documents analysis becomes proposed actions.

Same mechanism as `video actions` (shared code in `ultron_hub.proposals`). The text of the user (stdin, never argv)
is his interpretation. Claude (task `docs-actions`, sonnet, no tools) reads var/docs/<id>/presentation.md and
collected.json (the extracted text of the documents) and that text, and proposes at most MAX_ACTIONS actions: fiche,
todo, recherche (a todo with a research brief), profil. NOTHING is done here.
The source of the todos and fiches is `docs:<id>`. The documents are untrusted data and so is the reply: both sit
between tags with `<` `>` neutralised. Outputs (0600) in var/docs/<id>/: `actions.json` and the fiche drafts.
"""

import json
from typing import Any

from ultron_hub.common.settings import Settings
from ultron_hub.docs import store
from ultron_hub.docs.errors import DocsError
from ultron_hub.proposals import Source, propose
from ultron_hub.video.text import neutral

TASK = "docs-actions"
PRESENTATION_CHARS = 8_000     # the analysis comes first, then the text of the documents
DOCUMENTS_CHARS = 60_000       # same budget as the collector


def _read(path) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def analysis_text(folder) -> str:
    """What Claude reads: the analysis, then each document's extracted text as far as it fits. Neutralised."""
    presentation = _read(folder / store.PRESENTATION).strip()
    if not presentation:
        raise DocsError("Il n'y a pas encore d'analyse pour ce lot : lance d'abord 📝 Analyser.")
    try:
        collected = json.loads(_read(folder / store.COLLECTED) or "{}")
    except ValueError:
        collected = {}
    documents = collected.get("documents") if isinstance(collected, dict) else None
    parts = [f"Analyse :\n{presentation[:PRESENTATION_CHARS]}"]
    left = DOCUMENTS_CHARS
    for doc in documents if isinstance(documents, list) else []:
        if not isinstance(doc, dict) or left <= 0:
            continue
        body = str(doc.get("texte") or "")[:left]
        left -= len(body)
        parts.append(f"Document {doc.get('numero')} ({doc.get('source')}){' [coupé]' if doc.get('coupe') else ''} :\n{body}")
    return neutral("\n\n".join(parts))


def do_actions(settings: Settings, docs_id: str, raw_answer: str, requested_by: str | None) -> dict[str, Any]:
    """`docs actions <id>`. Returns an envelope; writes nothing but drafts and actions.json."""
    def load() -> tuple[Source, str, str]:
        folder = store.folder(settings, docs_id)
        if not folder.is_dir() or folder.is_symlink():
            raise DocsError("Ce lot n'existe plus. Recommence avec 📄 Documents.")
        text = analysis_text(folder)
        title = (_read(folder / store.PRESENTATION).strip().splitlines() or ["Documents"])[0][:100]
        source = Source(folder, "docs", title, f"analyse de documents « {title} » (lot {docs_id})", "de documents",
                        f"docs:{docs_id}")
        return source, text, ""

    return propose(settings, task=TASK, command="docs.actions", kind="docs", source_id=docs_id,
                   raw_answer=raw_answer, requested_by=requested_by, load=load)
