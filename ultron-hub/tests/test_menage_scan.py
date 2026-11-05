"""Tests of `ultron-hub menage scan` (todo #125): fake HOME under tmp_path, reduced YAML, no real disk."""

import argparse
import builtins
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ultron_hub.menage import checks, commands, rules

MB = 1024 * 1024
ACTIVE = "~/workspace/projects/active"


def base_config() -> dict:
    return {
        "version": 1,
        "exclusions": ["~/forum", "~/.ssh"],
        "secrets": ["~/.sudo_pass", "~/.hermes/.env", "**/auth.json"],
        "ignore_dirs": [".git", ".venv", "node_modules", "__pycache__"],
        "structure": {"expected": [], "allowed": {}, "obsolete": [], "abandoned": [],
                      "inactive_project_days": 45},
        "worktrees": {"projects_root": ACTIVE, "convention": "{projet}.worktrees/todo-{id}",
                      "pattern": r"^(?P<projet>[a-z0-9][a-z0-9-]*)\.worktrees/todo-(?P<id>[0-9]+)$",
                      "branch_pattern": r"^todo-(?P<id>[0-9]+)/.+$",
                      "merge_check": {"path": f"{ACTIVE}/hub/var/merge-check", "max_age_hours": 24}},
        "disposable": {"roots": ["~/workspace", "~/notes", "~/.hermes"], "patterns": [], "pycache": True,
                       "aged_dirs": [],
                       "logs": {"dirs": [], "files": [], "max_age_days": 30, "max_size_mb": 50},
                       "duplicates": {"min_size_mb": 1, "roots": ["~/workspace", "~/notes", "~/.hermes"],
                                      "skip": []}},
        "space": {"disk_warn_percent": 80, "disk_crit_percent": 90, "growth": {"pct": 20, "mb": 500},
                  "watched": [], "caches": []},
        "code": {"roots": ["~", "~/workspace"], "repos_root": "~/workspace/projects",
                 "systemd_user_dir": "~/.config/systemd/user",
                 "exec_allowed_prefixes": [f"{ACTIVE}/", "~/.local/bin/", "/usr/"],
                 "hermes_plugins_dir": "~/.hermes/plugins", "plugin_sources": {}},
        "budgets": {"files": {}},
    }


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def run(tmp_path, home):
    def go(probe=None, now=None, history=None, db=None, **sections):
        cfg = base_config()
        for name, values in sections.items():
            cfg[name].update(values)
        path = tmp_path / "menage.yaml"
        path.write_text(yaml.safe_dump(cfg))
        var = tmp_path / "var"
        if history is not None:
            (var / "menage").mkdir(parents=True, exist_ok=True)
            (var / "menage" / "history.jsonl").write_text("\n".join(json.dumps(x) for x in history) + "\n")
        settings = SimpleNamespace(root=tmp_path, var_dir=var, todo_db=db)
        return commands.run_scan(settings, home=str(home), config_path=path, probe=probe or (lambda: []),
                                 now=now)
    return go


def of(data, check):
    return [a for a in data["anomalies"] if a["check"] == check]


def write(path: Path, content: bytes | str = "x", age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    if age_days:
        age(path, age_days)
    return path


def age(path: Path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old), follow_symlinks=False)


def git_repo(path: Path) -> Path:
    (path / ".git" / "refs" / "heads").mkdir(parents=True)
    return path


# --- A -----------------------------------------------------------------------------------------------------------

def test_1_missing_expected_dir(run, home):
    (home / "notes").mkdir()
    data = run(structure={"expected": ["~/notes", "~/donnees"]})
    (found,) = of(data, 1)
    assert found["path"] == str(home / "donnees") and found["kind"] == "missing_dir"
    assert set(found) == {"check", "kind", "path", "detail", "action"}


