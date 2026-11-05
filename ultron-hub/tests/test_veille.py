"""Veilles (todo #85): silent watchers, Telegram only when something is new."""

import json
import os
import stat
from pathlib import Path

import pytest

from ultron_hub import cli
from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.todo import service
from ultron_hub.veille import core, sandbox

REPO = Path(__file__).resolve().parents[1]

# Reads its answer from answer.json next to the copied script, so a test can change it without changing the script.
SCRIPT = """#!/usr/bin/python3
import json, sys
state = json.load(sys.stdin)["state"]
open("seen.json", "a").write(json.dumps(state) + "\\n")
print(open("answer.json").read())
"""


@pytest.fixture(autouse=True)
def veilles_dir(tmp_path, monkeypatch):
    path = tmp_path / "veilles"
    monkeypatch.setenv(core.VEILLES_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def no_sandbox(request, monkeypatch):
    """The contract is tested without bwrap; the real sandbox is probed apart (test_real_sandbox_probe)."""
    if not request.node.name.startswith("test_real_sandbox_probe"):
        monkeypatch.setattr(sandbox, "argv", lambda script, folder, lecture, name: [str(script)])


class Box:
    def __init__(self, fail=False):
        self.sent, self.journal, self.fail = [], [], fail
        self.buttons = []

    def send(self, text, cle_alerte=None, boutons=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)
        self.buttons.append(boutons)

    def run(self, **kw):
        return core.run(self.send, self.journal.append, **kw)


def make(tmp_path, name="places", body=SCRIPT, every=30, timeout=10):
    src = tmp_path / f"{name}.py"
    src.write_text(body)
    core.add(name, src, every, "Places Hyrox", timeout)
    return core.folder(name)


def answer(path, new, state, message=""):
    (path / "travail").mkdir(exist_ok=True)
    (path / "travail" / "answer.json").write_text(json.dumps({"new": new, "message": message, "state": state}))


def test_first_run_seeds_state_silently_then_speaks_only_on_change(tmp_path):
    path, box = make(tmp_path), Box()
    answer(path, True, 1, "une place")
    box.run()
    assert box.sent == [] and json.loads((path / "state.json").read_text()) == 1
    answer(path, False, 1)
    box.run(force=True)
    assert box.sent == []
    answer(path, True, 2, "une place s'est libérée")
    report = box.run(force=True)
    assert box.sent == ["🔔 Places Hyrox : une place s'est libérée"]
    assert report["new"] == [{"nom": "places", "message": "une place s'est libérée"}]
    assert box.journal == ["Veille places : une place s'est libérée"]
    seen = [json.loads(line) for line in (path / "travail" / "seen.json").read_text().splitlines()]
    assert seen == [None, 1, 1]  # the script always receives the previous state


def test_not_due_is_skipped_and_force_runs(tmp_path):
    path, box = make(tmp_path), Box()
    answer(path, False, 0)
    box.run()
    assert box.run()["skipped"] == ["places"]
    assert box.run(force=True)["ran"] == ["places"]


def test_send_failure_keeps_old_state_for_next_pass(tmp_path):
    path = make(tmp_path)
    answer(path, False, 1)
    Box().run()
    answer(path, True, 2, "nouveau")
    report = Box(fail=True).run(force=True)
    assert json.loads((path / "state.json").read_text()) == 1
    assert report["failed"] and core.status("places")["failures"] == 0
    box = Box()
    box.run(force=True)
    assert box.sent == ["🔔 Places Hyrox : nouveau"]


def set_timeout(name, seconds):
    """core.add() only takes whole seconds from 5 to 300: a short test delay is written into veille.json."""
    config_path = core.folder(name) / core.CONFIG
    config = json.loads(config_path.read_text())
    config["timeout_s"] = seconds
    config_path.write_text(json.dumps(config))


@pytest.mark.parametrize("body, reason, delai", [
    ("#!/bin/sh\nexit 3\n", "code de sortie 3", 5),
    ("#!/bin/sh\necho pas du json\n", "pas du JSON", 5),
    ("#!/bin/sh\necho '{\"new\": true, \"state\": 1}'\n", "message est vide", 5),
    ("#!/bin/sh\necho '{\"new\": \"oui\", \"state\": 1}'\n", "new (booléen)", 5),
    ("#!/bin/sh\nhead -c 70000 /dev/zero | tr '\\\\0' a\n", "trop longue", 5),
    ("#!/bin/sh\nsleep 30\n", "délai dépassé", 0.3),
])
def test_contract_violations_are_failures(tmp_path, monkeypatch, body, reason, delai):
    make(tmp_path, body=body, timeout=5)
    if delai != 5:
        set_timeout("places", delai)
        monkeypatch.setattr(sandbox, "limits", lambda timeout: None)  # RLIMIT_CPU veut un entier ; le délai mur est testé
    report = Box().run()
    assert reason in report["failed"][0]["raison"]


def test_three_failures_pause_and_alert_once(tmp_path):
    make(tmp_path, body="#!/bin/sh\nexit 1\n")
    box = Box()
    for _ in range(5):
        box.run(force=True)
    st = core.status("places")
    assert st["paused"] and st["failures"] == 3
    assert len(box.sent) == 1 and "en pause après 3 échecs" in box.sent[0]
    assert box.run(force=True)["skipped"] == ["places"]
    core.resume("places")
    assert core.status("places")["failures"] == 0 and not core.status("places")["paused"]


def test_success_resets_failures(tmp_path):
    path, box = make(tmp_path), Box()
    (path / "travail").mkdir()
    (path / "travail" / "answer.json").write_text("cassé")
    box.run(force=True)
    box.run(force=True)
    answer(path, False, 1)
    box.run(force=True)
    assert core.status("places")["failures"] == 0


def test_modified_script_is_not_run(tmp_path):
    path = make(tmp_path)
    answer(path, False, 1)
    (path / "check").write_text(SCRIPT + "# changed\n")
    report = Box().run()
    assert "empreinte" in report["failed"][0]["raison"]
    assert not (path / "travail" / "seen.json").exists()


def test_script_gets_minimal_env_and_its_work_dir_as_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "x")
    body = ("#!/usr/bin/python3\nimport json, os, sys\nsys.stdin.read()\n"
            "print(json.dumps({'new': False, 'state': [sorted(os.environ), os.getcwd()]}))\n")
    path = make(tmp_path, body=body)
    Box().run()
    env, cwd = json.loads((path / "state.json").read_text())
    assert "SECRET_TOKEN" not in env and "ULTRON_VEILLE" in env and cwd == str(path / "travail")


