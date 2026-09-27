/* Replicate 配置：Token 脱敏展示，其他参数即时持久化到后端。 */

import { $ } from "./utils.js";
import { Api } from "./api.js";
import { toast } from "./toast.js";

function setStatus(kind, text) {
  const node = $("#replicateSettingsStatus");
  if (!node) return;
  node.className = `engine-status engine-status--${kind}`;
  node.textContent = text;
}

function fill(data) {
  $("#replicateWhisperModel").value = data.whisperModel || "";
  $("#replicateTimeout").value = data.timeout ?? 1800;
  $("#replicateRetries").value = data.retries ?? 3;
  $("#replicateRetryInterval").value = data.retryInterval ?? 3600;
  $("#replicatePollInterval").value = data.pollInterval ?? 30;
  // Token 只允许写入，不回填明文，避免页面源码或 DOM 暴露凭据。
  $("#replicateApiToken").value = "";
  const status = $("#replicateTokenStatus");
  if (status) {
    status.textContent = data.hasApiToken ? "已配置 Token" : "未配置 Token";
    status.className = `engine-status engine-status--${data.hasApiToken ? "available" : "unconfigured"}`;
  }
  setStatus(data.hasApiToken ? "available" : "unknown", data.hasApiToken ? "已配置" : "待配置");
  const meta = $("#replicateSettingsMeta");
  if (meta) meta.textContent = data.hasApiToken ? "页面配置优先于 REPLICATE_API_TOKEN" : "可使用环境变量或在此配置 Token";
}

async function refresh() {
  setStatus("checking", "正在读取");
  try {
    fill(await Api.getReplicateSettings());
  } catch (error) {
    setStatus("unavailable", "读取失败");
    toast(error.message || "读取 Replicate 设置失败", "ph-warning-circle");
  }
}

function payload(apiToken) {
  return {
    apiToken,
    whisperModel: $("#replicateWhisperModel").value.trim(),
    timeout: Number($("#replicateTimeout").value),
    retries: Number($("#replicateRetries").value),
    retryInterval: Number($("#replicateRetryInterval").value),
    pollInterval: Number($("#replicatePollInterval").value),
  };
}

async function save(event) {
  event.preventDefault();
  const button = $("#replicateSettingsSave");
  button.disabled = true;
  try {
    const token = $("#replicateApiToken").value.trim();
    const data = await Api.updateReplicateSettings(payload(token || null));
    fill(data);
    document.dispatchEvent(new CustomEvent("replicate-settings-saved"));
    toast("Replicate 设置已保存", "ph-check-circle");
  } catch (error) {
    toast(error.message || "保存 Replicate 设置失败", "ph-warning-circle");
  } finally {
    button.disabled = false;
  }
}

async function clearToken() {
  const button = $("#replicateApiTokenClear");
  button.disabled = true;
  try {
    const data = await Api.updateReplicateSettings(payload(""));
    fill(data);
    document.dispatchEvent(new CustomEvent("replicate-settings-saved"));
    toast("已清除页面 Token，将使用环境变量", "ph-check-circle");
  } catch (error) {
    toast(error.message || "清除 Replicate Token 失败", "ph-warning-circle");
  } finally {
    button.disabled = false;
  }
}

export function initReplicateSettings() {
  const form = $("#replicateSettingsForm");
  if (!form) return;
  form.addEventListener("submit", save);
  $("#replicateApiTokenClear")?.addEventListener("click", clearToken);
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view === "other-settings" && event.detail?.settingsTab === "replicate") refresh();
  });
  refresh();
}
