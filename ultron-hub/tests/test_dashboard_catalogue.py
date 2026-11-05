"""The one catalogue of the dashboard's actions (todo #251): every entry is checked here without adding a test for it.

Walks the WHOLE catalogue: values out of limits refused, sheet present and well formed, argv a list run without shell and
accepted by the real CLI parser, signed only for `signee` (with an action of closing.DASHBOARD_ACTIONS), unknown action
refused; then each entry end to end through the server with a fake `ultron-hub`.
"""

import dataclasses
import json
import re
from pathlib import Path

import pytest

from cli_contract import check_argv
from test_dashboard_actions import confirm, dash, fake_cli, fresh_cache, prepare, request, session  # noqa: F401
from ultron_hub import envoi
from ultron_hub.dashboard import actions, catalogue, server
from ultron_hub.montage import plan
from ultron_hub.todo import closing

ROOT = Path(__file__).parents[1]
ACTIONS = [a.id for a in catalogue.CATALOGUE]


def exemple(action, **over):
    return {**{c.nom: c.exemple for c in action.champs}, **over}


@pytest.fixture(autouse=True)
def delivered(monkeypatch):
    """Dashboard deliveries (`Actions._livrer` -> envoi.envoyer) are recorded, never sent (todo #393)."""
    sent: list[str] = []
    monkeypatch.setattr(envoi, "envoyer", lambda texte, **kw: sent.append(texte) or envoi.Resultat(envoyes=1))
    return sent


def test_catalogue_is_well_formed_and_holds_the_existing_actions():
    catalogue.verifier()
    assert {"done", "merge", "todo", "chantier-archiver", "chantier-rouvrir"} <= set(ACTIONS)
    assert len(set(ACTIONS)) == len(ACTIONS)


@pytest.mark.parametrize("action_id", ACTIONS)
def test_each_entry_gives_a_sheet_and_an_argv_without_shell(action_id):
    a = catalogue.PAR_ID[action_id]
    act, args, stdin = catalogue.valider(action_id, exemple(a))
    assert act is a
    argv = catalogue.commande(a, args, stdin)
    assert isinstance(a.argv, tuple) and isinstance(argv, list) and all(isinstance(x, str) and x for x in argv)
    check_argv(argv)  # the real `ultron-hub` parser accepts it
    sheet = catalogue.feuille(a, args, stdin)
    assert sheet["quoi"] and sheet["sur_quoi"] and sheet["effet"] and sheet["confirmer"]
    assert sheet["risque"] in ("🟢 faible", "🔴 élevé")  # the words of ui.approval_card
    assert catalogue.ligne_journal(a, args, stdin)
    for c in a.champs:  # a free text never sits alone in argv (it could start with « - ») unless a regex checks it (todo #248)
        if c.type == "texte" and c.regex is None and args[c.nom]:
            assert args[c.nom] not in argv
    if a.type == "signee":
        assert a.signe in closing.DASHBOARD_ACTIONS
        closing.message(a.signe, catalogue.cible(a, args, stdin), 0)  # a target the proof accepts
    else:
        assert a.signe is None and not a.cible


@pytest.mark.parametrize("action_id", ACTIONS)
def test_each_field_refuses_values_out_of_limits(action_id):
    a = catalogue.PAR_ID[action_id]
    for c in a.champs:
        bad = [["x"], {"x": 1}, True, "a" * (c.max + 1)]
        if c.min:
            bad += ["   "] + ([] if c.defaut else ["", None])  # a field with a default takes it when left empty
        if c.regex is not None:
            bad += ["-" + c.exemple, "--help", "../x", "a;rm -rf /", "a b", "A\n"]
        if c.choix:
            bad += ["inconnu", "d999"]
        for value in bad:
            with pytest.raises(catalogue.Refused):
                catalogue.valider(action_id, exemple(a, **{c.nom: value}))


