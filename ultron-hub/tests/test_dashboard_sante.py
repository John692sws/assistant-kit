"""Todo #247: the detailed health from the dashboard (Telegram's /sante: health reflexes, guard, battery; 🩺 Agir on a
known breakdown, signed `reflexe` on the "<reflex>:<fingerprint>" shown; the full analysis, simple).

Refusals: action outside the list, replayed proof, changed fingerprint, device not allowed. No network, no real restic,
no real claude: the dashboard calls a fake CLI, the signed press runs `reflexes.agir` with a fake ultron-user tool.
"""

import json
import time
from pathlib import Path

import pytest

from hermes_plugin import close_key, health_flow
from test_dashboard_actions import (PHONE_ID, SELF_ID, STRANGER_ID, confirm, dash, fake_cli, fresh_cache,  # noqa: F401
                                    prepare, request, session)
from test_health_reflexes import TARGET, FakeTool, diagnosed, settings  # noqa: F401  (fixtures)
from ultron_hub.dashboard import apercu, catalogue
from ultron_hub.health import reflexes
from ultron_hub.todo import closing

ROOT = Path(__file__).parents[1]
DEVICE = "telephone-test"


def dashboard_token(action, target):
    return closing.sign_dashboard(closing.load_key(), DEVICE, action, target, int(time.time()))


# -- closing: `reflexe` added at the end, the same on both sides ------------------------------------------------------

def test_reflexe_is_signable_from_the_dashboard_on_both_sides():
    assert closing.DASHBOARD_ACTIONS[-10] == "reflexe"  # then `revert-fusion` (todo #254), `compte` (todo #263), `session` (todo #265), `menage-lot` (todo #362), `rapports-lot` (todo #417), `habitude` (todo #429), `video-lot` (todo #442), `autorisation` (todo #495), `routine` (todo #503)
    assert close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    key = b"k" * 64
    assert close_key.dashboard_token(DEVICE, "reflexe", TARGET, key=key, now=1000) == \
        closing.sign_dashboard(key, DEVICE, "reflexe", TARGET, 1000)
    assert catalogue.PAR_ID["reflexe"].signe == "reflexe"


def test_entries_and_their_commands():
    a = catalogue.PAR_ID["reflexe"]
    _, args, stdin = catalogue.valider("reflexe", {"panne": TARGET})
    assert catalogue.commande(a, args, stdin) == ["health", "reflexe-agir", TARGET]
    assert catalogue.cible(a, args, stdin) == TARGET  # exactly the fingerprint shown on the sheet
    assert TARGET in catalogue.feuille(a, args, stdin)["sur_quoi"]
    assert a.delai == health_flow.REFLEX_TIMEOUT_S
    full = catalogue.PAR_ID["sante-analyse"]
    assert full.type == "simple" and full.signe is None and full.delai == health_flow.RUN_TIMEOUT_S
    assert catalogue.commande(full, {}, None) == ["run", "sante-serveur", "--requested-by", "dashboard"]
    for id_, argv in (("sante-reflexes", ["health", "reflexes"]), ("sante-garde", ["health", "guard"]),
                      ("sante-batterie", ["health", "battery"])):
        assert catalogue.PAR_ID[id_].type == "lecture" and catalogue.commande(catalogue.PAR_ID[id_], {}, None) == argv


@pytest.mark.parametrize("bad", ["verrou-restic:../../x", "verrou-restic:0123456789AB", "verrou-restic:0123456789a",
                                 "-x:0123456789ab", "verrou-restic", "", "verrou-restic:0123456789ab --i-am-human"])
def test_target_refused_unless_well_formed(bad):
    with pytest.raises(catalogue.Refused):
        catalogue.valider("reflexe", {"panne": bad})


def test_diagnosis_reaches_the_page_without_the_tool_action(dash, monkeypatch):
    from ultron_hub.dashboard import actions
    seen = []

    def run(self, argv, extra, **kw):
        seen.append((argv, extra))
        return {"status": "ok", "summary": "1 diagnostic(s)", "data": apercu.LECTURES["sante-reflexes"]}
    monkeypatch.setattr(actions.Actions, "_run", run)
    sid = session(dash)
    code, out, _ = request(dash, "POST", "/api/action/lire", {"action": "sante-reflexes"}, cookie=sid)
    assert code == 200 and out["ok"]
    [diag] = out["donnees"]["diagnostics"]
    assert diag["bouton"] == {"label": "🔓 Retirer le verrou", "cible": "verrou-restic:0123456789ab"}
    assert "jamais montré" not in json.dumps(out) and "args" not in json.dumps(out)
    assert seen == [(["health", "reflexes"], {})] and dash.journal == []


# -- 🩺 Agir: signed on the target shown, through the server ------------------------------------------------------------

