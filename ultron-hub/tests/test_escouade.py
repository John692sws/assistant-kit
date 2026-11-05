"""Prompts d'escouade (todo #95) : socle commun + fichier sujet."""

import json
from pathlib import Path

import pytest

from ultron_hub import cli
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.escouade import core

SOCLE = """## 2. Arrêt
Limite : {limite}. Drapeau : {drapeau}.
## 3. Orchestrateur
Travaille.
## 5. Règles
Fiche : {fiche}.
## 6. Rendre compte
Compte.
## 7. Passation
Passe {sujet}.
"""

SUJET = """# Sujet
Objectif {x} libre.

## 1. Lire d'abord
- lire
## 4. Pistes
- piste A
"""

KW = dict(sujet="demo", limite="3 h", fiche="/f.md", drapeau="/d")


def titles(text):
    return [line for line in text.splitlines() if line.startswith("## ")]


def test_order_and_parameters():
    text = core.assemble(SOCLE, "", SUJET, **KW)
    assert [t[:5] for t in titles(text)] == ["## 2.", "## 5.", "## 3.", "## 1.", "## 4.", "## 6.", "## 7."]
    assert "Limite : 3 h. Drapeau : /d." in text and "Fiche : /f.md." in text and "Passe demo." in text
    assert "{x}" in text  # autres accolades intactes


def test_missing_parameter_is_an_error():
    with pytest.raises(InvalidRequestError, match=r"\{fiche\}"):
        core.assemble(SOCLE, "", SUJET, **{**KW, "fiche": "{fiche}"})


def test_subject_without_section_4():
    with pytest.raises(InvalidRequestError, match=r"## 4\."):
        core.assemble(SOCLE, "", "# S\n## 1. Lire\n", **KW)


def test_socle_missing_section():
    with pytest.raises(InvalidRequestError, match=r"## 6\."):
        core.assemble(SOCLE.replace("## 6. Rendre compte\nCompte.\n", ""), "", SUJET, **KW)


def test_old_full_format_is_converted():
    old = ("# S\n## 1. Lire\n- l\n## 2. Vieux arrêt\nANCIEN2\n## 3. Vieux orch\nANCIEN3\n## 4. Pistes\n- p\n"
           "## 5. Vieilles règles\nANCIEN5\n## 6. Vieux\nANCIEN6\n## 7. Vieille passation\nANCIEN7\n")
    text = core.assemble(SOCLE, "", old, **KW)
    assert "ANCIEN" not in text
    assert [t[:5] for t in titles(text)] == ["## 2.", "## 5.", "## 3.", "## 1.", "## 4.", "## 6.", "## 7."]
    assert "- p" in text and "Limite : 3 h" in text


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "_socle.md").write_text(SOCLE)
    (tmp_path / "demo.md").write_text(SUJET)
    (tmp_path / "autre.md").write_text(SUJET)
    (tmp_path / "archives").mkdir()
    (tmp_path / "archives" / "vieux.md").write_text(SUJET)
    return tmp_path


def run(capsys, *argv):
    code = cli.main(["escouade", *argv])
    return code, json.loads(capsys.readouterr().out)


def test_cli_prompt_defaults_and_output_file(capsys, folder, tmp_path):
    out = tmp_path / "p.md"
    code, payload = run(capsys, "prompt", "demo", "--limite", "2 h", "--dossier", str(folder), "--sortie", str(out))
    assert code == 0 and payload["command"] == "escouade.prompt" and payload["status"] == "ok"
    assert "~/notes/chantier-demo.md" in payload["data"]["text"]
    assert "aucun (session lancée à la main)" in payload["data"]["text"]
    assert out.read_text() == payload["data"]["text"]


@pytest.mark.parametrize("name", ["_socle", "../x", "a/b", "..", ".caché"])
def test_cli_refuses_dangerous_names(capsys, folder, name):
    code, payload = run(capsys, "prompt", name, "--limite", "1 h", "--dossier", str(folder))
    assert code == 1 and payload["status"] == "error" and payload["error"]


def test_cli_unknown_subject(capsys, folder):
    code, payload = run(capsys, "prompt", "nul", "--limite", "1 h", "--dossier", str(folder))
    assert code == 1 and "introuvable" in payload["error"]


def test_cli_subjects(capsys, folder):
    code, payload = run(capsys, "sujets", "--dossier", str(folder))
    assert code == 0 and payload["data"]["sujets"] == ["autre", "demo"]


def test_dossier_parameter(tmp_path):
    socle = SOCLE.replace("Passe {sujet}.", "Passe {dossier}/{sujet}.md.")
    assert f"Passe {Path.home()}/notes/prompt/demo.md." in core.assemble(socle, "", SUJET, **KW)
    assert "Passe /essai/demo.md." in core.assemble(socle, "", SUJET, **KW, dossier="/essai")
    (tmp_path / "_socle.md").write_text(socle)
    (tmp_path / "demo.md").write_text(SUJET)
    assert f"Passe {tmp_path}/demo.md." in core.build("demo", "3 h", dossier=str(tmp_path))