@pytest.mark.parametrize("action_id", [None, "", "cancel", "reopen", "approve", "root", "__class__", "STOP", ["done"], 7])
def test_unknown_action_is_refused(action_id):
    with pytest.raises(catalogue.Refused, match="inconnue"):
        catalogue.valider(action_id, {"id": "7"})


def test_a_signed_entry_needs_a_closing_action_and_only_signed_entries_sign():
    done = catalogue.PAR_ID["done"]
    for bad in (dataclasses.replace(done, signe=None), dataclasses.replace(done, signe="root"),
                dataclasses.replace(done, signe="dashboard"), dataclasses.replace(done, cible=""),
                dataclasses.replace(done, type="simple"), dataclasses.replace(catalogue.PAR_ID["chantier-archiver"], signe="done"),
                dataclasses.replace(done, type="maison")):
        with pytest.raises(catalogue.CatalogueError):
            catalogue.verifier((bad,))


def test_ill_formed_entries_are_refused():
    todo, done = catalogue.PAR_ID["todo"], catalogue.PAR_ID["done"]
    for bad in (dataclasses.replace(todo, argv=("todo", "add", "{titre}")),          # free text positional
                dataclasses.replace(todo, argv="todo add --title={titre}"),           # a shell string
                dataclasses.replace(todo, argv=("{chantier}", "add")),               # no literal command first
                dataclasses.replace(done, argv=("todo", "done", "{inconnu}")),       # unknown field
                dataclasses.replace(done, effet=""), dataclasses.replace(done, risque="moyen"),
                dataclasses.replace(done, quoi=()), dataclasses.replace(done, sortie=("secret",)),
                dataclasses.replace(done, champs=(dataclasses.replace(done.champs[0], regex=None),)),
                dataclasses.replace(done, champs=(dataclasses.replace(done.champs[0], regex=re.compile(r".+")),)),
                dataclasses.replace(done, champs=(dataclasses.replace(done.champs[0], exemple="0"),)),
                (done, done)):
        with pytest.raises(catalogue.CatalogueError):
            catalogue.verifier(bad if isinstance(bad, tuple) else (bad,))


def test_page_gets_no_secret_and_knows_no_action_by_name(dash):
    pub = catalogue.public()
    text = json.dumps(pub, ensure_ascii=False)
    assert [p["id"] for p in pub] == ACTIONS
    for word in ("argv", "regex", '"signe"', "cible", "branch", "--title", "close.key", "TOKEN"):
        assert word not in text, word
    code, out, _ = request(dash, "GET", "/api/catalogue")
    assert code == 200 and out == {"actions": pub}
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    # only the sheet of `joindre` opened by itself once a file is stored (todo #293); every button is form()
    assert re.findall(r'action: "(\w+)"', js) == ["joindre"]  # `joindre-vocal` (hyphen) is the 🎙 without sheet (#379)
    assert "signé" not in js and "Fusion d'essai" not in js and "/api/catalogue" in js


@pytest.mark.parametrize("action_id", [a for a in ACTIONS if catalogue.PAR_ID[a].ouvert])
def test_each_open_entry_runs_end_to_end_without_sheet(dash, action_id):
    """Todo #283: an `ouvert` entry has no sheet (prepare refuses it) and runs through the reading POST, signed and journaled."""
    a = catalogue.PAR_ID[action_id]
    body = {"action": action_id, **exemple(a)}
    sid = session(dash)
    assert prepare(dash, sid, body)[0] == 409 and dash.calls() == []
    code, res, _ = request(dash, "POST", "/api/action/lire", body, cookie=sid)
    assert code == 200 and res["ok"], res
    [call] = dash.calls()
    _, args, stdin = catalogue.valider(action_id, body)
    assert call["argv"] == catalogue.commande(a, args, stdin) and call["stdin"] == (stdin or "")
    if a.type == "signee":
        assert closing.check(call["env"][closing.TOKEN_ENV], a.signe, catalogue.cible(a, args, stdin))[1] == "dashboard:telephone-test"
    assert dash.journal == [f"{catalogue.ligne_journal(a, args, stdin)} depuis le dashboard (by=dashboard:telephone-test)"]


