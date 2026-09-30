/* 独立开发者页：所有状态只存在于当前页面内，不进入主应用 store 或本地存储。 */

import { createDeveloperState, visibleLogs } from "./developer-state.js";

const LOGS = [
  { time: "刚刚", level: "info", message: "Developer workspace initialized." },
  { time: "刚刚", level: "success", message: "Runtime log stream is ready." },
  { time: "刚刚", level: "info", message: "No persistent tab state was loaded." },
  { time: "刚刚", level: "warn", message: "This page is intended for local debugging only." },
];

const state = createDeveloperState(LOGS);
const logRoot = document.querySelector("#developerLog");
const sessionRoot = document.querySelector("#developerSession");

function renderLogs() {
  const visible = visibleLogs(state);
  if (!visible.length) {
    logRoot.innerHTML = '<div class="developer-log__empty">暂无匹配的日志</div>';
    return;
  }
  logRoot.replaceChildren(...visible.map((item) => {
    const row = document.createElement("div");
    row.className = "developer-log__row";
    const time = document.createElement("span");
    time.className = "developer-log__time";
    time.textContent = item.time;
    const level = document.createElement("span");
    level.className = `developer-log__level developer-log__level--${item.level}`;
    level.textContent = item.level;
    const message = document.createElement("span");
    message.className = "developer-log__message";
    message.textContent = item.message;
    row.append(time, level, message);
    return row;
  }));
}

document.querySelectorAll(".developer-filter").forEach((button) => {
  button.addEventListener("click", () => {
    state.level = button.dataset.level;
    document.querySelectorAll(".developer-filter").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    renderLogs();
  });
});

document.querySelector("#developerClear")?.addEventListener("click", () => {
  state.logs = [];
  renderLogs();
});

document.querySelector("#developerBack")?.addEventListener("click", () => {
  window.location.assign("/tasks");
});

if (sessionRoot) {
  sessionRoot.textContent = `session ${new Date().toISOString().slice(11, 19)} UTC`;
}
renderLogs();
