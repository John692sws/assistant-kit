"""Todo #264: reading an ultron session (fake tmux, fake /proc, fake journals in tmp_path; no real tmux or claude)."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from ultron_hub.dashboard import apercu, catalogue
from ultron_hub.sessions import ecran, journal

UUID = "0a1b2c3d-0000-4000-8000-0123456789ab"
UUID2 = "0a1b2c3d-0000-4000-8000-0123456789ac"
CREATED = 1_792_420_000
ISO = "2026-10-19T14:00:00.000Z"


def jl(*objs) -> str:
    return "\n".join(json.dumps(o) if not isinstance(o, str) else o for o in objs) + "\n"


def asst(*blocks, ts=ISO):
    return {"type": "assistant", "timestamp": ts, "message": {"content": list(blocks)}}


def user(content, ts=ISO, **extra):
    return {"type": "user", "timestamp": ts, "message": {"content": content}, **extra}


def tool(id_, name, **inp):
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


@pytest.fixture
def projects(tmp_path):
    p = tmp_path / "projects"
    (p / "-home-exemple").mkdir(parents=True)
    return p


def fake_tmux(sessions, panes="4242\n"):
    def run(argv, **kw):
        if argv[1] == "list-sessions":
            out = "".join(f"{c} {n} {a}\n" for n, c, a in sessions)
        else:
            out = panes
        return subprocess.CompletedProcess(argv, 0, out, "")
    return run


def test_list_keeps_only_ultron_sessions_oldest_first():
    run = fake_tmux([("ultron-19-10-15h38", CREATED + 5, 1), ("autre", CREATED, 0), ("ultron-19-10-09h00", CREATED, 0),
                     ("ultron-x; rm", CREATED, 0)])
    assert [s["nom"] for s in journal.list_sessions(run)] == ["ultron-19-10-09h00", "ultron-19-10-15h38"]


def test_bubbles_tools_and_subagents(projects):
    f = projects / "-home-exemple" / f"{UUID}.jsonl"
    f.write_text(jl(
        {"type": "custom-title"}, "pas du json",
        user("Bonjour"),
        user("<system-reminder>caché</system-reminder>"),
        user("meta", isMeta=True),
        asst({"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "Salut"},
             tool("t1", "Bash", description="Liste les fichiers", command="cat ~/.sudo_pass")),
        user([{"type": "tool_result", "tool_use_id": "t1", "content": "SORTIE SECRETE"}]),
        asst(tool("a1", "Agent", description="Agent-1 (Sonnet)", model="sonnet", run_in_background=True),
             tool("a2", "Agent", description="Agent-2 (Opus)", model="opus", run_in_background=True)),
        {**user([{"type": "tool_result", "tool_use_id": "a1", "content": "launched"}]), "toolUseResult": {"status": "async_launched"}},
        {**user([{"type": "tool_result", "tool_use_id": "a2", "content": "launched"}]), "toolUseResult": {"status": "async_launched"}},
        user("<task-notification>\n<task-id>x</task-id>\n<tool-use-id>a1</tool-use-id>\n<status>completed</status>\n</task-notification>"),
        asst(tool("r1", "Read", file_path="/home/exemple/notes/x.md")),
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "interne"}]}},
    ))
    data = journal.read_entries(f, 50)
    kinds = [e["k"] for e in data["entrees"]]
    assert kinds == ["utilisateur", "reponse", "outil", "agent_lance", "agent_lance", "agent_fini", "outil"]
    text = json.dumps(data, ensure_ascii=False)
    for hidden in ("secret", "SORTIE", "sudo_pass", "caché", "interne"):
        assert hidden not in text
    assert data["entrees"][2]["texte"] == "Liste les fichiers" and data["entrees"][6]["texte"].endswith("x.md")
    assert [a["description"] for a in data["sous_agents"]] == ["Agent-2 (Opus)"] and data["sous_agents_finis"] == 1


def _notif(status):
    return f"<task-notification>\n<task-id>x</task-id>\n<tool-use-id>a1</tool-use-id>\n<status>{status}</status>\n</task-notification>"


def _attach(prompt, ts=ISO):
    return {"parentUuid": "p", "isSidechain": False, "type": "attachment", "timestamp": ts,
            "attachment": {"type": "queued_command", "prompt": prompt}}


def test_killed_background_agent_in_attachment_line_is_not_running(projects):
    f = projects / "-home-exemple" / f"{UUID}.jsonl"
    f.write_text(jl(
        asst(tool("a1", "Agent", description="Agent-1", model="sonnet", run_in_background=True)),
        {**user([{"type": "tool_result", "tool_use_id": "a1", "content": "launched"}]), "toolUseResult": {"status": "async_launched"}},
        _attach(_notif("killed")),
    ))
    data = journal.read_entries(f, 50)
    assert data["sous_agents"] == [] and data["sous_agents_finis"] == 1
    assert [(e["k"], e.get("etat")) for e in data["entrees"]] == [("agent_lance", None), ("agent_fini", "killed")]


def test_notification_in_both_forms_counts_once(projects):
    f = projects / "-home-exemple" / f"{UUID}.jsonl"
    f.write_text(jl(
        asst(tool("a1", "Agent", description="Agent-1", model="sonnet", run_in_background=True)),
        _attach(_notif("killed")), user(_notif("killed")), _attach(_notif("killed")),
    ))
    data = journal.read_entries(f, 50)
    assert [e["k"] for e in data["entrees"]].count("agent_fini") == 1 and data["sous_agents"] == []


def test_sync_subagent_finishes_on_its_result_and_n_limits_entries(projects):
    f = projects / "-home-exemple" / f"{UUID}.jsonl"
    f.write_text(jl(asst(tool("a1", "Task", description="sync")), user([{"type": "tool_result", "tool_use_id": "a1", "content": "ok"}]),
                    *[user(f"msg {i}") for i in range(10)]))
    data = journal.read_entries(f, 3)
    assert len(data["entrees"]) == 3 and data["tronque"] and data["total"] == 12 and data["sous_agents"] == []
    assert data["entrees"][-1]["texte"] == "msg 9"


def test_only_the_tail_of_a_huge_journal_is_read(projects, monkeypatch):
    monkeypatch.setattr(journal, "TAIL_BYTES", 400)
    f = projects / "-home-exemple" / f"{UUID}.jsonl"
    f.write_text(jl(*[user(f"message numéro {i}") for i in range(50)]))
    data = journal.read_entries(f, 200)
    assert 0 < len(data["entrees"]) < 50 and data["entrees"][-1]["texte"] == "message numéro 49"


# -- the path guard: nothing but ~/.claude/projects/<dir>/<uuid>.jsonl ----------------------------------------------------

def test_guard_accepts_a_journal_and_refuses_everything_else(projects, tmp_path):
    good = projects / "-home-exemple" / f"{UUID}.jsonl"
    good.write_text("{}\n")
    assert journal.safe_journal(good, projects) == good.resolve()
    claude = tmp_path / ".claude"
    claude.mkdir()
    secret = claude / ".credentials.json"
    secret.write_text("{}")
    outside = tmp_path / f"{UUID2}.jsonl"
    outside.write_text("{}")
    deeper = projects / "-home-exemple" / "sub"
    deeper.mkdir()
    (deeper / f"{UUID2}.jsonl").write_text("{}")
    (projects / "-home-exemple" / "notes.txt").write_text("x")
    (projects / "-home-exemple" / "settings.jsonl").write_text("x")
    link = projects / "-home-exemple" / f"{UUID2}.jsonl"
    link.symlink_to(secret)  # a link that leaves the folder, even named like a journal
    link_out = projects / "-home-exemple" / "0a1b2c3d-0000-4000-8000-0123456789ad.jsonl"
    link_out.symlink_to(outside)
    for bad in (secret, outside, deeper / f"{UUID2}.jsonl", projects / "-home-exemple" / "notes.txt",
                projects / "-home-exemple" / "settings.jsonl", link, link_out, projects / "-home-exemple" / "absent.jsonl",
                projects / ".." / ".claude" / ".credentials.json", projects):
        with pytest.raises(journal.SessionError):
            journal.safe_journal(bad, projects)


# -- finding the journal of a session --------------------------------------------------------------------------------------

def fake_proc(tmp_path, tree=((4242, 1), (4300, 4242), (5242, 1), (5300, 5242), (6242, 1), (6300, 6242))):
    """Three panes (pane pids 4242, 5242, 6242), each with a claude child (4300, 5300, 6300)."""
    proc = tmp_path / "proc"
    for pid, ppid in tree:
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "stat").write_text(f"{pid} (claude) S {ppid} 0 0")
    return proc


def declare(sessions, pid, sid, cwd, **extra):
    sessions.mkdir(exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": str(cwd), **extra}))


def tmux_by_pane(names_panes):
    def run(argv, **kw):
        if argv[1] == "list-sessions":
            out = "".join(f"{CREATED} {n} 1\n" for n in names_panes)
        else:
            out = names_panes[argv[3].lstrip("=")] + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")
    return run


A, B, C = "ultron-20-10-15h09", "ultron-fba-20-10-15h10", "ultron-20-10-15h11"


def test_each_session_reads_its_own_journal_in_the_same_cwd(projects, tmp_path):
    cwd = "/home/exemple"
    mine, other = projects / "-home-exemple" / f"{UUID}.jsonl", projects / "-home-exemple" / f"{UUID2}.jsonl"
    mine.write_text("{}\n")
    other.write_text("{}\n")
    sessions, proc = tmp_path / "sessions", fake_proc(tmp_path)
    declare(sessions, 4300, UUID, cwd, status="idle", tmux="x")
    declare(sessions, 5300, UUID2, cwd, status="busy")
    (sessions / "6300.key").write_text("SECRET")  # never opened, and not a session declaration anyway
    run = tmux_by_pane({A: "4242", B: "5242", C: "6242"})
    assert journal.find_journal(A, run, projects, proc, sessions) == mine.resolve()
    assert journal.find_journal(B, run, projects, proc, sessions) == other.resolve()
    with pytest.raises(journal.SessionError, match="pas encore démarrée"):
        journal.find_journal(C, run, projects, proc, sessions)  # no .json: never another session's journal
    with pytest.raises(journal.SessionError):
        journal.find_journal("ultron-19-10-15h39", run, projects, proc, sessions)  # unknown session
    with pytest.raises(journal.SessionError):
        journal.find_journal("../../etc/passwd", run, projects, proc, sessions)  # refused before any tmux call


def test_after_clear_the_current_session_id_is_followed(projects, tmp_path):
    cwd = "/home/exemple"
    first, cleared = projects / "-home-exemple" / f"{UUID}.jsonl", projects / "-home-exemple" / f"{UUID2}.jsonl"
    first.write_text("{}\n")
    cleared.write_text("{}\n")
    sessions, proc = tmp_path / "sessions", fake_proc(tmp_path)
    run = tmux_by_pane({A: "4242"})
    declare(sessions, 4300, UUID, cwd)
    assert journal.find_journal(A, run, projects, proc, sessions) == first.resolve()
    declare(sessions, 4300, UUID2, cwd)  # /clear: claude rewrites the file with the new sessionId
    assert journal.find_journal(A, run, projects, proc, sessions) == cleared.resolve()


def test_bad_declarations_are_ignored(projects, tmp_path):
    (projects / "-home-exemple" / f"{UUID}.jsonl").write_text("{}\n")
    sessions, proc = tmp_path / "sessions", fake_proc(tmp_path)
    run = tmux_by_pane({A: "4242"})
    sessions.mkdir()
    (sessions / "4300.json").write_text(json.dumps({"pid": 1, "sessionId": UUID, "cwd": "/home/exemple"}))
    with pytest.raises(journal.SessionError):
        journal.find_journal(A, run, projects, proc, sessions)
    (sessions / "4300.json").write_text("not json")
    with pytest.raises(journal.SessionError):
        journal.find_journal(A, run, projects, proc, sessions)
    declare(sessions, 4300, "../../x", "/home/exemple")  # traversal in sessionId
    with pytest.raises(journal.SessionError):
        journal.find_journal(A, run, projects, proc, sessions)


# -- through the catalogue ---------------------------------------------------------------------------------------------------

def test_three_readings_in_the_catalogue():
    for id_ in ("sessions", "session", "usage"):
        assert catalogue.PAR_ID[id_].type == "lecture"

    for bad in ("ultron-x", "../../.claude/.credentials.json", "ultron-19-10-15h38 --n=1", "-ultron-19-10-15h38"):
        with pytest.raises(catalogue.Refused):
            catalogue.valider("session", {"action": "session", "nom": bad})


def test_fixtures_are_cut_by_the_whitelist():
    out = catalogue.projeter(catalogue.PAR_ID["session"].donnees, apercu.LECTURES["session"])
    assert "chemin" not in json.dumps(out) and out["entrees"][0]["k"] == "utilisateur"
    assert "text" not in catalogue.projeter(catalogue.PAR_ID["usage"].donnees, apercu.LECTURES["usage"])


def test_cli_session_lire(projects, monkeypatch, capsys):
    f = projects / "-home-exemple" / f"{UUID}.jsonl"
    f.write_text(jl(user("Bonjour")))
    monkeypatch.setattr(journal, "find_journal", lambda name, *a, **k: f)
    monkeypatch.setattr(journal, "list_sessions", lambda *a, **k: [])  # todo #283: never the real tmux
    monkeypatch.setattr(ecran, "menu_of", lambda *a, **k: [])
    from ultron_hub.cli import main
    assert main(["session", "lire", "ultron-19-10-15h38", "--n", "5"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "ok" and out["data"]["entrees"][0]["texte"] == "Bonjour" and out["data"]["session"] == "ultron-19-10-15h38"
    assert str(f) not in json.dumps(out)


def test_bash_hook_and_compact_summary_not_rendered(projects):
    f = projects / "-home-exemple" / f"{UUID}.jsonl"
    f.write_text(jl(
        user("<bash-input>cat secret</bash-input>"), user("<bash-stdout>contenu</bash-stdout>"),
        user("<bash-stderr>err</bash-stderr>"), user("<user-prompt-submit-hook>x</user-prompt-submit-hook>"),
        user("résumé compact", isCompactSummary=True), user("vrai message")))
    out = journal.read_entries(f)
    assert [e["texte"] for e in out["entrees"]] == ["vrai message"]


def test_total_size_is_bounded_keeping_newest(projects):
    f = projects / "-home-exemple" / f"{UUID}.jsonl"
    f.write_text(jl(*[asst({"type": "text", "text": f"{i:02d}" + "x" * 5000}) for i in range(60)]))
    out = journal.read_entries(f)
    assert len(json.dumps(out)) < catalogue.DONNEES_MAX
    assert out["tronque"] and out["entrees"][-1]["texte"].startswith("59")


