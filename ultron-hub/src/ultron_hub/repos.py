"""Git repositories the developer agents can work on, and how their code is tested.

Repositories = git checkouts directly under `repos.dir` (settings.yaml, default: the parent of the
ultron-hub project, ~/workspace/projects/active). Linked worktrees (`.git` is a file) are not repositories.

Tests of untrusted code (a branch about to be merged) run in bubblewrap: home, /run
and /tmp hidden, no network, writes only in the tested tree.
"""

import logging
import re
import subprocess
import time
from pathlib import Path

from ultron_hub.common.errors import InvalidRequestError
from ultron_hub.common.settings import Settings

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}")
TESTS_TIMEOUT_S = 900
TESTS_WARN_PCT = 80  # a run longer than this share of the timeout is flagged (drift seen at every merge)

log = logging.getLogger("ultron_hub.repos")


def list_repos(settings: Settings) -> dict[str, Path]:
    """{name: path} of the repositories, sorted; the ultron-hub project itself is always one of them."""
    found: dict[str, Path] = {}
    base = settings.repos_dir
    if base and base.is_dir():
        for path in sorted(base.iterdir()):
            if NAME_RE.fullmatch(path.name) and not path.is_symlink() and (path / ".git").is_dir():
                found[path.name] = path
    if (settings.root / ".git").is_dir():
        found.setdefault(settings.root.name, settings.root)
    return dict(sorted(found.items()))


def repo_path(settings: Settings, name: str) -> Path:
    repos = list_repos(settings)
    if name not in repos:
        raise InvalidRequestError(f"dépôt inconnu : {name!r} (connus : {', '.join(repos) or 'aucun'})")
    return repos[name]


def test_command(settings: Settings, name: str, repo: Path) -> list[str] | None:
    """Test command of a repository (settings.yaml `repos.tests`), None if it has none.

    A relative executable (`.venv/bin/python`) is taken from the repository's main checkout.
    """
    cmd = settings.repo_tests.get(name)
    if not cmd:
        return None
    exe = Path(cmd[0])
    return [str(repo / exe) if "/" in cmd[0] and not exe.is_absolute() else cmd[0], *cmd[1:]]


def sandbox_argv(cmd: list[str], tree: Path, read_only: list[Path]) -> list[str]:
    """bwrap command line: the whole system read-only, home, /run (D-Bus, systemd, tailscaled sockets)
    and /tmp replaced by empty dirs, `read_only` dirs put back read-only, `tree` writable, no network."""
    home = str(Path.home())
    argv = ["bwrap", "--ro-bind", "/", "/", "--tmpfs", home, "--tmpfs", "/run", "--tmpfs", "/tmp",
            "--dev", "/dev", "--proc", "/proc"]
    for path in read_only:
        argv += ["--ro-bind", str(path), str(path)]
    argv += ["--bind", str(tree), str(tree), "--unshare-all", "--die-with-parent", "--new-session",
             "--chdir", str(tree), "--clearenv", "--setenv", "HOME", home, "--setenv", "PATH", "/usr/bin:/bin",
             "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--", *cmd]
    return argv


def run_tests(settings: Settings, name: str, repo: Path, tree: Path) -> dict:
    """Runs the repository's tests on `tree` in the sandbox: {"ok", "tested", "detail"}."""
    cmd = test_command(settings, name, repo)
    if cmd is None:
        return {"ok": True, "tested": False, "detail": f"aucune commande de test pour {name} : non testé"}
    exe = Path(cmd[0])
    if not exe.is_absolute() or not exe.exists():
        return {"ok": False, "tested": False, "detail": f"commande de test introuvable : {cmd[0]}"}
    # Only the dir holding the executable (e.g. the main checkout's .venv) is put back, read-only.
    read_only = []
    if repo in exe.parents:
        read_only.append(repo / exe.relative_to(repo).parts[0])
    started = time.monotonic()
    try:
        from ultron_hub.common import plateforme
        # todo #390: no bwrap on macOS, the tests run in clear there (the kit's v1)
        argv = cmd if plateforme.est_mac() else sandbox_argv(cmd, tree, read_only)
        proc = subprocess.run(argv, cwd=tree, capture_output=True, text=True,
                              timeout=TESTS_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        # todo #401: "timeout" tells a slow suite from a red one (the 🔀 of #393 on 25/10 read « les tests échouent »)
        return {"ok": False, "tested": True, "timeout": True, "detail": f"tests trop longs (> {TESTS_TIMEOUT_S} s)"}
    except OSError as exc:
        return {"ok": False, "tested": False, "detail": f"sandbox de test indisponible : {exc}"}
    elapsed = time.monotonic() - started
    warning = None
    if elapsed > TESTS_TIMEOUT_S * TESTS_WARN_PCT / 100:
        warning = f"⚠️ suite à {elapsed:.0f} s, délai {TESTS_TIMEOUT_S} s : à alléger"
        log.warning("step=tests-duree %s (repo %s)", warning, name)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    last = lines[-1] if lines else (proc.stderr.strip().splitlines() or ["aucune sortie"])[-1]
    if proc.returncode != 0:
        failed = [line for line in lines if line.startswith(("FAILED", "ERROR"))][:5]
        out = {"ok": False, "tested": True, "detail": "\n".join(failed + [last])[-800:]}
    else:
        out = {"ok": True, "tested": True, "detail": last}
    if warning:
        out["warning"] = warning
        out["detail"] = f"{out['detail']}\n{warning}"
    return out
