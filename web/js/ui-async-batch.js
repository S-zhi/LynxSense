/* 异步批次发布：输入收集、责任链编排与批次历史。 */
import { $, el } from "./utils.js";
import { Api } from "./api.js";
import { createTask } from "./store.js";
import { toast } from "./toast.js";

const STORAGE_KEY = "subtrans_async_batch_runs";
const VIDEO_RE = /\.(mp4|mov|mkv|webm|avi|m4v|flv|ts|mpeg|mpg|wmv)$/i;
const NODE_DEFS = [
  ["download", "下载源视频", "从 URL 或本地文件取得源视频"],
  ["transcribe", "语音识别", "将对白转换为原文字幕"],
  ["translate", "翻译字幕", "生成目标语言字幕"],
  ["burn", "烧录字幕", "把字幕合成到成品视频"],
  ["deliver", "转移产物", "整理并转移最终产物"],
];

function readRuns() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"); } catch (_) { return []; }
}
function saveRuns(runs) { localStorage.setItem(STORAGE_KEY, JSON.stringify(runs.slice(0, 8))); }
function activeNodes() { return Array.from($("#batchChain").querySelectorAll(".chain-item.is-enabled")); }
function renderSummary() {
  const count = getInputs().length;
  const enabled = activeNodes().length;
  $("#batchSummary").textContent = `${count} 个输入 · ${enabled} 个节点启用`;
  $("#batchSubmit").disabled = count === 0 || enabled === 0;
}
function getInputs() {
  const source = $("input[name=batchSource]:checked")?.value;
  if (source === "folder") return Array.from($("#batchFolder")?.files || []).filter((f) => f.type.startsWith("video/") || VIDEO_RE.test(f.name));
  return $("#batchUrls").value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
}
function renderRuns() {
  const box = $("#batchRuns");
  const runs = readRuns();
  box.innerHTML = runs.length ? runs.map((run) => `<article class="batch-run"><div><strong>${run.count} 个任务</strong><span>${new Date(run.createdAt).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span></div><div class="batch-run__meta">${run.chain.join(" → ")}</div><span class="batch-run__state">${run.status}</span></article>`).join("") : '<div class="batch-runs__empty">发布后的批次会显示在这里</div>';
}
function renderChain() {
  const chain = $("#batchChain");
  chain.innerHTML = NODE_DEFS.map(([id, title, desc], index) => `<div class="chain-item is-enabled" draggable="true" data-node="${id}"><button class="chain-item__handle" type="button" aria-label="拖动${title}"><i class="ph ph-dots-six-vertical"></i></button><span class="chain-item__index">0${index + 1}</span><div class="chain-item__copy"><strong>${title}</strong><span>${desc}</span></div><label class="toggle" title="启用或跳过 ${title}"><input type="checkbox" checked /><span></span><em>启用</em></label></div>`).join("");
  chain.addEventListener("change", (event) => { const label = event.target.closest(".toggle"); if (!label) return; const item = label.closest(".chain-item"); item.classList.toggle("is-enabled", event.target.checked); label.querySelector("em").textContent = event.target.checked ? "启用" : "Skip"; renderSummary(); });
  let dragged = null;
  chain.addEventListener("dragstart", (e) => { dragged = e.target.closest(".chain-item"); dragged?.classList.add("is-dragging"); });
  chain.addEventListener("dragend", () => { dragged?.classList.remove("is-dragging"); dragged = null; renderSummary(); });
  chain.addEventListener("dragover", (e) => { e.preventDefault(); const target = e.target.closest(".chain-item"); if (!dragged || !target || target === dragged) return; const box = target.getBoundingClientRect(); chain.insertBefore(dragged, e.clientY < box.top + box.height / 2 ? target : target.nextSibling); });
}
function collectParams() { return { sourceLang: $("#batchSourceLang").value, targetLang: $("#batchTargetLang").value, mode: "mono", burn: "hard", model: "small", engine: $("#batchEngine").value, needSubtitle: activeNodes().some((item) => ["transcribe", "translate", "burn"].includes(item.dataset.node)) }; }

export function initAsyncBatch() {
  const form = $("#asyncBatchForm");
  if (!form) return;
  renderChain(); renderRuns();
  form.querySelectorAll("input[name=batchSource]").forEach((radio) => radio.addEventListener("change", () => { form.querySelectorAll("[data-batch-source]").forEach((el) => { el.hidden = el.dataset.batchSource !== radio.value; }); renderSummary(); }));
  $("#batchUrls").addEventListener("input", renderSummary);
  $("#batchFolder").addEventListener("change", () => { const files = getInputs(); $("#batchFolderHint").textContent = files.length ? `已选择 ${files.length} 个视频文件` : "未找到支持的视频文件"; $("#batchFileList").textContent = files.slice(0, 6).map((f) => f.webkitRelativePath || f.name).join(" · ") || "尚未选择文件"; renderSummary(); });
  $("#batchClearRuns").addEventListener("click", () => { saveRuns([]); renderRuns(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const inputs = getInputs();
    if (!inputs.length || !activeNodes().length) return;
    const params = collectParams(); const button = $("#batchSubmit"); button.disabled = true; $("#asyncBatchStatus").textContent = `正在发布 0 / ${inputs.length}`;
    let created = 0; let cursor = 0;
    const worker = async () => {
      while (cursor < inputs.length) {
        const input = inputs[cursor++];
        try { await createTask(typeof input === "string" ? { ...params, url: input } : { ...params, file: input }); created += 1; $("#asyncBatchStatus").textContent = `正在发布 ${created} / ${inputs.length}`; }
        catch (error) { toast(`${input.name || input}：${error.message || "发布失败"}`, "ph-warning-circle"); }
      }
    };
    const concurrency = Math.max(1, Math.min(8, Number($("#batchConcurrency").value) || 2));
    await Promise.all(Array.from({ length: Math.min(concurrency, inputs.length) }, worker));
    const chain = Array.from($("#batchChain").querySelectorAll(".chain-item")).filter((item) => item.classList.contains("is-enabled")).map((item) => item.querySelector("strong").textContent);
    const runs = readRuns(); runs.unshift({ count: created, chain, status: created === inputs.length ? "已发布" : `部分完成 ${created}/${inputs.length}`, createdAt: Date.now() }); saveRuns(runs); renderRuns(); $("#asyncBatchStatus").textContent = `已发布 ${created} 个任务`; button.disabled = false; renderSummary();
  });
  renderSummary();
}
