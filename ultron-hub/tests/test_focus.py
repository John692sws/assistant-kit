"""Focus of the week (todo #165): file, CLI, Claude Code and Hermes context, Telegram screen, Sunday timer."""

import asyncio
import json
from pathlib import Path

import pytest


from cli_contract import check_argv
from tests.test_esprit import mind, run  # noqa: F401 (mind = fixture)
from ultron_hub import cli
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.esprit import focus, store

REPO = Path(__file__).resolve().parents[1]


def test_write_read_and_journal(mind):
    assert focus.read(mind) is None
    data = focus.write(mind, ["systeme", "business", "business"], "cli")
    assert data["domaines"] == ["systeme", "business"] and data["par"] == "cli"
    assert focus.read(mind)["domaines"] == ["systeme", "business"]
    assert (store.root(mind) / focus.FILE).stat().st_mode & 0o777 == 0o600
    assert "Focus de la semaine : systeme, business" in store.tail(mind, 1)[-1]["texte"]
    assert focus.line(focus.read(mind)).startswith("🧭 Focus de la semaine : systeme + business (depuis le ")


@pytest.mark.parametrize("names", [[], ["systeme", "business", "perso"], ["fba"], ["../x"], ["Systeme"]])
def test_bad_focus_is_refused(mind, names):
    with pytest.raises(InvalidRequestError):
        focus.write(mind, names, "cli")
    assert focus.read(mind) is None


def test_old_format_is_read_through_the_domains(mind, monkeypatch):
    path = store.root(mind) / focus.FILE
    path.write_text('{"chantiers": ["reflexion", "fba", "inconnu"], "depuis": "2026-10-18T09:00:00", "par": "cli"}')
    monkeypatch.setattr(focus, "_domaine_de_chantier",
                        lambda settings, n: {"reflexion": "systeme", "fba": "business"}.get(n, ""))
    assert focus.read(mind) == {"domaines": ["systeme", "business"], "depuis": "2026-10-18T09:00:00", "par": "cli"}
    assert "chantiers" in path.read_text()  # the file is not rewritten
    focus.write(mind, ["perso"], "cli")
    assert json.loads(path.read_text())["domaines"] == ["perso"] and "chantiers" not in path.read_text()


def test_old_format_reads_the_prompt_header(mind, tmp_path, monkeypatch):
    from ultron_hub.chantier import core
    (tmp_path / "fba.md").write_text("# Reprise « FBA »\n\nDomaine : business\n")
    monkeypatch.setattr(core, "sources", lambda *a, **k: core.Sources(tmp_path, tmp_path, tmp_path))
    assert focus._domaine_de_chantier(mind, "fba") == "business"
    assert focus._domaine_de_chantier(mind, "absent") == ""


def test_chantier_is_in_focus_by_its_domain():
    data = {"domaines": ["systeme"]}
    assert focus.dans_le_focus(data, "systeme") and not focus.dans_le_focus(data, "perso")
    assert not focus.dans_le_focus(data, "") and not focus.dans_le_focus(None, "systeme")


def test_unreadable_file_is_no_focus(mind):
    path = store.root(mind) / focus.FILE
    for text in ("pas du json", '{"domaines": ["../x", 3]}', "[]"):
        path.write_text(text)
        assert focus.read(mind) is None
    assert "aucun" in focus.line(None)


def test_cli(mind, capsys):
    code, out = run(capsys, "esprit", "focus")
    assert code == 0 and out["data"]["focus"] is None
    code, out = run(capsys, "esprit", "focus", "--set", "systeme", "business", "--by", "telegram-button")
    assert code == 0 and out["summary"] == "focus : systeme + business"
    code, out = run(capsys, "esprit", "focus", "--ask")
    assert "Focus actuel : systeme + business." in out["data"]["text"] and out["data"]["sent"] is False
    code, out = run(capsys, "esprit", "focus", "--send")
    assert code == 1 and "--send" in out["error"]
    code, out = run(capsys, "esprit", "focus", "--set", "systeme", "business", "perso")
    assert code == 1 and "1 ou 2" in out["error"]


@pytest.mark.real_send
def test_cli_ask_send(mind, capsys, monkeypatch):
    from ultron_hub.todo import service
    sent = []
    monkeypatch.setattr(service, "send_message", lambda text, target, binary: sent.append((text, target)))
    code, out = run(capsys, "esprit", "focus", "--ask", "--send")
    assert code == 0 and out["data"]["sent"] is True and "/plus puis 🧭 Focus" in sent[0][0]


def test_claude_hook_shows_the_focus(mind, capsys):
    focus.write(mind, ["business"], "cli")
    assert cli.main(["esprit", "context", "--claude-hook"]) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "🧭 Focus de la semaine : business" in context
    assert cli.main(["esprit", "context"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["focus"]["domaines"] == ["business"]








def labels(screen):
    return [label for row in screen.buttons for label, _ in row]






