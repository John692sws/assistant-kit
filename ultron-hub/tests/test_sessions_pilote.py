"""Todo #283: the Pilote of a session (fake tmux, fake journals; no real tmux or claude).

The raw screen (`session ecran`), the menu read by a script, the context in use, and what `session lire` adds for the bar.
"""

import json
import subprocess

import pytest

from ultron_hub.dashboard import apercu, catalogue
from ultron_hub.sessions import commands, ecran, journal
from ultron_hub.sessions.journal import SessionError

NOM = "ultron-19-10-15h38"
PERMISSION = ["● Bash(git merge)", "", "──────", " Bash command", "   git merge --no-ff todo-901/x", " Do you want to proceed?",
              " ❯ 1. Yes", "   2. Yes, and don't ask again for git commands", "   3. No, and tell Claude what to do differently (esc)",
              "", " Esc to cancel · Tab to amend"]


class FakeTmux:
    def __init__(self, screen, size="120 30", rc=0):
        self.calls, self.screen, self.size, self.rc = [], screen, size, rc

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and "shell" not in kw
        self.calls.append(argv)
        out = self.size + "\n" if argv[1] == "display-message" else "\n".join(self.screen) + "\n"
        return subprocess.CompletedProcess(argv, self.rc, out, "")


def test_permission_prompt_is_a_menu():
    assert ecran.menu(PERMISSION) == [{"touche": "1", "libelle": "Yes"},
                                      {"touche": "2", "libelle": "Yes, and don't ask again for git commands"},
                                      {"touche": "3", "libelle": "No, and tell Claude what to do differently (esc)"}]


def test_boxed_menu_and_question_with_descriptions():
    boxed = ["╭────╮", "│ Do you want to proceed? │", "│ ❯ 1. Yes │", "│   2. No  │", "╰────╯"]
    assert [o["libelle"] for o in ecran.menu(boxed)] == ["Yes", "No"]
    ask = ["❯ 1. Option A", "     la première", "  2. Option B", "     la seconde", "  3. Type something.", "Enter to select"]
    assert [o["touche"] for o in ecran.menu(ask)] == ["1", "2", "3"]


@pytest.mark.parametrize("lines", [
    ["● Voici le plan :", "1. lire", "2. écrire", "────", "❯ ", "────"],  # a list in a reply, prompt below: text
    ["❯ 1. seul"],  # one option
    ["1. a", "2. b"],  # no cursor
    ["❯ 1. a", "3. c"],  # not consecutive
    PERMISSION + ["────", "❯ Oui", "────"],  # answered: the prompt came back below
    [],
])
def test_no_menu(lines):
    assert ecran.menu(lines) == []


def test_capture_argv_size_visible_lines_and_menu():
    history = [f"vieille ligne {i}" for i in range(40)]
    tmux = FakeTmux(history + PERMISSION + ["", "", ""], size=f"120 {len(PERMISSION) + 3}")
    data = ecran.lire(NOM, run=tmux)
    assert tmux.calls == [["tmux", "display-message", "-p", "-t", f"={NOM}:", "#{pane_width} #{pane_height}"],
                          ["tmux", "capture-pane", "-p", "-t", f"={NOM}:", "-S", f"-{ecran.HISTORY}"]]
    assert data["lignes"] == history + PERMISSION and (data["largeur"], data["hauteur"]) == (120, len(PERMISSION) + 3)
    assert [o["touche"] for o in data["menu"]] == ["1", "2", "3"]


def test_a_menu_scrolled_out_of_the_screen_is_not_offered():
    tmux = FakeTmux(PERMISSION + ["● Fait.", "────", "❯ ", "────"], size="120 4")
    assert ecran.lire(NOM, run=tmux)["menu"] == []


def test_control_characters_and_long_lines_are_cut():
    data = ecran.lire(NOM, run=FakeTmux(["a\x1b[31mb\x07", "x" * 1000]))
    assert data["lignes"] == ["a[31mb", "x" * ecran.LINE_MAX]


@pytest.mark.parametrize("nom", ["autre", "ultron-x", "-t", f"{NOM}; rm", f"={NOM}"])
def test_bad_name_never_reaches_tmux(nom):
    tmux = FakeTmux([])
    with pytest.raises(SessionError, match="nom"):
        ecran.lire(nom, run=tmux)
    assert tmux.calls == []


def test_absent_session():
    with pytest.raises(SessionError, match="absente"):
        ecran.lire(NOM, run=FakeTmux([], rc=1))
    assert ecran.menu_of(NOM, run=FakeTmux([], rc=1)) == []


def _journal(tmp_path, *usages):
    f = tmp_path / "0a1b2c3d-0000-4000-8000-0123456789ab.jsonl"
    f.write_text("".join(json.dumps({"type": "assistant", "timestamp": "2026-10-19T14:00:00Z",
                                     "message": {"content": [{"type": "text", "text": "ok"}], "usage": u}}) + "\n"
                         for u in usages))
    return f


def test_context_is_the_last_reply_input_over_the_window(tmp_path):
    f = _journal(tmp_path, {"input_tokens": 10}, {"input_tokens": 4, "cache_read_input_tokens": 100_000,
                                                  "cache_creation_input_tokens": 20_000, "output_tokens": 999})
    assert journal.read_entries(f)["contexte"] == 60
    assert journal.read_entries(_journal(tmp_path, {"input_tokens": 300_000}))["contexte"] is None  # window unknown: omitted
    assert journal.read_entries(_journal(tmp_path, None))["contexte"] is None


def test_cli_ecran_and_lire_add_the_pilote_fields(monkeypatch, capsys, tmp_path):
    f = _journal(tmp_path, {"input_tokens": 50_000})
    monkeypatch.setattr(journal, "find_journal", lambda name, *a, **k: f)
    monkeypatch.setattr(journal, "list_sessions", lambda *a, **k: [{"nom": NOM, "cree": 1_792_420_000, "ouverte": True,
                                                                     "chantier": None}])
    monkeypatch.setattr(subprocess, "run", FakeTmux(PERMISSION))
    from ultron_hub.cli import main
    assert main(["session", "lire", NOM, "--n", "5"]) == 0
    data = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]
    assert (data["cree"], data["chantier"], data["contexte"], data["attend"]) == (1_792_420_000, None, 25, False)
    assert [o["touche"] for o in data["menu"]] == ["1", "2", "3"] and str(f) not in json.dumps(data)
    assert main(["session", "ecran", NOM]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "ok" and out["data"]["lignes"] == PERMISSION and out["data"]["attend"] is False
    assert main(["session", "ecran", "autre"]) != 0


def test_screen_reading_in_the_catalogue_and_its_whitelist():
    a = catalogue.PAR_ID["session-ecran"]
    assert a.type == "lecture" and a.argv == ("session", "ecran", "{nom}")
    out = catalogue.projeter(a.donnees, apercu.LECTURES["session-ecran"])
    assert "pid" not in out and out["menu"][0] == {"touche": "1", "libelle": "Yes"} and out["lignes"]
    lue = catalogue.projeter(catalogue.PAR_ID["session"].donnees, apercu.LECTURES["session"])
    assert {"cree", "chantier", "contexte", "attend", "travaille", "menu"} <= set(lue)
    with pytest.raises(catalogue.Refused):
        catalogue.valider("session-ecran", {"action": "session-ecran", "nom": "../../x"})


def test_commands_module_reads_without_writing():
    src = open(ecran.__file__, encoding="utf-8").read()
    assert "send-keys" not in src and "shell=True" not in src and commands.ecran is ecran
