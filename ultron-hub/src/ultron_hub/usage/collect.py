"""Claude plan usage (5 h session and week), read from the OAuth endpoint behind Claude Code's /usage.

Plain Python, never a Claude session: the access token in ~/.claude/.credentials.json is read
here only, never printed, logged, cached or refreshed. The endpoint is undocumented and may change.
Origin: mission of todo #10 (2026-10-10), checked against the real endpoint on 2026-10-11.
"""

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ultron_hub.executors.claude_cli import resolve_claude_binary

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
FALLBACK_VERSION = "2.1.296"
HTTP_TIMEOUT_S = 10
CACHE_TTL_S = 60  # a few taps in a row must not trigger 429s
CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


class UsageError(Exception):
    """A failure whose message is safe to show (never contains the token)."""


class TokenExpired(UsageError):
    """The access token's expiry date is past: expected after a few hours, the next Claude Code session renews it."""


def parse_time(value) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
        if isinstance(value, str) and value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def claude_version() -> str:
    """Version of the installed claude (~/.local/bin/claude -> .../versions/<x.y.z>), for the User-Agent."""
    name = Path(resolve_claude_binary()).resolve().name
    return name if re.fullmatch(r"\d+\.\d+\.\d+", name) else FALLBACK_VERSION


def read_token(path: Path, now: datetime) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError("identifiants Claude introuvables (Claude Code n'est pas connecté ?)")
    except (OSError, ValueError):
        raise UsageError("identifiants Claude illisibles")
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise UsageError("pas de jeton OAuth dans les identifiants Claude")
    expires = parse_time(oauth.get("expiresAt"))
    if expires is not None and expires <= now:
        raise TokenExpired("jeton Claude expiré : il sera renouvelé à la prochaine session Claude Code")
    return token


def fetch(token: str) -> dict:
    request = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": BETA_HEADER,
        "Content-Type": "application/json", "User-Agent": f"claude-code/{claude_version()}",
    })
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        messages = {401: "jeton refusé (401) : il sera renouvelé à la prochaine session Claude Code",
                    403: "accès refusé (403) : jeton sans la portée user:profile",
                    429: "trop de requêtes (429) : réessaie dans quelques minutes"}
        raise UsageError(messages.get(exc.code, f"erreur HTTP {exc.code} de l'endpoint d'usage"))
    except (urllib.error.URLError, OSError) as exc:
        raise UsageError(f"réseau indisponible : {getattr(exc, 'reason', exc)}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise UsageError("réponse de l'endpoint d'usage illisible")
    if not isinstance(data, dict):
        raise UsageError("réponse de l'endpoint d'usage inattendue")
    return data


def _window(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    percent = entry.get("utilization", entry.get("percent"))
    if isinstance(percent, bool) or not isinstance(percent, (int, float)):
        return None
    resets = parse_time(entry.get("resets_at"))
    return {"percent": round(float(percent), 1), "resets_at": resets.isoformat() if resets else None}


def parse(data: dict) -> dict:
    """{"session": window|None, "week": window|None}; `limits` list as fallback (seen on 2026-10-11)."""
    session, week = _window(data.get("five_hour")), _window(data.get("seven_day"))
    for item in data.get("limits") or []:
        if isinstance(item, dict) and item.get("kind") == "session" and session is None:
            session = _window(item)
        elif isinstance(item, dict) and item.get("kind") == "weekly_all" and week is None:
            week = _window(item)
    if session is None and week is None:
        raise UsageError("réponse sans limite exploitable (format de l'endpoint changé ?)")
    return {"session": session, "week": week}


def _read_cache(path: Path, now: datetime, stale_ok: bool = False) -> dict | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        fetched = parse_time(cached["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if fetched is None or (not stale_ok and (now - fetched).total_seconds() > CACHE_TTL_S):
        return None
    return cached


def _write_cache(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".usage-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    os.replace(tmp, path)


def usage(cache: Path, now: datetime | None = None, credentials: Path = CREDENTIALS,
          fetcher=fetch) -> dict:
    """Session and week usage, cached CACHE_TTL_S seconds; stale cache served on 429."""
    now = now or datetime.now(timezone.utc)
    cached = _read_cache(cache, now)
    if cached:
        return {**cached, "from_cache": True}
    token = read_token(credentials, now)
    try:
        result = {**parse(fetcher(token)), "fetched_at": now.isoformat(), "stale": False}
    except UsageError as exc:
        stale = _read_cache(cache, now, stale_ok=True)
        if stale and "429" in str(exc):
            return {**stale, "from_cache": True, "stale": True}
        raise UsageError(str(exc).replace(token, "***")) from None
    _write_cache(cache, result)
    return {**result, "from_cache": False}


SQUAD_ENV = "ULTRON_ESCOUADE_DIR"
QUOTA_STALE_S = 180  # a watchdog quota.json older than 3 min is flagged stale


def squad_quota(folder: Path, now: datetime) -> dict | None:
    """Inside a squad session the credentials are unreadable (sandbox denyRead): the watchdog, which runs outside,
    writes `quota.json` in the squad folder; `usage` serves it. None when absent or unreadable."""
    try:
        quota = json.loads((Path(folder) / "quota.json").read_text(encoding="utf-8"))
        written = parse_time(quota.get("ts") or quota.get("ecrit_a"))
        session, week = quota.get("session"), quota.get("week")
    except (OSError, ValueError, AttributeError):
        return None
    if written is None or (session is None and week is None):
        return None
    age = (now - written).total_seconds()
    return {"session": session, "week": week, "fetched_at": quota.get("fetched_at") or written.isoformat(),
            "from_cache": True, "stale": bool(quota.get("stale")) or age > QUOTA_STALE_S,
            "age_s": round(age), "source": "chien de garde de l'escouade",
            "escouade": {k: quota.get(k) for k in ("points_escouade", "hors_escouade", "ratio", "ratio_source",
                                                    "delta_semaine")}}
