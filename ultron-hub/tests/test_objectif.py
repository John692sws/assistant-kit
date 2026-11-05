"""Todo #496: objectif stocké hors session."""

import json
import os
import subprocess
import sys
from pathlib import Path

from ultron_hub import cli


def run(capsys, *argv):
    code = cli.main(["objectif", *argv])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_add_show_list(project, capsys):
    code, out = run(capsys, "add", "--but", "Finir X", "--critere", "pytest -q", "--critere", "git status", "--chantier", "c")
    assert code == 0 and out["data"]["id"] == 1
    obj = out["data"]
    assert obj["etape"] == "cadrage" and obj["statut"] == "ouvert" and obj["preuves"] == []
    assert obj["critere_fin"] == ["pytest -q", "git status"] and obj["budget"] is None and obj["chantier"] == "c"
    code, out = run(capsys, "add", "--but", "Y")
    assert out["data"]["id"] == 2 and out["data"]["critere_fin"] == []
    assert run(capsys, "show", "1")[1]["data"] == obj
    assert [o["id"] for o in run(capsys, "list")[1]["data"]["objectifs"]] == [1, 2]
    assert run(capsys, "list", "--statut", "clos")[1]["data"]["objectifs"] == []
    f = project / "var" / "objectifs" / "1.json"
    assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_etape_et_preuve(project, capsys):
    run(capsys, "add", "--but", "Z")
    run(capsys, "etape", "1", "--etape", "codage")
    code, out = run(capsys, "preuve", "1", "--texte", "tests verts")
    assert code == 0 and out["data"]["etape"] == "codage"
    assert out["data"]["preuves"][0]["texte"] == "tests verts" and out["data"]["preuves"][0]["le"]
    assert out["data"]["maj_le"] >= out["data"]["cree_le"]


def test_validation(project, capsys):
    for argv in (["add", "--but", "  "], ["add", "--but", "x" * 2001], ["show", "9"], ["etape", "9", "--etape", "a"],
                 ["preuve", "9", "--texte", "a"]):
        code, out = run(capsys, *argv)
        assert code == 1 and out["status"] == "error" and out["error"]
    assert run(capsys, "add", "--but", "x" * 2000)[0] == 0
    assert not list((project / "var" / "objectifs").glob("*.tmp"))


def test_relu_par_un_nouveau_processus(project, capsys):
    run(capsys, "add", "--but", "Survivre à la session", "--critere", "true")
    run(capsys, "preuve", "1", "--texte", "vu")
    env = {**os.environ, "ULTRON_HUB_ROOT": str(project)}
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    res = subprocess.run([sys.executable, "-m", "ultron_hub.cli", "objectif", "show", "1"], env=env,
                         capture_output=True, text=True, check=True)
    obj = json.loads(res.stdout.strip().splitlines()[-1])["data"]
    assert obj["but"] == "Survivre à la session" and obj["critere_fin"] == ["true"] and obj["preuves"][0]["texte"] == "vu"


# --- todo #497 : objectif verifier ---

import pytest  # noqa: E402

from ultron_hub import repos  # noqa: E402


@pytest.fixture
def no_bwrap(monkeypatch):
    monkeypatch.setattr(repos, "sandbox_argv", lambda cmd, tree, read_only: cmd)


def test_verifier_vrai_contrat(project, capsys, tmp_path, no_bwrap):
    (tmp_path / "a.txt").write_text("x")
    run(capsys, "add", "--but", "B", "--critere", "true", "--critere", "test -f a.txt", "--critere", "echo 'a b'")
    code, out = run(capsys, "verifier", "1", "--dossier", str(tmp_path))
    assert code == 0 and out["status"] == "ok" and out["data"]["ok"] is True
    assert [r["rc"] for r in out["data"]["resultats"]] == [0, 0, 0]
    assert out["data"]["resultats"][2]["sortie"].strip() == "a b" and "duree" in out["data"]["resultats"][0]
    assert run(capsys, "show", "1")[1]["data"]["preuves"][-1]["texte"] == "verifier : OK 3/3"


def test_verifier_faux_contrat(project, capsys, tmp_path, no_bwrap):
    run(capsys, "add", "--but", "B", "--critere", "true", "--critere", "test -f absent.txt", "--critere", "true")
    code, out = run(capsys, "verifier", "1", "--dossier", str(tmp_path))
    assert code == 1 and out["status"] == "ko" and out["data"]["ok"] is False
    assert [r["rc"] for r in out["data"]["resultats"]] == [0, 1, 0]
    assert run(capsys, "show", "1")[1]["data"]["preuves"][-1]["texte"] == "verifier : KO, 1/3 en échec, dont test -f absent.txt"


def test_verifier_vide_illisible_timeout_troncature(project, capsys, tmp_path, no_bwrap):
    run(capsys, "add", "--but", "vide")
    code, out = run(capsys, "verifier", "1", "--dossier", str(tmp_path))
    assert code == 1 and out["data"]["ok"] is False and "contrat vide" in out["summary"]
    run(capsys, "add", "--but", "illisible", "--critere", "echo 'non fermé")
    code, out = run(capsys, "verifier", "2", "--dossier", str(tmp_path))
    assert code == 1 and out["data"]["resultats"][0]["rc"] is None and "illisible" in out["data"]["resultats"][0]["sortie"]
    run(capsys, "add", "--but", "lent", "--critere", "sleep 5")
    code, out = run(capsys, "verifier", "3", "--dossier", str(tmp_path), "--timeout", "0.3")
    assert code == 1 and out["data"]["resultats"][0]["rc"] is None and "timeout" in out["data"]["resultats"][0]["sortie"]
    run(capsys, "add", "--but", "long", "--critere", "head -c 20000 /dev/zero")
    out = run(capsys, "verifier", "4", "--dossier", str(tmp_path))[1]
    assert len(out["data"]["resultats"][0]["sortie"]) < 4100


def test_verifier_dossiers_refuses(project, capsys, tmp_path, no_bwrap):
    run(capsys, "add", "--but", "B", "--critere", "true")
    fichier = tmp_path / "f"
    fichier.write_text("")
    for d in ("relatif", "/", str(Path.home()), str(Path.home() / ".ssh"), "/etc", str(tmp_path / "nope"), str(fichier)):
        code, out = run(capsys, "verifier", "1", "--dossier", d)
        assert code == 1 and out["status"] == "error" and out["error"], d
    code, out = run(capsys, "verifier", "9", "--dossier", str(tmp_path))
    assert code == 1 and out["status"] == "error"
    assert run(capsys, "show", "1")[1]["data"]["preuves"] == []


def test_verifier_dossier_par_defaut_et_sandbox(project, capsys, no_bwrap, monkeypatch):
    vu = []
    monkeypatch.setattr(repos, "sandbox_argv", lambda cmd, tree, read_only: vu.append((tree, read_only)) or cmd)
    run(capsys, "add", "--but", "B", "--critere", "true")
    assert run(capsys, "verifier", "1")[0] == 0
    assert vu == [(project.resolve(), [])]
