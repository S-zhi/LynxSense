import { test } from "node:test";
import assert from "node:assert/strict";
import { LANG_LABEL } from "../js/constants.js";

globalThis.window = {
  APP_CONFIG: {
    USE_MOCK: true,
    API_BASE_URL: "http://localhost:8000",
    API_TIMEOUT_MS: 15000,
  },
};

const { Api } = await import("../js/api.js");

test("LANG_LABEL contains 40 target languages (excluding auto and zh)", () => {
  const targetLangs = Object.keys(LANG_LABEL).filter(
    (k) => k !== "auto" && k !== "zh"
  );
  assert.equal(targetLangs.length, 40);
  assert.ok(targetLangs.includes("zh-CN"));
  assert.ok(targetLangs.includes("es"));
  assert.ok(targetLangs.includes("fr"));
  assert.ok(targetLangs.includes("de"));
  assert.ok(targetLangs.includes("sw"));
});

test("MockApi.listTargetLanguages returns all 40 target languages", async () => {
  const langs = await Api.listTargetLanguages();
  assert.equal(langs.length, 40);
  assert.ok(langs.includes("zh-CN"));
  assert.ok(langs.includes("es"));
  assert.ok(langs.includes("fr"));
  assert.ok(langs.includes("de"));
  assert.ok(langs.includes("sw"));
});
