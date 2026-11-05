"""Todo #442: every pending proposal of a video (fiches, todos, fait) filed on ONE signed ✅ (video/lot.py)."""

import json
import time

import pytest

from hermes_plugin import close_key
from test_dashboard_actions import confirm, dash, fake_cli, prepare, session  # noqa: F401  (fixtures)
from ultron_hub import cli, proposals
from ultron_hub.common.settings import load_settings
from ultron_hub.dashboard import catalogue
from ultron_hub.esprit import savoir
from ultron_hub.esprit import store as esprit
from ultron_hub.todo import closing
from ultron_hub.video import actions as vactions
from ultron_hub.video import collect as collector
from ultron_hub.video import lot

VID = "20261019T120000Z-0a1b2c3d"
RAW = [{"type": "fiche", "titre": "Découper en scènes courtes", "resume": "La méthode du montage", "corps": "Trois scènes.",
        "projet": "ultron", "tags": ["video"]},
       {"type": "todo", "titre": "Essayer le découpage en scènes", "resume": "Pour le montage", "corps": "Le faire."},
       {"type": "fiche", "titre": "Le son avant l'image", "resume": "Mixer d'abord", "corps": "Le son guide.",
        "projet": "ultron", "tags": []},
       {"type": "profil", "corps": "Aime les vidéos courtes"}]


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture
def video(project, capsys):
    (project / "deploy" / "esprit").mkdir(parents=True)
    (project / "deploy" / "esprit" / "charte.md").write_text("# Charte\nTu es Ultron.\n")
    (project / "profil-initial.md").write_text("# Profil\n- Parle français.\n")
    assert run_cli(capsys, "esprit", "init", "--profil-from", str(project / "profil-initial.md"))[0] == 0
    settings = load_settings()
    analysis = {"title": "Monter vite", "author": "Auteur", "url": "https://www.youtube.com/watch?v=abcdefghijk",
                "timeline": [{"start": 0}]}
    folder = collector.video_dir(VID)
    folder.mkdir(parents=True)
    items = proposals.build_actions(settings, vactions.video_source(VID, analysis), RAW, "Auteur", "garde ça")
    assert [a["type"] for a in items] == ["fiche", "todo", "fiche", "profil"]
    proposals.write_private(folder / proposals.ACTIONS_JSON, json.dumps({"reponse": "", "actions": items}))
    return settings


def voir(capsys):
    code, out = run_cli(capsys, "video", "lot", VID)
    assert code == 0, out
    return out["data"]


def ranger(capsys, monkeypatch, emp, token_target=None):
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("video-lot", token_target or f"{VID}:{emp}"))
    return run_cli(capsys, "video", "ranger-lot", VID, "--empreinte", emp)


def video_todos(capsys):
    tasks = run_cli(capsys, "todo", "list", "--status", "all")[1]["data"]["tasks"]
    return [t["title"] for t in tasks if t["source"] == f"video:{VID}"]


def test_the_lot_is_filed_on_one_signed_press_then_nothing_is_pending(video, capsys, monkeypatch):
    data = voir(capsys)
    assert [a["type"] for a in data["actions"]] == ["fiche", "todo", "fiche", "profil"] and data["empreinte"]
    code, out = ranger(capsys, monkeypatch, data["empreinte"])
    assert code == 0, out
    assert [b["ok"] for b in out["data"]["bilan"]] == [True] * 4 and out["summary"] == "📥 Propositions rangées : 4/4"
    assert video_todos(capsys) == ["Essayer le découpage en scènes"]
    rows = savoir.ledger(video)
    assert len(rows) == 2 and all(r["via"] == "video-lot" and r["lot_proof"] and "proof" not in r for r in rows)
    [fait] = [r for r in esprit.proposals(video) if r["source"] == f"video:{VID.lower()}"]
    assert fait["texte"] == "Aime les vidéos courtes"
    after = voir(capsys)
    assert after["actions"] == [] and after["empreinte"] is None and len(after["faites"]) == 4


def test_a_replayed_press_is_refused_and_files_nothing_twice(video, capsys, monkeypatch):
    emp = voir(capsys)["empreinte"]
    assert ranger(capsys, monkeypatch, emp)[0] == 0
    code, out = run_cli(capsys, "video", "ranger-lot", VID, "--empreinte", emp)  # same token, same lot
    assert code == 1 and "aucune proposition en attente" in out["error"]
    assert video_todos(capsys) == ["Essayer le découpage en scènes"] and len(savoir.ledger(video)) == 2


def test_a_spent_proof_is_refused(video, capsys, monkeypatch):
    emp = voir(capsys)["empreinte"]
    token = close_key.token("video-lot", f"{VID}:{emp}")
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    proof = closing.check(token, "video-lot", f"{VID}:{emp}")[0]
    lot.ledger_path(video).parent.mkdir(parents=True, exist_ok=True)
    lot.ledger_path(video).write_text(json.dumps({"proof": proof}) + "\n")
    code, out = run_cli(capsys, "video", "ranger-lot", VID, "--empreinte", emp)
    assert code == 1 and "déjà utilisé" in out["error"] and video_todos(capsys) == []


