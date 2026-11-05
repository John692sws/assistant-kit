"""Todo #249: profil, focus, revue de cap, souvenirs and the fiches of the knowledge notebook from the dashboard.

Each function is one entry of the catalogue (walked by test_dashboard_catalogue.py); here: the two new signed actions
(`profil` on the proposal's number, `savoir` on the draft's fingerprint) with the real CLI, their refusals (no token, wrong
device, replay, another target, draft changed), the readings' whitelist and the argv of the new entries.
"""

import json
import time
from pathlib import Path

import pytest

from hermes_plugin import close_key
from test_dashboard_actions import confirm, dash, fake_cli, fresh_cache, prepare, request, session  # noqa: F401
from test_esprit import mind, run  # noqa: F401  (fixture)
from test_savoir import draft  # noqa: F401  (fixture)
from ultron_hub.dashboard import actions, apercu, catalogue
from ultron_hub.esprit import savoir, store
from ultron_hub.todo import closing

ROOT = Path(__file__).parents[1]
DEVICE, OTHER = "telephone-test", "appareil-test"
VIDEO = "20261019T120000Z-0a1b2c3d"


def token(action, target, device=DEVICE, ts=None):
    close_key.load_or_create()  # throwaway key (conftest close_key_path)
    return closing.sign_dashboard(closing.load_key(), device, action, target, int(time.time() if ts is None else ts))


# -- closing: profil and savoir join the list, nothing else ---------------------------------------------------------------

def test_profil_and_savoir_are_signable_from_the_dashboard_on_both_sides():
    assert closing.DASHBOARD_ACTIONS[-12:-10] == ("profil", "savoir")  # then `reflexe` (todo #247), `revert-fusion` (#254), `compte` (#263), `session` (#265), `menage-lot` (#362), `rapports-lot` (#417), `habitude` (#429), `video-lot` (#442), `autorisation` (#495), `routine` (#503)
    assert close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    key = b"k" * 64
    assert close_key.dashboard_token(DEVICE, "savoir", "0123456789abcdef01234567", key=key, now=1000) == \
        closing.sign_dashboard(key, DEVICE, "savoir", "0123456789abcdef01234567", 1000)
    signed = {a.id: (a.signe, a.cible) for a in catalogue.CATALOGUE if a.signe in ("profil", "savoir")}
    assert signed == {"profil-accepter": ("profil", "{id}"), "savoir-ranger": ("savoir", "{empreinte}")}


# -- ✅ profil accept, signed on the number ------------------------------------------------------------------------------

def propose(capsys, text="Préfère le thé"):
    code, out = run(capsys, "profil", "propose", text, "--source", "agent:hermes", "--no-notify")
    assert code == 0
    return out["data"]["id"]


def test_profil_accept_with_the_dashboard_proof(mind, capsys, monkeypatch, esprit_dir):  # noqa: F811
    pid = propose(capsys)
    code, out = run(capsys, "profil", "accept", str(pid))  # no token: refused
    assert code == 1 and "réservé à l'utilisateur" in out["error"]
    for bad in (token("profil", pid + 1), token("done", pid), token("profil", pid, ts=time.time() - 3600),
                token("profil", pid).replace(DEVICE, OTHER, 1)):  # another proposal, action, too old, another device
        monkeypatch.setenv(closing.TOKEN_ENV, bad)
        assert run(capsys, "profil", "accept", str(pid))[0] == 1
    good = token("profil", pid)
    monkeypatch.setenv(closing.TOKEN_ENV, good)
    code, out = run(capsys, "profil", "accept", str(pid), "--text=Préfère le thé à la menthe")
    assert code == 0 and out["data"]["by"] == "dashboard:" + DEVICE
    assert "- Préfère le thé à la menthe (" in (esprit_dir / "profil.md").read_text()
    pid2 = propose(capsys, "Travaille le soir")
    assert store.proof_used(mind, good[-64:][:16])
    code, out = run(capsys, "profil", "accept", str(pid2))  # the same token on another proposal: refused
    assert code == 1


def test_profil_proof_is_not_replayed(mind, capsys, monkeypatch):  # noqa: F811
    pid = propose(capsys)
    monkeypatch.setenv(closing.TOKEN_ENV, token("profil", pid))
    by = closing.authorize("profil", pid, human=False, what="", used=lambda p: store.proof_used(mind, p))
    store.decide(mind, pid, True, by)
    with pytest.raises(closing.CloseRefused, match="déjà utilisé"):
        closing.authorize("profil", pid, human=False, what="", used=lambda p: store.proof_used(mind, p))


def test_reject_needs_no_proof_and_is_simple(mind, capsys):  # noqa: F811
    pid = propose(capsys)
    a = catalogue.PAR_ID["profil-rejeter"]
    _, args, stdin = catalogue.valider("profil-rejeter", {"id": str(pid)})
    assert a.type == "simple" and catalogue.commande(a, args, stdin) == ["profil", "reject", str(pid)]
    assert run(capsys, *catalogue.commande(a, args, stdin))[0] == 0


