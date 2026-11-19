"""Cog de descarga de video: /video (adjunto o link)."""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import settings
from media.downloader import (
    VideoError,
    VideoTooLargeError,
    VideoTooLongError,
    VideoUnavailableError,
    download_video,
    ffmpeg_available,
)

logger = logging.getLogger(__name__)


def _is_allowed(interaction: discord.Interaction) -> bool:
    """Permite solo en guilds autorizados; rechaza DMs."""
    if interaction.guild is None:
        return False
    if not settings.allowed_guild_ids:
        return True
    return interaction.guild.id in settings.allowed_guild_ids


class Video(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        if not ffmpeg_available():
            logger.warning(
                "ffmpeg no está disponible en el PATH; la descarga de video puede fallar."
            )

    @app_commands.command(name="video", description="Descarga un video")
    async def video(self, interaction: discord.Interaction, url: str) -> None:
        if not _is_allowed(interaction):
            await interaction.response.send_message(
                "Este bot solo funciona en servidores autorizados.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        guild_limit = interaction.guild.filesize_limit if interaction.guild else 0
        config_limit = settings.upload_limit_mb * 1024 * 1024
        max_bytes = min(guild_limit, config_limit) if guild_limit else config_limit

        path = None
        try:
            path = await download_video(url, max_bytes)
            await interaction.followup.send(file=discord.File(path))
        except VideoTooLongError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except VideoTooLargeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except VideoUnavailableError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except VideoError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception("Error inesperado en /video")
            await interaction.followup.send(
                "Ocurrió un error inesperado al procesar el video.",
                ephemeral=True,
            )
        finally:
            if path is not None:
                path.unlink(missing_ok=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Video(bot))
