"""Point d'envoi Telegram commun (todo #194) : aucun réseau, `envoi._urlopen` simulé."""
import io
import json
import logging
import subprocess
import urllib.error

import pytest

from ultron_hub import envoi
from ultron_hub.common.errors import ConfigError

TOKEN = "123456:SECRET-TOKEN_abc"


class Rep:
    def __init__(self, body=b'{"ok": true}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


@pytest.fixture
def direct(monkeypatch):
    monkeypatch.setenv(envoi.MODE_ENV, "direct")
    monkeypatch.setenv(envoi.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("ULTRON_TELEGRAM_CHAT_ID", "42")
    calls = []

    def fake(req, timeout):
        calls.append(req)
        return Rep()
    monkeypatch.setattr(envoi, "_urlopen", fake)
    return calls


def test_default_mode(monkeypatch):
    from ultron_hub.common.settings import Settings, load_settings
    monkeypatch.delenv(envoi.MODE_ENV, raising=False)
    monkeypatch.setattr(envoi, "_mode_fichier", lambda: "")
    assert Settings.__dataclass_fields__["envoi_mode"].default == "hermes"  # code fallback
    assert envoi.mode(load_settings()) == "direct"  # config/settings.yaml since the audit of 28/10 (todo #457)


def test_mode_fichier_hors_git_et_priorite(project, monkeypatch):
    (project / "config" / "settings.yaml").write_text("envoi:\n  mode: hermes\n")
    (project / "var").mkdir(exist_ok=True)
    (project / "var" / "envoi-mode").write_text("direct\n")
    assert envoi.mode() == "direct"  # fichier > settings
    monkeypatch.setenv(envoi.MODE_ENV, "hermes")
    assert envoi.mode() == "hermes"  # variable d'environnement > fichier


def test_settings_mode_and_chat(project):
    (project / "config" / "settings.yaml").write_text("envoi:\n  mode: direct\n  chat_id: 77\n")
    assert envoi.mode() == "direct"
    from ultron_hub.common.settings import load_settings
    assert load_settings().envoi_chat_id == "77"
    (project / "config" / "settings.yaml").write_text("envoi:\n  mode: telepathie\n")
    with pytest.raises(ConfigError):
        load_settings()


def test_direct_text(direct):
    res = envoi.envoyer("bonjour")
    assert res.ok and res.envoyes == 1
    [req] = direct
    assert req.full_url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert json.loads(req.data) == {"chat_id": "42", "text": "bonjour"}


def test_direct_target_with_chat(direct):
    envoi.envoyer("x", target="telegram:-100")
    assert json.loads(direct[0].data)["chat_id"] == "-100"


def test_direct_splits_long_messages(direct):
    text = "\n".join(["a" * 100] * 120)  # ~12 100 caractères
    res = envoi.envoyer(text)
    assert res.ok and len(direct) == res.envoyes >= 3
    assert all(len(json.loads(r.data)["text"]) <= 4096 for r in direct)


def test_direct_media_attachment(direct, tmp_path):
    pic, doc = tmp_path / "a.png", tmp_path / "b.md"
    pic.write_bytes(b"PNGDATA")
    doc.write_text("hello")
    res = envoi.envoyer(f"voici\nMEDIA:{pic}\nMEDIA:{doc}")
    assert res.ok and res.envoyes == 3
    assert [r.full_url.rsplit("/", 1)[1] for r in direct] == ["sendMessage", "sendPhoto", "sendDocument"]
    assert b"PNGDATA" in direct[1].data and b'name="photo"' in direct[1].data
    assert "multipart/form-data" in direct[1].headers["Content-type"]
    assert b'name="document"' in direct[2].data


def test_direct_media_only_and_missing(direct, tmp_path):
    res = envoi.envoyer(f"MEDIA:{tmp_path}/nope.png")
    assert not res.ok and "introuvable" in res.detail and direct == []


def test_direct_errors_are_statuses(direct, monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad", {}, io.BytesIO(b'{"ok":false,"description":"chat not found"}'))
    monkeypatch.setattr(envoi, "_urlopen", boom)
    res = envoi.envoyer("x")
    assert res.status == "error" and "chat not found" in res.detail and TOKEN not in res.detail

    def down(req, timeout):
        raise urllib.error.URLError(f"https://api.telegram.org/bot{TOKEN}/sendMessage unreachable")
    monkeypatch.setattr(envoi, "_urlopen", down)
    res = envoi.envoyer("x")
    assert res.status == "error" and TOKEN not in res.detail and "SECRET" not in res.detail


def test_direct_telegram_refuses(direct, monkeypatch):
    monkeypatch.setattr(envoi, "_urlopen", lambda req, timeout: Rep(b'{"ok": false, "description": "nope"}'))
    assert not envoi.envoyer("x").ok


def _faux_hermes(monkeypatch, rc=0):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: seen.append(cmd)
                        or subprocess.CompletedProcess(cmd, rc, "", "" if rc == 0 else "boom"))
    return seen


def test_direct_missing_token_falls_back_to_hermes(direct, monkeypatch):
    # todo #215 : seul ultron-bot reçoit le jeton ; timers et missions passent par hermes send (texte seul)
    monkeypatch.delenv(envoi.TOKEN_ENV)
    seen = _faux_hermes(monkeypatch)
    res = envoi.envoyer("alerte", binary="/bin/hermes", sujet="alertes", boutons=[[("ok", "hub:x")]])
    assert res.ok and seen == [["/bin/hermes", "send", "-t", "telegram", "alerte"]] and direct == []
    seen = _faux_hermes(monkeypatch, rc=1)
    res = envoi.envoyer("alerte", binary="/bin/hermes")
    assert not res.ok and "TELEGRAM_BOT_TOKEN" in res.detail and "repli hermes" in res.detail


def test_direct_missing_chat(direct, monkeypatch):
    monkeypatch.delenv("ULTRON_TELEGRAM_CHAT_ID")
    assert "destination" in envoi.envoyer("x").detail
    assert direct == []


def test_token_never_logged(direct, monkeypatch, caplog):
    def down(req, timeout):
        raise RuntimeError(f"échec {req.full_url}")
    monkeypatch.setattr(envoi, "_urlopen", down)
    with caplog.at_level(logging.DEBUG):
        res = envoi.envoyer("x")
    assert not res.ok
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(res.to_dict()) and "SECRET" not in res.detail


def test_hermes_mode_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(envoi.subprocess, "run", lambda cmd, **k: calls.append((cmd, k.get("input"))) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    assert envoi.envoyer("salut", binary="/h").ok
    assert envoi.envoyer("long", binary="/h", style="stdin").ok
    assert calls == [(["/h", "send", "-t", "telegram", "salut"], None),
                     (["/h", "send", "--to", "telegram", "--quiet", "--file", "-"], "long")]


def test_hermes_mode_failures_are_statuses(monkeypatch):
    def timeout(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(envoi.subprocess, "run", timeout)
    assert "timed out" in envoi.envoyer("x", binary="/h").detail
    monkeypatch.setattr(envoi.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 3, "", "boom"))
    assert "exit 3" in envoi.envoyer("x", binary="/h").detail
    monkeypatch.setattr(envoi.subprocess, "run", lambda cmd, **k: (_ for _ in ()).throw(FileNotFoundError("/h")))
    assert not envoi.envoyer("x", binary="/h").ok


def test_cli_send_direct(direct, capsys):
    from ultron_hub import cli
    code = cli.main(["send", "-t", "telegram", "bonjour"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "ok" and out["data"]["sent"] == 1 and TOKEN not in json.dumps(out)


def test_cli_send_error_is_json(monkeypatch, capsys):
    monkeypatch.setenv(envoi.MODE_ENV, "direct")
    _faux_hermes(monkeypatch, rc=1)
    from ultron_hub import cli
    code = cli.main(["send", "x"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["status"] == "error" and "TELEGRAM_BOT_TOKEN" in out["error"]


@pytest.mark.real_send
def test_service_send_message_uses_direct_mode(direct):
    from ultron_hub.todo import service
    service.send_message("digest", "telegram", None)
    assert json.loads(direct[0].data)["text"] == "digest"


@pytest.mark.real_send
def test_service_send_message_raises_on_direct_error(direct, monkeypatch):
    from ultron_hub.common.errors import UltronHubError
    from ultron_hub.todo import service
    monkeypatch.delenv(envoi.TOKEN_ENV)
    _faux_hermes(monkeypatch, rc=1)
    with pytest.raises(UltronHubError):
        service.send_message("digest", "telegram", None)


def test_restart_gateway_direct_uses_the_door(monkeypatch):
    from ultron_hub import branches
    calls = []
    monkeypatch.setenv(envoi.MODE_ENV, "direct")
    monkeypatch.setattr(branches.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(branches.shutil, "which", lambda n: "/x/ultron-hub")
    branches.restart_gateway()
    cmd = calls[0]
    assert "--setenv=TELEGRAM_BOT_TOKEN" in cmd and "/x/ultron-hub" in cmd and "-c" in cmd
    # todo #197 : en mode direct, c'est ultron-bot qui redémarre, jamais hermes-gateway
    assert "restart ultron-bot" in " ".join(cmd) and "hermes-gateway" not in " ".join(cmd)


def _groupe(project, texte="chat_id: -1001\nsujets:\n  missions: 7\n  alertes: 9\n  revue-de-cap: 11\n"):
    (project / "config" / "groupe.yaml").write_text(texte)


def test_sujet_direct_thread_and_group_chat(direct, project, tmp_path):
    _groupe(project)
    assert envoi.envoyer("a", sujet="Revue de cap").ok
    body = json.loads(direct[0].data)
    assert body["chat_id"] == "-1001" and body["message_thread_id"] == 11
    f = tmp_path / "x.txt"
    f.write_text("d")
    envoi.envoyer(f"MEDIA:{f}", sujet="missions")
    assert b'name="message_thread_id"\r\n\r\n7' in direct[1].data


@pytest.mark.parametrize("texte", [None, "chat_id: -1001\nsujets:\n  alertes: 9\n", "n'importe quoi", "sujets:\n  missions: 7\n"])
def test_sujet_fallback_private(direct, project, texte):
    if texte is not None:
        _groupe(project, texte)
    assert envoi.envoyer("a", sujet="missions").ok
    body = json.loads(direct[0].data)
    assert body["chat_id"] == "42" and "message_thread_id" not in body


def test_sujet_hermes_mode_ignored(project, monkeypatch):
    _groupe(project)
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert envoi.envoyer("a", binary="/bin/hermes", sujet="missions").ok
    assert "-1001" not in " ".join(seen[0])
