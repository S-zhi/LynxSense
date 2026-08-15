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

const { statusView } = await import("../js/ui-translation-settings.js");

test("translation engine status values render their explicit state", () => {
  assert.deepEqual(statusView("CHECKING"), ["checking", "检测中"]);
  assert.deepEqual(statusView("AVAILABLE"), ["available", "检测成功"]);
  assert.deepEqual(statusView("UNAVAILABLE"), ["unavailable", "检测失败"]);
  assert.deepEqual(statusView("UNKNOWN"), ["unknown", "待检测"]);
});
