"""Ultron's shared mind (todo #47): folder, journal, proposed facts, summaries, events, Hermes side."""

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from hermes_plugin import close_key
from ultron_hub import cli
from ultron_hub.common.settings import load_settings
from ultron_hub.core.job import JobResult
from ultron_hub.core.registry import Registry
from ultron_hub.esprit import store, summary
from ultron_hub.executors.claude_cli import JOB_ENV, clean_env
from ultron_hub.todo import closing
from tests.cli_contract import check_argv

REPO = Path(__file__).resolve().parents[1]


def run(capsys, *argv):
    code = cli.main(list(argv))
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1, lines
    return code, json.loads(lines[0])


@pytest.fixture
def mind(project, esprit_dir, capsys):
    """An initialised shared mind in the throwaway project."""
    (project / "deploy" / "esprit").mkdir(parents=True)
    (project / "deploy" / "esprit" / "charte.md").write_text("# Charte\nTu es Ultron.\n")
    profil = project / "profil-initial.md"
    profil.write_text("# Profil\n- Parle français.\n")
    code, out = run(capsys, "esprit", "init", "--profil-from", str(profil))
    assert code == 0, out
    return load_settings()


# -- folder ------------------------------------------------------------------------------

def test_init_creates_private_folder_and_never_overwrites(mind, esprit_dir, capsys, project):
    assert stat.S_IMODE(esprit_dir.stat().st_mode) == 0o700
    assert (esprit_dir / "profil.md").read_text() == "# Profil\n- Parle français.\n"
    assert "Tu es Ultron" in (esprit_dir / "charte.md").read_text()
    (esprit_dir / "profil.md").write_text("modifié par l'utilisateur\n")
    code, out = run(capsys, "esprit", "init", "--profil-from", str(project / "profil-initial.md"))
    assert code == 0 and set(out["data"]["kept"]) == {"profil.md", "charte.md"}
    assert (esprit_dir / "profil.md").read_text() == "modifié par l'utilisateur\n"


def test_settings_default_and_override(monkeypatch, tmp_path, project):
    monkeypatch.delenv("ULTRON_ESPRIT_DIR")
    assert load_settings().esprit_dir == Path.home() / "ultron" / "esprit"
    (project / "config" / "settings.yaml").write_text(f"paths:\n  esprit_dir: {tmp_path / 'x'}\n")
    assert load_settings().esprit_dir == tmp_path / "x"


def test_real_charter_is_bounded_and_says_ultron():
    text = (REPO / "deploy" / "esprit" / "charte.md").read_text()
    assert "Ultron" in text and "—" not in text and len(text) < store.CONTEXT_MAX


# -- journal -----------------------------------------------------------------------------

def test_journal_add_tail_search(mind, capsys):
    code, out = run(capsys, "journal", "add", "Spec du chantier 3\nvalidée", "--source", "claude-code",
                    "--type", "decision", "--todo", "47")
    assert code == 0 and out["data"]["texte"] == "Spec du chantier 3 validée" and out["data"]["todo"] == 47
    run(capsys, "journal", "add", "x" * 1000, "--source", "hermes")
    code, out = run(capsys, "journal", "tail", "--limit", "5")
    entries = out["data"]["entries"]
    assert [e["source"] for e in entries] == ["claude-code", "hermes"]
    assert len(entries[1]["texte"]) == store.TEXT_MAX and entries[1]["texte"].endswith("…")
    code, out = run(capsys, "journal", "search", "CHANTIER")
    assert len(out["data"]["entries"]) == 1


@pytest.mark.parametrize("argv", [["--source", "Hermes!"], ["--source", "hermes", "--type", "a b"],
                                  ["--source", "hermes", "--todo", "0"]])
def test_journal_add_refuses_bad_fields(mind, capsys, argv):
    code, out = run(capsys, "journal", "add", "texte", *argv)
    assert code == 1 and out["status"] == "error"


def test_journal_rotates_each_month(mind, esprit_dir, monkeypatch):
    monkeypatch.setattr(store, "now_iso", lambda settings: "2026-10-31T23:59:00+01:00")
    store.add(mind, "hermes", "note", "octobre")
    monkeypatch.setattr(store, "now_iso", lambda settings: "2026-11-01T00:01:00+01:00")
    store.add(mind, "hermes", "note", "novembre")
    assert "octobre" in (esprit_dir / "archives" / "journal-2026-10.jsonl").read_text()
    assert "octobre" not in (esprit_dir / "journal.jsonl").read_text()
    assert [e["texte"] for e in store.tail(mind, 5)] == ["octobre", "novembre"]  # archives fill a short tail


