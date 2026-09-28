# 本地快速启动

本文说明如何在本地运行 LynxSense。只需要字幕流水线时启动业务 API；需要 Google Drive 时再启动 sidecar。

## 环境准备

需要 Python 3.10–3.12、[uv](https://docs.astral.sh/uv/) 和带 `libass` 的 FFmpeg。macOS 可执行：

```bash
brew install uv
brew tap homebrew-ffmpeg/ffmpeg
brew install ffmpeg-full
uv sync
```

Linux 用户可参考 [Linux 部署文档](./quick-start-linux.md) 的系统依赖和服务配置。

确认硬字幕滤镜：

```bash
ffmpeg -hide_banner -filters | grep " subtitles "
```

## 配置并启动业务 API

```bash
cp .env.example .env
```

在 `.env` 中填写 `SUBTRANS_DEEPSEEK_API_KEY`。需要登录或年龄验证的网站，再配置 `SUBTRANS_COOKIES`；完整变量见 [`.env.example`](../.env.example)。

SQLite 数据库默认位于 `data/db/app.db`，SQLite 运行时可能在同一目录创建 `app.db-wal` 和 `app.db-shm`。已有安装如果在 `.env` 中设置了 `SUBTRANS_DB=./app.db`，需要停掉 API 后，将原数据库迁移到新目录并更新该变量。先确认没有进程使用数据库，执行 `mkdir -p data/db`，再把 `app.db` 以及存在的 `app.db-wal`、`app.db-shm` 一起移动到 `data/db/`；不要在服务运行时单独移动这些文件。自定义 `SUBTRANS_DATA_DIR` 时，未设置 `SUBTRANS_DB` 的默认位置是该数据目录下的 `db/app.db`。

启动 API：

```bash
uv run uvicorn src.handler.app:app --port 8000
```

验证：

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
```

然后访问 <http://127.0.0.1:8000/>。不使用 Web 页面时，可按 [MCP Server 文档](./mcp-server.md) 启动 MCP 客户端接入。

## 可选：Google Drive sidecar

复制本地配置并填写 OAuth Desktop app 信息：

```bash
cp drive-service/config.example.json drive-service/config.local.json
```

然后运行：

```bash
./scripts/start.sh
```

该脚本会同时启动业务 API 和 sidecar。只使用字幕流水线时，直接运行上面的 `uvicorn` 命令即可。OAuth 文件、Refresh Token 和 Client Secret 不要提交到 Git。详细 API 和同步行为见 [Google Drive sidecar README](../drive-service/README.md)。

## 常见本地问题

- 没有 `subtitles` 滤镜时无法硬字幕烧录，可安装带 libass 的 FFmpeg 或改用软字幕。
- `BUSINESS_UNAVAILABLE` 表示业务 API 没有启动。
- `NOT_INITIALIZED` 表示 `.env` 缺少配置，补齐后重启 API。
- Google Drive 页面显示 sidecar 离线时，检查 `http://127.0.0.1:8787/healthz`。
