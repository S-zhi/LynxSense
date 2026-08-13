/* 任务控制台：参数初始化 + URL 校验 + 提交 */

import { $, el } from "./utils.js";
import { createTask } from "./store.js";
import { toast } from "./toast.js";
import { Api } from "./api.js";
import { LANG_LABEL } from "./constants.js";

const CFG = window.APP_CONFIG;
const FALLBACK_LANGUAGES = ["en", "zh", "de", "es", "ru", "ko", "fr", "ja"];
const FALLBACK_TARGET_LANGUAGES = ["zh-CN", "zh-TW", "en", "ja", "ko"];
const FALLBACK_MODELS = [
  "tiny.en", "tiny", "base.en", "base", "small.en",
  "small", "medium.en", "medium", "large-v1", "large-v2",
];
const DEFAULT_SOURCE_LANGUAGE = "en";
const URL_RE = /^https?:\/\/.+/i;
const VIDEO_EXT_RE = /\.(mp4|mov|mkv|webm|avi|m4v|flv|ts|mpeg|mpg|wmv)$/i;
const PROBE_DEBOUNCE_MS = 800;
const LANGUAGE_DISPLAY = typeof Intl !== "undefined" && Intl.DisplayNames
  ? new Intl.DisplayNames(["zh-CN"], { type: "language" })
  : null;

function initEngines() {
  // 初始化翻译引擎下拉框。
  const sel = $("#engine");
  sel.innerHTML = "";
  CFG.TRANSLATION_ENGINES.forEach((e) => {
    const o = el("option");
    o.value = e.value;
    o.textContent = e.enabled ? e.label : e.label + "（暂未开放）";
    o.disabled = !e.enabled;
    sel.append(o);
  });
  const first = CFG.TRANSLATION_ENGINES.find((e) => e.enabled);
  if (first) sel.value = first.value;
}

function option(value, label = value) {
  // 构造一个 select option 元素。
  const item = el("option");
  item.value = value;
  item.textContent = label;
  return item;
}

function languageLabel(code) {
  // 把语言代码转换为中文展示文案，提交时仍使用原始代码。
  if (LANG_LABEL[code]) return LANG_LABEL[code];
  try {
    return LANGUAGE_DISPLAY?.of(code) || code;
  } catch (e) {
    return code;
  }
}

function isValidUrl(url) {
  // 判断输入是否是后端可接受的 http(s) 页面地址。
  return URL_RE.test(url);
}

function isVideoFile(file) {
  // 判断拖入或选择的文件是否属于后端支持的视频类型。
  return !!file && (file.type.startsWith("video/") || VIDEO_EXT_RE.test(file.name || ""));
}

function probeOkMessage(result) {
  // 根据探针成功结果生成控制台提示文案。
  return result.title
    ? `链接可下载：${result.title}`
    : "链接可下载，可以开始处理";
}

function probeFailMessage(result) {
  // 根据探针失败结果生成控制台提示文案。
  return result.reason || result.detail || "这个链接暂时无法下载";
}

function renderSourceLanguages(languages) {
  // 渲染源语言下拉框，保留自动检测作为本地特殊选项。
  const sel = $("#sourceLang");
  const current = sel.value || DEFAULT_SOURCE_LANGUAGE;
  sel.innerHTML = "";
  sel.append(option("auto", "自动检测"));
  languages.forEach((code) => {
    sel.append(option(code, languageLabel(code)));
  });
  sel.value = [...sel.options].some((item) => item.value === current)
    ? current
    : DEFAULT_SOURCE_LANGUAGE;
}

function renderTargetLanguages(languages) {
  // 渲染目标语言下拉框。
  const sel = $("#targetLang");
  const current = sel.value || "zh-CN";
  sel.innerHTML = "";
  languages.forEach((code) => {
    sel.append(option(code, languageLabel(code)));
  });
  sel.value = [...sel.options].some((item) => item.value === current)
    ? current
    : (languages[0] || "zh-CN");
}

function modelLabel(model) {
  // 生成 Whisper 模型下拉框展示文案。
  const labels = {
    "tiny.en": "tiny.en · 英语最快",
    tiny: "tiny · 最快",
    "base.en": "base.en · 英语快",
    base: "base · 快",
    "small.en": "small.en · 英语推荐",
    small: "small · 推荐",
    "medium.en": "medium.en · 英语较准",
    medium: "medium · 较准",
    "large-v1": "large-v1 · 高精度",
    "large-v2": "large-v2 · 高精度",
  };
  return labels[model] || model;
}

function renderModelWeights(models) {
  // 渲染 Whisper 模型权重下拉框。
  const sel = $("#model");
  const current = sel.value || "small";
  sel.innerHTML = "";
  models.forEach((model) => {
    sel.append(option(model, modelLabel(model)));
  });
  sel.value = [...sel.options].some((item) => item.value === current) ? current : "small";
}

