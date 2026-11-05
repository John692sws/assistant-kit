"""Todo #531: ↩️ of the bilan du soir = the existing catalogue actions `rouvrir` (todo reopen) and `annuler-fusion` (revert-last)."""
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from test_dashboard_actions import confirm, dash, fake_cli, prepare, session  # noqa: F401  (fixtures)
from ultron_hub.cap import bilan_soir
from ultron_hub.dashboard import server
from ultron_hub.todo import closing
from ultron_hub.todo.store import TodoStore


def test_undo_goes_through_the_catalogue_with_fake_executors(dash):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "rouvrir", "id": "7"})
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    assert dash.calls()[-1]["argv"] == ["todo", "reopen", "7"]
    assert closing.TOKEN_ENV not in dash.calls()[-1]["env"]  # reopen needs no signature
    _, out, _ = prepare(dash, sid, {"action": "annuler-fusion", "fusion": "7-0123456789ab"})
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    assert dash.calls()[-1]["argv"] == ["branch", "revert-last", "--cible=7-0123456789ab"]
    assert closing.TOKEN_ENV in dash.calls()[-1]["env"]  # the signed existing action
    sid = session(dash)
    assert prepare(dash, sid, {"action": "annuler-fusion", "fusion": "7"})[0] != 200  # a bad target is refused


def test_status_comes_back_and_the_line_turns_annulee(tmp_path):
    db = tmp_path / "t.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with TodoStore(db) as s:
        a, b = (s.add(f"Todo {i}", "perso").id for i in "ab")
        s.set_status(b, "done", {"by": "auto:verifie"})
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO events (task_id, at, action, details) VALUES (?, ?, 'livree', ?)",
                 (a, now, json.dumps({"by": "auto:orchestrateur", "note": "fusion automatique 0123456789ab"})))
    conn.commit()
    conn.close()
    cfg = SimpleNamespace(todo_db=db)
    k, f = bilan_soir.run(cfg)["lignes"]
    assert (f["sorte"], f["annulee"], k["sorte"], k["annulee"]) == ("fusion", False, "clôture", False)
    with TodoStore(db) as s:  # what `todo reopen` and `branch revert-last` write
        assert s.set_status(b, "todo", {"by": "dashboard:x"}).status == "todo"
        s.mark_merge_reverted(a, {"by": "dashboard:x"})
    assert all(x["annulee"] for x in bilan_soir.run(cfg)["lignes"])


def test_card_wiring_in_the_page():
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    for needle in ('form("rouvrir", { id: String(x.todo_id) })', 'form("annuler-fusion", { fusion: cible })',
                   '["fusion-auto",', "ce n'est plus la dernière fusion automatique"):
        assert needle in js, needle
    assert "bientôt (todo #531)" not in js
