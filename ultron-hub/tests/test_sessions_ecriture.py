"""Todo #265: writing into an ultron session (fake tmux runner, fake ultron-hub, throwaway close.key; no real tmux or claude)."""

import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from cli_contract import check_argv
from hermes_plugin import close_key
from test_dashboard_actions import (PHONE_ID, SELF_ID, STRANGER_ID, confirm, dash, fake_cli, fresh_cache,  # noqa: F401
                                    prepare, request, session)
from ultron_hub import cli
from ultron_hub.dashboard import catalogue
from ultron_hub.escouade.mcp_ultron import value_hash
from ultron_hub.sessions import envoi
from ultron_hub.sessions.journal import SessionError
from ultron_hub.todo import closing

ROOT = Path(__file__).parents[1]
NOM = "ultron-19-10-15h38"
DEVICE = "telephone-test"


_AGE = iter(range(100))  # each token a different second back: two tokens for one target are two proofs


def token(target, action="session"):
    return closing.sign_dashboard(closing.load_key(), DEVICE, action, target, int(time.time()) - next(_AGE) % 60)


class FakeTmux:
    """Records every tmux argv; `sessions` are the ones `has-session` knows."""

    def __init__(self, sessions=(NOM,), fail_send=False):
        self.calls, self.sessions, self.fail_send = [], set(sessions), fail_send

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and "shell" not in kw
        self.calls.append(argv)
        rc = 0
        if argv[1] == "has-session":
            rc = 0 if argv[3].removeprefix("=") in self.sessions else 1
        elif argv[1] == "send-keys" and self.fail_send:
            rc = 1
        return subprocess.CompletedProcess(argv, rc, "", "")

    def sent(self):
        return [a for a in self.calls if a[1] == "send-keys"]


@pytest.fixture
def key():
    close_key.load_or_create()  # throwaway key (conftest close_key_path)


def env_for(target):
    return {closing.TOKEN_ENV: token(target)}


# -- envoi: nominal argv -------------------------------------------------------------------------------------------

def test_write_sends_literal_text_then_enter(tmp_path, key):
    tmux, text = FakeTmux(), "-n regarde $(ls) ; les branches"
    out = envoi.ecrire(NOM, text, tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash(text)}"))
    assert out == {"session": NOM, "longueur": len(text), "by": "dashboard:" + DEVICE}
    assert tmux.calls[0] == ["tmux", "has-session", "-t", f"={NOM}"]
    assert tmux.sent() == [["tmux", "send-keys", "-t", f"={NOM}:", "-l", "--", text],  # screen unreadable: plain prompt
                           ["tmux", "send-keys", "-t", f"={NOM}:", "Enter"]]
    state = (tmp_path / "sessions" / "envois.json").read_text()
    assert "branches" not in state  # fingerprints only, never the text


def test_choose_and_interrupt_argv(tmp_path, key):
    tmux = FakeTmux()
    envoi.choisir(NOM, "2", tmp_path, run=tmux, env=env_for(f"{NOM}:2"))
    envoi.choisir(NOM, "bas", tmp_path, run=tmux, env=env_for(f"{NOM}:bas"))
    envoi.interrompre(NOM, run=tmux)
    assert tmux.sent() == [["tmux", "send-keys", "-t", f"={NOM}:", "-l", "--", "2"],
                           ["tmux", "send-keys", "-t", f"={NOM}:", "Down"],
                           ["tmux", "send-keys", "-t", f"={NOM}:", "Escape"]]


def test_terminal_shortcuts_argv(tmp_path, key):
    """Todo #419: Ctrl-B is ONE C-b (send-keys skips tmux's prefix), send-now is C-x then C-s, Escape by name."""
    tmux = FakeTmux()
    for touche in ("ctrl-b", "ctrl-x-s", "echap"):
        envoi.choisir(NOM, touche, tmp_path, run=tmux, env=env_for(f"{NOM}:{touche}"))
    assert tmux.sent() == [["tmux", "send-keys", "-t", f"={NOM}:", "C-b"],
                           ["tmux", "send-keys", "-t", f"={NOM}:", "C-x", "C-s"],
                           ["tmux", "send-keys", "-t", f"={NOM}:", "Escape"]]


def test_terminal_shortcuts_in_catalogue():
    from ultron_hub.dashboard import catalogue
    field = next(c for c in catalogue.PAR_ID["session-choisir"].champs if c.nom == "touche")
    values = {c.code for c in field.choix}
    assert {"ctrl-b", "ctrl-x-s", "echap"} <= values and all(len(v) <= field.max for v in values)
    for touche in ("ctrl-b", "ctrl-x-s", "echap"):
        catalogue.valider("session-choisir", {"nom": NOM, "touche": touche})


