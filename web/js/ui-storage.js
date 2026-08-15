/* 本地资源治理 Tab：统计卡片 + 任务产物表格 + 安全清理预览 / 执行。
 * 复用 store.loadTasks() 在清理后刷新任务列表，保持任务 Tab 一致。
 */

import { $, $$, el } from "./utils.js";
import { Api } from "./api.js";
import { state, loadTasks } from "./store.js";
import { toast } from "./toast.js";

const RUNNING = new Set([
  "PENDING", "DOWNLOADING", "EXTRACTING",
  "TRANSCRIBING", "TRANSLATING", "BURNING",
]);

const KIND_LABEL = {
  source: "源视频",
  audio: "音频",
  original_srt: "原文字幕",
  translated_srt: "译文字幕",
  output: "成品视频",
  other: "其它",
};

const STATUS_LABEL = {
  PENDING: "排队中",
  DOWNLOADING: "下载中",
  EXTRACTING: "提取中",
  TRANSCRIBING: "识别中",
  TRANSLATING: "翻译中",
  BURNING: "烧录中",
  SUCCESS: "已完成",
  FAILED: "失败",
};

// 本模块状态：仅在 Tab 内有效
const local = {
  stats: null,
  selected: new Set(),   // 当前选中的 taskId
  preview: null,         // 最近一次预览结果
  retentionDays: 0,
  kindFilter: "",
  loading: false,
  refreshTimer: null,
};

let els = {};

export function initStorage() {
  els = {
    cards: $("#storageCards"),
    total: $("#statTotal"),
    totalSub: $("#statTotalSub"),
    tasks: $("#statTasks"),
    cleanable: $("#statCleanable"),
    cleanableSub: $("#statCleanableSub"),
    retention: $("#statRetention"),
    kind: $("#storageKind"),
    retentionSelect: $("#storageRetention"),
    selInfo: $("#storageSelInfo"),
    selectAll: $("#storageSelectAll"),
    selectNone: $("#storageSelectNone"),
    preview: $("#storagePreview"),
    previewBtn: $("#storagePreview"),
    previewBox: $("#storagePreviewBox"),
    previewBody: $("#storagePreviewBody"),
    previewClose: $("#storagePreviewClose"),
    apply: $("#storageApply"),
    table: $("#storageTable"),
    tbody: $("#storageTbody"),
    empty: $("#storageEmpty"),
    checkAll: $("#storageCheckAll"),
    refresh: $("#storageRefresh"),
  };

  els.refresh.addEventListener("click", () => refresh(true));
  els.kind.addEventListener("change", () => {
    local.kindFilter = els.kind.value;
    renderTable();
  });
  els.retentionSelect.addEventListener("change", () => {
    const v = parseInt(els.retentionSelect.value, 10) || 0;
    local.retentionDays = v;
    saveRetention();
  });
  els.selectAll.addEventListener("click", () => selectAll(true));
  els.selectNone.addEventListener("click", () => selectAll(false));
  els.previewBtn.addEventListener("click", () => runPreview());
  els.apply.addEventListener("click", () => runApply());
  els.previewClose.addEventListener("click", () => closePreview());
  els.checkAll.addEventListener("change", (e) => selectAll(e.target.checked));

  // 当切到本 Tab 时再加载一次，其它时候用定时轻量刷新
  if (state.view === "storage") {
    refresh();
  }
  document.addEventListener("viewchange", (e) => {
    if (e.detail && e.detail.view === "storage") refresh();
  });

  startAutoRefresh();
}

/* ---------- 自动刷新：仅在 Tab 可见时每 8s 拉一次统计 ---------- */
function startAutoRefresh() {
  clearInterval(local.refreshTimer);
  local.refreshTimer = setInterval(() => {
    if (state.view === "storage" && !local.loading) refresh();
  }, 8000);
}

