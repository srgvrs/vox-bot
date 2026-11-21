"""Descarga de video con yt-dlp (+ ffmpeg).

Fase 2: una sola extracción con `yt-dlp -J --no-playlist` (CLI, matable) y
elección de formato ANTES de descargar, con escalones de calidad.
"""
import asyncio
import ipaddress
import json
import logging
import shutil
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from config import settings
from services.proc import (
    ProcError,
    ProcTimeout,
    run_ffmpeg,
    run_ffprobe,
    run_ytdlp,
)

logger = logging.getLogger(__name__)

# Margen de seguridad sobre el límite de adjunto (97%).
_ATTACHMENT_SAFETY_MARGIN = 0.97
# Margen sobre el tamaño estimado de cada candidato (5%).
_SIZE_MARGIN = 1.05
# Bitrate de audio objetivo al recompresar (kbps).
_RECOMPRESS_AUDIO_KBPS = 64
# Bitrate mínimo de video para que la recompresión tenga sentido (kbps).
_RECOMPRESS_MIN_VIDEO_KBPS = 150
# Timeout de la extracción con yt-dlp -J (segundos).
_EXTRACT_TIMEOUT_S = 60


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
    extractor: str | None = None
    video_id: str | None = None
    # "attachment" | "recompress" | "host"
    mode: str | None = None


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


@dataclass
class _Candidate:
    """Candidato de descarga: IDs exactos y tamaño estimado."""

    video_id: str
    audio_id: str | None
    height: int
    est_size: int
    label: str

    @property
    def format_spec(self) -> str:
        if self.audio_id:
            return f"{self.video_id}+{self.audio_id}"
        return self.video_id


def _is_mp4_video(fmt: dict) -> bool:
    """Video mp4 (avc1) con altura conocida."""
    if fmt.get("vcodec") in (None, "none"):
        return False
    if not str(fmt.get("vcodec", "")).startswith("avc1"):
        return False
    ext = (fmt.get("ext") or "").lower()
    if ext and ext != "mp4":
        return False
    return isinstance(fmt.get("height"), int) and fmt["height"] > 0


def _is_m4a_audio(fmt: dict) -> bool:
    """Audio m4a (mp4a) sin video."""
    if fmt.get("acodec") in (None, "none"):
        return False
    if fmt.get("vcodec") not in (None, "none"):
        return False
    ext = (fmt.get("ext") or "").lower()
    return ext in ("m4a", "mp4")


def _is_single_mp4(fmt: dict) -> bool:
    """Formato único mp4 con video y audio."""
    if fmt.get("vcodec") in (None, "none"):
        return False
    if fmt.get("acodec") in (None, "none"):
        return False
    ext = (fmt.get("ext") or "").lower()
    if ext and ext != "mp4":
        return False
    return isinstance(fmt.get("height"), int) and fmt["height"] > 0