async function initSrtOptions() {
  // 从后端加载源语言、目标语言和模型权重选项，失败时使用本地兜底。
  renderSourceLanguages(FALLBACK_LANGUAGES);
  renderTargetLanguages(CFG.TARGET_LANGUAGES || FALLBACK_TARGET_LANGUAGES);
  renderModelWeights(FALLBACK_MODELS);

  try {
    const [languages, targetLanguages, models] = await Promise.all([
      Api.listVideoLanguages(),
      Api.listTargetLanguages(),
      Api.listModelWeights(),
    ]);
    renderSourceLanguages(languages);
    renderTargetLanguages(targetLanguages);
    renderModelWeights(models);
  } catch (err) {
    toast(err.message || "获取识别选项失败，已使用默认选项", "ph-warning-circle");
  }
}

export function initConsole() {
  // 初始化控制台表单交互。
  initEngines();
  initSrtOptions();

  const form = $("#taskForm");
  const urlInput = $("#url");
  const fileInput = $("#videoFile");
  const uploadBtn = $("#uploadBtn");
  const hint = $("#urlHint");
  const bar = $("#consoleBar");
  const submitBtn = $("#submitBtn");
  let probeTimer = null;
  let probeSeq = 0;
  let submitting = false;
  let selectedFile = null;
  let dragDepth = 0;
  const probeState = { status: "idle", url: "", result: null };

  function syncSubmitDisabled() {
    // 根据提交状态和探针状态同步提交按钮可用性。
    submitBtn.disabled = submitting
      || probeState.status === "checking"
      || probeState.status === "failed";
  }

  function clearProbeTimer() {
    // 清理尚未触发的自动探测定时器。
    if (probeTimer) clearTimeout(probeTimer);
    probeTimer = null;
  }

  function setUrlHint(message, isError = false) {
    // 更新 URL 提示区域及错误样式。
    bar.classList.toggle("is-error", isError);
    hint.classList.toggle("is-error", isError);
    hint.textContent = message;
  }

  function setProbeState(status, message, isError = false, result = null) {
    // 更新当前探针状态并刷新按钮与提示。
    probeState.status = status;
    probeState.result = result;
    setUrlHint(message, isError);
    syncSubmitDisabled();
  }

  function resetProbeState(message = "粘贴单个视频页面地址，或把本地视频拖进输入框") {
    // 重置探针状态到空闲。
    clearProbeTimer();
    probeSeq += 1;
    probeState.status = "idle";
    probeState.url = "";
    probeState.result = null;
    setUrlHint(message, false);
    syncSubmitDisabled();
  }

  function clearSelectedFile() {
    // 清空已选择的本地视频，恢复 URL 输入模式。
    selectedFile = null;
    fileInput.value = "";
    urlInput.dataset.sourceType = "url";
    urlInput.placeholder = "粘贴视频页面地址，或拖入本地视频";
  }

  function setSelectedFile(file) {
    // 记录本地视频并切换到上传模式。
    selectedFile = file;
    clearProbeTimer();
    probeSeq += 1;
    probeState.status = "idle";
    probeState.url = "";
    probeState.result = null;
    urlInput.dataset.sourceType = "upload";
    urlInput.value = file.name;
    urlInput.placeholder = "已选择本地视频";
    setUrlHint(`已选择本地视频：${file.name}，会跳过在线下载并按当前参数继续处理`, false);
    syncSubmitDisabled();
  }

  async function runProbe(url) {
    // 对当前 URL 执行一次后端探针校验，并忽略过期返回。
    const seq = ++probeSeq;
    probeState.url = url;
    setProbeState("checking", "正在检查链接是否可下载...");
    try {
      const result = await Api.probeVideo(url);
      if (seq !== probeSeq) return { ok: false, stale: true };
      probeState.url = url;
      if (result.ok) {
        setProbeState("ok", probeOkMessage(result), false, result);
      } else {
        setProbeState("failed", probeFailMessage(result), true, result);
      }
      return result;
    } catch (err) {
      if (seq !== probeSeq) return { ok: false, stale: true };
      const result = {
        ok: false,
        reason: err.message || "链接校验失败",
      };
      setProbeState("failed", probeFailMessage(result), true, result);
      return result;
    }
  }

  function scheduleProbe() {
    // URL 输入变化后延迟触发探针，避免每次按键都请求后端。
    if (currentVideoFile()) {
      clearProbeTimer();
      probeSeq += 1;
      probeState.status = "idle";
      probeState.url = "";
      probeState.result = null;
      setUrlHint(`已选择本地视频：${currentVideoFile().name}，会跳过在线下载并按当前参数继续处理`, false);
      syncSubmitDisabled();
      return;
    }
    const url = urlInput.value.trim();
    clearProbeTimer();
    if (!url) {
      resetProbeState();
      return;
    }
    if (!isValidUrl(url)) {
      probeSeq += 1;
      probeState.url = url;
      setProbeState("failed", "请输入有效的视频链接（以 http(s):// 开头）", true);
      return;
    }
    probeState.url = url;
    setProbeState("checking", "等待链接输入完成后自动检查...");
    probeTimer = setTimeout(() => runProbe(url), PROBE_DEBOUNCE_MS);
  }

  async function ensureProbeOk(url) {
    // 提交前确保当前 URL 已通过探针，防止绕过自动校验。
    clearProbeTimer();
    if (probeState.url === url && probeState.status === "ok" && probeState.result?.ok) {
      return probeState.result;
    }
    return runProbe(url);
  }

  function collectPayload() {
    // 收集表单参数，URL 和上传任务共用同一组字幕/烧录设置。
    return {
      sourceLang: $("#sourceLang").value,
      targetLang: $("#targetLang").value,
      mode: form.elements.mode.value,
      burn: form.elements.burn.value,
      model: $("#model").value,
      engine: $("#engine").value,
      needSubtitle: form.elements.needSubtitle.value === "on",
    };
  }

  function currentVideoFile() {
    // 读取当前上传视频；即使组件状态被重建，也以 file input 中的文件为准。
    const file = selectedFile || Array.from(fileInput.files || []).find(isVideoFile);
    return isVideoFile(file) ? file : null;
  }

  function pickDroppedFile(dt) {
    // 从拖拽数据中选出第一个视频文件。
    return Array.from(dt?.files || []).find(isVideoFile) || null;
  }

  function useDroppedUrl(dt) {
    // 支持把文本 URL 拖进输入框，行为与手动粘贴一致。
    const text = (dt?.getData("text/uri-list") || dt?.getData("text/plain") || "").trim();
    if (!text) return false;
    clearSelectedFile();
    urlInput.value = text.split(/\s+/)[0];
    scheduleProbe();
    return true;
  }

  urlInput.addEventListener("input", scheduleProbe);
  uploadBtn.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    fileInput.click();
  });
  fileInput.addEventListener("change", () => {
    const file = Array.from(fileInput.files || []).find(isVideoFile);
    if (!file) {
      clearSelectedFile();
      setProbeState("failed", "请选择后端支持的视频文件", true);
      return;
    }
    setSelectedFile(file);
  });

  ["dragenter", "dragover"].forEach((type) => {
    bar.addEventListener(type, (e) => {
      e.preventDefault();
      dragDepth += type === "dragenter" ? 1 : 0;
      bar.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    bar.addEventListener(type, (e) => {
      e.preventDefault();
      dragDepth = type === "drop" ? 0 : Math.max(0, dragDepth - 1);
      if (dragDepth === 0) bar.classList.remove("is-dragover");
    });
  });
  bar.addEventListener("drop", (e) => {
    const file = pickDroppedFile(e.dataTransfer);
    if (file) {
      setSelectedFile(file);
      return;
    }
    if (useDroppedUrl(e.dataTransfer)) return;
    setProbeState("failed", "请拖入视频文件，或粘贴有效的视频链接", true);
  });

  // 「是否需要字幕」：选“仅下载”时禁用字幕相关参数（源/目标语言、模式、烧录、模型、引擎）
  const paramsBox = form.querySelector(".params");
  function syncSubtitleParams() {
    const need = form.elements.needSubtitle.value === "on";
    paramsBox.querySelectorAll(".param").forEach((p) => {
      if (p.querySelector('[name="needSubtitle"]')) return; // 跳过开关自身
      p.classList.toggle("is-disabled", !need);
      p.querySelectorAll("select, input").forEach((c) => (c.disabled = !need));
    });
  }
  form.querySelectorAll('input[name="needSubtitle"]').forEach((r) =>
    r.addEventListener("change", syncSubtitleParams)
  );
  syncSubtitleParams();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const videoFile = currentVideoFile();
    const url = urlInput.value.trim();

    if (!videoFile && (!url || !isValidUrl(url))) {
      setProbeState("failed", "请输入有效的视频链接（以 http(s):// 开头）", true);
      urlInput.focus();
      return;
    }

    if (!videoFile) {
      const probe = await ensureProbeOk(url);
      if (!probe.ok) {
        urlInput.focus();
        return;
      }
    }

    const payload = {
      ...collectPayload(),
      ...(videoFile ? { file: videoFile } : { url }),
    };

    submitting = true;
    syncSubmitDisabled();
    try {
      await createTask(payload);
      toast("任务已加入队列", "ph-check-circle");
      urlInput.value = "";
      clearSelectedFile();
      resetProbeState();
    } catch (err) {
      toast(err.message || "创建任务失败", "ph-warning-circle");
    } finally {
      submitting = false;
      syncSubmitDisabled();
    }
  });
}
