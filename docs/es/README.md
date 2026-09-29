[English](../en/README.md) | [简体中文](../../README.md) | [हिन्दी](../hi/README.md) | Español | [Français](../fr/README.md) | [Português](../pt/README.md) | [Русский](../ru/README.md)

<div align="center">
  <img src="../../web/assets/translatedsubs-logo.svg" width="88" alt="Logotipo de TranslatedSubs" />
  <h1>TranslatedSubs</h1>
  <p><strong>Del vídeo a los subtítulos traducidos en un solo flujo.</strong></p>
  <p>Descarga, transcribe, traduce e integra subtítulos. Previsualiza, edita y descarga los resultados en la interfaz web.</p>
</div>

TranslatedSubs es una plataforma de trabajo para subtítulos de vídeo y audio. Permite descargar medios, reconocer voz, traducir subtítulos y generar vídeos con subtítulos integrados o archivos de subtítulos independientes. Las tareas se gestionan desde la interfaz web o mediante MCP.

Está pensado para personas y equipos que necesitan convertir la URL de una página de vídeo o un archivo local en subtítulos traducidos. En un mismo lugar pueden seguir el progreso, corregir subtítulos y descargar el vídeo y el archivo SRT. La sincronización con Google Drive es opcional. La interfaz web todavía muestra sus controles en chino.

## Inicio rápido con Docker

Ejecuta lo siguiente desde la raíz del repositorio:

```bash
cp .env.example .env
# Set SUBTRANS_DEEPSEEK_API_KEY in .env
docker build -t translatedsubs:local . && docker run -d --name translatedsubs --restart unless-stopped -p 8000:8000 --env-file .env -e SUBTRANS_DATA_DIR=/data -e SUBTRANS_DB=/data/db/app.db -v translatedsubs-data:/data translatedsubs:local
```

Al actualizar un contenedor existente, sustituye `translatedsubs-data` por el nombre del volumen actual para conservar la base de datos de tareas y los archivos generados. Las variables de entorno `SUBTRANS_*` siguen siendo compatibles.

Abre <http://localhost:8000/>. Ejecuta `curl http://127.0.0.1:8000/api/health` para comprobar que la API responde; la respuesta correcta contiene `"ok":true`. `/api/health/ready` muestra además el estado de la clave de traducción, FFmpeg, el almacenamiento y el filtro de subtítulos integrados. Para el desarrollo local y la implementación en Linux, consulta el [índice de documentación (en chino)](../README.md).

## Funcionalidades

- **Procesamiento de subtítulos**: Descarga vídeos, extrae audio, transcribe y traduce la voz, y genera subtítulos independientes o integrados en el vídeo.
- **Interfaz web**: Gestiona la cola de tareas, sigue el progreso, previsualiza vídeos, edita subtítulos y descarga los resultados en el navegador.
- **Integración MCP**: Permite que Codex, Claude Desktop y otros clientes de IA creen y sigan tareas mediante lenguaje natural.
- **Extensión de Google Drive**: Sube, descarga y organiza archivos por tarea para compartir los resultados con un equipo.
- **Motores de transcripción intercambiables**: Elige entre faster-whisper local, Replicate o un servicio HTTP compatible según tus necesidades de coste, velocidad y privacidad.

## Del vídeo a los subtítulos

1. Pega la URL de una página de vídeo en la interfaz web o sube un vídeo local. Puedes comprobar primero la URL con la prueba de descarga.
2. Elige los idiomas de origen y destino, subtítulos solo traducidos o bilingües, y subtítulos separados o integrados. La transcripción utiliza faster-whisper local por defecto. Antes del primer trabajo, descarga el modelo elegido en la sección de modelos locales y espera a que esté listo.
3. Envía el trabajo y sigue en la cola la descarga, extracción de audio, transcripción, traducción e integración. Al terminar, puedes previsualizar el vídeo, editar los subtítulos, volver a integrarlos y descargar el vídeo y el SRT. El modo de solo descarga no genera subtítulos.

Los subtítulos separados se pueden activar o desactivar en el reproductor. Los integrados quedan grabados en la imagen y requieren el filtro `subtitles` (libass) de FFmpeg. La primera ejecución puede necesitar descargar un modelo y acceder a servicios externos. Los clientes de IA pueden seguir el mismo flujo mediante la [guía de agentes MCP (en chino)](../mcp-agent-guide.md).

## Configuración y datos

