// Fontra Hive editor plug-in: comments on glyphs ("post-its").
//
// A comment is a numbered topic (#12) pinned to a point of one source of a
// glyph, with a thread of replies, open or resolved. They are kept by the
// Hive server in the project's git repository, outside the font's history
// (refs/hive/comments), and read and written through
// /api/hive/projects/<project>/comments.
//
// What this module adds to the editor, with its public objects only:
//   - a "Comment" edit tool: click in the glyph being edited to pin a new
//     comment there, on the source being edited;
//   - a visualization layer ("Hive: comments", in the View menu) that draws
//     a numbered pin at each comment of the glyphs on the canvas: solid on
//     the source it was written on, pale on the glyph's other sources;
//   - the post-it itself, an HTML card over the canvas that follows zoom and
//     scrolling: the thread, a reply box, Resolve / Reopen, edit and delete.
//     A click on a pin opens it, whatever the tool; dragging a pin moves it;
//   - a "Comments" sidebar panel: the selected glyph's topics (all the
//     project's when no glyph is selected); a click shows one on the canvas.
//
// Who may do what is decided by the server; the "can" part of its answer
// only hides what would be refused.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const POLL_INTERVAL_MS = 3000;
const LAYER_ID = "hive.comments";
const TOOL_ID = "hive-comment-tool";
const PIN_RADIUS = 10; // screen pixels
const PIN_LIFT = 16; // the pin's circle sits this far above the point it marks
const DRAG_THRESHOLD = 3;
const PALE_ALPHA = 0.35;
const TOAST_MS = 3500;

const COLORS = {
  light: { open: "#c89222", resolved: "#8a8a8a", text: "#ffffff", ring: "#1c1c1c" },
  dark: { open: "#e6be5a", resolved: "#9a9a9a", text: "#1c1c1c", ring: "#ffffff" },
};

const CARD_STYLES = `
  :host {
    --hive-accent: #c89222;
    --card-bg: #fffdf5;
    --card-fg: #1c1c1c;
    --card-muted: rgba(0, 0, 0, 0.55);
    --card-line: rgba(0, 0, 0, 0.12);
    --input-bg: #ffffff;
    position: absolute;
    inset: 0;
    pointer-events: none;
    overflow: hidden;
    z-index: 5;
    font-family: fontra-ui-regular, -apple-system, sans-serif;
    font-size: 13px;
  }
  @media (prefers-color-scheme: dark) {
    :host {
      --hive-accent: #e6be5a;
      --card-bg: #2b2a26;
      --card-fg: #eeeeee;
      --card-muted: rgba(255, 255, 255, 0.55);
      --card-line: rgba(255, 255, 255, 0.14);
      --input-bg: #1f1f1f;
    }
  }
  :host-context(.light-theme) {
    --hive-accent: #c89222;
    --card-bg: #fffdf5;
    --card-fg: #1c1c1c;
    --card-muted: rgba(0, 0, 0, 0.55);
    --card-line: rgba(0, 0, 0, 0.12);
    --input-bg: #ffffff;
  }
  :host-context(.dark-theme) {
    --hive-accent: #e6be5a;
    --card-bg: #2b2a26;
    --card-fg: #eeeeee;
    --card-muted: rgba(255, 255, 255, 0.55);
    --card-line: rgba(255, 255, 255, 0.14);
    --input-bg: #1f1f1f;
  }
  .card {
    position: absolute;
    width: 280px;
    max-height: 60vh;
    display: flex;
    flex-direction: column;
    pointer-events: auto;
    background: var(--card-bg);
    color: var(--card-fg);
    border-radius: 8px;
    border-top: 4px solid var(--hive-accent);
    box-shadow: 0 4px 18px rgba(0, 0, 0, 0.28);
  }
  .card.resolved {
    border-top-color: #8a8a8a;
  }
  .card-header {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 7px 8px 6px 10px;
    border-bottom: 1px solid var(--card-line);
  }
  .card-header .number {
    font-weight: bold;
  }
  .card-header .where {
    color: var(--card-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
    flex: 1;
  }
  .card-header .state {
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 8px;
    background: var(--card-line);
  }
  button {
    font: inherit;
    color: inherit;
    cursor: pointer;
    border: 1px solid var(--card-line);
    background: transparent;
    border-radius: 5px;
    padding: 2px 8px;
  }
  button:hover {
    background: var(--card-line);
  }
  button.primary {
    background: var(--hive-accent);
    border-color: var(--hive-accent);
    color: #1c1c1c;
  }
  button:disabled {
    opacity: 0.45;
    cursor: default;
  }
  button.icon {
    border: none;
    padding: 0 5px;
    font-size: 15px;
    line-height: 1;
  }
  .svg-icon {
    display: inline-flex;
    width: 16px;
    height: 16px;
    vertical-align: middle;
  }
  .svg-icon svg {
    width: 100%;
    height: 100%;
  }
  .link {
    border: none;
    padding: 0;
    color: var(--card-muted);
    font-size: 11px;
    background: none;
  }
  .link:hover {
    background: none;
    text-decoration: underline;
  }
  .messages {
    overflow-y: auto;
    padding: 4px 10px;
  }
  .message {
    padding: 6px 0;
  }
  .message + .message {
    border-top: 1px solid var(--card-line);
  }
  .message .meta {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 2px;
  }
  .avatar {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    font-weight: bold;
    color: #fff;
    flex: none;
  }
  .message .who {
    font-weight: bold;
  }
  .message .when {
    color: var(--card-muted);
    font-size: 11px;
  }
  .message .actions {
    margin-left: auto;
    display: flex;
    gap: 8px;
  }
  .message .text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.35;
  }
  .compose {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 8px 10px 10px;
    border-top: 1px solid var(--card-line);
  }
  .compose.first {
    border-top: none;
  }
  textarea {
    font: inherit;
    color: inherit;
    background: var(--input-bg);
    border: 1px solid var(--card-line);
    border-radius: 5px;
    padding: 5px 6px;
    resize: vertical;
    min-height: 2.6em;
    max-height: 12em;
  }
  textarea:focus {
    outline: 2px solid var(--hive-accent);
    outline-offset: -1px;
  }
  .compose .row {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .compose .row .hint {
    color: var(--card-muted);
    font-size: 11px;
    margin-right: auto;
  }
  .error {
    color: #d13a3a;
    font-size: 12px;
    padding: 0 10px 6px;
  }
  .toast {
    position: absolute;
    left: 50%;
    bottom: 24px;
    transform: translateX(-50%);
    max-width: 70%;
    padding: 8px 14px;
    border-radius: 8px;
    background: rgba(30, 30, 30, 0.9);
    color: #fff;
    pointer-events: none;
  }
`;

