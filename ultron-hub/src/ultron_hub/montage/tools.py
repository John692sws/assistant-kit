"""Subprocess helpers of the montage: fixed binaries, argv lists (never a shell), minimal environment, timeout that
kills the whole process group. Tests replace `run` (no real ffmpeg, Whisper or Remotion in the suite)."""

import os
import signal
import subprocess
from pathlib import Path

from ultron_hub.common.errors import UltronHubError

HOME = Path.home()
FFMPEG = str(HOME / ".local" / "bin" / "ffmpeg")
FFPROBE = str(HOME / ".local" / "bin" / "ffprobe")
PYTHON = "/usr/bin/python3"   # the skill's Python (faster-whisper, requests, its key loader)
NODE = "/usr/bin/node"


class MontageError(UltronHubError):
    """A failure explained to the user in one sentence."""


def env() -> dict[str, str]:
    home = str(HOME)
    return {"HOME": home, "PATH": f"{home}/.local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}


def run(argv: list[str], timeout_s: float, cwd: str = "/tmp") -> tuple[int | None, str, str]:
    """(exit code, stdout, stderr); exit code None on timeout (the process group is killed)."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env(), cwd=cwd, start_new_session=True, text=True, errors="replace")
    try:
        out, err = proc.communicate(timeout=timeout_s)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
        return None, out or "", err or ""
