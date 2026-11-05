"""Todo #128: 🔀 Tout fusionner, every ready todo merged on one signed button (stacked branches parent first)."""

import asyncio
import json

import pytest

from cli_contract import check_argv
from hermes_plugin import close_key
from ultron_hub import after_merge, branch_commands, branch_lot, branches, cli, user_actions
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.todo import closing
from ultron_hub.todo.store import TodoStore

from test_branches import PASSING, branch, commit, no_bwrap, repo, sh, status  # noqa: F401  (fixtures)

FAILING = "def test_ko():\n    assert False\n"


def stacked(settings, parent="todo-4/base", child="todo-3/suite"):
    """`child` built on `parent` (the agent branched from the other todo's branch)."""
    branch(settings, parent, "base.txt")
    sh(settings.root, "switch", "-qc", child, parent)
    commit(settings.root, "suite.txt", "y\n", f"travail sur {child} (todo #{child.split('/')[0].removeprefix('todo-')})")
    sh(settings.root, "switch", "-q", "main")


def merge(settings, ids, restarts):
    data = branch_lot.lot(settings)
    data = branch_lot.check(settings, ids, data["empreinte"])
    return branch_lot.run(settings, data, by={"by": "test"}, restart=lambda: restarts.append(1))


# -- the lot ------------------------------------------------------------------------------------------

def test_stacked_branches_are_merged_parent_first_with_one_restart(repo):
    stacked(repo)  # #3 is stacked on #4: #4 must go first even though 3 < 4
    data = branch_lot.lot(repo)
    assert data["ids"] == [4, 3] and data["lot"][1]["apres"] == [4] and data["lot"][0]["apres"] == []
    assert len(data["empreinte"]) == 16 and data["ecartees"] == []
    restarts = []
    r = merge(repo, [3, 4], restarts)
    assert [m["todo_id"] for m in r["results"]] == [4, 3] and r["stopped"] is None and r["remaining"] == []
    assert restarts == [1] and r["restart"] == "planned"  # once for the whole lot
    assert all(m["restart"] is None for m in r["results"])
    assert status(repo, 3) == status(repo, 4) == "done"
    assert (repo.root / "base.txt").is_file() and (repo.root / "suite.txt").is_file()
    log = sh(repo.root, "log", "--first-parent", "--format=%s", "main")
    assert log.index("todo-3/suite") < log.index("todo-4/base")  # #4 merged before #3
    assert "2/2" in branch_commands.lot_text(r) and "redémarre" in branch_commands.lot_text(r)


def test_independent_todos_keep_their_number_order(repo):
    branch(repo, "todo-2/b", "b.txt")
    branch(repo, "todo-1/a", "a.txt")
    assert branch_lot.lot(repo)["ids"] == [1, 2]


def test_todo_refused_by_its_tests_lets_the_others_merge(repo):
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/b", "tests/test_b.py", FAILING)
    branch(repo, "todo-3/c", "c.txt")
    restarts = []
    r = merge(repo, [1, 2, 3], restarts)  # todo #378: #2 is tried again after #3, still red
    assert [m["todo_id"] for m in r["results"]] == [1, 3]
    assert r["stopped"]["todo_id"] == 2 and r["stopped"]["result"]["check"]["stage"] == "tests"
    assert r["remaining"] == [] and restarts == [1]
    assert (status(repo, 1), status(repo, 2), status(repo, 3)) == ("done", "todo", "done")
    text = branch_commands.lot_text(r)
    assert "2/3" in text and "Arrêt à #2" in text and "les tests échouent" in text


def test_branch_repairing_main_with_the_highest_number_unblocks_the_lot(repo):
    commit(repo.root, "tests/test_red.py", FAILING, "main red")  # todo #378: the lot of 24/10 stopped at once
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/b", "b.txt")
    branch(repo, "todo-3/repare", "tests/test_red.py", PASSING)
    r = merge(repo, [1, 2, 3], [])
    assert [m["todo_id"] for m in r["results"]] == [3, 1, 2] and r["stopped"] is None and r["remaining"] == []
    assert (status(repo, 1), status(repo, 2), status(repo, 3)) == ("done", "done", "done")