Copia `.env.example` y configura `SUBTRANS_DEEPSEEK_API_KEY` en `.env`. La [plantilla de variables de entorno](../../.env.example) contiene todos los valores y ajustes. Los más habituales son:

| Ajuste | Uso |
| --- | --- |
| `SUBTRANS_DEEPSEEK_API_KEY` | Clave de DeepSeek para traducir subtítulos; sin ella no está listo el flujo completo. |
| `SUBTRANS_DATA_DIR`, `SUBTRANS_DB` | Ubicación de los archivos y la base SQLite de tareas; el ejemplo con Docker guarda ambos en un volumen persistente. |
| `SUBTRANS_TRANSCRIBER_BACKEND` | Usa `local_whisper` por defecto; también admite `replicate` o un servicio HTTP compatible. |
| `SUBTRANS_COOKIES` | Archivo de cookies para sitios que requieren inicio de sesión o verificación de edad. |
| `SUBTRANS_WORKERS`, `SUBTRANS_DOWNLOAD_WORKERS` | Límites de concurrencia del procesamiento y las descargas. |

Al actualizar el contenedor, reutiliza el volumen actual y conserva tanto la base SQLite como los resultados. No subas al repositorio `.env`, cookies, credenciales OAuth ni archivos multimedia de prueba. Google Drive requiere un sidecar independiente; consulta el [inicio rápido local (en chino)](../local-quick-start.md).

## Problemas frecuentes

- La API responde pero los trabajos no empiezan: revisa `checks` y `capabilities` en `/api/health/ready` para comprobar la clave, FFmpeg/FFprobe, yt-dlp y el almacenamiento.
- `MODEL_NOT_READY`: descarga y comprueba el modelo Whisper elegido en la sección de modelos locales.
- No funcionan los subtítulos integrados: instala FFmpeg con libass o selecciona subtítulos separados. Comprueba el filtro con `ffmpeg -hide_banner -filters | grep ' subtitles '`.
- Falla la descarga de una URL: ejecuta primero la prueba de descarga; si el sitio exige inicio de sesión, configura `SUBTRANS_COOKIES` según la [guía local (en chino)](../local-quick-start.md).

## Documentación

La mayoría de las guías siguientes están en chino; el protocolo del servicio de transcripción está en inglés.

- [Índice de documentación](../README.md): guías de implementación y extensiones por caso de uso.
- [Inicio rápido local](../local-quick-start.md): macOS/Linux, variables de entorno y Google Drive sidecar.
- [Implementación en Linux](../quick-start-linux.md): instalación en Ubuntu/Debian, systemd, proxy inverso y resolución de problemas.
- [Servidor MCP](../mcp-server.md): stdio, Streamable HTTP y herramientas.
- [Guía de agentes MCP](../mcp-agent-guide.md): secuencia de llamadas, estados y errores.
- [Protocolo del servicio de transcripción (en inglés)](../transcriber-service.md): motores local, Replicate y HTTP.
- [Google Drive sidecar](../../drive-service/README.md): API y configuración para sincronizar archivos en la nube.

## Desarrollo

El proyecto utiliza Python 3.10–3.12, FastAPI, FFmpeg y JavaScript nativo. Para desarrollarlo localmente, ejecuta `uv sync` y después `uv run uvicorn src.handler.app:app --port 8000`; el mismo servicio publica la interfaz web. Ejecuta las pruebas de Python con `uv run pytest -q` y las pruebas del frontend con `npm test` desde `web/`. Las pruebas con servicios reales requieren activación explícita; consulta [AGENTS.md (en chino)](../../AGENTS.md).

`src/handler/` expone la API HTTP; `src/core/` procesa descargas, transcripción y subtítulos; `src/service/` y `src/store/` gestionan tareas y persistencia; `src/mcp_server/` proporciona MCP; y `web/` contiene la interfaz. Consulta [CONTRIBUTING.md (en chino)](../../.github/CONTRIBUTING.md). Comunica las vulnerabilidades de forma privada según [SECURITY.md](../../.github/SECURITY.md), nunca en un Issue público.

## Licencia y cumplimiento

El proyecto se distribuye bajo la [licencia MIT](../../LICENSE). Procesa únicamente contenido que tengas derecho a acceder, descargar, transcribir, traducir y redistribuir. Respeta las condiciones del sitio de origen, las restricciones de derechos de autor y la legislación aplicable.
