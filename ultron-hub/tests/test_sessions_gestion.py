"""Todo #266: opening and relaunching ultron sessions (fake runner for tmux, git and the ultron script; no real ones)."""

import argparse
import io
import json
import subprocess
from pathlib import Path

import pytest

from cli_contract import check_argv
from test_dashboard_actions import (confirm, dash, fake_cli, fresh_cache, prepare, session)  # noqa: F401
from test_sessions_ecriture import NOM, DEVICE, key, token  # noqa: F401
from ultron_hub import cli
from ultron_hub.dashboard import catalogue
from ultron_hub.escouade.mcp_ultron import value_hash
from ultron_hub.sessions import gestion
from ultron_hub.sessions.journal import SessionError
from ultron_hub.todo import closing

ROOT = Path(__file__).parents[1]
NEW = "ultron-19-10-16h00"
BIN = "/fake/maison/.local/bin/ultron"


class Fake:
    """Answers the ultron script (creates NEW), tmux and git; records every call in order."""

    def __init__(self, sessions=(NOM,), output=f"Session tmux : {NEW}\n", dirty=(), creates=True, new=NEW):
        self.new = new
        self.calls, self.sessions, self.output, self.dirty, self.creates = [], set(sessions), output, set(dirty), creates
        self.envs = []

    def __call__(self, argv, **kw):
        assert isinstance(argv, list) and "shell" not in kw
        self.calls.append(argv)
        out, rc = "", 0
        if argv[0] == BIN:
            self.envs.append(kw.get("env"))
            out, rc = self.output, 1  # the script fails at `attach-session` without a terminal: the code does not count
            if self.creates:
                self.sessions.add(self.new)
        elif argv[0] == "git":
            out = " M x.py\n" if Path(argv[2]).name in self.dirty else ""
        elif argv[1] == "has-session":
            rc = 0 if argv[3].removeprefix("=") in self.sessions else 1
        elif argv[1] == "kill-session":
            self.sessions.discard(argv[3].removeprefix("="))
        return subprocess.CompletedProcess(argv, rc, out, "")

    def launches(self):
        return [a for a in self.calls if a[0] == BIN]

    def kills(self):
        return [a for a in self.calls if a[:2] == ["tmux", "kill-session"]]


@pytest.fixture(autouse=True)
def fake_bin(monkeypatch):
    monkeypatch.setattr(gestion, "ULTRON_BIN", Path(BIN))
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")


def env_for(consigne, chantier=""):
    return {closing.TOKEN_ENV: token(f"nouvelle:{chantier}:{value_hash(consigne)}")}


# -- nouvelle ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("consigne,argv", [("", [BIN]), ("focus sur la todo 12", [BIN, "focus sur la todo 12"])])
def test_new_session_argv_and_result(tmp_path, key, consigne, argv):
    run = Fake()
    out = gestion.nouvelle(consigne, tmp_path, run=run, env=env_for(consigne))
    assert out == {"session": NEW, "id": NEW, "chantier": None, "consigne": bool(consigne), "by": "dashboard:" + DEVICE}
    assert run.launches() == [argv]
    assert run.calls[-1] == ["tmux", "has-session", "-t", f"={NEW}"]
    assert "TMUX" not in run.envs[0]


def test_new_session_logs_no_text(tmp_path, key, caplog):
    caplog.set_level("INFO")
    gestion.nouvelle("consigne-confidentielle", tmp_path, run=Fake(), env=env_for("consigne-confidentielle"))
    assert "consigne-confidentielle" not in caplog.text and f"session={NEW} chantier=- consigne=23 by=dashboard:{DEVICE}" in caplog.text


