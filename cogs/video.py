"""Cog de descarga de video: /video (adjunto o link)."""
import discord
from discord import app_commands
from discord.ext import commands


class Video(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="video", description="Descarga un video")
    async def video(self, interaction: discord.Interaction, url: str) -> None:
        # TODO (paso 5): defer(), descargar con media.downloader y responder
        await interaction.response.send_message(f"TODO: descargar {url}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Video(bot))
