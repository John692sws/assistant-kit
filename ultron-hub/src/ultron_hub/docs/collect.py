"""Collector `docs`: the text of the 1 to 3 elements of a batch (var/docs/<id>), shared in one budget of characters.

Everything extracted is DATA written by strangers: the prompt says so. A failure or a cut is reported, never hidden.
"""

import json
import tempfile
from pathlib import Path
from typing import Any

from ultron_hub.common.settings import load_settings
from ultron_hub.docs import extract, fetch, store
from ultron_hub.docs.errors import DocsError
from ultron_hub.montage import audio, transcribe
from ultron_hub.montage.tools import MontageError

BUDGET_CHARS = 60_000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 200_000   # a pasted text longer than this is cut before sharing the budget
MAX_AUDIO_S = 20 * 60      # a longer recording is refused (its transcription would exceed 10 minutes)


def shares(sizes: list[int], budget: int) -> list[int]:
    """Characters each document may keep: small ones keep everything, the rest is split equally among the others."""
    result = [0] * len(sizes)
    pending = sorted(range(len(sizes)), key=lambda i: sizes[i])
    left = budget
    while pending:
        each = left // len(pending)
        i = pending.pop(0)
        result[i] = min(sizes[i], each)
        left -= result[i]
    return result


def duration_label(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds // 60} min {seconds % 60:02d} s" if seconds >= 60 else f"{seconds} s"


def is_audio(path: Path) -> bool:
    return extract.FILE_KINDS.get(path.suffix.lower()) == "audio"


def _audio_entry(path: Path, name: str) -> dict[str, Any]:
    """An audio file -> WAV (ffmpeg) -> every word by the Whisper of the hermes-video skill. Failures are explained."""
    try:
        info = audio.probe(path)
        if not any(s.get("codec_type") == "audio" for s in info["streams"]):
            raise DocsError("Ce fichier ne contient pas de son.")
        duration = info["duration"]
        if duration > MAX_AUDIO_S:
            raise DocsError(f"Audio trop long ({duration_label(duration)}) : 20 minutes au maximum.")
        with tempfile.TemporaryDirectory(prefix="docs-audio-") as tmp:
            wav = Path(tmp) / "audio.wav"
            audio.to_wav(path, wav)
            words = transcribe.transcribe(load_settings(), wav, duration, full=True)["words"]
    except MontageError as exc:
        raise DocsError(str(exc)) from None
    return {"source": f"audio {name} ({duration_label(duration)})", "text": " ".join(w["w"] for w in words)}


def _file_entry(path: Path) -> dict[str, Any]:
    name = path.name.split("-", 1)[-1]
    kind = extract.FILE_KINDS.get(path.suffix.lower())
    if kind is None:
        raise DocsError("Format non pris en charge (PDF, TXT, MD, HTML ou audio).")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DocsError("Fichier trop gros (20 Mo au maximum).")
    if kind == "audio":
        return _audio_entry(path, name)
    return {"source": f"fichier {name}", "text": extract.extract(kind, path.read_bytes(), BUDGET_CHARS)}


def _link_entry(url: Any) -> dict[str, Any]:
    content_type, body = fetch.fetch(url)
    kind = extract.TYPE_KINDS.get(content_type)
    if kind is None:
        raise DocsError("Type de contenu non pris en charge pour ce lien.")
    return {"source": f"lien {url}", "text": extract.extract(kind, body, BUDGET_CHARS)}


def _text_entry(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        raise DocsError("Texte invalide.")
    cleaned = extract.clean(text[:MAX_TEXT_CHARS])
    if not cleaned:
        raise DocsError("Texte vide.")
    return {"source": "texte collé", "text": cleaned}


def gather(folder: Path) -> list[tuple[str, Any]]:
    """The elements in order: files by name, then the items of items.json."""
    found: list[tuple[str, Any]] = []
    files = folder / store.FILES
    for path in sorted(files.iterdir()) if files.is_dir() and not files.is_symlink() else []:
        if path.is_file() and not path.is_symlink():
            found.append(("file", path))
    for item in store.read_items(folder):
        found.append((str(item.get("kind")), item.get("url") if item.get("kind") == "link" else item.get("text")))
    return found


def collect_docs(params: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    folder = store.folder(settings, params.get("docs_id"))
    elements = gather(folder)
    if not elements:
        raise DocsError("Ce lot est vide : envoie d'abord un fichier, un lien ou un texte.")
    if len(elements) > store.MAX_ITEMS:
        raise DocsError(f"Trois éléments au maximum par lot ({len(elements)} reçus).")
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, (kind, value) in enumerate(elements, 1):
        label = f"{number}. " + (f"{'audio' if is_audio(value) else 'fichier'} {value.name.split('-', 1)[-1]}" if kind == "file"
                                 else f"lien {value}" if kind == "link" else "texte collé")
        try:
            if kind == "file":
                entry = _file_entry(value)
            elif kind == "link":
                entry = _link_entry(value)
            elif kind == "text":
                entry = _text_entry(value)
            else:
                raise DocsError("Élément inconnu.")
        except (DocsError, OSError) as exc:
            problems.append(f"{label} : non lu ({exc if isinstance(exc, DocsError) else 'fichier illisible'})")
            continue
        entry["number"] = number
        entries.append(entry)
    if not entries:
        raise DocsError("Je n'ai pu lire aucun élément. " + " ".join(problems))
    kept = shares([len(e["text"]) for e in entries], BUDGET_CHARS)
    documents = []
    for entry, keep in zip(entries, kept):
        text, cut = entry["text"], len(entry["text"]) > keep
        if cut:
            problems.append(f"{entry['number']}. {entry['source']} : coupé ({keep} caractères gardés sur "
                            f"{len(entry['text'])}), seul le début est lu")
        documents.append({"numero": entry["number"], "source": entry["source"], "texte": text[:keep],
                          "coupe": cut})
    result = {"documents": documents, "problemes": problems}
    try:  # read back by `docs present` to tell the user about cuts and failures
        (folder / store.COLLECTED).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return result
