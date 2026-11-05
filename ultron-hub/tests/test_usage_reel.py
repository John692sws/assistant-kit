"""Real-use counter (todo #166): keys written by the plugin, read by `menage usage` and the weekly report."""

import json
import re
import time

import pytest


from ultron_hub import cli
from ultron_hub.common.settings import load_settings
from ultron_hub.menage import usage

NOW = time.mktime((2026, 10, 18, 12, 0, 0, 0, 0, -1))








def write_log(var_dir, rows):
    folder = var_dir / "usage-reel"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "2026-10.jsonl").write_text("\n".join(rows) + "\n")


def test_summary_counts_recent_keys_and_lists_unused_doors(tmp_path):
    old = NOW - 40 * 86400
    write_log(tmp_path, [json.dumps({"t": NOW - 60, "k": "cmd:start"})] * 3
              + [json.dumps({"t": NOW - 60, "k": "hub:an:open"}), json.dumps({"t": old, "k": "hub:mtg:open"}),
                 "pas du json", json.dumps({"t": NOW, "k": "../etc"}), json.dumps({"k": "cmd:plus"})])
    data = usage.summary(tmp_path, now=NOW)
    assert data["total"] == 4 and data["top"][0] == {"cle": "cmd:start", "libelle": "/start (À toi)", "appuis": 3}
    assert "🎙 Montage" in data["jamais"] and "📥 Analyser" not in data["jamais"]
    line = usage.line(data)
    assert line.startswith("📊 Usage 30 j : 4 appuis ; le plus : /start (À toi) 3, 📥 Analyser 1 ; jamais servi : ")
    assert "rien n'est retiré tout seul" in usage.text(data)


def test_no_counter_yet(tmp_path):
    data = usage.summary(tmp_path, now=NOW)
    assert data["compteur_actif"] is False and usage.line(data) == "📊 Usage réel : compteur pas encore actif."




def test_cli_usage(project, capsys):
    settings = load_settings()
    write_log(settings.var_dir, [json.dumps({"t": time.time(), "k": "hub:sv:ask"})])
    assert cli.main(["menage", "usage", "--jours", "7"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["total"] == 1 and re.match(r"📊 Usage 7 j : 1 appuis", out["summary"])
    assert cli.main(["menage", "usage", "--jours", "0"]) == 1
    assert "--jours" in json.loads(capsys.readouterr().out)["error"]
