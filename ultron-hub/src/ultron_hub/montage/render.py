"""Remotion render of a checked plan: fixed React project montage/remotion (installed by `npm ci`, see the action
after merge `montage_install`), run as `node remotion-cli.js render` in a subprocess with a time limit, then a
check of the MP4 with ffprobe (1080x1920, H.264 + AAC, the expected duration)."""

import json
import os
import resource
import time
from pathlib import Path
from typing import Any

from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import Settings
from ultron_hub.montage import audio, tools
from ultron_hub.montage.plan import FPS, HEIGHT, WIDTH
from ultron_hub.montage.tools import MontageError

log = get_logger("montage")

COMPOSITION = "Montage"
ENTRY = Path("src") / "index.jsx"
CLI_JS = Path("node_modules") / "@remotion" / "cli" / "remotion-cli.js"
TIMEOUT_S = 15 * 60
CRF = 26                # a little lighter than the default 18: the boiling grain of « papier » made 88 MB (#436)
CONCURRENCY = 2         # 4 cores, 8 GB of RAM: two browser tabs at most
DURATION_TOLERANCE_S = 0.5
MAX_MP4_BYTES = 49 * 1024 * 1024   # Telegram bots send 50 MB at most


def project_dir(settings: Settings) -> Path:
    return settings.root / "montage" / "remotion"


def installed(settings: Settings) -> bool:
    return (project_dir(settings) / CLI_JS).is_file()


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    tmp.replace(path)


def argv(settings: Settings, folder: Path, props_file: Path, out: Path) -> list[str]:
    project = project_dir(settings)
    return [tools.NODE, str(project / CLI_JS), "render", str(project / ENTRY), COMPOSITION, str(out),
            f"--props={props_file}", f"--public-dir={folder}", f"--concurrency={CONCURRENCY}",
            "--codec=h264", f"--crf={CRF}", "--audio-codec=aac", "--log=error", "--overwrite"]


def check_mp4(path: Path, expected_s: float) -> dict[str, Any]:
    """ffprobe: vertical 1080x1920 H.264 with an AAC track, duration close to the voice. MontageError otherwise."""
    if not path.is_file():
        raise MontageError("Le rendu n'a pas produit de vidéo.")
    info = audio.probe(path)
    video = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    sound = next((s for s in info["streams"] if s.get("codec_type") == "audio"), {})
    if (video.get("width"), video.get("height")) != (WIDTH, HEIGHT) or video.get("codec_name") != "h264":
        raise MontageError("La vidéo rendue n'a pas le bon format (1080x1920 H.264 attendu).")
    if sound.get("codec_name") != "aac":
        raise MontageError("La vidéo rendue n'a pas de son.")
    if abs(info["duration"] - expected_s) > DURATION_TOLERANCE_S:
        raise MontageError(f"Durée inattendue : {info['duration']:.1f} s au lieu de {expected_s:.1f} s.")
    size = path.stat().st_size
    return {"duration_s": round(info["duration"], 2), "bytes": size, "width": WIDTH, "height": HEIGHT, "fps": FPS,
            "too_big_for_telegram": size > MAX_MP4_BYTES}


def render(settings: Settings, folder: Path, props_data: dict[str, Any], out: Path, expected_s: float) -> dict:
    """Renders props_data into `out` (MP4). Returns the ffprobe check + render time and peak memory of children."""
    if not installed(settings):
        raise MontageError("Remotion n'est pas installé sur le serveur : lance l'action « montage_install » "
                           "(🧰 Actions après fusion).")
    props_file = folder / "props.json"
    write_json(props_file, props_data)
    tmp = out.with_name(out.stem + ".part.mp4")
    started = time.monotonic()
    code, _, err = tools.run(argv(settings, folder, props_file, tmp), TIMEOUT_S, cwd=str(project_dir(settings)))
    elapsed = time.monotonic() - started
    if code is None:
        tmp.unlink(missing_ok=True)
        raise MontageError("Le rendu a dépassé 15 minutes.")
    if code != 0:
        log.warning("remotion render failed (exit %s): %s", code, err[-600:])
        tmp.unlink(missing_ok=True)
        raise MontageError("Le rendu de la vidéo a échoué.")
    try:
        checked = check_mp4(tmp, expected_s)
    except MontageError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)
    peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return {**checked, "render_s": round(elapsed, 1), "peak_child_mb": round(peak_kb / 1024)}


