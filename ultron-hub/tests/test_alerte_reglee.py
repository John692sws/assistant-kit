"""Alerte réglée sur place (todo #219) : enregistrement, modification, message introuvable, mode hermes."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ultron_hub import envoi
from ultron_hub.veille import core

NOW = datetime(2026, 10, 18, 14, 5, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(var_dir=tmp_path / "var", envoi_mode="direct", envoi_chat_id="42")


@pytest.fixture(autouse=True)
def direct(monkeypatch):
    monkeypatch.setattr(envoi, "mode", lambda settings=None: "direct")


def saved(settings):
    return json.loads(envoi.alertes_path(settings).read_text())


def res(mid=7):
    return envoi.Resultat(message_id=mid, chat="42")


class Appel:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def __call__(self, methode, champs, timeout_s=15):
        self.calls.append((methode, champs))
        if self.exc:
            raise self.exc
        return {"ok": True}


def test_enregistrer_garde_le_message(settings):
    assert envoi.enregistrer("a:1", res(), "⚠️ titre\nsuite", settings, NOW)
    info = saved(settings)["a:1"]
    assert info["chat_id"] == "42" and info["message_id"] == 7 and info["texte"] == "⚠️ titre"
    assert info["envoyee_a"].startswith("2026-10-18")


def test_envoyer_avec_cle_enregistre(settings, monkeypatch):
    monkeypatch.setenv(envoi.TOKEN_ENV, "tok")
    monkeypatch.setattr(envoi, "_direct", lambda *a, **k: res(9))
    envoi.envoyer("⚠️ alerte", settings=settings, cle_alerte="k")
    assert saved(settings)["k"]["message_id"] == 9
    envoi.envoyer("sans clé", settings=settings)
    assert list(saved(settings)) == ["k"]


def test_reglee_modifie_puis_oublie(settings):
    envoi.enregistrer("k", res(), "⚠️ titre", settings, NOW)
    appel = Appel()
    assert envoi.marquer_reglee("k", settings, NOW, appel) == "modifiee"
    methode, champs = appel.calls[0]
    assert methode == "editMessageText" and champs["message_id"] == 7 and champs["chat_id"] == "42"
    assert champs["text"].startswith("✅ réglée à ") and "⚠️ titre" in champs["text"]
    assert saved(settings) == {}
    assert envoi.marquer_reglee("k", settings, NOW, appel) == "inconnue" and len(appel.calls) == 1


@pytest.mark.parametrize("msg", ["Bad Request: message to edit not found", "message can't be edited"])
def test_message_introuvable_est_oublie(settings, msg):
    envoi.enregistrer("k", res(), "x", settings, NOW)
    assert envoi.marquer_reglee("k", settings, NOW, Appel(RuntimeError(msg))) == "oubliee"
    assert saved(settings) == {}


def test_autre_erreur_garde_la_cle(settings):
    envoi.enregistrer("k", res(), "x", settings, NOW)
    assert envoi.marquer_reglee("k", settings, NOW, Appel(RuntimeError("HTTP 500"))) == "erreur"
    assert "k" in saved(settings)


def test_mode_hermes_ne_change_rien(settings, monkeypatch):
    monkeypatch.setattr(envoi, "mode", lambda settings=None: "hermes")
    assert not envoi.enregistrer("k", res(), "x", settings, NOW)
    assert not envoi.alertes_path(settings).exists()
    appel = Appel()
    assert envoi.marquer_reglee("k", settings, NOW, appel) == "inconnue" and not appel.calls


def test_oublier_retire_la_cle_sans_appel(settings, monkeypatch):
    envoi.enregistrer("k", res(), "x", settings, NOW)
    envoi.enregistrer("autre", res(8), "y", settings, NOW)
    monkeypatch.setattr(envoi, "appeler", Appel(AssertionError("no call expected")))
    assert envoi.oublier("k", settings)
    assert list(saved(settings)) == ["autre"]
    assert not envoi.oublier("k", settings)


def test_veille_resume_regle_l_alerte_de_pause(settings, monkeypatch):
    envoi.enregistrer(core.alerte_cle("places"), res(), "⚠️ Veille en pause", settings, NOW)
    appel = Appel()
    monkeypatch.setattr(envoi, "appeler", appel)
    assert envoi.marquer_reglee(core.alerte_cle("places"), settings) == "modifiee"
    assert appel.calls
