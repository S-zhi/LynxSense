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

const { collectDroppedFiles, createFolderManifest, mergeFolderEntries, normalizeRelativePath, runWithConcurrency, summarizeFolderProgress } = await import("../js/ui-drive.js");
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

function droppedFileEntry(name, size = 1) {
  const file = { name, size, type: "text/plain", lastModified: 1, slice: () => ({}) };
  return { name, isFile: true, isDirectory: false, file: (resolve) => resolve(file) };
}

function droppedDirectoryEntry(name, batches) {
  return {
    name,
    isFile: false,
    isDirectory: true,
    createReader: () => ({
      readEntries(resolve) {
        resolve(batches.shift() || []);
      },
    }),
  };
}

test("collectDroppedFiles recursively expands a dragged folder with relative paths", async () => {
  const nested = droppedDirectoryEntry("Season 1", [[droppedFileEntry("episode.txt", 2)], []]);
  const root = droppedDirectoryEntry("Show", [[droppedFileEntry("cover.txt", 1), nested], []]);
  const result = await collectDroppedFiles({
    items: [{ kind: "file", webkitGetAsEntry: () => root }],
    files: [{ name: "Show", size: 0 }],
  });

  assert.equal(result.hasDirectory, true);
  assert.deepEqual(createFolderManifest(result.files), [
    { relativePath: "Show/cover.txt", name: "cover.txt", size: 1, mime: "text/plain" },
    { relativePath: "Show/Season 1/episode.txt", name: "episode.txt", size: 2, mime: "text/plain" },
  ]);
});

test("collectDroppedFiles reads every directory entry batch", async () => {
  const root = droppedDirectoryEntry("Batch", [
    [droppedFileEntry("one.txt")],
    [droppedFileEntry("two.txt")],
    [],
  ]);
  const result = await collectDroppedFiles({ items: [{ kind: "file", webkitGetAsEntry: () => root }] });
  assert.deepEqual(result.files.map((file) => file.webkitRelativePath), ["Batch/one.txt", "Batch/two.txt"]);
});

test("mergeFolderEntries matches remote entries by path when the response order changes", () => {
  const files = [
    { name: "z.png", size: 102126, webkitRelativePath: "AI学习/z.png" },
    { name: "a.png", size: 33690, webkitRelativePath: "AI学习/a.png" },
  ];
  const manifest = createFolderManifest(files);
  const entries = mergeFolderEntries(manifest, files, [
    { id: "entry-a", relativePath: "AI学习/a.png", size: 33690, state: "PENDING" },
    { id: "entry-z", relativePath: "AI学习/z.png", size: 102126, state: "PENDING" },
  ]);

  assert.equal(entries[0].id, "entry-z");
  assert.equal(entries[0].file, files[0]);
  assert.equal(entries[1].id, "entry-a");
  assert.equal(entries[1].file, files[1]);
});

test("mergeFolderEntries rejects a response that is missing a manifest path", () => {
  const files = [{ name: "a.png", size: 1, webkitRelativePath: "Root/a.png" }];
  assert.throws(
    () => mergeFolderEntries(createFolderManifest(files), files, [{ id: "wrong", relativePath: "Root/b.png" }]),
    /sidecar 未返回文件夹条目：Root\/a\.png/,
  );
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

test("DriveApi encodes Unicode filenames into ASCII-safe headers", async () => {
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url: String(url), options });
    return { ok: true, status: 201, text: async () => JSON.stringify({ id: "upload-1" }) };
  };
  const file = { name: "AI学习 + 100%.mp4", type: "video/mp4", size: 12 };
  await DriveApi.createUpload(file);
  await DriveApi.createFolderEntryUpload("batch-1", "entry-1", file);

  for (const { options } of requests) {
    assert.equal(options.headers["X-File-Name"], undefined);
    assert.equal(decodeURIComponent(options.headers["X-File-Name-Encoded"]), file.name);
    assert.match(options.headers["X-File-Name-Encoded"], /^[\x00-\x7F]+$/);
  }
});
