import { test } from "node:test";
import assert from "node:assert/strict";

// Setup global mock window and global mock fetch before importing api.js
globalThis.window = {
  APP_CONFIG: {
    USE_MOCK: false,
    API_BASE_URL: "http://localhost:8000",
    API_TIMEOUT_MS: 15000,
  }
};

let mockFetchHandler = null;
globalThis.fetch = async (url, options) => {
  if (mockFetchHandler) {
    return mockFetchHandler(url, options);
  }
  throw new Error("mockFetchHandler not set");
};

// Use dynamic import to prevent ESM hoisting from running the import before global setup
const { Api } = await import("../js/api.js");

test("Api.openFolder: JSON response with detail", async () => {
  mockFetchHandler = async (url, options) => {
    return {
      ok: false,
      status: 409,
      text: async () => JSON.stringify({ detail: "任务目录尚未生成" })
    };
  };

  await assert.rejects(
    async () => {
      await Api.openFolder("some-id");
    },
    (err) => {
      assert.match(err.message, /打开文件夹失败：任务目录尚未生成/);
      return true;
    }
  );
});

test("Api.openFolder: non-JSON response fallback to text content", async () => {
  mockFetchHandler = async (url, options) => {
    return {
      ok: false,
      status: 409,
      text: async () => "Internal Server Error"
    };
  };

  await assert.rejects(
    async () => {
      await Api.openFolder("some-id");
    },
    (err) => {
      assert.match(err.message, /打开文件夹失败：Internal Server Error/);
      return true;
    }
  );
});

test("Api.openFolder: empty text response fallback to status code", async () => {
  mockFetchHandler = async (url, options) => {
    return {
      ok: false,
      status: 409,
      text: async () => ""
    };
  };

  await assert.rejects(
    async () => {
      await Api.openFolder("some-id");
    },
    (err) => {
      assert.match(err.message, /打开文件夹失败：409/);
      return true;
    }
  );
});
