"""Proveedor de respaldo: Serper.dev (requiere SERPER_API_KEY)."""
import logging
from urllib.parse import urlparse

import aiohttp

from config import settings
from search.base import ImageResult, SearchProvider

logger = logging.getLogger(__name__)

_ENDPOINT = "https://google.serper.dev/images"
_TIMEOUT_SECONDS = 8


class SerperProvider(SearchProvider):
    name = "serper"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.serper_api_key

    async def search(self, query: str, limit: int = 30) -> list[ImageResult]:
        """Busca imágenes en Serper. Lanza excepción si el status no es 200."""
        if not self.api_key:
            raise RuntimeError("SERPER_API_KEY no configurada")

        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {"q": query, "num": limit}
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_ENDPOINT, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Serper respondió {resp.status}: {body[:200]}"
                    )
                data = await resp.json()

        return [self._to_result(item) for item in data.get("images", [])]

    @staticmethod
    def _to_result(item: dict) -> ImageResult:
        image_url = item.get("imageUrl") or ""
        page_url = item.get("link") or ""
        source = urlparse(page_url).netloc or ""
        return ImageResult(
            url=image_url,
            thumbnail=item.get("thumbnailUrl"),
            title=item.get("title") or "",
            source=source,
        )
