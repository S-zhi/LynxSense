/* 外壳：侧边导航 / 视图切换 / 筛选，全部由 store 状态驱动。 */

import { $, $$ } from "./utils.js";
import { state, subscribe, setView, setFilter, notificationsApi } from "./store.js";

export function initShell() {
  $("#nav").addEventListener("click", (e) => {
    const item = e.target.closest(".nav__item");
    if (item) setView(item.dataset.view);
  });

  $("#filters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) setFilter(chip.dataset.filter);
  });

  initNotifications();
  subscribe(syncShell);
  syncShell();
}

/* 把设置面板的开关与 store 的通知 API 连起来。 */
function initNotifications() {
  const toggle = $("#notifyToggle");
  const hint = $("#notifyHint");
  if (!toggle) return;
  const api = notificationsApi();
  if (!api) return;

  function refreshHint() {
    if (!hint) return;
    if (!api.isSupported()) {
      hint.textContent = "当前浏览器不支持通知 API。";
      toggle.disabled = true;
      return;
    }
    const perm = api.getPermission();
    if (!api.isEnabled()) {
      hint.textContent = "通知已关闭。开启后会请求浏览器授权。";
    } else if (perm === "granted") {
      hint.textContent = "已开启：任务完成 / 失败时会弹出系统通知。";
    } else if (perm === "denied") {
      hint.textContent = "浏览器已禁止通知权限，请在站点设置中手动开启。";
    } else {
      hint.textContent = "下次开启时会请求浏览器授权。";
    }
  }

  // 初始状态
  toggle.checked = api.isEnabled();
  refreshHint();

  toggle.addEventListener("change", async () => {
    await api.setEnabled(toggle.checked);
    refreshHint();
  });
}

function syncShell() {
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === state.view));
  $$(".nav__item").forEach((n) => n.classList.toggle("is-active", n.dataset.view === state.view));
  $$("#filters .chip").forEach((c) => c.classList.toggle("is-active", c.dataset.filter === state.filter));
}
