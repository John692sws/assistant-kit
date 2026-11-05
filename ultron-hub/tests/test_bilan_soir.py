"""`bilan soir` (todo #530): only `auto:` events of the day, fusion vs clôture, sha, annulee."""
import json
import sqlite3
from datetime import datetime, timezone

from ultron_hub.cap import bilan_soir
from types import SimpleNamespace
from ultron_hub.dashboard import apercu, catalogue
from ultron_hub.todo.store import TodoStore


def _at(day, hh):
    return datetime.fromisoformat(f"{day}T{hh}:00").astimezone().astimezone(timezone.utc).isoformat(timespec="seconds")


def test_bilan_soir(tmp_path):
    db = tmp_path / "t.db"
    with TodoStore(db) as s:
        a, b, c = (s.add(f"Todo {i}", "perso").id for i in "abc")
    conn = sqlite3.connect(db)
    rows = [(a, _at("2026-01-04", "10:15"), "livree", {"by": "auto:orchestrateur", "note": "fusion automatique 0123456789ab"}),
            (b, _at("2026-01-04", "18:40"), "status", {"from": "todo", "to": "done", "by": "auto:verifie"}),
            (b, _at("2026-01-04", "19:00"), "status", {"from": "todo", "to": "done", "by": "dashboard:x"}),
            (c, _at("2026-01-04", "11:00"), "livree", {"by": "cli", "note": "x"}),
            (c, _at("2026-01-05", "11:00"), "livree", {"by": "auto:orchestrateur", "note": "fusion automatique aaaaaaaaaaaa"}),
            (a, _at("2026-01-04", "20:00"), "fusion-annulee", {"by": "dashboard:x"})]
    for t, at, act, d in rows:
        conn.execute("INSERT INTO events (task_id, at, action, details) VALUES (?, ?, ?, ?)", (t, at, act, json.dumps(d)))
    conn.commit()
    conn.close()
    out = bilan_soir.run(SimpleNamespace(todo_db=db), "2026-01-04")
    assert (out["total"], out["fusions"], out["clotures"]) == (2, 1, 1)
    f, k = out["lignes"]
    assert (f["sorte"], f["sha"], f["heure"], f["todo_id"], f["annulee"]) == ("fusion", "0123456789ab", "10:15", a, True)
    assert (k["sorte"], k["sha"], k["annulee"]) == ("clôture", None, False)
    assert all("id_evenement" in x for x in out["lignes"])
    assert bilan_soir.run(SimpleNamespace(todo_db=db), "2026-01-06")["total"] == 0


def test_bilan_soir_catalogue_and_example():
    act = catalogue.PAR_ID["bilan-soir"]
    assert act.type == "lecture" and act.argv == ("bilan", "soir")
    assert catalogue.projeter(act.donnees, apercu.LECTURES["bilan-soir"])["lignes"][0]["id_evenement"] == 9001


def test_gestes_evites_card_equals_command(tmp_path):  # todo #532
    import json as _j
    from ultron_hub.cap import commands
    from ultron_hub.dashboard import server
    db = tmp_path / "g.db"
    with TodoStore(db) as s:
        a, b = (s.add(f"Todo {i}", "perso").id for i in "ab")
    conn = sqlite3.connect(db)
    for t, act, d in ((a, "livree", {"by": "auto:orchestrateur", "note": "fusion automatique 0123456789ab"}),
                      (b, "status", {"from": "todo", "to": "done", "by": "auto:verifie"}),
                      (a, "fusion-annulee", {"by": "dashboard:x"})):
        conn.execute("INSERT INTO events (task_id, at, action, details) VALUES (?, ?, ?, ?)", (t, _at("2026-01-04", "10:15"), act, _j.dumps(d)))
    conn.commit()
    conn.close()
    cfg = SimpleNamespace(todo_db=db)
    data = bilan_soir.run(cfg, "2026-01-04")
    assert (data["faits"], data["gestes_evites"]) == (2, 1)  # the undone merge is not an avoided gesture
    out = commands.handle_bilan(SimpleNamespace(bilan_command="soir", date="2026-01-04"), cfg)
    assert (out["data"]["gestes_evites"], out["data"]["faits"]) == (1, 2) and "1 gestes évités, 2 faits" in out["summary"]
    shown = catalogue.projeter(catalogue.PAR_ID["bilan-soir"].donnees, data)  # what the page receives
    assert (shown["gestes_evites"], shown["faits"]) == (data["gestes_evites"], data["faits"])
    apercu_d = catalogue.projeter(catalogue.PAR_ID["bilan-soir"].donnees, apercu.LECTURES["bilan-soir"])
    assert apercu_d["faits"] == len(apercu_d["lignes"]) and apercu_d["gestes_evites"] == 3
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert 'd.gestes_evites + " gestes évités, " + d.faits + " faits"' in js  # the card shows exactly these two fields
