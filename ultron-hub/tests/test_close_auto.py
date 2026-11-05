"""Verified automatic close of a todo merged alone (todo #528): `ultron-hub todo close-auto <id>`. Throwaway repos only."""

import json

import pytest

from test_branches import branch, commit, journals, no_bwrap, repo, sh, status  # noqa: F401
from ultron_hub import auto_merge, branches, cli
from ultron_hub.esprit import store as esprit
from ultron_hub.todo import closing
from ultron_hub.todo.store import TodoStore


@pytest.fixture(autouse=True)
def no_job(monkeypatch):
    for name in auto_merge.JOB_ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cli_root(repo, monkeypatch):
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    monkeypatch.setattr(branches, "restart_gateway", lambda: None)
    return repo


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def history(settings, todo_id=5):
    with TodoStore(settings.todo_db) as todos:
        return todos.history(todo_id)


def refused(settings, journals, capsys, step, todo_id=5):
    """The CLI refuses: error JSON, one WARNING in the log, a journal line, the todo still open."""
    code, out = run_cli(capsys, "todo", "close-auto", str(todo_id))
    assert code == 1 and out["status"] == "error" and "Clôture auto refusée" in out["error"]
    log = settings.logs_dir / "ultron-hub.log"
    lines = [line for line in log.read_text(encoding="utf-8").splitlines()
             if " WARNING ultron_hub.closing: " in line and f"step={step} " in line]
    assert len(lines) == 1 and f"todo={todo_id} step={step} reason=" in lines[0]
    assert any(e["texte"].startswith("Clôture auto refusée") for e in esprit.tail(settings, 20))
    assert status(settings, todo_id) == "todo"
    assert not any(h["action"] == "status" for h in history(settings, todo_id))
    return out["error"]


def test_merged_and_verified_todo_is_closed_by_auto_verifie(cli_root, journals, capsys):
    branch(cli_root, "todo-5/feat")
    auto_merge.auto_merge(cli_root, 5, restart=lambda: None)
    sha = sh(cli_root.root, "rev-parse", "main")
    code, out = run_cli(capsys, "todo", "close-auto", "5")
    assert code == 0 and out["status"] == "ok" and out["command"] == "todo.close-auto"
    assert status(cli_root) == "done"
    [ev] = [h for h in history(cli_root) if h["action"] == "status"]
    d = ev["details"]
    assert d["by"] == closing.AUTO_VERIFIED_ACTOR == "auto:verifie" and d["to"] == "done"
    assert d["proof"] == sha[:12] and d["fusion"] == [{"repo": "proj", "sha": sha}] and d["cible"] == f"5-{sha[:12]}"
    assert any("terminée seule" in e["texte"] for e in esprit.tail(cli_root, 20))
    assert "Clôture auto refusée" in run_cli(capsys, "todo", "close-auto", "5")[1]["error"]  # already closed


def test_refused_when_the_branch_was_not_merged(cli_root, journals, capsys):
    branch(cli_root, "todo-5/feat")
    assert "non fusionnée" in refused(cli_root, journals, capsys, "close-auto-unmerged")


def test_refused_when_merged_by_hand_without_the_tested_path(cli_root, journals, capsys):
    """A `Fusion de todo-5/…` commit made with git directly: no record of the tests, no close."""
    branch(cli_root, "todo-5/feat")
    sh(cli_root.root, "merge", "-q", "--no-ff", "-m", "Fusion de todo-5/feat (todo #5 terminée)", "todo-5/feat")
    sh(cli_root.root, "branch", "-qD", "todo-5/feat")
    refused(cli_root, journals, capsys, "close-auto-unmerged")


def test_refused_without_the_proof_of_the_tested_merge(cli_root, journals, capsys):
    branch(cli_root, "todo-5/feat")
    auto_merge.auto_merge(cli_root, 5, restart=lambda: None)
    with TodoStore(cli_root.todo_db) as todos:  # a later delivery by hand hides the one of the tested merge
        todos.mark_delivered(5, by="cli", note="autre chose")
    assert "pas de preuve" in refused(cli_root, journals, capsys, "close-auto-no-proof")


