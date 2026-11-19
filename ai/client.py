"""Cliente async para APIs compatibles con OpenAI Chat Completions.

- Sin dependencias nuevas: usa aiohttp.
- AIChain prueba entradas en orden, con circuit breaker por entrada.
- 429: pausa según Retry-After (o 60 s) y pasa a la siguiente.
- 401/403: desactiva la entrada hasta reiniciar.
- Timeout/5xx: un reintento y luego la siguiente.
- No registra el contenido de los mensajes.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

# Mensaje genérico cuando todos los proveedores fallan.
AI_UNAVAILABLE_MESSAGE = "Ahora mismo no puedo responder, intenta en un rato"

_DEFAULT_RETRY_AFTER_S = 60
_CIRCUIT_FAILURES = 3
_CIRCUIT_SECONDS = 300

# Nombres legibles para mostrar en el pie de los embeds.
_PROVIDER_LABELS = {
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
}


class AIError(Exception):
    """Error genérico del cliente de IA."""


@dataclass
class AIEntry:
    """Una entrada de proveedor+modelo configurada."""

    provider: str
    base_url: str
    api_key: str
    model: str
    # Estado del circuit breaker.
    failures: int = 0
    skip_until: float = 0.0
    disabled: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class AIResult:
    """Resultado de una consulta a la cadena."""

    text: str
    provider: str
    model: str
    elapsed_s: float
    provider_label: str = ""


@dataclass
class AIChain:
    """Cadena de proveedores con circuit breaker y fallback."""

    entries: list[AIEntry] = field(default_factory=list)
    last_provider: str | None = None
    last_model: str | None = None

    def __post_init__(self) -> None:
        if not self.entries:
            self.entries = _build_entries()

    # --- Estado -----------------------------------------------------------

    def _is_skipped(self, entry: AIEntry) -> bool:
        if entry.disabled:
            return True
        return time.monotonic() < entry.skip_until

    def _record_failure(self, entry: AIEntry) -> None:
        entry.failures += 1
        if entry.failures >= _CIRCUIT_FAILURES:
            entry.skip_until = time.monotonic() + _CIRCUIT_SECONDS
            logger.warning(
                "IA: %s en pausa %ss tras %d fallos",
                entry.label,
                _CIRCUIT_SECONDS,
                entry.failures,
            )

    def _record_success(self, entry: AIEntry) -> None:
        entry.failures = 0
        entry.skip_until = 0.0

    def _disable(self, entry: AIEntry, reason: str) -> None:
        entry.disabled = True
        logger.warning("IA: %s desactivado hasta reiniciar (%s)", entry.label, reason)

    def status(self) -> dict[str, Any]:
        """Resumen para /chat estado."""
        now = time.monotonic()
        paused: list[str] = []
        disabled: list[str] = []
        for entry in self.entries:
            if entry.disabled:
                disabled.append(entry.label)
            elif entry.skip_until > now:
                paused.append(f"{entry.label} ({int(entry.skip_until - now)}s)")
        return {
            "last_provider": self.last_provider,
            "last_model": self.last_model,
            "paused": paused,
            "disabled": disabled,
        }

    # --- Consulta ---------------------------------------------------------

    async def ask(
        self,
        messages: list[dict[str, str]],
        session: aiohttp.ClientSession,
    ) -> AIResult:
        """Prueba las entradas en orden. Lanza AIError si todas fallan."""
        if not self.entries:
            raise AIError("No hay proveedores de IA configurados.")

        for entry in self.entries:
            if self._is_skipped(entry):
                continue
            try:
                result = await self._ask_entry(entry, messages, session)
            except _PermanentError as exc:
                self._disable(entry, str(exc))
                continue
            except _RateLimited as exc:
                wait = exc.retry_after or _DEFAULT_RETRY_AFTER_S
                entry.skip_until = time.monotonic() + wait
                logger.warning(
                    "IA: %s limitado (429); pausa %ss", entry.label, wait
                )
                continue
            except _TransientError as exc:
                logger.warning("IA: %s fallo transitorio: %s", entry.label, exc)
                self._record_failure(entry)
                continue
            self._record_success(entry)
            self.last_provider = entry.provider
            self.last_model = entry.model
            logger.info(
                "IA: respondió %s en %.2fs", entry.label, result.elapsed_s
            )
            return result

        raise AIError(AI_UNAVAILABLE_MESSAGE)

    async def _ask_entry(
        self,
        entry: AIEntry,
        messages: list[dict[str, str]],
        session: aiohttp.ClientSession,
    ) -> AIResult:
        """Una llamada con un reintento ante timeout/5xx."""
        last_exc: Exception | None = None
        for attempt in range(2):
            start = time.monotonic()
            try:
                text = await _post_chat(entry, messages, session)
            except _PermanentError:
                raise
            except _RateLimited:
                raise
            except _TransientError as exc:
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                raise
            return AIResult(
                text=text,
                provider=entry.provider,
                model=entry.model,
                elapsed_s=time.monotonic() - start,
                provider_label=_PROVIDER_LABELS.get(
                    entry.provider, entry.provider
                ),
            )
        raise _TransientError(str(last_exc) if last_exc else "fallo desconocido")


class _PermanentError(Exception):
    """401/403: no reintentar, desactivar."""


class _RateLimited(Exception):
    """429: pausar según Retry-After."""

    def __init__(self, retry_after: int | None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class _TransientError(Exception):
    """Timeout, 5xx o error de red."""


async def _post_chat(
    entry: AIEntry,
    messages: list[dict[str, str]],
    session: aiohttp.ClientSession,
) -> str:
    url = entry.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": entry.model,
        "messages": messages,
        "max_tokens": settings.ai_max_tokens,
        "temperature": 0.6,
    }
    headers = {
        "Authorization": f"Bearer {entry.api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=settings.ai_timeout_seconds)
    try:
        async with session.post(
            url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            if resp.status in (401, 403):
                raise _PermanentError(f"HTTP {resp.status}")
            if resp.status == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                raise _RateLimited(retry_after)
            if resp.status >= 500:
                raise _TransientError(f"HTTP {resp.status}")
            if resp.status >= 400:
                body = await resp.text()
                raise _TransientError(f"HTTP {resp.status}: {body[:200]}")
            data = await resp.json()
    except asyncio.TimeoutError as exc:
        raise _TransientError("timeout") from exc
    except aiohttp.ClientError as exc:
        raise _TransientError(f"red: {exc}") from exc

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise _TransientError("respuesta inválida") from exc


def _parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(1, int(float(value)))
    except ValueError:
        return None


def _build_entries() -> list[AIEntry]:
    """Construye las entradas a partir de la configuración."""
    entries: list[AIEntry] = []
    for raw in settings.ai_providers:
        provider = raw.strip().lower()
        if not provider:
            continue
        if provider == "groq":
            if not settings.groq_api_key:
                continue
            entries.append(
                AIEntry(
                    provider="groq",
                    base_url=settings.groq_base_url,
                    api_key=settings.groq_api_key,
                    model=settings.groq_model,
                )
            )
        elif provider == "openrouter":
            if not settings.openrouter_api_key:
                continue
            entries.append(
                AIEntry(
                    provider="openrouter",
                    base_url=settings.openrouter_base_url,
                    api_key=settings.openrouter_api_key,
                    model=settings.openrouter_model,
                )
            )
        elif provider == "deepseek":
            if not settings.deepseek_api_key:
                continue
            entries.append(
                AIEntry(
                    provider="deepseek",
                    base_url=settings.deepseek_base_url,
                    api_key=settings.deepseek_api_key,
                    model=settings.deepseek_model,
                )
            )
        else:
            logger.warning("IA: proveedor desconocido en AI_PROVIDERS: %r", raw)
    return entries
