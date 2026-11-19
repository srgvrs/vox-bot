# Bot de Discord: imágenes + video

## Puesta en marcha
1. `python -m venv .venv` y activarlo
2. `pip install -r requirements.txt`
3. Instalar `ffmpeg` y comprobar con `ffmpeg -version`
4. Copiar `.env.example` a `.env` y completar `DISCORD_TOKEN`
5. `python main.py`

## Orden de desarrollo (con Aider)
1. Que el bot arranque y los comandos respondan
2. `/img` con una imagen y un proveedor (`search/ddg.py`)
3. Paginación, botones y validación de URLs (`media/validator.py`)
4. Cadena de proveedores y caché (`search/chain.py`, `utils/cache.py`)
5. `/video` solo con adjunto y límite de tamaño (`media/downloader.py`)
6. Subida a R2 y link con expiración (`media/storage.py`)
7. Cola, límites por usuario y logs
