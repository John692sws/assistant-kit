"""Daily plan of the points (todo #534): parts, box of the day (empty / set / expired), fast end, pause, learning off before
three weeks of complete days then on (profile, weekly correction), bilan lines, CLI and catalogue. No network."""
import json
from datetime import date, datetime, timedelta, timezone

from ultron_hub import cli
from ultron_hub.cap import bilan_soir
from ultron_hub.compte import budget, planning
from ultron_hub.dashboard import apercu, catalogue, server
from ultron_hub.todo.store import TodoStore

PARIS = planning.PARIS
NOW = datetime(2026, 11, 5, 14, 30, tzinfo=PARIS)


def acc(slot="a", pct=40.0, spent=0.0, hours=120.0):
    return {"nom": slot, "connu": True, "pourcent": pct, "depense_aujourdhui": spent, "heures_restantes": hours,
            "remise_a_zero": (NOW + timedelta(hours=hours)).isoformat()}


def plan(*accs, saved=None, history=(), now=NOW):
    return planning.plan({"comptes": list(accs)}, list(history), saved or {"planning": None, "pause": None}, now)


def row(p, slot="a"):
    return next(r for r in p["comptes"] if r["nom"] == slot)


# -- parts ------------------------------------------------------------------------------------------------------------

def test_parts_reserve_is_25_percent_at_least_3_points():
    assert planning.parts(21, 25, 100) == {"dose": 21, "reserve": 5, "auto": 16, "fin_rapide": False}
    assert planning.parts(8, 25, 100)["reserve"] == 3  # 2 would be under the floor
    assert planning.parts(2, 25, 100) == {"dose": 2, "reserve": 2, "auto": 0, "fin_rapide": False}  # never over the dose
    assert planning.parts(None, 25, 100) is None


def test_fast_end_gives_the_unused_reserve_to_the_auto():
    assert planning.parts(30, 25, 12.0) == {"dose": 30, "reserve": 0, "auto": 30, "fin_rapide": True}
    assert planning.parts(30, 25, 12.5)["reserve"] == 8
    p = plan(acc(hours=6))
    assert row(p)["fin_rapide"] and row(p)["texte"].endswith("(fin rapide)")


def test_day_dose_is_taken_at_midnight_and_the_text_is_the_one_asked():
    # 40 % now, 10 spent today: 30 % at midnight; reset in 5 days from now = 5.6 days from midnight -> 70 / 5.6 = 12.5 -> 12
    p = plan(acc(pct=40.0, spent=10.0))
    r = row(p)
    assert (r["dose"], r["reserve"], r["auto"], r["source"]) == (12, 3, 9, "défaut")
    assert r["auto_epuisee"] is True and p["seuil"] == 90 and p["texte"] == "A : 12 points = auto 9, à toi 3"
    unknown = plan({"nom": "b", "connu": False, "pourcent": None})
    assert row(unknown, "b")["auto_epuisee"] is None and unknown["texte"] is None


# -- the box of the day -----------------------------------------------------------------------------------------------

def test_box_empty_set_partial_and_expired(tmp_path):
    assert planning.load(tmp_path) == {"planning": None, "pause": None}
    planning.poser(tmp_path, NOW, reserve_b=40, seuil=80)
    saved = planning.load(tmp_path)
    p = plan(acc("a", pct=0.0, hours=100), acc("b", pct=0.0, hours=100), saved=saved)
    assert (row(p, "a")["reserve_pct"], row(p, "a")["source"]) == (25, "défaut")  # empty field = default
    assert (row(p, "b")["reserve_pct"], row(p, "b")["source"]) == (40, "case")
    assert (p["seuil"], p["seuil_source"]) == (80, "case")
    after_midnight = NOW.replace(hour=0, minute=1) + timedelta(days=1)  # Paris midnight passed: back to the defaults
    p2 = plan(acc("b", pct=0.0, hours=100), saved=saved, now=after_midnight)
    assert p2["case"] is None and p2["seuil"] == 90 and row(p2, "b")["source"] == "défaut"
    planning.poser(tmp_path, NOW)  # all empty = cleared
    assert planning.load(tmp_path)["planning"] is None


