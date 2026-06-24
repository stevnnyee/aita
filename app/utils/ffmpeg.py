from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger(__name__)


class FFmpegError(RuntimeError):
    def __init__(self, message: str, *, command: list[str], stderr: str = "") -> None:
        super().__init__(message)
        self.command = command
        self.stderr = stderr


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise FFmpegError(
            "ffmpeg not found on PATH. Install it (e.g. brew install ffmpeg).",
            command=["ffmpeg"],
        )
    if shutil.which("ffprobe") is None:
        raise FFmpegError(
            "ffprobe not found on PATH. Install ffmpeg (includes ffprobe).",
            command=["ffprobe"],
        )


def run(command: list[str], *, description: str = "ffmpeg") -> subprocess.CompletedProcess[str]:
    require_ffmpeg()
    logger.debug("Running %s: %s", description, " ".join(command))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or exc.stdout or ""
        logger.error("%s failed:\n%s", description, stderr)
        raise FFmpegError(
            f"{description} failed with exit code {exc.returncode}",
            command=command,
            stderr=stderr,
        ) from exc
    return result


def probe_duration(path: Path) -> float:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        description="ffprobe",
    )
    return float(result.stdout.strip())


def escape_subtitles_path(path: Path) -> str:
    """Escape a path for ffmpeg's subtitles filter."""
    escaped = path.resolve().as_posix()
    escaped = escaped.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", r"\'")
    return escaped
