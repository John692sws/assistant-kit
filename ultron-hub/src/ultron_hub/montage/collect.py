"""Collector `montage`: the numbered words of a prepared voice (var/montage/<id>/words.json) for montage-plan, or
the window of one scene for montage-scene (todo #75)."""

from typing import Any

from ultron_hub.common.settings import load_settings
from ultron_hub.montage import photos, plan, retouch, stock, store
from ultron_hub.montage.tools import MontageError


def collect_montage(params: dict[str, Any]) -> dict[str, Any]:
    """Whole plan (montage-plan), or with `scene` = n the words around scene n and its neighbours (montage-scene)."""
    settings = load_settings()
    data = store.read(settings, params.get("montage_id"), store.WORDS)
    words = data.get("words") if isinstance(data, dict) else None
    if not isinstance(words, list) or not words:
        raise MontageError("Les mots de ce montage sont illisibles.")
    if "scene" in params:
        n = params["scene"]
        if isinstance(n, bool) or not isinstance(n, int):
            raise MontageError("Cette scène n'existe pas.")
        return {**retouch.scene_metrics(store.read(settings, params["montage_id"], store.PLAN), words, n,
                                        photos.read(store.folder(settings, params["montage_id"]))),
                "photos_libres": stock.available()}
    return plan.metrics(words, float(data.get("duration") or 0),
                        photos.read(store.folder(settings, params["montage_id"])), stock.available())
