"""Limpieza de carpetas de trabajo huérfanas en TEMP_DIR."""
import logging
import re
import shutil
import time
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

_NOMBRE_CARPETA = re.compile(r"^[0-9a-f]{32}$")


def _ultima_modificacion(carpeta: Path) -> float:
    """Devuelve el mtime más reciente de la carpeta y sus hijos directos."""
    ultima = carpeta.stat().st_mtime
    for hijo in carpeta.iterdir():
        try:
            ultima = max(ultima, hijo.stat().st_mtime)
        except OSError:
            continue
    return ultima


def limpiar_temp(max_age_min: int | None = None) -> int:
    """Borra carpetas hexadecimales de TEMP_DIR más antiguas que el límite; devuelve cuántas borró."""
    raiz = settings.temp_dir
    if not raiz.exists():
        return 0
    minutos = settings.temp_max_age_min if max_age_min is None else max_age_min
    if minutos <= 0:
        return 0
    limite = time.time() - minutos * 60
    borradas = 0
    for entry in raiz.iterdir():
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not _NOMBRE_CARPETA.match(entry.name):
                continue
            if _ultima_modificacion(entry) >= limite:
                continue
            shutil.rmtree(entry)
            borradas += 1
        except OSError as exc:
            logger.warning("No se pudo limpiar %s: %s", entry, exc)
    if borradas:
        logger.info("Limpieza de TEMP_DIR: %d carpetas eliminadas", borradas)
    return borradas


def borrar_carpeta_trabajo(archivo: Path) -> bool:
    """Borra la carpeta padre de `archivo` si es una carpeta de trabajo de TEMP_DIR.

    Solo actúa si la carpeta es hija directa de TEMP_DIR y su nombre es
    hexadecimal de 32 caracteres. Devuelve True si la borró.
    """
    try:
        carpeta = archivo.parent.resolve()
        raiz = settings.temp_dir.resolve()
        if carpeta.parent != raiz or not _NOMBRE_CARPETA.match(carpeta.name):
            return False
        shutil.rmtree(carpeta)
    except OSError as exc:
        logger.warning("No se pudo borrar la carpeta de trabajo de %s: %s", archivo, exc)
        return False
    logger.info("Carpeta de trabajo eliminada: %s", carpeta.name)
    return True
