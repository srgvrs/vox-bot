"""Cog de enlaces: modo mención y modo automático para /video."""
import logging
import re
import time
import unicodedata

import discord
from discord.ext import commands

from cogs.chat import Chat
from cogs.video import MessageResponder, check_cooldown, run_video_flow
from config import settings
from media.downloader import _host_allowed
from utils.errors import UNEXPECTED_ERROR_MESSAGE, new_error_ref, report_error
from utils.mensajes_store import store as mensajes_store
from utils.permisos import es_admin

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

# Frases de activar/desactivar mensajes (sin acentos, minúsculas).
_ACTIVAR_PHRASES = ("activa mensajes", "enciende mensajes")
_DESACTIVAR_PHRASES = ("desactiva mensajes", "apaga mensajes")


def _normalize(text: str) -> str:
    """Minúsculas y sin acentos, para comparar frases."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _detect_toggle(text: str) -> bool | None:
    """Devuelve True (activar), False (desactivar) o None si no aplica."""
    norm = _normalize(text)
    for phrase in _ACTIVAR_PHRASES:
        if phrase in norm:
            return True
    for phrase in _DESACTIVAR_PHRASES:
        if phrase in norm:
            return False
    return None

# Anti-spam del aviso de error inesperado: máx. 1 por usuario cada 60 s.
_UNEXPECTED_NOTICE_INTERVAL_S = 60
_last_unexpected_notice: dict[int, float] = {}


def _extract_urls(text: str) -> list[str]:
    """Devuelve todas las URLs (http/https) del texto, sin puntuación final."""
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        urls.append(match.group(0).rstrip(".,;:!?)]}\"'"))
    return urls


def _extract_video_url(text: str) -> str | None:
    """Devuelve la primera URL con dominio permitido, o None."""
    for url in _extract_urls(text):
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

    async def _handle_toggle(
        self, message: discord.Message, activar: bool
    ) -> None:
        """Aplica el interruptor global de mensajes desde una mención."""
        if not es_admin(message.author, message.guild):
            await message.reply(
                "Solo los administradores pueden hacer eso.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await mensajes_store.set_global_enabled(activar)
        estado = "activado" if activar else "desactivado"
        logger.info(
            "Interruptor de mensajes %s por mención (usuario=%s canal=%s)",
            estado,
            message.author.id,
            message.channel.id,
        )
        await message.reply(
            f"Interruptor de mensajes: {estado}.",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

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

        # --- Enrutado en modo mención -----------------------------------
        if mentioned:
            urls = _extract_urls(message.content)
            if urls:
                url = _extract_video_url(message.content)
                if url is None:
                    logger.info(
                        "Mención con URL no soportada (usuario=%s canal=%s urls=%s)",
                        message.author.id,
                        message.channel.id,
                        urls,
                    )
                    await message.reply(
                        "Ese enlace no está soportado.",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                logger.info(
                    "Mención -> ruta=descarga (usuario=%s canal=%s url=%s)",
                    message.author.id,
                    message.channel.id,
                    url,
                )
            else:
                # Sin URLs: probar frases de activar/desactivar mensajes.
                text_only = message.content
                if self.bot.user is not None:
                    for mention in (
                        f"<@{self.bot.user.id}>",
                        f"<@!{self.bot.user.id}>",
                    ):
                        text_only = text_only.replace(mention, " ")
                toggle = _detect_toggle(text_only)
                if toggle is not None:
                    await self._handle_toggle(message, toggle)
                    return
                if not text_only.strip():
                    logger.info(
                        "Mención vacía (usuario=%s canal=%s)",
                        message.author.id,
                        message.channel.id,
                    )
                    await message.reply(
                        "Mencióname con un enlace de YouTube o Instagram",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                logger.info(
                    "Mención -> ruta=chat (usuario=%s canal=%s)",
                    message.author.id,
                    message.channel.id,
                )
                chat_cog = self.bot.get_cog("Chat")
                if isinstance(chat_cog, Chat):
                    await chat_cog.handle_mention(message)
                else:
                    logger.warning(
                        "Cog Chat no disponible para mención (usuario=%s)",
                        message.author.id,
                    )
                return

        # --- Modo automático (canal) ------------------------------------
        url = _extract_video_url(message.content)
        if url is None:
            return

        origin = "auto"
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

        logger.info(
            "Ruta=descarga (auto) usuario=%s canal=%s url=%s",
            message.author.id,
            message.channel.id,
            url,
        )

        responder = MessageResponder(message)
        try:
            await run_video_flow(
                url,
                message.author.id,
                responder,
                max_bytes,
                origin=origin,
                channel_id=message.channel.id,
            )
        except Exception as exc:
            ref = new_error_ref()
            logger.exception(
                "Error inesperado en enlaces (ref=%s) usuario=%s canal=%s url=%s",
                ref,
                message.author.id,
                message.channel.id,
                url,
            )
            await report_error(f"enlaces ref={ref}", exc)
            await _notify_unexpected(message, responder, ref)


async def _notify_unexpected(
    message: discord.Message, responder: MessageResponder, ref: str
) -> None:
    """Avisa del error inesperado (máx. 1 por usuario cada 60 s).

    Si ya hay un mensaje de progreso, se edita para no dejarlo colgado.
    """
    now = time.monotonic()
    last = _last_unexpected_notice.get(message.author.id, 0.0)
    if now - last < _UNEXPECTED_NOTICE_INTERVAL_S:
        return
    _last_unexpected_notice[message.author.id] = now

    mensaje = UNEXPECTED_ERROR_MESSAGE.format(ref=ref)
    try:
        if responder.reply is not None:
            await responder.reply.edit(
                content=mensaje,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await message.reply(
                mensaje,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except discord.HTTPException:
        logger.debug(
            "No se pudo enviar el aviso de error inesperado (ref=%s)", ref
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Enlaces(bot))
