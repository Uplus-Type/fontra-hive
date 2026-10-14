// Fontra Hive, "Try Fontra": the guided tour, for someone new to Fontra.
//
// A bubble beside a part of the interface, a ring around it, Back / Next,
// and the steps of try-tour-steps.js. On a first visit to a font kept in
// this browser the welcome bubble offers the tour; the ? in the bar starts
// it again. Done or skipped is remembered in localStorage.
//
// The bubble is a native popover (top layer, above Fontra's overlays) when
// the browser has them, else an element of its own at the end of <body>.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { el } from "./try-common.js";
import { TOURS } from "./try-tour-steps.js";

const KEY = "hive.try.tour"; // {"<tour>": "done" | "skipped" | {step: "<id>"}}
const GAP = 12; // between the ring and the bubble
const RING_PAD = 4;

const STYLE = `
.hive-tour, .hive-tour-ring { box-sizing: border-box; }
.hive-tour {
  position: fixed; inset: auto; margin: 0; padding: 14px 16px 12px; width: 320px; max-width: calc(100vw - 24px);
  border: 0; border-radius: 12px; background: #222; color: #f4f1ea;
  font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  box-shadow: 0 8px 30px rgba(0,0,0,.35); z-index: 1002; overflow: visible; /* the arrow sticks out */
}
.hive-tour:popover-open { display: block; }
.hive-tour h3 { font-size: 15px; font-weight: 600; margin: 0 0 6px; padding-right: 48px; }
.hive-tour p { margin: 0 0 10px; color: #e2ddd2; }
.hive-tour p.teams { color: #a9a397; font-size: 12px; }
.hive-tour p.teams b { color: #f3c744; font-weight: 600; }
.hive-tour .count { position: absolute; top: 14px; right: 16px; font-size: 12px; color: #a9a397; }
.hive-tour .row { display: flex; align-items: center; gap: 8px; }
.hive-tour .row .grow { flex: 1; }
.hive-tour button, .hive-tour a.learn {
  font: inherit; color: #222; background: #f3c744; border: 0; border-radius: 999px;
  padding: 5px 12px; cursor: pointer; text-decoration: none; white-space: nowrap;
}
.hive-tour button.plain { background: transparent; color: #f4f1ea; border: 1px solid #555; }
.hive-tour button.link { background: transparent; color: #a9a397; border: 0; padding: 5px 4px; }
.hive-tour a.learn { background: transparent; color: #f3c744; border: 1px solid #5a4d1e; }
.hive-tour .arrow { position: absolute; width: 14px; height: 14px; background: #222; transform: rotate(45deg); }
.hive-tour[data-side="right"] .arrow { left: -7px; top: 20px; }
.hive-tour[data-side="left"] .arrow { right: -7px; top: 20px; }
.hive-tour[data-side="bottom"] .arrow { top: -7px; left: 24px; }
.hive-tour[data-side="top"] .arrow { bottom: -7px; left: 24px; }
.hive-tour-ring {
  position: fixed; pointer-events: none; z-index: 1001; border: 2px solid #f3c744; border-radius: 8px;
  box-shadow: 0 0 0 4px rgba(243, 199, 68, .25);
}
`;

function remembered() {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "{}") || {};
  } catch (error) {
    return {};
  }
}

