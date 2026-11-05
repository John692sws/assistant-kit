"""Dashboard actions (todos #131, #132): device, Host, Origin, session cookie, one-time token, confirmation, then the same CLI.

No network beyond 127.0.0.1, whois simulated, a fake `ultron-hub` that records its argv and env; throwaway close.key.
"""

import http.client
import json
import sys
import threading
import time

import pytest

from cli_contract import check_argv
from hermes_plugin import close_key
from ultron_hub.dashboard import actions, auth, server, views

from ultron_hub.todo import closing

SELF_ID, PHONE_ID, MAC_ID, STRANGER_ID = "nSELF", "nPHONE", "nMAC", "nSTRANGER"
DEVICES = (auth.Device("telephone-test", PHONE_ID), auth.Device("appareil-test", MAC_ID),
           auth.Device("serveur-test", SELF_ID))
SQUAD = "agent-ultron"


@pytest.fixture(autouse=True)
def fresh_cache():
    views.clear_cache()
    yield
    views.clear_cache()


@pytest.fixture
def fake_cli(tmp_path):
    """Fake `ultron-hub`: appends {argv, env} to calls.jsonl, answers an ok envelope."""
    calls = tmp_path / "calls.jsonl"
    script = tmp_path / "fake-ultron-hub"
    script.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ), 'stdin': sys.stdin.read()}}) + '\\n')\n"
        "print(json.dumps({'status': 'ok', 'summary': 'fait : ' + ' '.join(sys.argv[1:3]), 'data': {'id': 'x-1'},"
        " 'error': None}))\n")
    script.chmod(0o755)

    def read():
        return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return [str(script)], read


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def dash(project, fake_cli, monkeypatch):
    """A running dashboard on 127.0.0.1; `dash.node` is who the simulated whois says is calling."""
    close_key.load_or_create()  # throwaway key (conftest close_key_path)
    cli, read = fake_cli
    journal: list[str] = []
    clock = Clock()
    acts = actions.Actions(cli=cli, clock=clock, journal=journal.append)

    class D:
        node = PHONE_ID
    d = D()
    gate = auth.Gate(DEVICES, SELF_ID, "100.64.0.1", whois=lambda ip: d.node)
    srv = server.make_server("127.0.0.1", 0, gate, acts=acts)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d.port, d.calls, d.journal, d.clock, d.acts = srv.server_address[1], read, journal, clock, acts
    d.host = f"127.0.0.1:{d.port}"
    yield d
    srv.shutdown()
    srv.server_close()


def request(d, method, path, body=None, *, cookie=None, origin="same", host=None, ctype="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=10)
    headers = {"Host": host or d.host}
    if origin == "same":
        headers["Origin"] = f"http://{host or d.host}"
    elif origin:
        headers["Origin"] = origin
    if cookie:
        headers["Cookie"] = f"{server.COOKIE}={cookie}"
    data = None
    if body is not None:
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        headers["Content-Type"] = ctype
    conn.request(method, path, body=data, headers=headers)
    r = conn.getresponse()
    raw = r.read()
    out = (r.status, json.loads(raw) if raw and r.getheader("Content-Type", "").startswith("application/json") else raw,
           r.getheader("Set-Cookie"))
    conn.close()
    return out


def session(d):
    code, _, set_cookie = request(d, "GET", "/")
    assert code == 200 and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
    return set_cookie.split(";")[0].split("=", 1)[1]


def prepare(d, sid, body):
    return request(d, "POST", "/api/action/preparer", body, cookie=sid)


def confirm(d, sid, jeton, confirme=True):
    return request(d, "POST", "/api/action/confirmer", {"jeton": jeton, "confirme": confirme}, cookie=sid)


@pytest.fixture
def no_signing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("signed although refused")
    monkeypatch.setattr(closing, "sign_dashboard", boom)


# -- the two allowed actions -----------------------------------------------------------------------------------

def test_archive_runs_the_same_cli_unsigned_after_confirmation(dash):
    sid = session(dash)
    code, out, _ = prepare(dash, sid, {"action": "chantier-archiver", "sujet": SQUAD})
    assert code == 200 and out["ok"] and out["sur_quoi"] == SQUAD and out["expire_s"] == actions.CSRF_TTL_S
    assert dash.calls() == []  # preparing runs nothing
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 200 and res["ok"] and "fait" in res["resume"]
    [call] = dash.calls()
    assert call["argv"] == ["chantier", "archiver", SQUAD] and closing.TOKEN_ENV not in call["env"]
    check_argv(call["argv"])
    assert set(call["env"]) <= {"HOME", "PATH", "LANG", "XDG_RUNTIME_DIR", "PYTHONPATH", "ULTRON_HUB_ROOT"}
    assert dash.journal == [f"🗄 chantier {SQUAD} archivé depuis le dashboard (by=dashboard:telephone-test)"]


def test_refused_caller_launches_nothing(dash, no_signing):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "done", "id": "7"})
    for node in (STRANGER_ID, SELF_ID, ""):  # unknown device, the server itself (agents), whois silent
        dash.node = node
        assert prepare(dash, sid, {"action": "chantier-archiver", "sujet": SQUAD})[0] == 403
        assert confirm(dash, sid, out["jeton"])[0] == 403
    assert dash.calls() == [] and dash.journal == []


