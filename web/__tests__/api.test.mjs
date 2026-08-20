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

test("Api.openFolder: structured detail includes actionable suggestion", async () => {
  mockFetchHandler = async () => ({
    ok: false,
    status: 413,
    text: async () => JSON.stringify({
      detail: {
        code: "UPLOAD_TOO_LARGE",
        message: "上传文件过大",
        suggestion: "请压缩或切分视频",
      },
    }),
  });

  await assert.rejects(
    () => Api.openFolder("some-id"),
    /打开文件夹失败：上传文件过大；请压缩或切分视频/,
  );
});

test("Api.cancelTask sends POST to /api/tasks/:id/cancel", async () => {
  let requestedUrl = "";
  let requestedMethod = "";
  mockFetchHandler = async (url, options) => {
    requestedUrl = url;
    requestedMethod = options.method;
    return {
      ok: true,
      json: async () => ({ id: "task_123", status: "CANCELLED", error: "用户取消" })
    };
  };

  const res = await Api.cancelTask("task_123");
  assert.equal(requestedUrl, "http://localhost:8000/api/tasks/task_123/cancel");
  assert.equal(requestedMethod, "POST");
  assert.equal(res.status, "CANCELLED");
});

test("Api.subscribeProgress: handles message, end, timeout, and reconnecting", async () => {
  const instances = [];
  class MockEventSource {
    constructor(url) {
      this.url = url;
      this.listeners = {};
      this.readyState = 0;
      instances.push(this);
    }
    addEventListener(event, fn) {
      this.listeners[event] = fn;
    }
    removeEventListener() {}
    close() {
      this.readyState = 2;
    }
    emit(event, data) {
      if (event === "message" && this.onmessage) {
        this.onmessage({ data: JSON.stringify(data) });
      } else if (this.listeners[event]) {
        this.listeners[event]({ data: JSON.stringify(data) });
      }
    }
    emitError() {
      if (this.onerror) this.onerror(new Event("error"));
    }
  }

  globalThis.EventSource = MockEventSource;

  const updates = [];
  const unsub = Api.subscribeProgress("task_test1", (data) => {
    updates.push(data);
  });

  assert.equal(instances.length, 1);
  const es1 = instances[0];

  // 1) 收到普通 progress 消息
  es1.emit("message", { id: "task_test1", status: "TRANSCRIBING", progress: 40 });
  assert.equal(updates.length, 1);
  assert.equal(updates[0]._streamStatus, "connected");
  assert.equal(updates[0].progress, 40);

  // 2) 连接异常 -> 触发 reconnecting 状态
  es1.emitError();
  assert.equal(updates.length, 2);
  assert.equal(updates[1]._streamStatus, "reconnecting");
  assert.equal(updates[1]._retryCount, 1);

  // 3) end 事件处理
  es1.emit("end", { id: "task_test1", status: "SUCCESS", progress: 100 });
  assert.equal(updates.length, 3);
  assert.equal(updates[2].status, "SUCCESS");

  // 4) timeout 事件处理
  es1.emit("timeout", { error: "stream timeout" });
  assert.equal(updates.length, 4);
  assert.equal(updates[3]._streamStatus, "timeout");

  unsub();
  delete globalThis.EventSource;
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
