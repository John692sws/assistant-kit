"""Automatic merge of a green branch by the orchestrator, and its signed revert (todo #254). Throwaway git repos only."""

import dataclasses
import json
import time
from datetime import datetime

import pytest

from hermes_plugin import close_key
from test_branches import PASSING, branch, commit, journals, no_bwrap, repo, sh, status, worktree  # noqa: F401
from ultron_hub import after_merge, auto_merge, autorisation, branch_commands, branches, cli
from ultron_hub.chantier import core as chantier_core
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.todo import closing, service
from ultron_hub.todo.store import TodoStore


# Todo #482 then #495: the clock around the last day of the user's authorisation (2026-11-08 included, Paris time).
DURING = datetime(2026, 11, 8, 23, 59, tzinfo=auto_merge.PARIS).timestamp()
AFTER = datetime(2026, 11, 9, 0, 0, tzinfo=auto_merge.PARIS).timestamp()
GIVEN = datetime(2026, 11, 2, 9, 0, tzinfo=auto_merge.PARIS).timestamp()  # 7 days given on 2026-11-02 → until the 8th


def give(settings, jours=7, now=GIVEN, by=None):
    """A signed authorisation written as the CLI does after checking the proof (the throwaway close key is created)."""
    close_key.load_or_create()
    return autorisation.donner(settings, jours, autorisation.perimetre(), by or {"by": "cli-human"}, now=now)


@pytest.fixture(autouse=True)
def authorised(request):
    """The tests of the merge rules run under a valid authorisation until 2026-11-08 (the former fixed week)."""
    if "repo" in request.fixturenames:
        give(request.getfixturevalue("repo"))


@pytest.fixture(autouse=True)
def no_job(monkeypatch):
    for name in auto_merge.JOB_ENVS:
        monkeypatch.delenv(name, raising=False)


def history(settings, todo_id=5):
    with TodoStore(settings.todo_db) as todos:
        return todos.history(todo_id)


def delivered(settings, todo_id=5):
    return service.is_delivered([(h["action"], h["details"]) for h in history(settings, todo_id)])


def entries(settings):
    from ultron_hub.esprit import store as esprit
    return [e["texte"] for e in esprit.tail(settings, 20)]


def warnings(records, step):
    return [r.getMessage() for r in records if r.levelname == "WARNING" and f"step={step} " in r.getMessage()]


def test_green_branch_is_merged_and_the_todo_stays_open_for_the_user(repo, journals):
    branch(repo, "todo-5/feat")
    restarts = []
    r = auto_merge.auto_merge(repo, 5, restart=lambda: restarts.append(1))
    assert r["merged"] and not r["closed"] and r["check"]["ok"] and restarts == [1]
    assert (repo.root / "feature.txt").is_file() and "todo-5/feat" not in sh(repo.root, "branch")
    head = sh(repo.root, "rev-parse", "main")
    assert sh(repo.root, "log", "-1", "--format=%s", "main") == "Fusion automatique de todo-5/feat (todo #5, reste ouverte)"
    assert len(sh(repo.root, "log", "-1", "--format=%P", "main").split()) == 2  # --no-ff
    assert status(repo) == "todo" and delivered(repo)
    [ev] = [h for h in history(repo) if h["action"] == "livree"]
    assert ev["details"]["by"] == "auto:orchestrateur" and head[:12] in ev["details"]["note"]
    assert f"Fusion automatique : todo #5, {head[:12]}" in entries(repo)
    assert not any(h["action"] == "status" for h in history(repo))  # never closed
    info = auto_merge.last(repo)
    assert info["cible"] == f"5-{head[:12]}" and not info["annulee"] and info["fusion"]["by"] == "auto:orchestrateur"
    text = branch_commands.auto_text(r)
    assert "fusionnée seule" in text and "encore ouverte" in text and "terminée" not in text
    assert not any((repo.var_dir / "merge-check").iterdir())


def test_the_merged_todo_shows_in_a_toi(repo):
    branch(repo, "todo-5/feat")
    auto_merge.auto_merge(repo, 5, restart=lambda: None)
    assert chantier_core.delivered_ids(repo, [5]) == frozenset({5})


