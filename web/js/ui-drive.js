/* Google Drive 视图：授权、文件列表、分片上传和后台传输控制。 */

import { $, escapeHtml } from "./utils.js";
import { state } from "./store.js";
import { toast } from "./toast.js";
import { DriveApi } from "./drive-api.js";

const CHUNK_SIZE = 16 * 1024 * 1024;
const REFRESH_INTERVAL = 5000;

const STATUS_LABELS = {
  PENDING: "等待中",
  TRANSFERRING: "传输中",
  RETRYING: "重试中",
  VERIFYING: "校验中",
  PAUSED: "已暂停",
  SUCCESS: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const KIND_LABELS = {
  DRIVE_UPLOAD: "上传 Drive",
  PYTHON_IMPORT: "导入字幕流水线",
};

const local = {
  auth: null,
  authError: "",
  files: [],
  filesError: "",
  transfers: [],
  transfersError: "",
  refreshing: false,
  upload: null,
  timer: null,
};

/**
 * 初始化 Google Drive 视图，并把按钮事件绑定到独立的 sidecar 状态流。
 */
export function initDrive() {
  const panel = $("#drivePanel");
  if (!panel) return;

  $("#driveRefresh")?.addEventListener("click", () => refresh(true));
  $("#driveAuthAction")?.addEventListener("click", openAuth);
  $("#driveDisconnect")?.addEventListener("click", disconnect);
  $("#driveFileInput")?.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) void startUpload(file);
    event.target.value = "";
  });
  $("#driveUploadCancel")?.addEventListener("click", cancelUpload);
  bindDropzone();

  $("#driveFiles")?.addEventListener("click", handleFileAction);
  $("#driveTransfers")?.addEventListener("click", handleTransferAction);
  document.addEventListener("viewchange", handleViewChange);

  if (state.view === "drive") void refresh();
}

/**
 * 让拖放区域同时支持鼠标拖放和键盘激活文件选择器。
 */
function bindDropzone() {
  const dropzone = $("#driveDropzone");
  const input = $("#driveFileInput");
  if (!dropzone || !input) return;

  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove("is-dragging");
    });
  });
  dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) void startUpload(file);
  });
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  });
}

/**
 * 当用户切换到 Drive 视图时加载数据，并在离开时停止轮询。
 */
function handleViewChange(event) {
  const view = event.detail?.view;
  if (view === "drive") {
    void refresh();
    startPolling();
  } else {
    stopPolling();
  }
}

/**
 * 轮询后台传输状态，让上传和导入在浏览器等待期间持续可见。
 */
function startPolling() {
  stopPolling();
  local.timer = setInterval(() => {
    if (state.view === "drive" && !local.refreshing) void refresh();
  }, REFRESH_INTERVAL);
}

/**
 * 停止 Drive 视图的后台轮询。
 */
function stopPolling() {
  if (local.timer) clearInterval(local.timer);
  local.timer = null;
}

/**
 * 并行读取 OAuth、文件和传输状态；OAuth 未连接时不请求 Drive 文件列表。
 */
async function refresh(showToast = false) {
  if (local.refreshing) return;
  local.refreshing = true;
  render();

  try {
    const [authResult, transferResult] = await Promise.allSettled([
      DriveApi.status(),
      DriveApi.listTransfers(),
    ]);

    local.authError = authResult.status === "rejected" ? messageOf(authResult.reason, "无法连接 Drive sidecar") : "";
    local.auth = authResult.status === "fulfilled" ? authResult.value : null;

    if (transferResult.status === "fulfilled") {
      local.transfers = transferResult.value;
      local.transfersError = "";
    } else {
      local.transfersError = messageOf(transferResult.reason, "无法读取传输队列");
    }

    if (local.auth?.connected) {
      try {
        const result = await DriveApi.listFiles();
        local.files = Array.isArray(result?.files) ? result.files : [];
        local.filesError = "";
      } catch (error) {
        local.filesError = messageOf(error, "无法读取 Drive 文件");
      }
    } else {
      local.files = [];
      local.filesError = "";
    }

    if (showToast) toast("Google Drive 状态已刷新", "ph-check-circle");
  } finally {
    local.refreshing = false;
    render();
  }
}

