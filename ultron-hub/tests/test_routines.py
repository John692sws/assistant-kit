"""Todo #503: 🔁 Routine proposée, a routine in one sentence activated by a ✅ signed on its exact content (nothing runs)."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from hermes_plugin import close_key
from test_dashboard_actions import confirm, dash, fake_cli, prepare, session  # noqa: F401  (fixtures)
from ultron_hub import cli, routines
from ultron_hub.dashboard import apercu, catalogue
from ultron_hub.todo import closing

PROPOSE = ["routine", "propose", "--nom", "veille-fba", "--phrase", "Chaque lundi, fais-moi un point FBA.",
           "--cadence", "chaque lundi 08:00", "--but", "Point hebdomadaire FBA", "--critere", "test -s point.md",
           "--plafond", "5", "--chantier", "fba"]


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture
def racine(project, tmp_path, monkeypatch):
    root = tmp_path / "routines"
    monkeypatch.setattr(routines, "RACINE", root)
    return root


def propose(capsys, *over):
    argv = list(PROPOSE)
    for i in range(0, len(over), 2):
        if over[i] in argv:
            argv[argv.index(over[i]) + 1] = over[i + 1]
        else:
            argv += [over[i], over[i + 1]]
    return run_cli(capsys, *argv)


def activer(capsys, monkeypatch, nom, emp, token_target=None):
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("routine", token_target or f"{nom}:{emp}"))
    return run_cli(capsys, "routine", "activer", "--nom", nom, "--empreinte", emp)


def liste(capsys):
    code, out = run_cli(capsys, "routine", "liste")
    assert code == 0, out
    return out["data"]


@pytest.mark.parametrize("cadence", ["toutes les 1 h", "toutes les 6 h", "chaque jour 07:30", "chaque dimanche 23:59",
                                     "chaque  lundi   08:00"])
def test_valid_cadences_are_accepted(racine, capsys, cadence):
    code, out = propose(capsys, "--cadence", cadence)
    assert code == 0, out
    assert out["data"]["cadence"] == " ".join(cadence.split()) and out["data"]["prochain"]
    assert (racine / "proposees" / "veille-fba.json").is_file()


@pytest.mark.parametrize("cadence", ["toutes les 0 h", "toutes les 30 min", "toutes les 01 h", "chaque heure",
                                     "chaque jour 24:00", "chaque jour 8:00", "chaque lundi 08:60", "chaque Lundi 08:00",
                                     "tous les jours 08:00", "chaque jour 08:00 et 09:00", "*/5 * * * *", ""])
def test_other_cadences_are_refused(racine, capsys, cadence):
    code, out = propose(capsys, "--cadence", cadence)
    assert code == 1 and "cadence" in out["error"], out
    assert not (racine / "proposees").exists() or list((racine / "proposees").iterdir()) == []


def test_one_hour_is_the_minimum_recurrence():
    now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    assert routines.prochain("toutes les 1 h", now) == now + timedelta(hours=1)
    depuis = now - timedelta(hours=5, minutes=30)
    assert routines.prochain("toutes les 2 h", now, depuis) == depuis + timedelta(hours=6)
    with pytest.raises(Exception):
        routines.parse_cadence("toutes les 0 h")
    lundi = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)  # a Monday
    assert routines.prochain("chaque lundi 08:00", lundi) == datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
    assert routines.prochain("chaque mercredi 08:00", lundi) == datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
    assert routines.prochain("chaque jour 09:30", lundi) == datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc)
    assert routines.prochain("chaque jour 08:00", lundi) == datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("over, mot", [
    (("--nom", "Veille"), "nom invalide"), (("--nom", "veille_fba"), "nom invalide"),
    (("--nom", "a" * 41), "nom invalide"), (("--nom", "x-" * 21), "nom invalide"), (("--nom", "../x"), "nom invalide"),
    (("--plafond", "0"), "plafond"), (("--plafond", "21"), "plafond"), (("--chantier", "Mon chantier"), "chantier"),
    (("--critere", "  "), "critère"), (("--phrase", " "), "phrase vide")])
def test_bad_fields_are_refused(racine, capsys, over, mot):
    argv = [x for x in PROPOSE]
    if over[0] == "--critere":
        i = argv.index("--critere")
        argv[i + 1] = over[1]
        code, out = run_cli(capsys, *argv)
    else:
        code, out = propose(capsys, *over)
    assert code == 1 and mot in out["error"], out
    assert not (racine / "proposees").exists() or list((racine / "proposees").iterdir()) == []


def test_one_to_three_end_commands(racine, capsys):
    argv = PROPOSE + ["--critere", "true", "--critere", "true", "--critere", "true"]
    code, out = run_cli(capsys, *argv)
    assert code == 1 and "1 à 3" in out["error"]
    code, out = run_cli(capsys, *(PROPOSE + ["--critere", "echo ok"]))
    assert code == 0 and out["data"]["critere_fin"] == ["test -s point.md", "echo ok"]
    code, out = propose(capsys)
    assert code == 1 and "existe déjà" in out["error"]


def test_fingerprint_is_the_canonical_json_and_activation_is_signed(racine, capsys, monkeypatch):
    assert propose(capsys)[0] == 0
    [r] = liste(capsys)["proposees"]
    stored = json.loads((racine / "proposees" / "veille-fba.json").read_text())
    assert r["empreinte"] == routines.empreinte(stored) and len(r["empreinte"]) == 16
    assert set(stored) == {"nom", "phrase", "cadence", "but", "critere_fin", "plafond", "chantier", "cree_le", "mode"}
    assert r["prochain"] and r["lignes"][1] == "Phrase : Chaque lundi, fais-moi un point FBA."
    code, out = run_cli(capsys, "routine", "activer", "--nom", "veille-fba", "--empreinte", r["empreinte"])  # no token
    assert code == 1 and "réservé à l'utilisateur" in out["error"] and (racine / "proposees" / "veille-fba.json").exists()
    code, out = activer(capsys, monkeypatch, "veille-fba", r["empreinte"])
    assert code == 0, out
    active = json.loads((racine / "actives" / "veille-fba.json").read_text())
    assert active["active_par"] == "telegram-button" and active["phrase"] == stored["phrase"]
    assert not (racine / "proposees" / "veille-fba.json").exists()
    data = liste(capsys)
    assert data["proposees"] == [] and [a["nom"] for a in data["actives"]] == ["veille-fba"]
    code, out = run_cli(capsys, "routine", "voir", "--nom", "veille-fba")
    assert code == 0 and out["data"]["statut"] == "active"


def test_a_changed_routine_is_refused_and_a_token_is_never_replayed(racine, capsys, monkeypatch):
    assert propose(capsys)[0] == 0
    path = racine / "proposees" / "veille-fba.json"
    emp = liste(capsys)["proposees"][0]["empreinte"]
    stored = json.loads(path.read_text())
    path.write_text(json.dumps({**stored, "plafond": 20}))
    code, out = activer(capsys, monkeypatch, "veille-fba", emp)
    assert code == 1 and "a changé" in out["error"] and not (racine / "actives").exists()
    new = liste(capsys)["proposees"][0]["empreinte"]
    code, out = activer(capsys, monkeypatch, "veille-fba", new, token_target=f"veille-fba:{emp}")  # token of the old one
    assert code == 1 and "invalide" in out["error"]
    monkeypatch.setenv(closing.TOKEN_ENV, close_key.token("routine", f"veille-fba:{new}"))
    code, out = run_cli(capsys, "routine", "activer", "--nom", "veille-fba", "--empreinte", new)
    assert code == 0, out
    path.write_text(json.dumps({**stored, "plafond": 20}))  # proposed again, same content: same fingerprint, same token
    code, out = run_cli(capsys, "routine", "activer", "--nom", "veille-fba", "--empreinte", new)
    assert code == 1 and "déjà" in out["error"], out


def test_a_hand_edited_cadence_never_gets_active(racine, capsys, monkeypatch):
    assert propose(capsys)[0] == 0
    path = racine / "proposees" / "veille-fba.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "cadence": "toutes les 0 h"}))
    [r] = liste(capsys)["proposees"]
    assert r["prochain"] is None
    code, out = activer(capsys, monkeypatch, "veille-fba", r["empreinte"])
    assert code == 1 and "cadence" in out["error"] and not (racine / "actives").exists()


def test_fifty_active_routines_at_most(racine, capsys, monkeypatch):
    actives = racine / "actives"
    actives.mkdir(parents=True)
    for i in range(routines.ACTIVES_MAX):
        (actives / f"r{i}.json").write_text("{}")
    assert propose(capsys)[0] == 0
    emp = liste(capsys)["proposees"][0]["empreinte"]
    code, out = activer(capsys, monkeypatch, "veille-fba", emp)
    assert code == 1 and "50 routines actives" in out["error"]
    assert (racine / "proposees" / "veille-fba.json").exists()
    (actives / "r0.json").unlink()
    code, out = activer(capsys, monkeypatch, "veille-fba", emp)
    assert code == 0, out


def test_refuser_moves_and_deletes_nothing(racine, capsys):
    assert propose(capsys)[0] == 0
    code, out = run_cli(capsys, "routine", "refuser", "--nom", "veille-fba")
    assert code == 0, out
    assert json.loads((racine / "refusees" / "veille-fba.json").read_text())["nom"] == "veille-fba"
    assert propose(capsys)[0] == 0
    assert run_cli(capsys, "routine", "refuser", "--nom", "veille-fba")[0] == 0
    assert (racine / "refusees" / "veille-fba-2.json").exists()
    code, out = run_cli(capsys, "routine", "refuser", "--nom", "veille-fba")
    assert code == 1 and "pas en attente" in out["error"]
    for bad in ("../x", "X"):
        code, out = run_cli(capsys, "routine", "voir", "--nom", bad)
        assert code == 1 and "nom invalide" in out["error"]


def test_catalogue_signs_routine_on_name_and_fingerprint_alike_on_both_sides():
    a = catalogue.PAR_ID["routine"]
    _, args, stdin = catalogue.valider("routine", {"nom": "veille-fba", "empreinte": "0123456789abcdef"})
    assert a.signe == "routine" and catalogue.cible(a, args, stdin) == "veille-fba:0123456789abcdef"
    assert a.detail == "routine-voir" and catalogue.PAR_ID["routines"].type == "lecture"
    assert catalogue.PAR_ID["routine-voir"].type == "lecture" and catalogue.PAR_ID["routine-refuser"].type == "simple"
    for bad in ({"nom": "../x"}, {"nom": "Veille"}, {"empreinte": "0123"}):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("routine", {"nom": "veille-fba", "empreinte": "0123456789abcdef", **bad})
    assert close_key.ACTIONS == closing.ACTIONS and close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    assert "routine" in closing.ACTIONS and "routine" in closing.DASHBOARD_ACTIONS
    key, target = b"k" * 64, "veille-fba:0123456789abcdef"
    assert close_key.token("routine", target, key=key, now=1000) == closing.sign(key, "routine", target, 1000)
    tok = closing.sign_dashboard(close_key.load_or_create(), "iphone", "routine", target, int(time.time()))
    assert closing.check(tok, "routine", target)[1] == "dashboard:iphone"
    with pytest.raises(closing.CloseRefused):
        closing.check(tok, "habitude", target)
    fake = catalogue.projeter(catalogue.PAR_ID["routines"].donnees, apercu.LECTURES["routines"])
    r = fake["proposees"][0]
    assert r["phrase"] and r["cadence"] and r["but"] and r["critere_fin"] and r["plafond"]


def test_the_sheet_lists_the_routine_and_refuses_a_changed_one(dash, monkeypatch):
    lu = []
    voir = {"empreinte": "0123456789abcdef", "lignes": ["Routine « veille-fba »", "Cadence : chaque lundi 08:00"]}
    monkeypatch.setattr(dash.acts, "_lire_detail", lambda reading, args: lu.append((reading.id, args)) or voir)
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "routine", "nom": "veille-fba", "empreinte": "0123456789abcdef"})
    assert out["ok"] and out["detail"] == voir["lignes"] and out["detail_brut"] is True
    assert lu == [("routine-voir", {"nom": "veille-fba"})]
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["routine", "activer", "--nom=veille-fba", "--empreinte=0123456789abcdef"]
    assert closing.check(call["env"][closing.TOKEN_ENV], "routine", "veille-fba:0123456789abcdef")[1].startswith("dashboard:")
    _, out, _ = prepare(dash, sid, {"action": "routine", "nom": "veille-fba", "empreinte": "fedcba9876543210"})
    assert not out["ok"] and "a changé" in out["erreur"] and len(dash.calls()) == 1


def test_porte_valide_et_refusee(racine, tmp_path, monkeypatch, capsys):
    """Todo #510: a gate is 1 to 10 absolute files under HOME, never a sensitive path; it is part of the fingerprint."""
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "suivi.md"
    code, out = propose(capsys, "--porte-fichier", str(f))
    assert code == 0 and out["data"]["porte"] == {"fichiers": [str(f)]}
    assert any(l.startswith("Porte : lance seulement si changé : ") and str(f) in l for l in out["data"]["lignes"])
    brut = {k: v for k, v in out["data"].items() if k not in ("statut", "empreinte", "lignes", "prochain")}
    assert out["data"]["empreinte"] == routines.empreinte(brut)
    sans = {k: v for k, v in brut.items() if k != "porte"}
    assert out["data"]["empreinte"] != routines.empreinte(sans)
    mauvais = ("relatif.md", str(tmp_path / ".ssh" / "id"), str(tmp_path / ".config" / "gh" / "hosts.yml"),
               str(tmp_path / "x" / "auth.json"), str(tmp_path / ".claude" / "a"), "/etc/passwd", str(tmp_path))
    for i, bad in enumerate(mauvais):
        code, out = propose(capsys, "--nom", f"mauvais-{i}", "--porte-fichier", bad)
        assert code != 0 or out["status"] != "ok", bad
        assert not (racine / "proposees" / f"mauvais-{i}.json").exists()
    onze = [x for i in range(11) for x in ("--porte-fichier", str(tmp_path / f"f{i}"))]
    code, out = run_cli(capsys, *PROPOSE[:3], "onze", *PROPOSE[4:], *onze)
    assert code != 0 or out["status"] != "ok"
    assert not (racine / "proposees" / "onze.json").exists()