def refused(settings, journals, step, todo_id=5, **kw):
    records, _ = journals
    before = sh(settings.root, "rev-parse", "main")
    with pytest.raises(InvalidRequestError) as exc:
        auto_merge.auto_merge(settings, todo_id, restart=lambda: pytest.fail("no restart"), **kw)
    lines = warnings(records, step)
    assert len(lines) == 1 and lines[0].startswith(f"todo={todo_id} step={step} reason=")
    assert f"Fusion refusée : {lines[0].split('reason=', 1)[1]}" in entries(settings)
    assert sh(settings.root, "rev-parse", "main") == before and status(settings, todo_id) == "todo"
    assert not delivered(settings, todo_id) and auto_merge.last(settings)["fusion"] is None
    text = str(exc.value)
    assert text.startswith("❌ Fusion refusée : ") and f"#{todo_id} reste ouverte" in text
    return text


@pytest.mark.parametrize("path", [s + "x.sh" if s.endswith("/") else s for s in auto_merge.SENSITIVE])
def test_each_sensitive_file_refuses_the_automatic_merge(repo, journals, path):
    branch(repo, "todo-5/feat", path, "changé\n")
    text = refused(repo, journals, "auto-sensitive", now=AFTER)
    assert f"• {path}" in text and "todo-5/feat" in sh(repo.root, "branch")


def test_the_sensitive_list_names_the_signing_privileged_and_sandbox_files():
    must = {"src/ultron_hub/todo/closing.py", "hermes_plugin/close_key.py", "deploy/", "src/ultron_hub/branches.py",
            "src/ultron_hub/repos.py", "src/ultron_hub/veille/sandbox.py"}
    assert must <= set(auto_merge.SENSITIVE)
    assert auto_merge.sensitive(["deploy/priv/a/b", "deploy/user/ultron-user", "src/ultron_hub/todo/closing.py.bak",
                                 "README.md"]) == ["deploy/priv/a/b", "deploy/user/ultron-user"]


NEW_SENSITIVE = ["src/ultron_hub/branches.py", "src/ultron_hub/branch_lot.py", "src/ultron_hub/branch_commands.py",
                 "src/ultron_hub/after_merge.py", "deploy/systemd/x.service", "deploy/priv/ultron-priv",
                 "deploy/user/ultron-user", "deploy/prompt/a.md", "src/ultron_hub/dashboard/auth.py",
                 "src/ultron_hub/dashboard/server.py", "src/ultron_hub/executors/claude_cli.py",
                 "src/ultron_hub/escouade/hooks.py", "tests/conftest.py", ".claude/settings.json"]


@pytest.mark.parametrize("path", NEW_SENSITIVE)
def test_merge_deploy_dashboard_executors_and_test_guards_are_sensitive(repo, journals, path):
    assert auto_merge.sensitive([path]) == [path]
    branch(repo, "todo-5/feat", path, "changé\n")
    assert f"• {path}" in refused(repo, journals, "auto-sensitive", now=AFTER)


def test_neighbours_of_the_sensitive_paths_are_not_sensitive():
    assert auto_merge.sensitive(["deployer.txt", "src/ultron_hub/dashboard/views.py", "src/ultron_hub/executors.py",
                                 "tests/test_conftest.py", ".claudeignore", "docs/deploy/x.md"]) == []


def test_refused_when_the_todo_has_branches_in_another_repository(repo, journals):
    other = repo.root.parent / "alpha"
    other.mkdir()
    sh(other, "init", "-q", "-b", "main")
    commit(other, "a.txt", "a\n", "init")
    sh(other, "switch", "-qc", "todo-5/feat", "main")
    commit(other, "b.txt", "b\n", "travail")
    sh(other, "switch", "-q", "main")
    branch(repo, "todo-5/feat")
    text = refused(repo, journals, "auto-multi-repo", now=AFTER)
    assert "todo-5/feat (alpha)" in text and "autres dépôts" in text
    assert "todo-5/feat" in sh(other, "branch") and "todo-5/feat" in sh(repo.root, "branch")


def test_a_sensitive_file_among_others_or_deleted_still_refuses(repo, journals):
    commit(repo.root, "src/ultron_hub/repos.py", "x = 1\n", "repos")
    sh(repo.root, "switch", "-qc", "todo-5/feat", "main")
    commit(repo.root, "ok.txt", "a\n", "ok (todo #5)")
    sh(repo.root, "rm", "-q", "src/ultron_hub/repos.py")
    sh(repo.root, "commit", "-qm", "supprime")
    sh(repo.root, "switch", "-q", "main")
    assert "src/ultron_hub/repos.py" in refused(repo, journals, "auto-sensitive", now=AFTER)


