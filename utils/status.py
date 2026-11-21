"""Mensaje de estado animado para interacciones de Discord."""
import asyncio
import logging
import time
from dataclasses import dataclass

import discord

from config import settings

logger = logging.getLogger(__name__)

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Intervalo mínimo entre ediciones en modo detalle (segundos).
_DETAIL_MIN_INTERVAL = 1.5

# Iconos por estado de paso.
_STEP_ICONS = {
    "ok": "✅",
    "en_curso": "⏳",
    "error": "❌",
}


@dataclass
class _Step:
    """Paso del mensaje de estado detallado."""

    texto: str
    estado: str  # "ok" | "en_curso" | "error"


class StatusMessage:
    """Edita el mensaje original de una interacción con un spinner y tiempo.

    - Sin STATUS_EMOJI: spinner braille + "{texto} · {n} s", cada 3 s.
    - Con STATUS_EMOJI: prefijo fijo + "{texto} · {n} s", cada 5 s.
    - Con STATUS_DETALLE=1: lista de pasos (✅/⏳/❌) editada en el mismo
      mensaje, con un mínimo de 1.5 s entre ediciones.
    """

    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self._texto: str = ""
        self._start = time.monotonic()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._emoji = (settings.status_emoji or "").strip()
        self._detalle = bool(settings.status_detalle)
        self._steps: list[_Step] = []
        self._last_rendered: str | None = None
        if settings.low_cpu_mode:
            # LOW_CPU_MODE: animación muy lenta para reducir ediciones/CPU.
            self._interval = 30.0
        elif self._detalle:
            self._interval = _DETAIL_MIN_INTERVAL
        else:
            self._interval = 5.0 if self._emoji else 3.0
        self._frame_idx = 0

    def _render(self) -> str:
        if self._detalle:
            return self._render_steps()
        elapsed = int(time.monotonic() - self._start)
        if self._emoji:
            prefix = self._emoji
        else:
            prefix = _FRAMES[self._frame_idx % len(_FRAMES)]
            self._frame_idx += 1
        return f"{prefix} {self._texto} · {elapsed} s"

    def _render_steps(self) -> str:
        """Renderiza la lista de pasos como texto plano."""
        if not self._steps:
            return self._texto or "Procesando..."
        lines: list[str] = []
        for step in self._steps:
            icon = _STEP_ICONS.get(step.estado, "⏳")
            lines.append(f"{icon} {step.texto}")
        return "\n".join(lines)

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
                content = self._render()
                if self._detalle and content == self._last_rendered:
                    continue
                if not await self._edit(content):
                    self._stopped = True
                    break
                self._last_rendered = content
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en la animación del estado")

    async def set(self, texto: str) -> None:
        """Cambia el texto; edita de inmediato y (re)inicia la animación.

        En modo detalle, el texto se muestra como un único paso "en_curso".
        """
        if self._stopped:
            return
        if self._detalle:
            self._steps = [_Step(texto=texto, estado="en_curso")]
            await self._flush_steps()
            return
        changed = texto != self._texto
        self._texto = texto
        if changed:
            if not await self._edit(self._render()):
                self._stopped = True
                return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def set_steps(self, steps: list[tuple[str, str]]) -> None:
        """Fija la lista de pasos.

        Cada paso es (texto, estado) con estado ∈ {"ok", "en_curso", "error"}.
        En modo spinner simple, se usa el texto del último paso como mensaje.
        """
        if self._stopped:
            return
        if not self._detalle:
            if steps:
                await self.set(steps[-1][0])
            return
        self._steps = [
            _Step(texto=texto, estado=estado) for texto, estado in steps
        ]
        await self._flush_steps()

    async def update_step(
        self, index: int, texto: str, estado: str
    ) -> None:
        """Actualiza (o agrega) un paso por índice."""
        if self._stopped:
            return
        if not self._detalle:
            await self.set(texto)
            return
        while len(self._steps) <= index:
            self._steps.append(_Step(texto="", estado="en_curso"))
        self._steps[index] = _Step(texto=texto, estado=estado)
        await self._flush_steps()

    async def _flush_steps(self) -> None:
        """Edita el mensaje con los pasos actuales y (re)inicia el bucle."""
        content = self._render_steps()
        if content != self._last_rendered:
            if not await self._edit(content):
                self._stopped = True
                return
            self._last_rendered = content
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