/**
 * 在新窗口打开动态 loopback OAuth 页面，避免当前工作台被 Google 页面替换。
 */
function openAuth() {
  if (local.auth && !local.auth.configured) {
    toast(local.auth.configuration_error || "请先配置 Google OAuth ClientID 和 ClientSecret", "ph-warning");
    return;
  }
  const popup = window.open(DriveApi.authUrl(), "google-drive-oauth", "noopener,noreferrer");
  if (!popup) toast("授权窗口被浏览器拦截，请允许本地页面打开新窗口", "ph-warning");
  else toast("授权页面已打开，完成后回到这里刷新状态", "ph-arrow-square-out");
}

/**
 * 撤销并删除 sidecar 本地保存的 Refresh Token。
 */
async function disconnect() {
  if (!window.confirm("断开 Google Drive 后，下次操作需要重新授权。继续吗？")) return;
  const button = $("#driveDisconnect");
  if (button) button.disabled = true;
  try {
    await DriveApi.disconnect();
    toast("Google Drive 已断开", "ph-sign-out");
    await refresh();
  } catch (error) {
    toast(messageOf(error, "断开 Google Drive 失败"), "ph-warning");
  } finally {
    if (button) button.disabled = false;
  }
}

/**
 * 以 16 MiB 分片上传文件，并在网络短暂中断后用 HEAD 重新对齐服务端偏移。
 */
async function startUpload(file) {
  if (!file?.size) {
    toast("不能上传空文件", "ph-warning");
    return;
  }
  if (!local.auth?.connected) {
    toast("请先连接 Google Drive", "ph-cloud-slash");
    return;
  }
  if (local.upload) {
    toast("当前已有文件正在上传", "ph-hourglass-simple");
    return;
  }

  const upload = {
    file,
    id: "",
    offset: 0,
    transferID: "",
    controller: new AbortController(),
    cancelled: false,
    done: false,
    error: "",
  };
  local.upload = upload;
  render();

  try {
    const created = await DriveApi.createUpload(file, upload.controller.signal);
    upload.id = created.id;
    upload.offset = Number(created.offset || 0);
    if (upload.cancelled) {
      await DriveApi.deleteUpload(upload.id);
      return;
    }
    render();

    while (upload.offset < file.size) {
      const before = upload.offset;
      const chunk = file.slice(before, Math.min(before + CHUNK_SIZE, file.size));
      try {
        const result = await DriveApi.uploadChunk(upload.id, chunk, before, upload.controller.signal);
        upload.offset = result.offset;
        upload.transferID = result.transferID || upload.transferID;
      } catch (error) {
        if (error?.status === 409 && !upload.cancelled) {
          upload.offset = await DriveApi.uploadOffset(upload.id);
          continue;
        }
        throw error;
      }
      if (upload.offset <= before) throw new Error("sidecar 没有推进上传偏移");
      render();
    }

    upload.done = true;
    toast("文件已上传，Drive 后台任务已排队", "ph-check-circle");
    await refresh();
    window.setTimeout(() => {
      if (local.upload === upload) {
        local.upload = null;
        render();
      }
    }, 4000);
  } catch (error) {
    if (upload.cancelled || error?.name === "AbortError") return;
    upload.error = messageOf(error, "上传失败");
    toast(upload.error, "ph-warning");
    render();
  }
}

/**
 * 中止浏览器分片请求，并清理尚未进入 Drive worker 的本地暂存文件。
 */
async function cancelUpload() {
  const upload = local.upload;
  if (!upload) return;
  upload.cancelled = true;
  upload.controller.abort();
  if (upload.id && !upload.done) {
    try {
      await DriveApi.deleteUpload(upload.id);
    } catch (error) {
      toast(messageOf(error, "本地暂存清理失败"), "ph-warning");
    }
  }
  local.upload = null;
  render();
  toast("上传已取消", "ph-x-circle");
}

