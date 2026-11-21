"""Mensajes personalizados por usuario con interruptor global e individual."""
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import settings
from utils.mensajes_store import store
from utils.permisos import admin_only, es_admin

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 300
MAX_MESSAGES_PER_USER = 20
PANEL_TIMEOUT_S = 180
PANEL_PAGE_SIZE = 25
PANEL_LABEL_MAX = 90

# Cooldown en memoria por usuario (monotonic).
_last_reply: dict[int, float] = {}


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
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @mensajes_group.command(
        name="estado", description="Muestra el estado del sistema de mensajes"
    )
    @admin_only()
    async def estado(self, interaction: discord.Interaction) -> None:
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
    @admin_only()
    async def activar(self, interaction: discord.Interaction) -> None:
        await store.set_global_enabled(True)
        await interaction.response.send_message(
            "Interruptor global **activado**.", ephemeral=True
        )

    @mensajes_group.command(
        name="desactivar", description="Desactiva el interruptor global"
    )
    @admin_only()
    async def desactivar(self, interaction: discord.Interaction) -> None:
        await store.set_global_enabled(False)
        await interaction.response.send_message(
            "Interruptor global **desactivado**.", ephemeral=True
        )

    @mensajes_group.command(
        name="agregar", description="Agrega un mensaje a un usuario"
    )
    @app_commands.describe(usuario="Usuario destino", texto="Texto del mensaje")
    @admin_only()
    async def agregar(
        self,
        interaction: discord.Interaction,
        usuario: discord.User,
        texto: str,
    ) -> None:
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
    @admin_only()
    async def quitar(
        self,
        interaction: discord.Interaction,
        usuario: discord.User,
        numero: int,
    ) -> None:
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
    @admin_only()
    async def listar(
        self, interaction: discord.Interaction, usuario: discord.User
    ) -> None:
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
    @admin_only()
    async def usuario_activar(
        self, interaction: discord.Interaction, usuario: discord.User
    ) -> None:
        await store.set_user_enabled(usuario.id, True)
        await interaction.response.send_message(
            f"Mensajes de {usuario.mention} **activados**.", ephemeral=True
        )

    @mensajes_group.command(
        name="usuario-desactivar",
        description="Desactiva los mensajes de un usuario",
    )
    @app_commands.describe(usuario="Usuario destino")
    @admin_only()
    async def usuario_desactivar(
        self, interaction: discord.Interaction, usuario: discord.User
    ) -> None:
        await store.set_user_enabled(usuario.id, False)
        await interaction.response.send_message(
            f"Mensajes de {usuario.mention} **desactivados**.", ephemeral=True
        )

    @mensajes_group.command(
        name="silenciar",
        description="Activa o desactiva tus propios mensajes personalizados",
    )
    @app_commands.describe(activar="True para activar, False para silenciar")
    @admin_only()
    async def silenciar(
        self, interaction: discord.Interaction, activar: bool
    ) -> None:
        await store.set_user_enabled(interaction.user.id, activar)
        estado = "activados" if activar else "silenciados"
        await interaction.response.send_message(
            f"Tus mensajes personalizados quedaron **{estado}**.",
            ephemeral=True,
        )

    @mensajes_group.command(
        name="panel",
        description="Panel interactivo para gestionar mensajes de un usuario",
    )
    @app_commands.describe(usuario="Usuario cuyos mensajes se gestionan")
    @admin_only()
    async def panel(
        self, interaction: discord.Interaction, usuario: discord.User
    ) -> None:
        view = MensajesPanelView(usuario.id, interaction.user.id)
        await interaction.response.send_message(
            view.render_text(), view=view, ephemeral=True
        )


def _short_label(text: str, index: int) -> str:
    """Etiqueta corta para el Select: '#N ' + texto recortado."""
    clean = " ".join(text.split())
    if not clean:
        return f"#{index} (emoji/adjunto)"
    if len(clean) > PANEL_LABEL_MAX:
        clean = clean[: PANEL_LABEL_MAX - 1] + "…"
    return f"#{index} {clean}"


