"""Punto de entrada del bot."""
import asyncio
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord.ext import commands

from config import settings

COGS = ["cogs.imagenes", "cogs.video", "cogs.enlaces"]


class MediaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        if settings.auto_link_enabled:
            intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        for cog in COGS:
            await self.load_extension(cog)
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
    """Configura logging a consola y a logs/bot.log con rotación."""
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        logs_dir / "bot.log",
        maxBytes=1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _cleanup_temp_dir() -> None:
    """Borra archivos de TEMP_DIR con más de 1 hora de antigüedad."""
    temp_dir = settings.temp_dir
    if not temp_dir.exists():
        return
    cutoff = time.time() - 3600
    removed = 0
    for entry in temp_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logging.warning("No se pudo borrar %s: %s", entry, exc)
    if removed:
        logging.info("Limpieza de TEMP_DIR: %d archivos eliminados", removed)


async def main() -> None:
    _setup_logging()
    _cleanup_temp_dir()
    if not settings.discord_token:
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")
    async with MediaBot() as bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
