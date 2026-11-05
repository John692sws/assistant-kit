"""Todo #514 : journal avant d'agir."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from ultron_hub import cli, objectif
from ultron_hub.common.settings import load_settings


def run(capsys, *argv):
    code = cli.main(["objectif", *argv])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_intention_puis_resultat(project, capsys):
    run(capsys, "add", "--but", "X")
    code, out = run(capsys, "intention", "1", "--texte", "pousser", "--commande", "git push")
    assert code == 0 and out["data"]["n"] == 1
    assert run(capsys, "intention", "1", "--texte", "autre")[1]["data"]["n"] == 2
    code, out = run(capsys, "resultat", "1", "--n", "1", "--statut", "ok", "--texte", "fait")
    a = out["data"]["actions"][0]
    assert code == 0 and a["statut"] == "ok" and a["fini_le"] and a["commande"] == "git push"
    assert out["data"]["actions"][1]["statut"] == "en_cours"


def test_refus(project, capsys):
    run(capsys, "add", "--but", "X")
    run(capsys, "intention", "1", "--texte", "a")
    assert run(capsys, "resultat", "1", "--n", "7", "--statut", "ok")[0] == 1
    assert run(capsys, "intention", "1", "--texte", "x" * 501)[0] == 1
    assert run(capsys, "intention", "1", "--texte", "a", "--commande", "c" * 501)[0] == 1
    assert run(capsys, "resultat", "1", "--n", "1", "--statut", "ok", "--texte", "x" * 501)[0] == 1
    assert run(capsys, "intention", "1", "--texte", "x" * 500)[0] == 0


def test_crash_laisse_la_trace_et_show_dit_interrompue(project, capsys, monkeypatch):
    s = load_settings()
    run(capsys, "add", "--but", "X")
    with pytest.raises(KeyboardInterrupt):
        with objectif.action(s, 1, "supprimer", "rm x"):
            raise KeyboardInterrupt
    acts = objectif.lire(s, 1)["actions"]
    assert acts[0]["statut"] == "en_cours" and "fini_le" not in acts[0]
    out = run(capsys, "show", "1")[1]
    assert out["data"]["interrompues"] == [] and "interrompue" not in out["summary"]
    futur = datetime.now(timezone.utc) + timedelta(minutes=31)
    monkeypatch.setattr(objectif, "_maintenant", lambda: futur)
    out = run(capsys, "show", "1")[1]
    assert out["data"]["interrompues"] == [1] and "action interrompue 1" in out["summary"]


def test_action_ok_et_exception(project, capsys):
    s = load_settings()
    run(capsys, "add", "--but", "X")
    with objectif.action(s, 1, "a"):
        pass
    with pytest.raises(ValueError):
        with objectif.action(s, 1, "b"):
            raise ValueError("boum")
    acts = objectif.lire(s, 1)["actions"]
    assert acts[0]["statut"] == "ok" and acts[1]["statut"] == "echec" and "boum" in acts[1]["resultat"]
