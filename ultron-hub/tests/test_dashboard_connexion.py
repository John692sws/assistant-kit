"""Todo #267: « session expirée » after every merge. Controls of that failure (one failure = one control):
- the session cookie is signed with the dashboard's own key: it survives a NEW server/Actions (= a restart);
- a tampered, other-device or expired cookie is refused (403 + `session: renouveler`, the page renews and re-prepares);
- a merge runs in the background (its own unit), its state file written before a restart is read back after it.
No network beyond 127.0.0.1, whois simulated, fake `ultron-hub`, throwaway keys (conftest)."""

import json
import os
import stat
import sys
import threading

import pytest

from hermes_plugin import close_key
from ultron_hub import cli, merge_progress
from ultron_hub.common.settings import load_settings
from ultron_hub.dashboard import actions, auth, fond, server
from ultron_hub.dashboard import session as session_cookie
from ultron_hub.todo import closing

from test_dashboard_actions import DEVICES, MAC_ID, PHONE_ID, SELF_ID, Clock, confirm, prepare, request, session

SQUAD = "agent-ultron"


@pytest.fixture
def cli_answer(tmp_path):
    """Fake `ultron-hub`: answers the envelope written in answer.json (ok by default), records argv and env."""
    answer, calls = tmp_path / "answer.json", tmp_path / "calls.jsonl"
    answer.write_text(json.dumps({"status": "ok", "summary": "fusionnée", "data": {"text": "#7 fusionnée"}, "error": None}))
    script = tmp_path / "fake-ultron-hub"
    script.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ)}}) + '\\n')\n"
        f"print(open({str(answer)!r}).read())\n")
    script.chmod(0o755)
    return [str(script)], answer, calls


class Dash:
    """One dashboard process: a server and its Actions. `restart()` = a new one, nothing kept in memory."""

    def __init__(self, cli_argv, clock):
        self.cli, self.clock, self.node, self.journal = cli_argv, clock, PHONE_ID, []
        self.srv = None
        self.restart()

    def restart(self):
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
        self.acts = actions.Actions(cli=self.cli, clock=self.clock, journal=self.journal.append)
        gate = auth.Gate(DEVICES, SELF_ID, "100.64.0.1", whois=lambda ip: self.node)
        self.srv = server.make_server("127.0.0.1", 0, gate, acts=self.acts)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.host = f"127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def dash(project, cli_answer):
    close_key.load_or_create()
    d = Dash(cli_answer[0], Clock())
    yield d
    d.close()


# -- 1. the session survives a restart -------------------------------------------------------------------------------

def test_session_survives_a_restart_of_the_dashboard(dash, no_real_dashboard_session_key):
    sid = session(dash)
    assert prepare(dash, sid, {"action": "chantier-archiver", "sujet": SQUAD})[0] == 200
    dash.restart()  # memory emptied: pending tokens are gone, the session is not
    code, out, _ = prepare(dash, sid, {"action": "chantier-archiver", "sujet": SQUAD})
    assert code == 200 and out["ok"] and out["jeton"]
    key = no_real_dashboard_session_key
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600 and key.name != closing.KEY_PATH.name
    assert key.read_bytes().strip() not in sid.encode()  # nothing secret in the cookie


def test_cookie_tampered_other_device_or_expired_is_refused(dash):
    sid = session(dash)
    v, dev, exp, nonce, mac = sid.split(".")
    other = session_cookie._b64("appareil-test")
    forged = [f"{v}.{dev}.{int(exp) + 3600}.{nonce}.{mac}",            # later expiry, same MAC
              f"{v}.{other}.{exp}.{nonce}.{mac}",                       # another device, same MAC
              f"{v}.{dev}.{exp}.{nonce}.{mac[:-1]}{'0' if mac[-1] != '0' else '1'}",  # MAC altered
              session_cookie.issue(b"x" * 64, "telephone-test", int(exp)),  # signed with another key
              "", "v1.x", sid + "x"]
    for cookie in forged:
        code, out, _ = prepare(dash, cookie, {"action": "chantier-archiver", "sujet": SQUAD})
        assert code == 403 and out["session"] == "renouveler" and "rien n'a été lancé" in out["erreur"], cookie
    dash.node = MAC_ID  # a valid cookie of the phone, presented by the Mac
    code, out, _ = prepare(dash, sid, {"action": "chantier-archiver", "sujet": SQUAD})
    assert code == 403 and out["session"] == "renouveler"
    dash.node = PHONE_ID
    dash.clock.now += actions.SESSION_TTL_S + 1
    assert prepare(dash, sid, {"action": "chantier-archiver", "sujet": SQUAD})[0] == 403
    code, _, set_cookie = request(dash, "GET", "/", cookie=sid)  # the page renews it with GET /
    fresh = set_cookie.split(";")[0].split("=", 1)[1]
    assert code == 200 and prepare(dash, fresh, {"action": "chantier-archiver", "sujet": SQUAD})[0] == 200