def test_new_session_needs_a_valid_unspent_proof(tmp_path, key):
    run = Fake()
    with pytest.raises(closing.CloseRefused, match="réservé"):
        gestion.nouvelle("salut", tmp_path, run=run, env={})
    with pytest.raises(closing.CloseRefused, match="invalide"):  # signed for another instruction
        gestion.nouvelle("salut", tmp_path, run=run, env=env_for("autre"))
    with pytest.raises(closing.CloseRefused):  # a write proof is not an opening proof
        gestion.nouvelle("salut", tmp_path, run=run, env={closing.TOKEN_ENV: token(f"{NOM}:{value_hash('salut')}")})
    (tmp_path / "dashboard.md").write_text("x")
    with pytest.raises(closing.CloseRefused, match="invalide"):  # signed without the chantier
        gestion.nouvelle("salut", tmp_path, run=run, chantier="dashboard", env=env_for("salut"), folder=tmp_path)
    assert run.calls == []
    env = env_for("salut")
    gestion.nouvelle("salut", tmp_path, run=run, env=env)
    with pytest.raises(closing.CloseRefused, match="déjà utilisé"):
        gestion.nouvelle("salut", tmp_path, run=run, env=env)
    assert len(run.launches()) == 1


@pytest.mark.parametrize("consigne,why", [("relance", "commande"), ("liste", "commande"), ("list", "commande"), ("12", "commande"),
                                          ("a\nb", "contrôle"), ("a\x1b", "contrôle"), ("x" * (gestion.CONSIGNE_MAX + 1), "trop longue")])
def test_bad_instruction_refused(tmp_path, key, consigne, why):
    run = Fake()
    with pytest.raises(SessionError, match=why):
        gestion.nouvelle(consigne, tmp_path, run=run, env=env_for(consigne))
    assert run.calls == []


def test_instruction_starting_with_a_reserved_word_is_fine(tmp_path, key):
    run = Fake()
    gestion.nouvelle("relance les tests", tmp_path, run=run, env=env_for("relance les tests"))
    assert run.launches() == [[BIN, "relance les tests"]]


@pytest.mark.parametrize("fake", [Fake(output="rien\n"), Fake(output="Session tmux : autre\n"),
                                  Fake(output=f"Session tmux : {NEW}\n", creates=False)])
def test_output_without_a_real_session_is_an_error(tmp_path, key, fake):
    with pytest.raises(SessionError):
        gestion.nouvelle("", tmp_path, run=fake, env=env_for(""))


# -- relancer ------------------------------------------------------------------------------------------------------------

class FakeClear(Fake):
    """Adds the answers of `display-message` (pane command), `show-environment` (ULTRON_MSG) and `ultron-hub prompt`."""

    def __init__(self, command="claude", msg="Lis /x/interactif.md et applique-le.", hub_rc=0, **kw):
        super().__init__(**kw)
        self.command, self.msg, self.hub_rc = command, msg, hub_rc

    def __call__(self, argv, **kw):
        if argv[0] == str(gestion.HUB_BIN):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, self.hub_rc, "", "")
        if argv[:2] == ["tmux", "display-message"]:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, self.command + "\n", "")
        if argv[:2] == ["tmux", "show-environment"]:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0 if self.msg is not None else 1,
                                               f"ULTRON_MSG={self.msg}\n" if self.msg is not None else "", "")
        return super().__call__(argv, **kw)

    def sends(self):
        return [a for a in self.calls if a[:2] == ["tmux", "send-keys"]]


def test_relaunch_clears_in_the_same_session():
    run, pauses = FakeClear(), []
    out = gestion.relancer(NOM, run=run, sleep=pauses.append)
    assert out == {"session": NOM, "ancienne": NOM, "mode": "clear"}
    target = ["-t", f"={NOM}:"]
    assert run.sends() == [["tmux", "send-keys", *target, "-l", "--", "/clear"], ["tmux", "send-keys", *target, "Enter"],
                           ["tmux", "send-keys", *target, "-l", "--", "Lis /x/interactif.md et applique-le."],
                           ["tmux", "send-keys", *target, "Enter"]]
    assert run.launches() == [] and run.kills() == [] and pauses == [gestion.CLEAR_WAIT_S] and NOM in run.sessions
    assert ["tmux", "show-environment", "-t", f"={NOM}", "ULTRON_MSG"] in run.calls
    # order: the prompt is rebuilt, then /clear, then (after the wait) the message
    kinds = [a[1] if a[0] == "tmux" else "hub" for a in run.calls]
    assert kinds.index("hub") < kinds.index("send-keys")
    assert run.calls[kinds.index("hub")][1:] == ["prompt", "interactif", "--sortie", str(gestion.PROMPTS_DIR / "interactif.md")]


