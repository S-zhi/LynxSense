# Transcriber service protocol

The transcription stage accepts either the default Replicate backend or a compatible custom HTTP service. Select the backend with `SUBTRANS_TRANSCRIBER_BACKEND`; the default is `replicate`.

## Configuration

```dotenv
SUBTRANS_TRANSCRIBER_BACKEND=http
SUBTRANS_TRANSCRIBER_URL=https://stt.example.com/v1/transcribe
SUBTRANS_TRANSCRIBER_API_KEY=optional-bearer-token
SUBTRANS_TRANSCRIBER_TIMEOUT=1800
```

Use `replicate` (or omit the setting) to keep the existing Replicate Whisper behavior. `local_whisper` runs the OpenAI open-source Whisper model locally through `faster-whisper`. `http`, `custom`, and `custom_http` select the custom HTTP adapter.

For the directly embedded local backend:

```dotenv
SUBTRANS_TRANSCRIBER_BACKEND=local_whisper
SUBTRANS_LOCAL_WHISPER_MODEL=small
SUBTRANS_LOCAL_WHISPER_DEVICE=cpu
SUBTRANS_LOCAL_WHISPER_COMPUTE_TYPE=int8
```

The `faster-whisper` package is already included in this project's dependencies. Run `uv sync --dev` to install them. The first request downloads the selected model from Hugging Face. Use `tiny` for a quick CPU smoke test, `small` for the default quality/speed balance, or `medium`/`large-v3` when the machine has enough memory. GPU users can set `SUBTRANS_LOCAL_WHISPER_DEVICE=cuda` and a CUDA-compatible compute type such as `float16`. A model selected for an individual task overrides `SUBTRANS_LOCAL_WHISPER_MODEL`.

The same implementation is available as a standalone HTTP service:

```bash
uv run uvicorn src.transcriber_server:app --host 0.0.0.0 --port 8010
```

Set `SUBTRANS_TRANSCRIBER_API_KEY` in the server environment and the same value in the main application's environment. Then point the main application at `http://127.0.0.1:8010/transcribe` with `SUBTRANS_TRANSCRIBER_BACKEND=http` and `SUBTRANS_TRANSCRIBER_URL`. The standalone service requires this Bearer key before accepting audio or fetching an `audio_url`.

## Request

The adapter sends one `POST` request to `SUBTRANS_TRANSCRIBER_URL`.

For a remote audio input, the request is JSON:

```json
{
  "audio_url": "https://example.com/audio.wav",
  "task_id": "task-123",
  "language": "en",
  "model": "small"
}
```

For a local audio input, the request is `multipart/form-data`. The audio is in the `audio` part and the same `task_id`, `language`, and `model` fields are sent as form fields. `language` can be omitted or null for automatic detection. The `Authorization: Bearer ...` header is sent when `SUBTRANS_TRANSCRIBER_API_KEY` is configured.

## Response

Return HTTP 2xx with a JSON object containing either timestamped `segments` or an `srt` string:

```json
{
  "language": "en",
  "language_probability": 0.99,
  "duration": 12.5,
  "segments": [
    {"text": "Hello", "start": 0.0, "end": 1.2}
  ]
}
```

Each segment must contain non-empty `text`, numeric `start`, and numeric `end`, with `end > start`. The application validates the response and writes the normalized result to `data/{task_id}/original.srt`. HTTP 401/403, 429, 5xx, and other 4xx responses are mapped to the same stable error codes used by the Replicate backend.

For in-process integrations, implement `TranscriberService.transcribe()` and return a `TranscribeResponse` (or a raw output accepted by `_extract_segments`). The request object is `TranscribeRequest` and includes `audio_path`, `task_id`, `language`, and `model_name`.
