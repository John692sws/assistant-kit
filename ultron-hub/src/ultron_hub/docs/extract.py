"""Text of one document: PDF (pypdf), TXT/MD as is, HTML (stdlib parser). Never executes anything from the document."""

import io
from html.parser import HTMLParser

from ultron_hub.docs.errors import DocsError

MAX_PDF_PAGES = 300
SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "ul", "ol",
              "table", "blockquote"}
FILE_KINDS = {".pdf": "pdf", ".txt": "text", ".md": "text", ".markdown": "text", ".html": "html", ".htm": "html"}
AUDIO_KINDS = {".mp3": "audio", ".m4a": "audio", ".ogg": "audio", ".oga": "audio", ".opus": "audio", ".wav": "audio"}
FILE_KINDS.update(AUDIO_KINDS)   # an audio file is transcribed by the collector, not read by `extract`
TYPE_KINDS = {"application/pdf": "pdf", "text/plain": "text", "text/markdown": "text", "text/x-markdown": "text",
              "text/html": "html", "application/xhtml+xml": "html"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in SKIPPED_TAGS and tag != "head":
            self.skip += 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIPPED_TAGS and tag != "head" and self.skip:
            self.skip -= 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def html_text(raw: bytes) -> str:
    parser = _Text()
    parser.feed(decode(raw))
    parser.close()
    body = clean("".join(parser.parts))
    title = clean(parser.title)
    return f"{title}\n\n{body}".strip() if title and title not in body[:200] else body


def pdf_text(raw: bytes, limit: int) -> str:
    """Stops reading pages once `limit` characters are collected."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DocsError("La lecture des PDF n'est pas installée sur le serveur.") from None
    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise DocsError("Ce PDF est protégé par un mot de passe.")
        chunks, size = [], 0
        for page in list(reader.pages)[:MAX_PDF_PAGES]:
            chunk = page.extract_text() or ""
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                break
    except DocsError:
        raise
    except Exception:
        raise DocsError("Ce PDF est illisible.") from None
    return clean("\n".join(chunks))


def extract(kind: str, raw: bytes, limit: int) -> str:
    if kind == "pdf":
        text = pdf_text(raw, limit)
        if not text:
            raise DocsError("Ce PDF ne contient pas de texte lisible (document scanné ?).")
        return text
    text = html_text(raw) if kind == "html" else clean(decode(raw))
    if not text:
        raise DocsError("Aucun texte trouvé.")
    return text
