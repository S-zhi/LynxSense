import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = {
  APP_CONFIG: {
    USE_MOCK: false,
    API_BASE_URL: "http://localhost:8000",
    API_TIMEOUT_MS: 15000,
  },
  localStorage: {
    getItem() { return null; },
    setItem() {},
  },
};

const { statusView } = await import("../js/ui-replicate-billing.js");

test("replicate billing status values render explicit states", () => {
  assert.deepEqual(statusView("available"), ["available", "已获取"]);
  assert.deepEqual(statusView("unsupported"), ["unknown", "官方未提供余额"]);
  assert.deepEqual(statusView("unconfigured"), ["unconfigured", "未配置 Token"]);
  assert.deepEqual(statusView("error"), ["unavailable", "Token 失效"]);
  assert.deepEqual(statusView("unavailable"), ["unavailable", "查询失败"]);
  assert.deepEqual(statusView("other"), ["unknown", "待查询"]);
});
