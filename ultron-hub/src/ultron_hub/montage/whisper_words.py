"""Word-level transcription with the Whisper of the hermes-video skill (run as a script, never imported by
ultron-hub).

    /usr/bin/python3 whisper_words.py --skill-src <hermes-video>/src <audio.wav>

The skill's own `watch.config.load_api_key` picks the backend (local faster-whisper, Groq or OpenAI) and reads its
key itself: the key never leaves this process and is never printed. Prints ONE JSON object on stdout:
    {"backend": str, "language": str, "words": [{"w": str, "s": float, "e": float}, ...]}
or {"error": str} with exit code 1.
"""

import argparse
import json
import sys
from pathlib import Path

GROQ = ("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo")
OPENAI = ("https://api.openai.com/v1/audio/transcriptions", "whisper-1")


def _local(model_name, audio):
    from faster_whisper import WhisperModel  # the skill's optional dependency

    model = WhisperModel(model_name or "base", device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(audio), beam_size=5, word_timestamps=True)
    words = [{"w": w.word, "s": w.start, "e": w.end} for seg in segments for w in (seg.words or [])]
    return words, getattr(info, "language", "") or ""


def _api(endpoint, model, key, audio):
    import requests

    data = {"model": model, "response_format": "verbose_json", "temperature": "0",
            "timestamp_granularities[]": ["word", "segment"]}
    with open(audio, "rb") as fh:
        resp = requests.post(endpoint, headers={"Authorization": f"Bearer {key}"}, data=data,
                             files={"file": (Path(audio).name, fh, "audio/wav")}, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"Whisper API answered HTTP {resp.status_code}")
    body = resp.json()
    words = [{"w": w.get("word", ""), "s": w.get("start"), "e": w.get("end")} for w in body.get("words") or []]
    return words, str(body.get("language") or "")


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-src", required=True)
    parser.add_argument("audio")
    args = parser.parse_args(argv)
    sys.path.insert(0, args.skill_src)
    try:
        from watch.config import load_api_key

        backend, key = load_api_key()
        if backend == "local":
            words, language = _local(key, args.audio)
        elif backend in ("groq", "openai"):
            endpoint, model = GROQ if backend == "groq" else OPENAI
            words, language = _api(endpoint, model, key, args.audio)
        else:
            raise RuntimeError("no Whisper backend configured in the hermes-video skill")
    except Exception as exc:  # never the key: only the kind of failure
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        sys.stdout.write(json.dumps({"error": message}) + "\n")
        return 1
    sys.stdout.write(json.dumps({"backend": backend, "language": language, "words": words}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
