"""Subida a hosts externos (Litterbox, storage.to, GoFile) con cadena de fallback."""
import asyncio
import logging
import ssl
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import certifi

from config import settings

logger = logging.getLogger(__name__)


def _build_ssl_context() -> ssl.SSLContext:
    """Contexto SSL basado en certifi (evita errores de certificado en Windows)."""
    return ssl.create_default_context(cafile=certifi.where())


class StorageError(Exception):
    """Error de subida a un host externo con mensaje apto para el usuario."""


@dataclass
class UploadResult:
    """Resultado de una subida externa."""

    url: str
    provider: str
    expiry_hours: int


class StorageProvider:
    """Interfaz base de un host de subida."""

    name: str = "base"
    max_bytes: int | None = None
    expiry_hours: int = 24

    async def upload(self, session: aiohttp.ClientSession, path: Path) -> str:
        """Sube el archivo y devuelve la URL pública."""
        raise NotImplementedError


class LitterboxProvider(StorageProvider):
    """Litterbox (catbox temporal)."""

    name = "Litterbox"
    max_bytes = 1024 * 1024 * 1024  # 1 GB
    _API = "https://litterbox.catbox.moe/resources/internals/api.php"

    def __init__(self) -> None:
        self.expiry_hours = _parse_litterbox_expiry(settings.litterbox_expiry)

    async def upload(self, session: aiohttp.ClientSession, path: Path) -> str:
        form = aiohttp.FormData()
        form.add_field("reqtype", "fileupload")
        form.add_field("time", settings.litterbox_expiry)
        form.add_field(
            "fileToUpload",
            path.open("rb"),
            filename=path.name,
            content_type="application/octet-stream",
        )
        async with session.post(self._API, data=form) as resp:
            text = (await resp.text()).strip()
            if resp.status != 200 or not text.startswith("http"):
                raise StorageError(f"Litterbox respondió {resp.status}.")
            return text


class TmpfilesProvider(StorageProvider):
    """tmpfiles.org (temporal, máx. 100 MB)."""

    name = "tmpfiles.org"
    max_bytes = 100 * 1024 * 1024
    _API = "https://tmpfiles.org/api/v1/upload"

    def __init__(self) -> None:
        self.expiry_hours = min(settings.tmpfiles_expiry_hours, 48)

    async def upload(self, session: aiohttp.ClientSession, path: Path) -> str:
        if path.stat().st_size > self.max_bytes:
            raise StorageError("tmpfiles.org: archivo supera 100 MB.")
        form = aiohttp.FormData()
        form.add_field("expire", str(int(self.expiry_hours * 3600)))
        form.add_field(
            "file",
            path.open("rb"),
            filename=path.name,
            content_type="application/octet-stream",
        )
        async with session.post(self._API, data=form) as resp:
            data = await resp.json()
            if resp.status != 200 or data.get("status") != "success":
                raise StorageError(f"tmpfiles.org respondió {resp.status}.")
        url = (data.get("data") or {}).get("url")
        if not url:
            raise StorageError("tmpfiles.org no devolvió URL.")
        if "/dl/" in url:
            return url
        return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)


class StorageToProvider(StorageProvider):
    """storage.to (flujo init -> PUT -> confirm)."""

    name = "storage.to"
    max_bytes = 50 * 1024 * 1024  # 50 MB (sin multipart)
    _BASE = "https://storage.to/api"

    def __init__(self) -> None:
        self.expiry_hours = settings.storage_to_expiry_hours

    async def upload(self, session: aiohttp.ClientSession, path: Path) -> str:
        size = path.stat().st_size
        init_body = {
            "filename": path.name,
            "content_type": "application/octet-stream",
            "size": size,
        }
        async with session.post(f"{self._BASE}/upload/init", json=init_body) as resp:
            data = await resp.json()
            if resp.status != 200 or not data.get("success"):
                raise StorageError(f"storage.to init respondió {resp.status}.")
        if data.get("type") != "single":
            raise StorageError("storage.to devolvió un upload multipart no soportado.")

        upload_url = data["upload_url"]
        r2_key = data["r2_key"]
        headers = {
            k: v[0] if isinstance(v, list) else v
            for k, v in data.get("headers", {}).items()
        }

        with path.open("rb") as fh:
            async with session.put(upload_url, data=fh, headers=headers) as resp:
                if resp.status not in (200, 201):
                    raise StorageError(f"storage.to PUT respondió {resp.status}.")

        confirm_body = {
            "filename": path.name,
            "size": size,
            "content_type": "application/octet-stream",
            "r2_key": r2_key,
        }
        async with session.post(f"{self._BASE}/upload/confirm", json=confirm_body) as resp:
            data = await resp.json()
            if resp.status != 200 or not data.get("success"):
                raise StorageError(f"storage.to confirm respondió {resp.status}.")
        return data["file"]["url"]