def test_2_undeclared_dir_obsolete_and_abandoned(run, home):
    for name in ("workspace", "mystery", "forum"):
        (home / name).mkdir()
    (home / "logs").mkdir()  # obsolete and empty
    write(home / "workspace" / "agents" / "a.txt")  # obsolete, not empty
    write(home / "workspace" / "documents" / "old.md")
    data = run(structure={
        "allowed": {"~": {"dirs": ["workspace", "forum"], "files": []},
                    "~/workspace": {"dirs": ["documents"], "files": []}},
        "obsolete": ["~/logs", "~/workspace/agents"],
        "abandoned": [{"path": "~/workspace/documents/old.md", "reason": "remplacée"}]})
    by_kind = {a["kind"]: a for a in of(data, 2)}
    assert [a["path"] for a in of(data, 2) if a["kind"] == "undeclared_dir"] == [str(home / "mystery")]
    assert "vides, à retirer" in by_kind["obsolete_empty"]["detail"]
    assert by_kind["obsolete_empty"]["path"] == str(home / "logs")
    assert by_kind["obsolete_not_empty"]["path"] == str(home / "workspace" / "agents")
    assert "abandonné, à archiver" in by_kind["abandoned"]["detail"]


def test_3_stray_file(run, home):
    write(home / ".bashrc")
    write(home / "oops.txt")
    data = run(structure={"allowed": {"~": {"dirs": [], "files": [".bashrc"]}}})
    (found,) = of(data, 3)
    assert found["path"] == str(home / "oops.txt")


def test_4_inactive_project(run, home):
    active = home / "workspace" / "projects" / "active"
    old = write(active / "old" / "a.py", age_days=100)
    age(old.parent, 100)
    write(active / "fresh" / "a.py")
    write(active / "fresh.worktrees" / "todo-1" / "x.py", age_days=100)
    data = run()
    (found,) = of(data, 4)
    assert found["path"] == str(active / "old")


def test_4_git_head_time_wins_over_old_mtime(run, home):
    active = home / "workspace" / "projects" / "active"
    repo = git_repo(active / "proj")
    write(repo / "a.py", age_days=100)
    write(repo / ".git" / "logs" / "HEAD", f"0 1 U <u@x> {int(time.time())} +0000\tcommit: x\n", age_days=100)
    assert of(run(), 4) == []


# --- B -----------------------------------------------------------------------------------------------------------

def add_worktree(repo: Path, name: str, where: Path) -> None:
    write(repo / ".git" / "worktrees" / name / "gitdir", f"{where}/.git\n")
    where.mkdir(parents=True, exist_ok=True)


def test_5_worktree_off_convention(run, home):
    active = home / "workspace" / "projects" / "active"
    repo = git_repo(active / "hub")
    add_worktree(repo, "ok", active / "hub.worktrees" / "todo-7")
    add_worktree(repo, "bad", home / "workspace" / "worktrees" / "feature")
    add_worktree(repo, "wrongname", active / "other.worktrees" / "todo-8")
    add_worktree(repo, "trial", active / "hub" / "var" / "merge-check" / "wt-1")  # check 7's business
    data = run()
    assert sorted(a["path"] for a in of(data, 5)) == sorted([str(home / "workspace/worktrees/feature"),
                                                            str(active / "other.worktrees/todo-8")])


def make_db(path: Path, rows) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, status TEXT)")
    conn.executemany("INSERT INTO tasks (id, title, status) VALUES (?, 't', ?)", rows)
    conn.commit()
    conn.close()
    return path


def test_6_closed_todo_branch_and_worktree(run, home, tmp_path):
    active = home / "workspace" / "projects" / "active"
    repo = git_repo(active / "hub")
    write(repo / ".git" / "refs" / "heads" / "todo-5" / "done-thing", "sha")
    write(repo / ".git" / "refs" / "heads" / "todo-6" / "open-thing", "sha")
    write(repo / ".git" / "packed-refs", "# pack\nabc refs/heads/todo-9/packed\nabc refs/heads/main\n")
    add_worktree(repo, "w5", active / "hub.worktrees" / "todo-5")
    add_worktree(repo, "w6", active / "hub.worktrees" / "todo-6")
    db = make_db(tmp_path / "t.db", [(5, "done"), (6, "todo"), (9, "cancelled")])
    data = run(db=db)
    paths = sorted(a["path"] for a in of(data, 6))
    assert paths == sorted([f"{repo}#todo-5/done-thing", f"{repo}#todo-9/packed",
                            str(active / "hub.worktrees" / "todo-5")])


