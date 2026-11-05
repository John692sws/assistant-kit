"""Todo #284: `ultron <chantier>` (deploy/bin/ultron), its prompt and Reprise, session names. Fake tmux and ultron-hub, offline."""

import json, time
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.escouade import core, interactif
from ultron_hub.esprit import reprise
from ultron_hub.menage import rangement
from ultron_hub.sessions import gestion, journal
from ultron_hub.sessions.journal import SessionError

from tests.test_prompt_interactif import REPRISE, SUJET, pieces

ROOT = Path(__file__).resolve().parent.parent
ULTRON = ROOT / "deploy/bin/ultron"
POSER = ROOT / "deploy/bin/poser.sh"
STAMP = r"[0-9]{2}-[0-9]{2}-[0-9]{2}h[0-9]{2}"


# -- prompt and Reprise of a chantier ---------------------------------------------------------------------------------

def test_chantier_prompt_reads_its_own_reprise_never_the_orchestrators(tmp_path):
    folder = pieces(tmp_path)
    (folder / "demo.md").write_text(SUJET)
    text = core.build_interactif("demo", dossier=str(folder))
    assert "DERNIERE_LIGNE" not in text and text.rstrip().endswith("Aucune passation encore pour ce chantier.")
    assert f"`{folder}/reprises/demo.md`" in text and "reprise-archiver --chantier demo" in text
    (folder / "reprises").mkdir()
    (folder / "reprises/demo.md").write_text("## Reprise du chantier demo\n- **20/10, passation.** PROPRE\n")
    (folder / "reprise-orchestrateur.md").unlink()  # not needed by a chantier session
    assert core.build_interactif("demo", dossier=str(folder)).rstrip().endswith("PROPRE")
    with pytest.raises(InvalidRequestError):  # the system session still needs it
        core.build_interactif(None, dossier=str(folder))


def test_chantier_prompt_is_aimed_at_its_chantier_only(tmp_path):  # todo #287
    folder = pieces(tmp_path)
    (folder / "demo.md").write_text(SUJET)
    text = core.build_interactif("demo", dossier=str(folder))
    assert "# Orchestrateur du chantier demo" in text and "Pas de travail hors chantier" in text
    assert "ultron-hub todo list --chantier demo" in text and "~/notes/chantier-demo.md" in text
    assert "chantier tableau" not in text and "chantiers système" not in text and "Focus : le **système Ultron**" not in text
    # delegation, branches, checks and handover rules are kept
    assert "## 3. Orchestrateur" in text and "## 7. Passation" in text and "branch auto-merge" in text


def test_no_contradicting_instruction_about_the_reprise_file(tmp_path):  # todo #287
    folder = pieces(tmp_path)
    (folder / "demo.md").write_text(SUJET)
    chantier = core.build_interactif("demo", dossier=str(folder))
    own = f"`{folder}/reprises/demo.md`"
    assert f"Mettre à jour la Reprise ({own}), puis `ultron-hub esprit reprise-archiver --chantier demo`" in chantier
    # the only mention of the orchestrator's file is the one telling NOT to use it
    for line in chantier.splitlines():
        if "reprise-orchestrateur" in line:
            assert "jamais" in line
    assert "reprise-archiver`" not in chantier.replace("reprise-archiver --chantier demo`", "")
    system = core.build_interactif(None, dossier=str(folder))
    assert "`~/notes/prompt/reprise-orchestrateur.md`), puis `ultron-hub esprit reprise-archiver`" in system
    assert "--chantier" not in system and "{reprise" not in system


def test_chantier_prompt_fixes_a_mode_file_posed_before_287(tmp_path):
    folder = pieces(tmp_path)
    (folder / "demo.md").write_text(SUJET)
    old = (folder / "_mode-interactif.md").read_text().replace("`{reprise_fichier}`", "`~/notes/prompt/reprise-orchestrateur.md`")
    (folder / "_mode-interactif.md").write_text(old.replace("`{reprise_archiver}`", "`ultron-hub esprit reprise-archiver`"))
    text = core.build_interactif("demo", dossier=str(folder))
    assert f"Mettre à jour la Reprise (`{folder}/reprises/demo.md`)" in text


def test_repo_mode_file_keeps_the_auto_merge_rule():
    assert "ultron-hub branch auto-merge <id>" in (ROOT / "deploy/prompt/_mode-interactif.md").read_text()


def test_system_prompt_is_unchanged(tmp_path):
    text = core.build_interactif(None, dossier=str(pieces(tmp_path)))
    assert text.rstrip().endswith("DERNIERE_LIGNE") and "session de chantier" not in text


