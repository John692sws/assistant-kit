"""Todo #417: 📋 Rapports du dimanche, cap review + proposed tidy-up + health, the advised actions on ONE signed ✅."""

import json
import time

import pytest

from hermes_plugin import close_key
from test_dashboard_actions import confirm, dash, fake_cli, prepare, session  # noqa: F401  (fixtures)
from ultron_hub import cli, rapports
from ultron_hub.dashboard import apercu, catalogue
from ultron_hub.groupe import chantiers
from ultron_hub.todo import closing

DATE = "2026-10-25"


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))


@pytest.fixture
def hub(project, prompts_dir, capsys, monkeypatch):
    monkeypatch.setattr(chantiers, "fermer", lambda settings, sujet: {"status": "skipped"})
    prompts_dir.mkdir()
    for name in ("vieux", "fba", "ultron"):
        (prompts_dir / f"{name}.md").write_text(f"# Chantier « {name} »\n")
    write(project / "var" / "cap" / "last.json", {
        "date": DATE, "status": "ok", "summary": "Semaine d'exemple.", "cost_usd": 0.3, "semaine_pct": 6,
        "chantiers": [{"nom": "fba", "verdict": "orange", "objectif": "o", "technique": "t", "derives": "d", "preuves": []},
                      {"nom": "ultron", "verdict": "vert", "objectif": "o", "technique": "t", "derives": "d", "preuves": []}],
        "corrections": [{"titre": "Mettre en attente les todos FBA", "chantier": "fba", "notes": "n1", "preuve": "p1"},
                        {"titre": "Classer les todos d'Ultron", "chantier": "ultron", "notes": "n2", "preuve": "p2"}]})
    write(project / "var" / "health" / "reflexes.json", {"updated_at": "2026-10-26T08:00:00+00:00", "diagnostics": [
        {"nom": "verrou-restic", "diagnostic": "Verrou périmé", "detail": "9 h", "action": "x",
         "bouton": {"label": "🔓", "cible": "verrou-restic:0123456789ab"}}]})
    write(project / "var" / "health" / "alertes.json", {"updated_at": "2026-10-26T08:00:00+00:00",
                                                        "actives": {"disque": "Disque à 91 %", "reflexe:verrou-restic": "x"}})
    code, out = run_cli(capsys, "todo", "add", "--title", "Vieille idée", "--clair", "")
    assert code == 0, out
    return project


def dimanche(capsys):
    code, out = run_cli(capsys, "rapports", "dimanche")
    assert code == 0, out
    return out["data"]


def appliquer(capsys, monkeypatch, emp, token_emp=None):
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("rapports-lot", token_emp or emp))
    return run_cli(capsys, "rapports", "appliquer", "--empreinte", emp)


def cap_todos(capsys):
    tasks = run_cli(capsys, "todo", "list", "--status", "all")[1]["data"]["tasks"]
    return [(t["title"], t["chantier"]) for t in tasks if t["source"] == f"cap:{DATE}"]


def test_dimanche_lists_the_three_reports_and_the_exact_actions(hub, capsys):
    data = dimanche(capsys)
    assert data["cap"]["date"] == DATE and len(data["cap"]["corrections"]) == 2
    assert [a["type"] for a in data["actions"]] == ["todo", "todo"]  # no proposal: no tidy-up action
    assert data["menage"]["empreinte"] is None and data["menage"]["resume"] == "aucun ménage proposé"
    assert data["sante"]["alertes"] == [{"cle": "disque", "texte": "Disque à 91 %"}]
    assert data["sante"]["pannes"][0]["cible"] == "verrou-restic:0123456789ab"  # listed, never an action of the lot
    assert len(data["empreinte"]) == 16 and data["empreinte"] == rapports.empreinte(data["actions"])
    run_cli(capsys, "menage", "proposer", "--cancel", "1:remplacée")
    data2 = dimanche(capsys)
    assert data2["actions"][-1]["type"] == "menage" and data2["actions"][-1]["empreinte"] == data2["menage"]["empreinte"]
    assert data2["empreinte"] != data["empreinte"]


def test_a_correction_already_made_into_a_todo_is_not_proposed_again(hub, capsys):
    # made from Telegram's /cap: title cut at 40, source cap:<date>
    run_cli(capsys, "todo", "add", "--title", "Classer les todos d'Ultron"[:40], "--source", f"cap:{DATE}", "--clair", "")
    run_cli(capsys, "todo", "add", "--title", "Mettre en attente les todos FBA", "--source", "cap:2026-10-18", "--clair", "")
    data = dimanche(capsys)
    assert [a["titre"] for a in data["actions"]] == ["Mettre en attente les todos FBA"]  # other date: still to create
    assert [c["todo"] is not None for c in data["cap"]["corrections"]] == [False, True]


