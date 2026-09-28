import { $, escapeHtml } from "./utils.js";
import { Api } from "./api.js";
import { toast } from "./toast.js";

const SIDECARS = new Set([
  "config.json", "generation_config.json", "preprocessor_config.json",
  "tokenizer_config.json", "tokenizer.json", "vocab.json", "merges.txt",
  "normalizer.json", "special_tokens_map.json", "added_tokens.json",
  "pytorch_model.bin.index.json", "model.safetensors.index.json",
]);
const WEIGHT = /^(pytorch_model(?:-\d{5}-of-\d{5})?\.bin|model(?:-\d{5}-of-\d{5})?\.safetensors)$/;

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function render(items) {
  const root = $("#localModelsList");
  if (!root) return;
  const ready = items.filter((item) => item.status === "READY");
  root.innerHTML = ready.length ? ready.map((item) => {
    const name = escapeHtml(item.name);
    const label = escapeHtml(item.label || item.name);
    return `<article class="local-model-card" aria-label="${label}">
      <div class="local-model-card__top">
        <div class="local-model-card__identity"><span class="local-model-card__icon"><i class="ph ph-cpu" aria-hidden="true"></i></span><div><h3>${label}</h3><span class="local-model-card__name">${name}</span></div></div>
        <span class="local-model-card__status local-model-card__status--ready">已就绪</span>
      </div>
      <div class="local-model-card__meta"><span>${item.format === "huggingface" ? "Hugging Face" : "CTranslate2"}</span><strong>${formatBytes(item.size) || escapeHtml(item.size || "")}</strong></div>
      <div class="local-model-card__foot">
        <span class="local-model-card__phase">本地</span>
        <div class="local-model-actions">
          ${item.format === "huggingface" ? `<button class="btn btn--ghost btn--sm" type="button" data-model-check="${name}" title="检查模型" aria-label="检查 ${label}"><i class="ph ph-shield-check" aria-hidden="true"></i></button>` : ""}
          <button class="btn btn--ghost btn--sm local-model-card__delete" type="button" data-model-delete="${name}" title="删除模型" aria-label="删除 ${label}"><i class="ph ph-trash" aria-hidden="true"></i></button>
        </div>
      </div>
    </article>`;
  }).join("") : '<div class="local-model-empty">暂无本地模型</div>';

  root.querySelectorAll("[data-model-check]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await Api.checkLocalModel(button.dataset.modelCheck);
      toast("模型检查通过", "ph-shield-check");
    } catch (error) {
      toast(error.message || "模型检查失败", "ph-warning-circle");
    } finally {
      button.disabled = false;
      await refresh();
    }
  }));

  root.querySelectorAll("[data-model-delete]").forEach((button) => button.addEventListener("click", async () => {
    const name = button.dataset.modelDelete;
    if (!window.confirm(`删除 ${name} 并释放本地空间？`)) return;
    button.disabled = true;
    try {
      await Api.deleteLocalModel(name);
      [...($("#model")?.options || [])].find((option) => option.value === `local:${name}`)?.remove();
      toast("模型已删除", "ph-trash");
      await refresh();
    } catch (error) {
      button.disabled = false;
      toast(error.message || "删除本地模型失败", "ph-warning-circle");
    }
  }));
}

async function refresh() {
  try {
    const items = await Api.listLocalModels();
    render(items);
    const select = $("#model");
    if (select) {
      const ready = new Set(items.filter((item) => item.status === "READY").map((item) => `local:${item.name}`));
      [...select.options].filter((option) => option.value.startsWith("local:") && !ready.has(option.value)).forEach((option) => option.remove());
      for (const item of items.filter((model) => model.status === "READY")) {
        if (![...select.options].some((option) => option.value === `local:${item.name}`)) {
          select.add(new Option(`本地 · ${item.label || item.name}`, `local:${item.name}`));
        }
      }
    }
  } catch (error) {
    toast(error.message || "获取本地模型失败", "ph-warning-circle");
  }
}

export function initLocalModels() {
  const form = $("#localModelImportForm");
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("#localModelName").value.trim();
    const label = $("#localModelLabel").value.trim();
    const files = [...$("#localModelFiles").files].filter((file) => {
      const relative = file.webkitRelativePath || file.name;
      return relative.split("/").length <= 2 && (SIDECARS.has(file.name) || WEIGHT.test(file.name));
    });
    const submit = form.querySelector('[type="submit"]');
    const status = $("#localModelImportStatus");
    submit.disabled = true;
    status.textContent = "正在导入并检查";
    try {
      const item = await Api.importLocalModel(name, label, files);
      const select = $("#model");
      if (select && ![...select.options].some((option) => option.value === `local:${item.name}`)) {
        select.add(new Option(`本地 · ${item.label || item.name}`, `local:${item.name}`));
      }
      form.reset();
      status.textContent = "检查通过";
      toast("模型已导入并通过检查", "ph-shield-check");
      await refresh();
    } catch (error) {
      status.textContent = "检查失败";
      toast(error.message || "导入本地模型失败", "ph-warning-circle");
    } finally {
      submit.disabled = false;
    }
  });
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view === "other-settings" && event.detail?.settingsTab === "models") void refresh();
  });
}
