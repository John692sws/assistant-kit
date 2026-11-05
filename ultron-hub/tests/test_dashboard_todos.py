"""Todo #242: the todos from the dashboard (list, card, edits, ✖ / ↩️, 🔀 Tout fusionner, 🧰 after a merge).

Each function is one entry of the catalogue (walked by test_dashboard_catalogue.py); here: the readings' whitelist, the
three new signed actions (cancel, fusion-lot, user) end to end with the real CLI, and their refusals (action outside the
list, replayed proof, changed fingerprint, device not allowed). No network, throwaway close.key.
"""

import json
import time
from pathlib import Path

import pytest

from hermes_plugin import close_key
from test_branch_lot import run_cli  # noqa: F401
from test_branches import branch, commit, no_bwrap, repo, sh, status  # noqa: F401  (fixtures)
from test_dashboard_actions import (PHONE_ID, SELF_ID, STRANGER_ID, confirm, dash, fake_cli, fresh_cache,  # noqa: F401
                                    prepare, request, session)
from ultron_hub import after_merge, branch_lot, branches, cli
from ultron_hub.dashboard import actions, apercu, catalogue
from ultron_hub.todo import closing
from ultron_hub.todo.store import TodoStore

ROOT = Path(__file__).parents[1]
DEVICE = "telephone-test"


def dashboard_token(action, target):
    return closing.sign_dashboard(closing.load_key(), DEVICE, action, target, int(time.time()))


# -- closing: exactly three more actions, the same on both sides ------------------------------------------------------

def test_dashboard_actions_grow_only_by_cancel_fusion_lot_and_user():  # (`approve`, todo #244, retired with the missions, todo #455),
    # `profil`, `savoir` (todo #249, test_dashboard_profil_savoir), `reflexe` (todo #247, test_dashboard_sante), `compte` (todo #263, test_compte)
    assert closing.DASHBOARD_ACTIONS[:5] == ("done", "merge", "cancel", "fusion-lot", "user")
    assert close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    signed = {a.signe for a in catalogue.CATALOGUE if a.type == "signee"}
    assert signed == set(closing.DASHBOARD_ACTIONS)  # nothing allowed that no entry uses
    for action in ("reopen", "root", "veille"):
        with pytest.raises(closing.CloseRefused):
            closing.sign_dashboard(b"k" * 64, DEVICE, action, "7", 1000)


def test_user_is_signed_only_on_an_after_merge_target():
    import dataclasses
    run = catalogue.PAR_ID["apres-run"]
    _, args, stdin = catalogue.valider("apres-run", {"id": "12", "n": "2"})
    assert catalogue.cible(run, args, stdin) == after_merge.target(12, 2) == "todo-12:2"
    with pytest.raises(catalogue.CatalogueError, match="todo-"):  # `user` only on « todo-<id>:<n> »
        catalogue.verifier((dataclasses.replace(run, cible="{id}:{n}"),))


def test_fusion_lot_target_is_the_fingerprint_and_the_lot_is_bounded():
    a = catalogue.PAR_ID["fusion-lot"]
    _, args, stdin = catalogue.valider("fusion-lot", {"ids": "81,82", "empreinte": "0123456789abcdef"})
    assert catalogue.cible(a, args, stdin) == "0123456789abcdef"
    assert catalogue.commande(a, args, stdin) == ["branch", "merge-lot", "--ids=81,82", "--empreinte=0123456789abcdef"]
    too_many = ",".join(str(i) for i in range(1, catalogue.LOT_MAX + 2))
    for body in ({"ids": too_many, "empreinte": "0123456789abcdef"}, {"ids": "81", "empreinte": "0123456789ABCDEF"},
                 {"ids": "81", "empreinte": "0123"}, {"ids": "0", "empreinte": "0123456789abcdef"}):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("fusion-lot", body)


# -- readings: whitelisted, whole or refused, never journaled, never signed ----------------------------------------------

def fake_run(monkeypatch, data, seen=None):
    def run(self, argv, extra, **kw):
        if seen is not None:
            seen.append((argv, extra))
        return {"status": "ok", "summary": "lu", "data": data}
    monkeypatch.setattr(actions.Actions, "_run", run)


