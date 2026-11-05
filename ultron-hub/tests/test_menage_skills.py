"""Todo #451: skill usage count (menage/skills.py) and its CLI; throwaway HOME, never the real ~/.claude."""

import json
import os
import time
from pathlib import Path

import pytest

from ultron_hub import cli
from ultron_hub.menage import skills

NOW = time.time()
DAY = 86400


def make_skill(root: Path, name: str, age_days: float) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    t = NOW - age_days * DAY
    os.utime(d / "SKILL.md", (t, t))
    os.utime(d, (t, t))


def use(tool: str, params: dict, days_ago: float, ident: str) -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW - days_ago * DAY))
    block = {"type": "tool_use", "id": ident, "name": tool, "input": params}
    return json.dumps({"timestamp": stamp, "message": {"role": "assistant", "content": [block]}})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "home" / ".claude"
    (root / "projects" / "p" / "sub").mkdir(parents=True)
    for name, age in (("neuve", 3), ("jamais", 20), ("vieille", 60), ("active", 60), ("recente", 60), ("lue", 60)):
        make_skill(root / "skills", name, age)
    make_skill(root / "skills" / ".trash", "morte", 90)
    make_skill(root / "skills" / "synced", "autre", 90)
    lines = [
        use("Skill", {"skill": "active"}, 40, "a"), "not json {", "",
        use("Skill", {"skill": "plugin:active"}, 2, "b"),
        use("Skill", {"skill": "recente"}, 10, "c"),
        use("Read", {"file_path": "/x/.claude/skills/lue/SKILL.md"}, 5, "d"),
        use("Bash", {"command": "cat ~/.claude/skills/active/SKILL.md"}, 1, "e"),
        use("Bash", {"command": "cat ~/.claude/skills/active/SKILL.md"}, 1, "e"),  # same id: counted once
        use("Read", {"file_path": "/x/other/SKILL.md"}, 1, "f"),
    ]
    (root / "projects" / "p" / "s.jsonl").write_text("\n".join(lines) + "\n")
    (root / "projects" / "p" / "sub" / "agent.jsonl").write_text(use("Skill", {"skill": "neuve"}, 1, "g") + "\n")
    return root


def test_usage_counts_and_statuses(home):
    data = skills.usage(home / "skills", home / "projects", now=NOW)
    rows = {r["nom"]: r for r in data["skills"]}
    assert set(rows) == {"neuve", "jamais", "vieille", "active", "recente", "lue"}  # .trash and synced skipped
    assert rows["active"]["n"] == 3 and rows["active"]["statut"] == "ok" and rows["active"]["dernier"].startswith(
        time.strftime("%Y-%m-%d", time.gmtime(NOW - DAY)))
    assert rows["neuve"]["n"] == 1 and rows["neuve"]["statut"] == "ok"
    assert rows["jamais"]["n"] == 0 and rows["jamais"]["dernier"] is None and rows["jamais"]["statut"] == "a_revoir"
    assert rows["vieille"]["statut"] == "corbeille" and rows["vieille"]["age_jours"] == 60
    assert rows["recente"]["statut"] == "ok" and rows["lue"]["n"] == 1 and rows["lue"]["statut"] == "ok"
    assert data["a_revoir"] == ["jamais"] and data["corbeille"] == ["vieille"]
    assert "1 à revoir, 1 à la corbeille" in skills.line(data)


def test_old_use_only_goes_to_corbeille(home):
    (home / "projects" / "p" / "old.jsonl").write_text(use("Skill", {"skill": "vieille"}, 45, "z") + "\n")
    rows = {r["nom"]: r for r in skills.usage(home / "skills", home / "projects", now=NOW)["skills"]}
    assert rows["vieille"]["n"] == 1 and rows["vieille"]["statut"] == "corbeille"


def test_missing_folders_are_empty(tmp_path):
    assert skills.usage(tmp_path / "no", tmp_path / "none", now=NOW)["skills"] == []


def test_cli_prints_json_only(home, project, capsys):
    assert cli.main(["menage", "skills"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "menage.skills" and out["data"]["corbeille"] == ["vieille"]


def test_weekly_report_line_and_proposal_never_overwrites(home, project, capsys):
    from ultron_hub.common.settings import load_settings
    from ultron_hub.menage import commands
    from ultron_hub import menage_lot
    settings = load_settings()
    scan = {"anomalies": [], "counts": {}, "truncated": {}, "sizes": {}, "disk": [], "warnings": []}
    out = commands.do_report(settings, False, scan=scan)
    assert "🧩 Skills : 1 à revoir, 1 à la corbeille (jamais, vieille)" in out["text"]
    view = menage_lot.voir(settings)
    assert view["par"] == "menage-hebdo" and [c["nom"] for c in view["corbeille"]] == ["vieille"]
    assert view["corbeille"][0]["raison"] == "aucun usage depuis 30 j"
    stamp = menage_lot.path(settings).read_text()
    menage_lot.path(settings).write_text(stamp.replace("menage-hebdo", "autre"))
    commands.do_report(settings, False, scan=scan)
    assert menage_lot.voir(settings)["par"] == "autre"  # a pending proposal is kept
