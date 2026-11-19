"""Almacén persistente de mensajes personalizados por usuario.

Estructura del archivo data/mensajes.json:
    {
        "enabled": true,
        "users": {
            "<id>": {"enabled": true, "messages": ["..."], "last_index": -1}
        }
    }

- Carga en memoria al iniciar; escribe solo cuando hay cambios.
- Escritura atómica (archivo temporal + os.replace) bajo asyncio.Lock.
- Todo el I/O se ejecuta en asyncio.to_thread.
"""
import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("data") / "mensajes.json"


@dataclass
class UserMessages:
    """Mensajes de un usuario y estado del interruptor individual."""

    enabled: bool = True
    messages: list[str] = field(default_factory=list)
    last_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "messages": list(self.messages),
            "last_index": self.last_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserMessages":
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        clean = [str(m) for m in messages if isinstance(m, str)]
        try:
            last_index = int(data.get("last_index", -1))
        except (TypeError, ValueError):
            last_index = -1
        return cls(
            enabled=bool(data.get("enabled", True)),
            messages=clean,
            last_index=last_index,
        )


class MensajesStore:
    """Almacén en memoria con persistencia atómica en JSON."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH
        self.enabled: bool = True
        self.users: dict[int, UserMessages] = {}
        self._lock = asyncio.Lock()

    # --- Carga / guardado -------------------------------------------------

    async def load(self) -> None:
        """Carga el archivo desde disco (si existe). No falla si está corrupto."""
        try:
            data = await asyncio.to_thread(self._read_sync)
        except Exception:
            logger.exception(
                "No se pudo leer %s; se arranca con almacén vacío.", self.path
            )
            return
        if data is None:
            return
        self.enabled = bool(data.get("enabled", True))
        users_raw = data.get("users", {})
        if not isinstance(users_raw, dict):
            logger.warning(
                "Campo 'users' inválido en %s; se ignora.", self.path
            )
            return
        parsed: dict[int, UserMessages] = {}
        for key, value in users_raw.items():
            try:
                user_id = int(key)
            except (TypeError, ValueError):
                logger.warning("ID de usuario inválido en %s: %r", self.path, key)
                continue
            if not isinstance(value, dict):
                continue
            parsed[user_id] = UserMessages.from_dict(value)
        self.users = parsed
        logger.info(
            "Mensajes personalizados cargados: enabled=%s, usuarios=%d",
            self.enabled,
            len(self.users),
        )

    def _read_sync(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    async def _save(self) -> None:
        """Persiste el estado actual de forma atómica."""
        payload = {
            "enabled": self.enabled,
            "users": {
                str(uid): um.to_dict() for uid, um in self.users.items()
            },
        }
        try:
            await asyncio.to_thread(self._write_sync, payload)
        except Exception:
            logger.exception("No se pudo guardar %s", self.path)

    def _write_sync(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".mensajes-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # --- Consultas --------------------------------------------------------

    def get_user(self, user_id: int) -> UserMessages | None:
        return self.users.get(user_id)

    def is_active(self, user_id: int) -> bool:
        """True si el interruptor global y el del usuario están activos."""
        if not self.enabled:
            return False
        um = self.users.get(user_id)
        return bool(um and um.enabled and um.messages)

    # --- Mutaciones -------------------------------------------------------

    async def set_global_enabled(self, enabled: bool) -> None:
        async with self._lock:
            self.enabled = enabled
            await self._save()

    async def set_user_enabled(self, user_id: int, enabled: bool) -> None:
        async with self._lock:
            um = self.users.get(user_id)
            if um is None:
                um = UserMessages(enabled=enabled)
                self.users[user_id] = um
            else:
                um.enabled = enabled
            await self._save()

    async def add_message(self, user_id: int, text: str) -> int:
        """Agrega un mensaje y devuelve el nuevo total."""
        async with self._lock:
            um = self.users.get(user_id)
            if um is None:
                um = UserMessages()
                self.users[user_id] = um
            um.messages.append(text)
            await self._save()
            return len(um.messages)

    async def remove_message(self, user_id: int, index: int) -> str | None:
        """Elimina el mensaje en `index` (1-based). Devuelve el texto o None."""
        async with self._lock:
            um = self.users.get(user_id)
            if um is None or not (1 <= index <= len(um.messages)):
                return None
            removed = um.messages.pop(index - 1)
            if um.last_index >= len(um.messages):
                um.last_index = -1
            await self._save()
            return removed

    async def pick_message(self, user_id: int) -> str | None:
        """Elige un mensaje al azar evitando repetir el anterior si hay >1."""
        async with self._lock:
            um = self.users.get(user_id)
            if um is None or not um.messages:
                return None
            import random

            total = len(um.messages)
            if total == 1:
                idx = 0
            else:
                candidates = [i for i in range(total) if i != um.last_index]
                idx = random.choice(candidates)
            um.last_index = idx
            await self._save()
            return um.messages[idx]


# Instancia compartida por el cog.
store = MensajesStore()
