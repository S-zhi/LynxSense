/* 字幕编辑：左侧任务选择 + 右侧视频预览 + 字幕条目编辑。
 * 数据走 Api.getSubtitles / saveSubtitles / reburnSubtitles。
 * 视频预览复用 Api.downloadUrl 指向 /api/tasks/{id}/download。
 */

import { LANG_LABEL } from "./constants.js";
import { $, el, escapeHtml } from "./utils.js";
import { state, subscribe } from "./store.js";
import { Api, USE_MOCK } from "./api.js";
import { toast } from "./toast.js";

let listEl, hintEl, titleEl, subEl, actionsEl, versionInput;
let videoEl, subsEl;
let addBtn, saveBtn, reburnBtn, localeBar;

let currentTaskId = null;
let currentDoc = null;        // { taskId, title, burn, hasOriginal, hasTranslated, original, translated }
let currentLocale = "translated";
let dirty = false;
let lastVideoUrl = null;

export function initEditor() {
  listEl = $("#editorList");
  hintEl = $("#editorHint");
  titleEl = $("#editorTitle");
  subEl = $("#editorSub");
  actionsEl = $("#editorActions");
  versionInput = $("#editorVersion");
  videoEl = $("#editorVideo");
  subsEl = $("#editorSubs");
  addBtn = $("#editorAdd");
  saveBtn = $("#editorSave");
  reburnBtn = $("#editorReburn");
  localeBar = $(".editor__locale");

  localeBar.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    if (dirty && !confirm("当前修改尚未保存，切换语言轨道会丢失。确定继续？")) return;
    currentLocale = chip.dataset.locale;
    [...localeBar.querySelectorAll(".chip")].forEach((c) =>
      c.classList.toggle("is-active", c.dataset.locale === currentLocale)
    );
    renderSubs();
  });

  addBtn.addEventListener("click", () => onAdd());
  saveBtn.addEventListener("click", () => onSave());
  reburnBtn.addEventListener("click", () => onReburn());

  // 视频播放进度联动：把视频 currentTime 同步到字幕高亮
  videoEl.addEventListener("timeupdate", highlightActiveSub);

  subscribe(renderIfActive);
  renderIfActive();
}

function renderIfActive() {
  if (state.view !== "editor") return;
  renderList();
  if (currentTaskId) {
    // 任务可能已被删/重试：始终以 state 为准同步一次
    const t = state.tasks.find((x) => x.id === currentTaskId);
    if (t) {
      titleEl.textContent = t.title || "字幕编辑";
    }
  }
}

/* ---------- 左侧任务列 ---------- */

function renderList() {
  const candidates = state.tasks.filter((t) => t.status === "SUCCESS" && t.needSubtitle !== false);
  listEl.replaceChildren();
  if (candidates.length === 0) {
    hintEl.textContent = "完成一个需要字幕的任务后会自动出现在这里";
    const e = el("div", "state");
    e.innerHTML = `
      <div class="state__icon"><i class="ph ph-text-aa" aria-hidden="true"></i></div>
      <div class="state__title">还没有可编辑的任务</div>
      <div class="state__desc">先在「任务」里跑一次完整流水线，生成 original.srt / translated.srt 后再回来。</div>`;
    listEl.append(e);
    actionsEl.hidden = true;
    renderVideo(null);
    renderSubsEmpty("未选择任务");
    return;
  }
  hintEl.textContent = "共 " + candidates.length + " 个可编辑任务";

  // 如果当前选中的任务已经不在列表里，重置
  if (currentTaskId && !candidates.some((t) => t.id === currentTaskId)) {
    currentTaskId = null;
    currentDoc = null;
  }
  if (!currentTaskId) currentTaskId = candidates[0].id;

  for (const t of candidates) {
    const item = el("button", "editem" + (t.id === currentTaskId ? " is-active" : ""));
    item.type = "button";
    item.dataset.id = t.id;
    item.innerHTML = `
      <span class="editem__thumb" aria-hidden="true"><i class="ph ph-text-aa"></i></span>
      <span class="editem__body">
        <span class="editem__title">${escapeHtml(t.title || "成品视频")}</span>
        <span class="editem__meta">${LANG_LABEL[t.targetLang] || t.targetLang} · ${t.burn === "hard" ? "硬字幕" : "软字幕"}</span>
      </span>`;
    item.addEventListener("click", () => selectTask(t.id));
    listEl.append(item);
  }

  // 只在选中变化时拉数据 / 重建视频
  ensureLoaded();
}

