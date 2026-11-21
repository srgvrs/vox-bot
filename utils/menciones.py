"""Enrutador único de menciones al bot.

Devuelve una de estas rutas:
- "vacia": mención sin texto ni URL.
- "descarga": contiene al menos una URL http/https.
- "toggle_mensajes": frase de activar/desactivar mensajes.
- "chat": todo lo demás.

Orden de evaluación: vacia, descarga, toggle_mensajes, chat.
"""
import re
import unicodedata

import discord

_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

_TOGGLE_VERBS = (
    "activa",
    "activar",
    "enciende",
    "desactiva",
    "desactivar",
    "apaga",
)
_TOGGLE_RE = re.compile(
    r"^(?:" + "|".join(_TOGGLE_VERBS) + r")\s+(?:los\s+)?mensajes\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Minúsculas y sin acentos, para comparar frases."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _strip_bot_mentions(text: str, bot_user: discord.abc.User | None) -> str:
    if bot_user is not None:
        for mention in (f"<@{bot_user.id}>", f"<@!{bot_user.id}>"):
            text = text.replace(mention, " ")
    return text


def clasificar_mencion(
    message: discord.Message, bot_user: discord.abc.User | None
) -> str:
    """Clasifica una mención al bot en una de las rutas conocidas."""
    content = message.content or ""
    text_only = _strip_bot_mentions(content, bot_user).strip()

    if not text_only and not _URL_RE.search(content):
        return "vacia"

    if _URL_RE.search(content):
        return "descarga"

    norm = _normalize(text_only)
    if _TOGGLE_RE.match(norm):
        return "toggle_mensajes"

    return "chat"