/* ---------- 数据加载 ---------- */
async function refresh(showToast = false) {
  if (local.loading) return;
  local.loading = true;
  try {
    const [stats, retention] = await Promise.all([
      Api.getStorageStats().catch((e) => {
        if (showToast) toast(e.message, "ph-warning-circle");
        return null;
      }),
      Api.getRetention().catch(() => ({ days: null, updatedAt: null })),
    ]);
    if (stats) {
      local.stats = stats;
      renderCards();
      renderTable();
    }
    if (retention && retention.days !== undefined) {
      local.retentionDays = retention.days || 0;
      // 仅在未初始化时同步 select
      if (els.retentionSelect.value !== String(local.retentionDays)) {
        els.retentionSelect.value = String(local.retentionDays);
      }
    }
    if (showToast) toast("已刷新", "ph-arrow-clockwise");
  } finally {
    local.loading = false;
  }
}

async function saveRetention() {
  try {
    const v = local.retentionDays > 0 ? local.retentionDays : null;
    await Api.putRetention(v);
    renderCards();
    toast(v ? `保留策略：${v} 天以上` : "保留策略：不限", "ph-check");
  } catch (e) {
    toast(e.message || "保存保留策略失败", "ph-warning-circle");
  }
}

/* ---------- 渲染 ---------- */
function renderCards() {
  if (!local.stats) return;
  const { totalBytes, totalTasks, runnableTaskCount, byKind } = local.stats;
  els.total.textContent = formatBytes(totalBytes);
  els.totalSub.textContent = totalTasks
    ? `${(byKind.source || 0) > 0 ? "源视频 " + formatBytes(byKind.source) + " · " : ""}共 ${totalTasks} 个任务`
    : "尚未生成任何产物";
  els.tasks.textContent = String(totalTasks);
  els.cleanable.textContent = String(runnableTaskCount);
  const running = totalTasks - runnableTaskCount;
  els.cleanableSub.textContent = running > 0
    ? `${running} 个运行中会被跳过`
    : "可全部清理";
  els.retention.textContent = local.retentionDays > 0
    ? `${local.retentionDays} 天`
    : "不限";
}

function renderTable() {
  const tbody = els.tbody;
  const stats = local.stats;
  tbody.replaceChildren();

  if (!stats || stats.byTask.length === 0) {
    els.table.hidden = true;
    els.empty.hidden = false;
    updateActions();
    return;
  }
  els.table.hidden = false;
  els.empty.hidden = true;

  // 按创建时间倒序（在占用降序的基础上，让新任务也更靠前）
  const rows = [...stats.byTask].sort((a, b) => {
    if (a.size !== b.size) return b.size - a.size;
    return 0;
  });

  for (const t of rows) {
    const tr = el("tr", "");
    tr.dataset.id = t.taskId;
    if (local.selected.has(t.taskId)) tr.classList.add("is-selected");
    const isRunning = RUNNING.has(t.status);
    if (isRunning) tr.classList.add("is-running");

    // 复选框：运行中任务禁用
    const checkTd = el("td", "storage-table__check");
    const check = el("label", "storage-check");
    const cb = el("input", "storage-check__input");
    cb.type = "checkbox";
    cb.checked = local.selected.has(t.taskId);
    cb.disabled = isRunning;
    cb.title = isRunning ? "运行中任务会被自动跳过" : "选择此任务";
    cb.setAttribute("aria-label", cb.title);
    const checkBox = el("span", "storage-check__box");
    checkBox.setAttribute("aria-hidden", "true");
    cb.addEventListener("change", () => toggleSelect(t.taskId, cb.checked));
    check.append(cb, checkBox);
    checkTd.append(check);

    // 标题
    const titleTd = el("td", "storage-table__title");
    const titleMain = el("div", "storage-table__title-main");
    titleMain.textContent = t.title || "处理中的视频";
    const titleId = el("div", "storage-table__title-id");
    titleId.textContent = t.taskId;
    titleTd.append(titleMain, titleId);

    // 状态
    const statusTd = el("td", "storage-table__status");
    const badge = el("span", "storage-badge " + badgeClass(t.status));
    badge.innerHTML = `<i class="ph ${badgeIcon(t.status)}"></i>${STATUS_LABEL[t.status] || t.status}`;
    statusTd.append(badge);

    // 产物
    const artTd = el("td", "storage-table__artifacts");
    const list = el("div", "storage-table__artifacts-list");
    if (t.artifactCount === 0) {
      const chip = el("span", "storage-table__art-chip");
      chip.textContent = "无产物";
      list.append(chip);
    } else {
      // 聚合同类
      const grouped = {};
      for (const a of t.artifacts) {
        grouped[a.kind] = (grouped[a.kind] || 0) + 1;
      }
      for (const k of Object.keys(grouped)) {
        const chip = el("span", "storage-table__art-chip");
        const label = KIND_LABEL[k] || k;
        chip.textContent = grouped[k] > 1 ? `${label} ×${grouped[k]}` : label;
        chip.title = t.artifacts
          .filter((a) => a.kind === k)
          .map((a) => a.name)
          .join(", ");
        list.append(chip);
      }
    }
    artTd.append(list);

    // 占用
    const sizeTd = el("td", "storage-table__size num");
    sizeTd.textContent = formatBytes(t.size);

    // 创建时间：后端 stats 不带 createdAt，用本地 state 兜底
    const ageTd = el("td", "storage-table__age");
    ageTd.textContent = "—";

    tr.append(checkTd, titleTd, statusTd, artTd, sizeTd, ageTd);
    tbody.append(tr);
  }

  // 用本地 store 的 tasks 兜底填入创建时间
  fillAges();
  updateActions();
}