def test_box_refuses_out_of_range_values(tmp_path):
    for kwargs in ({"reserve_a": 101}, {"reserve_b": -1}, {"seuil": 0}):
        try:
            planning.poser(tmp_path, NOW, **kwargs)
        except planning.store.CompteError:
            continue
        raise AssertionError(kwargs)
    assert planning.load(tmp_path)["planning"] is None


def test_box_wins_over_the_learned_reserve():
    hist = complete_history(22, percent_drop_at=None)
    learned = plan(acc(), history=hist)
    assert row(learned)["source"] == "appris"
    boxed = plan(acc(), history=hist, saved={"planning": {"jour": "2026-11-05", "reserve_a": 10}, "pause": None})
    assert (row(boxed)["reserve_pct"], row(boxed)["source"]) == (10, "case")


def test_pause_is_kept_until_resumed(tmp_path):
    planning.pause(tmp_path, NOW, True)
    assert plan(acc(), saved=planning.load(tmp_path))["pause"] is True
    assert plan(acc(), saved=planning.load(tmp_path), now=NOW + timedelta(days=3))["pause"] is True  # not tied to the day
    planning.pause(tmp_path, NOW, False)
    assert plan(acc(), saved=planning.load(tmp_path))["pause"] is False


# -- learning ---------------------------------------------------------------------------------------------------------

def complete_history(days, slot="a", percent_drop_at=None, start=date(2026, 10, 10), busy_hour=10, final=None, pct0=10.0):
    """`days` complete local days of readings every 15 min; 1 point spent per day at `busy_hour`; an optional reset."""
    rows, pct = [], pct0
    t = (datetime(start.year, start.month, start.day, tzinfo=PARIS) - timedelta(minutes=5)).astimezone(timezone.utc)  # real time steps (DST)
    last = start + timedelta(days=days)
    end = datetime(last.year, last.month, last.day, tzinfo=PARIS).astimezone(timezone.utc) + timedelta(minutes=5)
    while t <= end:
        local = t.astimezone(PARIS)
        if percent_drop_at is not None and local.date() == percent_drop_at and local.hour == 6 and local.minute < 15:
            pct = 0.0
        elif local.hour == busy_hour and local.minute < 15:
            pct += 1.0
        if final is not None and percent_drop_at is not None and local.date() == percent_drop_at - timedelta(days=1) and local.hour == 23 and local.minute >= 45:
            pct = final
        rows.append({"at": t.astimezone(timezone.utc).isoformat(), "compte": slot, "week": {"percent": pct}})
        t += timedelta(minutes=15)
    return rows


def test_complete_days_skip_holes():
    hist = complete_history(5)
    samples = planning._samples(hist, "a")
    assert len(planning.complete_days(samples, PARIS)) == 5
    holed = [r for r in hist if not r["at"].startswith("2026-10-12T1")]  # a sleeping account: 10:00-19:59 UTC missing
    assert len(planning.complete_days(planning._samples(holed, "a"), PARIS)) == 4


def test_learning_off_before_three_weeks_then_on():
    off = planning.learning(complete_history(20), "a")
    assert not off["actif"] and off["jours_complets"] == 20 and off["profil"] is None and off["reserve_pct"] == 25
    on = planning.learning(complete_history(21), "a")
    assert on["actif"] and on["jours_complets"] == 21
    shares = on["profil"][date(2026, 10, 10).weekday()]
    assert abs(sum(shares) - 1) < 1e-9 and shares[10] == 1.0  # all spending at 10:00


def test_profile_smoothing_weights_the_new_week_by_0_3():
    old = complete_history(7, busy_hour=10)
    new = complete_history(7, busy_hour=15, start=date(2026, 10, 17), pct0=17.0)
    samples = planning._samples(old + new, "a")
    shares = planning.profile(samples, PARIS)[date(2026, 10, 10).weekday()]
    assert abs(shares[10] - 0.7) < 1e-9 and abs(shares[15] - 0.3) < 1e-9


