/* 通知调度逻辑的最小单测。
 *
 * 设计目标：
 *  - 覆盖核心状态机转移（PENDING->active、active->SUCCESS/FAILED）。
 *  - 覆盖节流：同任务在窗口内只发一次。
 *  - 覆盖偏好 / 权限 / 浏览器支持三类门控。
 *  - 覆盖"首次 SSE 消息不通知"语义。
 *
 * 用 Node 内置 test runner 运行（npm test 或 node --test web/__tests__/）。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { decideNotification, createNotifications } from "../js/notifications.js";

/* ---------- 纯函数：decideNotification ---------- */

test("decideNotification: PENDING -> active 触发 started", () => {
  const t = { id: "a", status: "DOWNLOADING", url: "https://x/v" };
  const d = decideNotification(t, "PENDING");
  assert.ok(d, "should return a decision");
  assert.equal(d.kind, "started");
  assert.equal(d.title, "任务已开始处理");
});

test("decideNotification: active -> SUCCESS 触发 success", () => {
  const t = { id: "a", status: "SUCCESS", url: "https://x/v" };
  const d = decideNotification(t, "BURNING");
  assert.equal(d.kind, "success");
  assert.equal(d.title, "任务已完成");
});

test("decideNotification: active -> FAILED 触发 failed 并附带错误", () => {
  const t = { id: "a", status: "FAILED", error: "ffmpeg crashed", url: "https://x/v" };
  const d = decideNotification(t, "TRANSLATING");
  assert.equal(d.kind, "failed");
  assert.equal(d.title, "任务处理失败");
  assert.match(d.body, /ffmpeg crashed/);
});

test("decideNotification: 同状态不通知", () => {
  assert.equal(decideNotification({ id: "a", status: "DOWNLOADING" }, "DOWNLOADING"), null);
  assert.equal(decideNotification({ id: "a", status: "SUCCESS" }, "SUCCESS"), null);
});

test("decideNotification: active -> active 不通知（只是进度前进）", () => {
  // 下载阶段切到提取阶段是常见情况；通知面板不该刷屏。
  assert.equal(
    decideNotification({ id: "a", status: "EXTRACTING" }, "DOWNLOADING"),
    null
  );
});

test("decideNotification: PENDING -> 终态（如重试后立刻失败）不算 started", () => {
  // 任务以 PENDING 进入立刻失败：没有"开始"这一通知，但属于异常路径；
  // 此处只验证不会错误地归类为 started。
  const d = decideNotification({ id: "a", status: "FAILED" }, "PENDING");
  assert.equal(d, null);
});

test("decideNotification: 无 task / 无 id / 无 status 时返回 null", () => {
  assert.equal(decideNotification(null, "PENDING"), null);
  assert.equal(decideNotification({ status: "SUCCESS" }, "PENDING"), null);
  assert.equal(decideNotification({ id: "a" }, "PENDING"), null);
});

/* ---------- 工厂：createNotifications 行为 ---------- */

/** 构造一个 mock localStorage（最小子集）。 */
function mockStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
    _peek: (k) => data.get(k),
  };
}

/** 构造一个 mock Notification：记录每次构造的参数。 */
function mockNotification(initialPermission = "granted") {
  let permission = initialPermission;
  const calls = [];
  function FakeNotification(title, opts) {
    calls.push({ title, opts });
    this.title = title;
    this.body = opts?.body;
    this.tag = opts?.tag;
    this._listeners = {};
  }
  FakeNotification.permission = permission;
  FakeNotification.requestPermission = async () => {
    permission = "granted";
    FakeNotification.permission = "granted";
    return "granted";
  };
  FakeNotification.prototype.addEventListener = function (ev, fn) {
    this._listeners[ev] = fn;
  };
  FakeNotification.prototype.close = function () {};
  return { Ctor: FakeNotification, calls, setPermission: (p) => { permission = p; FakeNotification.permission = p; } };
}

test("notify: 禁用时不发通知", () => {
  const store = mockStorage();
  const { Ctor, calls } = mockNotification();
  const api = createNotifications({
    Notification: Ctor,
    localStorage: store,
    window: { addEventListener: () => {} },
    now: () => 0,
  });
  // 默认禁用
  assert.equal(api.isEnabled(), false);
  const r = api.notify({ id: "t1", status: "SUCCESS" }, "BURNING", { firstUpdate: false });
  assert.equal(r.fired, false);
  assert.equal(r.reason, "disabled");
  assert.equal(calls.length, 0);
});

