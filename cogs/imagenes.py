"""Cog de búsqueda de imágenes: /img (embed paginado + botones)."""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from search.ddg import DuckDuckGoProvider

logger = logging.getLogger(__name__)


class Imagenes(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="img", description="Busca imágenes")
    async def img(self, interaction: discord.Interaction, consulta: str) -> None:
        await interaction.response.defer()

        try:
            results = await DuckDuckGoProvider().search(consulta, limit=30)
        except Exception:
            logger.exception("Fallo la búsqueda de imágenes para %r", consulta)
            await interaction.followup.send(
                "No pude completar la búsqueda de imágenes. Intenta de nuevo en un momento.",
                ephemeral=True,
            )
            return

        if not results:
            await interaction.followup.send(
                f"No encontré imágenes para '{consulta}'.",
                ephemeral=True,
            )
            return

        first = results[0]
        embed = discord.Embed(title=first.title or consulta)
        embed.set_image(url=first.url)
        if first.source:
            embed.set_footer(text=first.source)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Imagenes(bot))
