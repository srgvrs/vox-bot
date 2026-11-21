"""Caché en disco de videos ya descargados (Fase 3).

- Índice en memoria: clave (extractor, id, modo, nivel) -> entrada.
- Archivos en CACHE_DIR; se borran al arrancar (el índice no persiste).
- Tope de tamaño (VIDEO_CACHE_MB): al superarlo se expulsan los más antiguos.
- TTL de links de host: min(VIDEO_CACHE_MIN, expiración_host - 5 min).
- Tarea periódica cada 5 min que borra lo expirado.
- Contador de "en uso" para no expulsar archivos que se están enviando.
"""
import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

# Modos válidos de caché.
MODE_ATTACHMENT = "adjunto"
MODE_HOST = "host"

# Intervalo de la tarea de limpieza (segundos).
_CLEANUP_INTERVAL_S = 300
# Margen de seguridad sobre la expiración del host (segundos).
_HOST_EXPIRY_MARGIN_S = 300


@dataclass
class CacheEntry:
    """Entrada del índice de caché."""

    key: tuple[str, str, str, int]
    path: Path | None
    url: str | None
    created_at: float
    size: int
    attachment_limit: int
    expires_at: float | None
    title: str = ""
    height: int | None = None
    thumbnail: str | None = None
    extractor: str | None = None
    video_id: str | None = None
    notice: str | None = None
    # Contador de usos activos (protege de la expulsión).
    in_use: int = field(default=0)