def test_child_waits_for_its_parent_refused_by_its_tests(repo):
    branch(repo, "todo-4/base", "tests/test_base.py", FAILING)
    sh(repo.root, "switch", "-qc", "todo-3/suite", "todo-4/base")
    commit(repo.root, "suite.txt", "y\n", "travail sur todo-3/suite (todo #3)")
    sh(repo.root, "switch", "-q", "main")
    branch(repo, "todo-5/c", "c.txt")
    r = merge(repo, [3, 4, 5], [])
    assert [m["todo_id"] for m in r["results"]] == [5]
    assert r["stopped"]["todo_id"] == 4 and r["remaining"] == [3] and status(repo, 3) == "todo"


def test_refusal_raised_by_merge_and_close_stops_the_lot(repo, monkeypatch):
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/b", "b.txt")
    data = branch_lot.lot(repo)
    real = branches.merge_and_close

    def refuse_two(settings, todo_id, **kw):
        if todo_id == 2:
            raise InvalidRequestError("❌ Fusion refusée : essai")
        return real(settings, todo_id, **kw)
    monkeypatch.setattr(branches, "merge_and_close", refuse_two)
    r = branch_lot.run(repo, data, by=None, restart=lambda: None)
    assert r["stopped"] == {"todo_id": 2, "refusal": "❌ Fusion refusée : essai"} and status(repo, 2) == "todo"
    assert "❌ Fusion refusée : essai" in branch_commands.lot_text(r)


def test_moved_branch_after_the_card_is_refused_and_nothing_merged(repo):
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/b", "b.txt")
    shown = branch_lot.lot(repo)
    before = sh(repo.root, "rev-parse", "main")
    sh(repo.root, "switch", "-q", "todo-2/b")
    commit(repo.root, "b2.txt", "z\n", "encore")
    sh(repo.root, "switch", "-q", "main")
    with pytest.raises(InvalidRequestError, match="a bougé"):
        branch_lot.check(repo, [1, 2], shown["empreinte"])
    assert sh(repo.root, "rev-parse", "main") == before and status(repo, 1) == "todo"


def test_branch_moving_during_the_lot_stops_before_it(repo):
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/b", "b.txt")
    data = branch_lot.check(repo, [1, 2], branch_lot.lot(repo)["empreinte"])
    sh(repo.root, "branch", "-f", "todo-2/b", "todo-1/a")  # moved between the check and its turn
    r = branch_lot.run(repo, data, by=None, restart=lambda: None)
    assert [m["todo_id"] for m in r["results"]] == [1] and r["stopped"]["todo_id"] == 2
    assert "a bougé" in r["stopped"]["refusal"] and status(repo, 2) == "todo"


def test_branch_stacked_on_an_old_head_waits_for_the_moved_parent(repo):
    """Todo #467 (lot of 28/10 18h59): #3 forked from an old head of todo-4, which then went on (test fix). Neither
    contains the other and both are 2 commits ahead: the shared commits are #4's, so #4 is the parent, not #3."""
    stacked(repo)
    sh(repo.root, "switch", "-q", "todo-4/base")
    commit(repo.root, "base.txt", "corrigé\n", "correctif des tests (todo #4)")
    sh(repo.root, "switch", "-q", "main")
    data = branch_lot.lot(repo)
    assert data["ids"] == [4, 3] and data["lot"][1]["apres"] == [4] and data["ecartees"] == []
    restarts = []
    r = merge(repo, [3, 4], restarts)
    assert [m["todo_id"] for m in r["results"]] == [4, 3] and r["stopped"] is None
    assert (repo.root / "base.txt").read_text() == "corrigé\n"


