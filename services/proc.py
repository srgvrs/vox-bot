"""Ejecución de subprocesos sin bloquear el bucle, con prioridad baja.

- Solo asyncio.create_subprocess_exec (nunca subprocess.run/Popen).
- Prioridad baja: Windows BELOW_NORMAL + CREATE_NEW_PROCESS_GROUP;
  Linux nice(15) + start_new_session.
- kill_tree() mata el árbol completo (yt-dlp + ffmpeg hijos).
"""
import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Windows: BELOW_NORMAL_PRIORITY_CLASS | CREATE_NEW_PROCESS_GROUP
_WIN_FLAGS = 0x00004000 | 0x00000200
_IS_WINDOWS = sys.platform.startswith("win")


class ProcError(Exception):
    """Fallo al ejecutar un subproceso."""


class ProcTimeout(ProcError):
    """El subproceso superó el tiempo máximo."""


@dataclass
class ProcResult:
    """Resultado de un subproceso."""

    returncode: int
    stdout: str
    stderr: str


def _popen_kwargs(low_priority: bool) -> dict:
    """Argumentos de creación según plataforma y prioridad."""
    kwargs: dict = {}
    if _IS_WINDOWS:
        if low_priority:
            kwargs["creationflags"] = _WIN_FLAGS
    else:
        if low_priority:
            kwargs["preexec_fn"] = lambda: os.nice(15)
        kwargs["start_new_session"] = True
    return kwargs


async def kill_tree(pid: int) -> None:
    """Mata el árbol de procesos (yt-dlp + ffmpeg hijos)."""
    if pid <= 0:
        return
    try:
        if _IS_WINDOWS:
            proc = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception as exc:
        logger.warning("No se pudo matar el árbol del PID %d: %s", pid, exc)


async def run(
    cmd: list[str],
    *,
    timeout: float | None = None,
    low_priority: bool = True,
    cwd: str | None = None,
) -> ProcResult:
    """Ejecuta un comando y devuelve (rc, stdout, stderr).

    Al vencer el timeout o al cancelar, mata el árbol de procesos.
    """
    logger.debug("proc.run: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        **_popen_kwargs(low_priority),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Subproceso excedió timeout=%.0fs; matando árbol (pid=%d)",
            timeout or 0,
            proc.pid,
        )
        await kill_tree(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("El subproceso no terminó tras kill_tree")
        raise ProcTimeout(f"Timeout de {timeout}s en {cmd[0]}")
    except asyncio.CancelledError:
        logger.info("Subproceso cancelado; matando árbol (pid=%d)", proc.pid)
        await kill_tree(proc.pid)
        raise
    return ProcResult(
        returncode=proc.returncode or 0,
        stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
        stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
    )


async def run_ytdlp(
    args: list[str], *, timeout: float | None = None
) -> ProcResult:
    """Ejecuta yt-dlp con --concurrent-fragments 2 y prioridad baja."""
    cmd = ["yt-dlp", "--concurrent-fragments", "2", *args]
    return await run(cmd, timeout=timeout, low_priority=True)


async def run_ffmpeg(
    args: list[str], *, timeout: float | None = None
) -> ProcResult:
    """Ejecuta ffmpeg con -threads 1 -filter_threads 1 y prioridad baja."""
    cmd = ["ffmpeg", "-threads", "1", "-filter_threads", "1", *args]
    return await run(cmd, timeout=timeout, low_priority=True)


async def run_ffprobe(
    args: list[str], *, timeout: float | None = None
) -> ProcResult:
    """Ejecuta ffprobe con prioridad baja."""
    cmd = ["ffprobe", *args]
    return await run(cmd, timeout=timeout, low_priority=True)
