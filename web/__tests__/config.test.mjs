import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { test } from "node:test";
import assert from "node:assert/strict";

const configSource = readFileSync(new URL("../config.js", import.meta.url), "utf8");

function loadConfig(pageUrl, override = null) {
  const location = new URL(pageUrl);
  const localStorage = {
    getItem(key) {
      return key === "SUBTRANS_API_BASE_URL" ? override : null;
    },
  };
  const window = { location, localStorage };

  runInNewContext(configSource, { window, localStorage });
  return window.APP_CONFIG;
}

test("cloud deployment uses the page origin as its API base URL", () => {
  const config = loadConfig("https://subtitles.example.com/settings");
  assert.equal(config.API_BASE_URL, "https://subtitles.example.com");
});

test("local frontend development keeps using the default FastAPI port", () => {
  const config = loadConfig("http://localhost:5273/");
  assert.equal(config.API_BASE_URL, "http://localhost:8000");
});

test("localStorage can still override the inferred API base URL", () => {
  const config = loadConfig(
    "https://subtitles.example.com/",
    "https://api.example.com",
  );
  assert.equal(config.API_BASE_URL, "https://api.example.com");
});
