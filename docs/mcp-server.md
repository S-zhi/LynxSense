# MCP Server

MCP Server 是业务 FastAPI 的独立适配层。它不直接访问 SQLite，也不直接调用
下载、识别、翻译和烧录函数，而是通过 HTTP 调用业务 API。

## 启动业务服务

先在项目根目录创建 `.env`：

```ini
REPLICATE_API_TOKEN=your-replicate-token
SUBTRANS_DEEPSEEK_API_KEY=your-deepseek-key
```

然后启动业务服务：

```bash
uv run uvicorn src.handler.app:app --port 8000
```

可以先检查业务服务是否就绪：

```bash
curl http://127.0.0.1:8000/api/health/ready
```

## 启动 MCP Server

桌面 MCP Host 通常使用 stdio：

```bash
uv run python -m src.mcp_server.server
```

MCP Server 默认访问 `http://127.0.0.1:8000`。如果业务服务地址不同，设置：

```bash
export SUBTRANS_API_BASE_URL=http://127.0.0.1:8000
```

也可以启动 Streamable HTTP：

```bash
SUBTRANS_MCP_TRANSPORT=streamable-http \
  uv run python -m src.mcp_server.server
```

默认地址为 `http://127.0.0.1:3001/mcp`。

## MCP 工具

- `check_subtitle_setup`：检查业务服务、密钥、FFmpeg 和存储目录。
- `probe_video`：预检测 URL，不下载文件。
- `start_subtitle_pipeline`：异步启动完整流水线并返回 `task_id`。
- `get_task_status`：查询任务状态和进度。
- `get_task_artifacts`：获取成功任务的视频和字幕下载地址。
- `list_tasks`：查看最近任务。
- `retry_task`：重试失败任务。

## 初始化失败时的行为

MCP 不会要求模型传递 API Key，也不会把密钥写入 MCP 配置。业务配置缺失时，
`check_subtitle_setup` 和 `start_subtitle_pipeline` 会返回项目根目录 `.env` 的
固定位置和缺失项，填写后需要重启业务服务。