def test_reading_gives_only_the_whitelist(dash, monkeypatch):
    seen = []
    fake_run(monkeypatch, {**apercu.LECTURES["todo-fiche"], "notes": "n", "chemin": "/home/exemple2", "jeton": "secret"}, seen)
    sid = session(dash)
    code, out, _ = request(dash, "POST", "/api/action/lire", {"action": "todo-fiche", "id": "901"}, cookie=sid)
    assert code == 200 and out["ok"]
    text = json.dumps(out)
    assert "secret" not in text and "/home/exemple2" not in text and "jamais montré" not in text
    assert out["donnees"]["history"][1]["details"] == {"cles": ["title"]}
    assert seen == [(["todo", "show", "901"], {})] and dash.journal == []


def test_too_long_a_reading_is_refused_whole(dash, monkeypatch):
    fake_run(monkeypatch, {"todo_id": 1, "requests": [{"n": 1, "status": "pending", "line": "x",
                                                       "text": "y" * (catalogue.DONNEES_MAX + 1)}]})
    sid = session(dash)
    code, out, _ = request(dash, "POST", "/api/action/lire", {"action": "apres", "id": "1"}, cookie=sid)
    assert code == 200 and not out["ok"] and "donnees" not in out


def test_list_filters_go_to_todo_board(dash, monkeypatch):
    seen = []
    fake_run(monkeypatch, apercu.LECTURES["todos"], seen)
    sid = session(dash)
    for body, argv in (({"filtre": "urgent"}, ["todo", "board", "--filter", "urgent"]),
                       ({"filtre": "closed", "categorie": "perso"}, ["todo", "board", "--filter", "closed", "--category=perso"]),
                       ({}, ["todo", "board", "--filter", "open"])):
        assert request(dash, "POST", "/api/action/lire", {"action": "todos", **body}, cookie=sid)[0] == 200
        assert seen[-1][0] == argv
    assert request(dash, "POST", "/api/action/lire", {"action": "todos", "filtre": "tout"}, cookie=sid)[0] == 409


def test_preview_readings_use_the_same_whitelist():
    acts = apercu.PreviewActions(cli=["false"], key=apercu._no_key)
    for action_id in apercu.LECTURES:
        champs = {c.nom: c.exemple for c in catalogue.PAR_ID[action_id].champs}
        out = acts.read("aperçu", "s", {"action": action_id, "id": "901", **champs})
        assert out["ok"] and out["donnees"] == catalogue.projeter(catalogue.PAR_ID[action_id].donnees,
                                                                    apercu.LECTURES[action_id])
    assert "jamais montré" not in json.dumps(acts.read("aperçu", "s", {"action": "todo-fiche", "id": "1"}))


# -- ✖ Abandonner (signed `cancel`) --------------------------------------------------------------------------------------

def test_cancel_is_signed_on_the_number_shown(dash):
    sid = session(dash)
    code, out, _ = prepare(dash, sid, {"action": "abandon", "id": "7"})
    assert code == 200 and out["danger"] and out["sur_quoi"] == "#7"
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 200 and res["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["todo", "cancel", "7"]
    assert closing.check(call["env"][closing.TOKEN_ENV], "cancel", "7")[1] == "dashboard:" + DEVICE
    with pytest.raises(closing.CloseRefused):  # bound to this todo
        closing.check(call["env"][closing.TOKEN_ENV], "cancel", "8")
    assert confirm(dash, sid, out["jeton"])[0] == 409 and len(dash.calls()) == 1  # the sheet's token is spent


@pytest.mark.parametrize("node", [STRANGER_ID, SELF_ID, None])
def test_device_not_allowed_runs_nothing(dash, node):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "abandon", "id": "7"})
    dash.node = node
    for body in ({"action": "fusion-lot", "ids": "1", "empreinte": "0123456789abcdef"}, {"action": "abandon", "id": "7"}):
        assert prepare(dash, sid, body)[0] == 403
    assert request(dash, "POST", "/api/action/lire", {"action": "lot"}, cookie=sid)[0] == 403
    assert confirm(dash, sid, out["jeton"])[0] == 403
    dash.node = PHONE_ID
    assert dash.calls() == []


