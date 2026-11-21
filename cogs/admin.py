"""Cog de administración: /estado (solo ADMIN_USER_IDS)."""
import logging
import platform
import shutil
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from config import settings
from utils.errors import count_last_hour

logger = logging.getLogger(__name__)

_START_TIME = time.monotonic()


def _is_admin(user_id: int) -> bool:
    allowed = settings.admin_user_ids
    logger.debug(
        "Comprobando admin: user_id=%s, admin_user_ids=%s",
        user_id,
        allowed,
    )
    return user_id in allowed


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _format_uptime(seconds: float) -> str:
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="estado", description="Estado del bot (solo admins)")
    async def estado(self, interaction: discord.Interaction) -> None:
        logger.info(
            "Comando /estado invocado por %s en guild %s",
            interaction.user.id,
            interaction.guild_id,
        )
        try:
            if not _is_admin(interaction.user.id):
                logger.info(
                    "/estado rechazado: user_id=%s no está en ADMIN_USER_IDS=%s",
                    interaction.user.id,
                    list(settings.admin_user_ids),
                )
                await interaction.response.send_message(
                    "No tienes permiso para usar este comando.", ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True)
        except Exception:
            logger.exception("Error en /estado antes de responder")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Error interno al procesar el comando.", ephemeral=True
                    )
            except Exception:
                logger.exception("No se pudo enviar el mensaje de error")
            return

        try:
            import yt_dlp

            ytdlp_version = getattr(yt_dlp.version, "__version__", "desconocida")
        except Exception:
            ytdlp_version = "no disponible"

        uptime = _format_uptime(time.monotonic() - _START_TIME)
        latency_ms = int(self.bot.latency * 1000) if self.bot.latency else -1

        try:
            from services.video_queue import video_queue

            active = video_queue.active_count()
            queued = video_queue.queue_size()
        except Exception:
            active = -1
            queued = -1

        try:
            from services.video_cache import video_cache

            cache_stats = video_cache.stats()
        except Exception:
            cache_stats = {"entries": -1, "bytes": -1, "hits": -1, "misses": -1}

        errors_last_hour = count_last_hour()

        temp_dir = settings.temp_dir
        temp_size = _dir_size(temp_dir)
        try:
            disk = shutil.disk_usage(temp_dir if temp_dir.exists() else Path("."))
            disk_free = _human_bytes(disk.free)
            disk_total = _human_bytes(disk.total)
        except OSError:
            disk_free = "?"
            disk_total = "?"

        embed = discord.Embed(title="Estado del bot", color=discord.Color.blurple())
        embed.add_field(
            name="Versiones",
            value=(
                f"yt-dlp: `{ytdlp_version}`\n"
                f"discord.py: `{discord.__version__}`\n"
                f"Python: `{platform.python_version()}`"
            ),
            inline=False,
        )
        embed.add_field(name="Uptime", value=uptime, inline=True)
        embed.add_field(
            name="Latencia",
            value=f"{latency_ms} ms" if latency_ms >= 0 else "desconocida",
            inline=True,
        )
        embed.add_field(
            name="Descargas en curso",
            value=str(active) if active >= 0 else "desconocido",
            inline=True,
        )
        embed.add_field(
            name="En cola",
            value=str(queued) if queued >= 0 else "desconocido",
            inline=True,
        )
        embed.add_field(
            name="Errores (última hora)", value=str(errors_last_hour), inline=True
        )
        embed.add_field(
            name="Caché",
            value=(
                f"{_human_bytes(cache_stats['bytes'])} · "
                f"{cache_stats['entries']} entradas\n"
                f"Aciertos: {cache_stats['hits']} · "
                f"Fallos: {cache_stats['misses']}"
            ),
            inline=False,
        )
        embed.add_field(
            name="TEMP_DIR",
            value=f"{_human_bytes(temp_size)} en `{temp_dir}`",
            inline=False,
        )
        embed.add_field(
            name="Disco",
            value=f"{disk_free} libres de {disk_total}",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