def test_refused_after_the_merge_was_reverted(cli_root, journals, capsys):
    branch(cli_root, "todo-5/feat")
    auto_merge.auto_merge(cli_root, 5, restart=lambda: None)
    auto_merge.revert_last(cli_root, {"by": "cli-human"}, restart=lambda: None)
    refused(cli_root, journals, capsys, "close-auto-reverted")


def test_refused_when_a_new_branch_waits(cli_root, journals, capsys):
    branch(cli_root, "todo-5/feat")
    auto_merge.auto_merge(cli_root, 5, restart=lambda: None)
    branch(cli_root, "todo-5/suite", "suite.txt")
    refused(cli_root, journals, capsys, "close-auto-branch")


def test_refused_when_the_merged_diff_touches_security(cli_root, journals, capsys):
    """A secret-looking name merges without the user's authorisation (SENSITIVE), but SECURITY keeps the user's ✅."""
    branch(cli_root, "todo-5/feat", "notes/mon-secret.txt", "x\n")
    assert auto_merge.auto_merge(cli_root, 5, restart=lambda: None)["merged"]
    text = refused(cli_root, journals, capsys, "close-auto-security")
    assert "notes/mon-secret.txt" in text


def merged_then_refused(cli_root, journals, capsys, path):
    branch(cli_root, "todo-5/feat", path, "x\n")
    assert auto_merge.auto_merge(cli_root, 5, restart=lambda: None)["merged"]
    assert path in refused(cli_root, journals, capsys, "close-auto-security")


@pytest.mark.parametrize("path", [".env", "notes/api-token.txt", "notes/ma.key", "notes/x.pem"])
def test_refused_when_the_merged_diff_touches_a_secret_name(cli_root, journals, capsys, path):
    """auto_merge.security covers SECRET_NAMES in any folder."""
    merged_then_refused(cli_root, journals, capsys, path)


@pytest.mark.parametrize("path", ["src/ultron_hub/envoi.py", "src/ultron_hub/bot/doors.py", "src/ultron_hub/dashboard/push.py"])
def test_refused_when_the_merged_diff_sends_to_a_third_party(cli_root, journals, capsys, path):
    """Telegram, the direct bot and Web Push are in SECURITY."""
    merged_then_refused(cli_root, journals, capsys, path)


@pytest.mark.parametrize("path", ["src/ultron_hub/smtp_client.py", "src/ultron_hub/mail/gmail.py", "scripts/sendmail.sh"])
def test_refused_when_the_merged_diff_sends_mail(cli_root, journals, capsys, path):
    """Mail sending is not in SECURITY: AUTO_CLOSE_EXTRA refuses it."""
    merged_then_refused(cli_root, journals, capsys, path)


@pytest.mark.parametrize("path", ["src/ultron_hub/paiement.py", "src/ultron_hub/stripe_client.py", "docs/Facture.py",
                                  "src/ultron_hub/virement.py"])
def test_refused_when_the_merged_diff_touches_money(cli_root, journals, capsys, path):
    """Payments and invoices: AUTO_CLOSE_EXTRA refuses them."""
    merged_then_refused(cli_root, journals, capsys, path)


@pytest.mark.parametrize("env", ["ULTRON_HUB_JOB", "ULTRON_ESCOUADE_DIR"])
def test_refused_from_a_job_or_a_squad(cli_root, journals, capsys, monkeypatch, env):
    branch(cli_root, "todo-5/feat")
    auto_merge.auto_merge(cli_root, 5, restart=lambda: None)
    monkeypatch.setenv(env, "1")
    assert "depuis un job ou une escouade" in refused(cli_root, journals, capsys, "close-auto-mission")


def test_verify_never_writes(cli_root):
    """The check alone closes nothing: only the CLI writes the status, after it."""
    branch(cli_root, "todo-5/feat")
    auto_merge.auto_merge(cli_root, 5, restart=lambda: None)
    with TodoStore(cli_root.todo_db) as todos:
        by = closing.verify_auto_close(cli_root, todos, 5, env={})
    assert by["by"] == "auto:verifie" and status(cli_root) == "todo"
