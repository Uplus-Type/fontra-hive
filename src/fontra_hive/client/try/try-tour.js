// Fontra Hive, "Try Fontra": the guided tours and lessons.
//
// A bubble beside a part of the interface, a ring around it, Back / Next,
// and the steps of try-tour-steps.js (one file per tour in tours/). On a
// first visit to a font kept in this browser the welcome bubble offers the
// tour of the editor; the ? in the bar lists every tour (try-tour-menu.js).
// Done or skipped is remembered in localStorage.
//
// A tour is watched (Next after Next); a lesson is done: a step with
// `until` waits for the thing to be done — its Next reads "I did it" until
// then (to go on anyway), and a check shows once it is done.
//
// A tour goes across Fontra's pages (the editor, Font overview, Font Info,
// Settings): a step on another page offers a button that opens it, the
// way Fontra does (a named window, or this tab), and the tour goes on
// there; the tab left behind closes its bubble. What is going on is in
// localStorage["hive.try.tour.now"]: {tour, step, view, project, tab, at}
// — `tab` is the tab showing it, or null while it waits for the page that
// was asked for.
//
// The bubble is a native popover (top layer, above Fontra's overlays) when
// the browser has them, else an element of its own at the end of <body>.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { el } from "./try-common.js";
import { TOURS } from "./try-tour-steps.js";

const KEY = "hive.try.tour"; // {"<tour>": "done" | "skipped" | {step: "<id>"}}
const NOW_KEY = "hive.try.tour.now";
const GAP = 12; // between the ring and the bubble
const RING_PAD = 4;
const POLL = 300; // ms, between two looks at a step's `until`
const NARROW = 900; // px: below, the bubble sits at the bottom of the screen
const STALE = 3600 * 1000; // a tour asked for on another page, so long ago, is let go