def test_one_signature_creates_the_todos_and_applies_the_tidy_up(hub, capsys, monkeypatch):
    run_cli(capsys, "menage", "proposer", "--cancel", "1:remplacée", "--archiver", "vieux")
    emp = dimanche(capsys)["empreinte"]
    code, out = run_cli(capsys, "rapports", "appliquer", "--empreinte", emp)  # no token
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "rapports appliquer" in out["error"]
    code, out = appliquer(capsys, monkeypatch, emp)
    assert code == 0, out
    assert [b["ok"] for b in out["data"]["bilan"]] == [True, True, True] and "3/3" in out["data"]["text"]
    assert cap_todos(capsys) == [("Mettre en attente les todos FBA", "fba"), ("Classer les todos d'Ultron", "ultron")]
    shown = run_cli(capsys, "todo", "show", "1")[1]["data"]
    assert shown["status"] == "cancelled"
    [event] = [h for h in shown["history"] if h["action"] == "status"]
    assert event["details"]["by"] == "telegram-button" and event["details"]["rapports"] == emp
    assert dimanche(capsys)["actions"] == []
    code, out = run_cli(capsys, "rapports", "appliquer", "--empreinte", emp)  # same token again
    assert code == 1 and "aucune action conseillée" in out["error"]


def test_a_different_fingerprint_is_refused_and_nothing_is_done(hub, capsys, monkeypatch):
    emp = dimanche(capsys)["empreinte"]
    code, out = appliquer(capsys, monkeypatch, "0123456789abcdef")
    assert code == 1 and "ont changé" in out["error"] and cap_todos(capsys) == []
    code, out = appliquer(capsys, monkeypatch, emp, token_emp="0123456789abcdef")  # token signed on another lot
    assert code == 1 and "invalide" in out["error"] and cap_todos(capsys) == []
    run_cli(capsys, "todo", "add", "--title", "Classer les todos d'Ultron", "--source", f"cap:{DATE}", "--clair", "")
    code, out = appliquer(capsys, monkeypatch, emp)  # a todo made meanwhile changes the lot
    assert code == 1 and "ont changé" in out["error"] and len(cap_todos(capsys)) == 1


def test_a_spent_proof_is_refused(hub, capsys, monkeypatch):
    emp = dimanche(capsys)["empreinte"]
    token = close_key.token("rapports-lot", emp)
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    from ultron_hub.common.settings import load_settings
    settings = load_settings()
    settings.var_dir.joinpath("rapports").mkdir(parents=True, exist_ok=True)
    proof = closing.check(token, "rapports-lot", emp)[0]
    rapports.applied_path(settings).write_text(json.dumps({"proof": proof}) + "\n")
    code, out = run_cli(capsys, "rapports", "appliquer", "--empreinte", emp)
    assert code == 1 and "déjà utilisé" in out["error"] and cap_todos(capsys) == []


def test_catalogue_signs_rapports_lot_on_the_fingerprint_and_lists_every_action():
    a = catalogue.PAR_ID["rapports-lot"]
    _, args, stdin = catalogue.valider("rapports-lot", {"empreinte": "0123456789abcdef"})
    assert a.signe == "rapports-lot" and catalogue.cible(a, args, stdin) == "0123456789abcdef" and a.detail == "rapports"
    assert catalogue.PAR_ID["rapports"].type == "lecture"
    for bad in ("", "0123", "0123456789ABCDEF", "0123456789abcdef0"):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("rapports-lot", {"empreinte": bad})
    assert "rapports-lot" in closing.DASHBOARD_ACTIONS and close_key.ACTIONS == closing.ACTIONS
    assert close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    key = b"k" * 64
    assert close_key.token("rapports-lot", "0123456789abcdef", key=key, now=1000) == \
        closing.sign(key, "rapports-lot", "0123456789abcdef", 1000)
    tok = closing.sign_dashboard(close_key.load_or_create(), "iphone", "rapports-lot", "0123456789abcdef", int(time.time()))
    assert closing.check(tok, "rapports-lot", "0123456789abcdef")[1] == "dashboard:iphone"
    with pytest.raises(closing.CloseRefused):
        closing.check(tok, "menage-lot", "0123456789abcdef")
    fake = catalogue.projeter(catalogue.PAR_ID["rapports"].donnees, apercu.LECTURES["rapports"])
    assert fake["empreinte"] and len(fake["actions"]) == 3


def test_the_sheet_lists_the_actions_and_refuses_a_changed_lot(dash, monkeypatch):
    lot = {"empreinte": "0123456789abcdef", "actions": [{"type": "todo", "texte": "➕ Todo « A »"},
                                                       {"type": "menage", "texte": "🧹 Appliquer le ménage proposé"}]}
    monkeypatch.setattr(dash.acts, "_lire_detail", lambda reading, args: lot)
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "rapports-lot", "empreinte": "0123456789abcdef"})
    assert out["ok"] and out["detail"] == ["➕ Todo « A »", "🧹 Appliquer le ménage proposé"]
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["rapports", "appliquer", "--empreinte=0123456789abcdef"]
    assert closing.check(call["env"][closing.TOKEN_ENV], "rapports-lot", "0123456789abcdef")[1].startswith("dashboard:")
    _, out, _ = prepare(dash, sid, {"action": "rapports-lot", "empreinte": "fedcba9876543210"})
    assert not out["ok"] and "a changé" in out["erreur"] and len(dash.calls()) == 1