def test_token_bound_to_the_device_and_session(dash, no_signing):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "done", "id": "7"})
    dash.node = MAC_ID  # another allowed device, with its own session, cannot spend the phone's token
    mac = session(dash)
    code, res, _ = confirm(dash, mac, out["jeton"])
    assert code == 409 and "autre appareil" in res["erreur"]
    assert confirm(dash, sid, out["jeton"])[0] in (403, 409)  # spent anyway, and the phone's cookie is not the Mac's
    assert dash.calls() == []


def test_csrf_token_replayed_is_refused(dash):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "chantier-archiver", "sujet": SQUAD})
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 409 and "déjà utilisé" in res["erreur"]
    assert len(dash.calls()) == 1


def test_csrf_token_expired_is_refused(dash, no_signing):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "done", "id": "7"})
    dash.clock.now += actions.CSRF_TTL_S + 1
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 409 and "périmé" in res["erreur"] and dash.calls() == []


@pytest.mark.parametrize("confirme", [False, None, "true", 1])
def test_without_confirmation_nothing_runs_and_the_token_is_spent(dash, no_signing, confirme):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "done", "id": "7"})
    code, res, _ = confirm(dash, sid, out["jeton"], confirme)
    assert code == 200 and res["ok"] is False and res.get("annule")
    assert confirm(dash, sid, out["jeton"])[0] == 409
    assert dash.calls() == []


@pytest.mark.parametrize("origin", ["http://evil.example", "null", None, "http://127.0.0.1:1", "https://{host}"])
def test_foreign_origin_is_refused(dash, no_signing, origin):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "done", "id": "7"})
    origin = origin.format(host=dash.host) if origin else origin
    code, res, _ = request(dash, "POST", "/api/action/confirmer", {"jeton": out["jeton"], "confirme": True},
                           cookie=sid, origin=origin)
    assert code == 403 and "origine" in res["erreur"]
    assert request(dash, "POST", "/api/action/preparer", {"action": "chantier-archiver", "sujet": SQUAD}, cookie=sid, origin=origin)[0] == 403
    assert dash.calls() == []


def test_foreign_host_is_refused_even_for_reads(dash):
    """DNS rebinding: evil.example resolving to the server would be same-origin with itself; its Host is not ours."""
    evil = f"evil.example:{dash.port}"
    assert request(dash, "GET", "/", host=evil)[0] == 403
    assert request(dash, "GET", "/api/state", host=evil)[0] == 403
    sid = session(dash)
    assert request(dash, "POST", "/api/action/preparer", {"action": "chantier-archiver", "sujet": SQUAD}, cookie=sid, host=evil)[0] == 403


