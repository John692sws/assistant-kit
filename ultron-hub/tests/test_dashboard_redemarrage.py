"""Todo #294: a merge touching src/ultron_hub/dashboard/ restarts the dashboard service by itself, and the watcher
checks the service is not older than the code. Nothing real: the restarters are fakes, systemctl/git answers are faked."""

import json
import subprocess
import time
from types import SimpleNamespace

from test_branches import branch, no_bwrap, repo, sh, status  # noqa: F401  (fixtures)
from ultron_hub import branches
from ultron_hub.health import reflexes


def test_merge_touching_dashboard_plans_restart(repo):
    branch(repo, "todo-5/feat", "src/ultron_hub/dashboard/page.js", "x\n")
    gateway, dash = [], []
    r = branches.merge_and_close(repo, 5, restart=lambda: gateway.append(1), restart_dash=lambda: dash.append(1))
    assert r["closed"] and r["dashboard"] == "planned" and dash == [1] and gateway == [1]


def test_merge_elsewhere_does_not_restart_dashboard(repo):
    branch(repo, "todo-5/feat", "src/ultron_hub/other.py", "x\n")
    dash = []
    r = branches.merge_and_close(repo, 5, restart=lambda: None, restart_dash=lambda: dash.append(1))
    assert r["closed"] and r["dashboard"] is None and dash == []


def test_dashboard_restart_failure_is_reported_not_fatal(repo):
    branch(repo, "todo-5/feat", "src/ultron_hub/dashboard/page.js", "x\n")

    def boom():
        raise RuntimeError("systemd absent")
    r = branches.merge_and_close(repo, 5, restart=lambda: None, restart_dash=boom)
    assert r["closed"] and "systemd absent" in r["dashboard"] and status(repo) == "done"


def test_restart_dashboard_is_a_delayed_transient_unit(monkeypatch):
    seen = []
    monkeypatch.setattr(branches.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or SimpleNamespace(returncode=0, stderr=""))
    branches.restart_dashboard()
    cmd = seen[0]
    assert cmd[0] == "systemd-run" and "--user" in cmd and any(a.startswith("--on-active=") for a in cmd)
    assert cmd[-4:] == [branches.SYSTEMCTL, "--user", "restart", "ultron-hub-dashboard.service"]


# --- the control ---

def fake_run(started_epoch, last_commit, state="active"):
    stamp = time.strftime("%a %Y-%m-%d %H:%M:%S %Z", time.localtime(started_epoch))

    def run(cmd):
        if cmd[0] == "systemctl":
            return SimpleNamespace(returncode=0, stdout=f"ActiveState={state}\nActiveEnterTimestamp={stamp}\n")
        out = "" if last_commit is None else f"{last_commit}\n"
        return SimpleNamespace(returncode=0, stdout=out)
    return run


def test_control_stale_when_service_older_than_code(tmp_path):
    now = int(time.time())
    res = reflexes.inspecter_dashboard(tmp_path, fake_run(now - 3600, now - 60))
    assert res["ok"] and res["stale"] and "code modifié" in res["reason"]


def test_control_fresh_when_service_newer_or_no_commit(tmp_path):
    now = int(time.time())
    assert not reflexes.inspecter_dashboard(tmp_path, fake_run(now - 60, now - 3600))["stale"]
    assert not reflexes.inspecter_dashboard(tmp_path, fake_run(now - 60, None))["stale"]


def test_control_silent_when_service_inactive_or_unreadable(tmp_path):
    now = int(time.time())
    assert not reflexes.inspecter_dashboard(tmp_path, fake_run(now, now, state="failed"))["ok"]

    def boom(cmd):
        raise OSError("pas de systemctl")
    assert not reflexes.inspecter_dashboard(tmp_path, boom)["ok"]


def test_control_with_real_git_uses_the_merge_commit(tmp_path):
    sh(tmp_path, "init", "-q", "-b", "main")
    sh(tmp_path, "config", "user.name", "t"); sh(tmp_path, "config", "user.email", "t@t")
    (tmp_path / "a").write_text("a"); sh(tmp_path, "add", "a"); sh(tmp_path, "commit", "-qm", "a")
    sh(tmp_path, "switch", "-qc", "b")
    p = tmp_path / "src/ultron_hub/dashboard/x.js"
    p.parent.mkdir(parents=True); p.write_text("x"); sh(tmp_path, "add", "."); sh(tmp_path, "commit", "-qm", "b")
    sh(tmp_path, "switch", "-q", "main"); sh(tmp_path, "merge", "-q", "--no-ff", "b", "-m", "merge")
    real = lambda cmd: subprocess.run(cmd, capture_output=True, text=True)  # noqa: E731
    last = int(sh(tmp_path, "log", "-1", "--format=%ct"))
    res = reflexes.inspecter_dashboard(tmp_path, lambda c: fake_run(last - 10, 0)(c) if c[0] == "systemctl" else real(c))
    assert res["stale"]
    res = reflexes.inspecter_dashboard(tmp_path, lambda c: fake_run(last + 10, 0)(c) if c[0] == "systemctl" else real(c))
    assert not res["stale"]


def test_reflex_listed_in_config_and_diagnosed():
    assert any(r["nom"] == "dashboard-a-jour" for r in reflexes.charger())
    stale = lambda: {"ok": True, "stale": True, "reason": "vieux"}  # noqa: E731
    diags = reflexes.diagnostiquer({}, inspect_dashboard=stale)
    assert [d["nom"] for d in diags] == ["dashboard-a-jour"] and diags[0]["bouton"] is None
    assert reflexes.diagnostiquer({}, inspect_dashboard=lambda: {"ok": True, "stale": False, "reason": "à jour"}) == []



def test_push_subscription_age_reflex(tmp_path):
    store, now = tmp_path / "abonnements.json", 1_000_000_000
    assert reflexes.inspecter_push(now, store)["stale"] is False  # no file: nothing to say
    store.write_text(json.dumps({"iphone": {"endpoint": "https://secret", "depuis": now - 15 * 86400},
                                 "ipad": {"depuis": now - 2 * 86400}, "vieux": {}}))
    res = reflexes.inspecter_push(now, store)
    assert res["ok"] and res["stale"] and "iphone : 15 j" in res["reason"] and "ipad" not in res["reason"]
    assert "secret" not in res["reason"]
    store.write_text("pas du json")
    assert not reflexes.inspecter_push(now, store)["ok"]
    assert any(r["nom"] == "push-abonnement" for r in reflexes.charger())
    diags = reflexes.diagnostiquer({}, inspect_push=lambda: {"ok": True, "stale": True, "reason": "iphone : 15 j"})
    assert [d["nom"] for d in diags] == ["push-abonnement"] and diags[0]["bouton"] is None
    assert "Moi > Notifications" in diags[0]["action"]
    assert reflexes.diagnostiquer({}, inspect_push=lambda: {"ok": True, "stale": False, "reason": ""}) == []
