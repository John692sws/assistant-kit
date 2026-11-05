"""Todo #509: lanceur des routines à l'heure (horloge, tmux, usage et lanceur injectés, jamais le vrai claude/tmux)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from ultron_hub import cli, objectif, routines, routines_lanceur as rl
from ultron_hub.common.settings import load_settings

T0 = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
ROUTINE = {"nom": "veille", "phrase": "p", "cadence": "toutes les 2 h", "but": "Faire le point",
           "critere_fin": ["test -s x"], "plafond": 5, "chantier": "fba", "cree_le": T0.isoformat(),
           "active_le": T0.isoformat()}


@pytest.fixture
def env(project, tmp_path, monkeypatch):
    root = tmp_path / "routines"
    monkeypatch.setattr(routines, "RACINE", root)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "fba.md").write_text("x")
    monkeypatch.setenv("ULTRON_PROMPTS_DIR", str(prompts))
    (root / "actives").mkdir(parents=True)
    (root / "actives" / "veille.json").write_text(json.dumps(ROUTINE))
    monkeypatch.setattr(rl, "ATTENTE_S", 0)
    return load_settings()


class Faux:
    def __init__(self, vivantes=(), usage=None):
        self.vivantes, self.lances = set(vivantes), []
        self.pct = 10 if usage is None else usage

    def tmux(self, argv):
        if argv[0] == "has-session":
            return (0 if argv[2][1:] in self.vivantes else 1), ""
        return 0, "\n".join(self.vivantes)

    def ultron(self, argv):
        self.lances.append(argv)
        nom = f"ultron-03-11-{len(self.lances):02d}h00"
        self.vivantes.add(nom)
        return 0, f"Session tmux : {nom}\n"

    def usage(self):
        return {"stale": False, "session": {"percent": self.pct}}

    def run(self, s, now, essai=False):
        return rl.lancer(s, essai, tmux=self.tmux, lanceur=self.ultron, usage=self.usage, now=now)


def test_pas_echue(env):
    f = Faux()
    res = f.run(env, T0 + timedelta(hours=1, minutes=59))
    assert res["plan"] == [] and f.lances == []


def test_echue_lance_et_lie(env):
    f = Faux()
    res = f.run(env, T0 + timedelta(hours=2, minutes=3))
    assert [e["action"] for e in res["plan"]] == ["lancement"] and len(f.lances) == 1
    assert f.lances[0][:4] == ["--modele", "opus", "--chantier", "fba"]
    assert f.lances[0][4] == "Objectif 1 (ultron-hub objectif show 1), routine veille : Faire le point"
    obj = objectif.lire(env, 1)
    assert obj["budget"] == "5 points" and obj["chantier"] == "fba" and obj["session"] == "ultron-03-11-01h00"
    assert [p["texte"] for p in obj["preuves"]] == ["routine : veille"]
    assert rl.lire_etat(env)["veille"]["session"] == "ultron-03-11-01h00"


def test_double_passage(env):
    f = Faux()
    f.run(env, T0 + timedelta(hours=2, minutes=3))
    f.vivantes.clear()
    res = f.run(env, T0 + timedelta(hours=2, minutes=8))
    assert res["plan"] == [] and len(f.lances) == 1


def test_etat_ecrit_avant_lancement(env):
    vu = {}

    def ultron(argv):
        vu.update(rl.lire_etat(env))
        return 1, ""
    res = rl.lancer(env, tmux=Faux().tmux, lanceur=ultron, usage=Faux().usage, now=T0 + timedelta(hours=3))
    assert vu["veille"]["objectif"] == 1 and res["plan"][0]["action"] == "echec"
    assert rl.lancer(env, tmux=Faux().tmux, lanceur=ultron, usage=Faux().usage, now=T0 + timedelta(hours=3, minutes=5))["plan"] == []


def test_session_encore_vivante(env):
    f = Faux()
    f.run(env, T0 + timedelta(hours=2))
    res = f.run(env, T0 + timedelta(hours=4, minutes=1))
    assert res["plan"][0]["action"] == "rien" and "existe encore" in res["plan"][0]["raison"]
    assert len(f.lances) == 1
    f.vivantes.clear()
    assert f.run(env, T0 + timedelta(hours=4, minutes=6))["plan"][0]["action"] == "lancement"


def test_quota_epuise(env):
    f = Faux(usage=97)
    res = f.run(env, T0 + timedelta(hours=2))
    assert res["plan"][0]["action"] == "rien" and "quota" in res["plan"][0]["raison"]
    assert f.lances == [] and rl.lire_etat(env) == {} and not objectif.lister(env)


def test_rafale_un_seul_lancement(env):
    f = Faux()
    f.run(env, T0 + timedelta(hours=11))  # 5 échéances ratées
    f.vivantes.clear()
    assert len(f.lances) == 1
    assert f.run(env, T0 + timedelta(hours=11, minutes=5))["plan"] == []
    assert f.run(env, T0 + timedelta(hours=13, minutes=1))["plan"][0]["action"] == "lancement"


def test_un_par_passage(env):
    (routines.RACINE / "actives" / "autre.json").write_text(json.dumps({**ROUTINE, "nom": "autre"}))
    f = Faux()
    res = f.run(env, T0 + timedelta(hours=3))
    assert sorted(e["action"] for e in res["plan"]) == ["lancement", "rien"] and len(f.lances) == 1


def test_essai_ne_touche_rien(env, capsys):
    f = Faux()
    res = f.run(env, T0 + timedelta(hours=3), essai=True)
    e = res["plan"][0]
    assert e["action"] == "lancement" and e["routine"] == "veille" and e["echeance"] and e["commande"][3] == "--chantier"
    assert f.lances == [] and rl.lire_etat(env) == {} and not objectif.lister(env)
    assert not (routines.var(env) / "lancements.json").exists()


def test_cli_essai(env, capsys, monkeypatch):
    monkeypatch.setattr(rl.reprise, "tmux_reel", Faux().tmux)
    monkeypatch.setattr(rl.reprise, "usage_reel", lambda s: {"stale": False, "session": {"percent": 1}})
    assert cli.main(["routine", "lancer", "--essai"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "ok" and out["data"]["essai"] is True and out["data"]["plan"][0]["action"] == "lancement"
    assert not objectif.lister(env)


def test_lanceur_auto_avale_les_exceptions(env, monkeypatch, capsys):
    monkeypatch.setattr(rl, "lancer", lambda s: 1 / 0)
    rl.lanceur_auto(env)
    assert "ZeroDivisionError" in capsys.readouterr().err



def test_deja_lancee_entre_decision_et_verrou(env, monkeypatch):
    """Another pass launched between the decision and the lock: nothing launched, no objective created."""
    now = T0 + timedelta(hours=3)
    routines.var(env).mkdir(parents=True, exist_ok=True)
    rl._ecrire_etat(env, {"veille": {"dernier": now.isoformat(), "objectif": 1, "session": None}})
    vraie = rl.lire_etat
    appels = []

    def lire(settings):
        appels.append(1)
        return {} if len(appels) == 1 else vraie(settings)

    monkeypatch.setattr(rl, "lire_etat", lire)
    avant = len(objectif.lister(env))
    f = Faux()
    res = f.run(env, now)
    assert f.lances == []
    assert res["plan"][0]["action"] == "rien" and "autre passage" in res["plan"][0]["raison"]
    assert len(objectif.lister(env)) == avant


@pytest.fixture
def porte(env, tmp_path, monkeypatch):
    """Todo #510: the routine carries a gate on one file under a fake HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "suivi.md"
    f.write_text("v1")
    r = {**ROUTINE, "porte": {"fichiers": [str(f)]}}
    (routines.RACINE / "actives" / "veille.json").write_text(json.dumps(r))
    return f