# --- measure (todo #436): is the band of the scenes alive and full? ---------------------------------------

BAND_Y, BAND_H = 260, 1120          # the band of the scenes (the captions keep the bottom)
MEASURE_FPS = 10
MEASURE_W, MEASURE_H = 108, 112     # the band, reduced, in grey levels
FROZEN_DIFF = 0.25                  # mean grey-level change between two frames below which the picture is frozen
FILL_TOLERANCE = 32                 # a pixel within this of the style's background level counts as empty
TARGET_FROZEN_S = 1.5               # target: the longest frozen run
TARGET_FILL = 0.25                  # target: share of the band that is not background
# Grey level of the surface the scenes sit on, per style (black, white card, paper card).
BACKGROUND_GRAY = {"noir": 0, "canevas": 255, "papier": 247}
MEASURE_TIMEOUT_S = 5 * 60


def analyse_frames(raw: bytes, background: int) -> dict[str, Any]:
    """Frozen run and filling of the reduced frames (MEASURE_W x MEASURE_H grey bytes, one after the other)."""
    size = MEASURE_W * MEASURE_H
    count = len(raw) // size
    if count < 2:
        raise MontageError("La vidéo est trop courte pour être mesurée.")
    pictures = [raw[i * size:(i + 1) * size] for i in range(count)]
    longest = run = frozen = 0
    for before, after in zip(pictures, pictures[1:]):
        change = sum(abs(a - b) for a, b in zip(before, after)) / size
        if change < FROZEN_DIFF:
            run += 1
            frozen += 1
            longest = max(longest, run)
        else:
            run = 0
    fill = sum(sum(1 for v in f if abs(v - background) > FILL_TOLERANCE) / size for f in pictures) / count
    return {"fige_max_s": round(longest / MEASURE_FPS, 2), "fige_pct": round(frozen / (count - 1), 3),
            "remplissage": round(fill, 3), "images": count}


def measure(video: Path, theme: str) -> dict[str, Any]:
    """Reads a rendered MP4 (nothing is changed): ffmpeg extracts the band of the scenes at MEASURE_FPS, reduced to
    grey levels, then `analyse_frames`. `fige_max_s` = longest run of frozen pictures, `remplissage` = mean share of
    the band that differs from the background of the style."""
    if not video.is_file() or video.is_symlink():
        raise MontageError("Cette vidéo n'existe pas.")
    raw_path = video.with_suffix(".measure.raw")
    vf = f"fps={MEASURE_FPS},crop=1080:{BAND_H}:0:{BAND_Y},scale={MEASURE_W}:{MEASURE_H}:flags=area,format=gray"
    try:
        code, _, err = tools.run([tools.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(video),
                                  "-an", "-vf", vf, "-f", "rawvideo", str(raw_path)], MEASURE_TIMEOUT_S)
        if code != 0 or not raw_path.is_file():
            log.warning("ffmpeg measure failed (exit %s): %s", code, err[-300:])
            raise MontageError("La mesure de la vidéo a échoué.")
        result = analyse_frames(raw_path.read_bytes(), BACKGROUND_GRAY.get(theme, 0))
    finally:
        raw_path.unlink(missing_ok=True)
    return {**result, "theme": theme, "cible_ok": (result["fige_max_s"] <= TARGET_FROZEN_S
                                                    and result["remplissage"] >= TARGET_FILL)}