def test_cancel_with_the_dashboard_proof_then_replay_refused(project, monkeypatch):
    from ultron_hub.common.settings import load_settings
    close_key.load_or_create()
    assert cli.main(["todo", "add", "--title", "Essai", "--clair", "essai"]) == 0
    token = dashboard_token("cancel", "1")
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    assert cli.main(["todo", "cancel", "1"]) == 0
    with TodoStore(load_settings().todo_db) as store:
        assert store.get(1).status == "cancelled"
        assert any((e.get("details") or {}).get("by") == "dashboard:" + DEVICE for e in store.history(1))
    monkeypatch.delenv(closing.TOKEN_ENV)
    assert cli.main(["todo", "reopen", "1"]) == 0  # ↩️ Rouvrir: no proof needed (simple entry)
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    assert cli.main(["todo", "cancel", "1"]) != 0  # the same proof never serves twice
    monkeypatch.setenv(closing.TOKEN_ENV, dashboard_token("done", "1"))
    assert cli.main(["todo", "cancel", "1"]) != 0  # a ✅ proof is not a ✖ proof
    with TodoStore(load_settings().todo_db) as store:
        assert store.get(1).status == "todo"


# -- 🔀 Tout fusionner (signed `fusion-lot` on the fingerprint shown) ------------------------------------------------------

def test_fusion_lot_with_the_dashboard_proof(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setattr(branches, "restart_gateway", lambda: None)
    close_key.load_or_create()
    branch(repo, "todo-1/a", "a.txt")
    emp = run_cli(capsys, "branch", "lot")[1]["data"]["empreinte"]
    monkeypatch.setenv(closing.TOKEN_ENV, dashboard_token("fusion-lot", "0" * 16))  # another lot
    assert run_cli(capsys, "branch", "merge-lot", "--ids=1", f"--empreinte={emp}")[0] == 1
    monkeypatch.setenv(closing.TOKEN_ENV, dashboard_token("merge", "1"))  # a 🔀 proof is not a lot proof
    assert run_cli(capsys, "branch", "merge-lot", "--ids=1", f"--empreinte={emp}")[0] == 1
    assert status(repo, 1) == "todo"
    token = dashboard_token("fusion-lot", emp)
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    code, out = run_cli(capsys, "branch", "merge-lot", "--ids=1", f"--empreinte={emp}")
    assert code == 0 and status(repo, 1) == "done"
    with TodoStore(repo.todo_db) as todos:
        [event] = [h for h in todos.history(1) if h["action"] == "status"]
    assert event["details"]["by"] == "dashboard:" + DEVICE
    proof = token.rsplit(".", 1)[1][:16]
    with TodoStore(repo.todo_db) as todos:  # replay: the proof is recorded on the todo
        assert todos.proof_used(1, proof)
    with pytest.raises(closing.CloseRefused, match="déjà utilisé"):
        closing.authorize("fusion-lot", emp, human=False, what="", used=lambda p: p == proof, env={closing.TOKEN_ENV: token})


def test_fusion_lot_refused_when_a_branch_moved_after_the_screen(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setattr(branches, "restart_gateway", lambda: None)
    close_key.load_or_create()
    branch(repo, "todo-1/a", "a.txt")
    shown = branch_lot.lot(repo)["empreinte"]
    before = sh(repo.root, "rev-parse", "main")
    sh(repo.root, "switch", "-q", "todo-1/a")
    commit(repo.root, "a2.txt", "z\n", "encore")
    sh(repo.root, "switch", "-q", "main")
    monkeypatch.setenv(closing.TOKEN_ENV, dashboard_token("fusion-lot", shown))  # signed on what was shown
    code, out = run_cli(capsys, "branch", "merge-lot", "--ids=1", f"--empreinte={shown}")
    assert code == 1 and status(repo, 1) == "todo" and sh(repo.root, "rev-parse", "main") == before


def test_fusion_lot_end_to_end_signs_the_fingerprint(dash):
    sid = session(dash)
    code, out, _ = prepare(dash, sid, {"action": "fusion-lot", "ids": "81,82", "empreinte": "0123456789abcdef"})
    assert code == 200 and "81,82" in out["sur_quoi"] and "0123456789abcdef" in out["sur_quoi"]
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["branch", "merge-lot", "--ids=81,82", "--empreinte=0123456789abcdef"]
    assert closing.check(call["env"][closing.TOKEN_ENV], "fusion-lot", "0123456789abcdef")[1] == "dashboard:" + DEVICE


# -- 🧰 ✅ Exécuter after a merge (signed `user` on todo-<id>:<n>) ---------------------------------------------------------

def test_after_run_proof_is_bound_to_the_action_shown(dash):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "apres-run", "id": "12", "n": "2"})
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["branch", "after-run", "12", "2"]
    token = call["env"][closing.TOKEN_ENV]
    by = closing.authorize("user", after_merge.target(12, 2), human=False, what="", used=lambda p: False,
                           env={closing.TOKEN_ENV: token})
    assert by["by"] == "dashboard:" + DEVICE
    for other in (after_merge.target(12, 1), after_merge.target(13, 2), "m-20261019:2"):
        with pytest.raises(closing.CloseRefused):
            closing.authorize("user", other, human=False, what="", used=lambda p: False, env={closing.TOKEN_ENV: token})
    with pytest.raises(closing.CloseRefused, match="déjà utilisé"):
        closing.authorize("user", after_merge.target(12, 2), human=False, what="", used=lambda p: True,
                          env={closing.TOKEN_ENV: token})


