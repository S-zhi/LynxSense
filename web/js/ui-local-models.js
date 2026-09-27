import { $, escapeHtml } from "./utils.js";
import { Api } from "./api.js";
import { state } from "./store.js";

let timer = null;
function render(items) {
  const root = $("#localModelsList");
  if (!root) return;
  root.innerHTML = items.map((item) => {
    const status = item.status || "NOT_INSTALLED";
    const busy = status === "DOWNLOADING";
    const action = status === "READY" ? "已就绪" : busy ? `${item.progress || 0}%` : "下载";
    const progress = Math.max(0, Math.min(100, Number(item.progress) || 0));
    const detail = status === "ERROR" ? item.error || "下载失败" : status === "READY" ? "可在快速开始中使用" : (item.phase === "downloading" ? `正在下载 · ${progress}%` : "尚未下载");
    return `<div class="settings__row"><div><strong>${escapeHtml(item.label || item.name)}</strong><div class="settings__hint">${escapeHtml(item.name)} · ${escapeHtml(item.size || "")} · ${escapeHtml(detail)}</div><progress class="model-download-progress" max="100" value="${progress}" aria-label="${escapeHtml(item.name)} 下载进度"></progress></div><button class="btn btn--ghost btn--sm" data-model-download="${escapeHtml(item.name)}" ${busy || status === "READY" ? "disabled" : ""}>${action}</button></div>`;
  }).join("");
  root.querySelectorAll("[data-model-download]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try { await Api.downloadLocalModel(button.dataset.modelDownload); await refresh(); } catch (error) { button.disabled = false; window.alert(error.message); }
  }));
}
async function refresh() { try { render(await Api.listLocalModels()); } catch (_) {} }
export function initLocalModels() {
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view === "other-settings" && event.detail?.settingsTab === "models") {
      void refresh();
      if (!timer) timer = setInterval(() => { if (state.view === "other-settings" && state.settingsTab === "models") void refresh(); }, 2000);
    }
  });
}