def test_todo_taken_out_of_the_lot_takes_its_stacked_children_with_it(repo):
    stacked(repo)
    branch(repo, "todo-1/a", "a.txt")
    data = branch_lot.lot(repo, sans=[4])
    assert data["ids"] == [1]
    why = {e["todo_id"]: e["raison"] for e in data["ecartees"]}
    assert why == {4: "retirée du lot", 3: "empilée sur #4, qui n'est pas dans le lot"}
    assert data["empreinte"] != branch_lot.lot(repo)["empreinte"]  # the signature is on the final lot
    with pytest.raises(InvalidRequestError, match="#3 n'est plus prête : empilée sur #4"):
        branch_lot.check(repo, [3], data["empreinte"])
    restarts = []
    r = branch_lot.run(repo, branch_lot.check(repo, [1], data["empreinte"]), by=None,
                       restart=lambda: restarts.append(1))
    assert [m["todo_id"] for m in r["results"]] == [1] and status(repo, 4) == status(repo, 3) == "todo"


def test_conflict_with_two_others_drops_only_that_one(repo):
    branch(repo, "todo-1/a", "x.txt", "a\n")
    sh(repo.root, "switch", "-q", "todo-1/a")
    commit(repo.root, "y.txt", "a\n", "y")
    sh(repo.root, "switch", "-q", "main")
    branch(repo, "todo-2/b", "x.txt", "b\n")  # conflicts with #1 (x.txt)
    branch(repo, "todo-3/c", "y.txt", "c\n")  # conflicts with #1 (y.txt)
    data = branch_lot.lot(repo)
    assert data["ids"] == [2, 3]
    assert [(e["todo_id"], e["raison"]) for e in data["ecartees"]] == [
        (1, "conflit avec #2 (x.txt) : à rebaser après la fusion")]
    with pytest.raises(InvalidRequestError, match="#1 n'est plus prête : conflit avec #2"):
        branch_lot.check(repo, [1, 2, 3], data["empreinte"])
    r = merge(repo, [2, 3], [])
    assert [m["todo_id"] for m in r["results"]] == [2, 3] and r["stopped"] is None


def test_conflict_with_main_itself_is_named(repo):
    branch(repo, "todo-1/a", "x.txt", "a\n")  # branched before main changes x.txt
    commit(repo.root, "x.txt", "main\n", "main moves on")
    data = branch_lot.lot(repo)
    assert data["ids"] == []
    assert [(e["todo_id"], e["raison"]) for e in data["ecartees"]] == [
        (1, "conflit avec main (x.txt) : à rebaser sur main")]


def test_conflict_free_order_is_found_by_the_larger_set(repo):
    branch(repo, "todo-1/a", "s.txt", "1\n")
    branch(repo, "todo-2/b", "s.txt", "2\n")  # without the exhaustive search #1 first would stop at #2
    branch(repo, "todo-3/c", "s.txt", "3\n")
    branch(repo, "todo-4/d", "d.txt")
    data = branch_lot.lot(repo)
    assert data["ids"] == [1, 4]
    assert {e["todo_id"] for e in data["ecartees"]} == {2, 3}


def test_greedy_beyond_eight_todos_takes_the_fewest_conflicts_first(repo):
    with TodoStore(repo.todo_db) as todos:
        for _ in range(5):  # todos 6 to 10
            todos.add(title="tâche", category="serveur")
    branch(repo, "todo-1/a", "s.txt", "1\n")
    branch(repo, "todo-2/b", "s.txt", "2\n")
    branch(repo, "todo-3/c", "s.txt", "3\n")
    for i in range(4, 11):
        branch(repo, f"todo-{i}/n", f"n{i}.txt")
    data = branch_lot.lot(repo)
    assert data["ids"] == [1, 4, 5, 6, 7, 8, 9, 10]
    assert {e["todo_id"] for e in data["ecartees"]} == {2, 3}


def test_stacked_child_follows_its_conflicting_parent(repo):
    stacked(repo)  # #3 on #4 (base.txt, suite.txt)
    branch(repo, "todo-1/a", "base.txt", "autre\n")
    data = branch_lot.lot(repo)
    assert data["ids"] == [4, 3]  # biggest set: #4 and #3 together, #1 out
    assert [e["todo_id"] for e in data["ecartees"]] == [1]


