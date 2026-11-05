"""Tests of `ultron-hub cap collect` (todo #189): fake HOME, throwaway DB, journal and git repo under tmp_path."""

import argparse
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ultron_hub.cap import collect, commands
from ultron_hub.chantier import core as chantier_core

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
SECRET = "TOP-SECRET-SENTINEL"


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / "notes" / "prompt").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / ".ssh" / "key").write_text(SECRET)
    (home / "notes" / "chantier-a.md").write_text(
        "# Chantier A\n\n## Objectif\nFaire A vite.\n\n## Décisions\n- D1 retenue\n\n## Autre\nignoré\n")
    (home / "notes" / "chantier-b.md").write_text("# B\n\nUn premier paragraphe sans titre d'objectif.\n")
    prompts = home / "notes" / "prompt"
    for name, dom, fiche in (("a", "systeme", "~/notes/chantier-a.md"), ("b", "perso", "~/notes/chantier-b.md")):
        (prompts / f"{name}.md").write_text(
            f"# Reprise « {name.upper()} »\nDomaine : `{dom}`\nFiche du chantier : `{fiche}`\n\n## Contexte\nx\n")
    esprit = tmp_path / "esprit"
    esprit.mkdir()
    var = tmp_path / "var"
    (var / "menage").mkdir(parents=True)
    (var / "menage" / "last.json").write_text(json.dumps(
        {"counts": {"2": 1}, "anomalies": [{"check": 2, "detail": "dossier non déclaré"}]}))
    (var / "cap").mkdir()
    (var / "cap" / "last.json").write_text(json.dumps({"corrections": [{"titre": "Faire X"}]}))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "menage.yaml").write_text("secrets: ['~/notes/chantier-secret.md']\nexclusions: []\n")
    db = tmp_path / "ultron.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, notes TEXT DEFAULT '', status TEXT, done_at TEXT,
                            updated_at TEXT, chantier TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INT, at TEXT, action TEXT, details TEXT);""")
    rows = [(1, "Ouverte", "todo", None, iso(30), "a"), (2, "Livrée", "todo", None, iso(3), "a"),
            (3, "Close récente", "done", iso(2), iso(2), "a"), (4, "Close ancienne", "done", iso(20), iso(20), "a"),
            (5, "Abandonnée", "cancelled", None, iso(1), "a"), (6, "Autre chantier", "todo", None, iso(1), "b")]
    for tid, title, status, done, upd, chantier in rows:
        conn.execute("INSERT INTO tasks (id, title, status, done_at, updated_at, chantier) VALUES (?,?,?,?,?,?)",
                     (tid, title, status, done, upd, chantier))
    conn.execute("INSERT INTO events (task_id, at, action, details) VALUES (2, ?, 'livree', '{}')", (iso(3),))
    conn.commit()
    conn.close()
    entries = [{"date": iso(1), "source": "ultron-hub", "type": "todo", "todo": 1, "texte": "récent " + "x" * 400},
               {"date": iso(2), "source": "claude-code", "type": "note", "todo": None, "texte": "autre"},
               {"date": iso(20), "source": "ultron-hub", "type": "todo", "todo": 9, "texte": "ancien"}]
    (esprit / "journal.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\ntorn{\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a, d=None: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), *a], check=True, capture_output=True,
        env={"PATH": "/usr/bin:/bin", "GIT_AUTHOR_DATE": d or iso(1), "GIT_COMMITTER_DATE": d or iso(1),
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    git("init", "-b", "main")
    (repo / "f").write_text("1")
    git("add", "f")
    git("commit", "-m", "Ancien (todo #1)", d=iso(20))
    (repo / "f").write_text("2")
    git("commit", "-am", "Récent (todo #7)")
    settings = SimpleNamespace(root=tmp_path, var_dir=var, todo_db=db, esprit_dir=esprit,
                               todo_timezone="Europe/Paris", repos_dir=tmp_path / "repos")
    src = chantier_core.Sources(prompts=prompts, boites=tmp_path / "boites")
    (esprit / "focus.json").write_text(json.dumps({"domaines": ["systeme"], "depuis": "x", "par": "t"}))
    return SimpleNamespace(home=home, settings=settings, src=src, repo=repo, tmp=tmp_path, esprit=esprit)


def run(env):
    return collect.collect(env.settings, home=str(env.home), now=NOW.timestamp(), src=env.src, repo=env.repo)


def by_name(data):
    return {c["nom"]: c for c in data["chantiers"]}


def test_shape_and_focus_first(env):
    data = run(env)
    json.dumps(data)
    assert set(data) == {"depuis", "jusqu_a", "focus", "chantiers", "commits", "journal", "echecs",
                         "revue_precedente", "non_revus", "warnings"}
    assert data["focus"] == ["systeme"]
    assert [c["nom"] for c in data["chantiers"]] == ["a", "b"] and data["chantiers"][0]["focus"] is True
    assert data["chantiers"][1]["focus"] is False
    assert data["revue_precedente"]["corrections"][0]["titre"] == "Faire X"


def test_fiche_extracts(env):
    a, b = by_name(run(env))["a"], by_name(run(env))["b"]
    assert a["objectif"] == "Faire A vite." and a["decisions"] == "- D1 retenue"
    assert b["objectif"].startswith("Un premier paragraphe") and "decisions" in b


def test_extract_is_capped(env):
    (env.home / "notes" / "chantier-a.md").write_text("## Objectif\n" + "y" * 5000)
    assert len(by_name(run(env))["a"]["objectif"]) == collect.EXTRACT_MAX


def test_todos_of_the_week(env):
    todos = by_name(run(env))["a"]["todos"]
    assert [t["id"] for t in todos["ouvertes"]] == [1, 2]
    assert [t["id"] for t in todos["livrees"]] == [2]
    assert [t["id"] for t in todos["closes"]] == [3]
    assert [t["id"] for t in todos["abandonnees"]] == [5]


def test_commits_of_the_week_only(env):
    commits = run(env)["commits"]
    assert [(c["sujet"], c["todo"]) for c in commits] == [("Récent (todo #7)", 7)]


def test_journal_week_counts_and_cut(env):
    j = run(env)["journal"]
    assert j["par_source"] == {"ultron-hub": 1, "claude-code": 1}
    assert len(j["dernieres"]) == 2 and all(len(e["texte"]) <= collect.TEXT_MAX for e in j["dernieres"])


def test_failures(env):
    f = run(env)["echecs"]
    assert set(f) == {"menage", "reflexes"}
    assert f["menage"]["total"] == 1 and f["menage"]["comptes"] == {"2": 1}


def test_unreadable_sources_become_warnings(env):
    env.settings.todo_db.write_bytes(b"not a database")
    (env.esprit / "journal.jsonl").unlink()
    (env.settings.var_dir / "menage" / "last.json").write_text("{bad")
    env.repo = env.tmp / "absent"
    data = run(env)
    text = " | ".join(data["warnings"])
    assert "todos illisibles" in text and "last.json illisible" in text and "commits" in text


def test_secrets_and_links_never_read(env):
    notes = env.home / "notes"
    (notes / "chantier-secret.md").write_text(SECRET)          # in the YAML secrets
    (notes / "chantier-lien.md").symlink_to(env.home / ".ssh" / "key")
    for name, fiche in (("s", "~/notes/chantier-secret.md"), ("l", "~/notes/chantier-lien.md"),
                        ("t", "~/.ssh/key"), ("u", "~/../../etc/passwd")):
        (env.src.prompts / f"{name}.md").write_text(f"# « {name} »\nDomaine : `perso`\nFiche du chantier : `{fiche}`\n")
    data = run(env)
    assert SECRET not in json.dumps(data)
    assert len([w for w in data["warnings"] if "fiche" in w]) == 4


def test_budget_drops_chantiers_out_of_focus(env, monkeypatch):
    for n in range(12):
        (env.src.prompts / f"x{n:02d}.md").write_text(f"# « x{n} »\nDomaine : `perso`\nFiche du chantier : `~/notes/chantier-x{n}.md`\n")
        (env.home / "notes" / f"chantier-x{n}.md").write_text("## Objectif\n" + "z" * 1400)
    monkeypatch.setattr(collect, "BUDGET", 6000)
    data = run(env)
    assert collect._size(data) <= 6000
    assert data["chantiers"][0]["nom"] == "a" and data["non_revus"]
    assert set(data["non_revus"]) <= {"b"} | {f"x{n:02d}" for n in range(12)}
    assert any("non revus" in w for w in data["warnings"])


def test_command_envelope(env, monkeypatch):
    monkeypatch.setattr(collect, "default_home", lambda: str(env.home), raising=False)
    monkeypatch.setattr("ultron_hub.cap.collect.chantier_core.sources", lambda: env.src)
    monkeypatch.setattr("ultron_hub.cap.collect.audit.default_home", lambda: str(env.home))
    out = commands.handle(argparse.Namespace(cap_command="collect"), env.settings)
    json.dumps(out)
    assert out["status"] == "ok" and out["command"] == "cap.collect" and out["error"] is None
    assert out["data"]["chantiers"]


def snapshot(*roots: Path) -> dict[str, tuple]:
    snap = {}
    for root in roots:
        for p in sorted(root.rglob("*")):
            st = p.lstat()
            snap[str(p)] = (st.st_mtime_ns, st.st_size, p.read_bytes() if p.is_file() and not p.is_symlink() else None)
    return snap


def test_collect_writes_nothing(env):
    before = snapshot(env.tmp)
    run(env)
    assert snapshot(env.tmp) == before


def test_package_source_has_no_write_primitive():
    forbidden = re.compile(r"unlink|rename|rmtree|os\.remove|\.remove\(|write_text|write_bytes|\.mkdir|makedirs|"
                           r"os\.replace|shutil\.(move|copy)|\.touch\(|chmod|\.write\(")
    open_write = re.compile(r"open\([^)]*[\"'][rwxab+]*[wxa+][rwxab+]*[\"']")
    package = Path(__file__).resolve().parent.parent / "src" / "ultron_hub" / "cap"
    files = sorted(package.glob("*.py"))
    assert len(files) >= 3
    for path in files:
        code = "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines()
                         if not line.lstrip().startswith(('"""', "'''")))
        assert not forbidden.search(code), f"{path.name}: write primitive"
        assert not open_write.search(code), f"{path.name}: open() in write mode"


def test_reflexes_of_the_watcher_best_effort(env):
    assert run(env)["echecs"]["reflexes"] == []  # absent file: empty, no warning
    assert not any("reflexes" in w for w in run(env)["warnings"])
    health = env.settings.var_dir / "health"
    health.mkdir()
    (health / "reflexes.json").write_text(json.dumps({"diagnostics": [{"nom": "verrou", "diagnostic": "d" * 500}, "texte", 3]}))
    reflexes = run(env)["echecs"]["reflexes"]
    assert reflexes[0]["nom"] == "verrou" and len(reflexes[0]["diagnostic"]) == collect.TEXT_MAX
    assert reflexes[1] == {"diagnostic": "texte"} and len(reflexes) == 2
    (health / "reflexes.json").write_text("{torn")
    data = run(env)
    assert data["echecs"]["reflexes"] == [] and "var/health/reflexes.json illisible" in data["warnings"]
