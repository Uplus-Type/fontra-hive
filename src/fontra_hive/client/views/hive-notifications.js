// The bell of Hive's pages and Fontra's views: how many notifications are
// unread, what they are, and a bubble when something new arrives.
//
// hive-api keeps them (/api/notifications). The page asks every 10 seconds
// how many are unread, when it can be seen; a hidden tab asks when it comes
// back. The bubble is shared by the tabs (localStorage): it shows in the tab
// in use, comes back in the next one until it has been closed, clicked or has
// lasted its time, which runs only while it can be seen.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { COMMENTS_TAB_PREFIX, editorTabName, issueLink } from "../plugin/comments.js";

const POLL_MS = 10000;
const TICK_MS = 1000;
const BUBBLE_MS = 8000;
const MAX_BUBBLE = 20; // what it keeps to say, the latest
const MAX_BADGE = 99;

const STYLE = `
  .hive-bell { position: relative; display: inline-flex; align-items: center; justify-content: center;
               width: 28px; height: 28px; padding: 0; border: none; background: none; color: inherit;
               cursor: pointer; opacity: 0.8; }
  .hive-bell:hover, .hive-bell.open { opacity: 1; }
  .hive-bell svg { width: 20px; height: 20px; }
  .hive-bell-count { position: absolute; top: -2px; right: -3px; min-width: 15px; height: 15px; padding: 0 3px;
                     box-sizing: border-box; border-radius: 8px; background: #e9b02a; color: #1c1c1c;
                     font: bold 10px/15px fontra-ui-regular, sans-serif; text-align: center; }
  .hive-bell-count[hidden] { display: none; }
  .hive-notifications { width: min(340px, calc(100vw - 24px)); max-height: 70vh; overflow-y: auto;
                        font-family: fontra-ui-regular, sans-serif; }
  .hive-notifications .head { display: flex; align-items: center; justify-content: space-between;
                              padding: 3px 16px 5px; font-weight: bold; }
  .hive-notifications .head a { padding: 0; font-weight: normal; font-size: 0.88em; opacity: 0.7; }
  .hive-notifications .head a:hover { background: none; color: inherit; text-decoration: underline; }
  .hive-notifications a.notification { white-space: normal; overflow-wrap: anywhere; line-height: 1.3; opacity: 0.7; }
  .hive-notifications a.notification.unread { opacity: 1; font-weight: bold; }
  .hive-notifications a.notification small { display: block; font-weight: normal; opacity: 0.65; }
  .hive-bubble { position: fixed; right: 16px; bottom: 16px; z-index: 1100; display: flex; align-items: flex-start;
                 gap: 8px; width: min(320px, calc(100vw - 32px)); box-sizing: border-box; padding: 9px 8px 9px 12px;
                 border-radius: 8px; border-left: 4px solid #e9b02a; cursor: pointer;
                 background: var(--ui-element-background-color, white); color: var(--ui-element-foreground-color, black);
                 box-shadow: 0 4px 18px #0005; font: 13px/1.3 fontra-ui-regular, sans-serif; }
  .hive-bubble .text { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .hive-bubble small { display: block; opacity: 0.65; }
  .hive-bubble button { border: none; background: none; color: inherit; font-size: 16px; line-height: 1;
                        padding: 0 3px; cursor: pointer; }
`;

const VERBS = {
  mentioned: "mentioned you in",
  assigned: "assigned you",
  replied: "replied in",
  resolved: "resolved",
  reopened: "reopened",
};

function el(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (key === "class") element.className = value;
    else if (value !== undefined && value !== null && value !== false) element.setAttribute(key, value);
  }
  element.append(...[children].flat().filter((child) => child !== null && child !== undefined));
  return element;
}

// "Ana mentioned you in #12 · H · Gotham".
export function notificationText(n) {
  const who = n.actor?.name || n.actor?.username || "Someone";
  const project = n.project.split("/").pop();
  return `${who} ${VERBS[n.type] || "wrote in"} #${n.issue} · ${n.glyph || "Font"} · ${project}`;
}

export function notificationLink(n, origin) {
  return issueLink(n.project, { number: n.issue, glyph: n.glyph }, origin);
}

// What the tabs share; a page without storage keeps it to itself.
const memory = new Map();

function remember(key) {
  try {
    const stored = localStorage.getItem(key);
    return stored === null ? null : JSON.parse(stored);
  } catch (error) {
    return memory.get(key) ?? null;
  }
}

function keep(key, value) {
  memory.set(key, value);
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch (error) {
    // the memory above will do
  }
}