def test_quand_valide_refuse_et_empreinte(racine, tmp_path, monkeypatch, capsys):
    """Todo #512: --quand-dossier / --quand-service replace the cadence, are shown in lignes and fingerprinted."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sans_cadence = [x for i, x in enumerate(PROPOSE) if i not in (6, 7)]
    code, out = run_cli(capsys, *sans_cadence, "--quand-dossier", str(tmp_path / "recus"))
    d = out["data"]
    assert code == 0 and d["quand"] == {"dossier": str(tmp_path / "recus")} and d["cadence"] == routines.CADENCE_EVT
    assert any(l.startswith("Quand : un fichier nouveau ou modifié dans le dossier ") for l in d["lignes"])
    brut = {k: v for k, v in d.items() if k not in ("statut", "empreinte", "lignes", "prochain")}
    assert d["empreinte"] == routines.empreinte(brut) != routines.empreinte({**brut, "quand": {"dossier": "/x"}})
    code, out = run_cli(capsys, *sans_cadence[:3], "svc", *sans_cadence[4:], "--quand-service", "ultron-bot.service")
    assert code == 0 and out["data"]["quand"] == {"service": "ultron-bot.service"}
    assert "Quand : le service ultron-bot.service est tombé" in out["data"]["lignes"]
    mauvais = [["--quand-dossier", str(tmp_path / ".ssh")], ["--quand-dossier", str(tmp_path / ".claude" / "x")],
               ["--quand-dossier", str(tmp_path / ".config" / "gh")], ["--quand-dossier", "/etc"],
               ["--quand-dossier", "relatif"], ["--quand-dossier", str(tmp_path)],
               ["--quand-service", "a b.service"], ["--quand-service", "x; rm -rf ~.service"],
               ["--quand-service", "-x.service"], ["--quand-service", "x"], ["--quand-service", "x.service\n"],
               ["--quand-dossier", str(tmp_path / "r"), "--quand-service", "x.service"],
               ["--porte-fichier", str(tmp_path / "f"), "--quand-service", "x.service"]]
    for i, bad in enumerate(mauvais):
        code, out = run_cli(capsys, *sans_cadence[:3], f"mauvais-{i}", *sans_cadence[4:], *bad)
        assert code != 0 or out["status"] != "ok", bad
        assert not (racine / "proposees" / f"mauvais-{i}.json").exists()
    code, out = run_cli(capsys, *sans_cadence[:3], "evt", *sans_cadence[4:])  # no cadence and no trigger
    assert code != 0 or out["status"] != "ok"
    code, out = run_cli(capsys, *PROPOSE[:3], "evt2", *PROPOSE[4:7], "sur évènement", *PROPOSE[8:])
    assert code != 0 or out["status"] != "ok"


def test_mode_defaut_valide_refuse_et_relu_a_l_activation(racine, monkeypatch, capsys):
    """Todo #517: --mode tri|travail|decider (default decider) is stored, shown in lignes, fingerprinted, reread at activation."""
    code, out = run_cli(capsys, *PROPOSE)
    d = out["data"]
    assert code == 0 and d["mode"] == "decider" and "Mode : decider (modèle opus)" in d["lignes"]
    code, out = run_cli(capsys, *PROPOSE[:3], "tri-fba", *PROPOSE[4:], "--mode", "tri")
    t = out["data"]
    assert code == 0 and t["mode"] == "tri" and "Mode : tri (modèle haiku)" in t["lignes"] and t["empreinte"] != d["empreinte"]
    code, out = activer(capsys, monkeypatch, "tri-fba", t["empreinte"])
    assert code == 0, out
    assert json.loads((racine / "actives" / "tri-fba.json").read_text())["mode"] == "tri"
    assert routines.MODES == {"tri": "haiku", "travail": "sonnet", "decider": "opus"}
    for mauvais in ("expert", "OPUS", ""):
        with pytest.raises(Exception):
            routines.valider("x", "p", "chaque jour 08:00", "b", ["c"], 5, "fba", mode=mauvais)
    assert run_cli(capsys, *PROPOSE[:3], "autre", *PROPOSE[4:], "--mode", "expert")[0] != 0
    assert not (racine / "proposees" / "autre.json").exists()
    assert [x for x in routines.lignes({"nom": "v"}) if x.startswith("Mode")] == ["Mode : decider (modèle opus)"]  # old file


def test_retirer_moves_active_and_keeps_names(racine, capsys):
    actives = racine / "actives"
    actives.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        (actives / "veille-fba.json").write_text(json.dumps({"nom": "veille-fba"}))
        code, out = run_cli(capsys, "routine", "retirer", "--nom", "veille-fba")
        assert code == 0, out
        assert not (actives / "veille-fba.json").exists()
    assert (racine / "retirees" / "veille-fba.json").exists() and (racine / "retirees" / "veille-fba-2.json").exists()
    code, out = run_cli(capsys, "routine", "retirer", "--nom", "veille-fba")
    assert code == 1 and "pas active" in out["error"]
    assert run_cli(capsys, "routine", "retirer", "--nom", "../x")[0] == 1


def test_catalogue_retirer_is_simple_unsigned():
    a = catalogue.PAR_ID["routine-retirer"]
    assert a.type == "simple" and not a.signe
    _, args, _ = catalogue.valider("routine-retirer", {"nom": "veille-fba"})
    assert args == {"nom": "veille-fba"}
    with pytest.raises(catalogue.Refused):
        catalogue.valider("routine-retirer", {"nom": "../x"})
