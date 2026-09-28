English | [简体中文](../../README.md)

<div align="center">
  <img src="../../web/assets/lynxsense-logo.png" width="88" alt="LynxSense Logo" />
  <h1>LynxSense</h1>
  <p><strong>Sense every signal in media with the acuity of a lynx.</strong></p>
  <p>Turn video and audio into information that people, search systems, and AI can understand and use.</p>
</div>

![LynxSense visual workbench](../assets/subtitles-ai-workbench.png)

LynxSense is a media-understanding workbench. The shipped pipeline downloads media, transcribes speech, translates subtitles, and produces soft or hard-subtitled output through a Web workbench. Classification, vocal expression, and LLM-ready structured understanding are planned extensions.

## Docker quick start

Run this from the repository root:

```bash
cp .env.example .env
# Set SUBTRANS_DEEPSEEK_API_KEY in .env
docker build -t lynxsense:local . && docker run -d --name lynxsense --restart unless-stopped -p 8000:8000 --env-file .env -e SUBTRANS_DATA_DIR=/data -e SUBTRANS_DB=/data/db/app.db -v lynxsense-data:/data lynxsense:local
```

Open <http://localhost:8000/>. See the [documentation index](../README.md) for local development, Linux deployment, and extensions.

## Capabilities

- **Subtitle pipeline**: Download, transcribe, translate, and produce soft or hard subtitles for faster understanding and cross-language viewing.
- **Web workbench**: Queue jobs, follow progress, preview video, edit subtitles, and download results in a browser.
- **MCP integration**: Let Codex, Claude Desktop, and other AI clients create and track jobs from natural language.
- **Google Drive extension**: Upload, download, and organize task files when results need to move through a shared file workflow.
- **Replaceable transcription backends**: Choose local faster-whisper, Replicate, or a compatible HTTP service for different cost, speed, and privacy needs.
- **Media-understanding roadmap**: Add time-ranged, confidence-scored categories, emotion, vocal tone, and key events for search and AI context.

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
