"""Images and sound of a downloaded video (todo #91): plain ffmpeg, no AI.

Everything here works on a local file and goes through an injected `run(argv, timeout_s) -> (code, out, err)`
(`collect._run`), so tests never start ffmpeg. Results are plain dicts, ready for collected.json.
"""

import re
from pathlib import Path
from typing import Any, Callable

Run = Callable[[list[str], float], tuple["int | None", str, str]]

FFMPEG = f"{Path.home()}/.local/bin/ffmpeg"
FRAME_WIDTH = 768
MAX_FRAMES = 150
INTERVAL_S = 10.0          # one image at least every 10 s, even on a still shot
SCENE_THRESHOLD = 0.3
MIN_GAP_S = 0.5            # two scene changes closer than this give one image
SILENCE_DB = -35
SILENCE_MIN_S = 1.0
OTHER_SOUND_MIN_S = 3.0
LOUDNESS_WINDOW_S = 10
SCAN_TIMEOUT_S = 6 * 60
FRAME_TIMEOUT_S = 60

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_SIL_START_RE = re.compile(r"silence_start: (-?[0-9.]+)")
_SIL_END_RE = re.compile(r"silence_end: (-?[0-9.]+)")
_MOMENTARY_RE = re.compile(r"\bt: ([0-9.]+)\s.*?\bM:\s*(-?[0-9.]+|-inf)")
_INTEGRATED_RE = re.compile(r"\n\s*I:\s+(-?[0-9.]+) LUFS")
_PEAK_RE = re.compile(r"Peak:\s+(-?[0-9.]+|-inf) dBFS")


def scene_times(video: Path, run: Run) -> list[float]:
    """Seconds of the scene changes (ffmpeg `scene` score above the threshold)."""
    code, _, err = run([FFMPEG, "-nostats", "-hide_banner", "-i", str(video), "-an",
                        "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo", "-f", "null", "-"], SCAN_TIMEOUT_S)
    if code != 0:
        return []
    return [float(m.group(1)) for m in _PTS_RE.finditer(err)]


def plan_frames(scenes: list[float], duration: float, cap: int = MAX_FRAMES) -> list[dict[str, Any]]:
    """Moments to keep: the start, every scene change, and one image per INTERVAL_S without any; at most `cap`.

    Over the cap the list is thinned evenly (first image kept), so the whole video stays covered.
    """
    picked: list[tuple[float, str]] = [(0.0, "start")]
    for t in sorted(scenes):
        if 0 < t < duration and t - picked[-1][0] >= MIN_GAP_S:
            picked.append((t, "scene"))
    plan: list[tuple[float, str]] = []
    for i, (t, kind) in enumerate(picked):
        plan.append((t, kind))
        nxt = picked[i + 1][0] if i + 1 < len(picked) else duration
        fill = t + INTERVAL_S
        while fill < nxt - MIN_GAP_S:
            plan.append((fill, "interval"))
            fill += INTERVAL_S
    if len(plan) > cap:
        plan = [plan[(i * len(plan)) // cap] for i in range(cap)]
    return [{"t": round(t, 2), "kind": kind} for t, kind in plan]


def extract_frames(video: Path, plan: list[dict[str, Any]], out_dir: Path, run: Run) -> list[dict[str, Any]]:
    """Writes one JPEG (768 px wide) per planned moment; returns the plan with a `file` name for those that worked."""
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    done = []
    for i, item in enumerate(plan, 1):
        name = f"{i:03d}_t{int(item['t']):05d}.jpg"
        target = out_dir / name
        code, _, _ = run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-ss", f"{item['t']:.2f}", "-i", str(video),
                          "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2",
                          "-pix_fmt", "yuvj420p", "-q:v", "4", str(target)],
                         FRAME_TIMEOUT_S)
        if code == 0 and target.is_file():
            target.chmod(0o600)
            done.append({"file": name, **item})
    return done


def _intervals_minus(base: list[tuple[float, float]], cut: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = base
    for c0, c1 in sorted(cut):
        nxt = []
        for a, b in out:
            if c1 <= a or c0 >= b:
                nxt.append((a, b))
                continue
            if c0 > a:
                nxt.append((a, c0))
            if c1 < b:
                nxt.append((c1, b))
        out = nxt
    return out


def audio_profile(video: Path, duration: float, speech: list[tuple[float, float]], run: Run) -> dict[str, Any] | None:
    """Loudness, silences, and sound with no speech (music, ambience: ffmpeg cannot tell them apart). None on failure."""
    code, _, err = run([FFMPEG, "-nostats", "-hide_banner", "-i", str(video), "-vn", "-af",
                        f"ebur128=peak=true,silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_S}", "-f", "null", "-"],
                       SCAN_TIMEOUT_S)
    if code != 0 or "Audio:" not in err:
        return None
    silences: list[tuple[float, float]] = []
    start = None
    for line in err.splitlines():
        if (m := _SIL_START_RE.search(line)):
            start = max(0.0, float(m.group(1)))
        elif (m := _SIL_END_RE.search(line)) and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    if start is not None:  # silence running until the end of the file
        silences.append((start, duration))
    windows: dict[int, float] = {}
    for m in _MOMENTARY_RE.finditer(err):
        level = -120.0 if m.group(2) == "-inf" else float(m.group(2))
        idx = int(float(m.group(1)) // LOUDNESS_WINDOW_S)
        windows[idx] = max(windows.get(idx, -120.0), level)
    integrated = _INTEGRATED_RE.findall(err)
    peaks = _PEAK_RE.findall(err)
    loud_parts = _intervals_minus(_intervals_minus([(0.0, duration)], silences), speech)
    other = [(a, b) for a, b in loud_parts if b - a >= OTHER_SOUND_MIN_S]
    rnd = lambda pairs: [{"start": round(a, 1), "end": round(b, 1)} for a, b in pairs]  # noqa: E731
    return {
        "loudness_lufs": float(integrated[-1]) if integrated else None,
        "peak_dbfs": float(peaks[-1]) if peaks and peaks[-1] != "-inf" else None,
        "silences": rnd(silences),
        "sound_without_speech": rnd(other),
        "volume_per_window": [{"start": i * LOUDNESS_WINDOW_S, "max_lufs": round(v, 1)}
                              for i, v in sorted(windows.items())],
        "window_s": LOUDNESS_WINDOW_S,
        "silence_threshold_db": SILENCE_DB,
    }
