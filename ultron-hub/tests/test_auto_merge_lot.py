"""Automatic merge of the whole lot under the user's advance authorisation (todo #533). Throwaway git repos only."""

import json

import pytest

from test_auto_merge import DURING, delivered, give, run_cli
from test_branches import PASSING, branch, commit, journals, no_bwrap, repo, sh, status  # noqa: F401
from ultron_hub import auto_merge, branches
from ultron_hub.common.errors import InvalidRequestError


@pytest.fixture(autouse=True)
def no_job(monkeypatch):
    for name in auto_merge.JOB_ENVS:
        monkeypatch.delenv(name, raising=False)


def lot(settings, **kw):
    restarts = []
    r = auto_merge.auto_merge_lot(settings, restart=lambda: restarts.append(1), now=DURING,
                                  restart_dash=lambda: restarts.append("dash"), **kw)
    return r, restarts


def by_id(r):
    return {b["todo_id"]: b for b in r["bilan"]}


def test_without_authorisation_nothing_is_merged(repo, journals):
    branch(repo, "todo-4/a", "a.txt")
    branch(repo, "todo-5/b", "b.txt")
    before = sh(repo.root, "rev-parse", "main")
    with pytest.raises(InvalidRequestError) as exc:
        auto_merge.auto_merge_lot(repo, restart=lambda: pytest.fail("no restart"), now=DURING)
    assert "autorisation d'avance" in str(exc.value) and "Rien n'a été fusionné" in str(exc.value)
    assert sh(repo.root, "rev-parse", "main") == before and "todo-4/a" in sh(repo.root, "branch")
    assert any("step=auto-lot-no-authorisation" in r.getMessage() for r in journals[0])
    assert auto_merge.last(repo)["fusion"] is None


def test_two_clean_branches_are_merged_once_restarted_and_stay_open(repo):
    give(repo)
    branch(repo, "todo-4/a", "a.txt")
    branch(repo, "todo-5/b", "b.txt")
    r, restarts = lot(repo)
    assert r["ids"] == [4, 5] and r["fusionnees"] == [4, 5] and r["refusees"] == [] and restarts == [1]
    assert r["restart"] == "planned"
    head = sh(repo.root, "rev-parse", "main")
    assert by_id(r)[5]["statut"] == "fusionnee" and by_id(r)[5]["sha"] == head
    assert (repo.root / "a.txt").is_file() and (repo.root / "b.txt").is_file()
    for tid in (4, 5):
        assert status(repo, tid) == "todo" and delivered(repo, tid)
    assert auto_merge.last(repo)["fusion"]["todo_id"] == 5


def test_a_security_branch_is_refused_and_listed_while_the_other_merges(repo, journals):
    give(repo)
    branch(repo, "todo-4/sec", "src/ultron_hub/privileged.py", "changé\n")
    branch(repo, "todo-5/b", "b.txt")
    r, restarts = lot(repo)
    b = by_id(r)
    assert b[4]["statut"] == "refusee" and "fichier sensible" in b[4]["raison"]
    assert "src/ultron_hub/privileged.py" in b[4]["detail"]
    assert b[5]["statut"] == "fusionnee" and r["fusionnees"] == [5] and r["refusees"] == [4] and restarts == [1]
    assert "todo-4/sec" in sh(repo.root, "branch") and not (repo.root / "src/ultron_hub/privileged.py").exists()
    assert status(repo, 4) == "todo" and not delivered(repo, 4) and delivered(repo, 5)
    assert any("todo=4 step=auto-sensitive" in x.getMessage() for x in journals[0])


def test_a_branch_stacked_on_a_refused_one_is_refused_too(repo):
    give(repo)
    branch(repo, "todo-4/sec", "src/ultron_hub/privileged.py", "changé\n")
    sh(repo.root, "switch", "-qc", "todo-5/child", "todo-4/sec")
    commit(repo.root, "c.txt", "c\n", "suite (todo #5)")
    sh(repo.root, "switch", "-q", "main")
    before = sh(repo.root, "rev-parse", "main")
    r, restarts = lot(repo)
    assert r["ids"] == [4, 5] and r["fusionnees"] == [] and restarts == []
    assert by_id(r)[5]["raison"] == "empilée sur #4, refusée"
    assert sh(repo.root, "rev-parse", "main") == before


def test_red_tests_are_refused_with_their_reason_and_the_lot_goes_on(repo):
    give(repo)
    branch(repo, "todo-4/ko", "tests/test_ko.py", "def test_ko():\n    assert False\n")
    branch(repo, "todo-5/b", "b.txt")
    r, _ = lot(repo)
    assert by_id(r)[4]["raison"] == "tests en échec" and r["fusionnees"] == [5]
    assert "todo-4/ko" in sh(repo.root, "branch")


@pytest.mark.parametrize("env", [{"ULTRON_HUB_JOB": "1"}, {"ULTRON_ESCOUADE_DIR": "/tmp/escouade"}])
def test_refused_from_a_job_or_a_squad(repo, env):
    give(repo)
    branch(repo, "todo-5/b", "b.txt")
    with pytest.raises(InvalidRequestError) as exc:
        auto_merge.auto_merge_lot(repo, restart=lambda: pytest.fail("no restart"), env=env, now=DURING)
    assert "depuis un job ou une escouade" in str(exc.value) and "todo-5/b" in sh(repo.root, "branch")


def test_cli_refuses_from_a_job_and_merges_otherwise(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setattr(branches, "restart_gateway", lambda: None)
    give(repo, jours=14, now=__import__("time").time())
    branch(repo, "todo-5/b", "b.txt")
    monkeypatch.setenv("ULTRON_HUB_JOB", "1")
    code, out = run_cli(capsys, "branch", "auto-merge-lot")
    assert code == 1 and "depuis un job" in out["error"] and "todo-5/b" in sh(repo.root, "branch")
    monkeypatch.delenv("ULTRON_HUB_JOB")
    code, out = run_cli(capsys, "branch", "auto-merge-lot")
    assert code == 0 and out["data"]["fusionnees"] == [5] and "#5 fusionnée" in out["data"]["text"]
    json.dumps(out)
