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
    host_video_height: int = _int("HOST_VIDEO_HEIGHT", 480)
    host_max_mb: int = _int("HOST_MAX_MB", 35)
    max_video_minutes: int = _int("MAX_VIDEO_MINUTES", 15)
    recompress_max_minutes: int = _int("RECOMPRESS_MAX_MINUTES", 3)
    instagram_long_reel_seconds: int = _int("INSTAGRAM_LONG_REEL_SECONDS", 60)
    video_cooldown_seconds: int = _int("VIDEO_COOLDOWN_SECONDS", 30)
    upload_limit_mb: int = _int("DISCORD_UPLOAD_LIMIT_MB", 10)
    max_concurrent_downloads: int = _int("MAX_CONCURRENT_DOWNLOADS", 2)
    max_uploads_per_user_day: int = _int("MAX_UPLOADS_PER_USER_DAY", 10)
    temp_dir: Path = Path(os.getenv("TEMP_DIR", "temp"))

    # Cloudflare R2 (API compatible con S3)
    r2_account_id: str = os.getenv("R2_ACCOUNT_ID", "")
    r2_access_key_id: str = os.getenv("R2_ACCESS_KEY_ID", "")
    r2_secret_access_key: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
    r2_bucket: str = os.getenv("R2_BUCKET", "")
    link_expiry_hours: int = _int("LINK_EXPIRY_HOURS", 24)

    # Hosts externos de subida (orden de preferencia)
    storage_providers: tuple[str, ...] = tuple(
        x.strip().lower()
        for x in os.getenv("STORAGE_PROVIDERS", "litterbox,storage_to,gofile").split(",")
        if x.strip()
    )
    litterbox_expiry: str = os.getenv("LITTERBOX_EXPIRY", "24h")
    storage_to_expiry_hours: int = _int("STORAGE_TO_EXPIRY_HOURS", 72)
    gofile_expiry_hours: int = _int("GOFILE_EXPIRY_HOURS", 24)
    storage_timeout_seconds: int = _int("STORAGE_TIMEOUT_SECONDS", 60)
    storage_max_retries: int = _int("STORAGE_MAX_RETRIES", 2)
    storage_circuit_seconds: int = _int("STORAGE_CIRCUIT_SECONDS", 300)

    # Dominios permitidos para /video (se aceptan subdominios)
    allowed_video_domains: tuple[str, ...] = tuple(
        x.strip().lower()
        for x in os.getenv(
            "ALLOWED_VIDEO_DOMAINS", "youtube.com,youtu.be,instagram.com"
        ).split(",")
        if x.strip()
    )

    # Seguridad / límites
    allowed_guild_ids: tuple[int, ...] = tuple(
        int(x) for x in os.getenv("ALLOWED_GUILD_IDS", "").replace(" ", "").split(",") if x.isdigit()
    )
    serper_daily_limit: int = _int("SERPER_DAILY_LIMIT", 50)

    # Estado animado de /video (formato <a:nombre:id> o vacío para spinner)
    status_emoji: str = os.getenv("STATUS_EMOJI", "")

    # Modo automático de enlaces (desactivado por defecto)
    auto_link_enabled: bool = os.getenv("AUTO_LINK_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    auto_link_channel_ids: tuple[int, ...] = tuple(
        int(x)
        for x in os.getenv("AUTO_LINK_CHANNEL_IDS", "").replace(" ", "").split(",")
        if x.isdigit()
    )


settings = Settings()
