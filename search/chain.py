"""Prueba los proveedores en orden con circuit breaker simple."""
import logging
import time

from search.base import ImageResult, SearchProvider

logger = logging.getLogger(__name__)

_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 5 * 60


class SearchChain:
    def __init__(self, providers: list[SearchProvider]) -> None:
        self.providers = providers
        self._failures: dict[str, int] = {}
        self._skip_until: dict[str, float] = {}

    def _is_skipped(self, name: str) -> bool:
        until = self._skip_until.get(name, 0.0)
        return time.monotonic() < until

    def _record_failure(self, name: str) -> None:
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        if count >= _FAILURE_THRESHOLD:
            self._skip_until[name] = time.monotonic() + _COOLDOWN_SECONDS
            self._failures[name] = 0
            logger.warning(
                "Proveedor %s saltado por %ss tras %d fallos seguidos",
                name,
                _COOLDOWN_SECONDS,
                _FAILURE_THRESHOLD,
            )

    def _record_success(self, name: str) -> None:
        self._failures[name] = 0
        self._skip_until.pop(name, None)

    async def search(self, query: str, limit: int = 30) -> tuple[list[ImageResult], str]:
        """Devuelve (resultados, nombre del proveedor) o lanza excepción si todos fallan."""
        errors: list[str] = []
        for provider in self.providers:
            if self._is_skipped(provider.name):
                logger.info("Proveedor %s en cooldown, se omite", provider.name)
                continue
            try:
                results = await provider.search(query, limit=limit)
            except Exception as exc:
                logger.warning("Proveedor %s falló: %s", provider.name, exc)
                self._record_failure(provider.name)
                errors.append(f"{provider.name}: {exc}")
                continue
            if not results:
                logger.info("Proveedor %s devolvió 0 resultados", provider.name)
                self._record_failure(provider.name)
                errors.append(f"{provider.name}: sin resultados")
                continue
            self._record_success(provider.name)
            logger.info(
                "Proveedor %s respondió %d resultados para %r",
                provider.name,
                len(results),
                query,
            )
            return results, provider.name

        raise RuntimeError(
            "Ningún proveedor de imágenes respondió. " + "; ".join(errors)
        )
