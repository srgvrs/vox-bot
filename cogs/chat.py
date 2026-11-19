"""Cog del chatbot: /preguntar y respuestas a menciones."""
import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ai.client import AIChain, AIError, AI_UNAVAILABLE_MESSAGE
from config import settings
from media.downloader import _host_allowed
from utils.chat_store import store
from utils.errors import UNEXPECTED_ERROR_MESSAGE, new_error_ref, report_error

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_MAX_OUTPUT_CHARS = 1900
_MEMORY_TURNS = 6
_MEMORY_TTL_S = 15 * 60

# Cooldown por usuario (monotonic).
_last_ask: dict[int, float] = {}
# Contador diario global (UTC).
_daily_count: dict[str, int] = {}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _check_daily() -> bool:
    today = _today()
    if _daily_count.get("date") != today:
        _daily_count.clear()
        _daily_count["date"] = today
        _daily_count["count"] = 0
    return int(_daily_count.get("count", 0)) < settings.chat_daily_limit


def _bump_daily() -> None:
    _daily_count["count"] = int(_daily_count.get("count", 0)) + 1


def _is_admin(interaction: discord.Interaction) -> bool:
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


def _channel_allowed(channel_id: int) -> bool:
    if not settings.chat_channel_ids:
        return True
    return channel_id in settings.chat_channel_ids


def _has_video_url(text: str) -> bool:
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}\"'")
        try:
            from urllib.parse import urlparse

            host = urlparse(url).hostname
        except ValueError:
            continue
        if host and _host_allowed(host):
            return True
    return False


@dataclass
class _Turn:
    role: str
    content: str


@dataclass
class _Memory:
    turns: deque[_Turn] = field(default_factory=lambda: deque(maxlen=_MEMORY_TURNS * 2))
    last_used: float = field(default_factory=time.monotonic)


_memories: dict[tuple[int, int], _Memory] = {}


def _get_memory(channel_id: int, user_id: int) -> _Memory:
    key = (channel_id, user_id)
    mem = _memories.get(key)
    now = time.monotonic()
    if mem is not None and now - mem.last_used > _MEMORY_TTL_S:
        _memories.pop(key, None)
        mem = None
    if mem is None:
        mem = _Memory()
        _memories[key] = mem
    mem.last_used = now
    return mem


def _prune_memories() -> None:
    now = time.monotonic()
    expired = [k for k, m in _memories.items() if now - m.last_used > _MEMORY_TTL_S]
    for k in expired:
        _memories.pop(k, None)


def _build_messages(memory: _Memory, user_text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": settings.chat_system_prompt}
    ]
    for turn in memory.turns:
        messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": user_text})
    return messages


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[: _MAX_OUTPUT_CHARS - 1] + "…"


