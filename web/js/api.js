/* 数据层：真实接口（REST + SSE）与 mock，按 config 切换。契约与后端一致。 */

import { TERMINAL, LANG_LABEL } from "./constants.js";
import { uid, clamp, shortUrl, statusForProgress } from "./utils.js";

const CFG = window.APP_CONFIG;
export const USE_MOCK = CFG.USE_MOCK;

// 为普通 REST 请求统一接入超时控制；SSE 订阅保留独立连接策略。
async function request(base, path, options = {}) {
  const timeoutMs = Number(CFG.API_TIMEOUT_MS) || 15000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(`${base}${path}`, {
      ...options,
      signal: controller.signal,
    });
  } catch (e) {
    if (e && e.name === "AbortError") {
      throw new Error("连接后端超时，请检查 FastAPI 是否启动");
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function readError(res, fallback) {
  let detail = "";
  try {
    const text = await res.text();
    try {
      const data = JSON.parse(text);
      if (data && data.detail) {
        if (Array.isArray(data.detail)) {
          detail = data.detail.map((item) => `${item.loc?.join(".")}: ${item.msg}`).join("; ");
        } else {
          detail = String(data.detail);
        }
      } else {
        detail = text || String(res.status);
      }
    } catch (e) {
      detail = text || String(res.status);
    }
  } catch (e) {
    detail = String(res.status);
  }
  if (!detail || !detail.trim()) {
    detail = String(res.status);
  }
  return `${fallback}：${detail}`;
}

const RealApi = {
  base: CFG.API_BASE_URL,

  async createTask(payload) {
    const res = await request(this.base, "/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "创建任务失败"));
    return res.json();
  },

  // 上传本地视频并创建后续字幕处理任务。
  async createUploadTask(payload) {
    if (!(payload.file instanceof File)) {
      throw new Error("请先选择本地视频文件");
    }
    const body = new FormData();
    body.append("file", payload.file, payload.file.name);
    body.append("sourceLang", payload.sourceLang);
    body.append("targetLang", payload.targetLang);
    body.append("mode", payload.mode);
    body.append("burn", payload.burn);
    body.append("model", payload.model);
    body.append("engine", payload.engine);
    body.append("needSubtitle", String(payload.needSubtitle));

    const res = await request(this.base, "/api/tasks/upload", {
      method: "POST",
      body,
    });
    if (!res.ok) throw new Error(await readError(res, "上传任务创建失败"));
    return res.json();
  },

  // 探测链接是否能被 yt-dlp 解析并找到可下载格式。
  async probeVideo(url) {
    const res = await request(this.base, "/api/tasks/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) throw new Error(await readError(res, "链接校验失败"));
    return res.json();
  },

  // 列出最近的下载测试历史记录（按时间倒序）。
  async listProbeRecords(limit = 50) {
    const res = await request(
      this.base,
      `/api/tasks/probe/records?limit=${encodeURIComponent(limit)}`,
    );
    if (!res.ok) throw new Error(await readError(res, "获取测试历史失败"));
    return res.json();
  },

  // 一键清空所有下载测试历史。
  async clearProbeRecords() {
    const res = await request(this.base, "/api/tasks/probe/records", {
      method: "DELETE",
    });
    if (!res.ok) throw new Error(await readError(res, "清空测试历史失败"));
    return res.json();
  },

  // 删除单条下载测试历史。
  async deleteProbeRecord(id) {
    const res = await request(this.base, `/api/tasks/probe/records/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error(await readError(res, "删除测试记录失败"));
  },

  async listTasks() {
    const res = await request(this.base, "/api/tasks");
    if (!res.ok) throw new Error(await readError(res, "获取任务列表失败"));
    return res.json();
  },

  // 获取源视频语言选项。
  async listVideoLanguages() {
    const res = await request(this.base, "/api/srt/languages");
    if (!res.ok) throw new Error(await readError(res, "获取源语言失败"));
    return res.json();
  },

  // 获取目标视频语言选项。
  async listTargetLanguages() {
    const res = await request(this.base, "/api/srt/target-languages");
    if (!res.ok) throw new Error("获取目标语言失败：" + res.status);
    return res.json();
  },

  // 获取 Whisper 模型权重选项。
  async listModelWeights() {
    const res = await request(this.base, "/api/srt/model-weights");
    if (!res.ok) throw new Error(await readError(res, "获取模型列表失败"));
    return res.json();
  },

  async getReplicateBalance() {
    const res = await request(this.base, "/api/replicate/balance");
    if (!res.ok) throw new Error(await readError(res, "获取 Replicate 账户状态失败"));
    return res.json();
  },

  async listTranslationEngines() {
    const res = await request(this.base, "/api/settings/translation-engines");
    if (!res.ok) throw new Error(await readError(res, "获取翻译引擎失败"));
    return res.json();
  },

  async createTranslationEngine(payload) {
    const res = await request(this.base, "/api/settings/translation-engines", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "创建翻译引擎失败"));
    return res.json();
  },

  async updateTranslationEngine(id, payload) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "保存翻译引擎失败"));
    return res.json();
  },

  async validateTranslationEngine(id) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}/validate`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(await readError(res, "检测翻译引擎失败"));
    return res.json();
  },

  async deleteTranslationEngine(id) {
    const res = await request(this.base, `/api/settings/translation-engines/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await readError(res, "删除翻译引擎失败"));
  },

  async deleteTask(id) {
    const res = await request(this.base, `/api/tasks/${id}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await readError(res, "删除失败"));
  },

  async retryTask(id) {
    const res = await request(this.base, `/api/tasks/${id}/retry`, { method: "POST" });
    if (!res.ok) throw new Error(await readError(res, "重试失败"));
    return res.json();
  },

  // 请求后端打开任务所在的本地文件夹。
  async openFolder(id) {
    const res = await request(this.base, `/api/tasks/${id}/folder`, { method: "POST" });
    if (!res.ok) throw new Error(await readError(res, "打开文件夹失败"));
  },

  // ---------- 本地资源治理 ----------
  async getStorageStats() {
    const res = await request(this.base, "/api/storage/stats");
    if (!res.ok) throw new Error("获取存储统计失败：" + res.status);
    return res.json();
  },

  async previewCleanup(payload) {
    const res = await request(this.base, "/api/storage/cleanup_preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    if (!res.ok) throw new Error(await readError(res, "预览清理失败"));
    return res.json();
  },

  async runCleanup(payload) {
    const res = await request(this.base, "/api/storage/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    if (!res.ok) throw new Error(await readError(res, "执行清理失败"));
    return res.json();
  },

  async getRetention() {
    const res = await request(this.base, "/api/storage/retention");
    if (!res.ok) throw new Error(await readError(res, "获取保留策略失败"));
    return res.json();
  },

  async putRetention(days) {
    const res = await request(this.base, "/api/storage/retention", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days }),
    });
    if (!res.ok) throw new Error(await readError(res, "保存保留策略失败"));
    return res.json();
  },

  // 拉取任务 current subtitles（original + translated），解析为前端可编辑结构
  async getSubtitles(id) {
    const res = await request(this.base, `/api/tasks/${id}/subtitles`);
    if (!res.ok) throw new Error(await readError(res, "读取字幕失败"));
    return res.json();
  },

  // 保存编辑后的字幕到后端；version 为可选版本号（例 "v2"）
  async saveSubtitles(id, payload) {
    const res = await request(this.base, `/api/tasks/${id}/subtitles`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "保存字幕失败"));
    return res.json();
  },

  // 基于当前 translated.srt 重新烧录成品；mode 可选覆盖任务设置
  async reburnSubtitles(id, payload = {}) {
    const res = await request(this.base, `/api/tasks/${id}/subtitles/burn`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await readError(res, "重新烧录失败"));
    return res.json();
  },

  // SSE 订阅单任务进度，返回取消函数
  subscribeProgress(id, onUpdate) {
    const es = new EventSource(`${this.base}/api/tasks/${id}/stream`);
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        onUpdate(data);
        if (TERMINAL.has(data.status)) es.close();
      } catch (_) {}
    };
    es.onerror = () => es.close();
    return () => es.close();
  },

  downloadUrl(id, kind) {
    return kind === "subtitle"
      ? `${this.base}/api/tasks/${id}/subtitle`
      : kind === "source"
        ? `${this.base}/api/tasks/${id}/source`
      : `${this.base}/api/tasks/${id}/download`;
  },
};

const MockApi = (() => {
  const STORE_KEY = "subtrans_mock_tasks_v1";
  // 下载测试历史：与真实后端的 probe_records 行为对齐
  const PROBE_STORE_KEY = "subtrans_mock_probe_records_v1";

  // 加载 / 持久化历史记录的辅助
  function _loadProbe() {
    try {
      const raw = localStorage.getItem(PROBE_STORE_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (e) {
      return [];
    }
  }
  function _saveProbe(arr) {
    try { localStorage.setItem(PROBE_STORE_KEY, JSON.stringify(arr)); } catch (e) {}
  }
  function _pushProbe(rec) {
    const arr = _loadProbe();
    arr.unshift(rec);
    // 简单上限：保留最近 100 条，避免 localStorage 膨胀
    if (arr.length > 100) arr.length = 100;
    _saveProbe(arr);
  }

  // 按 taskId 缓存 in-memory subtitles（编辑/保存即时反馈）
  const _subtitles = {};

  function _seedSubs(t) {
    const make = (origs, trans) => ({
      hasOriginal: !!origs, hasTranslated: !!trans,
      original: origs || [],
      translated: trans || [],
    });
    if (t.status !== "SUCCESS") return make(null, null);
    const orig = [
      { id: "sub_demo1", index: 1, start: 0, end: 2.4, text: "Welcome to the demo." },
      { id: "sub_demo2", index: 2, start: 2.4, end: 5.0, text: "Today we'll learn about subtitle editing." },
      { id: "sub_demo3", index: 3, start: 5.0, end: 7.8, text: "You can adjust timing, text, and split cues." },
    ];
    const tra = [
      { id: "sub_demo1", index: 1, start: 0, end: 2.4, text: "欢迎使用示例。" },
      { id: "sub_demo2", index: 2, start: 2.4, end: 5.0, text: "今天我们来看看字幕编辑。" },
      { id: "sub_demo3", index: 3, start: 5.0, end: 7.8, text: "你可以调整时间、文本，或者拆分合并。" },
    ];
    return make(orig, tra);
  }

  function seed() {
    const now = Date.now();
    return [
      {
        id: uid(), url: "https://example.com/watch?v=demo-finished", title: "示例视频 · 已完成",
        sourceLang: "en", targetLang: "zh-CN", mode: "bilingual", burn: "hard", model: "small",
        engine: "deepseek", status: "SUCCESS", progress: 100, error: null,
        createdAt: now - 1000 * 60 * 42, outputs: { video: "#", subtitle: "#" }, _sim: false,
      },
      {
        id: uid(), url: "https://example.com/watch?v=demo-running", title: null,
        sourceLang: "auto", targetLang: "zh-CN", mode: "mono", burn: "hard", model: "small",
        engine: "deepseek", status: "TRANSCRIBING", progress: 48, error: null,
        createdAt: now - 1000 * 90, outputs: null, _sim: true,
      },
      {
        id: uid(), url: "https://example.com/watch?v=demo-failed", title: null,
        sourceLang: "auto", targetLang: "ja", mode: "mono", burn: "soft", model: "medium",
        engine: "deepseek", status: "FAILED", progress: 22,
        error: "未设置 REPLICATE_API_TOKEN（请在 .env 中配置）",
        createdAt: now - 1000 * 60 * 8, outputs: null, _sim: false,
      },
    ];
  }

  let tasks;
  try {
    const raw = localStorage.getItem(STORE_KEY);
    tasks = raw ? JSON.parse(raw) : seed();
  } catch (e) {
    tasks = seed();
  }
  const persist = () => {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(tasks)); } catch (e) {}
  };
  const find = (id) => tasks.find((t) => t.id === id);
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));

  return {
    async createTask(payload) {
      const t = {
        id: uid(), url: payload.url, title: null,
        sourceLang: payload.sourceLang, targetLang: payload.targetLang,
        mode: payload.mode, burn: payload.burn, model: payload.model, engine: payload.engine,
        status: "PENDING", progress: 0, error: null, createdAt: Date.now(), outputs: null, _sim: true,
      };
      tasks.unshift(t); persist(); await delay(150); return { ...t };
    },
    // 示例模式下模拟本地视频上传任务。
    async createUploadTask(payload) {
      const t = {
        id: uid(), url: payload.file?.name || "uploaded-video", title: payload.file?.name || null,
        sourceLang: payload.sourceLang, targetLang: payload.targetLang,
        mode: payload.mode, burn: payload.burn, model: payload.model, engine: payload.engine,
        sourceType: "upload", needSubtitle: payload.needSubtitle,
        status: "PENDING", progress: 0, error: null, createdAt: Date.now(), outputs: null, _sim: true,
      };
      tasks.unshift(t); persist(); await delay(180); return { ...t };
    },
    // 示例模式下模拟链接探测成功，同时写入历史。
    async probeVideo(url) {
      await delay(180);
      const ok = /^https?:\/\/.+/i.test(url);
      const result = {
        ok,
        title: ok ? "示例视频 · " + shortUrl(url) : null,
        extractor: ok ? "Mock" : null,
        duration: ok ? 90 : null,
        formatsCount: ok ? 3 : 0,
        webpageUrl: url,
        reason: ok ? null : "请输入有效的视频链接",
        detail: null,
      };
      _pushProbe({
        id: "probe_" + Math.random().toString(16).slice(2, 10),
        url,
        ok,
        title: result.title,
        extractor: result.extractor,
        duration: result.duration,
        formatsCount: result.formatsCount,
        webpageUrl: result.webpageUrl,
        reason: result.reason,
        detail: result.detail,
        createdAt: Date.now(),
      });
      return result;
    },
    // 示例模式下：返回历史记录（按时间倒序，遵守 limit）。
    async listProbeRecords(limit = 50) {
      await delay(60);
      const arr = _loadProbe();
      const n = Math.max(1, Math.min(500, Number(limit) || 50));
      return arr.slice(0, n);
    },
    // 示例模式下：一键清空历史。
    async clearProbeRecords() {
      await delay(60);
      const arr = _loadProbe();
      _saveProbe([]);
      return { deleted: arr.length };
    },
    // 示例模式下：删除单条历史。
    async deleteProbeRecord(id) {
      await delay(40);
      const arr = _loadProbe().filter((r) => r.id !== id);
      _saveProbe(arr);
    },
    async listTasks() { await delay(300); return tasks.map((t) => ({ ...t })); },
    // 示例模式下返回常用源语言选项。
    async listVideoLanguages() { await delay(80); return ["en", "zh", "de", "es", "ru", "ko", "fr", "ja"]; },
    // 示例模式下返回目标语言选项。
    async listTargetLanguages() {
      await delay(80);
      return (
        CFG.TARGET_LANGUAGES ||
        Object.keys(LANG_LABEL).filter((k) => k !== "auto" && k !== "zh")
      );
    },
    // 示例模式下返回 Replicate Whisper 模型权重选项。
    async listModelWeights() { await delay(80); return ["tiny.en", "tiny", "base.en", "base", "small.en", "small", "medium.en", "medium", "large-v1", "large-v2"]; },
    async getReplicateBalance() {
      await delay(80);
      return {
        status: "unsupported", authenticated: false, account: null, balance: null,
        currency: "USD", balanceSupported: false, source: "mock",
        billingUrl: "https://replicate.com/account/billing", checkedAt: Date.now(),
        errorCode: "mock_mode",
        message: "示例模式不会访问真实 Replicate 账户；切换真实 API 后可检测 Token 状态。",
      };
    },
    async listTranslationEngines() {
      try { return JSON.parse(localStorage.getItem("subtrans_mock_engines_v1") || "[]"); } catch (_) { return []; }
    },
    async createTranslationEngine(payload) {
      const list = await this.listTranslationEngines();
      const item = { ...payload, id: uid().replace("task_", "engine_"), hasApiKey: !!payload.apiKey, availability: payload.apiKey ? "AVAILABLE" : "UNCONFIGURED", lastCheckedAt: Date.now() };
      list.push(item); localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(list)); return item;
    },
    async updateTranslationEngine(id, payload) {
      const list = await this.listTranslationEngines();
      const old = list.find((e) => e.id === id) || {};
      const item = { ...old, ...payload, id, hasApiKey: !!(payload.apiKey || old.hasApiKey), availability: (payload.apiKey || old.hasApiKey) ? "AVAILABLE" : "UNCONFIGURED" };
      localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(list.map((e) => e.id === id ? item : e))); return item;
    },
    async validateTranslationEngine(id) { const list = await this.listTranslationEngines(); const item = list.find((e) => e.id === id); if (item) { item.availability = item.hasApiKey ? "AVAILABLE" : "UNCONFIGURED"; localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(list)); } return item || {}; },
    async deleteTranslationEngine(id) { const list = (await this.listTranslationEngines()).filter((e) => e.id !== id); localStorage.setItem("subtrans_mock_engines_v1", JSON.stringify(list)); },
    async deleteTask(id) { tasks = tasks.filter((t) => t.id !== id); persist(); await delay(80); },
    async retryTask(id) {
      const t = find(id);
      if (t) { t.status = "PENDING"; t.progress = 0; t.error = null; t._sim = true; persist(); }
      return { ...t };
    },
    // 示例模式下模拟打开任务文件夹。
    async openFolder() { await delay(80); },
    // 示例模式下返回空统计 / 空预览，方便 UI 演练。
    async getStorageStats() {
      await delay(60);
      return { totalBytes: 0, totalTasks: 0, runnableTaskCount: 0, byKind: {}, byTask: [] };
    },
    async previewCleanup() {
      await delay(60);
      return { matchedTasks: 0, matchedBytes: 0, skippedTasks: [], targets: [] };
    },
    async runCleanup() {
      await delay(60);
      return { deletedTasks: 0, deletedBytes: 0, skippedTasks: [], partial: [] };
    },
    async getRetention() {
      await delay(40);
      try { return JSON.parse(localStorage.getItem("subtrans_mock_retention") || "null") || { days: null, updatedAt: null }; }
      catch (e) { return { days: null, updatedAt: null }; }
    },
    async putRetention(days) {
      await delay(40);
      const out = { days, updatedAt: Date.now() };
      try { localStorage.setItem("subtrans_mock_retention", JSON.stringify(out)); } catch (e) {}
      return out;
    },

    // 示例模式下的字幕编辑：内存中维护一份，供前端 UI 调试
    async getSubtitles(id) {
      await delay(120);
      const t = find(id);
      if (!t) throw new Error("任务不存在");
      const seed = MockApi._subtitles[id] || MockApi._seedSubs(t);
      MockApi._subtitles[id] = seed;
      return { taskId: id, title: t.title, burn: t.burn || "hard", ...seed };
    },
    async saveSubtitles(id, payload) {
      await delay(160);
      const cur = MockApi._subtitles[id] || {};
      cur[payload.locale] = payload.entries;
      if (payload.version) cur[`__v__${payload.locale}__${payload.version}`] = payload.entries;
      MockApi._subtitles[id] = cur;
      return {
        ok: true,
        taskId: id,
        locale: payload.locale,
        path: payload.version ? `${payload.locale}.${payload.version}.srt` : `${payload.locale}.srt`,
        count: payload.entries.length,
      };
    },
    async reburnSubtitles(id /* , payload */) {
      await delay(900);
      return { ok: true, taskId: id, mode: "hard", outputPath: "output.mp4" };
    },
    subscribeProgress(id, onUpdate) {
      const t = find(id);
      if (!t || !t._sim) return () => {};
      let stopped = false;
      const tick = () => {
        if (stopped) return;
        t.progress = clamp(t.progress + 3 + Math.random() * 8, 0, 100);
        t.status = statusForProgress(t.progress);
        if (t.progress >= 100) {
          t.status = "SUCCESS"; t.title = "示例视频 · " + shortUrl(t.url);
          t.outputs = { video: "#", subtitle: "#" }; t._sim = false;
        }
        persist();
        onUpdate({ id: t.id, status: t.status, progress: Math.round(t.progress), title: t.title, outputs: t.outputs, error: t.error });
        if (!TERMINAL.has(t.status)) setTimeout(tick, 700 + Math.random() * 500);
      };
      setTimeout(tick, 500);
      return () => { stopped = true; };
    },
    downloadUrl() { return "#"; },
  };
})();

export const Api = USE_MOCK ? MockApi : RealApi;
