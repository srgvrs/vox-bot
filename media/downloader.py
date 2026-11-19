"""Descarga de video con yt-dlp (+ ffmpeg)."""
import asyncio
import ipaddress
import logging
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from config import settings

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget

    _IMPERSONATE_AVAILABLE = True
except Exception:  # pragma: no cover - depende de curl_cffi
    ImpersonateTarget = None  # type: ignore[assignment]
    _IMPERSONATE_AVAILABLE = False

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


class VideoLoginRequiredError(VideoError):
    """El origen exige iniciar sesión (p. ej. Instagram)."""


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
    thumbnail: str | None = None
    notice: str | None = None


def _host_allowed(host: str) -> bool:
    """Comprueba si el host (o un subdominio) está en ALLOWED_VIDEO_DOMAINS."""
    host = host.lower().rstrip(".")
    for domain in settings.allowed_video_domains:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _is_safe_url(url: str) -> bool:
    """Solo permite http/https, dominios permitidos y rechaza hosts internos."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in ("localhost", "127.0.0.1", "::1"):
        return False
    if not _host_allowed(host):
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


def _short_side(fmt: dict) -> int | None:
    """Lado corto del formato (min(width, height)); en vertical height es el largo."""
    w = fmt.get("width")
    h = fmt.get("height")
    if isinstance(w, int) and w > 0 and isinstance(h, int) and h > 0:
        return min(w, h)
    if isinstance(h, int) and h > 0:
        return h
    return None


def _build_format(height: int | None = None) -> str:
    height = height or settings.max_video_height
    return (
        f"bv*[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]"
        f"/bv*[height<={height}]+ba"
        f"/b[height<={height}]"
        f"/bv*+ba"
        f"/b"
    )


def _build_small_format(max_bytes: int) -> str:
    """Formato que ya debería caber en max_bytes (evita recomprimir)."""
    return (
        f"bv*[height<=480][filesize_approx<{max_bytes}]+ba"
        f"/b[height<=480][filesize_approx<{max_bytes}]"
        f"/bv*[height<=480]+ba"
        f"/b[height<=480]"
        f"/b"
    )


def _build_short_side_format(short_side: int) -> str:
    """Formato limitado por el lado corto (para verticales de Instagram)."""
    return (
        f"bv*[height<={short_side}][vcodec^=avc1]+ba[acodec^=mp4a]"
        f"/bv*[height<={short_side}]+ba"
        f"/b[height<={short_side}]"
        f"/bv*+ba"
        f"/b"
    )


def _estimate_size(fmt: dict, duration: float | None) -> int | None:
    """Estima el tamaño de un formato en bytes (filesize, approx o tbr*duración)."""
    size = fmt.get("filesize")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    approx = fmt.get("filesize_approx")
    if isinstance(approx, (int, float)) and approx > 0:
        return int(approx)
    tbr = fmt.get("tbr")
    if isinstance(tbr, (int, float)) and tbr > 0 and duration and duration > 0:
        return int(tbr * duration * 125)
    return None


def _pick_host_formats(
    info: dict, duration: float | None, max_bytes: int
) -> tuple[dict, dict, int, int] | None:
    """Elige (video, audio, altura, tamaño_estimado) para host externo.

    Devuelve None si no hay formatos utilizables.
    """
    formats = info.get("formats") or []
    videos = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none")
        and _short_side(f) is not None
        and _short_side(f) <= settings.host_video_height
    ]
    audios = [
        f
        for f in formats
        if f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
    ]
    if not videos:
        # Sin formatos con altura (p. ej. Instagram): usar el mejor disponible.
        fallback = [
            f
            for f in formats
            if f.get("vcodec") not in (None, "none") and f.get("format_id")
        ]
        if not fallback:
            return None
        best = max(
            fallback,
            key=lambda f: _estimate_size(f, duration) or 0,
        )
        logger.info(
            "Sin formatos con altura; se usa el mejor disponible (%s)",
            best.get("format_id"),
        )
        return best, None, 0, _estimate_size(best, duration) or 0

    # Audio más ligero con abr >= 96 (si no hay, el más ligero disponible).
    def _audio_size(f: dict) -> int:
        return _estimate_size(f, duration) or 0

    audio_candidates = [
        f for f in audios if isinstance(f.get("abr"), (int, float)) and f["abr"] >= 96
    ]
    if not audio_candidates:
        audio_candidates = audios
    audio = min(audio_candidates, key=_audio_size) if audio_candidates else None
    audio_size = _audio_size(audio) if audio else 0

    # Preferir códec avc1 si existe, luego ordenar por lado corto descendente.
    def _video_key(f: dict) -> tuple[int, int]:
        is_avc = 1 if str(f.get("vcodec", "")).startswith("avc1") else 0
        return (is_avc, _short_side(f) or 0)

    videos_sorted = sorted(videos, key=_video_key, reverse=True)

    chosen: dict | None = None
    chosen_size = 0
    for f in videos_sorted:
        v_size = _estimate_size(f, duration)
        if v_size is None:
            continue
        total = int((v_size + audio_size) * 1.05)
        if total <= max_bytes:
            chosen = f
            chosen_size = total
            break

    if chosen is None:
        # Ninguno cabe: usar el de menor lado corto disponible.
        chosen = min(videos, key=lambda f: _short_side(f) or 0)
        v_size = _estimate_size(chosen, duration) or 0
        chosen_size = int((v_size + audio_size) * 1.05)
        logger.warning(
            "Ningún formato cabe en HOST_MAX_MB=%d; se usa el de menor lado corto (%dp, ~%d bytes)",
            settings.host_max_mb,
            _short_side(chosen) or 0,
            chosen_size,
        )

    return chosen, audio, _short_side(chosen) or 0, chosen_size


_GENERIC_TITLES = {
    "",
    "instagram",
    "video",
    "reel",
    "post",
    "untitled",
    "tiktok",
    "x",
    "twitter",
}


def _pick_title(info: dict, fallback_stem: str) -> str:
    """Elige un título útil; usa @uploader si falta y recorta a 100 caracteres."""
    title = (info.get("title") or "").strip()
    if title and title.lower() not in _GENERIC_TITLES:
        return title[:100]
    uploader = (info.get("uploader") or info.get("uploader_id") or "").strip()
    if uploader:
        if not uploader.startswith("@"):
            uploader = "@" + uploader
        return f"Video de {uploader}"[:100]
    if "instagram.com" in (info.get("webpage_url") or ""):
        return "Video de Instagram"
    return fallback_stem[:100]


def _pick_instagram_short_side(
    info: dict, duration: float | None, max_bytes: int
) -> tuple[dict, dict | None, int, int] | None:
    """Elige formato para un reel largo de Instagram limitando el lado corto a 480.

    Devuelve (video, audio, lado_corto, tamaño_estimado) o None si no hay formatos.
    """
    formats = info.get("formats") or []
    videos = [
        f
        for f in formats
        if f.get("vcodec") not in (None, "none")
        and _short_side(f) is not None
        and _short_side(f) <= 480
    ]
    if not videos:
        return None
    audios = [
        f
        for f in formats
        if f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
    ]

    def _audio_size(f: dict) -> int:
        return _estimate_size(f, duration) or 0

    audio_candidates = [
        f for f in audios if isinstance(f.get("abr"), (int, float)) and f["abr"] >= 96
    ]
    if not audio_candidates:
        audio_candidates = audios
    audio = min(audio_candidates, key=_audio_size) if audio_candidates else None
    audio_size = _audio_size(audio) if audio else 0

    def _video_key(f: dict) -> tuple[int, int]:
        is_avc = 1 if str(f.get("vcodec", "")).startswith("avc1") else 0
        return (is_avc, _short_side(f) or 0)

    videos_sorted = sorted(videos, key=_video_key, reverse=True)
    chosen: dict | None = None
    chosen_size = 0
    for f in videos_sorted:
        v_size = _estimate_size(f, duration)
        if v_size is None:
            continue
        total = int((v_size + audio_size) * 1.05)
        if total <= max_bytes:
            chosen = f
            chosen_size = total
            break
    if chosen is None:
        chosen = min(videos, key=lambda f: _short_side(f) or 0)
        v_size = _estimate_size(chosen, duration) or 0
        chosen_size = int((v_size + audio_size) * 1.05)

    return chosen, audio, _short_side(chosen) or 0, chosen_size


def _probe_duration(path: Path) -> float | None:
    """Mide la duración del archivo con ffprobe; None si no se puede."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        logger.warning("ffprobe no está disponible; no se puede medir la duración.")
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        logger.warning("ffprobe falló al medir la duración: %s", exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "ffprobe devolvió código %d al medir la duración", proc.returncode
        )
        return None
    raw = (proc.stdout or "").strip().splitlines()
    if not raw:
        return None
    try:
        value = float(raw[0])
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def _normalize_thumbnail(url: str | None) -> str | None:
    """Normaliza la miniatura: YouTube .webp -> .jpg."""
    if not url:
        return None
    if "ytimg.com" in url or "youtube.com" in url or "youtu.be" in url:
        if url.endswith(".webp"):
            return url[:-5] + ".jpg"
    return url


