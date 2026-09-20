/**
 * Role: Mounts the Mistral Vibe CLI block modal frontend.
 * File Name: block_modal.js
 * Author: Alexandre EL
 * Email: alex@hackinvent.com
 * Created Date: 2026-05-19
 */
const previous = registry.mistral_vibe_cli || {};

/**
 * Return Mistral Vibe modal tabs in DOM order.
 *
 * @param {HTMLElement} root - Mounted Mistral Vibe modal root.
 * @returns {HTMLElement[]} Tab buttons controlled by this asset.
 */
function tabElements(root) {
  return Array.from(root.querySelectorAll("[data-mistral-vibe-modal-tab]"));
}

/**
 * Return Mistral Vibe modal panels in DOM order.
 *
 * @param {HTMLElement} root - Mounted Mistral Vibe modal root.
 * @returns {HTMLElement[]} Panels associated with modal tabs.
 */
function panelElements(root) {
  return Array.from(root.querySelectorAll("[data-mistral-vibe-modal-panel]"));
}

/**
 * Activate one Mistral Vibe modal tab and hide inactive panels.
 *
 * @param {HTMLElement} root - Mounted Mistral Vibe modal root.
 * @param {HTMLElement} tab - Tab element to activate.
 * @param {object} options - Activation options.
 * @param {boolean} options.focus - Whether keyboard focus should move to the tab.
 */
function activateTab(root, tab, { focus = false } = {}) {
  if (!(tab instanceof HTMLElement)) {
    return;
  }
  const tabId = String(tab.dataset.mistralVibeTabId || "");
  for (const candidate of tabElements(root)) {
    const selected = candidate === tab;
    candidate.setAttribute("aria-selected", selected ? "true" : "false");
    candidate.tabIndex = selected ? 0 : -1;
  }
  for (const panel of panelElements(root)) {
    panel.hidden = String(panel.dataset.mistralVibeTabId || "") !== tabId;
  }
  if (focus) {
    tab.focus();
  }
}

/**
 * Move selection to a neighboring Mistral Vibe tab.
 *
 * @param {HTMLElement} root - Mounted Mistral Vibe modal root.
 * @param {HTMLElement} current - Currently focused tab.
 * @param {number} direction - Relative movement, usually -1 or 1.
 */
function moveTab(root, current, direction) {
  const tabs = tabElements(root);
  const index = tabs.indexOf(current);
  if (index < 0 || !tabs.length) {
    return;
  }
  activateTab(root, tabs[(index + direction + tabs.length) % tabs.length], { focus: true });
}

/**
 * Bind Mistral Vibe modal tabs while persistence stays on generic block UI fields.
 *
 * @param {HTMLElement} root - Mounted Mistral Vibe modal root.
 * @param {object} api - Generic block UI API passed by the framework.
 * @param {object} context - Render context returned by block.py.
 */
export function mount(root, api, context) {
  previous.mount?.(root, api, context);
  const selected = root.querySelector('[data-mistral-vibe-modal-tab][aria-selected="true"]')
    || root.querySelector("[data-mistral-vibe-modal-tab]");
  activateTab(root, selected);

  root.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-mistral-vibe-modal-tab]");
    if (!tab || !root.contains(tab)) {
      return;
    }
    event.preventDefault();
    activateTab(root, tab, { focus: true });
  });

  root.addEventListener("keydown", (event) => {
    const tab = event.target.closest("[data-mistral-vibe-modal-tab]");
    if (!tab || !root.contains(tab)) {
      return;
    }
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      moveTab(root, tab, 1);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      moveTab(root, tab, -1);
    } else if (event.key === "Home") {
      event.preventDefault();
      activateTab(root, tabElements(root)[0], { focus: true });
    } else if (event.key === "End") {
      event.preventDefault();
      const tabs = tabElements(root);
      activateTab(root, tabs[tabs.length - 1], { focus: true });
    }
  });
}