def test_not_ready_todos_are_left_out_with_a_reason(repo):
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/a", "a2.txt")
    branch(repo, "todo-2/b", "b2.txt")  # two branches in one repository
    branch(repo, "todo-3/c", "c.txt")
    wt = repo.root.parent / "wt-3"
    sh(repo.root, "worktree", "add", "-q", str(wt), "todo-3/c")
    (wt / ".venv").write_text("lien\n")  # an untracked file is enough
    sh(repo.root, "branch", "todo-4/vide", "main")  # nothing to merge: not listed
    branch(repo, "todo-5/close", "e.txt")
    with TodoStore(repo.todo_db) as todos:
        todos.set_status(5, "cancelled")
    data = branch_lot.lot(repo)
    assert data["ids"] == [1]
    why = {e["todo_id"]: e["raison"] for e in data["ecartees"]}
    assert why[2].startswith("plusieurs branches") and why[3].startswith("worktree non propre")
    assert set(why) == {2, 3}


def test_lot_across_repositories_without_restart_when_ultron_hub_is_untouched(repo):
    other = repo.root.parent / "autre"
    other.mkdir()
    sh(other, "init", "-q", "-b", "main")
    sh(other, "config", "user.name", "t"); sh(other, "config", "user.email", "t@t")
    commit(other, "app.txt", "v1\n", "init")
    branch(repo, "todo-2/x", "app2.txt")  # in the project itself
    sh(other, "switch", "-qc", "todo-1/y", "main")
    commit(other, "app.txt", "v2\n", "travail (todo #1)")
    sh(other, "switch", "-q", "main")
    data = branch_lot.lot(repo)
    assert data["ids"] == [1, 2] and data["lot"][0]["repos"][0]["label"] == "todo-1/y (autre)"
    restarts = []
    r = branch_lot.run(repo, branch_lot.check(repo, [1], branch_lot.lot(repo, sans=[2])["empreinte"]), by=None,
                       restart=lambda: restarts.append(1))
    assert r["results"][0]["merged"] and restarts == [] and r["restart"] is None
    assert (other / "app.txt").read_text() == "v2\n" and status(repo, 1) == "done"


def test_parse_ids():
    assert branch_lot.parse_ids("84,81,81") == [81, 84]
    for bad in ("", "8 1", "81,", "-1", "81\n"):
        with pytest.raises(InvalidRequestError):
            branch_lot.parse_ids(bad)


# -- after-merge actions covered by the lot's ✅ (todo #182) ---------------------------------------------

DECLARATION = json.dumps({"actions": [
    {"action": "hermes_menu_add", "args": ["atoi"], "reason": "nouvelle commande"},
    {"action": "hermes_config_set", "args": ["approvals.mode", "off"], "reason": "tentative"},
    {"action": "user_unit_restart", "args": ["hermes-gateway"], "reason": "charger le menu"},
]})
SIGNED = {"by": "telegram-merge", "proof": "0123456789abcdef"}


@pytest.fixture
def ran(monkeypatch):
    """No real ultron-user: every action that reaches the tool is recorded here (and nothing else may run)."""
    calls = []
    monkeypatch.setattr(user_actions, "run", lambda action, args: calls.append((action, args)) or
                        {"status": "ok", "output": "ok"})
    monkeypatch.setattr(after_merge.decide, "__defaults__", (user_actions.run, None))
    return calls


def declared(settings, tid=1, content=DECLARATION):
    branch(settings, f"todo-{tid}/x", after_merge.declaration_path(tid), content)


def launcher(settings, events):
    """Stands for after_merge.launch_unit (no systemd-run): runs the approved action inline."""
    def launch(s, tid, n):
        events.append(("action", tid, n, status(settings, 2)))
        after_merge.execute(s, tid, n, run_user=user_actions.run)
    return launch


