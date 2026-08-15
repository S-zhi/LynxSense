/* 处理队列：横向任务单元；按 id 复用行节点，保证进度平滑、入场只触发一次。 */

import { STEPS, STATUS_META, LANG_LABEL, TERMINAL } from "./constants.js";
import { $, el, escapeHtml, shortUrl, progressToStepIndex } from "./utils.js";
import {
  state, subscribe, visibleTasks,
  retryTask, deleteTask, loadTasks, setView, setPreviewId,
} from "./store.js";
import { Api, USE_MOCK } from "./api.js";
import { toast } from "./toast.js";

let listEl;
const rows = new Map(); // id -> element
let renderedSpecial = false;

export function initQueue() {
  listEl = $("#taskList");
  subscribe(renderQueue);
  renderQueue();
}

/* ---------- 整体渲染（复用节点） ---------- */
function renderQueue() {
  if (state.loading) {
    listEl.replaceChildren(skeleton());
    rows.clear();
    renderedSpecial = true;
    return;
  }
  if (state.loadError) {
    listEl.replaceChildren(loadErrorState());
    rows.clear();
    renderedSpecial = true;
    return;
  }
  const visible = visibleTasks();
  if (visible.length === 0) {
    listEl.replaceChildren(emptyState());
    rows.clear();
    renderedSpecial = true;
    return;
  }
  if (renderedSpecial) {
    listEl.replaceChildren();
    renderedSpecial = false;
  }

  // 更新或新建
  for (const t of visible) {
    let row = rows.get(t.id);
    if (row) {
      updateRow(row, t);
    } else {
      row = buildRow(t);
      rows.set(t.id, row);
    }
  }
  // 移除已不在列表中的行
  for (const [id, row] of [...rows]) {
    if (!visible.some((t) => t.id === id)) {
      row.remove();
      rows.delete(id);
    }
  }
  // 同步顺序（仅在位置不符时移动，避免重放入场动画）
  visible.forEach((t, i) => {
    const node = rows.get(t.id);
    if (listEl.children[i] !== node) listEl.insertBefore(node, listEl.children[i] || null);
  });
}

/* ---------- 单行构建 / 更新 ---------- */
function buildRow(t) {
  const row = el("article", "qrow entering");
  row.dataset.id = t.id;
  row.innerHTML = `
    <div class="qrow__status"><i class="ph"></i></div>
    <div class="qrow__main">
      <div class="qrow__titleline">
        <span class="qrow__title"></span>
        <span class="qrow__badge"></span>
      </div>
      <div class="qrow__url"></div>
      <div class="qrow__meta"></div>
      <div class="qrow__dyn"></div>
    </div>
    <div class="qrow__actions"></div>`;
  row.addEventListener("animationend", () => row.classList.remove("entering"), { once: true });

  // 元信息（静态）
  const metaBox = row.querySelector(".qrow__meta");
  if (t.needSubtitle === false) {
    metaBox.append(tag("仅下载视频"));
  } else {
    metaBox.append(
      tag(`${LANG_LABEL[t.sourceLang] || t.sourceLang} → ${LANG_LABEL[t.targetLang] || t.targetLang}`),
      tag(t.mode === "bilingual" ? "双语对照" : "仅译文"),
      tag(t.burn === "hard" ? "硬烧录" : "软字幕"),
      tag("whisper " + t.model)
    );
  }

  setHeader(row, t);
  applyDynamic(row, t);
  return row;
}

function updateRow(row, t) {
  setHeader(row, t);
  applyDynamic(row, t);
}

function setHeader(row, t) {
  const meta = STATUS_META[t.status] || STATUS_META.PENDING;
  ["pending", "active", "success", "failed"].forEach((c) => row.classList.remove("is-" + c));
  row.classList.add("is-" + meta.cls);
  row.querySelector(".qrow__status i").className = "ph " + meta.icon;
  row.querySelector(".qrow__title").textContent = t.title || "处理中的视频";
  row.querySelector(".qrow__url").textContent = shortUrl(t.url);
  row.querySelector(".qrow__badge").innerHTML =
    `<span class="badge badge--${meta.cls}"><i class="ph ${meta.icon}"></i>${meta.label}</span>`;
}

