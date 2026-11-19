"""Configuración centralizada. Lee variables de entorno (.env)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    serper_api_key: str = os.getenv("SERPER_API_KEY", "")

    # Video
    max_video_height: int = _int("MAX_VIDEO_HEIGHT", 720)
    max_video_minutes: int = _int("MAX_VIDEO_MINUTES", 15)
    upload_limit_mb: int = _int("DISCORD_UPLOAD_LIMIT_MB", 10)
    max_concurrent_downloads: int = _int("MAX_CONCURRENT_DOWNLOADS", 2)
    temp_dir: Path = Path(os.getenv("TEMP_DIR", "temp"))

    # Cloudflare R2 (API compatible con S3)
    r2_account_id: str = os.getenv("R2_ACCOUNT_ID", "")
    r2_access_key_id: str = os.getenv("R2_ACCESS_KEY_ID", "")
    r2_secret_access_key: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
    r2_bucket: str = os.getenv("R2_BUCKET", "")
    link_expiry_hours: int = _int("LINK_EXPIRY_HOURS", 24)


settings = Settings()