def test_cli_writes_its_own_file_and_creates_the_reprise_once(tmp_path):
    (tmp_path / "p").mkdir()
    folder = pieces(tmp_path / "p")
    (folder / "demo.md").write_text(SUJET)
    settings = SimpleNamespace(var_dir=tmp_path / "var")
    args = SimpleNamespace(prompt_command="interactif", sujet="demo", sortie=None, dossier=str(folder))
    out = interactif.handle(args, settings)
    assert out["data"]["fichier"] == str(tmp_path / "var/prompts/interactif-demo.md")
    assert out["data"]["reprise_creee"] is True
    own = folder / "reprises/demo.md"
    assert own.read_text().startswith("## Reprise du chantier demo")
    reprise.split(own.read_text())  # the archiver finds its section
    own.write_text(own.read_text() + "- **20/10, passation.** GARDEE\n")
    again = interactif.handle(args, settings)
    assert again["data"]["reprise_creee"] is False and "GARDEE" in own.read_text()  # never overwritten
    assert (tmp_path / "var/prompts/interactif-demo.md").read_text().rstrip().endswith("GARDEE")
    system = interactif.handle(SimpleNamespace(prompt_command="interactif", sujet=None, sortie=None, dossier=str(folder)),
                               settings)
    assert system["data"]["fichier"] == str(tmp_path / "var/prompts/interactif.md") and "reprise" not in system["data"]


@pytest.mark.parametrize("bad", ["../x", "_socle", "reprise-orchestrateur", ".cache"])
def test_reprise_path_refuses_bad_names(bad):
    with pytest.raises(InvalidRequestError):
        core.reprise_path(bad)


ENTRY = "- **{n}/10, passation.**\n" + "  - ligne " + "x" * 90 + "\n"


def test_chantier_reprise_archives_under_its_own_name(tmp_path):
    path = tmp_path / "reprises/demo.md"
    path.parent.mkdir()
    path.write_text("## Reprise du chantier demo\n" + "".join(ENTRY.format(n=n) for n in range(20, 10, -1)))
    out = reprise.archive(path, tmp_path / "archives", nom="reprises-demo", now=1_792_400_000)
    assert out["ecrit"] and out["gardees"] == 3 and out["archivees"] == 7
    assert out["archive"].endswith("archives/reprises-demo-2026-10.md")
    assert "demo.md (todo #284)" in Path(out["archive"]).read_text()
    assert reprise.of_chantier("demo", tmp_path) == (tmp_path / "demo.md", "reprises-demo")


def test_menage_archives_every_chantier_reprise_over_budget(tmp_path, monkeypatch):
    folder = tmp_path / "notes/prompt/reprises"
    folder.mkdir(parents=True)
    big = "## Reprise du chantier a\n" + "".join(ENTRY.format(n=n) for n in range(30, 10, -1))
    (folder / "a.md").write_text(big)
    (folder / "b.md").write_text("## Reprise du chantier b\n" + ENTRY.format(n=20))  # under budget
    monkeypatch.setattr(rangement.journal, "record", lambda *a, **k: None)
    cfg = {"budgets": {"files": {"~/notes/prompt/*.md": 50, "~/notes/prompt/reprises/*.md": 1},
                       "archiver_seul": ["~/notes/prompt/reprise-orchestrateur.md", "~/notes/prompt/reprises/*.md"]}}
    out = rangement.archiver_reprises(SimpleNamespace(), cfg, home=str(tmp_path))
    assert [Path(o["path"]).name for o in out] == ["a.md"] and out[0]["ecrit"]
    assert list((tmp_path / "notes/prompt/archives").glob("reprises-a-*.md"))


def test_menage_config_lists_the_chantier_reprises():
    import yaml
    budgets = yaml.safe_load((ROOT / "config/menage.yaml").read_text())["budgets"]
    assert "~/notes/prompt/reprises/*.md" in budgets["archiver_seul"] and "~/notes/prompt/reprises/*.md" in budgets["files"]


# -- session names ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,chantier", [
    ("ultron-20-10-09h12", None), ("ultron-20-10-09h12-2", None), ("ultron-dashboard-20-10-09h12", "dashboard"),
    ("ultron-agent-ultron-20-10-09h12-3", "agent-ultron"), ("ultron-fba-20-10-09h12", "fba"),
    ("ultron-main-22-10-09h12", None), ("ultron-main-22-10-09h12-2", None)])
def test_names_of_system_and_chantier_sessions(name, chantier):
    assert journal.NAME_RE.fullmatch(name) and journal.chantier_of(name) == chantier


@pytest.mark.parametrize("bad", ["ultron-", "ultron-Dash-20-10-09h12", "ultron-x;rm-20-10-09h12", "ultron-_a-20-10-09h12",
                                 "ultron-dashboard", "autre-20-10-09h12", "ultron-" + "a" * 41 + "-20-10-09h12"])
def test_bad_names_refused(bad):
    assert journal.NAME_RE.fullmatch(bad) is None and journal.chantier_of(bad) is None


