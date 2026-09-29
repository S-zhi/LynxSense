English | [简体中文](../../README.md) | [हिन्दी](../hi/README.md) | [Español](../es/README.md) | [Français](../fr/README.md) | [Português](../pt/README.md) | [Русский](../ru/README.md)

<div align="center">
  <img src="../../web/assets/translatedsubs-logo.svg" width="88" alt="TranslatedSubs Logo" />
  <h1>TranslatedSubs</h1>
  <p><strong>From video to translated subtitles in one workflow.</strong></p>
  <p>Download, transcribe, translate, and package subtitles. Preview, edit, and download results in the Web workbench.</p>
</div>

TranslatedSubs is a video and audio subtitle workbench. It downloads media, transcribes speech, translates subtitles, and produces soft or hard-subtitled output. Jobs can be managed through the Web workbench or MCP.

It is designed for people and teams turning a video page URL or a local video into translated subtitles. Track progress, correct subtitles, and retrieve the video and SRT file in one place. Google Drive syncing is optional and is not required for the subtitle pipeline. The Web workbench currently uses Chinese labels.

## Docker quick start

Run this from the repository root:

```bash
cp .env.example .env
# Set SUBTRANS_DEEPSEEK_API_KEY in .env
docker build -t translatedsubs:local . && docker run -d --name translatedsubs --restart unless-stopped -p 8000:8000 --env-file .env -e SUBTRANS_DATA_DIR=/data -e SUBTRANS_DB=/data/db/app.db -v translatedsubs-data:/data translatedsubs:local
```

When upgrading an existing container, replace `translatedsubs-data` with the existing volume name to retain jobs and output files. The `SUBTRANS_*` environment variables remain supported.

Open <http://localhost:8000/>. Run `curl http://127.0.0.1:8000/api/health` to check that the API is running; a healthy response contains `"ok":true`. `/api/health/ready` also reports whether the translation key, FFmpeg, storage paths, and hard-subtitle filter are ready. See the [documentation index (Chinese)](../README.md) for local development, Linux deployment, and extensions.

## Capabilities

- **Subtitle pipeline**: Download, transcribe, translate, and produce soft or hard subtitles for faster understanding and cross-language viewing.
- **Web workbench**: Queue jobs, follow progress, preview video, edit subtitles, and download results in a browser.
- **MCP integration**: Let Codex, Claude Desktop, and other AI clients create and track jobs from natural language.
- **Google Drive extension**: Upload, download, and organize task files when results need to move through a shared file workflow.
- **Replaceable transcription backends**: Choose local faster-whisper, Replicate, or a compatible HTTP service for different cost, speed, and privacy needs.

## From video to subtitles

1. Paste a video page URL into the Web workbench or upload a local video. You can use the download probe to check a URL first.
2. Choose the source and target languages, translated-only or bilingual subtitles, and soft or hard subtitles. The default transcription backend is local faster-whisper. Before the first subtitle job, download the selected model in the Local Models settings and wait until it is ready.
3. Submit the job and follow download, audio extraction, transcription, translation, and packaging in the queue. When it succeeds, preview the video, edit subtitles, package it again, and download the video and SRT. A video-only job produces no subtitles.

Soft subtitles can be toggled in a player. Hard subtitles are written into the picture and require FFmpeg's `subtitles` (libass) filter. The first run may need a model download and access to external services, so allow enough network bandwidth and disk space. AI clients can use the same pipeline through the [MCP Agent guide (Chinese)](../mcp-agent-guide.md).

## Key configuration and data

Copy `.env.example` and set `SUBTRANS_DEEPSEEK_API_KEY` in `.env`. The [environment template](../../.env.example) lists every setting and its default. Common settings are:

| Setting | Purpose |
| --- | --- |
| `SUBTRANS_DEEPSEEK_API_KEY` | DeepSeek key for subtitle translation; the full subtitle pipeline is not ready without it. |
| `SUBTRANS_DATA_DIR`, `SUBTRANS_DB` | Locations of video and subtitle files and the SQLite job database; the Docker example stores both in a persistent volume. |
| `SUBTRANS_TRANSCRIBER_BACKEND` | Defaults to `local_whisper`; select `replicate` or a compatible HTTP service explicitly. |
| `SUBTRANS_COOKIES` | Cookie file for sites that require login or age verification. |
| `SUBTRANS_WORKERS`, `SUBTRANS_DOWNLOAD_WORKERS` | Pipeline and download concurrency limits; adjust for available resources. |

Reuse the existing data volume when upgrading a container. Keep the SQLite database along with the output files. Do not commit `.env`, cookies, OAuth credentials, or generated test media. Google Drive requires a separate sidecar; see the [local quick start (Chinese)](../local-quick-start.md).

## Troubleshooting

- The API responds, but jobs cannot start: inspect `checks` and `capabilities` from `/api/health/ready` for the key, FFmpeg/FFprobe, yt-dlp, and storage status.
- `MODEL_NOT_READY`: download and verify the selected Whisper model in Local Models before creating a subtitle job.
- Hard subtitles are unavailable: install FFmpeg with libass or choose soft subtitles. Check with `ffmpeg -hide_banner -filters | grep ' subtitles '`.
- A URL fails to download: run the download probe first. If the site requires login, configure `SUBTRANS_COOKIES` as described in the [local setup guide (Chinese)](../local-quick-start.md).

## Documentation

Most detailed guides below are in Chinese; the transcriber protocol is in English.

- [Documentation index](../README.md)
- [Local quick start](../local-quick-start.md)
- [Linux deployment](../quick-start-linux.md)
- [MCP Server](../mcp-server.md)
- [MCP Agent guide](../mcp-agent-guide.md)
- [Transcriber service protocol (English)](../transcriber-service.md)
- [Google Drive sidecar](../../drive-service/README.md)

## Development

The project uses Python 3.10–3.12, FastAPI, FFmpeg, and vanilla JavaScript. For local development, run `uv sync` and then `uv run uvicorn src.handler.app:app --port 8000`; the same service hosts the frontend. Run backend tests with `uv run pytest -q` and frontend tests with `npm test` from `web/`. Tests that use real cloud services and downloads require explicit opt-in; see [AGENTS.md (Chinese)](../../AGENTS.md).

`src/handler/` serves the HTTP API; `src/core/` handles downloads, transcription, and subtitles; `src/service/` and `src/store/` manage jobs and persistence; `src/mcp_server/` provides MCP; and `web/` is the browser workbench. See [CONTRIBUTING.md (Chinese)](../../.github/CONTRIBUTING.md) for development guidelines. Report security issues privately through [SECURITY.md](../../.github/SECURITY.md), not a public issue.

## License and compliance

Released under the [MIT License](../../LICENSE). Process only media you are authorized to access, download, transcribe, translate, and redistribute, and comply with the source site's terms, copyright restrictions, and applicable laws.