def test_session_key_with_loose_mode_is_refused(no_real_dashboard_session_key):
    key = session_cookie.load_key()
    assert session_cookie.load_key() == key and len(key) >= session_cookie.KEY_BYTES
    os.chmod(no_real_dashboard_session_key, 0o644)
    with pytest.raises(session_cookie.SessionKeyError):
        session_cookie.load_key()
    acts = actions.Actions()
    assert acts.new_session("telephone-test") is None and not acts.session_ok("v1.x", "telephone-test")


# -- 2. merges in the background -------------------------------------------------------------------------------------

def _start_merge(dash, monkeypatch):
    """Confirm 🔀 #7; the unit is 'started' but has not run yet (the launch is recorded, nothing runs)."""
    launched = {}
    monkeypatch.setattr(actions.Actions, "_launch",
                        lambda self, settings, job, extra, delay: launched.update(job=job, extra=extra, delay=delay))
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "merge", "id": "7"})
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 200 and res["ok"] and res["suivi"] == launched["job"] and fond.JOB_RE.fullmatch(res["suivi"])
    return res["suivi"], launched


def _suivi(capsys, job):
    assert cli.main(["dashboard", "suivi", job]) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]


def test_the_optional_sentence_of_a_video_travels_in_the_job_not_in_argv(dash, monkeypatch):
    """Todo #440: a background action may carry an optional stdin: written in the job file (0600), given to the CLI by the runner."""
    seen = []
    monkeypatch.setattr(actions.Actions, "_run", lambda self, argv, extra, *, stdin=None, timeout=0:
                        seen.append((argv, stdin)) or {"status": "ok", "summary": "", "data": {"text": "ok"}})
    launched = []
    monkeypatch.setattr(actions.Actions, "_livrer", lambda *a, **k: None, raising=False)
    sid = session(dash)
    body = {"action": "video-tout", "lien": "https://youtu.be/abcdefghijk", "session": "ultron-fba-20-10-09h12",
            "phrase": "pour le chantier"}
    _, out, _ = prepare(dash, sid, body)
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 200 and res["ok"] and res["suivi"]
    state = fond.read(load_settings(), res["suivi"])
    assert state["stdin"] == "pour le chantier" and "phrase" not in state["args"]
    assert seen[0][0] == ["video", "analyse-lien", "https://youtu.be/abcdefghijk", "--session=ultron-fba-20-10-09h12",
                          "--phrase-stdin"]
    assert seen[0][1] == "pour le chantier" and "pour le chantier" not in " ".join(seen[0][0])
    path = fond.directory(load_settings()) / f"{res['suivi']}.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_merge_state_written_before_a_restart_is_read_after_it(dash, monkeypatch, capsys, cli_answer):
    job, launched = _start_merge(dash, monkeypatch)
    settings = load_settings()
    state = fond.read(settings, job)
    assert state["etat"] == "en_cours" and state["action"] == "merge" and state["args"] == {"id": "7"}
    assert closing.TOKEN_ENV not in json.dumps(state) and launched["extra"][closing.TOKEN_ENV] not in json.dumps(state)
    assert launched["delay"] == actions.MERGE_TIMEOUT_S
    monkeypatch.setenv(merge_progress.SUIVI_ENV, job)  # what the runner gives the CLI
    progress = merge_progress.Progress(settings, [7])
    progress.step(7, "tests")
    dash.restart()  # e.g. an after-merge action restarts the dashboard
    data = _suivi(capsys, job)
    assert not data["fini"] and [e["etat"] for e in data["etapes"]] == ["fait", "en_cours", "a_venir", "a_venir", "a_venir"]
    assert data["etapes"][1]["detail"] == "#7 (1/1)"
    progress.done(7)
    progress.restart()
    assert [e["etat"] for e in _suivi(capsys, job)["etapes"]][3] == "en_cours"
    # the unit finishes after the restart: same command, signed token from its env, answer recorded
    out = fond.run_job(settings, job, actions.Actions(cli=cli_answer[0], journal=dash.journal.append),
                       launched["extra"][closing.TOKEN_ENV])
    assert out["ok"] and out["texte"] == "#7 fusionnée"
    [call] = [json.loads(line) for line in cli_answer[2].read_text().splitlines()]
    assert call["argv"] == ["branch", "merge", "7"] and call["env"][merge_progress.SUIVI_ENV] == job
    assert closing.check(call["env"][closing.TOKEN_ENV], "merge", "7")[1] == "dashboard:telephone-test"
    data = _suivi(capsys, job)
    assert data["fini"] and data["resultat"]["ok"] and {e["etat"] for e in data["etapes"]} == {"fait"}
    assert "by=dashboard:telephone-test" in dash.journal[-1]
    with pytest.raises(SystemExit):  # a finished job never runs twice
        fond.run_job(settings, job, actions.Actions(cli=cli_answer[0]), None)


