"""Account alerts (todo #329): each alert fired / not fired, anti-spam per half-day, no switch advice without room."""

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ultron_hub.compte import alertes, budget

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def row(nom, session, week, *, stale=False, registered=True):
    quota = None if session is None else {"session": {"percent": session}, "week": {"percent": week}, "stale": stale}
    return {"nom": nom, "enregistre": registered, "actif": False, "adresse": None, "quota": quota}


def state(active, a, b):
    return {"actif": active, "comptes": [a, b]}


class Sender:
    def __init__(self, status="ok"):
        self.sent, self.status = [], status

    def __call__(self, text, **kw):
        self.sent.append((text, kw))
        return SimpleNamespace(status=self.status)


def write_history(var, rows):
    path = budget.history_path(var)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def sample(slot, at, percent):
    return {"at": at.isoformat(), "compte": slot, "session": None,
            "week": {"percent": percent, "resets_at": (NOW + timedelta(days=2)).isoformat()}}


def test_switch_alert_when_other_has_room(tmp_path, monkeypatch):
    monkeypatch.setattr(alertes, "SESSION_LIMIT", 90.0)  # explicit user limit
    send = Sender()
    done = alertes.verifier(tmp_path, state("a", row("a", 92, 40), row("b", 10, 30)), NOW, send)
    assert done == ["bascule"] and "Bascule vers" in send.sent[0][0] and send.sent[0][1]["sujet"] == "alertes"


def test_no_switch_alert_below_threshold_or_without_room(tmp_path, monkeypatch):
    monkeypatch.setattr(alertes, "SESSION_LIMIT", 90.0)
    monkeypatch.setattr(alertes, "WEEK_LIMIT", 88.0)
    send = Sender()
    assert alertes.verifier(tmp_path, state("a", row("a", 89, 40), row("b", 10, 30)), NOW, send) == []
    assert alertes.verifier(tmp_path, state("a", row("a", 95, 40), row("b", 95, 30)), NOW, send) == []  # other session full
    assert alertes.verifier(tmp_path, state("a", row("a", 95, 40), row("b", 10, 90)), NOW, send) == []  # other week full
    assert alertes.verifier(tmp_path, state("a", row("a", 95, 40), row("b", None, 0, registered=False)), NOW, send) == []
    assert alertes.verifier(tmp_path, state("a", row("a", 95, 40, stale=True), row("b", 10, 30)), NOW, send) == []
    assert send.sent == []


def test_week_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(alertes, "WEEK_LIMIT", 88.0)  # explicit user limit
    send = Sender()
    assert alertes.verifier(tmp_path, state("a", row("a", 10, 88), row("b", 10, 30)), NOW, send) == ["semaine"]
    assert alertes.verifier(tmp_path, state("a", row("a", 10, 87), row("b", 10, 30)), NOW, Sender()) == []


def test_budget_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")  # local midnight = UTC midnight
    time.tzset()
    midnight = NOW.replace(hour=0)
    write_history(tmp_path, [sample("a", midnight - timedelta(hours=1), 50.0), sample("a", NOW, 80.0)])
    send = Sender()
    done = alertes.verifier(tmp_path, state("a", row("a", 10, 80), row("b", 10, 30)), NOW, send)
    assert done == ["budget"] and "Budget du jour dépassé" in send.sent[0][0]
    write_history(tmp_path, [sample("a", midnight - timedelta(hours=1), 50.0), sample("a", NOW, 52.0)])
    assert alertes.verifier(tmp_path / "x", state("a", row("a", 10, 52), row("b", 10, 30)), NOW, Sender()) == []


def test_anti_spam_per_half_day_and_retry_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(alertes, "WEEK_LIMIT", 88.0)
    st = state("a", row("a", 10, 90), row("b", 10, 30))
    failing = Sender("error")
    assert alertes.verifier(tmp_path, st, NOW, failing) == []  # not remembered: retried
    send = Sender()
    assert alertes.verifier(tmp_path, st, NOW, send) == ["semaine"]
    assert alertes.verifier(tmp_path, st, NOW + timedelta(minutes=15), send) == []
    assert len(send.sent) == 1
    assert alertes.verifier(tmp_path, st, NOW + timedelta(hours=13), send) == ["semaine"]  # next half-day
    assert json.loads(alertes.state_path(tmp_path).read_text())["semaine"] == alertes.half_day(NOW + timedelta(hours=13))


def test_no_default_limit_no_session_or_week_alert(tmp_path):
    send = Sender()
    assert alertes.verifier(tmp_path, state("a", row("a", 99, 99), row("b", 10, 30)), NOW, send) == []
    assert send.sent == []
