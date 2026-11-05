"""Todo #160: health alerts on state change only (no network, no real send, no real systemd)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ultron_hub.common.settings import load_settings
from ultron_hub.health import alertes, seuils

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
CFG = seuils.charger(REPO / "config" / "sante.yaml")


def healthy(**over):
    m = {"services": {"failed_system": [], "failed_user": []},
         "disks": [{"mount": "/", "used_pct": 40.0}],
         "backup": {"configured": True, "last_success_age_h": 5.0, "last_run_status": "ok", "last_run_error": None,
                    "last_check_at": (NOW - timedelta(days=2)).isoformat(), "last_check_status": "ok"},
         "errors": {}}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(m.get(k), dict):
            m[k] = {**m[k], **v}
        else:
            m[k] = v
    return m


def test_healthy_server_has_no_anomaly():
    assert alertes.evaluer(healthy(), CFG, NOW) == {}


def test_each_rule_fires_at_its_threshold():
    ev = lambda m: alertes.evaluer(m, CFG, NOW)  # noqa: E731
    assert list(ev(healthy(services={"failed_user": ["x.service"]}))) == ["unite:utilisateur:x.service"]
    assert list(ev(healthy(services={"failed_system": ["y.service"]}))) == ["unite:système:y.service"]
    assert ev(healthy(disks=[{"mount": "/", "used_pct": 85.0}])) == {}
    assert list(ev(healthy(disks=[{"mount": "/", "used_pct": 85.1}]))) == ["disque:/"]
    assert ev(healthy(backup={"last_success_age_h": 30.0})) == {}
    assert list(ev(healthy(backup={"last_success_age_h": 30.5}))) == ["sauvegarde:ancienne"]
    assert list(ev(healthy(backup={"last_success_age_h": None}))) == ["sauvegarde:ancienne"]
    assert list(ev(healthy(backup={"last_run_status": "error", "last_run_error": "boom"}))) == ["sauvegarde:erreur"]
    assert ev(healthy(backup={"last_run_status": "partial"})) == {}
    old = (NOW - timedelta(days=8, hours=1)).isoformat()
    assert list(ev(healthy(backup={"last_check_at": old}))) == ["check:ancien"]
    assert list(ev(healthy(backup={"last_check_at": None}))) == ["check:ancien"]


def test_unconfigured_backup_or_failed_probe_gives_no_backup_alert():
    assert alertes.evaluer(healthy(backup={"configured": False, "last_success_age_h": None}), CFG, NOW) == {}
    assert alertes.evaluer(healthy(backup=None, disks=None, services=None, errors={"backup": "x"}), CFG, NOW) == {}


def test_thresholds_come_from_the_config(tmp_path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text("alertes:\n  disque_pct: 30\n")
    assert list(alertes.evaluer(healthy(), seuils.charger(cfg), NOW)) == ["disque:/"]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    return load_settings(tmp_path)


def run(settings, metrics, sent, fail=False):
    def send(text):
        if fail:
            raise RuntimeError("hermes down")
        sent.append(text)
    return alertes.run_alertes(settings, metrics=metrics, cfg=CFG, now=NOW, send=send)


def test_message_only_on_change_never_repeated(settings):
    sent = []
    bad = healthy(disks=[{"mount": "/", "used_pct": 90.0}])
    assert run(settings, healthy(), sent)["sent"] is False and sent == []  # healthy first run: silent
    r = run(settings, bad, sent)
    assert r["sent"] and r["new"] == ["disque:/"] and len(sent) == 1 and "90 %" in sent[0]
    run(settings, bad, sent)
    run(settings, healthy(disks=[{"mount": "/", "used_pct": 91.0}]), sent)  # same anomaly, text changed: silent
    assert len(sent) == 1
    r = run(settings, healthy(), sent)  # resolved
    assert r["resolved"] == ["disque:/"] and len(sent) == 2 and "Résolu" in sent[1] and "Plus aucune" in sent[1]
    run(settings, healthy(), sent)
    assert len(sent) == 2
    assert json.loads((settings.root / "var/health/alertes.json").read_text())["actives"] == {}


def test_new_and_resolved_in_one_message_with_remaining(settings):
    sent = []
    run(settings, healthy(services={"failed_user": ["a"]}), sent)
    run(settings, healthy(services={"failed_user": ["a", "b"]}), sent)
    assert len(sent) == 2 and "b" in sent[1] and "Toujours en cours" in sent[1]


def test_failed_send_keeps_state_and_retries(settings):
    bad = healthy(disks=[{"mount": "/", "used_pct": 90.0}])
    sent = []
    r = run(settings, bad, sent, fail=True)
    assert r["sent"] is False and "hermes down" in r["send_error"]
    assert not (settings.root / "var/health/alertes.json").exists() or \
        json.loads((settings.root / "var/health/alertes.json").read_text())["actives"] == {}
    assert run(settings, bad, sent)["sent"] and len(sent) == 1


def test_corrupt_state_is_treated_as_empty(settings):
    p = settings.root / "var/health/alertes.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert alertes.read_state(p) == {}
    p.write_text('{"actives": [1]}')
    assert alertes.read_state(p) == {}


def test_change_is_logged_in_the_shared_journal(settings, monkeypatch):
    lines = []
    monkeypatch.setattr(alertes.journal, "record", lambda s, src, kind, text, todo=None: lines.append((src, kind, text)))
    run(settings, healthy(disks=[{"mount": "/", "used_pct": 90.0}]), [])
    assert lines == [("ultron-hub", "sante", "Alertes santé : 1 nouvelle(s), 0 résolue(s), 1 active(s)")]


def test_default_send_goes_through_hermes_send(settings, no_real_send):
    alertes.run_alertes(settings, metrics=healthy(disks=[{"mount": "/", "used_pct": 99.0}]), cfg=CFG, now=NOW)
    assert len(no_real_send) == 1 and "99 %" in no_real_send[0]


def test_command_envelope(settings, monkeypatch, capsys):
    from ultron_hub import cli
    # the CLI reads the real clock: a check dated from the fixed NOW aged past its 8-day threshold on 2026-10-24 (todo #377)
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    monkeypatch.setattr(alertes, "collect", lambda only=None: healthy(backup={"last_check_at": recent}))
    assert cli.main(["health", "alertes"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "health.alertes" and out["status"] == "ok" and out["summary"] == "rien à signaler"


def test_service_runs_alertes_after_collect_and_action_is_declared():
    unit = (REPO / "deploy/systemd/ultron-hub-health.service").read_text()
    lines = [x for x in unit.splitlines() if x.startswith("Exec")]
    assert lines[0].startswith("ExecStart=") and lines[0].endswith("health collect --record")
    assert lines[1].startswith("ExecStartPost=-") and lines[1].endswith("health alertes")
    actions = json.loads((REPO / "deploy/user/apres-fusion/todo-160.json").read_text())["actions"]
    assert actions[0]["action"] == "user_unit_enable" and actions[0]["args"] == ["ultron-hub-health.timer"]


def test_restore_test_rule_fires_on_failure_or_after_35_days():
    ev = lambda m: list(alertes.evaluer(m, CFG, NOW))  # noqa: E731
    recent = (NOW - timedelta(days=10)).isoformat()
    assert ev(healthy(backup={"last_restore_status": "ok", "last_restore_at": recent})) == []
    assert ev(healthy(backup={"last_restore_status": "ok", "last_restore_at": (NOW - timedelta(days=36)).isoformat()})) \
        == ["restauration:ancien"]
    assert ev(healthy(backup={"last_restore_status": "ok", "last_restore_at": (NOW - timedelta(days=35)).isoformat()})) == []
    assert ev(healthy(backup={"last_restore_status": "error", "last_restore_at": recent,
                              "last_restore_error": "sha256"})) == ["restauration:echec"]
    assert ev(healthy()) == []  # never recorded yet: no false alarm right after the merge


def test_alerte_reglee_edite_le_message_direct(settings):
    from types import SimpleNamespace
    bad = healthy(disks=[{"mount": "/", "used_pct": 90.0}])
    edits = []
    res = SimpleNamespace(message_id=42, chat="-100")
    alertes.run_alertes(settings, metrics=bad, cfg=CFG, now=NOW, send=lambda t: res)
    stocke = json.loads(alertes.messages_path(settings.root).read_text())
    assert stocke["disque:/"]["message_id"] == 42
    new, resolved = {}, {"disque:/": "x"}
    import ultron_hub.envoi as envoi
    anciens = envoi.appeler, envoi.mode
    envoi.mode = lambda: "direct"
    envoi.appeler = lambda meth, ch, *a, **k: edits.append((ch["chat_id"], ch["message_id"], ch["text"])) or {"ok": True}
    try:
        alertes.run_alertes(settings, metrics=healthy(), cfg=CFG, now=NOW + timedelta(hours=1), send=lambda t: None)
    finally:
        envoi.appeler, envoi.mode = anciens
    assert edits and edits[0][:2] == ("-100", 42) and edits[0][2].startswith("✅ réglée à ") and "disque" in edits[0][2]
    assert json.loads(alertes.messages_path(settings.root).read_text()) == {}


def test_mode_hermes_ou_erreur_telegram_ne_casse_rien(settings):
    bad = healthy(disks=[{"mount": "/", "used_pct": 90.0}])
    alertes.run_alertes(settings, metrics=bad, cfg=CFG, now=NOW, send=lambda t: None)  # pas d'id : rien de gardé
    assert not alertes.messages_path(settings.root).exists() or json.loads(alertes.messages_path(settings.root).read_text()) == {}
    from types import SimpleNamespace
    alertes.run_alertes(settings, metrics=bad, cfg=CFG, now=NOW, send=lambda t: SimpleNamespace(message_id=1, chat="c"))
    def boom(*a):
        raise RuntimeError("message trop vieux")
    alertes.suivre_messages(settings.root, {}, {"disque:/": "x"}, None, "", NOW, edit=boom)  # ne lève pas
