"""Telegram coupé par défaut (todo #290) : aucun réseau, `_envoyer_telegram`, push et historique simulés."""
import argparse
import json

import pytest

from ultron_hub import cli, envoi


@pytest.fixture
def espion(monkeypatch, tmp_path):
    monkeypatch.delenv(envoi.TELEGRAM_ENV, raising=False)
    monkeypatch.setattr(envoi, "telegram_path", lambda settings=None: tmp_path / "telegram-etat")
    vus = {"telegram": [], "pousse": []}
    monkeypatch.setattr(envoi, "_envoyer_telegram", lambda *a: vus["telegram"].append(a) or envoi.Resultat("ok", "", 1))
    monkeypatch.setattr(envoi, "_pousser", lambda *a, **k: vus["pousse"].append(a))
    return vus


def test_coupe_par_defaut_garde_le_fil(espion):
    res = envoi.envoyer("bonjour", sujet="missions")
    assert res.ok and res.envoyes == 0 and res.detail.startswith("envoyé au Fil")
    assert "coupé" not in res.detail  # todo #447: « coupé » was read as a failure by the sessions
    assert espion["telegram"] == [] and len(espion["pousse"]) == 1


def test_alertes_passent_toujours(espion):
    envoi.envoyer("panne", sujet="alertes")
    envoi.envoyer("panne", cle_alerte="garde:x")
    assert len(espion["telegram"]) == 2


def test_active_puis_coupe(espion):
    envoi.regler_telegram(True)
    assert envoi.telegram_actif()
    envoi.envoyer("salut")
    assert len(espion["telegram"]) == 1
    envoi.regler_telegram(False)
    assert not envoi.telegram_actif()


def test_variable_prioritaire(espion, monkeypatch):
    envoi.regler_telegram(False)
    monkeypatch.setenv(envoi.TELEGRAM_ENV, "on")
    assert envoi.telegram_actif()


def test_cli_telegram(espion, capsys):
    assert cli.main(["telegram", "on"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["data"]["actif"] is True
    cli.main(["telegram", "etat"])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]["actif"] is True
    cli.main(["telegram", "off"])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]["actif"] is False


def test_detail_donne_le_push(monkeypatch, tmp_path):
    monkeypatch.delenv(envoi.TELEGRAM_ENV, raising=False)
    monkeypatch.setattr(envoi, "telegram_path", lambda settings=None: tmp_path / "telegram-etat")
    monkeypatch.setattr(envoi, "_pousser", lambda *a, **k: 2)
    assert envoi.envoyer("x").detail == "envoyé au Fil (push : 2 appareil(s))"
    monkeypatch.setattr(envoi, "_pousser", lambda *a, **k: None)
    assert envoi.envoyer("x").detail == "envoyé au Fil (push absent)"
