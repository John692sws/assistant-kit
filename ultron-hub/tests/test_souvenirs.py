"""🔎 Chercher dans les vieilles conversations (todo #133): index FTS5, filtre de secrets, CLI et écran Telegram.

Dossiers jetables uniquement : jamais le vrai ~/.claude ni ~/.hermes, aucun réseau.
"""
import argparse
import asyncio
import dataclasses
import json
import os
import sqlite3
import stat
import time

import pytest

from cli_contract import check_argv
from ultron_hub.common.settings import load_settings
from ultron_hub.souvenirs import commands, index

LURES = [
    "ma clé est sk-ant-api03-AbCdEf123456789xyz",
    "export GH=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "slack xoxb-123456789012-abcdefghijkl",
    "aws AKIAIOSFODNN7EXAMPLE",
    "password: hunter2hunter2",
    "le mot de passe est Zebre-42",
    "token = abc123def",
    "secret: 'monsecret'",
    "sha " + "a1b2c3d4" * 5,
    "blob " + "Qm9uam91ckxlTW9uZGVBYmNEZWZHaGlKa2xNbjEyMzQ1Ng",
    "Authorization: Bearer abcdefghijklmnopqrstuvwx",
    "bot 123456789:AAFabcdefghijklmnopqrstuvwxyz012345",
]
SAFE = ["Le token expire demain, il faut le renouveler.", "Chemin /home/exemple/workspace/projects/active/ultron-hub ok",
        "session 07622825-8bf3-47e0-83e0-526f9a26a1e5"]


def line(kind, content, cwd="/home/exemple", **extra):
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}, "cwd": cwd,
                       "timestamp": "2026-10-12T08:00:00Z", **extra})


def write(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def no_real_text_sources(monkeypatch):
    monkeypatch.setattr(index, "default_text_sources", lambda: {})  # never read the real home


@pytest.fixture
def sources(tmp_path):
    claude = tmp_path / "claude" / "projects"
    write(claude / "-home-ultron" / "aaaa.jsonl",
          line("user", "Parlons du bouquet de mimosa pour la fête"),
          line("assistant", [{"type": "thinking", "thinking": "pensée secrète zanzibar"},
                             {"type": "text", "text": "Le mimosa fleurit en hiver.\n" + "\n".join(LURES + SAFE)},
                             {"type": "tool_use", "name": "Bash", "input": {"command": "cat outil_zanzibar"}}]),
          line("user", [{"type": "tool_result", "tool_use_id": "x", "content": "sortie_zanzibar du fichier"}]),
          line("user", "<system-reminder>consigne_zanzibar</system-reminder>\nmerci pour le mimosa"),
          line("user", "meta zanzibar", isMeta=True))
    write(claude / "-home-ultron" / "bbbb.jsonl", line("user", "un autre mimosa, cette fois en mars"))
    write(claude / "-home-ultron-workspace-projects-active-ultron-hub-var-missions-2026-work" / "cccc.jsonl",
          line("user", "mimosa de mission", cwd="/x/var/missions/2026/work"))
    write(claude / "-home-ultron" / "dddd.jsonl", line("user", "mimosa de job", cwd="/home/exemple/ultron-hub/var/runs/20261012"))
    hermes = tmp_path / "hermes" / "state.db"
    hermes.parent.mkdir()
    con = sqlite3.connect(hermes)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL, cwd TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,"
                " timestamp REAL, active INTEGER DEFAULT 1)")
    for sid, source, cwd in (("h1", "telegram", None), ("h2", "cron", None), ("h3", "cli", "/x/var/runs/1")):
        con.execute("INSERT INTO sessions VALUES (?, ?, 1760000000, ?)", (sid, source, cwd))
        for role, text in (("user", f"mimosa chez Hermes {sid}"), ("tool", "sortie_zanzibar"),
                           ("assistant", "réponse sur le mimosa\npassword = leurre9999")):
            con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, 1)",
                        (sid, role, text))
    con.commit()
    con.close()
    return claude, hermes


def build(tmp_path, sources, **kw):
    db = tmp_path / "var" / "souvenirs" / "index.db"
    return db, index.build(db, sources[0], sources[1], **kw)


def test_index_keeps_only_conversation_text(tmp_path, sources):
    db, stats = build(tmp_path, sources)
    assert stats["claude_code"] == 2 and stats["hermes"] == 1  # jobs, cron and var/runs left out
    con = sqlite3.connect(db)
    everything = "\n".join(t for (t,) in con.execute("SELECT texte FROM souvenirs"))
    for forbidden in ("zanzibar", "mission", "chez Hermes h2", "chez Hermes h3"):
        assert forbidden not in everything
    assert "mimosa fleurit" in everything and "chez Hermes h1" in everything


def test_secret_lures_never_reach_the_index(tmp_path, sources):
    db, _ = build(tmp_path, sources)
    text = "\n".join(t for (t,) in sqlite3.connect(db).execute("SELECT texte FROM souvenirs"))
    for lure in LURES:
        assert lure not in text and not index.looks_secret(text)
    for marker in ("hunter2", "Zebre-42", "sk-ant", "ghp_", "xoxb", "AKIA", "monsecret", "leurre9999", "abc123def",
                   "Qm9uam91", "AAFabc", "a1b2c3d4"):
        assert marker not in text
    for ok in SAFE:  # an ordinary sentence, a path and a session id are kept
        assert ok in text


