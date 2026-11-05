"""stdout read by a pipe that closes early (`ultron-hub todo list | head -c 10`).

Before the fix, the BrokenPipeError raised by the JSON write re-entered the error handler, which wrote to the same
closed stdout again: a traceback cascade on stderr and « Exception ignored in <stdout> » at exit. The real CLI runs
in a subprocess here, because the damage only shows at interpreter exit.
"""

import os
import subprocess
import sys
from pathlib import Path

from ultron_hub import cli

SRC = Path(__file__).resolve().parents[1] / "src"


def _child(project: Path, stdout) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(SRC), ULTRON_HUB_ROOT=str(project))
    return subprocess.run([sys.executable, "-m", "ultron_hub.cli", "todo", "list"],
                          stdout=stdout, stderr=subprocess.PIPE, env=env, timeout=60)


def test_closed_reader_gives_fixed_code_and_silent_stderr(project):
    read_end, write_end = os.pipe()
    os.close(read_end)  # nobody reads stdout: the first write gets EPIPE
    with os.fdopen(write_end, "wb") as out:
        proc = _child(project, out)
    stderr = proc.stderr.decode()
    assert proc.returncode == cli.BROKEN_PIPE_EXIT, stderr
    assert "Traceback" not in stderr
    assert "Exception ignored" not in stderr


def test_open_reader_still_gets_one_json_line(project):
    proc = subprocess.run([sys.executable, "-m", "ultron_hub.cli", "todo", "list"], capture_output=True,
                          env=dict(os.environ, PYTHONPATH=str(SRC), ULTRON_HUB_ROOT=str(project)), timeout=60)
    lines = proc.stdout.decode().strip().splitlines()
    assert len(lines) == 1 and '"status"' in lines[0]
    assert proc.returncode == 0, proc.stderr.decode()