function selectTask(id) {
  if (id === currentTaskId) return;
  if (dirty && !confirm("当前修改尚未保存，切换任务会丢失。确定继续？")) return;
  currentTaskId = id;
  dirty = false;
  currentDoc = null; // 强制重拉
  versionInput.value = "";
  renderList();
}

/* ---------- 数据加载 ---------- */

async function ensureLoaded() {
  if (!currentTaskId) return;
  if (currentDoc && currentDoc.taskId === currentTaskId) {
    renderMain();
    return;
  }
  try {
    const doc = await Api.getSubtitles(currentTaskId);
    currentDoc = doc;
    // 如果当前 locale 在该任务里没有数据，自动切到有的那一个
    if (currentLocale === "original" && !doc.hasOriginal) currentLocale = "translated";
    if (currentLocale === "translated" && !doc.hasTranslated) currentLocale = "original";
    [...localeBar.querySelectorAll(".chip")].forEach((c) =>
      c.classList.toggle("is-active", c.dataset.locale === currentLocale)
    );
    renderMain();
  } catch (e) {
    toast(e.message || "加载字幕失败", "ph-warning-circle");
    renderVideo(null);
    renderSubsEmpty(e.message || "无法加载字幕");
  }
}

function renderMain() {
  if (!currentDoc) return;
  const t = state.tasks.find((x) => x.id === currentTaskId);
  titleEl.textContent = t ? (t.title || "字幕编辑") : "字幕编辑";
  const burn = t ? (t.burn === "hard" ? "硬字幕" : "软字幕") : "";
  subEl.textContent = `烧录方式：${burn} · 共 ${currentDoc[currentLocale].length} 条`;
  actionsEl.hidden = false;
  renderVideo(currentTaskId);
  renderSubs();
}

/* ---------- 视频预览（与 ui-preview 同源，简化） ---------- */

function renderVideo(taskId) {
  videoEl.replaceChildren();
  if (!taskId) {
    videoEl.append(emptyMsg("ph-monitor-play", "尚未选择任务", "从左侧选一个任务开始。"));
    lastVideoUrl = null;
    return;
  }
  if (USE_MOCK) {
    videoEl.append(emptyMsg("ph-flask", "示例模式", "前端示例数据，没有真实视频可播放；字幕编辑仍可体验。"));
    lastVideoUrl = null;
    return;
  }
  const url = Api.downloadUrl(taskId, "video");
  lastVideoUrl = url;
  const video = el("video");
  video.controls = true;
  video.preload = "metadata";
  video.src = url;
  videoEl.append(video);
}

function emptyMsg(icon, title, desc) {
  const e = el("div", "editor__videoempty");
  e.innerHTML = `
    <div class="state__icon"><i class="ph ${icon}" aria-hidden="true"></i></div>
    <div class="state__title">${escapeHtml(title)}</div>
    <div class="state__desc">${escapeHtml(desc)}</div>`;
  return e;
}

/* ---------- 字幕条目 ---------- */

function currentEntries() {
  if (!currentDoc) return [];
  return currentDoc[currentLocale] || [];
}

function setCurrentEntries(arr) {
  if (!currentDoc) return;
  currentDoc[currentLocale] = arr;
  // hasX 由后端在加载时给出，编辑过程不更新；保存时才落盘
}

function renderSubsEmpty(msg) {
  subsEl.replaceChildren();
  const e = el("div", "state");
  e.innerHTML = `
    <div class="state__icon"><i class="ph ph-text-aa" aria-hidden="true"></i></div>
    <div class="state__title">${escapeHtml(msg || "没有字幕")}</div>`;
  subsEl.append(e);
}

function renderSubs() {
  subsEl.replaceChildren();
  const entries = currentEntries();
  if (entries.length === 0) {
    renderSubsEmpty("该轨道暂无字幕");
    return;
  }
  for (let i = 0; i < entries.length; i++) {
    subsEl.append(buildRow(entries[i], i));
  }
  highlightActiveSub();
}

