"""Todo #140: progression des fusions en direct (fichier d'avancement, plugin, message après redémarrage)."""

import asyncio
import json
import os
import stat
import subprocess
import time

from ultron_hub import branch_lot, branches, merge_progress, repos

from test_branches import branch, no_bwrap, repo, status  # noqa: F401  (fixtures)


def read_state(settings):
    return json.loads(merge_progress.path(settings).read_text())


# -- côté commande ------------------------------------------------------------------------------------

def test_lot_of_two_writes_the_expected_steps_in_order(repo, monkeypatch):
    branch(repo, "todo-1/a", "a.txt")
    branch(repo, "todo-2/b", "b.txt")
    seen = []
    real = repos.run_tests

    def slow_tests(settings, name, root, tree):
        state = read_state(settings)
        seen.append((state["todo"], state["step"], sorted(state["done"])))
        return real(settings, name, root, tree)
    monkeypatch.setattr(repos, "run_tests", slow_tests)
    steps = []
    real_step = merge_progress.Progress.step
    monkeypatch.setattr(merge_progress.Progress, "step",
                        lambda self, t, s: (steps.append((t, s)), real_step(self, t, s))[1])
    data = branch_lot.lot(repo)
    restarts = []
    r = branch_lot.run(repo, data, by={"by": "t"}, restart=lambda: restarts.append(1))
    assert r["restart"] == "planned" and restarts == [1]
    assert steps == [(1, "essai"), (1, "tests"), (1, "fusion"), (1, "cloture"),
                     (2, "essai"), (2, "tests"), (2, "fusion"), (2, "cloture")]
    assert seen == [(1, "tests", []), (2, "tests", ["1"])]  # #1 is already done when #2 is tested
    state = read_state(repo)
    assert state["ids"] == [1, 2] and sorted(state["done"]) == ["1", "2"] and state["restart"] is True
    assert stat.S_IMODE(os.stat(merge_progress.path(repo)).st_mode) == 0o600
    assert merge_progress.merged_ids(repo) == [1, 2]
    assert not list(merge_progress.path(repo).parent.glob(".progress-*"))


def test_unwritable_progress_never_fails_the_merge(repo, monkeypatch):
    branch(repo, "todo-5/feat")

    def boom(*a, **k):
        raise OSError("disque plein")
    monkeypatch.setattr(merge_progress.os, "replace", boom)
    r = branches.merge_and_close(repo, 5, restart=lambda: None)
    assert r["merged"] and r["closed"] and status(repo) == "done"


def test_single_merge_marks_restart_only_when_the_bot_changes(repo):
    branch(repo, "todo-5/feat")
    branches.merge_and_close(repo, 5, restart=lambda: None)
    assert read_state(repo)["restart"] is True and read_state(repo)["done"].keys() == {"5"}


# -- restart_gateway ----------------------------------------------------------------------------------

def test_restart_gateway_command_sends_the_final_message(repo, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(branches.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(branches, "resolve_hermes", lambda b: "/home/exemple2/.local/bin/hermes")
    monkeypatch.setenv("ULTRON_HUB_ROOT", str(repo.root))
    p = merge_progress.Progress(repo, [141, 142])
    p.step(141, "tests"); p.done(141); p.step(142, "tests"); p.done(142)
    branches.restart_gateway()
    [cmd] = calls
    assert cmd[:5] == ["systemd-run", "--user", "--collect", "--quiet", "--on-active=5"]
    assert cmd[5:7] == ["/bin/sh", "-c"] and "/usr/bin/systemctl --user restart hermes-gateway" in cmd[7]
    assert 'send -t telegram "$2"' in cmd[7] and 'send -t telegram "$3 (code $rc)"' in cmd[7]
    assert cmd[9:] == ["/home/exemple2/.local/bin/hermes", "✅ Hermes redémarré (fusion de #141, #142)",
                       "❌ Hermes ne redémarre pas"]


def test_restart_gateway_without_hermes_keeps_the_plain_restart(monkeypatch):
    monkeypatch.setenv("ULTRON_ENVOI_MODE", "hermes")
    calls = []
    monkeypatch.setattr(branches.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))

    def missing(b):
        raise FileNotFoundError
    monkeypatch.setattr(branches, "resolve_hermes", missing)
    branches.restart_gateway()
    assert calls[0][5:] == ["/usr/bin/systemctl", "--user", "restart", "hermes-gateway"]


# -- côté plugin --------------------------------------------------------------------------------------





def lot(chat=1):
    return {"origin": "a", "out": set(), "ids": [1, 2], "empreinte": "0" * 16}








