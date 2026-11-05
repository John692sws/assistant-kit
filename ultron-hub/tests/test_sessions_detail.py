"""Per-session detail of `session liste` (todo #406): context %, running sub-agents, last sentence; fake journals."""

import json

from ultron_hub.escouade import hooks
from ultron_hub.sessions import etat, journal

NAME = "ultron-demo-25-10-10h00"


def fake_read(entries, agents=()):
    return lambda path, n: {"entrees": entries, "sous_agents": list(agents), "maj": "2026-10-25T10:00:00+00:00"}


def digest(entries, agents=(), gauge=lambda p: 42):
    return etat.digest(NAME, find=lambda n: "x.jsonl", read=fake_read(entries, agents), now=lambda: 0.0, gauge=gauge)


def test_last_sentence_strips_markdown_and_cuts():
    text = "Intro.\n\n**Fait** : voir [le doc](http://x/y) et `code`. Prochaine étape : tests"
    assert digest([{"k": "reponse", "texte": text}])["derniere_phrase"] == "Prochaine étape : tests"
    long = digest([{"k": "reponse", "texte": "a" * 300}])["derniere_phrase"]
    assert len(long) == etat.SENTENCE_MAX and long.endswith("…")


def test_last_sentence_skips_tool_entries_and_unknown_without_reply():
    entries = [{"k": "reponse", "texte": "Un. Deux."}, {"k": "outil", "outil": "Bash", "texte": "ls"}]
    assert digest(entries)["derniere_phrase"] == "Deux."
    assert digest([{"k": "outil", "texte": "ls"}])["derniere_phrase"] is None


def test_agents_detail():
    running = [{"description": "Lire le code", "model": "haiku", "depuis": "t1"}, {"description": "", "model": "", "depuis": "t2"}]
    assert digest([], running)["agents"] == [{"nom": "Lire le code", "modele": "haiku", "depuis": "t1"},
                                             {"nom": "sous-agent", "modele": None, "depuis": "t2"}]
    assert digest([], running)["sous_agents"] == 2


def test_context_uses_hook_gauge(tmp_path):
    log = tmp_path / "j.jsonl"
    usage = {"input_tokens": 10, "cache_read_input_tokens": 90_000, "cache_creation_input_tokens": 0}
    log.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-5", "usage": usage}}) + "\n")
    assert etat.context_pct(log, hooks.context_config(None)) == 45
    assert etat.context_pct(tmp_path / "absent.jsonl", hooks.context_config(None)) is None


def test_unreadable_journal_leaves_fields_unknown():
    def boom(name):
        raise journal.SessionError("no journal")
    out = etat.digest(NAME, find=boom)
    assert out["contexte"] is None and out["agents"] == [] and out["derniere_phrase"] is None
    assert digest([], gauge=lambda p: 1 / 0)["contexte"] is None