def test_list_shows_chantier_sessions_too():
    def run(argv, **kw):
        out = "10 ultron-20-10-09h00 0\n12 ultron-dashboard-20-10-09h05 1\n11 autre 0\n"
        return subprocess.CompletedProcess(argv, 0, out, "")
    assert journal.list_sessions(run) == [
        {"nom": "ultron-20-10-09h00", "cree": 10, "ouverte": False, "chantier": None},
        {"nom": "ultron-dashboard-20-10-09h05", "cree": 12, "ouverte": True, "chantier": "dashboard"}]


class Relaunch:
    def __init__(self, created):
        self.calls, self.created, self.sessions = [], created, {"ultron-fba-20-10-09h00"}

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        rc, out = 0, ""
        if argv[0].endswith("/ultron"):
            out, rc = f"Session tmux : {self.created}\n", 1
            self.sessions.add(self.created)
        elif argv[0] == "tmux" and argv[1] == "display-message":
            out = "bash\n"  # Claude is gone from the pane: the old way (a new session)
        elif argv[0] == "tmux" and argv[1] == "show-environment":
            rc = 1
        elif argv[0] == "tmux" and argv[1] == "has-session":
            rc = 0 if argv[3][1:] in self.sessions else 1
        elif argv[0] == "tmux" and argv[1] == "kill-session":
            self.sessions.discard(argv[3][1:])
        return subprocess.CompletedProcess(argv, rc, out, "")


def test_relaunch_keeps_the_chantier():
    run = Relaunch("ultron-fba-20-10-10h00")
    out = gestion.relancer("ultron-fba-20-10-09h00", run=run)
    assert out == {"session": "ultron-fba-20-10-10h00", "ancienne": "ultron-fba-20-10-09h00", "mode": "nouvelle"}
    assert [c[1:] for c in run.calls if c[0].endswith("/ultron")] == [["fba"]]


def test_relaunch_refused_when_the_chantier_is_gone():
    run = Relaunch("ultron-20-10-10h00")  # the script read "fba" as a consigne: plain session
    with pytest.raises(SessionError, match="plus connu"):
        gestion.relancer("ultron-fba-20-10-09h00", run=run)
    assert run.sessions == {"ultron-fba-20-10-09h00"}  # the wrong new one closed, the old one kept


# -- deploy/bin/ultron with a fake tmux and a fake ultron-hub ---------------------------------------------------------

FAKE_TMUX = """#!{py}
import json, os, sys
log = os.environ["FAKE_LOG"]
open(log, "a").write(json.dumps(["tmux", *sys.argv[1:]]) + "\\n")
cmd = sys.argv[1]
if cmd == "ls":
    sys.stdout.write(os.environ.get("FAKE_LS", ""))
elif cmd == "has-session":
    sys.exit(1)
elif cmd == "display-message":
    # with -t: the pane's shell pid or the command running in the pane (pane_current_command); without: the session name
    if "#{{pane_pid}}" in sys.argv:
        print(os.environ.get("FAKE_PANE_PID", ""))
    else:
        print(os.environ.get("FAKE_PANE_CMD", "") if "-t" in sys.argv else os.environ.get("FAKE_CURRENT", ""))
elif cmd == "show-environment":
    if "FAKE_MSG" not in os.environ:
        sys.exit(1)
    print("ULTRON_MSG=" + os.environ["FAKE_MSG"])
elif cmd == "capture-pane":
    sys.stdout.write(os.environ.get("FAKE_SCREEN", ""))
"""

FAKE_PGREP = """#!/bin/sh
# a claude process under the pane's shell when FAKE_CLAUDE_UNDER = that shell's pid
[ -n "$FAKE_CLAUDE_UNDER" ] && [ "$*" = "-x -P $FAKE_CLAUDE_UNDER claude" ]
"""

FAKE_HUB = """#!/bin/sh
echo "$*" >> "$FAKE_LOG.hub"
[ "$1 $2" = "prompt interactif" ] || exit 1
[ -z "$FAKE_HUB_FAIL" ]
"""


@pytest.fixture
def box(tmp_path):
    home, hub, bin_ = tmp_path / "home", tmp_path / "hub", tmp_path / "bin"
    for d in (home / "notes/prompt", hub / ".venv/bin", bin_):
        d.mkdir(parents=True)
    (home / "notes/prompt/dashboard.md").write_text(SUJET)
    (home / "notes/prompt/_socle.md").write_text("socle\n")
    (bin_ / "tmux").write_text(FAKE_TMUX.format(py=sys.executable))
    (bin_ / "pgrep").write_text(FAKE_PGREP)
    (bin_ / "claude").write_text("#!/bin/sh\n")  # todo #537: the launcher checks that claude is executable
    (hub / ".venv/bin/ultron-hub").write_text(FAKE_HUB)
    for f in (bin_ / "tmux", bin_ / "pgrep", bin_ / "claude", hub / ".venv/bin/ultron-hub"):
        f.chmod(0o755)
    log = tmp_path / "log"
    env = {"HOME": str(home), "PATH": f"{bin_}:{os.environ['PATH']}", "ULTRON_HUB_DIR": str(hub), "FAKE_LOG": str(log),
           "LANG": "C.UTF-8", "ULTRON_CONFIANCE_ESSAIS": "0"}  # todo #302: the background trust loop races the call order; on only where tested

    def run(*args, **extra):
        proc = subprocess.run(["sh", str(ULTRON), *args], env={**env, **extra}, capture_output=True, text=True, timeout=30)
        calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        hub_calls = Path(f"{log}.hub").read_text().splitlines() if Path(f"{log}.hub").exists() else []
        log.unlink(missing_ok=True)
        Path(f"{log}.hub").unlink(missing_ok=True)
        box.journal = [h for h in hub_calls if h.startswith("journal ")]  # todo #298
        return proc, calls, [h for h in hub_calls if not h.startswith("journal ")]
    box = SimpleNamespace(run=run, env=env, log=log, home=home, hub=hub, journal=[])
    return box