def test_6_without_database_is_a_warning(run, home):
    git_repo(home / "workspace" / "projects" / "active" / "hub")
    data = run(db=None)
    assert of(data, 6) == [] and any("base des todos" in w for w in data["warnings"])


def test_7_stale_merge_check(run, home):
    base = home / "workspace/projects/active/hub/var/merge-check"
    write(base / "old" / "f", age_days=3)
    age(base / "old", 3)
    write(base / "new" / "f")
    data = run()
    (found,) = of(data, 7)
    assert found["path"] == str(base / "old")


def test_7_uses_the_oldest_of_folder_and_git_file(run, home):
    base = home / "workspace/projects/active/hub/var/merge-check"
    write(base / "wt-52_pipul" / ".git", "gitdir: x", age_days=5)  # folder itself looks recent
    write(base / "wt-fresh" / ".git", "gitdir: x")
    data = run()
    assert [a["path"] for a in of(data, 7)] == [str(base / "wt-52_pipul")]


def test_7_detached_worktree_with_git_file_and_old_mtimes(run, home):
    active = home / "workspace/projects/active"
    repo = git_repo(active / "hub")
    wt = active / "hub/var/merge-check/wt-52_pipul"
    write(wt / "src.py", "x")
    write(wt / ".git", f"gitdir: {repo}/.git/worktrees/wt-52_pipul\n", age_days=4)
    add_worktree(repo, "wt-52_pipul", wt)
    age(wt, 4)
    data = run()
    assert [a["path"] for a in of(data, 7)] == [str(wt)]
    assert of(data, 5) == []


def test_7_old_git_record_alone_is_enough(run, home):
    active = home / "workspace/projects/active"
    repo = git_repo(active / "hub")
    wt = active / "hub/var/merge-check/wt-1"
    write(wt / ".git", "gitdir: x")  # recent folder and .git file
    add_worktree(repo, "wt-1", wt)
    age(repo / ".git/worktrees/wt-1/gitdir", 3)
    assert [a["path"] for a in of(run(), 7)] == [str(wt)]


# --- C -----------------------------------------------------------------------------------------------------------

def test_8_disposable_patterns_and_age(run, home):
    write(home / "notes" / "a.bak.1", age_days=20)
    write(home / "notes" / "b.bak", age_days=2)
    write(home / "notes" / "c.tmp", age_days=2)
    write(home / "notes" / "keep.md", age_days=200)
    write(home / "forum" / "x.tmp", age_days=50)
    data = run(disposable={"patterns": [{"glob": "*.bak*", "min_age_days": 14},
                                        {"glob": "*.tmp", "min_age_days": 1}]})
    assert sorted(Path(a["path"]).name for a in of(data, 8)) == ["a.bak.1", "c.tmp"]


def test_9_only_orphan_pycache(run, home):
    write(home / "workspace" / "scripts" / "__pycache__" / "gone.cpython-312.pyc")  # no gone.py: orphan
    write(home / "workspace" / "lib" / "__pycache__" / "mod.cpython-312.pyc")
    write(home / "workspace" / "lib" / "mod.py")  # has its source: normal
    write(home / "workspace" / "p" / ".venv" / "lib" / "__pycache__" / "y.cpython-312.pyc")
    write(home / "workspace" / "deploy" / "__pycache__" / "ultron-priv.cpython-312.pyc")
    write(home / "workspace" / "deploy" / "ultron-priv")  # extensionless script, normal
    write(home / "workspace" / "user" / "__pycache__" / "ultron-usercpython-312.pyc")  # real name, no dot
    write(home / "workspace" / "user" / "ultron-user")
    write(home / "workspace" / "tests" / "__pycache__" / "conftest.cpython-312-pytest-9.1.1.pyc")  # pytest's name
    write(home / "workspace" / "tests" / "conftest.py")
    data = run()
    assert [a["path"] for a in of(data, 9)] == [str(home / "workspace/scripts/__pycache__")]


