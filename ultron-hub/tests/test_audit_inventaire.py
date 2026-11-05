"""Todo #452: read only inventory (temporary home, fake systemctl, secret never opened)."""

import builtins
import dataclasses
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

from ultron_hub.audit import commands, inventory
from ultron_hub.common.settings import load_settings

NOW = 1_793_000_000.0  # 2026-10-26
TIMERS = [{"unit": "backup.timer", "next": 0, "last": 1_800_000_000_000_000, "activates": "backup.service"}]
UNITS = [{"unit": "hub.service", "load": "loaded", "active": "active", "sub": "running", "description": "x"}]


def fake_run(cmd, **kw):
    assert isinstance(cmd, list) and "--user" in cmd
    data = TIMERS if "list-timers" in cmd else UNITS
    return types.SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "config" / "menage.yaml").write_text("version: 1\nexclusions: [~/forum]\nsecrets: [~/.hermes/auth.json]\n")
    (root / "tasks" / "demo").mkdir(parents=True)
    (root / "tasks" / "demo" / "task.yaml").write_text("name: demo\nmodel: haiku\n")
    run = root / "var" / "runs" / "20261001T101010Z-aaaa"
    run.mkdir(parents=True)
    (run / "request.json").write_text(json.dumps({"action": "demo"}))
    (root / "var" / "logs").mkdir()
    (root / "var" / "logs" / "ultron-hub.log").write_text(
        "2026-10-02 10:00:00,000 WARNING ultron_hub.cli: refused command=menage scan: bad\n")
    (root / "var" / "cache").mkdir()
    (root / "var" / "cache" / "f").write_text("12345")
    for d in ("workspace/projects/active/p1", "notes/n1", "ultron/esprit", ".claude/skills/s1", ".claude/skills/synced",
              ".claude/skills/.trash", "forum/secret", ".ssh", ".hermes", ".config/gh"):
        (home / d).mkdir(parents=True)
    (home / "forum" / "secret" / "x").write_text("nope")
    (home / ".hermes" / "auth.json").write_text("TOKEN")
    (home / ".sudo_pass").write_text("pw")
    (home / "notes" / "n1" / "auth.json").write_text("TOKEN")
    (home / "notes" / "n1" / "ok.md").write_text("abc")
    tr = home / ".claude" / "projects" / "-x"
    tr.mkdir(parents=True)
    line = {"timestamp": "2026-10-20T10:00:00Z", "message": {"content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": "s1"}}]}}
    (tr / "a.jsonl").write_text(json.dumps(line) + "\nnot json \"Skill\"\n")
    esprit = home / "ultron" / "esprit"
    (esprit / "journal.jsonl").write_text(json.dumps(
        {"date": "2026-10-05T10:00:00+01:00", "source": "dashboard", "type": "action", "todo": None,
         "texte": "✍️ 5 caractères envoyés à la session abc depuis le dashboard (by=x)"}) + "\n")
    settings = dataclasses.replace(load_settings(root), esprit_dir=esprit)
    return home, settings, esprit


def build(world):
    home, settings, esprit = world
    return inventory.build(settings, str(home), NOW, fake_run)


def by(data, kind):
    return {e["nom"]: e for e in data["elements"] if e["type"] == kind}


def test_inventory_content(world):
    data = build(world)
    el = data["elements"]
    assert {"p1"} == set(by(data, "projet"))
    assert "n1" in by(data, "notes-dossier") and by(data, "notes-dossier")["n1"]["taille"] == 3  # auth.json skipped
    skills = by(data, "skill")
    assert set(skills) == {"s1"} and skills["s1"]["appels_30j"] == 1
    assert skills["s1"]["dernier_usage"] == "2026-10-20T10:00:00Z"
    assert by(data, "var")["cache"]["taille"] == 5
    assert by(data, "tache")["demo"]["dernier_usage"] == "2026-10-01T10:10:10Z"
    assert by(data, "timer")["backup.timer"]["dernier_usage"]
    assert by(data, "service")["hub.service"]["etat"] == "active/running"
    assert by(data, "commande")["menage scan"]["dernier_usage"] == "2026-10-02T10:00:00"
    assert by(data, "commande")["menage"]["dernier_usage"] == "2026-10-02T10:00:00"
    assert by(data, "action-dashboard")
    assert data["totaux"]["elements"] == len(el)
    for e in el:
        assert {"nom", "type", "chemin", "taille", "modifie", "dernier_usage", "source_usage"} <= set(e)
    assert not any("forum" in e["chemin"] or ".ssh" in e["chemin"] or "auth.json" in e["chemin"] for e in el)


def test_dashboard_action_linked_to_journal(world):
    data = build(world)
    linked = [e for e in by(data, "action-dashboard").values() if e["dernier_usage"] == "2026-10-05T10:00:00+01:00"]
    assert linked


def test_secret_paths_never_opened(world, monkeypatch):
    home, _, _ = world
    touched: list[str] = []
    secret = ("/forum", "/.ssh", "/.hermes", "/.config/gh", ".sudo_pass", "auth.json")

    def spy(fn):
        def wrapper(path, *a, **k):
            touched.append(str(path))
            return fn(path, *a, **k)
        return wrapper

    monkeypatch.setattr(builtins, "open", spy(builtins.open))
    monkeypatch.setattr(os, "open", spy(os.open))
    monkeypatch.setattr(os, "lstat", spy(os.lstat))
    monkeypatch.setattr(os, "stat", spy(os.stat))
    monkeypatch.setattr(os, "scandir", spy(os.scandir))
    build(world)
    bad = [p for p in touched if str(home) in p and any(s in p for s in secret)]
    assert bad == []


def test_unreadable_sources_are_warnings(world):
    home, settings, esprit = world

    def boom(cmd, **kw):
        raise FileNotFoundError("systemctl")

    data = inventory.build(settings, str(home) + "/absent", 1.0, boom)
    assert data["warnings"] and isinstance(data["elements"], list)


def test_read_only_and_out_file(world, tmp_path):
    home, settings, esprit = world

    def snapshot():
        return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in
                      [*home.rglob("*"), *settings.root.rglob("*")])
    before = snapshot()
    out = tmp_path / "out.json"
    args = types.SimpleNamespace(audit_command="inventaire", out=str(out))
    orig = subprocess.run
    subprocess.run = fake_run
    try:
        res = commands.handle(args, settings, home=str(home))
    finally:
        subprocess.run = orig
    assert snapshot() == before
    assert res["status"] == "ok" and res["command"] == "audit.inventaire"
    assert json.loads(out.read_text())["command"] == "audit.inventaire"
    assert (out.stat().st_mode & 0o777) == 0o600
