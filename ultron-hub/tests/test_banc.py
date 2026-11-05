"""Todo #513: historique du banc."""

import argparse
import json

import pytest

from ultron_hub import banc
from ultron_hub.common.errors import InvalidRequestError


class S:
    def __init__(self, d):
        self.var_dir = d


@pytest.mark.parametrize("kw", [
    dict(reussis=-1, sur=10), dict(reussis=11, sur=10), dict(reussis=0, sur=51), dict(reussis=0, sur=0),
    dict(reussis=1, sur=10, note="x" * 201), dict(reussis=1, sur=10, note="a\nb"),
    dict(reussis=1, sur=10, jour="2026-02-30"), dict(reussis=1, sur=10, jour="2/11"), dict(reussis=1, sur=10, cout=-1),
])
def test_refus(tmp_path, kw):
    with pytest.raises(InvalidRequestError):
        banc.noter(S(tmp_path), **kw)
    assert not (tmp_path / "banc" / "historique.jsonl").exists()


def test_ajout_relecture_tendance(tmp_path):
    s = S(tmp_path)
    assert banc.historique(s) == {"passes": [], "texte": ""}
    banc.noter(s, 7, 10, 120, "relecteur neutre", "2026-11-02")
    banc.noter(s, 8, 10, None, "", "2026-11-09")
    d = banc.historique(s)
    assert [p["reussis"] for p in d["passes"]] == [7, 8]
    assert d["texte"] == "02/11 : 7/10, 120 pts (relecteur neutre) ; 09/11 : 8/10"
    assert banc.tendance(d["passes"]) == "7/10 puis 8/10"
    assert len((tmp_path / "banc" / "historique.jsonl").read_text().splitlines()) == 2


def test_ligne_abimee_ignoree(tmp_path):
    s = S(tmp_path)
    banc.noter(s, 7, 10, jour="2026-11-02")
    with open(banc.historique_path(s), "a") as f:
        f.write("pas du json\n" + json.dumps({"date": "x", "reussis": 99, "sur": 10}) + "\n")
    assert len(banc.lire(s)) == 1


def test_handle(tmp_path):
    s = S(tmp_path)
    r = banc.handle(argparse.Namespace(banc_command="noter", reussis=11, sur=10, cout=None, note="", date=None), s)
    assert r["status"] == "error"
    banc.handle(argparse.Namespace(banc_command="noter", reussis=7, sur=10, cout=None, note="", date="2026-11-02"), s)
    r = banc.handle(argparse.Namespace(banc_command="historique"), s)
    assert r["status"] == "ok" and r["summary"] == "7/10"