const PANEL_STYLES = `
  :host {
    --hive-accent: #c89222;
    display: block;
    height: 100%;
    font-family: fontra-ui-regular, sans-serif;
    color: var(--ui-element-foreground-color, inherit);
  }
  @media (prefers-color-scheme: dark) {
    :host { --hive-accent: #e6be5a; }
  }
  :host-context(.light-theme) { --hive-accent: #c89222; }
  :host-context(.dark-theme) { --hive-accent: #e6be5a; }
  .panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    gap: 0.5em;
    padding: 0.8em 0.8em 0.6em;
    box-sizing: border-box;
  }
  .header {
    display: flex;
    align-items: baseline;
    gap: 0.5em;
  }
  .header .title {
    font-weight: bold;
  }
  .header .subtitle {
    opacity: 0.7;
    font-size: 0.9em;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }
  .header label {
    margin-left: auto;
    font-size: 0.85em;
    white-space: nowrap;
    opacity: 0.8;
  }
  .hint {
    font-size: 0.85em;
    opacity: 0.6;
  }
  .list {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 0.35em;
  }
  .group-title {
    font-size: 0.8em;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    opacity: 0.55;
    margin-top: 0.5em;
  }
  .issue {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.1em 0.5em;
    padding: 0.4em 0.5em;
    border-radius: 0.4em;
    cursor: pointer;
    background: rgba(128, 128, 128, 0.08);
  }
  .issue:hover {
    background: rgba(128, 128, 128, 0.18);
  }
  .issue.current {
    outline: 1.5px solid var(--hive-accent);
  }
  .issue .badge {
    grid-row: span 2;
    align-self: start;
    min-width: 1.7em;
    height: 1.7em;
    border-radius: 0.85em;
    padding: 0 0.3em;
    box-sizing: border-box;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.8em;
    font-weight: bold;
    background: var(--hive-accent);
    color: #1c1c1c;
  }
  .issue.resolved .badge {
    background: #8a8a8a;
    color: #fff;
  }
  .issue .text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .issue .meta {
    font-size: 0.8em;
    opacity: 0.6;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
`;

// --- small helpers ------------------------------------------------------------

function el(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (key === "class") element.className = value;
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (key in element && typeof value !== "string") element[key] = value;
    else element.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [children].flat()) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

// "project@branch", or "project" alone for the project's default branch
// (branch undefined: the server knows which one it is).
function parseProjectIdentifier(identifier) {
  const at = identifier.indexOf("@");
  return at === -1
    ? { name: identifier, branch: undefined }
    : { name: identifier.slice(0, at), branch: identifier.slice(at + 1) };
}

export function avatarColor(username) {
  let hash = 0;
  for (const c of username || "?") hash = (hash * 31 + c.codePointAt(0)) >>> 0;
  return `hsl(${hash % 360}, 45%, 45%)`;
}

function initials(person) {
  const name = (person?.name || person?.username || "?").trim();
  const parts = name.split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] || "?") + (parts.length > 1 ? parts.at(-1)[0] : "")).toUpperCase();
}

