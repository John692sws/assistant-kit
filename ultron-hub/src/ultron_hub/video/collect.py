"""Collection of a video (`ultron-hub video collect`): runs the hermes-video "watch" skill on a link, then extracts
the images and the sound measures, and returns everything the neutral analysis needs (no AI here).

The skill is started as a plain subprocess (no shell), with a minimal environment. It writes progress lines on
stderr, among them "[watch] report written to /tmp/watch-XXXX/report.json"; stdout may be empty, so the report
file is what we read. Its working folder (/tmp/watch-*) is always removed afterwards.
"""

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

from ultron_hub.common.errors import UltronHubError
from ultron_hub.common.logging import get_logger
from ultron_hub.common.settings import load_settings
from ultron_hub.video import media
from ultron_hub.video.links import is_allowed

log = get_logger("video")

PYTHON = "/usr/bin/python3"
SKILL_CLI = Path("skills") / "watch" / "scripts" / "cli.py"
TIMEOUT_S = 15 * 60
PROBE_TIMEOUT_S = 60
MAX_DURATION_S = 45 * 60
MAX_TRANSCRIPT_CHARS = 60_000
_WORKDIR_RE = re.compile(r"/tmp/watch-[A-Za-z0-9_]+")
_REPORT_RE = re.compile(r"report written to (\S+)")
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov")
IMAGES_DIR = "images"
DESCRIPTION_MAX = 5000


class VideoError(UltronHubError):
    """A failure explained to the user in one sentence."""


SHIM_DIR = Path(__file__).resolve().parent / "shim"  # ffmpeg relay for the skill (todo #476)


def _env(shim: bool = False) -> dict[str, str]:
    home = str(Path.home())
    path = f"{home}/.local/bin:/usr/bin:/bin"
    return {"HOME": home, "PATH": f"{SHIM_DIR}:{path}" if shim else path, "LANG": "C.UTF-8"}


