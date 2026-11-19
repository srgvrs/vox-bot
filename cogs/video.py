"""Cog de descarga de video: /video (adjunto o link)."""
import asyncio
import logging
import time
from collections import defaultdict
from typing import Protocol
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from config import settings
from media.downloader import (
    DownloadResult,
    RecompressError,
    VideoError,
    VideoLoginRequiredError,
    VideoNetworkError,
    VideoRestrictedError,
    VideoTooLongError,
    VideoUnavailableError,
    VideoUnsupportedError,
    download_video,
    ffmpeg_available,
    recompress_video,
)
from media.storage import StorageChain, StorageError, UploadResult
from utils.ratelimit import DailyUserLimiter
from utils.status import StatusMessage

logger = logging.getLogger(__name__)

_upload_limiter = DailyUserLimiter(settings.max_uploads_per_user_day)
_storage_chain = StorageChain()
_cooldowns: dict[int, float] = defaultdict(float)


def _is_allowed(interaction: discord.Interaction) -> bool:
    """Permite solo en guilds autorizados; rechaza DMs."""
    if interaction.guild is None:
        return False
    if not settings.allowed_guild_ids:
        return True
    return interaction.guild.id in settings.allowed_guild_ids


def check_cooldown(user_id: int) -> int:
    """Devuelve los segundos restantes de cooldown (0 si puede usar)."""
    now = time.monotonic()
    remaining = settings.video_cooldown_seconds - (now - _cooldowns[user_id])
    if remaining > 0:
        return int(remaining) + 1
    _cooldowns[user_id] = now
    return 0


class Responder(Protocol):
    """Abstracción de respuesta para reutilizar el flujo de /video."""

    async def set_status(self, texto: str) -> None:
        """Actualiza el mensaje de estado."""

    async def send_result(
        self,
        result: DownloadResult,
        recompressed: bool,
        upload: UploadResult | None,
    ) -> None:
        """Envía el resultado final (adjunto o link)."""

    async def send_error(self, mensaje: str) -> None:
        """Envía un error al usuario."""