def test_cookie_json_and_shape_are_required(dash, no_signing):
    sid = session(dash)
    assert prepare(dash, None, {"action": "chantier-archiver", "sujet": SQUAD})[0] == 403          # no session cookie
    assert prepare(dash, "forged", {"action": "chantier-archiver", "sujet": SQUAD})[0] == 403      # unknown cookie
    assert request(dash, "POST", "/api/action/preparer", b"action=stop", cookie=sid,
                   ctype="application/x-www-form-urlencoded")[0] == 400           # a plain HTML form
    assert request(dash, "POST", "/api/action/preparer", b"[1]", cookie=sid)[0] == 400
    for body in ({"action": "chantier-archiver", "sujet": "../x"}, {"action": "done", "id": "x"},
                 {"action": "done", "id": "-1"}, {"action": "cancel", "id": "1"},
                 {"action": "done", "id": "0"}, {"action": "merge", "id": "1;rm"}, {"action": "done", "id": "-1"},
                 {"action": "reponse", "question": "Q1;x", "texte": "a"}, {"action": "reponse", "question": "Q1"},
                 {"action": "reponse", "question": "Q1", "type": "choix", "texte": "a"},
                 {"action": "reponse", "question": "Q1", "texte": "a" * 2001},
                 {"action": "remarque", "chantier": "../x", "texte": "a"}, {"action": "remarque", "chantier": "fba"},
                 {"action": "todo", "titre": "  "}, {"action": "todo", "titre": "a", "chantier": "Bad Name"},
                 {"action": "todo", "titre": "a", "priorite": "urgente"}, {"action": "todo", "titre": "a", "echeance": "hier"},
                 {"action": "todo", "titre": "a", "categorie": "--x"}, {"action": "ranger-appliquer", "empreinte": "zz"},
                 {"action": "ranger-appliquer"}):
        assert prepare(dash, sid, body)[0] == 409, body
    assert request(dash, "PUT", "/api/action/confirmer", {}, cookie=sid)[0] == 405
    assert dash.calls() == []


def test_session_cookie_expires(dash, no_signing):
    sid = session(dash)
    dash.clock.now += actions.SESSION_TTL_S + 1
    code, res, _ = prepare(dash, sid, {"action": "chantier-archiver", "sujet": SQUAD})
    assert code == 403 and "session" in res["erreur"]


# -- the dashboard proof in closing.py ---------------------------------------------------------------------------

def test_plugin_and_cli_sign_alike_for_the_dashboard():
    key = b"k" * 64
    assert close_key.dashboard_token("telephone-test", "done", "a", key=key, now=1000) == \
        closing.sign_dashboard(key, "telephone-test", "done", "a", 1000)
    assert close_key.DASHBOARD_ACTIONS == closing.DASHBOARD_ACTIONS
    assert close_key.ACTIONS == closing.ACTIONS and "dashboard" not in closing.ACTIONS  # no message clash


def test_dashboard_token_checks():
    key = b"k" * 64
    now = time.time()
    good = closing.sign_dashboard(key, "telephone-test", "done", "a", int(now))
    assert closing.check(good, "done", "a", key=key)[1] == "dashboard:telephone-test"
    forged_device = good.replace("telephone-test", "appareil-test", 1)
    for token, action, target in ((forged_device, "done", "a"), (good, "done", "b"),
                                  (closing.sign_dashboard(b"x" * 64, "telephone-test", "done", "a", int(now)), "done", "a"),
                                  (f"{int(now)}.{'0' * 64}", "done", "a")):
        with pytest.raises(closing.CloseRefused):
            closing.check(token, action, target, key=key)
    with pytest.raises(closing.CloseRefused, match="expiré"):
        closing.check(closing.sign_dashboard(key, "telephone-test", "done", "a", int(now) - 3600), "done", "a", key=key)
    with pytest.raises(closing.CloseRefused):  # signed for another action
        closing.check(good, "merge", "a", key=key)
    with pytest.raises(closing.CloseRefused, match="dashboard"):  # actions outside DASHBOARD_ACTIONS: never
        closing.check(good.replace("done", "root"), "root", "a", key=key)
    with pytest.raises(closing.CloseRefused):
        closing.sign_dashboard(key, "telephone-test", "root", "1", int(now))
    with pytest.raises(closing.CloseRefused):
        closing.sign_dashboard(key, "Bad.Device", "done", "a", int(now))
    assert closing.check(close_key.token("done", "a", key=key), "done", "a", key=key)[1] == "telegram-button"


