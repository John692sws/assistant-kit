import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ultron_hub.menage import commands, report

ROOT = Path(__file__).resolve().parents[1]

DISK = [{"mount": "/", "used_pct": 41, "free_gib": 30.2}]


def anomaly(n, path="/home/exemple/x", detail="d", action="a"):
    return {"check": n, "kind": "k", "path": path, "detail": detail, "action": action}


def scan_with(anomalies):
    counts = {}
    for a in anomalies:
        counts[str(a["check"])] = counts.get(str(a["check"]), 0) + 1
    return {"anomalies": anomalies, "counts": counts, "disk": DISK, "sizes": {"/home/exemple/w": 10}, "warnings": []}


def boom(_text):
    raise AssertionError("AI must not be called")


def test_no_anomaly_means_no_ai_call():
    built = report.build(scan_with([]), boom, "2026-10-18")
    assert built["ai_called"] is False
    assert "Tout est rangé" in built["text"]
    assert "20/20" in built["text"]


def test_anomalies_call_ai_once_with_bounded_input():
    seen = []

    def fake(text):
        seen.append(text)
        return {"summary": "Un peu de rangement.", "actions": [{"quoi": "Retirer", "ou": "~/logs", "pourquoi": "vide",
                                                                 "commande": "rmdir ~/logs"}]}

    many = [anomaly(8, path="/home/exemple/" + "x" * 100 + str(i)) for i in range(200)]
    built = report.build(scan_with(many), fake, "2026-10-18")
    assert len(seen) == 1
    assert len(seen[0].encode("utf-8")) <= report.AI_INPUT_MAX_BYTES
    assert json.loads(seen[0])["total"] == 200
    assert "👉 1. Retirer : ~/logs" in built["text"]
    assert "`rmdir ~/logs`" in built["markdown"]


def test_short_text_never_exceeds_twenty_lines():
    many = [anomaly(n) for n in range(1, 19) for _ in range(3)]
    summary = {"summary": "s", "actions": [{"quoi": f"q{i}", "ou": "o", "pourquoi": "p", "commande": ""}
                                           for i in range(6)]}
    text = report.short_text(scan_with(many), summary)
    assert len(text.splitlines()) <= report.MAX_LINES
    assert text.splitlines()[-1].startswith("Détail")


def test_ai_failure_falls_back_to_script_actions():
    def broken(_text):
        raise RuntimeError("quota")

    built = report.build(scan_with([anomaly(15, action="pip cache purge")]), broken, "2026-10-18")
    assert built["ai_error"].startswith("RuntimeError")
    assert "pip cache purge" in built["text"]


def test_save_state_writes_files_and_bounds_history(tmp_path):
    state = tmp_path / "menage"
    scan = scan_with([anomaly(3)])
    for i in range(report.HISTORY_MAX_LINES + 5):
        report.save_state(state, scan, "# md\n", now=float(i))
    assert json.loads((state / "last.json").read_text())["counts"] == {"3": 1}
    assert (state / "last.md").read_text() == "# md\n"
    lines = (state / "history.jsonl").read_text().splitlines()
    assert len(lines) == report.HISTORY_MAX_LINES
    assert json.loads(lines[-1]) == {"ts": float(report.HISTORY_MAX_LINES + 4), "sizes": {"/home/exemple/w": 10}}
    assert (state / "last.md").stat().st_mode & 0o777 == 0o600


# --- menage report / last ------------------------------------------------------------------------------------------

@pytest.fixture
def hub(tmp_path, monkeypatch):
    sent, journal = [], []
    monkeypatch.setattr(commands.service, "send_message", lambda text, target, binary: sent.append((text, target)))
    monkeypatch.setattr(commands.journal, "record", lambda s, src, kind, text, todo=None: journal.append(text))

    def no_claude(*_a, **_k):
        raise AssertionError("claude must not be called")

    monkeypatch.setattr(commands, "run_job", no_claude)
    settings = SimpleNamespace(var_dir=tmp_path / "var", todo_notify_target="telegram", hermes_binary=None)
    return SimpleNamespace(settings=settings, sent=sent, journal=journal)


