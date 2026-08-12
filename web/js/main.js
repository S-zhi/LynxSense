/* 入口：装配各模块并加载数据 */

import { initTheme } from "./theme.js";
import { initShell } from "./ui-shell.js";
import { initConsole } from "./ui-console.js";
import { initQueue } from "./ui-queue.js";
import { initPreview } from "./ui-preview.js";
import { initEditor } from "./ui-editor.js";
import { initProbe } from "./ui-probe.js";
import { loadTasks, stopAll } from "./store.js";

initTheme();
initShell();
initConsole();
initQueue();
initPreview();
initEditor();
initProbe();

loadTasks();

window.addEventListener("beforeunload", stopAll);