def _build_candidates(
    info: dict, duration: float | None, limit_bytes: int
) -> list[_Candidate]:
    """Construye candidatos de mayor a menor calidad (altura <= MAX_VIDEO_HEIGHT).

    Para cada altura: primero el mejor audio m4a, luego el de menor bitrate.
    Devuelve la lista ordenada por altura descendente (sin filtrar por tamaño).
    """
    formats = info.get("formats") or []
    max_height = settings.max_video_height

    videos = [
        f
        for f in formats
        if _is_mp4_video(f) and f["height"] <= max_height and f.get("format_id")
    ]
    audios = [f for f in formats if _is_m4a_audio(f) and f.get("format_id")]
    singles = [
        f
        for f in formats
        if _is_single_mp4(f) and f["height"] <= max_height and f.get("format_id")
    ]

    # Audio: mejor (mayor abr) y menor (menor abr).
    def _abr(f: dict) -> float:
        v = f.get("abr")
        return float(v) if isinstance(v, (int, float)) and v > 0 else 0.0

    best_audio = max(audios, key=_abr) if audios else None
    worst_audio = min(audios, key=_abr) if audios else None

    # Agrupar videos por altura; dentro de cada altura, el de mayor tbr.
    by_height: dict[int, list[dict]] = {}
    for f in videos:
        by_height.setdefault(int(f["height"]), []).append(f)

    candidates: list[_Candidate] = []
    for height in sorted(by_height.keys(), reverse=True):
        group = sorted(
            by_height[height],
            key=lambda f: float(f.get("tbr") or 0),
            reverse=True,
        )
        video = group[0]
        v_size = _estimate_size(video, duration)
        if v_size is None:
            continue
        # 1) mejor audio m4a
        if best_audio is not None:
            a_size = _estimate_size(best_audio, duration) or 0
            total = int((v_size + a_size) * _SIZE_MARGIN)
            candidates.append(
                _Candidate(
                    video_id=video["format_id"],
                    audio_id=best_audio["format_id"],
                    height=height,
                    est_size=total,
                    label=f"{height}p+audio_best",
                )
            )
        # 2) audio de menor bitrate (si es distinto)
        if worst_audio is not None and (
            best_audio is None
            or worst_audio["format_id"] != best_audio["format_id"]
        ):
            a_size = _estimate_size(worst_audio, duration) or 0
            total = int((v_size + a_size) * _SIZE_MARGIN)
            candidates.append(
                _Candidate(
                    video_id=video["format_id"],
                    audio_id=worst_audio["format_id"],
                    height=height,
                    est_size=total,
                    label=f"{height}p+audio_min",
                )
            )
        # 3) video sin audio (por si no hay pista m4a)
        if best_audio is None:
            candidates.append(
                _Candidate(
                    video_id=video["format_id"],
                    audio_id=None,
                    height=height,
                    est_size=int(v_size * _SIZE_MARGIN),
                    label=f"{height}p+no_audio",
                )
            )

    # Formatos únicos mp4 (con audio) como respaldo.
    for f in sorted(singles, key=lambda x: int(x["height"]), reverse=True):
        size = _estimate_size(f, duration)
        if size is None:
            continue
        candidates.append(
            _Candidate(
                video_id=f["format_id"],
                audio_id=None,
                height=int(f["height"]),
                est_size=int(size * _SIZE_MARGIN),
                label=f"{int(f['height'])}p+single",
            )
        )

    # Orden final: por altura descendente, luego por tamaño ascendente.
    candidates.sort(key=lambda c: (-c.height, c.est_size))
    logger.info(
        "Candidatos considerados (LIM=%d bytes): %s",
        limit_bytes,
        ", ".join(
            f"{c.label}={c.est_size / (1024 * 1024):.1f}MB" for c in candidates
        )
        or "(ninguno)",
    )
    return candidates


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


async def _extract_info_cli(url: str, work_dir: Path) -> dict:
    """Extrae la info con `yt-dlp -J --no-playlist` (CLI, matable).

    Guarda el JSON en work_dir/info.json y devuelve el dict.
    """
    info_path = work_dir / "info.json"
    args = [
        "-J",
        "--no-playlist",
        "--no-warnings",
        "--skip-download",
        "-o",
        str(info_path),
        url,
    ]
    try:
        result = await run_ytdlp(args, timeout=_EXTRACT_TIMEOUT_S)
    except ProcTimeout as exc:
        raise VideoNetworkError(
            "El origen del video tardó demasiado en responder; intenta de nuevo."
        ) from exc
    except ProcError as exc:
        raise VideoUnavailableError(
            "No se pudo leer la información del video."
        ) from exc
    if result.returncode != 0:
        # yt-dlp escribe el error en stderr; se traduce con el mapeador.
        raise _map_ytdlp_error(
            yt_dlp.utils.DownloadError(result.stderr or "yt-dlp falló"), url
        )
    raw = (result.stdout or "").strip()
    if not raw:
        raise VideoUnavailableError("No se pudo leer la información del video.")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VideoUnavailableError(
            "La información del video no es válida."
        ) from exc
    if not isinstance(info, dict):
        raise VideoUnavailableError("No se pudo leer la información del video.")
    try:
        info_path.write_text(raw, encoding="utf-8")
    except OSError as exc:
        logger.debug("No se pudo guardar info.json: %s", exc)
    return info