export function relativeTime(iso, now = Date.now()) {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} d ago`;
  const date = new Date(then);
  const sameYear = date.getFullYear() === new Date(now).getFullYear();
  return date.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

function drawArguments(args) {
  // Fontra calls draw({context, positionedGlyph, parameters, model, controller})
  // since fontra/fontra#2785, and draw(context, positionedGlyph, parameters,
  // model, controller) before.
  if (args.length === 1 && args[0] && "context" in args[0]) return args[0];
  const [context, positionedGlyph, parameters, model, controller] = args;
  return { context, positionedGlyph, parameters, model, controller };
}

// "trash" from Tabler Icons (outline, MIT; see TABLER-ICONS-LICENSE.txt), the
// icon set Fontra uses for its own panels.
const TRASH_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" ' +
  'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
  'stroke-linejoin="round"><path d="M4 7l16 0" /><path d="M10 11l0 6" />' +
  '<path d="M14 11l0 6" /><path d="M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2 -2l1 -12" />' +
  '<path d="M9 7v-3a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v3" /></svg>';

function trashIcon() {
  const span = document.createElement("span");
  span.className = "svg-icon";
  span.innerHTML = TRASH_SVG;
  return span;
}

function stopKeys(element) {
  // Keys typed in the card must not reach the editor's shortcuts.
  for (const type of ["keydown", "keyup", "keypress"]) {
    element.addEventListener(type, (event) => event.stopPropagation());
  }
}

// Where the pin of a comment is drawn (screen), for a point in glyph units.
export function pinCenter(tip) {
  return { x: tip.x, y: tip.y - PIN_LIFT };
}

export function hitsPin(screenTip, screenPoint) {
  const center = pinCenter(screenTip);
  return Math.hypot(center.x - screenPoint.x, center.y - screenPoint.y) <= PIN_RADIUS + 2;
}

// Which of a glyph's sources a positioned glyph shows: {layer, name,
// location}, or null at an interpolated position (not on a source).
export function sourceOf(glyphController) {
  const layerName = glyphController?.layerName;
  if (!layerName) return null;
  const varGlyph = glyphController.varGlyph;
  const source = varGlyph?.sources?.[glyphController.sourceIndex];
  let name = layerName;
  let location = {};
  if (source) {
    name = varGlyph.getSourceName?.(source) || source.name || layerName;
    location = varGlyph.getSourceLocation?.(source) || source.location || {};
  }
  return { layer: layerName, name, location };
}

// --- the controller -----------------------------------------------------------

export class HiveComments {
  constructor(editor, pluginPath = "/hive/plugin") {
    this.editor = editor;
    this.pluginPath = pluginPath;
    const { name, branch } = parseProjectIdentifier(editor.projectIdentifier || "");
    this.projectName = name;
    this.branch = branch;
    this.issues = [];
    this.head = undefined;
    this.you = null;
    this.can = { comment: false, resolveAny: false, moderate: false };
    this.loaded = false;
    this.showResolved = false;
    this.openNumber = null; // the topic whose post-it is open
    this.draft = null; // {glyph, source, point, text} while a new comment is written
    this.editing = null; // {number, id} while a message is edited
    this.drag = null; // {number, point} while a pin is dragged
    this.busy = false;
    this.error = null;
    this.timer = null;
    this.listeners = new Set();
    this.overlayFrame = null;
    this.generation = 0; // bumped by each refresh and each change we send
    this.sentRoles = new Set(); // compose boxes whose text was sent: cleared at the next rebuild
  }

  install() {
    this.installLayer();
    this.installOverlay();
    this.installTool();
    this.installCanvasListeners();
    const settings = this.editor.sceneController?.sceneSettingsController;
    settings?.addKeyListener?.(["viewBox", "positionedLines"], () => this.scheduleOverlay());
    settings?.addKeyListener?.(["selectedGlyphName"], () => {
      if (this.draft && this.draft.glyph !== this.selectedGlyphName) this.cancelDraft();
      this.changed();
    });
    this.refresh();
    this.startPolling();
  }

  // --- data -------------------------------------------------------------------

  apiURL(route = "") {
    return new URL(
      `/api/hive/projects/${encodeURIComponent(this.projectName)}/comments${route}`,
      window.location.origin
    );
  }

  startPolling() {
    this.stopPolling();
    this.timer = setInterval(() => this.checkHead(), POLL_INTERVAL_MS);
  }

  stopPolling() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  async checkHead() {
    if (document.visibilityState === "hidden") return;
    try {
      const response = await fetch(this.apiURL("/head"));
      if (!response.ok) return;
      const { head } = await response.json();
      if (head !== this.head) await this.refresh();
    } catch (error) {
      // a network hiccup: next tick
    }
  }

  async refresh() {
    // An answer that started before one of our own changes is stale.
    const generation = ++this.generation;
    try {
      const response = await fetch(this.apiURL());
      if (generation !== this.generation) return;
      if (!response.ok) {
        this.loaded = true;
        this.changed();
        return;
      }
      const data = await response.json();
      if (generation !== this.generation) return;
      this.head = data.head;
      this.issues = data.issues || [];
      this.you = data.you || null;
      this.can = { ...this.can, ...(data.can || {}) };
      this.loaded = true;
      if (this.openNumber !== null && !this.issue(this.openNumber)) this.openNumber = null;
      this.changed();
    } catch (error) {
      // keep what we have
    }
  }

  issue(number) {
    return this.issues.find((issue) => issue.number === number) || null;
  }

  issuesOf(glyphName) {
    return this.issues.filter((issue) => issue.glyph === glyphName);
  }

  isShown(issue) {
    return issue.state === "open" || this.showResolved || issue.number === this.openNumber;
  }

  // After any change: redraw the canvas, the post-it and the panel.
  changed() {
    this.editor.canvasController?.requestUpdate?.();
    this.scheduleOverlay();
    for (const listener of this.listeners) listener();
  }

  async send(method, route, body) {
    this.busy = true;
    this.error = null;
    this.renderOverlay();
    try {
      const response = await fetch(this.apiURL(route), {
        method,
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!response.ok) {
        const text = (await response.text()) || response.statusText;
        throw new Error(text);
      }
      const data = await response.json();
      this.generation++;
      if (data.issue) {
        const index = this.issues.findIndex((i) => i.number === data.issue.number);
        if (index === -1) this.issues.push(data.issue);
        else this.issues[index] = data.issue;
        this.issues.sort((a, b) => a.number - b.number);
      }
      // The head moved by our own change: take it, without refetching.
      this.head = data.head;
      return data;
    } catch (error) {
      this.error = error.message || String(error);
      return null;
    } finally {
      this.busy = false;
      this.changed();
    }
  }

  // --- actions ------------------------------------------------------------------

  async submitDraft(text) {
    const draft = this.draft;
    if (!draft || !text.trim()) return false;
    draft.text = text;
    const data = await this.send("POST", "", {
      glyph: draft.glyph,
      source: draft.source,
      point: draft.point,
      text,
      branch: this.branch,
    });
    if (!data?.issue) return false;
    this.draft = null;
    this.openNumber = data.issue.number;
    this.changed();
    this.focusCompose();
    return true;
  }

  async reply(number, text) {
    if (!text.trim()) return false;
    return !!(await this.send("POST", `/${number}/messages`, { text }));
  }

  async setState(number, state) {
    await this.send("PATCH", `/${number}`, { state, branch: this.branch });
  }

  async move(number, point) {
    const data = await this.send("PATCH", `/${number}`, { point });
    if (!data) {
      // Refused: put the pin back where the server has it, and say why.
      const why = this.error;
      await this.refresh();
      this.toast(`#${number} could not be moved: ${why}`);
    }
  }

  async editMessage(number, id, text) {
    const data = await this.send("PATCH", `/${number}/messages/${id}`, { text });
    if (data) this.editing = null;
    this.changed();
    return !!data;
  }

  async deleteMessage(number, id) {
    await this.send("DELETE", `/${number}/messages/${id}`);
  }

  async deleteIssue(number) {
    const data = await this.send("DELETE", `/${number}`);
    if (data) {
      this.issues = this.issues.filter((issue) => issue.number !== number);
      if (this.openNumber === number) this.openNumber = null;
      this.changed();
    }
  }

  // --- permissions (the server has the last word) ---------------------------------

  isMine(item) {
    const author = item?.author;
    if (!this.you || !author) return false;
    if (this.you.uid && author.uid) return author.uid === this.you.uid;
    return author.username === this.you.username;
  }

  mayChangeState(issue) {
    return this.can.comment && (this.can.resolveAny || this.isMine(issue));
  }

  mayMove(issue) {
    return this.mayChangeState(issue);
  }

  mayDeleteIssue(issue) {
    return (
      this.can.comment &&
      (this.can.moderate || issue.messages.every((message) => this.isMine(message)))
    );
  }

  mayDeleteMessage(issue, message) {
    return (
      this.can.comment &&
      message !== issue.messages[0] &&
      (this.can.moderate || this.isMine(message))
    );
  }

  // --- opening, the draft ---------------------------------------------------------

  open(number) {
    if (this.draft?.text?.trim() && !window.confirm("Discard the comment you are writing?")) {
      return;
    }
    this.draft = null;
    this.editing = null;
    this.error = null;
    this.openNumber = number;
    this.changed();
    this.focusCompose();
  }

  close() {
    this.openNumber = null;
    this.editing = null;
    this.error = null;
    this.changed();
  }

  startDraft(glyph, source, point) {
    const text = this.draft?.text || "";
    this.openNumber = null;
    this.editing = null;
    this.error = null;
    this.draft = { glyph, source, point, text };
    this.changed();
    this.focusCompose();
  }

  cancelDraft() {
    this.draft = null;
    this.error = null;
    this.changed();
  }

  focusCompose() {
    requestAnimationFrame(() => this.overlayRoot?.querySelector("textarea")?.focus());
  }

  // --- the editor's scene --------------------------------------------------------

  get selectedGlyphName() {
    return this.editor.sceneController?.sceneSettings?.selectedGlyphName || null;
  }

  get sceneModel() {
    return this.editor.sceneController?.sceneModel;
  }

  positionedGlyphs() {
    const lines = this.sceneModel?.positionedLines || [];
    return lines.flatMap((line) => line.glyphs || []);
  }

  // The positioned glyph a topic is drawn on: the selected one if it is that
  // glyph, else the first occurrence on the canvas.
  positionedGlyphFor(glyphName) {
    const selected = this.sceneModel?.getSelectedPositionedGlyph?.();
    if (selected?.glyphName === glyphName) return selected;
    return this.positionedGlyphs().find((g) => g.glyphName === glyphName) || null;
  }

  pointOf(issue) {
    return this.drag?.number === issue.number ? this.drag.point : issue.point;
  }

  screenPoint(positionedGlyph, point) {
    const canvas = this.editor.canvasController;
    if (!canvas?.canvasPoint) return null;
    return canvas.canvasPoint({ x: positionedGlyph.x + point.x, y: positionedGlyph.y + point.y });
  }

  // The topic whose pin is under a screen point (relative to the canvas), if any.
  pinAt(screen) {
    const candidates = [];
    for (const positionedGlyph of this.positionedGlyphs()) {
      for (const issue of this.issuesOf(positionedGlyph.glyphName)) {
        if (!this.isShown(issue)) continue;
        const tip = this.screenPoint(positionedGlyph, this.pointOf(issue));
        if (tip && hitsPin(tip, screen)) candidates.push({ issue, positionedGlyph });
      }
    }
    // The topmost is drawn last: the highest number.
    return candidates.at(-1) || null;
  }

  // --- the canvas layer ------------------------------------------------------------

  installLayer() {
    const layers = this.editor.visualizationLayers;
    if (!layers?.definitions || layers.definitions.some((d) => d.identifier === LAYER_ID)) {
      return;
    }
    const definition = {
      identifier: LAYER_ID,
      name: "Hive: comments",
      userSwitchable: true,
      defaultOn: true,
      zIndex: 600, // above the editing nodes (500)
      selectionFunc: (visContext) => {
        const glyphs = visContext.glyphsBySelectionMode?.all || [];
        return glyphs.filter(
          (g) =>
            this.issuesOf(g.glyphName).some((issue) => this.isShown(issue)) ||
            this.draft?.glyph === g.glyphName
        );
      },
      draw: (...args) => this.drawPins(drawArguments(args)),
    };
    let index = layers.definitions.findIndex((d) => definition.zIndex < d.zIndex);
    if (index === -1) index = layers.definitions.length;
    layers.definitions.splice(index, 0, definition);
    // On unless switched off in the View menu before (Fontra keeps that
    // choice in localStorage; this adds our layer to what it keeps).
    const settings = this.editor.visualizationLayersSettings;
    let on = true;
    try {
      settings?.synchronizeItemWithLocalStorage?.(LAYER_ID, true);
      if (settings?.model && typeof settings.model[LAYER_ID] === "boolean") {
        on = settings.model[LAYER_ID];
      }
    } catch (error) {
      // an older Fontra: always on at start
    }
    layers.toggle(LAYER_ID, on);
  }

  drawPins({ context, positionedGlyph, controller }) {
    if (!context || !positionedGlyph) return;
    const magnification =
      controller?.magnification || this.editor.canvasController?.magnification || 1;
    const dark = !!this.editor.visualizationLayers?.darkTheme;
    const colors = dark ? COLORS.dark : COLORS.light;
    const currentLayer = positionedGlyph.glyph?.layerName;
    const pins = this.issuesOf(positionedGlyph.glyphName)
      .filter((issue) => this.isShown(issue))
      .map((issue) => ({
        label: String(issue.number),
        point: this.pointOf(issue),
        color: issue.state === "open" ? colors.open : colors.resolved,
        pale: issue.source?.layer !== currentLayer,
        current: issue.number === this.openNumber,
      }));
    if (this.draft?.glyph === positionedGlyph.glyphName) {
      pins.push({
        label: "+",
        point: this.draft.point,
        color: colors.open,
        pale: this.draft.source?.layer !== currentLayer,
        current: true,
      });
    }
    for (const pin of pins) {
      drawPin(context, pin, magnification, colors);
    }
    this.scheduleOverlay();
  }

  // --- the post-it (HTML over the canvas) ------------------------------------------------

  installOverlay() {
    const container = this.editor.canvasController?.canvas?.parentElement;
    if (!container) return;
    this.overlayHost = el("div", { class: "hive-comments-overlay" });
    this.overlayRoot = this.overlayHost.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = CARD_STYLES;
    this.overlayRoot.append(style);
    this.cardElement = null;
    container.append(this.overlayHost);
  }

  scheduleOverlay() {
    if (this.overlayFrame !== null || !this.overlayRoot) return;
    this.overlayFrame = requestAnimationFrame(() => {
      this.overlayFrame = null;
      this.renderOverlay();
    });
  }

  // The card's content is rebuilt only when what it shows changed (typing in
  // it must survive a redraw of the canvas); its position follows every frame.
  renderOverlay() {
    if (!this.overlayRoot) return;
    const target = this.cardTarget();
    if (!target) {
      this.cardElement?.remove();
      this.cardElement = null;
      this.cardKey = null;
      return;
    }
    const key = JSON.stringify([
      target.kind,
      target.issue ?? null,
      this.editing,
      this.busy,
      this.error,
      this.can,
      this.you?.username,
    ]);
    if (key !== this.cardKey || !this.cardElement) {
      const previous = this.cardElement;
      // What was typed survives the rebuild (same card), unless it was sent.
      const identity = `${target.kind}:${target.issue?.number ?? ""}`;
      const typed = new Map();
      if (previous && identity === this.cardIdentity) {
        for (const textarea of previous.querySelectorAll("textarea[data-role]")) {
          typed.set(textarea.dataset.role, textarea.value);
        }
      }
      const focused = this.overlayRoot.activeElement;
      const focusedRole = focused?.dataset?.role;
      const caret = focusedRole ? [focused.selectionStart, focused.selectionEnd] : null;
      const scrollTop =
        identity === this.cardIdentity ? previous?.querySelector(".messages")?.scrollTop : 0;
      for (const role of this.sentRoles) typed.delete(role);
      this.sentRoles.clear();
      this.cardElement =
        target.kind === "draft" ? this.draftCard() : this.issueCard(target.issue);
      if (previous) previous.replaceWith(this.cardElement);
      else this.overlayRoot.append(this.cardElement);
      for (const textarea of this.cardElement.querySelectorAll("textarea[data-role]")) {
        const role = textarea.dataset.role;
        if (typed.has(role)) textarea.value = typed.get(role);
        textarea.dispatchEvent(new Event("input"));
        if (role === focusedRole) {
          textarea.focus();
          if (caret) textarea.setSelectionRange(caret[0], caret[1]);
        }
      }
      const messages = this.cardElement.querySelector(".messages");
      if (messages && scrollTop) messages.scrollTop = scrollTop;
      this.cardKey = key;
      this.cardIdentity = identity;
    }
    this.placeCard(target.screen);
    this.watchView();
  }

  // While a post-it is open, follow the view even when nothing redraws the
  // pins (the layer switched off, a pan with the Hand tool).
  watchView() {
    if (this.viewWatch) return;
    const canvas = this.editor.canvasController;
    const viewKey = () =>
      canvas ? `${canvas.origin?.x},${canvas.origin?.y},${canvas.magnification}` : "";
    let last = viewKey();
    const tick = () => {
      if (!this.cardElement) {
        this.viewWatch = null;
        return;
      }
      const key = viewKey();
      if (key !== last) {
        last = key;
        this.scheduleOverlay();
      }
      this.viewWatch = requestAnimationFrame(tick);
    };
    this.viewWatch = requestAnimationFrame(tick);
  }

  cardTarget() {
    let glyphName, point, kind, issue;
    if (this.draft) {
      kind = "draft";
      glyphName = this.draft.glyph;
      point = this.draft.point;
    } else if (this.openNumber !== null) {
      issue = this.issue(this.openNumber);
      if (!issue) return null;
      kind = "issue";
      glyphName = issue.glyph;
      point = this.pointOf(issue);
    } else {
      return null;
    }
    const positionedGlyph = this.positionedGlyphFor(glyphName);
    if (!positionedGlyph) return null;
    const screen = this.screenPoint(positionedGlyph, point);
    if (!screen) return null;
    return { kind, issue, screen };
  }

  placeCard(tip) {
    const card = this.cardElement;
    const host = this.overlayHost;
    const width = host.clientWidth || 800;
    const height = host.clientHeight || 600;
    const cardWidth = card.offsetWidth || 280;
    const cardHeight = card.offsetHeight || 160;
    let left = tip.x + PIN_RADIUS + 8;
    if (left + cardWidth > width - 8) left = tip.x - PIN_RADIUS - 8 - cardWidth;
    let top = tip.y - PIN_LIFT - PIN_RADIUS;
    top = Math.min(top, height - cardHeight - 8);
    left = Math.max(8, left);
    top = Math.max(8, top);
    card.style.left = `${Math.round(left)}px`;
    card.style.top = `${Math.round(top)}px`;
  }

  composeBox({ role, placeholder, submitLabel, onSubmit, onCancel, first = false, initial = "" }) {
    const textarea = el("textarea", {
      class: "compose-text",
      placeholder,
      rows: 2,
      dataset: { role },
    });
    textarea.value = initial;
    stopKeys(textarea);
    const submit = el("button", { class: "primary", disabled: true }, [submitLabel]);
    const update = () => (submit.disabled = this.busy || !textarea.value.trim());
    const go = async () => {
      if (submit.disabled) return;
      if (await onSubmit(textarea.value)) {
        this.sentRoles.add(role);
        this.changed();
      }
    };
    textarea.addEventListener("input", () => {
      if (this.draft && role === "draft") this.draft.text = textarea.value;
      update();
    });
    textarea.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        go();
      } else if (event.key === "Escape") {
        event.preventDefault();
        onCancel?.();
      }
    });
    submit.addEventListener("click", go);
    update();
    return el("div", { class: `compose${first ? " first" : ""}` }, [
      textarea,
      el("div", { class: "row" }, [
        el("span", { class: "hint" }, ["⏎ send · ⇧⏎ new line"]),
        onCancel && first ? el("button", { onclick: () => onCancel() }, ["Cancel"]) : null,
        submit,
      ]),
    ]);
  }

  draftCard() {
    const draft = this.draft;
    return el("div", { class: "card draft" }, [
      el("div", { class: "card-header" }, [
        el("span", { class: "number" }, ["New comment"]),
        el("span", { class: "where", title: `${draft.glyph} · ${draft.source.name}` }, [
          `${draft.glyph} · ${draft.source.name}`,
        ]),
        el("button", { class: "icon", title: "Cancel", onclick: () => this.cancelDraft() }, ["×"]),
      ]),
      this.error ? el("div", { class: "error" }, [this.error]) : null,
      this.composeBox({
        role: "draft",
        placeholder: `Comment on ${draft.glyph}…`,
        submitLabel: this.busy ? "Sending…" : "Comment",
        onSubmit: (text) => this.submitDraft(text),
        onCancel: () => this.cancelDraft(),
        first: true,
        initial: draft.text || "",
      }),
    ]);
  }

  issueCard(issue) {
    const resolved = issue.state === "resolved";
    const header = el("div", { class: "card-header" }, [
      el("span", { class: "number" }, [`#${issue.number}`]),
      el(
        "span",
        {
          class: "where",
          title: `${issue.glyph} · ${issue.source?.name || ""} · on ${issue.branch}`,
        },
        [`${issue.glyph} · ${issue.source?.name || ""}`]
      ),
      resolved ? el("span", { class: "state" }, ["Resolved"]) : null,
      this.mayChangeState(issue)
        ? el(
            "button",
            {
              disabled: this.busy,
              title: resolved ? "Open this topic again" : "Mark this topic as resolved",
              onclick: () => this.setState(issue.number, resolved ? "open" : "resolved"),
            },
            [resolved ? "Reopen" : "Resolve"]
          )
        : null,
      this.mayDeleteIssue(issue)
        ? el(
            "button",
            {
              class: "icon",
              title: "Delete this topic",
              disabled: this.busy,
              onclick: () => this.confirmDelete(issue),
            },
            [trashIcon()]
          )
        : null,
      el("button", { class: "icon", title: "Close", onclick: () => this.close() }, ["×"]),
    ]);
    const messages = el(
      "div",
      { class: "messages" },
      issue.messages.map((message) => this.messageElement(issue, message))
    );
    const note = issue.resolved
      ? el("div", { class: "message" }, [
          el("span", { class: "when" }, [
            `Resolved by ${issue.resolved.by?.name || issue.resolved.by?.username || "?"} ${relativeTime(issue.resolved.at)}`,
          ]),
        ])
      : null;
    if (note) messages.append(note);
    return el("div", { class: `card${resolved ? " resolved" : ""}`, dataset: { number: issue.number } }, [
      header,
      messages,
      this.error ? el("div", { class: "error" }, [this.error]) : null,
      this.can.comment
        ? this.composeBox({
            role: "reply",
            placeholder: "Reply…",
            submitLabel: "Reply",
            onSubmit: (text) => this.reply(issue.number, text),
            onCancel: () => this.close(),
          })
        : null,
    ]);
  }

  confirmDelete(issue) {
    const others = issue.messages.length - 1;
    const text =
      `Delete #${issue.number}` + (others ? ` and its ${others} repl${others > 1 ? "ies" : "y"}?` : "?");
    if (window.confirm(text)) this.deleteIssue(issue.number);
  }

  messageElement(issue, message) {
    const author = message.author || {};
    const isEditing = this.editing?.number === issue.number && this.editing?.id === message.id;
    const actions = [];
    if (this.can.comment && this.isMine(message) && !isEditing) {
      actions.push(
        el("button", {
          class: "link",
          onclick: () => {
            this.editing = { number: issue.number, id: message.id };
            this.changed();
            this.focusCompose();
          },
        }, ["Edit"])
      );
    }
    if (this.mayDeleteMessage(issue, message)) {
      actions.push(
        el("button", {
          class: "link",
          onclick: () => this.deleteMessage(issue.number, message.id),
        }, ["Delete"])
      );
    }
    const body = isEditing
      ? this.composeBox({
          role: `edit-${message.id}`,
          placeholder: "Edit your message…",
          submitLabel: "Save",
          onSubmit: (text) => this.editMessage(issue.number, message.id, text),
          onCancel: () => {
            this.editing = null;
            this.changed();
          },
          first: true,
          initial: message.text,
        })
      : el("div", { class: "text" }, [message.text]);
    return el("div", { class: "message", dataset: { id: message.id } }, [
      el("div", { class: "meta" }, [
        el("span", {
          class: "avatar",
          style: `background:${avatarColor(author.username)}`,
          title: author.username || "",
        }, [initials(author)]),
        el("span", { class: "who" }, [author.name || author.username || "?"]),
        el("span", { class: "when", title: message.created || "" }, [
          relativeTime(message.created) + (message.edited ? " · edited" : ""),
        ]),
        actions.length ? el("span", { class: "actions" }, actions) : null,
      ]),
      body,
    ]);
  }

  toast(text) {
    if (!this.overlayRoot) return;
    this.overlayRoot.querySelector(".toast")?.remove();
    const toast = el("div", { class: "toast" }, [text]);
    this.overlayRoot.append(toast);
    setTimeout(() => toast.remove(), TOAST_MS);
  }

  // --- canvas events: pins answer the click whatever the tool -------------------------

  installCanvasListeners() {
    const canvas = this.editor.canvasController?.canvas;
    const container = canvas?.parentElement;
    if (!container) return;
    const local = (event) => {
      const rect = canvas.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    };
    // Capture: runs before Fontra's own listener on the canvas.
    container.addEventListener(
      "mousedown",
      (event) => {
        if (event.target !== canvas || event.button !== 0) return;
        if (!this.layerVisible()) return;
        const hit = this.pinAt(local(event));
        if (!hit) return;
        event.preventDefault();
        event.stopPropagation();
        this.pressPin(hit, event, local);
      },
      true
    );
    container.addEventListener("mousemove", (event) => {
      if (event.target !== canvas || this.drag || !this.layerVisible()) return;
      if (this.pinAt(local(event))) canvas.style.cursor = "pointer";
    });
  }

  layerVisible() {
    const visible = this.editor.visualizationLayers?.visibleLayerIds;
    return !visible || visible.has(LAYER_ID);
  }

  // A press on a pin: a click opens (or closes) its post-it, a drag moves it.
  pressPin({ issue, positionedGlyph }, downEvent, local) {
    const start = local(downEvent);
    const canMove = this.mayMove(issue);
    const origin = { ...issue.point };
    const magnification = this.editor.canvasController?.magnification || 1;
    let dragging = false;
    const onMove = (event) => {
      const now = local(event);
      if (!dragging && Math.hypot(now.x - start.x, now.y - start.y) < DRAG_THRESHOLD) return;
      if (!canMove) return;
      dragging = true;
      this.drag = {
        number: issue.number,
        point: {
          x: Math.round(origin.x + (now.x - start.x) / magnification),
          y: Math.round(origin.y - (now.y - start.y) / magnification),
        },
      };
      this.editor.canvasController?.canvas && (this.editor.canvasController.canvas.style.cursor = "grabbing");
      this.changed();
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove, true);
      window.removeEventListener("mouseup", onUp, true);
      if (dragging && this.drag) {
        const point = this.drag.point;
        issue.point = point; // shown where it was dropped while the server answers
        this.drag = null;
        this.move(issue.number, point);
      } else if (this.openNumber === issue.number) {
        this.close();
      } else {
        this.open(issue.number);
      }
    };
    window.addEventListener("mousemove", onMove, true);
    window.addEventListener("mouseup", onUp, true);
  }

  // --- the tool --------------------------------------------------------------------

  installTool() {
    if (typeof this.editor.addEditTool !== "function" || this.editor.tools?.[TOOL_ID]) return;
    this.tool = new HiveCommentTool(this);
    this.editor.addEditTool(this.tool);
    const button = document.querySelector(`[data-tool="${TOOL_ID}"]`);
    if (button) {
      button.title = "Comment (Fontra Hive): click in the glyph to pin a comment";
      // Fontra creates tool buttons "selected" and relies on the
      // setSelectedTool() that follows its own initTools(); a tool added
      // later must clear it, or it looks selected next to the real one.
      button.classList.toggle("selected", this.editor.selectedToolIdentifier === TOOL_ID);
    }
  }

  // A click with the Comment tool, in canvas coordinates (glyph space of the scene).
  clickWithTool(scenePoint) {
    const model = this.sceneModel;
    const positionedGlyph = model?.getSelectedPositionedGlyph?.();
    const editing = model?.selectedGlyph?.isEditing ?? this.editor.sceneSettings?.selectedGlyph?.isEditing;
    if (!positionedGlyph || !editing) return false; // let Fontra select a glyph
    const advance = positionedGlyph.glyph?.xAdvance ?? Infinity;
    const x = scenePoint.x - positionedGlyph.x;
    const y = scenePoint.y - positionedGlyph.y;
    const margin = Math.max(50, advance * 0.15);
    if (x < -margin || x > advance + margin) {
      // Over another glyph of the line: let Fontra select it first.
      if (this.positionedGlyphs().some((g) => g !== positionedGlyph &&
          scenePoint.x >= g.x && scenePoint.x <= g.x + (g.glyph?.xAdvance ?? 0))) {
        return false;
      }
    }
    if (this.openNumber !== null) {
      this.close();
      return true;
    }
    if (!this.can.comment) {
      this.toast(this.loaded ? "Your role on this project cannot comment." : "Loading comments…");
      return true;
    }
    const source = sourceOf(positionedGlyph.glyph);
    if (!source) {
      this.toast(
        `Comments are pinned to a source: go to one of ${positionedGlyph.glyphName}'s sources first.`
      );
      return true;
    }
    this.startDraft(positionedGlyph.glyphName, source, {
      x: Math.round(x),
      y: Math.round(y),
    });
    return true;
  }

  // --- going to a topic from the list ---------------------------------------------------

  async show(issue) {
    const sceneSettings = this.editor.sceneController?.sceneSettings;
    const model = this.sceneModel;
    if (!sceneSettings || !model) {
      this.open(issue.number);
      return;
    }
    // Select the glyph if it is on the canvas; else put it in place of the
    // selected glyph, like Fontra's glyph search does.
    const selected = model.getSelectedPositionedGlyph?.();
    if (selected?.glyphName !== issue.glyph) {
      const lines = model.positionedLines || [];
      let found = null;
      lines.forEach((line, lineIndex) =>
        (line.glyphs || []).forEach((g, glyphIndex) => {
          if (!found && g.glyphName === issue.glyph) found = { lineIndex, glyphIndex };
        })
      );
      if (found) {
        sceneSettings.selectedGlyph = { ...found, isEditing: true };
      } else if (sceneSettings.selectedGlyph && this.editor.insertGlyphInfos) {
        const info = this.editor.sceneController.glyphInfoFromGlyphName?.(issue.glyph) || {
          glyphName: issue.glyph,
        };
        await this.editor.insertGlyphInfos([info], 0, true);
        const glyphMap = this.editor.fontController?.glyphMap;
        if (glyphMap && !(issue.glyph in glyphMap)) {
          this.toast(`${issue.glyph} is not in the font any more.`);
          return;
        }
        sceneSettings.selectedGlyph = { ...sceneSettings.selectedGlyph, isEditing: true };
      } else {
        this.toast(`Put ${issue.glyph} on the canvas to see #${issue.number}.`);
        return;
      }
    }
    // Go to the source it was written on.
    try {
      const varGlyph = await model.getSelectedVariableGlyphController?.();
      const index = varGlyph?.sources?.findIndex((s) => s.layerName === issue.source?.layer);
      if (index !== undefined && index >= 0) {
        await this.editor.sceneController.setLocationFromSourceIndex?.(index);
      }
    } catch (error) {
      // the glyph may be gone: show the comment anyway
    }
    this.open(issue.number);
    requestAnimationFrame(() => this.centerOn(issue));
  }

  centerOn(issue) {
    const canvas = this.editor.canvasController;
    const positionedGlyph = this.positionedGlyphFor(issue.glyph);
    if (!canvas?.canvasPoint || !positionedGlyph) return;
    const screen = this.screenPoint(positionedGlyph, issue.point);
    const width = canvas.canvasWidth ?? canvas.canvas?.clientWidth;
    const height = canvas.canvasHeight ?? canvas.canvas?.clientHeight;
    if (!screen || !width || !height) return;
    const margin = 60;
    if (screen.x > margin && screen.x < width - 340 && screen.y > margin && screen.y < height - margin) {
      return; // already well in view
    }
    canvas.origin.x += width / 2 - 150 - screen.x;
    canvas.origin.y += height / 2 - screen.y;
    canvas.requestUpdate();
  }
}