def test_edits_are_not_signed(dash):
    for body in ({"action": "rouvrir", "id": "7"},
                 {"action": "priorite", "id": "7", "priorite": "high"}, {"action": "tete", "id": "7"}):
        sid = session(dash)
        _, out, _ = prepare(dash, sid, body)
        assert confirm(dash, sid, out["jeton"])[1]["ok"]
        assert closing.TOKEN_ENV not in dash.calls()[-1]["env"]


def test_restart_after_merge_is_declared():
    plan = json.loads((ROOT / "deploy/user/apres-fusion/todo-242.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]


# -- todo #243: 🗂 Ranger (proposal read, then applied by its fingerprint) and ✖ Abandonner one by one (ménage) -------------

def test_ranger_reads_the_proposal_then_applies_its_fingerprint_unsigned(dash, monkeypatch):
    seen = []
    fake_run(monkeypatch, apercu.LECTURES["ranger-proposition"], seen)
    sid = session(dash)
    code, out, _ = request(dash, "POST", "/api/action/lire", {"action": "ranger-proposition"}, cookie=sid)
    assert code == 200 and seen[-1][0] == ["todo", "rattacher", "--proposer"]
    assert out["donnees"]["empreinte"] == "0123abcd" and out["donnees"]["lines"][0]["vers"] == "fba"
    _, out, _ = prepare(dash, sid, {"action": "ranger-appliquer", "empreinte": "0123abcd"})
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    argv, env = seen[-1][0], seen[-1][1]
    assert argv == ["todo", "rattacher", "--appliquer=0123abcd", "--by", "dashboard"]
    assert closing.TOKEN_ENV not in (env or {})


def test_menage_signs_each_todo_on_its_own_number(dash):
    sid = session(dash)
    for n in ("7", "8", "9"):  # the page chains the sheets: one proof per todo, no new action in closing
        _, out, _ = prepare(dash, sid, {"action": "abandon", "id": n})
        assert confirm(dash, sid, out["jeton"])[1]["ok"]
    assert [c["argv"] for c in dash.calls()] == [["todo", "cancel", n] for n in ("7", "8", "9")]
    for call, n in zip(dash.calls(), ("7", "8", "9")):
        assert closing.check(call["env"][closing.TOKEN_ENV], "cancel", n)[1] == "dashboard:" + DEVICE


def test_menage_propose_is_one_proof_on_the_fingerprint(dash):  # todo #362
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "menage-lot", "empreinte": "0123456789abcdef"})
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["menage", "appliquer", "--empreinte=0123456789abcdef"]
    assert closing.check(call["env"][closing.TOKEN_ENV], "menage-lot", "0123456789abcdef")[1] == "dashboard:" + DEVICE