def news(calls):
    return [c for c in calls if c[1] == "new-session"]


def test_script_is_valid_sh():
    assert subprocess.run(["sh", "-n", str(ULTRON)]).returncode == 0
    assert os.access(ULTRON, os.X_OK) and os.access(POSER, os.X_OK)


def test_one_known_word_opens_a_chantier_session(box):
    proc, calls, hub = box.run("dashboard")
    assert proc.returncode == 0, proc.stderr
    (new,) = news(calls)
    name = new[new.index("-s") + 1]
    assert re.fullmatch(rf"ultron-dashboard-{STAMP}", name) and journal.chantier_of(name) == "dashboard"
    assert f"Session tmux : {name}" in proc.stdout and calls[-1] == ["tmux", "attach-session", "-t", f"={name}"]
    sortie = f"{box.hub}/var/prompts/interactif-dashboard.md"
    assert hub == [f"prompt interactif dashboard --sortie {sortie}"]
    assert f"ULTRON_MSG=Lis {sortie} et applique-le. Session du chantier dashboard." in new  # sans consigne : inchangé
    # as the orchestrator: interactive, auto mode (todo #494), Opus, the session keeps living after Claude
    assert new[-1].startswith('claude --permission-mode auto --model opus -n "$ULTRON_SESSION" "$ULTRON_MSG"')


def test_no_session_and_an_alert_when_claude_is_missing(box):  # todo #537
    """Claude Code updating = the claude link may be missing: no empty tmux shell, an alert to the Fil, a journal line."""
    (Path(box.env["PATH"].split(":")[0]) / "claude").unlink()
    proc, calls, hub = box.run("dashboard", PATH=f"{box.env['PATH'].split(':')[0]}:/usr/bin:/bin",
                               ULTRON_CLAUDE_ESSAIS="2", ULTRON_CLAUDE_PAUSE="0")
    assert proc.returncode == 1 and not news(calls) and "claude introuvable" in proc.stderr
    assert any(h.startswith("send ") and "--genre alerte" in h and "non lancée" in h for h in hub)
    assert any("échec lancement" in j and "claude introuvable" in j for j in box.journal)
    assert not any("début" in j for j in box.journal)


def test_claude_present_launches_as_before(box):  # todo #537
    proc, calls, hub = box.run("dashboard")
    assert proc.returncode == 0 and len(news(calls)) == 1 and not any(h.startswith("send ") for h in hub)


@pytest.mark.parametrize("args,consigne", [(["inconnu"], "inconnu"), (["focus X"], "focus X"), (["dashboard", "vite"], "dashboard vite"),
                                           (["_socle"], "_socle"), ([], None)])
def test_otherwise_a_system_session_with_a_consigne(box, args, consigne):
    proc, calls, hub = box.run("--essai", *args)
    assert proc.returncode == 0 and not news(calls)
    assert re.search(rf"^Session prévue : ultron-main-{STAMP}$", proc.stdout, re.M)
    assert "Chantier : Main" in proc.stdout and hub == [f"prompt interactif --sortie {box.hub}/var/prompts/interactif.md"]
    assert ("Consigne du jour : " + consigne in proc.stdout) if consigne else "Consigne" not in proc.stdout


def test_chantier_option_with_a_consigne(box):
    """Todo #297: `--chantier <nom> "consigne"` = the chantier session AND the day's instruction (the dashboard's call)."""
    proc, calls, hub = box.run("--chantier", "dashboard", "regarde les branches")
    assert proc.returncode == 0, proc.stderr
    (new,) = news(calls)
    sortie = f"{box.hub}/var/prompts/interactif-dashboard.md"
    assert journal.chantier_of(new[new.index("-s") + 1]) == "dashboard" and hub == [f"prompt interactif dashboard --sortie {sortie}"]
    msg = next(a for a in new if a.startswith("ULTRON_MSG="))
    assert msg.startswith("ULTRON_MSG=Consigne du jour : regarde les branches. Si elle se traite sans le prompt")
    assert f"sans lire {sortie}." in msg and f"lis {sortie} et applique-le. Session du chantier dashboard." in msg
    proc, calls, _ = box.run("--chantier", "dashboard")  # option alone = as `ultron dashboard`
    assert proc.returncode == 0 and "Consigne du jour" not in " ".join(news(calls)[0])
    # a lone word after the option is an instruction, even if it is a chantier name
    proc, calls, _ = box.run("--chantier", "dashboard", "dashboard")
    assert "ULTRON_MSG=Consigne du jour : dashboard." in " ".join(news(calls)[0])


