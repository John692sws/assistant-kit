"""Hooks Stop/PostToolUse/PreToolUse des sessions Claude Code (todo #96, réduits au retrait de l'escouade autonome, #458)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ultron_hub.escouade import hooks

SOCLE = "## 2. A\n{limite} {drapeau}\n## 3. B\nb\n## 5. C\n{fiche}\n## 6. D\nd\n## 7. E\n{sujet}\n"
SUJET = "# S\n## 4. Pistes\n- a\n"


@pytest.fixture
def prompts(tmp_path):
    folder = tmp_path / "prompt"
    folder.mkdir()
    (folder / "_socle.md").write_text(SOCLE)
    (folder / "demo.md").write_text(SUJET)
    return folder


@pytest.fixture
def squad(tmp_path, prompts):
    d = tmp_path / "esc" / "demo-1"
    d.mkdir(parents=True)
    (d / "etat.json").write_text(json.dumps({"id": "demo-1", "sujet": "demo", "debut_ts": time.time() - 100,
                          "prompts_dir": str(prompts), "statut": "en_cours"}))
    return d


def stop(squad, active=False, msg="x"):
    return hooks.stop_decision(squad, {"stop_hook_active": active, "last_assistant_message": msg})


def do_handover(prompts):
    (prompts / "demo.md").write_text("réécrit")
    (prompts / "archives" / "demo").mkdir(parents=True)
    (prompts / "archives" / "demo" / "a.md").write_text("old")


def read_etat(squad):
    return json.loads((squad / "etat.json").read_text())


def write_etat(squad, etat):
    (squad / "etat.json").write_text(json.dumps(etat))


# -- Stop hook ---------------------------------------------------------------------------------------------

def test_stop_without_flag_blocks_with_continue(squad):
    assert stop(squad) == {"decision": "block", "reason": hooks.REASON_CONTINUE}


def test_stop_with_flag_blocks_until_handover(squad):
    (squad / "drapeau-passation").touch()
    assert stop(squad) == {"decision": "block", "reason": "Fais la passation maintenant (section 7)."}


def test_stop_allows_after_handover_and_writes_status(squad, prompts):
    (squad / "drapeau-passation").touch()
    do_handover(prompts)
    assert stop(squad) == {}
    assert read_etat(squad)["statut"] == "passation_ok"


def test_handover_archive_copied_with_its_old_date(squad, prompts):
    """cp -p keeps the subject's old mtime on the archive: its ctime is new (found by the #102 trial)."""
    (squad / "drapeau-passation").touch()
    old = time.time() - 1000
    (prompts / "archives" / "demo").mkdir(parents=True)
    archive = prompts / "archives" / "demo" / "a.md"
    archive.write_text("old")
    os.utime(archive, (old, old))
    (prompts / "demo.md").write_text("réécrit")
    assert stop(squad) == {} and read_etat(squad)["statut"] == "passation_ok"


def test_stop_needs_both_subject_and_archive(squad, prompts):
    (squad / "drapeau-passation").touch()
    (prompts / "demo.md").write_text("réécrit")  # no archive yet
    assert stop(squad)["decision"] == "block"


def test_stop_ignores_old_archive(squad, prompts):
    """An archive of a previous session (created before this session started) does not count."""
    (squad / "drapeau-passation").touch()
    (prompts / "archives" / "demo").mkdir(parents=True)
    (prompts / "archives" / "demo" / "a.md").write_text("old")
    start = time.time() + 5  # this session starts after the archive was created (mtime and ctime)
    write_etat(squad, {**read_etat(squad), "session_debut_ts": start})
    subject = prompts / "demo.md"
    subject.write_text("réécrit")
    os.utime(subject, (start + 10, start + 10))
    assert stop(squad)["decision"] == "block"


def test_stop_anti_loop_same_message(squad):
    results = [stop(squad, active=i > 0, msg="même réponse") for i in range(6)]
    assert [r.get("decision") for r in results[:3]] == ["block"] * 3
    assert results[hooks.MAX_SAME_BLOCKS] == {}
    assert read_etat(squad)["statut"] == "boucle_stoppee"


