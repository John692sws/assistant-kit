"""Context gauge (todo #175): `esprit contexte --claude-hook` and the squad PostToolUse hook."""

import io
import json
from pathlib import Path

import pytest

from ultron_hub import cli
from ultron_hub.escouade import hooks


def transcript(path: Path, tokens: int, model: str = "claude-opus-4-5", sidechain_after: int | None = None) -> Path:
    lines = [{"type": "user", "message": {"role": "user", "content": "salut"}},
             {"type": "assistant", "message": {"model": model, "usage": {
                 "input_tokens": 10, "cache_read_input_tokens": tokens - 30, "cache_creation_input_tokens": 20,
                 "output_tokens": 5}}}]
    if sidechain_after:
        lines.append({"type": "assistant", "isSidechain": True,
                      "message": {"model": model, "usage": {"input_tokens": sidechain_after}}})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def hook_run(capsys, monkeypatch, event: dict | str) -> dict:
    raw = event if isinstance(event, str) else json.dumps(event)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert cli.main(["esprit", "contexte", "--claude-hook"]) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


def text_of(answer: dict) -> str:
    return answer.get("hookSpecificOutput", {}).get("additionalContext", "")


@pytest.fixture
def ev(project, tmp_path, monkeypatch):
    monkeypatch.delenv("ULTRON_ESCOUADE_DIR", raising=False)
    monkeypatch.delenv("ULTRON_HUB_JOB", raising=False)

    def make(tokens, **extra):
        return {"session_id": "s1", "transcript_path": str(transcript(tmp_path / "t.jsonl", tokens)), **extra}
    return make


def test_under_50_silent(capsys, monkeypatch, ev):
    assert hook_run(capsys, monkeypatch, ev(99_000)) == {}


def test_50_once_per_tier(capsys, monkeypatch, ev, project):
    first = text_of(hook_run(capsys, monkeypatch, ev(100_000)))
    assert first == "Contexte à 50 % : prépare ta passation (Reprise, journal)."
    assert hook_run(capsys, monkeypatch, ev(110_000)) == {}
    assert json.loads((project / "var" / "contexte" / "s1.json").read_text()) == {"palier": 50}


def test_60_every_call(capsys, monkeypatch, ev):
    for _ in range(3):
        assert text_of(hook_run(capsys, monkeypatch, ev(125_000))) == \
            "CONTEXTE 62 % : PASSATION OBLIGATOIRE MAINTENANT, rien d'autre."


def test_tier_rearms_after_compaction(capsys, monkeypatch, ev):
    assert text_of(hook_run(capsys, monkeypatch, ev(100_000)))
    assert hook_run(capsys, monkeypatch, ev(20_000)) == {}
    assert text_of(hook_run(capsys, monkeypatch, ev(100_000)))


def test_silences(capsys, monkeypatch, ev, tmp_path):
    no_usage = tmp_path / "n.jsonl"
    no_usage.write_text(json.dumps({"type": "assistant", "message": {"model": "m"}}) + "\nnot json\n")
    assert hook_run(capsys, monkeypatch, {"session_id": "s1", "transcript_path": str(no_usage)}) == {}
    assert hook_run(capsys, monkeypatch, {"session_id": "s1", "transcript_path": str(tmp_path / "absent")}) == {}
    assert hook_run(capsys, monkeypatch, {"session_id": "s1"}) == {}
    assert hook_run(capsys, monkeypatch, "{cassé") == {}
    assert hook_run(capsys, monkeypatch, "") == {}
    assert hook_run(capsys, monkeypatch, ev(125_000, session_id="../x")) == {}


def test_subagent_and_sidechain(capsys, monkeypatch, ev, tmp_path):
    assert hook_run(capsys, monkeypatch, ev(125_000, agent_id="a1")) == {}
    path = transcript(tmp_path / "side.jsonl", 20_000, sidechain_after=190_000)  # the sidechain line is ignored
    assert hook_run(capsys, monkeypatch, {"session_id": "s2", "transcript_path": str(path)}) == {}


def test_extended_window_and_config(capsys, monkeypatch, ev, project):
    assert hook_run(capsys, monkeypatch, ev(300_000)) == {}  # beyond 200k: 1M window, 30 %
    (project / "config" / "contexte.yaml").write_text("fenetres:\n  opus: 100000\nseuil_info: 40\n")
    assert "Contexte à 45 %" in text_of(hook_run(capsys, monkeypatch, ev(45_000, session_id="s3")))


def test_job_and_squad_sessions_silent(capsys, monkeypatch, ev):
    monkeypatch.setenv("ULTRON_ESCOUADE_DIR", "/x")
    assert hook_run(capsys, monkeypatch, ev(125_000)) == {}


# -- squad hook --------------------------------------------------------------------------------------------

def squad_event(tmp_path, tokens, **extra):
    return {"tool_name": "Bash", "transcript_path": str(transcript(tmp_path / "e.jsonl", tokens)), **extra}


@pytest.fixture
def squad(tmp_path):
    d = tmp_path / "esc-1"
    d.mkdir()
    (d / "etat.json").write_text(json.dumps({"id": "esc-1"}))
    return d


def test_squad_50_telegram_once(squad, tmp_path, no_real_hook_telegram):
    first = hooks.post_decision(squad, squad_event(tmp_path, 100_000))
    assert "Contexte à 50 %" in first["hookSpecificOutput"]["additionalContext"]
    assert no_real_hook_telegram == ["Escouade esc-1 : contexte à 50 %, passation à préparer."]
    assert hooks.post_decision(squad, squad_event(tmp_path, 105_000)) == {}
    assert len(no_real_hook_telegram) == 1
    assert not (squad / "drapeau-passation").exists()


def test_squad_60_flag_every_call(squad, tmp_path, no_real_hook_telegram):
    for _ in range(2):
        out = hooks.post_decision(squad, squad_event(tmp_path, 125_000))
        assert out["hookSpecificOutput"]["additionalContext"] == \
            "CONTEXTE 62 % : PASSATION OBLIGATOIRE MAINTENANT, rien d'autre."
    assert (squad / "drapeau-passation").read_text() == "contexte\n"
    assert len(no_real_hook_telegram) == 1


def test_squad_subagent_and_errors(squad, tmp_path):
    assert hooks.post_decision(squad, squad_event(tmp_path, 125_000, agent_id="a")) == {}
    assert hooks.post_decision(squad, {"transcript_path": str(tmp_path / "absent")}) == {}
    assert not (squad / "drapeau-passation").exists()


def test_squad_uses_contexte_json(squad, tmp_path):
    (squad / "contexte.json").write_text(json.dumps({"fenetres": {"opus": 100000}, "seuil_info": 30}))
    assert "Contexte à 45 %" in hooks.post_decision(squad, squad_event(tmp_path, 45_000))["hookSpecificOutput"]["additionalContext"]