def test_only_short_low_risk_entries_may_be_open():
    base = catalogue.PAR_ID["session-choisir"]
    for bad in (dataclasses.replace(base, risque="high"), dataclasses.replace(base, danger=True),
                dataclasses.replace(base, fond=True), dataclasses.replace(base, delai=catalogue.MERGE_TIMEOUT_S),
                dataclasses.replace(catalogue.PAR_ID["session"], ouvert=True)):
        with pytest.raises(catalogue.CatalogueError):
            catalogue.verifier((bad,))
    assert {a.id for a in catalogue.CATALOGUE if a.ouvert} == {"session-ecrire", "session-choisir", "notifs-lire",
                                                                     "joindre-vocal", "voix-lire"}


def test_the_long_open_exception_is_only_for_the_vocal_only_analysis():
    """Todo #379: an ordinary open entry stays short; `ouvert_long` (25 min) needs --vocal-seul, low risk, no stdin, no fond."""
    ok = catalogue.PAR_ID["joindre-vocal"]
    catalogue.verifier((ok,))
    assert ok.ouvert_long and "--vocal-seul" in ok.argv and ok.delai == catalogue.JOINDRE_TIMEOUT_S
    plain = catalogue.PAR_ID["joindre"]
    for bad in (dataclasses.replace(ok, argv=("joindre", "analyser", "{fichier}")),
                dataclasses.replace(ok, risque="high"), dataclasses.replace(ok, danger=True), dataclasses.replace(ok, ouvert=False),
                dataclasses.replace(ok, delai=catalogue.JOINDRE_TIMEOUT_S + 1),
                dataclasses.replace(plain, ouvert=True, ouvert_long=True),
                dataclasses.replace(ok, ouvert_long=False)):
        with pytest.raises(catalogue.CatalogueError):
            catalogue.verifier((bad,))


@pytest.mark.parametrize("action_id", [a for a in ACTIONS if catalogue.PAR_ID[a].type != "lecture" and not catalogue.PAR_ID[a].ouvert])
def test_each_entry_runs_end_to_end(dash, action_id, monkeypatch):
    a = catalogue.PAR_ID[action_id]
    body = {"action": action_id, **exemple(a)}
    if a.detail:  # todo #417: the reading listed on the sheet gives the same fingerprint (test_rapports checks the refusal)
        monkeypatch.setattr(dash.acts, "_lire_detail", lambda r, a: {"empreinte": body["empreinte"], "actions": [{"texte": "x"}]})
    sid = session(dash)
    code, out, _ = prepare(dash, sid, body)
    assert code == 200 and out["ok"], out
    assert {"quoi", "sur_quoi", "effet", "risque", "confirmer", "jeton", "expire_s"} <= set(out)
    assert dash.calls() == []
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 200 and res["ok"], res
    [call] = dash.calls()
    _, args, stdin = catalogue.valider(action_id, body)
    assert call["argv"] == catalogue.commande(a, args, stdin)
    assert call["stdin"] == (stdin or "")
    if a.type == "signee":
        proof, by = closing.check(call["env"][closing.TOKEN_ENV], a.signe, catalogue.cible(a, args, stdin))
        assert by == "dashboard:telephone-test"
    else:
        assert closing.TOKEN_ENV not in call["env"]
    assert dash.journal == [f"{catalogue.ligne_journal(a, args, stdin)} depuis le dashboard (by=dashboard:telephone-test)"]