def test_relaunch_rebuilds_the_chantier_prompt():
    run = FakeClear(sessions=("ultron-dashboard-20-10-09h12",))
    gestion.relancer("ultron-dashboard-20-10-09h12", run=run, sleep=lambda s: None)
    [hub] = [a for a in run.calls if a[0] == str(gestion.HUB_BIN)]
    assert hub[1:] == ["prompt", "interactif", "dashboard", "--sortie", str(gestion.PROMPTS_DIR / "interactif-dashboard.md")]


def test_relaunch_unknown_chantier_sends_nothing():
    run = FakeClear(sessions=("ultron-vieux-20-10-09h12",), hub_rc=1)
    with pytest.raises(SessionError, match="vieux"):
        gestion.relancer("ultron-vieux-20-10-09h12", run=run, sleep=lambda s: None)
    assert run.sends() == [] and run.launches() == [] and run.kills() == []


def test_relaunch_dirty_worktree_is_no_longer_refused():
    run = FakeClear(dirty={"todo-2-y"})
    assert gestion.relancer(NOM, run=run, sleep=lambda s: None)["mode"] == "clear"
    assert not [a for a in run.calls if a[0] == "git"]


@pytest.mark.parametrize("fake", [lambda: FakeClear(command="bash"), lambda: FakeClear(command="zsh"),
                                  lambda: FakeClear(msg=None), lambda: FakeClear(msg="")])
def test_relaunch_falls_back_to_a_new_session(fake):
    run = fake()
    out = gestion.relancer(NOM, run=run, sleep=lambda s: None)
    assert out == {"session": NEW, "ancienne": NOM, "mode": "nouvelle"}
    assert run.sends() == [] and run.launches() == [[BIN]]
    kinds = [a[0] if a[0] == BIN else a[1] for a in run.calls]
    assert kinds.index(BIN) < kinds.index("kill-session")
    assert run.kills() == [["tmux", "kill-session", "-t", f"={NOM}"]]
    assert NEW in run.sessions and NOM not in run.sessions


def test_relaunch_fallback_failed_opening_keeps_the_old_session():
    run = FakeClear(command="bash", creates=False)
    with pytest.raises(SessionError):
        gestion.relancer(NOM, run=run, sleep=lambda s: None)
    assert run.kills() == [] and NOM in run.sessions


def test_relaunch_refuses_a_message_with_control_characters():
    run = FakeClear(msg="a\x1bb")
    assert gestion.relancer(NOM, run=run, sleep=lambda s: None)["mode"] == "nouvelle"  # never typed into the pane
    assert run.sends() == []


@pytest.mark.parametrize("nom", ["autre", "ultron-19-10-15h38; rm", "=ultron-19-10-15h38", ""])
def test_relaunch_bad_or_absent_name(nom):
    run = FakeClear(sessions=())
    with pytest.raises(SessionError, match="nom de session"):
        gestion.relancer(nom, run=run, sleep=lambda s: None)
    with pytest.raises(SessionError, match="absente"):
        gestion.relancer(NOM, run=run, sleep=lambda s: None)
    assert run.launches() == [] and run.kills() == [] and run.sends() == []


# -- CLI -------------------------------------------------------------------------------------------------------------------