def test_add_refuses_bad_input(tmp_path):
    good = tmp_path / "s.py"
    good.write_text(SCRIPT)
    with pytest.raises(InvalidRequestError):
        core.add("../x", good, 30, "d")
    with pytest.raises(InvalidRequestError):
        core.add("ok-name", good, 1, "d")
    with pytest.raises(InvalidRequestError):
        core.add("ok-name", good, 30, "")
    nohash = tmp_path / "n.py"
    nohash.write_text("print(1)\n")
    with pytest.raises(InvalidRequestError):
        core.add("ok-name", nohash, 30, "d")
    link = tmp_path / "l.py"
    link.symlink_to(good)
    with pytest.raises(InvalidRequestError):
        core.add("ok-name", link, 30, "d")
    core.add("ok-name", good, 30, "d")
    with pytest.raises(InvalidRequestError):
        core.add("ok-name", good, 30, "d")
    mode = stat.S_IMODE(os.stat(core.folder("ok-name")).st_mode)
    assert mode == 0o700


def test_pause_resume_remove_to_trash(tmp_path):
    path = make(tmp_path)
    core.pause("places")
    assert core.entries()[0]["paused"]
    target = core.remove("places")
    assert not path.exists() and (target / "check").is_file() and target.parent.name == ".corbeille"
    assert core.names() == []


def test_lock_busy(tmp_path):
    make(tmp_path)
    with core._run_lock() as locked:
        assert locked
        assert Box().run()["busy"]


