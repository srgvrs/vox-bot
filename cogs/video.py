"""Cog de descarga de video: /video (adjunto o link)."""
import asyncio
import logging
import time

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

        await interaction.response.send_message("Buscando video...")
        status = StatusMessage(interaction)
        await status.set("Buscando video...")

        guild_limit = interaction.guild.filesize_limit if interaction.guild else 0
        config_limit = settings.upload_limit_mb * 1024 * 1024
        max_bytes = min(guild_limit, config_limit) if guild_limit else config_limit
        logger.info(
            "Límite de adjunto: %d bytes (env=%d MB, guild.filesize_limit=%d bytes)",
            max_bytes,
            settings.upload_limit_mb,
            guild_limit,
        )

        result: DownloadResult | None = None
        recompressed: DownloadResult | None = None
        try:
            result = await download_video(
                url,
                max_bytes=max_bytes,
                on_status=status.set,
            )
            logger.info(
                "Descarga OK: %s (%d bytes, límite adjunto %d bytes)",
                result.title,
                result.size,
                max_bytes,
            )

            # 1) Intentar adjunto directo.
            if result.size <= max_bytes:
                if await self._try_send_attachment(
                    interaction, result, recompressed=False, status=status
                ):
                    return

            # 2) Recomprimir una sola vez (si la duración lo permite).
            #    Si la duración es desconocida, se saltan las reglas por duración
            #    pero no se bloquea la recompresión cuando el archivo no cabe.
            duration_known = result.duration is not None and result.duration > 0
            duration_min = (result.duration or 0) / 60
            if duration_known and duration_min > settings.recompress_max_minutes:
                logger.info(
                    "Se omite la recompresión: duración %.1f min > RECOMPRESS_MAX_MINUTES=%d",
                    duration_min,
                    settings.recompress_max_minutes,
                )
                # Si el archivo de HOST_VIDEO_HEIGHT cabe como adjunto, enviarlo.
                if result.size <= max_bytes:
                    if await self._try_send_attachment(
                        interaction, result, recompressed=False, status=status
                    ):
                        return
            elif ffmpeg_available() and duration_known:
                try:
                    recompressed = await recompress_video(
                        result,
                        max_bytes,
                        on_status=status.set,
                    )
                    logger.info(
                        "Recompresión OK: %d bytes (antes %d)",
                        recompressed.size,
                        result.size,
                    )
                    if recompressed.size <= max_bytes:
                        if await self._try_send_attachment(
                            interaction, recompressed, recompressed=True, status=status
                        ):
                            return
                except RecompressError as exc:
                    logger.warning("Recompresión fallida: %s", exc)

            # 3) Subida externa.
            await self._send_external(interaction, recompressed or result, status=status)

        except VideoTooLongError as exc:
            await status.stop()
            await self._fail(interaction, str(exc))
        except VideoRestrictedError as exc:
            await status.stop()
            await self._fail(interaction, str(exc))
        except VideoUnsupportedError as exc:
            await status.stop()
            await self._fail(interaction, str(exc))
        except VideoNetworkError as exc:
            await status.stop()
            await self._fail(interaction, str(exc))
        except VideoLoginRequiredError as exc:
            await status.stop()
            await self._fail(interaction, str(exc))
        except VideoUnavailableError as exc:
            await status.stop()
            await self._fail(interaction, str(exc))
        except VideoError as exc:
            await status.stop()
            await self._fail(interaction, str(exc))
        except asyncio.TimeoutError:
            logger.exception("Timeout en /video")
            await status.stop()
            await self._fail(
                interaction, "Se agotó el tiempo de espera al procesar el video."
            )
        except Exception as exc:
            logger.exception("Error inesperado en /video")
            await status.stop()
            await self._fail(
                interaction,
                f"Error inesperado: {type(exc).__name__}: {str(exc)[:200]}",
            )
        finally:
            await status.stop()
            for item in (result, recompressed):
                if item is not None:
                    item.path.unlink(missing_ok=True)

    async def _fail(self, interaction: discord.Interaction, mensaje: str) -> None:
        """Borra el mensaje de estado y responde con un error ephemeral."""
        try:
            await interaction.delete_original_response()
        except (discord.HTTPException, discord.NotFound) as exc:
            logger.debug("No se pudo borrar el mensaje de estado: %s", exc)
        try:
            await interaction.followup.send(mensaje, ephemeral=True)
        except discord.HTTPException:
            logger.exception("No se pudo enviar el mensaje de error")

    async def _try_send_attachment(
        self,
        interaction: discord.Interaction,
        result: DownloadResult,
        recompressed: bool,
        status: StatusMessage,
    ) -> bool:
        """Intenta enviar como adjunto. Devuelve True si se envió."""
        origen = "Adjunto (recomprimido)" if recompressed else "Adjunto"
        embed = discord.Embed(title=(result.title or "Video")[:256])
        if result.notice:
            embed.description = result.notice
        embed.set_footer(text=origen)
        await status.set("Enviando...")
        await status.stop()
        start = time.perf_counter()
        try:
            await interaction.edit_original_response(
                content=None,
                embed=embed,
                attachments=[discord.File(result.path)],
            )
            logger.info(
                "Enviado como %s en %.2f s: %s",
                origen,
                time.perf_counter() - start,
                result.title,
            )
            return True
        except discord.HTTPException as exc:
            if exc.status == 413 or getattr(exc, "code", None) == 40005:
                logger.warning(
                    "Discord rechazó el adjunto (413/40005); se intentará subida externa."
                )
                return False
            logger.exception("HTTPException al enviar adjunto")
            await self._fail(
                interaction,
                f"Discord rechazó el archivo: {exc.status} ({exc.text or 'sin detalle'})",
            )
            return True

    async def _send_external(
        self,
        interaction: discord.Interaction,
        result: DownloadResult,
        status: StatusMessage,
    ) -> None:
        """Sube a un host externo y envía el enlace."""
        if not _upload_limiter.is_allowed(interaction.user.id):
            await status.stop()
            await self._fail(
                interaction,
                "Alcanzaste el límite diario de subidas externas. Intenta de nuevo mañana.",
            )
            return

        await status.set(
            "El video es muy grande para Discord, se subirá a un host externo "
            "(puede tardar un poco)...",
        )
        start = time.perf_counter()
        try:
            upload: UploadResult = await _storage_chain.upload(result.path)
        except StorageError as exc:
            logger.exception("Fallo de StorageChain")
            await status.stop()
            await self._fail(
                interaction, f"No se pudo subir el video: {str(exc)[:300]}"
            )
            return

        _upload_limiter.consume(interaction.user.id)
        logger.info(
            "Subida externa completada en %.2f s (%s)",
            time.perf_counter() - start,
            upload.provider,
        )

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
        await status.set("Enviando...")
        await status.stop()
        try:
            await interaction.edit_original_response(
                content=None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.HTTPException, discord.NotFound) as exc:
            logger.warning("No se pudo editar el mensaje final: %s", exc)
        logger.info("Enviado como %s: %s", origen, result.title)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Video(bot))
