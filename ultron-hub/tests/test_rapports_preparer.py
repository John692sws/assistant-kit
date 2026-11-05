"""Todo #460: ONE Sunday timer (`ultron-hub-dimanche.timer`) runs every weekly report, then sends ONE message to the Fil."""

import configparser
import json
from pathlib import Path

import pytest

from ultron_hub import cli, rapports
from ultron_hub.cap import commands as cap_commands
from ultron_hub.common.settings import load_settings
from ultron_hub.esprit import focus
from ultron_hub.menage import commands as menage_commands

REPO = Path(__file__).resolve().parents[1]
REMOVED = ("focus", "menage", "cap", "bilan", "todo-digest")


class Sender:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, text, target, binary, **kw):
        self.calls.append((text, kw))
        if self.error:
            raise RuntimeError(self.error)


@pytest.fixture
def settings(project):
    return load_settings(project)


@pytest.fixture
def steps(monkeypatch):
    """The real wiring of the four steps, each command replaced by a fake (records how it was called)."""
    calls = []

    def menage(args, s):
        calls.append(("menage", args.menage_command, args.send))
        return {"status": "ok", "summary": "3 anomalie(s)", "data": {}, "error": None}

    def cap(args, s):
        calls.append(("cap", args.cap_command, args.send))
        return {"status": "ok", "data": {"status": "ok", "summary": "s", "corrections": [{}, {}]}}

    def bilan(args, s):
        calls.append(("bilan", args.bilan_command, args.send))
        return {"status": "ok", "summary": "12 todo(s) faites, 4 boutons", "data": {}}

    monkeypatch.setattr(menage_commands, "handle", menage)
    monkeypatch.setattr(cap_commands, "handle", cap)
    monkeypatch.setattr(cap_commands, "handle_bilan", bilan)
    monkeypatch.setattr(focus, "read", lambda s: {"domaines": ["systeme", "business"]})
    return calls


def test_every_report_runs_without_its_own_send_then_one_message(settings, steps):
    sender = Sender()
    data = rapports.preparer(settings, sender=sender)
    assert steps == [("menage", "report", False), ("cap", "report", False), ("bilan", "semaine", False)]
    assert len(sender.calls) == 1
    text, kw = sender.calls[0]
    assert text.splitlines() == [
        "📋 Rapports du dimanche prêts, onglet Moi",
        "🧹 Ménage : 3 anomalie(s)",
        "🔭 Revue de cap : 2 correction(s) proposée(s)",
        "📊 Bilan de la semaine : 12 todo(s) faites, 4 boutons",
        "🧭 Focus : actuel : systeme + business. Sans réponse il reste ; le changer : Moi > 🧭 Focus. "
        "Quel focus cette semaine ?"]
    assert kw["genre"] == "question"  # the focus stays a question on the dashboard
    saved = json.loads((settings.var_dir / "rapports" / "preparation.json").read_text())
    assert saved["sent"] is True and [r["rapport"] for r in saved["rapports"]] == ["menage", "cap", "bilan", "focus"]


def test_a_failing_report_does_not_stop_the_others(settings, steps, monkeypatch):
    def boom(args, s):
        raise RuntimeError("disque illisible")
    monkeypatch.setattr(menage_commands, "handle", boom)
    monkeypatch.setattr(cap_commands, "handle", lambda a, s: {"data": {"status": "failed", "summary": "revue impossible : quota"}})
    sender = Sender()
    data = rapports.preparer(settings, sender=sender)
    lines = sender.calls[0][0].splitlines()
    assert lines[1] == "🧹 Ménage : ❌ échec : RuntimeError: disque illisible"
    assert lines[2] == "🔭 Revue de cap : ❌ échec : revue impossible : quota"
    assert lines[3].startswith("📊 Bilan de la semaine : 12 todo") and lines[4].startswith("🧭 Focus : ")
    assert [r["ok"] for r in data["rapports"]] == [False, False, True, True]


def test_a_skipped_cap_review_is_not_a_failure(settings, steps, monkeypatch):
    monkeypatch.setattr(cap_commands, "handle",
                        lambda a, s: {"data": {"status": "skipped", "summary": "revue sautée (quota) : semaine à 95 %"}})
    data = rapports.preparer(settings, sender=Sender())
    assert data["rapports"][1] == {"rapport": "cap", "ok": True, "detail": "revue sautée (quota) : semaine à 95 %"}


def test_cli_fails_when_a_report_or_the_send_failed(project, steps, monkeypatch, capsys):
    monkeypatch.setattr(rapports.service, "send_message", Sender(error="fil hors ligne"))
    assert cli.main(["rapports", "preparer"]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "error" and "envoi : RuntimeError: fil hors ligne" in out["error"]
    assert out["summary"] == "4/4 rapport(s) prêt(s), envoi échoué"
    monkeypatch.setattr(rapports.service, "send_message", Sender())
    assert cli.main(["rapports", "preparer"]) == 0


def test_focus_without_choice_is_still_asked(settings, steps, monkeypatch):
    monkeypatch.setattr(focus, "read", lambda s: None)
    sender = Sender()
    rapports.preparer(settings, sender=sender)
    assert sender.calls[0][0].splitlines()[-1].startswith("🧭 Focus : actuel : aucun.")


def test_one_sunday_timer_replaces_the_five_old_ones():
    timer = configparser.ConfigParser()
    timer.optionxform = str
    timer.read(REPO / "deploy/systemd/ultron-hub-dimanche.timer")
    assert timer["Timer"]["OnCalendar"] == "Sun *-*-* 10:00:00" and timer["Timer"]["Persistent"] == "true"
    service = (REPO / "deploy/systemd/ultron-hub-dimanche.service").read_text()
    execs = [line for line in service.splitlines() if line.startswith("ExecStart")]
    assert execs == ["ExecStart={{HOME}}/workspace/projects/active/ultron-hub/.venv/bin/ultron-hub rapports preparer"]
    for name in REMOVED:
        for kind in ("timer", "service"):
            assert not (REPO / f"deploy/systemd/ultron-hub-{name}.{kind}").exists()
    heartbeat = (REPO / "deploy/systemd/ultron-hub-heartbeat.service").read_text()
    assert "hermes-gateway" not in heartbeat