function fillAges() {
  const byId = new Map(state.tasks.map((t) => [t.id, t]));
  $$("tr[data-id]", els.tbody).forEach((tr) => {
    const t = byId.get(tr.dataset.id);
    const ageTd = tr.querySelector(".storage-table__age");
    if (!ageTd) return;
    ageTd.textContent = t && t.createdAt ? formatAgeFromMs(t.createdAt) : "—";
  });
}

function formatAgeFromMs(ms) {
  const days = Math.max(0, Math.floor((Date.now() - ms) / 86400000));
  if (days === 0) return "今天";
  if (days === 1) return "1 天前";
  if (days < 30) return `${days} 天前`;
  const months = Math.floor(days / 30);
  return months < 12 ? `${months} 个月前` : `${Math.floor(months / 12)} 年前`;
}

/* ---------- 选择 / 动作状态 ---------- */
function toggleSelect(id, on) {
  if (on) local.selected.add(id);
  else local.selected.delete(id);
  const tr = els.tbody.querySelector(`tr[data-id="${cssEscape(id)}"]`);
  if (tr) tr.classList.toggle("is-selected", on);
  updateActions();
}

function selectAll(on) {
  if (!local.stats) return;
  if (on) {
    // 跳过 RUNNING
    for (const t of local.stats.byTask) {
      if (!RUNNING.has(t.status)) local.selected.add(t.taskId);
    }
  } else {
    local.selected.clear();
  }
  // 同步所有复选框
  $$('input[type="checkbox"]', els.tbody).forEach((cb) => {
    const tr = cb.closest("tr");
    if (!tr) return;
    if (on) {
      if (!cb.disabled) cb.checked = true;
    } else {
      cb.checked = false;
    }
    tr.classList.toggle("is-selected", cb.checked);
  });
  updateActions();
}

function updateActions() {
  const n = local.selected.size;
  els.selInfo.textContent = n > 0 ? `已选 ${n} 项` : "未选择";
  const hasSelection = n > 0;
  els.previewBtn.disabled = !hasSelection;
  els.apply.disabled = !hasSelection;

  // 让表头选择器反映真实状态：全选、部分选择和无可选项分别可见。
  const selectable = local.stats
    ? local.stats.byTask.filter((t) => !RUNNING.has(t.status))
    : [];
  const selectedCount = selectable.filter((t) => local.selected.has(t.taskId)).length;
  els.checkAll.checked = selectable.length > 0 && selectedCount === selectable.length;
  els.checkAll.indeterminate = selectedCount > 0 && selectedCount < selectable.length;
  els.checkAll.disabled = selectable.length === 0;
}

/* ---------- 预览 / 执行 ---------- */
function buildBody() {
  const body = {};
  if (local.selected.size > 0) body.taskIds = [...local.selected];
  if (local.kindFilter) body.kinds = [local.kindFilter];
  if (local.retentionDays > 0) body.olderThanDays = local.retentionDays;
  return body;
}

