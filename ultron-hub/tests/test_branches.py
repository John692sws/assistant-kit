import subprocess
import sys
from pathlib import Path

import pytest

from ultron_hub import branch_commands, branches, repos
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import load_settings
from ultron_hub.todo.store import TodoStore

PASSING = "def test_ok():\n    assert True\n"
SETTINGS = """default_model: sonnet
repos:
  tests:
    proj: [".venv/bin/python", "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider"]
"""


@pytest.fixture(autouse=True)
def no_bwrap(monkeypatch):
    """The fake .venv runs this Python, hidden by the real sandbox: tested apart (test_repos.py)."""
    monkeypatch.setattr(repos, "sandbox_argv", lambda cmd, tree, read_only: cmd)


def sh(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(root, path, content, message):
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(content)
    sh(root, "add", path)
    sh(root, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git project: main with one passing test, an open todo #5, and the current Python as .venv."""
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t"); monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t"); monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    sh(root, "init", "-q", "-b", "main")
    sh(root, "config", "user.name", "t"); sh(root, "config", "user.email", "t@t")
    commit(root, ".gitignore", "var/\n.venv/\nconfig/\n", "ignore")
    commit(root, "tests/test_a.py", PASSING, "tests")
    (root / ".venv" / "bin").mkdir(parents=True)
    python = root / ".venv" / "bin" / "python"  # a wrapper: a symlink would lose the venv (and pytest)
    python.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    python.chmod(0o755)
    (root / "config").mkdir()
    (root / "config" / "settings.yaml").write_text(SETTINGS)
    settings = load_settings(root)
    with TodoStore(settings.todo_db) as todos:
        for _ in range(5):
            todos.add(title="tâche", category="serveur")
    return settings


def branch(settings, name, path="feature.txt", content="x\n"):
    root = settings.root
    sh(root, "switch", "-qc", name, "main")
    commit(root, path, content, f"travail sur {name} (todo #{name.split('/')[0].removeprefix('todo-')})")
    sh(root, "switch", "-q", "main")


def status(settings, todo_id=5):
    with TodoStore(settings.todo_db) as todos:
        return todos.get(todo_id).status


def test_info_only_matches_exact_todo(repo):
    branch(repo, "todo-5/feat")
    branch(repo, "todo-50/other", "other.txt")
    data = branches.info(repo, 5)
    assert data["branch"] == "todo-5/feat" and data["ahead"] == 1 and data["commits"] == ["travail sur todo-5/feat (todo #5)"]
    assert branches.info(repo, 7)["branch"] is None


def test_merge_and_close(repo):
    branch(repo, "todo-5/feat")
    restarts = []
    r = branches.merge_and_close(repo, 5, restart=lambda: restarts.append(1))
    assert r["merged"] and r["closed"] and r["check"]["ok"] and "passed" in r["check"]["detail"]
    assert (repo.root / "feature.txt").is_file() and sh(repo.root, "branch", "--show-current") == "main"
    assert "todo-5/feat" not in sh(repo.root, "branch") and status(repo) == "done" and restarts == [1]
    assert not any((repo.var_dir / "merge-check").iterdir()) and "merge-check" not in sh(repo.root, "worktree", "list")
    assert "fusionnée dans main" in branch_commands.merge_text(r)


def test_live_tree_on_the_branch_is_merged(repo):
    branch(repo, "todo-5/feat")
    sh(repo.root, "switch", "-q", "todo-5/feat")
    assert branches.merge_and_close(repo, 5, restart=lambda: None)["merged"]
    assert sh(repo.root, "branch", "--show-current") == "main"


def test_failing_tests_block_merge(repo):
    branch(repo, "todo-5/feat", "tests/test_b.py", "def test_ko():\n    assert False\n")
    before = sh(repo.root, "rev-parse", "main")
    r = branches.merge_and_close(repo, 5, restart=lambda: pytest.fail("no restart"))
    assert not r["merged"] and not r["closed"] and r["check"]["stage"] == "tests" and "test_ko" in r["check"]["detail"]
    assert sh(repo.root, "rev-parse", "main") == before and status(repo) == "todo"
    assert "reste ouverte" in branch_commands.merge_text(r)


def test_conflict_blocks_merge(repo):
    branch(repo, "todo-5/feat", "tests/test_a.py", PASSING + "# branche\n")
    commit(repo.root, "tests/test_a.py", PASSING + "# main\n", "main bouge")
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    assert r["check"]["stage"] == "conflict" and "tests/test_a.py" in r["check"]["detail"]
    assert status(repo) == "todo" and not sh(repo.root, "status", "--porcelain", "--untracked-files=no")


def test_refusals(repo):
    with pytest.raises(InvalidRequestError, match="aucune branche"):
        branches.merge_and_close(repo, 5, restart=lambda: None)
    branch(repo, "todo-5/feat")
    branch(repo, "todo-4/other", "other.txt")
    sh(repo.root, "switch", "-q", "todo-4/other")
    with pytest.raises(InvalidRequestError, match="todo-4/other"):  # its code would vanish from the bot
        branches.merge_and_close(repo, 5, restart=lambda: None)
    sh(repo.root, "switch", "-q", "main")
    (repo.root / "tests" / "test_a.py").write_text(PASSING + "# en cours\n")
    with pytest.raises(InvalidRequestError, match="non commitées"):
        branches.merge_and_close(repo, 5, restart=lambda: None)
    assert status(repo) == "todo"


def worktree(settings, name="todo-5/feat"):
    """The branch checked out in its own worktree, as agents work (seen on 2026-10-11 with #19 and #20)."""
    branch(settings, name)
    path = settings.root.parent / "wt-5"
    sh(settings.root, "worktree", "add", "-q", str(path), name)
    return path


def test_branch_in_a_worktree_is_merged_and_cleaned(repo):
    path = worktree(repo)
    restarts = []
    r = branches.merge_and_close(repo, 5, restart=lambda: restarts.append(1))
    assert r["merged"] and r["closed"] and r["cleanup"] is None and restarts == [1]
    assert not path.exists() and "todo-5/feat" not in sh(repo.root, "branch") and status(repo) == "done"


def test_already_merged_branch_in_a_worktree_closes(repo):
    worktree(repo)
    sh(repo.root, "merge", "-q", "--no-ff", "todo-5/feat", "-m", "fusion manuelle")
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    assert not r["merged"] and r["closed"] and r["cleanup"] is None and status(repo) == "done"


def test_dirty_worktree_blocks_merge(repo):
    path = worktree(repo)
    (path / "brouillon.txt").write_text("pas commité\n")
    head = sh(repo.root, "rev-parse", "main")
    with pytest.raises(InvalidRequestError, match="worktree .* non commitées"):
        branches.merge_and_close(repo, 5, restart=lambda: None)
    assert status(repo) == "todo" and sh(repo.root, "rev-parse", "main") == head and path.exists()


def test_cleanup_failure_still_closes(repo):
    path = worktree(repo)
    sh(repo.root, "worktree", "lock", str(path))  # `worktree remove` refuses a locked worktree
    restarts = []
    r = branches.merge_and_close(repo, 5, restart=lambda: restarts.append(1))
    assert r["merged"] and r["closed"] and status(repo) == "done" and restarts == [1]
    assert r["cleanup"] and "à supprimer à la main" in branch_commands.merge_text(r)


def test_restart_failure_is_reported_not_fatal(repo):
    branch(repo, "todo-5/feat")
    def boom():
        raise RuntimeError("systemd absent")
    r = branches.merge_and_close(repo, 5, restart=boom)
    assert r["closed"] and "systemd absent" in r["restart"]


def test_branch_map(repo):
    branch(repo, "todo-5/feat")
    branch(repo, "todo-50/other", "other.txt")
    sh(repo.root, "branch", "unrelated")
    assert branches.branch_map(repo) == {
        5: {"branch": "todo-5/feat", "ahead": 1, "hors_main": 1, "pret": True, "raison": None},
        50: {"branch": "todo-50/other", "ahead": 1, "hors_main": 1, "pret": True, "raison": None}}


def test_info_lists_every_branch_when_a_repo_has_several(repo):
    branch(repo, "todo-5/a")
    sh(repo.root, "switch", "-q", "main")
    branch(repo, "todo-5/b", "b.txt")
    sh(repo.root, "switch", "-q", "main")
    data = branches.info(repo, 5)
    assert data["branch"] is None and data["unique"] is False
    assert sorted(data["branches"]) == ["todo-5/a", "todo-5/b"]
    assert data["ahead"] == 2 and len(data["commits"]) == 2  # the warning in /todo relies on this
    assert branches.branch_map(repo)[5]["ahead"] == 2
    assert sorted(branches.branch_map(repo)[5]["branch"].split(", ")) == ["todo-5/a", "todo-5/b"]


def test_merge_with_several_branches_is_refused_and_todo_stays_open(repo):
    branch(repo, "todo-5/a")
    sh(repo.root, "switch", "-q", "main")
    branch(repo, "todo-5/b", "b.txt")
    sh(repo.root, "switch", "-q", "main")
    with pytest.raises(InvalidRequestError):
        branches.merge_and_close(repo, 5, restart=lambda: None)
    with TodoStore(repo.todo_db) as todos:
        assert todos.get(5).status == "todo"


# --- refusals are explained and journalled (todo #51) ---------------------------------------------

@pytest.fixture
def journals(esprit_dir):
    """Captured ultron_hub log records + the esprit journal (the folder must exist for entries to be written)."""
    import logging
    from ultron_hub.esprit import store as esprit
    esprit_dir.mkdir()
    records = []
    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)
    handler, logger = Grab(), logging.getLogger("ultron_hub")
    logger.addHandler(handler)
    old = logger.level
    logger.setLevel(logging.WARNING)
    yield records, lambda settings: [e["texte"] for e in esprit.tail(settings, 20) if e["texte"].startswith("Fusion refusée")]
    logger.removeHandler(handler)
    logger.setLevel(old)