def test_10_aged_dirs(run, home):
    base = home / "workspace" / "runs"
    write(base / "old1" / "f", age_days=40)
    age(base / "old1", 40)
    write(base / "new" / "f")
    data = run(disposable={"aged_dirs": [{"path": "~/workspace/runs", "max_age_days": 30}]})
    (found,) = of(data, 10)
    assert found["path"] == str(base) and found["count"] == 1 and "old1" in found["detail"]


def test_10_aged_dirs_traite_si_only_marked(run, home):
    # todo #381 : seuls les lots portant un marqueur de traitement sont proposés ; rien n'est supprimé.
    base = home / "workspace" / "docs"
    for name, mark in (("done", "presentation.md"), ("pending", "collected.json"), ("young", "presentation.md")):
        write(base / name / mark)
    for name in ("done", "pending"):
        age(base / name, 40)
    cfg = {"path": "~/workspace/docs", "max_age_days": 30, "traite_si": ["presentation.md"]}
    (found,) = of(run(disposable={"aged_dirs": [cfg]}), 10)
    assert found["count"] == 1 and "done" in found["detail"] and "pending" not in found["detail"]
    assert "traitée" in found["detail"]
    assert (base / "pending" / "collected.json").exists() and (base / "done" / "presentation.md").exists()


def test_10_aged_dirs_sous_dossier_work_only(run, home):
    # todo #464 : seul <mission>/work est proposé (avec la taille), result.json et steps/ restent ; rien n'est supprimé.
    base = home / "workspace" / "missions"
    write(base / "m1" / "work" / "big.bin", "x" * 2_000_000)
    write(base / "m1" / "result.json")
    write(base / "m2" / "result.json")  # sans work/ : ignorée
    age(base / "m1" / "work", 1)
    cfg = {"path": "~/workspace/missions", "max_age_days": 0, "sous_dossier": "work"}
    found = [a for a in of(run(disposable={"aged_dirs": [cfg]}), 10)]
    assert len(found) == 1 and found[0]["count"] == 1
    assert "m1/work" in found[0]["detail"] and "m2" not in found[0]["detail"] and "2 Mo" in found[0]["detail"]
    assert (base / "m1" / "work" / "big.bin").exists() and (base / "m1" / "result.json").exists()


def test_11_logs_old_and_big(run, home):
    logs = home / "logs"
    write(logs / "old.log", age_days=40)
    write(logs / "big.log", b"0" * (2 * MB))
    write(logs / "fine.log")
    write(home / "single.log", age_days=40)
    data = run(disposable={"logs": {"dirs": ["~/logs"], "files": ["~/single.log"], "max_age_days": 30,
                                    "max_size_mb": 1}})
    kinds = {Path(a["path"]).name: a["kind"] for a in of(data, 11)}
    assert kinds == {"old.log": "old_log", "big.log": "big_log", "single.log": "old_log"}


def test_12_duplicates_skip_secrets_and_small(run, home, monkeypatch):
    blob = b"d" * (2 * MB)
    write(home / "notes" / "a.bin", blob)
    write(home / "notes" / "b.bin", blob)
    write(home / "notes" / "other.bin", b"e" * (2 * MB))
    write(home / "notes" / "small1", b"s")
    write(home / "notes" / "small2", b"s")
    write(home / ".hermes" / "auth.json", blob)
    write(home / "notes" / "auth.json", blob)
    hashed = []
    real = checks._sha256
    monkeypatch.setattr(checks, "_sha256", lambda p: hashed.append(p) or real(p))
    data = run()
    (found,) = of(data, 12)
    assert found["path"].endswith("a.bin") and "b.bin" in found["detail"] and "auth.json" not in found["detail"]
    assert not any(p.endswith("auth.json") for p in hashed)


# --- D -----------------------------------------------------------------------------------------------------------

def test_13_disk_thresholds(run):
    disks = [{"mount": "/", "total_gib": 100, "free_gib": 5, "used_pct": 95.0},
             {"mount": "/home", "total_gib": 100, "free_gib": 15, "used_pct": 85.0},
             {"mount": "/data", "total_gib": 100, "free_gib": 60, "used_pct": 40.0}]
    data = run(probe=lambda: disks)
    assert {a["path"]: a["kind"] for a in of(data, 13)} == {"/": "disk_crit", "/home": "disk_warn"}
    assert data["disk"] == disks


