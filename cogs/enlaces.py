"""Cog de enlaces: modo mención y modo automático para /video."""
import logging
import re

import discord
from discord.ext import commands

from cogs.video import MessageResponder, check_cooldown, run_video_flow
from config import settings
from media.downloader import _host_allowed

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _extract_video_url(text: str) -> str | None:
    """Devuelve la primera URL con dominio permitido, o None."""
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}\"'")
        try:
            from urllib.parse import urlparse

            host = urlparse(url).hostname
        except ValueError:
            continue
        if host and _host_allowed(host):
            return url
    return None


def _guild_allowed(guild: discord.Guild | None) -> bool:
    if guild is None:
        return False
    if not settings.allowed_guild_ids:
        return True
    return guild.id in settings.allowed_guild_ids


class Enlaces(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not _guild_allowed(message.guild):
            return

        mentioned = self.bot.user is not None and self.bot.user in message.mentions
        auto_channel = (
            settings.auto_link_enabled
            and message.channel.id in settings.auto_link_channel_ids
        )
        if not mentioned and not auto_channel:
            return

        url = _extract_video_url(message.content)
        if url is None:
            if mentioned:
                await message.reply(
                    "Mencióname con un enlace de YouTube o Instagram",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return

        origin = "mencion" if mentioned else "auto"
        remaining = check_cooldown(message.author.id)
        if remaining > 0:
            await message.reply(
                f"Espera {remaining} s para usar /video de nuevo",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        guild_limit = message.guild.filesize_limit if message.guild else 0
        env_limit_mb = settings.discord_upload_limit_mb
        # DISCORD_UPLOAD_LIMIT_MB definido en .env => override (gana siempre).
        # No definido => se usa el límite del servidor (guild.filesize_limit).
        if env_limit_mb is not None:
            max_bytes = env_limit_mb * 1024 * 1024
            origen_limite = ".env"
        elif guild_limit:
            max_bytes = guild_limit
            origen_limite = "servidor"
        else:
            max_bytes = settings.upload_limit_mb * 1024 * 1024
            origen_limite = "default"
        logger.info(
            "Límite de adjunto (enlaces): guild.filesize_limit=%d bytes, "
            "DISCORD_UPLOAD_LIMIT_MB=%s, final=%d bytes (origen=%s)",
            guild_limit,
            f"{env_limit_mb} MB" if env_limit_mb is not None else "no definido",
            max_bytes,
            origen_limite,
        )

        responder = MessageResponder(message)
        await run_video_flow(
            url,
            message.author.id,
            responder,
            max_bytes,
            origin=origin,
            channel_id=message.channel.id,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Enlaces(bot))