function drawPin(context, pin, magnification, colors) {
  const r = PIN_RADIUS / magnification;
  const lift = PIN_LIFT / magnification;
  const { x, y } = pin.point;
  context.save();
  if (pin.pale) context.globalAlpha = PALE_ALPHA;
  // Tail from the circle to the point it marks.
  context.beginPath();
  context.moveTo(x, y);
  context.lineTo(x - r * 0.55, y + lift - r * 0.6);
  context.lineTo(x + r * 0.55, y + lift - r * 0.6);
  context.closePath();
  context.fillStyle = pin.color;
  context.fill();
  context.beginPath();
  context.arc(x, y + lift, r, 0, 2 * Math.PI);
  context.fill();
  if (pin.current) {
    context.lineWidth = 2 / magnification;
    context.strokeStyle = colors.ring;
    context.stroke();
  }
  // The number, upright in screen pixels (the canvas is y-up in glyph units).
  context.translate(x, y + lift);
  context.scale(1 / magnification, -1 / magnification);
  context.fillStyle = colors.text;
  const label = pin.label.length > 3 ? "…" : pin.label;
  context.font = `bold ${label.length > 2 ? 9 : 11}px fontra-ui-regular, sans-serif`;
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(label, 0, 0.5);
  context.restore();
}

// --- the Comment edit tool ------------------------------------------------------------