function remember(name, state) {
  try {
    const all = remembered();
    all[name] = state;
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch (error) {
    // no localStorage: offered again next time
  }
}

export function tourState(name) {
  return remembered()[name] || null;
}

function install() {
  if (!document.getElementById("hive-tour-style")) {
    document.head.append(el("style", { id: "hive-tour-style" }, STYLE));
  }
}

// The sidebar panel's tab is "selected" when the panel is open.
function openPanel(name) {
  const tab = document.querySelector(`.sidebar-tab[data-sidebar-name="${name}"]`);
  if (!tab || tab.classList.contains("selected")) return;
  const editor = window.editorController;
  if (editor && typeof editor.toggleSidebar === "function") editor.toggleSidebar(name);
  else tab.click();
}

export class Tour {
  constructor(name, tour = TOURS[name]) {
    this.name = name;
    this.tour = tour;
    this.steps = tour.steps.filter((step) => !step.anchor || document.querySelector(step.anchor));
    this.index = -1;
    this.bubble = null;
    this.ring = null;
    this.onKey = (event) => this.key(event);
    this.onMove = () => this.place();
    this.placing = 0;
  }

  static current = null;

  start(index = 0) {
    if (Tour.current) Tour.current.end(null);
    Tour.current = this;
    install();
    this.bubble = el("div", { class: "hive-tour", role: "dialog", "aria-label": this.tour.title });
    if ("showPopover" in this.bubble) this.bubble.setAttribute("popover", "manual");
    this.ring = el("div", { class: "hive-tour-ring", hidden: "" });
    document.body.append(this.ring, this.bubble);
    if (this.bubble.showPopover) this.bubble.showPopover();
    window.addEventListener("keydown", this.onKey, true);
    window.addEventListener("resize", this.onMove);
    window.addEventListener("scroll", this.onMove, true);
    this.show(index);
    return this;
  }

  show(index) {
    this.index = Math.max(0, Math.min(index, this.steps.length - 1));
    const step = this.steps[this.index];
    const last = this.index === this.steps.length - 1;
    // No Back on the first step, nor to the welcome from the step after it.
    const first = this.index === 0 || (this.index === 1 && this.steps[0].id === "welcome");
    if (step.open) openPanel(step.open);
    // Where the tour is, for a page that reloads mid-way (a branch switch).
    if (step.id !== "welcome") remember(this.name, { step: step.id });
    const count = this.steps.length > 1 && step.id !== "welcome" ? `${this.index}/${this.steps.length - 1}` : "";
    const parts = [
      el("span", { class: "arrow", hidden: "" }),
      count ? el("span", { class: "count" }, count) : null,
      el("h3", {}, step.title),
      el("p", {}, step.text),
      step.teams ? el("p", { class: "teams" }, el("b", {}, "Fontra Hive for teams: "), step.teams) : null,
      el(
        "div",
        { class: "row" },
        step.learn
          ? el("a", { class: "learn", href: step.learn, target: "_blank", rel: "noopener" }, "Learn more")
          : null,
        el("span", { class: "grow" }),
        first ? null : el("button", { type: "button", class: "plain", onclick: () => this.back() }, "Back"),
        last
          ? null
          : el("button", { type: "button", class: "link", onclick: () => this.end("skipped") }, step.skip || "Skip"),
        el("button", { type: "button", onclick: () => (last ? this.end("done") : this.next()) }, step.next || "Next")
      ),
    ];
    this.bubble.replaceChildren(...parts.filter((part) => part));
    this.bubble.querySelector(".row button:last-of-type")?.focus();
    this.place();
    // The sidebar may be sliding open: follow it for a moment.
    const until = performance.now() + 700;
    const follow = () => {
      if (!this.bubble || performance.now() > until) return;
      this.place();
      requestAnimationFrame(follow);
    };
    requestAnimationFrame(follow);
  }

  next() {
    if (this.index < this.steps.length - 1) this.show(this.index + 1);
    else this.end("done");
  }

  back() {
    if (this.index > 0) this.show(this.index - 1);
  }

  // Escape ends the tour; the arrows move through it while the bubble has
  // the focus (the editor keeps them otherwise).
  key(event) {
    if (!this.bubble) return;
    const inBubble = this.bubble.contains(document.activeElement);
    if (event.key === "Escape") this.end("skipped");
    else if (event.key === "ArrowRight" && inBubble) this.next();
    else if (event.key === "ArrowLeft" && inBubble) this.back();
    else return;
    event.preventDefault();
    event.stopPropagation();
  }

  // The bubble beside its anchor (or centred, with no anchor), the ring
  // around the anchor.
  place() {
    if (!this.bubble) return;
    const step = this.steps[this.index];
    const bubble = this.bubble;
    const arrow = bubble.querySelector(".arrow");
    const anchor = step.anchor ? document.querySelector(step.anchor) : null;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const bw = bubble.offsetWidth;
    const bh = bubble.offsetHeight;
    const clamp = (value, min, max) => Math.max(min, Math.min(value, max));
    if (!anchor) {
      this.ring.hidden = true;
      arrow.hidden = true;
      bubble.removeAttribute("data-side");
      bubble.style.left = Math.round((vw - bw) / 2) + "px";
      bubble.style.top = Math.round((vh - bh) / 2) + "px";
      return;
    }
    const r = anchor.getBoundingClientRect();
    if (step.ring === false) {
      this.ring.hidden = true;
    } else {
      this.ring.hidden = false;
      this.ring.style.left = r.left - RING_PAD + "px";
      this.ring.style.top = r.top - RING_PAD + "px";
      this.ring.style.width = r.width + 2 * RING_PAD + "px";
      this.ring.style.height = r.height + 2 * RING_PAD + "px";
    }
    let side = step.placement || (r.left + r.width / 2 < vw / 2 ? "right" : "left");
    if (side === "right" && r.right + GAP + bw > vw) side = "left";
    if (side === "left" && r.left - GAP - bw < 0) side = "right";
    if (side === "bottom" && r.bottom + GAP + bh > vh) side = "top";
    if (side === "top" && r.top - GAP - bh < 0) side = "bottom";
    let left;
    let top;
    arrow.hidden = false;
    if (side === "inside-bottom") {
      arrow.hidden = true;
      left = r.left + (r.width - bw) / 2;
      top = r.bottom - bh - 24;
    } else if (side === "right") {
      left = r.right + GAP + RING_PAD;
      top = r.top - 14;
    } else if (side === "left") {
      left = r.left - GAP - RING_PAD - bw;
      top = r.top - 14;
    } else if (side === "bottom") {
      left = r.left + Math.min(r.width / 2, 40) - 31;
      top = r.bottom + GAP + RING_PAD;
    } else {
      left = r.left + Math.min(r.width / 2, 40) - 31;
      top = r.top - GAP - RING_PAD - bh;
    }
    bubble.setAttribute("data-side", side === "inside-bottom" ? "" : side);
    bubble.style.left = Math.round(clamp(left, 8, vw - bw - 8)) + "px";
    bubble.style.top = Math.round(clamp(top, 8, vh - bh - 8)) + "px";
  }

  // state: "done", "skipped", or null (replaced by another tour).
  end(state) {
    if (state) remember(this.name, state);
    window.removeEventListener("keydown", this.onKey, true);
    window.removeEventListener("resize", this.onMove);
    window.removeEventListener("scroll", this.onMove, true);
    if (this.bubble?.hidePopover && this.bubble.matches(":popover-open")) this.bubble.hidePopover();
    this.bubble?.remove();
    this.ring?.remove();
    this.bubble = null;
    this.ring = null;
    if (Tour.current === this) Tour.current = null;
    window.dispatchEvent(new CustomEvent("hive-tour-end", { detail: { tour: this.name, state } }));
  }
}

// `stepId`: where to start, when not at the beginning.
export function startTour(name = "editor", stepId = null) {
  if (!TOURS[name]) return null;
  const tour = new Tour(name);
  const index = stepId ? tour.steps.findIndex((step) => step.id === stepId) : -1;
  return tour.start(index < 0 ? 0 : index);
}

// Hive's panels and Fontra's editor are mounted once the font has loaded
// (Python in the browser may still be starting); the tour waits for them,
// up to `timeout` ms. True when they are there.
function whenReady(selectors, timeout = 120000) {
  return new Promise((resolve) => {
    const started = performance.now();
    const check = () => {
      const ready = !!window.editorController?.fontController && selectors.every((s) => document.querySelector(s));
      if (ready) resolve(true);
      else if (performance.now() - started > timeout) resolve(false);
      else setTimeout(check, 250);
    };
    check();
  });
}

const EDITOR_READY = ['.sidebar-tab[data-sidebar-name="hive-history"]', ".hive-branch", "#edit-tools .tool-button"];

// On a first visit: the welcome bubble, once the editor and Hive's panels
// are up — never on a page still loading, or broken; a tour left mid-way
// (the page reloaded on a branch switch) goes on from its step. Returns
// the tour, or null when it was done or skipped before, or the editor
// never came up.
export async function offerTour(name = "editor", timeout = 120000) {
  const done = (state) => state === "done" || state === "skipped";
  if (done(tourState(name)) || !TOURS[name]) return null;
  if (!(await whenReady(EDITOR_READY, timeout))) return null;
  const state = tourState(name);
  if (done(state) || Tour.current) return null;
  return startTour(name, state && typeof state === "object" ? state.step : null);
}
