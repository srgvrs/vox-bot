"""Cog de descarga de video: /video (adjunto o link)."""
import discord
from discord import app_commands
from discord.ext import commands

from config import settings


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

    @app_commands.command(name="video", description="Descarga un video")
    async def video(self, interaction: discord.Interaction, url: str) -> None:
        if not _is_allowed(interaction):
            await interaction.response.send_message(
                "Este bot solo funciona en servidores autorizados.",
                ephemeral=True,
            )
            return
        # TODO (paso 5): defer(), descargar con media.downloader y responder
        await interaction.response.send_message(f"TODO: descargar {url}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Video(bot))