def test_cli_run_sends_through_hermes(tmp_path, monkeypatch, capsys):
    path = make(tmp_path)
    answer(path, False, 1)
    sent = []
    monkeypatch.setattr(service, "send_message", lambda text, target, binary, **k: sent.append((text, target)))
    assert cli.main(["veille", "run"]) == 0
    answer(path, True, 2, "ça bouge")
    capsys.readouterr()
    assert cli.main(["veille", "run", "--nom", "places", "--force"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "veille.run" and out["data"]["new"][0]["nom"] == "places"
    assert sent == [("🔔 Places Hyrox : ça bouge", "telegram")]
    assert cli.main(["veille", "pause", "inconnue"]) == 1
    assert "inconnue" in json.loads(capsys.readouterr().out)["error"]


def test_demo_veille_reports_a_moved_main():
    script = REPO / "deploy/veilles/ultron-hub-main/check"
    assert os.access(script, os.X_OK) and script.read_bytes().startswith(b"#!")
    assert "git" in script.read_text() and "log" in script.read_text()


def test_timer_and_after_merge_declared():
    unit = (REPO / "deploy/systemd/ultron-hub-veille.service").read_text()
    assert "ultron-hub veille run" in unit
    assert (REPO / "deploy/systemd/ultron-hub-veille.timer").is_file()
    actions = json.loads((REPO / "deploy/user/apres-fusion/todo-85.json").read_text())["actions"]
    assert actions[0]["action"] == "user_unit_enable" and actions[0]["args"] == ["ultron-hub-veille.timer"]


def test_access_lists_are_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "lecture_roots", lambda: (tmp_path,))
    repo = tmp_path / "depot"
    repo.mkdir()
    (tmp_path / "lien").symlink_to(repo)
    for bad in (["relatif"], [str(tmp_path / "lien")], [str(tmp_path / "absent")], ["/etc"], [str(repo)] * 4):
        with pytest.raises(InvalidRequestError):
            sandbox.check_lecture(bad)
    for bad in ("http://example.com", "https://user@example.com/", "https://example.com:8443/",
                "https://127.0.0.1/", "https://[::1]/", "https://localhost/", "https://exemple.ts.net/",
                "https://intranet/", "https://a.local/x", "https://example.com/a b", "ftp://example.com"):
        with pytest.raises(InvalidRequestError):
            sandbox.check_url(bad)
    assert sandbox.check_urls(["https://example.com/", "https://example.com/"]) == ["https://example.com/"]
    path = make(tmp_path)
    config = core.set_access("places", [str(repo)], ["https://example.com/"])
    assert config["lecture"] == [str(repo)] and core.load("places")["sha256"] == config["sha256"]
    with pytest.raises(InvalidRequestError):
        core.set_access("places", ["/home"], [])
    # veille.json edited by hand: refused at run time, the script is not run
    config["lecture"] = ["/"]
    (path / "veille.json").write_text(json.dumps(config))
    report = Box().run()
    assert "accès refusé" in report["failed"][0]["raison"]


def test_pages_are_fetched_outside_and_given_on_stdin(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "fetch_page", lambda url: {"url": url, "status": 200, "type": "text/html",
                                                              "texte": "<p>2 places</p>"})
    body = ("#!/usr/bin/python3\nimport json, sys\nd = json.load(sys.stdin)\n"
            "print(json.dumps({'new': False, 'state': d['pages']}))\n")
    path = make(tmp_path, body=body)
    core.set_access("places", [], ["https://example.com/"])
    Box().run()
    assert json.loads((path / "state.json").read_text())[0]["texte"] == "<p>2 places</p>"


def test_fetch_refuses_private_addresses(monkeypatch):
    monkeypatch.setattr(sandbox.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("192.168.1.2", 443))])
    assert "publique" in sandbox.fetch_page("https://example.com/")["erreur"]
    assert "erreur" in sandbox.fetch_page("http://example.com/")
    for embedded in ("64:ff9b::7f00:1", "2002:7f00:1::"):  # NAT64 / 6to4 around 127.0.0.1
        monkeypatch.setattr(sandbox.socket, "getaddrinfo", lambda *a, ip=embedded, **k: [(0, 0, 0, "", (ip, 443, 0, 0))])
        assert "publique" in sandbox.fetch_page("https://example.com/")["erreur"]


def test_fetch_connects_to_the_checked_address_and_refuses_redirects(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))])

    class Resp:
        status, headers = 302, None

    class Conn:
        def __init__(self, host, ip, timeout):
            calls.append((host, ip))

        def request(self, method, target, headers):
            calls.append(target)

        def getresponse(self):
            return Resp()

        def close(self):
            pass

    monkeypatch.setattr(sandbox, "_PinnedHTTPS", Conn)
    page = sandbox.fetch_page("https://example.com/a?b=1")
    assert calls == [("example.com", "93.184.216.34"), "/a?b=1"] and "redirection refusée" in page["erreur"]


def test_missing_bwrap_is_a_failure_not_a_fallback(tmp_path, monkeypatch):
    path = make(tmp_path)
    answer(path, False, 1)
    monkeypatch.setattr(sandbox, "argv", lambda *a: [str(tmp_path / "pas-de-bwrap")])
    report = Box().run()
    assert "bac à sable indisponible" in report["failed"][0]["raison"]
    assert not (path / "travail" / "seen.json").exists()