def test_lot_reads_the_declaration_on_the_branch_and_signs_it(repo):
    declared(repo, 1)
    branch(repo, "todo-2/b", "b.txt")
    data = branch_lot.lot(repo)
    acts = data["lot"][0]["actions"]
    assert [(a["n"], a["status"], a["plan"]) for a in acts] == [
        (1, "pending", "lancée"), (2, "invalid", "jamais"), (3, "pending", "redémarrage du lot")]
    assert acts[0]["command"] == user_actions.preview("hermes_menu_add", ["atoi"]) and acts[1]["command"] is None
    assert data["lot"][1]["actions"] == [] and data["lot"][0]["actions_error"] is None
    bare = [{**t, "actions": []} for t in data["lot"]]
    assert data["empreinte"] != branch_lot.empreinte(bare)  # the actions are in the signed fingerprint
    changed = json.loads(json.dumps(data["lot"]))
    changed[0]["actions"][0]["args"] = ["autre"]
    assert branch_lot.empreinte(changed) != data["empreinte"]
    changed = json.loads(json.dumps(data["lot"]))
    changed[0]["actions"][0]["command"] = ["/bin/autre"]
    assert branch_lot.empreinte(changed) != data["empreinte"]


def test_unreadable_declaration_is_shown_and_launches_nothing(repo):
    declared(repo, 1, "{pas du json")
    item = branch_lot.lot(repo)["lot"][0]
    assert item["actions"] == [] and "illisible" in item["actions_error"]


def test_action_changed_between_card_and_press_is_refused(repo, monkeypatch, ran):
    declared(repo, 1)
    shown = branch_lot.lot(repo)
    before = sh(repo.root, "rev-parse", "main")
    real = user_actions.preview
    monkeypatch.setattr(user_actions, "preview", lambda a, b: real(a, b) + ["et autre chose"])  # the tool changed
    with pytest.raises(InvalidRequestError, match="action après fusion a changé"):
        branch_lot.check(repo, [1], shown["empreinte"])
    assert sh(repo.root, "rev-parse", "main") == before and status(repo, 1) == "todo" and ran == []


def test_one_press_merges_then_runs_the_actions_with_one_restart(repo, ran):
    declared(repo, 1)
    branch(repo, "todo-2/b", "b.txt")
    events = []
    data = branch_lot.check(repo, [1, 2], branch_lot.lot(repo)["empreinte"])
    r = branch_lot.run(repo, data, by=SIGNED, restart=lambda: events.append(("restart",)),
                       launch=launcher(repo, events))
    assert r["stopped"] is None and [m["todo_id"] for m in r["results"]] == [1, 2]
    # fusions first (#2 already done when the action starts), then the action, then ONE restart
    assert events == [("action", 1, 1, "done"), ("restart",)]
    assert ran == [("hermes_menu_add", ["atoi"])]  # the invalid one never runs, the gateway restart is folded
    state = after_merge.load(repo, 1)["actions"]
    assert [a["status"] for a in state] == ["done", "invalid", "done"]
    assert state[2]["output"] == branch_lot.LOT_RESTART_OUTPUT
    for a in (state[0], state[2]):
        assert a["approval"]["by"] == "telegram-merge" and a["approval"]["via"] == "fusion-lot"
        assert a["approval"]["empreinte"] == data["empreinte"]
        assert a["approval"]["proof"] == f"{SIGNED['proof']}:{a['n']}"
    assert r["results"][0]["after_merge"] == state
    text = branch_commands.lot_text(r)
    assert "#1 hermes_menu_add atoi : ✅ faite" in text and "à valider" not in text


def test_gateway_restart_stays_pending_if_the_lot_restart_fails(repo, ran):
    declared(repo, 1, json.dumps({"actions": [{"action": "user_unit_restart", "args": ["hermes-gateway"]}]}))
    data = branch_lot.check(repo, [1], branch_lot.lot(repo)["empreinte"])

    def broken():
        raise RuntimeError("systemd absent")
    r = branch_lot.run(repo, data, by=SIGNED, restart=broken, launch=launcher(repo, []))
    assert r["restart"].startswith("échec") and after_merge.load(repo, 1)["actions"][0]["status"] == "pending"
    assert ran == []


