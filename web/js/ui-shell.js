/* 外壳：侧边导航 / 视图切换 / 筛选，全部由 store 状态驱动。 */

import { $, $$ } from "./utils.js";
import { state, subscribe, setView, setFilter } from "./store.js";

export function initShell() {
  $("#nav").addEventListener("click", (e) => {
    const item = e.target.closest(".nav__item");
    if (item) setView(item.dataset.view);
  });

  $("#filters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) setFilter(chip.dataset.filter);
  });

  subscribe(syncShell);
  syncShell();
}

function syncShell() {
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === state.view));
  $$(".nav__item").forEach((n) => n.classList.toggle("is-active", n.dataset.view === state.view));
  $$("#filters .chip").forEach((c) => c.classList.toggle("is-active", c.dataset.filter === state.filter));
  // 广播视图切换事件（各 Tab 自取所需）
  document.dispatchEvent(new CustomEvent("viewchange", { detail: { view: state.view } }));
}