PROBE = """#!/usr/bin/python3
import json, os, socket, sys
json.load(sys.stdin)
def tries(fn):
    try:
        fn()
        return "ok"
    except Exception as exc:
        return type(exc).__name__
def value(fn):
    try:
        return fn()
    except Exception as exc:
        return type(exc).__name__
home =os.path.expanduser("~")
res = {
    "leurre": tries(lambda: open(os.environ["LEURRE"]).read()) if "LEURRE" in os.environ else "absent",
    "home": tries(lambda: open(os.path.join(home, "x-veille-sonde"), "w").write("x")),
    "travail": tries(lambda: open("ecrit.txt", "w").write("x")),
    "dossier": tries(lambda: open(os.path.join(os.path.dirname(sys.argv[0]), "x"), "w").write("x")),
    "script": tries(lambda: open(sys.argv[0], "a").write("#")),
    "lecture": tries(lambda: open(os.environ["ULTRON_VEILLE_LECTURE"]).read()),
    "lecture_ecrit": tries(lambda: open(os.environ["ULTRON_VEILLE_LECTURE"], "a").write("x")),
    "reseau": tries(lambda: socket.create_connection(("1.1.1.1", 443), timeout=3)),
    "veille": os.environ.get("ULTRON_VEILLE"),
    "masque": [value(lambda: os.listdir(os.path.join(os.path.dirname(os.environ["ULTRON_VEILLE_LECTURE"]), "var"))),
               value(lambda: open(os.path.join(os.path.dirname(os.environ["ULTRON_VEILLE_LECTURE"]), ".env")).read())],
}
print(json.dumps({"new": False, "state": res}))
"""


def test_real_sandbox_probe(tmp_path, monkeypatch):
    """Real bwrap: decoy unreadable, home and the veille folder read-only, travail/ writable, no network."""
    import subprocess
    try:
        subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "true"], check=True, capture_output=True,
                       timeout=10)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("bwrap indisponible ici (bac à sable imbriqué)")
    monkeypatch.setattr(sandbox, "lecture_roots", lambda: (tmp_path,))
    decoy = tmp_path / "leurre"
    decoy.write_text("secret")
    repo = tmp_path / "depot"
    repo.mkdir()
    (repo / "f.txt").write_text("lisible")
    (repo / "var").mkdir()
    (repo / "var" / "ultron.db").write_text("todos")
    (repo / ".env").write_text("TOKEN=x")
    # The script cannot get variables from us: the paths are written in the script itself.
    body = PROBE.replace('os.environ["LEURRE"]', repr(str(decoy))).replace(
        '"LEURRE" in os.environ', "True").replace('os.environ["ULTRON_VEILLE_LECTURE"]', repr(str(repo / "f.txt")))
    path = make(tmp_path, body=body)
    core.set_access("places", [str(repo)], [])
    report = Box().run()
    assert not report["failed"], report
    res = json.loads((path / "state.json").read_text())
    assert res["leurre"] in ("FileNotFoundError", "PermissionError")
    # home is an empty tmpfs: a write there is thrown away, the real home is never touched
    assert not (Path.home() / "x-veille-sonde").exists()
    assert res["dossier"] != "ok" and res["script"] != "ok"
    assert res["travail"] == "ok" and (path / "travail" / "ecrit.txt").is_file()
    assert res["lecture"] == "ok" and res["lecture_ecrit"] != "ok"
    assert res["reseau"] != "ok" and res["veille"] == "places"
    assert res["masque"][0] == [] and res["masque"][1] in ("", "PermissionError")  # var/ and .env hidden


def test_real_sandbox_probe_demo(tmp_path):
    """The demo veille in the real sandbox: blind without `lecture`, sees main with the repository read-only."""
    import subprocess
    repo = Path.home() / "workspace/projects/active/ultron-hub"
    try:
        subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "true"], check=True, capture_output=True,
                       timeout=10)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("bwrap indisponible ici (bac à sable imbriqué)")
    if not (repo / ".git").is_dir():
        pytest.skip("dépôt ultron-hub absent")
    path = make(tmp_path, body=(REPO / "deploy/veilles/ultron-hub-main/check").read_text())
    assert "code de sortie" in Box().run()["failed"][0]["raison"]
    core.set_access("places", [str(repo)], None)
    assert not Box().run(force=True)["failed"]
    assert json.loads((path / "state.json").read_text()).split()[0] in subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%h", "main"], capture_output=True, text=True).stdout


