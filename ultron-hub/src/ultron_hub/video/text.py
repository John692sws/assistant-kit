"""Text helpers shared by the /video steps (analysis, actions): cleaning of what Claude writes, cuts, links.

Everything Claude writes here comes from an untrusted video (transcript, title, on-screen text): `clean_text` strips
control characters, em dashes and sentences that look like an instruction to an AI (the filter of the carnet).
Text given TO Claude as data goes through `neutral`, so it cannot close the tags of the prompt.
"""

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ultron_hub.common.errors import UltronHubError
from ultron_hub.esprit import savoir

TELEGRAM_CHARS = 3500
MAX_ANSWER_CHARS = 2000      # the reply typed by the user after a report (the plugin mirrors it)
CLAIR_LIMIT = 140
TAG_MAX = 5
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ANSWER_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # keeps the new lines of a typed reply
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_TAG_BAD_RE = re.compile(r"[^a-z0-9à-ÿ_-]+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")
# The category of a todo made from a video, from its project; anything else is « perso ».
PROJECT_CATEGORIES = {"ultron": "serveur", "clients": "travail", "metro": "apprentissage", "arabe": "apprentissage"}


class ReplyError(UltronHubError):
    """A refusal explained to the user in one sentence."""


def neutral(text: str) -> str:
    """Text placed between tags of the prompt: it cannot close them (‹ › instead of < >)."""
    return text.replace("<", "‹").replace(">", "›")


def clean_answer(raw: Any) -> str:
    """The user's reply: no control characters (new lines kept), trimmed, bounded, refused when empty."""
    text = _ANSWER_CTRL_RE.sub(" ", str(raw or "")).replace("\r", "")
    text = "\n".join(" ".join(line.split()) for line in text.split("\n")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        raise ReplyError("Réponse vide : écris ce que tu en penses.")
    if len(text) > MAX_ANSWER_CHARS:
        raise ReplyError(f"Réponse trop longue ({len(text)} caractères) : {MAX_ANSWER_CHARS} au maximum.")
    return text


def clean_text(value: Any, limit: int = 600) -> str:
    """Text written by Claude from an untrusted transcript: no control characters, no em dash, no instruction aimed
    at an AI (the same filter as the carnet), one paragraph."""
    text = " ".join(CTRL_RE.sub(" ", str(value or "")).replace("—", ",").replace("–", ",").split())
    text = text.replace(" ,", ",")
    kept, _ = savoir.filter_instructions(text)
    return " ".join(kept.split())[:limit]


def cut(text: str, limit: int = TELEGRAM_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit - 1].rstrip() + "…", True


def cut_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text.rstrip(" .,;:")
    head = text[:limit]
    return (head.rsplit(" ", 1)[0] if " " in head else head).rstrip(" .,;:")


# Short words that cannot end a title (« Ajouter des règles de réalisateur au »): dropped after a cut (todo #443)
DANGLING = frozenset("à a au aux de du des la le les un une et ou ni en pour sur sous avec dans par sans vers chez "
                     "entre son sa ses leur leurs ce cet cette ces mon ma mes ton ta tes notre nos votre vos qui que "
                     "dont the of to and or for with in on by".split())


def cut_title(text: str, limit: int) -> str:
    """A title cut on a whole word, never ending on a dangling short word (« au », « de », « l' »)."""
    if len(text) <= limit:
        return text.rstrip(" .,;:")
    words = text[:limit + 1].split(" ")
    words.pop()  # cut in the middle, or empty when a space follows the limit (the previous word is whole)
    while len(words) > 1 and (words[-1].lower().strip(".,;:") in DANGLING or words[-1].endswith(("'", "’"))
                              or not words[-1].strip(".,;:-")):
        words.pop()
    title = " ".join(words).rstrip(" .,;:-")
    return title or text[:limit].rstrip(" .,;:")


def cut_prose(text: str, limit: int) -> str:
    """A paragraph kept whole when it fits (final period included), else cut on a word with « … »."""
    return text if len(text) <= limit else cut_words(text, limit - 1) + "…"


def clair_line(text: str, limit: int = CLAIR_LIMIT) -> str:
    """« En clair » of a todo: the whole text if it fits, else its first sentence, else cut after a word with « … »."""
    if len(text) <= limit:
        return text
    first = _SENTENCE_END_RE.split(text, 1)[0]
    return first if len(first) <= limit else cut_words(text, limit - 1) + "…"


def clean_tag(raw: Any) -> str:
    """One tag as the carnet accepts it: lower case, words joined by « - », at most 30 characters; empty if none."""
    text = _TAG_BAD_RE.sub("-", str(raw or "").strip().lstrip("#").casefold()).strip("-_")
    return re.sub(r"-{2,}", "-", text)[:30].strip("-_")


def clean_tags(raw: Any, limit: int = TAG_MAX) -> list[str]:
    tags: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        tag = clean_tag(clean_text(item, 60))
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:limit]


def timed_link(url: str, at: int | None) -> str:
    """The video link at that second (YouTube only: TikTok has no time parameter)."""
    if at is None:
        return url
    parts = urlsplit(url)
    if parts.hostname not in _YOUTUBE_HOSTS:
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "t"]
    return urlunsplit(parts._replace(query=urlencode([*query, ("t", f"{at}s")])))


def todo_category(projet: str | None, allowed: tuple[str, ...] | list[str]) -> str:
    """The todo category for a project, or the first allowed one when the table names a category the settings lack."""
    wanted = PROJECT_CATEGORIES.get(projet or "", "perso")
    return wanted if wanted in allowed or not allowed else allowed[0]
