import os
import subprocess
from pathlib import Path

import pytest

from ultron_hub import repos
from ultron_hub.common.errors import ConfigError, InvalidRequestError
from ultron_hub.common.settings import load_settings


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


@pytest.fixture
def settings(tmp_path, project):
    base = tmp_path / "active"
    git_repo(base / "alpha")
    git_repo(base / "beta")
    (base / "notes").mkdir()  # not a repository
    (base / "alpha-todo-3").mkdir()
    (base / "alpha-todo-3" / ".git").write_text("gitdir: ../alpha/.git/worktrees/x\n")  # a linked worktree
    (base / "lien").symlink_to(base / "beta")
    (base / ".cache").mkdir()
    (project / "config" / "settings.yaml").write_text(
        f"repos:\n  dir: {base}\n  tests:\n    alpha: ['.venv/bin/python', '-m', 'pytest']\n    beta: [make, test]\n")
    return load_settings()


def test_list_repos_only_real_checkouts(settings, project):
    assert list(repos.list_repos(settings)) == ["alpha", "beta"]
    # the ultron-hub project itself counts once it is a git checkout, wherever it is
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    assert list(repos.list_repos(settings)) == ["alpha", "beta", "project"]
    with pytest.raises(InvalidRequestError, match="dépôt inconnu"):
        repos.repo_path(settings, "notes")


def test_repos_dir_defaults_to_the_project_parent(project):
    assert load_settings().repos_dir == project.parent


def test_settings_refuse_bad_values(project):
    (project / "config" / "settings.yaml").write_text("repos:\n  tests:\n    alpha: python -m pytest\n")
    with pytest.raises(ConfigError, match="repos.tests"):
        load_settings()
    (project / "config" / "settings.yaml").write_text("repos:\n  dir: relatif\n")
    with pytest.raises(ConfigError, match="absolute"):
        load_settings()


def test_test_command(settings):
    alpha = settings.repos_dir / "alpha"
    assert repos.test_command(settings, "alpha", alpha) == [str(alpha / ".venv/bin/python"), "-m", "pytest"]
    assert repos.test_command(settings, "beta", alpha) == ["make", "test"]
    assert repos.test_command(settings, "gamma", alpha) is None
    out = repos.run_tests(settings, "gamma", alpha, alpha)
    assert out["ok"] and not out["tested"] and "non testé" in out["detail"]
    out = repos.run_tests(settings, "alpha", alpha, alpha)  # no .venv in alpha
    assert not out["ok"] and "introuvable" in out["detail"]


def _fake_run_tests(monkeypatch, tmp_path, settings, seconds, returncode=0):
    exe = tmp_path / "py"
    exe.write_text("")
    monkeypatch.setattr(repos, "test_command", lambda *_: [str(exe)])
    clock = iter([100.0, 100.0 + seconds])
    monkeypatch.setattr(repos.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(repos.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, returncode, stdout="3 passed\n", stderr=""))
    return repos.run_tests(settings, "alpha", tmp_path, tmp_path)


def test_run_tests_warns_when_close_to_the_timeout(monkeypatch, tmp_path, settings, caplog):
    assert repos.TESTS_TIMEOUT_S == 900
    with caplog.at_level("WARNING", logger="ultron_hub.repos"):
        out = _fake_run_tests(monkeypatch, tmp_path, settings, 800)
    assert out["ok"] and "suite à 800 s, délai 900 s : à alléger" in out["warning"]
    assert out["detail"].startswith("3 passed") and out["warning"] in out["detail"]
    assert "step=tests-duree" in caplog.text


def test_run_tests_quiet_when_fast_enough(monkeypatch, tmp_path, settings, caplog):
    with caplog.at_level("WARNING", logger="ultron_hub.repos"):
        out = _fake_run_tests(monkeypatch, tmp_path, settings, 300)
    assert out == {"ok": True, "tested": True, "detail": "3 passed"} and not caplog.text


def test_run_tests_warning_kept_on_failure(monkeypatch, tmp_path, settings):
    out = _fake_run_tests(monkeypatch, tmp_path, settings, 750, returncode=1)
    assert not out["ok"] and "warning" in out


def test_sandbox_argv_hides_home_run_and_network(tmp_path):
    argv = repos.sandbox_argv(["true"], tmp_path / "t", [tmp_path / "venv"])
    joined = " ".join(argv)
    assert argv[0] == "bwrap" and argv[-2:] == ["--", "true"]
    assert f"--tmpfs {Path.home()}" in joined and "--tmpfs /run" in joined and "--unshare-all" in joined
    assert joined.index(f"--tmpfs {Path.home()}") < joined.index(f"--ro-bind {tmp_path / 'venv'}")
    assert f"--bind {tmp_path / 't'} {tmp_path / 't'}" in joined and "--clearenv" in joined


def _bwrap_works() -> bool:
    try:
        return subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "true"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _bwrap_works(), reason="bwrap unavailable here (e.g. already inside the sandbox)")
def test_real_sandbox_probe(tmp_path):
    """The real bwrap: the tree is writable, home secrets, /run sockets and the network are not reachable."""
    tree = Path.home() / ".cache" / f"ultron-probe-{os.getpid()}"  # under home, like a real worktree
    decoy = Path.home() / ".cache" / f"ultron-decoy-{os.getpid()}"
    tree.mkdir(parents=True)
    decoy.write_text("secret")
    try:
        script = (
            "import os, socket, pathlib\n"
            f"pathlib.Path('ok.txt').write_text('ok')\n"
            "res = []\n"
            f"for p in ({str(decoy)!r}, {str(Path.home() / '.ssh')!r}, '/run/user/{os.getuid()}/bus'):\n"
            "    res.append(os.path.exists(p))\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=3); res.append('net')\n"
            "except OSError:\n"
            "    res.append('nonet')\n"
            "try:\n"
            f"    open({str(Path.home() / 'escape.txt')!r}, 'w').write('x'); res.append('wrote-home')\n"
            "except OSError:\n"
            "    res.append('home-ro')\n"
            "print(res)\n")
        proc = subprocess.run(repos.sandbox_argv(["/usr/bin/python3", "-c", script], tree, []),
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "[False, False, False, 'nonet', 'wrote-home']"
        assert (tree / "ok.txt").read_text() == "ok"
        assert not (Path.home() / "escape.txt").exists()  # written to the throwaway tmpfs only
    finally:
        for p in tree.iterdir():
            p.unlink()
        tree.rmdir()
        decoy.unlink()


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_run_tests_bwrap_on_linux_in_clear_on_mac(monkeypatch, tmp_path, settings, system):
    """todo #390: no bwrap on macOS, the same command runs in clear; Linux keeps the sandbox."""
    from ultron_hub.common import plateforme
    exe = tmp_path / "py"
    exe.write_text("")
    monkeypatch.setattr(repos, "test_command", lambda *_: [str(exe), "-m", "pytest"])
    monkeypatch.setattr(plateforme.platform, "system", lambda: system)
    seen = []
    monkeypatch.setattr(repos.subprocess, "run", lambda argv, **k: seen.append(argv) or subprocess.CompletedProcess(
        argv, 0, stdout="3 passed\n", stderr=""))
    assert repos.run_tests(settings, "alpha", tmp_path, tmp_path)["ok"]
    if system == "Linux":
        assert seen == [repos.sandbox_argv([str(exe), "-m", "pytest"], tmp_path, [exe])]
    else:
        assert seen == [[str(exe), "-m", "pytest"]]
