/* 高级设置：多服务商引擎配置、脱敏状态与连接检测。 */

import { $, escapeHtml } from "./utils.js";
import { Api } from "./api.js";
import { toast } from "./toast.js";
import { state } from "./store.js";

const STATUS = {
  AVAILABLE: ["available", "可用"],
  CHECKING: ["checking", "检测中"],
  UNKNOWN: ["unknown", "待检测"],
  UNAVAILABLE: ["unavailable", "不可用"],
  UNCONFIGURED: ["unconfigured", "未配置"],
};

let engines = [];

function statusView(value) {
  return STATUS[value] || STATUS.UNKNOWN;
}

function typeLabel(value) {
  return value === "anthropic_compatible" ? "Anthropic Compatible" : "OpenAI Compatible";
}

function cardTemplate(engine) {
  const status = statusView(engine.availability);
  const id = engine.id || "new";
  const isNew = !engine.id;
  return `<article class="engine-card" data-engine-id="${escapeHtml(id)}">
    <div class="engine-card__top">
      <div class="engine-card__identity">
        <span class="engine-card__icon"><i class="ph ${engine.apiType === "anthropic_compatible" ? "ph-aperture" : "ph-brackets-curly"}"></i></span>
        <div><h3 class="engine-card__name">${escapeHtml(engine.name || "新翻译引擎")}</h3><div class="engine-card__type">${typeLabel(engine.apiType)}</div></div>
      </div>
      <span class="engine-status engine-status--${status[0]}">${status[1]}</span>
    </div>
    <div class="engine-card__form">
      <div class="engine-field"><label>显示名称</label><input data-field="name" value="${escapeHtml(engine.name || "")}" placeholder="例如：DeepSeek 主力" /></div>
      <div class="engine-card__grid">
        <div class="engine-field"><label>API 接入类型</label><select data-field="apiType"><option value="openai_compatible" ${engine.apiType !== "anthropic_compatible" ? "selected" : ""}>OpenAI Compatible</option><option value="anthropic_compatible" ${engine.apiType === "anthropic_compatible" ? "selected" : ""}>Anthropic Compatible</option></select></div>
        <div class="engine-field"><label>模型</label><input data-field="model" value="${escapeHtml(engine.model || "")}" placeholder="例如：gpt-4.1-mini" /></div>
      </div>
      <div class="engine-field"><label>Base URL</label><input data-field="baseUrl" value="${escapeHtml(engine.baseUrl || "")}" placeholder="https://api.openai.com/v1" /></div>
      <div class="engine-field"><label>API Key ${engine.hasApiKey ? "<span style=\"color:var(--ok-text)\">· 已配置，留空保持不变</span>" : ""}</label><input data-field="apiKey" type="password" autocomplete="new-password" placeholder="${engine.hasApiKey ? "已配置 · 不修改" : "粘贴 API Key"}" /></div>
    </div>
    <div class="engine-card__foot">
      <label class="engine-card__meta"><input data-field="enabled" type="checkbox" ${engine.enabled !== false ? "checked" : ""} /> 在任务页启用</label>
      <div class="engine-card__actions">
        ${!isNew ? `<button class="btn btn--ghost btn--sm" data-action="validate" type="button"><i class="ph ph-plugs-connected"></i><span>检测</span></button>` : ""}
        <button class="btn btn--primary btn--sm" data-action="save" type="button"><i class="ph ph-floppy-disk"></i><span>保存</span></button>
        ${!isNew ? `<button class="iconbtn" data-action="delete" type="button" title="删除配置"><i class="ph ph-trash"></i></button>` : ""}
      </div>
    </div>
  </article>`;
}

function values(card) {
  const get = (name) => card.querySelector(`[data-field="${name}"]`);
  return {
    name: get("name").value.trim(), apiType: get("apiType").value,
    baseUrl: get("baseUrl").value.trim(), model: get("model").value.trim(),
    apiKey: get("apiKey").value.trim(), enabled: get("enabled").checked,
  };
}

async function refresh(autoCheck = true) {
  try {
    engines = await Api.listTranslationEngines();
    render();
    if (autoCheck) {
      const pending = engines.filter((e) => e.hasApiKey && ["UNKNOWN", "UNAVAILABLE"].includes(e.availability));
      for (const engine of pending) {
        try { await Api.validateTranslationEngine(engine.id); } catch (_) {}
      }
      if (pending.length) {
        engines = await Api.listTranslationEngines();
        render();
      }
    }
  } catch (err) {
    toast(err.message || "无法读取翻译引擎配置", "ph-warning-circle");
  }
}

function render() {
  const list = $("#engineList");
  const empty = $("#engineEmpty");
  if (!list || !empty) return;
  list.innerHTML = engines.map(cardTemplate).join("");
  empty.hidden = engines.length > 0;
}

async function onAction(event) {
  const action = event.target.closest("[data-action]")?.dataset.action;
  const card = event.target.closest(".engine-card");
  if (!action || !card) return;
  const id = card.dataset.engineId;
  try {
    if (action === "save") {
      const payload = values(card);
      if (!payload.name || !payload.baseUrl || !payload.model) throw new Error("请填写名称、Base URL 和模型");
      const saved = id === "new" ? await Api.createTranslationEngine(payload) : await Api.updateTranslationEngine(id, payload);
      engines = id === "new" ? [...engines, saved] : engines.map((e) => e.id === id ? saved : e);
      render();
      toast("翻译引擎已保存，正在检测连接", "ph-check-circle");
      if (saved.hasApiKey) await Api.validateTranslationEngine(saved.id);
      await refresh(false);
      document.dispatchEvent(new CustomEvent("translation-engines-change"));
      return;
    }
    if (action === "validate") {
      await Api.validateTranslationEngine(id);
      await refresh(false);
      toast("引擎检测完成", "ph-check-circle");
      document.dispatchEvent(new CustomEvent("translation-engines-change"));
      return;
    }
    if (action === "delete" && window.confirm("删除这个翻译引擎配置？")) {
      await Api.deleteTranslationEngine(id);
      engines = engines.filter((e) => e.id !== id);
      render();
      document.dispatchEvent(new CustomEvent("translation-engines-change"));
    }
  } catch (err) {
    toast(err.message || "操作失败", "ph-warning-circle");
  }
}

export function initTranslationSettings() {
  const add = $("#engineAdd");
  const list = $("#engineList");
  if (!add || !list) return;
  add.addEventListener("click", () => {
    engines.push({ apiType: "openai_compatible", availability: "UNCONFIGURED", enabled: true });
    render();
    list.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  list.addEventListener("click", onAction);
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view === "translation-settings" && state.view === "translation-settings") refresh();
  });
  if (state.view === "translation-settings") refresh();
}
