"""Mensaje de estado animado para interacciones de Discord."""
import asyncio
import logging
import time

import discord

from config import settings

logger = logging.getLogger(__name__)

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StatusMessage:
    """Edita el mensaje original de una interacción con un spinner y tiempo.

    - Sin STATUS_EMOJI: spinner braille + "{texto} · {n} s", cada 3 s.
    - Con STATUS_EMOJI: prefijo fijo + "{texto} · {n} s", cada 5 s.
    """

    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self._texto: str = ""
        self._start = time.monotonic()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._emoji = (settings.status_emoji or "").strip()
        if settings.low_cpu_mode:
            # LOW_CPU_MODE: animación muy lenta para reducir ediciones/CPU.
            self._interval = 30.0
        else:
            self._interval = 5.0 if self._emoji else 3.0
        self._frame_idx = 0

    def _render(self) -> str:
        elapsed = int(time.monotonic() - self._start)
        if self._emoji:
            prefix = self._emoji
        else:
            prefix = _FRAMES[self._frame_idx % len(_FRAMES)]
            self._frame_idx += 1
        return f"{prefix} {self._texto} · {elapsed} s"

    async def _edit(self, content: str) -> bool:
        """Edita el mensaje; devuelve False si ya no existe."""
        try:
            await self.interaction.edit_original_response(content=content)
            return True
        except discord.NotFound:
            logger.debug("Mensaje de estado ya no existe; se detiene la animación.")
            return False
        except discord.HTTPException as exc:
            logger.debug("No se pudo editar el estado: %s", exc)
            return True

    async def _loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self._interval)
                if self._stopped:
                    break
                if not await self._edit(self._render()):
                    self._stopped = True
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en la animación del estado")

    async def set(self, texto: str) -> None:
        """Cambia el texto; edita de inmediato y (re)inicia la animación."""
        if self._stopped:
            return
        changed = texto != self._texto
        self._texto = texto
        if changed:
            if not await self._edit(self._render()):
                self._stopped = True
                return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Detiene la animación y espera a que la tarea termine."""
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
