"""Todo #498: frein Stop (objectif stop-hook) et objectif lier."""

import io
import json

import pytest

from ultron_hub import cli, objectif

ERR: list = []


def run(capsys, *argv):
    code = cli.main(["objectif", *argv])
    cap = capsys.readouterr()
    ERR.append(cap.err)
    return code, cap.out.strip()


def jrun(capsys, *argv):
    code, out = run(capsys, *argv)
    return code, json.loads(out.splitlines()[-1])


@pytest.fixture
def tmux(monkeypatch):
    monkeypatch.setattr(objectif, "_session_tmux", lambda: "sess-1")


def stop(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"hook_event_name": "Stop"}'))
    code, out = run(capsys, "stop-hook")
    assert code == 0
    return json.loads(out) if out else None


def lie(capsys, session="sess-1"):
    jrun(capsys, "add", "--but", "X", "--critere", "true")
    return jrun(capsys, "lier", "1", "--session", session)


def test_pas_lie_permis(project, capsys, monkeypatch, tmux):
    jrun(capsys, "add", "--but", "X")
    assert stop(capsys, monkeypatch) is None


def test_hors_tmux_permis(project, capsys, monkeypatch):
    lie(capsys)
    monkeypatch.delenv("TMUX", raising=False)
    assert stop(capsys, monkeypatch) is None


def test_sans_preuve_trois_relances_puis_permis(project, capsys, monkeypatch, tmux):
    lie(capsys)
    for n in (1, 2, 3):
        out = stop(capsys, monkeypatch)
        assert out["decision"] == "block" and "Objectif 1" in out["reason"] and "verifier 1" in out["reason"]
        assert jrun(capsys, "show", "1")[1]["data"]["preuves"][-1]["texte"] == f"frein : relance {n}/3"
    assert stop(capsys, monkeypatch) is None
    assert jrun(capsys, "show", "1")[1]["data"]["preuves"][-1]["texte"] == "frein : arrêt permis après 3 relances"


def test_verifier_ok_permis_et_remet_a_zero(project, capsys, monkeypatch, tmux):
    lie(capsys)
    stop(capsys, monkeypatch)
    jrun(capsys, "preuve", "1", "--texte", "verifier : OK 1/1")
    assert stop(capsys, monkeypatch) is None
    assert jrun(capsys, "show", "1")[1]["data"]["relances_stop"] == 0


def test_ok_puis_changement_etape_bloque(project, capsys, monkeypatch, tmux):
    lie(capsys)
    jrun(capsys, "preuve", "1", "--texte", "verifier : OK 1/1")
    jrun(capsys, "etape", "1", "--etape", "retouche")
    assert stop(capsys, monkeypatch)["decision"] == "block"
    jrun(capsys, "preuve", "1", "--texte", "verifier : OK 1/1")
    assert stop(capsys, monkeypatch) is None


def test_ko_bloque(project, capsys, monkeypatch, tmux):
    lie(capsys)
    jrun(capsys, "preuve", "1", "--texte", "verifier : KO, 1/1 en échec, dont false")
    assert stop(capsys, monkeypatch)["decision"] == "block"


def test_exception_permis(project, capsys, monkeypatch, tmux):
    lie(capsys)
    monkeypatch.setattr(objectif, "lister", lambda *a, **k: 1 / 0)
    assert stop(capsys, monkeypatch) is None
    assert "ZeroDivisionError" in ERR[-1]


def test_tmux_echec_permis(project, capsys, monkeypatch):
    lie(capsys)
    monkeypatch.setenv("TMUX", "x")
    monkeypatch.setattr(objectif.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("pas de tmux")))
    assert stop(capsys, monkeypatch) is None


def test_lier_nom_pris_refuse(project, capsys):
    lie(capsys)
    jrun(capsys, "add", "--but", "Y")
    code, out = jrun(capsys, "lier", "2", "--session", "sess-1")
    assert code == 1 and out["status"] == "error" and "déjà liée" in out["error"]
    assert jrun(capsys, "lier", "1", "--session", "")[1]["data"]["session"] is None
    assert jrun(capsys, "lier", "2", "--session", "sess-1")[0] == 0
    assert jrun(capsys, "lier", "2", "--session", "a b")[0] == 1
