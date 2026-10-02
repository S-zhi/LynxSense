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

test("Api.probeVideo sends only the URL by default", async () => {
  let requestBody;
  mockFetchHandler = async (url, options) => {
    requestBody = JSON.parse(options.body);
    return { ok: true, json: async () => ({ ok: true }) };
  };

  await Api.probeVideo("https://example.com/video");
  assert.deepEqual(requestBody, { url: "https://example.com/video" });
});

test("Api.probeVideo sends only a whitelisted browser cookie source when selected", async () => {
  let requestBody;
  mockFetchHandler = async (url, options) => {
    requestBody = JSON.parse(options.body);
    return { ok: true, json: async () => ({ ok: true }) };
  };

  await Api.probeVideo("https://example.com/video", {
    cookiesFromBrowser: "chrome",
    cookieFile: "/private/profile/cookies.txt",
    cookies: "secret",
  });
  assert.deepEqual(requestBody, {
    url: "https://example.com/video",
    cookiesFromBrowser: "chrome",
  });
});

test("Api.probeVideo drops an invalid browser cookie source", async () => {
  let requestBody;
  mockFetchHandler = async (url, options) => {
    requestBody = JSON.parse(options.body);
    return { ok: true, json: async () => ({ ok: true }) };
  };

  await Api.probeVideo("https://example.com/video", { cookiesFromBrowser: "profile/path" });
  assert.deepEqual(requestBody, { url: "https://example.com/video" });
});

test("Api.getProbeStartupStatus reads the shared startup batch endpoint", async () => {
  let requestedUrl = "";
  mockFetchHandler = async (url) => {
    requestedUrl = url;
    return { ok: true, json: async () => ({ state: "running", total: 10 }) };
  };

  const status = await Api.getProbeStartupStatus();
  assert.equal(requestedUrl, "http://localhost:8000/api/tasks/probe/startup-status");
  assert.equal(status.total, 10);
});

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

test("Api.deleteLocalModel sends DELETE to the model resource", async () => {
  let requestedUrl = "";
  let requestedMethod = "";
  mockFetchHandler = async (url, options) => {
    requestedUrl = url;
    requestedMethod = options.method;
    return { ok: true, json: async () => ({ name: "large-v3", status: "NOT_INSTALLED" }) };
  };

  const model = await Api.deleteLocalModel("large-v3");
  assert.equal(requestedUrl, "http://localhost:8000/api/srt/local-models/large-v3");
  assert.equal(requestedMethod, "DELETE");
  assert.equal(model.status, "NOT_INSTALLED");
});

test("Api.downloadLocalModel starts an official model download", async () => {
  let requestedUrl = "";
  let requestedMethod = "";
  mockFetchHandler = async (url, options) => {
    requestedUrl = url;
    requestedMethod = options.method;
    return { ok: true, json: async () => ({ name: "tiny.en", status: "DOWNLOADING" }) };
  };

  const model = await Api.downloadLocalModel("tiny.en");
  assert.equal(requestedUrl, "http://localhost:8000/api/srt/local-models/tiny.en/download");
  assert.equal(requestedMethod, "POST");
  assert.equal(model.status, "DOWNLOADING");
});

test("Api.importLocalModel uploads provided files without requesting a download", async () => {
  let requestedUrl = "";
  let requestedBody;
  mockFetchHandler = async (url, options) => {
    requestedUrl = url;
    requestedBody = options.body;
    assert.equal(options.method, "POST");
    return { ok: true, json: async () => ({ name: "external", status: "READY" }) };
  };

  const file = new File(["weights"], "pytorch_model.bin");
  const result = await Api.importLocalModel("external", "External", [file]);
  assert.equal(requestedUrl, "http://localhost:8000/api/srt/local-models/import");
  assert.equal(requestedBody.get("name"), "external");
  assert.equal(requestedBody.get("files").name, "pytorch_model.bin");
  assert.equal(result.status, "READY");
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