def test_cli_new_reads_stdin_and_logs_no_text(project, key, monkeypatch, capsys, caplog):
    run = Fake()
    monkeypatch.setattr(subprocess, "run", run)
    secret = "consigne-secrete-xyz"
    monkeypatch.setenv(closing.TOKEN_ENV, token(f"nouvelle::{value_hash(secret)}"))
    monkeypatch.setattr("sys.stdin", io.StringIO(secret + "\n"))
    caplog.set_level("INFO")
    assert cli.main(["session", "nouvelle", "--consigne-stdin"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["data"]["session"] == NEW
    assert run.launches() == [[BIN, secret]] and secret not in caplog.text
    check_argv(["session", "nouvelle", "--consigne-stdin"])
    check_argv(["session", "relancer", NOM])


def test_cli_new_refused_without_token_is_json(project, key, monkeypatch, capsys):
    run = Fake()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv(closing.TOKEN_ENV, raising=False)
    assert cli.main(["session", "nouvelle"]) != 0
    assert json.loads(capsys.readouterr().out)["status"] == "error" and run.calls == []


# -- dashboard: catalogue entries -------------------------------------------------------------------------------------------

def test_catalogue_entries():
    nouvelle, relancer = catalogue.PAR_ID["session-nouvelle"], catalogue.PAR_ID["session-relancer"]
    assert (nouvelle.type, nouvelle.signe, nouvelle.stdin, nouvelle.risque) == ("signee", "session", "consigne", "high")
    assert nouvelle.champ("consigne").min == 0 and nouvelle.champ("consigne").max == gestion.CONSIGNE_MAX
    assert (relancer.type, relancer.danger, relancer.risque) == ("simple", True, "high")


def test_dashboard_new_session_with_and_without_instruction(dash):
    sid = session(dash)
    for texte in ("Regarde   la todo\n12", ""):
        _, out, _ = prepare(dash, sid, {"action": "session-nouvelle", "consigne": texte})
        assert out["ok"] and "todo" not in json.dumps(out)
        assert confirm(dash, sid, out["jeton"])[1]["ok"]
    first, second = dash.calls()
    assert first["argv"] == ["session", "nouvelle", "--consigne-stdin"] and first["stdin"] == "Regarde la todo 12"
    assert second["stdin"] == ""
    closing.check(first["env"][closing.TOKEN_ENV], "session", f"nouvelle::{value_hash('Regarde la todo 12')}")
    closing.check(second["env"][closing.TOKEN_ENV], "session", f"nouvelle::{value_hash('')}")


def test_dashboard_relaunch_is_simple_and_checks_the_name(dash):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "session-relancer", "nom": NOM})
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["session", "relancer", NOM] and closing.TOKEN_ENV not in call["env"]
    check_argv(call["argv"])
    assert not prepare(dash, sid, {"action": "session-relancer", "nom": "autre"})[1].get("ok")
    assert not prepare(dash, sid, {"action": "session-nouvelle", "consigne": "x" * (gestion.CONSIGNE_MAX + 1)})[1].get("ok")


def test_restart_after_merge_is_declared():
    plan = json.loads((ROOT / "deploy/user/apres-fusion/todo-266.json").read_text())
    assert [(a["action"], a["args"]) for a in plan["actions"]] == [("user_unit_restart", ["ultron-hub-dashboard.service"])]


def test_relancer_summary_depends_on_the_mode(monkeypatch, capsys):
    import argparse
    from ultron_hub.sessions import commands
    args = argparse.Namespace(session_command="relancer", nom=NOM)
    monkeypatch.setattr(commands.gestion, "relancer", lambda nom, **kw: {"session": nom, "ancienne": nom, "mode": "clear"})
    assert "relancée" in commands.handle(args, None)["summary"] and "fermée" not in commands.handle(args, None)["summary"]
    monkeypatch.setattr(commands.gestion, "relancer", lambda nom, **kw: {"session": "ultron-neuve", "ancienne": nom, "mode": "nouvelle"})
    assert commands.handle(args, None)["summary"] == f"session ultron-neuve ouverte, {NOM} fermée"


# -- todo #297: chantier of a new session, closing a session -------------------------------------------------------------------

NEW_CH = "ultron-dashboard-19-10-16h00"


@pytest.fixture
def fiches(tmp_path):
    folder = tmp_path / "prompt"
    folder.mkdir()
    for name in ("dashboard", "fba", "_socle", "reprise-orchestrateur", "reprise-x", "relance"):
        (folder / f"{name}.md").write_text("x")
    return folder


def test_chantiers_are_the_sheets_without_socle_and_reprise(fiches):
    assert gestion.chantiers(fiches) == ["dashboard", "fba"]


