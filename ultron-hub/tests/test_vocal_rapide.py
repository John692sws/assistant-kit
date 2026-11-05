"""Fast vocal transcription (todo #397): fake model, never a real Whisper."""

import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from ultron_hub import vocal_rapide
from ultron_hub.bot.voice import VoiceError


class FakeModel:
    def __init__(self, texts):
        self.texts, self.calls = texts, []

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))
        return iter(SimpleNamespace(text=t) for t in self.texts), None


@pytest.fixture
def wav_ok(monkeypatch):
    def fake_wav(src, dest):
        with wave.open(str(dest), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
            w.writeframes(b"\x00\x10" * 1600)
    monkeypatch.setattr(vocal_rapide, "_to_wav16k", fake_wav)
    from ultron_hub.montage import audio
    monkeypatch.setattr(audio, "probe", lambda p: {"duration": 5.0, "streams": [{"codec_type": "audio"}]})


def test_light_settings_and_text(wav_ok, tmp_path):
    model = FakeModel([" Bonjour ", "le monde"])
    assert vocal_rapide.transcrire(tmp_path / "a.m4a", lambda: model) == "Bonjour le monde"
    audio, kw = model.calls[0]
    assert kw == {"language": "fr", "beam_size": 1, "word_timestamps": False} and audio.dtype.name == "float32"


def test_no_speech(wav_ok, tmp_path):
    with pytest.raises(VoiceError, match="pas de parole"):
        vocal_rapide.transcrire(tmp_path / "a.m4a", lambda: FakeModel(["  "]))


def test_too_long(monkeypatch, tmp_path):
    from ultron_hub.montage import audio
    monkeypatch.setattr(audio, "probe", lambda p: {"duration": 21 * 60, "streams": [{"codec_type": "audio"}]})
    with pytest.raises(VoiceError, match="trop long"):
        vocal_rapide.transcrire(tmp_path / "a.m4a", lambda: FakeModel(["x"]))


def test_no_audio_stream(monkeypatch, tmp_path):
    from ultron_hub.montage import audio
    monkeypatch.setattr(audio, "probe", lambda p: {"duration": 3, "streams": [{"codec_type": "video"}]})
    with pytest.raises(VoiceError, match="pas de son"):
        vocal_rapide.transcrire(tmp_path / "a.m4a", lambda: FakeModel(["x"]))


def test_faster_whisper_missing(wav_ok, monkeypatch, tmp_path):
    import builtins
    real = builtins.__import__

    def deny(name, *a, **k):
        if name == "faster_whisper":
            raise ImportError
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", deny)
    with pytest.raises(VoiceError, match="faster-whisper absent"):
        vocal_rapide.transcrire(tmp_path / "a.m4a")
