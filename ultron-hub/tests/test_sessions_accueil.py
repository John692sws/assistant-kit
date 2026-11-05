"""Todo #281: what the dashboard's home screen says of each ultron session, and the x/y todos of each chantier."""

import sqlite3
from types import SimpleNamespace

from ultron_hub.chantier import core as chantier_core
from ultron_hub.dashboard import catalogue
from ultron_hub.sessions import etat, journal

NOW = 1_800_000_000.0


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _read(entries, agents=(), maj=NOW - 10):
    return lambda path, n: {"entrees": list(entries), "sous_agents": list(agents), "maj": _iso(maj)}


def test_chantier_of_a_session_name():
    assert etat.chantier_of("ultron-dashboard-20-10-09h12") == "dashboard"
    assert etat.chantier_of("ultron-escouade-autonome-20-10-09h12-2") == "escouade-autonome"
    assert etat.chantier_of("ultron-20-10-09h12") is None
    assert etat.chantier_of("ultron-20-10-09h12-2") is None
    assert etat.chantier_of("autre-chose") is None


def test_waits_for_an_answer_only_when_the_agent_asks():
    assert etat.waits({"k": "reponse", "texte": "On fusionne maintenant ?"})
    assert etat.waits({"k": "reponse", "texte": "Deux options :\n**Tu préfères A ou B ?**\n1. A\n2. B"})
    assert etat.waits({"k": "outil", "outil": "AskUserQuestion", "texte": ""})
    assert not etat.waits({"k": "reponse", "texte": "Pourquoi ? Parce que.\n\nC'est fait, tests verts.\nRien d'autre.\nFin."})
    assert not etat.waits({"k": "utilisateur", "texte": "Tu vas bien ?"})
    assert not etat.waits({"k": "outil", "outil": "Bash", "texte": "ls ?"})
    assert not etat.waits(None)


def test_digest_working_waiting_and_sub_agents():
    find = lambda name: "/x.jsonl"  # noqa: E731
    working = etat.digest("ultron-20-10-09h12", find, _read([{"k": "outil", "outil": "Bash"}], [{"description": "a"}] * 2),
                          lambda: NOW)
    assert {k: working[k] for k in ("chantier", "sous_agents", "attend", "travaille")} == {
        "chantier": None, "sous_agents": 2, "attend": False, "travaille": True}
    asking = etat.digest("ultron-fba-20-10-09h12", find, _read([{"k": "reponse", "texte": "Je continue ?"}]), lambda: NOW)
    assert (asking["chantier"], asking["sous_agents"], asking["attend"], asking["travaille"]) == ("fba", 0, True, False)
    idle = etat.digest("ultron-20-10-09h12", find, _read([{"k": "reponse", "texte": "Fini."}], maj=NOW - 3600), lambda: NOW)
    assert idle["travaille"] is False and idle["attend"] is False


def test_digest_of_an_unreadable_session_leaves_fields_unknown():
    def missing(name):
        raise journal.SessionError("journal introuvable")
    assert etat.digest("ultron-dashboard-20-10-09h12", missing) == {
        "chantier": "dashboard", "sous_agents": None, "attend": False, "travaille": False,
        "contexte": None, "agents": [], "derniere_phrase": None}


def test_session_list_reading_whitelists_the_home_fields():
    data = {"sessions": [{"nom": "ultron-20-10-09h12", "cree": 1, "ouverte": True, "chantier": None, "sous_agents": 1,
                          "attend": True, "travaille": False, "chemin": "/secret/x.jsonl"}]}
    out = catalogue.projeter(catalogue.PAR_ID["sessions"].donnees, data)
    assert out["sessions"][0] == {"nom": "ultron-20-10-09h12", "cree": 1, "ouverte": True, "chantier": None, "sous_agents": 1,
                                  "attend": True, "travaille": False}


def test_done_counts_per_chantier(tmp_path):
    db = tmp_path / "ultron.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id INTEGER, title TEXT, status TEXT, chantier TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)", [
        (1, "a", "done", "fba"), (2, "b", "done", "fba"), (3, "c", "todo", "fba"), (4, "d", "cancelled", "fba"),
        (5, "e", "done", None), (6, "f", "done", "dashboard")])
    conn.commit()
    conn.close()
    assert chantier_core.done_counts(SimpleNamespace(todo_db=db)) == {"fba": 2, "dashboard": 1}
    assert chantier_core.done_counts(SimpleNamespace(todo_db=tmp_path / "absent.db")) == {}
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE tasks (id INTEGER, title TEXT, status TEXT)")
    conn.close()
    assert chantier_core.done_counts(SimpleNamespace(todo_db=old)) == {}
