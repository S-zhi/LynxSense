/* Replicate 账户状态卡片：只展示脱敏账户信息和官方余额能力状态。 */

import { $, escapeHtml } from "./utils.js";
import { Api } from "./api.js";
import { state } from "./store.js";

export const STATUS_LABEL = {
  available: ["available", "已获取"],
  unsupported: ["unknown", "官方未提供余额"],
  unconfigured: ["unconfigured", "未配置 Token"],
  unavailable: ["unavailable", "查询失败"],
  error: ["unavailable", "Token 失效"],
};

export function statusView(status) {
  return STATUS_LABEL[status] || ["unknown", "待查询"];
}

function accountLabel(account) {
  if (!account) return "未识别账户";
  return account.username || account.name || "已验证账户";
}

function formatBalance(data) {
  if (!data.balanceSupported || typeof data.balance !== "number") {
    return "—";
  }
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency", currency: data.currency || "USD",
    }).format(data.balance);
  } catch (_) {
    return `${data.currency || "USD"} ${data.balance.toFixed(2)}`;
  }
}

function render(data) {
  const body = $("#replicateBalanceBody");
  if (!body) return;
  const status = statusView(data.status);
  const account = accountLabel(data.account);
  const billingUrl = data.billingUrl || "https://replicate.com/account/billing";
  body.innerHTML = `
    <div class="replicate-billing__metric">
      <span class="replicate-billing__label">当前余额</span>
      <strong>${escapeHtml(formatBalance(data))}</strong>
    </div>
    <div class="replicate-billing__detail">
      <span class="engine-status engine-status--${status[0]}">${status[1]}</span>
      <span class="replicate-billing__account">${escapeHtml(account)}</span>
      <span class="replicate-billing__message">${escapeHtml(data.message || "")}</span>
      <a href="${escapeHtml(billingUrl)}" target="_blank" rel="noreferrer">打开 Replicate 账单页 <i class="ph ph-arrow-up-right"></i></a>
    </div>`;
}

async function refresh(button) {
  if (button) button.disabled = true;
  const body = $("#replicateBalanceBody");
  if (body) body.innerHTML = `<span class="engine-status engine-status--checking">正在查询 Replicate…</span>`;
  try {
    render(await Api.getReplicateBalance());
  } catch (error) {
    render({
      status: "unavailable", balance: null, balanceSupported: false,
      account: null, message: error?.message || "查询失败，请稍后重试",
      billingUrl: "https://replicate.com/account/billing",
    });
  } finally {
    if (button) button.disabled = false;
  }
}

export function initReplicateBilling() {
  if (typeof document === "undefined") return;
  const root = $("#replicateBilling");
  const button = $("#replicateBalanceRefresh");
  if (!root || !button) return;
  button.addEventListener("click", () => refresh(button));
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view === "translation-settings") refresh(button);
  });
  if (state.view === "translation-settings") refresh(button);
}

if (typeof document !== "undefined") {
  initReplicateBilling();
}
