import { $, escapeHtml } from "./utils.js";
import { Api } from "./api.js";
import { state } from "./store.js";
import { toast } from "./toast.js";

let timer = null;

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const amount = bytes / (1024 ** unitIndex);
  return `${amount.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function render(items) {
  const root = $("#localModelsList");
  if (!root) return;
  root.innerHTML = items.map((item) => {
    const status = item.status || "NOT_INSTALLED";
    const busy = status === "DOWNLOADING";
    const ready = status === "READY";
    const progress = Math.max(0, Math.min(99, Number(item.progress) || 0));
    const downloaded = Number(item.downloadedBytes) || 0;
    const total = Number(item.totalBytes) || 0;
    const name = escapeHtml(item.name);
    const label = escapeHtml(item.label || item.name);

    let stateLabel = "未下载";
    let progressMarkup = `<div class="local-model-card__availability">下载后约占用 ${escapeHtml(item.size || "未知空间")}</div>`;
    if (busy) {
      stateLabel = "下载中";
      const countLabel = total > 0
        ? `${formatBytes(downloaded) || "0 B"} / ${formatBytes(total)} · ${progress}%`
        : downloaded > 0 ? `已写入 ${formatBytes(downloaded)} · 正在获取总大小` : "正在获取模型文件清单";
      progressMarkup = `<div class="local-model-card__progress-label"><span>${total > 0 ? "下载进度" : "下载数据"}</span><strong>${escapeHtml(countLabel)}</strong></div><progress class="local-model-card__progress" max="100"${total > 0 ? ` value="${progress}"` : ""} aria-label="${name} 下载进度"${total > 0 ? ` aria-valuetext="${progress}%"` : ` aria-valuetext="已写入 ${formatBytes(downloaded) || "0 B"}"`}></progress>`;
    } else if (ready) {
      stateLabel = "已就绪";
      progressMarkup = `<div class="local-model-card__availability">已保存在本机${downloaded > 0 ? ` · 占用 ${formatBytes(downloaded)}` : item.size ? ` · ${escapeHtml(item.size)}` : ""}</div>`;
    } else if (status === "ERROR") {
      stateLabel = "下载失败";
      progressMarkup = `<div class="local-model-card__error" title="${escapeHtml(item.error || "下载失败")}">${escapeHtml(item.error || "下载失败")}</div>`;
    }

    const action = ready
      ? `<button class="btn btn--ghost btn--sm local-model-card__delete" type="button" data-model-delete="${name}" aria-label="删除 ${label}"><i class="ph ph-trash" aria-hidden="true"></i><span>删除</span></button>`
      : `<button class="btn btn--primary btn--sm" type="button" data-model-download="${name}"${busy ? " disabled aria-busy=\"true\"" : ""}><i class="ph ${busy ? "ph-spinner-gap" : "ph-download-simple"}" aria-hidden="true"></i><span>${busy ? `${progress}%` : status === "ERROR" ? "重试" : "下载"}</span></button>`;

    return `<article class="local-model-card${busy ? " is-downloading" : ""}" aria-label="${label}">
      <div class="local-model-card__top">
        <div class="local-model-card__identity"><span class="local-model-card__icon"><i class="ph ph-cpu" aria-hidden="true"></i></span><div><h3>${label}</h3><span class="local-model-card__name">${name}</span></div></div>
        <span class="local-model-card__status local-model-card__status--${ready ? "ready" : busy ? "busy" : status === "ERROR" ? "error" : "idle"}">${stateLabel}</span>
      </div>
      <div class="local-model-card__meta"><span>预计大小</span><strong>${escapeHtml(item.size || "—")}</strong></div>
      <div class="local-model-card__progress-area" aria-live="polite">${progressMarkup}</div>
      <div class="local-model-card__foot">${busy ? `<span class="local-model-card__phase">${item.phase === "checking" ? "正在获取模型文件清单" : "正在下载"}</span>` : ready ? `<span class="local-model-card__phase">可在任务页使用</span>` : status === "ERROR" ? `<span class="local-model-card__phase">可重试，已下载数据会尽量续传</span>` : `<span class="local-model-card__phase">下载不会阻塞其他任务</span>`}${action}</div>
    </article>`;
  }).join("");

  root.querySelectorAll("[data-model-download]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await Api.downloadLocalModel(button.dataset.modelDownload);
      await refresh();
    } catch (error) {
      button.disabled = false;
      toast(error.message || "下载本地模型失败", "ph-warning-circle");
    }
  }));

  root.querySelectorAll("[data-model-delete]").forEach((button) => button.addEventListener("click", async () => {
    const modelName = button.dataset.modelDelete;
    const item = items.find((model) => model.name === modelName);
    const modelLabel = item?.label || modelName;
    if (!window.confirm(`删除 ${modelLabel} 并释放本地空间？下次使用前需要重新下载。`)) return;
    button.disabled = true;
    try {
      await Api.deleteLocalModel(modelName);
      toast(`${modelLabel} 已删除`, "ph-trash");
      await refresh();
    } catch (error) {
      button.disabled = false;
      toast(error.message || "删除本地模型失败", "ph-warning-circle");
    }
  }));
}

async function refresh() {
  try { render(await Api.listLocalModels()); } catch (_) {}
}

export function initLocalModels() {
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view === "other-settings" && event.detail?.settingsTab === "models") {
      void refresh();
      if (!timer) timer = setInterval(() => {
        if (state.view === "other-settings" && state.settingsTab === "models") void refresh();
      }, 2000);
    }
  });
}