@pytest.mark.parametrize("todo", [131, 132])
def test_restart_after_merge_is_declared(todo):
    from pathlib import Path
    plan = json.loads((Path(__file__).parents[1] / f"deploy/user/apres-fusion/todo-{todo}.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]


# -- todo #132: ✅ done, 🔀 merge, ❓ answer (signed), ➕ remark (sealed as Telegram), ➕ todo (unsigned) -----------------

def run_action(d, body):
    sid, before = session(d), len(d.calls())
    code, out, _ = prepare(d, sid, body)
    assert code == 200 and out["ok"], out
    assert len(d.calls()) == before  # preparing runs nothing
    code, res, _ = confirm(d, sid, out["jeton"])
    assert code == 200 and res["ok"], res
    [call] = d.calls()[before:]
    check_argv(call["argv"])
    assert set(call["env"]) <= {"HOME", "PATH", "LANG", "XDG_RUNTIME_DIR", "PYTHONPATH", "ULTRON_HUB_ROOT", closing.TOKEN_ENV,
                                "ULTRON_HUB_SUIVI"}  # the background job of a merge (todo #267)
    return out, res, call


@pytest.mark.parametrize("action,argv", [("done", ["todo", "done", "7"]), ("merge", ["branch", "merge", "7"])])
def test_done_and_merge_are_signed_with_the_dashboard_proof(dash, monkeypatch, action, argv):
    seen = {}
    real_run = actions.Actions._run

    def spy(self, argv_, extra, **kw):
        seen.update(kw)
        return real_run(self, argv_, extra, **kw)
    monkeypatch.setattr(actions.Actions, "_run", spy)
    out, res, call = run_action(dash, {"action": action, "id": "7"})
    assert out["sur_quoi"] == "#7" and "signé" in out["quoi"]
    assert call["argv"] == argv  # the same command as the plugin (todo_flow, merge_flow)
    proof, by = closing.check(call["env"][closing.TOKEN_ENV], action, "7")
    assert by == "dashboard:telephone-test" and len(proof) == 16
    with pytest.raises(closing.CloseRefused):  # bound to this todo and this action
        closing.check(call["env"][closing.TOKEN_ENV], action, "8")
    assert seen["timeout"] == (actions.MERGE_TIMEOUT_S if action == "merge" else actions.RUN_TIMEOUT_S)
    assert "by=dashboard:telephone-test" in dash.journal[0]


def test_todo_add_is_unsigned(dash):
    _, res, call = run_action(dash, {"action": "todo", "titre": "-Relancer   le fournisseur", "notes": "--force\nvoir mail",
                                     "chantier": "fba"})
    assert call["argv"] == ["todo", "add", "--title=-Relancer le fournisseur", "--truncate", "--source", "dashboard",
                            "--notes=--force\nvoir mail", "--chantier=fba", "--priority=normal", "--due=none"]  # values can never become options
    assert closing.TOKEN_ENV not in call["env"] and res["id"] == "x-1"
    _, _, call = run_action(dash, {"action": "todo", "titre": "Payer", "priorite": "high", "echeance": "tomorrow",
                                   "categorie": "travail"})  # todo #243: priority, due date and category like Telegram
    assert call["argv"] == ["todo", "add", "--title=Payer", "--truncate", "--source", "dashboard", "--priority=high",
                            "--due=tomorrow", "--category=travail"]


@pytest.mark.parametrize("body", [{"action": "done", "id": "7"}, {"action": "merge", "id": "7"},
                                  {"action": "todo", "titre": "x"}])
def test_new_actions_refused_for_the_server_and_strangers(dash, no_signing, body):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, body)
    for node in (STRANGER_ID, SELF_ID, ""):
        dash.node = node
        assert prepare(dash, sid, body)[0] == 403
        assert confirm(dash, sid, out["jeton"])[0] == 403
    dash.node = PHONE_ID
    code, res, _ = confirm(dash, sid, out["jeton"], confirme=False)  # never confirmed: nothing runs, token spent
    assert code == 200 and res.get("annule")
    assert confirm(dash, sid, out["jeton"])[0] == 409
    _, out, _ = prepare(dash, sid, body)
    dash.clock.now += actions.CSRF_TTL_S + 1
    assert "périmé" in confirm(dash, sid, out["jeton"])[1]["erreur"]
    assert dash.calls() == [] and dash.journal == []


@pytest.mark.parametrize("action", sorted(set(closing.ACTIONS) - set(closing.DASHBOARD_ACTIONS)))
def test_dashboard_proof_refused_for_actions_outside_the_list(action):
    key = b"k" * 64
    ts = int(time.time())
    msg = f"ultron-hub-close:v1:dashboard:telephone-test:{action}:7:{ts}".encode()
    forged = f"dashboard.telephone-test.{ts}.{__import__('hmac').new(key, msg, 'sha256').hexdigest()}"
    with pytest.raises(closing.CloseRefused, match="dashboard"):
        closing.check(forged, action, "7", key=key)
    with pytest.raises(closing.CloseRefused):
        closing.sign_dashboard(key, "telephone-test", action, "7", ts)
    with pytest.raises(ValueError):
        close_key.dashboard_token("telephone-test", action, "7", key=key)


def test_dashboard_proof_closes_a_todo_once(project, monkeypatch):
    from ultron_hub import cli
    close_key.load_or_create()
    assert cli.main(["todo", "add", "--title", "Essai", "--clair", "essai"]) == 0
    token = closing.sign_dashboard(closing.load_key(), "telephone-test", "done", "1", int(time.time()))
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    assert cli.main(["todo", "done", "1"]) == 0
    from ultron_hub.common.settings import load_settings
    from ultron_hub.todo.store import TodoStore
    with TodoStore(load_settings().todo_db) as store:
        events = store.history(1)
    assert any((e.get("details") or {}).get("by") == "dashboard:telephone-test" for e in events), events
    monkeypatch.delenv(closing.TOKEN_ENV)
    assert cli.main(["todo", "reopen", "1"]) == 0
    monkeypatch.setenv(closing.TOKEN_ENV, token)
    assert cli.main(["todo", "done", "1"]) != 0  # the same proof never serves twice


# -- todo #331: a long result started from the dashboard is delivered (message + attachment through envoi) ----------------

@pytest.mark.parametrize("action,args,title", [
    ("video-analyse", {"id": "20261020T160906Z-d22ffb83"}, "🔬 Analyse de la vidéo prête"),
])
def test_long_result_is_delivered_with_its_attachment(dash, monkeypatch, tmp_path, action, args, title):
    sent = []
    md = tmp_path / "analyse.md"
    data = {"id": args["id"], "text": "Résumé court", "path": str(md)}
    monkeypatch.setattr(actions.Actions, "_run", lambda self, argv, extra, **kw: {"status": "ok", "summary": "ok", "data": data})
    monkeypatch.setattr(actions.envoi, "envoyer", lambda texte, **kw: sent.append(texte))
    sid = session(dash)
    code, out, _ = prepare(dash, sid, {"action": action, **args})
    assert code == 200 and out["ok"], out
    assert sent == []  # preparing delivers nothing
    code, res, _ = confirm(dash, sid, out["jeton"])
    assert code == 200 and res["ok"], res
    assert sent == [f"{title}\n\nRésumé court\nMEDIA:{md}"]  # one message, the attachment as a MEDIA line


def test_failed_action_delivers_nothing(dash, monkeypatch):
    sent = []
    monkeypatch.setattr(actions.Actions, "_run", lambda self, argv, extra, **kw: {"status": "error", "error": "non"})
    monkeypatch.setattr(actions.envoi, "envoyer", lambda texte, **kw: sent.append(texte))
    sid = session(dash)
    code, out, _ = prepare(dash, sid, {"action": "video-analyse", "id": "20261020T160906Z-d22ffb83"})
    confirm(dash, sid, out["jeton"])
    assert sent == []


def test_only_the_long_dashboard_results_are_delivered():
    assert {a.id for a in actions.catalogue.CATALOGUE if a.livre} == {"video-analyse", "video-tout", "montage-script", "montage-audio"}


def test_the_one_button_video_failure_is_delivered_too(dash, monkeypatch):  # todo #335
    sent = []
    monkeypatch.setattr(actions.Actions, "_run", lambda self, argv, extra, **kw: {"status": "error", "error": "vidéo privée"})
    monkeypatch.setattr(actions.envoi, "envoyer", lambda texte, **kw: sent.append(texte))
    a = actions.Actions()
    p = actions.Pending("telephone-test", "", "video-tout", {"lien": "https://youtu.be/abcdefghijk"}, 0.0, None)
    out = a._finish(p, actions.catalogue.PAR_ID["video-tout"], a._run([], {}))
    assert not out["ok"] and sent == ["❌ L'analyse de la vidéo a échoué\n\nvidéo privée"]


def test_the_one_button_video_success_is_delivered_like_video_analyse(monkeypatch, tmp_path):  # todo #335
    sent = []
    md = tmp_path / "analyse.md"
    monkeypatch.setattr(actions.envoi, "envoyer", lambda texte, **kw: sent.append(texte))
    a = actions.Actions()
    p = actions.Pending("telephone-test", "", "video-tout", {"lien": "https://youtu.be/abcdefghijk"}, 0.0, None)
    a._journal = lambda text: None
    out = a._finish(p, actions.catalogue.PAR_ID["video-tout"],
                    {"status": "ok", "summary": "ok", "data": {"id": "x", "text": "Résumé", "path": str(md)}})
    assert out["ok"] and sent == [f"🔬 Analyse de la vidéo prête\n\nRésumé\nMEDIA:{md}"]
