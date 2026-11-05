import json
import subprocess

from ultron_hub.cli import main


def msg(mid, ts, out):
    usage = {"input_tokens": 10, "output_tokens": out, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": "claude-sonnet-5-5", "usage": usage}})


def commit(repo, n, when):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                    "-m", f"Truc (todo #{n})"], check=True, capture_output=True,
                   env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "PATH": "/usr/bin:/bin"})


def build(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    commit(repo, 5, "2026-10-18T10:30:00Z")
    commit(repo, 6, "2026-10-18T10:40:00Z")
    commit(repo, 7, "2026-10-18T12:30:00Z")
    state = tmp_path / "state"
    squad = state / "demo-1"
    squad.mkdir(parents=True)
    (squad / "etat.json").write_text(json.dumps({"debut_ts": 1792319400.0, "points_consommes": 3.0, "session_id": "sq-1"}))  # 2026-10-18T10:10Z
    result = {"type": "result", "total_cost_usd": 4.0, "usage": {"input_tokens": 1}}
    stream = squad / "session-1.jsonl"
    stream.write_text(json.dumps({"type": "system", "session_id": "sq-1"}) + "\n" + json.dumps(result))
    import os
    os.utime(stream, (1792323000.0, 1792323000.0))  # 11:10Z
    (state / "code").mkdir()  # no etat.json: ignored
    projects = tmp_path / "projects"
    mine = projects / "-home-ultron"
    mine.mkdir(parents=True)
    (mine / "abcdef123456.jsonl").write_text("\n".join([msg("m1", "2026-10-18T10:00:00Z", 5), msg("m2", "2026-10-18T11:00:00Z", 5)]))
    (mine / "sq-1.jsonl").write_text(msg("m9", "2026-10-18T10:00:00Z", 5))  # a squad's own transcript: not interactive
    other = projects / "-home-ultron-notes-escouade-autonome-essais-x"
    other.mkdir()
    (other / "zzz.jsonl").write_text(msg("m8", "2026-10-18T10:00:00Z", 5))
    return repo, state, projects


def test_compare(tmp_path, capsys):
    repo, state, projects = build(tmp_path)
    from ultron_hub.cout import compare
    d = compare.compare(5, state, projects, repo)
    sq = d["escouade"]
    assert [r["id"] for r in sq["sessions"]] == ["demo-1"]
    assert sq["sessions"][0]["todos_livrees"] == [5, 6] and sq["sessions"][0]["points"] == 3.0
    assert sq["resume"]["cout_par_todo"] == 2.0 and sq["resume"]["points_par_todo"] == 1.5
    it = d["interactif"]
    assert [r["id"] for r in it["sessions"]] == ["abcdef12"]
    assert it["sessions"][0]["todos_livrees"] == [5, 6] and it["sessions"][0]["points"] is None
    assert it["resume"]["points_par_todo"] is None and it["resume"]["cout_par_todo"] == round(it["resume"]["cout_usd"] / 2, 4)
    assert "estimation" in d["unite"] and "Par todo livrée" in d["text"]


def test_compare_cli_and_n(tmp_path, capsys):
    repo, state, projects = build(tmp_path)
    code = main(["cout", "compare", "--n", "1", "--etat-dir", str(state), "--projets-dir", str(projects)])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["command"] == "cout.compare" and out["data"]["n"] == 1
    assert "escouade" in out["summary"] and out["data"]["text"] == out["summary"]


def test_compare_empty(tmp_path):
    from ultron_hub.cout import compare
    d = compare.compare(5, tmp_path / "none", tmp_path / "nada", tmp_path)
    assert d["escouade"]["sessions"] == [] and d["interactif"]["resume"]["cout_par_todo"] is None