@pytest.mark.parametrize("args", [["--chantier"], ["--chantier", "inconnu"], ["--chantier", "_socle", "x"]])
def test_chantier_option_refuses_unknown(box, args):
    proc, calls, _ = box.run(*args)
    assert proc.returncode == 1 and not news(calls) and proc.stderr


def test_dry_run_opens_nothing(box):
    proc, calls, _ = box.run("--essai", "dashboard")
    assert proc.returncode == 0 and not news(calls) and not [c for c in calls if c[1] == "attach-session"]
    assert re.search(rf"^Session prévue : ultron-dashboard-{STAMP}$", proc.stdout, re.M) and "Chantier : dashboard" in proc.stdout


def test_chantier_prompt_failure_opens_nothing(box):
    proc, calls, _ = box.run("dashboard", FAKE_HUB_FAIL="1")
    assert proc.returncode == 1 and "impossible" in proc.stderr and not news(calls)


def test_system_prompt_failure_falls_back_as_before(box):
    proc, calls, _ = box.run("--essai", FAKE_HUB_FAIL="1")
    assert proc.returncode == 0 and "orchestrateur-ultron.md" in proc.stdout and "impossible" in proc.stderr


def test_list_shows_chantier_sessions(box):
    ls = "1 ultron-20-10-09h00 détachée\n3 autre ouverte\n2 ultron-dashboard-20-10-09h05 ouverte\n"
    proc, _, _ = box.run("liste", FAKE_LS=ls)
    assert proc.stdout.splitlines()[:2] == ["1. ultron-20-10-09h00 (détachée)", "2. ultron-dashboard-20-10-09h05 (ouverte)"]
    proc, calls, _ = box.run("2", FAKE_LS=ls)
    assert calls[-1] == ["tmux", "attach-session", "-t", "=ultron-dashboard-20-10-09h05"]


def test_relance_from_a_chantier_session_keeps_the_chantier(box):
    proc, calls, hub = box.run("relance", TMUX="/tmp/x,1,0", FAKE_CURRENT="ultron-dashboard-20-10-09h00")
    assert proc.returncode == 0, proc.stderr
    (new,) = news(calls)
    assert re.fullmatch(rf"ultron-dashboard-{STAMP}", new[new.index("-s") + 1])
    assert ["tmux", "run-shell", "-b", "sleep 1; tmux kill-session -t '=ultron-dashboard-20-10-09h00'"] in calls
    assert hub[0].startswith("prompt interactif dashboard ")


def test_relance_from_the_system_session_stays_system(box):
    proc, calls, _ = box.run("relance", "focus Y", TMUX="/tmp/x,1,0", FAKE_CURRENT="ultron-20-10-09h00")
    (new,) = news(calls)
    assert re.fullmatch(rf"ultron-main-{STAMP}", new[new.index("-s") + 1])
    assert any(a.startswith("ULTRON_MSG=Consigne du jour : focus Y.") for a in new)


def test_relance_refused_when_the_chantier_is_gone(box):
    proc, calls, _ = box.run("relance", TMUX="/tmp/x,1,0", FAKE_CURRENT="ultron-fba-20-10-09h00")
    assert proc.returncode == 1 and "plus connu" in proc.stderr and not news(calls)


def test_relance_clears_in_the_same_session_when_claude_runs(box):
    old = "ultron-dashboard-20-10-09h00"
    proc, calls, hub = box.run("relance", "--force", TMUX="/tmp/x,1,0", TMUX_PANE="%3", FAKE_CURRENT=old, FAKE_PANE_CMD="claude",
                               FAKE_MSG="Lis /x.md et applique-le.")
    assert proc.returncode == 0, proc.stderr
    assert not news(calls) and not [c for c in calls if c[1] in ("switch-client", "kill-session")]
    assert hub[0].startswith("prompt interactif dashboard ")
    [shell] = [c for c in calls if c[1] == "run-shell"]
    assert shell[2] == "-b"
    keys = shell[3]  # background, after a delay; the message is re-read from the tmux environment, never pasted
    assert keys.startswith("sleep 1; ") and keys.index("/clear") < keys.index("Enter") < keys.index("show-environment")
    assert f"'={old}:'" in keys and "Lis /x.md" not in keys


