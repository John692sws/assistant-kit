import json
import subprocess

from ultron_hub.cli import main


def line(mid, ts, out, model="claude-sonnet-5-5", **extra):
    usage = {"input_tokens": 10, "output_tokens": out, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": model, "usage": usage}, **extra})


def make(tmp_path):
    t = tmp_path / "proj" / "abc.jsonl"
    (tmp_path / "proj" / "abc" / "subagents").mkdir(parents=True)
    t.write_text("\n".join([
        line("m1", "2026-10-18T10:00:00.000Z", 5),
        line("m1", "2026-10-18T10:00:01.000Z", 50),  # same message rewritten: counted once, last line wins
        json.dumps({"type": "user", "timestamp": "2026-10-18T10:30:00.000Z"}),
        "not json",
    ]))
    (tmp_path / "proj" / "abc" / "subagents" / "agent-1.jsonl").write_text(
        line("m2", "2026-10-18T10:10:00.000Z", 20, model="claude-opus-5-5"))
    return t


def run(capsys, argv):
    code = main(argv)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_measure(tmp_path):
    from ultron_hub.cout import session
    repo = tmp_path / "repo"
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    repo.mkdir()
    git("init", "-q")
    for n, when in ((7, "2026-10-18T10:05:00Z"), (8, "2026-10-18T10:20:00Z"), (9, "2026-10-19T10:00:00Z")):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", f"Truc (todo #{n})"], check=True, capture_output=True,
                       env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "PATH": "/usr/bin:/bin"})
    d = session.measure(make(tmp_path), repo)
    assert (d["entree"], d["sortie"], d["cache_ecriture"], d["cache_lecture"]) == (20, 70, 200, 2000)
    assert d["messages"] == 2
    assert d["duree_s"] == 1800 and d["debut"] == "2026-10-18T10:00:00Z"
    assert d["todos_livrees"] == [7, 8]
    # sonnet m1: 10+250+125+100 = 485 ; opus m2: (10+100+125+100)*5 = 1675
    assert d["cout_usd"] == round((485 + 1675) * 3e-6, 4)
    assert d["cout_par_todo"] == round(d["cout_usd"] / 2, 4)


def test_cli_json(tmp_path, capsys):
    code, out = run(capsys, ["cout", "session", str(make(tmp_path))])
    assert code == 0 and out["command"] == "cout.session" and out["data"]["sortie"] == 70


def test_missing_transcript(tmp_path, capsys):
    code, out = run(capsys, ["cout", "session", str(tmp_path / "none.jsonl")])
    assert code != 0 and out["status"] == "error"