def test_report_without_anomaly_never_calls_claude(hub):
    data = commands.do_report(hub.settings, send=True, scan=scan_with([]))
    assert data["ai_called"] is False and data["anomalies"] == 0
    assert hub.sent and "Tout est rangé" in hub.sent[0][0]
    assert hub.journal == ["Ménage du serveur : 0 écart(s), rapport envoyé"]
    last = commands.do_last(hub.settings)
    assert last["exists"] and "Tout est rangé" in last["text"]


def test_report_with_anomalies_uses_the_summary_task(hub, monkeypatch):
    calls = []

    def fake_job(request, settings):
        calls.append(request)
        return SimpleNamespace(ok=True, error=None, data={"summary": "s", "actions": [
            {"quoi": "Vider le cache npm", "ou": "~/.npm", "pourquoi": "1,5 Go", "commande": "npm cache clean --force"}]})

    monkeypatch.setattr(commands, "run_job", fake_job)
    data = commands.do_report(hub.settings, send=False, scan=scan_with([anomaly(15)]))
    assert [c.action for c in calls] == ["menage-resume"]
    assert "👉 1. Vider le cache npm : ~/.npm" in data["text"]
    assert hub.sent == []
    assert (hub.settings.var_dir / "menage" / "last.md").is_file()


def test_report_ignores_accepted_anomalies(hub):
    folder = hub.settings.var_dir / "menage"
    folder.mkdir(parents=True)
    (folder / "ignores.yaml").write_text("paths:\n  - /home/exemple/accepted\n")
    data = commands.do_report(hub.settings, send=False, scan=scan_with([anomaly(3, path="/home/exemple/accepted")]))
    assert data["anomalies"] == 0 and data["ai_called"] is False


def test_last_without_report(hub):
    assert commands.do_last(hub.settings) == {"text": "", "exists": False}


def test_report_and_last_are_cli_commands():
    from cli_contract import check_argv

    check_argv(["menage", "report", "--send"])
    check_argv(["menage", "last"])


def test_task_is_haiku_without_tools():
    task = yaml.safe_load((ROOT / "tasks" / "menage-resume" / "task.yaml").read_text())
    assert task["model"] == "haiku" and task["tools"] == [] and "collect" not in task


# --- audit every 60 days (todo #127) -------------------------------------------------------------------------------

def test_audit_line_survives_the_twenty_line_cut():
    many = [anomaly(n) for n in range(1, 19) for _ in range(3)]
    summary = {"summary": "s", "actions": [{"quoi": f"q{i}", "ou": "o", "pourquoi": "p", "commande": ""}
                                           for i in range(6)]}
    text = report.short_text(scan_with(many), summary, audit_due=True)
    assert len(text.splitlines()) <= report.MAX_LINES
    assert report.AUDIT_LINE in text and text.splitlines()[-1].startswith("Détail")
    assert report.AUDIT_LINE not in report.short_text(scan_with(many), summary, audit_due=False)


def test_report_proposes_the_audit_until_it_is_done(hub):
    import time

    from ultron_hub.menage import audit

    data = commands.do_report(hub.settings, send=False, scan=scan_with([]))
    assert data["audit_due"] is True and report.AUDIT_LINE in data["text"]
    audit.mark_done(hub.settings.var_dir / "menage", time.time())
    data = commands.do_report(hub.settings, send=False, scan=scan_with([]))
    assert data["audit_due"] is False and report.AUDIT_LINE not in data["text"]


def test_audit_is_a_cli_command():
    from cli_contract import check_argv

    check_argv(["menage", "audit"])


def test_report_has_a_line_on_the_last_restore_test(hub):
    from ultron_hub.backup import run as backup
    assert "Restauration" not in commands.do_report(hub.settings, send=False, scan=scan_with([]))["text"]
    layout = backup.default_layout(hub.settings.var_dir, None)
    backup.write_state(layout, {"last_restore_test": {"status": "ok", "at": "2026-10-01T06:00:00+00:00"}})
    text = commands.do_report(hub.settings, send=False, scan=scan_with([]))["text"]
    assert "Restauration testée le 2026-10-01" in text
    backup.write_state(layout, {"last_restore_test": {"status": "error", "at": "2026-11-01T06:00:00+00:00",
                                                       "error": "sha256"}})
    assert "en échec : sha256" in commands.do_report(hub.settings, send=False, scan=scan_with([]))["text"]
