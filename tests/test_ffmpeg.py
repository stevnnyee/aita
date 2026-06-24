from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.utils import ffmpeg


# --- require_ffmpeg ----------------------------------------------------------

def test_require_ffmpeg_ok(monkeypatch):
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: f"/usr/bin/{name}")
    ffmpeg.require_ffmpeg()  # no raise


def test_require_ffmpeg_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)
    with pytest.raises(ffmpeg.FFmpegError, match="ffmpeg not found"):
        ffmpeg.require_ffmpeg()


def test_require_ffmpeg_missing_ffprobe(monkeypatch):
    monkeypatch.setattr(
        ffmpeg.shutil, "which", lambda name: None if name == "ffprobe" else "/x"
    )
    with pytest.raises(ffmpeg.FFmpegError, match="ffprobe not found"):
        ffmpeg.require_ffmpeg()


# --- run ---------------------------------------------------------------------

def test_run_wraps_called_process_error(monkeypatch):
    monkeypatch.setattr(ffmpeg, "require_ffmpeg", lambda: None)

    def boom(*a, **k):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=["ffmpeg"], stderr="explosion"
        )

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ffmpeg.FFmpegError) as exc:
        ffmpeg.run(["ffmpeg", "-version"], description="test")
    assert exc.value.stderr == "explosion"
    assert exc.value.command == ["ffmpeg", "-version"]


def test_run_returns_completed_process(monkeypatch):
    monkeypatch.setattr(ffmpeg, "require_ffmpeg", lambda: None)
    sentinel = subprocess.CompletedProcess(["x"], 0, stdout="ok", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: sentinel)
    assert ffmpeg.run(["ffmpeg"]) is sentinel


# --- probe_duration ----------------------------------------------------------

def test_probe_duration_parses_float(monkeypatch):
    monkeypatch.setattr(
        ffmpeg, "run",
        lambda cmd, description="": subprocess.CompletedProcess(cmd, 0, "12.5\n", ""),
    )
    assert ffmpeg.probe_duration(Path("a.mp3")) == 12.5


# --- escape_subtitles_path ---------------------------------------------------

def test_escape_subtitles_path_plain_posix(tmp_path):
    p = tmp_path / "captions.ass"
    escaped = ffmpeg.escape_subtitles_path(p)
    # no colon/backslash/quote in a normal tmp path -> unchanged absolute posix
    assert escaped == p.resolve().as_posix()


def test_escape_subtitles_path_escapes_specials(monkeypatch, tmp_path):
    fake = Path("/tmp/a:b'c")

    class _P:
        def resolve(self):
            return self

        def as_posix(self):
            return "/tmp/a:b'c"

    assert ffmpeg.escape_subtitles_path(_P()) == r"/tmp/a\:b\'c"