def test_record_is_best_effort(project, esprit_dir, monkeypatch):
    settings = load_settings()
    store.record(settings, "ultron-hub", "todo", "rien")  # folder absent: silently nothing
    assert not esprit_dir.exists()
    esprit_dir.mkdir()
    monkeypatch.setattr(store, "add", lambda *a, **k: (_ for _ in ()).throw(OSError("disque plein")))
    store.record(settings, "ultron-hub", "todo", "rien")  # never raises


def test_todo_events_reach_the_journal(mind, capsys, monkeypatch):
    code, out = run(capsys, "todo", "add", "--title", "Tester le journal", "--source", "agent:hermes")
    todo_id = out["data"]["id"]
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("done", todo_id))
    assert run(capsys, "todo", "done", str(todo_id))[0] == 0
    texts = [(e["type"], e["todo"], e["texte"]) for e in store.tail(mind, 10)]
    assert texts == [("todo", todo_id, "Todo créée : Tester le journal (par agent:hermes)"),
                     ("todo", todo_id, "Todo terminée : Tester le journal (telegram-button)")]


# -- profile and proposals --------------------------------------------------------------

def test_propose_then_accept_needs_the_button(mind, capsys, monkeypatch, esprit_dir):
    sent = []
    monkeypatch.setattr("ultron_hub.todo.service.send_message", lambda text, target, binary: sent.append(text))
    code, out = run(capsys, "profil", "propose", "Préfère le thé", "--source", "agent:hermes")
    assert code == 0 and out["data"]["id"] == 1 and "/profil" in sent[0]
    assert run(capsys, "profil", "propose", "Préfère le thé", "--source", "agent:hermes")[1]["data"]["doublon"]
    assert len(sent) == 1

    code, out = run(capsys, "profil", "accept", "1")
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "profil accept 1" in out["error"]
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("done", 1))  # a todo token is not a profile token
    assert run(capsys, "profil", "accept", "1")[0] == 1
    token = close_key.token("profil", 1)
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    code, out = run(capsys, "profil", "accept", "1", "--text", "Préfère le thé à la menthe")
    assert code == 0 and out["data"]["by"] == "telegram-button"
    assert "- Préfère le thé à la menthe (" in (esprit_dir / "profil.md").read_text()
    assert run(capsys, "profil", "accept", "1")[0] == 1  # already decided (and the token is spent)
    assert store.proposals(mind) == []
    assert "Fait ajouté au profil (proposition 1)" in store.tail(mind, 5)[-1]["texte"]


def test_invisible_characters_are_stripped_and_counted(mind):
    row = store.propose(mind, "Aime\u202e le\u200b thé\ufeff", "agent:hermes")
    assert row["texte"] == "Aime le thé" and row["invisible_retire"] == 3
    plain = store.propose(mind, "Préfère le café ☕ 👍", "agent:hermes")
    assert plain["texte"] == "Préfère le café ☕ 👍" and "invisible_retire" not in plain
    with pytest.raises(Exception, match="fait vide"):
        store.propose(mind, "\u200b\u202e\u2066", "agent:hermes")
    done = store.decide(mind, plain["id"], True, {"by": "cli"}, text="Corrigé\u202e ok\u200d")
    assert done["texte_final"] == "Corrigé ok"


def test_reject_leaves_the_profile_alone(mind, capsys, esprit_dir):
    before = (esprit_dir / "profil.md").read_text()
    run(capsys, "profil", "propose", "Fait faux", "--source", "agent:hermes", "--no-notify")
    code, out = run(capsys, "profil", "reject", "1")
    assert code == 0 and out["data"]["statut"] == "refusee"
    assert (esprit_dir / "profil.md").read_text() == before
    assert run(capsys, "profil", "show")[1]["data"]["pending"] == []


def test_profile_signature_matches_the_plugin():
    key = b"k" * 64
    assert close_key.token("profil", 4, key=key, now=1000) == closing.sign(key, "profil", 4, 1000)


# -- context for Claude Code and Hermes -----------------------------------

def test_claude_hook_context(mind, capsys, monkeypatch):
    store.add(mind, "hermes", "resume", "Todo #45 créée sur Telegram", 45)
    assert cli.main(["esprit", "context", "--claude-hook"]) == 0
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Tu es Ultron" in context and "Parle français" in context and "Todo #45 créée" in context
    monkeypatch.setenv(JOB_ENV, "1")  # an ultron-hub job: no profile, no journal
    cli.main(["esprit", "context", "--claude-hook"])
    assert json.loads(capsys.readouterr().out) == {}


def test_claude_hook_journal_skips_dashboard_plumbing(mind, capsys):
    store.add(mind, "dashboard", "action", "📜 fil marqué lu depuis le dashboard (by=x)", None)
    store.add(mind, "dashboard", "action", "✍️ 12 caractères envoyés à la session s depuis le dashboard (by=x)", None)
    store.add(mind, "dashboard", "action", "🔀 fusion de la todo #362 depuis le dashboard (by=x)", None)
    cli.main(["esprit", "context", "--claude-hook"])
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "fusion de la todo #362" in context
    assert "fil marqué lu" not in context and "caractères envoyés" not in context


