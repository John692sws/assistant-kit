"""Reflex video-skill (todo #476): a recent "video skill failed" line in the hub log raises an alert."""
import time
from datetime import datetime, timedelta
from pathlib import Path

from ultron_hub.health import reflexes

REPO = Path(__file__).resolve().parents[1]
inspecter = reflexes.inspecter_video  # the real one: conftest replaces it during each test


def _log(root, *lines):
    (root / "var" / "logs").mkdir(parents=True)
    (root / "var" / "logs" / "ultron-hub.log").write_text("\n".join(lines) + "\n")


def _line(when, msg="video skill failed (exit 1): boom"):
    return f"{when:%Y-%m-%d %H:%M:%S},905 WARNING ultron_hub.video: {msg}"


def test_recent_failure_is_found_old_or_absent_is_not(tmp_path):
    now = datetime.now()
    _log(tmp_path, _line(now - timedelta(days=3)), _line(now - timedelta(hours=1), "collected ok"))
    assert inspecter(tmp_path)["stale"] is False
    _log(tmp_path.parent / "b", _line(now - timedelta(hours=2)))
    res = inspecter(tmp_path.parent / "b", now=time.time())
    assert res["ok"] and res["stale"] and "video skill failed" in res["reason"]
    assert inspecter(tmp_path / "nope") == {"ok": True, "stale": False, "reason": "pas de journal"}


def test_reflex_is_in_the_yaml_and_the_defaults_and_diagnoses():
    assert any(r["nom"] == "video-skill" and r["verification"] == "video_skill" for r in reflexes.charger(REPO / "config" / "sante.yaml"))
    diags = reflexes.diagnostiquer({}, [reflexes.VIDEO_DEFAUT], inspect_video=lambda: {"ok": True, "stale": True, "reason": "x"})
    assert [d["nom"] for d in diags] == ["video-skill"] and diags[0]["bouton"] is None
    assert reflexes.diagnostiquer({}, [reflexes.VIDEO_DEFAUT], inspect_video=lambda: {"ok": True, "stale": False}) == []
