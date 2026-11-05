"""LLM-based title shortener via tasks/todo-titre/.

Injected into resolve_title() as the `summarizer` callable.
Never call this directly in tests — inject a fake instead.
"""

from pathlib import Path

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import load_settings
from ultron_hub.core.job import JobRequest
from ultron_hub.core.registry import Registry
from ultron_hub.core.runner import run_job
from ultron_hub.todo.service import NOTES_MAX_LEN, TITLE_MAX_LEN, clean_clair
from ultron_hub.todo.store import CLAIR_MAX

log = get_logger("todo.summarizer")


def llm_title(text: str) -> str:
    """Run tasks/todo-titre via the executor; return structured_output['title'].

    Raises UltronHubError on failure so resolve_title() can fall back to truncate.
    """
    settings = load_settings()
    registry = Registry(settings.tasks_dir, settings.default_model, settings.default_timeout_s)
    spec = registry.load("todo-titre")
    # Truncate text to at most NOTES_MAX_LEN chars before passing to LLM
    safe_text = text[:NOTES_MAX_LEN]
    prompt = spec.render_prompt({"text": safe_text, "max_len": str(TITLE_MAX_LEN)})
    request = JobRequest(action="todo-titre", params={"text": safe_text, "max_len": TITLE_MAX_LEN})
    result = run_job(request, settings)
    if not result.ok:
        raise UltronHubError(f"todo-titre failed: {result.error}")
    data = result.data
    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        raise UltronHubError("todo-titre: missing or invalid structured_output.title")
    title = data["title"]
    if not title or len(title) > TITLE_MAX_LEN:
        raise UltronHubError(f"todo-titre: title out of bounds ({len(title)!r})")
    return title


def llm_clair(title: str, notes: str = "") -> str:
    """Run tasks/todo-clair (haiku) and return the one-sentence "En clair" text.

    Raises UltronHubError on failure; callers treat it as best effort.
    """
    settings = load_settings()
    params = {"title": title, "notes": notes[:NOTES_MAX_LEN] or "—", "max_len": CLAIR_MAX}
    result = run_job(JobRequest(action="todo-clair", params=params), settings)
    if not result.ok:
        raise UltronHubError(f"todo-clair failed: {result.error}")
    data = result.data
    if not isinstance(data, dict) or not isinstance(data.get("clair"), str):
        raise UltronHubError("todo-clair: missing or invalid structured_output.clair")
    clair = clean_clair(data["clair"])
    if not clair:
        raise UltronHubError("todo-clair: empty answer")
    return clair
