"""Heartbeat: every 5 min, tell healthchecks.io the server is alive and its internal watcher keeps collecting.

Todo #460: hermes-gateway (disabled since 18/10) is no longer checked; it made every beat a /fail.
"""

from typing import Any, Callable

from ultron_hub.backup import hc
from ultron_hub.health import seuils
from ultron_hub.health.collect import last_sample_age_h


def pouls_problem() -> str | None:
    """Todo #159: the internal watcher (`health collect --record`, every 30 min) must keep running."""
    max_h = seuils.charger()["pouls"]["collecte_max_h"]
    age = last_sample_age_h()
    if age is None:
        return "veilleur interne : aucune collecte lisible (var/health/history.jsonl)"
    if age > max_h:
        return f"veilleur interne : dernière collecte il y a {age:.1f} h (seuil {max_h} h)"
    return None


def beat(urls: dict[str, str] | None = None, pinger: Callable[..., str | None] = hc.ping,
         pouls: Callable[[], str | None] | None = None) -> dict[str, Any]:
    urls = hc.load_urls() if urls is None else urls
    url = urls.get("HC_VIVANT_URL")
    if not url:
        return {"configured": False, "pinged": None, "ping_error": None}
    try:
        stale = (pouls or pouls_problem)()
    except Exception as exc:  # an unreadable pulse is itself a failure to report
        stale = f"veilleur interne illisible ({type(exc).__name__})"
    if stale:
        pinged, error = "fail", pinger(url, "fail", stale)
    else:
        pinged, error = "ok", pinger(url)
    return {"configured": True, "pinged": pinged, "ping_error": error, "pouls": stale}