def refused(settings, journals, step, todo_id=5):
    """Run the merge, expect a refusal; check the WARNING line and the esprit entry; return the message."""
    records, entries = journals
    with pytest.raises(InvalidRequestError) as exc:
        branches.merge_and_close(settings, todo_id, restart=lambda: pytest.fail("no restart"))
    lines = [r.getMessage() for r in records if r.levelname == "WARNING" and f"step={step} " in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith(f"todo={todo_id} step={step} reason=")
    assert entries(settings) == [f"Fusion refusée : {lines[0].split('reason=', 1)[1]}"]
    text = str(exc.value)
    assert text.startswith("❌ Fusion refusée : ") and "À faire :" in text and f"#{todo_id} reste ouverte" in text
    assert status(settings, todo_id) == "todo"
    return text


def test_refusal_closed_todo(repo, journals):
    branch(repo, "todo-5/feat")
    with TodoStore(repo.todo_db) as todos:
        todos.set_status(5, "done")
    with pytest.raises(InvalidRequestError, match="n'est plus ouverte"):
        branches.merge_and_close(repo, 5, restart=lambda: None)
    lines = [r.getMessage() for r in journals[0] if "step=todo-status" in r.getMessage()]
    assert lines == ["todo=5 step=todo-status reason=la tâche #5 n'est plus ouverte"]
    assert journals[1](repo) == ["Fusion refusée : la tâche #5 n'est plus ouverte"]


def test_refusal_no_branch(repo, journals):
    text = refused(repo, journals, "branch-missing")
    assert "aucune branche" in text and "🌿" in text


def test_refusal_several_branches(repo, journals):
    branch(repo, "todo-5/a")
    branch(repo, "todo-5/b", "b.txt")
    text = refused(repo, journals, "branches-multiple")
    assert "todo-5/a" in text and "todo-5/b" in text


def test_refusal_wrong_branch_in_main_tree(repo, journals):
    branch(repo, "todo-5/feat")
    branch(repo, "todo-4/other", "other.txt")
    sh(repo.root, "switch", "-q", "todo-4/other")
    text = refused(repo, journals, "wrong-branch")
    assert "todo-4/other" in text and "git switch main" in text


def test_refusal_main_tree_modified_lists_files_max_10(repo, journals):
    branch(repo, "todo-5/feat")
    for i in range(12):
        commit(repo.root, f"data/f{i:02}.txt", "a\n", f"f{i}")
    for i in range(12):
        (repo.root / "data" / f"f{i:02}.txt").write_text("b\n")
    text = refused(repo, journals, "main-dirty")
    assert text.count("• data/f") == 10 and "… et 2 autres" in text and "(modifié)" in text
    assert "b\n" not in text  # names only, never content
    entry = journals[1](repo)[0]
    assert "data/f09.txt" in entry and "data/f10.txt" not in entry


def test_refusal_dirty_worktree_untracked_file(repo, journals):
    path = worktree(repo)
    (path / ".venv").write_text("lien\n")
    text = refused(repo, journals, "worktree-dirty")
    assert "fichier non suivi .venv dans le worktree : supprime-le ou demande à Claude Code" in text
    assert "• .venv (non suivi)" in text and path.exists()


def test_refusal_dirty_worktree_many_files(repo, journals):
    path = worktree(repo)
    for i in range(11):
        (path / f"x{i:02}.txt").write_text("secret contenu\n")
    text = refused(repo, journals, "worktree-dirty")
    assert text.count("• x") == 10 and "… et 1 autres" in text and "secret contenu" not in text


def test_refusal_lock_taken(repo, journals):
    import fcntl
    branch(repo, "todo-5/feat")
    repo.var_dir.mkdir(parents=True, exist_ok=True)
    with open(repo.var_dir / "merge.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        text = refused(repo, journals, "lock")
    assert "déjà en cours" in text


def test_refusal_conflict_is_journalled_and_explained(repo, journals):
    branch(repo, "todo-5/feat", "tests/test_a.py", PASSING + "# branche\n")
    commit(repo.root, "tests/test_a.py", PASSING + "# main\n", "main bouge")
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    records, entries = journals
    lines = [x.getMessage() for x in records if "step=trial-merge" in x.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("todo=5 step=trial-merge reason=") and "tests/test_a.py" in lines[0]
    assert entries(repo) == [f"Fusion refusée : {lines[0].split('reason=', 1)[1]}"]
    text = branch_commands.merge_text(r)
    assert "• tests/test_a.py" in text and "À faire :" in text and "rebase" in text


def test_refusal_failing_tests_is_journalled_and_explained(repo, journals):
    branch(repo, "todo-5/feat", "tests/test_b.py", "def test_ko():\n    assert False\n")
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    records, entries = journals
    lines = [x.getMessage() for x in records if "step=tests" in x.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("todo=5 step=tests reason=")
    assert "assert False" not in lines[0] and entries(repo) == [f"Fusion refusée : {lines[0].split('reason=', 1)[1]}"]
    text = branch_commands.merge_text(r)
    assert "À faire :" in text and "corrige les tests" in text


def test_cli_returns_refusal_text_as_error(repo, journals):
    path = worktree(repo)
    (path / ".venv").write_text("lien\n")
    with pytest.raises(InvalidRequestError) as exc:
        branches.merge_and_close(repo, 5, restart=lambda: None)
    assert str(exc.value).startswith("❌ Fusion refusée")


# -- todo #286: ready to merge = a clean commit « (todo #<id>) » and a clean worktree; otherwise « en cours » ------------

def stack(settings, parent="todo-4/base", child="todo-3/suite"):
    """`child` branched from `parent`, with no commit of its own yet."""
    branch(settings, parent, "base.txt")
    sh(settings.root, "branch", child, parent)


def test_ready_normal_case(repo):
    branch(repo, "todo-5/feat")
    assert branches.readiness(repo, 5) == {"pret": True, "raison": None, "propres": 1}
    assert branches.branch_map(repo)[5]["pret"] and branches.info(repo, 5)["pret"]


def test_stacked_branch_without_own_commit_is_not_ready_but_its_parent_is(repo):
    stack(repo)
    bm = branches.branch_map(repo)
    assert bm[3]["pret"] is False and bm[3]["ahead"] == 0 and bm[3]["hors_main"] == 1
    assert "aucun commit propre" in bm[3]["raison"]
    assert bm[4]["pret"] and bm[4]["ahead"] == 1  # the parent keeps its commit although the child contains it
    with pytest.raises(InvalidRequestError, match="pas prête"):
        branches.merge_and_close(repo, 3, restart=lambda: None)
    assert status(repo, 3) == "todo"


def test_stacked_branch_with_own_commit_is_ready(repo):
    stack(repo)
    sh(repo.root, "switch", "-q", "todo-3/suite")
    commit(repo.root, "suite.txt", "y\n", "suite (todo #3)")
    sh(repo.root, "switch", "-q", "main")
    assert branches.branch_map(repo)[3]["pret"]


def test_branch_without_todo_tag_is_not_ready(repo):
    sh(repo.root, "switch", "-qc", "todo-5/feat", "main")
    commit(repo.root, "f.txt", "x\n", "travail sans étiquette")
    commit(repo.root, "g.txt", "x\n", "autre numéro (todo #6)")
    sh(repo.root, "switch", "-q", "main")
    assert branches.branch_map(repo)[5]["ahead"] == 0 and not branches.info(repo, 5)["pret"]
    with pytest.raises(InvalidRequestError, match=r"finit par « \(todo #5\) »"):
        branches.merge_and_close(repo, 5, restart=lambda: None)


def test_dirty_worktree_is_not_ready_untracked_included(repo):
    path = worktree(repo)
    assert branches.branch_map(repo)[5]["pret"]
    (path / "brouillon.txt").write_text("non suivi\n")
    ready = branches.readiness(repo, 5)
    assert not ready["pret"] and "worktree" in ready["raison"]
    assert branches.branch_map(repo)[5]["ahead"] == 0
