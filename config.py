"""Configuración centralizada. Lee variables de entorno (.env)."""
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# override=True: el .env gana sobre variables ya definidas en el entorno del
# hosting (p. ej. panel Pterodactyl), que suele inyectar valores obsoletos.
load_dotenv(override=True)

logger = logging.getLogger(__name__)


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
    recompress_enabled: bool = os.getenv("RECOMPRESS_ENABLED", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    # 0 = sin tope de tamaño de origen para recomprimir.
    recompress_max_source_mb: int = _int("RECOMPRESS_MAX_SOURCE_MB", 30)
    recompress_timeout_s: int = _int("RECOMPRESS_TIMEOUT_S", 180)
    instagram_long_reel_seconds: int = _int("INSTAGRAM_LONG_REEL_SECONDS", 60)
    video_cooldown_seconds: int = _int("VIDEO_COOLDOWN_SECONDS", 30)
    # None = no definido en .env (se usa el límite del servidor).
    discord_upload_limit_mb: int | None = (
        _int("DISCORD_UPLOAD_LIMIT_MB", 0) or None
    )
    # Valor efectivo si está definido, si no el default 20.
    upload_limit_mb: int = discord_upload_limit_mb or 20
    max_concurrent_downloads: int = _int("MAX_CONCURRENT_DOWNLOADS", 2)
    max_uploads_per_user_day: int = _int("MAX_UPLOADS_PER_USER_DAY", 10)
    temp_dir: Path = Path(os.getenv("TEMP_DIR", "temp"))

    # Encoder de recompresión: auto | libx264 | h264_qsv
    encoder: str = os.getenv("ENCODER", "auto").strip().lower() or "auto"

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
            "ALLOWED_VIDEO_DOMAINS",
            "youtube.com,youtu.be,instagram.com,x.com,twitter.com,tiktok.com",
        ).split(",")
        if x.strip()
    )

    # Seguridad / límites
    allowed_guild_ids: tuple[int, ...] = tuple(
        int(x) for x in os.getenv("ALLOWED_GUILD_IDS", "").replace(" ", "").split(",") if x.isdigit()
    )
    serper_daily_limit: int = _int("SERPER_DAILY_LIMIT", 50)

    # Administración / monitoreo
    admin_user_ids: tuple[int, ...] = tuple(
        int(x)
        for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",")
        if x.isdigit()
    )
    error_webhook_url: str = os.getenv("ERROR_WEBHOOK_URL", "")

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
    delete_original_message: bool = os.getenv(
        "DELETE_ORIGINAL_MESSAGE", "true"
    ).lower() in ("1", "true", "yes", "on")

    # Mensajes personalizados por usuario
    custom_msg_cooldown_seconds: int = _int("CUSTOM_MSG_COOLDOWN_SECONDS", 30)
    custom_msg_delete_after: int = _int("CUSTOM_MSG_DELETE_AFTER", 0)


settings = Settings()

logger.info(
    "Config cargada: admin_user_ids=%s, allowed_guild_ids=%s, upload_limit_mb=%d, "
    "recompress_max_minutes=%d, encoder=%s, recompress_enabled=%s, "
    "recompress_max_source_mb=%d, recompress_timeout_s=%d",
    settings.admin_user_ids,
    settings.allowed_guild_ids,
    settings.upload_limit_mb,
    settings.recompress_max_minutes,
    settings.encoder,
    settings.recompress_enabled,
    settings.recompress_max_source_mb,
    settings.recompress_timeout_s,
)
logger.info(
    "DISCORD_UPLOAD_LIMIT_MB en entorno=%r (efectivo=%d MB)",
    os.getenv("DISCORD_UPLOAD_LIMIT_MB"),
    settings.upload_limit_mb,
)
if settings.upload_limit_mb < 20:
    logger.warning(
        "upload_limit_mb=%d (< 20). Revisa que DISCORD_UPLOAD_LIMIT_MB=20 esté "
        "en el .env del directorio de trabajo del bot y reinicia el proceso.",
        settings.upload_limit_mb,
    )
if not settings.recompress_enabled:
    logger.warning(
        "RECOMPRESS_ENABLED=false: recompresión desactivada; los videos que no "
        "quepan como adjunto se subirán a un host externo."
    )
elif settings.recompress_max_minutes <= 0:
    logger.warning(
        "RECOMPRESS_MAX_MINUTES=0: recompresión desactivada; los videos que no "
        "quepan como adjunto se subirán a un host externo."
    )
if settings.encoder not in ("auto", "libx264", "h264_qsv"):
    logger.warning(
        "ENCODER=%r no reconocido; se usará 'auto'. Valores válidos: auto, libx264, h264_qsv.",
        settings.encoder,
    )
