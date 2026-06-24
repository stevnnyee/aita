from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import Settings, get_settings
from app.services.script_generator import SAMPLE_POST, Script, generate as generate_script
from app.services.voiceover import (
    Alignment,
    VoiceoverResult,
    generate as generate_voiceover,
    load_alignment,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

PLAY_RES_X = 1080
PLAY_RES_Y = 1920
FONT_NAME = "Arial"
FONT_SIZE = 72
OUTLINE = 4
SHADOW = 0
MARGIN_V = 120
MIN_WORDS_PER_LINE = 2
MAX_WORDS_PER_LINE = 4

ASS_HEADER = f"""[Script Info]
Title: AITA Captions
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},{FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{OUTLINE},{SHADOW},2,40,40,{MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass(frozen=True)
class TimedPhrase:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class CaptionResult:
    ass_path: Path
    line_count: int


def _format_ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - math.floor(seconds)) * 100))
    if centis >= 100:
        centis = 0
        secs += 1
        if secs >= 60:
            secs = 0
            minutes += 1
            if minutes >= 60:
                minutes = 0
                hours += 1
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _words_from_alignment(alignment: Alignment) -> list[TimedPhrase]:
    words: list[TimedPhrase] = []
    buffer: list[str] = []
    word_start: Optional[float] = None
    word_end: Optional[float] = None

    for char, start, end in zip(
        alignment.characters,
        alignment.character_start_times_seconds,
        alignment.character_end_times_seconds,
    ):
        if char.isspace():
            if buffer and word_start is not None and word_end is not None:
                words.append(TimedPhrase("".join(buffer), word_start, word_end))
                buffer = []
                word_start = None
                word_end = None
            continue

        if word_start is None:
            word_start = start
        word_end = end
        buffer.append(char)

    if buffer and word_start is not None and word_end is not None:
        words.append(TimedPhrase("".join(buffer), word_start, word_end))

    return words


def _even_word_timing(text: str, duration_seconds: float) -> list[TimedPhrase]:
    words = re.findall(r"\S+", text)
    if not words or duration_seconds <= 0:
        return []

    slice_duration = duration_seconds / len(words)
    return [
        TimedPhrase(
            word,
            index * slice_duration,
            (index + 1) * slice_duration,
        )
        for index, word in enumerate(words)
    ]


def _chunk_words(words: list[TimedPhrase]) -> list[TimedPhrase]:
    if not words:
        return []

    lines: list[TimedPhrase] = []
    index = 0
    total = len(words)

    while index < total:
        remaining = total - index
        if remaining <= MAX_WORDS_PER_LINE:
            chunk_size = remaining
        elif remaining == MAX_WORDS_PER_LINE + 1:
            chunk_size = MIN_WORDS_PER_LINE
        else:
            chunk_size = 3

        chunk = words[index : index + chunk_size]
        lines.append(
            TimedPhrase(
                text=" ".join(word.text for word in chunk),
                start=chunk[0].start,
                end=chunk[-1].end,
            )
        )
        index += chunk_size

    return lines


def _timed_phrases(
    text: str,
    alignment: Optional[Alignment],
    duration_seconds: float,
) -> list[TimedPhrase]:
    if alignment is not None and alignment.characters:
        words = _words_from_alignment(alignment)
        if words:
            return _chunk_words(words)

    logger.warning(
        "No alignment data; distributing captions evenly across audio duration"
    )
    return _chunk_words(_even_word_timing(text, duration_seconds))


def _build_ass_content(phrases: list[TimedPhrase]) -> str:
    lines = [ASS_HEADER]
    for phrase in phrases:
        start = _format_ass_time(phrase.start)
        end = _format_ass_time(phrase.end)
        text = _escape_ass_text(phrase.text.upper())
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")
    return "".join(lines)


def _script_from_alignment(alignment: Optional[Alignment], duration: float) -> Script:
    text = "".join(alignment.characters) if alignment else ""
    if not text.strip():
        text = "Follow for more AITA verdicts."
    return Script(
        full_text=text,
        hook="",
        story="",
        verdict="",
        cta="",
        word_count=len(re.findall(r"\S+", text)),
    )


def generate(
    script: Script,
    voiceover: VoiceoverResult,
    reddit_id: str,
    *,
    settings: Optional[Settings] = None,
) -> CaptionResult:
    """Generate TikTok-style ASS captions synced to the voiceover."""
    settings = settings or get_settings()
    out_dir = settings.output_dir / reddit_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ass_path = out_dir / "captions.ass"

    alignment = voiceover.alignment
    if alignment is None and voiceover.alignment_path.is_file():
        alignment = load_alignment(voiceover.alignment_path)

    phrases = _timed_phrases(
        script.full_text,
        alignment,
        voiceover.duration_seconds,
    )
    ass_path.write_text(_build_ass_content(phrases), encoding="utf-8")

    logger.info(
        "Generated captions for post %s: %d lines -> %s",
        reddit_id,
        len(phrases),
        ass_path,
    )

    return CaptionResult(ass_path=ass_path, line_count=len(phrases))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate ASS captions from a script and voiceover.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Run sample script + voiceover pipeline, then build captions",
    )
    parser.add_argument(
        "--reddit-id",
        default="sample",
        help="Output subdirectory (default: sample)",
    )
    parser.add_argument(
        "--from-output",
        action="store_true",
        help="Use existing output/{reddit-id}/voiceover + alignment.json",
    )
    args = parser.parse_args()

    settings = get_settings()

    if args.from_output:
        out_dir = settings.output_dir / args.reddit_id
        alignment_path = out_dir / "alignment.json"
        audio_path = out_dir / "voiceover.mp3"
        if not audio_path.is_file():
            raise SystemExit(f"Missing voiceover: {audio_path}")

        alignment = load_alignment(alignment_path)
        duration = alignment.duration_seconds if alignment else 0.0
        if duration <= 0:
            from app.services.voiceover import _probe_duration_seconds

            duration = _probe_duration_seconds(audio_path)

        script = _script_from_alignment(alignment, duration)
        voiceover = VoiceoverResult(
            audio_path=audio_path,
            alignment_path=alignment_path,
            duration_seconds=duration,
            alignment=alignment,
        )
    elif args.sample:
        if not settings.openai_api_key:
            raise SystemExit("Set OPENAI_API_KEY in .env to use --sample.")
        if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
            raise SystemExit("Set ElevenLabs keys in .env to use --sample.")

        script = generate_script(SAMPLE_POST, settings=settings)
        voiceover = generate_voiceover(
            script.full_text, args.reddit_id, settings=settings
        )
    else:
        raise SystemExit("Use --sample or --from-output.")

    result = generate(script, voiceover, args.reddit_id, settings=settings)
    print(f"Captions: {result.ass_path}")
    print(f"Lines:    {result.line_count}")


if __name__ == "__main__":
    main()
