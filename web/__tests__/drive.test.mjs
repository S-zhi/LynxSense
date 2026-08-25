import assert from "node:assert/strict";
import test from "node:test";

globalThis.window = {
  APP_CONFIG: {
    DRIVE_API_BASE_URL: "http://drive.test",
    DRIVE_API_TIMEOUT_MS: 5000,
    API_BASE_URL: "http://api.test",
    USE_MOCK: true,
  },
};

const { createFolderManifest, normalizeRelativePath, runWithConcurrency, summarizeFolderProgress } = await import("../js/ui-drive.js");
const { DriveApi } = await import("../js/drive-api.js");

test("normalizeRelativePath removes traversal and keeps nested folder names", () => {
  assert.equal(normalizeRelativePath("../Season 1\\clip:01.mp4"), "Season 1/clip_01.mp4");
  assert.equal(normalizeRelativePath("", "fallback.txt"), "fallback.txt");
});

test("createFolderManifest uses webkitRelativePath and preserves file metadata", () => {
  const file = { name: "clip.mp4", type: "video/mp4", size: 12, webkitRelativePath: "Demo/clip.mp4" };
  assert.deepEqual(createFolderManifest([file]), [{
    relativePath: "Demo/clip.mp4",
    name: "clip.mp4",
    size: 12,
    mime: "video/mp4",
  }]);
});

test("summarizeFolderProgress calculates total bytes and failed entries", () => {
  const result = summarizeFolderProgress({ entries: [
    { size: 100, offset: 100, state: "SUCCESS" },
    { size: 300, offset: 75, state: "FAILED" },
  ] });
  assert.deepEqual(result, {
    totalEntries: 2,
    completedEntries: 1,
    failedEntries: 1,
    totalBytes: 400,
    completedBytes: 175,
    percent: 44,
  });
});

test("runWithConcurrency never runs more than the requested workers", async () => {
  let active = 0;
  let peak = 0;
  const result = await runWithConcurrency([1, 2, 3, 4, 5], 2, async (value) => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, 2));
    active -= 1;
    return value * 2;
  });
  assert.equal(peak, 2);
  assert.deepEqual(result, [2, 4, 6, 8, 10]);
});

test("DriveApi.listFiles sends parentId and page token", async () => {
  const requests = [];
  globalThis.fetch = async (url) => {
    requests.push(String(url));
    return { ok: true, status: 200, text: async () => JSON.stringify({ files: [], nextPageToken: "next" }) };
  };
  const response = await DriveApi.listFiles("folder-1", "page-2", 25);
  assert.equal(response.nextPageToken, "next");
  assert.equal(requests[0], "http://drive.test/api/drive/files?pageSize=25&parentId=folder-1&pageToken=page-2");
});

test("DriveApi folder batch methods use the documented routes", async () => {
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url: String(url), options });
    return { ok: true, status: 200, text: async () => JSON.stringify({ batch: { id: "batch-1" }, entries: [] }) };
  };
  await DriveApi.createFolderUpload([{ relativePath: "Root/a.txt", name: "a.txt", size: 1, mime: "text/plain" }], undefined, "request-1", "parent-1");
  await DriveApi.folderEntryAction("batch-1", "entry-1", "retry");
  assert.equal(requests[0].url, "http://drive.test/api/drive/folder-uploads");
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    entries: [{ relativePath: "Root/a.txt", name: "a.txt", size: 1, mime: "text/plain" }],
    clientRequestId: "request-1",
    parentId: "parent-1",
  });
  assert.equal(requests[1].url, "http://drive.test/api/drive/folder-uploads/batch-1/entries/entry-1/retry");
});