def test_refusal_in_the_middle_launches_no_action(repo, ran):
    declared(repo, 1)
    branch(repo, "todo-2/b", "tests/test_b.py", FAILING)
    declared(repo, 3)
    events = []
    data = branch_lot.check(repo, [1, 2, 3], branch_lot.lot(repo)["empreinte"])
    r = branch_lot.run(repo, data, by=SIGNED, restart=lambda: events.append(("restart",)),
                       launch=launcher(repo, events))
    assert r["stopped"]["todo_id"] == 2 and events == [("restart",)] and ran == []
    assert [a["status"] for a in after_merge.load(repo, 1)["actions"]] == ["pending", "invalid", "pending"]
    assert "done" not in {a["status"] for a in after_merge.load(repo, 3)["actions"]}  # merged (#378), nothing launched
    assert "🧰 2 action(s) serveur à valider après la fusion de #1" in branch_commands.lot_text(r)


def test_no_signature_no_launch(repo, ran):
    declared(repo, 1)
    data = branch_lot.check(repo, [1], branch_lot.lot(repo)["empreinte"])
    branch_lot.run(repo, data, by=None, restart=lambda: None, launch=launcher(repo, []))
    assert ran == [] and after_merge.load(repo, 1)["actions"][0]["status"] == "pending"


def test_declaration_different_on_main_is_left_pending(repo, monkeypatch, ran):
    declared(repo, 1)
    data = branch_lot.check(repo, [1], branch_lot.lot(repo)["empreinte"])
    other = json.dumps({"actions": [{"action": "hermes_menu_add", "args": ["plus"], "reason": "x"}]})
    monkeypatch.setattr(branches, "show_main", lambda root, path: other)  # what the merge left on main
    r = branch_lot.run(repo, data, by=SIGNED, restart=lambda: None, launch=launcher(repo, []))
    assert ran == [] and after_merge.load(repo, 1)["actions"][0]["status"] == "pending"
    assert "différentes de la carte" in branch_commands.lot_text(r)


def test_tool_changed_during_the_lot_launches_nothing(repo, monkeypatch, ran):
    declared(repo, 1)
    data = branch_lot.check(repo, [1], branch_lot.lot(repo)["empreinte"])
    digests = iter(["avant", "après"])
    monkeypatch.setattr(branch_lot, "_tool_digest", lambda: next(digests))
    r = branch_lot.run(repo, data, by=SIGNED, restart=lambda: None, launch=launcher(repo, []))
    assert ran == [] and "ultron-user a changé" in branch_commands.lot_text(r)
    assert after_merge.load(repo, 1)["actions"][2]["status"] == "pending"  # gateway restart not marked either


# -- CLI: signed on the lot ---------------------------------------------------------------------------

def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cli_lot_and_signed_merge_lot(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setattr(branches, "restart_gateway", lambda: None)
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/b", "b.txt")
    code, out = run_cli(capsys, "branch", "lot", "--sans", "2")
    assert code == 0 and out["data"]["ids"] == [1]
    emp = out["data"]["empreinte"]
    code, out = run_cli(capsys, "branch", "merge-lot", "--ids", "1", "--empreinte", emp)
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "merge-lot" in out["error"]
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("merge", 1))  # a 🔀 token is not a lot token
    assert run_cli(capsys, "branch", "merge-lot", "--ids", "1", "--empreinte", emp)[0] == 1
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("fusion-lot", "0" * 16))  # another lot
    assert run_cli(capsys, "branch", "merge-lot", "--ids", "1", "--empreinte", emp)[0] == 1
    assert status(repo, 1) == "todo"
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("fusion-lot", emp))
    code, out = run_cli(capsys, "branch", "merge-lot", "--ids", "1", "--empreinte", emp)
    assert code == 0 and out["data"]["results"][0]["closed"] and "1/1" in out["data"]["text"]
    assert status(repo, 1) == "done" and status(repo, 2) == "todo"
    with TodoStore(repo.todo_db) as todos:
        [event] = [h for h in todos.history(1) if h["action"] == "status"]
    assert event["details"]["by"] == "telegram-merge" and event["details"]["proof"]
    code, out = run_cli(capsys, "branch", "merge-lot", "--ids", "1", "--empreinte", emp)  # replayed
    assert code == 1 and "n'est plus prête" in out["error"]