function buildRow(entry, i) {
  const row = el("div", "subrow");
  row.dataset.id = entry.id;
  row.innerHTML = `
    <div class="subrow__head">
      <span class="subrow__num num">${i + 1}</span>
      <div class="subrow__times">
        <input class="subrow__time num" data-field="start" type="text" inputmode="decimal" value="${fmtTime(entry.start)}" aria-label="开始时间" />
        <span class="subrow__arrow" aria-hidden="true">→</span>
        <input class="subrow__time num" data-field="end" type="text" inputmode="decimal" value="${fmtTime(entry.end)}" aria-label="结束时间" />
      </div>
      <div class="subrow__ops">
        <button class="iconbtn" type="button" data-op="seek" title="跳到该时间" aria-label="跳到第 ${i + 1} 条字幕的开始时间"><i class="ph ph-skip-forward"></i></button>
        <button class="iconbtn" type="button" data-op="merge-next" title="与下一条合并" aria-label="将第 ${i + 1} 条字幕与下一条合并"><i class="ph ph-arrows-in-line-vertical"></i></button>
        <button class="iconbtn" type="button" data-op="split" title="从中间拆成两条" aria-label="将第 ${i + 1} 条字幕拆成两条"><i class="ph ph-scissors"></i></button>
        <button class="iconbtn iconbtn--danger" type="button" data-op="delete" title="删除该条" aria-label="删除第 ${i + 1} 条字幕"><i class="ph ph-trash"></i></button>
      </div>
    </div>
    <textarea class="subrow__text" rows="2" placeholder="字幕文本（双语可用换行）" spellcheck="false"></textarea>`;

  const startInput = row.querySelector('[data-field="start"]');
  const endInput = row.querySelector('[data-field="end"]');
  const textArea = row.querySelector(".subrow__text");

  startInput.value = fmtTime(entry.start);
  endInput.value = fmtTime(entry.end);
  textArea.value = entry.text || "";

  const onChange = () => {
    const start = parseTime(startInput.value);
    const end = parseTime(endInput.value);
    const list = currentEntries();
    const idx = list.findIndex((x) => x.id === entry.id);
    if (idx < 0) return;
    list[idx] = { ...list[idx], start, end, text: textArea.value };
    setCurrentEntries(list);
    markDirty();
    if (startInput.classList.contains("is-invalid") || endInput.classList.contains("is-invalid")) {
      validateRow(row);
    }
  };

  startInput.addEventListener("change", () => {
    if (!validateRow(row)) return;
    onChange();
  });
  endInput.addEventListener("change", () => {
    if (!validateRow(row)) return;
    onChange();
  });
  textArea.addEventListener("input", onChange);

  row.querySelectorAll("[data-op]").forEach((btn) => {
    btn.addEventListener("click", () => handleOp(btn.dataset.op, entry.id));
  });

  return row;
}

function validateRow(row) {
  const startInput = row.querySelector('[data-field="start"]');
  const endInput = row.querySelector('[data-field="end"]');
  const start = parseTime(startInput.value);
  const end = parseTime(endInput.value);
  const startOk = Number.isFinite(start) && start >= 0;
  const endOk = Number.isFinite(end) && end >= 0;
  const orderOk = startOk && endOk && end >= start;
  startInput.classList.toggle("is-invalid", !startOk);
  endInput.classList.toggle("is-invalid", !endOk || !orderOk);
  return startOk && endOk && orderOk;
}

function handleOp(op, id) {
  const list = currentEntries();
  const idx = list.findIndex((x) => x.id === id);
  if (idx < 0) return;

  if (op === "seek") {
    const e = list[idx];
    const v = videoEl.querySelector("video");
    if (v) v.currentTime = e.start;
    return;
  }
  if (op === "delete") {
    list.splice(idx, 1);
    setCurrentEntries(list);
    markDirty();
    renderSubs();
    return;
  }
  if (op === "merge-next") {
    if (idx >= list.length - 1) {
      toast("已经是最后一条，无法与下一条合并", "ph-info");
      return;
    }
    const a = list[idx];
    const b = list[idx + 1];
    const merged = {
      ...a,
      end: b.end,
      text: [a.text, b.text].filter(Boolean).join("\n"),
    };
    list.splice(idx, 2, merged);
    setCurrentEntries(list);
    markDirty();
    renderSubs();
    return;
  }
  if (op === "split") {
    const a = list[idx];
    if (a.end - a.start < 0.4) {
      toast("当前条目时长太短，无法再拆分", "ph-info");
      return;
    }
    const mid = (a.start + a.end) / 2;
    const lines = (a.text || "").split("\n");
    const half = Math.max(1, Math.ceil(lines.length / 2));
    const left = {
      ...a,
      end: mid,
      text: lines.slice(0, half).join("\n"),
    };
    const right = {
      ...a,
      id: "sub_" + Math.random().toString(36).slice(2, 10),
      start: mid,
      text: lines.slice(half).join("\n"),
    };
    list.splice(idx, 1, left, right);
    setCurrentEntries(list);
    markDirty();
    renderSubs();
    return;
  }
}