# -- envoi: refusals (nothing sent) ----------------------------------------------------------------------------------

@pytest.mark.parametrize("nom", ["autre", "ultron", "ultron-19-10-15h3", "ultron-19-10-15h38; rm", "=ultron-19-10-15h38",
                                 "ultron-19-10-15h38:1", "-t", ""])
def test_session_outside_ultron_refused(tmp_path, key, nom):
    tmux = FakeTmux(sessions={nom})
    with pytest.raises(SessionError, match="nom de session"):
        envoi.ecrire(nom, "salut", tmp_path, run=tmux, env=env_for(f"x:{value_hash('salut')}"))
    with pytest.raises(SessionError, match="nom de session"):
        envoi.interrompre(nom, run=tmux)
    with pytest.raises(SessionError, match="nom de session"):
        envoi.choisir(nom, "1", tmp_path, run=tmux, env=env_for("x:1"))
    assert tmux.calls == []


def test_absent_session_refused(tmp_path, key):
    tmux = FakeTmux(sessions=())
    with pytest.raises(SessionError, match="absente"):
        envoi.ecrire(NOM, "salut", tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash('salut')}"))
    with pytest.raises(SessionError, match="absente"):
        envoi.interrompre(NOM, run=tmux)
    assert tmux.sent() == []


@pytest.mark.parametrize("text,why", [("", "vide"), ("   ", "vide"), ("x" * (envoi.TEXT_MAX + 1), "trop long"),
                                      ("a\nb", "contrôle"), ("a\rb", "contrôle"), ("a\x1b[2Jb", "contrôle"),
                                      ("a\tb", "contrôle"), ("a\x00", "contrôle"), ("a\x7f", "contrôle"),
                                      ("a\x9bb", "contrôle"), ("a b", "contrôle")])
def test_bad_text_refused(tmp_path, key, text, why):
    tmux = FakeTmux()
    with pytest.raises(SessionError, match=why):
        envoi.ecrire(NOM, text, tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash(text)}"))
    assert tmux.calls == []


def test_max_length_and_unicode_accepted(tmp_path, key):
    text = "é" * (envoi.TEXT_MAX - 2) + "👍"
    tmux = FakeTmux()
    envoi.ecrire(NOM, text, tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash(text)}"))
    assert tmux.sent()[0][-1] == text


def test_double_send_refused_then_allowed_later(tmp_path, key):
    tmux, now = FakeTmux(), [1000.0]
    target = f"{NOM}:{value_hash('salut')}"
    envoi.ecrire(NOM, "salut", tmp_path, run=tmux, env=env_for(target), clock=lambda: now[0])
    now[0] += envoi.DEDUP_S - 1
    with pytest.raises(SessionError, match="déjà envoyé"):
        envoi.ecrire(NOM, "salut", tmp_path, run=tmux, env=env_for(target), clock=lambda: now[0])
    envoi.ecrire(NOM, "autre", tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash('autre')}"), clock=lambda: now[0])
    assert len(tmux.sent()) == 4
    now[0] += envoi.DEDUP_S + 1
    envoi.ecrire(NOM, "salut", tmp_path, run=tmux, env=env_for(target), clock=lambda: now[0])
    assert len(tmux.sent()) == 6


def test_unsigned_wrong_target_or_replay_refused(tmp_path, key):
    tmux = FakeTmux()
    with pytest.raises(closing.CloseRefused, match="réservé"):
        envoi.ecrire(NOM, "salut", tmp_path, run=tmux, env={})
    with pytest.raises(closing.CloseRefused, match="invalide"):  # signed for another text
        envoi.ecrire(NOM, "salut", tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash('autre')}"))
    with pytest.raises(closing.CloseRefused, match="invalide"):  # signed for another session
        envoi.choisir(NOM, "1", tmp_path, run=tmux, env=env_for("ultron-19-10-09h00:1"))
    with pytest.raises(closing.CloseRefused):  # a ✅ done proof is not a session proof
        envoi.choisir(NOM, "1", tmp_path, run=tmux, env={closing.TOKEN_ENV: token(f"{NOM}:1", "done")})
    assert tmux.calls == []
    env = env_for(f"{NOM}:1")
    envoi.choisir(NOM, "1", tmp_path, run=tmux, env=env)
    with pytest.raises(closing.CloseRefused, match="déjà utilisé"):
        envoi.choisir(NOM, "1", tmp_path, run=tmux, env=env)
    assert len(tmux.sent()) == 1


