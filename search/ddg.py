"""Proveedor DuckDuckGo (librería ddgs, gratis y sin clave)."""
import asyncio
import logging
from urllib.parse import urlparse

from ddgs import DDGS

from search.base import ImageResult, SearchProvider

logger = logging.getLogger(__name__)


class DuckDuckGoProvider(SearchProvider):
    name = "duckduckgo"

    async def search(self, query: str, limit: int = 30) -> list[ImageResult]:
        """Busca imágenes en DuckDuckGo. Lanza excepción si el proveedor falla."""
        raw = await asyncio.to_thread(self._search_sync, query, limit)
        return [self._to_result(item) for item in raw]

    @staticmethod
    def _search_sync(query: str, limit: int) -> list[dict]:
        # ddgs es bloqueante: se ejecuta en un hilo aparte.
        return DDGS().images(
            query=query,
            safesearch="moderate",
            max_results=limit,
            backend="auto",
        )

    @staticmethod
    def _to_result(item: dict) -> ImageResult:
        image_url = item.get("image") or ""
        page_url = item.get("url") or ""
        source = urlparse(page_url).netloc or item.get("source") or ""
        return ImageResult(
            url=image_url,
            thumbnail=item.get("thumbnail"),
            title=item.get("title") or "",
            source=source,
        )
