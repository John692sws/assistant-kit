"""Logging back into a Claude account from the dashboard (todo #336): `compte connexion demarrer|code|abandonner`.

No real tmux, no real claude, no network, no real waiting: `connexion.run_cmd` is a fake tmux + `claude auth status`, the
clock is fake, and the Claude login (typing the code then Enter) writes a fake credentials file (fixture `comptes_paths`).
"""

import io
import json
import re
import subprocess
from pathlib import Path

import pytest

from test_compte import (A1, B1, PROFILES, SECRETS, USAGE, creds, dashboard_press, env, login,  # noqa: F401
                         mode, register_both, run)
from ultron_hub.compte import commands, connexion, store
from ultron_hub.escouade.mcp_ultron import value_hash
from ultron_hub.todo import closing

ROOT = Path(__file__).parents[1]
B2, A3 = creds("ACCESS-B2", "REFRESH-B2"), creds("ACCESS-A3", "REFRESH-A3")
CODE = "Zq9xCODEsecret_42#state-77"
LINK = "https://claude.ai/oauth/authorize?code=true&client_id=abc&state=LINKSECRET77"


class FakeTmux:
    """One tmux session at most, a pane showing `screen`; Enter after the code runs `on_enter` (the login)."""

    def __init__(self, active: Path, screen: list[str], result: bytes | None = B2, status: int = 0, logged: bool = True):
        self.active, self.screen, self.result, self.status, self.logged = active, screen, result, status, logged
        self.alive = self.dead = False
        self.calls: list[list[str]] = []
        self.typed: list[str] = []
        self.buffer: str | None = None
        self.envs: list[dict] = []

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and "shell" not in kw
        self.calls.append(list(argv))
        if argv[0] != "tmux":
            assert argv[1:] == ["auth", "status", "--json"]
            self.envs.append(kw.get("env"))
            return self._cp(0, json.dumps({"loggedIn": self.logged, "email": "secret@example.com"}))
        cmd, rest = argv[1], argv[2:]
        if cmd == "has-session":
            return self._cp(0 if self.alive else 1)
        if cmd == "kill-session":
            self.alive = False
            return self._cp(0)
        if cmd == "new-session":
            self.alive, self.dead = True, False
            return self._cp(0)
        if not self.alive:
            return self._cp(1)
        if cmd == "set-option":
            return self._cp(0)
        if cmd == "capture-pane":
            return self._cp(0, "\n".join(self.screen) + "\n")
        if cmd == "display-message":
            return self._cp(0, f"{int(self.dead)} {self.status if self.dead else ''}")
        if cmd == "load-buffer":
            assert rest == ["-b", connexion.BUFFER, "-"] and "stdin" not in kw
            self.buffer = kw["input"]
            return self._cp(0)
        if cmd == "paste-buffer":
            assert rest == ["-d", "-b", connexion.BUFFER, "-t", connexion.TARGET]
            self.typed.append(self.buffer)
            self.buffer = None  # -d
            return self._cp(0)
        if cmd == "send-keys":
            keys = rest[2:]
            if keys == ["Enter"] and self.typed:
                if self.result is not None and self.status == 0:
                    self.active.write_bytes(self.result)
                self.dead = True
            return self._cp(0)
        raise AssertionError(argv)

    @staticmethod
    def _cp(code, out=""):
        return subprocess.CompletedProcess([], code, out, "")


