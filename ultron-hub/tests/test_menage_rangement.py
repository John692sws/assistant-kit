"""Le ménage archive lui-même la Reprise trop grosse (todo #231) : sans réseau, tout sous tmp_path."""

import os
from types import SimpleNamespace

from ultron_hub.menage import commands, rangement

ENTRY = "- **{d}, passation.**\n" + "".join("  - ligne {i} " + "x" * 90 + "\n" for i in range(12))
TEXT = "# Reprise\n\n## 8. Reprise\n- **FOCUS : test.**\n" + "".join(ENTRY.format(d=f"{n}/10", i="{i}").replace("{i}", str(n))
                                                                     for n in range(20, 10, -1))
KEY = "~/notes/prompt/reprise-orchestrateur.md"


def cfg(limit=4):
    return {"budgets": {"files": {KEY: limit}, "archiver_seul": [KEY]}}


def setup(tmp_path, monkeypatch, text=TEXT):
    path = tmp_path / "notes" / "prompt" / "reprise-orchestrateur.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    lines = []
    monkeypatch.setattr(rangement.journal, "record", lambda settings, source, kind, txt, todo=None: lines.append((kind, txt)))
    return path, lines


def test_over_budget_is_archived_and_journaled(tmp_path, monkeypatch):
    path, lines = setup(tmp_path, monkeypatch)
    before = path.read_text().splitlines()
    out = rangement.archiver_reprises(SimpleNamespace(), cfg(), home=str(tmp_path))
    assert out[0]["ecrit"] is True and out[0]["archivees"] == 7 and out[0]["gardees"] == 3
    archive = next((tmp_path / "notes" / "prompt" / "archives").glob("reprises-*.md")).read_text().splitlines()
    for line in before:  # nothing lost
        assert line in path.read_text().splitlines() + archive or not line.strip()
    assert len(lines) == 1 and lines[0][0] == "menage" and "7 entrée(s)" in lines[0][1]


def test_under_budget_does_nothing(tmp_path, monkeypatch):
    path, lines = setup(tmp_path, monkeypatch)
    assert rangement.archiver_reprises(SimpleNamespace(), cfg(limit=500), home=str(tmp_path)) == []
    assert path.read_text() == TEXT and lines == []
    assert not (tmp_path / "notes" / "prompt" / "archives").exists()


def test_file_not_listed_is_left_alone(tmp_path, monkeypatch):
    path, lines = setup(tmp_path, monkeypatch)
    conf = {"budgets": {"files": {KEY: 4}}}
    assert rangement.archiver_reprises(SimpleNamespace(), conf, home=str(tmp_path)) == []
    assert path.read_text() == TEXT


def test_failure_never_raises(tmp_path, monkeypatch):
    path, lines = setup(tmp_path, monkeypatch, text="x" * 6000)  # no « ## 8. Reprise » section
    out = rangement.archiver_reprises(SimpleNamespace(), cfg(), home=str(tmp_path))
    assert out[0]["ecrit"] is False and "erreur" in out[0] and lines == []
    assert path.read_text() == "x" * 6000


def test_symlink_is_ignored(tmp_path, monkeypatch):
    path, _ = setup(tmp_path, monkeypatch)
    real = tmp_path / "real.md"
    real.write_text(TEXT)
    path.unlink()
    os.symlink(real, path)
    assert rangement.archiver_reprises(SimpleNamespace(), cfg(), home=str(tmp_path)) == []


def test_report_archives_before_scanning_only_without_given_scan(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rangement, "archiver_reprises", lambda settings, conf, home=None: calls.append(1) or [])
    monkeypatch.setattr(commands, "run_scan", lambda settings: {"anomalies": [], "counts": {}, "disk": [], "sizes": {}, "warnings": []})
    monkeypatch.setattr(commands.rules, "load_config", lambda p: {})
    monkeypatch.setattr(commands.journal, "record", lambda *a, **k: None)
    monkeypatch.setattr(commands.audit, "due", lambda *a: False)
    settings = SimpleNamespace(root=tmp_path, var_dir=tmp_path / "var", todo_notify_target="t", hermes_binary="h")
    commands.do_report(settings, False, ai=lambda m: {})
    assert calls == [1]
    commands.do_report(settings, False, scan={"anomalies": [], "counts": {}, "disk": [], "sizes": {}, "warnings": []}, ai=lambda m: {})
    assert calls == [1]