def test_new_session_with_chantier_and_instruction(tmp_path, key, fiches):
    run = Fake(output=f"Session tmux : {NEW_CH}\n", new=NEW_CH)
    out = gestion.nouvelle("regarde X", tmp_path, chantier="dashboard", run=run, env=env_for("regarde X", "dashboard"), folder=fiches)
    assert run.launches() == [[BIN, "--chantier", "dashboard", "regarde X"]]
    assert out["chantier"] == "dashboard" and out["session"] == NEW_CH


def test_new_session_with_chantier_only(tmp_path, key, fiches):
    run = Fake(output=f"Session tmux : {NEW_CH}\n", new=NEW_CH)
    gestion.nouvelle("", tmp_path, chantier="dashboard", run=run, env=env_for("", "dashboard"), folder=fiches)
    assert run.launches() == [[BIN, "--chantier", "dashboard"]]


def test_unknown_chantier_refused_before_anything(tmp_path, key, fiches):
    run = Fake()
    for bad in ("inconnu", "_socle", "reprise-x", "relance", "../x"):
        with pytest.raises(SessionError, match="inconnu"):
            gestion.nouvelle("", tmp_path, chantier=bad, run=run, env={}, folder=fiches)
    assert run.calls == []


def test_old_script_without_chantier_option_is_undone(tmp_path, key, fiches):
    run = Fake()  # makes NEW, a system session: the script did not know --chantier
    with pytest.raises(SessionError, match="poser.sh"):
        gestion.nouvelle("", tmp_path, chantier="dashboard", run=run, env=env_for("", "dashboard"), folder=fiches)
    assert run.kills() == [["tmux", "kill-session", "-t", f"={NEW}"]] and NEW not in run.sessions


class FakeSettings:
    pass


@pytest.fixture(autouse=True)
def journal_calls(monkeypatch):
    """Todo #298: never the real journal; the lines `fin ...` that closing would write."""
    lines = []
    monkeypatch.setattr(gestion.store, "record", lambda settings, source, kind, text, todo=None: lines.append((source, kind, text)))
    return lines


@pytest.fixture
def closing_env(monkeypatch):
    monkeypatch.setattr(gestion, "travail_non_commite", lambda settings: ["todo-9-x"])


def close_env(nom=NOM):
    return {closing.TOKEN_ENV: token(f"fermer:{nom}")}


def test_close_kills_then_checks_it_is_gone(tmp_path, key, closing_env):
    run = Fake()
    out = gestion.fermer(NOM, tmp_path, FakeSettings(), run=run, env=close_env(), digest=lambda n: {"sous_agents": 0})
    assert out == {"session": NOM, "fermee": True, "verifiee": True, "by": "dashboard:" + DEVICE,
                   "travail_non_commite": ["todo-9-x"]}
    assert [a[1] for a in run.calls] == ["has-session", "display-message", "kill-session", "has-session"]
    assert run.kills() == [["tmux", "kill-session", "-t", f"={NOM}"]] and NOM not in run.sessions
    assert all("--force" not in a and "-f" not in a for a in run.calls)


def test_close_refused_while_sub_agents_work(tmp_path, key, closing_env):
    run = Fake()
    with pytest.raises(SessionError, match="2 sous-agent"):
        gestion.fermer(NOM, tmp_path, FakeSettings(), run=run, env=close_env(), digest=lambda n: {"sous_agents": 2})
    assert run.kills() == [] and NOM in run.sessions


def test_close_unknown_count_does_not_block(tmp_path, key, closing_env):
    run = Fake()
    assert gestion.fermer(NOM, tmp_path, FakeSettings(), run=run, env=close_env(), digest=lambda n: {"sous_agents": None})["fermee"]


def test_close_says_when_the_session_is_still_there(tmp_path, key, closing_env):
    class Stubborn(Fake):
        def __call__(self, argv, **kw):
            out = super().__call__(argv, **kw)
            if argv[1:2] == ["kill-session"]:
                self.sessions.add(NOM)
            return out
    with pytest.raises(SessionError, match="toujours ouverte"):
        gestion.fermer(NOM, tmp_path, FakeSettings(), run=Stubborn(), env=close_env(), digest=lambda n: {"sous_agents": 0})


