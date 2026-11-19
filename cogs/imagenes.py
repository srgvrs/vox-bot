"""Cog de búsqueda de imágenes: /img (embed paginado + botones)."""
import discord
from discord import app_commands
from discord.ext import commands


class Imagenes(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="img", description="Busca imágenes")
    async def img(self, interaction: discord.Interaction, consulta: str) -> None:
        # TODO (paso 2): buscar con search.chain y mostrar el primer resultado
        await interaction.response.send_message(f"TODO: buscar '{consulta}'")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Imagenes(bot))