def test_porte_premier_passage_lance_puis_saute_puis_relance(env, porte):
    f = Faux()
    assert f.run(env, T0 + timedelta(hours=2))["plan"][0]["action"] == "lancement"
    assert rl.lire_etat(env)["veille"]["porte"] and len(f.lances) == 1
    f.vivantes.clear()
    nb = len(objectif.lister(env))
    res = f.run(env, T0 + timedelta(hours=4, minutes=1))
    e = res["plan"][0]
    assert e["action"] == "saute" and e["raison"].startswith("porte : rien") and "objectif" not in e
    assert "1 sautée(s)" in res["resume"] and len(f.lances) == 1 and len(objectif.lister(env)) == nb
    assert rl.lire_etat(env)["veille"]["evites"] == 1
    assert f.run(env, T0 + timedelta(hours=4, minutes=6))["plan"] == []  # waits for the cadence again
    assert routines.liste(env)["actives"][0]["evites"] == 1 and routines.voir("veille", env)["evites"] == 1
    porte.write_text("version deux")
    res = f.run(env, T0 + timedelta(hours=6, minutes=2))
    assert res["plan"][0]["action"] == "lancement" and len(f.lances) == 2
    assert rl.lire_etat(env)["veille"]["evites"] == 1


def test_porte_essai_n_ecrit_rien(env, porte):
    f = Faux()
    f.run(env, T0 + timedelta(hours=2))
    f.vivantes.clear()
    avant = (routines.var(env) / "lancements.json").read_text()
    res = f.run(env, T0 + timedelta(hours=4, minutes=1), essai=True)
    assert res["plan"][0]["action"] == "saute" and len(f.lances) == 1
    assert (routines.var(env) / "lancements.json").read_text() == avant