def test_14_growth_against_history(run, home):
    write(home / "grow" / "f", b"0" * (30 * MB))
    write(home / "same" / "f", b"0" * (30 * MB))
    write(home / "tiny" / "f", b"0" * 100)
    history = [{"ts": 1, "sizes": {str(home / "grow"): 1 * MB, str(home / "same"): 30 * MB,
                                    str(home / "tiny"): 1}}]
    data = run(space={"watched": ["~/grow", "~/same", "~/tiny", "~/forum"]}, history=history)
    assert [a["path"] for a in of(data, 14)] == [str(home / "grow")]
    assert data["sizes"][str(home / "grow")] == 30 * MB
    assert str(home / "forum") not in data["sizes"]


def test_14_no_history_no_anomaly_but_sizes(run, home):
    write(home / "grow" / "f", b"0" * 1000)
    data = run(space={"watched": ["~/grow"]})
    assert of(data, 14) == [] and data["sizes"] == {str(home / "grow"): 1000}


def test_15_cache_over_limit(run, home):
    write(home / ".cache" / "pip" / "f", b"0" * (3 * MB))
    write(home / ".cache" / "uv" / "f", b"0" * 10)
    data = run(space={"caches": [{"name": "pip", "path": "~/.cache/pip", "max_mb": 2, "purge": "pip cache purge"},
                                 {"name": "uv", "path": "~/.cache/uv", "max_mb": 2, "purge": "uv cache clean"}]})
    (found,) = of(data, 15)
    assert found["action"] == "pip cache purge" and found["path"] == str(home / ".cache/pip")


# --- E -----------------------------------------------------------------------------------------------------------

def test_16_tests_outside_repo(run, home):
    write(home / "workspace" / "loose" / "tests" / "test_a.py")
    write(home / "workspace" / "single" / "test_b.py")
    repo = git_repo(home / "workspace" / "projects" / "active" / "hub")
    write(repo / "tests" / "test_ok.py")
    write(home / "workspace" / "scripts" / "tool.py")
    write(home / "deep" / "inner" / "tests" / "test_c.py")  # under ~: only the first level is looked at
    write(home / "shallow" / "tests" / "test_d.py")
    data = run()
    assert sorted(a["path"] for a in of(data, 16)) == sorted([
        str(home / "workspace/loose"), str(home / "workspace/single"), str(home / "shallow")])


def test_16_only_the_topmost_folder(run, home):
    write(home / "workspace" / "enc" / "tests" / "test_a.py")
    write(home / "workspace" / "enc" / "archive-todo36" / "test_old.py")
    data = run()
    assert [a["path"] for a in of(data, 16)] == [str(home / "workspace/enc")]


def test_17_exec_outside_allowed_prefixes(run, home):
    units = home / ".config" / "systemd" / "user"
    script = write(home / "workspace/projects/active/hub/run.sh")
    write(units / "good.service", f"[Service]\nExecStart=/usr/bin/python3 {script}\n")
    write(units / "bad.service", "[Service]\nExecStart=-/usr/bin/env python3 %h/scripts/x.py\n")
    write(units / "cont.service", "[Service]\nExecStart=/usr/bin/sh \\\n  /opt/evil.sh\n")
    write(script.parent / "linked-target.service", "[Service]\nExecStart=/srv/other\n")
    (units / "linked.service").symlink_to(script.parent / "linked-target.service")
    write(units / "ignored.timer", "ExecStart=/srv/other\n")
    data = run()
    found = {(Path(a["path"]).name, a["target"]) for a in of(data, 17)}
    assert found == {("bad.service", "%h/scripts/x.py"), ("cont.service", "/opt/evil.sh"),
                     ("linked.service", "/srv/other")}