def test_stop_different_messages_keep_going(squad):
    for i in range(10):
        assert stop(squad, active=i > 0, msg=f"piste {i}")["decision"] == "block"


def test_stop_total_cap(squad, monkeypatch):
    monkeypatch.setattr(hooks, "MAX_TOTAL_BLOCKS", 4)
    out = [stop(squad, active=i > 0, msg=f"m{i}") for i in range(6)]
    assert out[3]["decision"] == "block" and out[4] == {}


# -- PostToolUse -------------------------------------------------------------------------------------------

def test_post_context_only_with_flag(squad):
    assert hooks.post_decision(squad, {"tool_name": "Read"}) == {}
    (squad / "drapeau-passation").touch()
    out = hooks.post_decision(squad, {"tool_name": "Read"})["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse" and out["additionalContext"].startswith("PASSATION MAINTENANT")


# -- PreToolUse --------------------------------------------------------------------------------------------

def branch(name):
    return lambda directory: name


@pytest.mark.parametrize("command", [
    "sudo true", "ls && sudo rm x", "echo a; sudo id", "cat f | sudo tee x", "FOO=1 sudo id", "env sudo id",
    "bash -c 'sudo id'", "echo $(sudo id)", "echo `sudo id`", "(sudo id)",
    "git push", "git push origin x", "git -C /tmp/x push", "cd a && git push", "git merge todo-1/x",
    "git commit -m x", "git rebase main", "git reset --hard", "git -c user.name=x commit -m y",
    "ultron-hub todo done 5", "ultron-hub todo cancel 5", ".venv/bin/ultron-hub todo done 5",
    "ultron-hub branch merge 5", "ultron-hub escouade run x --limite 1h", "ultron-hub mission start 3",
    "systemctl stop foo", "systemctl --user stop foo", "systemctl status foo", "systemctl --user restart x",
    "systemd-run --user x",
])
def test_pre_denies(command):
    reason = hooks.check_command(command, "/w", branch_of=branch("main"))
    assert reason, command


@pytest.mark.parametrize("command", [
    "ls -la", "git status", "git log --oneline", "git diff", "git add -A", "echo 'sudo' > /dev/null",
    "grep sudo file", "echo \"git push\"", "ultron-hub usage", "ultron-hub todo list", "ultron-hub todo show 3",
    "ultron-hub escouade passation x", "ultron-hub escouade etat", ".venv/bin/pytest -q && git status",
    "systemctl --user status foo", "systemctl --user is-active foo", "hermes send -t telegram hi",
])
def test_pre_allows(command):
    assert hooks.check_command(command, "/w", branch_of=branch("main")) is None, command


def test_pre_git_commit_depends_on_branch():
    assert hooks.check_command("git commit -m x", "/w", branch_of=branch("todo-96/x")) is None
    assert hooks.check_command("git rebase x", "/w", branch_of=branch("todo-96/x")) is None
    for b in ("main", "master"):
        assert hooks.check_command("git reset --hard", "/w", branch_of=branch(b))


def test_pre_git_branch_uses_real_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    assert hooks.check_command("git commit -m x", str(tmp_path))
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "-b", "todo-1/x"], check=True)
    assert hooks.check_command("git commit -m x", str(tmp_path)) is None


def test_pre_decision_format(squad):
    out = hooks.pre_decision(squad, {"tool_name": "Bash", "tool_input": {"command": "sudo true"}, "cwd": "/w"})
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny" and spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecisionReason"]
    assert hooks.pre_decision(squad, {"tool_name": "Read", "tool_input": {}}) == {}
    assert hooks.pre_decision(squad, {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/w"}) == {}


def test_hook_script_end_to_end(squad):
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "sudo id"}, "cwd": str(squad)})
    proc = subprocess.run([sys.executable, "-I", str(hooks.__file__), "pre", str(squad)], input=event,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0 and json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    bad = subprocess.run([sys.executable, "-I", str(hooks.__file__), "pre", str(squad)], input="pas du json",
                         capture_output=True, text=True, check=False)
    assert bad.returncode == 0 and bad.stdout == ""
