"""Tests for spec-titre-long-todo: title length rules in service.py."""

import json
import pytest

from ultron_hub.todo.service import (
    TITLE_MAX_LEN, NOTES_MAX_LEN,
    TitleTooLongError, EmptyTitleError, NotesTooLongError,
    normalize_title, truncate_title, append_notes, resolve_title,
)
from ultron_hub.todo.store import TodoStore


@pytest.fixture
def store(tmp_path):
    with TodoStore(tmp_path / "todo.db") as s:
        yield s


# ---------------------------------------------------------------------------
# normalize_title
# ---------------------------------------------------------------------------

def test_title_exact_limit_unchanged():
    title = "A" * TITLE_MAX_LEN
    result = resolve_title(title)
    assert result.title == title
    assert result.source == "as_is"
    assert result.notes is None


def test_title_limit_plus_one_rejected():
    title = "A" * (TITLE_MAX_LEN + 1)
    with pytest.raises(TitleTooLongError) as exc_info:
        resolve_title(title)
    assert exc_info.value.length == TITLE_MAX_LEN + 1
    assert exc_info.value.limit == TITLE_MAX_LEN


def test_title_limit_counts_unicode_chars():
    # 60 simple emojis: accepted
    title60 = "🎯" * TITLE_MAX_LEN
    result = resolve_title(title60)
    assert result.source == "as_is"
    # 61 simple emojis: rejected
    title61 = "🎯" * (TITLE_MAX_LEN + 1)
    with pytest.raises(TitleTooLongError):
        resolve_title(title61)


def test_truncate_ends_with_ellipsis_and_fits():
    title = "A " * 50  # 100 chars
    result = truncate_title(title.strip())
    assert len(result) <= TITLE_MAX_LEN
    assert result.endswith("…")


def test_truncate_single_long_word():
    title = "X" * 200
    result = truncate_title(title)
    assert len(result) <= TITLE_MAX_LEN
    assert result.endswith("…")
    assert len(result) == TITLE_MAX_LEN  # 59 chars + '…'


def test_truncate_copies_full_text_to_notes():
    long_title = "word " * 20  # 100 chars
    result = resolve_title(long_title.strip(), mode="truncate")
    assert result.source == "truncated"
    assert result.notes is not None
    assert long_title.strip() in result.notes


def test_newlines_replaced_by_spaces():
    raw = "line1\nline2\ttab"
    result = resolve_title(raw)
    assert "\n" not in result.title
    assert "\t" not in result.title


def test_empty_and_whitespace_title_rejected():
    with pytest.raises(EmptyTitleError):
        resolve_title("")
    with pytest.raises(EmptyTitleError):
        resolve_title("   ")
    with pytest.raises(EmptyTitleError):
        resolve_title("\n\t")


def test_existing_notes_preserved_on_append():
    existing = "Note existante"
    long_title = "word " * 20
    result = resolve_title(long_title.strip(), existing_notes=existing, mode="truncate")
    assert existing in result.notes
    assert long_title.strip() in result.notes


def test_append_notes_no_duplicate():
    text = "some long text"
    first = append_notes(None, text)
    second = append_notes(first, text)
    assert second.count(text) == 1


def test_notes_too_long_rejected():
    big_existing = "X" * NOTES_MAX_LEN
    with pytest.raises(NotesTooLongError):
        append_notes(big_existing, "extra content")


def test_summarize_uses_llm_title():
    long_title = "word " * 20
    fake_llm = lambda text: "Short title"
    result = resolve_title(long_title.strip(), mode="summarize", summarizer=fake_llm)
    assert result.title == "Short title"
    assert result.source == "llm"


def test_summarize_fallback_on_llm_error():
    long_title = "word " * 20
    def bad_llm(text):
        raise RuntimeError("LLM down")
    result = resolve_title(long_title.strip(), mode="summarize", summarizer=bad_llm)
    assert result.source == "truncated"
    assert result.title.endswith("…")
    assert result.notes is not None


def test_summarize_fallback_on_timeout():
    long_title = "word " * 20
    def timeout_llm(text):
        raise TimeoutError("timeout")
    result = resolve_title(long_title.strip(), mode="summarize", summarizer=timeout_llm)
    assert result.source == "truncated"


def test_summarize_fallback_when_llm_title_too_long():
    long_title = "word " * 20
    def too_long_llm(text):
        return "X" * (TITLE_MAX_LEN + 1)
    result = resolve_title(long_title.strip(), mode="summarize", summarizer=too_long_llm)
    assert result.source == "truncated"


def test_summarize_fallback_when_llm_title_empty():
    long_title = "word " * 20
    def empty_llm(text):
        return ""
    result = resolve_title(long_title.strip(), mode="summarize", summarizer=empty_llm)
    assert result.source == "truncated"


# ---------------------------------------------------------------------------
# task.yaml and schema validation
# ---------------------------------------------------------------------------

def test_task_yaml_model_haiku_no_tools():
    import yaml
    from pathlib import Path
    task_yaml = Path(__file__).parents[1] / "tasks" / "todo-titre" / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text())
    assert data["model"] == "haiku"
    assert data.get("tools") == []


def test_schema_maxlength_matches_title_max_len():
    import json
    from pathlib import Path
    schema_file = Path(__file__).parents[1] / "tasks" / "todo-titre" / "output.schema.json"
    schema = json.loads(schema_file.read_text())
    assert schema["properties"]["title"]["maxLength"] == TITLE_MAX_LEN


def test_prompt_marks_text_as_data():
    from pathlib import Path
    prompt = (Path(__file__).parents[1] / "tasks" / "todo-titre" / "prompt.md").read_text()
    assert "{text}" in prompt
    assert "{max_len}" in prompt
    assert "DONNÉE" in prompt or "donnée" in prompt or "données" in prompt.lower()