class GoFileProvider(StorageProvider):
    """GoFile (subida directa, cuenta invitado)."""

    name = "GoFile"
    max_bytes = None
    _API = "https://upload.gofile.io/uploadfile"

    def __init__(self) -> None:
        self.expiry_hours = settings.gofile_expiry_hours

    async def upload(self, session: aiohttp.ClientSession, path: Path) -> str:
        form = aiohttp.FormData()
        form.add_field(
            "file",
            path.open("rb"),
            filename=path.name,
            content_type="application/octet-stream",
        )
        async with session.post(self._API, data=form) as resp:
            data = await resp.json()
            if resp.status != 200 or data.get("status") != "ok":
                raise StorageError(f"GoFile respondió {resp.status}.")
        return data["data"]["downloadPage"]


def _parse_litterbox_expiry(value: str) -> int:
    """Convierte '1h'/'12h'/'24h'/'72h' a horas."""
    value = value.strip().lower()
    if value.endswith("h") and value[:-1].isdigit():
        return int(value[:-1])
    return 24


_PROVIDERS: dict[str, type[StorageProvider]] = {
    "litterbox": LitterboxProvider,
    "tmpfiles": TmpfilesProvider,
    "storage_to": StorageToProvider,
    "gofile": GoFileProvider,
}


class StorageChain:
    """Prueba los hosts en orden, con reintentos y circuit breaker simple."""

    def __init__(self) -> None:
        self.providers: list[StorageProvider] = [
            _PROVIDERS[name]()
            for name in settings.storage_providers
            if name in _PROVIDERS
        ]
        self._failures: dict[str, int] = {}
        self._skip_until: dict[str, float] = {}

    def _is_skipped(self, name: str) -> bool:
        until = self._skip_until.get(name, 0.0)
        return time.monotonic() < until

    def _record_failure(self, name: str) -> None:
        self._failures[name] = self._failures.get(name, 0) + 1
        if self._failures[name] >= 3:
            self._skip_until[name] = time.monotonic() + settings.storage_circuit_seconds
            logger.warning("Circuit breaker abierto para %s", name)

    def _record_success(self, name: str) -> None:
        self._failures[name] = 0
        self._skip_until.pop(name, None)

    async def upload(self, path: Path) -> UploadResult:
        """Sube el archivo al primer host que lo acepte."""
        try:
            return await asyncio.wait_for(self._upload_inner(path), timeout=900)
        except asyncio.TimeoutError as exc:
            raise StorageError("La subida externa superó el tiempo máximo (900 s).") from exc

    async def _upload_inner(self, path: Path) -> UploadResult:
        size = path.stat().st_size
        size_mb = size / (1024 * 1024)
        errors: list[str] = []
        logger.info(
            "StorageChain: proveedores disponibles en orden: %s",
            ", ".join(p.name for p in self.providers) or "(ninguno)",
        )

        # Timeout proporcional al tamaño: 30 s base + 6 s por MB.
        total = 30 + int(6 * size_mb)
        timeout = aiohttp.ClientTimeout(total=total, sock_connect=15)
        connector = aiohttp.TCPConnector(ssl=_build_ssl_context())
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector
        ) as session:
            for provider in self.providers:
                if provider.max_bytes is not None and size > provider.max_bytes:
                    logger.info(
                        "Se omite %s: archivo de %.1f MB supera el máximo de %.1f MB",
                        provider.name,
                        size_mb,
                        provider.max_bytes / (1024 * 1024),
                    )
                    errors.append(f"{provider.name}: archivo demasiado grande")
                    continue
                if self._is_skipped(provider.name):
                    logger.info(
                        "Se omite %s: circuit breaker abierto", provider.name
                    )
                    errors.append(f"{provider.name}: circuit breaker abierto")
                    continue

                # Máximo 1 reintento (2 intentos totales).
                for attempt in range(2):
                    start = time.perf_counter()
                    logger.info(
                        "Intentando %s con archivo de %.1f MB (intento %d)",
                        provider.name,
                        size_mb,
                        attempt + 1,
                    )
                    try:
                        url = await provider.upload(session, path)
                        elapsed = time.perf_counter() - start
                        self._record_success(provider.name)
                        logger.info(
                            "Subida a %s OK: intento %d, %.2f MB, %.2f s, url=%s",
                            provider.name,
                            attempt + 1,
                            size_mb,
                            elapsed,
                            url,
                        )
                        return UploadResult(
                            url=url,
                            provider=provider.name,
                            expiry_hours=provider.expiry_hours,
                        )
                    except aiohttp.ClientConnectorCertificateError as exc:
                        elapsed = time.perf_counter() - start
                        logger.warning(
                            "Subida a %s falló: intento %d, %.2f MB, %.2f s, "
                            "error de certificado SSL: %s",
                            provider.name,
                            attempt + 1,
                            size_mb,
                            elapsed,
                            exc,
                        )
                        errors.append(f"{provider.name}: error de certificado SSL")
                        logger.info(
                            "Resultado %s: error de certificado SSL", provider.name
                        )
                        break
                    except asyncio.TimeoutError:
                        elapsed = time.perf_counter() - start
                        logger.warning(
                            "Subida a %s falló: intento %d, %.2f MB, %.2f s, timeout",
                            provider.name,
                            attempt + 1,
                            size_mb,
                            elapsed,
                        )
                        errors.append(f"{provider.name}: timeout")
                        logger.info("Resultado %s: timeout", provider.name)
                    except aiohttp.ClientResponseError as exc:
                        elapsed = time.perf_counter() - start
                        logger.warning(
                            "Subida a %s falló: intento %d, %.2f MB, %.2f s, "
                            "status %d",
                            provider.name,
                            attempt + 1,
                            size_mb,
                            elapsed,
                            exc.status,
                        )
                        errors.append(f"{provider.name}: HTTP {exc.status}")
                        logger.info(
                            "Resultado %s: HTTP %d", provider.name, exc.status
                        )
                        if exc.status < 500:
                            break
                    except aiohttp.ClientError as exc:
                        elapsed = time.perf_counter() - start
                        logger.warning(
                            "Subida a %s falló: intento %d, %.2f MB, %.2f s, %s",
                            provider.name,
                            attempt + 1,
                            size_mb,
                            elapsed,
                            type(exc).__name__,
                        )
                        errors.append(f"{provider.name}: {type(exc).__name__}")
                        logger.info(
                            "Resultado %s: %s", provider.name, type(exc).__name__
                        )
                        break
                    except StorageError as exc:
                        elapsed = time.perf_counter() - start
                        logger.warning(
                            "Subida a %s falló: intento %d, %.2f MB, %.2f s, %s",
                            provider.name,
                            attempt + 1,
                            size_mb,
                            elapsed,
                            exc,
                        )
                        errors.append(f"{provider.name}: {exc}")
                        logger.info("Resultado %s: %s", provider.name, exc)
                        break
                    except Exception as exc:
                        elapsed = time.perf_counter() - start
                        logger.warning(
                            "Subida a %s falló: intento %d, %.2f MB, %.2f s, %s",
                            provider.name,
                            attempt + 1,
                            size_mb,
                            elapsed,
                            type(exc).__name__,
                        )
                        errors.append(f"{provider.name}: {type(exc).__name__}")
                        logger.info(
                            "Resultado %s: %s", provider.name, type(exc).__name__
                        )
                        break

                    if attempt == 0:
                        await asyncio.sleep(1)

                self._record_failure(provider.name)

        raise StorageError("Fallaron todos los hosts: " + "; ".join(errors))