def test_cli_merge_lot_runs_actions_only_with_the_right_signature(repo, capsys, monkeypatch, ran):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setattr(branches, "restart_gateway", lambda: None)
    launched = []
    monkeypatch.setattr(after_merge, "launch_unit", lambda s, t, n: launched.append((t, n)) or
                        after_merge.execute(s, t, n, run_user=user_actions.run))
    declared(repo, 1)
    emp = run_cli(capsys, "branch", "lot")[1]["data"]["empreinte"]
    bare = branch_lot.empreinte([{**t, "actions": []} for t in branch_lot.lot(repo)["lot"]])
    for token in (None, close_key.token("fusion-lot", bare), close_key.token("user", "todo-1:1")):
        if token:
            monkeypatch.setenv(closing.TOKEN_ENV, token)
        code, out = run_cli(capsys, "branch", "merge-lot", "--ids", "1", "--empreinte", emp)
        assert code == 1 and status(repo, 1) == "todo" and launched == [] and ran == []
    code, out = run_cli(capsys, "branch", "merge-lot", "--ids", "1", "--empreinte", bare)  # card without actions
    assert code == 1 and "a changé" in out["error"] and status(repo, 1) == "todo"
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("fusion-lot", emp))
    code, out = run_cli(capsys, "branch", "merge-lot", "--ids", "1", "--empreinte", emp)
    assert code == 0 and launched == [(1, 1)] and ran == [("hermes_menu_add", ["atoi"])]
    assert [a["status"] for a in out["data"]["results"][0]["after_merge"]] == ["done", "invalid", "done"]


