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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not settings.discord_token:
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")
    async with MediaBot() as bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