class HiveCommentTool {
  identifier = TOOL_ID;

  constructor(comments) {
    this.comments = comments;
    this.editor = comments.editor;
    this.iconPath = `${comments.pluginPath}/comment-tool.svg`;
    this.isActive = false;
  }

  get canvasController() {
    return this.editor.canvasController;
  }

  setCursor() {
    if (this.canvasController?.canvas) this.canvasController.canvas.style.cursor = "crosshair";
  }

  activate() {
    this.isActive = true;
    this.setCursor();
  }

  deactivate() {
    this.isActive = false;
  }

  handleHover(event) {
    this.setCursor();
  }

  handleKeyDown(event) {
    if (event.key === "Escape") {
      if (this.comments.draft) this.comments.cancelDraft();
      else if (this.comments.openNumber !== null) this.comments.close();
    }
  }

  getContextMenuItems() {
    return [];
  }

  async handleDrag(eventStream, initialEvent) {
    const scenePoint = this.canvasController.localPoint(initialEvent);
    if (this.comments.clickWithTool(scenePoint)) {
      // Swallow the rest of this press.
      for await (const event of eventStream) {
        // nothing
      }
      return;
    }
    // Not for a comment: select glyphs like the pointer tool does.
    await this.editor.tools?.["pointer-tool"]?.handleDrag(eventStream, initialEvent);
  }
}

