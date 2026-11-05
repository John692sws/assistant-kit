"""Tests of `ultron-hub cap report --send` (todo #191): text shape, send path, journal line (the timer is ultron-hub-dimanche, todo #460)."""

import argparse
from types import SimpleNamespace

from ultron_hub.cap import commands, report


def rep(n=2, corr=2, **over):
    r = {"date": "2027-01-15", "status": "ok", "summary": "ok", "faits": "x", "cost_usd": 0.42, "semaine_pct": 40.0,
         "chantiers": [{"nom": f"c{i}", "verdict": ["vert", "orange", "rouge"][i % 3], "objectif": "but",
                        "technique": "t", "derives": "dérive " + "x" * 200, "preuves": ["p"]} for i in range(n)],
         "corrections": [{"titre": f"Corriger {i}", "chantier": "c0", "notes": "n", "preuve": "p"} for i in range(corr)]}
    r.update(over)
    return r


def test_text_shape():
    lines = report.short_text(rep()).split("\n")
    assert lines[1].startswith("🟢 c0 : but") and lines[2].startswith("🟠 c1 : dérive")
    assert all(len(x) < 140 for x in lines)
    assert "1. Corriger 0 (c0)" in lines and "2. Corriger 1 (c0)" in lines
    assert "0.42 $" in lines[-2] and "40 %" in lines[-2] and lines[-1] == "👉 /cap pour créer les todos"


def test_text_at_most_25_lines_with_many_chantiers():
    lines = report.short_text(rep(n=60, corr=3)).split("\n")
    assert len(lines) <= 25 and "autre(s) chantier(s)" in "\n".join(lines) and lines[-1].startswith("👉 /cap")


def test_no_correction_has_no_cap_line():
    text = report.short_text(rep(corr=0))
    assert "/cap" not in text and "Aucune correction" in text


def test_skipped_and_failed_text():
    for status in ("skipped", "failed"):
        text = report.short_text(rep(status=status, summary="revue sautée (quota) : semaine à 90 %", chantiers=[],
                                     corrections=[]))
        assert "revue sautée (quota)" in text and "/cap" not in text


def test_send_uses_service_and_logs(tmp_path, monkeypatch):
    sent, logged = [], []
    monkeypatch.setattr("ultron_hub.esprit.store.record", lambda s, src, kind, text, todo=None: logged.append((src, kind, text)))
    settings = SimpleNamespace(todo_notify_target="telegram", hermes_binary=None)
    out = report.send(settings, rep(), sender=lambda t, target, binary, **k: sent.append((t, target)))
    assert out["sent"] and sent[0][1] == "telegram" and sent[0][0] == out["text"]
    assert logged == [("ultron-hub", "cap", "Revue de cap 2027-01-15 : ok, 2 correction(s), envoyée")]


def test_send_failure_is_not_fatal(monkeypatch):
    monkeypatch.setattr("ultron_hub.esprit.store.record", lambda *a, **k: None)
    settings = SimpleNamespace(todo_notify_target="telegram", hermes_binary=None)

    def boom(*a, **k):
        raise RuntimeError("hermes absent")
    out = report.send(settings, rep(), sender=boom)
    assert out["sent"] is False and "hermes absent" in out["send_error"]


def test_command_send_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(commands.report, "run", lambda s: rep())
    monkeypatch.setattr(commands.report, "send", lambda s, r: calls.append(r) or {"sent": True})
    ns = argparse.Namespace(cap_command="report", send=True)
    assert commands.handle(ns, SimpleNamespace())["data"]["sent"] is True and calls
    calls.clear()
    ns.send = False
    assert "sent" not in commands.handle(ns, SimpleNamespace())["data"] and not calls