def test_corrected_text_is_optional_and_never_a_bare_argument():
    a = catalogue.PAR_ID["profil-accepter"]
    _, args, stdin = catalogue.valider("profil-accepter", {"id": "3"})
    assert catalogue.commande(a, args, stdin) == ["profil", "accept", "3"]
    _, args, stdin = catalogue.valider("profil-accepter", {"id": "3", "texte": "--help  et   le reste"})
    assert catalogue.commande(a, args, stdin) == ["profil", "accept", "3", "--text=--help et le reste"]
    with pytest.raises(catalogue.Refused):
        catalogue.valider("profil-accepter", {"id": "3", "texte": "x" * (store.FACT_MAX + 1)})


# -- 📚 savoir ranger, signed on the draft's fingerprint ---------------------------------------------------------------

def video_draft(project, draft):  # noqa: F811
    """A fiche draft where `video actions` writes it: var/video/<id>/actions/1.md of the project."""
    src = draft()
    path = project / "var" / "video" / VIDEO / "actions" / "1.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(src.read_bytes())
    return path


def ranger_argv():
    a = catalogue.PAR_ID["savoir-ranger"]
    _, args, stdin = catalogue.valider("savoir-ranger", {"source": "video", "id": VIDEO, "n": "1",
                                                         "empreinte": "0123456789abcdef01234567"})
    return catalogue.commande(a, args, stdin)


def test_savoir_ranger_with_the_dashboard_proof(project, draft, capsys, monkeypatch, savoir_dir):  # noqa: F811
    path = video_draft(project, draft)
    monkeypatch.chdir(project)  # the dashboard runs the CLI from the project root
    argv = ranger_argv()
    assert argv == ["savoir", "ranger", f"var/video/{VIDEO}/actions/1.md"]
    code, out = run(capsys, *argv)
    assert code == 1 and "réservé à l'utilisateur" in out["error"]
    target = savoir.fingerprint(path.read_bytes())
    for bad in (token("savoir", "f" * 24), token("profil", target), token("savoir", target).replace(DEVICE, OTHER, 1)):
        monkeypatch.setenv(closing.TOKEN_ENV, bad)
        assert run(capsys, *argv)[0] == 1
    good = token("savoir", target)
    monkeypatch.setenv(closing.TOKEN_ENV, good)
    code, out = run(capsys, *argv)
    assert code == 0 and out["data"]["by"] == "dashboard:" + DEVICE
    code, out = run(capsys, *argv)  # replay
    assert code == 1 and "déjà utilisé" in out["error"]
    assert len(list((savoir_dir / "fiches").glob("*.md"))) == 1


def test_a_draft_changed_after_the_check_is_refused(project, draft, capsys, monkeypatch, savoir_dir):  # noqa: F811
    path = video_draft(project, draft)
    monkeypatch.chdir(project)
    monkeypatch.setenv(closing.TOKEN_ENV, token("savoir", savoir.fingerprint(path.read_bytes())))
    path.write_text(path.read_text().replace("30 %", "10 %"))
    code, out = run(capsys, *ranger_argv())
    assert code == 1 and "jeton" in out["error"]
    assert not list((savoir_dir / "fiches").glob("*.md"))


def test_verifier_writes_nothing_and_gives_the_fingerprint(project, draft, capsys, monkeypatch, savoir_dir):  # noqa: F811
    path = video_draft(project, draft)
    monkeypatch.chdir(project)
    a = catalogue.PAR_ID["savoir-verifier"]
    _, args, stdin = catalogue.valider("savoir-verifier", {"source": "video", "id": VIDEO, "n": "1"})
    code, out = run(capsys, *catalogue.commande(a, args, stdin))
    assert code == 0 and out["data"]["empreinte"] == savoir.fingerprint(path.read_bytes())
    assert catalogue.FICHE_RE.fullmatch(out["data"]["empreinte"])
    assert a.type == "simple" and set(a.sortie) == {"texte", "empreinte"} and not (savoir_dir / "fiches").exists()


@pytest.mark.parametrize("body", [{"source": "../x"}, {"source": "fiches"}, {"id": "../../etc"}, {"n": "0"}, {"n": "1/../2"},
                                  {"empreinte": "0123456789abcdef"}, {"empreinte": "../" + "0" * 21}])
def test_a_draft_is_named_never_typed(body):
    base = {"source": "docs", "id": VIDEO, "n": "2", "empreinte": "0123456789abcdef01234567"}
    with pytest.raises(catalogue.Refused):
        catalogue.valider("savoir-ranger", {**base, **body})


# -- through the server: wrong device, no token, spent token -----------------------------------------------------------

@pytest.mark.parametrize("action_id,body", [("profil-accepter", {"id": "3"}),
                                            ("savoir-ranger", {"source": "video", "id": VIDEO, "n": "1",
                                                               "empreinte": "0123456789abcdef01234567"})])
