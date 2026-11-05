"""/escouade sur Telegram (todo #99) : lancement signé, ligne « Projet : », nouveau sujet, écrans du plugin."""

import asyncio
import io
import json
import time
from pathlib import Path

import pytest

from hermes_plugin import close_key

from ultron_hub import cli
from ultron_hub.common.settings import load_settings
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.escouade import core
from ultron_hub.todo import closing
from cli_contract import check_argv

SOCLE = "## 2. A\n{limite} {drapeau}\n## 3. B\nb\n## 5. C\n{fiche}\n## 6. D\nd\n## 7. E\n{sujet}\n"
SUJET = "# S\n## 4. Pistes\n- a\n"
CHAT = 5


@pytest.fixture
def prompts(tmp_path):
    folder = tmp_path / "prompt"
    folder.mkdir()
    (folder / "_socle.md").write_text(SOCLE)
    (folder / "demo.md").write_text(SUJET)
    return folder


@pytest.fixture
def settings(project):
    return load_settings()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway HOME with a ~/notes folder (the default work folder of a subject)."""
    (tmp_path / "home" / "notes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def run_cli(capsys, *argv):
    code = cli.main(["escouade", *argv])
    return code, json.loads(capsys.readouterr().out)


# -- signed action -----------------------------------------------------------------------------------------

def test_escouade_is_a_signed_action():
    assert "escouade" in closing.ACTIONS and closing.SIGNED_ACTORS["escouade"] == "telegram-button"
    assert close_key.signed_env("escouade", "agent-ultron")[close_key.TOKEN_ENV]
    assert "escouade run agent-ultron" in closing.refusal_hint("escouade", "agent-ultron")


def test_projet_line(tmp_path, home):
    assert core.projet("# T\nProjet : /srv/x\n\n## 1. L\nProjet : /autre\n## 4. P\n") == Path("/srv/x")
    assert core.projet("# T\nProjet : ~/workspace/y\n## 4. P\n") == home / "workspace" / "y"
    assert core.projet("# T\n## 1. L\nProjet : /pas/dans/l-entete\n## 4. P\n") is None
    assert core.projet("# T sans ligne\n## 4. P\n") is None
    with pytest.raises(InvalidRequestError, match="absolu"):
        core.projet("Projet : relatif/chemin\n## 4. P\n")


def test_projet_of_defaults_to_notes_and_must_exist(tmp_path, prompts, home):
    assert core.projet_of("demo", str(prompts)) == home / "notes"
    (prompts / "demo.md").write_text(f"# S\nProjet : {tmp_path}\n## 4. P\n")
    assert core.projet_of("demo", str(prompts)) == tmp_path
    (prompts / "demo.md").write_text("# S\nProjet : /n/existe/pas\n## 4. P\n")
    with pytest.raises(InvalidRequestError, match="introuvable"):
        core.projet_of("demo", str(prompts))
    (prompts / "demo.md").write_text(f"# S\nProjet : {prompts / 'demo.md'}\n## 4. P\n")  # a file, not a folder
    with pytest.raises(InvalidRequestError, match="pas un dossier"):
        core.projet_of("demo", str(prompts))


def nouveau(capsys, monkeypatch, prompts, name, goal, titre="Mon titre"):
    monkeypatch.setattr("sys.stdin", io.StringIO(goal))
    return run_cli(capsys, "nouveau", name, "--titre", titre, "--dossier", str(prompts))


def ok(data=None):
    return {"status": "ok", "summary": "", "data": data, "error": None}


class FakeClient:
    def __init__(self):
        self.calls, self.envs, self.stdins = [], [], []
        self.sujets = {"sujets": ["agent", "fba"], "en_cours": {"fba": "fba-20261016-101010"},
                       "projets": {"agent": "/home/exemple/ws/ultron-hub", "fba": "/home/exemple/notes"}}
        self.fail = {}

    async def call(self, *args, timeout_s=None, env=None, stdin_text=None):
        check_argv(args)  # the real parser: options and arguments the CLI accepts
        self.calls.append(args)
        self.envs.append(env)
        self.stdins.append(stdin_text)
        if args[1] in self.fail:
            return {"status": "error", "summary": "", "data": None, "error": self.fail[args[1]]}
        if args[1] == "sujets":
            return ok(self.sujets)
        if args[1] == "nouveau":
            return ok({"sujet": args[2], "projet": "/home/exemple/notes"})
        if args[1] == "run":
            return ok({"id": f"{args[2]}-20261016-120000", "statut": "en_cours"})
        if args[1] == "stop":
            return ok({"id": args[2]})
        if args[1] == "etat":
            return ok({"id": args[2], "sujet": "agent", "numero": 3, "limite": "la première atteinte de : 3 h",
                       "resume": {"texte": "en_cours | écoulé 1 h 02 min"}, "drapeaux": [], "vivante": True})
        raise AssertionError(args)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def flow_with(client=None):
    client = client or FakeClient()
    signed = []
    flow = AutonomeFlow(client, signer=lambda action, target: signed.append((action, target)) or {"TOKEN": "t"},
                        clock=Clock())
    return flow, client, signed


def run(coro):
    return asyncio.run(coro)


def data_of(screen):
    return [d for row in screen.buttons for _, d in row]


def test_nouveau_creates_a_subject_that_assembles(tmp_path, prompts, capsys, monkeypatch, home):
    code, payload = nouveau(capsys, monkeypatch, prompts, "veille-x", "Surveiller X.\nPuis résumer.")
    assert code == 0, payload
    text = (prompts / "veille-x.md").read_text()
    assert text.startswith("# Mon titre\n\nSession précédente : aucune.\nProjet : " + str(home / "notes"))
    assert "Surveiller X." in text and "## 1. Lire d'abord" in text and "### A. Cadrer le chantier" in text
    assert payload["data"]["projet"] == str(home / "notes")
    assert "Surveiller X." in core.build("veille-x", "1 h", dossier=str(prompts))
    assert core.projet_of("veille-x", str(prompts)) == home / "notes"
    code, again = nouveau(capsys, monkeypatch, prompts, "veille-x", "Autre.")
    assert code != 0 and "existe déjà" in again["error"]
    assert "Surveiller X." in (prompts / "veille-x.md").read_text()


def test_nouveau_refuses_the_name_of_an_archived_subject(tmp_path, prompts, capsys, monkeypatch, home):
    (prompts / "termines").mkdir(parents=True, exist_ok=True)
    (prompts / "termines" / "vieux.md").write_text("# Vieux\n")
    code, payload = nouveau(capsys, monkeypatch, prompts, "vieux", "Refaire.")
    assert code != 0 and "archivés" in payload["error"]
    assert not (prompts / "vieux.md").exists()


def test_nouveau_mentions_the_fiche_when_it_exists(tmp_path, prompts, capsys, monkeypatch, home):
    (home / "notes" / "chantier-avec-fiche.md").write_text("fiche")
    assert nouveau(capsys, monkeypatch, prompts, "avec-fiche", "But.")[0] == 0
    assert "chantier-avec-fiche.md" in (prompts / "avec-fiche.md").read_text()
    assert nouveau(capsys, monkeypatch, prompts, "sans-fiche", "But.")[0] == 0
    assert "chantier-sans-fiche.md" not in (prompts / "sans-fiche.md").read_text().split("## 4.")[0]


@pytest.mark.parametrize("name", ["Majuscule", "a/b", "..", "_socle", "avec espace", "-tiret", "x" * 41, ".caché", "é"])
def test_nouveau_refuses_bad_names(tmp_path, prompts, capsys, monkeypatch, name):
    code, payload = nouveau(capsys, monkeypatch, prompts, name, "But.")
    assert code != 0 and payload["status"] == "error"
    assert sorted(p.name for p in prompts.iterdir()) == ["_socle.md", "demo.md"]


@pytest.mark.parametrize("goal,titre", [("", "T"), ("x" * 2001, "T"), ("But.", ""), ("But.", "t" * 121),
                                       ("But.\n## 4. Faux titre\n", "T")])
def test_nouveau_refuses_bad_text(tmp_path, prompts, capsys, monkeypatch, goal, titre):
    code, payload = nouveau(capsys, monkeypatch, prompts, "ok-nom", goal, titre)
    assert code != 0 and payload["status"] == "error" and not (prompts / "ok-nom.md").exists()


def test_nouveau_removes_the_file_when_it_does_not_assemble(tmp_path, capsys, monkeypatch):
    empty = tmp_path / "vide"  # no _socle.md: nothing can be assembled
    code, payload = nouveau(capsys, monkeypatch, empty, "orphelin", "But.")
    assert code != 0 and "introuvable" in payload["error"] and not (empty / "orphelin.md").exists()


def test_sujets_separate_incomplete_ones(tmp_path, prompts, capsys, home):  # todo #216
    (prompts / "sans-quatre.md").write_text("# S\nProjet : /tmp\n## 1. Lire\n")
    code, payload = run_cli(capsys, "sujets", "--dossier", str(prompts))
    assert code == 0 and "sans-quatre" not in payload["data"]["sujets"]
    assert "## 4." in payload["data"]["incomplets"]["sans-quatre"] and "demo" in payload["data"]["sujets"]
    assert not (tmp_path / "state").exists() or not list((tmp_path / "state").iterdir())
