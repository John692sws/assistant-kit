"""Todo #201 : boutons des envois. Aucun réseau (`envoi._urlopen` simulé) ; callback_data identiques aux écrans."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from test_cap_last import REVIEW
from ultron_hub import boutons, envoi
from ultron_hub.cap import report

TOKEN = "123456:SECRET-TOKEN_abc"
ROWS = [[("✅ 1", "hub:cap:axs:2027-01-17:1"), ("✅ 2", "hub:cap:axs:2027-01-17:2")]]


class Rep:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"ok": true}'


@pytest.fixture
def direct(monkeypatch):
    monkeypatch.setenv(envoi.MODE_ENV, "direct")
    monkeypatch.setenv(envoi.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("ULTRON_TELEGRAM_CHAT_ID", "42")
    calls = []
    monkeypatch.setattr(envoi, "_urlopen", lambda req, timeout: calls.append(req) or Rep())
    return calls


def test_direct_puts_reply_markup_on_the_text(direct):
    res = envoi.envoyer("revue", boutons=ROWS)
    assert res.ok
    body = json.loads(direct[0].data)
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "✅ 1", "callback_data": "hub:cap:axs:2027-01-17:1"},
                                                         {"text": "✅ 2", "callback_data": "hub:cap:axs:2027-01-17:2"}]]}


def test_direct_without_buttons_has_no_markup(direct):
    envoi.envoyer("bonjour")
    assert "reply_markup" not in json.loads(direct[0].data)


def test_buttons_go_on_the_last_piece_only(direct, tmp_path):
    pic = tmp_path / "a.png"
    pic.write_bytes(b"PNG")
    res = envoi.envoyer("\n".join(["a" * 100] * 120) + f"\nMEDIA:{pic}", boutons=ROWS)
    assert res.ok
    texts = [r for r in direct if r.full_url.endswith("/sendMessage")]
    assert len(texts) >= 3 and all("reply_markup" not in json.loads(r.data) for r in texts[:-1])
    assert "reply_markup" in json.loads(texts[-1].data)
    assert b"reply_markup" not in direct[-1].data  # the attachment has none


@pytest.mark.parametrize("bad", [[[("", "hub:x")]], [[("ok", "x" * 65)]], [[("ok", "")]], [[]]])
def test_bad_buttons_send_nothing(direct, bad):
    res = envoi.envoyer("x", boutons=bad)
    assert not res.ok and direct == []


def test_hermes_mode_sends_text_only(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: seen.append((cmd, k.get("input")))
                        or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert envoi.envoyer("revue", binary="/bin/hermes", boutons=ROWS).ok
    [(cmd, entree)] = seen
    assert cmd == ["/bin/hermes", "send", "-t", "telegram", "revue"] and entree is None
    assert "hub:" not in " ".join(cmd)


# --- the alerts are text: no button left on the cap review nor on the paused veille (todo #463) -----------------------

def test_no_alert_button_factory_is_left():
    assert not [n for n in dir(boutons) if n in ("branche_prete", "cap_cartes", "veille_relancer")]


def test_cap_report_send_gives_text_without_button(monkeypatch):
    monkeypatch.setattr("ultron_hub.esprit.store.record", lambda *a, **k: None)
    seen = []
    settings = SimpleNamespace(todo_notify_target="telegram", hermes_binary=None)
    report.send(settings, REVIEW, sender=lambda t, target, binary, **k: seen.append(k))
    assert "boutons" not in seen[0] and seen[0]["sujet"] == "revue-de-cap"
