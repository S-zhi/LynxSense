import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_SITES,
  SITE_CATEGORIES,
  categoryLabel,
} from "../js/developer-sites-data.js";
import {
  deriveSiteStats,
  isHttpUrl,
  normalizeProbeResult,
  siteMatchesQuery,
} from "../js/developer-sites.js";

test("developer site registry has unique ids and complete probe metadata", () => {
  const ids = DEFAULT_SITES.map((site) => site.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const site of DEFAULT_SITES) {
    assert.ok(site.name);
    assert.ok(site.domain);
    assert.ok(site.category);
    assert.ok(site.extractor);
    assert.ok(site.url);
    assert.ok(isHttpUrl(site.url));
    assert.ok(SITE_CATEGORIES.some((category) => category.id === site.category));
  }
});

test("site filtering matches display metadata and ignores empty queries", () => {
  const site = DEFAULT_SITES.find((item) => item.id === "bilibili");
  assert.ok(site);
  assert.equal(siteMatchesQuery(site, ""), true);
  assert.equal(siteMatchesQuery(site, "哔哩"), true);
  assert.equal(siteMatchesQuery(site, "BILIBILI.COM"), true);
  assert.equal(siteMatchesQuery(site, "youtube"), false);
  assert.equal(categoryLabel(site.category), "国内主流");
});

test("site stats distinguish idle, testing, successful, and failed probes", () => {
  assert.deepEqual(
    deriveSiteStats([
      { status: "idle" },
      { status: "testing" },
      { status: "ok" },
      { status: "fail" },
      { status: "ok" },
    ]),
    { total: 5, ok: 2, fail: 1, idle: 1, testing: 1 },
  );
});

test("probe results are normalized to the frontend contract", () => {
  assert.deepEqual(
    normalizeProbeResult({
      ok: true,
      title: "Sample",
      duration: "12.5",
      formats: [{ format_id: "18" }],
      availableQualities: ["720p", 1080, null],
      cached: 1,
    }),
    {
      ok: true,
      title: "Sample",
      extractor: null,
      duration: 12.5,
      formatsCount: 1,
      webpageUrl: null,
      reason: null,
      detail: null,
      cached: true,
      language: null,
      availableQualities: ["720p", "1080"],
      formats: [{ format_id: "18" }],
      thumbnail: null,
      uploader: null,
    },
  );

  const failed = normalizeProbeResult({ ok: false, detail: "private video" });
  assert.equal(failed.reason, "yt-dlp 无法解析这个链接");
  assert.equal(failed.detail, "private video");
  assert.equal(failed.formatsCount, 0);
});

test("HTTP URL validation rejects unsupported and malformed values", () => {
  assert.equal(isHttpUrl("https://example.com/video"), true);
  assert.equal(isHttpUrl("http://localhost:8000/video"), true);
  assert.equal(isHttpUrl("ftp://example.com/video"), false);
  assert.equal(isHttpUrl("not a URL"), false);
  assert.equal(isHttpUrl(""), false);
});
