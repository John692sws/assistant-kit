"""Full video collection (todo #91): images, sound, metadata. No network, no real skill, no real ffmpeg."""

import json
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from ultron_hub.video import collect, media

GOOD = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
VIDEO_ID = "20261017T065537Z-913b02cd"

AUDIO_STDERR = """Stream #0:1: Audio: aac
[Parsed_ebur128_0 @ 0x1] t: 0.0999792 TARGET:-23 LUFS    M: -120.7 S:-120.7     I: -70.0 LUFS
[Parsed_ebur128_0 @ 0x1] t: 4.5 TARGET:-23 LUFS    M: -18.0 S:-20.0     I: -21.0 LUFS
[Parsed_ebur128_0 @ 0x1] t: 12.0 TARGET:-23 LUFS    M: -inf S:-120.7     I: -21.0 LUFS
[Parsed_silencedetect_1 @ 0x2] silence_start: 12
[Parsed_silencedetect_1 @ 0x2] silence_end: 14 | silence_duration: 2
[Parsed_silencedetect_1 @ 0x2] silence_start: 18
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -21.5 LUFS
  True peak:
    Peak:       -3.2 dBFS
"""


def test_plan_has_start_scenes_and_ten_second_fill():
    plan = media.plan_frames([3.0, 3.2, 35.0], 50.0)
    assert [(p["t"], p["kind"]) for p in plan] == [
        (0.0, "start"), (3.0, "scene"), (13.0, "interval"), (23.0, "interval"), (33.0, "interval"),
        (35.0, "scene"), (45.0, "interval")]  # 3.2 is too close to 3.0


def test_plan_still_shot_gets_one_image_every_ten_seconds():
    assert [p["t"] for p in media.plan_frames([], 31.0)] == [0.0, 10.0, 20.0, 30.0]


def test_plan_is_capped_and_keeps_the_whole_video_covered():
    plan = media.plan_frames([float(t) for t in range(1, 2000)], 2000.0)
    assert len(plan) == media.MAX_FRAMES and plan[0]["t"] == 0.0 and plan[-1]["t"] > 1900


def test_audio_profile_parses_silences_loudness_and_other_sound():
    calls = []

    def run(argv, timeout_s):
        calls.append(argv)
        return 0, "", AUDIO_STDERR

    audio = media.audio_profile(Path("v.mp4"), 20.0, [(0.0, 4.0)], run)
    assert audio["silences"] == [{"start": 12.0, "end": 14.0}, {"start": 18.0, "end": 20.0}]
    assert audio["sound_without_speech"] == [{"start": 4.0, "end": 12.0}, {"start": 14.0, "end": 18.0}]
    assert audio["loudness_lufs"] == -21.5 and audio["peak_dbfs"] == -3.2
    assert audio["volume_per_window"] == [{"start": 0, "max_lufs": -18.0}, {"start": 10, "max_lufs": -120.0}]
    assert "-vn" in calls[0]


def test_audio_profile_without_audio_track_is_none():
    assert media.audio_profile(Path("v.mp4"), 5.0, [], lambda a, t: (0, "", "Stream #0:0: Video: h264")) is None
    assert media.audio_profile(Path("v.mp4"), 5.0, [], lambda a, t: (1, "", "Audio:")) is None


def test_extract_frames_names_modes_and_skips_failures(tmp_path):
    def run(argv, timeout_s):
        out = Path(argv[-1])
        if "t00010" in out.name:
            return 1, "", "boom"
        out.write_bytes(b"jpg")
        return 0, "", ""

    plan = [{"t": 0.0, "kind": "start"}, {"t": 10.0, "kind": "interval"}, {"t": 20.5, "kind": "scene"}]
    frames = media.extract_frames(Path("v.mp4"), plan, tmp_path / "images", run)
    assert [f["file"] for f in frames] == ["001_t00000.jpg", "003_t00020.jpg"]
    assert stat.S_IMODE((tmp_path / "images").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "images" / "001_t00000.jpg").stat().st_mode) == 0o600


@pytest.fixture
def full(monkeypatch, project):
    skill_dir = project / "skill"
    cli_file = skill_dir / "skills" / "watch" / "scripts" / "cli.py"
    cli_file.parent.mkdir(parents=True)
    cli_file.write_text("")
    with (project / "config" / "settings.yaml").open("a") as fh:
        fh.write(f"video:\n  skill_dir: {skill_dir}\n")
    work = Path(tempfile.mkdtemp(prefix="watch-", dir="/tmp"))
    state = {"calls": [], "speech": True, "video": True, "work": work}

    def run(argv, timeout_s):
        state["calls"].append(argv)
        if argv[0].endswith("ffmpeg"):
            if "-frames:v" in argv:
                Path(argv[-1]).write_bytes(b"jpg")
                return 0, "", ""
            if any("scene" in a for a in argv):
                return 0, "", "pts_time:12.5 x\n"
            return 0, "", AUDIO_STDERR
        work.mkdir(exist_ok=True)
        segs = [{"start": 0, "end": 4, "text": "hello  world"}] if state["speech"] else []
        report = {"title": "T", "uploader": "U", "duration": 20.0, "transcript": segs, "transcript_source": "whisper",
                  "working_dir": str(work)}
        (work / "report.json").write_text(json.dumps(report))
        (work / "video.info.json").write_text(json.dumps({
            "title": "T", "uploader": "U", "upload_date": "20240101", "description": "d" * 6000, "view_count": 7,
            "chapters": [{"start_time": 0, "end_time": 5, "title": "Intro"}]}))
        if state["video"]:
            (work / "video.mp4").write_bytes(b"x")
        return 0, "", f"[watch] report written to {work}/report.json\n"

    monkeypatch.setattr(collect, "_run", run)
    monkeypatch.setattr(collect, "probe_duration", lambda url: None)
    yield state
    shutil.rmtree(work, ignore_errors=True)


