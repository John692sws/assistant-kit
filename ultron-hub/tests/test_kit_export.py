import json
import subprocess

import pytest

from ultron_hub.cli import main

CONFIG = "include:\n  - src/*\n  - README.md\nexclude:\n  - src/secret/*\n  - var/*\n  - kit.yaml\n"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args], check=True,
                   capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    for rel, text in {"src/a.py": "x = 1\n", "src/secret/k.py": "k\n", "README.md": "hi\n", "extra/z.txt": "z\n",
                      "var/runs/r.json": "{}\n"}.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    (r / "kit.yaml").write_text(CONFIG)
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    (r / "src" / "untracked.py").write_text("u\n")  # working tree only, must not leak
    (r / "src" / "a.py").write_text("x = 2  # uncommitted\n")
    lst = tmp_path / "interdits.txt"
    lst.write_text("zzzforbidden\n")
    return r, lst


def run(capsys, repo, out, lst, *extra):
    r, _ = repo
    code = main(["kit", "export", str(out), "--depot", str(r), "--config", str(r / "kit.yaml"),
                 "--interdits", str(lst), *extra])
    return code, json.loads(capsys.readouterr().out)


def test_allowlist_respected(repo, tmp_path, capsys):
    out = tmp_path / "out"
    code, res = run(capsys, repo, out, repo[1])
    assert code == 0 and res["status"] == "ok" and res["data"]["publiable"] is True
    tree = sorted(p.relative_to(out / "ultron-hub").as_posix() for p in (out / "ultron-hub").rglob("*") if p.is_file())
    assert tree == ["README.md", "src/a.py"]
    assert (out / "ultron-hub/src/a.py").read_text() == "x = 1\n"  # committed content, not working tree


def test_unclassified_reported_not_copied(repo, tmp_path, capsys):
    code, res = run(capsys, repo, tmp_path / "out", repo[1])
    assert res["data"]["non_classes"] == ["extra/z.txt"]
    assert not (tmp_path / "out/ultron-hub/extra").exists()


def test_var_never_copied(repo, tmp_path, capsys):
    run(capsys, repo, tmp_path / "out", repo[1])
    assert not (tmp_path / "out/ultron-hub/var").exists()


def test_non_empty_output_refused(repo, tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep").write_text("x")
    code, res = run(capsys, repo, out, repo[1])
    assert code == 1 and res["status"] == "error" and (out / "keep").exists()
    assert not (out / "ultron-hub").exists()


def test_finding_means_not_publishable(repo, tmp_path, capsys):
    r, lst = repo
    (r / "README.md").write_text("zzzforbidden\n")
    _git(r, "commit", "-q", "-am", "leak")
    out = tmp_path / "out"
    code, res = run(capsys, repo, out, lst)
    assert code == 1 and res["status"] == "found" and res["data"]["publiable"] is False
    assert res["data"]["findings"] == [{"path": "ultron-hub/README.md", "line": 1, "rule": "interdit-perso"}]
    assert "zzzforbidden" not in json.dumps(res)
    assert (out / "ultron-hub").is_dir()


def test_essai_writes_nothing(repo, tmp_path, capsys):
    out = tmp_path / "out"
    code, res = run(capsys, repo, out, repo[1], "--essai")
    assert code == 0 and not out.exists()
    d = res["data"]
    assert (d["retenus"], d["exclus"]) == (2, 3) and d["non_classes"] == ["extra/z.txt"]
    assert d["fichiers_retenus"] == ["README.md", "src/a.py"]
    assert d["fichiers_exclus"] == ["kit.yaml", "src/secret/k.py", "var/runs/r.json"]


def test_missing_interdits_refused_before_writing(repo, tmp_path, capsys):
    out = tmp_path / "out"
    code, res = run(capsys, repo, out, tmp_path / "absent.txt")
    assert code == 1 and res["status"] == "error" and not out.exists()


def test_replacements_and_kit_folders(repo, tmp_path, capsys):
    r, lst = repo
    (r / "src" / "p.py").write_text('H = "/home/exemple3"  # box zedbox, ultron stays\n')
    (r / "kit/gabarits").mkdir(parents=True)
    (r / "kit/gabarits/a.md").write_text("{{PRENOM}}\n")
    (r / "kit/setup").mkdir()
    (r / "kit/setup/remplir.py").write_text("print(1)\n")
    (r / "kit/README.md").write_text("lis-moi\n")
    (r / "kit/CLAUDE.md").write_text("consignes\n")
    (r / "kit.yaml").write_text(CONFIG + "  - kit/*\nremplacer:\n  /home/exemple3: '{{HOME}}'\n  zedbox: '{{MACHINE}}'\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "more")
    out = tmp_path / "out"
    code, res = run(capsys, repo, out, lst)
    assert code == 0 and res["data"]["non_classes"] == ["extra/z.txt"]
    assert (out / "ultron-hub/src/p.py").read_text() == 'H = "{{HOME}}"  # box {{MACHINE}}, ultron stays\n'
    assert (out / "gabarits/a.md").read_text() == "{{PRENOM}}\n"
    assert (out / "setup/remplir.py").exists() and not (out / "ultron-hub/kit").exists()
    assert (out / "README.md").read_text() == "lis-moi\n" and (out / "CLAUDE.md").read_text() == "consignes\n"


def test_symlink_to_kept_file_is_copied(repo, tmp_path, capsys):
    """Todo #484: tasks/video-analyse-lot/prompt.md links to ../video-analyse/prompt.md; the kit needs the file."""
    r, lst = repo
    (r / "src" / "lot").mkdir()
    (r / "src" / "lot" / "a.py").symlink_to("../a.py")
    (r / "src" / "lot" / "out.py").symlink_to("../../extra/z.txt")  # target not kept
    (r / "src" / "lot" / "abs.py").symlink_to("/etc/hostname")
    _git(r, "add", "-A", "src/lot")
    _git(r, "commit", "-q", "-m", "links")
    out = tmp_path / "out"
    code, res = run(capsys, repo, out, lst)
    assert code == 0
    copy = out / "ultron-hub/src/lot/a.py"
    assert copy.is_file() and not copy.is_symlink() and copy.read_text() == "x = 1\n"
    assert not (out / "ultron-hub/src/lot/out.py").exists()
    assert not (out / "ultron-hub/src/lot/abs.py").exists()