def _notify(
    loop: asyncio.AbstractEventLoop | None,
    on_status: Callable[[str], Awaitable[None]] | None,
    text: str,
) -> None:
    """Programa el callback de estado en el loop (desde un hilo)."""
    if loop is None or on_status is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(on_status(text), loop)
    except Exception:
        logger.debug("No se pudo programar el estado: %s", text)


def _download_sync(
    url: str,
    out_path: Path,
    max_bytes: int | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    on_status: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[Path, str, float | None, int, str | None, str | None]:
    """Descarga bloqueante (se ejecuta en un hilo).

    Devuelve (ruta, título, duración, altura_usada, thumbnail, aviso).
    """
    _notify(loop, on_status, "Obteniendo información del video...")
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

    host = (urlparse(url).hostname or "").lower()
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        if _IMPERSONATE_AVAILABLE and ImpersonateTarget is not None:
            ydl_opts["impersonate"] = ImpersonateTarget("chrome")
            logger.info("TikTok: usando impersonate=chrome (curl_cffi)")
        else:
            logger.warning("TikTok puede fallar sin curl_cffi")

    # 1) Comprobar duración antes de descargar.
    with yt_dlp.YoutubeDL({**ydl_opts, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise VideoUnavailableError("No se pudo leer la información del video.")

    # Carrusel de Instagram: tomar solo la primera entrada.
    notice: str | None = None
    entries = info.get("entries")
    if entries:
        entries = [e for e in entries if isinstance(e, dict)]
        if entries:
            if len(entries) > 1:
                notice = (
                    f"El enlace contiene {len(entries)} elementos; "
                    "se descarga solo el primero."
                )
                logger.info("Carrusel detectado (%d entradas); se usa la primera.", len(entries))
            info = entries[0]

    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = None
    title = _pick_title(info, out_path.stem)
    thumbnail = _normalize_thumbnail(info.get("thumbnail"))
    if duration is not None and duration > settings.max_video_minutes * 60:
        raise VideoTooLongError(
            f"El video dura más de {settings.max_video_minutes} minutos."
        )

    webpage = (info.get("webpage_url") or url or "").lower()
    is_instagram = "instagram.com" in webpage
    is_long_instagram = (
        is_instagram
        and duration is not None
        and duration > settings.instagram_long_reel_seconds
    )
    logger.info(
        "Dominio=%s duración=%s instagram=%s long_reel=%s",
        urlparse(url).hostname,
        f"{duration:.1f}s" if duration is not None else "desconocida",
        is_instagram,
        is_long_instagram,
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
                return candidates[0], title, duration, 480, thumbnail, notice
            for c in candidates:
                c.unlink(missing_ok=True)
        except yt_dlp.utils.DownloadError:
            logger.info("No hay formato pre-ajustado; se usa el flujo normal.")

    # 3) Elegir altura según destino: si va a host externo (duración > límite de
    #    recompresión), usar HOST_VIDEO_HEIGHT con tope HOST_MAX_MB; si no,
    #    MAX_VIDEO_HEIGHT. Los reels largos de Instagram se limitan a 480p
    #    (lado corto) para intentar que quepan como adjunto sin recomprimir.
    duration_min = (duration or 0) / 60
    if duration is None:
        logger.warning(
            "Duración desconocida; se saltan las reglas por duración y se usa "
            "el flujo de adjunto (recompresión permitida si no cabe)."
        )
    if is_long_instagram:
        picked_ig = _pick_instagram_short_side(info, duration, max_bytes or 0)
        if picked_ig is not None:
            video_fmt, audio_fmt, short_side, est_size = picked_ig
            logger.info(
                "Instagram reel largo: lado corto=%dp, tamaño estimado ~%d bytes "
                "(límite adjunto %d bytes)",
                short_side,
                est_size,
                max_bytes or 0,
            )
            if audio_fmt is not None:
                fmt = f"{video_fmt['format_id']}+{audio_fmt['format_id']}"
            else:
                fmt = video_fmt["format_id"]
            height = short_side
        else:
            height = 480
            logger.info(
                "Instagram reel largo sin formatos con lado corto; se usa %dp", height
            )
            fmt = _build_short_side_format(height)
    elif duration is not None and duration_min > settings.recompress_max_minutes:
        host_max_bytes = settings.host_max_mb * 1024 * 1024
        picked = _pick_host_formats(info, duration, host_max_bytes)
        if picked is None:
            height = settings.host_video_height
            logger.info(
                "Altura elegida: %dp (destino host externo, sin formatos; duración %.1f min)",
                height,
                duration_min,
            )
            fmt = _build_format(height)
        else:
            video_fmt, audio_fmt, height, est_size = picked
            logger.info(
                "Altura elegida: %dp (destino host externo, duración %.1f min, "
                "tamaño estimado ~%d bytes)",
                height,
                duration_min,
                est_size,
            )
            if audio_fmt is not None:
                fmt = f"{video_fmt['format_id']}+{audio_fmt['format_id']}"
            else:
                fmt = video_fmt["format_id"]
    else:
        height = settings.max_video_height
        logger.info(
            "Altura elegida: %dp (destino adjunto, duración %.1f min)",
            height,
            duration_min,
        )
        fmt = _build_format(height)

    # 4) Descargar con el formato elegido (contenedor mp4).
    _notify(loop, on_status, f"Descargando video ({height}p)...")
    with yt_dlp.YoutubeDL({**ydl_opts, "format": fmt}) as ydl:
        ydl.download([url])

    # 5) Localizar el archivo final (la extensión puede variar tras el merge).
    candidates = list(out_path.parent.glob(f"{out_path.stem}.*"))
    if not candidates:
        raise VideoUnavailableError("No se pudo descargar el video.")
    final_path = candidates[0]
    if duration is None:
        probed = _probe_duration(final_path)
        if probed is not None:
            duration = probed
            logger.info("Duración medida con ffprobe: %.1fs", duration)
        else:
            logger.warning(
                "No se pudo medir la duración del archivo; se deja como desconocida."
            )
    logger.info(
        "Tamaño real final: %d bytes (lado corto %dp, dominio=%s, duración=%s)",
        final_path.stat().st_size,
        height,
        urlparse(url).hostname,
        f"{duration:.1f}s" if duration is not None else "desconocida",
    )
    return final_path, title, duration, height, thumbnail, notice


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


def _map_ytdlp_error(
    exc: yt_dlp.utils.DownloadError, url: str | None = None
) -> VideoError:
    """Traduce un DownloadError de yt-dlp a un error con mensaje claro."""
    msg = str(exc).lower()
    host = (urlparse(url).hostname or "").lower() if url else ""
    is_x = host == "x.com" or host.endswith(".x.com") or host == "twitter.com" or host.endswith(".twitter.com")
    is_tiktok = host == "tiktok.com" or host.endswith(".tiktok.com")

    if "timed out" in msg or "timeout" in msg:
        return VideoNetworkError(
            "El origen del video tardó demasiado en responder; intenta de nuevo."
        )
    if "403" in msg or "forbidden" in msg or "blocked" in msg or "captcha" in msg:
        return VideoUnavailableError(
            "El sitio bloqueó la descarga; intenta más tarde."
        )
    if (
        "login" in msg
        or "log in" in msg
        or "sign in" in msg
        or "cookies" in msg
        or "rate-limit" in msg
        or "rate limit" in msg
        or "sensitive" in msg
        or "age-restricted" in msg
    ):
        if is_x or is_tiktok:
            return VideoLoginRequiredError(
                "Requiere iniciar sesión; no se puede descargar."
            )
        return VideoLoginRequiredError(
            "Instagram pide iniciar sesión para este contenido; no se puede descargar."
        )
    if "requested format is not available" in msg:
        return VideoUnavailableError(
            "El formato del video no está disponible; intenta de nuevo."
        )
    if "no video" in msg or "no media" in msg or "no formats" in msg:
        if is_x:
            return VideoUnavailableError("Ese tweet no tiene video.")
        if is_tiktok:
            return VideoUnavailableError(
                "Esa publicación no tiene video; puede ser de fotos."
            )
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


async def download_video(
    url: str,
    max_bytes: int | None = None,
    on_status: Callable[[str], Awaitable[None]] | None = None,
) -> DownloadResult:
    """Descarga un video (máx. 720p) y devuelve el resultado con metadatos."""
    if not _is_safe_url(url):
        raise VideoUnavailableError("La URL no es válida o apunta a un host no permitido.")

    logger.info("Solicitud /video para dominio: %s", urlparse(url).hostname)

    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings.temp_dir / uuid.uuid4().hex

    if on_status is not None:
        await on_status("Buscando video...")

    start = time.perf_counter()
    loop = asyncio.get_running_loop()
    async with _semaphore:
        try:
            # yt-dlp y ffmpeg son bloqueantes: se ejecutan en un hilo para no
            # congelar el event loop (y la animación del estado).
            path, title, duration, height, thumbnail, notice = await asyncio.to_thread(
                _download_sync, url, out_path, max_bytes, loop, on_status
            )
        except VideoError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            raise _map_ytdlp_error(exc, url) from exc
    logger.info("Descarga completada en %.2f s", time.perf_counter() - start)

    size = path.stat().st_size
    return DownloadResult(
        path=path,
        title=title,
        recompressed=False,
        duration=duration,
        size=size,
        height=height,
        thumbnail=thumbnail,
        notice=notice,
    )


_qsv_available: bool | None = None
# Recuerda si h264_qsv falló en tiempo de ejecución para no reintentarlo.
_qsv_failed_runtime: bool = False


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


def _should_use_qsv() -> bool:
    """Decide si usar h264_qsv según ENCODER y el historial de fallos."""
    encoder = settings.encoder
    if encoder == "libx264":
        return False
    if encoder == "h264_qsv":
        return True
    # auto: usar QSV solo si está disponible y no ha fallado antes.
    if _qsv_failed_runtime:
        return False
    return _has_qsv()


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
    force_encoder: str | None = None,
    force_video_kbps: int | None = None,
) -> int:
    """Recomprime a un bitrate objetivo (una sola pasada).

    Devuelve el bitrate de video usado (kbps). Si force_encoder es "libx264",
    se salta la detección de QSV y se usa libx264 con el bitrate indicado.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RecompressError("ffmpeg no está disponible.")

    # Margen de seguridad: 20% con QSV (menos preciso), 10% con libx264.
    use_qsv = force_encoder is None and _should_use_qsv()
    margin = 0.80 if use_qsv else 0.90

    audio_kbps = 64
    if force_video_kbps is not None:
        video_kbps = max(int(force_video_kbps), 50)
        total_kbps = video_kbps + audio_kbps
    else:
        total_kbps = max(
            int((target_bytes * 8 * margin) / duration / 1000), audio_kbps + 50
        )
        video_kbps = total_kbps - audio_kbps
    if video_kbps >= 400:
        audio_kbps = 96
        total_kbps = max(total_kbps, audio_kbps + 50)
        video_kbps = total_kbps - audio_kbps

    height = _pick_height(video_kbps, original_height)
    logger.info(
        "Recompresión: bitrate video=%d kbps, audio=%d kbps, altura=%dp, margen=%.0f%%",
        video_kbps,
        audio_kbps,
        height,
        margin * 100,
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
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _, stderr = proc.communicate()
        except KeyboardInterrupt:
            # Cierre ordenado: terminar ffmpeg y limpiar el temporal.
            logger.warning("SIGINT durante la recompresión; terminando ffmpeg.")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            dst.unlink(missing_ok=True)
            raise
        if proc.returncode != 0:
            tail = (stderr or "").strip().splitlines()[-1:] or ["error desconocido"]
            logger.warning("Encoder %s falló: %s", encoder, tail[0][:200])
            return False
        return dst.exists() and dst.stat().st_size > 0

    global _qsv_failed_runtime
    if use_qsv:
        logger.info("Recompresión usando encoder h264_qsv")
        if _run("h264_qsv", []):
            return video_kbps
        _qsv_failed_runtime = True
        logger.warning(
            "h264_qsv falló; se usará libx264 en los próximos videos (hasta reiniciar)."
        )

    logger.info("Recompresión usando encoder libx264 (preset veryfast)")
    if not _run("libx264", ["-preset", "veryfast"]):
        raise RecompressError("Falló la recompresión con libx264.")
    return video_kbps


async def recompress_video(
    result: DownloadResult,
    target_bytes: int,
    original_height: int | None = None,
    on_status: Callable[[str], Awaitable[None]] | None = None,
) -> DownloadResult:
    """Recomprime el video para intentar que quepa en target_bytes.

    Si el primer intento se pasa del límite por menos del 25%, reintenta una
    vez con libx264 -preset veryfast y un bitrate ajustado al tamaño real.
    """
    if not result.duration or result.duration <= 0:
        raise RecompressError("No se conoce la duración; no se puede recomprimir.")

    if on_status is not None:
        await on_status("Comprimiendo video para que quepa en Discord...")

    dst = result.path.with_name(f"{result.path.stem}_rc.mp4")
    start = time.perf_counter()
    try:
        video_kbps = await asyncio.to_thread(
            _recompress_sync,
            result.path,
            dst,
            target_bytes,
            result.duration,
            original_height,
        )
    except RecompressError:
        raise
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Cierre ordenado: borrar el temporal parcial y propagar.
        dst.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dst.unlink(missing_ok=True)
        raise RecompressError(f"Falló la recompresión: {exc}") from exc

    if not dst.exists():
        raise RecompressError("Falló la recompresión: no se generó el archivo.")

    real_size = dst.stat().st_size
    logger.info(
        "Recompresión intento 1: estimado<=%d bytes, real=%d bytes (encoder inicial)",
        target_bytes,
        real_size,
    )

    # Reintento si se pasa por menos del 25% del límite.
    if real_size > target_bytes and real_size <= target_bytes * 1.25:
        new_kbps = max(int(video_kbps * (target_bytes * 0.93 / real_size)), 50)
        logger.info(
            "Recompresión intento 2 con libx264: bitrate %d -> %d kbps "
            "(real %d > límite %d, dentro del 25%%)",
            video_kbps,
            new_kbps,
            real_size,
            target_bytes,
        )
        try:
            await asyncio.to_thread(
                _recompress_sync,
                result.path,
                dst,
                target_bytes,
                result.duration,
                original_height,
                "libx264",
                new_kbps,
            )
        except RecompressError as exc:
            logger.warning("Reintento de recompresión falló: %s", exc)
        else:
            if dst.exists():
                real_size = dst.stat().st_size
                logger.info(
                    "Recompresión intento 2: estimado<=%d bytes, real=%d bytes "
                    "(encoder libx264, bitrate %d kbps)",
                    target_bytes,
                    real_size,
                    new_kbps,
                )

    logger.info("Recompresión completada en %.2f s", time.perf_counter() - start)

    return DownloadResult(
        path=dst,
        title=result.title,
        recompressed=True,
        duration=result.duration,
        size=dst.stat().st_size,
        height=result.height,
        thumbnail=result.thumbnail,
    )


def ffmpeg_available() -> bool:
    """Indica si el binario ffmpeg está disponible en el PATH."""
    return shutil.which("ffmpeg") is not None
