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
    duration: float | None = None,
) -> str | None:
    proveedor = upload.provider.strip().lower()
    info = (
        f"-# {upload.provider} · caduca en {upload.expiry_hours} h · "
        f"{size_mb:.1f} MB"
    )
    if extra:
        info += f" · {extra}"

    worker_ok = bool(settings.worker_url and settings.worker_secret)
    largo = (
        duration is not None
        and settings.worker_umbral_min > 0
        and duration >= settings.worker_umbral_min * 60
    )
    if proveedor in _DIRECTOS and not (largo and worker_ok):
        return f"{upload.url}\n{info}"

    if (proveedor in _CON_WORKER or proveedor in _DIRECTOS) and worker_ok:
        t = titulo[:100]
        s = firmar(settings.worker_secret, upload.url, t)
        enlace = (
            f"{settings.worker_url.rstrip('/')}/"
            f"?u={quote(upload.url, safe='')}"
            f"&t={quote(t, safe='')}"
            f"&s={s}"
        )
        etiqueta = t.replace("[", "(").replace("]", ")").replace("\n", " ").strip()
        return f"[{etiqueta or 'Video'}]({enlace})\n{info}"

    return None
