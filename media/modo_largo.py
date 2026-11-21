"""Modo largo: descarga de videos de larga duración sin recodificar.

- Selecciona el mejor formato con altura <= 240 (video+audio o formato único).
- Estima el tamaño con filesize, filesize_approx o tbr*duración/8.
- Descarga con yt-dlp (CLI, prioridad baja) y --max-filesize.
- Nunca recodifica: el archivo se entrega tal cual.
"""
import logging
from collections.abc import Callable
from pathlib import Path

import yt_dlp

from config import settings
from services.proc import ProcError, ProcTimeout, run_ytdlp

logger = logging.getLogger(__name__)

# Altura máxima del formato en modo largo.
_LARGO_MAX_HEIGHT = 240
# Selector de respaldo si no hay formatos utilizables en la info.
_LARGO_FALLBACK_SELECTOR = (
    f"bv*[height<={_LARGO_MAX_HEIGHT}]+ba/b[height<={_LARGO_MAX_HEIGHT}]/w"
)
# Timeout de la descarga en modo largo (segundos).
_LARGO_DOWNLOAD_TIMEOUT_S = 1800

# Callback de progreso: recibe un evento corto y el texto de estado.
ProgressCallback = Callable[[str, str], None]


class ModoLargoError(Exception):
    """Error del modo largo con mensaje apto para el usuario."""


def es_largo(duracion_s: float | None) -> bool:
    """True si la duración supera VIDEO_LARGO_UMBRAL_MIN minutos."""
    if duracion_s is None or duracion_s <= 0:
        return False
    return duracion_s > settings.video_largo_umbral_min * 60


