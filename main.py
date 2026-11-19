"""Punto de entrada del bot."""
import asyncio
import logging
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord.ext import commands

from config import settings

COGS = ["cogs.imagenes", "cogs.video", "cogs.enlaces", "cogs.admin"]

_SHUTDOWN_TIMEOUT_SECONDS = 30


class MediaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        if settings.auto_link_enabled:
            intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        for cog in COGS:
            await self.load_extension(cog)
        await _log_versions()
        self.tree.copy_global_to(guild=discord.Object(id=0)) if False else None
        if settings.allowed_guild_ids:
            for guild_id in settings.allowed_guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                try:
                    await self.tree.sync(guild=guild)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logging.error(
                        "No se pudieron sincronizar los comandos en el servidor %s: %s. "
                        "Causa probable: ID de servidor incorrecto o el bot no fue invitado "
                        "con el scope 'applications.commands'. Se continúa con el siguiente servidor.",
                        guild_id,
                        exc,
                    )
                    continue
                logging.info("Comandos sincronizados en guild %s", guild_id)
        else:
            await self.tree.sync()
            logging.info("Comandos sincronizados globalmente (sin ALLOWED_GUILD_IDS)")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if settings.allowed_guild_ids and guild.id not in settings.allowed_guild_ids:
            logging.warning(
                "Guild no permitido %s (%s); saliendo.", guild.id, guild.name
            )
            await guild.leave()
        else:
            logging.info("Unido a guild permitido %s (%s)", guild.id, guild.name)


def _setup_logging() -> None:
    """Configura logging a consola y a logs/voxbot.log con rotación.

    No duplica handlers si ya existen (por ejemplo, al recargar el módulo).
    """
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "voxbot.log"
    if not any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", "") == str(log_path.resolve())
        for h in root.handlers
    ):
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def _prepare_path() -> None:
    """Prepara el PATH: antepone ./bin y, si falta ffmpeg, lo copia desde imageio_ffmpeg.

    No debe romper el arranque: cualquier fallo se registra como WARNING.
    """
    try:
        bin_dir = Path(__file__).resolve().parent / "bin"
        if bin_dir.is_dir():
            current = os.environ.get("PATH", "")
            bin_str = str(bin_dir)
            parts = current.split(os.pathsep) if current else []
            if bin_str not in parts:
                os.environ["PATH"] = bin_str + (os.pathsep + current if current else "")
                logging.info("PATH: se antepuso %s", bin_str)
    except Exception as exc:
        logging.warning("No se pudo anteponer ./bin al PATH: %s", exc)

    if shutil.which("ffmpeg"):
        return

    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if not exe:
            logging.warning("imageio_ffmpeg no devolvió una ruta de ffmpeg.")
            return
        bin_dir = Path(__file__).resolve().parent / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        target_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        target = bin_dir / target_name
        if not target.exists():
            shutil.copy2(exe, target)
            try:
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass
            logging.info("ffmpeg copiado desde imageio_ffmpeg a %s", target)
        current = os.environ.get("PATH", "")
        bin_str = str(bin_dir)
        parts = current.split(os.pathsep) if current else []
        if bin_str not in parts:
            os.environ["PATH"] = bin_str + (os.pathsep + current if current else "")
    except Exception as exc:
        logging.warning(
            "No se pudo preparar ffmpeg desde imageio_ffmpeg: %s", exc
        )


def _cleanup_temp_dir() -> None:
    """Borra archivos de TEMP_DIR con más de 1 hora de antigüedad.

    Omite `.gitkeep` para conservar la carpeta en el repositorio.
    """
    temp_dir = settings.temp_dir
    if not temp_dir.exists():
        return
    cutoff = time.time() - 3600
    removed = 0
    for entry in temp_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.name == ".gitkeep":
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logging.warning("No se pudo borrar %s: %s", entry, exc)
    if removed:
        logging.info("Limpieza de TEMP_DIR: %d archivos eliminados", removed)


