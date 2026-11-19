# Convenciones del proyecto: bot de Discord (imágenes + video)

## Objetivo
Bot de Discord con dos funciones:
1. `/img consulta`: búsqueda de imágenes con embed paginado y botones, tolerante a fallos.
2. `/video url`: descarga de video con calidad pasable (máx. 720p). Se envía como adjunto si cabe en el límite de Discord; si no, se sube a Cloudflare R2 y se manda un link con expiración.

## Stack
- Python 3.12+, `discord.py` 2.x (slash commands con `app_commands`, cogs)
- `aiohttp` para HTTP (nada de `requests`, es bloqueante)
- `yt-dlp` + `ffmpeg` para video
- `ddgs` (DuckDuckGo) como proveedor de imágenes gratuito; Serper como respaldo opcional
- `boto3` contra la API S3 de Cloudflare R2
- `python-dotenv` para configuración
- No agregar dependencias nuevas sin avisar y justificarlo.

## Arquitectura y responsabilidades
- `cogs/`: solo capa de Discord (comandos, embeds, views). Sin lógica de negocio.
- `search/`: proveedores de imágenes detrás de la interfaz `SearchProvider`. `chain.py` los prueba en orden.
- `media/`: validación de URLs de imagen, descarga de video, subida a R2.
- `utils/`: caché y límites de uso.
- `config.py`: única fuente de configuración. Nada de `os.getenv` fuera de ahí.
- `referencias/`: código de ejemplo de solo lectura. NUNCA editar ni importar desde ahí; solo tomar ideas y reescribir.

## Reglas de código
- Todo el código I/O es `async`. Prohibido bloquear el event loop.
- Código bloqueante (yt-dlp, ffmpeg, boto3) va en `asyncio.to_thread(...)`.
- Type hints en todas las funciones. Funciones pequeñas, una responsabilidad.
- Identificadores en inglés; comentarios, docstrings y mensajes al usuario en español.
- Usar `logging`, no `print`. Nunca registrar tokens ni claves.
- Cambios mínimos: modificar solo lo necesario para la tarea y mencionar qué archivos se tocaron.

## Tolerancia a fallos
- Todas las peticiones HTTP llevan timeout explícito (5 a 10 s) y reintento con backoff corto (máx. 2 reintentos).
- Un proveedor que falla varias veces seguidas se salta unos minutos (circuit breaker simple).
- Validar cada URL de imagen antes de mostrarla: `GET` con `User-Agent` de navegador y `Referer` del dominio; descartar 403, 404 y contenido que no sea imagen.
- Si la URL original no sirve para Discord pero el bot puede descargarla, enviarla como `discord.File`. Último recurso: la miniatura del buscador.
- Cachear resultados por consulta unos minutos para no repetir búsquedas al paginar.
- Capturar excepciones en los comandos y responder con un mensaje claro, nunca dejar la interacción colgada.

## Reglas de Discord
- Comandos lentos: `await interaction.response.defer()` primero y luego `followup`.
- Los botones solo responden a quien invocó el comando (`interaction_check`).
- Las `View` tienen `timeout`; al expirar se deshabilitan los botones.
- Errores al usuario como mensaje `ephemeral`.
- `allowed_mentions` restringido al enviar texto derivado de la entrada del usuario.

## Reglas de video
- Formato: tope de altura 720p, preferir H.264/AAC en mp4, por ejemplo
  `bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/b[height<=720]/b`
- Límite de duración configurable (`MAX_VIDEO_MINUTES`) y de descargas simultáneas (semáforo).
- Límite por usuario con `utils/ratelimit.py`.
- Archivos temporales en `temp/` con nombre aleatorio (UUID); borrarlos siempre en `try/finally`.
- Si el archivo supera `DISCORD_UPLOAD_LIMIT_MB`: subir a R2 y enviar link con expiración. Nunca devolver una URL permanente adivinable.
- Traducir errores comunes de yt-dlp (privado, región, demasiado largo) a mensajes claros.

## Seguridad
- Secretos solo en `.env` (nunca en el código ni en commits).
- Solo aceptar URLs `http`/`https` y rechazar hosts internos (localhost, IPs privadas) antes de descargar.

## Flujo de trabajo con Aider
- Una tarea pequeña por vez; confirmar que corre antes de pasar a la siguiente.
- Antes de usar una API de librería, consultar su documentación (`/web`) en lugar de adivinar parámetros.
- Si algo no está claro, preguntar en lugar de inventar.
