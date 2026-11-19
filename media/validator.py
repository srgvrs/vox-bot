"""Valida que una URL de imagen responda y sea realmente una imagen."""
import logging
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = aiohttp.ClientTimeout(total=6)


async def is_valid_image(session: aiohttp.ClientSession, url: str) -> bool:
    """Devuelve True si la URL responde 200 con Content-Type image/*."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    headers = {
        "User-Agent": _BROWSER_UA,
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    }
    try:
        async with session.get(url, headers=headers, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            content_type = resp.headers.get("Content-Type", "")
            return content_type.lower().startswith("image/")
    except Exception:
        logger.debug("URL de imagen inválida: %s", url, exc_info=True)
        return False
