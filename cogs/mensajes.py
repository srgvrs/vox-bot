"""Mensajes personalizados por usuario con interruptor global e individual."""
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import settings
from utils.mensajes_store import store

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 300
MAX_MESSAGES_PER_USER = 20

# Cooldown en memoria por usuario (monotonic).
_last_reply: dict[int, float] = {}


def _is_admin(interaction: discord.Interaction) -> bool:
    """True si el usuario es admin configurado o tiene Gestionar servidor."""
    if interaction.user.id in settings.admin_user_ids:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def _guild_allowed(guild: Optional[discord.Guild]) -> bool:
    if guild is None:
        return False
    if not settings.allowed_guild_ids:
        return True
    return guild.id in settings.allowed_guild_ids


class Mensajes(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await store.load()
        if not self.bot.intents.message_content:
            logger.warning(
                "Mensajes personalizados: el intent 'message_content' está "
                "desactivado; on_message solo verá menciones, respuestas y "
                "mensajes en canales donde el bot tenga acceso al contenido."
            )

    # --- Listener ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._handle_message(message)
        except Exception:
            logger.exception(
                "Error procesando mensaje para respuestas personalizadas "
                "(canal=%s autor=%s)",
                message.channel.id if message.channel else "?",
                message.author.id if message.author else "?",
            )

    async def _handle_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        if not _guild_allowed(message.guild):
            return
        if not store.is_active(message.author.id):
            return

        now = time.monotonic()
        cooldown = settings.custom_msg_cooldown_seconds
        last = _last_reply.get(message.author.id, 0.0)
        if cooldown > 0 and now - last < cooldown:
            return

        text = await store.pick_message(message.author.id)
        if not text:
            return
        _last_reply[message.author.id] = now

        rendered = text.replace("{usuario}", message.author.mention)
        sent: discord.Message | None = None
        try:
            sent = await message.reply(
                rendered,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        except discord.Forbidden:
            try:
                sent = await message.channel.send(
                    rendered,
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False
                    ),
                )
            except discord.HTTPException:
                logger.exception(
                    "No se pudo enviar respuesta personalizada en canal %s",
                    message.channel.id,
                )
                return
        except discord.HTTPException:
            logger.exception(
                "No se pudo responder con mensaje personalizado en canal %s",
                message.channel.id,
            )
            return

        delete_after = settings.custom_msg_delete_after
        if sent is not None and delete_after > 0:
            try:
                await sent.delete(delay=delete_after)
            except discord.HTTPException:
                logger.debug(
                    "No se pudo programar el borrado del mensaje %s", sent.id
                )

    # --- Grupo /mensajes --------------------------------------------------

    mensajes_group = app_commands.Group(
        name="mensajes",
        description="Gestiona los mensajes personalizados",
    )

    @mensajes_group.command(
        name="estado", description="Muestra el estado del sistema de mensajes"
    )
    async def estado(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        total_users = len(store.users)
        total_msgs = sum(len(u.messages) for u in store.users.values())
        estado_global = "activado" if store.enabled else "desactivado"
        await interaction.response.send_message(
            f"Interruptor global: **{estado_global}**\n"
            f"Usuarios con mensajes: **{total_users}**\n"
            f"Mensajes totales: **{total_msgs}**",
            ephemeral=True,
        )

    @mensajes_group.command(
        name="activar", description="Activa el interruptor global"
    )
    async def activar(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        await store.set_global_enabled(True)
        await interaction.response.send_message(
            "Interruptor global **activado**.", ephemeral=True
        )

    @mensajes_group.command(
        name="desactivar", description="Desactiva el interruptor global"
    )
    async def desactivar(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        await store.set_global_enabled(False)
        await interaction.response.send_message(
            "Interruptor global **desactivado**.", ephemeral=True
        )

    @mensajes_group.command(
        name="agregar", description="Agrega un mensaje a un usuario"
    )
    @app_commands.describe(usuario="Usuario destino", texto="Texto del mensaje")
    async def agregar(
        self,
        interaction: discord.Interaction,
        usuario: discord.User,
        texto: str,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        texto = texto.strip()
        if not texto:
            await interaction.response.send_message(
                "El texto no puede estar vacío.", ephemeral=True
            )
            return
        if len(texto) > MAX_MESSAGE_LENGTH:
            await interaction.response.send_message(
                f"El texto supera el máximo de {MAX_MESSAGE_LENGTH} caracteres.",
                ephemeral=True,
            )
            return
        um = store.get_user(usuario.id)
        if um is not None and len(um.messages) >= MAX_MESSAGES_PER_USER:
            await interaction.response.send_message(
                f"{usuario.mention} ya tiene el máximo de "
                f"{MAX_MESSAGES_PER_USER} mensajes.",
                ephemeral=True,
            )
            return
        total = await store.add_message(usuario.id, texto)
        await interaction.response.send_message(
            f"Mensaje agregado a {usuario.mention} (total: {total}).",
            ephemeral=True,
        )

    @mensajes_group.command(
        name="quitar", description="Quita un mensaje de un usuario por número"
    )
    @app_commands.describe(
        usuario="Usuario destino", numero="Número del mensaje (1-based)"
    )
    async def quitar(
        self,
        interaction: discord.Interaction,
        usuario: discord.User,
        numero: int,
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        removed = await store.remove_message(usuario.id, numero)
        if removed is None:
            await interaction.response.send_message(
                f"No existe el mensaje #{numero} para {usuario.mention}.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Mensaje #{numero} eliminado de {usuario.mention}.",
            ephemeral=True,
        )

    @mensajes_group.command(
        name="listar", description="Lista los mensajes de un usuario"
    )
    @app_commands.describe(usuario="Usuario a consultar")
    async def listar(
        self, interaction: discord.Interaction, usuario: discord.User
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        um = store.get_user(usuario.id)
        if um is None or not um.messages:
            await interaction.response.send_message(
                f"{usuario.mention} no tiene mensajes configurados.",
                ephemeral=True,
            )
            return
        estado = "activado" if um.enabled else "desactivado"
        lineas = [f"Mensajes de {usuario.mention} (interruptor: {estado}):"]
        for i, msg in enumerate(um.messages, start=1):
            lineas.append(f"{i}. {msg}")
        texto = "\n".join(lineas)
        if len(texto) > 1900:
            texto = texto[:1900] + "\n…"
        await interaction.response.send_message(texto, ephemeral=True)

    @mensajes_group.command(
        name="usuario-activar",
        description="Activa los mensajes de un usuario",
    )
    @app_commands.describe(usuario="Usuario destino")
    async def usuario_activar(
        self, interaction: discord.Interaction, usuario: discord.User
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        await store.set_user_enabled(usuario.id, True)
        await interaction.response.send_message(
            f"Mensajes de {usuario.mention} **activados**.", ephemeral=True
        )

    @mensajes_group.command(
        name="usuario-desactivar",
        description="Desactiva los mensajes de un usuario",
    )
    @app_commands.describe(usuario="Usuario destino")
    async def usuario_desactivar(
        self, interaction: discord.Interaction, usuario: discord.User
    ) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        await store.set_user_enabled(usuario.id, False)
        await interaction.response.send_message(
            f"Mensajes de {usuario.mention} **desactivados**.", ephemeral=True
        )

    @mensajes_group.command(
        name="silenciar",
        description="Activa o desactiva tus propios mensajes personalizados",
    )
    @app_commands.describe(activar="True para activar, False para silenciar")
    async def silenciar(
        self, interaction: discord.Interaction, activar: bool
    ) -> None:
        await store.set_user_enabled(interaction.user.id, activar)
        estado = "activados" if activar else "silenciados"
        await interaction.response.send_message(
            f"Tus mensajes personalizados quedaron **{estado}**.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Mensajes(bot))