def test_relance_falls_back_to_a_new_session_when_the_pane_is_a_shell(box):
    proc, calls, _ = box.run("relance", TMUX="/tmp/x,1,0", TMUX_PANE="%3", FAKE_CURRENT="ultron-20-10-09h00",
                             FAKE_PANE_CMD="bash", FAKE_MSG="m")
    assert proc.returncode == 0 and len(news(calls)) == 1


def test_relance_from_an_agent_clears_in_the_same_session(box):
    """Todo #430: run by an agent's Bash tool, the pane's foreground is that bash; a claude under the pane's shell still counts."""
    proc, calls, _ = box.run("relance", TMUX="/tmp/x,1,0", TMUX_PANE="%3", FAKE_CURRENT="ultron-main-26-10-10h40",
                             FAKE_PANE_CMD="bash", FAKE_PANE_PID="4242", FAKE_CLAUDE_UNDER="4242", FAKE_MSG="m")
    assert proc.returncode == 0, proc.stderr
    assert not news(calls) and "même session" in proc.stdout
    assert len([c for c in calls if c[1] == "run-shell"]) == 1


@pytest.mark.parametrize("arg", ["--help", "-h"])
def test_help_opens_nothing(box, arg):
    proc, calls, hub = box.run(arg)
    assert proc.returncode == 0 and not calls and not hub
    assert "ultron relance" in proc.stdout and "ultron --help" in proc.stdout and not proc.stdout.startswith("#")


@pytest.mark.parametrize("args", [["--aide"], ["--essai", "-x"], ["-v", "consigne"]])
def test_unknown_option_is_refused_not_a_consigne(box, args):
    proc, calls, hub = box.run(*args)
    assert proc.returncode == 1 and "Option inconnue" in proc.stderr and not news(calls) and not hub


