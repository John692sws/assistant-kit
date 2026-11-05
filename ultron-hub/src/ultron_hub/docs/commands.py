"""`ultron-hub docs new | present | actions` (todo #48, #156): short written presentation of 1 to 3 documents.

Each command prints one JSON object on stdout:
    {"status": "ok"|"error", "command": "docs.<cmd>", "summary": str, "data": obj|null, "error": str|null}
- `new`           creates var/docs/<id>/ (files/ and items.json are filled by the caller); data.id + data.path.
                  `--link URL` or `--text-stdin` (todo #248, dashboard) puts that one element in items.json.
                  Batches older than 7 days are removed at the same time.
- `present <id>`  collector `docs` (files, links, pasted texts) then task docs-presenter (sonnet, no tools);
                  data.text = the presentation (also in presentation.md). The journal gets the count only.
- `actions <id>`  (stdin) the user's reply to the presentation becomes proposed actions (docs/actions.py, shared
                  code in proposals.py): fiche, todo, recherche, profil. None is done.
"""

import argparse
import os
import sys
from typing import Any

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.core.job import JobRequest, new_job_id
from ultron_hub.core.runner import run_job
from ultron_hub.docs import store
from ultron_hub.docs.errors import DocsError
from ultron_hub.esprit import store as esprit

log = get_logger("docs")
TASK = "docs-presenter"


def envelope(status: str = "ok", summary: str = "", data: Any = None, error: str | None = None,
             command: str = "docs") -> dict[str, Any]:
    return {"status": status, "command": command, "summary": summary, "data": data, "error": error}


def add_parser(sub: argparse._SubParsersAction, parser_class: type) -> None:
    docs = sub.add_parser("docs", help="present 1 to 3 documents (files, links, texts) in a few lines")
    cmds = docs.add_subparsers(dest="docs_command", required=True, parser_class=parser_class)
    new = cmds.add_parser("new", help="create a batch folder (data.id, data.path), optionally with one link or one text")
    what = new.add_mutually_exclusive_group()
    what.add_argument("--link", default=None, help="an http(s) link to put in the batch (todo #248, the dashboard)")
    what.add_argument("--text-stdin", action="store_true", help="the text to put in the batch is read on stdin")
    present = cmds.add_parser("present", help="write the presentation of a batch")
    present.add_argument("id")
    present.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")
    actions = cmds.add_parser("actions", help="the user's reply (stdin) to a presentation becomes proposed actions")
    actions.add_argument("id")
    actions.add_argument("--requested-by", default=None, help="e.g. telegram:<chat_id>")


def render(data: dict[str, Any], problems: list[str]) -> str:
    """The presentation as plain text. The model's fields are shown as text only."""
    lines = [str(data.get("titre", "")).strip(), ""]
    lines += [f"- {str(p).strip()}" for p in data.get("puces", [])]
    comparison = str(data.get("comparaison", "")).strip()
    if comparison:
        lines += ["", "Points communs et différences :", comparison]
    sources = [str(s).strip() for s in data.get("sources", []) if str(s).strip()]
    if sources:
        lines += ["", "Sources :", *[f"- {s}" for s in sources]]
    if problems:
        lines += ["", "À savoir :", *[f"- {p}" for p in problems]]
    return "\n".join(lines).strip()


def _item(link: str | None, text: str | None) -> dict[str, str] | None:
    """The one element given to `docs new` (same limits as the Telegram flow), or None."""
    if link is not None:
        if not store.LINK_RE.fullmatch(link) or len(link) > store.MAX_URL_CHARS:
            raise DocsError("Lien refusé : http(s) seulement, 2000 caractères au plus, sans espace.")
        return {"kind": "link", "url": link}
    if text is not None:
        body = text.strip()
        if not body:
            raise DocsError("Le texte est vide.")
        return {"kind": "text", "text": body[:store.MAX_TEXT_CHARS]}
    return None


def do_new(settings: Settings, link: str | None = None, text: str | None = None) -> dict[str, Any]:
    command = "docs.new"
    try:
        item = _item(link, text)
    except DocsError as exc:
        return envelope(status="error", summary="élément refusé", error=str(exc), command=command)
    store.purge(settings)
    docs_id = new_job_id()
    path = store.create(settings, docs_id)
    if item is not None:
        store.write_items(path, [item])
    return envelope(summary="lot créé", command=command, data={"id": docs_id, "path": str(path)})


def do_present(settings: Settings, docs_id: str, requested_by: str | None) -> dict[str, Any]:
    command = "docs.present"
    try:
        folder = store.folder(settings, docs_id)
        if not folder.is_dir() or folder.is_symlink():
            raise DocsError("Ce lot n'existe plus. Recommence avec 📄 Documents.")
    except DocsError as exc:
        return envelope(status="error", summary="lot invalide", error=str(exc), command=command)
    (folder / store.COLLECTED).unlink(missing_ok=True)
    request = JobRequest.from_dict({"job_id": new_job_id(), "action": TASK, "params": {"docs_id": docs_id},
                                    "requested_by": requested_by})
    result = run_job(request, settings)
    if not result.ok or not isinstance(result.data, dict):
        return envelope(status="error", summary="présentation impossible", command=command,
                        error=result.error or result.summary or "La présentation a échoué.")
    collected = _read_collected(folder)
    text = render(result.data, [str(p) for p in collected.get("problemes", [])])
    fd = os.open(folder / store.PRESENTATION, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    count = len(collected.get("documents", []))
    esprit.record(settings, "ultron-hub", "docs", f"Documents présentés : {count} élément{'s' if count > 1 else ''}")
    log.info("docs=%s presented: %d element(s) requested_by=%s", docs_id, count, requested_by)
    return envelope(summary=f"{count} élément(s) présenté(s)", command=command,
                    data={"id": docs_id, "text": text, "path": str(folder / store.PRESENTATION),
                          "elements": count, "cost_usd": result.cost_usd})


def _read_collected(folder) -> dict[str, Any]:
    import json
    try:
        data = json.loads((folder / store.COLLECTED).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def handle(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    try:
        if args.docs_command == "new":
            # the text of --text-stdin comes on stdin, never argv
            return do_new(settings, args.link, sys.stdin.read(store.MAX_TEXT_CHARS + 1) if args.text_stdin else None)
        if args.docs_command == "actions":
            from ultron_hub.docs import actions  # imported here: it imports the shared proposals module
            from ultron_hub.video.text import MAX_ANSWER_CHARS
            # stdin, never argv: the reply is the user's free text.
            return actions.do_actions(settings, args.id, sys.stdin.read(MAX_ANSWER_CHARS * 4 + 1), args.requested_by)
        return do_present(settings, args.id, args.requested_by)
    except UltronHubError as exc:
        return envelope(status="error", summary="docs failed", error=str(exc), command=f"docs.{args.docs_command}")
