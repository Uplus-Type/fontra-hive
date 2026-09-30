// Fontra Hive in Fontra's own views (editor, font overview, font info,
// settings, and the project list), without changing Fontra.
//
// Hive serves Fontra's view pages with this module added at the end of
// <body> (see injectHiveScripts in projectmanager.py). It adds, in Fontra's
// top bar and in Fontra's style:
//
//   - the people currently on the same project (an avatar stack; a heartbeat
//     every few seconds tells the server where we are: view, branch, glyph);
//   - the signed-in user's chip, with a menu (name, role, My projects,
//     Sign out);
//   - a "Read only" badge when the role cannot edit (Fontra already shows its
//     lock icon, from isReadOnly());
//   - File › Share…: the project's members and their roles, editable by
//     managers and admins;
//   - the branch pill (⎇ main ▾): the project's branches, who is on which,
//     how each compares with the default branch; switch branch (the same
//     view on the other branch), make a new branch, delete one.
//
// It only uses what Fontra exposes publicly: the top bar element, the
// #fontra-project-name element Fontra adds to it, and the view controller
// on window (editorController, fontOverviewController, fontInfoController)
// for the File menu (makeFontraMenuBar asks it for getFileMenuItems()).
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const HEARTBEAT_MS = 5000;
// hive-api's access cookie lasts 15 minutes: renew it well before.
const REFRESH_MS = 10 * 60 * 1000;
const WAIT_TIMEOUT_MS = 20000;
const AMBER = "#c89222";
const AVATAR_COLORS = ["#5b7fbf", "#3f9a8a", "#b07cc6", "#c0694e", "#6d8f3a", "#8a6fd1", "#c89222"];

// --- helpers --------------------------------------------------------------------

export function initials(name) {
  const words = (name || "?").trim().split(/\s+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[words.length - 1][0] : (words[0] || "?").slice(0, 2);
  return letters.toUpperCase();
}

export function avatarColor(username) {
  let hash = 0;
  for (const ch of username || "") hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

export function describePresence(entry) {
  const where = entry.view === "editor" ? (entry.glyph ? `editing “${entry.glyph}”` : "in the editor")
    : entry.view === "fontoverview" ? "in the font overview"
    : entry.view === "fontinfo" ? "in font info"
    : "viewing";
  const branch = entry.branch && entry.branch !== "main" ? ` · ${entry.branch}` : "";
  return `${entry.name} · ${where}${branch}`;
}

// One entry per person (someone may have several tabs open): the most telling
// one, editing a glyph first.
export function mostSpecificPerPerson(entries, exceptUsername) {
  const rank = (e) => (e.view === "editor" ? (e.glyph ? 3 : 2) : e.view === "fontoverview" ? 1 : 0);
  const best = new Map();
  for (const entry of entries) {
    if (entry.username === exceptUsername) continue;
    const current = best.get(entry.username);
    if (!current || rank(entry) > rank(current)) best.set(entry.username, entry);
  }
  return [...best.values()];
}

// --- branches: pure helpers (exported for the tests) ---------------------------

const BRANCH_ICON =
  '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><g fill="none" ' +
  'stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="4.5" cy="3.5" r="1.7"/>' +
  '<circle cx="4.5" cy="12.5" r="1.7"/><circle cx="11.5" cy="5" r="1.7"/>' +
  '<path d="M4.5 5.2v5.6M11.5 6.7c0 2.8-2.4 3.4-5.6 4.4"/></g></svg>';

// The same characters and rules as the server (gitstore.branch_name_error),
// for an answer while typing; the server has the last word.
export function branchNameProblem(name) {
  if (!name) return "Give the branch a name.";
  if (!/^[A-Za-z0-9._/-]{1,100}$/.test(name)) return "Use letters, digits, “.”, “_”, “-” and “/”.";
  const parts = name.split("/");
  if (parts.some((p) => !p || p.startsWith(".") || p.endsWith(".lock")) || name.includes("..") ||
      name.endsWith(".") || name.startsWith("-") || name === "HEAD") {
    return "Not a valid branch name.";
  }
  if (/^(snapshot|glyph-snapshot|archive)\//.test(name)) return `Branch names cannot start with ${parts[0]}/.`;
  return null;
}

// The URL of the same page on another branch of the project: only the
// "project" parameter changes (the editor keeps its glyph, text, location…).
export function branchURL(href, projectName, branch, defaultBranch) {
  const url = new URL(href);
  url.searchParams.set("project", branch === defaultBranch ? projectName : `${projectName}@${branch}`);
  return url.toString();
}

export function timeAgo(seconds, now = Date.now() / 1000) {
  const d = Math.max(0, now - seconds);
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  if (d < 2 * 86400) return "yesterday";
  if (d < 30 * 86400) return `${Math.floor(d / 86400)} days ago`;
  return new Date(seconds * 1000).toLocaleDateString();
}

// How a branch compares with the default one, in a few words.
export function compareWithDefault(branch, defaultBranch) {
  if (branch.isDefault) return "default branch";
  const { ahead, behind } = branch;
  if (!ahead && !behind) return `same as ${defaultBranch}`;
  if (!ahead) return `nothing new · ${behind} behind ${defaultBranch}`;
  return behind ? `${ahead} ahead · ${behind} behind ${defaultBranch}` : `${ahead} ahead of ${defaultBranch}`;
}

function el(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") element.className = value;
    else if (key === "style") element.style.cssText = value;
    else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) element.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined && child !== false) element.append(child);
  }
  return element;
}