class Chat(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.chain = AIChain()

    async def cog_load(self) -> None:
        await store.load()
        self.session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    # --- Listener de menciones -------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._handle_mention(message)
        except Exception:
            logger.exception(
                "Error procesando mención para el chatbot (canal=%s autor=%s)",
                message.channel.id if message.channel else "?",
                message.author.id if message.author else "?",
            )

    async def _handle_mention(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        if not _guild_allowed(message.guild):
            return
        if not _channel_allowed(message.channel.id):
            return
        if not store.enabled:
            return
        if self.bot.user is None or self.bot.user not in message.mentions:
            return
        if _has_video_url(message.content):
            return

        text = message.content
        for mention in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            text = text.replace(mention, " ")
        text = text.strip()
        if not text:
            return
        if len(text) > settings.chat_max_input_chars:
            await message.reply(
                f"Tu mensaje supera el máximo de "
                f"{settings.chat_max_input_chars} caracteres.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        remaining = self._cooldown_remaining(message.author.id)
        if remaining > 0:
            await message.reply(
                f"Espera {remaining} s antes de volver a preguntar.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not _check_daily():
            await message.reply(
                "Se alcanzó el límite diario de consultas. Intenta mañana.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        _last_ask[message.author.id] = time.monotonic()
        _bump_daily()
        _prune_memories()

        memory = _get_memory(message.channel.id, message.author.id)
        messages = _build_messages(memory, text)
        try:
            result = await self._ask(messages)
        except AIError:
            await message.reply(
                AI_UNAVAILABLE_MESSAGE,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except Exception as exc:
            ref = new_error_ref()
            logger.exception(
                "Error inesperado en chatbot (ref=%s) usuario=%s canal=%s",
                ref,
                message.author.id,
                message.channel.id,
            )
            await report_error(f"chat ref={ref}", exc)
            await message.reply(
                UNEXPECTED_ERROR_MESSAGE.format(ref=ref),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        memory.turns.append(_Turn(role="user", content=text))
        memory.turns.append(_Turn(role="assistant", content=result.text))
        memory.last_used = time.monotonic()

        await message.reply(
            _truncate(result.text),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- Comando /preguntar ----------------------------------------------

    @app_commands.command(
        name="preguntar", description="Hazle una pregunta al chatbot"
    )
    @app_commands.describe(texto="Tu pregunta")
    async def preguntar(
        self, interaction: discord.Interaction, texto: str
    ) -> None:
        if not _guild_allowed(interaction.guild):
            await interaction.response.send_message(
                "Este bot solo funciona en servidores autorizados.",
                ephemeral=True,
            )
            return
        if interaction.channel_id is None or not _channel_allowed(
            interaction.channel_id
        ):
            await interaction.response.send_message(
                "El chatbot no está habilitado en este canal.", ephemeral=True
            )
            return
        if not store.enabled:
            await interaction.response.send_message(
                "El chatbot está desactivado.", ephemeral=True
            )
            return
        texto = texto.strip()
        if not texto:
            await interaction.response.send_message(
                "Escribe una pregunta.", ephemeral=True
            )
            return
        if len(texto) > settings.chat_max_input_chars:
            await interaction.response.send_message(
                f"Tu pregunta supera el máximo de "
                f"{settings.chat_max_input_chars} caracteres.",
                ephemeral=True,
            )
            return

        remaining = self._cooldown_remaining(interaction.user.id)
        if remaining > 0:
            await interaction.response.send_message(
                f"Espera {remaining} s antes de volver a preguntar.",
                ephemeral=True,
            )
            return
        if not _check_daily():
            await interaction.response.send_message(
                "Se alcanzó el límite diario de consultas. Intenta mañana.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        _last_ask[interaction.user.id] = time.monotonic()
        _bump_daily()
        _prune_memories()

        memory = _get_memory(interaction.channel_id, interaction.user.id)
        messages = _build_messages(memory, texto)
        try:
            result = await self._ask(messages)
        except AIError:
            await interaction.followup.send(
                AI_UNAVAILABLE_MESSAGE, ephemeral=True
            )
            return
        except Exception as exc:
            ref = new_error_ref()
            logger.exception(
                "Error inesperado en /preguntar (ref=%s) usuario=%s canal=%s",
                ref,
                interaction.user.id,
                interaction.channel_id,
            )
            await report_error(f"chat ref={ref}", exc)
            await interaction.followup.send(
                UNEXPECTED_ERROR_MESSAGE.format(ref=ref), ephemeral=True
            )
            return

        memory.turns.append(_Turn(role="user", content=texto))
        memory.turns.append(_Turn(role="assistant", content=result.text))
        memory.last_used = time.monotonic()

        await interaction.followup.send(
            _truncate(result.text),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _ask(self, messages: list[dict[str, str]]):
        if self.session is None:
            raise AIError("Sesión no inicializada")
        return await asyncio.wait_for(
            self.chain.ask(messages, self.session),
            timeout=settings.ai_timeout_seconds * 2 + 5,
        )

    @staticmethod
    def _cooldown_remaining(user_id: int) -> int:
        cooldown = settings.chat_cooldown_seconds
        if cooldown <= 0:
            return 0
        last = _last_ask.get(user_id, 0.0)
        elapsed = time.monotonic() - last
        if elapsed >= cooldown:
            return 0
        return int(cooldown - elapsed) + 1

    # --- Grupo /chat ------------------------------------------------------

    chat_group = app_commands.Group(
        name="chat", description="Gestiona el chatbot"
    )

    @chat_group.command(name="activar", description="Activa el chatbot")
    async def activar(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        await store.set_enabled(True)
        await interaction.response.send_message(
            "Chatbot **activado**.", ephemeral=True
        )

    @chat_group.command(name="desactivar", description="Desactiva el chatbot")
    async def desactivar(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        await store.set_enabled(False)
        await interaction.response.send_message(
            "Chatbot **desactivado**.", ephemeral=True
        )

    @chat_group.command(name="estado", description="Estado del chatbot")
    async def estado(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(
                "Solo administradores pueden usar este comando.",
                ephemeral=True,
            )
            return
        status = self.chain.status()
        estado_global = "activado" if store.enabled else "desactivado"
        last = (
            f"{status['last_provider']}:{status['last_model']}"
            if status["last_provider"]
            else "ninguno todavía"
        )
        paused = ", ".join(status["paused"]) or "ninguno"
        disabled = ", ".join(status["disabled"]) or "ninguno"
        await interaction.response.send_message(
            f"Interruptor global: **{estado_global}**\n"
            f"Último proveedor que respondió: **{last}**\n"
            f"En pausa: {paused}\n"
            f"Desactivados: {disabled}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Chat(bot))