def test_close_needs_a_proof_for_that_session_and_a_valid_name(tmp_path, key, closing_env):
    run = Fake()
    with pytest.raises(closing.CloseRefused):
        gestion.fermer(NOM, tmp_path, FakeSettings(), run=run, env={}, digest=lambda n: {"sous_agents": 0})
    with pytest.raises(closing.CloseRefused):
        gestion.fermer(NOM, tmp_path, FakeSettings(), run=run, env=close_env("ultron-autre"), digest=lambda n: {"sous_agents": 0})
    with pytest.raises(SessionError, match="refusé"):
        gestion.fermer("autre", tmp_path, FakeSettings(), run=run, env=close_env("autre"))
    with pytest.raises(SessionError, match="absente"):
        gestion.fermer("ultron-01-01-00h00", tmp_path, FakeSettings(), run=run, env=close_env("ultron-01-01-00h00"),
                       digest=lambda n: {"sous_agents": 0})
    assert run.kills() == []


def test_closing_sheet_data(closing_env):
    run = Fake()
    out = gestion.fermeture(NOM, FakeSettings(), run=run, digest=lambda n: {"sous_agents": 1})
    assert out == {"session": NOM, "sous_agents": 1, "travail_non_commite": ["todo-9-x"]} and run.kills() == []


def test_dirty_worktrees_are_listed_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(gestion.repos, "list_repos", lambda s: {"a": tmp_path / "a", "b": tmp_path / "b"})
    monkeypatch.setattr(gestion.branches, "_todo_refs", lambda path: ["r"])
    monkeypatch.setattr(gestion.branches, "dirty_worktrees",
                        lambda path, names: [str(tmp_path / "w/todo-1-x")] if path.name == "a" else [])
    assert gestion.travail_non_commite(FakeSettings()) == ["todo-1-x"]


def test_cli_close_and_state(project, key, monkeypatch, capsys, closing_env):
    run = Fake()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(gestion.etat, "digest", lambda n: {"sous_agents": 0})
    assert cli.main(["session", "fermeture", NOM]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["sous_agents"] == 0
    monkeypatch.setenv(closing.TOKEN_ENV, close_env()[closing.TOKEN_ENV])
    assert cli.main(["session", "fermer", NOM]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["data"]["fermee"] and "disparu" in out["summary"]
    check_argv(["session", "fermer", NOM])
    check_argv(["session", "fermeture", NOM])
    check_argv(["session", "nouvelle", "--chantier", "fba", "--consigne-stdin"])


def test_dashboard_close_is_signed_for_that_session(dash):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "session-fermer", "nom": NOM})
    assert out["ok"] and out["danger"] and "vérifiée" in out["effet"]
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["session", "fermer", NOM]
    closing.check(call["env"][closing.TOKEN_ENV], "session", f"fermer:{NOM}")
    assert not prepare(dash, sid, {"action": "session-fermer", "nom": "autre"})[1].get("ok")


def test_dashboard_new_session_with_chantier_is_signed_for_both(dash):
    sid = session(dash)
    _, out, _ = prepare(dash, sid, {"action": "session-nouvelle", "chantier": "dashboard", "consigne": "vas-y"})
    assert out["ok"] and "dashboard" in out["sur_quoi"]
    assert confirm(dash, sid, out["jeton"])[1]["ok"]
    [call] = dash.calls()
    assert call["argv"] == ["session", "nouvelle", "--chantier=dashboard", "--consigne-stdin"]
    closing.check(call["env"][closing.TOKEN_ENV], "session", f"nouvelle:dashboard:{value_hash('vas-y')}")
    assert not prepare(dash, sid, {"action": "session-nouvelle", "chantier": "../x", "consigne": ""})[1].get("ok")
    assert not prepare(dash, sid, {"action": "session-nouvelle", "chantier": "-h", "consigne": ""})[1].get("ok")


# -- todo #298: end line in the journal ---------------------------------------------------------------------------------

