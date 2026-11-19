"""Persistencia del interruptor global del chatbot (data/chat_state.json).

- Carga en memoria al iniciar; escribe solo cuando cambia.
- Escritura atómica (archivo temporal + os.replace) bajo asyncio.Lock.
- Todo el I/O se ejecuta en asyncio.to_thread.
"""
import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("data") / "chat_state.json"


class ChatStore:
    """Almacén en memoria con persistencia atómica en JSON."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH
        self.enabled: bool = True
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        try:
            data = await asyncio.to_thread(self._read_sync)
        except Exception:
            logger.exception(
                "No se pudo leer %s; se arranca con el chatbot activado.",
                self.path,
            )
            return
        if data is None:
            return
        self.enabled = bool(data.get("enabled", True))
        logger.info("Chatbot: enabled=%s", self.enabled)

    def _read_sync(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    async def _save(self) -> None:
        payload = {"enabled": self.enabled}
        try:
            await asyncio.to_thread(self._write_sync, payload)
        except Exception:
            logger.exception("No se pudo guardar %s", self.path)

    def _write_sync(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".chat-", suffix=".tmp", dir=str(self.path.parent)
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

    async def set_enabled(self, enabled: bool) -> None:
        async with self._lock:
            self.enabled = enabled
            await self._save()


store = ChatStore()
