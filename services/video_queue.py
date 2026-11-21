"""Cola con prioridad, workers y timeouts para trabajos de video.

- asyncio.PriorityQueue con (prioridad, contador, trabajo).
- Un solo trabajo (en cola o activo) por usuario.
- Timeout por trabajo: VIDEO_TIMEOUT_S / VIDEO_TIMEOUT_LARGO_S.
- Al vencer: mata el árbol de procesos, limpia temp y avisa al usuario.
- Cierre limpio: deja de aceptar, espera activos hasta 30 s, mata el resto.
"""
import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import settings
from services.proc import kill_tree

logger = logging.getLogger(__name__)

PRIORITY_NORMAL = 0
PRIORITY_LARGO = 10

_SHUTDOWN_WAIT_S = 30


class QueueFull(Exception):
    """La cola está llena."""


class UserBusy(Exception):
    """El usuario ya tiene un trabajo en cola o activo."""


class JobTimeout(Exception):
    """El trabajo superó su tiempo máximo."""


class QueueClosed(Exception):
    """La cola está cerrada (apagado en curso)."""


@dataclass
class Job:
    """Trabajo encolado."""

    user_id: int
    channel_id: int | None
    url: str
    priority: int
    timeout_s: float
    runner: Callable[["Job"], Awaitable[Any]]
    on_timeout: Callable[["Job"], Awaitable[None]] | None = None
    on_queued: Callable[["Job", int], Awaitable[None]] | None = None
    on_started: Callable[["Job"], Awaitable[None]] | None = None
    responder: Any = None
    temp_dir: Path | None = None
    future: asyncio.Future = field(default_factory=asyncio.Future)
    enqueued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    pid: int | None = None


class VideoQueue:
    """Cola con prioridad y workers."""

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._counter = itertools.count()
        self._workers: list[asyncio.Task] = []
        self._active: dict[int, Job] = {}
        self._queued_users: set[int] = set()
        self._closed = False
        self._started = False

    # --- Ciclo de vida ----------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._closed = False
        n = max(1, settings.video_workers)
        for i in range(n):
            self._workers.append(asyncio.create_task(self._worker(i)))
        logger.info("VideoQueue: %d worker(s) iniciados", n)

    async def stop(self) -> None:
        """Cierre limpio: deja de aceptar, espera activos, mata el resto."""
        if not self._started:
            return
        self._closed = True
        logger.info(
            "VideoQueue: cierre solicitado (activos=%d, en cola=%d)",
            len(self._active),
            self._queue.qsize(),
        )
        # Avisar a los que quedaron en cola.
        while not self._queue.empty():
            try:
                _, _, job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queued_users.discard(job.user_id)
            if not job.future.done():
                job.future.set_exception(QueueClosed("El bot se está reiniciando"))
        # Esperar activos hasta 30 s.
        deadline = time.monotonic() + _SHUTDOWN_WAIT_S
        while self._active and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        # Matar los que sigan activos.
        for job in list(self._active.values()):
            if job.pid:
                await kill_tree(job.pid)
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()
        self._started = False
        logger.info("VideoQueue: cerrada")

    # --- API pública ------------------------------------------------------

    def queue_size(self) -> int:
        return self._queue.qsize()

    def active_count(self) -> int:
        return len(self._active)

    def is_user_busy(self, user_id: int) -> bool:
        return user_id in self._queued_users or user_id in self._active

    async def submit(self, job: Job) -> Any:
        """Encola el trabajo y espera su resultado."""
        if self._closed:
            raise QueueClosed("El bot se está reiniciando")
        if self.is_user_busy(job.user_id):
            raise UserBusy("Ya tienes un video en proceso.")
        if self._queue.qsize() >= settings.video_max_cola:
            raise QueueFull("Ocupado, intenta en un momento")
        self._queued_users.add(job.user_id)
        await self._queue.put((job.priority, next(self._counter), job))
        logger.info(
            "VideoQueue: encolado usuario=%s prioridad=%d cola=%d",
            job.user_id,
            job.priority,
            self._queue.qsize(),
        )
        if job.on_queued is not None:
            try:
                await job.on_queued(job, self._queue.qsize())
            except Exception:
                logger.exception("on_queued falló")
        return await job.future

    # --- Worker -----------------------------------------------------------

    async def _worker(self, idx: int) -> None:
        while True:
            try:
                _, _, job = await self._queue.get()
            except asyncio.CancelledError:
                return
            self._queued_users.discard(job.user_id)
            if self._closed:
                if not job.future.done():
                    job.future.set_exception(
                        QueueClosed("El bot se está reiniciando")
                    )
                continue
            self._active[job.user_id] = job
            job.started_at = time.monotonic()
            logger.info(
                "VideoQueue[%d]: inicio usuario=%s url=%s timeout=%.0fs",
                idx,
                job.user_id,
                job.url,
                job.timeout_s,
            )
            if job.on_started is not None:
                try:
                    await job.on_started(job)
                except Exception:
                    logger.exception("on_started falló")
            close_reason = "ok"
            try:
                result = await asyncio.wait_for(
                    job.runner(job), timeout=job.timeout_s
                )
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.TimeoutError:
                close_reason = "timeout"
                logger.warning(
                    "VideoQueue[%d]: TIMEOUT usuario=%s url=%s (%.0fs)",
                    idx,
                    job.user_id,
                    job.url,
                    job.timeout_s,
                )
                if job.pid:
                    await kill_tree(job.pid)
                self._cleanup_temp(job)
                if job.on_timeout is not None:
                    try:
                        await job.on_timeout(job)
                    except Exception:
                        logger.exception("on_timeout falló")
                if not job.future.done():
                    job.future.set_exception(
                        JobTimeout(
                            "El video tardó demasiado y se canceló."
                        )
                    )
            except asyncio.CancelledError:
                close_reason = "cancelado"
                if job.pid:
                    await kill_tree(job.pid)
                self._cleanup_temp(job)
                if not job.future.done():
                    job.future.set_exception(
                        QueueClosed("El bot se está reiniciando")
                    )
                raise
            except Exception as exc:
                close_reason = "error"
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                if job.responder is not None:
                    try:
                        await job.responder.close(close_reason)
                    except Exception:
                        logger.exception("close del responder falló")
                self._active.pop(job.user_id, None)
                logger.info(
                    "VideoQueue[%d]: fin usuario=%s (%.1fs)",
                    idx,
                    job.user_id,
                    time.monotonic() - (job.started_at or time.monotonic()),
                )

    @staticmethod
    def _cleanup_temp(job: Job) -> None:
        if job.temp_dir is None or not job.temp_dir.exists():
            return
        try:
            for entry in job.temp_dir.iterdir():
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass
            job.temp_dir.rmdir()
        except OSError as exc:
            logger.warning("No se pudo limpiar %s: %s", job.temp_dir, exc)


# Instancia compartida.
video_queue = VideoQueue()
