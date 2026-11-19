"""Descarga de video con yt-dlp (+ ffmpeg)."""
import asyncio
import ipaddress
import logging
import shutil
import socket
import subprocess
import time
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
    height: int | None = None


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


def _build_format(height: int | None = None) -> str:
    height = height or settings.max_video_height
    return (
        f"bv*[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]"
        f"/b[height<={height}]/b"
    )


def _build_small_format(max_bytes: int) -> str:
    """Formato que ya debería caber en max_bytes (evita recomprimir)."""
    return (
        f"bv*[height<=480][filesize_approx<{max_bytes}]+ba"
        f"/b[height<=480][filesize_approx<{max_bytes}]"
    )


def _download_sync(
    url: str, out_path: Path, max_bytes: int | None = None
) -> tuple[Path, str, float | None, int]:
    """Descarga bloqueante (se ejecuta en un hilo).

    Devuelve (ruta, título, duración, altura_usada).
    """
    ydl_opts = {
        "format": _build_format(),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": str(out_path.with_suffix(".%(ext)s")),
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": 8,
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

    # 2) Intentar un formato que ya quepa en max_bytes (evita recomprimir).
    if max_bytes:
        small_opts = {**ydl_opts, "format": _build_small_format(max_bytes)}
        try:
            with yt_dlp.YoutubeDL(small_opts) as ydl:
                ydl.download([url])
            candidates = list(out_path.parent.glob(f"{out_path.stem}.*"))
            if candidates and candidates[0].stat().st_size <= max_bytes:
                logger.info(
                    "Descarga con formato pre-ajustado (<= %d bytes, altura<=480p)",
                    max_bytes,
                )
                return candidates[0], title, duration, 480
            for c in candidates:
                c.unlink(missing_ok=True)
        except yt_dlp.utils.DownloadError:
            logger.info("No hay formato pre-ajustado; se usa el flujo normal.")

    # 3) Elegir altura según destino: si va a host externo (duración > límite de
    #    recompresión), usar HOST_VIDEO_HEIGHT; si no, MAX_VIDEO_HEIGHT.
    duration_min = (duration or 0) / 60
    if duration_min > settings.recompress_max_minutes:
        height = settings.host_video_height
        logger.info(
            "Altura elegida: %dp (destino host externo, duración %.1f min)",
            height,
            duration_min,
        )
    else:
        height = settings.max_video_height
        logger.info(
            "Altura elegida: %dp (destino adjunto, duración %.1f min)",
            height,
            duration_min,
        )

    # 4) Descargar con el formato normal.
    with yt_dlp.YoutubeDL({**ydl_opts, "format": _build_format(height)}) as ydl:
        ydl.download([url])

    # 5) Localizar el archivo final (la extensión puede variar tras el merge).
    candidates = list(out_path.parent.glob(f"{out_path.stem}.*"))
    if not candidates:
        raise VideoUnavailableError("No se pudo descargar el video.")
    return candidates[0], title, duration, height


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


async def download_video(url: str, max_bytes: int | None = None) -> DownloadResult:
    """Descarga un video (máx. 720p) y devuelve el resultado con metadatos."""
    if not _is_safe_url(url):
        raise VideoUnavailableError("La URL no es válida o apunta a un host no permitido.")

    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings.temp_dir / uuid.uuid4().hex

    start = time.perf_counter()
    async with _semaphore:
        try:
            path, title, duration, height = await asyncio.to_thread(
                _download_sync, url, out_path, max_bytes
            )
        except VideoError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            raise _map_ytdlp_error(exc) from exc
    logger.info("Descarga completada en %.2f s", time.perf_counter() - start)

    size = path.stat().st_size
    return DownloadResult(
        path=path,
        title=title,
        recompressed=False,
        duration=duration,
        size=size,
        height=height,
    )


_qsv_available: bool | None = None


def _has_qsv() -> bool:
    """Detecta h264_qsv una sola vez y lo cachea."""
    global _qsv_available
    if _qsv_available is not None:
        return _qsv_available
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        _qsv_available = False
        return False
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        _qsv_available = "h264_qsv" in (proc.stdout or "")
    except Exception:
        _qsv_available = False
    return _qsv_available


def _pick_height(video_kbps: int, original_height: int | None) -> int:
    """Elige la altura objetivo según el bitrate de video calculado."""
    if video_kbps < 400:
        height = 360
    elif video_kbps < 800:
        height = 480
    else:
        height = 720
    if original_height and original_height > 0:
        height = min(height, original_height)
    return height


def _recompress_sync(
    src: Path,
    dst: Path,
    target_bytes: int,
    duration: float,
    original_height: int | None = None,
) -> None:
    """Recomprime a un bitrate objetivo (una sola pasada)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RecompressError("ffmpeg no está disponible.")

    # Margen de seguridad del 10% y reserva para el audio.
    audio_kbps = 64
    total_kbps = max(int((target_bytes * 8 * 0.9) / duration / 1000), audio_kbps + 50)
    video_kbps = total_kbps - audio_kbps
    if video_kbps >= 400:
        audio_kbps = 96
        total_kbps = max(total_kbps, audio_kbps + 50)
        video_kbps = total_kbps - audio_kbps

    height = _pick_height(video_kbps, original_height)
    logger.info(
        "Recompresión: bitrate video=%d kbps, audio=%d kbps, altura=%dp",
        video_kbps,
        audio_kbps,
        height,
    )

    def _run(encoder: str, extra: list[str]) -> bool:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(src),
            "-vf",
            f"scale=-2:{height}",
            "-c:v",
            encoder,
            *extra,
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
            logger.warning("Encoder %s falló: %s", encoder, tail[0][:200])
            return False
        return dst.exists() and dst.stat().st_size > 0

    if _has_qsv():
        logger.info("Recompresión usando encoder h264_qsv")
        if _run("h264_qsv", []):
            return
        logger.info("h264_qsv falló; reintentando con libx264")

    logger.info("Recompresión usando encoder libx264")
    if not _run("libx264", ["-preset", "veryfast"]):
        raise RecompressError("Falló la recompresión con libx264.")


async def recompress_video(
    result: DownloadResult,
    target_bytes: int,
    original_height: int | None = None,
) -> DownloadResult:
    """Recomprime el video para intentar que quepa en target_bytes."""
    if not result.duration or result.duration <= 0:
        raise RecompressError("No se conoce la duración; no se puede recomprimir.")

    dst = result.path.with_name(f"{result.path.stem}_rc.mp4")
    start = time.perf_counter()
    try:
        await asyncio.to_thread(
            _recompress_sync,
            result.path,
            dst,
            target_bytes,
            result.duration,
            original_height,
        )
    except RecompressError:
        raise
    except Exception as exc:
        raise RecompressError(f"Falló la recompresión: {exc}") from exc
    logger.info("Recompresión completada en %.2f s", time.perf_counter() - start)

    if not dst.exists():
        raise RecompressError("Falló la recompresión: no se generó el archivo.")

    return DownloadResult(
        path=dst,
        title=result.title,
        recompressed=True,
        duration=result.duration,
        size=dst.stat().st_size,
        height=result.height,
    )


def ffmpeg_available() -> bool:
    """Indica si el binario ffmpeg está disponible en el PATH."""
    return shutil.which("ffmpeg") is not None
