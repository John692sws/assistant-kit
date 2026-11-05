"""Todo #478: `ultron-hub chantier nuit creer | fin | attendre`. Throwaway folders, fake git log, fake sessions."""

import json
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from cli_contract import check_argv
from ultron_hub.chantier import core, nuit
from ultron_hub.common.errors import InvalidRequestError

LOG = "aaa1111 Add thing (todo #11)\nbbb2222 Fusion automatique de todo-12/x (todo #12, reste ouverte)\n"


@pytest.fixture
def env(tmp_path):
    notes = tmp_path / "notes"
    prompts = notes / "prompt"
    prompts.mkdir(parents=True)
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, status TEXT, chantier TEXT)")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, task_id INTEGER, action TEXT, details TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [(10, "libre", "todo", "alpha"), (11, "dans le log", "todo", "alpha"),
                                                           (12, "fusionnée", "todo", None), (13, "livrée", "todo", "beta"),
                                                           (14, "sans chantier", "todo", None)])
    conn.execute("INSERT INTO events VALUES (1, 13, 'livree', '{}')")
    conn.commit()
    conn.close()
    settings = SimpleNamespace(todo_db=db, root=tmp_path / "hub")
    return settings, core.sources(str(prompts), str(tmp_path / "boites")), prompts, notes


@pytest.mark.parametrize("day,nom", [(date(2026, 10, 29), "nuit-29-30-10"), (date(2026, 10, 31), "nuit-31-01-10"),
                                     (date(2026, 12, 31), "nuit-31-01-12")])
def test_name_by_date(day, nom):
    assert nuit.nom_de(day) == nom


def test_creer_writes_three_files_and_refuses_twice(env):
    settings, src, prompts, notes = env
    data = nuit.creer(settings, src, date(2026, 10, 29), LOG, datetime(2026, 10, 29, 22, 0))
    assert data["nom"] == "nuit-29-30-10"
    text = (prompts / "nuit-29-30-10.md").read_text()
    assert text.startswith("# Chantier « Nuit 29-30/10 » (temporaire)") and "Session précédente : aucune." in text
    assert "Fiche du chantier : `~/notes/chantier-nuit-29-30-10.md`" in text and "Domaine : systeme" in text
    assert "## 1. Mission" in text and "## 4. Pistes" in text and "UN SEUL Opus" in text and "85 %" in text
    assert "45 min" in text and "ultron relance" in text and "pgrep -f pytest" in text
    assert (prompts / "reprises/nuit-29-30-10.md").read_text().startswith("## Reprise")
    assert (notes / "chantier-nuit-29-30-10.md").is_file()
    with pytest.raises(InvalidRequestError, match="existe déjà"):
        nuit.creer(settings, src, date(2026, 10, 29), LOG)


def test_fiche_discards_delivered_and_seen_in_git_log(env):
    settings, src, _, notes = env
    nuit.creer(settings, src, date(2026, 10, 29), LOG, datetime(2026, 10, 29, 22, 5))
    fiche = (notes / "chantier-nuit-29-30-10.md").read_text()
    todo, ecarte = fiche.split("## Déjà faites, à écarter")
    ecarte, preuve = ecarte.split("## Preuve")
    assert "#10 libre" in todo and "#14 sans chantier" in todo and "### alpha" in todo and "### Hors chantier" in todo
    assert "#11" in ecarte and "#12" in ecarte and "#13" in ecarte and "livrée" in ecarte
    assert "#11" not in todo and "#12" not in todo and "#13" not in todo
    assert "bbb2222 Fusion automatique" in preuve and "22h05" in fiche


def test_git_log_reads_main_without_a_shell(env, monkeypatch):
    calls = []
    monkeypatch.setattr(nuit.subprocess, "run", lambda *a, **k: calls.append((a, k)) or SimpleNamespace(returncode=0, stdout="x\n", stderr=""))
    assert nuit.git_log(env[0]) == "x\n"
    argv = calls[0][0][0]
    assert isinstance(argv, list) and argv[-3:] == ["--oneline", "-40", "main"] and not calls[0][1].get("shell")


def test_fin_refuses_bad_name_and_open_session(env):
    settings, src, prompts, _ = env
    nuit.creer(settings, src, date(2026, 10, 29), LOG)
    for bad in ("alpha", "nuit-1-2-3", "../nuit-29-30-10"):
        with pytest.raises(InvalidRequestError):
            nuit.fin(settings, src, bad, lambda: [])
    with pytest.raises(InvalidRequestError, match="encore ouverte"):
        nuit.fin(settings, src, "nuit-29-30-10", lambda: ["ultron-nuit-29-30-10-22h00"])
    assert (prompts / "nuit-29-30-10.md").is_file()