def test_full_collection_keeps_images_sound_metadata_and_cleans_up(full, project):
    data = collect.collect_video(GOOD, VIDEO_ID)
    assert "--keep-video" in full["calls"][0]
    assert data["metadata"]["upload_date"] == "20240101" and data["metadata"]["view_count"] == 7
    assert len(data["metadata"]["description"]) == collect.DESCRIPTION_MAX
    assert data["metadata"]["chapters"] == [{"start": 0, "end": 5, "title": "Intro"}]
    assert data["transcript_segments"] == [{"start": 0.0, "end": 4.0, "text": "hello world"}]
    assert data["audio"]["silences"][0] == {"start": 12.0, "end": 14.0}
    assert [(f["file"], f["kind"]) for f in data["frames"]] == [
        ("001_t00000.jpg", "start"), ("002_t00010.jpg", "interval"), ("003_t00012.jpg", "scene")]
    assert data["images_dir"] == "images" and data["full_warnings"] == []
    images = collect.video_dir(VIDEO_ID) / "images"
    assert sorted(p.name for p in images.iterdir()) == [f["file"] for f in data["frames"]]
    assert not full["work"].exists()
    path = collect.save_collected(VIDEO_ID, data)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_video_without_speech_is_accepted_when_collecting_fully(full):
    full["speech"] = False
    data = collect.collect_video(GOOD, VIDEO_ID)
    assert data["transcript"] == "" and data["transcript_segments"] == [] and data["frames"]
    assert data["audio"]["sound_without_speech"]  # all the loud part is non-speech


def test_missing_video_file_gives_a_warning_not_a_failure(full):
    full["video"] = False
    data = collect.collect_video(GOOD, VIDEO_ID)
    assert data["frames"] == [] and data["audio"] is None and "introuvable" in data["full_warnings"][0]


def test_a_silent_collection_is_kept_and_read_back(full):
    full["speech"] = False
    silent = collect.collect_video(GOOD, VIDEO_ID)
    collect.save_collected(VIDEO_ID, silent)
    back = collect.load_collected(VIDEO_ID)  # the old « aucune parole » refusal no longer exists
    assert back["transcript"] == "" and back["transcript_segments"] == [] and back["frames"]


def test_bad_save_as_id_is_refused(full):
    with pytest.raises(collect.VideoError, match="Identifiant"):
        collect.collect_video(GOOD, "../x")


# todo #476: limited-range yuv420p (yt-dlp format 18) must still give a JPEG, in our code and through the skill's relay.
SHIM = collect.SHIM_DIR / "ffmpeg"


def _limited_range_video(tmp_path):
    import shutil as sh
    import subprocess
    ffmpeg = sh.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg absent")
    video = tmp_path / "tv.mp4"
    subprocess.run([ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=2:s=320x240", "-pix_fmt", "yuv420p",
                    "-color_range", "tv", str(video)], check=True, timeout=60)
    return ffmpeg, video


def test_extract_frames_writes_a_jpeg_from_limited_range_video(tmp_path):
    import subprocess
    _limited_range_video(tmp_path)
    run = lambda argv, t: (lambda r: (r.returncode, r.stdout, r.stderr))(  # noqa: E731
        subprocess.run(argv, capture_output=True, text=True, timeout=t))
    done = media.extract_frames(tmp_path / "tv.mp4", [{"t": 0.5, "kind": "start"}], tmp_path / "img", run)
    assert [d["file"] for d in done] == ["001_t00000.jpg"]
    assert (tmp_path / "img" / done[0]["file"]).read_bytes()[:2] == b"\xff\xd8"


def test_relay_makes_a_plain_jpg_command_work(tmp_path):
    import os
    import subprocess
    ffmpeg, video = _limited_range_video(tmp_path)
    out = tmp_path / "f.jpg"
    env = {"PATH": f"{collect.SHIM_DIR}:{os.path.dirname(ffmpeg)}", "HOME": str(tmp_path)}
    res = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(video), "-frames:v", "1", str(out)],
                         env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and out.read_bytes()[:2] == b"\xff\xd8"


@pytest.fixture
def relay():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("ffmpeg_relay", str(SHIM))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("ffmpeg_relay", loader))
    loader.exec_module(mod)
    return mod


def test_relay_inserts_pix_fmt_before_a_jpeg_output(relay):
    assert relay.adjust(["-i", "a.mp4", "o.JPG"]) == ["-i", "a.mp4", "-pix_fmt", "yuvj420p", "o.JPG"]
    assert relay.adjust(["-i", "a", "o.jpeg"])[-3:] == ["-pix_fmt", "yuvj420p", "o.jpeg"]


def test_relay_leaves_other_commands_and_explicit_pix_fmt_alone(relay):
    for args in (["-i", "a.mp4", "o.png"], ["-i", "a.mp4", "o.mp4"], ["-version"], [],
                 ["-i", "a", "-pix_fmt", "yuv444p", "o.jpg"]):
        assert relay.adjust(args) == args


def test_relay_never_picks_itself_as_the_real_ffmpeg(relay, tmp_path):
    me = str(SHIM.resolve())
    assert relay.real_ffmpeg(str(collect.SHIM_DIR), me) is None
    other = tmp_path / "ffmpeg"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    assert relay.real_ffmpeg(f"{collect.SHIM_DIR}:{tmp_path}", me) == str(other)


def test_only_the_skill_gets_the_relay_in_its_path():
    assert str(collect.SHIM_DIR) in collect._env(shim=True)["PATH"].split(":")[0]
    assert str(collect.SHIM_DIR) not in collect._env()["PATH"]
    assert SHIM.is_file() and SHIM.stat().st_mode & 0o111
