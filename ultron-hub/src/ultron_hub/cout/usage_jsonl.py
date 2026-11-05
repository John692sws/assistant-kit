"""Tokens and cost read from Claude Code `.jsonl` streams/transcripts (shared by `cout session` and `cout compare`)."""

import json
import os
from pathlib import Path
from typing import Any

STATE_ENV = "ULTRON_ESCOUADES_DIR"
DEFAULT_STATE = Path("~/.local/state/ultron-hub/escouades")  # where the (retired) autonomous squads left their sessions

TOKEN_KEYS = {"entree": "input_tokens", "sortie": "output_tokens", "cache_ecriture": "cache_creation_input_tokens",
              "cache_lecture": "cache_read_input_tokens"}
# Equivalent tokens: input 1, output 5, cache write 1.25, cache read 0.1, times a model factor.
FACTEUR_MODELE = (("opus", 5.0), ("haiku", 0.3), ("sonnet", 1.0))


def state_root(override: str | None = None) -> Path:
    return Path(override or os.environ.get(STATE_ENV) or DEFAULT_STATE).expanduser()


def usage_of(usage: Any) -> dict[str, int]:
    usage = usage if isinstance(usage, dict) else {}
    return {k: int(usage.get(v) or 0) for k, v in TOKEN_KEYS.items()}


def poids(usage: Any, model: str | None) -> float:
    usage = usage if isinstance(usage, dict) else {}
    num = lambda k: float(usage.get(k) or 0)  # noqa: E731
    units = (num("input_tokens") + 5 * num("output_tokens") + 1.25 * num("cache_creation_input_tokens")
             + 0.1 * num("cache_read_input_tokens"))
    name = (model or "").lower()
    factor = next((f for key, f in FACTEUR_MODELE if key in name), 1.0)
    return units * factor


def session_consumption(stream: Path) -> dict[str, Any]:
    """Tokens and cost of one session stream. The final `result` event is the session total; without it
    (session cut short) the assistant messages are summed, one count per message id."""
    per_message: dict[str, dict[str, int]] = {}
    result: dict[str, Any] | None = None
    try:
        lines = stream.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            result = event
        elif event.get("type") == "assistant" and isinstance(event.get("message"), dict):
            message = event["message"]
            per_message[message.get("id") or f"ligne-{len(per_message)}"] = usage_of(message.get("usage"))
    if result is not None:
        tokens = usage_of(result.get("usage"))
        return {**tokens, "cout_usd": float(result.get("total_cost_usd") or 0), "complete": True}
    tokens = {k: sum(u[k] for u in per_message.values()) for k in TOKEN_KEYS}
    return {**tokens, "cout_usd": 0.0, "complete": False}  # no cost without the result event


def consommation(squad: Path) -> dict[str, Any]:
    """Cumulated tokens (entree, sortie, cache_ecriture, cache_lecture) and estimated cost of every
    `session-*.jsonl` of a squad. The percentage of the quota is computed elsewhere (todo #97)."""
    total: dict[str, Any] = {k: 0 for k in TOKEN_KEYS}
    total.update(cout_usd=0.0, sessions=0, incompletes=0)
    for stream in sorted(Path(squad).glob("session-*.jsonl")):
        one = session_consumption(stream)
        for k in TOKEN_KEYS:
            total[k] += one[k]
        total["cout_usd"] = round(total["cout_usd"] + one["cout_usd"], 6)
        total["sessions"] += 1
        total["incompletes"] += 0 if one["complete"] else 1
    return total
