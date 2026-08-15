/* 视频审片室：剧院感舞台 + 已完成清单。仅在选中变化时重建播放器，避免打断播放。 */

import { LANG_LABEL } from "./constants.js";
import { $, el, escapeHtml } from "./utils.js";
import { state, subscribe, completedTasks, setPreviewId } from "./store.js";
import { Api, USE_MOCK } from "./api.js";
import { toast } from "./toast.js";

let stageEl, listEl;
let lastStageId = undefined;

export function initPreview() {
  stageEl = $("#previewStage");
  listEl = $("#previewList");
  subscribe(renderPreview);
}

function renderPreview() {
  if (state.view !== "preview") return;
  const completed = completedTasks();
  renderList(completed);

  const sel = completed.find((t) => t.id === state.previewId) || completed[0] || null;
  const selId = sel ? sel.id : null;
  if (selId !== lastStageId) {
    renderStage(sel);
    lastStageId = selId;
  }
}

function renderList(completed) {
  listEl.replaceChildren();
  if (completed.length === 0) {
    const e = el("div", "state");
    e.innerHTML = `
      <div class="state__icon"><i class="ph ph-film-slate" aria-hidden="true"></i></div>
      <div class="state__title">还没有成品</div>
      <div class="state__desc">完成一个任务后会出现在这里。</div>`;
    listEl.append(e);
    return;
  }
  const selId = state.previewId || (completed[0] && completed[0].id);
  completed.forEach((t) => {
    const item = el("button", "pvitem" + (t.id === selId ? " is-active" : ""));
    item.type = "button";
    item.dataset.id = t.id;
    item.innerHTML = `
      <span class="pvitem__thumb" aria-hidden="true"><i class="ph ph-film-strip"></i></span>
      <span class="pvitem__body">
        <span class="pvitem__title">${escapeHtml(t.title || "成品视频")}</span>
        <span class="pvitem__meta">${LANG_LABEL[t.targetLang] || t.targetLang} · ${
      t.burn === "hard" ? "硬字幕" : "软字幕"
    }</span>
      </span>`;
    item.addEventListener("click", () => setPreviewId(t.id));
    listEl.append(item);
  });
}

function renderStage(sel) {
  stageEl.replaceChildren();

  if (!sel) {
    stageEl.append(stageEmpty("ph-monitor-play", "选择一个成品", "从右侧已完成列表挑一个开始播放。"));
    return;
  }
  if (USE_MOCK) {
    stageEl.append(stageEmpty("ph-flask", "示例模式", "当前为前端示例数据，没有真实视频可播放。"));
    return;
  }

  const stage = el("div", "stage");

  const screen = el("div", "stage__screen");
  const video = el("video");
  video.controls = true;
  video.preload = "metadata";
  video.addEventListener("error", () => {
    // 成品文件被清理后，下载接口会返回 409。此时不能保留浏览器的原生空播放器，
    // 否则用户会误以为视频仍可播放。
    screen.classList.add("is-unavailable");
    screen.replaceChildren(stageVideoMissing());
    dlVideo.className = "btn btn--ghost btn--sm";
    dlVideo.disabled = true;
    dlVideo.innerHTML = `<i class="ph ph-video-camera-slash"></i><span>视频不可用</span>`;
  }, { once: true });
  screen.append(video);

  const bar = el("div", "stage__bar");
  const title = el("div", "stage__title");
  title.textContent = sel.title || "成品视频";
  const tags = el("div", "stage__tags");
  tags.append(
    stageTag(`${LANG_LABEL[sel.sourceLang] || sel.sourceLang} → ${LANG_LABEL[sel.targetLang] || sel.targetLang}`),
    stageTag(sel.mode === "bilingual" ? "双语" : "单语"),
    stageTag(sel.burn === "hard" ? "硬字幕" : "软字幕")
  );
  const actions = el("div", "stage__actions");
  const folder = el("button", "btn btn--ghost btn--sm");
  folder.innerHTML = `<i class="ph ph-folder-open"></i><span>打开文件夹</span>`;
  folder.addEventListener("click", () => openFolder(sel));
  const dlVideo = el("button", "btn btn--primary btn--sm");
  dlVideo.innerHTML = `<i class="ph ph-download-simple"></i><span>下载视频</span>`;
  dlVideo.addEventListener("click", () => open(sel, "video"));
  const dlSub = el("button", "btn btn--ghost btn--sm");
  dlSub.innerHTML = `<i class="ph ph-closed-captioning"></i><span>下载字幕</span>`;
  dlSub.addEventListener("click", () => open(sel, "subtitle"));
  actions.append(folder, dlVideo, dlSub);

  bar.append(title, tags, actions);
  stage.append(screen, bar);
  stageEl.append(stage);
  // 等舞台和操作区构建完成后再加载资源，确保 409 回退时可同步禁用下载操作。
  video.src = Api.downloadUrl(sel.id, "video");
}

function open(sel, kind) {
  if (USE_MOCK) {
    toast("示例模式：下载占位");
    return;
  }
  window.open(Api.downloadUrl(sel.id, kind), "_blank");
}

// 打开当前预览视频对应的本地产物文件夹。
async function openFolder(sel) {
  try {
    await Api.openFolder(sel.id);
    toast(USE_MOCK ? "示例模式：文件夹打开占位" : "已打开任务文件夹", "ph-folder-open");
  } catch (e) {
    toast(e.message || "打开文件夹失败", "ph-warning-circle");
  }
}

function stageTag(text) {
  const s = el("span", "stage__tag");
  s.textContent = text;
  return s;
}

function stageEmpty(icon, title, desc) {
  const e = el("div", "stage__empty");
  e.innerHTML = `
    <div class="state__icon"><i class="ph ${icon}" aria-hidden="true"></i></div>
    <div class="state__title">${title}</div>
    <div class="state__desc">${desc}</div>`;
  return e;
}

function stageVideoMissing() {
  const e = el("div", "stage__missing");
  e.innerHTML = `
    <span class="stage__missing-kicker">PREVIEW UNAVAILABLE</span>
    <div class="stage__missing-title">视频找不到了</div>
    <p class="stage__missing-desc">成品文件可能已被清理或移动。</p>
    <p class="stage__missing-hint">请返回任务页重新处理后，再回来预览。</p>`;
  return e;
}