async def _probe_duration(path: Path) -> float | None:
    """Mide la duración del archivo con ffprobe; None si no se puede."""
    if shutil.which("ffprobe") is None:
        logger.warning("ffprobe no está disponible; no se puede medir la duración.")
        return None
    try:
        result = await run_ffprobe(
            [
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            timeout=15,
        )
    except ProcTimeout:
        logger.warning("ffprobe excedió el timeout al medir la duración.")
        return None
    except ProcError as exc:
        logger.warning("ffprobe falló al medir la duración: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "ffprobe devolvió código %d al medir la duración", result.returncode
        )
        return None
    raw = (result.stdout or "").strip().splitlines()
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


async def _download_with_format(
    url: str,
    out_path: Path,
    format_spec: str,
    max_filesize: int | None = None,
    extra_args: list[str] | None = None,
) -> Path | None:
    """Descarga con IDs exactos vía CLI. Devuelve la ruta o None si no cabe."""
    args = [
        "-f",
        format_spec,
        "--merge-output-format",
        "mp4",
        "--no-playlist",
        "--no-warnings",
        "--concurrent-fragments",
        "8",
        "-o",
        str(out_path.with_suffix(".%(ext)s")),
    ]
    if max_filesize is not None:
        args += ["--max-filesize", str(max_filesize)]
    if extra_args:
        args += extra_args
    args.append(url)

    try:
        result = await run_ytdlp(args, timeout=None)
    except ProcError as exc:
        raise VideoNetworkError(
            "Fallo de red al descargar el video."
        ) from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        if "max-filesize" in stderr or "file is larger than max" in stderr:
            logger.info(
                "Descarga abortada por --max-filesize (%d bytes)", max_filesize or 0
            )
            return None
        raise _map_ytdlp_error(
            yt_dlp.utils.DownloadError(result.stderr or "yt-dlp falló"), url
        )

    candidates = list(out_path.parent.glob(f"{out_path.stem}.*"))
    if not candidates:
        return None
    return candidates[0]


async def _download_sync(
    url: str,
    out_path: Path,
    max_bytes: int | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    on_status: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[Path, str, float | None, int, str | None, str | None, str | None, str | None, str]:
    """Descarga el video con escalones de formato (Fase 2).

    Devuelve (ruta, título, duración, altura, thumbnail, aviso, extractor,
    video_id, mode). mode ∈ {"attachment", "recompress", "host"}.
    """
    _notify(loop, on_status, "Obteniendo información del video...")
    work_dir = out_path.parent
    info = await _extract_info_cli(url, work_dir)

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
                logger.info(
                    "Carrusel detectado (%d entradas); se usa la primera.",
                    len(entries),
                )
            info = entries[0]

    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = None
    title = _pick_title(info, out_path.stem)
    thumbnail = _normalize_thumbnail(info.get("thumbnail"))
    extractor = info.get("extractor_key") or info.get("extractor")
    video_id = info.get("id")
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
        "Extracción: extractor=%s id=%s dominio=%s duración=%s instagram=%s long_reel=%s",
        extractor,
        video_id,
        urlparse(url).hostname,
        f"{duration:.1f}s" if duration is not None else "desconocida",
        is_instagram,
        is_long_instagram,
    )

    # Instagram reel largo: flujo dedicado (lado corto <= 480).
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
                "Instagram reel largo sin formatos con lado corto; se usa %dp",
                height,
            )
            fmt = _build_short_side_format(height)
        _notify(loop, on_status, f"Descargando video ({height}p)...")
        final_path = await _download_with_format(url, out_path, fmt)
        if final_path is None:
            raise VideoUnavailableError("No se pudo descargar el video.")
        if duration is None:
            probed = await _probe_duration(final_path)
            if probed is not None:
                duration = probed
        logger.info(
            "Tamaño real final: %d bytes (lado corto %dp, dominio=%s, duración=%s)",
            final_path.stat().st_size,
            height,
            urlparse(url).hostname,
            f"{duration:.1f}s" if duration is not None else "desconocida",
        )
        return (
            final_path,
            title,
            duration,
            height,
            thumbnail,
            notice,
            extractor,
            video_id,
            "attachment",
        )

    # Escalones de formato: elegir ANTES de descargar.
    limit_bytes = max_bytes or (settings.upload_limit_mb * 1024 * 1024)
    candidates = _build_candidates(info, duration, limit_bytes)

    chosen: _Candidate | None = None
    for cand in candidates:
        if cand.est_size <= limit_bytes:
            chosen = cand
            logger.info(
                "Candidato elegido: %s (estimado %.1f MB <= LIM %.1f MB)",
                cand.label,
                cand.est_size / (1024 * 1024),
                limit_bytes / (1024 * 1024),
            )
            break
        logger.info(
            "Candidato descartado: %s (estimado %.1f MB > LIM %.1f MB)",
            cand.label,
            cand.est_size / (1024 * 1024),
            limit_bytes / (1024 * 1024),
        )

    # Descargar el elegido; si el tamaño real excede LIM, probar el siguiente.
    if chosen is not None:
        idx = candidates.index(chosen)
        for cand in candidates[idx:]:
            _notify(loop, on_status, f"Descargando video ({cand.height}p)...")
            final_path = await _download_with_format(
                url, out_path, cand.format_spec, max_filesize=limit_bytes
            )
            if final_path is None:
                logger.info(
                    "Descarte por --max-filesize: %s", cand.label
                )
                continue
            real = final_path.stat().st_size
            if real > limit_bytes:
                logger.info(
                    "Descarte por tamaño real: %s (%.1f MB > LIM %.1f MB)",
                    cand.label,
                    real / (1024 * 1024),
                    limit_bytes / (1024 * 1024),
                )
                final_path.unlink(missing_ok=True)
                continue
            logger.info(
                "Descarga OK: %s, real %.1f MB (LIM %.1f MB)",
                cand.label,
                real / (1024 * 1024),
                limit_bytes / (1024 * 1024),
            )
            if duration is None:
                probed = await _probe_duration(final_path)
                if probed is not None:
                    duration = probed
            return (
                final_path,
                title,
                duration,
                cand.height,
                thumbnail,
                notice,
                extractor,
                video_id,
                "attachment",
            )

    # Ningún candidato cabe: decidir recompresión vs host.
    smallest = min(candidates, key=lambda c: c.est_size) if candidates else None
    duration_known = duration is not None and duration > 0
    duration_min = (duration or 0) / 60
    recompress_allowed = (
        settings.recompress_enabled
        and settings.recompress_max_minutes > 0
        and duration_known
        and duration_min <= settings.recompress_max_minutes
    )
    kbps = 0
    if duration_known and duration and duration > 0:
        kbps = int((limit_bytes * 8 / 1000 / duration) - _RECOMPRESS_AUDIO_KBPS)
    can_recompress = (
        recompress_allowed
        and smallest is not None
        and kbps >= _RECOMPRESS_MIN_VIDEO_KBPS
    )

    if can_recompress and smallest is not None:
        source_mb = smallest.est_size / (1024 * 1024)
        max_source_mb = settings.recompress_max_source_mb
        if max_source_mb > 0 and source_mb > max_source_mb:
            logger.info(
                "Recompresión descartada: candidato más pequeño %.1f MB > "
                "RECOMPRESS_MAX_SOURCE_MB=%d",
                source_mb,
                max_source_mb,
            )
            can_recompress = False
        else:
            logger.info(
                "Recompresión elegida: candidato %s (%.1f MB), kbps=%d",
                smallest.label,
                source_mb,
                kbps,
            )

    if can_recompress and smallest is not None:
        _notify(loop, on_status, f"Descargando video ({smallest.height}p)...")
        final_path = await _download_with_format(
            url, out_path, smallest.format_spec, max_filesize=None
        )
        if final_path is None:
            raise VideoUnavailableError("No se pudo descargar el video.")
        if duration is None:
            probed = await _probe_duration(final_path)
            if probed is not None:
                duration = probed
        logger.info(
            "Descarga para recompresión OK: %s, real %.1f MB",
            smallest.label,
            final_path.stat().st_size / (1024 * 1024),
        )
        return (
            final_path,
            title,
            duration,
            smallest.height,
            thumbnail,
            notice,
            extractor,
            video_id,
            "recompress",
        )

    # Host externo: descargar a HOST_VIDEO_HEIGHT (nunca 720p).
    host_height = settings.host_video_height
    logger.info(
        "Ningún candidato cabe y no se recompone; se descarga a %dp para host externo",
        host_height,
    )
    _notify(loop, on_status, f"Descargando video ({host_height}p)...")
    fmt = _build_format(host_height)
    final_path = await _download_with_format(url, out_path, fmt)
    if final_path is None:
        raise VideoUnavailableError("No se pudo descargar el video.")
    if duration is None:
        probed = await _probe_duration(final_path)
        if probed is not None:
            duration = probed
    logger.info(
        "Descarga para host OK: %dp, real %.1f MB",
        host_height,
        final_path.stat().st_size / (1024 * 1024),
    )
    return (
        final_path,
        title,
        duration,
        host_height,
        thumbnail,
        notice,
        extractor,
        video_id,
        "host",
    )


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
    # La concurrencia la controla services/video_queue.py (Fase 1).
    try:
        (
            path,
            title,
            duration,
            height,
            thumbnail,
            notice,
            extractor,
            video_id,
            mode,
        ) = await _download_sync(url, out_path, max_bytes, loop, on_status)
    except VideoError:
        raise
    except yt_dlp.utils.DownloadError as exc:
        raise _map_ytdlp_error(exc, url) from exc
    logger.info(
        "Descarga completada en %.2f s (mode=%s)", time.perf_counter() - start, mode
    )

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
        extractor=extractor,
        video_id=video_id,
        mode=mode,
    )