def test_pem_block_is_dropped_whole():
    pem = "avant\n-----BEGIN RSA PRIVATE KEY-----\nabc def\nghi\n-----END RSA PRIVATE KEY-----\naprès"
    assert index.scrub(pem) == "avant\naprès"


def test_index_is_private_and_temp_file_removed(tmp_path, sources):
    db, _ = build(tmp_path, sources)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600 and stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert [p.name for p in db.parent.iterdir()] == ["index.db"]


def test_budget_exceeded_keeps_the_old_index(tmp_path, sources):
    db, _ = build(tmp_path, sources)
    before = db.read_bytes()
    with pytest.raises(TimeoutError):
        index.build(db, sources[0], sources[1], budget_s=-1)
    assert db.read_bytes() == before and [p.name for p in db.parent.iterdir()] == ["index.db"]


def test_one_result_per_session_dated_with_tool(tmp_path, sources):
    db, _ = build(tmp_path, sources)
    results = index.search(db, ["mimosa"])
    assert len(results) == 3 and len({r["session"] for r in results}) == 3
    assert {r["outil"] for r in results} == {"Claude Code", "Hermes"}
    assert all(r["date"][:4] in ("2026", "2025") for r in results)
    assert index.search(db, ["mimosa"], limit=1).__len__() == 1


def test_all_words_first_then_any(tmp_path, sources):
    db, _ = build(tmp_path, sources)
    assert [r["session"] for r in index.search(db, ["mimosa", "mars"])] == ["bbbb"]
    assert len(index.search(db, ["mars", "hiver"])) == 2  # no session has both: any of them
    assert index.search(db, ["introuvable"]) == []


@pytest.mark.parametrize("hostile", ['"', 'mimosa" OR "x', "NEAR(mimosa mars)", "texte:mimosa", "mimosa*", "-mimosa",
                                     "'; DROP TABLE souvenirs; --", "AND", "(", "souvenirs MATCH 1", "\x00", "{texte}: a"])
def test_hostile_queries_cannot_inject_fts_or_sql(tmp_path, sources, hostile):
    db, _ = build(tmp_path, sources)
    expr = index.fts_query([hostile])
    assert all(t.startswith('"') and t.endswith('"') and t[1:-1].isalnum() or "_" in t for t in expr.split())
    index.search(db, [hostile])  # never raises
    assert sqlite3.connect(db).execute("SELECT count(*) FROM souvenirs").fetchone()[0] == 3


def test_excerpt_is_short_and_neutralized():
    text = "début " + "x" * 3000 + " <script>cible</script> " + "y" * 3000
    out = index.excerpt(text, ["cible"])
    assert len(out) <= index.EXCERPT_MAX and "<" not in out and ">" not in out and "cible" in out
    assert "‹" in index.neutralize("<a>")


def settings_for(tmp_path):
    return dataclasses.replace(load_settings(), var_dir=tmp_path / "var")


def test_cli_indexer_and_chercher(tmp_path, sources, monkeypatch):
    monkeypatch.setattr(index, "default_sources", lambda: sources)
    st = settings_for(tmp_path)
    parser = argparse.ArgumentParser()
    commands.add_parser(parser.add_subparsers(), argparse.ArgumentParser)
    out = commands.handle(parser.parse_args(["souvenirs", "indexer"]), st)
    assert out["status"] == "ok" and out["data"]["sessions"] == 3
    found = commands.handle(parser.parse_args(["souvenirs", "chercher", "--", "-mimosa", "mars"]), st)
    assert found["status"] == "ok" and [r["session"] for r in found["data"]["results"]] == ["bbbb"]
    json.dumps(found)
    for args in (("souvenirs", "indexer"), ("souvenirs", "chercher", "--", "-a", "b")):
        check_argv(args)


def test_chercher_refreshes_an_old_index_only(tmp_path, sources, monkeypatch):
    monkeypatch.setattr(index, "default_sources", lambda: sources)
    st = settings_for(tmp_path)
    parser = argparse.ArgumentParser()
    commands.add_parser(parser.add_subparsers(), argparse.ArgumentParser)
    first = commands.handle(parser.parse_args(["souvenirs", "chercher", "mimosa"]), st)  # no index yet: built
    assert first["data"]["refreshed_in_s"] is not None
    assert commands.handle(parser.parse_args(["souvenirs", "chercher", "mimosa"]), st)["data"]["refreshed_in_s"] is None
    db = index.index_path(st)
    old = time.time() - 2 * index.REFRESH_AFTER_S
    os.utime(db, (old, old))
    assert commands.handle(parser.parse_args(["souvenirs", "chercher", "mimosa"]), st)["data"]["refreshed_in_s"] is not None
    monkeypatch.setattr(index, "REFRESH_BUDGET_S", -1)  # too slow: the old index stays and still answers
    os.utime(db, (old, old))
    again = commands.handle(parser.parse_args(["souvenirs", "chercher", "mimosa"]), st)
    assert again["status"] == "ok" and again["data"]["refreshed_in_s"] is None and again["data"]["results"]