async function ask(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

const visible = () => document.visibilityState !== "hidden";

export class NotificationBell {
  // ``username``: what the tabs share is per person (a browser may change hands).
  constructor({ username, onClick } = {}) {
    this.seenKey = `hive.notifications.seen.${username}`; // the newest id told
    this.bubbleKey = `hive.notifications.bubble.${username}`; // {items, shown, tickedAt}
    if (!document.getElementById("hive-notifications-style")) {
      document.head.append(el("style", { id: "hive-notifications-style" }, [STYLE]));
    }
    this.badge = el("span", { class: "hive-bell-count", hidden: "" });
    this.element = el("button", { class: "hive-bell", title: "Notifications", onclick: onClick }, [this.badge]);
    this.element.insertAdjacentHTML(
      "afterbegin",
      // Tabler Icons "bell" (outline, MIT), like Fontra's panels.
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M10 5a2 2 0 1 1 4 0a7 7 0 0 1 4 6v3a4 4 0 0 0 2 3h-16a4 4 0 0 0 2 -3v-3a7 7 0 0 1 4 -6"/>' +
        '<path d="M9 17v1a3 3 0 0 0 6 0v-1"/></svg>'
    );
    this.bubble = null;
    this.bubbleIds = null;
    this.hovering = false;
    this.polling = false;
  }

  start() {
    this.poll();
    this.showBubble();
    setInterval(() => this.poll(), POLL_MS);
    setInterval(() => this.tick(), TICK_MS);
    document.addEventListener("visibilitychange", () => {
      this.showBubble();
      if (visible()) this.poll();
    });
    window.addEventListener("storage", (event) => {
      if (event.key === this.bubbleKey || event.key === null) this.showBubble();
    });
  }

  setCount(count) {
    this.badge.hidden = !count;
    this.badge.textContent = count > MAX_BADGE ? `${MAX_BADGE}+` : String(count);
  }

  // How many are unread, and whether there is anything new to tell.
  async poll() {
    if (!visible() || this.polling) return;
    this.polling = true;
    try {
      const { count, latest } = await ask("/api/notifications/count");
      this.setCount(count);
      const seen = remember(this.seenKey);
      if (seen === null) keep(this.seenKey, latest || 0); // what came before is not news
      else if (latest > seen) await this.tell(latest);
    } catch (error) {
      // signed out or away: next time
    } finally {
      this.polling = false;
    }
  }

  async tell(latest) {
    const { notifications } = await ask("/api/notifications");
    // Read again: another tab may have told them while this one waited.
    const seen = remember(this.seenKey) ?? 0;
    keep(this.seenKey, Math.max(seen, latest));
    const fresh = notifications.filter((n) => !n.read && n.id > seen);
    if (!fresh.length) return;
    const earlier = remember(this.bubbleKey)?.items || [];
    const ids = new Set(fresh.map((n) => n.id));
    const items = [...fresh, ...earlier.filter((n) => !ids.has(n.id))].slice(0, MAX_BUBBLE);
    keep(this.bubbleKey, { items, shown: 0 });
    this.showBubble();
  }

  list() {
    return ask("/api/notifications");
  }

  async markRead(body) {
    const answer = await ask("/api/notifications/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    this.setCount(answer.count);
  }

  // A click on a notification: read, and there.
  async open(n) {
    this.closeBubble();
    this.markRead({ id: n.id }).catch(() => {});
    const comments = window.hiveComments; // in the editor of this project: no need to leave
    const issue = comments?.projectName === n.project ? comments.issue(n.issue) : null;
    if (issue) {
      comments.show(issue);
      return;
    }
    const tab = n.glyph ? editorTabName(n.project) : COMMENTS_TAB_PREFIX + n.project;
    window.open(notificationLink(n), tab);
  }

  // --- the bubble --------------------------------------------------------------

  showBubble() {
    const items = remember(this.bubbleKey)?.items || [];
    if (!items.length || !visible()) {
      this.bubble?.remove();
      this.bubble = this.bubbleIds = null;
      this.hovering = false; // no "mouseleave" from what is gone
      return;
    }
    const ids = items.map((n) => n.id).join();
    if (ids === this.bubbleIds) return;
    const others = items.length - 1;
    const close = el("button", { title: "Close", "aria-label": "Close" }, ["×"]);
    close.addEventListener("click", (event) => {
      event.stopPropagation();
      this.closeBubble();
    });
    const bubble = el(
      "div",
      {
        class: "hive-bubble",
        role: "status",
        onclick: () => this.open(items[0]),
        onmouseenter: () => (this.hovering = true),
        onmouseleave: () => (this.hovering = false),
      },
      [
        el("div", { class: "text" }, [
          notificationText(items[0]),
          others ? el("small", {}, [`and ${others} more`]) : null,
        ]),
        close,
      ]
    );
    this.bubble?.remove();
    this.bubble = bubble;
    this.bubbleIds = ids;
    this.hovering = false;
    document.body.append(bubble);
  }

  closeBubble() {
    keep(this.bubbleKey, null);
    this.showBubble();
  }

  // Its time runs only while it can be seen, and not under the mouse. Two
  // windows side by side both tick: one tick counts.
  tick(now = Date.now()) {
    if (!this.bubble || !visible() || this.hovering) return;
    const data = remember(this.bubbleKey);
    if (!data) {
      this.showBubble();
      return;
    }
    if (now - (data.tickedAt || 0) < TICK_MS / 2) return;
    data.tickedAt = now;
    data.shown += TICK_MS;
    if (data.shown >= BUBBLE_MS) this.closeBubble();
    else keep(this.bubbleKey, data);
  }
}