export const VIEWS = ["editor", "fontoverview", "fontinfo", "applicationsettings"];
const VIEW_NAMES = {
  editor: "the editor",
  fontoverview: "Font overview",
  fontinfo: "Font Info",
  applicationsettings: "Settings",
};
const VIEW_BUTTONS = {
  editor: "Back to the editor",
  fontoverview: "Open Font overview",
  fontinfo: "Open Font Info",
  applicationsettings: "Open Settings",
};

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
.hive-tour h3 .check { display: none; color: #7bd88f; margin-left: 6px; }
.hive-tour.done h3 .check { display: inline; }
.hive-tour p { margin: 0 0 10px; color: #e2ddd2; }
.hive-tour p.teams { color: #a9a397; font-size: 12px; }
.hive-tour p.teams b { color: #f3c744; font-weight: 600; }
.hive-tour p.where { color: #a9a397; font-size: 12px; }
.hive-tour .count { position: absolute; top: 14px; right: 16px; font-size: 12px; color: #a9a397; }
.hive-tour .row { display: flex; align-items: center; gap: 8px; }
.hive-tour .row .grow { flex: 1; }
.hive-tour p.more { margin: -4px 0 10px; }
.hive-tour p.more a { color: #f3c744; text-decoration: none; font-size: 12px; }
.hive-tour p.more a:hover { text-decoration: underline; }
.hive-tour button {
  font: inherit; color: #222; background: #f3c744; border: 0; border-radius: 999px;
  padding: 5px 12px; cursor: pointer; text-decoration: none; white-space: nowrap;
}
.hive-tour button.plain, .hive-tour button.waiting { background: transparent; color: #f4f1ea; border: 1px solid #555; }
.hive-tour button.link { background: transparent; color: #a9a397; border: 0; padding: 5px 4px; }
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

// --- What is remembered ----------------------------------------------------

function readJSON(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || "null");
  } catch (error) {
    return null;
  }
}

function remembered() {
  return readJSON(KEY) || {};
}

function remember(name, state) {
  try {
    const all = remembered();
    if (state === undefined) delete all[name];
    else all[name] = state;
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch (error) {
    // no localStorage: offered again next time
  }
}

export function tourState(name) {
  return remembered()[name] || null;
}

// Forget a tour: it starts over, and is offered again.
export function forgetTour(name) {
  remember(name, undefined);
  if (readNow()?.tour === name) writeNow(null);
}

function readNow() {
  const now = readJSON(NOW_KEY);
  return now && typeof now === "object" && TOURS[now.tour] ? now : null;
}

function writeNow(now) {
  try {
    if (now) localStorage.setItem(NOW_KEY, JSON.stringify({ ...now, at: Date.now() }));
    else localStorage.removeItem(NOW_KEY);
  } catch (error) {
    // no localStorage: the tour stays in this tab
  }
}

// --- Where we are ----------------------------------------------------------

// This tab, across reloads (a branch switch reloads the page). A window
// opened by another gets a copy of its sessionStorage: the key carries the
// window's name (Fontra names the windows it opens), and a window just
// opened, unnamed, starts afresh.
export const TAB = (() => {
  const fresh = () => Math.random().toString(36).slice(2, 10);
  try {
    const key = "hive.try.tab:" + (window.name.startsWith("fontra.") ? window.name : "");
    let justOpened = false;
    try {
      justOpened = !!window.opener && history.length === 1 && performance.getEntriesByType("navigation")[0]?.type === "navigate";
    } catch (error) {
      justOpened = false;
    }
    let id = justOpened ? null : sessionStorage.getItem(key);
    if (!id) sessionStorage.setItem(key, (id = fresh()));
    return id;
  } catch (error) {
    return fresh();
  }
})();

export function viewOf(pathname) {
  const match = /\/([a-z]+)\.html$/.exec(pathname || "");
  return match && VIEWS.includes(match[1]) ? match[1] : "editor";
}

export function hereView() {
  return viewOf(location.pathname);
}

function projectHere() {
  return new URLSearchParams(location.search).get("project") || "";
}

// The font, whatever its branch ("local:<id>@<branch>").
function sameFont(a, b) {
  return !a || !b || a.split("@")[0] === b.split("@")[0];
}

function stepView(tour, step) {
  return step.view || tour.view || "editor";
}

// A selector, through open shadow roots: "a >>> b" is b in a's shadow root.
export function deepQuery(selector, root = document) {
  let node = root;
  for (const part of selector.split(">>>").map((s) => s.trim())) {
    const scope = node === root ? root : node.shadowRoot;
    node = scope?.querySelector(part);
    if (!node) return null;
  }
  return node;
}

// What a step's `until` gets: Fontra's public objects, on whichever page.
function context() {
  const editor = window.editorController || null;
  const controller =
    editor || window.fontInfoController || window.fontOverviewController || window.applicationSettingsController;
  return {
    editor,
    fontController: controller?.fontController || null,
    sceneSettings: editor?.sceneSettings || null,
    hiveTry: window.hiveTry || null,
  };
}

function install() {
  if (!document.getElementById("hive-tour-style")) {
    document.head.append(el("style", { id: "hive-tour-style" }, STYLE));
  }
  listen();
}

// The sidebar panel's tab is "selected" when the panel is open.
function openPanel(name) {
  const tab = document.querySelector(`.sidebar-tab[data-sidebar-name="${name}"]`);
  if (!tab || tab.classList.contains("selected")) return;
  const editor = window.editorController;
  if (editor && typeof editor.toggleSidebar === "function") editor.toggleSidebar(name);
  else tab.click();
}

// Font Info and Settings show one panel at a time (#<panel> in the address).
function selectPanel(hash) {
  if (!hash || location.hash === "#" + hash) return;
  const multi = (window.fontInfoController || window.applicationSettingsController)?.multiPanelController;
  if (multi && typeof multi.selectPanel === "function") multi.selectPanel(hash);
  else location.hash = hash;
}

// The address of a page of Fontra, for the font open here.
function viewURL(view, hash) {
  const url = new URL(location.href);
  url.pathname = url.pathname.replace(/[^/]*$/, view + ".html");
  url.hash = hash ? "#" + hash : "";
  if (view !== "editor") url.searchParams.delete("text");
  return url.toString();
}

// The editor's tab of a font is given a name while a tour shows there (Fontra
// does not name it), so that Font Info or Settings can bring it back.
function editorWindowName(project = projectHere()) {
  return "hive.try.editor." + (project || "").split("@")[0];
}

function nameEditorWindow() {
  if (!window.name || window.name.startsWith("hive.try.editor.")) window.name = editorWindowName();
}

// Open a page the way Fontra does: Font overview and Font Info in a window
// named after the font, Settings in its own, the editor back in the window
// that opened this one when there is one.
function navigate(view, hash) {
  const url = viewURL(view, hash);
  const project = projectHere();
  let target = "_self";
  if (view === hereView()) target = "_self";
  else if (view === "fontinfo" || view === "fontoverview") target = `fontra.${view}.${project}`;
  else if (view === "applicationsettings") target = "fontra.applicationsettings";
  else if (view === "editor") {
    // The editor's tab when it is still open (it is not reloaded: what is
    // open there stays); else a new one.
    let editor = null;
    try {
      editor = window.open("", editorWindowName(project));
    } catch (error) {
      editor = null;
    }
    if (editor) {
      let blank = false;
      try {
        blank = editor.location.href === "about:blank";
      } catch (error) {
        blank = false;
      }
      if (blank) editor.location.href = url;
      editor.focus();
      return "editor";
    }
  }
  const opened = window.open(url, target);
  if (!opened) location.href = url;
  return target;
}

// --- The tour --------------------------------------------------------------

export class Tour {
  constructor(name, tour = TOURS[name]) {
    this.name = name;
    this.tour = tour;
    this.view = hereView();
    // A step whose part of the interface is absent from this page is left
    // out — unless it is on another page, or waits for something to be done
    // (what it points at may come later).
    this.steps = tour.steps.filter(
      (step) =>
        !step.anchor || step.until || stepView(tour, step) !== this.view || deepQuery(step.anchor)
    );
    this.index = -1;
    this.bubble = null;
    this.ring = null;
    this.done = false;
    this.polling = null;
    this.onKey = (event) => this.key(event);
    this.onMove = () => this.place();
    // The part of the interface a step points at may grow, shrink or move
    // while it is shown (a panel resized, the window, a sidebar opening):
    // the bubble and the ring follow it. A sidebar's tab does not change
    // size when its panel is resized, it moves — the box around it does: the
    // anchor and every box it sits in are watched.
    this.watched = null;
    this.placing = 0;
    this.observer =
      typeof ResizeObserver === "function"
        ? new ResizeObserver(() => {
            if (this.placing) return;
            this.placing = requestAnimationFrame(() => {
              this.placing = 0;
              this.place();
            });
          })
        : null;
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

  get step() {
    return this.steps[this.index];
  }

  // Is the current step on this page?
  get here() {
    return stepView(this.tour, this.step) === this.view;
  }

  show(index) {
    this.polling = null;
    this.done = false;
    this.index = Math.max(0, Math.min(index, this.steps.length - 1));
    const step = this.step;
    const here = this.here;
    const view = stepView(this.tour, step);
    const last = this.index === this.steps.length - 1;
    // No Back on the first step, nor to the welcome from the step after it.
    const first =
      this.index === 0 ||
      (this.index === 1 && this.steps[0].id === "welcome") ||
      (here && stepView(this.tour, this.steps[this.index - 1]) !== view);
    if (here) {
      if (view === "editor") nameEditorWindow();
      selectPanel(step.hash);
      if (step.open) openPanel(step.open);
    }
    // Where the tour is, for a page that reloads mid-way (a branch switch),
    // or another page that takes it over.
    if (!step.transient) {
      if (step.id !== "welcome") remember(this.name, { step: step.id });
      writeNow({ tour: this.name, step: step.id, view, project: projectHere(), tab: TAB });
    }
    const count = this.steps.length > 1 && step.id !== "welcome" ? `${this.index}/${this.steps.length - 1}` : "";
    this.primary = el("button", { type: "button", class: "next", onclick: () => this.primaryClick() });
    const parts = [
      el("span", { class: "arrow", hidden: "" }),
      count ? el("span", { class: "count" }, count) : null,
      el("h3", {}, step.title, el("span", { class: "check", "aria-label": "done" }, "✓")),
      // From another page, the step's text would be read too early: only
      // where it goes on.
      el("p", {}, here ? step.text : step.away || `The next step is in ${VIEW_NAMES[view]}: open it to go on.`),
      here && step.teams ? el("p", { class: "teams" }, el("b", {}, "Fontra Hive for teams: "), step.teams) : null,
      here && step.learn
        ? el("p", { class: "more" }, el("a", { class: "learn", href: step.learn, target: "_blank", rel: "noopener" }, "Learn more ↗"))
        : null,
      el(
        "div",
        { class: "row" },
        el("span", { class: "grow" }),
        first ? null : el("button", { type: "button", class: "plain", onclick: () => this.back() }, "Back"),
        last
          ? null
          : el("button", { type: "button", class: "link", onclick: () => this.end("skipped") }, step.skip || "Skip"),
        last && TOURS[this.tour.then]
          ? el(
              "button",
              { type: "button", class: "plain", onclick: () => this.keepGoing() },
              "Keep going: " + TOURS[this.tour.then].title
            )
          : null,
        this.primary
      ),
    ];
    this.bubble.replaceChildren(...parts.filter((part) => part));
    this.bubble.classList.remove("done");
    this.label();
    this.primary.focus();
    this.place();
    if (here && step.until) this.poll(step);
    // The sidebar may be sliding open: follow it for a moment.
    const until = performance.now() + 700;
    const follow = () => {
      if (!this.bubble || performance.now() > until) return;
      this.place();
      requestAnimationFrame(follow);
    };
    requestAnimationFrame(follow);
  }

  // The main button: open the step's page; "I did it" while a lesson's step
  // is not done; Next, or the step's own word.
  label() {
    const step = this.step;
    const last = this.index === this.steps.length - 1;
    let text;
    let waiting = false;
    if (!this.here) text = step.go || VIEW_BUTTONS[stepView(this.tour, step)];
    else if (step.until && !this.done) {
      text = "I did it";
      waiting = true;
    } else text = step.next || (last ? "Done" : "Next");
    this.primary.textContent = text;
    this.primary.classList.toggle("waiting", waiting);
  }

  primaryClick() {
    if (!this.here) this.goTo(this.step);
    else this.next();
  }

  // Look at a step's `until` every POLL ms while it is shown.
  poll(step) {
    const token = {};
    this.polling = token;
    const check = async () => {
      if (this.polling !== token) return;
      let ok = false;
      try {
        ok = !!(await step.until(context()));
      } catch (error) {
        ok = false;
      }
      if (this.polling !== token || !this.bubble) return;
      if (ok !== this.done) {
        this.done = ok;
        this.bubble.classList.toggle("done", ok);
        this.label();
      }
      setTimeout(check, POLL);
    };
    check();
  }

  // The step is on another page: open it; the tour goes on there.
  goTo(step) {
    const view = stepView(this.tour, step);
    const stepId = step.resume || step.id;
    if (!step.transient) remember(this.name, { step: stepId });
    writeNow({ tour: this.name, step: stepId, view, project: projectHere(), tab: null });
    this.close();
    if (step.self) {
      location.href = viewURL(view, step.hash);
      return;
    }
    if (navigate(view, step.hash) === "editor") {
      // The browser may keep this tab in front: say where the tour went.
      setTimeout(() => {
        if (document.visibilityState !== "visible" || Tour.current || readNow()?.tab) return;
        new Tour(this.name, {
          title: this.tour.title,
          steps: [
            {
              id: "elsewhere",
              transient: true,
              self: true,
              resume: stepId,
              view,
              hash: step.hash,
              title: "In the editor's tab",
              text: "The tour goes on in the tab with the editor: switch to it, or open the editor here.",
              go: "Open the editor here",
            },
          ],
        }).start(0);
      }, 600);
    }
  }

  // The last step: on to the tour that follows this one.
  keepGoing() {
    const then = this.tour.then;
    this.end("done");
    forgetTour(then);
    startTour(then);
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
    // The ? menu, open above the bubble, has the keys.
    if (!this.bubble || document.querySelector(".hive-tour-menu")) return;
    const inBubble = this.bubble.contains(document.activeElement);
    if (event.key === "Escape") this.end("skipped");
    else if (event.key === "ArrowRight" && inBubble && this.here) this.next();
    else if (event.key === "ArrowLeft" && inBubble) this.back();
    else return;
    event.preventDefault();
    event.stopPropagation();
  }

  // The bubble beside its anchor (or centred, with no anchor, or at the
  // bottom of a narrow screen), the ring around the anchor.
  place() {
    if (!this.bubble) return;
    const step = this.step;
    const bubble = this.bubble;
    const arrow = bubble.querySelector(".arrow");
    const anchor = this.here && step.anchor ? deepQuery(step.anchor) : null;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const bw = bubble.offsetWidth;
    const bh = bubble.offsetHeight;
    const clamp = (value, min, max) => Math.max(min, Math.min(value, max));
    const [dx, dy] = step.offset || [0, 0];
    this.watch(anchor);
    const r = anchor ? anchor.getBoundingClientRect() : null;
    if (!r || (r.width === 0 && r.height === 0) || step.ring === false) {
      this.ring.hidden = true;
    } else {
      this.ring.hidden = false;
      this.ring.style.left = r.left - RING_PAD + "px";
      this.ring.style.top = r.top - RING_PAD + "px";
      this.ring.style.width = r.width + 2 * RING_PAD + "px";
      this.ring.style.height = r.height + 2 * RING_PAD + "px";
    }
    if (!r || vw < NARROW) {
      arrow.hidden = true;
      bubble.removeAttribute("data-side");
      bubble.style.left = Math.round((vw - bw) / 2) + "px";
      // Narrow: at the bottom, above the bar, the ring alone shows the place.
      bubble.style.top = Math.round(r ? Math.max(8, vh - bh - 72) : (vh - bh) / 2) + "px";
      return;
    }
    let side = step.placement || (r.left + r.width / 2 < vw / 2 ? "right" : "left");
    if (side === "right" && r.right + GAP + bw > vw) side = "left";
    if (side === "left" && r.left - GAP - bw < 0) side = "right";
    if (side === "bottom" && r.bottom + GAP + bh > vh) side = "top";
    if (side === "top" && r.top - GAP - bh < 0) side = "bottom";
    let left;
    let top;
    const inside = side.startsWith("inside-");
    arrow.hidden = inside;
    if (side === "inside-bottom") {
      left = r.left + (r.width - bw) / 2;
      top = r.bottom - bh - 24;
    } else if (side === "inside-top") {
      left = r.left + (r.width - bw) / 2;
      top = r.top + 24;
    } else if (side === "inside-right") {
      left = r.right - bw - 24;
      top = r.top + 24;
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
    if (inside) bubble.removeAttribute("data-side");
    else bubble.setAttribute("data-side", side);
    bubble.style.left = Math.round(clamp(left + dx, 8, vw - bw - 8)) + "px";
    bubble.style.top = Math.round(clamp(top + dy, 8, vh - bh - 8)) + "px";
  }

  // Watch the step's anchor and the boxes it sits in (through shadow roots),
  // up to <body>; nothing when the step points at nothing here.
  watch(anchor) {
    if (!this.observer || anchor === this.watched) return;
    this.observer.disconnect();
    this.watched = anchor;
    for (let node = anchor; node && node !== document.documentElement; ) {
      this.observer.observe(node);
      node = node.parentElement || node.getRootNode?.().host || null;
    }
  }

  // Take the bubble away, nothing remembered (another tab shows the tour now).
  close() {
    this.polling = null;
    this.observer?.disconnect();
    this.watched = null;
    if (this.placing) cancelAnimationFrame(this.placing);
    this.placing = 0;
    window.removeEventListener("keydown", this.onKey, true);
    window.removeEventListener("resize", this.onMove);
    window.removeEventListener("scroll", this.onMove, true);
    if (this.bubble?.hidePopover && this.bubble.matches(":popover-open")) this.bubble.hidePopover();
    this.bubble?.remove();
    this.ring?.remove();
    this.bubble = null;
    this.ring = null;
    if (Tour.current === this) Tour.current = null;
  }

  // state: "done", "skipped", or null (replaced by another tour).
  end(state) {
    if (state) {
      remember(this.name, state);
      if (readNow()?.tour === this.name) writeNow(null);
    }
    this.close();
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

// --- When to show it -------------------------------------------------------

// Fontra's page is up (and, in the editor, Hive's panels: the font has
// loaded, Python in the browser may still be starting); up to `timeout` ms.
const READY = {
  editor: [
    () => window.editorController?.fontController,
    ['.sidebar-tab[data-sidebar-name="hive-history"]', ".hive-branch", "#edit-tools .tool-button"],
  ],
  fontinfo: [() => window.fontInfoController?.fontController, ["#font-info-panel"]],
  fontoverview: [() => window.fontOverviewController?.fontController, []],
  applicationsettings: [() => window.applicationSettingsController, ["#theme-settings-panel"]],
};

export function whenReady(view = hereView(), timeout = 120000) {
  const [controller, selectors] = READY[view] || READY.editor;
  return new Promise((resolve) => {
    const started = performance.now();
    const check = () => {
      const ready = !!controller() && selectors.every((s) => document.querySelector(s));
      if (ready) resolve(true);
      else if (performance.now() - started > timeout) resolve(false);
      else setTimeout(check, 250);
    };
    check();
  });
}

// The tour going on, if it is this page's to show: asked for from another
// page (no tab yet), or shown here before a reload.
function claimable(now) {
  return (
    !!now &&
    (!now.tab || now.tab === TAB) &&
    now.view === hereView() &&
    (now.view === "applicationsettings" || TOURS[now.tour].anyFont || sameFont(now.project, projectHere())) &&
    Date.now() - (now.at || 0) < STALE
  );
}

let resuming = null;

// Take over the tour going on, when it is this page's turn.
export function resumeTour(timeout = 120000) {
  if (resuming) return resuming;
  if (Tour.current || document.visibilityState === "hidden" || !claimable(readNow())) {
    return Promise.resolve(null);
  }
  resuming = (async () => {
    const now = readNow();
    writeNow({ ...now, tab: TAB }); // this tab shows it: another one closes its bubble
    if (!(await whenReady(hereView(), timeout))) return null;
    const still = readNow();
    if (Tour.current || !still || still.tab !== TAB) return null;
    return startTour(still.tour, still.step);
  })().finally(() => (resuming = null));
  return resuming;
}

let listening = false;

// Another tab took the tour over, or ended it: this one's bubble goes; or
// it asked for this page: this one takes it.
function listen() {
  if (listening) return;
  listening = true;
  window.addEventListener("storage", (event) => {
    if (event.key !== NOW_KEY && event.key !== null) return;
    const now = readNow();
    const current = Tour.current;
    if (current && (!now || now.tab !== TAB)) current.close();
    if (!Tour.current) resumeTour();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !Tour.current) resumeTour();
  });
}

// On a first visit: the welcome bubble, once the editor and Hive's panels
// are up — never on a page still loading, or broken; a tour left mid-way
// (the page reloaded on a branch switch) goes on from its step. Returns
// the tour, or null when it was done or skipped before, or the editor
// never came up.
export async function offerTour(name = "editor", timeout = 120000) {
  install();
  const done = (state) => state === "done" || state === "skipped";
  if (!TOURS[name]) return null;
  const now = readNow();
  if (claimable(now)) return resumeTour(timeout);
  // Shown in another tab, still open: not here too.
  if (now && now.tab && now.tab !== TAB && Date.now() - (now.at || 0) < STALE) return null;
  if (done(tourState(name))) return null;
  if (!(await whenReady(hereView(), timeout))) return null;
  const state = tourState(name);
  if (done(state) || Tour.current) return null;
  return startTour(name, state && typeof state === "object" ? state.step : null);
}

// What a page of Try does once loaded: the tour going on, if it is this
// page's; a tour asked for in the address (?tour=<name>, from the docs'
// "Try it" buttons), from its start even when done; else, in the editor,
// the welcome on a first visit.
export async function tourOnLoad(timeout = 120000) {
  install();
  const params = new URLSearchParams(location.search);
  const asked = params.get("tour");
  if (asked !== null) {
    // Once only: a reload does not start it again.
    params.delete("tour");
    const url = new URL(location.href);
    url.search = params.toString();
    history.replaceState(history.state, "", url);
  }
  if (claimable(readNow()) && !TOURS[asked]) return resumeTour(timeout);
  if (TOURS[asked]) {
    if (!(await whenReady(hereView(), timeout))) return null;
    forgetTour(asked);
    return startTour(asked);
  }
  if (hereView() === "editor") return offerTour("editor", timeout);
  return null;
}