def test_a_reading_entry_runs_without_sheet_and_unsigned(dash, monkeypatch):
    lire = catalogue.Action("lire", "📋 Voir…", "lecture", ("todo", "show", "{id}"), (catalogue.PAR_ID["done"].champs[0],),
                            ("📋 Voir la todo",), ("#{id}",), "rien ne change", "low", ("📋 todo #{id} lue",))
    catalogue.verifier((lire,))
    monkeypatch.setitem(catalogue.PAR_ID, "lire", lire)
    sid = session(dash)
    code, out, _ = prepare(dash, sid, {"action": "lire", "id": "7"})
    assert code == 409 and "lecture" in out["erreur"]
    code, out, _ = request(dash, "POST", "/api/action/lire", {"action": "lire", "id": "7"}, cookie=sid)
    assert code == 200 and out["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["todo", "show", "7"] and closing.TOKEN_ENV not in call["env"]
    assert request(dash, "POST", "/api/action/lire", {"action": "lire", "id": "0"}, cookie=sid)[0] == 409
    assert request(dash, "POST", "/api/action/lire", {"action": "done", "id": "7"}, cookie=sid)[0] == 409  # never skips the sheet
    assert request(dash, "POST", "/api/action/lire", {"action": "lire", "id": "7"}, cookie=sid, origin="http://evil.example")[0] == 403
    assert request(dash, "POST", "/api/action/lire", {"action": "lire", "id": "7"}, cookie=None)[0] == 403
    assert len(dash.calls()) == 1


def test_no_shell_anywhere_on_the_path():
    for mod in (actions, catalogue):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "shell=True" not in src and "os.system" not in src and "Popen(" not in src


def test_restart_after_merge_is_declared():
    plan = json.loads((ROOT / "deploy/user/apres-fusion/todo-251.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]


# -- 📥 Analyser (todo #248) ---------------------------------------------------------------------------------------------

ANALYSER = ["joindre", "video-tout", "video-duree", "video-collecte", "video-analyse", "montage-script", "montage-audio"]


def test_the_analyser_actions_are_in_the_catalogue_and_simple_except_the_duration_reading():
    assert set(ANALYSER) <= set(ACTIONS)
    assert {i: catalogue.PAR_ID[i].type for i in ANALYSER if catalogue.PAR_ID[i].type != "simple"} == {"video-duree": "lecture"}
    assert all(catalogue.PAR_ID[i].signe is None for i in ANALYSER)  # nothing signed: closing.py is untouched


@pytest.mark.parametrize("action_id", ["video-duree", "video-collecte", "video-tout"])
@pytest.mark.parametrize("lien", ["ftp://exemple.fr/a", "javascript:alert(1)", "file:///etc/passwd", "//exemple.fr/a", "https://a b",
                                  "https://exemple.fr/\nautre", " -https://exemple.fr", "https://", "https://x.fr/" + "a" * 500])
def test_a_link_from_outside_is_data_and_checked(action_id, lien):
    with pytest.raises(catalogue.Refused):
        catalogue.valider(action_id, {"lien": lien})


def test_a_good_link_goes_whole_into_one_argv_element_without_shell():
    lien = "https://www.youtube.com/watch?v=abc&t=3;rm$IFS-rf"
    _, args, stdin = catalogue.valider("video-collecte", {"lien": f"  {lien}  "})
    assert catalogue.commande(catalogue.PAR_ID["video-collecte"], args, stdin) == ["video", "collect", lien, "--phrase-stdin"]


def test_long_actions_wait_as_long_as_telegram_and_say_so():
    wait = {i: catalogue.PAR_ID[i] for i in ("video-collecte", "video-analyse")}
    assert wait["video-collecte"].delai == 17 * 60 and wait["video-analyse"].delai == catalogue.LONG_TIMEOUT_S == 45 * 60
    assert all(a.attente for a in wait.values())
    with pytest.raises(catalogue.CatalogueError):
        catalogue.verifier((dataclasses.replace(wait["video-analyse"], delai=catalogue.VIDEO_TOUT_S + 1),))


def test_outputs_duration_text_and_fingerprint_reach_the_page(dash, monkeypatch):
    answers = {"video-duree": {"duration": 754.2},
               "video-analyse": {"text": "Vue d'ensemble : …", "id": "20261019T120000Z-0a1b2c3d", "title": "T",
                                 "reponse": "Tu veux…", "path": "/x/analyse.md",
                                 "actions": [{"n": 1, "type": "fiche", "titre": "F", "resume": "r", "apercu": "secret"}]}}
    seen = {}

    def fake_run(self, argv, extra, *, stdin=None, timeout=0):
        seen["timeout"] = timeout
        return {"status": "ok", "summary": "ok", "data": answers[seen["action"]]}
    monkeypatch.setattr(actions.Actions, "_run", fake_run)
    sid = session(dash)
    seen["action"] = "video-duree"
    code, out, _ = request(dash, "POST", "/api/action/lire", {"action": "video-duree", "lien": "https://youtu.be/abcdefghijk"}, cookie=sid)
    assert code == 200 and out["ok"] and out["duree"] == 754.2
    seen["action"] = action_id = "video-analyse"
    body = {"action": action_id, **exemple(catalogue.PAR_ID[action_id])}
    _, prep, _ = prepare(dash, sid, body)
    code, res, _ = confirm(dash, sid, prep["jeton"])
    assert code == 200 and res["ok"], res
    assert res["texte"].startswith("Vue d'ensemble") and seen["timeout"] == catalogue.LONG_TIMEOUT_S
    # todo #441: the batch of proposals reaches the page through the whitelist (no draft text, no path)
    assert res["donnees"] == {"id": "20261019T120000Z-0a1b2c3d", "title": "T", "reponse": "Tu veux…",
                              "actions": [{"n": 1, "type": "fiche", "titre": "F", "resume": "r"}]}


def test_the_analysis_of_a_video_gives_its_proposals_to_the_page():  # todo #441
    for action_id in ("video-analyse", "video-tout"):
        a = catalogue.PAR_ID[action_id]
        assert "donnees" in a.sortie and a.donnees is catalogue.PROPOSITIONS


def test_restart_after_merge_is_declared_for_248():
    plan = json.loads((ROOT / "deploy/user/apres-fusion/todo-248.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]


def test_session_closing_and_new_session_entries_todo_297():
    """Todo #297: closing is signed and dangerous, its warning is a reading; the chantier of a new session is an optional list."""
    fermer, lecture = catalogue.PAR_ID["session-fermer"], catalogue.PAR_ID["session-fermeture"]
    assert (fermer.type, fermer.signe, fermer.cible, fermer.danger, fermer.risque, fermer.ouvert) == (
        "signee", "session", "fermer:{nom}", True, "high", False)
    assert lecture.type == "lecture" and lecture.argv == ("session", "fermeture", "{nom}")
    assert "--force" not in " ".join(fermer.argv)
    sheet = catalogue.feuille(fermer, {"nom": "ultron-20-10-09h12"}, None)
    assert all(sheet[k] for k in ("quoi", "sur_quoi", "effet", "risque")) and "vérifiée" in sheet["effet"]
    nouvelle = catalogue.PAR_ID["session-nouvelle"]
    chantier = nouvelle.champ("chantier")
    assert (chantier.type, chantier.liste, chantier.min) == ("choix", "chantiers", 0)
    assert catalogue.cible(nouvelle, {"chantier": "", "consigne": "x"}, "x").startswith("nouvelle::")
    assert catalogue.commande(nouvelle, {"chantier": "", "consigne": ""}, "") == ["session", "nouvelle", "--consigne-stdin"]
    assert catalogue.commande(nouvelle, {"chantier": "fba", "consigne": ""}, "")[2] == "--chantier=fba"
    for bad in ("-h", "../x", "a b", "A"):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("session-nouvelle", {"chantier": bad, "consigne": ""})


def test_a_video_knows_its_session_and_takes_an_optional_sentence_on_stdin():  # todo #440
    for action_id, base in (("video-tout", ["video", "analyse-lien"]), ("video-collecte", ["video", "collect"])):
        a = catalogue.PAR_ID[action_id]
        lien = "https://youtu.be/abcdefghijk"
        _, args, stdin = catalogue.valider(action_id, {"lien": lien})
        assert catalogue.commande(a, args, stdin) == [*base, lien, "--phrase-stdin"] and stdin == ""
        assert "depuis la session" not in catalogue.feuille(a, args, stdin)["sur_quoi"]
        body = {"lien": lien, "session": "ultron-fba-20-10-09h12", "phrase": "  pour le chantier  "}
        _, args, stdin = catalogue.valider(action_id, body)
        assert catalogue.commande(a, args, stdin) == [*base, lien, "--session=ultron-fba-20-10-09h12", "--phrase-stdin"]
        assert stdin == "pour le chantier"
        assert catalogue.feuille(a, args, stdin)["sur_quoi"] == f"{lien} · depuis la session ultron-fba-20-10-09h12"
        assert "pour le chantier" not in catalogue.ligne_journal(a, args, stdin)
        for bad in ({"session": "../x"}, {"phrase": "x" * 501}):
            with pytest.raises(catalogue.Refused):
                catalogue.valider(action_id, {"lien": lien, **bad})


def test_a_background_action_carries_a_known_text_stdin():  # todo #440, then #437: a required one too (a montage script)
    base = catalogue.PAR_ID["video-tout"]
    required = dataclasses.replace(base, champs=(base.champs[0], base.champs[1], dataclasses.replace(base.champs[2], min=1)))
    catalogue.verifier((base,))
    catalogue.verifier((required,))
    catalogue.verifier((catalogue.PAR_ID["montage-script"],))
    for bad in (dataclasses.replace(base, stdin="inconnu"), dataclasses.replace(base, type="lecture")):
        with pytest.raises(catalogue.CatalogueError):
            catalogue.verifier((bad,))


def test_one_button_collects_then_analyses_in_its_own_unit_and_delivers_both_ways():  # todo #335
    a, args, stdin = catalogue.valider("video-tout", {"lien": "https://youtu.be/abcdefghijk"})
    assert catalogue.commande(a, args, stdin) == ["video", "analyse-lien", "https://youtu.be/abcdefghijk", "--phrase-stdin"]
    assert a.fond and a.livre and a.livre_echec  # survives a closed page / a restart, and warns on success and on failure
    assert a.delai == catalogue.PAR_ID["video-collecte"].delai + catalogue.PAR_ID["video-analyse"].delai


def test_comptes_reading_exposes_the_addresses_and_the_page_shows_them():
    """Todo #339: the `comptes` reading keeps `adresse` (rows, to reconnect, active/other) and the page uses it, letters stay internal."""
    data = {"actif": "a", "actif_adresse": "a@exemple.fr", "autre_adresse": "b@exemple.fr", "token": "x",
            "comptes": [{"nom": "a", "adresse": "a@exemple.fr", "secret": 1}], "a_reconnecter": [{"nom": "b", "adresse": "b@exemple.fr"}]}
    out = catalogue.projeter(catalogue.PAR_ID["comptes"].donnees, data)
    assert out["actif_adresse"] == "a@exemple.fr" and out["comptes"] == [{"nom": "a", "adresse": "a@exemple.fr"}]
    assert out["a_reconnecter"] == [{"nom": "b", "adresse": "b@exemple.fr"}] and "token" not in out
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert "nomCompte(c.nom)" in js and "boutonCompte(id, ctx, a.bouton)" in js


def test_page_shows_the_address_in_the_switch_sheet():
    js = (server.STATIC / "app.js").read_text(encoding="utf-8")
    assert 'kv("Sur quoi", surCompte(data, d.sur_quoi))' in js and "ADRESSES[slot]" in js


def test_missions_are_gone_from_the_catalogue():  # todo #455
    assert not [a.id for a in catalogue.CATALOGUE if a.id == "demande" or a.id.startswith("mission") or a.argv[0] == "mission"]
    with pytest.raises(catalogue.Refused):
        catalogue.valider("demande", {"texte": "x"})


def test_voix_lire_is_open_stdin_only_and_capped():
    """todo #439: 🔊 Lire: the text goes on stdin (never argv), the journal keeps the length, not the words."""
    a = catalogue.PAR_ID["voix-lire"]
    assert a.ouvert and a.stdin == "texte" and a.argv == ("montage", "lire") and a.risque == "low"
    _, args, stdin = catalogue.valider("voix-lire", {"action": "voix-lire", "texte": "Bonjour."})
    assert stdin == "Bonjour." and catalogue.commande(a, args, stdin) == ["montage", "lire"]
    assert "Bonjour" not in catalogue.ligne_journal(a, args, stdin)
    with pytest.raises(catalogue.Refused):
        catalogue.valider("voix-lire", {"action": "voix-lire", "texte": "a" * 2501})


# -- 🎬 Monter un script / un fichier audio (todo #437) ------------------------------------------------------------------

def test_montage_script_sends_the_text_on_stdin_with_a_closed_style_and_end():
    a, args, stdin = catalogue.valider("montage-script", {"texte": "--help Salut le monde", "style": "papier", "fin": "suite"})
    assert catalogue.commande(a, args, stdin) == ["montage", "make", "--script", "--style", "papier", "--fin", "suite"]
    assert stdin == "--help Salut le monde" and "Salut" not in " ".join(catalogue.commande(a, args, stdin))
    _, args, stdin = catalogue.valider("montage-script", {"texte": "Bonjour"})  # defaults: noir, abonne
    assert catalogue.commande(a, args, stdin)[3:] == ["--style", "noir", "--fin", "abonne"]
    assert a.fond and a.livre == "🎬 Montage rendu" and a.livre_echec and a.delai == 25 * 60
    assert [c.code for c in a.champ("style").choix] == list(plan.THEMES) and [c.code for c in a.champ("fin").choix] == list(plan.FINS)
    assert all(c.libelle == plan.THEME_LABELS[c.code] for c in a.champ("style").choix)
    assert all(c.libelle == plan.FIN_LABELS[c.code] for c in a.champ("fin").choix)


@pytest.mark.parametrize("bad", [{"style": "neon"}, {"style": "--help"}, {"fin": "autre"}, {"fin": "abonne --x"}, {"texte": "  "}])
def test_montage_script_refuses_what_is_not_in_the_lists(bad):
    with pytest.raises(catalogue.Refused):
        catalogue.valider("montage-script", {"texte": "Bonjour", **bad})


def test_montage_audio_takes_only_a_received_file_name():
    name = "20261020T101500-a1b2c3-note.m4a"
    a, args, stdin = catalogue.valider("montage-audio", {"fichier": name, "style": "canevas", "fin": "rien"})
    assert catalogue.commande(a, args, stdin) == ["montage", "make", f"--recu={name}", "--style", "canevas", "--fin", "rien"]
    assert stdin is None and a.fond and a.livre and a.livre_echec and a.delai == 25 * 60
    for bad in ("/etc/passwd", "../recus/x.m4a", "note.m4a", "20261020T101500-a1b2c3-note.exe", "--help", name + " --x", ""):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("montage-audio", {"fichier": bad})
    with pytest.raises(catalogue.Refused):
        catalogue.valider("montage-audio", {"fichier": name, "style": "neon"})


def test_a_background_action_runs_its_stdin_in_its_own_unit(dash):
    sid = session(dash)
    body = {"action": "montage-script", "texte": "Salut le monde", "style": "papier", "fin": "rien"}
    _, prep, _ = prepare(dash, sid, body)
    code, res, _ = confirm(dash, sid, prep["jeton"])
    assert code == 200 and res["ok"] and res["suivi"], res
    [call] = dash.calls()
    assert call["argv"][-5:] == ["--script", "--style", "papier", "--fin", "rien"] and call["stdin"] == "Salut le monde"
