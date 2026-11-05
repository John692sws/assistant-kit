"""`branch etat` (todo #255): branches in progress, waiting for the user's 🔀 and why, automatic merges. Throwaway repos only."""

import json

import pytest

from test_auto_merge import run_cli
from test_branches import branch, journals, no_bwrap, repo, sh, status, worktree  # noqa: F401
from ultron_hub import after_merge, auto_merge, branch_etat, branches


@pytest.fixture(autouse=True)
def no_job(monkeypatch):
    for name in auto_merge.JOB_ENVS:
        monkeypatch.delenv(name, raising=False)


def test_green_branch_is_in_progress_with_commit_and_worktree(repo):
    path = worktree(repo)
    d = branch_etat.etat(repo)
    assert d["attente"] == [] and d["auto"] == [] and d["annulation"] == {"cible": None, "annulee": False}
    [row] = d["en_cours"]
    [b] = row["branches"]
    assert row["todo_id"] == 5 and row["title"] and b["branch"] == "todo-5/feat" and b["ahead"] == 1
    assert b["commit"]["sha"] == sh(repo.root, "rev-parse", "todo-5/feat")[:12] and b["commit"]["subject"].startswith("travail")
    assert b["worktree"] == {"path": str(path), "propre": True}
    (path / "untracked.txt").write_text("x\n")
    assert branch_etat.etat(repo)["en_cours"][0]["branches"][0]["worktree"]["propre"] is False


def test_branch_without_worktree(repo):
    branch(repo, "todo-5/feat")
    assert branch_etat.etat(repo)["en_cours"][0]["branches"][0]["worktree"] is None


def test_refusals_are_the_ones_of_auto_merge(repo):
    branch(repo, "todo-5/feat", "deploy/priv/x.service", "[Unit]\n")  # refused during the week of todo #482 and after
    [row] = branch_etat.etat(repo)["attente"]
    [why] = row["raisons"]
    assert why["step"] == "auto-sensitive" and why["files"] == ["deploy/priv/x.service"] and "sensible" in why["reason"]
    found = branches.targets(repo, 5)
    assert [r["step"] for r in auto_merge.refusals(repo, 5, found)] == ["auto-sensitive"]
    assert branch_etat.etat(repo)["en_cours"] == []


def test_after_merge_declaration_waits_for_the_user(repo):
    plan = {"actions": [{"action": "user_unit_restart", "args": ["x.service"], "reason": "test"}]}
    branch(repo, "todo-5/feat", after_merge.declaration_path(5), json.dumps(plan))
    [row] = branch_etat.etat(repo)["attente"]
    assert [r["step"] for r in row["raisons"]] == ["auto-after-merge"]


def test_automatic_merge_is_listed_with_its_todo_waiting_for_the_check(repo, journals):
    branch(repo, "todo-5/feat")
    auto_merge.auto_merge(repo, 5, restart=lambda: None)
    d = branch_etat.etat(repo)
    [f] = d["auto"]
    head = sh(repo.root, "rev-parse", "main")[:12]
    assert f["todo_id"] == 5 and f["sha"] == head and f["a_valider"] is True and f["annulee"] is False and f["at"]
    assert d["annulation"] == {"cible": f"5-{head}", "annulee": False} and d["en_cours"] == []
    auto_merge.revert_last(repo, {"by": "test", "proof": "p"}, restart=lambda: None)
    d = branch_etat.etat(repo)
    assert d["auto"][0]["annulee"] is True and d["annulation"]["annulee"] is True


def test_cli_etat_is_read_only_json(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    branch(repo, "todo-5/feat")
    before = sh(repo.root, "rev-parse", "main", "todo-5/feat")
    code, out = run_cli(capsys, "branch", "etat")
    assert code == 0 and out["command"] == "branch.etat" and len(out["data"]["en_cours"]) == 1
    assert sh(repo.root, "rev-parse", "main", "todo-5/feat") == before and status(repo) == "todo"
