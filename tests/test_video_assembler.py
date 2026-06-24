from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.services import video_assembler as va
from app.services.voiceover import VoiceoverResult


# --- target_duration ---------------------------------------------------------

@pytest.mark.parametrize(
    "vo,expected",
    [
        (5.0, 65.0),     # below min -> clamp up
        (60.0, 65.0),    # 65 exactly
        (62.0, 67.0),    # inside range (vo + 5)
        (74.0, 79.0),
        (80.0, 80.0),    # 85 -> clamp down
        (200.0, 80.0),
    ],
)
def test_target_duration(vo, expected):
    assert va.target_duration(vo) == expected


# --- _pick_random_asset ------------------------------------------------------

def test_pick_random_asset_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError, match="directory not found"):
        va._pick_random_asset(tmp_path / "nope", va.MUSIC_EXTENSIONS, "music")


def test_pick_random_asset_no_matching_files(tmp_path):
    (tmp_path / "readme.txt").write_text("x")
    with pytest.raises(FileNotFoundError, match="No music files"):
        va._pick_random_asset(tmp_path, va.MUSIC_EXTENSIONS, "music")


def test_pick_random_asset_filters_by_extension(tmp_path, monkeypatch):
    (tmp_path / "a.mp3").write_text("x")
    (tmp_path / "b.MP3").write_text("x")   # case-insensitive
    (tmp_path / "c.txt").write_text("x")   # ignored
    (tmp_path / "sub").mkdir()             # dir ignored
    monkeypatch.setattr(va.random, "choice", lambda seq: sorted(seq)[0])

    picked = va._pick_random_asset(tmp_path, va.MUSIC_EXTENSIONS, "music")
    assert picked.suffix.lower() == ".mp3"
    assert picked.name in {"a.mp3", "b.MP3"}


# --- assemble ----------------------------------------------------------------

def _setup(tmp_path):
    settings = Settings(output_dir=tmp_path / "output")
    out = settings.output_dir / "r1"
    out.mkdir(parents=True)
    (out / "voiceover.mp3").write_bytes(b"audio")
    (out / "captions.ass").write_text("[Script Info]\n")
    bg = tmp_path / "bg.mp4"
    bg.write_bytes(b"vid")
    music = tmp_path / "track.mp3"
    music.write_bytes(b"mus")
    vo = VoiceoverResult(
        audio_path=out / "voiceover.mp3",
        alignment_path=out / "alignment.json",
        duration_seconds=60.0,
        alignment=None,
    )
    return settings, vo, bg, music, out


def test_assemble_missing_voiceover(tmp_path):
    settings, vo, bg, music, out = _setup(tmp_path)
    (out / "voiceover.mp3").unlink()
    with pytest.raises(FileNotFoundError, match="Missing voiceover"):
        va.assemble("r1", vo, settings=settings, background=bg, music=music)


def test_assemble_missing_captions(tmp_path):
    settings, vo, bg, music, out = _setup(tmp_path)
    (out / "captions.ass").unlink()
    with pytest.raises(FileNotFoundError, match="Missing captions"):
        va.assemble("r1", vo, settings=settings, background=bg, music=music)


def test_assemble_builds_command_and_result(tmp_path, monkeypatch):
    settings, vo, bg, music, out = _setup(tmp_path)
    captured = {}

    def fake_run(command, *, description=""):
        captured["command"] = command
        captured["description"] = description

    monkeypatch.setattr(va, "run", fake_run)

    result = va.assemble("r1", vo, settings=settings, background=bg, music=music)

    # result
    assert result.video_path == out / "final.mp4"
    assert result.duration_seconds == 65.0  # target_duration(60)
    assert result.background_path == bg
    assert result.music_path == music

    cmd = captured["command"]
    joined = " ".join(cmd)
    assert cmd[0] == "ffmpeg" and cmd[-1] == str(out / "final.mp4")
    assert "libx264" in cmd and "aac" in cmd
    assert "-t" in cmd and "65.000" in cmd
    # voice level preserved (normalize disabled), music attenuated
    assert "normalize=0" in joined
    assert f"volume={va.MUSIC_VOLUME}" in joined
    assert f"scale={va.VIDEO_WIDTH}:{va.VIDEO_HEIGHT}" in joined
    assert "subtitles=" in joined
    # both streams mapped
    assert cmd.count("-map") == 2


def test_assemble_picks_random_assets_when_not_given(tmp_path, monkeypatch):
    _settings, vo, _bg, _music, out = _setup(tmp_path)
    settings = Settings(
        output_dir=tmp_path / "output",
        assets_bg_dir=tmp_path / "assets_bg",
        assets_music_dir=tmp_path / "assets_music",
        music_chance=1.0,  # force music so the assertion is deterministic
    )
    settings.assets_bg_dir.mkdir(parents=True, exist_ok=True)
    settings.assets_music_dir.mkdir(parents=True, exist_ok=True)
    (settings.assets_bg_dir / "x.mp4").write_bytes(b"v")
    (settings.assets_music_dir / "y.mp3").write_bytes(b"m")
    monkeypatch.setattr(va, "run", lambda command, *, description="": None)

    result = va.assemble("r1", vo, settings=settings)
    assert result.background_path.name == "x.mp4"
    assert result.music_path.name == "y.mp3"


# --- _maybe_pick_music -------------------------------------------------------

def test_maybe_pick_music_disabled_returns_none(tmp_path):
    s = Settings(assets_music_dir=tmp_path, music_chance=0.0)
    (tmp_path / "a.mp3").write_bytes(b"m")
    assert va._maybe_pick_music(s) is None


def test_maybe_pick_music_always_picks_when_chance_one(tmp_path):
    s = Settings(assets_music_dir=tmp_path, music_chance=1.0)
    (tmp_path / "a.mp3").write_bytes(b"m")
    assert va._maybe_pick_music(s).name == "a.mp3"


def test_maybe_pick_music_none_when_no_files(tmp_path):
    s = Settings(assets_music_dir=tmp_path, music_chance=1.0)  # wants music, but none exist
    assert va._maybe_pick_music(s) is None


def test_maybe_pick_music_respects_probability(tmp_path, monkeypatch):
    s = Settings(assets_music_dir=tmp_path, music_chance=0.5)
    (tmp_path / "a.mp3").write_bytes(b"m")
    monkeypatch.setattr(va.random, "random", lambda: 0.9)   # >= 0.5 -> skip
    assert va._maybe_pick_music(s) is None
    monkeypatch.setattr(va.random, "random", lambda: 0.1)   # < 0.5 -> pick
    assert va._maybe_pick_music(s) is not None


# --- assemble without music --------------------------------------------------

def test_assemble_without_music_omits_mix(tmp_path, monkeypatch):
    settings, vo, bg, _music, out = _setup(tmp_path)
    settings = Settings(output_dir=tmp_path / "output", music_chance=0.0)
    captured = {}
    monkeypatch.setattr(
        va, "run",
        lambda command, *, description="": captured.update(command=command),
    )

    result = va.assemble("r1", vo, settings=settings, background=bg)  # music auto -> none

    assert result.music_path is None
    cmd = captured["command"]
    joined = " ".join(cmd)
    assert "amix" not in joined          # no mixing
    assert "volume=" not in joined        # no music volume filter
    assert "[1:a]asetpts=PTS-STARTPTS[aout]" in joined  # voice is the only audio
    assert cmd.count("-i") == 2           # only background + voiceover inputs
    assert cmd.count("-map") == 2         # video + audio still mapped