export function avatar(user, size = 26) {
  if (user.avatar) {
    return el("img", {
      class: "avatar",
      src: user.avatar,
      alt: "",
      title: user.name,
      style: `width:${size}px;height:${size}px;object-fit:cover`,
    });
  }
  return el(
    "span",
    {
      class: "avatar",
      title: user.name,
      style: `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.42)}px;background:${avatarColor(user.username)}`,
    },
    [initials(user.name)]
  );
}

// For hive-api answers ({"detail": "…"} on errors, maybe no body on success).
async function fetchOk(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.json()).detail;
    } catch (error) {
      detail = response.statusText;
    }
    throw new Error(typeof detail === "string" && detail ? detail : `Error ${response.status}`);
  }
  return response;
}

// Build a project's file on the server and hand it to the browser as a
// download (a notice while it builds: fonts can take a while).
export async function exportDownload(project, format) {
  const url =
    `/api/hive/projects/${encodeURIComponent(project.name)}/export` +
    `?format=${encodeURIComponent(format || "fontra")}&branch=${encodeURIComponent(project.branch || "main")}`;
  const notice = el("div", { class: "hive-notice" }, [`Preparing ${format}…`]);
  ensureStyle();
  document.body.append(notice);
  try {
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok) throw new Error((await response.text()) || `Error ${response.status}`);
    const disposition = response.headers.get("Content-Disposition") || "";
    const name = /filename="([^"]+)"/.exec(disposition)?.[1] || `font.${format}`;
    const blob = await response.blob();
    const link = el("a", { href: URL.createObjectURL(blob), download: name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 60000);
    notice.remove();
  } catch (error) {
    notice.textContent = `Export failed: ${error.message}`;
    notice.classList.add("error");
    setTimeout(() => notice.remove(), 8000);
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (!response.ok) {
    const error = new Error(`${response.status} ${(await response.text()) || response.statusText}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

// hive-api: exchange the 30-day refresh cookie for a new 15-minute access
// cookie. 409: another tab just did it (its new cookies are already ours).
export async function refreshSession() {
  const response = await fetch("/api/auth/refresh", { method: "POST", credentials: "same-origin" });
  return response.ok || response.status === 409;
}

// "owner/name" -> "/api/projects/owner/name" (hive-api's project routes)
export function hiveApiProjectPath(projectName) {
  return "/api/projects/" + projectName.split("/").map(encodeURIComponent).join("/");
}

function waitFor(test, timeout = WAIT_TIMEOUT_MS) {
  return new Promise((resolve) => {
    const started = Date.now();
    const tick = () => {
      const value = test();
      if (value) resolve(value);
      else if (Date.now() - started > timeout) resolve(null);
      else setTimeout(tick, 100);
    };
    tick();
  });
}

function currentView() {
  const name = location.pathname.split("/").pop().replace(/\.html$/, "");
  return name || "projects";
}

function currentProject() {
  const identifier = new URLSearchParams(location.search).get("project");
  if (!identifier) return null;
  const at = identifier.indexOf("@");
  return at === -1
    ? { identifier, name: identifier, branch: "main" }
    : { identifier, name: identifier.slice(0, at), branch: identifier.slice(at + 1) };
}

function viewController() {
  return window.editorController || window.fontOverviewController || window.fontInfoController || null;
}

// Keys typed in Hive's fields must not reach Fontra's shortcuts (window listener).
function ensureStyle() {
  if (!document.getElementById("hive-views-style")) {
    document.head.append(el("style", { id: "hive-views-style" }, [STYLE]));
  }
}

function isolateKeys(element) {
  for (const type of ["keydown", "keyup", "keypress"]) {
    element.addEventListener(type, (event) => event.stopPropagation());
  }
}

const STYLE = `
  .hive-right { display: flex; align-items: center; gap: 0.7em; margin-right: 0.6em; }
  .hive-right #fontra-project-name { margin-right: 0; }
  .hive-corner { position: fixed; top: 10px; right: 14px; z-index: 300;
                 display: flex; align-items: center; gap: 0.6em; font-family: fontra-ui-regular; }
  .avatar { display: inline-flex; align-items: center; justify-content: center; border-radius: 50%;
            color: white; font-family: fontra-ui-regular; font-weight: 700; flex: none;
            user-select: none; }
  .hive-stack { display: flex; cursor: default; }
  .hive-stack .avatar { margin-left: -5px; box-shadow: 0 0 0 2px var(--top-bar-background-color, #eee); }
  .hive-stack .avatar:first-child { margin-left: 0; }
  .hive-chip { cursor: pointer; border: none; padding: 0; background: none; display: flex; }
  .hive-chip .avatar { box-shadow: 0 0 0 2px transparent; }
  .hive-chip:hover .avatar, .hive-chip.open .avatar { box-shadow: 0 0 0 2px #46f; }
  .hive-badge { position: absolute; left: 50%; transform: translateX(-50%); top: 7px;
                display: flex; align-items: center; gap: 0.35em; font-size: 0.85em; padding: 2px 10px;
                border-radius: 10px; background: rgba(200, 146, 34, 0.16); border: 1px solid rgba(200, 146, 34, 0.45);
                white-space: nowrap; font-family: fontra-ui-regular; }
  .hive-menu { position: fixed; z-index: 1000; min-width: 230px; padding: 5px 0; border-radius: 6px;
               background: var(--ui-element-background-color, white); color: var(--ui-element-foreground-color, black);
               box-shadow: 0 3px 16px #0004; font-family: fontra-ui-regular; font-size: 0.95em; }
  .hive-menu .who { padding: 7px 16px 5px; line-height: 1.25; }
  .hive-menu .who small, .hive-menu .note { display: block; opacity: 0.6; font-size: 0.88em; }
  .hive-menu .note { padding: 3px 16px; }
  .hive-menu a { display: block; padding: 4px 16px; color: inherit; text-decoration: none; cursor: pointer; }
  .hive-menu a:hover { background: #46f; color: white; }
  .hive-menu hr { border: none; border-top: 1px solid var(--horizontal-rule-color, #aaa8); margin: 5px 0; }
  .hive-menu .person { display: flex; align-items: center; gap: 8px; padding: 4px 16px; }
  .hive-backdrop { position: fixed; inset: 0; background: #8888; z-index: 1000; }
  .hive-dialog { position: fixed; left: 50%; top: 70px; transform: translateX(-50%); z-index: 1001;
                 width: min(560px, calc(100vw - 40px)); max-height: calc(100vh - 120px); overflow: auto;
                 background: var(--ui-element-background-color, white); color: var(--ui-element-foreground-color, black);
                 border-radius: 0.5em; box-shadow: 1px 3px 8px #0006; padding: 22px 26px;
                 font-family: fontra-ui-regular; box-sizing: border-box; }
  .hive-dialog h3 { margin: 0 0 12px; font-size: 1.25em; }
  .hive-member { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid #0000000d; }
  .hive-member .who { flex: 1; line-height: 1.2; min-width: 0; }
  .hive-member .who small { display: block; opacity: 0.6; font-size: 0.85em; overflow: hidden; text-overflow: ellipsis; }
  .hive-member .role { font-size: 0.92em; opacity: 0.8; }
  .hive-dialog select, .hive-dialog input { font: inherit; font-size: 0.95em; border: none; border-radius: 0.25em;
                 padding: 5px 8px; background: var(--text-input-background-color, #eee);
                 color: var(--text-input-foreground-color, black); }
  .hive-dialog .remove { border: none; background: none; font-size: 1.1em; opacity: 0.5; cursor: pointer; width: 22px; }
  .hive-dialog .remove:hover { opacity: 1; }
  .hive-dialog .add { display: flex; gap: 8px; margin: 14px 0 4px; }
  .hive-dialog .add select:first-child { flex: 1; }
  .hive-dialog button.pill { font: inherit; border: none; border-radius: 1.2em; padding: 5px 20px; cursor: pointer; }
  .hive-dialog button.blue { background: #46f; color: white; }
  .hive-dialog button.red { background: var(--fontra-red-color, #f11759); color: white; }
  .hive-dialog button:disabled { opacity: 0.4; cursor: default; }
  .hive-dialog .note { opacity: 0.6; font-size: 0.85em; margin: 10px 0 0; }
  .hive-dialog .error { color: var(--fontra-red-color, #d33); font-size: 0.9em; margin-top: 8px; }
  .hive-dialog .hive-member.pending .who { opacity: 0.7; }
  .hive-dialog .add input { flex: 1; min-width: 0; font: inherit; padding: 4px 6px; border-radius: 4px;
    border: 1px solid #8884; background: var(--text-input-background-color, #eee);
    color: var(--text-input-foreground-color, black); }
  .hive-dialog .done { color: #3f9a8a; font-size: 0.9em; margin-top: 8px; }
  .hive-dialog .footer { display: flex; justify-content: flex-end; margin-top: 16px; }
  .hive-notice { position: fixed; bottom: 1.5em; left: 50%; transform: translateX(-50%);
    background: #222; color: white; padding: 0.6em 1.1em; border-radius: 0.6em; z-index: 1000;
    font-family: fontra-ui-regular, sans-serif; box-shadow: 1px 2px 8px #0005; }
  .hive-notice.error { background: var(--fontra-red-color, #f11759); }
  .hive-branch { display: flex; align-items: center; gap: 0.3em; font: inherit; font-family: fontra-ui-regular;
                 font-size: 0.92em; cursor: pointer; border: 1px solid transparent; border-radius: 1em;
                 padding: 2px 9px 2px 7px; background: none; color: inherit; max-width: 16em; }
  .hive-branch span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hive-branch:hover, .hive-branch.open { border-color: #8886; }
  .hive-branch.off-default { background: rgba(200, 146, 34, 0.16); border-color: rgba(200, 146, 34, 0.45); }
  .hive-branch svg { flex: none; opacity: 0.75; }
  .hive-menu.branches { min-width: 300px; max-width: 420px; }
  .hive-menu .title { padding: 5px 16px 3px; font-weight: bold; }
  .hive-menu .branch-row { display: flex; align-items: center; gap: 8px; padding: 4px 8px 4px 12px; cursor: pointer; }
  .hive-menu .branch-row:hover { background: #46f; color: white; }
  .hive-menu .branch-row.current { cursor: default; }
  .hive-menu .branch-row.current:hover { background: none; color: inherit; }
  .hive-menu .branch-row .check { width: 1em; flex: none; text-align: center; }
  .hive-menu .branch-row .text { flex: 1; min-width: 0; line-height: 1.25; }
  .hive-menu .branch-row .text b, .hive-menu .branch-row .text small { display: block; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
  .hive-menu .branch-row .text small { opacity: 0.65; font-size: 0.85em; }
  .hive-menu .branch-row .faces { display: flex; }
  .hive-menu .branch-row .faces .avatar { margin-left: -4px; box-shadow: 0 0 0 1.5px var(--ui-element-background-color, white); }
  .hive-menu .branch-row .delete { border: none; background: none; color: inherit; opacity: 0.45; cursor: pointer;
                 font-size: 1.1em; width: 22px; padding: 0; }
  .hive-menu .branch-row .delete:hover { opacity: 1; }
  .hive-menu .branch-row .delete:disabled { opacity: 0.15; cursor: default; }
  .hive-menu a.disabled { opacity: 0.45; pointer-events: none; }
  .hive-dialog label { display: block; margin: 10px 0 4px; font-size: 0.92em; }
  .hive-dialog label input, .hive-dialog label select { display: block; width: 100%; box-sizing: border-box; margin-top: 4px; }
  .hive-dialog .footer { gap: 8px; }
  .hive-dialog button.plain { background: #8882; color: inherit; }
`;

// --- the Hive UI ------------------------------------------------------------------

export class HiveViews {
  constructor({ me, project, access, view, source = "dev" }) {
    this.me = me;
    this.source = source; // "hive-api", or "dev" (hive-dev-users.json)
    this.project = project;
    this.access = access;
    this.view = view;
    this.client = Math.random().toString(36).slice(2) + Date.now().toString(36);
    this.others = [];
    this.menu = null;
    this.menuOwner = null; // the element that opened this.menu
    this.branches = null; // what /branches answered
  }

  async mount() {
    ensureStyle();
    // Fontra adds #fontra-project-name to the top bar once the project is
    // open; wait for it so that ours sits next to it.
    const topBar = await waitFor(() => {
      const bar = document.querySelector(".top-bar-container");
      if (!bar) return null;
      if (this.project && !bar.querySelector("#fontra-project-name")) return null;
      return bar;
    }, this.view === "projects" ? 0 : WAIT_TIMEOUT_MS);

    this.stack = el("span", { class: "hive-stack" });
    this.chip = el("button", { class: "hive-chip", title: this.me.name, onclick: (e) => this.toggleUserMenu(e) }, [
      avatar(this.me, 26),
    ]);
    if (topBar) {
      const right = el("div", { class: "hive-right" });
      const projectName = topBar.querySelector("#fontra-project-name");
      topBar.append(right);
      right.append(this.stack);
      if (projectName) right.append(projectName); // moved next to ours
      if (this.project) right.append(this.makeBranchPill(projectName));
      right.append(this.chip);
      if (this.access && this.access.role && !this.access.capabilities.includes("edit")) {
        topBar.append(
          el("span", { class: "hive-badge", title: "Your role on this project cannot edit" }, [
            `Read only · ${this.access.role}`,
          ])
        );
      }
    } else {
      document.body.append(el("div", { class: "hive-corner" }, [this.stack, this.chip]));
    }

    if (this.project) {
      this.installFileMenu();
      this.installExport();
      this.heartbeat();
      this.timer = setInterval(() => this.heartbeat(), HEARTBEAT_MS);
    }
    if (this.source === "hive-api") this.keepSessionAlive();
    document.addEventListener("click", (event) => {
      if (this.menu && !this.menu.contains(event.target) && !this.menuOwner?.contains(event.target)) {
        this.closeMenu();
      }
    });
  }

  keepSessionAlive() {
    this.lastRefresh = Date.now();
    const renew = async () => {
      this.lastRefresh = Date.now();
      await refreshSession();
    };
    this.refreshTimer = setInterval(renew, REFRESH_MS);
    // Timers sleep with the laptop: renew on coming back if it is late.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && Date.now() - this.lastRefresh > REFRESH_MS) renew();
    });
  }

  // --- presence --------------------------------------------------------------

  currentGlyph() {
    return window.editorController?.sceneSettings?.selectedGlyphName || null;
  }

  async heartbeat() {
    try {
      const data = await api(`/api/hive/projects/${encodeURIComponent(this.project.name)}/presence`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client: this.client,
          view: this.view,
          branch: this.project.branch,
          glyph: this.currentGlyph(),
        }),
      });
      this.others = data.others || [];
      this.renderPresence();
    } catch (error) {
      // network hiccup: next heartbeat
    }
  }

  renderPresence() {
    // One avatar per person (several tabs of the same person count once).
    const people = new Map();
    for (const entry of this.others) {
      if (entry.username === this.me.username) continue;
      if (!people.has(entry.username)) people.set(entry.username, []);
      people.get(entry.username).push(entry);
    }
    this.stack.replaceChildren();
    for (const [username, entries] of people) {
      const face = avatar({ username, name: entries[0].name }, 24);
      face.title = entries.map(describePresence).join("\n");
      this.stack.append(face);
    }
    this.stack.title = people.size ? "Also on this project" : "";
  }

  // --- user menu -------------------------------------------------------------

  toggleUserMenu(event) {
    event.stopPropagation();
    if (this.menu) {
      const wasOurs = this.menuOwner === this.chip;
      this.closeMenu();
      if (wasOurs) return;
    }
    const rect = this.chip.getBoundingClientRect();
    const others = mostSpecificPerPerson(this.others, this.me.username);
    const role = this.access?.role;
    this.menu = el("div", { class: "hive-menu" }, [
      el("div", { class: "who" }, [
        el("b", {}, [this.me.name]),
        el("small", {}, [[this.me.username, this.me.email, role].filter(Boolean).join(" · ")]),
      ]),
      others.length ? el("hr") : null,
      others.length ? el("div", { class: "note" }, ["Also here"]) : null,
      ...others.map((o) =>
        el("div", { class: "person" }, [avatar(o, 20), el("span", {}, [describePresence(o)])])
      ),
      el("hr"),
      el("a", { href: "/" }, ["My projects"]),
      this.project ? el("a", { onclick: () => { this.closeMenu(); this.openShare(); } }, ["Share…"]) : null,
      el("hr"),
      el("a", { href: "/hive/logout" }, ["Sign out"]), // a page that signs out, then goes home
    ]);
    this.showMenu(this.chip, rect);
  }

  showMenu(owner, rect) {
    this.menu.style.top = `${rect.bottom + 4}px`;
    this.menu.style.right = `${Math.max(8, window.innerWidth - rect.right)}px`;
    this.menuOwner = owner;
    owner.classList.add("open");
    document.body.append(this.menu);
  }

  closeMenu() {
    this.menu?.remove();
    this.menu = null;
    this.menuOwner?.classList.remove("open");
    this.menuOwner = null;
  }

  // --- branches ------------------------------------------------------------------

  branchesPath() {
    return `/api/hive/projects/${encodeURIComponent(this.project.name)}/branches`;
  }

  // ⎇ main ▾, next to the project name. Coloured off the default branch.
  makeBranchPill(projectNameElement) {
    this.branchPill = el("button", { class: "hive-branch", title: "Branches of this project" });
    this.branchLabel = el("span", {}, [this.project.branch]);
    const icon = el("span");
    icon.innerHTML = BRANCH_ICON;
    this.branchPill.append(icon.firstChild, this.branchLabel, el("small", {}, ["▾"]));
    this.branchPill.addEventListener("click", (event) => this.toggleBranchMenu(event));
    // Fontra's project name says "Name · branch" off the default branch: the
    // pill says it now.
    const suffix = ` · ${this.project.branch}`;
    if (projectNameElement?.textContent.endsWith(suffix)) {
      projectNameElement.textContent = projectNameElement.textContent.slice(0, -suffix.length);
    }
    this.loadBranches();
    return this.branchPill;
  }

  async loadBranches() {
    try {
      this.branches = await api(this.branchesPath());
    } catch (error) {
      return null;
    }
    // A URL without "@branch" is on the default branch, whatever its name.
    if (!this.project.identifier.includes("@")) this.project.branch = this.branches.default;
    this.branchLabel.textContent = this.project.branch;
    this.branchPill.classList.toggle("off-default", this.project.branch !== this.branches.default);
    return this.branches;
  }

  gotoBranch(branch) {
    location.href = branchURL(location.href, this.project.name, branch, this.branches.default);
  }

  async toggleBranchMenu(event) {
    event.stopPropagation();
    if (this.menu) {
      const wasOurs = this.menuOwner === this.branchPill;
      this.closeMenu();
      if (wasOurs) return;
    }
    const rect = this.branchPill.getBoundingClientRect();
    const data = (await this.loadBranches()) || this.branches;
    this.closeMenu(); // another one may have opened meanwhile
    if (!data) return;
    const people = new Map(); // branch -> people on it (one avatar each)
    for (const entry of mostSpecificPerPerson(this.others, this.me.username)) {
      const branch = entry.branch || data.default;
      if (!people.has(branch)) people.set(branch, []);
      people.get(branch).push(entry);
    }
    const rows = data.branches.map((b) => {
      const current = b.name === this.project.branch;
      const mayDelete = !b.isDefault && (data.can.delete || (data.can.create && b.createdBy === data.you));
      const deleteButton = mayDelete
        ? el("button", {
            class: "delete",
            title: current ? "Switch to another branch to delete this one"
              : b.open ? "Someone has this branch open" : `Delete ${b.name}…`,
            disabled: current || b.open ? "" : null,
            onclick: (e) => {
              e.stopPropagation();
              this.closeMenu();
              this.openDeleteBranch(b);
            },
          }, ["×"])
        : el("span", { style: "width:22px" });
      const faces = el("span", { class: "faces" }, (people.get(b.name) || []).map((p) => {
        const face = avatar(p, 18);
        face.title = describePresence(p);
        return face;
      }));
      const who = b.author ? ` · ${b.author}` : "";
      return el("div", {
        class: `branch-row${current ? " current" : ""}`,
        "data-branch": b.name,
        title: b.message,
        onclick: current ? null : () => this.gotoBranch(b.name),
      }, [
        el("span", { class: "check" }, [current ? "✓" : ""]),
        el("span", { class: "text" }, [
          el("b", {}, [b.name]),
          el("small", {}, [`${compareWithDefault(b, data.default)} · ${timeAgo(b.time)}${who}`]),
        ]),
        faces,
        deleteButton,
      ]);
    });
    this.menu = el("div", { class: "hive-menu branches" }, [
      el("div", { class: "title" }, ["Branches"]),
      ...rows,
      el("hr"),
      el("a", {
        class: data.can.create ? "" : "disabled",
        title: data.can.create ? "" : "Your role cannot make branches",
        onclick: () => {
          this.closeMenu();
          this.openNewBranch();
        },
      }, ["New branch…"]),
    ]);
    this.showMenu(this.branchPill, rect);
  }

  // A small dialog of ours (Fontra's dialogs are not reachable from here).
  dialog(title) {
    ensureStyle();
    const backdrop = el("div", { class: "hive-backdrop", onclick: () => close() });
    const dialog = el("div", { class: "hive-dialog", role: "dialog" }, [el("h3", {}, [title])]);
    isolateKeys(dialog);
    const close = () => {
      backdrop.remove();
      dialog.remove();
      document.removeEventListener("keydown", onKey, true);
    };
    const onKey = (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
      }
    };
    document.addEventListener("keydown", onKey, true);
    document.body.append(backdrop, dialog);
    return { dialog, close };
  }

  async openNewBranch() {
    const data = this.branches;
    const { dialog, close } = this.dialog("New branch");
    this.branchDialog = dialog;
    const name = el("input", { "aria-label": "Branch name", placeholder: "e.g. bold-extension, ana/italic",
                               maxlength: "100", autocomplete: "off", spellcheck: "false" });
    const from = el("select", { "aria-label": "Start from" }, [
      el("option", { value: this.project.branch }, [`${this.project.branch}, as it is now`]),
      data && data.default !== this.project.branch
        ? el("option", { value: data.default }, [`${data.default}, as it is now`])
        : null,
    ]);
    const error = el("div", { class: "error" });
    const create = el("button", { class: "pill blue", disabled: "" }, ["Create"]);
    const submit = async () => {
      const problem = branchNameProblem(name.value.trim());
      if (problem) {
        error.textContent = problem;
        return;
      }
      create.disabled = true;
      error.textContent = "";
      try {
        const query = new URLSearchParams({ name: name.value.trim(), from: from.value });
        const created = await api(`${this.branchesPath()}?${query}`, { method: "POST" });
        close();
        this.gotoBranch(created.branch.name);
      } catch (e) {
        error.textContent = e.message.replace(/^\d+ /, "");
        create.disabled = false;
      }
    };
    name.addEventListener("input", () => {
      const problem = name.value.trim() ? branchNameProblem(name.value.trim()) : null;
      error.textContent = problem || "";
      create.disabled = !name.value.trim() || !!problem;
    });
    name.addEventListener("keydown", (e) => e.key === "Enter" && submit());
    create.addEventListener("click", submit);
    dialog.append(
      el("p", { class: "note", style: "margin-top:0" }, [
        `A branch is a copy of the font to try things out without changing ${data?.default || "main"}. ` +
          "Its changes can be merged back later.",
      ]),
      el("label", {}, ["Name", name]),
      el("label", {}, ["Start from", from]),
      error,
      el("div", { class: "footer" }, [el("button", { class: "pill plain", onclick: close }, ["Cancel"]), create])
    );
    name.focus();
    // Snapshots of the current branch are good starting points too.
    try {
      const query = new URLSearchParams({ branch: this.project.branch });
      const { snapshots } = await api(
        `/api/hive/projects/${encodeURIComponent(this.project.name)}/snapshots?${query}`
      );
      for (const snapshot of snapshots || []) {
        from.append(el("option", { value: `snapshot/${snapshot.name}` }, [
          `Snapshot “${snapshot.title}” · ${timeAgo(snapshot.time)}`,
        ]));
      }
    } catch (e) {
      // no snapshots: the branches are enough
    }
  }

  openDeleteBranch(branch) {
    const data = this.branches;
    const { dialog, close } = this.dialog(`Delete the branch “${branch.name}”?`);
    this.branchDialog = dialog;
    const error = el("div", { class: "error" });
    const remove = el("button", { class: "pill red" }, ["Delete"]);
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        const query = new URLSearchParams({ branch: branch.name });
        await api(`${this.branchesPath()}?${query}`, { method: "DELETE" });
        close();
        await this.loadBranches();
      } catch (e) {
        error.textContent = e.message.replace(/^\d+ /, "");
        remove.disabled = false;
      }
    });
    dialog.append(
      el("p", {}, [
        branch.ahead
          ? `${branch.ahead} of its changes are not in ${data.default}. They stay in the project's history ` +
            `(as “archive/${branch.name}”), but the branch will not be listed any more.`
          : `Everything it contains is already in ${data.default}.`,
      ]),
      error,
      el("div", { class: "footer" }, [el("button", { class: "pill plain", onclick: close }, ["Cancel"]), remove])
    );
  }

  // --- File › Share… ---------------------------------------------------------

  installFileMenu() {
    waitFor(viewController).then((controller) => {
      if (!controller || controller.getFileMenuItems) return;
      controller.getFileMenuItems = () => [
        { title: "Share…", callback: () => this.openShare() },
        { title: "-" },
        ...defaultFileMenuItems(controller),
      ];
    });
  }

  // --- File › Export as: a download ---------------------------------------------

  // Fontra's "Export as" asks the server to write a file (Fontra Pak opens a
  // save dialog); in a browser it must be a download. The formats come from
  // Hive's export manager; the choice becomes a GET of …/export.
  installExport() {
    waitFor(() => viewController()?.fontController).then((fontController) => {
      if (!fontController) return;
      fontController.exportAs = (options) => exportDownload(this.project, options?.format);
    });
  }

  async openShare() {
    if (this.source === "hive-api") return this.openShareHiveApi();
    const backdrop = el("div", { class: "hive-backdrop", onclick: () => close() });
    const dialog = el("div", { class: "hive-dialog", role: "dialog" });
    isolateKeys(dialog);
    const close = () => {
      backdrop.remove();
      dialog.remove();
      document.removeEventListener("keydown", onKey, true);
    };
    const onKey = (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
      }
    };
    document.addEventListener("keydown", onKey, true);
    document.body.append(backdrop, dialog);
    this.shareDialog = dialog;
    const base = `/api/hive/projects/${encodeURIComponent(this.project.name)}/members`;
    const render = (data, error) => {
      dialog.replaceChildren(el("h3", {}, [`Share “${this.project.name}”`]));
      for (const m of data.members) {
        const isYou = m.username === data.you;
        const editable = data.canManage && m.via === "collaborator";
        const roleCell = editable
          ? el(
              "select",
              {
                "aria-label": `Role of ${m.name}`,
                onchange: (e) => save({ username: m.username, role: e.target.value }),
              },
              data.roles.map((r) => el("option", { value: r, selected: r === m.role ? "" : null }, [r]))
            )
          : el("span", { class: "role", title: m.via === "collaborator" ? "" : `Role from: ${m.via}` }, [
              m.via === "collaborator" ? m.role : `${m.role} · ${m.via}`,
            ]);
        dialog.append(
          el("div", { class: "hive-member", "data-username": m.username }, [
            avatar(m, 30),
            el("div", { class: "who" }, [
              `${m.name}${isYou ? " (you)" : ""}`,
              el("small", {}, [[m.username, m.email].filter(Boolean).join(" · ")]),
            ]),
            roleCell,
            editable
              ? el("button", {
                  class: "remove",
                  title: `Remove ${m.name}`,
                  onclick: () => save({ username: m.username, role: null }),
                }, ["×"])
              : el("span", { style: "width:22px" }),
          ])
        );
      }
      if (data.canManage) {
        const who = el("select", { "aria-label": "Person to add" }, [
          el("option", { value: "" }, [data.users.length ? "Add a person…" : "Everyone is already a member"]),
          ...data.users.map((u) => el("option", { value: u.username }, [`${u.name} (${u.username})`])),
        ]);
        const role = el("select", { "aria-label": "Role" }, data.roles.map((r) =>
          el("option", { value: r, selected: r === "designer" ? "" : null }, [r])));
        const add = el("button", { class: "pill blue", disabled: "" }, ["Add"]);
        who.addEventListener("change", () => (add.disabled = !who.value));
        add.addEventListener("click", () => save({ username: who.value, role: role.value }));
        dialog.append(el("div", { class: "add" }, [who, role, add]));
        dialog.append(el("p", { class: "note" }, [
          "Development server: people are added from hive-dev-users.json. Invitations by email come with hive-api.",
        ]));
      } else {
        dialog.append(el("p", { class: "note" }, ["Managers and admins can add people and change roles."]));
      }
      if (error) dialog.append(el("div", { class: "error" }, [error]));
      dialog.append(el("div", { class: "footer" }, [el("button", { class: "pill red", onclick: close }, ["Done"])]));
    };
    const save = async (change) => {
      try {
        render(await api(base, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(change),
        }));
      } catch (error) {
        render(await api(base), `Could not change the members (${error.message}).`);
      }
    };
    try {
      render(await api(base));
    } catch (error) {
      dialog.replaceChildren(el("h3", {}, ["Share"]), el("div", { class: "error" }, [error.message]));
    }
  }

  // Share… with hive-api (the dialog is shared with Hive's home page).
  openShareHiveApi() {
    this.shareDialog = openShareDialog(this.project.name);
  }
}


// People and invitations of a hive-api project: who has which role (and
// from where), change roles up to one's own, invite by username or email,
// cancel invitations. Returns the dialog element (closed with Escape, the
// backdrop or Done; onClose is then called).
export function openShareDialog(projectName, { onClose } = {}) {
  ensureStyle();
  const backdrop = el("div", { class: "hive-backdrop", onclick: () => close() });
  const dialog = el("div", { class: "hive-dialog", role: "dialog" });
  isolateKeys(dialog);
  const close = () => {
    onClose?.();
    backdrop.remove();
    dialog.remove();
    document.removeEventListener("keydown", onKey, true);
  };
  const onKey = (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
    }
  };
  document.addEventListener("keydown", onKey, true);
  document.body.append(backdrop, dialog);
  const base = hiveApiProjectPath(projectName);
  const json = (method, body) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const load = async () => {
    const data = await api(`${base}/members`);
    data.invitations = data.canManage ? (await api(`${base}/invitations`)).invitations : [];
    return data;
  };
  const act = async (path, options, done) => {
    try {
      await fetchOk(path, options);
      render(await load(), null, done);
    } catch (error) {
      render(await load(), error.message);
    }
  };
  const render = (data, error, done) => {
    dialog.replaceChildren(el("h3", {}, [`Share “${projectName}”`]));
    for (const m of data.members) {
      const isYou = m.username === data.you;
      const editable = data.canManage && m.via === "collaborator" && data.roles.includes(m.role);
      const roleCell = editable
        ? el(
            "select",
            {
              "aria-label": `Role of ${m.name}`,
              onchange: (e) => act(`${base}/collaborators/${encodeURIComponent(m.username)}`, json("PUT", { role: e.target.value })),
            },
            data.roles.map((r) => el("option", { value: r, selected: r === m.role ? "" : null }, [r]))
          )
        : el("span", { class: "role", title: m.via === "collaborator" ? "" : `Role from: ${m.via}` }, [
            m.via === "collaborator" ? m.role : `${m.role} · ${m.via}`,
          ]);
      const canRemove = m.via === "collaborator" && (editable || isYou);
      dialog.append(
        el("div", { class: "hive-member", "data-username": m.username }, [
          avatar(m, 30),
          el("div", { class: "who" }, [
            `${m.name}${isYou ? " (you)" : ""}`,
            el("small", {}, [[m.username, m.email].filter(Boolean).join(" · ")]),
          ]),
          roleCell,
          canRemove
            ? el("button", {
                class: "remove",
                title: isYou ? "Leave this project" : `Remove ${m.name}`,
                onclick: () => act(`${base}/collaborators/${encodeURIComponent(m.username)}`, { method: "DELETE" }),
              }, ["×"])
            : el("span", { style: "width:22px" }),
        ])
      );
    }
    for (const inv of data.invitations) {
      const who = inv.invitee ? inv.invitee.name : inv.email;
      dialog.append(
        el("div", { class: "hive-member pending", "data-invitation": inv.id }, [
          avatar({ username: inv.invitee?.username || inv.email, name: who }, 30),
          el("div", { class: "who" }, [who, el("small", {}, [`invited · ${inv.role}`])]),
          el("span", { class: "role" }, ["pending"]),
          el("button", {
            class: "remove",
            title: `Cancel the invitation of ${who}`,
            onclick: () => act(`${base}/invitations/${inv.id}`, { method: "DELETE" }),
          }, ["×"]),
        ])
      );
    }
    if (data.canManage) {
      const who = el("input", { "aria-label": "Username or email", placeholder: "Username or email" });
      const role = el("select", { "aria-label": "Role" }, data.roles.map((r) =>
        el("option", { value: r, selected: r === "designer" ? "" : null }, [r])));
      const add = el("button", { class: "pill blue", disabled: "" }, ["Invite"]);
      who.addEventListener("input", () => (add.disabled = !who.value.trim()));
      const invite = () => {
        const value = who.value.trim();
        if (!value) return;
        const body = value.includes("@") ? { email: value, role: role.value } : { username: value, role: role.value };
        act(`${base}/invitations`, json("POST", body), `Invitation sent to ${value}.`);
      };
      add.addEventListener("click", invite);
      who.addEventListener("keydown", (e) => e.key === "Enter" && invite());
      dialog.append(el("div", { class: "add" }, [who, role, add]));
      dialog.append(el("p", { class: "note" }, [
        "People who already have a Hive account, by username or by the address of their account. " +
          "They receive an email with a link, valid 7 days, and join when they accept.",
      ]));
    } else {
      dialog.append(el("p", { class: "note" }, ["Managers and admins can invite people and change roles."]));
    }
    if (done) dialog.append(el("div", { class: "done" }, [done]));
    if (error) dialog.append(el("div", { class: "error" }, [error]));
    dialog.append(el("div", { class: "footer" }, [el("button", { class: "pill red", onclick: close }, ["Done"])]));
  };
  load().then(
    (data) => render(data),
    (error) => dialog.replaceChildren(el("h3", {}, ["Share"]), el("div", { class: "error" }, [error.message]))
  );
  return dialog;
}

// The File menu Fontra shows when a view has no getFileMenuItems (copied from
// fontra-menus.js, which a page script cannot import): "Export as" when the
// project manager offers formats, else disabled New / Open.
export function defaultFileMenuItems(controller) {
  const formats = controller.fontController?.backendInfo?.projectManagerFeatures?.["export-as"] || [];
  if (formats.length) {
    return [
      {
        title: "Export as",
        getItems: () => formats.map((format) => ({ actionIdentifier: `action.export-as.${format}` })),
      },
    ];
  }
  return [
    { title: "New", enabled: () => false, callback: () => {} },
    { title: "Open", enabled: () => false, callback: () => {} },
  ];
}

export async function start() {
  let me;
  try {
    me = await api("/api/hive/me");
  } catch (error) {
    // hive-api: the 15-minute cookie may have lapsed (a sleeping laptop).
    if (error.status !== 401 || !(await refreshSession())) return null;
    try {
      me = await api("/api/hive/me");
    } catch (error2) {
      return null;
    }
  }
  if (!me.accounts || !me.user) return null; // no accounts: nothing to show
  if (me.source === "hive-api") {
    try {
      const full = (await api("/api/me")).user; // email and photo, from hive-api
      Object.assign(me.user, { name: full.name, email: full.email, avatar: full.avatar });
    } catch (error) {
      // keep what the token says
    }
  }
  const project = currentProject();
  let access = null;
  if (project) {
    try {
      access = await api(`/api/hive/projects/${encodeURIComponent(project.name)}/access`);
    } catch (error) {
      access = null;
    }
  }
  const hive = new HiveViews({ me: me.user, project, access, view: currentView(), source: me.source });
  await hive.mount();
  window.hiveViews = hive;
  return hive;
}

// --- labels Fontra translated too early ---------------------------------------

// Some of Fontra's labels are translated when its modules load (the font
// overview's "Group by" choices, in glyph-organizer.js), while the language
// strings are still on their way: over the network they arrive later than on
// a local Fontra Pak, and the keys show ("glyph-organizing.group-by.script").
// We load the same strings and replace any text that is exactly a known key,
// in the page and in the components' shadow roots, now and as they render.
// To be proposed upstream (translate when rendering); harmless once fixed.

const KEY_PATTERN = /^[a-z][a-z0-9-]*(\.[a-z0-9-]+)+$/;

export function translateLeftoverKeys(root, strings, onShadowRoot) {
  let count = 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  for (let node = walker.currentNode; node; node = walker.nextNode()) {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.nodeValue.trim();
      if (KEY_PATTERN.test(text) && typeof strings[text] === "string") {
        node.nodeValue = node.nodeValue.replace(text, strings[text]);
        count++;
      }
    } else if (node.shadowRoot) {
      onShadowRoot?.(node.shadowRoot);
      count += translateLeftoverKeys(node.shadowRoot, strings, onShadowRoot);
    }
  }
  return count;
}

async function languageStrings() {
  let language = "en";
  try {
    language = JSON.parse(localStorage.getItem("fontra-language-language")) || "en";
  } catch (error) {
    // no setting: English
  }
  if (!/^[A-Za-z]{2,3}(-[A-Za-z]{2,4})?$/.test(language)) language = "en";
  for (const locale of new Set([language, "en"])) {
    try {
      return (await import(`/lang/${locale}.js`)).strings;
    } catch (error) {
      // try English
    }
  }
  return null;
}

export async function fixEarlyTranslations() {
  const strings = await languageStrings();
  if (!strings || !document.body) return null;
  const observed = new WeakSet();
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) translateLeftoverKeys(node, strings, observe);
    }
  });
  const observe = (root) => {
    if (observed.has(root)) return;
    observed.add(root);
    observer.observe(root, { childList: true, subtree: true });
  };
  observe(document.body);
  translateLeftoverKeys(document.body, strings, observe);
  return observer;
}

if (!window.__hiveViewsNoAutoStart) {
  start();
  fixEarlyTranslations();
}
