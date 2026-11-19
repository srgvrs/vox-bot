"""Descarga de video con yt-dlp (+ ffmpeg)."""
import asyncio
import ipaddress
import logging
import shutil
import socket
import subprocess
import uuid
from dataclasses import dataclass
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



class VideoUnavailableError(VideoError):
    """El video es privado, no está disponible o la URL no es válida."""


class VideoRestrictedError(VideoError):
    """El video está restringido por edad o por región."""


class VideoUnsupportedError(VideoError):
    """La URL no es soportada por yt-dlp."""


class VideoNetworkError(VideoError):
    """Fallo de red al contactar con el origen del video."""


class RecompressError(VideoError):
    """Falló la recompresión con ffmpeg."""


@dataclass
class DownloadResult:
    """Resultado de una descarga: ruta, metadatos y si fue recomprimido."""

    path: Path
    title: str
    recompressed: bool
    duration: float | None
    size: int


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


def _download_sync(url: str, out_path: Path) -> tuple[Path, str, float | None]:
    """Descarga bloqueante (se ejecuta en un hilo). Devuelve (ruta, título, duración)."""
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
    if not isinstance(info, dict):
        raise VideoUnavailableError("No se pudo leer la información del video.")
    duration = info.get("duration")
    title = (info.get("title") or "").strip() or out_path.stem
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
    return candidates[0], title, duration


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


def _map_ytdlp_error(exc: yt_dlp.utils.DownloadError) -> VideoError:
    """Traduce un DownloadError de yt-dlp a un error con mensaje claro."""
    msg = str(exc).lower()
    if "private" in msg or "unavailable" in msg or "not available" in msg:
        return VideoUnavailableError("El video es privado o no está disponible.")
    if "age" in msg and "restrict" in msg:
        return VideoRestrictedError("El video está restringido por edad.")
    if "region" in msg or "country" in msg or "geo" in msg:
        return VideoRestrictedError("El video está restringido en esta región.")
    if "unsupported url" in msg or "no suitable" in msg:
        return VideoUnsupportedError("La URL no es soportada.")
    if (
        "timed out" in msg
        or "connection" in msg
        or "network" in msg
        or "temporary failure" in msg
    ):
        return VideoNetworkError("Fallo de red al contactar con el origen del video.")
    return VideoUnavailableError("No se pudo descargar el video.")


async def download_video(url: str) -> DownloadResult:
    """Descarga un video (máx. 720p) y devuelve el resultado con metadatos."""
    if not _is_safe_url(url):
        raise VideoUnavailableError("La URL no es válida o apunta a un host no permitido.")

    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings.temp_dir / uuid.uuid4().hex

    async with _semaphore:
        try:
            path, title, duration = await asyncio.to_thread(
                _download_sync, url, out_path
            )
        except VideoError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            raise _map_ytdlp_error(exc) from exc

    size = path.stat().st_size
    return DownloadResult(
        path=path,
        title=title,
        recompressed=False,
        duration=duration,
        size=size,
    )


def _recompress_sync(src: Path, dst: Path, target_bytes: int, duration: float) -> None:
    """Recomprime a un bitrate objetivo (una sola pasada)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RecompressError("ffmpeg no está disponible.")

    # Margen de seguridad del 10% y reserva para el audio.
    audio_kbps = 96
    total_kbps = max(int((target_bytes * 8 * 0.9) / duration / 1000), audio_kbps + 50)
    video_kbps = total_kbps - audio_kbps

    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(src),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        f"{video_kbps}k",
        "-maxrate",
        f"{video_kbps}k",
        "-bufsize",
        f"{video_kbps * 2}k",
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_kbps}k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["error desconocido"]
        raise RecompressError(f"Falló la recompresión: {tail[0][:200]}")


async def recompress_video(result: DownloadResult, target_bytes: int) -> DownloadResult:
    """Recomprime el video para intentar que quepa en target_bytes."""
    if not result.duration or result.duration <= 0:
        raise RecompressError("No se conoce la duración; no se puede recomprimir.")

    dst = result.path.with_name(f"{result.path.stem}_rc.mp4")
    try:
        await asyncio.to_thread(
            _recompress_sync, result.path, dst, target_bytes, result.duration
        )
    except RecompressError:
        raise
    except Exception as exc:
        raise RecompressError(f"Falló la recompresión: {exc}") from exc

    if not dst.exists():
        raise RecompressError("Falló la recompresión: no se generó el archivo.")

    return DownloadResult(
        path=dst,
        title=result.title,
        recompressed=True,
        duration=result.duration,
        size=dst.stat().st_size,
    )


def ffmpeg_available() -> bool:
    """Indica si el binario ffmpeg está disponible en el PATH."""
    return shutil.which("ffmpeg") is not None