def test_a_sensitive_file_changed_by_main_only_does_not_count(repo):
    branch(repo, "todo-5/feat")
    commit(repo.root, "hermes_plugin/close_key.py", "# main\n", "main bouge")
    assert auto_merge.auto_merge(repo, 5, restart=lambda: None)["merged"]


@pytest.mark.parametrize("env", [{"ULTRON_HUB_JOB": "1"}, {"ULTRON_ESCOUADE_DIR": "/tmp/escouade"}])
def test_refused_from_a_mission_or_a_squad(repo, journals, env):
    branch(repo, "todo-5/feat")
    assert "depuis un job ou une escouade" in refused(repo, journals, "auto-mission", env=env)


def test_refused_from_a_mission_by_the_cli(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setenv("ULTRON_HUB_JOB", "1")
    monkeypatch.setattr(branches, "restart_gateway", lambda: pytest.fail("no restart"))
    branch(repo, "todo-5/feat")
    code, out = run_cli(capsys, "branch", "auto-merge", "5")
    assert code == 1 and "depuis un job" in out["error"] and "todo-5/feat" in sh(repo.root, "branch")


def test_refused_when_the_todo_declares_after_merge_actions(repo, journals):
    plan = {"actions": [{"action": "user_unit_restart", "args": ["hermes-gateway"]}]}
    branch(repo, "todo-5/feat", after_merge.declaration_path(5), json.dumps(plan))
    assert "actions après fusion" in refused(repo, journals, "auto-after-merge")


def test_refused_when_the_worktree_is_dirty(repo, journals):
    path = worktree(repo)
    (path / ".venv").write_text("lien\n")
    refused(repo, journals, "worktree-dirty")


def test_failing_tests_merge_nothing_and_leave_the_todo_untouched(repo, journals):
    branch(repo, "todo-5/feat", "tests/test_b.py", "def test_ko():\n    assert False\n")
    before = sh(repo.root, "rev-parse", "main")
    r = auto_merge.auto_merge(repo, 5, restart=lambda: pytest.fail("no restart"))
    assert not r["merged"] and r["check"]["stage"] == "tests" and len(warnings(journals[0], "tests")) == 1
    assert sh(repo.root, "rev-parse", "main") == before and status(repo) == "todo" and not delivered(repo)
    assert auto_merge.last(repo)["fusion"] is None and "corrige les tests" in branch_commands.auto_text(r)


def test_the_merged_commit_is_the_one_checked(repo, monkeypatch):
    """A commit added to the branch after the checks is not merged (head pinned before the trial merge)."""
    branch(repo, "todo-5/feat")
    real = branches.trial_merge

    def trial(settings, name, root, rev, on_tests=None):
        out = real(settings, name, root, rev, on_tests)
        sh(root, "switch", "-q", "todo-5/feat")
        commit(root, "late.txt", "tard\n", "ajout tardif")
        sh(root, "switch", "-q", "main")
        return out
    monkeypatch.setattr(branches, "trial_merge", trial)
    r = auto_merge.auto_merge(repo, 5, restart=lambda: None)
    assert r["merged"] and not (repo.root / "late.txt").exists() and r["cleanup"]  # moved branch kept, reported



# -- todo #482: the autonomous week (SECURITY only, other repositories too) -----------------------------------------

def test_the_authorisation_ends_after_its_last_day_paris_time(repo):
    assert auto_merge.autonomous_week(repo, DURING) and not auto_merge.autonomous_week(repo, AFTER)
    assert autorisation.valide(repo, GIVEN)["jusqu_au"] == "2026-11-08"


def test_without_authorisation_the_sensitive_rules_apply(repo, journals):
    autorisation.retirer(repo)
    assert not auto_merge.autonomous_week(repo, DURING)
    branch(repo, "todo-5/feat", "tests/conftest.py", "# changé\n")
    refused(repo, journals, "auto-sensitive", now=DURING)
    assert "Autorisation de fusion retirée" in entries(repo)


def test_a_forged_file_is_refused(repo):
    path = repo.var_dir / autorisation.FILE_NAME
    entry = json.loads(path.read_text())
    path.write_text(json.dumps({**entry, "jusqu_au": "2099-12-31"}))
    assert autorisation.valide(repo, DURING) is None
    path.write_text(json.dumps({**entry, "sceau": "0" * 64}))
    assert autorisation.valide(repo, DURING) is None
    path.write_text("pas du json")
    assert autorisation.valide(repo, DURING) is None


def test_no_key_means_no_authorisation(repo, close_key_path):
    close_key_path.unlink()
    assert autorisation.valide(repo, DURING) is None


def test_a_changed_security_list_voids_the_authorisation(repo, monkeypatch):
    assert autorisation.valide(repo, DURING)
    monkeypatch.setattr(auto_merge, "SECURITY", auto_merge.SECURITY[:-1])
    assert autorisation.valide(repo, DURING) is None


def test_durations_and_perimeter_are_checked(repo):
    with pytest.raises(InvalidRequestError):
        autorisation.check(5, autorisation.perimetre())
    with pytest.raises(InvalidRequestError):
        autorisation.check(7, "0123456789abcdef")
    give(repo, jours=1)
    assert autorisation.valide(repo, GIVEN)["jusqu_au"] == "2026-11-02"
    assert autorisation.valide(repo, GIVEN + 86400) is None


def test_cli_gives_with_a_signed_token_refuses_replay_and_withdraws(repo, journals, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    autorisation.retirer(repo)
    code, out = run_cli(capsys, "branch", "autorisation", "etat")
    assert code == 0 and out["data"]["valide"] is False and out["data"]["jours"] == [1, 3, 7, 14]
    perim = out["data"]["perimetre"]
    assert perim == autorisation.perimetre() and len(perim) == 16
    code, out = run_cli(capsys, "branch", "autorisation", "donner", "--jours", "3", "--perimetre", perim)
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "autorisation donner" in out["error"]
    key = close_key.load_or_create()
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("autorisation", f"{perim}:7", key=key))
    assert run_cli(capsys, "branch", "autorisation", "donner", "--jours", "3", "--perimetre", perim)[0] == 1  # other target
    code, out = run_cli(capsys, "branch", "autorisation", "donner", "--jours", "7", "--perimetre", "0123456789abcdef")
    assert code == 1 and "périmètre" in out["error"]
    code, out = run_cli(capsys, "branch", "autorisation", "donner", "--jours", "7", "--perimetre", perim)
    assert code == 0 and out["data"]["by"] == "telegram-button" and out["data"]["jours"] == 7
    assert autorisation.valide(repo) and "Autorisation de fusion donnée" in entries(repo)[-1]
    code, out = run_cli(capsys, "branch", "autorisation", "donner", "--jours", "7", "--perimetre", perim)
    assert code == 1 and "déjà utilisé" in out["error"]
    code, out = run_cli(capsys, "branch", "autorisation", "etat")
    assert out["data"]["valide"] is True and out["data"]["by"] == "telegram-button"
    monkeypatch.delenv(closing.TOKEN_ENV)
    code, out = run_cli(capsys, "branch", "autorisation", "retirer")
    assert code == 0 and out["data"]["retiree"] is True and autorisation.valide(repo) is None
    assert [e["type"] for e in autorisation._entries(repo)][-3:] == ["retrait", "don", "retrait"]


def test_security_is_a_subset_of_what_stays_refused_and_keeps_its_own_rules():
    assert {"deploy/priv/", "src/ultron_hub/todo/closing.py", "hermes_plugin/close_key.py",
            "src/ultron_hub/dashboard/auth.py", "src/ultron_hub/auto_merge.py"} <= set(auto_merge.SECURITY)
    assert auto_merge.security(["deploy/bin/ultron", "src/ultron_hub/executors/claude_cli.py", "tests/conftest.py",
                                "src/ultron_hub/branches.py", "README.md"]) == []
    assert auto_merge.security(["a/.env", "config/x.key", "b/auth.json", "c/my_token.json", "src/ultron_hub/bot/run.py",
                                "deploy/priv/ultron-priv"]) == ["a/.env", "config/x.key", "b/auth.json",
                                                                "c/my_token.json", "src/ultron_hub/bot/run.py",
                                                                "deploy/priv/ultron-priv"]


@pytest.mark.parametrize("path", [s + "x.sh" if s.endswith("/") else s for s in auto_merge.SECURITY]
                         + ["app/.env", "src/cle.key", "x/credentials.json"])
def test_security_files_are_refused_during_the_week(repo, journals, path):
    branch(repo, "todo-5/feat", path, "changé\n")
    text = refused(repo, journals, "auto-sensitive", now=DURING)
    assert f"• {path}" in text and "sécurité" in text and "todo-5/feat" in sh(repo.root, "branch")


@pytest.mark.parametrize("path", ["deploy/bin/ultron", "src/ultron_hub/executors/claude_cli.py", "tests/conftest.py",
                                  "src/ultron_hub/branches.py", "src/ultron_hub/repos.py", ".claude/settings.json"])
def test_formerly_sensitive_files_merge_during_the_week_and_not_after(repo, journals, path):
    branch(repo, "todo-5/feat", path, "# changé\n")
    assert auto_merge.refusals(repo, 5, branches.targets(repo, 5), now=AFTER)[0]["step"] == "auto-sensitive"
    refused(repo, journals, "auto-sensitive", now=AFTER)
    restarts = []
    r = auto_merge.auto_merge(repo, 5, restart=lambda: restarts.append(1), now=DURING)
    assert r["merged"] and not r["closed"] and restarts == [1] and (repo.root / path).read_text() == "# changé\n"
    assert status(repo) == "todo" and delivered(repo)


def other_repo(settings, name="alpha", tested=True):
    """A second repository next to the project, with a passing test, and a todo-5/… branch tagged for #5."""
    other = settings.root.parent / name
    other.mkdir()
    sh(other, "init", "-q", "-b", "main")
    sh(other, "config", "user.name", "t"); sh(other, "config", "user.email", "t@t")
    commit(other, ".gitignore", ".venv/\n", "ignore")
    commit(other, "tests/test_o.py", PASSING, "tests")
    (other / ".venv").symlink_to(settings.root / ".venv")
    sh(other, "switch", "-qc", "todo-5/feat", "main")
    commit(other, "b.txt", "b\n", "travail (todo #5)")
    sh(other, "switch", "-q", "main")
    if not tested:
        return settings, other
    return dataclasses.replace(settings, repo_tests={**settings.repo_tests, name: settings.repo_tests["proj"]}), other


def test_a_branch_of_another_repository_merges_during_the_week_without_restart(repo, journals):
    settings, other = other_repo(repo)
    refused(settings, journals, "auto-multi-repo", now=AFTER)
    r = auto_merge.auto_merge(settings, 5, restart=lambda: pytest.fail("no restart"), now=DURING)
    assert r["merged"] and not r["closed"] and r["restart"] is None and r["check"]["tested"]
    assert (other / "b.txt").is_file() and "todo-5/feat" not in sh(other, "branch")
    assert sh(other, "log", "-1", "--format=%s", "main") == "Fusion automatique de todo-5/feat (todo #5, reste ouverte)"
    assert status(settings) == "todo" and delivered(settings)
    assert [x["repo"] for x in auto_merge.last(settings)["fusion"]["repos"]] == ["alpha"]


def test_another_repository_and_ultron_hub_together_during_the_week(repo):
    settings, other = other_repo(repo)
    branch(settings, "todo-5/feat")
    restarts = []
    r = auto_merge.auto_merge(settings, 5, restart=lambda: restarts.append(1), now=DURING)
    assert r["merged"] and restarts == [1] and (other / "b.txt").is_file() and (settings.root / "feature.txt").is_file()


def test_another_repository_keeps_the_security_rules_during_the_week(repo, journals):
    settings, other = other_repo(repo)
    sh(other, "switch", "-q", "todo-5/feat")
    commit(other, "config/.env", "X=1\n", "secret (todo #5)")
    sh(other, "switch", "-q", "main")
    assert "• config/.env (alpha)" in refused(settings, journals, "auto-sensitive", now=DURING)
    assert "todo-5/feat" in sh(other, "branch")


def test_another_repository_without_a_test_command_is_refused_during_the_week(repo, journals):
    settings, other = other_repo(repo, tested=False)
    assert "commande de test" in refused(settings, journals, "auto-untested", now=DURING)
    assert "todo-5/feat" in sh(other, "branch")


def test_failing_tests_of_another_repository_merge_nothing(repo, journals):
    settings, other = other_repo(repo)
    sh(other, "switch", "-q", "todo-5/feat")
    commit(other, "tests/test_ko.py", "def test_ko():\n    assert False\n", "casse (todo #5)")
    sh(other, "switch", "-q", "main")
    before = sh(other, "rev-parse", "main")
    r = auto_merge.auto_merge(settings, 5, restart=lambda: pytest.fail("no restart"), now=DURING)
    assert not r["merged"] and r["check"]["stage"] == "tests" and sh(other, "rev-parse", "main") == before
    assert not delivered(settings) and auto_merge.last(settings)["fusion"] is None

# -- ↩️ revert of the last automatic merge -------------------------------------------------------------------------

HUMAN = {"by": "telegram-button", "proof": "abc"}


def test_revert_last_undoes_the_merge_and_keeps_the_todo_open(repo):
    branch(repo, "todo-5/feat")
    auto_merge.auto_merge(repo, 5, restart=lambda: None)
    cible = auto_merge.last(repo)["cible"]
    restarts = []
    r = auto_merge.revert_last(repo, HUMAN, cible=cible, restart=lambda: restarts.append(1))
    assert not (repo.root / "feature.txt").exists() and restarts == [1] and r["restart"] == "planned"
    assert sh(repo.root, "rev-parse", "main") == r["repos"][0]["revert"]
    assert sh(repo.root, "log", "-1", "--format=%s", "main").startswith("Annulation de la fusion automatique")
    assert status(repo) == "todo" and not delivered(repo) and auto_merge.last(repo)["annulee"]
    assert not sh(repo.root, "status", "--porcelain", "--untracked-files=no")
    with pytest.raises(InvalidRequestError, match="déjà annulée"):
        auto_merge.revert_last(repo, HUMAN, restart=lambda: None)


def test_revert_passes_when_main_moved_without_conflict(repo):
    branch(repo, "todo-5/feat")
    auto_merge.auto_merge(repo, 5, restart=lambda: None)
    commit(repo.root, "other.txt", "o\n", "autre travail")
    auto_merge.revert_last(repo, HUMAN, restart=lambda: None)
    assert not (repo.root / "feature.txt").exists() and (repo.root / "other.txt").exists()


def test_revert_refused_on_conflict_and_main_untouched(repo):
    branch(repo, "todo-5/feat")
    auto_merge.auto_merge(repo, 5, restart=lambda: None)
    commit(repo.root, "feature.txt", "x\ny\n", "main change le fichier fusionné")
    before = sh(repo.root, "rev-parse", "main")
    with pytest.raises(InvalidRequestError, match="sans conflit"):
        auto_merge.revert_last(repo, HUMAN, restart=lambda: pytest.fail("no restart"))
    assert sh(repo.root, "rev-parse", "main") == before and not auto_merge.last(repo)["annulee"]
    assert not sh(repo.root, "status", "--porcelain", "--untracked-files=no")
    assert not any((repo.var_dir / "merge-check").iterdir())


def test_revert_refused_for_another_target_nothing_to_revert_or_dirty_main(repo):
    with pytest.raises(InvalidRequestError, match="aucune fusion automatique"):
        auto_merge.revert_last(repo, HUMAN, restart=lambda: None)
    branch(repo, "todo-5/feat")
    auto_merge.auto_merge(repo, 5, restart=lambda: None)
    with pytest.raises(InvalidRequestError, match="n'est plus"):
        auto_merge.revert_last(repo, HUMAN, cible="5-0123456789ab", restart=lambda: None)
    (repo.root / "tests" / "test_a.py").write_text(PASSING + "# en cours\n")
    with pytest.raises(InvalidRequestError, match="non commitées"):
        auto_merge.revert_last(repo, HUMAN, restart=lambda: None)
    assert (repo.root / "feature.txt").exists()


def test_revert_targets_only_the_last_automatic_merge(repo):
    branch(repo, "todo-5/feat")
    auto_merge.auto_merge(repo, 5, restart=lambda: None)
    branch(repo, "todo-4/other", "other.txt")
    auto_merge.auto_merge(repo, 4, restart=lambda: None)
    assert auto_merge.last(repo)["cible"].startswith("4-")
    auto_merge.revert_last(repo, HUMAN, restart=lambda: None)
    assert (repo.root / "feature.txt").exists() and not (repo.root / "other.txt").exists()
    with pytest.raises(InvalidRequestError, match="déjà annulée"):  # never walks back to #5 by itself
        auto_merge.revert_last(repo, HUMAN, restart=lambda: None)


# -- CLI: auto-merge unsigned, revert-last signed --------------------------------------------------------------------

def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cli_auto_merge_needs_no_token(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setattr(branches, "restart_gateway", lambda: None)
    branch(repo, "todo-5/feat")
    code, out = run_cli(capsys, "branch", "auto-merge", "5")
    assert code == 0 and out["data"]["merged"] and not out["data"]["closed"] and status(repo) == "todo"
    code, out = run_cli(capsys, "branch", "auto-last")
    assert code == 0 and out["data"]["cible"].startswith("5-") and out["data"]["annulee"] is False


def test_cli_revert_last_requires_the_signed_proof(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setattr(branches, "restart_gateway", lambda: None)
    branch(repo, "todo-5/feat")
    auto_merge.auto_merge(repo, 5, restart=lambda: None)
    cible = auto_merge.last(repo)["cible"]
    code, out = run_cli(capsys, "branch", "revert-last", "--cible", cible)
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "revert-last" in out["error"]
    for token in (close_key.token("done", 5), close_key.token("merge", 5), close_key.token("revert-fusion", "5-0123456789ab"),
                  close_key.token("revert-fusion", cible, now=time.time() - 600)):
        monkeypatch.setenv(closing.TOKEN_ENV, token)
        assert run_cli(capsys, "branch", "revert-last", "--cible", cible)[0] == 1
    assert (repo.root / "feature.txt").exists() and not auto_merge.last(repo)["annulee"]
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("revert-fusion", cible))
    code, out = run_cli(capsys, "branch", "revert-last", "--cible", cible)
    assert code == 0 and "annulée" in out["data"]["text"] and not (repo.root / "feature.txt").exists()
    [entry] = [e for e in auto_merge._entries(repo) if e["type"] == "revert"]
    assert entry["by"] == "telegram-button" and entry["proof"]
    [ev] = [h for h in history(repo) if h["action"] == "fusion-annulee"]
    assert ev["details"]["by"] == "telegram-button" and ev["details"]["cible"] == cible
    code, out = run_cli(capsys, "branch", "revert-last", "--cible", cible)  # replayed
    assert code == 1 and "déjà annulée" in out["error"]


def test_cli_revert_last_accepts_the_dashboard_proof(repo, capsys, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setattr(branches, "restart_gateway", lambda: None)
    branch(repo, "todo-5/feat")
    auto_merge.auto_merge(repo, 5, restart=lambda: None)
    cible = auto_merge.last(repo)["cible"]
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.dashboard_token("telephone-test", "revert-fusion", cible))
    code, out = run_cli(capsys, "branch", "revert-last", "--cible", cible)
    assert code == 0 and auto_merge._entries(repo)[-1]["by"] == "dashboard:telephone-test"


def test_revert_fusion_is_a_signed_action_on_both_sides():
    assert "revert-fusion" in closing.ACTIONS and close_key.ACTIONS == closing.ACTIONS
    assert "revert-fusion" in closing.DASHBOARD_ACTIONS and close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    key = b"k" * 64
    tok = close_key.token("revert-fusion", "5-0123456789ab", key=key, now=1000)
    assert closing.check(tok, "revert-fusion", "5-0123456789ab", key=key, now=1000)[1] == "telegram-button"
    with pytest.raises(closing.CloseRefused):
        closing.check(tok, "merge", "5-0123456789ab", key=key, now=1000)


# Todo #505: a CLAUDE.md (project rules) never merges by itself, at the root or below, even during the authorisation.
def test_claude_md_is_sensitive_and_security_by_name_only():
    paths = ["CLAUDE.md", "docs/CLAUDE.md", "a/b/CLAUDE.md"]
    assert auto_merge.sensitive(paths) == paths and auto_merge.security(paths) == paths
    other = ["CLAUDE.md.txt", "notCLAUDE.md", "docs/claude.md", "CLAUDE.md/x.txt"]
    assert auto_merge.sensitive(other) == [] and auto_merge.security(other) == []


@pytest.mark.parametrize("path", ["CLAUDE.md", "docs/CLAUDE.md"])
@pytest.mark.parametrize("now", [AFTER, DURING])
def test_claude_md_refuses_the_automatic_merge_with_or_without_authorisation(repo, journals, path, now):
    branch(repo, "todo-5/feat", path, "règle\n")
    assert f"• {path}" in refused(repo, journals, "auto-sensitive", now=now)
