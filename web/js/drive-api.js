/* Google Drive sidecar 的轻量 HTTP 客户端。该模块独立于任务 API，避免两套状态流互相覆盖。 */

const CFG = window.APP_CONFIG || {};
const BASE = String(CFG.DRIVE_API_BASE_URL || "http://127.0.0.1:8787").replace(/\/+$/, "");
const DEFAULT_TIMEOUT = Number(CFG.DRIVE_API_TIMEOUT_MS) || 600000;

/**
 * 发送 sidecar 请求，并为普通请求提供可配置的超时。
 * 上传分片如果传入 AbortSignal，则由调用方控制中断，不再叠加第二个计时器。
 */
async function request(path, options = {}) {
  const callerSignal = options.signal;
  const controller = callerSignal ? null : new AbortController();
  const timer = controller ? setTimeout(() => controller.abort(), DEFAULT_TIMEOUT) : null;
  try {
    return await fetch(`${BASE}${path}`, {
      ...options,
      signal: callerSignal || controller.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError" && !callerSignal) {
      throw new Error("Google Drive 请求超时，请检查 sidecar 是否仍在运行");
    }
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * 将 sidecar 的 JSON 或纯文本错误转换成用户可读的异常。
 */
async function readError(response, fallback) {
  let detail = "";
  try {
    const text = await response.text();
    try {
      const data = JSON.parse(text);
      detail = data?.error || data?.detail || text;
    } catch {
      detail = text;
    }
  } catch {
    detail = "";
  }
  const error = new Error(`${fallback}：${String(detail || response.status)}`);
  error.status = response.status;
  return error;
}

/**
 * 检查响应状态，失败时统一抛出带 HTTP 状态码的错误。
 */
async function ensureOK(response, fallback) {
  if (!response.ok) throw await readError(response, fallback);
  return response;
}

/**
 * 读取 JSON 响应，空响应则返回 null。
 */
async function readJSON(response) {
  const text = await response.text();
  return text ? JSON.parse(text) : null;
}

/**
 * Header values are ByteStrings in browsers, so raw Unicode filenames make
 * fetch throw before the request is sent. Percent-encode UTF-8 into ASCII and
 * let the sidecar decode the explicitly named header.
 */
function uploadMetadataHeaders(file) {
  return {
    "X-Upload-Length": String(file.size),
    "X-File-Name-Encoded": encodeURIComponent(file.name),
    "X-File-Mime": file.type || "application/octet-stream",
  };
}

/**
 * Drive sidecar API。所有路径均相对本机的 8787 端口。
 */
export const DriveApi = {
  /** 返回当前 sidecar 地址，供界面展示或打开授权窗口。 */
  base: BASE,

  /** 查询 OAuth 应用配置和用户连接状态。 */
  async status() {
    const response = await request("/api/oauth/status");
    await ensureOK(response, "读取 Google Drive 状态失败");
    return readJSON(response);
  },

  /** 打开 Google OAuth 动态 loopback 授权入口。 */
  authUrl() {
    return `${BASE}/api/oauth/google/start`;
  },

  /** 删除本地保存的 Refresh Token，使下次操作重新走授权流程。 */
  async disconnect() {
    const response = await request("/api/oauth/google/disconnect", { method: "POST" });
    await ensureOK(response, "断开 Google Drive 失败");
    return readJSON(response);
  },

  /** 获取 sidecar 专属 Drive 文件夹中的一页文件。 */
  async listFiles(parentId = "", pageToken = "", pageSize = 100) {
    // 兼容早期的 listFiles(pageToken, pageSize) 调用，避免旧页面在
    // sidecar 更新期间把数字页大小误当成 parentId/pageToken。
    if (arguments.length === 1 && parentId) {
      pageToken = parentId;
      parentId = "";
    } else if (typeof pageToken === "number") {
      pageSize = pageToken;
      pageToken = parentId;
      parentId = "";
    }
    if (typeof pageSize !== "number" || !Number.isFinite(pageSize)) pageSize = 100;
    const query = new URLSearchParams({ pageSize: String(pageSize) });
    if (parentId) query.set("parentId", parentId);
    if (pageToken) query.set("pageToken", pageToken);
    const response = await request(`/api/drive/files?${query}`);
    await ensureOK(response, "读取 Google Drive 文件失败");
    return readJSON(response);
  },

  async createFolderUpload(manifest, signal, clientRequestId = "", parentId = "") {
    const body = { entries: manifest };
    if (clientRequestId) body.clientRequestId = clientRequestId;
    if (parentId) body.parentId = parentId;
    const response = await request("/api/drive/folder-uploads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
    await ensureOK(response, "创建文件夹上传失败");
    return readJSON(response);
  },
  async folderUploadStatus(id) {
    const response = await request(`/api/drive/folder-uploads/${encodeURIComponent(id)}`);
    await ensureOK(response, "读取文件夹上传进度失败");
    return readJSON(response);
  },
  async folderUploadAction(id, action, signal) {
    const response = await request(`/api/drive/folder-uploads/${encodeURIComponent(id)}/${encodeURIComponent(action)}`, { method: "POST", signal });
    await ensureOK(response, `文件夹上传${action}失败`);
    return readJSON(response);
  },
  async folderEntryAction(batchId, entryId, action, signal) {
    const response = await request(`/api/drive/folder-uploads/${encodeURIComponent(batchId)}/entries/${encodeURIComponent(entryId)}/${encodeURIComponent(action)}`, { method: "POST", signal });
    await ensureOK(response, `文件夹条目${action}失败`);
    return readJSON(response);
  },
  async createFolderEntryUpload(batchId, entryId, file, signal) {
    const response = await request(`/api/drive/folder-uploads/${encodeURIComponent(batchId)}/entries/${encodeURIComponent(entryId)}/upload`, { method: "POST", headers: uploadMetadataHeaders(file), signal });
    await ensureOK(response, "创建文件夹条目上传失败");
    return readJSON(response);
  },

  /** 创建本地暂存上传记录，并获取分片上传地址。 */
  async createUpload(file, signal) {
    const response = await request("/api/drive/uploads", {
      method: "POST",
      headers: uploadMetadataHeaders(file),
      signal,
    });
    await ensureOK(response, "创建 Drive 上传失败");
    return readJSON(response);
  },

  /** 查询本地暂存上传已经确认的字节偏移。 */
  async uploadOffset(id) {
    const response = await request(`/api/drive/uploads/${encodeURIComponent(id)}`, { method: "HEAD" });
    await ensureOK(response, "查询上传进度失败");
    return Number(response.headers.get("Upload-Offset") || 0);
  },

  /** 写入一个上传分片，并返回服务端确认的偏移和最终传输 ID。 */
  async uploadChunk(id, blob, offset, signal) {
    const response = await request(`/api/drive/uploads/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Upload-Offset": String(offset),
      },
      body: blob,
      signal,
    });
    await ensureOK(response, "上传 Drive 分片失败");
    return {
      offset: Number(response.headers.get("Upload-Offset") || offset + blob.size),
      transferID: response.headers.get("X-Transfer-ID") || "",
    };
  },

  /** 删除尚未进入 Drive worker 的本地暂存上传。 */
  async deleteUpload(id) {
    const response = await request(`/api/drive/uploads/${encodeURIComponent(id)}`, { method: "DELETE" });
    await ensureOK(response, "取消本地上传失败");
  },

  /** 列出所有可恢复的 Drive 上传和 Python 导入传输。 */
  async listTransfers() {
    const response = await request("/api/drive/transfers");
    await ensureOK(response, "读取传输队列失败");
    const data = await readJSON(response);
    return Array.isArray(data?.transfers) ? data.transfers : [];
  },

  /** 对单个传输执行 pause、resume 或 cancel 操作。 */
  async transferAction(id, action) {
    const response = await request(
      `/api/drive/transfers/${encodeURIComponent(id)}/${encodeURIComponent(action)}`,
      { method: "POST" },
    );
    await ensureOK(response, `传输${action}失败`);
    return readJSON(response);
  },

  /** 创建 Drive 文件到 Python 字幕流水线的异步导入任务。 */
  async importFile(id) {
    const response = await request(`/api/drive/files/${encodeURIComponent(id)}/import`, { method: "POST" });
    await ensureOK(response, "导入任务创建失败");
    return readJSON(response);
  },

  /** 将 Drive 文件移动到回收站，而不是执行不可逆删除。 */
  async trashFile(id) {
    const response = await request(`/api/drive/files/${encodeURIComponent(id)}`, { method: "DELETE" });
    await ensureOK(response, "移入回收站失败");
  },

  /** 返回可直接交给浏览器下载的地址。 */
  downloadUrl(id) {
    return `${BASE}/api/drive/files/${encodeURIComponent(id)}/download`;
  },
};
