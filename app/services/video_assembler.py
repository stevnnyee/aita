from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import Settings, get_settings
from app.services.voiceover import VoiceoverResult, load_alignment
from app.utils.ffmpeg import FFmpegError, escape_subtitles_path, probe_duration, run
from app.utils.logging import get_logger

logger = get_logger(__name__)

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
MIN_VIDEO_DURATION = 65.0
MAX_VIDEO_DURATION = 80.0
VOICEOVER_PADDING_SECONDS = 5.0
MUSIC_VOLUME = 0.25

BACKGROUND_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
MUSIC_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg"}


@dataclass(frozen=True)
class AssembleResult:
    video_path: Path
    duration_seconds: float
    background_path: Path
    music_path: Optional[Path]  # None when the video has no background music


def target_duration(voiceover_duration: float) -> float:
    return max(
        MIN_VIDEO_DURATION,
        min(MAX_VIDEO_DURATION, voiceover_duration + VOICEOVER_PADDING_SECONDS),
    )


def _list_assets(directory: Path, extensions: set[str]) -> list[Path]:
    if not directory.is_dir():
        return []
    return [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in extensions
    ]


def _pick_random_asset(directory: Path, extensions: set[str], label: str) -> Path:
    if not directory.is_dir():
        raise FileNotFoundError(
            f"{label} directory not found: {directory}. "
            f"Create it and add media files."
        )

    candidates = _list_assets(directory, extensions)
    if not candidates:
        raise FileNotFoundError(
            f"No {label} files found in {directory}. "
            f"Supported extensions: {', '.join(sorted(extensions))}"
        )

    return random.choice(candidates)


def _maybe_pick_music(settings: Settings) -> Optional[Path]:
    """Pick a music track with probability settings.music_chance, else None.

    Returns None when music is disabled (chance <= 0), when the dice say "no
    music" for this video, or when there are simply no music files available —
    so an empty music/ folder is fine and just means no background music.
    """
    if settings.music_chance <= 0 or random.random() >= settings.music_chance:
        return None
    candidates = _list_assets(settings.assets_music_dir, MUSIC_EXTENSIONS)
    return random.choice(candidates) if candidates else None


def _voiceover_from_output(reddit_id: str, settings: Settings) -> VoiceoverResult:
    out_dir = settings.output_dir / reddit_id
    audio_path = out_dir / "voiceover.mp3"
    alignment_path = out_dir / "alignment.json"

    if not audio_path.is_file():
        raise FileNotFoundError(f"Missing voiceover: {audio_path}")

    alignment = load_alignment(alignment_path) if alignment_path.is_file() else None
    duration = (
        alignment.duration_seconds
        if alignment and alignment.character_end_times_seconds
        else probe_duration(audio_path)
    )

    return VoiceoverResult(
        audio_path=audio_path,
        alignment_path=alignment_path,
        duration_seconds=duration,
        alignment=alignment,
    )


def assemble(
    reddit_id: str,
    voiceover: VoiceoverResult,
    *,
    settings: Optional[Settings] = None,
    background: Optional[Path] = None,
    music: Optional[Path] = None,
) -> AssembleResult:
    """Assemble final vertical video with background, voiceover, music, and captions."""
    settings = settings or get_settings()
    out_dir = settings.output_dir / reddit_id
    captions_path = out_dir / "captions.ass"
    output_path = out_dir / "final.mp4"

    if not voiceover.audio_path.is_file():
        raise FileNotFoundError(f"Missing voiceover: {voiceover.audio_path}")
    if not captions_path.is_file():
        raise FileNotFoundError(
            f"Missing captions: {captions_path}. Run the captions step first."
        )

    background_path = background or _pick_random_asset(
        settings.assets_bg_dir, BACKGROUND_EXTENSIONS, "background"
    )
    # Explicit music wins; otherwise maybe pick one (some videos get none).
    music_path = music if music is not None else _maybe_pick_music(settings)

    duration = target_duration(voiceover.duration_seconds)
    subtitle_path = escape_subtitles_path(captions_path)

    video_filter = (
        f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,"
        f"subtitles='{subtitle_path}'[vout]"
    )

    command = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(background_path),
               "-i", str(voiceover.audio_path)]

    if music_path is not None:
        command += ["-stream_loop", "-1", "-i", str(music_path)]
        audio_filter = (
            f"[1:a]asetpts=PTS-STARTPTS[voice];"
            f"[2:a]volume={MUSIC_VOLUME},asetpts=PTS-STARTPTS[music];"
            f"[voice][music]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
        )
    else:
        # No music: the voiceover is the only audio track.
        audio_filter = "[1:a]asetpts=PTS-STARTPTS[aout]"

    command += [
        "-t",
        f"{duration:.3f}",
        "-filter_complex",
        f"{video_filter};{audio_filter}",
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    logger.info(
        "Assembling video for post %s: duration=%.1fs bg=%s music=%s",
        reddit_id,
        duration,
        background_path.name,
        music_path.name if music_path else "(none)",
    )

    try:
        run(command, description="ffmpeg assemble")
    except FFmpegError:
        raise
    except Exception:
        logger.exception("Video assembly failed for post %s", reddit_id)
        raise

    logger.info("Assembled video for post %s -> %s", reddit_id, output_path)

    return AssembleResult(
        video_path=output_path,
        duration_seconds=duration,
        background_path=background_path,
        music_path=music_path,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble final video from voiceover, captions, and assets.",
    )
    parser.add_argument(
        "--reddit-id",
        default="sample",
        help="Output subdirectory with voiceover + captions (default: sample)",
    )
    parser.add_argument(
        "--background",
        type=Path,
        default=None,
        help="Optional background video path (default: random from assets/)",
    )
    parser.add_argument(
        "--music",
        type=Path,
        default=None,
        help="Optional music path (default: random from assets/)",
    )
    args = parser.parse_args()

    settings = get_settings()
    voiceover = _voiceover_from_output(args.reddit_id, settings)

    result = assemble(
        args.reddit_id,
        voiceover,
        settings=settings,
        background=args.background,
        music=args.music,
    )

    print(f"Video:      {result.video_path}")
    print(f"Duration:   {result.duration_seconds:.1f}s")
    print(f"Background: {result.background_path}")
    print(f"Music:      {result.music_path or '(none)'}")


if __name__ == "__main__":
    main()
