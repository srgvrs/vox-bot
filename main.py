"""Punto de entrada del bot."""
import asyncio
import logging

import discord
from discord.ext import commands

from config import settings

COGS = ["cogs.imagenes", "cogs.video"]


class MediaBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self) -> None:
        for cog in COGS:
            await self.load_extension(cog)
        await self.tree.sync()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not settings.discord_token:
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")
    async with MediaBot() as bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
