import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from hermes_plugin.usage_flow import PREFIX, UsageFlow
from ultron_hub.usage import collect, text
from ultron_hub.usage.collect import UsageError

NOW = datetime(2026, 10, 11, 9, 20, tzinfo=timezone.utc)
# Shape of the real response (2026-10-11), trimmed.
RESPONSE = {
    "five_hour": {"utilization": 7.0, "resets_at": "2026-10-11T13:50:00.013940+00:00"},
    "seven_day": {"utilization": 1.0, "resets_at": "2026-10-18T00:59:59.990000+00:00"},
    "seven_day_opus": None,
    "limits": [{"kind": "session", "percent": 7, "resets_at": "2026-10-11T13:50:00+00:00"}],
}
TOKEN = "sk-ant-oat01-SECRET"


@pytest.fixture
def creds(tmp_path):
    path = tmp_path / "credentials.json"
    expires = int((NOW + timedelta(hours=1)).timestamp() * 1000)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": expires}}))
    return path


def run(tmp_path, creds, fetcher, now=NOW):
    return collect.usage(tmp_path / "cache" / "usage.json", now, credentials=creds, fetcher=fetcher)


def test_parse_real_shape_and_limits_fallback():
    assert collect.parse(RESPONSE)["session"] == {"percent": 7.0, "resets_at": "2026-10-11T13:50:00.013940+00:00"}
    only_limits = {"five_hour": None, "limits": [{"kind": "weekly_all", "percent": 40, "resets_at": None}]}
    assert collect.parse(only_limits) == {"session": None, "week": {"percent": 40.0, "resets_at": None}}
    with pytest.raises(UsageError):
        collect.parse({"five_hour": None})


def test_usage_fetches_then_caches(tmp_path, creds):
    calls = []
    def fetcher(token):
        calls.append(token)
        return RESPONSE
    first = run(tmp_path, creds, fetcher)
    second = run(tmp_path, creds, fetcher, NOW + timedelta(seconds=30))
    assert calls == [TOKEN] and not first["from_cache"] and second["from_cache"]
    assert TOKEN not in (tmp_path / "cache" / "usage.json").read_text()
    run(tmp_path, creds, fetcher, NOW + timedelta(seconds=collect.CACHE_TTL_S + 1))
    assert len(calls) == 2


def test_stale_cache_served_on_429_only(tmp_path, creds):
    run(tmp_path, creds, lambda t: RESPONSE)
    later = NOW + timedelta(minutes=10)
    def too_many(token):
        raise UsageError("trop de requêtes (429) : réessaie")
    out = run(tmp_path, creds, too_many, later)
    assert out["stale"] and out["session"]["percent"] == 7.0
    def down(token):
        raise UsageError(f"réseau indisponible {token}")
    with pytest.raises(UsageError) as exc:
        run(tmp_path, creds, down, later)
    assert TOKEN not in str(exc.value)


def test_token_errors(tmp_path):
    with pytest.raises(UsageError, match="introuvables"):
        collect.read_token(tmp_path / "missing.json", NOW)
    expired = tmp_path / "c.json"
    expired.write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": 1}}))
    with pytest.raises(UsageError, match="expiré") as exc:
        collect.read_token(expired, NOW)
    assert TOKEN not in str(exc.value)


def test_text_is_short_and_in_paris_time():
    out = text.usage_text(collect.parse(RESPONSE), NOW)
    assert "Session 5 h : 7 %" in out and "à 15h50" in out  # 13:50 UTC = 15:50 Paris (CEST)
    assert "Semaine : 1 %" in out and "dim. 18/10 à 03h00" in out  # 00:59:59.99 shown as 01:00 UTC
    assert "{" not in out and len(out.splitlines()) == 6
    assert text.bar(0) == "░" * 10 and text.bar(100) == "▓" * 10 and text.bar(250) == "▓" * 10
    assert "🔴" in text.usage_text({"session": {"percent": 92, "resets_at": None}, "week": None}, NOW)
    assert "non disponible" in text.usage_text({"session": None, "week": None}, NOW)


class FakeClient:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def call(self, *args, timeout_s=None):
        self.calls.append(args)
        return self.result


def test_flow_shows_text_and_errors():
    ok = FakeClient({"status": "ok", "data": {"text": "📊 Usage Claude (abonnement)"}})
    screen = asyncio.run(UsageFlow(ok).on_command(1))
    assert screen.text.startswith("📊") and ok.calls == [("usage",)]
    assert screen.buttons[0][0][1] == f"{PREFIX}refresh"
    assert asyncio.run(UsageFlow(ok).on_callback(1, f"{PREFIX}refresh")).text.startswith("📊")
    bad = FakeClient({"status": "error", "error": "réseau indisponible", "data": None})
    assert "❌ réseau indisponible" in asyncio.run(UsageFlow(bad).on_command(1)).text
