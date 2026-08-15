import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = {
  APP_CONFIG: {
    USE_MOCK: false,
    API_BASE_URL: "http://localhost:8000",
    API_TIMEOUT_MS: 15000,
  },
};

const {
  VIDEO_MISSING_TITLE,
  checkVideoAvailability,
  isVideoKnownMissing,
} = await import("../js/ui-preview.js");

test("missing resource uses the explicit missing-video copy", () => {
  assert.equal(VIDEO_MISSING_TITLE, "视频找不到了");
  assert.equal(
    isVideoKnownMissing({ resourceStatus: "MISSING", needSubtitle: true }, "video"),
    true,
  );
});

test("source track is only known missing for download-only tasks", () => {
  assert.equal(
    isVideoKnownMissing({ resourceStatus: "MISSING", needSubtitle: true }, "source"),
    false,
  );
  assert.equal(
    isVideoKnownMissing({ resourceStatus: "MISSING", needSubtitle: false }, "source"),
    true,
  );
});

test("409 preflight is unavailable and never creates a player", async () => {
  let requestOptions;
  const available = await checkVideoAvailability("/api/tasks/missing/download", async (_, options) => {
    requestOptions = options;
    return { ok: false, status: 409 };
  });

  assert.equal(available, false);
  assert.equal(requestOptions.method, "HEAD");
  assert.equal(requestOptions.cache, "no-store");
});

test("successful preflight marks the video available", async () => {
  const available = await checkVideoAvailability("/api/tasks/ready/download", async () => ({
    ok: true,
    status: 204,
  }));
  assert.equal(available, true);
});

test("preflight timeout falls back to unavailable", async () => {
  const available = await checkVideoAvailability(
    "/api/tasks/stalled/download",
    (_, { signal }) => new Promise((_, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), {
        once: true,
      });
    }),
    1,
  );
  assert.equal(available, false);
});