def test_a_changed_lot_or_a_wrong_token_or_no_token_is_refused(video, capsys, monkeypatch):
    emp = voir(capsys)["empreinte"]
    code, out = run_cli(capsys, "video", "ranger-lot", VID, "--empreinte", emp)  # no token, no --i-am-human
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and "ranger-lot" in out["error"]
    code, out = ranger(capsys, monkeypatch, "0123456789abcdef")  # fingerprint not the lot's
    assert code == 1 and "ont changé" in out["error"]
    code, out = ranger(capsys, monkeypatch, emp, token_target=f"{VID}:0123456789abcdef")  # signed on another lot
    assert code == 1 and "invalide" in out["error"]
    assert video_todos(capsys) == [] and savoir.ledger(video) == []
    run_cli(capsys, "todo", "add", "--title", "Essayer le découpage en scènes", "--source", f"video:{VID}", "--clair", "")
    data = voir(capsys)  # the unit ✅ of the todo meanwhile: out of the lot, another fingerprint
    assert data["empreinte"] != emp and [a["type"] for a in data["actions"]] == ["fiche", "fiche", "profil"]
    code, out = ranger(capsys, monkeypatch, emp)
    assert code == 1 and "ont changé" in out["error"] and savoir.ledger(video) == []


def test_a_changed_draft_changes_the_fingerprint(video, capsys, monkeypatch):
    emp = voir(capsys)["empreinte"]
    draft = collector.video_dir(VID) / proposals.ACTIONS_DIR / "1.md"
    draft.write_text(draft.read_text().replace("Trois scènes.", "Quatre scènes."))
    assert voir(capsys)["empreinte"] != emp
    assert ranger(capsys, monkeypatch, emp)[0] == 1 and savoir.ledger(video) == []


def test_no_proposal_and_bad_id_are_refused(project, capsys):
    code, out = run_cli(capsys, "video", "lot", VID)
    assert code == 1 and "aucune proposition" in out["error"]
    code, out = run_cli(capsys, "video", "ranger-lot", "../x", "--empreinte", "0123456789abcdef")
    assert code == 1 and "invalide" in out["error"]


def test_catalogue_signs_video_lot_on_id_and_fingerprint_and_plugin_signs_alike():
    a = catalogue.PAR_ID["video-lot"]
    _, args, stdin = catalogue.valider("video-lot", {"id": VID, "empreinte": "0123456789abcdef"})
    assert a.signe == "video-lot" and catalogue.cible(a, args, stdin) == f"{VID}:0123456789abcdef"
    assert a.detail == "video-lot-voir" and catalogue.PAR_ID["video-lot-voir"].type == "lecture"
    for bad in ("", "0123", "0123456789ABCDEF"):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("video-lot", {"id": VID, "empreinte": bad})
    with pytest.raises(catalogue.Refused):
        catalogue.valider("video-lot", {"id": "../etc", "empreinte": "0123456789abcdef"})
    assert "video-lot" in closing.DASHBOARD_ACTIONS and close_key.ACTIONS == closing.ACTIONS
    assert close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    key, target = b"k" * 64, f"{VID}:0123456789abcdef"
    assert close_key.token("video-lot", target, key=key, now=1000) == closing.sign(key, "video-lot", target, 1000)
    tok = closing.sign_dashboard(close_key.load_or_create(), "iphone", "video-lot", target, int(time.time()))
    assert closing.check(tok, "video-lot", target)[1] == "dashboard:iphone"
    with pytest.raises(closing.CloseRefused):
        closing.check(tok, "rapports-lot", target)


def test_the_sheet_lists_the_proposals_and_refuses_a_changed_lot(dash, monkeypatch):
    view = {"id": VID, "empreinte": "0123456789abcdef",
            "actions": [{"type": "fiche", "texte": "📚 Fiche 1. A"}, {"type": "todo", "texte": "➕ Todo 2. B"}]}
    monkeypatch.setattr(dash.acts, "_lire_detail", lambda reading, args: view)
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "video-lot", "id": VID, "empreinte": "0123456789abcdef"})
    assert out["ok"] and out["detail"] == ["📚 Fiche 1. A", "➕ Todo 2. B"]
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["video", "ranger-lot", VID, "--empreinte=0123456789abcdef"]
    assert closing.check(call["env"][closing.TOKEN_ENV], "video-lot", f"{VID}:0123456789abcdef")[1].startswith("dashboard:")
    _, out, _ = prepare(dash, sid, {"action": "video-lot", "id": VID, "empreinte": "fedcba9876543210"})
    assert not out["ok"] and "a changé" in out["erreur"] and len(dash.calls()) == 1