def test_fin_keeps_the_reprise_and_deletes_the_rest(env):
    settings, src, prompts, notes = env
    nuit.creer(settings, src, date(2026, 10, 29), LOG)
    (prompts / "reprises/nuit-29-30-10.md").write_text("## Reprise\n- compte rendu\n")
    (prompts / "archives").mkdir()
    (prompts / "archives/reprises-nuit-29-30-10-2026-10.md").write_text("old")
    (settings.root / "var/prompts").mkdir(parents=True)
    (settings.root / "var/prompts/interactif-nuit-29-30-10.md").write_text("p")
    data = nuit.fin(settings, src, "nuit-29-30-10", lambda: ["ultron-alpha-29-10-22h00"])
    assert (prompts / "archives/reprise-nuit-29-30-10.md").read_text().endswith("compte rendu\n")
    assert not (prompts / "nuit-29-30-10.md").exists() and not (notes / "chantier-nuit-29-30-10.md").exists()
    assert not (prompts / "reprises/nuit-29-30-10.md").exists()
    assert not (prompts / "archives/reprises-nuit-29-30-10-2026-10.md").exists()
    assert not (settings.root / "var/prompts/interactif-nuit-29-30-10.md").exists()
    assert len(data["fait"]) == 5
    nuit.creer(settings, src, date(2026, 10, 29), LOG)  # a fresh night can be created again
    with pytest.raises(InvalidRequestError, match="existe déjà"):  # the kept Reprise is never overwritten
        nuit.fin(settings, src, "nuit-29-30-10", lambda: [])


def make_attendre(states):
    """`states`: one dict {session: travaille} per poll."""
    clock = {"t": 0.0}
    polls = iter(states)
    cur = {}

    def sessions():
        cur.clear()
        cur.update(next(polls))
        return list(cur)
    return clock, sessions, (lambda name: {"travaille": cur[name]}), (lambda s: clock.__setitem__("t", clock["t"] + s))


def test_attendre_waits_for_others_only(capsys):
    clock, sessions, digest, sleep = make_attendre([{"ultron-me-1": True, "ultron-a-1": True}, {"ultron-me-1": True, "ultron-a-1": False}])
    out = nuit.attendre("ultron-me-1", 1000, 60, sleep=sleep, clock=lambda: clock["t"], sessions=sessions, digest=digest)
    assert out == {"attendu_s": 60, "attendues": ["ultron-a-1"]}
    assert "ultron-a-1" in capsys.readouterr().err


def test_attendre_does_not_wait_when_idle():
    clock, sessions, digest, sleep = make_attendre([{"ultron-me-1": True}])
    assert nuit.attendre("ultron-me-1", 1000, 60, sleep=sleep, clock=lambda: clock["t"], sessions=sessions, digest=digest) == {
        "attendu_s": 0, "attendues": []}


def test_attendre_gives_up_after_max():
    clock, sessions, digest, sleep = make_attendre([{"ultron-a-1": True}] * 10)
    out = nuit.attendre("ultron-me-1", 120, 60, sleep=sleep, clock=lambda: clock["t"], sessions=sessions, digest=digest)
    assert out == {"attendu_s": 120, "attendues": ["ultron-a-1"], "temps_depasse": True}


def test_cli_parses_nuit_commands(env):
    check_argv(["chantier", "nuit", "creer", "--date", "2026-10-29"])
    check_argv(["chantier", "nuit", "fin", "nuit-29-30-10"])
    check_argv(["chantier", "nuit", "attendre", "--session", "ultron-x-29-10-22h00", "--max-s", "10", "--pause-s", "1"])


def test_handle_creer_envelope(env, monkeypatch):
    from ultron_hub.chantier import commands
    settings, _, prompts, _ = env
    monkeypatch.setattr(nuit, "git_log", lambda s: LOG)
    args = SimpleNamespace(chantier_command="nuit", nuit_command="creer", date="2026-10-31", dossier=str(prompts), boites_dir=None)
    res = commands.handle(args, settings)
    assert res["status"] == "ok" and res["data"]["nom"] == "nuit-31-01-10" and res["command"] == "chantier.nuit.creer"
    json.dumps(res)
    args.date = "pas-une-date"
    with pytest.raises(InvalidRequestError):
        commands.handle(args, settings)