/**
 * 处理文件列表中的下载、导入和软删除按钮。
 */
async function handleFileAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const fileID = button.dataset.id;
  const action = button.dataset.action;
  if (!fileID) return;

  if (action === "download") {
    window.open(DriveApi.downloadUrl(fileID), "_blank", "noopener,noreferrer");
    return;
  }
  if (action === "import") {
    button.disabled = true;
    try {
      await DriveApi.importFile(fileID);
      toast("已加入字幕处理流水线", "ph-arrow-line-down");
      await refresh();
    } catch (error) {
      toast(messageOf(error, "创建导入任务失败"), "ph-warning");
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "trash") {
    const name = button.dataset.name || "该文件";
    if (!window.confirm(`将“${name}”移入 Google Drive 回收站？`)) return;
    button.disabled = true;
    try {
      await DriveApi.trashFile(fileID);
      toast("文件已移入回收站", "ph-trash");
      await refresh();
    } catch (error) {
      toast(messageOf(error, "移入回收站失败"), "ph-warning");
    } finally {
      button.disabled = false;
    }
  }
}

/**
 * 处理后台传输的暂停、恢复和取消操作。
 */
async function handleTransferAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = button.dataset.id;
  const action = button.dataset.action;
  if (!id || !action) return;
  button.disabled = true;
  try {
    await DriveApi.transferAction(id, action);
    toast(`传输${action === "pause" ? "已暂停" : action === "resume" ? "已恢复" : "已取消"}`, "ph-check-circle");
    await refresh();
  } catch (error) {
    toast(messageOf(error, "更新传输状态失败"), "ph-warning");
  } finally {
    button.disabled = false;
  }
}

/**
 * 渲染整个 Drive 视图，保持 DOM 结构集中，减少各异步操作之间的竞态。
 */
function render() {
  renderAuth();
  renderUpload();
  renderFiles();
  renderTransfers();
  const refreshButton = $("#driveRefresh");
  if (refreshButton) refreshButton.disabled = local.refreshing;
}

/**
 * 渲染顶部连接状态、授权按钮和配置错误提示。
 */
function renderAuth() {
  const status = $("#driveStatus");
  const statusText = $("#driveStatusText");
  const hint = $("#driveAuthHint");
  const action = $("#driveAuthAction");
  const disconnectButton = $("#driveDisconnect");
  if (!status || !statusText || !hint || !action || !disconnectButton) return;

  const auth = local.auth;
  let tone = "offline";
  let label = "未连接";
  if (local.authError) {
    label = "sidecar 离线";
  } else if (auth?.connected) {
    tone = "online";
    label = "已连接";
  } else if (auth?.configured) {
    tone = "pending";
    label = auth.reauthorize ? "需要重新授权" : "等待授权";
  } else if (auth) {
    label = "待配置 OAuth";
  }
  status.className = `drive-status drive-status--${tone}`;
  statusText.textContent = label;

  if (auth?.connected) {
    action.hidden = true;
    disconnectButton.hidden = false;
    const expiry = auth.token_expiry ? ` · Token 到期 ${formatDate(auth.token_expiry)}` : "";
    hint.textContent = `已连接到本地 OAuth 账户${expiry}。文件会进入 sidecar 管理的 Subtitles AI 文件夹。`;
  } else {
    action.hidden = false;
    disconnectButton.hidden = true;
    action.disabled = Boolean(auth && !auth.configured) || Boolean(local.authError);
    action.querySelector("span").textContent = auth?.reauthorize ? "重新授权" : "连接 Google Drive";
    hint.textContent = local.authError
      || auth?.configuration_error
      || (auth?.configured ? "点击后会打开 Google 授权页面；完成后回到此处刷新状态。" : "请在 drive-service/config.local.json 配置 Google OAuth ClientID 和 ClientSecret。");
  }
}

/**
 * 渲染浏览器到 sidecar 的本地分片上传进度。
 */
