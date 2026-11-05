"""Automatic account switch on a near quota (todo #534): written rule, refusals, proof, bilan du soir line and its ↩️.

Fake accounts and endpoints of tests/test_compte.py (never the real credentials, never the network)."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import test_compte as tc
from test_compte import env, run, dashboard_press, register_both  # noqa: F401  (fixtures)
from ultron_hub.cap import bilan_soir
from ultron_hub.compte import alertes, auto, budget, planning, store
from ultron_hub.esprit import store as journal
from ultron_hub.todo.store import TodoStore

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def row(nom, session, week=30):
    quota = None if session is None else {"session": {"percent": session}, "week": {"percent": week}, "stale": False}
    return {"nom": nom, "enregistre": True, "quota": quota}


def state(a, b, *, last=None, reco=()):
    return {"actif": "a", "comptes": [row("a", a), row("b", b)], "derniere_bascule": last,
            "a_reconnecter": [{"nom": n} for n in reco]}


def test_decide_follows_the_written_rule_and_its_refusals():
    ok, why = auto.decide(state(95, 10), NOW, 90.0, None)
    assert ok and ok["vers"] == "b" and why == ""
    assert auto.decide(state(95, 10), NOW, None, None) == (None, "pas de limite posée")
    assert auto.decide(state(80, 10), NOW, 90.0, None)[1] == "quota du compte actif sous le seuil"
    assert "à reconnecter" in auto.decide(state(95, 10, reco=("b",)), NOW, 90.0, None)[1]
    recent = {"a": (NOW - timedelta(hours=5)).isoformat()}
    assert "moins de 6 h" in auto.decide(state(95, 10, last=recent), NOW, 90.0, None)[1]
    old = {"a": (NOW - timedelta(hours=7)).isoformat()}
    assert auto.decide(state(95, 10, last=old), NOW, 90.0, None)[0]
    assert "au-dessus du seuil" in auto.decide(state(95, 92), NOW, 90.0, None)[1]
    s = state(95, 10)
    s["comptes"][1]["quota"]["week"]["percent"] = 95
    assert "au-dessus du seuil" in auto.decide(s, NOW, 90.0, 88.0)[1]
    assert "illisible" in auto.decide(state(95, None), NOW, 90.0, None)[1]
    # the alert uses the same rule
    assert alertes.candidates(state(95, 10), {}, 90.0, None).keys() == {"bascule"}


def history(var, a_week=40.0, b_week=10.0, a_spent=0.0, b_spent=0.0, now=None):
    """Two readings per account (yesterday 23:50 Paris, then 1 minute ago): `x_spent` points used today."""
    now = now or datetime.now(timezone.utc)
    midnight = now.astimezone(planning.PARIS).replace(hour=0, minute=0, second=0, microsecond=0)
    resets = (now + timedelta(days=5)).isoformat()
    rows = []
    for slot, week, spent in (("a", a_week, a_spent), ("b", b_week, b_spent)):
        for at, pct in ((midnight - timedelta(minutes=10), week - spent), (now - timedelta(minutes=1), week)):
            rows.append({"at": at.isoformat(), "compte": slot, "session": None, "week": {"percent": pct, "resets_at": resets}})
    budget._write_history(var, rows)


def plan_of(a_spent=0.0, b_spent=0.0, pause=False, tmp=None):
    figures = {"comptes": [
        {"nom": s, "connu": True, "pourcent": 40.0, "depense_aujourdhui": sp, "remise_a_zero": (NOW + timedelta(days=5)).isoformat(),
         "heures_restantes": 120.0} for s, sp in (("a", a_spent), ("b", b_spent))]}
    return planning.plan(figures, [], {"planning": None, "pause": {"a": "x"} if pause else None}, NOW)


def test_decide_with_the_day_parts_and_the_pause():
    # dose (100 - 40) / 5 = 12 points: reserve 3 (25 % = 3, at least 3), auto 9
    assert auto.decide(state(95, 10), NOW, 90.0, None, plan_of())[0]["vers"] == "b"
    why = auto.decide(state(95, 10), NOW, 90.0, None, plan_of(a_spent=9.0))[1]
    assert why.startswith("l'auto s'arrête : A a dépensé sa part auto du jour (9.0 points pour 9.0 permis)")
    assert "B : part auto du jour déjà dépensée" in auto.decide(state(95, 10), NOW, 90.0, None, plan_of(b_spent=10.0))[1]
    assert auto.decide(state(95, 10), NOW, 90.0, None, plan_of(pause=True))[1] == auto.PAUSED
    unknown = plan_of()
    unknown["comptes"][1].update(auto_epuisee=None)
    assert "part auto du jour de B inconnue" in auto.decide(state(95, 10), NOW, 90.0, None, unknown)[1]
    assert auto.decide(state(85, 10), NOW, 90.0, None, plan_of())[1] == "quota du compte actif sous le seuil"


def test_paused_no_reading_and_refused_from_a_job(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no reading while paused or from a job")
    cfg = SimpleNamespace(var_dir=tmp_path)
    planning.pause(tmp_path, NOW, True)
    assert auto.basculer_auto(cfg, NOW, etat=boom)["raison"] == auto.PAUSED
    planning.pause(tmp_path, NOW, False)
    monkeypatch.setenv("ULTRON_HUB_JOB", "1")
    assert auto.basculer_auto(cfg, NOW, etat=boom)["fait"] is False
    auto.heartbeat(cfg)  # never raises


def test_default_threshold_is_90_and_the_box_moves_it(env, capsys, monkeypatch, tmp_path):  # noqa: F811
    register_both(env, capsys)
    cfg = SimpleNamespace(var_dir=tmp_path / "var")
    history(cfg.var_dir)
    monkeypatch.setitem(tc.USAGE, "ACCESS-A1", 85.0)  # no SESSION_LIMIT set: the plan's 90 % applies
    assert auto.basculer_auto(cfg)["raison"] == "quota du compte actif sous le seuil"
    planning.poser(cfg.var_dir, datetime.now(timezone.utc), seuil=80)
    monkeypatch.setattr(journal, "record", lambda *a, **k: None)
    res = auto.basculer_auto(cfg)
    assert res["fait"] and "seuil 80 %" in res["raison"] and env["active"].read_bytes() == tc.B1


def test_a_pause_set_meanwhile_wins_under_the_lock(env, capsys, monkeypatch, tmp_path):  # noqa: F811
    register_both(env, capsys)
    cfg = SimpleNamespace(var_dir=tmp_path / "var")
    history(cfg.var_dir)
    monkeypatch.setitem(tc.USAGE, "ACCESS-A1", 95.0)
    real = store.etat

    def etat(cache_dir, now):  # the pause button pressed between the reading and the switch
        planning.pause(cfg.var_dir, now, True)
        return real(cache_dir, now)
    res = auto.basculer_auto(cfg, etat=etat)
    assert not res["fait"] and "pause" in res["raison"] and env["active"].read_bytes() == tc.A1


def test_auto_switch_traced_listed_and_undone(env, capsys, monkeypatch, tmp_path):  # noqa: F811
    register_both(env, capsys)
    monkeypatch.setitem(tc.USAGE, "ACCESS-A1", 95.0)
    history(tmp_path / "var")
    notes = []
    monkeypatch.setattr(journal, "record", lambda settings, source, kind, text, todo=None: notes.append((kind, text)))
    cfg = SimpleNamespace(var_dir=tmp_path / "var", todo_db=tmp_path / "t.db")
    res = auto.basculer_auto(cfg)
    assert res["fait"], res
    assert env["active"].read_bytes() == tc.B1
    raw = (store.DIR / store.META).read_text()
    meta = json.loads(raw)
    assert meta["derniere_bascule"]["by"] == "auto:quota" and meta["derniere_bascule"]["vers"] == "b"
    assert meta["bascules_auto"][-1]["by"] == "auto:quota" and "95 %" in meta["bascules_auto"][-1]["regle"]
    for secret in tc.SECRETS:
        assert secret not in raw
    assert notes and notes[0][0] == "compte" and "auto:quota" in notes[0][1]
    # a second beat: refused, the last switch is less than 6 h old
    monkeypatch.setitem(tc.USAGE, "ACCESS-B1", 95.0)
    monkeypatch.setitem(tc.USAGE, "ACCESS-A1", 3.0)
    again = auto.basculer_auto(cfg)
    assert not again["fait"] and "moins de 6 h" in again["raison"]
    assert env["active"].read_bytes() == tc.B1
    # bilan du soir: one 🔁 line, ↩️ = the existing signed switch back to A
    with TodoStore(cfg.todo_db):
        pass
    out = bilan_soir.run(cfg)
    (ligne,) = [x for x in out["lignes"] if x["sorte"] == "bascule"]
    assert (ligne["retour"], ligne["by"], ligne["annulee"], out["bascules"], out["clotures"]) == ("a", "auto:quota", False, 1, 0)
    dashboard_press(monkeypatch, "a")
    code, done = run(capsys, "basculer", "--vers", "a")
    assert code == 0 and env["active"].read_bytes() == tc.A1
    assert [x for x in bilan_soir.run(cfg)["lignes"] if x["sorte"] == "bascule"][0]["annulee"] is True


def test_destination_to_log_into_again_is_refused(env, capsys, monkeypatch, tmp_path):  # noqa: F811
    register_both(env, capsys)
    monkeypatch.setitem(tc.USAGE, "ACCESS-A1", 95.0)
    history(tmp_path)
    real = store.etat

    def etat(cache_dir, now):
        out = real(cache_dir, now)
        return {**out, "a_reconnecter": [{"nom": "b", "raison": "x"}]}
    res = auto.basculer_auto(SimpleNamespace(var_dir=tmp_path), etat=etat)
    assert not res["fait"] and "à reconnecter" in res["raison"] and env["active"].read_bytes() == tc.A1


def test_bilan_card_wiring():
    from ultron_hub.dashboard import server
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert 'form("compte-basculer", { vers: x.retour })' in js