def rewrite_script(path, body):
    """Replace the veille's script and keep its fingerprint valid (as if it had been added so)."""
    (path / "check").write_text(body)
    config = json.loads((path / core.CONFIG).read_text())
    config["sha256"] = core.fingerprint((path / "check").read_bytes())
    (path / core.CONFIG).write_text(json.dumps(config))


def test_failure_keeps_the_last_stderr_line_not_the_trace_head(tmp_path):
    body = ("#!/bin/sh\necho 'Traceback (most recent call last):' >&2\necho '  File \"x.py\", line 3' >&2\n"
            "echo 'KeyError: prix' >&2\necho >&2\nexit 1\n")
    make(tmp_path, body=body)
    assert Box().run()["failed"][0]["raison"] == "code de sortie 1 : KeyError: prix"


def test_pause_alert_is_text_with_the_resume_command_and_no_button(tmp_path):
    make(tmp_path, body="#!/bin/sh\nexit 1\n")
    box = Box()
    for _ in range(3):
        box.run(force=True)
    assert box.buttons == [None]
    assert "ultron-hub veille resume places" in box.sent[0]


def test_relancer_one_trial_without_advancing_state_or_sending(tmp_path):
    path = make(tmp_path, body="#!/bin/sh\nexit 1\n")
    with pytest.raises(InvalidRequestError, match="pas en pause"):
        core.relancer("places")
    box = Box()
    for _ in range(3):
        box.run(force=True)
    with pytest.raises(core.CheckFailed, match="code de sortie 1"):
        core.relancer("places")
    assert core.status("places")["paused"] and core.status("places")["failures"] == 3
    rewrite_script(path, '#!/bin/sh\necho \'{"new": true, "message": "x", "state": 9}\'\n')
    state_before = (path / core.STATE).exists()
    st = core.relancer("places")
    assert not st["paused"] and st["failures"] == 0 and len(box.sent) == 1
    assert (path / core.STATE).exists() == state_before


def test_cli_relancer_reports_the_cause_or_resumes(tmp_path, capsys):
    path = make(tmp_path, body="#!/bin/sh\necho 'ValueError: boum' >&2\nexit 1\n")
    core.pause("places")
    assert cli.main(["veille", "relancer", "places"]) != 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "ValueError: boum" in out["error"] and core.status("places")["paused"]
    rewrite_script(path, '#!/bin/sh\necho \'{"new": false, "message": "", "state": 1}\'\n')
    assert cli.main(["veille", "relancer", "places"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok" and not core.status("places")["paused"]


def test_canal_fil_keeps_news_off_telegram(tmp_path, monkeypatch, capsys):
    """todo #393: a veille with canal=fil reaches send(fil_seul=True); the others are untouched."""
    path = make(tmp_path)
    answer(path, False, 1)
    sent = []
    monkeypatch.setattr(service, "send_message", lambda text, target, binary, **k: sent.append((text, k.get("fil_seul"))))
    assert cli.main(["veille", "run"]) == 0
    assert cli.main(["veille", "canal", "places", "fil"]) == 0
    assert core.load("places")["canal"] == "fil"
    answer(path, True, 2, "ça bouge")
    assert cli.main(["veille", "run", "--nom", "places", "--force"]) == 0
    assert sent == [("🔔 Places Hyrox : ça bouge", True)]
    assert cli.main(["veille", "canal", "places", "telegram"]) == 0
    assert "canal" not in core.load("places")
    answer(path, True, 3, "encore")
    assert cli.main(["veille", "run", "--nom", "places", "--force"]) == 0
    assert sent[-1] == ("🔔 Places Hyrox : encore", False)


def test_envoyer_fil_seul_sends_nothing_but_keeps_the_fil(tmp_path, monkeypatch):
    from ultron_hub import envoi
    pushed = []
    monkeypatch.setattr(envoi, "_pousser", lambda *a, **k: pushed.append(a[0]))
    monkeypatch.setattr(envoi, "_envoyer_telegram", lambda *a, **k: pytest.fail("Telegram must not be reached"))
    res = envoi.envoyer("🔔 main a avancé", sujet="alertes", fil_seul=True)
    assert res.ok and res.envoyes == 0 and pushed == ["🔔 main a avancé"]
