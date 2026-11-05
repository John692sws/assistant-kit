"""Todo #501: reprise des objectifs coupés ou bloqués par le quota (tmux, ultron et usage factices)."""

from datetime import datetime, timedelta, timezone

import pytest

from ultron_hub import objectif, reprise
from ultron_hub.common.settings import load_settings

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
OK = {"session": {"percent": 40.0}, "stale": False}


class Faux:
    def __init__(self, vivantes=(), ecran=""):
        self.vivantes, self.ecran, self.lances = set(vivantes), ecran, []

    def tmux(self, argv):
        if argv[0] == "has-session":
            return (0 if argv[2][1:] in self.vivantes else 1), ""
        if argv[0] == "capture-pane":
            return 0, self.ecran
        return 0, "\n".join(self.vivantes)

    def lanceur(self, argv):
        self.lances.append(argv)
        return 1, "Session tmux : ultron-main-02-11-12h00\n"


@pytest.fixture
def st(project):
    return load_settings()


def objet(st, session="essai-x", chantier=None):
    o = objectif.creer(st, "but", ["true"], chantier=chantier)
    objectif.lier(st, o["id"], session)
    objectif._modifier(st, o["id"], lambda x: x.update(etape="étape 2"))
    return o["id"]


def go(st, faux, essai=False, usage=OK, now=NOW):
    return reprise.reprendre(st, essai, tmux=faux.tmux, lanceur=faux.lanceur, usage=lambda: usage, now=now)


def preuves(st, i):
    return [p["texte"] for p in objectif.lire(st, i)["preuves"]]


def test_coupee(st):
    i = objet(st)
    faux = Faux()
    r = go(st, faux)
    assert r["plan"][0]["cause"] == "coupée" and len(faux.lances) == 1
    assert faux.lances[0][-1] == f"Reprends l'objectif {i} (ultron-hub objectif show {i}) à l'étape « étape 2 »."
    assert objectif.lire(st, i)["session"] == "ultron-main-02-11-12h00"
    assert preuves(st, i) == ["reprise : coupée -> ultron-main-02-11-12h00"]


def test_quota_delie_sans_fermer(st):
    i = objet(st)
    faux = Faux(vivantes={"essai-x"}, ecran="You've hit your usage limit")
    r = go(st, faux)
    assert r["plan"][0]["cause"] == "quota" and faux.vivantes == {"essai-x"}
    assert objectif.lire(st, i)["session"] == "ultron-main-02-11-12h00"
    assert preuves(st, i)[0].startswith("reprise : quota -> ")


def test_session_vivante_sans_quota_ignoree(st):
    objet(st)
    faux = Faux(vivantes={"essai-x"}, ecran="tout va bien")
    assert go(st, faux)["plan"] == [] and faux.lances == []


@pytest.mark.parametrize("usage", [{"session": {"percent": 97.0}, "stale": False},
                                   {"session": {"percent": 10.0}, "stale": True}])
def test_quota_pas_revenu(st, usage):
    i = objet(st)
    faux = Faux()
    r = go(st, faux, usage=usage)
    assert r["plan"][0]["action"] == "rien" and faux.lances == [] and preuves(st, i) == []


def test_plafond_3_par_24h(st):
    i = objet(st)
    for h in (20, 15, 10):
        d = (NOW - timedelta(hours=h)).isoformat()
        objectif._modifier(st, i, lambda o, d=d: o["preuves"].append({"le": d, "texte": "reprise : coupée -> x"}))
    faux = Faux()
    r = go(st, faux)
    assert "plafond" in r["plan"][0]["raison"] and faux.lances == []
    # une reprise sortie de la fenêtre de 24 h ne compte plus
    objectif._modifier(st, i, lambda o: o["preuves"].__setitem__(0, {"le": (NOW - timedelta(hours=30)).isoformat(),
                                                                      "texte": "reprise : coupée -> x"}))
    assert go(st, faux)["plan"][0]["action"] == "reprise"


def test_delai_30_min(st):
    i = objet(st)
    objectif._modifier(st, i, lambda o: o["preuves"].append(
        {"le": (NOW - timedelta(minutes=10)).isoformat(), "texte": "reprise : coupée -> x"}))
    faux = Faux()
    assert "30 min" in go(st, faux)["plan"][0]["raison"] and faux.lances == []
    objectif._modifier(st, i, lambda o: o["preuves"].__setitem__(0, {"le": (NOW - timedelta(minutes=31)).isoformat(),
                                                                      "texte": "reprise : coupée -> x"}))
    assert go(st, faux)["plan"][0]["action"] == "reprise"


def test_essai_ne_lance_rien(st, capsys):
    from ultron_hub import cli
    i = objet(st)
    faux = Faux()
    r = go(st, faux, essai=True)
    p = r["plan"][0]
    assert p["action"] == "reprise" and p["commande"][0] == reprise.ULTRON and f"objectif {i}" in p["commande"][-1]
    assert faux.lances == [] and objectif.lire(st, i)["session"] == "essai-x" and preuves(st, i) == []
    assert "reprendrait" in r["resume"]


def test_echec_du_lancement_compte(st):
    i = objet(st)
    faux = Faux()
    faux.lanceur = lambda argv: (1, "")
    r = go(st, faux)
    assert r["plan"][0]["action"] == "echec" and preuves(st, i)[0].startswith("reprise : échec")
    assert objectif.lire(st, i)["session"] == "essai-x"


def test_reprise_auto_n_leve_jamais(st, monkeypatch):
    monkeypatch.setattr(reprise, "reprendre", lambda *a, **k: 1 / 0)
    reprise.reprise_auto(st)


def test_session_fermee_par_l_utilisateur_pas_reprise(st):
    """Todo #538 : ✖ Fermer délie l'objectif ; reprendre ne le voit plus."""
    i = objet(st, session="ultron-03-11-10h00")
    autre = objet(st, session="ultron-03-11-11h00")
    assert objectif.delier_fermee(st, "ultron-03-11-10h00") == [i]
    o = objectif.lire(st, i)
    assert o["session"] is None and preuves(st, i) == ["fermée : par l'utilisateur (ultron-03-11-10h00)"]
    assert objectif.lire(st, autre)["session"] == "ultron-03-11-11h00"
    faux = Faux(vivantes=["ultron-03-11-11h00"])
    assert go(st, faux, essai=True)["plan"] == []
