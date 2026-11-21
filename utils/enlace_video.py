"""Arma el texto del mensaje con el enlace del video según el host."""
import hashlib
import hmac
from urllib.parse import quote

from config import settings
from media.storage import UploadResult

_DIRECTOS = {"uguu.se"}
_CON_WORKER = {"litterbox"}


def firmar(secreto: str, url: str, titulo: str) -> str:
    return hmac.new(
        secreto.encode("utf-8"),
        f"{url}\n{titulo}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def texto_enlace(
    upload: UploadResult,
    titulo: str,
    size_mb: float,
    extra: str | None = None,
) -> str | None:
    proveedor = upload.provider.strip().lower()
    info = (
        f"-# {upload.provider} · caduca en {upload.expiry_hours} h · "
        f"{size_mb:.1f} MB"
    )
    if extra:
        info += f" · {extra}"

    if proveedor in _DIRECTOS:
        return f"{upload.url}\n{info}"

    if proveedor in _CON_WORKER and settings.worker_url and settings.worker_secret:
        t = titulo[:100]
        s = firmar(settings.worker_secret, upload.url, t)
        enlace = (
            f"{settings.worker_url.rstrip('/')}/"
            f"?u={quote(upload.url, safe='')}"
            f"&t={quote(t, safe='')}"
            f"&s={s}"
        )
        return f"{enlace}\n{info}"

    return None