@pytest.mark.parametrize("touche", ["0", "10", "Escape", "C-c", "-l", "Enter", ""])
def test_unknown_key_refused(tmp_path, key, touche):
    with pytest.raises(SessionError, match="touche"):
        envoi.choisir(NOM, touche, tmp_path, run=FakeTmux(), env=env_for(f"{NOM}:1"))


def test_tmux_failure_reported(tmp_path, key):
    with pytest.raises(SessionError, match="refusé par tmux"):
        envoi.interrompre(NOM, run=FakeTmux(fail_send=True))


# -- CLI: stdin, JSON on stdout, the trace without the text ----------------------------------------------------------------

def test_cli_reads_stdin_and_logs_no_text(project, key, monkeypatch, capsys, caplog):
    tmux = FakeTmux()
    monkeypatch.setattr(subprocess, "run", tmux)
    secret = "texte-confidentiel-xyz"
    monkeypatch.setenv(closing.TOKEN_ENV, token(f"{NOM}:{value_hash(secret)}"))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(secret + "\n"))
    caplog.set_level("INFO")
    assert cli.main(["session", "ecrire", NOM, "--texte-stdin"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["data"]["longueur"] == len(secret)
    assert tmux.sent()[0][-1] == secret
    assert secret not in caplog.text and f"session={NOM} longueur={len(secret)} by=dashboard:{DEVICE}" in caplog.text
    assert secret not in json.dumps(out)


def test_cli_refusal_is_json(project, key, monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", FakeTmux())
    assert cli.main(["session", "interrompre", "autre"]) != 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "nom de session" in out["error"]
    check_argv(["session", "ecrire", NOM, "--texte-stdin"])
    check_argv(["session", "choisir", NOM, "entree"])


# -- closing: the new signed action, the same on both sides --------------------------------------------------------------

def test_session_is_a_dashboard_action_on_both_sides():
    assert "session" in closing.ACTIONS and close_key.ACTIONS == closing.ACTIONS
    assert "session" in closing.DASHBOARD_ACTIONS and close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS


# -- dashboard: catalogue entries, end to end through the server ------------------------------------------------------

def lire(d, sid, body, **kw):
    """Todo #283: writing and keys are `ouvert` entries, sent through the reading POST, without a sheet."""
    return request(d, "POST", "/api/action/lire", body, cookie=sid, **kw)


def test_catalogue_entries():
    ecrire, choisir = (catalogue.PAR_ID[i] for i in ("session-ecrire", "session-choisir"))
    assert (ecrire.type, ecrire.signe, ecrire.stdin, ecrire.ouvert) == ("signee", "session", "texte", True)
    assert (choisir.type, choisir.signe, choisir.ouvert) == ("signee", "session", True)
    assert [c.code for c in choisir.champ("touche").choix] == list(envoi.KEYS)
    assert all("{texte}" not in p for p in (*ecrire.journal, *ecrire.sur, *ecrire.quoi, *ecrire.argv))
    assert "session-interrompre" not in catalogue.PAR_ID  # todo #283: Escape is a signed key of session-choisir


def test_dashboard_write_is_signed_without_sheet_and_journal_has_no_text(dash):
    sid = session(dash)
    text = "Regarde   l'état\ndes branches"
    assert prepare(dash, sid, {"action": "session-ecrire", "nom": NOM, "texte": text})[0] == 409  # no sheet any more
    code, res, _ = lire(dash, sid, {"action": "session-ecrire", "nom": NOM, "texte": text})
    cleaned = "Regarde l'état des branches"  # whitespace collapsed: no newline reaches tmux
    assert code == 200 and res["ok"] and "l'état" not in json.dumps(res)
    [call] = dash.calls()
    assert call["argv"] == ["session", "ecrire", NOM, "--texte-stdin"] and call["stdin"] == cleaned
    check_argv(call["argv"])
    assert closing.check(call["env"][closing.TOKEN_ENV], "session", f"{NOM}:{value_hash(cleaned)}")[1] == "dashboard:" + DEVICE
    assert dash.journal == [f"✍️ {len(cleaned)} caractères envoyés à la session {NOM} depuis le dashboard (by=dashboard:{DEVICE})"]


@pytest.mark.parametrize("touche", list(envoi.KEYS))
def test_dashboard_every_key_is_signed_without_sheet(dash, touche):
    sid = session(dash)
    code, res, _ = lire(dash, sid, {"action": "session-choisir", "nom": NOM, "touche": touche})
    assert code == 200 and res["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["session", "choisir", NOM, touche]
    check_argv(call["argv"])
    closing.check(call["env"][closing.TOKEN_ENV], "session", f"{NOM}:{touche}")
    assert dash.journal == [f"⌨️ touche {touche} envoyée à la session {NOM} depuis le dashboard (by=dashboard:{DEVICE})"]


def test_two_quick_presses_of_the_same_key_are_two_proofs(dash):
    sid = session(dash)
    for _ in range(3):  # same second (the test clock does not move): each token gets its own timestamp
        assert lire(dash, sid, {"action": "session-choisir", "nom": NOM, "touche": "bas"})[1]["ok"]
    tokens = [c["env"][closing.TOKEN_ENV] for c in dash.calls()]
    assert len(set(tokens)) == 3
    for t in tokens:
        closing.check(t, "session", f"{NOM}:bas")


def test_a_burst_of_the_same_key_is_refused_before_tokens_go_stale(dash):
    sid = session(dash)
    oks = [lire(dash, sid, {"action": "session-choisir", "nom": NOM, "touche": "1"})[0] == 200
           for _ in range(closing.TOKEN_TTL_S // 2 + 2)]
    assert oks.count(True) == closing.TOKEN_TTL_S // 2 and not oks[-1]


@pytest.mark.parametrize("body", [{"nom": "autre", "texte": "x"}, {"nom": NOM + "; rm", "texte": "x"},
                                  {"nom": NOM, "texte": ""}, {"nom": NOM, "texte": "x" * (envoi.TEXT_MAX + 1)}])
def test_dashboard_refuses_bad_values(dash, body):
    sid = session(dash)
    code, out, _ = lire(dash, sid, {"action": "session-ecrire", **body})
    assert code == 409 and not out.get("ok") and dash.calls() == []
    for bad in ("Escape", "C-c", "0", "-l"):
        assert lire(dash, sid, {"action": "session-choisir", "nom": NOM, "touche": bad})[0] == 409
    assert dash.calls() == [] and dash.journal == []


@pytest.mark.parametrize("node", [STRANGER_ID, SELF_ID, None])
def test_dashboard_unknown_device_runs_nothing(dash, node):
    sid = session(dash)
    dash.node = node
    assert lire(dash, sid, {"action": "session-ecrire", "nom": NOM, "texte": "salut"})[0] == 403
    assert lire(dash, sid, {"action": "session-choisir", "nom": NOM, "touche": "echap"})[0] == 403
    dash.node = PHONE_ID
    assert dash.calls() == []


def test_dashboard_open_send_keeps_origin_and_cookie_checks(dash):
    sid = session(dash)
    body = {"action": "session-choisir", "nom": NOM, "touche": "entree"}
    assert lire(dash, None, body)[0] == 403
    assert lire(dash, sid, body, origin="http://evil.example")[0] == 403
    assert dash.calls() == []


# -- todo #279: quick replies, sent through the same signed entry ------------------------------------------------------

QUICK = [["Oui.", "Oui"], ["Non.", "Non"], ["Vas-y, suis tes recommandations.", "Suis tes recos"]]


def test_quick_replies_and_commands_are_public():
    entry = next(a for a in catalogue.public() if a["id"] == "session-ecrire")
    texte = next(c for c in entry["champs"] if c["nom"] == "texte")
    assert texte["rapides"] == QUICK and entry["ouvert"] is True
    assert "/clear" in texte["suggestions"] and all(c.startswith("/") for c in texte["suggestions"])


@pytest.mark.parametrize("texte", [q[0] for q in QUICK])
def test_quick_reply_is_a_signed_write(dash, texte):
    sid = session(dash)
    code, res, _ = lire(dash, sid, {"action": "session-ecrire", "nom": NOM, "texte": texte})
    assert code == 200 and res["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["session", "ecrire", NOM, "--texte-stdin"] and call["stdin"] == texte
    closing.check(call["env"][closing.TOKEN_ENV], "session", f"{NOM}:{value_hash(texte)}")


def test_verifier_refuses_a_bad_quick_reply():
    ecrire = catalogue.PAR_ID["session-ecrire"]
    texte = ecrire.champ("texte")
    for bad in (replace(texte, rapides=(catalogue.Choix("x" * (envoi.TEXT_MAX + 1), "Long"),)),
                replace(texte, rapides=(catalogue.Choix("", "Vide"),))):
        with pytest.raises(catalogue.CatalogueError):
            catalogue.verifier((replace(ecrire, champs=(ecrire.champs[0], bad)),))
    touche = catalogue.PAR_ID["session-choisir"].champ("touche")
    choisir = catalogue.PAR_ID["session-choisir"]
    bad = replace(touche, rapides=(catalogue.Choix("1", "Un"),))
    with pytest.raises(catalogue.CatalogueError):
        catalogue.verifier((replace(choisir, champs=(choisir.champs[0], bad)),))


def test_restart_after_merge_is_declared():
    plan = json.loads((ROOT / "deploy/user/apres-fusion/todo-265.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]


# -- todo #308: a text typed while a choice question is on screen -------------------------------------------------------

ASK = ["{c0}1. Option A", "     la première", "{c1}2. Option B", "     la seconde", "{c2}3. Type something.", "Enter to select"]


class MenuTmux(FakeTmux):
    """A fake pane showing an AskUserQuestion menu: Down/Up move the cursor, typed text is kept only on the free-text row
    (anywhere else the menu eats it, as Claude Code does: this is the garbling of the 20/10 report)."""

    def __init__(self, cursor=0, rows=ASK, move=True):
        super().__init__()
        self.cursor, self.rows, self.move, self.typed = cursor, rows, move, ""

    def __call__(self, argv, **kw):
        proc = super().__call__(argv, **kw)
        if argv[1] == "capture-pane":
            marks = {f"c{i}": ("❯ " if i == self.cursor else "  ") for i in range(3)}
            return subprocess.CompletedProcess(argv, 0, "\n".join(r.format(**marks) for r in self.rows) + "\n", "")
        if argv[1] == "display-message":
            return subprocess.CompletedProcess(argv, 0, f"80 {len(self.rows)}\n", "")
        if argv[1] == "send-keys":
            for key in argv[3:]:
                if key in ("Down", "Up") and self.move:
                    self.cursor = max(0, min(2, self.cursor + (1 if key == "Down" else -1)))
            if "-l" in argv and self.cursor == 2:
                self.typed += argv[-1]
        return proc


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(envoi, "PAUSE_S", 0)


def test_text_on_a_choice_question_goes_to_the_free_text_row_first(tmp_path, key):
    tmux, text = MenuTmux(cursor=0), "31113 ok, on garde la deuxième"
    envoi.ecrire(NOM, text, tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash(text)}"))
    keys = [a[4:] for a in tmux.sent()]
    assert keys == [["Down", "Down"], ["-l", "--", text], ["Enter"]]  # cursor first, then the text, then Enter
    assert tmux.typed == text  # nothing was eaten by the menu


def test_text_already_on_the_free_text_row_needs_no_move(tmp_path, key):
    tmux, text = MenuTmux(cursor=2), "ok"
    envoi.ecrire(NOM, text, tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash(text)}"))
    assert [a[4:] for a in tmux.sent()] == [["-l", "--", text], ["Enter"]]


def test_text_refused_when_the_cursor_does_not_arrive(tmp_path, key):
    tmux, text = MenuTmux(cursor=0, move=False), "ok"
    with pytest.raises(SessionError, match="curseur"):
        envoi.ecrire(NOM, text, tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash(text)}"))
    assert not any("-l" in a for a in tmux.sent())  # no text typed into the menu


def test_text_refused_on_a_menu_without_free_text_row(tmp_path, key):
    rows = ["{c0}1. Yes", "{c1}2. No", "{c2}3. Maybe"]
    tmux, text = MenuTmux(rows=rows), "31113ok"
    with pytest.raises(SessionError, match="sans champ libre"):
        envoi.ecrire(NOM, text, tmp_path, run=tmux, env=env_for(f"{NOM}:{value_hash(text)}"))
    assert tmux.sent() == []


def test_free_text_gap_reads_the_screen():
    from ultron_hub.sessions import ecran
    assert ecran.free_text_gap(["❯ 1. A", "  2. Other", "  3. Chat about this"]) == 1
    assert ecran.free_text_gap(["  1. A", "❯ 2. Type something."]) == 0
    assert ecran.free_text_gap(["  1. A", "  2. B", "❯ 3. Autre"]) == 0
    assert ecran.free_text_gap(["❯ 1. Yes", "  2. No"]) is None
    with pytest.raises(ValueError):
        ecran.free_text_gap(["● hello", "❯ "])
