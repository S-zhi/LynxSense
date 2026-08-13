// 前端运行时配置。后端就绪后，把 USE_MOCK 改为 false 即可对接真实接口。
window.APP_CONFIG = {
  // FastAPI 后端地址（REST + SSE 同源）
  API_BASE_URL: "http://localhost:8000",

  // true  = 纯前端 mock，自动模拟整条流水线进度，无需后端
  // false = 走真实 REST + SSE 接口
  USE_MOCK: false,

  // 请求超时（毫秒）
  API_TIMEOUT_MS: 15000,

  // 当前仅支持 DeepSeek 翻译，后续可在此追加引擎
  TRANSLATION_ENGINES: [{ value: "deepseek", label: "DeepSeek", enabled: true }],

  // 默认支持的目标语言列表
  TARGET_LANGUAGES: ["zh-CN", "zh-TW", "en", "ja", "ko"],
};

// 运行时覆盖 API 地址：localStorage.setItem('SUBTRANS_API_BASE_URL', 'http://...')
const _override = (() => {
  try {
    return localStorage.getItem("SUBTRANS_API_BASE_URL");
  } catch (e) {
    return null;
  }
})();
if (_override) window.APP_CONFIG.API_BASE_URL = _override;