function onAdd() {
  if (!currentDoc) return;
  const list = currentEntries();
  const last = list[list.length - 1];
  const start = last ? round2(last.end) : 0;
  const end = round2(start + 2);
  const next = {
    id: "sub_" + Math.random().toString(36).slice(2, 10),
    index: list.length + 1,
    start,
    end,
    text: "",
  };
  list.push(next);
  setCurrentEntries(list);
  markDirty();
  renderSubs();
  // 滚到新行
  subsEl.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "end" });
}

/* ---------- 视频高亮联动 ---------- */

function highlightActiveSub() {
  const v = videoEl.querySelector("video");
  if (!v) return;
  const t = v.currentTime || 0;
  const list = currentEntries();
  let activeId = null;
  for (const e of list) {
    if (t >= e.start && t < e.end) { activeId = e.id; break; }
  }
  subsEl.querySelectorAll(".subrow").forEach((row) => {
    row.classList.toggle("is-active", row.dataset.id === activeId);
  });
}

/* ---------- 工具：dirty / 保存 / 烧录 ---------- */

function markDirty() {
  dirty = true;
  saveBtn.classList.add("btn--accent");
  saveBtn.classList.remove("btn--ghost");
  document.title = "• 字幕编辑";
}

function clearDirty() {
  dirty = false;
  saveBtn.classList.remove("btn--accent");
  saveBtn.classList.add("btn--ghost");
  document.title = "字幕编辑";
}

async function onSave() {
  if (!currentDoc || !currentTaskId) return;
  // 提交前再做一次全表校验
  const rows = subsEl.querySelectorAll(".subrow");
  for (const r of rows) if (!validateRow(r)) {
    toast("存在非法时间，请修正后再保存", "ph-warning-circle");
    return;
  }
  const entries = currentEntries().map((e, i) => ({
    id: e.id,
    index: i + 1,
    start: round2(e.start),
    end: round2(e.end),
    text: e.text || "",
  }));
  const version = (versionInput.value || "").trim() || null;
  saveBtn.disabled = true;
  try {
    const res = await Api.saveSubtitles(currentTaskId, {
      locale: currentLocale,
      entries,
      version,
    });
    clearDirty();
    toast(
      version
        ? `已保存版本 ${res.path}（${res.count} 条）`
        : `已覆盖 ${res.path}（${res.count} 条）`,
      "ph-floppy-disk",
    );
  } catch (e) {
    toast(e.message || "保存失败", "ph-warning-circle");
  } finally {
    saveBtn.disabled = false;
  }
}

async function onReburn() {
  if (!currentTaskId) return;
  if (!confirm("将基于当前 translated.srt 重新烧录 output.mp4。确定继续？")) return;
  reburnBtn.disabled = true;
  try {
    const res = await Api.reburnSubtitles(currentTaskId, {});
    toast(`已重新烧录 ${res.outputPath}（${res.mode}）`, "ph-fire");
  } catch (e) {
    toast(e.message || "重新烧录失败", "ph-warning-circle");
  } finally {
    reburnBtn.disabled = false;
  }
}

/* ---------- 工具：时间格式 ---------- */

function fmtTime(sec) {
  if (!Number.isFinite(sec)) return "00:00.000";
  const s = Math.max(0, sec);
  const m = Math.floor(s / 60);
  const ss = (s - m * 60);
  return `${pad2(m)}:${ss.toFixed(3).padStart(6, "0")}`;
}

function parseTime(str) {
  // 接受 1.234 / 1,234 / 1:23.456 / 01:23.456
  const t = String(str || "").trim().replace(",", ".");
  if (!t) return NaN;
  if (/^\d+(\.\d+)?$/.test(t)) return Number(t);
  const m = t.match(/^(\d+):(\d+(?:\.\d+)?)$/);
  if (!m) return NaN;
  return Number(m[1]) * 60 + Number(m[2]);
}

function pad2(n) { return String(n).padStart(2, "0"); }
function round2(n) { return Math.round(n * 1000) / 1000; }