function categoryOf(status) {
  if (status === "FAILED") return "failed";
  if (status === "SUCCESS") return "success";
  return "progress"; // pending / active
}

function applyDynamic(row, t) {
  const cat = categoryOf(t.status);
  const dyn = row.querySelector(".qrow__dyn");
  const actions = row.querySelector(".qrow__actions");

  if (row.dataset.cat !== cat) {
    row.dataset.cat = cat;
    dyn.innerHTML = dynMarkup(cat);
    if (cat === "failed") fillError(dyn, t);
    actions.replaceChildren(...buildActions(t, cat));
  }
  if (cat === "progress") updateTrack(row, t);
  if (cat === "failed") fillError(dyn, t); // 错误文案可能更新
}

function dynMarkup(cat) {
  if (cat === "success") return `<div class="donebar"></div>`;
  if (cat === "failed") return `<div class="qrow__error"></div>`;
  const segs = STEPS.map(
    (s) => `<div class="track__seg"><div class="track__bar"></div><div class="track__label">${s.label}</div></div>`
  ).join("");
  return `
    <div class="progress-line">
      <span class="progress-line__step"></span>
      <span class="progress-line__pct num"></span>
    </div>
    <div class="track">${segs}</div>`;
}

function updateTrack(row, t) {
  const meta = STATUS_META[t.status] || STATUS_META.PENDING;
  const isActive = !TERMINAL.has(t.status) && t.status !== "PENDING";
  const curIdx = progressToStepIndex(t.progress);
  const segs = row.querySelectorAll(".track__seg");
  segs.forEach((s, i) => {
    s.classList.toggle("is-done", t.status !== "PENDING" && i < curIdx);
    s.classList.toggle("is-current", isActive && i === curIdx);
  });
  row.querySelector(".progress-line__step").textContent = t.status === "PENDING" ? "排队中" : meta.label;
  row.querySelector(".progress-line__pct").textContent = Math.round(t.progress) + "%";
}

function fillError(dyn, t) {
  const box = dyn.querySelector(".qrow__error");
  if (!box) return;
  const hint = errorHint(t.error);
  box.innerHTML =
    `<b>${escapeHtml(t.error || "处理失败")}</b>` +
    (hint ? `<div class="qrow__hint">${hint}</div>` : "");
}

function errorHint(msg) {
  const m = (msg || "").toLowerCase();
  if (m.includes("replicate") || m.includes("token"))
    return `检查 .env 中的 <code>REPLICATE_API_TOKEN</code>`;
  if (m.includes("deepseek") || m.includes("api key"))
    return `检查 .env 中的 <code>SUBTRANS_DEEPSEEK_API_KEY</code>`;
  if (m.includes("libass") || m.includes("ffmpeg"))
    return `检查 ffmpeg 是否安装且带 <code>libass</code>`;
  if (m.includes("下载") || m.includes("download") || m.includes("404"))
    return `检查链接是否有效，或该站点是否需要 cookies`;
  return "";
}

/* ---------- 行操作 ---------- */
function buildActions(t, cat) {
  if (cat === "success") {
    return [
      iconBtn("ph-play", "预览", "iconbtn--accent", () => {
        setPreviewId(t.id);
        setView("preview");
      }),
      iconBtn("ph-folder-open", "打开文件夹", "", () => openFolder(t)),
      iconBtn("ph-download-simple", "下载视频", "", () => download(t, "video")),
      iconBtn("ph-closed-captioning", "下载字幕", "", () => download(t, "subtitle")),
      iconBtn("ph-trash", "删除", "iconbtn--danger", () => remove(t)),
    ];
  }
  if (cat === "failed") {
    return [
      iconBtn("ph-folder-open", "打开文件夹", "", () => openFolder(t)),
      iconBtn("ph-arrow-clockwise", "重试", "iconbtn--accent", () => retry(t)),
      iconBtn("ph-trash", "删除", "iconbtn--danger", () => remove(t)),
    ];
  }
  return [
    iconBtn("ph-folder-open", "打开文件夹", "", () => openFolder(t)),
    iconBtn("ph-trash", "删除", "iconbtn--danger", () => remove(t)),
  ];
}