_qsv_available: bool | None = None
# Recuerda si h264_qsv falló en tiempo de ejecución para no reintentarlo.
_qsv_failed_runtime: bool = False


async def _has_qsv() -> bool:
    """Detecta h264_qsv una sola vez y lo cachea."""
    global _qsv_available
    if _qsv_available is not None:
        return _qsv_available
    if shutil.which("ffmpeg") is None:
        _qsv_available = False
        return False
    try:
        result = await run_ffmpeg(["-hide_banner", "-encoders"], timeout=10)
        _qsv_available = "h264_qsv" in (result.stdout or "")
    except (ProcError, ProcTimeout):
        _qsv_available = False
    return _qsv_available


async def _should_use_qsv() -> bool:
    """Decide si usar h264_qsv según ENCODER y el historial de fallos."""
    encoder = settings.encoder
    if encoder == "libx264":
        return False
    if encoder == "h264_qsv":
        return True
    # auto: usar QSV solo si está disponible y no ha fallado antes.
    if _qsv_failed_runtime:
        return False
    return await _has_qsv()


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


async def _recompress_sync(
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
    if shutil.which("ffmpeg") is None:
        raise RecompressError("ffmpeg no está disponible.")

    # Margen de seguridad: 20% con QSV (menos preciso), 10% con libx264.
    use_qsv = force_encoder is None and await _should_use_qsv()
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

    timeout_s = settings.recompress_timeout_s

    async def _run(encoder: str, extra: list[str]) -> bool:
        args = [
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
        try:
            result = await run_ffmpeg(args, timeout=timeout_s)
        except ProcTimeout:
            logger.warning(
                "Recompresión con %s excedió RECOMPRESS_TIMEOUT_S=%d; "
                "árbol de procesos terminado.",
                encoder,
                timeout_s,
            )
            dst.unlink(missing_ok=True)
            raise RecompressError(
                f"Recompresión excedió el tiempo máximo ({timeout_s} s)."
            )
        except ProcError as exc:
            logger.warning("Recompresión con %s falló: %s", encoder, exc)
            dst.unlink(missing_ok=True)
            return False
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-1:] or ["error desconocido"]
            logger.warning("Encoder %s falló: %s", encoder, tail[0][:200])
            dst.unlink(missing_ok=True)
            return False
        if not dst.exists() or dst.stat().st_size <= 0:
            dst.unlink(missing_ok=True)
            return False
        return True

    global _qsv_failed_runtime
    if use_qsv:
        logger.info("Recompresión usando encoder h264_qsv")
        if await _run("h264_qsv", []):
            return video_kbps
        _qsv_failed_runtime = True
        logger.warning(
            "h264_qsv falló; se usará libx264 en los próximos videos (hasta reiniciar)."
        )

    logger.info("Recompresión usando encoder libx264 (preset veryfast)")
    if not await _run("libx264", ["-preset", "veryfast"]):
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
        video_kbps = await _recompress_sync(
            result.path,
            dst,
            target_bytes,
            result.duration,
            original_height,
        )
    except RecompressError:
        raise
    except asyncio.CancelledError:
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
    logger.info(
        "Recompresión: bitrate objetivo=%d kbps, resultado=%.1f MB (límite %.1f MB)",
        video_kbps,
        real_size / (1024 * 1024),
        target_bytes / (1024 * 1024),
    )
    if real_size > target_bytes:
        logger.info(
            "Recompresión: sigue siendo demasiado grande (%.1f MB > %.1f MB)",
            real_size / (1024 * 1024),
            target_bytes / (1024 * 1024),
        )
    else:
        logger.info(
            "Recompresión: quedó en %.1f MB (cabe en el límite)",
            real_size / (1024 * 1024),
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
            await _recompress_sync(
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

    final_size = dst.stat().st_size
    if final_size > target_bytes:
        logger.info(
            "Sigue pesando %.1f MB; se subirá a un host externo (límite %.1f MB)",
            final_size / (1024 * 1024),
            target_bytes / (1024 * 1024),
        )

    return DownloadResult(
        path=dst,
        title=result.title,
        recompressed=True,
        duration=result.duration,
        size=dst.stat().st_size,
        height=result.height,
        thumbnail=result.thumbnail,
        extractor=result.extractor,
        video_id=result.video_id,
        mode="recompress",
    )


def ffmpeg_available() -> bool:
    """Indica si el binario ffmpeg está disponible en el PATH."""
    return shutil.which("ffmpeg") is not None
