"""Todo #127: bimonthly audit of the tidy-up map (due/mark_done, collect, run with a fake job)."""

import builtins
import json
import os
import stat

import pytest

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.settings import Settings
from ultron_hub.core.job import JobResult
from ultron_hub.menage import audit

DAY = 86400
NOW = 1_800_000_000.0

CONFIG = """\
version: 1
exclusions:
  - ~/forum
  - ~/.ssh
secrets:
  - ~/.sudo_pass
  - ~/.hermes/auth.json
  - ~/.config/ultron-hub
  - "**/auth.json"
structure:
  expected: [~/notes]
"""


def settings_for(tmp_path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "config" / "menage.yaml").write_text(CONFIG, encoding="utf-8")
    return Settings(root=root, tasks_dir=root / "tasks", var_dir=root / "var", runs_dir=root / "var" / "runs",
                    logs_dir=root / "var" / "logs")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / "notes").mkdir(parents=True)
    (h / "notes" / "serveur.md").write_text(
        "# Serveur\n\nintro\n\n## Architecture Workspace\n\nprojects/active = projets\n\n### Détail\n\nsous-section\n\n"
        "## Autre\n\nNE PAS ENVOYER\n", encoding="utf-8")
    (h / "ultron" / "esprit").mkdir(parents=True)
    (h / "ultron" / "esprit" / "charte.md").write_text("Charte " + "x" * 6000, encoding="utf-8")
    (h / "workspace" / "projects").mkdir(parents=True)
    (h / "workspace" / "a.txt").write_text("a")
    (h / "forum").mkdir()
    (h / "forum" / "FORUM_MARKER.txt").write_text("secret")
    (h / ".ssh").mkdir()
    (h / ".ssh" / "id_ed25519").write_text("SSH_MARKER")
    (h / ".sudo_pass").write_text("SUDO_MARKER")
    (h / ".hermes").mkdir()
    (h / ".hermes" / "auth.json").write_text("AUTH_MARKER")
    (h / ".hermes" / "config.yaml").write_text("c")
    (h / ".config" / "ultron-hub").mkdir(parents=True)
    (h / ".config" / "ultron-hub" / "close.key").write_text("KEY_MARKER")
    (h / "link").symlink_to(h / "workspace")
    return h


# --- due / mark_done ------------------------------------------------------------------------------------------------

def test_due_without_state_or_with_garbage(tmp_path):
    assert audit.due(tmp_path, NOW)
    (tmp_path / "audit.json").write_text("not json")
    assert audit.due(tmp_path, NOW)
    (tmp_path / "audit.json").write_text(json.dumps({"last_ts": "x"}))
    assert audit.due(tmp_path, NOW)
    (tmp_path / "audit.json").write_text(json.dumps({"nope": 1}))
    assert audit.due(tmp_path, NOW)


def test_due_after_sixty_days_only(tmp_path):
    audit.mark_done(tmp_path, NOW - 59 * DAY)
    assert not audit.due(tmp_path, NOW)
    audit.mark_done(tmp_path, NOW - 60 * DAY)
    assert audit.due(tmp_path, NOW)
    audit.mark_done(tmp_path, NOW - 10 * DAY)
    assert audit.due(tmp_path, NOW, every_days=7)


def test_due_with_a_date_in_the_future(tmp_path):
    audit.mark_done(tmp_path, NOW + DAY)
    assert audit.due(tmp_path, NOW)


