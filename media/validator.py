"""Valida que una URL de imagen responda y sea realmente una imagen."""
# TODO (paso 3): async def is_valid_image(session, url) -> bool
#  - GET con User-Agent de navegador y Referer del dominio, timeout corto
#  - descartar 403/404 y Content-Type que no sea image/*
