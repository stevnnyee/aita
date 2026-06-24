from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Reddit
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "aita-pipeline/1.0"
    subreddit_name: str = "AmItheAsshole"
    min_upvotes: int = 500

    # OpenAI
    openai_api_key: str = ""

    # ElevenLabs
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    # YouTube
    youtube_client_secrets_file: Path = Path("credentials/client_secrets.json")
    youtube_token_file: Path = Path("credentials/youtube_token.json")
    youtube_privacy_status: str = "public"

    # Scheduler
    scheduler_timezone: str = "America/New_York"

    # Logging
    log_level: str = "INFO"

    # Paths
    database_url: str = "sqlite:///./aita.db"
    assets_bg_dir: Path = Path("assets/backgrounds")
    assets_music_dir: Path = Path("assets/music")
    output_dir: Path = Path("output")
    # Flat collection of finished videos, named {reddit_id}.mp4, for manual
    # posting (e.g. TikTok, which has no easy automated upload path).
    ready_dir: Path = Path("output/ready")
    log_dir: Path = Path("logs")

    # Secrets that the pipeline cannot run without.
    _REQUIRED_SECRETS: ClassVar[tuple[str, ...]] = (
        "reddit_client_id",
        "reddit_client_secret",
        "openai_api_key",
        "elevenlabs_api_key",
        "elevenlabs_voice_id",
    )

    def ensure_dirs(self) -> None:
        self.assets_bg_dir.mkdir(parents=True, exist_ok=True)
        self.assets_music_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ready_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.youtube_client_secrets_file.parent.mkdir(parents=True, exist_ok=True)
        self.youtube_token_file.parent.mkdir(parents=True, exist_ok=True)

    def validate_required(self) -> None:
        """Fail fast if any credential the pipeline depends on is missing.

        Call this explicitly at startup; ``get_settings()`` stays side-effect
        free so it can be imported safely (e.g. by logging) without config
        being fully populated.
        """
        missing = [name for name in self._REQUIRED_SECRETS if not getattr(self, name)]
        if missing:
            raise ValueError(
                "Missing required configuration: "
                + ", ".join(sorted(missing))
                + ". Set these via environment variables or the .env file."
            )


@lru_cache
def get_settings() -> Settings:
    # No side effects here (no dir creation / validation): this is imported at
    # module load time in several places, so it must not fail on a read-only
    # CWD or partial config. Call ``ensure_dirs()`` / ``validate_required()``
    # explicitly during startup instead.
    return Settings()