def test_18_plugin_copy_differs(run, home):
    source = home / "src" / "hub_plugin"
    write(source / "a.py", "one")
    write(source / "b.py", "two")
    plugins = home / ".hermes" / "plugins"
    write(plugins / "hub" / "a.py", "one")
    write(plugins / "hub" / "b.py", "TWO")
    write(plugins / "hub" / "__pycache__" / "a.pyc", "zzz")
    write(plugins / "same" / "a.py", "one")
    write(home / "src" / "same_src" / "a.py", "one")
    plugins.joinpath("linked").symlink_to(source)
    data = run(code={"plugin_sources": {"hub": "~/src/hub_plugin", "same": "~/src/same_src",
                                        "linked": "~/src/hub_plugin", "missing": "~/src/nope"}})
    (found,) = of(data, 18)
    assert found["path"] == str(plugins / "hub") and "b.py" in found["detail"] and "a.py" not in found["detail"]


# --- guarantees --------------------------------------------------------------------------------------------------

def test_secrets_are_never_opened(run, home, monkeypatch):
    secret = write(home / ".hermes" / "auth.json", b"top" * (2 * MB))
    write(home / ".sudo_pass", "pw")
    write(home / ".hermes" / ".env", "KEY=1")
    write(home / ".hermes" / "other.bak", "x", age_days=50)
    opened = []
    real_open = builtins.open
    real_read = Path.read_text

    def spy_open(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    def spy_read(self, *a, **k):
        opened.append(str(self))
        return real_read(self, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read)
    data = run(disposable={"patterns": [{"glob": "*.bak", "min_age_days": 1}]},
               structure={"allowed": {"~": {"dirs": [".hermes"], "files": []}}},
               space={"watched": ["~/.hermes"]})
    assert not [p for p in opened if p.endswith(("auth.json", ".sudo_pass", ".env"))]
    assert data["sizes"][str(home / ".hermes")] >= secret.stat().st_size  # size counted, content not read
    assert any(a["path"].endswith(".sudo_pass") for a in of(data, 3))  # name reported


def test_forum_and_ssh_are_never_walked(run, home, monkeypatch):
    write(home / "forum" / "x.bak", age_days=90)
    write(home / "forum" / "tests" / "test_x.py")
    write(home / ".ssh" / "id_rsa", "k")
    write(home / "notes" / "n.md")
    seen = []
    real = os.scandir

    def spy(path="."):
        seen.append(str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    data = run(disposable={"roots": ["~", "~/notes"], "patterns": [{"glob": "*.bak", "min_age_days": 1}]},
               space={"watched": ["~/forum", "~/.ssh", "~/notes"]},
               structure={"allowed": {"~": {"dirs": ["notes"], "files": []}}})
    forbidden = (str(home / "forum"), str(home / ".ssh"))
    assert not [p for p in seen if p.startswith(forbidden)]
    assert not [a for a in data["anomalies"] if a["path"].startswith(forbidden)]
    assert set(data["sizes"]) == {str(home / "notes")}


def test_symlinks_are_not_followed(run, home, tmp_path):
    outside = tmp_path / "outside"
    write(outside / "x.tmp", age_days=10)
    (home / "notes").mkdir()
    (home / "notes" / "link").symlink_to(outside)
    data = run(disposable={"patterns": [{"glob": "*.tmp", "min_age_days": 1}]},
               space={"watched": ["~/notes"]})
    assert of(data, 8) == [] and data["sizes"][str(home / "notes")] == 0


def test_a_failing_check_becomes_a_warning(run, monkeypatch):
    def boom(scan):
        raise RuntimeError("no")

    monkeypatch.setattr(checks, "CHECKS", [boom, checks.check_1_expected])
    data = run(structure={"expected": ["~/missing"]})
    assert len(of(data, 1)) == 1 and any("boom" in w for w in data["warnings"])


def test_output_is_capped_per_check_but_counts_stay_true(run, home):
    write(home / "notes" / "keep.md")
    for i in range(55):
        write(home / "notes" / f"f{i}.tmp", age_days=3)
    data = run(disposable={"patterns": [{"glob": "*.tmp", "min_age_days": 1}]})
    assert len(of(data, 8)) == 50 and data["counts"]["8"] == 55 and data["truncated"] == {"8": 5}


def test_home_comes_from_pwd_not_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/not-me")
    assert rules.user_home() != "/tmp/not-me"
    assert rules.expand("~/x", "/h") == "/h/x"


def test_bad_config_is_refused(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("version: 2\n")
    with pytest.raises(Exception, match="version 1"):
        rules.load_config(path)


def test_real_config_loads():
    cfg = rules.load_config(Path(__file__).resolve().parent.parent / "config" / "menage.yaml")
    assert cfg["worktrees"]["pattern"] and len(cfg["space"]["caches"]) >= 7
    assert "~/.hermes/tools/" in cfg["code"]["exec_allowed_prefixes"]


def test_handle_envelope_shape(tmp_path, home, monkeypatch):
    (tmp_path / "config").mkdir()
    cfg = base_config()
    cfg["structure"]["expected"] = ["~/missing"]
    (tmp_path / "config" / "menage.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(rules, "user_home", lambda: str(home))
    monkeypatch.setattr("ultron_hub.health.collect.probe_disks", lambda: [])
    settings = SimpleNamespace(root=tmp_path, var_dir=tmp_path / "var", todo_db=None)
    out = commands.handle(argparse.Namespace(menage_command="scan"), settings)
    json.dumps(out)
    assert out["status"] == "ok" and out["command"] == "menage.scan" and out["error"] is None
    assert out["summary"] == "1 anomalie(s)"
    assert set(out["data"]) == {"anomalies", "counts", "truncated", "sizes", "disk", "warnings"}
    assert out["data"]["counts"] == {"1": 1}


# --- read only by construction -----------------------------------------------------------------------------------

def test_package_source_has_no_write_primitive():
    forbidden = re.compile(r"unlink|rename|rmtree|os\.remove|\.remove\(|write_text|write_bytes|\.mkdir|makedirs|"
                           r"os\.replace|shutil\.(move|copy)|\.touch\(|chmod|\.write\(")
    open_write = re.compile(r"open\([^)]*[\"'][rwxab+]*[wxa+][rwxab+]*[\"']")
    # The state writers only write under var/menage/ (report, audit date); nothing in the package deletes or moves.
    destructive = re.compile(r"unlink|rmtree|os\.remove|\.remove\(|shutil\.(move|copy)|\.rename\(|os\.rename")
    writers = {"report.py", "audit.py", "tests_audit.py"}  # tests_audit: var/menage history + throwaway dir
    package = Path(__file__).resolve().parent.parent / "src" / "ultron_hub" / "menage"
    files = sorted(package.glob("*.py"))
    assert len(files) >= 4
    for path in files:
        code = "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines()
                         if not line.lstrip().startswith(('"""', "'''")))
        assert not destructive.search(code), f"{path.name}: delete or move primitive"
        if path.name in writers:
            continue
        assert not forbidden.search(code), f"{path.name}: write primitive"
        assert not open_write.search(code), f"{path.name}: open() in write mode"


def test_19_living_file_over_budget(run, home):
    write(home / "hub" / "CLAUDE.md", b"x" * 31 * 1024)
    write(home / "notes" / "chantier-a.md", b"x" * 10 * 1024)
    write(home / "notes" / "chantier-b.md", b"x" * 16 * 1024)
    (home / "notes" / "chantier-lien.md").symlink_to(home / "notes" / "chantier-b.md")
    data = run(budgets={"files": {"~/hub/CLAUDE.md": 30, "~/notes/chantier-*.md": 15, "~/absent.md": 1}})
    found = {(a["path"], a["detail"]) for a in of(data, 19)}
    assert found == {(str(home / "hub" / "CLAUDE.md"), "31 Ko pour un budget de 30 Ko"),
                     (str(home / "notes" / "chantier-b.md"), "16 Ko pour un budget de 15 Ko")}
    assert all("archives/ ou docs/" in a["action"] for a in of(data, 19))


def test_19_without_budgets_is_silent(run, home):
    write(home / "hub" / "CLAUDE.md", b"x" * 99 * 1024)
    assert of(run(), 19) == []


def test_19_proposes_the_file_own_gesture(run, home):
    write(home / "notes" / "orchestrateur.md", b"x" * 13 * 1024)
    data = run(budgets={"files": {"~/notes/orchestrateur.md": 12},
                        "actions": {"~/notes/orchestrateur.md": "ultron-hub esprit reprise-archiver"}})
    (found,) = of(data, 19)
    assert found["action"] == "ultron-hub esprit reprise-archiver"