@pytest.fixture
def fake(env, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(connexion, "clock", lambda: now[0])
    monkeypatch.setattr(connexion, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(connexion, "claude_bin", lambda: "/opt/fake/claude")
    for token, (pct, uuid) in {"ACCESS-B2": (5.0, "uuid-b"), "ACCESS-A3": (44.0, "uuid-a")}.items():
        monkeypatch.setitem(USAGE, token, pct)
        monkeypatch.setitem(PROFILES, token, uuid)
    tmux = FakeTmux(env["active"], ["Browser didn't open? Use the url below to sign in:", "", LINK[:40], LINK[40:], "",
                                    "Paste code here if prompted >"])
    monkeypatch.setattr(connexion, "run_cmd", tmux)
    return tmux


def start(capsys, monkeypatch, slot="b"):
    dashboard_press(monkeypatch, f"login:{slot}")
    return run(capsys, "connexion", "demarrer", f"--compte={slot}")


def paste(capsys, monkeypatch, code=CODE, sign=True, *extra):
    if sign:
        dashboard_press(monkeypatch, f"code:{value_hash(code)}")
    monkeypatch.setattr("sys.stdin", io.StringIO(code + "\n"))
    return run(capsys, "connexion", "code", "--code-stdin", *extra)


def everything_written(env):
    logs = "".join(p.read_text() for p in (env["project"] / "var" / "logs").glob("*.log*"))
    state = "".join(p.read_text() for p in (env["project"] / "var").rglob("*") if p.is_file() and "logs" not in p.parts)
    meta = (store.DIR / store.META).read_text() if (store.DIR / store.META).exists() else ""
    return logs + state + meta


def test_login_from_the_dashboard_registers_the_new_login(env, fake, capsys, monkeypatch):
    register_both(env, capsys)  # active = A1
    code, out = start(capsys, monkeypatch)
    assert code == 0 and out["data"]["lien"] == LINK and out["data"]["compte"] == "b"  # cut over two lines, glued back
    new = next(c for c in fake.calls if c[:2] == ["tmux", "new-session"])
    assert new[new.index("--") + 1:] == ["/usr/bin/env", "-i", *(f"{k}={v}" for k, v in connexion.minimal_env().items()),
                                          "/opt/fake/claude", "auth", "login", "--claudeai"]
    assert set(connexion.minimal_env()) == {"HOME", "PATH", "LANG", "TERM"}
    code, out = paste(capsys, monkeypatch, CODE, True, "--compte=b")
    assert code == 0, out
    assert out["data"]["compte"] == "b" and out["data"]["verifie"] and out["data"]["actif"] == "b"
    assert fake.typed == [CODE] and not fake.alive  # typed once, session closed
    assert (store.DIR / "b.json").read_bytes() == B2 and (store.DIR / "a.json").read_bytes() == A1
    assert fake.envs == [connexion.minimal_env()]
    written = everything_written(env)
    assert "compte connexion code compte=b" in written
    for secret in (CODE, LINK, "LINKSECRET77", "secret@example.com", *SECRETS, "ACCESS-B2", "REFRESH-B2"):
        assert secret not in written, secret
    assert not any("logout" in part for call in fake.calls for part in call)


def test_logged_in_with_the_other_account_says_so_and_registers_nothing(env, fake, capsys, monkeypatch):
    register_both(env, capsys)
    fake.result = A3  # the claude.ai page was on account A
    start(capsys, monkeypatch)
    code, out = paste(capsys, monkeypatch)
    assert code == 1 and "l'autre compte (pro@exemple.fr)" in out["error"] and "rien n'a été enregistré dans perso@exemple.fr" in out["error"]
    assert (store.DIR / "b.json").read_bytes() == B1 and not fake.alive


def test_a_link_to_another_site_is_refused(env, fake, capsys, monkeypatch):
    fake.screen = ["Open https://claude.ai.evil.example/oauth?x=1"]
    code, out = start(capsys, monkeypatch)
    assert code == 1 and "lien de connexion inattendu" in out["error"] and not fake.alive


def test_the_claude_com_link_of_october_2026_is_accepted(env, fake, capsys, monkeypatch):
    # Screen of `claude auth login --claudeai` on 22/10/2026 (secrets replaced): claude.com/cai/, cut over three lines.
    real = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code&redirect_uri=https%3A%2F%2F"
            "platform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Aprofile+user%3Ainference&code_challenge=hG7q_npe7b&"
            "code_challenge_method=S256&state=LINKSECRET77")
    fake.screen = ["Opening browser to sign in…", "If the browser didn't open, visit: " + real[:120], real[120:200], real[200:],
                   "Paste code here if prompted >"]
    code, out = start(capsys, monkeypatch)
    assert code == 0 and out["data"]["lien"] == real and fake.alive


def test_a_refusal_logs_its_reason_but_never_the_link(env, fake, capsys, monkeypatch):
    warnings = []
    monkeypatch.setattr(commands.log, "warning", lambda fmt, *a: warnings.append(fmt % a))
    fake.screen = ["Open https://claude.ai.evil.example/oauth?x=LINKSECRET77"]
    assert start(capsys, monkeypatch)[0] == 1
    assert len(warnings) == 1 and "lien de connexion inattendu" in warnings[0] and "LINKSECRET77" not in warnings[0]


def test_no_link_within_20_s_closes_the_session(env, fake, capsys, monkeypatch):
    fake.screen = ["Starting…"]
    code, out = start(capsys, monkeypatch)
    assert code == 1 and "20 s" in out["error"] and not fake.alive


def test_each_step_needs_its_own_proof(env, fake, capsys, monkeypatch):
    code, out = run(capsys, "connexion", "demarrer", "--compte=b")
    assert code == 1 and "🔑" in out["error"] and fake.calls == []
    dashboard_press(monkeypatch, "login:a")  # a proof for A does not start B
    code, out = run(capsys, "connexion", "demarrer", "--compte=b")
    assert code == 1 and fake.calls == []
    dashboard_press(monkeypatch, "login:b")
    assert run(capsys, "connexion", "demarrer", "--compte=b")[0] == 0
    code, out = run(capsys, "connexion", "demarrer", "--compte=b")  # replayed
    assert code == 1 and "déjà utilisé" in out["error"]
    dashboard_press(monkeypatch, f"code:{value_hash('autre-code-123')}")  # signed on another code
    monkeypatch.setattr("sys.stdin", io.StringIO(CODE + "\n"))
    code, out = run(capsys, "connexion", "code", "--code-stdin")
    assert code == 1 and fake.typed == []


def test_a_code_without_a_login_in_progress_is_refused(env, fake, capsys, monkeypatch):
    code, out = paste(capsys, monkeypatch)
    assert code == 1 and "aucune connexion en cours" in out["error"] and fake.typed == []


def test_a_code_for_the_other_slot_is_refused(env, fake, capsys, monkeypatch):
    start(capsys, monkeypatch, "b")
    code, out = paste(capsys, monkeypatch, CODE, True, "--compte=a")
    assert code == 1 and "pour le compte B" in out["error"] and fake.typed == []


@pytest.mark.parametrize("bad", ["a b c d", "-abcdef", "../x/y", "ab", "x" * 600, "abc\x1bdef"])
def test_a_malformed_code_is_never_typed_nor_repeated(env, fake, capsys, monkeypatch, bad):
    start(capsys, monkeypatch)
    code, out = paste(capsys, monkeypatch, bad, False)
    assert code == 1 and fake.typed == [] and bad not in json.dumps(out)


def test_a_code_refused_by_claude_registers_nothing(env, fake, capsys, monkeypatch):
    register_both(env, capsys)
    fake.status = 1
    start(capsys, monkeypatch)
    code, out = paste(capsys, monkeypatch)
    assert code == 1 and "refusé le code" in out["error"] and not fake.alive
    assert (store.DIR / "b.json").read_bytes() == B1 and env["active"].read_bytes() == A1
    code, out = paste(capsys, monkeypatch)  # one code per link
    assert code == 1 and "aucune connexion en cours" in out["error"]


def test_not_logged_in_after_the_code_registers_nothing(env, fake, capsys, monkeypatch):
    register_both(env, capsys)
    fake.logged = False
    start(capsys, monkeypatch)
    code, out = paste(capsys, monkeypatch)
    assert code == 1 and "auth status" in out["error"] and (store.DIR / "b.json").read_bytes() == B1


def test_abandon_closes_the_session(env, fake, capsys, monkeypatch):
    start(capsys, monkeypatch)
    assert fake.alive
    code, out = run(capsys, "connexion", "abandonner")
    assert code == 0 and out["data"]["ferme"] and not fake.alive
    assert paste(capsys, monkeypatch)[0] == 1


def test_a_renewed_active_file_is_saved_back_before_the_login(env, fake, capsys, monkeypatch):
    register_both(env, capsys)
    a2 = creds("ACCESS-A2", "REFRESH-A1")  # same connection, renewed access token: the copy A is older
    login(env, a2)
    start(capsys, monkeypatch)
    assert (store.DIR / "a.json").read_bytes() == a2


def test_link_glued_back_over_lines():
    assert connexion.extract_link(["x", "  https://claude.ai/a?b=", "c%20d", "e", "", "Paste code"]) == "https://claude.ai/a?b=c%20de"
    assert connexion.extract_link(["see https://claude.ai/abc now", "more"]) == "https://claude.ai/abc"
    assert connexion.extract_link(["nothing"]) is None
    for bad in ("http://claude.ai/x", "https://claude.ai.evil/x", "https://evil/https://claude.ai/"):
        with pytest.raises(store.CompteError):
            connexion.check_link(bad)
    assert connexion.check_link("https://console.anthropic.com/oauth?x=1")
    assert connexion.check_link("https://claude.com/cai/oauth/authorize?x=1")
    with pytest.raises(store.CompteError):
        connexion.check_link("https://claude.com.evil/x")


def test_state_flags_an_unreadable_quota_and_an_unchecked_switch(env, capsys, monkeypatch):
    register_both(env, capsys)

    def failing(token):
        if token == "ACCESS-B1":
            raise store.UsageError("erreur HTTP 401")
        return {"five_hour": {"utilization": 1.0}, "seven_day": {"utilization": 2.0}}
    monkeypatch.setattr(store, "fetch_usage", failing)
    code, out = run(capsys, "etat")
    assert out["data"]["a_reconnecter"] == [{"nom": "b", "adresse": "perso@exemple.fr", "raison": "quota illisible (erreur HTTP 401)"}]
    assert "🔑 perso@exemple.fr à reconnecter" in out["data"]["text"]
    dashboard_press(monkeypatch, "b")
    code, out = run(capsys, "basculer")
    assert code == 0 and not out["data"]["verification"]["ok"]
    assert run(capsys, "etat")[1]["data"]["derniere_bascule"]["verifie"] is False
    rows = [{"nom": "a", "enregistre": True, "quota": {}, "enregistre_a": "2026-10-20T09:00:00+00:00"},
            {"nom": "b", "enregistre": True, "quota": {}, "enregistre_a": "2026-10-20T09:00:00+00:00"}]
    last = {"de": "a", "vers": "b", "a": "2026-10-21T08:58:00+00:00", "verifie": False}
    assert [x["nom"] for x in store.a_reconnecter(rows, last)] == ["b"]
    rows[1]["enregistre_a"] = "2026-10-21T09:10:00+00:00"  # registered again since: cleared
    assert store.a_reconnecter(rows, last) == []
    before_336 = {k: v for k, v in last.items() if k != "verifie"}
    assert store.a_reconnecter(rows, before_336) == []  # switches before #336: unknown, not flagged


def test_closing_and_dashboard_know_the_login(env):
    from ultron_hub.dashboard import catalogue
    for s in "ab":
        a = catalogue.PAR_ID[f"compte-connexion-{s}"]
        assert (a.type, a.signe, a.cible) == ("signee", "compte", f"login:{s}") and a.argv[-1] == f"--compte={s}"
        assert a.sortie == ("donnees",) and catalogue.projeter(a.donnees, {"lien": LINK, "compte": s, "by": "x"}) == {
            "lien": LINK, "compte": s}
    c = catalogue.PAR_ID["compte-connexion-code"]
    assert (c.signe, c.cible, c.stdin, c.risque) == ("compte", "code:{empreinte}", "code", "high")
    _, args, stdin = catalogue.valider("compte-connexion-code", {"compte": "b", "code": CODE})
    assert CODE not in catalogue.commande(c, args, stdin) and stdin == CODE
    assert CODE not in catalogue.ligne_journal(c, args, stdin) and catalogue.cible(c, args, stdin) == f"code:{value_hash(CODE)}"
    assert catalogue.PAR_ID["compte-connexion-abandonner"].type == "simple"
    assert "connexion demarrer" in closing.refusal_hint("compte", "login:a")
    js = (ROOT / "src/ultron_hub/dashboard/static/app.js").read_text(encoding="utf-8")
    assert 'form("compte-connexion-code"' in js and "a_reconnecter" in js and 'target = "_blank"' in js
    assert "LIEN_CONNEXION.test" in js
    # the page checks the link again: same sites as the server (todo #338, claude.com was missing on both sides)
    pattern = re.search(r"var LIEN_CONNEXION = /\^https:\\/\\/\(([^)]*)\)", js).group(1)
    assert sorted(pattern.replace("\\.", ".").split("|")) == sorted(p[len("https://"):-1] for p in connexion.LINK_PREFIXES)
    plan = json.loads((ROOT / "deploy/user/apres-fusion/todo-336.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]


def test_the_module_never_logs_out_nor_uses_a_shell():
    src = (ROOT / "src/ultron_hub/compte/connexion.py").read_text(encoding="utf-8")
    assert "shell=True" not in src and "os.system" not in src and "print(" not in src
    assert '"logout"' not in src and "auth logout" in src  # only named in the doc: never run


def test_the_code_never_goes_through_argv(env, fake, capsys, monkeypatch):
    register_both(env, capsys)
    start(capsys, monkeypatch)
    assert paste(capsys, monkeypatch)[0] == 0
    assert fake.typed == [CODE] and fake.buffer is None
    assert not any(CODE in part or "Zq9xCODE" in part for call in fake.calls for part in call)
    assert ["tmux", "send-keys", "-t", connexion.TARGET, "Enter"] in fake.calls


def test_an_unverified_login_keeps_the_old_copy_aside(env, fake, capsys, monkeypatch):
    register_both(env, capsys)
    start(capsys, monkeypatch)
    monkeypatch.delitem(PROFILES, "ACCESS-B2")  # profile unreachable for the new login

    def unreachable(token):
        env["calls"]["profile"].append(token)
        if token not in PROFILES:
            raise store.UsageError("profil injoignable")
        return {"account": {"uuid": PROFILES[token]}}
    monkeypatch.setattr(store, "fetch_profile", unreachable)
    code, out = paste(capsys, monkeypatch)
    assert code == 0 and not out["data"]["verifie"] and out["data"]["sauvegarde"] == "b.avant-connexion.json"
    kept = store.DIR / "b.avant-connexion.json"
    assert kept.read_bytes() == B1 and mode(kept) == 0o600 and (store.DIR / "b.json").read_bytes() == B2
    assert "b.avant-connexion.json" in out["data"]["text"]
    assert env["calls"]["profile"].count("ACCESS-B2") == 2  # adopter asked once, enregistrer reused it; etat tried the address (#339)


def test_a_verified_login_asks_the_profile_once_and_keeps_no_backup(env, fake, capsys, monkeypatch):
    register_both(env, capsys)
    start(capsys, monkeypatch)
    assert paste(capsys, monkeypatch)[0] == 0
    assert env["calls"]["profile"].count("ACCESS-B2") == 1 and not (store.DIR / "b.avant-connexion.json").exists()


def test_an_unknown_active_file_gets_a_rescue_copy_before_the_login(env, fake, capsys, monkeypatch):
    from datetime import datetime, timedelta, timezone
    register_both(env, capsys)
    stranger = creds("ACCESS-C1", "REFRESH-C1")
    monkeypatch.setitem(PROFILES, "ACCESS-C1", "uuid-c")
    t0 = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
    names = []
    for i in range(5):
        login(env, creds(f"ACCESS-C{i}", f"REFRESH-C{i}") if i else stranger)
        monkeypatch.setitem(PROFILES, f"ACCESS-C{i}", f"uuid-c{i}")
        names.append(store.sauver_actif(t0 + timedelta(minutes=i)))
    assert names[0] == "inconnu-20261021-090000.json"
    assert store.sauver_actif(t0 + timedelta(hours=1)) is None  # same bytes as the newest rescue copy: not again
    kept = sorted(p.name for p in store.DIR.glob("inconnu-*.json"))
    assert kept == names[2:] and all(mode(store.DIR / n) == 0o600 for n in kept)
    assert (store.DIR / kept[-1]).read_bytes() == env["active"].read_bytes()
    code, out = start(capsys, monkeypatch)
    assert code == 0 and out["data"]["sauve"] is None  # the login reuses the copy already made


def test_the_dashboard_waits_long_enough_for_the_code():
    from ultron_hub.dashboard import catalogue
    assert catalogue.PAR_ID["compte-connexion-code"].delai == 180