def test_profile_spreads_the_auto_part_over_the_day():
    shares = [0.0] * 24
    shares[9], shares[15] = 0.5, 0.5
    assert planning.allowed_so_far(16, shares, NOW.replace(hour=8)) == 0.0
    assert planning.allowed_so_far(16, shares, NOW.replace(hour=12)) == 8.0
    assert planning.allowed_so_far(16, shares, NOW.replace(hour=15, minute=30)) == 12.0
    assert planning.allowed_so_far(16, None, NOW.replace(hour=1)) == 16.0  # learning off: the whole part


def test_weekly_correction_moves_the_reserve_by_one_step():
    assert planning.correction(93.0, 25) == 20  # 7 points under the target: reserve unused, one step down
    assert planning.correction(96.0, 25) == 25  # within 5 points: unchanged
    assert planning.correction(106.0, 25) == 30
    assert planning.correction(50.0, 0) == 0 and planning.correction(200.0, 50) == 50


def test_a_reset_corrects_only_once_learning_is_on_and_gives_its_bilan_line():
    early = complete_history(25, percent_drop_at=date(2026, 10, 20), final=80.0)  # reset after 10 complete days
    b = planning.learning(early, "a")["bilans"]
    assert len(b) == 1 and not b[0]["apprentissage"] and b[0]["reserve_apres"] == 25
    late = complete_history(25, percent_drop_at=date(2026, 11, 2), final=80.0)  # reset after 23 complete days
    lrn = planning.learning(late, "a")
    assert lrn["bilans"][0]["apprentissage"] and lrn["bilans"][0]["reel"] == 80.0 and lrn["reserve_pct"] == 20
    (line,) = planning.bilans_du_jour(late, date(2026, 11, 2))
    assert line["texte"] == "Remise à zéro de A : prévu 100 %, réel 80 %, réserve 25 % → 20 %"


def test_destinations_soonest_reset_first():
    rows = [{"nom": "a", "remise_a_zero": "2026-11-09"}, {"nom": "b", "remise_a_zero": "2026-11-07"}, {"nom": "c", "remise_a_zero": None}]
    assert planning.destinations(rows, "a") == ["b", "c"]


# -- CLI, bilan du soir, catalogue --------------------------------------------------------------------------------------

def test_cli_planning_is_json_and_files_only(project, capsys):
    def call(*argv):
        code = cli.main(["compte", "planning", *argv])
        return code, json.loads(capsys.readouterr().out)
    code, out = call("poser", "--reserve-a", "30", "--seuil", "85")
    assert code == 0 and out["data"]["seuil"] == 85 and out["data"]["case"]["reserve_a"] == 30
    code, out = call("pause")
    assert out["data"]["pause"] is True and "en pause" in out["summary"]
    code, out = call("reprendre")
    assert out["data"]["pause"] is False
    code, out = call("poser", "--seuil", "0")
    assert out["status"] == "error" and "seuil refusé" in out["error"]
    code, out = call("voir")
    assert code == 0 and out["command"] == "compte.planning.voir"


def test_bilan_du_soir_lists_the_weekly_reset(project, capsys, monkeypatch):
    from ultron_hub.common.settings import load_settings
    settings = load_settings()
    hist = complete_history(25, percent_drop_at=date(2026, 11, 2), final=80.0)
    budget._write_history(settings.var_dir, hist)
    with TodoStore(settings.todo_db):
        pass
    out = bilan_soir.run(settings, "2026-11-02")
    assert [r["compte"] for r in out["remises"]] == ["a"] and out["faits"] == 0  # not a gesture: outside `lignes`


def test_catalogue_entries_and_card_wiring():
    poser = catalogue.PAR_ID["planning-poser"]
    assert poser.type == "simple" and all(c.min == 0 for c in poser.champs)
    assert catalogue.PAR_ID["planning-pause"].argv == ("compte", "planning", "pause")
    shown = catalogue.projeter(catalogue.PAR_ID["planning"].donnees, apercu.LECTURES["planning"])
    assert shown["comptes"][0]["texte"] == "A : 21 points = auto 16, à toi 5"
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert 'form("planning-poser"' in js and 'd.pause ? "planning-reprendre" : "planning-pause"' in js
    assert '["planning", function (d) { aToiSources.planning = d; }]' in js