def _first_line(cmd: list[str]) -> str | None:
    """Ejecuta un comando y devuelve su primera línea de salida (o None)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    out = (proc.stdout or proc.stderr or "").strip().splitlines()
    return out[0] if out else None


async def _log_versions() -> None:
    """Registra versiones de dependencias y herramientas externas al iniciar."""
    try:
        import yt_dlp

        ytdlp_version = getattr(yt_dlp.version, "__version__", "desconocida")
    except Exception:
        ytdlp_version = "no disponible"

    logging.info(
        "Versiones: yt-dlp=%s discord.py=%s Python=%s (%s)",
        ytdlp_version,
        discord.__version__,
        platform.python_version(),
        platform.platform(),
    )

    ffmpeg_line = await asyncio.to_thread(_first_line, ["ffmpeg", "-version"])
    if ffmpeg_line:
        logging.info("ffmpeg: %s", ffmpeg_line)
    else:
        logging.warning(
            "ffmpeg no está disponible en el PATH; la descarga de video puede fallar."
        )

    deno_line = await asyncio.to_thread(_first_line, ["deno", "--version"])
    if deno_line:
        logging.info("deno: %s", deno_line)
    else:
        logging.warning(
            "deno no está disponible en el PATH; yt-dlp puede fallar en algunos sitios."
        )


async def _wait_active_downloads(timeout: float) -> None:
    """Espera a que el semáforo de descargas quede libre (hasta timeout)."""
    try:
        from media.downloader import _semaphore

        async def _acquire_all() -> None:
            # Adquiere todos los slots para asegurar que no hay descargas activas.
            acquired = 0
            try:
                while acquired < _semaphore._value + acquired:
                    await _semaphore.acquire()
                    acquired += 1
            finally:
                for _ in range(acquired):
                    _semaphore.release()

        await asyncio.wait_for(_acquire_all(), timeout=timeout)
    except asyncio.TimeoutError:
        logging.warning(
            "Cierre: se agotó el tiempo de espera (%ss) para descargas activas.",
            timeout,
        )
    except Exception as exc:
        logging.debug("Cierre: no se pudo esperar descargas activas: %s", exc)


async def _shutdown(bot: MediaBot) -> None:
    """Cierre limpio: espera descargas, limpia TEMP_DIR y cierra el bot."""
    logging.info("Cierre solicitado; esperando descargas activas...")
    await _wait_active_downloads(_SHUTDOWN_TIMEOUT_SECONDS)
    _cleanup_temp_dir()
    try:
        await bot.close()
    except Exception as exc:
        logging.debug("Error al cerrar el bot: %s", exc)


async def main() -> None:
    _setup_logging()
    _prepare_path()
    try:
        os.makedirs(settings.temp_dir, exist_ok=True)
    except OSError as exc:
        logging.warning(
            "No se pudo crear TEMP_DIR (%s): %s", settings.temp_dir, exc
        )
    _cleanup_temp_dir()
    if not settings.discord_token:
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")

    bot = MediaBot()
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _request_shutdown(signame: str) -> None:
        if shutdown_event.is_set():
            return
        logging.info("Señal %s recibida; iniciando cierre.", signame)
        shutdown_event.set()

    for signame in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_shutdown, signame)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler no está soportado; usar signal.signal.
            try:
                signal.signal(
                    sig, lambda *_, s=signame: loop.call_soon_threadsafe(_request_shutdown, s)
                )
            except (ValueError, OSError):
                pass

    try:
        async with bot:
            start_task = asyncio.create_task(bot.start(settings.discord_token))
            shutdown_task = asyncio.create_task(shutdown_event.wait())
            done, pending = await asyncio.wait(
                {start_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if shutdown_task in done:
                await _shutdown(bot)
            else:
                # bot.start terminó por sí solo (error o cierre interno).
                exc = start_task.exception()
                if exc is not None:
                    raise exc
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt recibido; cerrando.")
        await _shutdown(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Evita traceback al cerrar con Ctrl+C.
        pass
