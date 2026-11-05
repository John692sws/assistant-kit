"""Todo #364: `chantier conseil`, the session to launch, by plain rules (no AI, every source best effort)."""

import json

from ultron_hub import cli
from ultron_hub.chantier import conseil, core
from ultron_hub.dashboard import catalogue


def c(sujet, domaine, n, groupe="📥"):
    return {"sujet": sujet, "domaine": domaine, "groupe": groupe, "todos": [{"id": i, "titre": "t"} for i in range(n)]}


def run(chantiers, panne=None):
    src = core.Sources(prompts=None, boites=None)
    return conseil.conseiller(
        None, src, tableau=lambda s, r: {"chantiers": chantiers, "avertissements": ["w"]},
        panne=lambda s, w: panne)


def test_panne_wins_over_everything():
    out = run([c("fba", "business", 3)], panne="disque / plein à 95 %")
    assert (out["chantier"], out["regle"]) == ("", "a-panne")
    assert out["raison"].startswith("panne en cours : disque")


def test_usage_before_tooling_even_with_fewer_todos():
    out = run([c("agent-ultron", "systeme", 9), c("fba", "business", 1)])
    assert (out["chantier"], out["regle"]) == ("fba", "b-usage")


def test_tooling_with_most_todos():
    out = run([c("a", "systeme", 2), c("b", "systeme", 5), c("vide", "systeme", 0)])
    assert (out["chantier"], out["regle"]) == ("b", "c-outillage")
    assert "outillage" in out["raison"]


def test_paused_chantier_never_advised():
    out = run([c("fba", "business", 3, groupe="💤"), c("ultron", "systeme", 2)])
    assert (out["chantier"], out["regle"]) == ("ultron", "c-outillage")


def test_reason_short_french_no_em_dash():
    out = run([c("a", "systeme", 1)], panne="x — " + "y" * 200)
    assert len(out["raison"]) <= 80 and "—" not in out["raison"]


def test_sources_missing_gives_main_and_warnings():
    def boom(s, r):
        raise OSError("nope")
    src = core.Sources(prompts=None, boites=None)
    out = conseil.conseiller(None, src, tableau=boom, panne=lambda s, w: w.append("santé illisible : X"))
    assert (out["chantier"], out["regle"]) == ("", "d-rien")
    assert any("illisibles" in w for w in out["avertissements"]) and "santé illisible : X" in out["avertissements"]


def test_cli_prints_json_only(capsys, tmp_path):
    rc = cli.main(["chantier", "conseil", "--dossier", str(tmp_path / "p"), "--boites-dir", str(tmp_path / "b")])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["command"] == "chantier.conseil"
    assert set(data["data"]) >= {"chantier", "raison", "regle"}


def test_catalogue_reading_only():
    a = catalogue.PAR_ID["conseil"]
    assert a.type == "lecture" and a.argv == ("chantier", "conseil")


def test_alerte_de_reflexe_automatique_nest_pas_une_panne(tmp_path, monkeypatch):
    """Todo #472: merge-check-orphelin se règle seul (seul: true) -> info, pas une panne."""
    from types import SimpleNamespace
    from ultron_hub.health import alertes
    st = alertes.state_path(tmp_path)
    st.parent.mkdir(parents=True)
    st.write_text(json.dumps({"actives": {"reflexe:merge-check-orphelin": "worktree orphelin"}}))
    monkeypatch.setattr(conseil, "_seuls", lambda: {"reflexe:merge-check-orphelin"})
    w = []
    assert conseil._panne(SimpleNamespace(root=tmp_path), w) is None
    assert any("réglée seule" in x for x in w)


def test_vraie_alerte_reste_une_panne(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ultron_hub.health import alertes
    st = alertes.state_path(tmp_path)
    st.parent.mkdir(parents=True)
    st.write_text(json.dumps({"actives": {"reflexe:merge-check-orphelin": "orphelin", "disque:/": "disque plein"}}))
    monkeypatch.setattr(conseil, "_seuls", lambda: {"reflexe:merge-check-orphelin"})
    assert conseil._panne(SimpleNamespace(root=tmp_path), []) == "disque plein"


def test_seuls_vient_de_la_config():
    assert conseil._seuls() == {"reflexe:merge-check-orphelin"}
