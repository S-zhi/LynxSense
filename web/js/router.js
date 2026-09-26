/* 浏览器路由：让每个工作区页面拥有稳定、可分享的 URL。 */

import { state, subscribe, setView } from "./store.js";

export const ROUTES = Object.freeze({
  tasks: { path: "/tasks", title: "任务" },
  preview: { path: "/preview", title: "视频预览" },
  editor: { path: "/editor", title: "字幕编辑" },
  probe: { path: "/probe", title: "下载测试" },
  storage: { path: "/storage", title: "本地资源" },
  drive: { path: "/drive", title: "Google Drive" },
  "translation-settings": { path: "/settings", title: "高级设置" },
});

const VIEW_BY_PATH = Object.freeze(
  Object.fromEntries(Object.entries(ROUTES).map(([view, route]) => [route.path, view])),
);

export function viewFromPath(pathname = window.location.pathname) {
  return VIEW_BY_PATH[pathname.replace(/\/$/, "") || "/tasks"] || "tasks";
}

export function pathForView(view) {
  return ROUTES[view]?.path || ROUTES.tasks.path;
}

function updateDocument(view) {
  const route = ROUTES[view] || ROUTES.tasks;
  document.title = `${route.title} · LynxSense`;
  document.body.dataset.route = view;
}

export function initRouter() {
  const initialView = viewFromPath();
  if (window.location.pathname !== pathForView(initialView)) {
    window.history.replaceState({ view: initialView }, "", pathForView(initialView));
  }
  setView(initialView, { history: false });
  updateDocument(initialView);

  window.addEventListener("popstate", () => {
    setView(viewFromPath(), { history: false });
  });

  subscribe(({ type }) => {
    if (type !== "view") return;
    const view = state.view;
    const path = pathForView(view);
    updateDocument(view);
    if (window.location.pathname !== path) {
      window.history.pushState({ view }, "", path);
    }
  });
}
