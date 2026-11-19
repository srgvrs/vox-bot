"""Descarga de video con yt-dlp (+ ffmpeg)."""
import asyncio
import ipaddress
import logging
import shutil
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from config import settings

logger = logging.getLogger(__name__)

_semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)


class VideoError(Exception):
    """Error de descarga de video con mensaje apto para el usuario."""


class VideoTooLongError(VideoError):
    """El video supera la duración máxima permitida."""


class VideoTooLargeError(VideoError):
    """El archivo descargado supera el tamaño máximo permitido."""


class VideoUnavailableError(VideoError):
    """El video es privado, no está disponible o la URL no es válida."""


def _is_safe_url(url: str) -> bool:
    """Solo permite http/https y rechaza hosts internos (localhost, IPs privadas)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in ("localhost", "127.0.0.1", "::1"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def _build_format() -> str:
    height = settings.max_video_height
    return (
        f"bv*[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]"
        f"/b[height<={height}]/b"
    )


def _download_sync(url: str, out_path: Path) -> Path:
    """Descarga bloqueante (se ejecuta en un hilo)."""
    ydl_opts = {
        "format": _build_format(),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": str(out_path.with_suffix(".%(ext)s")),
        "quiet": True,
        "no_warnings": True,
        "logger": _YdlLogger(),
    }

    # 1) Comprobar duración antes de descargar.
    with yt_dlp.YoutubeDL({**ydl_opts, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    duration = info.get("duration") if isinstance(info, dict) else None
    if duration and duration > settings.max_video_minutes * 60:
        raise VideoTooLongError(
            f"El video dura más de {settings.max_video_minutes} minutos."
        )

    # 2) Descargar.
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # 3) Localizar el archivo final (la extensión puede variar tras el merge).
    candidates = list(out_path.parent.glob(f"{out_path.stem}.*"))
    if not candidates:
        raise VideoUnavailableError("No se pudo descargar el video.")
    return candidates[0]


class _YdlLogger:
    """Redirige los logs de yt-dlp a logging, sin filtrar secretos."""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            logger.debug(msg)

    def info(self, msg: str) -> None:
        logger.debug(msg)

    def warning(self, msg: str) -> None:
        logger.warning(msg)

    def error(self, msg: str) -> None:
        logger.error(msg)


async def download_video(url: str, max_bytes: int) -> Path:
    """Descarga un video (máx. 720p) y devuelve la ruta del archivo temporal."""
    if not _is_safe_url(url):
        raise VideoUnavailableError("La URL no es válida o apunta a un host no permitido.")

    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings.temp_dir / uuid.uuid4().hex

    async with _semaphore:
        try:
            path = await asyncio.to_thread(_download_sync, url, out_path)
        except VideoError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc).lower()
            if "private" in msg or "unavailable" in msg or "not available" in msg:
                raise VideoUnavailableError(
                    "El video es privado o no está disponible."
                ) from exc
            raise VideoUnavailableError(
                "No se pudo descargar el video."
            ) from exc

    if path.stat().st_size > max_bytes:
        path.unlink(missing_ok=True)
        raise VideoTooLargeError(
            "El archivo es demasiado grande para enviarlo como adjunto."
        )
    return path


def ffmpeg_available() -> bool:
    """Indica si el binario ffmpeg está disponible en el PATH."""
    return shutil.which("ffmpeg") is not None