test("notify: 启用 + 首次消息只标记不通知", () => {
  const store = mockStorage();
  const { Ctor, calls } = mockNotification();
  const api = createNotifications({
    Notification: Ctor,
    localStorage: store,
    window: { addEventListener: () => {} },
    now: () => 0,
  });
  // 模拟用户主动开启
  api.setEnabled(true);
  assert.equal(api.isEnabled(), true);
  calls.length = 0;

  const r = api.notify({ id: "t1", status: "DOWNLOADING" }, "PENDING");
  assert.equal(r.fired, false);
  assert.equal(r.reason, "first-update");
  assert.equal(calls.length, 0);
});

test("notify: PENDING -> active -> SUCCESS 走通 start/success 两条通知", () => {
  const store = mockStorage();
  const { Ctor, calls } = mockNotification();
  let t = 0;
  const api = createNotifications({
    Notification: Ctor,
    localStorage: store,
    window: { addEventListener: () => {} },
    now: () => t,
    throttleMs: 1000,
  });
  api.setEnabled(true);
  calls.length = 0;

  // 首次消息
  api.notify({ id: "t1", status: "DOWNLOADING" }, "PENDING");
  // PENDING -> DOWNLOADING：第二次同状态转移判定为 started
  t += 100;
  // 这里状态没变（SSE 反复发同样的状态也算首次）—— 我们手动模拟"真正开始"
  // 用 PENDING 之前的 prev 是 PENDING，再走一次 DOWNLOADING 不会发。
  // 改用 reset 后再来一次以模拟"另一条 PENDING 任务"。
  api.reset();
  t += 100;
  const r1 = api.notify({ id: "t2", status: "PENDING" }, "PENDING", { firstUpdate: true });
  // 首次消息 firstUpdate=true 也会被识别为 first-update
  assert.equal(r1.fired, false);

  // 现在让它真正进入 DOWNLOADING
  t += 100;
  const r2 = api.notify({ id: "t2", status: "DOWNLOADING" }, "PENDING");
  assert.equal(r2.fired, true);
  assert.equal(r2.kind, "started");

  // 进度推进，不发
  t += 200;
  const r3 = api.notify({ id: "t2", status: "EXTRACTING" }, "DOWNLOADING");
  assert.equal(r3.fired, false);

  // 进入终态 SUCCESS：发
  t += 200;
  const r4 = api.notify({ id: "t2", status: "SUCCESS" }, "EXTRACTING");
  assert.equal(r4.fired, true);
  assert.equal(r4.kind, "success");

  // 标题/body 检查
  assert.equal(calls.length, 2);
  assert.equal(calls[0].title, "任务已开始处理");
  assert.equal(calls[1].title, "任务已完成");
});

test("notify: 节流窗口内重复状态转移只发一次", () => {
  const store = mockStorage();
  const { Ctor, calls } = mockNotification();
  let t = 1000;
  const api = createNotifications({
    Notification: Ctor,
    localStorage: store,
    window: { addEventListener: () => {} },
    now: () => t,
    throttleMs: 60_000,
  });
  api.setEnabled(true);
  calls.length = 0;

  // 标记已知
  api.notify({ id: "t1", status: "PENDING" }, "PENDING", { firstUpdate: true });
  // PENDING -> active 发一条
  const r1 = api.notify({ id: "t1", status: "DOWNLOADING" }, "PENDING");
  assert.equal(r1.fired, true);

  // 5 秒后再次 PENDING -> active（理论上不会发生，但即便发生也节流）
  t += 5000;
  const r2 = api.notify({ id: "t1", status: "DOWNLOADING" }, "PENDING");
  assert.equal(r2.fired, false);
  assert.equal(r2.reason, "throttled");

  // 跨过窗口后又能发
  t += 70_000;
  const r3 = api.notify({ id: "t1", status: "DOWNLOADING" }, "PENDING");
  assert.equal(r3.fired, true);

  assert.equal(calls.length, 2);
});