def test_mark_done_is_private_and_atomic(tmp_path):
    state = tmp_path / "var" / "menage"
    audit.mark_done(state, NOW)
    assert json.loads((state / "audit.json").read_text()) == {"last_ts": NOW}
    assert stat.S_IMODE((state / "audit.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) & 0o077 == 0
    assert not list(state.glob("*.tmp"))
    audit.mark_done(state, NOW + 5)
    assert json.loads((state / "audit.json").read_text()) == {"last_ts": NOW + 5}
    assert not audit.due(state, NOW + 6)


# --- collect --------------------------------------------------------------------------------------------------------

def test_collect_never_opens_or_lists_a_secret(tmp_path, home, monkeypatch):
    settings = settings_for(tmp_path)
    opened, scanned = [], []
    real_open, real_scandir = builtins.open, os.scandir

    def spy_open(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    def spy_scandir(path="."):
        scanned.append(str(path))
        return real_scandir(path)

    docs = [home / "notes" / "serveur.md", home / "ultron" / "esprit" / "charte.md"]
    with monkeypatch.context() as m:
        m.setattr(builtins, "open", spy_open)
        m.setattr(os, "scandir", spy_scandir)
        data = audit.collect(str(home), settings.root / "config" / "menage.yaml", docs)

    dump = json.dumps({k: data[k] for k in ("tree", "docs", "warnings")})  # the raw map names the secrets itself
    for marker in ("FORUM_MARKER", "SSH_MARKER", "SUDO_MARKER", "AUTH_MARKER", "KEY_MARKER", "close.key", "forum",
                   ".ssh", ".sudo_pass", "auth.json"):
        assert marker not in dump, marker
    forbidden = (str(home / "forum"), str(home / ".ssh"), str(home / ".config" / "ultron-hub"))
    assert not [p for p in opened + scanned if p.startswith(forbidden)]
    assert not [p for p in opened if p.endswith(("auth.json", ".sudo_pass"))]

    names = {e["name"]: e for e in data["tree"]}
    assert {"notes", "workspace", "ultron", ".hermes", ".config", "link"} <= set(names)
    assert names["workspace"]["entries"] == 2
    assert {c["name"] for c in names["workspace"]["children"]} == {"projects", "a.txt"}
    assert names["link"]["type"] == "link" and "children" not in names["link"]  # links are not followed
    assert {c["name"] for c in names[".hermes"]["children"]} == {"config.yaml"}
    assert data["yaml"] == CONFIG
    assert data["warnings"] == []


def test_collect_doc_excerpts(tmp_path, home):
    settings = settings_for(tmp_path)
    docs = [home / "notes" / "serveur.md", home / "ultron" / "esprit" / "charte.md"]
    data = audit.collect(str(home), settings.root / "config" / "menage.yaml", docs)
    server = data["docs"]["serveur.md"]
    assert "projects/active = projets" in server and "sous-section" in server
    assert "NE PAS ENVOYER" not in server and "intro" not in server
    assert len(data["docs"]["charte.md"]) == audit.DOC_MAX


def test_collect_is_best_effort(tmp_path, home):
    data = audit.collect(str(home), tmp_path / "missing.yaml", [home / "notes" / "absent.md"])
    assert data["yaml"] == ""
    assert "carte illisible" in data["warnings"]
    assert "absent.md illisible" in data["warnings"]
    # Without the map, the fixed exclusions still hold.
    assert "forum" not in json.dumps(data["tree"]) and ".ssh" not in json.dumps(data["tree"])


def test_collect_cuts_a_big_map(tmp_path, home):
    big = tmp_path / "big.yaml"
    big.write_text("version: 1\n" + "# " + "y" * 20000 + "\n")
    data = audit.collect(str(home), big, [])
    assert len(data["yaml"]) == audit.YAML_MAX
    assert any("12 Ko" in w for w in data["warnings"])


def test_collect_refuses_a_doc_that_is_a_link(tmp_path, home):
    target = home / "notes" / "serveur.md"
    link = home / "notes" / "lien.md"
    link.symlink_to(target)
    data = audit.collect(str(home), settings_for(tmp_path).root / "config" / "menage.yaml", [link])
    assert data["docs"] == {}
    assert "lien.md illisible" in data["warnings"]


# --- run ------------------------------------------------------------------------------------------------------------

def fake_job(output, calls):
    def job(request, settings):
        calls.append(request)
        if isinstance(output, Exception):
            return JobResult(job_id=request.job_id, action=request.action, status="error", error="boom")
        return JobResult(job_id=request.job_id, action=request.action, status="ok", data=output)
    return job


def test_run_writes_the_report_and_marks_done(tmp_path, home):
    settings = settings_for(tmp_path)
    calls = []
    output = {"summary": "Carte à jour — presque.", "proposals": [
        {"section": "structure", "changement": "Déclarer ~/scripts", "raison": "Dossier présent — durable",
         "yaml": "structure:\n  expected:\n    - ~/scripts"},
        {"section": "inconnue", "changement": "x", "raison": "y", "yaml": "a: 1"},        # unknown section
        {"section": "space", "changement": "x", "raison": "y", "yaml": "a: [unclosed"},   # invalid YAML
    ]}
    result = audit.run(settings, home=str(home), now=NOW, job=fake_job(output, calls))

    assert calls[0].action == "menage-audit"
    sent = json.loads(calls[0].params["metrics"])
    assert sent["yaml"] == CONFIG and "workspace" in {e["name"] for e in sent["tree"]}
    assert "FORUM_MARKER" not in calls[0].params["metrics"]

    assert [p["section"] for p in result["proposals"]] == ["structure"]
    assert "—" not in json.dumps(result, ensure_ascii=False)
    assert result["summary"] == "Carte à jour - presque."
    path = settings.var_dir / "menage" / f"audit-{result['path'][-13:-3]}.md"
    assert str(path) == result["path"]
    text = path.read_text(encoding="utf-8")
    assert "Déclarer ~/scripts" in text and "```yaml" in text and "Rien n'est appliqué" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not audit.due(settings.var_dir / "menage", NOW + DAY)
    assert audit.due(settings.var_dir / "menage", NOW + 61 * DAY)
    assert (settings.root / "config" / "menage.yaml").read_text() == CONFIG  # nothing applied


def test_run_without_proposal(tmp_path, home):
    settings = settings_for(tmp_path)
    result = audit.run(settings, home=str(home), now=NOW,
                       job=fake_job({"summary": "Rien à changer.", "proposals": []}, []))
    assert result["proposals"] == []
    assert "Aucune modification proposée." in open(result["path"], encoding="utf-8").read()


def test_run_failure_leaves_no_trace(tmp_path, home):
    settings = settings_for(tmp_path)
    with pytest.raises(UltronHubError):
        audit.run(settings, home=str(home), now=NOW, job=fake_job(RuntimeError(), []))
    assert not (settings.var_dir / "menage").exists()
    assert audit.due(settings.var_dir / "menage", NOW)


@pytest.mark.parametrize("bad", [None, "text", {"summary": 1, "proposals": []}, {"summary": "s"},
                                 {"summary": "s", "proposals": "no"}])
def test_run_rejects_a_malformed_answer(tmp_path, home, bad):
    settings = settings_for(tmp_path)
    with pytest.raises(UltronHubError):
        audit.run(settings, home=str(home), now=NOW, job=fake_job(bad, []))
    assert audit.due(settings.var_dir / "menage", NOW)


def test_task_files_are_consistent():
    from pathlib import Path
    task = Path(__file__).resolve().parent.parent / "tasks" / "menage-audit"
    schema = json.loads((task / "output.schema.json").read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["proposals"]["maxItems"] == 8
    assert "{metrics}" in (task / "prompt.md").read_text(encoding="utf-8")
