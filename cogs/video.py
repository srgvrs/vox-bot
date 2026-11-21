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
from services.video_queue import (
    Job,
    JobTimeout,
    QueueClosed,
    QueueFull,
    UserBusy,
    video_queue,
)
from utils.errors import UNEXPECTED_ERROR_MESSAGE, new_error_ref, report_error
from utils.ratelimit import DailyUserLimiter
from utils.status import StatusMessage

logger = logging.getLogger(__name__)

_upload_limiter = DailyUserLimiter(settings.max_uploads_per_user_day)
_storage_chain = StorageChain()
_cooldowns: dict[int, float] = defaultdict(float)

# Margen de seguridad al decidir si un archivo cabe como adjunto.
_ATTACHMENT_SAFETY_MARGIN = 0.97


def _is_attachment_too_large(exc: discord.HTTPException) -> bool:
    """Detecta el rechazo de Discord por adjunto demasiado grande (413 / 40005)."""
    return getattr(exc, "status", None) == 413 or getattr(exc, "code", None) == 40005


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
            await self._publish(
                embed=embed,
                file=discord.File(result.path),
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
        await self._publish(embed=embed)

    async def _publish(
        self,
        embed: discord.Embed,
        file: discord.File | None = None,
    ) -> None:
        """Publica el resultado como mensaje nuevo; si falla, edita el original."""
        channel = self.interaction.channel
        if channel is not None:
            try:
                await channel.send(
                    embed=embed,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                logger.info("Resultado /video publicado como mensaje nuevo")
                try:
                    await self.interaction.delete_original_response()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    logger.debug(
                        "No se pudo borrar el mensaje de progreso: %s", exc
                    )
                return
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "channel.send falló (%s); se usa edición de respaldo", exc
                )
                # El File ya pudo consumirse; se recrea desde la ruta si aplica.
                if file is not None:
                    file = discord.File(file.fp.name)  # type: ignore[union-attr]
        logger.info("Resultado /video publicado por edición de respaldo")
        try:
            await self.interaction.edit_original_response(
                content=None,
                embed=embed,
                attachments=[file] if file is not None else [],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("No se pudo editar el mensaje de progreso (respaldo)")
            if file is not None:
                await self.interaction.followup.send(
                    embed=embed,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await self.interaction.followup.send(
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
            await self._publish(
                reply=reply,
                embed=embed,
                file=discord.File(result.path),
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
        await self._publish(reply=reply, embed=embed)
        await self._delete_original()

    async def _publish(
        self,
        reply: discord.Message,
        embed: discord.Embed,
        file: discord.File | None = None,
    ) -> None:
        """Publica el resultado como mensaje nuevo; si falla, edita el progreso."""
        try:
            await self.message.channel.send(
                embed=embed,
                file=file,
                reference=self.message.to_reference(fail_if_not_exists=False),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            logger.info("Resultado de enlaces publicado como mensaje nuevo")
            try:
                await reply.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.debug(
                    "No se pudo borrar el mensaje de progreso: %s", exc
                )
            return
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "channel.send falló (%s); se usa edición de respaldo", exc
            )
            if file is not None:
                file = discord.File(file.fp.name)  # type: ignore[union-attr]
        logger.info("Resultado de enlaces publicado por edición de respaldo")
        await reply.edit(
            content=None,
            embed=embed,
            attachments=[file] if file is not None else [],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_error(self, mensaje: str) -> None:
        reply = await self._ensure_reply(mensaje)
        try:
            await reply.edit(
                content=mensaje,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("No se pudo editar el mensaje de error")


async def _upload_external(
    final: DownloadResult,
    user_id: int,
    responder: Responder,
    recompressed: bool,
) -> None:
    """Sube el archivo a un host externo y envía el link."""
    if not _upload_limiter.is_allowed(user_id):
        logger.info(
            "Subida externa rechazada: usuario=%s alcanzó MAX_UPLOADS_PER_USER_DAY=%d",
            user_id,
            settings.max_uploads_per_user_day,
        )
        await responder.send_error(
            "Alcanzaste el límite diario de subidas externas. Intenta de nuevo mañana."
        )
        return

    size_mb = final.size / (1024 * 1024)
    await responder.set_status(
        f"El video pesa {size_mb:.1f} MB, supera el límite; "
        "lo estoy subiendo a un host…",
    )
    logger.info(
        "Subiendo a host externo: %.1f MB (usuario=%s, recomprimido=%s)",
        size_mb,
        user_id,
        recompressed,
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
    await responder.send_result(final, recompressed=recompressed, upload=upload)


async def run_video_flow(
    url: str,
    user_id: int,
    responder: Responder,
    max_bytes: int,
    origin: str,
    channel_id: int | None = None,
) -> None:
    """Flujo reutilizable de /video: descarga, recompresión y envío.

    Fase 1: el trabajo pasa por services/video_queue.py (prioridad, timeout,
    un trabajo por usuario). La lógica de descarga/recompresión no cambia.
    """
    logger.info(
        "Origen=%s usuario=%s canal=%s dominio=%s límite=%.1f MB",
        origin,
        user_id,
        channel_id,
        urlparse(url).hostname,
        max_bytes / (1024 * 1024),
    )

    async def _runner(job: Job) -> None:
        await _run_video_flow_inner(
            url, user_id, responder, max_bytes, origin, channel_id
        )

    async def _on_timeout(job: Job) -> None:
        logger.warning(
            "Timeout de video: usuario=%s canal=%s url=%s",
            user_id,
            channel_id,
            url,
        )
        try:
            await responder.send_error(
                "El video tardó demasiado y se canceló. Intenta de nuevo."
            )
        except Exception:
            logger.exception("No se pudo avisar del timeout")

    job = Job(
        user_id=user_id,
        channel_id=channel_id,
        url=url,
        priority=0,
        timeout_s=float(settings.video_timeout_s),
        runner=_runner,
        on_timeout=_on_timeout,
    )
    try:
        await video_queue.submit(job)
    except UserBusy:
        await responder.send_error("Ya tienes un video en proceso.")
    except QueueFull:
        await responder.send_error("Ocupado, intenta en un momento")
    except QueueClosed:
        await responder.send_error("El bot se está reiniciando")
    except JobTimeout:
        # on_timeout ya avisó al usuario.
        pass


async def _run_video_flow_inner(
    url: str,
    user_id: int,
    responder: Responder,
    max_bytes: int,
    origin: str,
    channel_id: int | None = None,
) -> None:
    """Cuerpo original del flujo (sin cambios de lógica)."""

    # Margen de seguridad: Discord rechaza adjuntos que rozan el límite.
    safe_bytes = int(max_bytes * _ATTACHMENT_SAFETY_MARGIN)

    result: DownloadResult | None = None
    recompressed: DownloadResult | None = None
    try:
        result = await download_video(
            url,
            max_bytes=safe_bytes,
            on_status=responder.set_status,
        )
        logger.info(
            "Descarga OK: %s (%d bytes, límite adjunto seguro %d bytes)",
            result.title,
            result.size,
            safe_bytes,
        )

        # 1) Intentar adjunto directo.
        if result.size <= safe_bytes:
            logger.info(
                "Enviando como adjunto: %.1f MB (límite %.1f MB)",
                result.size / (1024 * 1024),
                safe_bytes / (1024 * 1024),
            )
            try:
                await responder.send_result(result, recompressed=False, upload=None)
                return
            except discord.HTTPException as exc:
                if _is_attachment_too_large(exc):
                    logger.warning(
                        "Discord rechazó el adjunto (%d bytes, status=%s, code=%s); "
                        "se cae al flujo de subida externa.",
                        result.size,
                        getattr(exc, "status", "?"),
                        getattr(exc, "code", "?"),
                    )
                else:
                    raise
            await _upload_external(result, user_id, responder, recompressed=False)
            return

        # 2) Recomprimir una sola vez, solo si el archivo no cabe y las reglas
        #    de seguridad lo permiten. Si no, se sube directo al host externo.
        #    Fase 2: el downloader ya eligió el modo ("recompress" o "host");
        #    si eligió "host", no se intenta recomprimir aquí.
        logger.info(
            "El archivo pesa %.1f MB y el límite es %.1f MB (origen: %s, mode=%s); "
            "intento recomprimir",
            result.size / (1024 * 1024),
            safe_bytes / (1024 * 1024),
            origin,
            result.mode,
        )
        duration_known = result.duration is not None and result.duration > 0
        duration_min = (result.duration or 0) / 60
        source_mb = result.size / (1024 * 1024)
        max_source_mb = settings.recompress_max_source_mb
        skip_reason: str | None = None
        if result.mode == "host":
            skip_reason = "el downloader eligió host externo (Fase 2)"
        elif not settings.recompress_enabled:
            skip_reason = "RECOMPRESS_ENABLED=false (desactivada)"
        elif settings.recompress_max_minutes <= 0:
            skip_reason = "RECOMPRESS_MAX_MINUTES=0 (desactivada)"
        elif not duration_known:
            skip_reason = "duración desconocida (no se pudo medir con ffprobe)"
        elif duration_min > settings.recompress_max_minutes:
            skip_reason = (
                f"duración {duration_min:.1f} min > "
                f"RECOMPRESS_MAX_MINUTES={settings.recompress_max_minutes}"
            )
        elif max_source_mb > 0 and source_mb > max_source_mb:
            skip_reason = (
                f"tamaño origen {source_mb:.1f} MB > "
                f"RECOMPRESS_MAX_SOURCE_MB={max_source_mb}"
            )
        elif not ffmpeg_available():
            skip_reason = "ffmpeg no disponible"

        if skip_reason is not None:
            logger.info(
                "Se omite la recompresión: %s. Se sube el original (%d bytes) al host externo.",
                skip_reason,
                result.size,
            )
        else:
            try:
                recompressed = await recompress_video(
                    result,
                    safe_bytes,
                    on_status=responder.set_status,
                )
                logger.info(
                    "Recompresión OK: %d bytes (antes %d)",
                    recompressed.size,
                    result.size,
                )
                if recompressed.size <= safe_bytes:
                    logger.info(
                        "Enviando como adjunto (recomprimido): %.1f MB",
                        recompressed.size / (1024 * 1024),
                    )
                    try:
                        await responder.send_result(
                            recompressed, recompressed=True, upload=None
                        )
                        return
                    except discord.HTTPException as exc:
                        if _is_attachment_too_large(exc):
                            logger.warning(
                                "Discord rechazó el adjunto recomprimido "
                                "(%d bytes, status=%s, code=%s); se sube el original.",
                                recompressed.size,
                                getattr(exc, "status", "?"),
                                getattr(exc, "code", "?"),
                            )
                            recompressed.path.unlink(missing_ok=True)
                            recompressed = None
                        else:
                            raise
            except RecompressError as exc:
                logger.warning(
                    "Recompresión fallida (%s); se sube el original al host externo.",
                    exc,
                )
                if recompressed is not None:
                    recompressed.path.unlink(missing_ok=True)
                    recompressed = None

        # 3) Subida externa. Si el recomprimido no cabe, se sube el ORIGINAL.
        if recompressed is not None and recompressed.size > safe_bytes:
            logger.info(
                "El recomprimido (%d bytes) no cabe en %d; se sube el original (%d bytes).",
                recompressed.size,
                safe_bytes,
                result.size,
            )
            recompressed.path.unlink(missing_ok=True)
            recompressed = None
        final = recompressed or result
        logger.info(
            "Subiendo a host externo: %.1f MB (recomprimido=%s)",
            final.size / (1024 * 1024),
            final is recompressed,
        )
        await _upload_external(
            final, user_id, responder, recompressed=final is recompressed
        )

    except VideoTooLongError as exc:
        logger.exception("Video demasiado largo")
        await responder.send_error(str(exc))
    except VideoRestrictedError as exc:
        logger.exception("Video restringido")
        await responder.send_error(str(exc))
    except VideoUnsupportedError as exc:
        logger.exception("Video no soportado")
        await responder.send_error(str(exc))
    except VideoNetworkError as exc:
        logger.exception("Error de red al descargar el video")
        await responder.send_error(str(exc))
    except VideoLoginRequiredError as exc:
        logger.exception("El origen exige iniciar sesión")
        await responder.send_error(str(exc))
    except VideoUnavailableError as exc:
        logger.exception("Video no disponible")
        await responder.send_error(str(exc))
    except VideoError as exc:
        logger.exception("Error de video")
        await responder.send_error(str(exc))
    except asyncio.TimeoutError as exc:
        logger.exception("Timeout en /video")
        await report_error("video", exc)
        await responder.send_error(
            "Se agotó el tiempo de espera al procesar el video."
        )
    except Exception as exc:
        logger.exception("Error inesperado en /video")
        await report_error("video", exc)
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

        try:
            guild_limit = interaction.guild.filesize_limit if interaction.guild else 0
            config_limit = settings.upload_limit_mb * 1024 * 1024
            # Discord expone el límite real del servidor (10/25/50/100 MB según boosts).
            # Si el guild reporta un límite mayor que el configurado, se usa el del guild.
            # Si el config es mayor (servidor boosteado con filesize_limit desactualizado),
            # se usa el config para no subir innecesariamente a un host externo.
            if guild_limit and guild_limit > config_limit:
                max_bytes = guild_limit
                winner = "guild"
            else:
                max_bytes = config_limit
                winner = "env"
            logger.info(
                "Límite de adjunto: %d bytes (gana %s; env=%d MB, guild.filesize_limit=%d bytes)",
                max_bytes,
                winner,
                settings.upload_limit_mb,
                guild_limit,
            )
            logger.info(
                "Solicitud /video: usuario=%s servidor=%s url=%s límite=%.1f MB (origen=%s)",
                interaction.user.id,
                interaction.guild.id if interaction.guild else "DM",
                url,
                max_bytes / (1024 * 1024),
                winner,
            )

            await run_video_flow(
                url,
                interaction.user.id,
                responder,
                max_bytes,
                origin="slash",
                channel_id=interaction.channel_id,
            )
        except Exception as exc:
            ref = new_error_ref()
            logger.exception(
                "Error inesperado en /video (ref=%s) usuario=%s canal=%s url=%s",
                ref,
                interaction.user.id,
                interaction.channel_id,
                url,
            )
            await report_error(f"video ref={ref}", exc)
            await _reply_unexpected(interaction, ref)


async def _reply_unexpected(
    interaction: discord.Interaction, ref: str
) -> None:
    """Responde con el mensaje genérico (ephemeral) sin filtrar detalles."""
    mensaje = UNEXPECTED_ERROR_MESSAGE.format(ref=ref)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(mensaje, ephemeral=True)
        else:
            await interaction.response.send_message(mensaje, ephemeral=True)
    except discord.HTTPException:
        logger.debug("No se pudo enviar el mensaje de error inesperado (ref=%s)", ref)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Video(bot))