function renderUpload() {
  const box = $("#driveUploadProgress");
  const name = $("#driveUploadName");
  const value = $("#driveUploadValue");
  const bar = $("#driveUploadBar");
  const bytes = $("#driveUploadBytes");
  const cancel = $("#driveUploadCancel");
  if (!box || !name || !value || !bar || !bytes || !cancel) return;

  const upload = local.upload;
  box.hidden = !upload;
  if (!upload) return;
  const percent = upload.file.size ? Math.min(100, Math.round((upload.offset / upload.file.size) * 100)) : 0;
  name.textContent = upload.file.name;
  value.textContent = upload.error || (upload.done ? "已排队" : `${percent}%`);
  bar.style.width = `${upload.done ? 100 : percent}%`;
  bytes.textContent = upload.done
    ? "本地分片已全部提交，Drive worker 正在继续"
    : `${formatBytes(upload.offset)} / ${formatBytes(upload.file.size)}`;
  cancel.disabled = upload.done;
  cancel.textContent = upload.done ? "已提交" : "取消";
}

/**
 * 渲染 Drive 文件表格及其下载、导入、软删除操作。
 */
function renderFiles() {
  const root = $("#driveFiles");
  const count = $("#driveFileCount");
  if (!root || !count) return;
  count.textContent = local.auth?.connected ? `${local.files.length} 个文件` : "未连接";

  if (local.authError) {
    root.innerHTML = emptyState("ph-plugs-connected", "无法连接 sidecar", local.authError);
    return;
  }
  if (!local.auth?.connected) {
    root.innerHTML = emptyState("ph-lock-key", "连接后查看文件", "Google Drive 授权完成后，这里会显示 Subtitles AI 文件夹内容。");
    return;
  }
  if (local.filesError) {
    root.innerHTML = emptyState("ph-warning", "文件列表读取失败", local.filesError);
    return;
  }
  if (!local.files.length) {
    root.innerHTML = emptyState("ph-folder-open", "还没有文件", "上传一个视频或字幕文件，完成后会出现在这里。");
    return;
  }

  root.innerHTML = local.files.map((file) => {
    const name = escapeHtml(file.name || "未命名文件");
    const id = escapeHtml(file.id);
    const mime = escapeHtml(shortMime(file.mimeType));
    const canDownload = file.capabilities?.canDownload !== false;
    return `<article class="drive-file">
      <div class="drive-file__icon" aria-hidden="true"><i class="ph ${fileIcon(file.mimeType)}"></i></div>
      <div class="drive-file__main">
        <strong class="drive-file__name" title="${name}">${name}</strong>
        <span class="drive-file__meta">${mime} · ${formatBytes(file.size)} · ${formatDate(file.modifiedTime)}</span>
      </div>
      <div class="drive-file__actions">
        <button class="iconbtn iconbtn--accent" type="button" data-action="download" data-id="${id}" title="下载" aria-label="下载 ${name}" ${canDownload ? "" : "disabled"}>
          <i class="ph ph-download-simple" aria-hidden="true"></i>
        </button>
        <button class="iconbtn" type="button" data-action="import" data-id="${id}" title="导入字幕流水线" aria-label="导入 ${name}" ${canDownload ? "" : "disabled"}>
          <i class="ph ph-arrow-line-down" aria-hidden="true"></i>
        </button>
        <button class="iconbtn iconbtn--danger" type="button" data-action="trash" data-id="${id}" data-name="${name}" title="移入回收站" aria-label="删除 ${name}">
          <i class="ph ph-trash" aria-hidden="true"></i>
        </button>
      </div>
    </article>`;
  }).join("");
}

/**
 * 渲染上传到 Drive 和导入 Python 的持久化传输队列。
 */
