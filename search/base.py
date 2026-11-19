"""Interfaz común de los proveedores de búsqueda de imágenes."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ImageResult:
    url: str
    thumbnail: str | None
    title: str
    source: str  # dominio o página de origen


class SearchProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def search(self, query: str, limit: int = 30) -> list[ImageResult]:
        """Devuelve resultados o lanza excepción si el proveedor falla."""