class InteractionResponder:
    """Responder basado en una interacción de slash command."""

    def __init__(self, interaction: discord.Interaction) -> None:
        self.interaction = interaction
        self.status = StatusMessage(interaction)

    async def set_status(self, texto: str) -> None:
        await self.status.set(texto)

    async def send_result(
        self,
        result: DownloadResult,
        recompressed: bool,
        upload: UploadResult | None,
    ) -> None:
        await self.status.stop()
        if upload is None:
            origen = "Adjunto (recomprimido)" if recompressed else "Adjunto"
            embed = discord.Embed(title=(result.title or "Video")[:256])
            if result.notice:
                embed.description = result.notice
            embed.set_footer(text=origen)
            await self.interaction.edit_original_response(
                content=None,
                embed=embed,
                attachments=[discord.File(result.path)],
            )
            return
        origen = f"Subido a {upload.provider}"
        size_mb = result.size / (1024 * 1024)
        descripcion = "Toca el título para abrir el video"
        if result.notice:
            descripcion = f"{result.notice}\n{descripcion}"
        embed = discord.Embed(
            title=(result.title or "Video")[:256],
            url=upload.url,
            description=descripcion,
        )
        if result.thumbnail:
            embed.set_image(url=result.thumbnail)
        embed.set_footer(
            text=f"{origen} (caduca en {upload.expiry_hours} h) · {size_mb:.1f} MB"
        )
        await self.interaction.edit_original_response(
            content=None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_error(self, mensaje: str) -> None:
        await self.status.stop()
        try:
            await self.interaction.delete_original_response()
        except (discord.HTTPException, discord.NotFound) as exc:
            logger.debug("No se pudo borrar el mensaje de estado: %s", exc)
        try:
            await self.interaction.followup.send(mensaje, ephemeral=True)
        except discord.HTTPException:
            logger.exception("No se pudo enviar el mensaje de error")


_forbidden_warned_channels: set[int] = set()


class MessageResponder:
    """Responder basado en un mensaje (reply o channel.send editado)."""

    def __init__(self, message: discord.Message) -> None:
        self.message = message
        self.reply: discord.Message | None = None
        self.delete_original = settings.delete_original_message
        self.author_label = f"Pedido por {message.author.display_name}"

    async def _ensure_reply(self, content: str) -> discord.Message:
        if self.reply is None:
            if self.delete_original:
                self.reply = await self.message.channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                self.reply = await self.message.reply(
                    content,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        return self.reply

    async def _delete_original(self) -> None:
        """Borra el mensaje original; tolera permisos y mensajes ya borrados."""
        if not self.delete_original:
            return
        channel_id = self.message.channel.id
        try:
            await self.message.delete()
        except discord.Forbidden:
            if channel_id not in _forbidden_warned_channels:
                _forbidden_warned_channels.add(channel_id)
                logger.warning(
                    "No se pudo borrar el mensaje original en el canal %s: "
                    "falta permiso Administrar mensajes",
                    channel_id,
                )
        except discord.NotFound:
            logger.debug("El mensaje original ya no existe en el canal %s", channel_id)
        except discord.HTTPException as exc:
            logger.warning(
                "HTTPException al borrar el mensaje original en el canal %s: %s",
                channel_id,
                exc,
            )

    async def set_status(self, texto: str) -> None:
        reply = await self._ensure_reply(texto)
        try:
            await reply.edit(
                content=texto,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            logger.debug("No se pudo editar el estado del mensaje: %s", exc)

    async def send_result(
        self,
        result: DownloadResult,
        recompressed: bool,
        upload: UploadResult | None,
    ) -> None:
        reply = await self._ensure_reply("Listo")
        if upload is None:
            origen = "Adjunto (recomprimido)" if recompressed else "Adjunto"
            embed = discord.Embed(title=(result.title or "Video")[:256])
            if result.notice:
                embed.description = result.notice
            footer = origen
            if self.delete_original:
                footer = f"{origen} · {self.author_label}"
            embed.set_footer(text=footer)
            await reply.edit(
                content=None,
                embed=embed,
                attachments=[discord.File(result.path)],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self._delete_original()
            return
        origen = f"Subido a {upload.provider}"
        size_mb = result.size / (1024 * 1024)
        descripcion = "Toca el título para abrir el video"
        if result.notice:
            descripcion = f"{result.notice}\n{descripcion}"
        embed = discord.Embed(
            title=(result.title or "Video")[:256],
            url=upload.url,
            description=descripcion,
        )
        if result.thumbnail:
            embed.set_image(url=result.thumbnail)
        footer = f"{origen} (caduca en {upload.expiry_hours} h) · {size_mb:.1f} MB"
        if self.delete_original:
            footer = f"{footer} · {self.author_label}"
        embed.set_footer(text=footer)
        await reply.edit(
            content=None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._delete_original()

    async def send_error(self, mensaje: str) -> None:
        reply = await self._ensure_reply(mensaje)
        try:
            await reply.edit(
                content=mensaje,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("No se pudo editar el mensaje de error")


async def run_video_flow(
    url: str,
    user_id: int,
    responder: Responder,
    max_bytes: int,
    origin: str,
    channel_id: int | None = None,
) -> None:
    """Flujo reutilizable de /video: descarga, recompresión y envío."""
    logger.info(
        "Origen=%s usuario=%s canal=%s dominio=%s",
        origin,
        user_id,
        channel_id,
        urlparse(url).hostname,
    )

    result: DownloadResult | None = None
    recompressed: DownloadResult | None = None
    try:
        result = await download_video(
            url,
            max_bytes=max_bytes,
            on_status=responder.set_status,
        )
        logger.info(
            "Descarga OK: %s (%d bytes, límite adjunto %d bytes)",
            result.title,
            result.size,
            max_bytes,
        )

        # 1) Intentar adjunto directo.
        if result.size <= max_bytes:
            await responder.send_result(result, recompressed=False, upload=None)
            return

        # 2) Recomprimir una sola vez (si la duración lo permite).
        duration_known = result.duration is not None and result.duration > 0
        duration_min = (result.duration or 0) / 60
        if duration_known and duration_min > settings.recompress_max_minutes:
            logger.info(
                "Se omite la recompresión: duración %.1f min > RECOMPRESS_MAX_MINUTES=%d",
                duration_min,
                settings.recompress_max_minutes,
            )
        elif ffmpeg_available() and duration_known:
            try:
                recompressed = await recompress_video(
                    result,
                    max_bytes,
                    on_status=responder.set_status,
                )
                logger.info(
                    "Recompresión OK: %d bytes (antes %d)",
                    recompressed.size,
                    result.size,
                )
                if recompressed.size <= max_bytes:
                    await responder.send_result(
                        recompressed, recompressed=True, upload=None
                    )
                    return
            except RecompressError as exc:
                logger.warning("Recompresión fallida: %s", exc)

        # 3) Subida externa.
        final = recompressed or result
        if not _upload_limiter.is_allowed(user_id):
            await responder.send_error(
                "Alcanzaste el límite diario de subidas externas. Intenta de nuevo mañana."
            )
            return

        await responder.set_status(
            "El video es muy grande para Discord, se subirá a un host externo "
            "(puede tardar un poco)...",
        )
        start = time.perf_counter()
        try:
            upload: UploadResult = await _storage_chain.upload(final.path)
        except StorageError as exc:
            logger.exception("Fallo de StorageChain")
            await responder.send_error(f"No se pudo subir el video: {str(exc)[:300]}")
            return

        _upload_limiter.consume(user_id)
        logger.info(
            "Subida externa completada en %.2f s (%s)",
            time.perf_counter() - start,
            upload.provider,
        )
        await responder.send_result(final, recompressed=final is recompressed, upload=upload)

    except VideoTooLongError as exc:
        await responder.send_error(str(exc))
    except VideoRestrictedError as exc:
        await responder.send_error(str(exc))
    except VideoUnsupportedError as exc:
        await responder.send_error(str(exc))
    except VideoNetworkError as exc:
        await responder.send_error(str(exc))
    except VideoLoginRequiredError as exc:
        await responder.send_error(str(exc))
    except VideoUnavailableError as exc:
        await responder.send_error(str(exc))
    except VideoError as exc:
        await responder.send_error(str(exc))
    except asyncio.TimeoutError:
        logger.exception("Timeout en /video")
        await responder.send_error(
            "Se agotó el tiempo de espera al procesar el video."
        )
    except Exception as exc:
        logger.exception("Error inesperado en /video")
        await responder.send_error(
            f"Error inesperado: {type(exc).__name__}: {str(exc)[:200]}"
        )
    finally:
        for item in (result, recompressed):
            if item is not None:
                item.path.unlink(missing_ok=True)


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

        remaining = check_cooldown(interaction.user.id)
        if remaining > 0:
            await interaction.response.send_message(
                f"Espera {remaining} s para usar /video de nuevo",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("Buscando video...")
        responder = InteractionResponder(interaction)
        await responder.set_status("Buscando video...")

        guild_limit = interaction.guild.filesize_limit if interaction.guild else 0
        config_limit = settings.upload_limit_mb * 1024 * 1024
        max_bytes = min(guild_limit, config_limit) if guild_limit else config_limit
        logger.info(
            "Límite de adjunto: %d bytes (env=%d MB, guild.filesize_limit=%d bytes)",
            max_bytes,
            settings.upload_limit_mb,
            guild_limit,
        )

        await run_video_flow(
            url,
            interaction.user.id,
            responder,
            max_bytes,
            origin="slash",
            channel_id=interaction.channel_id,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Video(bot))
