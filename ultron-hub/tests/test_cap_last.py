"""`cap last` (todo #192): the CLI reads the last review; the /cap screens of the bot are retired (todo #463)."""

import argparse
import json
from types import SimpleNamespace

from ultron_hub.cap import commands, report

REVIEW = {"date": "2027-01-17", "status": "ok", "summary": "ok", "faits": "x", "cost_usd": 0.4, "semaine_pct": 30.0,
          "chantiers": [{"nom": "fba", "verdict": "orange", "objectif": "but", "technique": "t", "derives": "dérive",
                         "preuves": ["p"]}],
          "corrections": [{"titre": "Relancer le test FBA", "chantier": "fba", "notes": "notes autosuffisantes",
                           "preuve": "commit abc"},
                          {"titre": "Ranger les notes", "chantier": "fba", "notes": "n2", "preuve": "p2"}]}


def settings_with(tmp_path, report_data=None):
    (tmp_path / "cap").mkdir()
    if report_data is not None:
        (tmp_path / "cap" / "last.json").write_text(json.dumps(report_data), encoding="utf-8")
    return SimpleNamespace(var_dir=tmp_path)


def test_cap_last_reads_last_json(tmp_path):
    out = commands.handle(argparse.Namespace(cap_command="last"), settings_with(tmp_path, REVIEW))
    assert out["status"] == "ok" and out["command"] == "cap.last" and out["data"]["exists"] is True
    assert out["data"]["date"] == "2027-01-17" and len(out["data"]["corrections"]) == 2
    assert out["data"]["text"] == report.short_text(REVIEW)


def test_cap_last_without_review(tmp_path):
    out = commands.handle(argparse.Namespace(cap_command="last"), settings_with(tmp_path))
    assert out["status"] == "ok" and out["data"]["exists"] is False
