from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import voiceover as vo


def make_alignment(chars="abc", starts=(0.0, 0.5, 1.0), ends=(0.5, 1.0, 1.5)):
    return vo.Alignment(
        characters=list(chars),
        character_start_times_seconds=list(starts),
        character_end_times_seconds=list(ends),
    )


# --- Alignment (de)serialization --------------------------------------------

def test_alignment_round_trips_through_dict():
    a = make_alignment()
    assert vo.Alignment.from_dict(a.to_dict()) == a


def test_alignment_duration_is_max_end_time():
    assert make_alignment(ends=(0.5, 2.0, 1.5)).duration_seconds == 2.0


def test_alignment_duration_empty_is_zero():
    assert make_alignment(chars="", starts=(), ends=()).duration_seconds == 0.0


# --- save / load alignment ---------------------------------------------------

def test_save_and_load_alignment_round_trip(tmp_path):
    path = tmp_path / "alignment.json"
    a = make_alignment()
    vo._save_alignment(path, a)
    assert vo.load_alignment(path) == a


def test_save_none_alignment_writes_null(tmp_path):
    path = tmp_path / "alignment.json"
    vo._save_alignment(path, None)
    assert json.loads(path.read_text()) is None
    assert vo.load_alignment(path) is None


def test_load_alignment_missing_file_returns_none(tmp_path):
    assert vo.load_alignment(tmp_path / "nope.json") is None


# --- _alignment_from_response -----------------------------------------------

def test_alignment_from_response_none():
    assert vo._alignment_from_response(None) is None


def test_alignment_from_response_maps_fields():
    raw = SimpleNamespace(
        characters=["h", "i"],
        character_start_times_seconds=[0.0, 0.2],
        character_end_times_seconds=[0.2, 0.4],
    )
    a = vo._alignment_from_response(raw)
    assert a == make_alignment("hi", (0.0, 0.2), (0.2, 0.4))


# --- _resolve_duration -------------------------------------------------------

def test_resolve_duration_prefers_alignment(monkeypatch):
    monkeypatch.setattr(vo, "_probe_duration_seconds", lambda p: pytest.fail("probed"))
    assert vo._resolve_duration("x.mp3", make_alignment(ends=(0.5, 1.0, 3.0))) == 3.0


def test_resolve_duration_falls_back_to_ffprobe(monkeypatch):
    monkeypatch.setattr(vo, "_probe_duration_seconds", lambda p: 65.0)
    assert vo._resolve_duration("x.mp3", None) == 65.0
    # also when alignment present but empty
    empty = make_alignment(chars="", starts=(), ends=())
    assert vo._resolve_duration("x.mp3", empty) == 65.0


# --- generate ----------------------------------------------------------------

AUDIO_B64 = base64.b64encode(b"ID3fake-mp3-bytes").decode("ascii")


def fake_response(normalized=None, alignment=None, audio=AUDIO_B64):
    tts = SimpleNamespace(
        convert_with_timestamps=lambda **kw: SimpleNamespace(
            audio_base_64=audio,
            normalized_alignment=normalized,
            alignment=alignment,
        )
    )
    return SimpleNamespace(text_to_speech=tts)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        elevenlabs_api_key="k",
        elevenlabs_voice_id="v",
        output_dir=tmp_path / "output",
    )


def raw_alignment(ends):
    n = len(ends)
    return SimpleNamespace(
        characters=["x"] * n,
        character_start_times_seconds=[0.0] * n,
        character_end_times_seconds=list(ends),
    )


def test_generate_requires_api_key(tmp_path):
    with pytest.raises(ValueError, match="elevenlabs_api_key"):
        vo.generate("hi", "r1", settings=Settings(elevenlabs_api_key=""))


def test_generate_requires_voice_id(tmp_path):
    s = Settings(elevenlabs_api_key="k", elevenlabs_voice_id="")
    with pytest.raises(ValueError, match="elevenlabs_voice_id"):
        vo.generate("hi", "r1", settings=s)


def test_generate_writes_files_and_uses_alignment(settings, monkeypatch):
    monkeypatch.setattr(
        vo, "_elevenlabs_client",
        lambda s: fake_response(normalized=raw_alignment([10.0, 65.0])),
    )
    result = vo.generate("hello world", "post42", settings=settings)

    assert result.audio_path.read_bytes() == b"ID3fake-mp3-bytes"
    assert result.audio_path.name == "voiceover.mp3"
    assert result.duration_seconds == 65.0
    assert result.alignment is not None
    # alignment.json round-trips back to the same object
    assert vo.load_alignment(result.alignment_path) == result.alignment


def test_generate_prefers_normalized_over_alignment(settings, monkeypatch):
    monkeypatch.setattr(
        vo, "_elevenlabs_client",
        lambda s: fake_response(
            normalized=raw_alignment([62.0]), alignment=raw_alignment([99.0])
        ),
    )
    result = vo.generate("hi", "p", settings=settings)
    assert result.duration_seconds == 62.0


def test_generate_falls_back_to_plain_alignment(settings, monkeypatch):
    monkeypatch.setattr(
        vo, "_elevenlabs_client",
        lambda s: fake_response(normalized=None, alignment=raw_alignment([63.0])),
    )
    result = vo.generate("hi", "p", settings=settings)
    assert result.duration_seconds == 63.0


def test_generate_uses_ffprobe_when_no_alignment(settings, monkeypatch):
    monkeypatch.setattr(
        vo, "_elevenlabs_client",
        lambda s: fake_response(normalized=None, alignment=None),
    )
    monkeypatch.setattr(vo, "_probe_duration_seconds", lambda p: 64.0)
    result = vo.generate("hi", "p", settings=settings)
    assert result.alignment is None
    assert result.duration_seconds == 64.0
    assert vo.load_alignment(result.alignment_path) is None
