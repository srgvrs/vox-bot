"""Reporte de errores: contador por hora y webhook opcional (anti-spam)."""
import asyncio
import logging
import time
from collections import deque

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 3600
_MIN_INTERVAL_SECONDS = 60
_MAX_MESSAGE_CHARS = 1500

_error_timestamps: deque[float] = deque()
_last_webhook_sent: float = 0.0
_webhook_lock = asyncio.Lock()


def _prune(now: float) -> None:
    cutoff = now - _WINDOW_SECONDS
    while _error_timestamps and _error_timestamps[0] < cutoff:
        _error_timestamps.popleft()


def count_last_hour() -> int:
    """Cantidad de errores registrados en la última hora."""
    _prune(time.monotonic())
    return len(_error_timestamps)


def _sanitize(text: str) -> str:
    """Recorta el mensaje y elimina posibles secretos conocidos."""
    for secret in (
        settings.discord_token,
        settings.serper_api_key,
    ):
        if secret:
            text = text.replace(secret, "***")
    return text[:_MAX_MESSAGE_CHARS]


async def _send_webhook(payload: str) -> None:
    url = settings.error_webhook_url
    if not url:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"content": payload}) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "Webhook de errores devolvió HTTP %d", resp.status
                    )
    except Exception as exc:
        logger.warning("No se pudo enviar el webhook de errores: %s", exc)


async def report_error(source: str, exc: BaseException) -> None:
    """Registra un error y, si procede, lo envía al webhook (máx. 1/min)."""
    global _last_webhook_sent
    now = time.monotonic()
    _error_timestamps.append(now)
    _prune(now)

    if not settings.error_webhook_url:
        return

    async with _webhook_lock:
        if now - _last_webhook_sent < _MIN_INTERVAL_SECONDS:
            return
        _last_webhook_sent = now

    payload = _sanitize(
        f"⚠️ Error en {source}: {type(exc).__name__}: {exc}"
    )
    await _send_webhook(payload)