// --- the Comments sidebar panel --------------------------------------------------------------

export class HiveCommentsPanel extends HTMLElement {
  identifier = "hive-comments";
  iconPath = "/hive/plugin/comments.svg";

  constructor(comments) {
    super();
    this.comments = comments;
    this.visible = false;
    const shadow = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = PANEL_STYLES;
    this.titleElement = el("span", { class: "title" }, ["Comments"]);
    this.subtitleElement = el("span", { class: "subtitle" });
    this.resolvedToggle = el("input", {
      type: "checkbox",
      onchange: () => {
        comments.showResolved = this.resolvedToggle.checked;
        comments.changed();
      },
    });
    this.hintElement = el("div", { class: "hint" });
    this.listElement = el("div", { class: "list" });
    shadow.append(
      style,
      el("div", { class: "panel" }, [
        el("div", { class: "header" }, [
          this.titleElement,
          this.subtitleElement,
          el("label", { title: "Show resolved comments on the canvas too" }, [
            this.resolvedToggle,
            " Resolved",
          ]),
        ]),
        this.hintElement,
        this.listElement,
      ])
    );
    comments.listeners.add(() => {
      if (this.visible) this.render();
    });
  }

  async toggle(on) {
    this.visible = on;
    if (on) {
      this.render();
      this.comments.refresh();
    }
  }