def test_signed_sheet_refused_from_another_device_and_spent_once(dash, action_id, body):
    from test_dashboard_actions import MAC_ID, STRANGER_ID
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": action_id, **body})
    for node in (MAC_ID, STRANGER_ID, None):  # another allowed device (its cookie is not this one's), a stranger, no whois
        dash.node = node
        assert confirm(dash, sid, out["jeton"])[0] == 403
    dash.node = "nPHONE"
    assert confirm(dash, sid, "inconnu")[0] == 409  # no sheet token: nothing signed
    assert dash.calls() == []
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 200 and res["ok"]
    assert confirm(dash, sid, out["jeton"])[0] == 409  # replayed sheet token
    [call] = dash.calls()
    a = catalogue.PAR_ID[action_id]
    _, args, stdin = catalogue.valider(action_id, body)
    assert closing.check(call["env"][closing.TOKEN_ENV], a.signe, catalogue.cible(a, args, stdin))[1] == "dashboard:" + DEVICE


# -- readings and the other entries --------------------------------------------------------------------------------------

def test_readings_are_the_telegram_commands():
    cmd = {i: catalogue.commande(catalogue.PAR_ID[i], *catalogue.valider(i, {"mots": "  facture   fournisseur "})[1:])
           for i in ("profil", "focus", "cap", "souvenirs")}
    assert cmd == {"profil": ["profil", "show"], "focus": ["esprit", "focus"], "cap": ["cap", "last"],
                   "souvenirs": ["souvenirs", "chercher", "--texte=facture fournisseur"]}
    assert all(catalogue.PAR_ID[i].type == "lecture" for i in cmd)


def test_souvenirs_takes_the_words_as_one_text(project, capsys, monkeypatch):
    from ultron_hub.souvenirs import index
    seen = []
    monkeypatch.setattr(index, "age_s", lambda db: 1)
    monkeypatch.setattr(index, "search", lambda db, words: seen.append(words) or [])
    assert run(capsys, "souvenirs", "chercher", "--texte=facture  fournisseur")[0] == 0
    assert run(capsys, "souvenirs", "chercher", "--", "a", "b")[0] == 0
    assert seen == [["facture", "fournisseur"], ["a", "b"]]
    code, out = run(capsys, "souvenirs", "chercher", "--texte=   ")
    assert code == 1 and "au moins un mot" in out["error"]


def test_focus_choices_are_one_or_two_domains_in_order():
    codes = [c.code for c in catalogue.FOCUS_CHOIX]
    assert codes == ["systeme", "business", "perso", "systeme+business", "systeme+perso", "business+perso"]
    a = catalogue.PAR_ID["focus-choisir"]
    _, args, stdin = catalogue.valider("focus-choisir", {"domaines": "business+perso"})
    assert catalogue.commande(a, args, stdin) == ["esprit", "focus", "--set", "business", "perso", "--by", "dashboard"]
    for bad in ("systeme+business+perso", "business+systeme", "--help"):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("focus-choisir", {"domaines": bad})


def test_readings_whitelist_drops_what_the_page_does_not_need(dash, monkeypatch):
    def run_(self, argv, extra, **kw):
        return {"status": "ok", "summary": "lu", "data": {**apercu.LECTURES["profil"], "chemin": "/home/exemple2/profil.md",
                                                          "pending": [{"id": 1, "texte": "t", "secret": "jamais"}]}}
    monkeypatch.setattr(actions.Actions, "_run", run_)
    sid = session(dash)
    code, out, _ = request(dash, "POST", "/api/action/lire", {"action": "profil"}, cookie=sid)
    assert code == 200 and out["ok"] and out["donnees"]["pending"] == [{"id": 1, "texte": "t"}]
    assert "jamais" not in json.dumps(out) and "/home/exemple2" not in json.dumps(out) and dash.journal == []


def test_actions_give_the_proposals_to_the_page(dash, monkeypatch):
    data = {"id": VIDEO, "title": "t", "reponse": "compris", "cost_usd": 0.1,
            "actions": [{"n": 1, "type": "fiche", "titre": "F", "resume": "r", "path": "/home/exemple2/1.md", "empreinte": "e"}]}
    monkeypatch.setattr(actions.Actions, "_run", lambda self, argv, extra, **kw: {"status": "ok", "summary": "1", "data": data})
    monkeypatch.setattr(actions.envoi, "envoyer", lambda texte, **kw: None)  # video-analyse is delivered (todo #331)
    sid = session(dash)
    _, prep, _ = prepare(dash, sid, {"action": "video-analyse", "id": VIDEO})
    code, res, _ = confirm(dash, sid, prep["jeton"])
    assert code == 200 and res["donnees"] == {"id": VIDEO, "title": "t", "reponse": "compris",
                                              "actions": [{"n": 1, "type": "fiche", "titre": "F", "resume": "r"}]}


def test_restart_after_merge_is_declared_for_249():
    plan = json.loads((ROOT / "deploy/user/apres-fusion/todo-249.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]
