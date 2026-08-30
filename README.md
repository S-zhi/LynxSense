[English](./README.en.md) | 简体中文

<div align="center">
  <img src="./web/assets/subtitles-ai-logo.svg" width="88" alt="Subtitles AI Logo" />
  <h1>Subtitles AI</h1>
  <p><strong>把任意视频变成可理解、可翻译、可交付的字幕成片。</strong></p>
  <p>既是开箱即用的可视化字幕工作台，也是可被 AI Agent 调用的 MCP 字幕能力。</p>

  <p>
    <a href="#-快速开始">快速开始</a> ·
    <a href="#-mcp-接入">MCP 接入</a> ·
    <a href="#-web-工作台">Web 工作台</a> ·
    <a href="./docs/mcp-server.md">完整文档</a>
  </p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10--3.12-3776AB?logo=python&logoColor=white" alt="Python" />
    <img src="https://img.shields.io/badge/MCP-2.x-6C5CE7" alt="MCP" />
    <img src="https://img.shields.io/badge/FastAPI-API-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
    <img src="https://img.shields.io/badge/FFmpeg-libass-388E3C?logo=ffmpeg&logoColor=white" alt="FFmpeg" />
    <img src="https://img.shields.io/badge/License-MIT-F5C518" alt="MIT License" />
  </p>
</div>

![Subtitles AI 可视化工作台](./docs/assets/subtitles-ai-workbench.png)

Subtitles AI 将视频下载、音频提取、语音识别、字幕翻译与字幕烧录串成一条自动化流水线。粘贴视频页面地址或拖入本地视频，即可获得翻译后的 SRT 与成品视频；也可以把同一套能力接入支持 MCP 的 AI 客户端，让 Agent 自主检查环境、创建任务、跟踪进度并交付产物。

## ✨ 两种使用方式

| | 🤖 MCP 接入 | 🖥️ Web 工作台 |
| --- | --- | --- |
| 适合谁 | 希望让 Codex、Claude Desktop 等 AI 客户端处理视频的用户 | 希望直接在浏览器中操作的用户 |
| 怎么使用 | 用自然语言让 Agent 创建、查询、重试字幕任务 | 粘贴 URL 或拖入视频，选择语言与字幕参数 |
| 核心体验 | 工具可发现、状态可追踪、结果结构化返回 | 任务队列、实时进度、视频预览、字幕编辑与下载 |
| 接入方式 | stdio 或 Streamable HTTP | 本地打开 `http://localhost:8000` |

```mermaid
flowchart LR
    Input["视频 URL / 本地视频"] --> Entry{"选择入口"}
    Entry -->|自然语言| Agent["AI Agent + MCP"]
    Entry -->|可视化操作| Web["Web 工作台"]
    Agent --> API["Subtitles AI API"]
    Web --> API
    API --> Pipeline["下载 → 识别 → 翻译 → 烧录"]
    Pipeline --> Output["成品视频 + SRT 字幕"]
```

## 🚀 快速开始

### Ubuntu / Debian 一键安装（推荐）

登录服务器后执行这一行即可：

```bash
curl -fsSL https://raw.githubusercontent.com/S-zhi/Subtitles-AI/main/scripts/install-linux.sh | sudo bash
```

安装器会在终端中静默询问 Replicate 和 DeepSeek 密钥，并自动完成代码下载、FFmpeg、uv、Python 3.12、项目依赖、持久化目录、systemd 服务和健康检查。安装结束后打开 `http://服务器IP:8000/`。

```bash
systemctl status subtitles-ai --no-pager
journalctl -u subtitles-ai -f
```

脚本支持 Ubuntu/Debian，重复执行时留空密钥即可保留原值。公网访问前还需要在云平台安全组中放行 TCP 8000，生产环境建议改用 HTTPS 反向代理。参数、非交互安装和故障排查见 [Linux 部署文档](./docs/quick-start-linux.md)。

### macOS 本地运行

#### 1. 准备环境