def test_porte_fichier_absent_puis_cree(env, porte):
    porte.unlink()
    f = Faux()
    assert f.run(env, T0 + timedelta(hours=2))["plan"][0]["action"] == "lancement"
    f.vivantes.clear()
    assert f.run(env, T0 + timedelta(hours=4, minutes=1))["plan"][0]["action"] == "saute"
    porte.write_text("apparu")
    assert f.run(env, T0 + timedelta(hours=6, minutes=2))["plan"][0]["action"] == "lancement"


@pytest.fixture
def recus(env, tmp_path, monkeypatch):
    """Todo #512: the routine fires on a file landing in a watched folder (tmp_path plays var/recus)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "recus"
    d.mkdir()
    r = {k: v for k, v in ROUTINE.items() if k != "cadence"}
    (routines.RACINE / "actives" / "veille.json").write_text(
        json.dumps({**r, "cadence": routines.CADENCE_EVT, "quand": {"dossier": str(d)}}))
    return d


def test_evenement_dossier_lance_une_fois(env, recus):
    f = Faux()
    assert f.run(env, T0)["plan"] == []  # empty folder: nothing
    (recus / "a.pdf").write_text("un")
    res = f.run(env, T0 + timedelta(minutes=5), essai=True)  # --essai: action shown, nothing written
    assert res["plan"][0]["action"] == "lancement" and "recus" in res["plan"][0]["evenement"]
    assert f.lances == [] and rl.lire_etat(env) == {} and objectif.lister(env) == []
    res = f.run(env, T0 + timedelta(minutes=5))
    assert res["plan"][0]["action"] == "lancement" and len(f.lances) == 1
    assert rl.lire_etat(env)["veille"]["evenement"]
    f.vivantes.clear()
    nb = len(objectif.lister(env))
    assert f.run(env, T0 + timedelta(minutes=10))["plan"] == [] and len(f.lances) == 1  # same fingerprint
    (recus / "b.pdf").write_text("deux")
    assert f.run(env, T0 + timedelta(minutes=15))["plan"][0]["action"] == "lancement"
    assert len(f.lances) == 2 and len(objectif.lister(env)) == nb + 1


def test_evenement_dossier_garde_fous(env, recus):
    (recus / "a.pdf").write_text("un")
    f = Faux(usage=99)
    assert f.run(env, T0)["plan"][0]["action"] == "rien" and rl.lire_etat(env) == {}  # quota: retried later
    f.pct = 10
    assert f.run(env, T0 + timedelta(minutes=5))["plan"][0]["action"] == "lancement"
    (recus / "b.pdf").write_text("deux")  # session still alive: no double launch
    res = f.run(env, T0 + timedelta(minutes=10))
    assert res["plan"][0]["action"] == "rien" and "existe encore" in res["plan"][0]["raison"] and len(f.lances) == 1


def test_evenement_service_tombe(env, tmp_path):
    (routines.RACINE / "actives" / "veille.json").write_text(json.dumps(
        {**ROUTINE, "cadence": routines.CADENCE_EVT, "quand": {"service": "ultron-bot.service"}}))
    etat, appels = ["active"], []

    def svc(unite):
        appels.append(unite)
        return etat[0]

    f = Faux()
    run = lambda m, **k: rl.lancer(env, tmux=f.tmux, lanceur=f.ultron, usage=f.usage, now=T0 + timedelta(minutes=m),
                                   service=svc, **k)
    assert run(0)["plan"] == [] and appels == ["ultron-bot.service"]
    etat[0] = "failed"
    assert run(5, essai=True)["plan"][0]["action"] == "lancement" and f.lances == [] and rl.lire_etat(env) == {}
    assert run(5)["plan"][0]["action"] == "lancement" and len(f.lances) == 1
    f.vivantes.clear()
    assert run(10)["plan"] == [] and len(f.lances) == 1  # stays down: no relaunch
    etat[0] = "active"
    assert run(15)["plan"] == []
    etat[0] = "inactive"
    assert run(20)["plan"][0]["action"] == "lancement" and len(f.lances) == 2  # fell again
    etat[0] = ""  # unreadable: nothing
    f.vivantes.clear()
    assert run(25)["plan"] == []


def test_commande_porte_le_modele_du_mode(env):
    """Todo #517: argv = ultron --modele <model of the mode> --chantier ...; a routine without mode = opus."""
    assert rl.commande(ROUTINE, 7)[1:3] == ["--modele", "opus"]  # old active file, no mode field
    for mode, modele in routines.MODES.items():
        argv = rl.commande({**ROUTINE, "mode": mode}, 7)
        assert argv[1:5] == ["--modele", modele, "--chantier", "fba"] and argv[-1].startswith("Objectif 7 ")
    f = Faux()
    f.run(env, T0 + timedelta(hours=3))
    assert f.lances[0][:2] == ["--modele", "opus"]
    (routines.RACINE / "actives" / "veille.json").write_text(json.dumps({**ROUTINE, "mode": "tri"}))
    res = Faux().run(env, T0 + timedelta(hours=9), essai=True)
    assert res["plan"][0]["commande"][1:3] == ["--modele", "haiku"], res


