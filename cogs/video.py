"""Cog de descarga de video: /video (adjunto o link)."""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import settings
from media.downloader import (
    DownloadResult,
    RecompressError,
    VideoError,
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

        await interaction.response.defer()

        guild_limit = interaction.guild.filesize_limit if interaction.guild else 0
        config_limit = settings.upload_limit_mb * 1024 * 1024
        max_bytes = min(guild_limit, config_limit) if guild_limit else config_limit

        result: DownloadResult | None = None
        recompressed: DownloadResult | None = None
        try:
            result = await download_video(url)
            logger.info("Descarga OK: %s (%d bytes)", result.title, result.size)

            # 1) Intentar adjunto directo.
            if result.size <= max_bytes:
                if await self._try_send_attachment(interaction, result, recompressed=False):
                    return

            # 2) Recomprimir una sola vez.
            if ffmpeg_available() and result.duration:
                try:
                    recompressed = await recompress_video(result, max_bytes)
                    logger.info(
                        "Recompresión OK: %d bytes (antes %d)",
                        recompressed.size,
                        result.size,
                    )
                    if recompressed.size <= max_bytes:
                        if await self._try_send_attachment(
                            interaction, recompressed, recompressed=True
                        ):
                            return
                except RecompressError as exc:
                    logger.warning("Recompresión fallida: %s", exc)

            # 3) Subida externa.
            await self._send_external(interaction, recompressed or result)

        except VideoTooLongError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except VideoRestrictedError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except VideoUnsupportedError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except VideoNetworkError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except VideoUnavailableError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except VideoError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except asyncio.TimeoutError:
            logger.exception("Timeout en /video")
            await interaction.followup.send(
                "Se agotó el tiempo de espera al procesar el video.",
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception("Error inesperado en /video")
            await interaction.followup.send(
                f"Error inesperado: {type(exc).__name__}: {str(exc)[:200]}",
                ephemeral=True,
            )
        finally:
            for item in (result, recompressed):
                if item is not None:
                    item.path.unlink(missing_ok=True)

    async def _try_send_attachment(
        self,
        interaction: discord.Interaction,
        result: DownloadResult,
        recompressed: bool,
    ) -> bool:
        """Intenta enviar como adjunto. Devuelve True si se envió."""
        origen = "Adjunto (recomprimido)" if recompressed else "Adjunto"
        embed = discord.Embed(title=(result.title or "Video")[:256])
        embed.set_footer(text=origen)
        try:
            await interaction.followup.send(
                embed=embed, file=discord.File(result.path)
            )
            logger.info("Enviado como %s: %s", origen, result.title)
            return True
        except discord.HTTPException as exc:
            if exc.status == 413 or getattr(exc, "code", None) == 40005:
                logger.warning(
                    "Discord rechazó el adjunto (413/40005); se intentará subida externa."
                )
                return False
            logger.exception("HTTPException al enviar adjunto")
            await interaction.followup.send(
                f"Discord rechazó el archivo: {exc.status} ({exc.text or 'sin detalle'})",
                ephemeral=True,
            )
            return True

    async def _send_external(
        self, interaction: discord.Interaction, result: DownloadResult
    ) -> None:
        """Sube a un host externo y envía el enlace."""
        if not _upload_limiter.is_allowed(interaction.user.id):
            await interaction.followup.send(
                "Alcanzaste el límite diario de subidas externas. Intenta de nuevo mañana.",
                ephemeral=True,
            )
            return

        try:
            upload: UploadResult = await _storage_chain.upload(result.path)
        except StorageError as exc:
            logger.exception("Fallo de StorageChain")
            await interaction.followup.send(
                f"No se pudo subir el video: {str(exc)[:300]}",
                ephemeral=True,
            )
            return

        _upload_limiter.consume(interaction.user.id)

        origen = f"Subido a {upload.provider}"
        footer = f"{origen} (el enlace caduca en {upload.expiry_hours} h)"
        embed = discord.Embed(title=(result.title or "Video")[:256])
        embed.description = upload.url
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)
        logger.info("Enviado como %s: %s", origen, result.title)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Video(bot))