function iconBtn(icon, label, extra, onClick) {
  const b = el("button", "iconbtn" + (extra ? " " + extra : ""));
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.innerHTML = `<i class="ph ${icon}" aria-hidden="true"></i>`;
  b.addEventListener("click", onClick);
  return b;
}

function download(t, kind) {
  if (USE_MOCK) {
    toast(kind === "video" ? "示例模式：成品下载占位" : "示例模式：字幕下载占位");
    return;
  }
  window.open(Api.downloadUrl(t.id, kind), "_blank");
}

// 打开当前任务对应的本地产物文件夹。
async function openFolder(t) {
  try {
    await Api.openFolder(t.id);
    toast(USE_MOCK ? "示例模式：文件夹打开占位" : "已打开任务文件夹", "ph-folder-open");
  } catch (e) {
    toast(e.message || "打开文件夹失败", "ph-warning-circle");
  }
}

async function retry(t) {
  try {
    await retryTask(t.id);
    toast("已重新加入队列", "ph-arrow-clockwise");
  } catch (e) {
    toast(e.message || "重试失败", "ph-warning-circle");
  }
}

async function remove(t) {
  try {
    await deleteTask(t.id);
    toast("已删除任务", "ph-trash");
  } catch (e) {
    toast(e.message || "删除失败", "ph-warning-circle");
  }
}

/* ---------- 特殊态 ---------- */
function tag(text) {
  const s = el("span", "tag");
  s.textContent = text;
  return s;
}

function skeleton() {
  const frag = document.createDocumentFragment();
  for (let i = 0; i < 3; i++) {
    const s = el("div", "skel");
    s.innerHTML = `
      <div class="skel__tile sk"></div>
      <div class="skel__lines">
        <div class="sk" style="width:46%"></div>
        <div class="sk" style="width:28%;height:9px"></div>
        <div class="sk" style="width:100%;height:6px;margin-top:4px"></div>
      </div>`;
    frag.append(s);
  }
  const wrap = el("div");
  wrap.style.display = "flex";
  wrap.style.flexDirection = "column";
  wrap.style.gap = "10px";
  wrap.append(frag);
  return wrap;
}

function emptyState() {
  const filtered = state.tasks.length > 0;
  const e = el("div", "state");
  e.innerHTML = `
    <div class="state__icon"><i class="ph ${filtered ? "ph-funnel" : "ph-tray"}" aria-hidden="true"></i></div>
    <div class="state__title">${filtered ? "该筛选下没有任务" : "队列为空"}</div>
    <div class="state__desc">${
      filtered ? "切换上方筛选查看其它任务。" : "在上方命令栏粘贴视频链接，点击开始处理即可加入队列。"
    }</div>`;
  return e;
}

function loadErrorState() {
  const base = window.APP_CONFIG.API_BASE_URL;
  const e = el("div", "state state--error");
  e.innerHTML = `
    <div class="state__icon"><i class="ph ph-plugs" aria-hidden="true"></i></div>
    <div class="state__title">无法连接后端</div>
    <div class="state__desc">确认后端已在运行：<code>${escapeHtml(base)}</code></div>`;
  const btn = el("button", "btn btn--ghost btn--sm state__action");
  btn.innerHTML = `<i class="ph ph-arrow-clockwise"></i><span>重试</span>`;
  btn.addEventListener("click", () => loadTasks());
  e.append(btn);
  return e;
}
