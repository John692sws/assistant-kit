"""System state page (todo #170): throwaway git repo, fake journal, size cap, no network."""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tests.test_esprit import mind, run  # noqa: F401 (mind = fixture)
from ultron_hub.esprit import etat, focus, store

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def g(path, *args):
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


def commit(path, name, text, msg="c"):
    (path / name).write_text(text)
    g(path, "add", name)
    g(path, "commit", "-m", msg)




@pytest.fixture
def repo(mind, project):
    """The project root becomes a throwaway git repo: main + 170 (clean), 171 (stacked on 170), 172 (conflicts)."""
    root = project
    g(root, "init", "-b", "main")
    commit(root, "a.txt", "base\n")
    g(root, "switch", "-c", "todo-170/un")
    commit(root, "b.txt", "b\n")
    g(root, "switch", "-c", "todo-171/deux")
    commit(root, "c.txt", "c\n")
    g(root, "switch", "main")
    g(root, "switch", "-c", "todo-172/trois")
    commit(root, "a.txt", "trois\n")
    g(root, "switch", "main")
    commit(root, "a.txt", "main change\n")
    return root


def entry(mind, kind, text, when):
    row = {"date": when.isoformat(), "source": "ultron-hub", "type": kind, "todo": None, "texte": text}
    with open(store.root(mind) / store.JOURNAL, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def test_page_content(repo, mind):
    focus.write(mind, ["systeme", "business"], "cli")
    entry(mind, "fusion", "Branche todo-169 fusionnée", NOW - timedelta(hours=2))
    entry(mind, "fusion", "Vieille fusion", NOW - timedelta(hours=30))
    entry(mind, "todo", "Pas une fusion", NOW - timedelta(hours=1))
    text = etat.fit(etat.collect(mind, NOW))
    assert "systeme + business" in text
    lines = [line for line in text.splitlines() if line[:2] in ("1.", "2.", "3.")]
    assert [x.split()[1] for x in lines] == ["#170", "#171", "#172"]  # 171 stacked after its parent 170
    assert "sans conflit" in lines[0] and "après #170" in lines[1] and "CONFLIT" in lines[2]
    assert "Branche todo-169 fusionnée" in text and "Vieille fusion" not in text and "Pas une fusion" not in text
    assert len(text.encode()) <= etat.MAX_BYTES and "Sources illisibles" not in text


def test_hash_matches_branch(repo, mind):
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=7", "todo-170/un"], capture_output=True,
                         text=True, check=True).stdout.strip()
    assert f"todo-170/un {sha} " in etat.render(etat.collect(mind, NOW))


def test_size_cap_and_hard_cut(mind):
    data = etat.collect(mind, NOW)
    data["branches"] = []
    data["journal"] = [{"date": NOW.isoformat(), "texte": "x" * 300} for _ in range(60)]
    data["atoi"] = [{"icone": f"i{i}", "todo": i, "texte": "y" * 200} for i in range(80)]
    text = etat.fit(data)
    assert len(text.encode()) <= etat.MAX_BYTES and "autre(s)" in text
    data["apres"] = [{"todo": i, "actions": ["z" * 400]} for i in range(200)]
    assert len(etat.fit(data).encode()) <= etat.MAX_BYTES


def test_unreadable_source_is_named(mind, monkeypatch):
    monkeypatch.setattr(etat, "_atoi", lambda settings: 1 / 0)
    text = etat.fit(etat.collect(mind, NOW))
    assert "Sources illisibles : à toi" in text and "## Branches" in text


def test_after_merge(mind, project):
    folder = project / "var" / "after-merge"
    folder.mkdir(parents=True)
    (folder / "todo-165.json").write_text(json.dumps({"todo_id": 165, "proofs": [], "actions": [
        {"action": "user_unit_enable", "status": "pending"}, {"action": "x", "status": "done"}]}))
    text = etat.fit(etat.collect(mind, NOW))
    assert "#165 : user_unit_enable" in text and "## Missions" not in text


def test_cli_writes_file_and_refresh_is_quiet(mind, capsys, esprit_dir):
    code, out = run(capsys, "esprit", "etat")
    assert code == 0 and out["data"]["bytes"] <= etat.MAX_BYTES
    assert (esprit_dir / "etat.md").read_text().startswith("# État du système")
    assert (esprit_dir / "etat.md").stat().st_mode & 0o777 == 0o600
    (esprit_dir / "etat.md").unlink()
    etat.refresh(mind)
    assert (esprit_dir / "etat.md").exists()


def test_refresh_without_esprit_does_nothing(project, esprit_dir):
    from ultron_hub.common.settings import load_settings
    etat.refresh(load_settings())
    assert not esprit_dir.exists()


def test_budget_in_menage_matches():
    cfg = yaml.safe_load((REPO / "config" / "menage.yaml").read_text())
    assert cfg["budgets"]["files"]["~/ultron/esprit/etat.md"] * 1024 == etat.MAX_BYTES
    assert "~/ultron/esprit/etat.md" in cfg["budgets"]["actions"]


def test_call_sites_regenerate():
    root = REPO / "src" / "ultron_hub"
    assert "etat.refresh(settings)" in (root / "todo" / "commands.py").read_text()
    text = (root / "branch_commands.py").read_text()
    assert text.count("etat.refresh(settings)") == 5  # merge, merge-lot, auto-merge, revert-last (#254), auto-merge-lot (#533)