class _AddMessageModal(discord.ui.Modal, title="Agregar mensaje"):
    texto: discord.ui.TextInput = discord.ui.TextInput(
        label="Texto del mensaje",
        style=discord.TextStyle.paragraph,
        max_length=1500,
        required=True,
    )

    def __init__(self, panel: "MensajesPanelView") -> None:
        super().__init__()
        self.panel = panel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not es_admin(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Solo los administradores pueden hacer eso.",
                ephemeral=True,
            )
            return False
        return True

    async def on_submit(self, interaction: discord.Interaction) -> None:
        texto = str(self.texto.value).strip()
        if not texto:
            await interaction.response.send_message(
                "El texto no puede estar vacío.", ephemeral=True
            )
            return
        um = store.get_user(self.panel.user_id)
        if um is not None and len(um.messages) >= MAX_MESSAGES_PER_USER:
            await interaction.response.send_message(
                f"El usuario ya tiene el máximo de "
                f"{MAX_MESSAGES_PER_USER} mensajes.",
                ephemeral=True,
            )
            return
        await store.add_message(self.panel.user_id, texto)
        await self.panel.refresh(interaction)


class _RemoveSelect(discord.ui.Select):
    def __init__(self, panel: "MensajesPanelView") -> None:
        self.panel = panel
        options = panel.build_options()
        super().__init__(
            placeholder="Quitar mensajes…",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
            disabled=not options,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not es_admin(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Solo los administradores pueden hacer eso.",
                ephemeral=True,
            )
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        indices = sorted((int(v) for v in self.values), reverse=True)
        for idx in indices:
            await store.remove_message(self.panel.user_id, idx)
        await self.panel.refresh(interaction)


class MensajesPanelView(discord.ui.View):
    """Panel interactivo para gestionar los mensajes de un usuario."""

    def __init__(self, user_id: int, author_id: int) -> None:
        super().__init__(timeout=PANEL_TIMEOUT_S)
        self.user_id = user_id
        self.author_id = author_id
        self.page = 0
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Este panel no es tuyo.", ephemeral=True
            )
            return False
        if not es_admin(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Solo los administradores pueden hacer eso.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

    def _messages(self) -> list[str]:
        um = store.get_user(self.user_id)
        return list(um.messages) if um else []

    def _total_pages(self) -> int:
        total = len(self._messages())
        return max(1, (total + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE)

    def build_options(self) -> list[discord.SelectOption]:
        messages = self._messages()
        start = self.page * PANEL_PAGE_SIZE
        end = start + PANEL_PAGE_SIZE
        options: list[discord.SelectOption] = []
        for offset, text in enumerate(messages[start:end]):
            index = start + offset + 1
            options.append(
                discord.SelectOption(
                    label=_short_label(text, index),
                    value=str(index),
                )
            )
        return options

    def render_text(self) -> str:
        um = store.get_user(self.user_id)
        estado = "activado" if (um and um.enabled) else "desactivado"
        total = len(um.messages) if um else 0
        return (
            f"<@{self.user_id}>\n"
            f"Interruptor: **{estado}**\n"
            f"Mensajes: **{total}**"
        )

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(_AddButton(self))
        self.add_item(_RemoveSelect(self))
        self.add_item(_ToggleButton(self))
        total_pages = self._total_pages()
        if total_pages > 1:
            self.add_item(_PrevButton(self, disabled=self.page <= 0))
            self.add_item(_NextButton(self, disabled=self.page >= total_pages - 1))

    async def refresh(self, interaction: discord.Interaction) -> None:
        total_pages = self._total_pages()
        if self.page >= total_pages:
            self.page = total_pages - 1
        self._rebuild()
        await interaction.response.edit_message(
            content=self.render_text(), view=self
        )


class _AddButton(discord.ui.Button):
    def __init__(self, panel: MensajesPanelView) -> None:
        super().__init__(label="Agregar", style=discord.ButtonStyle.primary)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_AddMessageModal(self.panel))


class _ToggleButton(discord.ui.Button):
    def __init__(self, panel: MensajesPanelView) -> None:
        um = store.get_user(panel.user_id)
        activo = bool(um and um.enabled)
        label = "Desactivar" if activo else "Activar"
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        um = store.get_user(self.panel.user_id)
        nuevo = not bool(um and um.enabled)
        await store.set_user_enabled(self.panel.user_id, nuevo)
        await self.panel.refresh(interaction)


class _PrevButton(discord.ui.Button):
    def __init__(self, panel: MensajesPanelView, disabled: bool) -> None:
        super().__init__(label="Anterior", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel.page = max(0, self.panel.page - 1)
        await self.panel.refresh(interaction)


class _NextButton(discord.ui.Button):
    def __init__(self, panel: MensajesPanelView, disabled: bool) -> None:
        super().__init__(label="Siguiente", style=discord.ButtonStyle.secondary, disabled=disabled)
        self.panel = panel

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel.page = min(self.panel._total_pages() - 1, self.panel.page + 1)
        await self.panel.refresh(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Mensajes(bot))
