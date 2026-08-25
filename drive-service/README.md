# Subtitles AI Drive Service

这是一个单用户、本机运行的 Google Drive sidecar。它不读取 Python 服务的数据库，也不导入 Python 包；只通过内部 HTTP 调用现有的 `/api/tasks/upload`。Drive 的 OAuth token、上传状态和下载 `.part` 文件都保存在 `data_dir`，进程重启后会自动恢复未完成传输。

## 首次配置

1. 在 Google Cloud Console 创建项目并启用 Google Drive API。
2. 配置 OAuth consent screen，把自己的 Google 账号加入测试用户。
3. 创建 OAuth Client，类型选择 **Desktop app**，下载 JSON 文件。
4. 复制 `config.example.json` 为 `config.local.json`，将 `google_client_config_file` 指向下载的 JSON；也可以直接填写 `google_client_id` 与 `google_client_secret`。
5. 启动服务后打开 `http://127.0.0.1:8787/api/oauth/google/start`，浏览器完成授权。

当前提交中 `google_client_id` 和 `google_client_secret` 都故意留空。若 `google_client_config_file` 也留空，服务会自动读取 `data_dir/oauth_client.json`。sidecar 每次授权都会创建一个随机端口的 `127.0.0.1` loopback 回调，这是 Desktop app 的推荐方式；不支持固定回调地址，也不需要把回调地址写死在代码里。OAuth 客户端 JSON 和 token 都是本地敏感文件，不应提交到 Git。

本地配置必须使用 **Desktop app** 类型的 OAuth Client，以支持动态 loopback 回调。

默认权限是 `https://www.googleapis.com/auth/drive.file`：sidecar 创建的 `Subtitles AI` 文件夹及其文件可被读写，不申请整个 Drive 的权限。

## 运行

```sh
cp config.example.json config.local.json
go run ./cmd/server -config ./config.local.json
```

服务默认只监听 `127.0.0.1:8787`。Python 服务默认地址为 `http://127.0.0.1:8000`；如果 Python 服务配置了 `SUBTRANS_API_TOKEN`，把同一个值填入 `python_api_token`，导入操作会通过 `X-API-Token` 调用 `/api/tasks/upload`。

## API

```text
GET    /healthz
GET    /api/oauth/status
GET    /api/oauth/google/start
POST   /api/oauth/google/disconnect
GET    /api/drive/files?pageToken=&pageSize=
DELETE /api/drive/files/{fileId}                 # 移入 Drive 垃圾箱
GET    /api/drive/files/{fileId}/download        # 支持 Range/HEAD
POST   /api/drive/files/{fileId}/import          # 下载后提交 Python 流水线
POST   /api/drive/uploads                        # 创建浏览器断点上传
HEAD   /api/drive/uploads/{uploadId}              # 查询上传偏移
PATCH  /api/drive/uploads/{uploadId}              # 写入一段数据
DELETE /api/drive/uploads/{uploadId}              # 放弃未完成上传并清理暂存文件
GET    /api/drive/transfers
GET    /api/drive/transfers/{id}
POST   /api/drive/transfers/{id}/pause
POST   /api/drive/transfers/{id}/resume
POST   /api/drive/transfers/{id}/cancel
```

浏览器上传先发送 `X-Upload-Length`、`X-File-Name`、`X-File-Mime`：

```sh
curl -i -X POST http://127.0.0.1:8787/api/drive/uploads \
  -H 'X-Upload-Length: 1048576' -H 'X-File-Name: video.mp4' \
  -H 'X-File-Mime: video/mp4'
```

随后以 `PATCH` 搭配 `X-Upload-Offset` 上传任意大小的片段；服务端返回新的 `Upload-Offset`。客户端中断后先 `HEAD` 查询偏移再继续即可。文件收齐后，sidecar 使用 Google Drive resumable upload，默认 16 MiB（按 Drive 要求对齐到 256 KiB）的块，并将每块完成后的 offset 写入状态文件。

下载同样使用 `Range: bytes=<offset>-` 写入本地 `.part` 文件，支持重试、暂停、取消和进程重启恢复。下载完成后会校验 Drive 提供的 MD5（如果有），再把文件流式提交给 Python 的 `/api/tasks/upload`；sidecar 不在本地引入或复制 Python 业务代码。