def test_refused_merge_shows_the_step_and_the_reason(dash, monkeypatch, capsys, cli_answer):
    cli_answer[1].write_text(json.dumps({"status": "error", "summary": "", "data": None,
                                         "error": "les tests échouent sur todo-7/x"}))
    job, launched = _start_merge(dash, monkeypatch)
    settings = load_settings()
    monkeypatch.setenv(merge_progress.SUIVI_ENV, job)
    merge_progress.Progress(settings, [7]).step(7, "tests")
    fond.run_job(settings, job, actions.Actions(cli=cli_answer[0]), launched["extra"][closing.TOKEN_ENV])
    data = _suivi(capsys, job)
    assert data["fini"] and not data["resultat"]["ok"]
    assert [e["etat"] for e in data["etapes"]] == ["fait", "refus", "a_venir", "a_venir", "refus"]
    assert data["etapes"][-1] == {"nom": "Refusé", "etat": "refus", "detail": ""}
    assert data["resultat"]["erreur"] == "les tests échouent sur todo-7/x"


def test_lot_steps_follow_the_progress_of_this_job_only(project, monkeypatch):
    settings = load_settings()
    job = fond.new_id(0)
    fond.write(settings, job, {"etat": "en_cours", "action": "fusion-lot", "args": {}, "device": "d", "lance": 1e12,
                               "delai": 60})
    other = merge_progress.Progress(settings, [81, 82])  # a merge from Telegram: not this job's
    other.step(81, "fusion")
    assert [e["etat"] for e in fond.suivi(settings, job, now=1e12)["etapes"]][0] == "en_cours"
    monkeypatch.setenv(merge_progress.SUIVI_ENV, job)
    p = merge_progress.Progress(settings, [81, 82])
    p.step(81, "essai")
    p.done(81)
    p.step(82, "essai")
    steps = fond.suivi(settings, job, now=1e12)["etapes"]
    assert steps[1] == {"nom": "Tests", "etat": "en_cours", "detail": "#82 (2/2)"}
    p.done(82)
    p.actions()
    assert fond.suivi(settings, job, now=1e12)["etapes"][3]["etat"] == "en_cours"
    late = fond.suivi(settings, job, now=1e12 + 60 + fond.GRACE_S + 1)  # the unit never answered
    assert late["fini"] and "interrompue" in late["resultat"]["erreur"]
    assert fond.suivi(settings, "../x") is None and fond.read(settings, "20261019-120000-00000000") is None


def test_launch_starts_its_own_unit_without_waiting_and_without_the_token_in_argv(project, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return __import__("subprocess").CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr("subprocess.run", fake_run)
    fond.launch(load_settings(), "20261019-120000-0a1b2c3d", {closing.TOKEN_ENV: "secret-token", "HOME": "/h"}, 420)
    cmd = seen["cmd"]
    assert cmd[:3] == ["systemd-run", "--user", "--unit"] and cmd[3] == "ultron-dashboard-fond-20261019-120000-0a1b2c3d"
    assert "--wait" not in cmd and f"RuntimeMaxSec={420 + fond.GRACE_S}" in cmd
    assert f"--setenv={closing.TOKEN_ENV}" in cmd and "secret-token" not in " ".join(cmd)
    assert seen["env"][closing.TOKEN_ENV] == "secret-token"
    assert cmd[-4:] == ["-P", "-m", "ultron_hub.dashboard.fond", "20261019-120000-0a1b2c3d"]


def test_launch_failure_is_reported_and_nothing_runs(dash, monkeypatch, cli_answer):
    def boom(self, settings, job, extra, delay):
        raise RuntimeError("systemd-run a échoué : pas de bus")
    monkeypatch.setattr(actions.Actions, "_launch", boom)
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "fusion-lot", "ids": "81,82", "empreinte": "0123456789abcdef"})
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 200 and not res["ok"] and "systemd-run" in res["erreur"] and "suivi" not in res
    assert not cli_answer[2].exists()
