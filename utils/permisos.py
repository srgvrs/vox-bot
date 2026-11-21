"""Utilidades de permisos: verificación de administrador y decorador."""
import logging
from typing import Any, Callable, TypeVar

import discord
from discord import app_commands

from config import settings

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])


def es_admin(user: Any, guild: discord.Guild | None = None) -> bool:
    """True si el usuario es admin configurado o tiene permiso de administrador.

    - IDs en ADMIN_USER_IDS (mismo parseo que /estado).
    - Miembros con guild_permissions.administrator.
    """
    user_id = getattr(user, "id", None)
    if user_id is not None and user_id in settings.admin_user_ids:
        return True
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return True
    return False


def admin_only() -> Callable[[_F], _F]:
    """Decorador de app_commands que exige ser administrador."""

    async def _predicate(interaction: discord.Interaction) -> bool:
        return es_admin(interaction.user, interaction.guild)

    def decorator(func: _F) -> _F:
        return app_commands.check(_predicate)(func)  # type: ignore[return-value]

    return decorator
