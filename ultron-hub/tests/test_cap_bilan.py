"""Tests of `ultron-hub bilan semaine` (todo #256): each counter on fake data, no journalctl, no network, no AI."""

import argparse
import json
import os
import sqlite3
import time
from types import SimpleNamespace

from ultron_hub.cap import bilan, commands

NOW = 1_800_000_000.0


def iso(days_ago):
    from datetime import datetime
    return datetime.fromtimestamp(NOW - days_ago * 86400).astimezone().isoformat(timespec="seconds")


def make_db(path):
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE tasks (id INTEGER PRIMARY KEY, nature TEXT, status TEXT, done_at TEXT);
    CREATE TABLE events (id INTEGER PRIMARY KEY, task_id INT, at TEXT, action TEXT, details TEXT);""")
    c.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [
        (1, "fond", "done", iso(1)), (2, "chantier", "done", iso(2)), (3, "fond", "done", iso(20)),
        (4, "chantier", "todo", None), (5, "fond", "todo", None), (6, "fond", "cancelled", iso(1))])
    c.executemany("INSERT INTO events (task_id, at, action, details) VALUES (?,?,?,?)", [
        (4, iso(1), "livree", "{}"), (4, iso(0), "edit", '{"clair": "x"}'),
        (5, iso(30), "livree", "{}")])
    c.commit()
    c.close()


def test_todos_by_nature(tmp_path):
    db = tmp_path / "t.db"
    make_db(db)
    w = []
    out = bilan.todos(db, NOW - 7 * 86400, w)
    assert (out["fond"], out["chantier"], out["total"]) == (1, 2, 3) and out["ids"]["chantier"] == [2, 4] and not w
    w = []
    assert bilan.todos(tmp_path / "none.db", 0, w)["total"] == 0 and w


def test_buttons_by_action_and_merge_share(tmp_path):
    folder = tmp_path / "usage-reel"
    folder.mkdir()
    rows = [{"t": NOW - 100, "k": "hub:merge:aq"}, {"t": NOW - 90, "k": "hub:merge:ax"}, {"t": NOW - 80, "k": "cmd:start"},
            {"t": NOW - 70, "k": "hub:at:plus"}, {"t": NOW - 20 * 86400, "k": "hub:merge:old"}]
    (folder / "2027-01.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    out = bilan.buttons(tmp_path, NOW)
    assert out["total"] == 4 and out["fusions"] == 2 and out["part_fusions_pct"] == 50
    assert out["par_action"]["hub:merge"] == 2 and out["par_action"]["cmd:start"] == 1
    assert bilan.buttons(tmp_path / "vide", NOW)["part_fusions_pct"] is None


def test_scripts_versus_ai(tmp_path):
    for name, action, age in (("a", "journal-resume", 1), ("b", "journal-resume", 2), ("c", "veille", 30)):
        d = tmp_path / name
        d.mkdir()
        p = d / "request.json"
        p.write_text(json.dumps({"action": action}))
        os.utime(p, (NOW - age * 86400,) * 2)
    lines = lambda: ["Started ultron-hub-cap.service - x.", "Started ultron-hub-backup.service - y.",
                     "Started ultron-hub-cap.service - x.", "Started other.service - z.", "noise"]
    w = []
    out = bilan.scripts_vs_ai(tmp_path, NOW - 7 * 86400, w, lines)
    assert out["par_minuterie"] == {"ultron-hub-cap": 2, "ultron-hub-backup": 1} and out["minuteries"] == 3
    assert out["par_action_ia"] == {"journal-resume": 2} and out["part_scripts_pct"] == 60 and not w

    def boom():
        raise OSError("no journal")
    w = []
    out = bilan.scripts_vs_ai(tmp_path, NOW - 7 * 86400, w, boom)
    assert out["minuteries"] == 0 and out["ia"] == 2 and w


def test_run_text_and_send(tmp_path):
    make_db(tmp_path / "t.db")
    s = SimpleNamespace(todo_db=tmp_path / "t.db", var_dir=tmp_path, runs_dir=tmp_path / "runs",
                        todo_notify_target="x", hermes_binary=None)
    data = bilan.run(s, now=NOW, lines=lambda: ["Started ultron-hub-cap.service - x."], quota=lambda _s: None)
    assert data["quota_semaine_pct"] is None and "non mesuré" in data["text"] and "—" not in data["text"]
    assert "3 (1 de fond, 2 de chantier)" in data["text"]
    sent = []
    assert bilan.send(s, data, sender=lambda *a, **k: sent.append(a))["sent"] is True and sent[0][0] == data["text"]
    assert bilan.send(s, data, sender=lambda *a, **k: 1 / 0)["sent"] is False


def test_command_envelope(monkeypatch):
    monkeypatch.setattr(commands.bilan, "run", lambda s: {"text": "t\nl2", "warnings": []})
    sent = []
    monkeypatch.setattr(commands.bilan, "send", lambda s, d: sent.append(1) or {"sent": True})
    out = commands.handle_bilan(argparse.Namespace(bilan_command="semaine", send=True), SimpleNamespace())
    assert out["command"] == "bilan.semaine" and out["data"]["sent"] is True and sent