  render() {
    const comments = this.comments;
    const glyphName = comments.selectedGlyphName;
    this.resolvedToggle.checked = comments.showResolved;
    const issues = glyphName ? comments.issuesOf(glyphName) : comments.issues;
    this.subtitleElement.textContent = glyphName ? glyphName : "whole project";
    const open = issues.filter((i) => i.state === "open");
    const resolved = issues.filter((i) => i.state !== "open");
    this.hintElement.textContent = !comments.loaded
      ? "Loading…"
      : !comments.can.comment
        ? issues.length
          ? ""
          : "No comments yet."
        : issues.length
          ? ""
          : glyphName
            ? "No comments on this glyph. With the Comment tool, click in the glyph to add one."
            : "No comments yet. Select a glyph and use the Comment tool to add one.";
    const rows = [];
    if (open.length) {
      rows.push(el("div", { class: "group-title" }, [`Open (${open.length})`]));
      rows.push(...open.map((issue) => this.row(issue, !glyphName)));
    }
    if (resolved.length) {
      rows.push(el("div", { class: "group-title" }, [`Resolved (${resolved.length})`]));
      rows.push(...resolved.map((issue) => this.row(issue, !glyphName)));
    }
    this.listElement.replaceChildren(...rows);
  }

  row(issue, withGlyph) {
    const first = issue.messages[0] || {};
    const replies = issue.messages.length - 1;
    const meta = [
      withGlyph ? issue.glyph : null,
      issue.source?.name,
      first.author?.name || first.author?.username,
      relativeTime(issue.created),
      replies ? `${replies} repl${replies > 1 ? "ies" : "y"}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    return el(
      "div",
      {
        class: `issue ${issue.state}${issue.number === this.comments.openNumber ? " current" : ""}`,
        dataset: { number: issue.number },
        title: first.text || "",
        onclick: () => this.comments.show(issue),
      },
      [
        el("span", { class: "badge" }, [String(issue.number)]),
        el("span", { class: "text" }, [first.text || ""]),
        el("span", { class: "meta" }, [meta]),
      ]
    );
  }
}

if (!customElements.get("hive-comments-panel")) {
  customElements.define("hive-comments-panel", HiveCommentsPanel);
}

export function initComments(editor, pluginPath) {
  const comments = new HiveComments(editor, pluginPath);
  comments.install();
  const panel = new HiveCommentsPanel(comments);
  panel.iconPath = `${pluginPath}/comments.svg`;
  editor.addSidebarPanel?.(panel, "right");
  return { comments, panel };
}