def test_reflex_is_signed_on_the_target_shown(dash):
    sid = session(dash)
    code, out, _ = prepare(dash, sid, {"action": "reflexe", "panne": TARGET})
    assert code == 200 and TARGET in out["sur_quoi"] and out["attente"]
    assert dash.calls() == []
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 200 and res["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["health", "reflexe-agir", TARGET]
    token = call["env"][closing.TOKEN_ENV]
    assert closing.check(token, "reflexe", TARGET)[1] == "dashboard:" + DEVICE
    for action, target in (("reflexe", "verrou-restic:ffffffffffff"), ("user", TARGET), ("done", TARGET)):
        with pytest.raises(closing.CloseRefused):  # another fingerprint, another action: never
            closing.check(token, action, target)
    assert confirm(dash, sid, out["jeton"])[0] == 409 and len(dash.calls()) == 1  # the sheet's token is spent
    assert dash.journal == [f"🩺 réflexe {TARGET} depuis le dashboard (by=dashboard:{DEVICE})"]


def test_unknown_action_and_unsigned_analysis(dash):
    sid = session(dash)
    assert prepare(dash, sid, {"action": "reflexe-agir", "panne": TARGET})[0] == 409  # not an entry
    assert prepare(dash, sid, {"action": "sante-reflexes"})[0] == 409  # a reading has no sheet
    _, out, _ = prepare(dash, sid, {"action": "sante-analyse"})
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["run", "sante-serveur", "--requested-by", "dashboard"] and closing.TOKEN_ENV not in call["env"]


@pytest.mark.parametrize("node", [STRANGER_ID, SELF_ID, None])
def test_device_not_allowed_runs_nothing(dash, node):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "reflexe", "panne": TARGET})
    dash.node = node
    assert prepare(dash, sid, {"action": "reflexe", "panne": TARGET})[0] == 403
    assert prepare(dash, sid, {"action": "sante-analyse"})[0] == 403
    for id_ in ("sante-reflexes", "sante-garde", "sante-batterie"):
        assert request(dash, "POST", "/api/action/lire", {"action": id_}, cookie=sid)[0] == 403
    assert confirm(dash, sid, out["jeton"])[0] == 403
    dash.node = PHONE_ID
    assert dash.calls() == []


# -- the CLI side: reflexes.agir with the dashboard proof ---------------------------------------------------------------

def test_dashboard_proof_runs_the_tool_once_then_replay_refused(diagnosed, monkeypatch):
    close_key.load_or_create()
    monkeypatch.setenv(closing.TOKEN_ENV, dashboard_token("reflexe", TARGET))
    tool = FakeTool({"status": "error", "error": "conditions plus réunies, rien fait"})  # the diagnosis stays
    res = reflexes.agir(diagnosed, TARGET, run=tool)
    assert res["by"]["by"] == "dashboard:" + DEVICE and len(tool.calls) == 1
    with pytest.raises(closing.CloseRefused, match="déjà utilisé"):
        reflexes.agir(diagnosed, TARGET, run=tool)
    assert len(tool.calls) == 1


def test_dashboard_proof_runs_the_tool(diagnosed, monkeypatch):
    close_key.load_or_create()
    monkeypatch.setenv(closing.TOKEN_ENV, dashboard_token("reflexe", TARGET))
    tool = FakeTool()
    res = reflexes.agir(diagnosed, TARGET, run=tool)
    assert res["ok"] and tool.calls == [("restic_unlock_stale", ["6", "0123456789ab"])]


@pytest.mark.parametrize("signed", [("reflexe", "verrou-restic:ffffffffffff"), ("user", TARGET), ("fusion-lot", TARGET)])
def test_proof_for_another_fingerprint_or_action_refused(diagnosed, monkeypatch, signed):
    close_key.load_or_create()
    monkeypatch.setenv(closing.TOKEN_ENV, dashboard_token(*signed))
    tool = FakeTool()
    with pytest.raises(closing.CloseRefused):
        reflexes.agir(diagnosed, TARGET, run=tool)
    assert tool.calls == []


def test_fingerprint_changed_since_the_screen_does_nothing(diagnosed, monkeypatch):
    close_key.load_or_create()
    shown = "verrou-restic:ffffffffffff"  # the page showed an older diagnosis: the current one has another fingerprint
    monkeypatch.setenv(closing.TOKEN_ENV, dashboard_token("reflexe", shown))
    tool = FakeTool()
    res = reflexes.agir(diagnosed, shown, run=tool)
    assert not res["ok"] and "plus d'actualité" in res["message"] and tool.calls == []


def test_restart_after_merge_is_declared():
    plan = json.loads((ROOT / "deploy/user/apres-fusion/todo-247.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [
        ("user_unit_restart", ["ultron-hub-dashboard.service"]), ("user_unit_restart", ["hermes-gateway"])]