当前已在 macOS 验证，需要 Python `3.10–3.12`、[uv](https://docs.astral.sh/uv/) 和带 `libass` 的 FFmpeg：

```bash
brew install uv
brew tap homebrew-ffmpeg/ffmpeg
brew install ffmpeg-full
uv sync
```

确认字幕滤镜可用：

```bash
ffmpeg -hide_banner -filters | grep " subtitles "
```

> 普通版 FFmpeg 可能不包含 `libass`，硬字幕会因此无法烧录；遇到该情况也可以选择软字幕模式。

#### 2. 配置密钥

```bash
cp .env.example .env
```

在 `.env` 中填写：

```ini
REPLICATE_API_TOKEN=your-replicate-token
SUBTRANS_DEEPSEEK_API_KEY=your-deepseek-key
```

密钥只由业务服务读取，不应写入 MCP 参数或提交到仓库。

#### 3. 配置 Google Drive（可选）

Google Drive sidecar 使用本地配置文件读取 OAuth 应用身份；该文件已被 Git 忽略，不会提交到仓库：

```bash
cp drive-service/config.example.json drive-service/config.local.json
```

然后在 `drive-service/config.local.json` 中填写 `google_client_id`、`google_client_secret`，或将 Google Desktop OAuth JSON 放到 `drive-service/drive-data/oauth_client.json`。首次使用时，在网页的 **Google Drive** 页面点击授权，浏览器会打开动态 loopback 回调；Refresh Token 会保存在本地 `drive-data` 目录。OAuth Client 必须是 Desktop app 类型；sidecar 默认只监听本机 `127.0.0.1:8787`，不支持固定回调地址，也不应把 OAuth JSON、Refresh Token 或 Client Secret 提交到 Git。

#### 4. 启动业务服务和 Drive sidecar

`./scripts/start.sh` 会同时启动 Python 业务服务和 Google Drive sidecar；只有使用 Google Drive 时才需要准备 `drive-service/config.local.json`。如果暂时不用云盘功能，可以只启动 API，避免启动 sidecar。

```bash
./scripts/start.sh
```

如果只需要业务 API（例如不使用 Google Drive），可单独启动：

```bash
uv run uvicorn src.handler.app:app --port 8000
```

如需指定其他 Drive 配置文件，可使用：

```bash
DRIVE_CONFIG=/absolute/path/to/config.local.json ./scripts/start.sh
```

验证服务：

```bash
curl http://127.0.0.1:8000/api/health
# {"ok":true}
curl http://127.0.0.1:8787/healthz
# {"ok":true}
```

现在可以选择以下任一路径：

- 想直接操作：打开 <http://localhost:8000/>。
- 想让 AI Agent 操作：继续阅读 [MCP 接入](#-mcp-接入)。

### Google Drive 任务级同步

Google Drive 仅同步用户明确选择的文件或任务产物，不会自动同步全部本地资源，也不会按 Web 页面的保留策略自动清理云端文件。单文件上传支持可恢复分片；下载或导入会使用 `Range` 续传，完成后在可用时校验 Drive 提供的 MD5，再提交业务 API。

文件夹批量上传通过 manifest 建立任务级目录：路径必须是相对路径，批次和条目分别提供状态、进度、暂停/恢复、取消与失败重试；相同 `Idempotency-Key` 或 `X-Client-Request-ID` 可安全重试批次创建。以任务 ID 创建的目录通过元数据关联同一任务，重复请求不会重复创建目录。Drive 文件夹不能下载或导入 Python 流水线。

`drive-service/README.md` 列出了 sidecar API；真实 OAuth、云端传输和凭据测试不属于本地验证范围。

## 🤖 MCP 接入

MCP Server 是业务 API 的独立适配层。它不会直接访问 SQLite，也不会持有 Replicate 或 DeepSeek 密钥。

### stdio：接入桌面 AI 客户端

在 MCP 客户端配置中加入以下内容，并把 `/absolute/path/to/Subtitles-AI` 改为本仓库的绝对路径：

```json
{
  "mcpServers": {
    "subtitles-ai": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/Subtitles-AI",
        "run",
        "python",
        "-m",
        "src.mcp_server.server"
      ],
      "env": {
        "SUBTRANS_API_BASE_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

重启 MCP 客户端后，可以直接说：

> 检查字幕服务是否就绪，把这个视频翻译成中英双语字幕，使用软字幕，并在完成后给我下载地址：`<视频 URL>`

Agent 会按以下可追踪流程执行：

```text
check_subtitle_setup → probe_video → start_subtitle_pipeline
→ get_task_status（轮询）→ get_task_artifacts（成功后）
```

首次调用前，先确保业务 API 已启动并检查 `/api/health/ready`。`check_subtitle_setup` 会返回 `initialized`、`missing`、`config_file`、`agent_action` 和 `restart_required`：配置缺失时，按返回路径补齐业务服务项目根目录的 `.env`，重启 FastAPI 后再次检查；不要把业务密钥传入 MCP 参数。`start_subtitle_pipeline` 只表示任务已入队，必须保存 `task_id` 并轮询状态，不要提前读取产物。

任务进入 `FAILED` 时先向用户展示错误和阶段，只有用户确认后才调用 `retry_task`。如果返回 `TASK_ALREADY_RUNNING`，复用返回的 `task_id`；只有状态为 `SUCCESS` 时才调用 `get_task_artifacts`。`RESOURCE_MISSING` 表示产物已被清理，需要重新运行任务；`HARD_BURN_UNAVAILABLE` 时应询问用户改用 `burn=soft` 或安装带 libass 的 FFmpeg，不能静默改变明确指定的硬字幕选项。

`start_subtitle_pipeline` 的默认参数为 `source_lang=auto`、`target_lang=zh-CN`、`mode=mono`、`burn=hard`、`model=small` 和 `need_subtitle=true`。支持的主要错误码包括 `BUSINESS_UNAVAILABLE`、`NOT_INITIALIZED`、`INVALID_URL`、`PROBE_FAILED`、`INVALID_ARGUMENT`、`TASK_NOT_READY`、`TASK_NOT_FOUND` 和 `RESOURCE_MISSING`。

### Streamable HTTP：接入远程或共享 Host

```bash
SUBTRANS_MCP_TRANSPORT=streamable-http \
  uv run python -m src.mcp_server.server
```

默认 MCP 地址：`http://127.0.0.1:3001/mcp`。

### MCP 能力一览

| 工具 | 用途 |
| --- | --- |
| `check_subtitle_setup` | 检查业务服务、密钥、FFmpeg 与存储是否就绪 |
| `probe_video` | 在下载前验证视频 URL |
| `start_subtitle_pipeline` | 异步创建下载 / 识别 / 翻译 / 烧录任务 |
| `get_task_status` | 查询阶段、进度与错误信息 |
| `get_task_artifacts` | 获取成功任务的视频与字幕地址 |
| `list_tasks` | 查看最近任务 |
| `retry_task` | 经用户确认后重试失败任务 |

完整配置、错误码和 Agent 行为规范见 [MCP Server 文档](./docs/mcp-server.md) 与 [MCP Agent 指南](./docs/mcp-agent-guide.md)。

## 🖥️ Web 工作台

Web 工作台与 API 共用 `8000` 端口，无需单独启动前端服务；Google Drive 页面在启用云盘功能时通过 CORS 访问同机的 `8787` sidecar。sidecar 是本机 Drive 适配层，不是业务 API；两项服务都默认仅供本机访问。

1. 打开 <http://localhost:8000/>。
2. 粘贴视频页面地址，或拖入本地视频。
3. 选择源语言、目标语言、仅译文 / 双语字幕、硬烧录 / 软字幕和识别模型。
4. 点击「开始处理」，在任务队列中查看实时进度。
5. 完成后预览视频、编辑字幕，并下载视频或 SRT 文件。

主要能力：

- **任务中心**：批量任务队列、实时阶段与失败重试。
- **视频预览**：在浏览器中直接检查最终效果。
- **字幕编辑**：查看并调整识别或翻译后的字幕。
- **本地资源**：查看磁盘占用、保留策略和清理预览；默认保留产物 30 天，自动清理只删除产物并保留任务记录。
- **灵活输出**：仅下载视频、单语 / 双语字幕、硬烧录 / 软字幕。

## 🧩 无页面使用

仓库当前没有独立的 `main.py` 命令行入口。不打开 Web 页面时，请启动 API，并通过 MCP 客户端调用 `start_subtitle_pipeline`；完整的 stdio 配置和调用顺序见上方 [MCP 接入](#-mcp-接入)。

## 🏗️ 工作原理

```mermaid
flowchart LR
    Client["Web / MCP"] --> API["FastAPI"]
    API --> Runner["后台任务队列"]
    Runner --> Download["yt-dlp 下载"]
    Download --> Audio["FFmpeg 提取音频"]
    Audio --> ASR["Replicate Whisper 识别"]
    ASR --> Translate["DeepSeek 翻译"]
    Translate --> Burn["FFmpeg 字幕封装"]
    Burn --> Store["视频 + SRT + SQLite 状态"]
```

任务状态：

```text
PENDING → DOWNLOADING → EXTRACTING → TRANSCRIBING
→ TRANSLATING → BURNING → SUCCESS
```

任一步失败会进入 `FAILED`，并保存失败阶段与错误信息。任务产物默认位于 `data/{task_id}/`。

## ⚙️ 常用配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SUBTRANS_DATA_DIR` | `./data` | 任务产物目录 |
| `SUBTRANS_DB` | `./app.db` | SQLite 数据库路径 |
| `SUBTRANS_WORKERS` | `8` | 整条后台流水线的并发上限（应高于下载并发） |
| `SUBTRANS_DOWNLOAD_WORKERS` | `2` | 同时进行 yt-dlp 媒体下载的任务数 |
| `SUBTRANS_DL_CONCURRENT_FRAGMENTS` | `4` | 单个 HLS/DASH 下载的分片并发数 |
| `SUBTRANS_COOKIES` | 空 | 需要登录或验证的网站 cookies 文件 |
| `SUBTRANS_WHISPER_MODEL` | 锁定版本 | Replicate Whisper 模型 |
| `SUBTRANS_DEEPSEEK_MODEL` | `deepseek-chat` | 翻译模型 |
| `SUBTRANS_API_BASE_URL` | `http://127.0.0.1:8000` | MCP 访问的业务 API 地址 |
| `SUBTRANS_MCP_TRANSPORT` | `stdio` | `stdio` 或 `streamable-http` |
| `SUBTRANS_MCP_HOST` | `127.0.0.1` | Streamable HTTP 监听地址 |
| `SUBTRANS_MCP_PORT` | `3001` | Streamable HTTP 监听端口 |
| `SUBTRANS_MCP_PATH` | `/mcp` | Streamable HTTP 路径 |

完整配置项见 [.env.example](./.env.example) 与 [mcp.env.example](./mcp.env.example)。启动后可访问 <http://localhost:8000/docs> 查看 API 文档。

## 🧪 开发与验证

```bash
uv sync
uv run pytest -q
cd web && npm test
```

项目主要目录：

```text
src/core/          下载、音频、识别、翻译与字幕烧录
src/handler/       FastAPI 路由与前端静态托管
src/mcp_server/    MCP Server、工具与业务 API 客户端
src/service/       流水线编排与后台执行
src/store/         SQLite 任务存储
web/               原生 HTML / CSS / JavaScript 工作台
tests/             Python 测试
```

参与开发前请阅读 [CONTRIBUTING.md](./CONTRIBUTING.md)。安全问题请通过 [SECURITY.md](./SECURITY.md) 中的私密渠道报告，不要创建公开 Issue。

## ❓ 常见问题

| 问题 | 处理方式 |
| --- | --- |
| 硬字幕提示缺少 `subtitles` filter | 安装 `ffmpeg-full`，或改用 `--burn soft` |
| 下载失败或网站要求登录 | 检查 URL，并通过 `SUBTRANS_COOKIES` 指定 cookies 文件 |
| MCP 返回 `BUSINESS_UNAVAILABLE` | 启动业务 API（`./scripts/start.sh` 或 API-only 命令），确认 8000 端口可访问；使用 Google Drive 时再确认 8787 端口 |
| MCP 返回 `NOT_INITIALIZED` | 补齐 `.env` 中的密钥并重启业务服务 |
| 前端无法连接后端 | 确认 <http://localhost:8000/api/health> 可访问 |
| Google Drive 页面显示 sidecar 离线 | 确认已按配置启动 sidecar，并检查 <http://127.0.0.1:8787/healthz>；只启动 API-only 命令不会提供 Drive 功能 |

## 📄 许可证与合规

本项目基于 [MIT License](./LICENSE) 发布。

请仅处理你有权访问、下载、转写、翻译和再发布的视频，并遵守目标网站服务条款、版权限制及所在地法律。
