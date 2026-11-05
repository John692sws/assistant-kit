"""Un sujet par chantier (todo #203) : aucun réseau, `envoi._urlopen` simulé."""
import io
import json
import urllib.error

import pytest

from tests.test_send import Rep, TOKEN
from ultron_hub import envoi
from ultron_hub.common.settings import load_settings
from ultron_hub.groupe import chantiers

BASE = "chat_id: -100\nsujets:\n  systeme: 11\n  missions: 12\n"


@pytest.fixture
def groupe(project, monkeypatch):
    path = project / "config" / "groupe.yaml"
    path.write_text(BASE)
    monkeypatch.setenv(envoi.MODE_ENV, "direct")
    monkeypatch.setenv(envoi.TOKEN_ENV, TOKEN)
    calls, plan = [], {}

    def fake(req, timeout):
        methode = req.full_url.rsplit("/", 1)[1]
        calls.append((methode, json.loads(req.data)))
        if plan.get(methode):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad", {}, io.BytesIO(
                json.dumps({"ok": False, "description": plan[methode]}).encode()))
        return Rep(json.dumps({"ok": True, "result": {"message_thread_id": 40 + len(calls)}}).encode())
    monkeypatch.setattr(envoi, "_urlopen", fake)
    return path, calls, plan, load_settings()


def test_create_writes_id_and_keeps_rest(groupe):
    path, calls, _, settings = groupe
    res = chantiers.creer(settings, "alpha", "systeme", path=path)
    assert res["status"] == "ok" and calls == [("createForumTopic", {"chat_id": "-100", "name": "🛠 alpha"})]
    assert path.read_text().startswith(BASE) and "chantiers:\n  alpha: 41\n" in path.read_text()
    assert envoi.lire_groupe(path)["sujets"] == {"systeme": 11, "missions": 12}
    assert envoi.lire_groupe(path)["chantiers"] == {"alpha": 41}


def test_archive_closes_and_reopen_reopens(groupe):
    path, calls, _, settings = groupe
    chantiers.creer(settings, "alpha", None, path=path)
    assert calls[0][1]["name"] == "❔ alpha"
    assert chantiers.fermer(settings, "alpha", path=path)["status"] == "ok"
    assert calls[1] == ("closeForumTopic", {"chat_id": "-100", "message_thread_id": 41})
    assert chantiers.creer(settings, "alpha", None, path=path)["status"] == "ok"
    assert calls[2][0] == "reopenForumTopic" and len(calls) == 3
    assert chantiers.fermer(settings, "inconnu", path=path)["status"] == "skipped"


def test_telegram_error_never_raises(groupe):
    path, calls, plan, settings = groupe
    plan["createForumTopic"] = "Not enough rights"
    res = chantiers.creer(settings, "alpha", None, path=path)
    assert res["status"] == "error" and "chantiers:" not in path.read_text()


def test_skipped_in_hermes_mode_or_without_group(groupe, monkeypatch, project):
    path, calls, _, settings = groupe
    monkeypatch.setenv(envoi.MODE_ENV, "hermes")
    assert chantiers.creer(settings, "alpha", None, path=path)["status"] == "skipped"
    monkeypatch.setenv(envoi.MODE_ENV, "direct")
    path.unlink()
    assert chantiers.creer(settings, "alpha", None, path=path)["status"] == "skipped" and calls == []


def test_destination_chantier_then_domaine(groupe):
    path, _, _, _ = groupe
    chantiers.creer(load_settings(), "alpha", "systeme", path=path)
    assert envoi.destination_sujet("Système", path, chantier="alpha") == ("-100", 41)
    assert envoi.destination_sujet("Système", path, chantier="autre") == ("-100", 11)
    assert envoi.destination_sujet(None, path, chantier="alpha") == ("-100", 41)
    assert envoi.destination_sujet("Système", path) == ("-100", 11)
    assert envoi.destination_sujet(None, path, chantier="autre") is None


def test_chantier_name_does_not_shadow_domain_key(groupe):
    path, _, _, _ = groupe
    path.write_text(BASE + "chantiers:\n  missions: 99\n")
    assert envoi.destination_sujet("missions", path) == ("-100", 12)
    assert envoi.destination_sujet(None, path, chantier="missions") == ("-100", 99)