def test_session_morte_trop_tot_arrete_la_routine(env):
    f = Faux()
    f.run(env, T0 + timedelta(hours=2))
    f.vivantes.clear()  # the session died right away; the objective is still open
    res = f.run(env, T0 + timedelta(hours=2, minutes=1))  # a passage less than 2 min after the launch notes it
    assert res["plan"] == [] and any("arrêtée" in p["texte"] for p in objectif.lire(env, 1)["preuves"])
    res = f.run(env, T0 + timedelta(hours=4, minutes=1))
    assert res["plan"][0]["action"] == "rien" and "morte dès le lancement" in res["plan"][0]["raison"] and len(f.lances) == 1
    # a new ✅ (new active_le) lifts the stop
    r = dict(ROUTINE, active_le=(T0 + timedelta(hours=9)).isoformat())
    (routines.RACINE / "actives" / "veille.json").write_text(json.dumps(r))
    assert f.run(env, T0 + timedelta(hours=12))["plan"][0]["action"] == "lancement"


def test_session_morte_juste_apres_lancement_est_vue_par_le_lanceur(env):
    f = Faux()
    orig = f.ultron

    def ephemere(argv):
        rc, out = orig(argv)
        f.vivantes.clear()
        return rc, out

    res = rl.lancer(env, tmux=f.tmux, lanceur=ephemere, usage=f.usage, now=T0 + timedelta(hours=2))
    assert "morte dès le lancement" in res["plan"][0]["raison"]
    assert "arrêtée" in objectif.lire(env, 1)["preuves"][-1]["texte"]
    assert f.run(env, T0 + timedelta(hours=4))["plan"][0]["action"] == "rien" and len(f.lances) == 1


def test_session_finie_normalement_n_arrete_pas(env):
    f = Faux()
    f.run(env, T0 + timedelta(hours=2))
    f.vivantes.clear()  # finished long after the launch
    res = f.run(env, T0 + timedelta(hours=4, minutes=1))
    assert res["plan"][0]["action"] == "lancement"