function renderTransfers() {
  const root = $("#driveTransfers");
  const count = $("#driveTransferCount");
  if (!root || !count) return;
  count.textContent = local.transfers.length ? `${local.transfers.length} 项` : "空闲";
  if (local.transfersError) {
    root.innerHTML = emptyState("ph-warning", "队列读取失败", local.transfersError);
    return;
  }
  if (!local.transfers.length) {
    root.innerHTML = emptyState("ph-check-circle", "暂无传输任务", "上传或导入 Drive 文件后，后台进度会显示在这里。");
    return;
  }

  root.innerHTML = local.transfers.map((transfer) => {
    const total = Number(transfer.total_bytes || 0);
    const transferred = Math.max(0, Number(transfer.transferred_bytes || 0));
    const complete = transfer.state === "SUCCESS";
    const percent = complete ? 100 : total > 0 ? Math.min(100, Math.round((transferred / total) * 100)) : 0;
    const state = escapeHtml(transfer.state || "PENDING");
    const stateLabel = escapeHtml(STATUS_LABELS[transfer.state] || transfer.state || "等待中");
    const fileName = escapeHtml(transfer.file_name || transfer.python_task_id || "未命名任务");
    const canPause = ["PENDING", "TRANSFERRING", "RETRYING", "VERIFYING"].includes(transfer.state);
    const canResume = transfer.state === "PAUSED";
    const canCancel = !["SUCCESS", "FAILED", "CANCELLED"].includes(transfer.state);
    const action = canPause
      ? `<button class="btn btn--ghost btn--sm" type="button" data-action="pause" data-id="${escapeHtml(transfer.id)}">暂停</button>`
      : canResume
        ? `<button class="btn btn--ghost btn--sm" type="button" data-action="resume" data-id="${escapeHtml(transfer.id)}">恢复</button>`
        : "";
    const cancel = canCancel
      ? `<button class="btn btn--ghost btn--sm drive-transfer__cancel" type="button" data-action="cancel" data-id="${escapeHtml(transfer.id)}">取消</button>`
      : "";
    const error = transfer.error ? `<p class="drive-transfer__error">${escapeHtml(transfer.error)}</p>` : "";
    return `<article class="drive-transfer">
      <div class="drive-transfer__top">
        <div class="drive-transfer__identity">
          <span class="drive-transfer__kind">${escapeHtml(KIND_LABELS[transfer.kind] || transfer.kind || "传输")}</span>
          <strong title="${fileName}">${fileName}</strong>
        </div>
        <span class="drive-transfer__state drive-transfer__state--${state.toLowerCase()}">${stateLabel}</span>
      </div>
      <div class="drive-progress"><span style="width:${percent}%"></span></div>
      <div class="drive-transfer__bottom">
        <span>${formatBytes(transferred)} / ${formatBytes(total)} <b>${percent}%</b></span>
        <div class="drive-transfer__actions">${action}${cancel}</div>
      </div>
      ${error}
    </article>`;
  }).join("");
}

/**
 * 生成列表空状态，统一文件列表和队列的视觉节奏。
 */
function emptyState(icon, title, detail) {
  return `<div class="drive-empty"><i class="ph ${icon}" aria-hidden="true"></i><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

/**
 * 将异常转换成短文本，避免把网络对象直接展示到页面。
 */
function messageOf(error, fallback) {
  return String(error?.message || error || fallback);
}

/**
 * 按文件 MIME 类型选择一个稳定的 Phosphor 图标。
 */
function fileIcon(mime = "") {
  if (mime.startsWith("video/")) return "ph-film-strip";
  if (mime.startsWith("audio/")) return "ph-waveform";
  if (mime.includes("subtitle") || mime.includes("text")) return "ph-text-aa";
  if (mime.includes("zip") || mime.includes("compressed")) return "ph-file-zip";
  return "ph-file";
}

/**
 * 把过长 MIME 类型压缩成适合列表展示的标签。
 */
function shortMime(mime = "") {
  if (!mime) return "未知类型";
  return mime.replace("application/", "").replace("video/", "video/").replace("audio/", "audio/");
}

/**
 * 格式化字节数，统一使用二进制单位以匹配上传分片和 Drive 元数据。
 */
function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const amount = bytes / 1024 ** index;
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

/**
 * 格式化 Drive 的 RFC3339 时间戳；异常值回退为“未知时间”。
 */
function formatDate(value) {
  if (!value) return "未知时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