async function runPreview() {
  if (local.selected.size === 0) return;
  els.previewBtn.disabled = true;
  try {
    const data = await Api.previewCleanup(buildBody());
    local.preview = data;
    renderPreview(data);
    toast(`将处理 ${data.matchedTasks} 个任务（${formatBytes(data.matchedBytes)}）`, "ph-eye");
  } catch (e) {
    toast(e.message || "预览失败", "ph-warning-circle");
  } finally {
    els.previewBtn.disabled = false;
  }
}

async function runApply() {
  if (local.selected.size === 0) return;
  if (!confirm(`确定要清理所选任务吗？\n\n运行中任务会被自动跳过。\n该操作不可撤销。`)) return;

  els.apply.disabled = true;
  try {
    const res = await Api.runCleanup(buildBody());
    const msg = res.deletedTasks
      ? `已清理 ${res.deletedTasks} 个任务，释放 ${formatBytes(res.deletedBytes)}`
      : `已清理 ${formatBytes(res.deletedBytes)} 产物`;
    toast(msg, "ph-trash");
    local.selected.clear();
    local.preview = null;
    closePreview();
    await refresh();
    // 任务列表也需要同步
    await loadTasks();
  } catch (e) {
    toast(e.message || "执行清理失败", "ph-warning-circle");
  } finally {
    els.apply.disabled = false;
    updateActions();
  }
}

function renderPreview(data) {
  els.previewBox.hidden = false;
  const skipIds = new Set((data.skippedTasks || []).map((t) => t.taskId));
  const targetIds = new Set((data.targets || []).map((t) => t.taskId));

  // 同步 local.selected：把预览后才进入 RUNNING 的从选中里去掉
  for (const id of skipIds) local.selected.delete(id);
  updateActions();

  const body = els.previewBody;
  body.replaceChildren();
  const summary = el("div", "storage__preview-summary");
  summary.innerHTML = `
    <span>将处理 <b>${data.matchedTasks}</b> 个任务</span>
    <span>预计释放 <b>${formatBytes(data.matchedBytes)}</b></span>
    ${skipIds.size > 0 ? `<span>已跳过 <b>${skipIds.size}</b> 个运行中任务</span>` : ""}
  `;
  body.append(summary);

  if (targetIds.size > 0) {
    const list = el("div", "storage__preview-list");
    for (const t of data.targets.slice(0, 12)) {
      const chip = el("span", "storage__preview-chip");
      chip.textContent = `${t.title || t.taskId} · ${formatBytes(t.size)}`;
      chip.title = `${t.taskId} (${t.status})`;
      list.append(chip);
    }
    if (data.targets.length > 12) {
      const more = el("span", "storage__preview-chip");
      more.textContent = `… 另有 ${data.targets.length - 12} 个`;
      list.append(more);
    }
    body.append(list);
  }
  if (skipIds.size > 0) {
    const list = el("div", "storage__preview-list");
    for (const t of data.skippedTasks) {
      const chip = el("span", "storage__preview-chip storage__preview-chip--skipped");
      chip.textContent = `跳过 · ${t.title || t.taskId} (${STATUS_LABEL[t.status] || t.status})`;
      chip.title = t.taskId;
      list.append(chip);
    }
    body.append(list);
  }
}

function closePreview() {
  els.previewBox.hidden = true;
  local.preview = null;
}

/* ---------- 工具 ---------- */
function formatBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const fixed = v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1);
  return `${fixed} ${units[i]}`;
}

function badgeClass(s) {
  if (s === "SUCCESS") return "storage-badge--success";
  if (s === "FAILED") return "storage-badge--failed";
  if (s === "PENDING") return "storage-badge--pending";
  return "storage-badge--active";
}
function badgeIcon(s) {
  if (s === "SUCCESS") return "ph-check";
  if (s === "FAILED") return "ph-warning";
  if (s === "PENDING") return "ph-clock";
  return "ph-spinner";
}

function cssEscape(s) {
  // 简易 CSS attr escape：taskId 形如 task_xxxxxxxx，安全字符
  return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
}
