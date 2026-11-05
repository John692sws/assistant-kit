"""Todo #395: the merge-check-orphelin reflex flags throwaway merge worktrees older than 1 h (read-only)."""

import os
import subprocess

from ultron_hub.health import reflexes

inspecter = reflexes.inspecter_merge_check  # the real one: conftest replaces it during each test
NOW = 1_000_000_000.0


def _no_git(cmd):
    return subprocess.CompletedProcess(cmd, 1, "", "")


def _dir(tmp_path, name, age_h):
    d = tmp_path / "var" / "merge-check" / name
    d.mkdir(parents=True)
    os.utime(d, (NOW - age_h * 3600, NOW - age_h * 3600))
    return d


def test_old_worktree_is_flagged_recent_ignored(tmp_path):
    _dir(tmp_path, "wt-old", 120)
    _dir(tmp_path, "rv-old", 2)
    _dir(tmp_path, "wt-fresh", 0.2)
    _dir(tmp_path, "other", 500)
    res = inspecter(tmp_path, _no_git, NOW)
    assert res["ok"] and res["stale"]
    assert "wt-old" in res["reason"] and "rv-old" in res["reason"]
    assert "wt-fresh" not in res["reason"] and "other" not in res["reason"]
    assert (tmp_path / "var" / "merge-check" / "wt-old").is_dir()  # nothing removed


def test_only_recent_or_nothing_is_quiet(tmp_path):
    assert inspecter(tmp_path, _no_git, NOW)["stale"] is False
    _dir(tmp_path, "wt-fresh", 0.5)
    assert inspecter(tmp_path, _no_git, NOW)["stale"] is False


def test_registered_without_directory_is_flagged(tmp_path):
    base = (tmp_path / "var" / "merge-check").resolve()
    out = f"worktree {tmp_path}\n\nworktree {base}/wt-gone\nHEAD abc\ndetached\n\nworktree {tmp_path}/elsewhere\n"
    res = inspecter(tmp_path, lambda cmd: subprocess.CompletedProcess(cmd, 0, out, ""), NOW)
    assert res["stale"] and "wt-gone" in res["reason"] and "elsewhere" not in res["reason"]


def test_inspection_failure_gives_no_diagnosis(tmp_path):
    def boom(cmd):
        raise OSError("no git")
    assert inspecter(tmp_path, boom, NOW)["ok"] is False


def test_reflex_is_in_the_table_and_diagnoses():
    assert any(r["nom"] == "merge-check-orphelin" and r["verification"] == "merge_check_orphelin"
               for r in reflexes.charger())
    stale = {"ok": True, "stale": True, "reason": "wt-x (130.0 h)"}
    diags = reflexes.diagnostiquer({}, [reflexes.MERGE_CHECK_DEFAUT], inspect_merge_check=lambda: stale)
    assert diags[0]["nom"] == "merge-check-orphelin" and diags[0]["bouton"] is None and "wt-x" in diags[0]["detail"]
    assert reflexes.diagnostiquer({}, [reflexes.MERGE_CHECK_DEFAUT],
                                  inspect_merge_check=lambda: {"ok": True, "stale": False}) == []
