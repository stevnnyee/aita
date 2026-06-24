from __future__ import annotations

import argparse
import base64
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from app.config import Settings, get_settings
from app.services.script_generator import SAMPLE_POST, Script, generate as generate_script
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from elevenlabs import ElevenLabs

logger = get_logger(__name__)

MODEL_ID = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"
MIN_DURATION_SECONDS = 60.0
MAX_DURATION_SECONDS = 70.0


@dataclass(frozen=True)
class Alignment:
    characters: list[str]
    character_start_times_seconds: list[float]
    character_end_times_seconds: list[float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Alignment:
        return cls(
            characters=list(data["characters"]),
            character_start_times_seconds=list(data["character_start_times_seconds"]),
            character_end_times_seconds=list(data["character_end_times_seconds"]),
        )

    @property
    def duration_seconds(self) -> float:
        if not self.character_end_times_seconds:
            return 0.0
        return max(self.character_end_times_seconds)


@dataclass(frozen=True)
class VoiceoverResult:
    audio_path: Path
    alignment_path: Path
    duration_seconds: float
    alignment: Optional[Alignment]


def _elevenlabs_client(settings: Settings) -> "ElevenLabs":
    from elevenlabs import ElevenLabs

    return ElevenLabs(api_key=settings.elevenlabs_api_key)


def _output_dir(settings: Settings, reddit_id: str) -> Path:
    path = settings.output_dir / reddit_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _alignment_from_response(response_alignment: Any) -> Optional[Alignment]:
    if response_alignment is None:
        return None
    return Alignment(
        characters=list(response_alignment.characters),
        character_start_times_seconds=list(response_alignment.character_start_times_seconds),
        character_end_times_seconds=list(response_alignment.character_end_times_seconds),
    )


def _probe_duration_seconds(audio_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def _resolve_duration(audio_path: Path, alignment: Optional[Alignment]) -> float:
    if alignment is not None and alignment.character_end_times_seconds:
        return alignment.duration_seconds
    return _probe_duration_seconds(audio_path)


def _save_alignment(path: Path, alignment: Optional[Alignment]) -> None:
    payload = None if alignment is None else alignment.to_dict()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_alignment(path: Path) -> Optional[Alignment]:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data is None:
        return None
    return Alignment.from_dict(data)


def generate(
    text: str,
    reddit_id: str,
    *,
    settings: Optional[Settings] = None,
) -> VoiceoverResult:
    """Generate voiceover MP3 with character-level alignment timestamps."""
    settings = settings or get_settings()
    if not settings.elevenlabs_api_key:
        raise ValueError(
            "Missing elevenlabs_api_key. Set ELEVENLABS_API_KEY via environment or .env."
        )
    if not settings.elevenlabs_voice_id:
        raise ValueError(
            "Missing elevenlabs_voice_id. Set ELEVENLABS_VOICE_ID via environment or .env."
        )

    client = _elevenlabs_client(settings)
    response = client.text_to_speech.convert_with_timestamps(
        voice_id=settings.elevenlabs_voice_id,
        text=text,
        model_id=MODEL_ID,
        output_format=OUTPUT_FORMAT,
    )

    out_dir = _output_dir(settings, reddit_id)
    audio_path = out_dir / "voiceover.mp3"
    alignment_path = out_dir / "alignment.json"

    audio_bytes = base64.b64decode(response.audio_base_64)
    audio_path.write_bytes(audio_bytes)

    alignment = _alignment_from_response(
        response.normalized_alignment or response.alignment
    )
    _save_alignment(alignment_path, alignment)

    duration_seconds = _resolve_duration(audio_path, alignment)

    if duration_seconds < MIN_DURATION_SECONDS or duration_seconds > MAX_DURATION_SECONDS:
        logger.warning(
            "Voiceover duration %.1fs outside target range [%.0f, %.0f] for post %s",
            duration_seconds,
            MIN_DURATION_SECONDS,
            MAX_DURATION_SECONDS,
            reddit_id,
        )

    logger.info(
        "Generated voiceover for post %s: %.1fs -> %s",
        reddit_id,
        duration_seconds,
        audio_path,
    )

    return VoiceoverResult(
        audio_path=audio_path,
        alignment_path=alignment_path,
        duration_seconds=duration_seconds,
        alignment=alignment,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate ElevenLabs voiceover from a script.",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Generate script from built-in sample post, then synthesize voiceover",
    )
    parser.add_argument(
        "--reddit-id",
        default="sample",
        help="Output subdirectory name (default: sample)",
    )
    parser.add_argument(
        "--text",
        default="",
        help="Speak this text directly instead of generating a script",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.elevenlabs_api_key or not settings.elevenlabs_voice_id:
        raise SystemExit("Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in .env.")

    if args.text:
        text = args.text
    elif args.sample:
        if not settings.openai_api_key:
            raise SystemExit("Set OPENAI_API_KEY in .env to use --sample.")
        script: Script = generate_script(SAMPLE_POST, settings=settings)
        text = script.full_text
        print(f"Script: {script.word_count} words\n")
    else:
        raise SystemExit("Provide --text or use --sample.")

    result = generate(text, args.reddit_id, settings=settings)
    print(f"Audio:     {result.audio_path}")
    print(f"Alignment: {result.alignment_path}")
    print(f"Duration:  {result.duration_seconds:.1f}s")
    if result.alignment is None:
        print("Warning:   no alignment data returned")


if __name__ == "__main__":
    main()
