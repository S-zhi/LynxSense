English | [简体中文](../../README.md)

<div align="center">
  <img src="../../web/assets/translatedsubs-logo.svg" width="88" alt="TranslatedSubs Logo" />
  <h1>TranslatedSubs</h1>
  <p><strong>From video to translated subtitles in one workflow.</strong></p>
  <p>Download, transcribe, translate, and package subtitles. Preview, edit, and download results in the Web workbench.</p>
</div>

TranslatedSubs is a video and audio subtitle workbench. It downloads media, transcribes speech, translates subtitles, and produces soft or hard-subtitled output. Jobs can be managed through the Web workbench or MCP.

## Docker quick start

Run this from the repository root:

```bash
cp .env.example .env
# Set SUBTRANS_DEEPSEEK_API_KEY in .env
docker build -t translatedsubs:local . && docker run -d --name translatedsubs --restart unless-stopped -p 8000:8000 --env-file .env -e SUBTRANS_DATA_DIR=/data -e SUBTRANS_DB=/data/db/app.db -v translatedsubs-data:/data translatedsubs:local
```

When upgrading an existing container, replace `translatedsubs-data` with the existing volume name to retain jobs and output files. The `SUBTRANS_*` environment variables remain supported.

Open <http://localhost:8000/>. See the [documentation index](../README.md) for local development, Linux deployment, and extensions.

## Capabilities

- **Subtitle pipeline**: Download, transcribe, translate, and produce soft or hard subtitles for faster understanding and cross-language viewing.
- **Web workbench**: Queue jobs, follow progress, preview video, edit subtitles, and download results in a browser.
- **MCP integration**: Let Codex, Claude Desktop, and other AI clients create and track jobs from natural language.
- **Google Drive extension**: Upload, download, and organize task files when results need to move through a shared file workflow.
- **Replaceable transcription backends**: Choose local faster-whisper, Replicate, or a compatible HTTP service for different cost, speed, and privacy needs.

## Documentation

- [Documentation index](../README.md)
- [Local quick start](../local-quick-start.md)
- [Linux deployment](../quick-start-linux.md)
- [MCP Server](../mcp-server.md)
- [MCP Agent guide](../mcp-agent-guide.md)
- [Transcriber service protocol](../transcriber-service.md)
- [Google Drive sidecar](../../drive-service/README.md)

## License

Released under the [MIT License](../../LICENSE). Process only media you are authorized to access, download, transcribe, translate, and redistribute.