def _run(argv: list[str], timeout_s: float) -> tuple[int | None, str, str]:
    """Runs argv without a shell; (None, ...) on timeout, the whole process group being killed.
    The skill (started with PYTHON) alone gets the ffmpeg relay first in its PATH."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=_env(shim=argv[0] == PYTHON), cwd="/tmp", start_new_session=True, text=True, errors="replace")
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


def skill_cli(skill_dir: Path) -> Path:
    return skill_dir / SKILL_CLI


def probe_duration(url: str) -> float | None:
    """Duration in seconds before any download (yt-dlp), None when it cannot be told: the skill then decides."""
    try:
        code, out, _ = _run([f"{Path.home()}/.local/bin/yt-dlp", "--no-playlist", "--skip-download",
                             "--print", "duration", url], PROBE_TIMEOUT_S)
        return float(out.strip().splitlines()[-1]) if code == 0 and out.strip() else None
    except (OSError, ValueError):
        return None


def cleanup(*dirs: str | None) -> None:
    """Removes skill working folders; anything not exactly /tmp/watch-<id> (or a symlink) is left alone."""
    for raw in dirs:
        if raw and _WORKDIR_RE.fullmatch(raw) and Path(raw).is_dir() and not Path(raw).is_symlink():
            shutil.rmtree(raw, ignore_errors=True)


def _workdirs(*texts: str) -> list[str]:
    found = []
    for text in texts:
        for match in _REPORT_RE.finditer(text):
            parent = str(Path(match.group(1)).parent)
            if _WORKDIR_RE.fullmatch(parent) and parent not in found:
                found.append(parent)
    return found


def explain_failure(stderr: str, stdout: str = "") -> str:
    low = f"{stderr}\n{stdout}".lower()
    if any(w in low for w in ("private video", "sign in", "login required", "members-only", "not available",
                              "video unavailable", "has been removed")):
        return "Cette vidéo est privée, supprimée ou inaccessible."
    if any(w in low for w in ("unsupported url", "not a valid url", "invalid url", "is not a valid")):
        return "Ce lien n'est pas une vidéo que je sais lire."
    if "tiktok" in low and any(w in low for w in ("403", "429", "blocked", "ip address", "captcha", "unable to extract",
                                                  "rate")):
        return "TikTok bloque le téléchargement depuis le serveur. Essaie un lien YouTube."
    return "Le téléchargement ou la transcription a échoué. Réessaie, ou envoie un autre lien."


def timestamp(seconds: Any) -> str:
    total = int(seconds) if isinstance(seconds, (int, float)) else 0
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def transcript_text(segments: Any, limit: int = MAX_TRANSCRIPT_CHARS) -> tuple[str, bool]:
    """Timestamped lines "[m:ss] text", cut at `limit` characters (whole lines); second value = was cut."""
    lines, size = [], 0
    for seg in segments if isinstance(segments, list) else []:
        if not isinstance(seg, dict) or not str(seg.get("text") or "").strip():
            continue
        line = f"[{timestamp(seg.get('start'))}] {' '.join(str(seg['text']).split())}"
        if size + len(line) + 1 > limit:
            return "\n".join(lines), True
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines), False


def read_report(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VideoError("Le rapport de la vidéo est illisible.") from None
    if not isinstance(report, dict):
        raise VideoError("Le rapport de la vidéo est illisible.")
    return report


VIDEO_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
COLLECTED = "collected.json"


def video_dir(video_id: str) -> Path:
    """var/video/<id>: the id is validated before it becomes part of a path."""
    if not isinstance(video_id, str) or VIDEO_ID_RE.fullmatch(video_id) is None:
        raise VideoError("Identifiant de vidéo invalide.")
    return load_settings().var_dir / "video" / video_id


def save_collected(video_id: str, data: dict[str, Any]) -> Path:
    """Keeps what `collect_video` returned (title, transcript...) so every mode reuses it: no second download."""
    folder = video_dir(video_id)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / COLLECTED
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path


def load_collected(video_id: str) -> dict[str, Any]:
    path = video_dir(video_id) / COLLECTED
    if not path.is_file() or path.is_symlink():
        raise VideoError("Cette vidéo n'a pas été téléchargée (ou son dossier a été purgé). Renvoie le lien.")
    data = read_report(path)
    if not is_allowed(data.get("url")) or not isinstance(data.get("transcript"), str):
        raise VideoError("Les données de cette vidéo sont illisibles.")
    return data


def _segments(raw: Any) -> list[dict[str, Any]]:
    out = []
    for seg in raw if isinstance(raw, list) else []:
        text = " ".join(str(seg.get("text") or "").split()) if isinstance(seg, dict) else ""
        if text:
            start = float(seg.get("start") or 0)
            out.append({"start": round(start, 2), "end": round(float(seg.get("end") or start), 2), "text": text})
    return out


def _metadata(info: dict[str, Any]) -> dict[str, Any]:
    chapters = [{"start": c.get("start_time"), "end": c.get("end_time"), "title": str(c.get("title") or "")[:200]}
                for c in (info.get("chapters") or []) if isinstance(c, dict)]
    return {
        "title": str(info.get("title") or "")[:300],
        "author": str(info.get("uploader") or info.get("channel") or "")[:200],
        "upload_date": str(info.get("upload_date") or ""),  # YYYYMMDD
        "description": str(info.get("description") or "")[:DESCRIPTION_MAX],
        "chapters": chapters,
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "tags": [str(t)[:60] for t in (info.get("tags") or [])[:30]],
    }


def _full_collection(workdir: Path, report: dict[str, Any], video_id: str) -> dict[str, Any]:
    """Images + sound + metadata + timed transcript (todo #91). Never fails the collection: gaps become warnings."""
    warnings: list[str] = []
    segments = _segments(report.get("transcript"))
    info: dict[str, Any] = {}
    try:
        info = json.loads((workdir / "video.info.json").read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            info = {}
    except (OSError, ValueError):
        warnings.append("métadonnées de la plateforme illisibles")
    duration = report.get("duration") if isinstance(report.get("duration"), (int, float)) else info.get("duration")
    video = next((p for ext in VIDEO_EXTS for p in sorted(workdir.glob(f"video*{ext}")) if p.is_file()), None)
    audio, frames = None, []
    if video is None or not isinstance(duration, (int, float)) or duration <= 0:
        warnings.append("fichier vidéo introuvable : ni images ni analyse du son")
    else:
        speech = [(s["start"], s["end"]) for s in segments]
        audio = media.audio_profile(video, float(duration), speech, _run)
        if audio is None:
            warnings.append("analyse du son impossible (vidéo sans piste audio ?)")
        plan = media.plan_frames(media.scene_times(video, _run), float(duration))
        images = video_dir(video_id) / IMAGES_DIR
        images.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        frames = media.extract_frames(video, plan, images, _run)
        if not frames:
            warnings.append("aucune image extraite")
    return {"metadata": _metadata(info), "transcript_segments": segments, "audio": audio,
            "images_dir": IMAGES_DIR, "frames": frames, "full_warnings": warnings}


def collect_video(url: str, video_id: str) -> dict[str, Any]:
    """Downloads and reads a video; images and sound analysis go to var/video/<video_id>/ (the id is validated).
    A video without speech is a normal case: its transcript is empty."""
    video_dir(video_id)  # validates the id
    if not is_allowed(url):
        raise VideoError("Lien refusé : seuls les liens https YouTube et TikTok sont acceptés.")
    skill_dir = load_settings().video_skill_dir
    cli = skill_cli(skill_dir)
    if not cli.is_file():
        raise VideoError("Le skill de lecture vidéo est introuvable sur le serveur.")
    duration = probe_duration(url)
    if duration is not None and duration > MAX_DURATION_S:
        raise VideoError(f"Vidéo trop longue ({int(duration // 60)} min) : 45 minutes au maximum.")

    workdirs: list[str] = []
    try:
        # --keep-video: images and sound are read from the downloaded file before cleanup().
        argv = [PYTHON, str(cli), url, "--output", "json", "--keep-video"]
        code, out, err = _run(argv, TIMEOUT_S)
        workdirs = _workdirs(err, out)
        if code is None:
            raise VideoError("Le traitement a dépassé 15 minutes : vidéo trop longue ou serveur occupé.")
        reports = [m.group(1) for m in _REPORT_RE.finditer(err)]
        if code != 0 or not reports:
            log.warning("video skill failed (exit %s): %s", code, err[-400:])
            raise VideoError(explain_failure(err, out))
        report_path = Path(reports[-1])
        if not _WORKDIR_RE.fullmatch(str(report_path.parent)) or report_path.name != "report.json":
            raise VideoError("Le rapport de la vidéo est illisible.")
        report = read_report(report_path)
        workdirs += [d for d in [str(report.get("working_dir") or "")] if d not in workdirs]
        full = _full_collection(report_path.parent, report, video_id)
    finally:
        cleanup(*workdirs)

    duration = report.get("duration")
    if isinstance(duration, (int, float)) and duration > MAX_DURATION_S:
        raise VideoError(f"Vidéo trop longue ({int(duration // 60)} min) : 45 minutes au maximum.")
    text, truncated = transcript_text(report.get("transcript"))
    return {**full,
        "url": url,
        "title": str(report.get("title") or "")[:300],
        "author": str(report.get("uploader") or "")[:200],
        "duration_s": round(duration) if isinstance(duration, (int, float)) else None,
        "duration": timestamp(duration) if isinstance(duration, (int, float)) else "?",
        "transcript_source": str(report.get("transcript_source") or ""),
        "transcript_truncated": truncated,
        "warnings": [str(w)[:200] for w in (report.get("warnings") or [])[:5]] if isinstance(
            report.get("warnings"), list) else [],
        "transcript": text,
    }