def test_cli_merge_lot_bad_fingerprint(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    code, out = run_cli(capsys, "branch", "merge-lot", "--ids", "1", "--empreinte", "XYZ")
    assert code == 1 and "empreinte invalide" in out["error"]


def test_plugin_and_cli_know_the_lot_action():
    assert "fusion-lot" in closing.ACTIONS and close_key.ACTIONS == closing.ACTIONS
    key = b"k" * 64
    assert close_key.token("fusion-lot", "0123456789abcdef", key=key, now=1000) == \
        closing.sign(key, "fusion-lot", "0123456789abcdef", 1000)


def test_lot_leaves_out_a_stacked_branch_without_own_commit(repo):
    """todo #286: the child is « en cours » (not even listed as left out), its parent stays in the lot."""
    branch(repo, "todo-4/base", "base.txt")
    sh(repo.root, "branch", "todo-3/suite", "todo-4/base")
    data = branch_lot.lot(repo)
    assert data["ids"] == [4] and data["ecartees"] == []


# -- todo #401: the card says what will be merged, the report says what was and why ---------------------------------

def _journal(esprit_dir, settings):
    from ultron_hub.esprit import store as esprit
    return [e["texte"] for e in esprit.tail(settings, 50)]


def test_card_gives_branch_commits_and_files_touched_in_order(repo):
    branch(repo, "todo-4/base", "base.txt")
    sh(repo.root, "switch", "-q", "todo-4/base")
    commit(repo.root, "base2.txt", "z\n", "encore (todo #4)")
    sh(repo.root, "switch", "-qc", "todo-3/suite")  # #3 stacked on #4
    commit(repo.root, "suite.txt", "y\n", "travail sur todo-3/suite (todo #3)")
    sh(repo.root, "switch", "-q", "main")
    data = branch_lot.lot(repo)
    assert data["ids"] == [4, 3]
    four, three = data["lot"]
    assert (four["ahead"], four["files"], four["repos"][0]["files"]) == (2, 2, 2)
    assert three["apres"] == [4] and three["files"] == 3  # its parent's files come with it, as on main


def test_excluded_todo_keeps_its_real_reason_after_check(repo):
    # 25/10 09:12: #393 was not in the lot of #414 and nothing told why afterwards; `check` called everything
    # outside the signed ids « retirée du lot », hiding the real reason (here a dirty worktree).
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/b", "b.txt")
    path = repo.root.parent / "wt-2"
    sh(repo.root, "worktree", "add", "-q", str(path), "todo-2/b")
    (path / "brouillon.txt").write_text("x\n")
    data = branch_lot.lot(repo)
    assert data["ids"] == [1] and "worktree non propre" in data["ecartees"][0]["raison"]
    checked = branch_lot.check(repo, [1], data["empreinte"])
    assert "worktree non propre" in checked["ecartees"][0]["raison"]
    r = branch_lot.run(repo, checked, by={"by": "test"}, restart=lambda: None)
    text = branch_commands.lot_text(r)
    assert "🚫 Hors du lot, pas tentées :" in text and "#2 tâche : worktree non propre" in text


def test_report_lists_merged_and_open_with_reasons_first_and_journals_a_stopped_lot(repo, esprit_dir):
    # 25/10 08:30: the lot stopped on #393 and the dashboard journalled nothing (it journals a success only).
    esprit_dir.mkdir()
    branch(repo, "todo-4/base", "tests/test_base.py", FAILING)
    sh(repo.root, "switch", "-qc", "todo-3/suite", "todo-4/base")
    commit(repo.root, "suite.txt", "y\n", "travail sur todo-3/suite (todo #3)")
    sh(repo.root, "switch", "-q", "main")
    branch(repo, "todo-5/c", "c.txt")
    r = merge(repo, [3, 4, 5], [])
    assert [(b["todo_id"], b["fusionnee"], b["raison"]) for b in r["bilan"]] == [
        (4, False, "tests en échec"), (3, False, "attend #4, pas fusionnée"), (5, True, None)]
    text = branch_commands.lot_text(r)
    assert text.index("✅ Fusionnées : #5") < text.index("⏸ Restées ouvertes") < text.index("⛔ Arrêt à #4")
    assert "• #3 tâche : attend #4, pas fusionnée" in text and len(text.split("⛔")[0]) < 3000
    line = [t for t in _journal(esprit_dir, repo) if t.startswith("Tout fusionner :")]
    assert line == ["Tout fusionner : 1/3 fusionnée(s) (#5) ; restée(s) ouverte(s) : #4 (tests en échec), "
                    "#3 (attend #4, pas fusionnée)"]


def test_todos_after_a_stop_are_reported_not_tried(repo, monkeypatch):
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/b", "b.txt")
    data = branch_lot.lot(repo)

    def refuse(settings, tid, **kw):
        raise InvalidRequestError("❌ Fusion refusée : une fusion est déjà en cours\n\nÀ faire : attends")
    monkeypatch.setattr(branches, "merge_and_close", refuse)
    r = branch_lot.run(repo, data, by=None, restart=lambda: None)
    assert [b["raison"] for b in r["bilan"]] == ["une fusion est déjà en cours", "pas tentée : le lot s'est arrêté à #1"]


def test_slow_suite_is_reported_as_too_long_not_as_red(repo, monkeypatch, esprit_dir):
    # 25/10 08:30: #393's suite took longer than the 300 s limit; log, journal and text said « les tests échouent ».
    esprit_dir.mkdir()
    branch(repo, "todo-5/feat")
    monkeypatch.setattr(branches.repos, "run_tests", lambda *a: {
        "ok": False, "tested": True, "timeout": True, "detail": "tests trop longs (> 900 s)"})
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    assert r["check"]["timeout"] and not r["closed"]
    assert "Fusion refusée : les tests dépassent le délai de 900 s sur todo-5/feat" in _journal(esprit_dir, repo)
    text = branch_commands.merge_text(r)
    assert "(tests trop longs)" in text and "les tests échouent" not in text and "allège la suite" in text


def test_stale_throwaway_worktree_of_a_killed_merge_is_swept(repo):
    # var/merge-check/wt-urfrhowy (20/10 23:17, half-done trial merge) was still registered on 25/10.
    base = repo.var_dir / "merge-check"
    base.mkdir(parents=True)
    sh(repo.root, "worktree", "add", "-q", "--detach", str(base / "wt-stale"), "main")
    (base / "rv-stale").mkdir()
    branch(repo, "todo-5/feat")
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    assert r["closed"] and not any(base.iterdir())
    assert "wt-stale" not in sh(repo.root, "worktree", "list")
