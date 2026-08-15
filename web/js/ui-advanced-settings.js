/* 高级设置的二级标签页。使用 data 映射，方便未来继续扩展更多设置页。 */

import { $, $$ } from "./utils.js";

function selectTab(tabName, tabs, panels) {
  tabs.forEach((tab) => {
    const active = tab.dataset.settingsTab === tabName;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  panels.forEach((panel) => {
    panel.hidden = panel.dataset.settingsPanel !== tabName;
  });
}

export function initAdvancedSettings() {
  const root = $("#advancedSettings");
  if (!root) return;
  const tabs = $$('[data-settings-tab]', root);
  const panels = $$('[data-settings-panel]', root);
  if (!tabs.length || !panels.length) return;

  const firstTab = tabs[0];
  const initial = firstTab.dataset.settingsTab;
  selectTab(initial, tabs, panels);

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(tab.dataset.settingsTab, tabs, panels));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const nextIndex = event.key === "Home"
        ? 0
        : event.key === "End"
          ? tabs.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      tabs[nextIndex].focus();
      selectTab(tabs[nextIndex].dataset.settingsTab, tabs, panels);
    });
  });
}

initAdvancedSettings();
