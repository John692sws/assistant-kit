"""Daily budget of the two Claude accounts (todo #328): history, figures, advice, sleeping account. No network."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ultron_hub import cli
from ultron_hub.compte import alertes, budget, store

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def sample(slot, at, percent, resets):
    return {"at": at.isoformat(), "compte": slot, "session": None, "week": {"percent": percent, "resets_at": resets.isoformat()}}


def write(var, rows):
    path = budget.history_path(var)
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "garbage\n", encoding="utf-8")


def by(out, slot):
    return next(c for c in out["comptes"] if c["nom"] == slot)


def test_figures_budget_spent_today_and_advice(tmp_path):
    a_reset, b_reset = NOW + timedelta(hours=30), NOW + timedelta(days=5, hours=2)
    write(tmp_path, [
        sample("a", NOW - timedelta(hours=20), 60.0, a_reset),  # before midnight: the baseline
        sample("a", NOW - timedelta(hours=5), 66.0, a_reset),
        sample("a", NOW - timedelta(minutes=5), 70.0, a_reset),
        sample("b", NOW - timedelta(minutes=10), 40.0, b_reset)])
    out = budget.budget(tmp_path, NOW, UTC)
    a, b = by(out, "a"), by(out, "b")
    assert (a["restant"], a["jours_restants"], a["budget_jour"], a["depense_aujourdhui"]) == (30.0, 2, 15.0, 10.0)
    assert (b["restant"], b["jours_restants"], b["budget_jour"], b["depense_aujourdhui"]) == (60.0, 6, 10.0, 0.0)
    assert out["total"] == {"restant": 90.0, "budget_jour": 25.0, "depense_aujourdhui": 10.0}
    assert not a["veille"] and not b["veille"]
    assert out["conseil"].startswith("Épuise d'abord le compte A")



def test_advice_names_the_account_by_its_address(tmp_path, monkeypatch):  # todo #413
    monkeypatch.setattr(budget.store, "adresses_of", lambda meta: {"a": "a@exemple.fr", "b": None})
    monkeypatch.setattr(budget.store, "load_meta", lambda: {})
    write(tmp_path, [sample("a", NOW - timedelta(minutes=5), 70.0, NOW + timedelta(hours=30)),
                     sample("b", NOW - timedelta(minutes=10), 40.0, NOW + timedelta(days=5))])
    assert budget.budget(tmp_path, NOW, UTC)["conseil"] == "Épuise d'abord a@exemple.fr : sa semaine se remet à zéro dans 30 h."

def test_sleeping_account_keeps_last_value_and_time_and_reset_counts_from_zero(tmp_path):
    reset = NOW + timedelta(days=2)
    write(tmp_path, [sample("a", NOW - timedelta(hours=6), 30.0, reset),
                     sample("b", NOW - timedelta(hours=1), 90.0, NOW - timedelta(minutes=30))])  # week turned over
    out = budget.budget(tmp_path, NOW, UTC)
    a, b = by(out, "a"), by(out, "b")
    assert a["veille"] and a["pourcent"] == 30.0 and a["lu_a"].startswith("2026-10-23T06:00")
    assert b["remise_passee"] and b["pourcent"] == 0.0 and b["jours_restants"] == 7 and b["restant"] == 100.0


def test_spent_counts_a_reset_from_zero_and_empty_history(tmp_path):
    midnight = NOW.replace(hour=0)
    rows = [sample("a", midnight - timedelta(hours=1), 80.0, NOW), sample("a", midnight + timedelta(hours=2), 85.0, NOW),
            sample("a", midnight + timedelta(hours=4), 3.0, NOW + timedelta(days=7))]
    assert budget._spent_today(rows, midnight) == 8.0  # 5 before the reset, 3 after
    empty = budget.budget(tmp_path, NOW, UTC)
    assert empty["total"] is None and empty["conseil"] is None and not by(empty, "a")["connu"]


def test_releve_appends_fresh_quotas_only_and_keeps_no_token(tmp_path, monkeypatch):
    week = {"percent": 12.5, "resets_at": "2026-10-28T08:00:00+00:00"}
    state = {"comptes": [{"nom": "a", "enregistre": True, "quota": {"session": None, "week": week, "stale": False}},
                         {"nom": "b", "enregistre": True, "quota": None}]}
    monkeypatch.setattr(store, "etat", lambda cache, now: state)
    out = budget.releve(tmp_path, tmp_path / "cache", NOW)
    assert out == {"releves": ["a"], "ignores": ["b"], "alertes": []}
    text = budget.history_path(tmp_path).read_text(encoding="utf-8")
    assert json.loads(text)["week"] == week and "oken" not in text
    old = NOW - budget.KEEP - timedelta(days=1)
    budget._write_history(tmp_path, [sample("a", old, 1.0, NOW)])
    budget.releve(tmp_path, tmp_path / "cache", NOW)
    assert len(budget.read_history(tmp_path)) == 1  # the old row was dropped


def test_cli_budget_prints_json_only(project, capsys):
    assert cli.main(["compte", "budget"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "compte.budget" and out["data"]["limite"] == 100.0


def test_units_and_after_merge_file():
    root = Path(__file__).parents[1]
    assert "compte releve" in (root / "deploy/systemd/ultron-hub-compte-releve.service").read_text()
    assert "OnCalendar=*:0/15" in (root / "deploy/systemd/ultron-hub-compte-releve.timer").read_text()
    actions = json.loads((root / "deploy/user/apres-fusion/todo-328.json").read_text())["actions"]
    assert actions[0] == {**actions[0], "action": "user_unit_enable", "args": ["ultron-hub-compte-releve.timer"]}


def test_dose_pure_function_floor_and_rounding():
    reset = NOW + timedelta(days=3, hours=12)
    assert budget.dose_du_jour(30.0, reset.isoformat(), NOW) == 20  # 70 / 3.5 days
    assert budget.dose_du_jour(30.0, (NOW + timedelta(hours=2)).isoformat(), NOW) == 70  # floor of 1 day
    assert budget.dose_du_jour(100.0, reset.isoformat(), NOW) == 0 and budget.dose_du_jour(None, None, NOW) is None
    assert budget.dose_du_jour(50.0, None, NOW) == 7  # no reset known: a full week
    assert budget.dose_texte({"a": 12, "b": 9}) == "Aujourd'hui : A 12 points, B 9 points"


def test_offset_accounts_each_use_their_own_reset(tmp_path):
    a_reset, b_reset = datetime(2026, 11, 1, 2, tzinfo=UTC), datetime(2026, 11, 2, 5, tzinfo=UTC)
    now = datetime(2026, 10, 30, 2, tzinfo=UTC)
    assert budget.dose_du_jour(50.0, a_reset.isoformat(), now) == 25  # 2 days
    assert budget.dose_du_jour(50.0, b_reset.isoformat(), now) == 16  # 50 / 3.125 days
    write(tmp_path, [sample("a", now - timedelta(minutes=5), 50.0, a_reset), sample("b", now - timedelta(minutes=5), 50.0, b_reset)])
    out = budget.budget(tmp_path, now, UTC)
    assert (by(out, "a")["dose"], by(out, "b")["dose"]) == (25, 16)
    assert out["dose_texte"] == "Aujourd'hui : A 25 points, B 16 points"


def test_overspending_yesterday_lowers_the_dose():
    reset = NOW + timedelta(days=4)
    normal = budget.dose_du_jour(40.0, reset.isoformat(), NOW)
    over = budget.dose_du_jour(60.0, reset.isoformat(), NOW)
    assert over < normal and over == 10 and normal == 15


def test_no_default_threshold_left():
    assert budget.TARGET == 100.0 and alertes.SESSION_LIMIT is None and alertes.WEEK_LIMIT is None
