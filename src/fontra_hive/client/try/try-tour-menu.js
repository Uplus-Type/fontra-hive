// Fontra Hive, "Try Fontra": the menu behind the ? of the bar — Try's
// manual. Every tour and lesson with its length and where you are in it
// (✓ done, ● going on), Start over, Fontra's documentation, and Fontra Hive
// for teams. No long text here: that is the documentation's job.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { el } from "./try-common.js";
import { Tour, forgetTour, startTour, tourState } from "./try-tour.js";
import { TOURS } from "./try-tour-steps.js";
import { DOCS, HIVE } from "./tours/lib.js";

// As Hive's own menus (hive-views.js), which are not loaded on every page.
const STYLE = `
.hive-tour-menu { position: fixed; z-index: 1003; min-width: 300px; max-width: min(380px, calc(100vw - 24px));
  margin: 0; inset: auto; padding: 5px 0; border: 0; border-radius: 6px;
  background: var(--ui-element-background-color, white); color: var(--ui-element-foreground-color, black);
  box-shadow: 0 3px 16px #0004; font: 13px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
.hive-tour-menu:popover-open { display: block; }
.hive-tour-menu .title { padding: 5px 16px 3px; font-weight: bold; }
.hive-tour-menu hr { border: none; border-top: 1px solid var(--horizontal-rule-color, #aaa8); margin: 5px 0; }
.hive-tour-menu .row, .hive-tour-menu a.item { display: flex; align-items: center; gap: 8px; padding: 4px 16px 4px 12px;
  cursor: pointer; color: inherit; text-decoration: none; background: none; border: 0; width: 100%; text-align: left; font: inherit; }
.hive-tour-menu .row:hover, .hive-tour-menu .row:focus-visible, .hive-tour-menu a.item:hover { background: #46f; color: white; outline: none; }
.hive-tour-menu .mark { width: 1em; flex: none; text-align: center; }
.hive-tour-menu .text { flex: 1; min-width: 0; line-height: 1.25; }
.hive-tour-menu .text b, .hive-tour-menu .text small { display: block; }
.hive-tour-menu .text b { font-weight: 600; }
.hive-tour-menu .text small { opacity: 0.65; font-size: 0.88em; }
.hive-tour-menu .length { flex: none; opacity: 0.65; font-size: 0.88em; }
`;

function install() {
  if (!document.getElementById("hive-tour-menu-style")) {
    document.head.append(el("style", { id: "hive-tour-menu-style" }, STYLE));
  }
}

// ✓ done, ● going on, nothing otherwise (skipped is not done).
export function tourMark(name) {
  const state = tourState(name);
  if (state === "done") return "✓";
  if (state && typeof state === "object") return "●";
  return "";
}

let open = null;

export function closeTourMenu() {
  open?.close();
}

// The menu above `button` (the bar's ?).
export function openTourMenu(button) {
  if (open) {
    open.close();
    return null;
  }
  install();
  const menu = el("div", { class: "hive-tour-menu", role: "menu", "aria-label": "Tutorials" });
  if ("showPopover" in menu) menu.setAttribute("popover", "manual");

  const close = () => {
    document.removeEventListener("pointerdown", outside, true);
    document.removeEventListener("keydown", onKey, true);
    if (menu.hidePopover && menu.matches(":popover-open")) menu.hidePopover();
    menu.remove();
    open = null;
  };
  const outside = (event) => {
    if (!menu.contains(event.target) && event.target !== button) close();
  };
  const onKey = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
    }
  };
  const run = (name) => {
    close();
    const state = tourState(name);
    // Going on: from its step; done or never seen: from the start.
    startTour(name, state && typeof state === "object" ? state.step : null);
  };

  const rows = Object.entries(TOURS).map(([name, tour]) => {
    const mark = tourMark(name);
    const what = tour.kind === "lesson" ? "lesson" : "tour";
    return el(
      "button",
      {
        type: "button",
        class: "row",
        role: "menuitem",
        "data-tour": name,
        title: mark === "●" ? "Go on where you left it" : "",
        onclick: () => run(name),
      },
      el("span", { class: "mark", "aria-label": mark === "✓" ? "done" : mark === "●" ? "going on" : "" }, mark),
      el("span", { class: "text" }, el("b", {}, tour.title), tour.about ? el("small", {}, tour.about) : null),
      el("span", { class: "length" }, `${what} · ${tour.minutes} min`)
    );
  });

  menu.append(
    el("div", { class: "title" }, "Tutorials"),
    ...rows,
    el("hr"),
    el(
      "button",
      {
        type: "button",
        class: "row",
        role: "menuitem",
        "data-action": "start-over",
        title: "Forget what you have done: every tutorial starts from the beginning",
        onclick: () => {
          Tour.current?.end(null);
          for (const name of Object.keys(TOURS)) forgetTour(name);
          close();
        },
      },
      el("span", { class: "mark" }, "↺"),
      el("span", { class: "text" }, "Start over")
    ),
    el(
      "a",
      { class: "item", role: "menuitem", href: DOCS, target: "_blank", rel: "noopener", onclick: close },
      el("span", { class: "mark" }),
      el("span", { class: "text" }, "Documentation")
    ),
    el(
      "a",
      { class: "item", role: "menuitem", href: HIVE, target: "_blank", rel: "noopener", onclick: close },
      el("span", { class: "mark" }),
      el("span", { class: "text" }, "Fontra Hive for teams")
    )
  );

  document.body.append(menu);
  if (menu.showPopover) menu.showPopover();
  // Above the button, its right edge on the button's.
  const r = button.getBoundingClientRect();
  const left = Math.max(8, Math.min(r.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8));
  menu.style.left = left + "px";
  menu.style.top = Math.max(8, r.top - menu.offsetHeight - 8) + "px";
  document.addEventListener("pointerdown", outside, true);
  document.addEventListener("keydown", onKey, true);
  menu.querySelector(".row")?.focus();
  open = { close, menu };
  return menu;
}