class VideoCache:
    """Caché en disco con índice en memoria."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str, int], CacheEntry] = {}
        self._locks: dict[tuple[str, str, str, int], asyncio.Lock] = {}
        self._total_bytes: int = 0
        self._hits: int = 0
        self._misses: int = 0
        self._task: asyncio.Task | None = None
        self._stopped = False

    # --- Ciclo de vida ----------------------------------------------------

    def start(self) -> None:
        """Inicia la tarea periódica de limpieza."""
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "VideoCache: tarea de limpieza iniciada (cada %d s)",
            _CLEANUP_INTERVAL_S,
        )

    async def stop(self) -> None:
        """Detiene la tarea de limpieza."""
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("VideoCache: tarea de limpieza detenida")

    def wipe(self) -> None:
        """Borra el contenido de CACHE_DIR y el índice (al arrancar)."""
        cache_dir = settings.cache_dir
        if cache_dir.exists():
            for entry in cache_dir.iterdir():
                try:
                    if entry.is_file():
                        entry.unlink(missing_ok=True)
                    elif entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                except OSError as exc:
                    logger.warning("No se pudo borrar %s: %s", entry, exc)
        else:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("No se pudo crear CACHE_DIR: %s", exc)
        self._entries.clear()
        self._locks.clear()
        self._total_bytes = 0
        self._hits = 0
        self._misses = 0
        logger.info("VideoCache: caché vaciada al arrancar")

    # --- Estadísticas -----------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Devuelve estadísticas para /estado."""
        return {
            "entries": len(self._entries),
            "bytes": self._total_bytes,
            "hits": self._hits,
            "misses": self._misses,
        }

    # --- Locks por clave --------------------------------------------------

    def lock_for(self, key: tuple[str, str, str, int]) -> asyncio.Lock:
        """Devuelve (creando si hace falta) el lock de una clave."""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def release_lock(self, key: tuple[str, str, str, int]) -> None:
        """Elimina el lock de una clave (llamar en finally)."""
        self._locks.pop(key, None)

    # --- Consulta / guardado ---------------------------------------------

    def get(
        self,
        key: tuple[str, str, str, int],
        current_limit: int,
    ) -> CacheEntry | None:
        """Devuelve la entrada si es válida para el límite actual.

        - Adjunto: solo cuenta si sus bytes caben en el 97% del límite actual.
        - Host: solo cuenta si el link no ha expirado.
        - Si el archivo ya no existe en disco, se trata como fallo.
        """
        entry = self._entries.get(key)
        if entry is None:
            self._misses += 1
            logger.info("VideoCache MISS (sin entrada): %s", key)
            return None
        if entry.expires_at is not None and time.time() >= entry.expires_at:
            self._misses += 1
            logger.info("VideoCache MISS (expirado): %s", key)
            self._remove_entry(entry)
            return None
        if entry.path is not None:
            if not entry.path.exists():
                self._misses += 1
                logger.info("VideoCache MISS (archivo ausente): %s", key)
                self._remove_entry(entry)
                return None
            safe_limit = int(current_limit * 0.97)
            if entry.size > safe_limit:
                self._misses += 1
                logger.info(
                    "VideoCache MISS (no cabe: %d > %d): %s",
                    entry.size,
                    safe_limit,
                    key,
                )
                return None
        self._hits += 1
        logger.info("VideoCache HIT: %s", key)
        return entry

    def put(self, entry: CacheEntry) -> None:
        """Registra una entrada nueva y aplica el tope de tamaño."""
        old = self._entries.get(entry.key)
        if old is not None:
            self._remove_entry(old)
        self._entries[entry.key] = entry
        if entry.path is not None:
            self._total_bytes += entry.size
        logger.info(
            "VideoCache PUT: %s (%d bytes, total=%d)",
            entry.key,
            entry.size,
            self._total_bytes,
        )
        self._enforce_size_limit()

    def acquire(self, entry: CacheEntry) -> None:
        """Marca la entrada como en uso (protege de la expulsión)."""
        entry.in_use += 1

    def release(self, entry: CacheEntry) -> None:
        """Desmarca la entrada como en uso."""
        if entry.in_use > 0:
            entry.in_use -= 1

    # --- Internos ---------------------------------------------------------

    def _remove_entry(self, entry: CacheEntry) -> None:
        self._entries.pop(entry.key, None)
        if entry.path is not None:
            self._total_bytes = max(0, self._total_bytes - entry.size)
            try:
                entry.path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("No se pudo borrar %s: %s", entry.path, exc)

    def _enforce_size_limit(self) -> None:
        """Expulsa los más antiguos hasta respetar VIDEO_CACHE_MB."""
        max_bytes = settings.video_cache_mb * 1024 * 1024
        if max_bytes <= 0 or self._total_bytes <= max_bytes:
            return
        # Ordenar por antigüedad (más antiguos primero), saltando los en uso.
        candidates = sorted(
            (e for e in self._entries.values() if e.path is not None),
            key=lambda e: e.created_at,
        )
        for entry in candidates:
            if self._total_bytes <= max_bytes:
                break
            if entry.in_use > 0:
                continue
            logger.info(
                "VideoCache: expulsando %s (%d bytes) por tope de tamaño",
                entry.key,
                entry.size,
            )
            self._remove_entry(entry)

    async def _cleanup_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(_CLEANUP_INTERVAL_S)
                if self._stopped:
                    break
                self._cleanup_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en la limpieza de VideoCache")

    def _cleanup_expired(self) -> None:
        """Borra entradas expiradas (links de host) y archivos huérfanos."""
        now = time.time()
        expired = [
            e
            for e in self._entries.values()
            if e.expires_at is not None and now >= e.expires_at
        ]
        for entry in expired:
            if entry.in_use > 0:
                continue
            logger.info("VideoCache: expirando %s", entry.key)
            self._remove_entry(entry)
        # Archivos huérfanos en CACHE_DIR (no referenciados por el índice).
        cache_dir = settings.cache_dir
        if not cache_dir.exists():
            return
        known = {e.path for e in self._entries.values() if e.path is not None}
        for path in cache_dir.iterdir():
            if not path.is_file():
                continue
            if path in known:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("No se pudo borrar huérfano %s: %s", path, exc)


def host_ttl_seconds(expiry_hours: int) -> int:
    """TTL efectivo para links de host: min(VIDEO_CACHE_MIN, expiración - 5 min)."""
    cache_min_s = settings.video_cache_min * 60
    host_s = max(0, expiry_hours * 3600 - _HOST_EXPIRY_MARGIN_S)
    return min(cache_min_s, host_s)


def new_cache_path(suffix: str = ".mp4") -> Path:
    """Genera una ruta única dentro de CACHE_DIR."""
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    return settings.cache_dir / f"{uuid.uuid4().hex}{suffix}"


# Instancia compartida.
video_cache = VideoCache()