def test_poser_installs_backs_up_and_is_idempotent(tmp_path, box):
    target = box.home / ".local/bin/ultron"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\necho ancien\n")
    env = {"HOME": str(box.home), "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "ULTRON_HUB_DIR": str(box.hub),
           "FAKE_LOG": str(tmp_path / "log")}
    essai = subprocess.run(["bash", str(POSER), "--essai"], env=env, capture_output=True, text=True, timeout=30)
    assert essai.returncode == 0 and "remplacerait" in essai.stdout and target.read_text() == "#!/bin/sh\necho ancien\n"
    done = subprocess.run(["bash", str(POSER)], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert target.read_bytes() == ULTRON.read_bytes() and os.access(target, os.X_OK) and "essai à sec réussi" in done.stdout
    assert target.with_name("ultron.avant-284").read_text() == "#!/bin/sh\necho ancien\n"
    again = subprocess.run(["bash", str(POSER)], env=env, capture_output=True, text=True, timeout=30)
    assert again.returncode == 0 and "déjà à jour" in again.stdout
    assert "prompt interactif" in target.read_text()  # deploy/prompt/poser.sh sees it as up to date


def test_launch_journals_the_start_and_the_end_hook(box):
    """Todo #298: « début » at launch (source ultron, type session); « fin » written by the tmux command once Claude exits."""
    proc, calls, _ = box.run("dashboard")
    (new,) = news(calls)
    name = new[new.index("-s") + 1]
    assert box.journal == [f"journal add --source ultron --type session début {name} chantier=dashboard"]
    assert any(a.startswith("ULTRON_T0=") for a in new) and any(a.startswith("ULTRON_HUB_BIN=") for a in new)
    assert '; d=$(( $(date +%s) - ULTRON_T0 ))' in new[-1] and "fin %s durée=" in new[-1] and new[-1].endswith("-l")
    box.run()
    assert box.journal[0].endswith("chantier=main")


def test_main_session_name_and_old_names_stay_main(box):
    """Todo #334: Main is ultron-main-JJ-MM-HHhMM; old ultron-JJ-MM-HHhMM names are still Main; \"main\" is no chantier."""
    proc, calls, _ = box.run("--essai")
    assert re.search(r"Session prévue : ultron-main-\d\d-\d\d-\d\dh\d\d\n", proc.stdout) and "Chantier : Main" in proc.stdout
    for current in ("ultron-main-22-10-09h12", "ultron-20-10-09h12"):  # relaunch from Main: no chantier, plain prompt
        proc, calls, hub = box.run("relance", TMUX="/tmp/x,1,0", FAKE_CURRENT=current)
        (new,) = news(calls)
        assert proc.returncode == 0 and re.fullmatch(rf"ultron-main-{STAMP}", new[new.index("-s") + 1])
    proc, _, _ = box.run("--essai", "main")  # « main » is a consigne, never a chantier
    assert "Chantier : Main" in proc.stdout and "Consigne du jour : main" in proc.stdout


def test_main_is_refused_as_a_chantier_name():
    with pytest.raises(InvalidRequestError, match="session Main"):
        core.check_name("main")
    with pytest.raises(InvalidRequestError, match="session Main"):
        core.nouveau("main", "Titre", "Objectif", fiche_existe=False)


def test_a_failing_journal_never_blocks_the_launch(box):
    (box.hub / ".venv/bin/ultron-hub").write_text("#!/bin/sh\ncase \"$1\" in journal) exit 3 ;; esac\nexit 0\n")
    proc, calls, _ = box.run("dashboard")
    assert proc.returncode == 0 and len(news(calls)) == 1
    (box.hub / ".venv/bin/ultron-hub").unlink()
    proc, calls, _ = box.run("--chantier", "dashboard")  # prompt assembly needs the binary: that refusal is older than the journal
    assert "journal" not in proc.stderr


# -- todo #302 : question de confiance du dossier, réponse automatique et stricte -------------------------------------------

def _sends(box, screen, wait):
    log = box.log
    log.unlink(missing_ok=True)
    env = {**box.env, "FAKE_SCREEN": screen, "ULTRON_CONFIANCE_ESSAIS": "3", "ULTRON_CONFIANCE_PAUSE": "0.1"}
    subprocess.run(["sh", str(ULTRON), "dashboard"], env=env, capture_output=True, text=True, timeout=30)
    time.sleep(wait)  # la boucle tourne détachée
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    name = news(calls)[0][4]
    return name, [c for c in calls if c[1] == "send-keys"], [c for c in calls if c[1] == "capture-pane"]


def test_trust_screen_gets_down_enter_once_on_the_session_target(box):
    trust = "Quick safety check\n ❯ 1. No, exit\n   2. Yes, I trust this folder\nEnter to confirm\n"
    name, sends, _ = _sends(box, trust, 1.0)
    assert sends == [["tmux", "send-keys", "-t", f"={name}:", "Down", "Enter"]]


def test_any_other_screen_sends_nothing(box):
    name, sends, captures = _sends(box, "Welcome\n ⏵⏵ auto mode on (shift+tab to cycle)\n", 1.0)
    assert sends == [] and all(c[-1] == f"={name}:" for c in captures) and captures
    _, sends, captures = _sends(box, "autre chose\n", 1.0)
    assert sends == [] and len(captures) == 3  # borné


FAKE_SYSTEMD_RUN = """#!/bin/sh
echo "$*" >> "$FAKE_LOG.sdrun"
[ -z "$FAKE_SDRUN_FAIL" ] || exit 1
while [ "${1#--}" != "$1" ]; do shift; done
exec "$@"
"""


def _with_systemd_run(box):
    sd = box.home.parent / "bin" / "systemd-run"
    sd.write_text(FAKE_SYSTEMD_RUN)
    sd.chmod(0o755)
    return Path(f"{box.log}.sdrun")


def test_tmux_is_launched_in_its_own_scope(box):  # todo #354
    sdlog = _with_systemd_run(box)
    proc, calls, _ = box.run("dashboard")
    assert proc.returncode == 0, proc.stderr
    (line,) = sdlog.read_text().splitlines()
    assert line.startswith("--user --scope --quiet --collect --unit=ultron-tmux-ultron-dashboard-") and " tmux new-session -d " in line
    assert len(news(calls)) == 1


def test_tmux_falls_back_to_a_direct_call_when_systemd_run_fails(box):  # todo #354
    _with_systemd_run(box)
    proc, calls, _ = box.run("dashboard", FAKE_SDRUN_FAIL="1")
    assert proc.returncode == 0, proc.stderr
    assert len(news(calls)) == 1


def test_message_starts_with_consigne_or_lis(box):
    """Todo #356 : avec consigne, le message commence par la consigne ; sans, par « Lis »."""
    for args, debut in ((["focus X"], "ULTRON_MSG=Consigne du jour : focus X."), ([], "ULTRON_MSG=Lis ")):
        proc, calls, _ = box.run(*args)
        assert proc.returncode == 0, proc.stderr
        msg = next(a for a in news(calls)[0] if a.startswith("ULTRON_MSG="))
        assert msg.startswith(debut) and (" — " not in msg)


def test_relance_never_resends_the_original_consigne(box):
    """Todo #433: ULTRON_MSG of a session born with a consigne ("--help…") must not come back on a relance; the env is rewritten."""
    old = "ultron-dashboard-20-10-09h00"
    kw = dict(TMUX="/tmp/x,1,0", TMUX_PANE="%3", FAKE_CURRENT=old, FAKE_PANE_CMD="claude", FAKE_MSG="Consigne du jour : --help. Lis /x.md")
    proc, calls, _ = box.run("relance", **kw)
    assert proc.returncode == 0, proc.stderr
    [setenv] = [c for c in calls if c[1] == "set-environment"]
    sortie = f"{box.hub}/var/prompts/interactif-dashboard.md"
    assert setenv[2:] == ["-t", f"={old}", "ULTRON_MSG", f"Lis {sortie} et applique-le. Session du chantier dashboard."]
    assert "--help" not in " ".join(setenv) and len(box.journal) == 1
    assert "relance ultron-dashboard-20-10-09h00" in box.journal[0] and "sans consigne" in box.journal[0]
    proc, calls, _ = box.run("relance", "focus Z", **kw)  # an explicit consigne is kept
    [setenv] = [c for c in calls if c[1] == "set-environment"]
    assert setenv[-1].startswith("Consigne du jour : focus Z. ") and "--help" not in setenv[-1] and "avec consigne" in box.journal[0]
    proc, calls, _ = box.run("relance", **{**kw, "FAKE_CURRENT": "ultron-main-20-10-09h00"}, FAKE_HUB_FAIL="1")  # failing hub: not blocking
    assert proc.returncode == 0 and any(c[1] == "run-shell" for c in calls)


# -- todo #478 : ultron nuit ------------------------------------------------------------------------------------------

def test_nuit_is_in_the_help_and_not_a_chantier_word(box):
    proc, calls, _ = box.run("--help")
    assert "ultron nuit [--detache]" in proc.stdout and not calls
    (box.home / "notes/prompt/nuit.md").write_text(SUJET)  # even with a file of that name, `nuit` is the command
    proc, calls, hub = box.run("--essai", "nuit")
    assert "Session prévue : ultron-nuit-" in proc.stdout and "Chantier : nuit-" in proc.stdout and not hub and not news(calls)


def test_nuit_dry_run_creates_nothing(box):
    proc, calls, hub = box.run("--essai", "nuit")
    assert proc.returncode == 0 and not hub and not news(calls)
    assert re.search(rf"^Session prévue : ultron-nuit-\d\d-\d\d-\d\d-{STAMP}$", proc.stdout, re.M)


def test_nuit_creates_then_opens_with_the_wait_prefix_and_detaches(box):
    fake = box.hub / ".venv/bin/ultron-hub"
    fake.write_text('#!/bin/sh\necho "$*" >> "$FAKE_LOG.hub"\n'
                    'if [ "$1 $2 $3" = "chantier nuit creer" ]; then echo \'{"status":"ok","data":{"nom":"nuit-29-30-10"}}\'; '
                    f'echo x > "{box.home}/notes/prompt/nuit-29-30-10.md"; exit 0; fi\n'
                    '[ "$1 $2" = "prompt interactif" ]\n')
    proc, calls, hub = box.run("nuit", "--detache")
    assert proc.returncode == 0, proc.stderr
    assert hub[0] == "chantier nuit creer" and hub[1].startswith("prompt interactif nuit-29-30-10 --sortie")
    (new,) = news(calls)
    assert re.fullmatch(rf"ultron-nuit-29-30-10-{STAMP}", new[new.index("-s") + 1])
    assert new[-1].startswith('"$ULTRON_HUB_BIN" chantier nuit attendre --session "$ULTRON_SESSION" >/dev/null; claude --permission-mode auto')
    assert not [c for c in calls if c[1] in ("attach-session", "switch-client")]  # --detache


def test_any_nuit_chantier_waits_but_others_do_not(box):
    (box.home / "notes/prompt/nuit-01-02-11.md").write_text(SUJET)
    _, calls, _ = box.run("nuit-01-02-11")
    assert news(calls)[0][-1].startswith('"$ULTRON_HUB_BIN" chantier nuit attendre')
    _, calls, _ = box.run("dashboard")
    assert news(calls)[0][-1].startswith("claude --permission-mode auto")


def test_nuit_refuses_extra_arguments(box):
    proc, calls, _ = box.run("nuit", "vite")
    assert proc.returncode == 1 and not news(calls)


def test_modele_option_sets_the_model_and_defaults_to_opus(box):
    """Todo #517: `--modele haiku|sonnet|opus` (alone or before --chantier) replaces `--model opus`; closed list."""
    for args, model in [((), "opus"), (("--modele", "haiku"), "haiku"), (("--modele", "sonnet", "--chantier", "dashboard", "x"), "sonnet"),
                        (("--chantier", "dashboard", "x"), "opus"), (("--modele", "opus", "une consigne"), "opus")]:
        proc, calls, _ = box.run(*args)
        assert proc.returncode == 0, proc.stderr
        assert f" --model {model} " in news(calls)[0][-1]
    proc, calls, _ = box.run("--essai", "--modele", "haiku", "--chantier", "dashboard")
    assert proc.returncode == 0 and "Modèle : haiku" in proc.stdout and "Chantier : dashboard" in proc.stdout and not news(calls)


@pytest.mark.parametrize("args", [["--modele"], ["--modele", "gpt"], ["--modele", "Opus", "x"], ["--modele", "--chantier", "dashboard"]])
def test_modele_option_refuses_unknown(box, args):
    proc, calls, _ = box.run(*args)
    assert proc.returncode == 1 and not news(calls) and "Modèle inconnu" in proc.stderr