def test_claude_hook_journal_is_capped_at_8_after_filtering(mind, capsys):
    for i in range(12):
        store.add(mind, "hermes", "resume", f"entree-{i:02d}", None)
    for _ in range(5):
        store.add(mind, "dashboard", "action", "📜 notification lue depuis le dashboard", None)
    cli.main(["esprit", "context", "--claude-hook"])
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert [i for i in range(12) if f"entree-{i:02d}" in context] == [4, 5, 6, 7, 8, 9, 10, 11]
    assert store.journal_context(mind, 20).count("notification lue") == 5  # other readers are unchanged


def test_claude_hook_context_never_fails(project, capsys):
    assert cli.main(["esprit", "context", "--claude-hook"]) == 0  # folder absent
    assert json.loads(capsys.readouterr().out) == {}


def test_jobs_are_marked_for_the_hooks():
    assert clean_env()[JOB_ENV] == "1"


# -- summaries ---------------------------------------------------------------------------

def claude_session(tmp_path, rows) -> Path:
    path = tmp_path / ".claude" / "projects" / "-home-ultron" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return path


SESSION = [
    {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
    {"type": "user", "message": {"content": "lance le chantier 3"}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Je crée la todo."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ultron-hub todo add --title X"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "secret output"}]}},
]


def test_claude_transcript_keeps_text_and_tool_names(tmp_path):
    text = summary.claude_transcript(claude_session(tmp_path, SESSION))
    assert text.splitlines() == ["Utilisateur : lance le chantier 3", "Assistant : Je crée la todo.",
                                 "[outil Bash: ultron-hub todo add --title X]"]


def fake_run(data, calls=None):
    def run_job(request, settings):
        if calls is not None:
            calls.append(request)
        return JobResult(job_id="j", action=request.action, status="ok", data=data)
    return run_job


def test_summarize_records_one_line(mind):
    calls = []
    result = summary.summarize(mind, "Utilisateur : a\nAssistant : b", "hermes",
                               run=fake_run({"action": True, "resume": "Todo #45 créée", "todo": 45}, calls))
    assert result["recorded"] and result["entry"]["todo"] == 45 and result["entry"]["type"] == "resume"
    assert calls[0].action == "journal-resume" and "Assistant : b" in calls[0].params["transcript"]
    nothing = summary.summarize(mind, "Utilisateur : salut\nAssistant : salut", "hermes",
                                run=fake_run({"action": False, "resume": "", "todo": 0}))
    assert nothing == {"recorded": False, "reason": "rien de fait"}
    assert summary.summarize(mind, "", "hermes", run=None)["reason"] == "conversation vide"


def test_claude_hook_input_checks_path_and_jobs(mind, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    session = claude_session(tmp_path, SESSION)
    text, reason = summary.claude_hook_input(mind, json.dumps({"transcript_path": str(session), "cwd": "/home"}))
    assert "lance le chantier 3" in text and reason == ""
    outside = tmp_path / "ailleurs.jsonl"
    outside.write_text("{}")
    with pytest.raises(Exception, match="hors de"):
        summary.claude_hook_input(mind, json.dumps({"transcript_path": str(outside)}))
    job_cwd = mind.runs_dir / "20261012-x"
    assert summary.claude_hook_input(mind, json.dumps({"transcript_path": str(session), "cwd": str(job_cwd)})) \
        == (None, "session d'un job ultron-hub")
    monkeypatch.setenv(JOB_ENV, "1")
    assert summary.claude_hook_input(mind, json.dumps({"transcript_path": str(session)}))[0] is None


def test_detach_then_pending_run(mind, monkeypatch, capsys):
    spawned = []
    result = summary.detach(mind, "Utilisateur : a\nAssistant : b", "claude-code", spawn=spawned.append)
    pending = Path(result["pending"])
    assert result["detached"] and stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert spawned[0][1:4] == ["journal", "summarize", "--pending"]
    monkeypatch.setattr(summary, "run_job", fake_run({"action": True, "resume": "Fait", "todo": 0}))
    code, out = run(capsys, "journal", "summarize", "--pending", str(pending), "--source", "claude-code")
    assert code == 0 and out["data"]["recorded"] and not pending.exists()
    with pytest.raises(Exception, match="hors de var/journal-pending"):
        summary.read_pending(mind, "/etc/passwd")


def test_summary_task_is_valid():
    spec = Registry(REPO / "tasks").load("journal-resume")
    assert spec.model == "haiku" and spec.tools == ()
    assert "Utilisateur : x" in spec.render_prompt({"source": "hermes", "transcript": "Utilisateur : x {y}"})
