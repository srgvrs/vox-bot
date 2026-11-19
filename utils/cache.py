"""Caché en memoria con TTL para resultados de búsqueda."""
import time
from typing import Generic, TypeVar

T = TypeVar("T")

_DEFAULT_TTL_SECONDS = 10 * 60


def normalize_query(query: str) -> str:
    """Normaliza la consulta: minúsculas y espacios colapsados."""
    return " ".join(query.lower().split())


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._store: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: T) -> None:
        self._store[key] = (time.monotonic() + self.ttl_seconds, value)