test("notify: 权限被拒时不发", () => {
  const store = mockStorage();
  const { Ctor, calls } = mockNotification("denied");
  const api = createNotifications({
    Notification: Ctor,
    localStorage: store,
    window: { addEventListener: () => {} },
    now: () => 0,
  });
  api.setEnabled(true);
  calls.length = 0;

  api.notify({ id: "t1", status: "PENDING" }, "PENDING", { firstUpdate: true });
  const r = api.notify({ id: "t1", status: "DOWNLOADING" }, "PENDING");
  assert.equal(r.fired, false);
  assert.equal(r.reason, "denied");
  assert.equal(calls.length, 0);
});

test("notify: 浏览器不支持时不发", () => {
  const store = mockStorage();
  const api = createNotifications({
    Notification: undefined,
    localStorage: store,
    window: {},
    now: () => 0,
  });
  api.setEnabled(true);
  assert.equal(api.isSupported(), false);

  const r = api.notify({ id: "t1", status: "SUCCESS" }, "BURNING", { firstUpdate: false });
  assert.equal(r.fired, false);
  assert.equal(r.reason, "unsupported");
});

test("notify: forget 清理任务状态后能重新通知", () => {
  const store = mockStorage();
  const { Ctor, calls } = mockNotification();
  let t = 0;
  const api = createNotifications({
    Notification: Ctor,
    localStorage: store,
    window: { addEventListener: () => {} },
    now: () => t,
    throttleMs: 1000,
  });
  api.setEnabled(true);
  calls.length = 0;

  // 模拟：任务在重试后被 forget，重新进入队列。
  t += 1;
  api.notify({ id: "t1", status: "PENDING" }, "PENDING", { firstUpdate: true });
  t += 10;
  const r1 = api.notify({ id: "t1", status: "DOWNLOADING" }, "PENDING");
  assert.equal(r1.fired, true);

  // 用户重试 -> forget
  api.forget("t1");
  t += 10;
  // 又一次 PENDING 首条
  api.notify({ id: "t1", status: "PENDING" }, "PENDING", { firstUpdate: true });
  t += 10;
  const r2 = api.notify({ id: "t1", status: "DOWNLOADING" }, "PENDING");
  assert.equal(r2.fired, true);
  assert.equal(calls.length, 2);
});

test("notify: 不同 kind 在节流窗口内仍可触发（started -> success 不会丢）", () => {
  // 设计选择：节流只对相同 kind 生效，避免漏掉关键结果通知。
  const store = mockStorage();
  const { Ctor, calls } = mockNotification();
  let t = 0;
  const api = createNotifications({
    Notification: Ctor,
    localStorage: store,
    window: { addEventListener: () => {} },
    now: () => t,
    throttleMs: 60_000,
  });
  api.setEnabled(true);
  calls.length = 0;

  api.notify({ id: "t1", status: "PENDING" }, "PENDING", { firstUpdate: true });
  t += 100;
  api.notify({ id: "t1", status: "DOWNLOADING" }, "PENDING"); // started
  t += 200; // 仍在 60s 窗口内
  api.notify({ id: "t1", status: "SUCCESS" }, "BURNING"); // success
  assert.equal(calls.length, 2);
  assert.equal(calls[1].title, "任务已完成");
});

test("setEnabled 写入 localStorage 并请求权限", async () => {
  const store = mockStorage();
  let requested = 0;
  const Ctor = function () {};
  Ctor.permission = "default";
  Ctor.requestPermission = async () => { requested += 1; Ctor.permission = "granted"; return "granted"; };

  const api = createNotifications({
    Notification: Ctor,
    localStorage: store,
    window: {},
    now: () => 0,
  });

  // 关闭 -> 不请求权限
  await api.setEnabled(false);
  assert.equal(requested, 0);
  assert.equal(store._peek("subtrans_notifications_enabled"), "0");

  // 打开 -> 请求一次权限
  await api.setEnabled(true);
  assert.equal(requested, 1);
  assert.equal(store._peek("subtrans_notifications_enabled"), "1");

  // 已授权时再开不重复请求
  await api.setEnabled(false);
  await api.setEnabled(true);
  assert.equal(requested, 1);
});