def validar_largo(duracion_s: float | None, mb_estimado: float) -> None:
    """Valida duración y tamaño estimado del modo largo.

    Lanza ModoLargoError indicando cuál límite se superó.
    """
    if duracion_s is not None and duracion_s > settings.video_max_largo_min * 60:
        raise ModoLargoError(
            f"El video dura más de {settings.video_max_largo_min} minutos "
            "(límite del modo largo)."
        )
    if mb_estimado > settings.video_largo_max_mb:
        raise ModoLargoError(
            f"El video pesa aproximadamente {mb_estimado:.0f} MB y supera el "
            f"límite de {settings.video_largo_max_mb} MB del modo largo."
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


def _is_video(fmt: dict) -> bool:
    """Formato con pista de video."""
    return fmt.get("vcodec") not in (None, "none")


def _is_audio_only(fmt: dict) -> bool:
    """Formato solo audio."""
    return (
        fmt.get("acodec") not in (None, "none")
        and fmt.get("vcodec") in (None, "none")
    )


def _height(fmt: dict) -> int | None:
    """Altura del formato si es un entero positivo."""
    h = fmt.get("height")
    if isinstance(h, int) and h > 0:
        return h
    return None


def _pick_audio(formats: list[dict], duration: float | None) -> dict | None:
    """Elige el audio más ligero disponible (o None si no hay)."""
    audios = [f for f in formats if _is_audio_only(f) and f.get("format_id")]
    if not audios:
        return None
    # Preferir audio sin sufijo "-drc" (dynamic range compression).
    non_drc = [
        f for f in audios if not str(f.get("format_id", "")).endswith("-drc")
    ]
    pool = non_drc or audios
    return min(pool, key=lambda f: _estimate_size(f, duration) or 0)


def elegir_formato_largo(
    formatos: list[dict], duracion_s: float | None
) -> tuple[str, float]:
    """Elige el mejor formato con altura <= 240 y devuelve (selector, MB estimado).

    Prefiere video+audio separados con IDs exactos; si no hay, usa un formato
    único con audio. Si nada es utilizable, devuelve el selector de respaldo.
    """
    duration = duracion_s if duracion_s and duracion_s > 0 else None
    videos = [
        f
        for f in formatos
        if _is_video(f)
        and f.get("format_id")
        and _height(f) is not None
        and _height(f) <= _LARGO_MAX_HEIGHT
    ]
    if not videos:
        logger.info(
            "Modo largo: sin formatos con altura <= %dp; se usa el selector de respaldo",
            _LARGO_MAX_HEIGHT,
        )
        return _LARGO_FALLBACK_SELECTOR, 0.0

    # Preferir formatos https con filesize exacto y sin av01 (tamaño fiable).
    def _exact(f: dict) -> bool:
        fs = f.get("filesize")
        return (
            f.get("protocol") == "https"
            and isinstance(fs, (int, float))
            and fs > 0
            and not str(f.get("vcodec") or "").startswith("av01")
        )

    exact = [f for f in videos if _exact(f)]
    if exact:
        top_h = max(_height(f) or 0 for f in exact)
        at_h = [f for f in exact if (_height(f) or 0) == top_h]
        video = min(at_h, key=lambda f: int(f["filesize"]))
    else:
        # Ordenar por altura descendente y luego por tbr descendente.
        videos.sort(
            key=lambda f: (_height(f) or 0, float(f.get("tbr") or 0)),
            reverse=True,
        )
        video = videos[0]
    v_size = _estimate_size(video, duration) or 0

    audio = _pick_audio(formatos, duration)
    if audio is not None:
        a_size = _estimate_size(audio, duration) or 0
        selector = f"{video['format_id']}+{audio['format_id']}"
        total = v_size + a_size
    else:
        selector = video["format_id"]
        total = v_size

    mb = total / (1024 * 1024)
    logger.info(
        "Modo largo: formato elegido %s (%dp, ~%.1f MB)",
        selector,
        _height(video) or 0,
        mb,
    )
    return selector, mb


def _emit_progress(
    on_progress: ProgressCallback | None, event: str, text: str
) -> None:
    """Invoca el callback de progreso (síncrono, tolerante a fallos)."""
    if on_progress is None:
        return
    try:
        on_progress(event, text)
    except Exception:
        logger.debug("on_progress falló (event=%s)", event)


async def descargar_largo(
    url: str,
    info: dict,
    carpeta: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Descarga un video en modo largo (sin recodificar).

    Usa el selector elegido a partir de `info` y --max-filesize según
    VIDEO_LARGO_MAX_MB. Devuelve la ruta del archivo descargado.
    """
    carpeta.mkdir(parents=True, exist_ok=True)
    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = None

    formatos = info.get("formats") or []
    selector, mb_estimado = elegir_formato_largo(formatos, duration)
    validar_largo(duration, mb_estimado)

    max_bytes = settings.video_largo_max_mb * 1024 * 1024
    out_template = carpeta / "largo.%(ext)s"

    _emit_progress(on_progress, "formato", f"Formato elegido: {selector}")
    _emit_progress(on_progress, "descargando", "Descargando video (modo largo)")

    args = [
        "-f",
        selector,
        "--merge-output-format",
        "mp4",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--progress",
        "--max-filesize",
        str(max_bytes),
        "-o",
        str(out_template),
        url,
    ]
    try:
        result = await run_ytdlp(args, timeout=_LARGO_DOWNLOAD_TIMEOUT_S)
    except ProcTimeout as exc:
        raise ModoLargoError(
            "La descarga en modo largo superó el tiempo máximo."
        ) from exc
    except ProcError as exc:
        raise ModoLargoError(
            "Fallo de red al descargar el video en modo largo."
        ) from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        if "max-filesize" in stderr or "file is larger than max" in stderr:
            raise ModoLargoError(
                f"El video supera el límite de {settings.video_largo_max_mb} MB "
                "del modo largo."
            )
        raise ModoLargoError(
            _map_ytdlp_error(yt_dlp.utils.DownloadError(result.stderr or "yt-dlp falló"))
        )

    candidates = list(carpeta.glob("largo.*"))
    if not candidates:
        raise ModoLargoError("No se pudo descargar el video en modo largo.")

    final_path = candidates[0]
    _emit_progress(on_progress, "descargado", "Video descargado")
    logger.info(
        "Modo largo OK: %s (%.1f MB)",
        final_path.name,
        final_path.stat().st_size / (1024 * 1024),
    )
    return final_path


def _map_ytdlp_error(exc: yt_dlp.utils.DownloadError) -> str:
    """Traduce un DownloadError de yt-dlp a un mensaje claro en español."""
    msg = str(exc).lower()
    if "timed out" in msg or "timeout" in msg:
        return "El origen del video tardó demasiado en responder; intenta de nuevo."
    if "403" in msg or "forbidden" in msg or "blocked" in msg or "captcha" in msg:
        return "El sitio bloqueó la descarga; intenta más tarde."
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
        return "Requiere iniciar sesión; no se puede descargar."
    if "requested format is not available" in msg:
        return "El formato del video no está disponible; intenta de nuevo."
    if "private" in msg or "unavailable" in msg or "not available" in msg:
        return "El video es privado o no está disponible."
    if "region" in msg or "country" in msg or "geo" in msg:
        return "El video está restringido en esta región."
    if "unsupported url" in msg or "no suitable" in msg:
        return "La URL no es soportada."
    if "connection" in msg or "network" in msg or "temporary failure" in msg:
        return "Fallo de red al contactar con el origen del video."
    return "No se pudo descargar el video."
