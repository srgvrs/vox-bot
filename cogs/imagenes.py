"""Cog de búsqueda de imágenes: /img (embed paginado + botones)."""
import asyncio
import logging
import random
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from config import settings
from media.validator import is_valid_image
from search.base import ImageResult
from search.chain import SearchChain
from search.ddg import DuckDuckGoProvider
from search.serper import SerperProvider
from utils.cache import TTLCache, normalize_query
from utils.errors import report_error

logger = logging.getLogger(__name__)

_MAX_SKIP_ATTEMPTS = 5


def _is_allowed(interaction: discord.Interaction) -> bool:
    """Permite solo en guilds autorizados; rechaza DMs."""
    if interaction.guild is None:
        return False
    if not settings.allowed_guild_ids:
        return True
    return interaction.guild.id in settings.allowed_guild_ids


class PageModal(discord.ui.Modal, title="Ir a página"):
    numero: discord.ui.TextInput = discord.ui.TextInput(
        label="Número de página",
        placeholder="Ej. 3",
        min_length=1,
        max_length=4,
    )

    def __init__(self, view: "ImagePaginator") -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            page = int(self.numero.value) - 1
        except ValueError:
            await interaction.response.send_message(
                "Escribe un número de página válido.", ephemeral=True
            )
            return
        if page < 0 or page >= len(self.view.results):
            await interaction.response.send_message(
                f"Esa página no existe. Rango válido: 1-{len(self.view.results)}.",
                ephemeral=True,
            )
            return
        await self.view.show_page(interaction, page)


class ImagePaginator(discord.ui.View):
    def __init__(
        self,
        session: aiohttp.ClientSession,
        results: list[ImageResult],
        consulta: str,
        author_id: int,
        provider_name: str,
    ) -> None:
        super().__init__(timeout=120)
        self.session = session
        self.results = results
        self.consulta = consulta
        self.author_id = author_id
        self.provider_name = provider_name
        self.index = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Solo quien invocó el comando puede usar estos botones.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

    def _build_embed(self, result: ImageResult | None) -> discord.Embed:
        total = len(self.results)
        if result is None:
            embed = discord.Embed(
                title=self.consulta,
                description="No encontré una imagen válida en esta búsqueda.",
            )
        else:
            embed = discord.Embed(title=result.title or self.consulta)
            embed.set_image(url=result.url)
        footer = f"Página {self.index + 1}/{total} · {self.provider_name}"
        if result is not None and result.source:
            footer = f"{footer} · {result.source}"
        embed.set_footer(text=footer)
        return embed

    async def _find_valid(self, start: int) -> tuple[int, ImageResult] | None:
        """Busca desde `start` hacia adelante hasta 5 imágenes válidas."""
        total = len(self.results)
        for offset in range(_MAX_SKIP_ATTEMPTS):
            idx = (start + offset) % total
            candidate = self.results[idx]
            if await is_valid_image(self.session, candidate.url):
                return idx, candidate
        return None

    async def show_page(self, interaction: discord.Interaction, page: int) -> None:
        found = await self._find_valid(page)
        if found is None:
            self.index = page
            embed = self._build_embed(None)
        else:
            self.index, result = found
            embed = self._build_embed(result)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Anterior", style=discord.ButtonStyle.secondary)
    async def anterior(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.show_page(interaction, (self.index - 1) % len(self.results))

    @discord.ui.button(label="Siguiente ▶", style=discord.ButtonStyle.secondary)
    async def siguiente(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.show_page(interaction, (self.index + 1) % len(self.results))

    @discord.ui.button(label="🎲 Aleatorio", style=discord.ButtonStyle.primary)
    async def aleatorio(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.show_page(interaction, random.randrange(len(self.results)))

    @discord.ui.button(label="Ir a página", style=discord.ButtonStyle.primary)
    async def ir_a_pagina(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(PageModal(self))

    @discord.ui.button(label="🗑 Borrar", style=discord.ButtonStyle.danger)
    async def borrar(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(content="Búsqueda cerrada.", embed=None, view=None)


class Imagenes(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        providers = []
        if settings.serper_api_key:
            providers.append(SerperProvider())
        providers.append(DuckDuckGoProvider())
        self.chain = SearchChain(providers)
        self.cache: TTLCache[tuple[list[ImageResult], str]] = TTLCache()

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    @app_commands.command(name="img", description="Busca imágenes")
    async def img(self, interaction: discord.Interaction, consulta: str) -> None:
        try:
            if not _is_allowed(interaction):
                await interaction.response.send_message(
                    "Este bot solo funciona en servidores autorizados.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer()
        except Exception:
            logger.exception("Error en /img antes de responder")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Error interno al procesar el comando.", ephemeral=True
                    )
            except Exception:
                logger.exception("No se pudo enviar el mensaje de error")
            return

        cache_key = normalize_query(consulta)
        cached = self.cache.get(cache_key)
        if cached is not None:
            results, provider_name = cached
        else:
            try:
                results, provider_name = await asyncio.wait_for(
                    self.chain.search(consulta, limit=100),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                logger.warning("Búsqueda de imágenes agotó el tiempo para %r", consulta)
                await interaction.followup.send(
                    "La búsqueda tardó demasiado. Intenta de nuevo en un momento.",
                    ephemeral=True,
                )
                return
            except Exception as exc:
                logger.exception("Fallo la búsqueda de imágenes para %r", consulta)
                try:
                    await asyncio.wait_for(report_error("img", exc), timeout=5)
                except Exception:
                    logger.exception("No se pudo reportar el error")
                await interaction.followup.send(
                    "No pude completar la búsqueda de imágenes. Intenta de nuevo en un momento.",
                    ephemeral=True,
                )
                return
            self.cache.set(cache_key, (results, provider_name))

        if not results:
            await interaction.followup.send(
                f"No encontré imágenes para '{consulta}'.",
                ephemeral=True,
            )
            return

        if self.session is None:
            logger.error("Sesión aiohttp no inicializada en /img")
            await interaction.followup.send(
                "El bot no está listo todavía. Intenta de nuevo en unos segundos.",
                ephemeral=True,
            )
            return
        view = ImagePaginator(
            self.session, results, consulta, interaction.user.id, provider_name
        )
        try:
            found = await asyncio.wait_for(view._find_valid(0), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("Validación de imágenes agotó el tiempo para %r", consulta)
            found = None
        if found is None:
            view.index = 0
            embed = view._build_embed(None)
        else:
            view.index, result = found
            embed = view._build_embed(result)
        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Imagenes(bot))
