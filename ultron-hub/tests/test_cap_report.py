"""Tests of `ultron-hub cap report` (todo #190): fake job (no real claude), fake quota, throwaway var dir."""

import json
import os
import stat
from types import SimpleNamespace

import pytest

from ultron_hub.cap import commands, report
from ultron_hub.core.job import JobResult
from ultron_hub.core.registry import Registry

NOW = 1_800_000_000.0
FACTS = {"chantiers": [{"nom": "a", "todos": {"ouvertes": [{"id": 1}], "livrees": [], "closes": [], "abandonnees": []}},
                       {"nom": "b", "todos": {}}],
         "commits": [{"sujet": "x"}], "echecs": {"menage": {"total": 2}, "reflexes": []}}


def good(**over):
    out = {"summary": "Semaine correcte.",
           "chantiers": [{"nom": "a", "verdict": "orange", "objectif": "o", "technique": "t", "derives": "d",
                          "preuves": ["todo #1"]}],
           "corrections": [{"titre": "Fusionner les doublons", "chantier": "a", "notes": "n", "preuve": "p"}]}
    out.update(over)
    return out


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(report.cap_collect, "collect", lambda *a, **k: FACTS)
    return SimpleNamespace(root=tmp_path, var_dir=tmp_path / "var", todo_timezone="Europe/Paris")


def fake_job(output, calls, cost=0.42):
    def job(request, settings):
        calls.append(request)
        if isinstance(output, Exception):
            raise output
        if output is None:
            return JobResult(job_id=request.job_id, action=request.action, status="error", error="quota atteint")
        return JobResult(job_id=request.job_id, action=request.action, status="ok", data=output, cost_usd=cost)
    return job


def go(settings, output, calls=None, pct=40.0):
    return report.run(settings, home="/nonexistent", now=NOW, job=fake_job(output, [] if calls is None else calls),
                      week=lambda s: pct)


def test_task_is_opus_without_tools():
    spec = Registry(os.path.join(os.path.dirname(report.__file__), "..", "..", "..", "tasks"), "sonnet", 300).load("revue-cap")
    assert spec.model == "opus" and not spec.tools and not spec.allowed_tools and not spec.add_dirs


def test_ok_writes_files_and_cost(settings):
    calls = []
    r = go(settings, good(), calls)
    assert r["status"] == "ok" and r["cost_usd"] == 0.42 and r["semaine_pct"] == 40.0
    assert json.loads(calls[0].params["metrics"])["commits"] == [{"sujet": "x"}]
    folder = settings.var_dir / "cap"
    for name in ("2027-01-15.md", "2027-01-15.json", "last.json"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600
    md = (folder / "2027-01-15.md").read_text()
    assert "🟠 a" in md and "Fusionner les doublons" in md and "0.42 $" in md and "40 %" in md
    assert json.loads((folder / "last.json").read_text())["corrections"][0]["chantier"] == "a"


def test_quota_guard_makes_no_call(settings, monkeypatch):
    monkeypatch.setattr(report, "QUOTA_MAX_PCT", 88.0)  # explicit user threshold
    calls = []
    r = go(settings, good(), calls, pct=88.0)
    assert calls == [] and r["status"] == "skipped" and "revue sautée (quota)" in r["summary"]
    assert not (settings.var_dir / "cap" / "last.json").exists()
    assert "revue sautée (quota)" in (settings.var_dir / "cap" / "2027-01-15.md").read_text()


def test_unknown_quota_still_reviews(settings):
    assert go(settings, good(), pct=None)["status"] == "ok"


def test_corrections_cut_long_title_and_unknown_chantier(settings):
    k = lambda t, c="a": {"titre": t, "chantier": c, "notes": "n", "preuve": "p"}  # noqa: E731
    r = go(settings, good(corrections=[k("Un"), k("Deux"), k("Trois"), k("Quatre")]))
    assert [c["titre"] for c in r["corrections"]] == ["Un", "Deux", "Trois"]
    r = go(settings, good(corrections=[k("x" * 41), k("Bon", "inconnu"), k("Valide")]))
    assert [c["titre"] for c in r["corrections"]] == ["Valide"]
    r = go(settings, good(corrections=[k("x" * 40)]))
    assert len(r["corrections"]) == 1


def test_dashes_removed(settings):
    r = go(settings, good(summary="a — b"))
    assert r["summary"] == "a - b"


@pytest.mark.parametrize("bad", [
    None, "texte", {"summary": "x"}, good(chantiers=[{"nom": "a", "verdict": "bleu", "objectif": "", "technique": "",
                                                       "derives": "", "preuves": ["p"]}]),
    good(chantiers=[{"nom": "a", "verdict": "vert", "objectif": "", "technique": "", "derives": "", "preuves": []}])])
def test_bad_answer_is_a_fallback_report(settings, bad):
    r = go(settings, bad)
    assert r["status"] == "failed" and r["summary"].startswith("revue impossible : ")
    assert "2 anomalie(s) de ménage" in (settings.var_dir / "cap" / "2027-01-15.md").read_text()
    assert not (settings.var_dir / "cap" / "last.json").exists()


def test_claude_failure_and_exception_never_raise(settings):
    assert go(settings, None)["summary"] == "revue impossible : quota atteint"
    r = go(settings, RuntimeError("boom"))
    assert r["status"] == "failed" and "RuntimeError" in r["summary"]


def test_failed_review_keeps_previous_last(settings):
    go(settings, good())
    go(settings, None)
    assert json.loads((settings.var_dir / "cap" / "last.json").read_text())["status"] == "ok"


def test_collect_failure_is_a_fallback(settings, monkeypatch):
    def boom(*a, **k):
        raise OSError("disque")
    monkeypatch.setattr(report.cap_collect, "collect", boom)
    r = go(settings, good())
    assert r["status"] == "failed" and "disque" in r["summary"]


def test_symlink_target_not_followed(settings):
    folder = settings.var_dir / "cap"
    folder.mkdir(parents=True)
    victim = settings.var_dir / "victim"
    victim.write_text("keep")
    (folder / "last.json").symlink_to(victim)
    go(settings, good())
    assert victim.read_text() == "keep"


def test_unwritable_folder_does_not_raise(settings):
    settings.var_dir.parent.mkdir(exist_ok=True)
    settings.var_dir.write_text("a file, not a folder")
    r = go(settings, good())
    assert r["status"] == "ok" and r["path"] is None


def test_command_envelope(settings, monkeypatch):
    monkeypatch.setattr(report, "QUOTA_MAX_PCT", 88.0)
    monkeypatch.setattr(report, "week_percent", lambda s: 95.0)
    monkeypatch.setattr(report, "run_job", lambda *a, **k: pytest.fail("no call under the quota guard"))
    out = commands.handle(SimpleNamespace(cap_command="report"), settings)
    assert out["status"] == "ok" and out["command"] == "cap.report" and "quota" in out["summary"]


def test_no_default_quota_guard(settings):
    calls = []
    assert go(settings, good(), calls, pct=99.0)["status"] == "ok" and calls