class Listed(FakeClear):
    """Adds `list-sessions` (creation time 1000) to the fake tmux."""

    def __call__(self, argv, **kw):
        if argv[:2] == ["tmux", "list-sessions"]:
            return subprocess.CompletedProcess(argv, 0, f"1000 {NOM} 0\n", "")
        return super().__call__(argv, **kw)


def test_close_journals_the_end_with_the_duration_when_claude_runs(tmp_path, key, closing_env, journal_calls):
    run = Listed()
    gestion.fermer(NOM, tmp_path, FakeSettings(), run=run, env=close_env(), digest=lambda n: {"sous_agents": 0},
                   clock=lambda: 1000 + 2 * 3600 + 35 * 60 + 9)
    assert journal_calls == [("ultron", "session", f"fin {NOM} durée=2:35")]


def test_close_writes_no_second_end_when_claude_already_ended(tmp_path, key, closing_env, journal_calls):
    gestion.fermer(NOM, tmp_path, FakeSettings(), run=Listed(command="bash"), env=close_env(), digest=lambda n: {"sous_agents": 0})
    assert journal_calls == []  # the launch command wrote it when Claude quit


def test_a_journal_failure_never_blocks_closing(tmp_path, key, closing_env, monkeypatch):
    def boom(*a, **k):
        raise OSError("disque plein")
    monkeypatch.setattr(gestion.store, "record", boom)
    assert gestion.fermer(NOM, tmp_path, FakeSettings(), run=Listed(), env=close_env(), digest=lambda n: {"sous_agents": 0})["fermee"]


def test_relaunch_by_new_session_journals_the_end_only_if_claude_runs(journal_calls):
    gestion.relancer(NOM, run=Listed(msg=None), settings=FakeSettings(), sleep=lambda s: None)  # no start message: new session
    assert [c[2].split(" durée")[0] for c in journal_calls] == [f"fin {NOM}"]
    journal_calls.clear()
    gestion.relancer(NOM, run=Listed(command="bash"), settings=FakeSettings(), sleep=lambda s: None)
    assert journal_calls == []


@pytest.mark.parametrize("secondes,texte", [(0, "0:00"), (59, "0:00"), (7 * 60, "0:07"), (3600 * 2 + 35 * 60, "2:35"), (-5, "0:00")])
def test_duration_format(secondes, texte):
    assert gestion.duree_hmm(secondes) == texte


def test_session_list_gives_each_duration(monkeypatch):
    from ultron_hub.sessions import commands
    monkeypatch.setattr(commands.time, "time", lambda: 1000 + 3 * 3600 + 5 * 60)
    monkeypatch.setattr(commands.journal, "list_sessions", lambda: [{"nom": NOM, "cree": 1000, "ouverte": False, "chantier": None}])
    monkeypatch.setattr(commands.etat, "digest", lambda nom: {"sous_agents": 0})
    monkeypatch.setattr(commands.gestion, "chantiers", lambda: [])
    out = commands.handle(argparse.Namespace(session_command="liste"), FakeSettings())
    (session,) = out["data"]["sessions"]
    assert session["duree"] == "3:05" and session["duree_s"] == 3 * 3600 + 300


def test_close_unlinks_objectives(tmp_path, key, closing_env, monkeypatch):
    """Todo #538 : la fermeture vérifiée délie les objectifs de la session ; un échec de délie ne bloque pas la fermeture."""
    from ultron_hub import objectif
    vus = []
    monkeypatch.setattr(objectif, "delier_fermee", lambda settings, nom: vus.append(nom) or [1])
    gestion.fermer(NOM, tmp_path, FakeSettings(), run=Fake(), env=close_env(), digest=lambda n: {"sous_agents": 0})
    assert vus == [NOM]

    def boom(settings, nom):
        raise OSError("disque")
    monkeypatch.setattr(objectif, "delier_fermee", boom)
    assert gestion.fermer(NOM, tmp_path, FakeSettings(), run=Fake(), env=close_env(), digest=lambda n: {"sous_agents": 0})["fermee"]
