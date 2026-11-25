# vox-bot

Bot de Discord con dos comandos: búsqueda de imágenes con embed paginado y descarga de video con envío adaptado al límite de subida de Discord.

## Funciones

### `/img consulta`
- Busca imágenes y las muestra en un embed paginado con botones.
- Usa DuckDuckGo (`ddgs`) como proveedor gratuito y Serper como respaldo opcional.
- Valida cada URL antes de mostrarla (descarta 403, 404 y contenido que no sea imagen). Si la URL original no sirve para Discord pero el bot puede descargarla, la envía como archivo adjunto; como último recurso usa la miniatura del buscador.
- Cachea los resultados por consulta durante unos minutos, así que paginar no repite la búsqueda.
- Si un proveedor falla varias veces seguidas, se salta durante unos minutos (circuit breaker simple).
- Los botones solo responden a quien ejecutó el comando.

### `/video url`
- Descarga el video con `yt-dlp` en calidad máxima de 720p, priorizando H.264/AAC en mp4.
- Si el archivo cabe en el límite de Discord, lo envía como adjunto.
- Si no cabe, lo sube a Cloudflare R2 y envía un link con expiración.
- Límite de duración configurable, límite de descargas simultáneas y límite de uso por usuario.
- Traduce los errores comunes de `yt-dlp` (video privado, bloqueo por región, duración excesiva) a mensajes claros.

## Requisitos

- Python 3.12 o superior
- [ffmpeg](https://ffmpeg.org/) instalado y disponible en el `PATH`
- Un bot de Discord creado en el [portal de desarrolladores](https://discord.com/developers/applications)
- (Opcional) Una cuenta de Cloudflare con un bucket de R2, para videos que superen el límite de Discord
- (Opcional) Una clave de API de Serper, para el proveedor de respaldo de imágenes

## Instalación

```bash
git clone https://github.com/srgvrs/vox-bot.git
cd vox-bot
python -m venv .venv
source .venv/bin/activate   # En Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Luego completa el archivo `.env` (ver la sección siguiente).

## Configuración

Toda la configuración se lee desde `.env` a través de `config.py`. Los secretos nunca deben ir en el código ni en commits.

| Variable | Descripción |
|---|---|
| `DISCORD_TOKEN` | Token del bot |
| `MAX_VIDEO_MINUTES` | Duración máxima permitida de un video |
| `DISCORD_UPLOAD_LIMIT_MB` | Tamaño máximo para enviar como adjunto; por encima se sube a R2 |
| `SERPER_API_KEY` | (Opcional) Clave de Serper para el respaldo de imágenes |
| `R2_*` | (Opcional) Credenciales y datos del bucket de Cloudflare R2 |

> Los nombres exactos de las variables están definidos en `config.py`. Esta tabla debe mantenerse sincronizada con ese archivo.

## Uso

```bash
python main.py
```

Una vez que el bot esté en tu servidor:

- `/img gatos astronautas` busca imágenes y las muestra paginadas.
- `/video https://ejemplo.com/video` descarga el video y lo envía como adjunto o como link.

## Estructura del proyecto

```
vox-bot/
├── cogs/          # Capa de Discord: comandos, embeds y views (sin lógica de negocio)
├── search/        # Proveedores de imágenes (SearchProvider) y chain.py
├── media/         # Validación de imágenes, descarga de video, subida a R2
├── utils/         # Caché y límites de uso
├── referencias/   # Código de ejemplo de solo lectura (no se edita ni se importa)
├── temp/          # Archivos temporales de descarga
└── config.py      # Única fuente de configuración
```

## Stack

- `discord.py` 2.x (slash commands con `app_commands` y cogs)
- `aiohttp` para HTTP
- `yt-dlp` + `ffmpeg` para video
- `ddgs` (DuckDuckGo) y Serper para imágenes
- `boto3` contra la API S3 de Cloudflare R2
- `python-dotenv` para configuración

## Seguridad

- Solo se aceptan URLs `http` y `https`.
- Se rechazan hosts internos (localhost, IPs privadas) antes de descargar.
- Los videos subidos a R2 se entregan con links que expiran; no se generan URLs permanentes.
- Los tokens y claves no se registran en logs.

## Convenciones de desarrollo

- Todo el I/O es `async`; el código bloqueante (`yt-dlp`, `ffmpeg`, `boto3`) corre con `asyncio.to_thread`.
- Type hints en todas las funciones.
- Identificadores en inglés; comentarios, docstrings y mensajes al usuario en español.
- Se usa `logging`, nunca `print`.
- No se agregan dependencias nuevas sin justificarlas.

## Licencia