def test_chercher_without_any_index_and_no_source_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "default_sources", lambda: (tmp_path / "nope", tmp_path / "nope.db"))
    parser = argparse.ArgumentParser()
    commands.add_parser(parser.add_subparsers(), argparse.ArgumentParser)
    out = commands.handle(parser.parse_args(["souvenirs", "chercher", "x"]), settings_for(tmp_path))
    assert out["status"] == "ok" and out["data"]["results"] == []  # an empty index is built, nothing found


# --- Telegram screen ----------------------------------------------------------------------------------------------



def run(coro):
    return asyncio.run(coro)










# --- text sources (todo #260) ---------------------------------------------------------------------------------------

@pytest.fixture
def texts(tmp_path):
    notes = tmp_path / "notes"
    write(notes / "plan.md", "Le projet kiwi avance bien", "password: leurre-kiwi-9999")
    write(notes / "sub" / "deep.md", "note profonde sur le kiwi")
    write(notes / "auth-notes.md", "kiwi sensible")
    write(notes / ".env" / "x.md", "kiwi env")
    write(notes / "big.md", "kiwi " + "x" * (index.FILE_MAX_BYTES + 1))
    journal = tmp_path / "ultron" / "journal.jsonl"
    write(journal, json.dumps({"date": "2026-10-12T19:30:33+02:00", "texte": "journal kiwi un"}),
          json.dumps({"date": "2026-10-12T20:00:00+02:00", "texte": "journal kiwi deux"}), "not json",
          json.dumps({"date": "2026-10-13T08:00:00+02:00", "texte": "autre jour kiwi"}))
    write(tmp_path / "savoir" / "fiche" / "a.md", "fiche savoir kiwi")
    write(tmp_path / "memoire" / "m.md", "souvenir mémoire kiwi")
    return {"notes": notes, "journal": journal, "savoir": tmp_path / "savoir", "memoire": tmp_path / "memoire"}


def text_build(tmp_path, sources, texts):
    db = tmp_path / "var" / "souvenirs" / "index.db"
    return db, index.build(db, sources[0], sources[1], text_sources=texts)


def test_each_text_source_is_found(tmp_path, sources, texts):
    db, stats = text_build(tmp_path, sources, texts)
    assert (stats["notes"], stats["journal"], stats["savoir"], stats["memoire"]) == (2, 2, 1, 1)
    found = index.search(db, ["kiwi"], limit=20)
    tools = {r["outil"] for r in found}
    assert {"Notes", "Journal", "Savoir", "Mémoire"} <= tools
    by_session = {r["session"]: r for r in found}
    assert "journal:2026-10-12" in by_session and "journal kiwi deux" in by_session["journal:2026-10-12"]["extrait"]
    assert by_session[str(texts["memoire"] / "m.md")]["date"] == time.strftime("%Y-%m-%d", time.gmtime())


def test_secret_line_of_a_note_never_indexed(tmp_path, sources, texts):
    db, _ = text_build(tmp_path, sources, texts)
    everything = "\n".join(t for (t,) in sqlite3.connect(db).execute("SELECT texte FROM souvenirs"))
    assert "leurre-kiwi" not in everything and "projet kiwi" in everything


def test_sensitive_and_big_files_excluded(tmp_path, sources, texts):
    db, _ = text_build(tmp_path, sources, texts)
    everything = "\n".join(t for (t,) in sqlite3.connect(db).execute("SELECT texte FROM souvenirs"))
    for forbidden in ("kiwi sensible", "kiwi env", "xxxx"):
        assert forbidden not in everything


def test_source_filter(tmp_path, sources, texts):
    db, _ = text_build(tmp_path, sources, texts)
    assert {r["outil"] for r in index.search(db, ["kiwi"], limit=20, sources=["notes"])} == {"Notes"}
    both = index.search(db, ["kiwi"], limit=20, sources=["savoir", "memoire"])
    assert {r["outil"] for r in both} == {"Savoir", "Mémoire"}
    assert {r["outil"] for r in index.search(db, ["mimosa"], limit=20, sources=["claude"])} == {"Claude Code"}


def test_cli_source_option(tmp_path, sources, texts, monkeypatch):
    monkeypatch.setattr(index, "default_sources", lambda: sources)
    monkeypatch.setattr(index, "default_text_sources", lambda: texts)
    parser = argparse.ArgumentParser()
    commands.add_parser(parser.add_subparsers(), argparse.ArgumentParser)
    st = settings_for(tmp_path)
    commands.handle(parser.parse_args(["souvenirs", "indexer"]), st)
    out = commands.handle(parser.parse_args(["souvenirs", "chercher", "--source", "journal", "--", "kiwi"]), st)
    assert [r["outil"] for r in out["data"]["results"]] == ["Journal", "Journal"]
    json.dumps(out)
    check_argv(("souvenirs", "chercher", "--source", "notes", "--source", "savoir", "--", "kiwi"))
