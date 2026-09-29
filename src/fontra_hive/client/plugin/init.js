// Fontra Hive editor plug-in: "Glyph history" sidebar panel.
//
// Loaded by Fontra's editor plug-in mechanism (Application settings → Plugins,
// address "/hive/plugin"): the editor fetches plugin.json, imports this module
// and calls init(editor, pluginPath). The module is plain JavaScript on
// purpose — a runtime plug-in cannot import Fontra's bundled modules — and only
// uses the editor's public objects: sceneSettingsController, addSidebarPanel(),
// visualizationLayers and canvasController.
//
// What it does:
//   - lists the commits that touched the selected glyph (author, time, message);
//   - hover a version to see it as an overlay on the glyph canvas itself (a
//     visualization layer added at runtime, drawn in glyph coordinates); click
//     to keep it there ("pinned", drawn stronger than a hovered one);
//   - "Restore" (in the preview bar under the list) asks the server to commit that glyph file again
//     on top of the branch: history is never rewritten, and the editor picks
//     the change up like any external change;
//   - "Snapshot…" names the current state of the branch: the commits made
//     since the previous snapshot are grouped under it in the list (an empty
//     commit plus a snapshot/<name> tag on the server; nothing is rewritten).
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const POLL_INTERVAL_MS = 1500; // how often the branch head is checked (a tiny request)
const CONFIRM_TIMEOUT_MS = 5000; // how long "Confirm restore?" stays armed
const PREVIEW_LAYER_ID = "hive.history.preview";
const MAX_COMPONENT_DEPTH = 8;
const HOVER_DELAY_MS = 120; // hover this long before a version not yet loaded is fetched
const HOVER_ALPHA = 0.45; // a hovered version is drawn like a pinned one, paler
const PRELOAD_COUNT = 8; // versions loaded in the background after the list is shown
const MAX_CACHED_PREVIEWS = 60;
const MAX_CACHED_GLYPHS = 600;

const STYLES = `
  :host {
    display: block;
    height: 100%;
    font-family: fontra-ui-regular, sans-serif;
    color: var(--ui-element-foreground-color, inherit);
  }
  .panel {
    display: flex;
    flex-direction: column;
    height: 100%;
    gap: 0.4em;
    padding: 0.8em 0.8em 0.6em;
    box-sizing: border-box;
  }
  .header {
    display: flex;
    align-items: baseline;
    gap: 0.5em;
    min-width: 0;
  }
  .header .title {
    font-weight: bold;
    white-space: nowrap;
  }
  .header .branch {
    opacity: 0.7;
    font-size: 0.9em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }
  .header .tools {
    display: flex;
    gap: 0.3em;
    margin-left: auto;
  }
  .snapshot-button.active {
    background: rgba(128, 128, 128, 0.25);
  }
  .snapshot-form {
    display: flex;
    flex-direction: column;
    gap: 0.4em;
    padding: 0.5em 0.6em;
    border-radius: 0.4em;
    background: var(--text-input-background-color, rgba(128,128,128,0.12));
    font-size: 0.9em;
  }
  .snapshot-form:empty {
    display: none;
  }
  .snapshot-form input {
    font: inherit;
    padding: 0.25em 0.4em;
    border-radius: 0.3em;
    border: 1px solid rgba(128, 128, 128, 0.5);
    background: var(--background-color, transparent);
    color: inherit;
  }
  .snapshot-form .form-actions {
    display: flex;
    gap: 0.4em;
  }
  .summary-line {
    font-size: 0.85em;
    opacity: 0.6;
  }
  .list {
    flex: 1;
    min-height: 0;
    overflow: hidden auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 2px; /* room for the row outlines */
  }
  /* One line per version: author · message ... date. Details in the tooltip. */
  .commit, .snapshot {
    flex: none;
    display: flex;
    align-items: baseline;
    gap: 0.45em;
    height: 1.8em;
    line-height: 1.8em;
    padding: 0 0.5em;
    box-sizing: border-box;
    border-radius: 0.3em;
    font-size: 0.9em;
    white-space: nowrap;
    cursor: pointer;
  }
  .commit {
    background: var(--text-input-background-color, rgba(128,128,128,0.1));
  }
  .commit:hover, .snapshot:hover {
    background: var(--text-input-background-color-hover, rgba(128,128,128,0.2));
  }
  .commit.nested {
    margin-left: 1.2em;
  }
  .commit.current, .snapshot.current {
    outline: 1px solid var(--foreground-color, currentColor);
    cursor: default;
  }
  .commit.previewing, .snapshot.previewing {
    outline: 2px solid #e8781e;
    outline-offset: -1px;
    background: rgba(232, 120, 30, 0.14);
  }
  .commit.hovering, .snapshot.hovering {
    outline: 1px solid rgba(232, 120, 30, 0.75);
  }
  .commit .author {
    flex: none;
    max-width: 40%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .commit .message, .snapshot .name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .commit .message {
    opacity: 0.65;
  }
  .when, .count {
    flex: none;
    opacity: 0.65;
    font-size: 0.9em;
    font-variant-numeric: tabular-nums;
  }
  .current-mark {
    flex: none;
    font-size: 0.85em;
    opacity: 0.7;
  }
  .group-label {
    flex: none;
    font-size: 0.8em;
    opacity: 0.6;
    margin: 0.4em 0 0.1em;
  }
  .snapshot {
    padding-left: 0.1em;
    border-left: 3px solid rgba(128, 128, 128, 0.6);
    background: rgba(128, 128, 128, 0.2);
    margin-top: 0.3em;
  }
  .snapshot .name {
    font-weight: bold;
  }
  button.caret {
    flex: none;
    border: none;
    padding: 0;
    width: 1.3em;
    line-height: inherit;
  }
  .empty {
    opacity: 0.6;
    padding: 0.4em 0;
  }
  .error {
    color: var(--fontra-red-color, #d33);
  }
  /* The preview bar: always there, always the same height, below the list,
     so that hovering never moves the rows. */
  .status {
    flex: none;
    display: flex;
    align-items: center;
    gap: 0.4em;
    height: 2.2em;
    padding: 0 0.3em 0 0.6em;
    box-sizing: border-box;
    border-radius: 0.4em;
    border-left: 3px solid transparent;
    font-size: 0.85em;
    background: rgba(128, 128, 128, 0.08);
  }
  .status.pinned {
    background: rgba(232, 120, 30, 0.15);
    border-left-color: #e8781e;
  }
  .status.hover {
    background: rgba(232, 120, 30, 0.07);
    border-left: 3px dotted #e8781e;
  }
  .status .text {
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .status .hint {
    opacity: 0.55;
  }
  button {
    font: inherit;
    font-size: 0.85em;
    background: none;
    border: 1px solid currentColor;
    border-radius: 0.3em;
    color: inherit;
    opacity: 0.8;
    padding: 0.1em 0.5em;
    cursor: pointer;
    white-space: nowrap;
  }
  button:hover {
    opacity: 1;
  }
  button:disabled {
    opacity: 0.4;
    cursor: default;
  }
  button.restore {
    flex: none;
    border-color: #e8781e;
    color: #e8781e;
  }
  button.restore.armed {
    background: #e8781e;
    color: white;
  }
  button.close {
    flex: none;
    border: none;
    padding: 0 0.3em;
  }
`;

function parseProjectIdentifier(identifier) {
  const at = identifier.indexOf("@");
  return at === -1
    ? { name: identifier, branch: "main" }
    : { name: identifier.slice(0, at), branch: identifier.slice(at + 1) };
}

function previewKey(sha, glyphName) {
  return `${sha}\n${glyphName}`;
}

// Short date for the rows: the time today, the day this year, else the year.
function formatDate(unixSeconds) {
  const date = new Date(unixSeconds * 1000);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  if (date.getFullYear() === now.getFullYear()) {
    return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }
  return date.toLocaleDateString(undefined, { month: "short", year: "numeric" });
}

// Full date and time, for tooltips.
function formatFullDate(unixSeconds) {
  return new Date(unixSeconds * 1000).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function el(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") element.className = value;
    else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else element.setAttribute(key, value);
  }
  for (const child of children) {
    element.append(child);
  }
  return element;
}

// ---------------------------------------------------------------------------
// Glyph JSON (the .fontra glyph file) → Path2D, in glyph coordinates.
//
// The file stores "unpacked" contours: points {x, y, type?, smooth?} where
// type is undefined (on-curve), "cubic" or "quad" (off-curve), and isClosed.

function midpoint(a, b) {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

function appendContour(path, contour) {
  let points = contour.points || [];
  if (!points.length) return;
  const isClosed = !!contour.isClosed;
  const firstOnCurve = points.findIndex((p) => !p.type);
  let start;
  if (firstOnCurve === -1) {
    // TrueType-style contour made of quadratic off-curves only.
    if (!isClosed || points.some((p) => p.type !== "quad")) return;
    start = midpoint(points[points.length - 1], points[0]);
  } else {
    if (isClosed) {
      points = [...points.slice(firstOnCurve), ...points.slice(0, firstOnCurve)];
    }
    start = points[0];
    points = points.slice(1);
  }
  path.moveTo(start.x, start.y);
  let offCurves = [];
  const segmentTo = (p) => {
    if (!offCurves.length) {
      path.lineTo(p.x, p.y);
    } else if (offCurves.length === 2 && offCurves.every((c) => c.type === "cubic")) {
      const [c1, c2] = offCurves;
      path.bezierCurveTo(c1.x, c1.y, c2.x, c2.y, p.x, p.y);
    } else if (offCurves.every((c) => c.type === "quad")) {
      for (let i = 0; i < offCurves.length; i++) {
        const c = offCurves[i];
        const end = i === offCurves.length - 1 ? p : midpoint(c, offCurves[i + 1]);
        path.quadraticCurveTo(c.x, c.y, end.x, end.y);
      }
    } else {
      // Unusual off-curve run (e.g. a single cubic control): approximate.
      for (const c of offCurves) path.lineTo(c.x, c.y);
      path.lineTo(p.x, p.y);
    }
    offCurves = [];
  };
  for (const p of points) {
    if (p.type) offCurves.push(p);
    else segmentTo(p);
  }
  if (isClosed) {
    segmentTo(start);
    path.closePath();
  } else if (offCurves.length) {
    segmentTo(offCurves.pop());
  }
}

function transformFromDecomposed(t) {
  // Same composition as Fontra's decomposedToTransform(), as a DOMMatrix.
  const d = {
    translateX: 0, translateY: 0, rotation: 0, scaleX: 1, scaleY: 1,
    skewX: 0, skewY: 0, tCenterX: 0, tCenterY: 0, ...(t || {}),
  };
  const rad = Math.PI / 180;
  let m = new DOMMatrix();
  m = m.translate(d.translateX + d.tCenterX, d.translateY + d.tCenterY);
  m = m.rotate(d.rotation); // degrees
  m = m.scale(d.scaleX, d.scaleY);
  m = m.multiply(new DOMMatrix([1, Math.tan(d.skewY * rad), Math.tan(d.skewX * rad), 1, 0, 0]));
  m = m.translate(-d.tCenterX, -d.tCenterY);
  return m;
}

// Which layer of a stored glyph to show for a wanted layer name: the same
// layer if the version has it, else the layer of its default source (empty
// location), else the first source's layer, else the first layer.
function pickLayerName(glyphJSON, wantedLayerName) {
  const layers = glyphJSON.layers || {};
  if (wantedLayerName && layers[wantedLayerName]) return wantedLayerName;
  const sources = glyphJSON.sources || [];
  const isDefault = (s) => !s.location || Object.keys(s.location).length === 0;
  const source = sources.find((s) => isDefault(s) && layers[s.layerName])
    || sources.find((s) => layers[s.layerName]);
  if (source) return source.layerName;
  const names = Object.keys(layers);
  return names.length ? names[0] : null;
}

// Path of one layer, components included (resolved against the same version,
// through `getGlyph(name)` which must answer synchronously from a cache).
function buildLayerPath(glyphJSON, layerName, getGlyph, depth = 0) {
  const path = new Path2D();
  const layerGlyph = glyphJSON.layers?.[layerName]?.glyph;
  if (!layerGlyph) return path;
  for (const contour of layerGlyph.path?.contours || []) {
    appendContour(path, contour);
  }
  if (depth < MAX_COMPONENT_DEPTH) {
    for (const component of layerGlyph.components || []) {
      const baseGlyph = getGlyph(component.name);
      if (!baseGlyph) continue;
      const baseLayer = pickLayerName(baseGlyph, layerName);
      if (!baseLayer) continue;
      const basePath = buildLayerPath(baseGlyph, baseLayer, getGlyph, depth + 1);
      path.addPath(basePath, transformFromDecomposed(component.transformation));
    }
  }
  return path;
}

// Names of all glyphs referenced as components, recursively, in any layer.
function componentNames(glyphJSON) {
  const names = new Set();
  for (const layer of Object.values(glyphJSON.layers || {})) {
    for (const component of layer.glyph?.components || []) {
      names.add(component.name);
    }
  }
  return names;
}

// ---------------------------------------------------------------------------

class HiveHistoryPanel extends HTMLElement {
  // The three members Fontra's sidebar expects from a panel element:
  identifier = "hive-history";
  iconPath = "/hive/plugin/history.svg";

  constructor(editor) {
    super();
    this.editor = editor;
    this.visible = false;
    this.lastGlyph = null;
    this.head = null;
    this.loading = false;
    this.timer = null;
    this.commits = [];
    this.snapshots = []; // snapshots met in the glyph's history, newest first
    this.expanded = new Set(); // names of the snapshot groups shown open
    // Two previews: the pinned one (click; offers "Restore") and the hovered
    // one (mouse over a row). The canvas shows the hovered one if any.
    this.pinned = null; // {sha, glyphName, glyphs: Map, ready, error, paths: Map, shownLayer, layerNote}
    this.hovered = null;
    this.hoverTimer = null;
    this.previews = new Map(); // `${sha}\n${glyphName}` → preview (kept, bounded)
    this.glyphRequests = new Map(); // `${sha}\n${glyphName}` → Promise<glyph JSON | null>
    this.preloadGeneration = 0;
    this.armedRestore = null; // {sha, timer}
    this.snapshotForm = null; // {info, error, busy} while the form is open

    const shadow = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLES;
    shadow.append(style);

    this.branchElement = el("span", { class: "branch" });
    this.formElement = el("div", { class: "snapshot-form" });
    // Always shown, fixed height, under the list: see renderStatus().
    this.statusElement = el("div", { class: "status" });
    this.listElement = el("div", {
      class: "list",
      // Safety net: rows can be re-created under the pointer (live refresh),
      // in which case their own mouseleave never fires.
      onmouseleave: () => this.unhover(null),
    });
    this.snapshotButton = el(
      "button",
      {
        class: "snapshot-button",
        title: "Group the changes made since the last snapshot under a name",
        onclick: () => this.toggleSnapshotForm(),
      },
      ["Snapshot…"]
    );
    shadow.append(
      el("div", { class: "panel" }, [
        el("div", { class: "header" }, [
          el("span", { class: "title" }, ["Glyph history"]),
          this.branchElement,
          el("span", { class: "tools" }, [
            this.snapshotButton,
            el("button", { class: "refresh", title: "Refresh", onclick: () => this.refresh(true) }, ["↻"]),
          ]),
        ]),
        this.formElement,
        this.listElement,
        this.statusElement,
      ])
    );

    const { name, branch } = parseProjectIdentifier(editor.projectIdentifier || "");
    this.projectName = name;
    this.branch = branch;
    this.branchElement.textContent = `${name} · ${branch}`;

    const settings = editor.sceneController?.sceneSettingsController;
    if (settings) {
      settings.addKeyListener(["selectedGlyphName"], () => {
        const glyphName = this.selectedGlyphName;
        if (this.activePreview && this.activePreview.glyphName !== glyphName) {
          this.clearAllPreviews();
        }
        this.refresh(true);
      });
    }

    this.installPreviewLayer();
  }

  // --- sidebar protocol ----------------------------------------------------

  async toggle(on, focus) {
    this.visible = on;
    if (on) {
      this.refresh(true);
      this.startPolling();
    } else {
      this.stopPolling();
      this.unhover(null);
    }
  }

  startPolling() {
    this.stopPolling();
    this.timer = setInterval(() => this.checkHead(), POLL_INTERVAL_MS);
  }

  stopPolling() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  get selectedGlyphName() {
    return this.editor.sceneController?.sceneSettings?.selectedGlyphName || null;
  }

  apiURL(route, params) {
    const url = new URL(
      `/api/hive/projects/${encodeURIComponent(this.projectName)}/${route}`,
      window.location.origin
    );
    url.searchParams.set("branch", this.branch);
    for (const [key, value] of Object.entries(params)) {
      url.searchParams.set(key, value);
    }
    return url;
  }

  // --- history list --------------------------------------------------------

  // Cheap poll: only the branch head; the list is reloaded when it moved.
  async checkHead() {
    if (!this.visible || this.loading) return;
    try {
      const response = await fetch(this.apiURL("head", {}));
      if (!response.ok) return;
      const { head } = await response.json();
      if (head !== this.head) {
        this.refresh(true);
        if (this.snapshotForm && !this.snapshotForm.busy) this.loadSnapshotInfo();
      }
    } catch (error) {
      // network hiccup: try again at the next tick
    }
  }

  async refresh(force = false) {
    if (!this.visible && !force) return;
    const glyphName = this.selectedGlyphName;
    if (!glyphName) {
      this.lastGlyph = null;
      this.commits = [];
      this.snapshots = [];
      this.render(null, "Select a glyph to see its history.");
      return;
    }
    if (glyphName === this.lastGlyph && !force) return;
    this.loading = true;
    let data;
    try {
      const response = await fetch(this.apiURL("log", { glyph: glyphName, limit: 100 }));
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      data = await response.json();
    } catch (error) {
      this.render(null, `Could not load history (${error.message}).`, true);
      return;
    } finally {
      this.loading = false;
    }
    if (glyphName !== this.selectedGlyphName) return; // the user moved on meanwhile
    if (glyphName !== this.lastGlyph) this.expanded.clear();
    this.lastGlyph = glyphName;
    this.head = data.head;
    this.commits = data.commits;
    this.snapshots = data.snapshots || [];
    this.render(glyphName, data.commits.length ? null : `No history yet for ${glyphName}.`);
    this.preload(glyphName);
  }

  // Rows in display order: changes since the last snapshot, then one
  // (collapsible) group per snapshot, newest first.
  groupCommits() {
    const bySnapshot = new Map(this.snapshots.map((s) => [s.name, []]));
    const loose = [];
    for (const commit of this.commits) {
      const group = commit.snapshot ? bySnapshot.get(commit.snapshot) : null;
      (group || loose).push(commit);
    }
    return { loose, bySnapshot };
  }

  render(glyphName, note, isError = false) {
    const commits = glyphName ? this.commits : [];
    this.listElement.replaceChildren();
    if (glyphName) {
      this.listElement.append(
        el("div", { class: "summary-line" }, [
          `${glyphName} — ${commits.length} version${commits.length === 1 ? "" : "s"}`,
        ])
      );
    }
    if (note) {
      this.listElement.append(el("div", { class: isError ? "empty error" : "empty" }, [note]));
    }
    if (glyphName) {
      const snapshots = this.snapshots;
      const { loose, bySnapshot } = this.groupCommits();
      if (snapshots.length && loose.length) {
        this.listElement.append(
          el("div", { class: "group-label" }, [`Since the last snapshot · ${loose.length}`])
        );
      }
      for (const commit of loose) {
        this.listElement.append(this.commitRow(commit, glyphName, false));
      }
      for (const snapshot of snapshots) {
        const grouped = bySnapshot.get(snapshot.name) || [];
        this.listElement.append(this.snapshotRow(snapshot, grouped, glyphName));
        if (this.expanded.has(snapshot.name)) {
          for (const commit of grouped) {
            this.listElement.append(this.commitRow(commit, glyphName, true));
          }
        }
      }
    }
    this.dropHiddenPreviews();
    this.updateRowHighlights();
    this.renderStatus();
  }

  // A pinned or hovered version whose row is no longer in the list (its
  // group was collapsed, a snapshot just grouped it, another glyph…) is
  // dropped: nothing stays orange on the canvas without a row to explain it.
  dropHiddenPreviews() {
    const visible = new Set(
      [...this.listElement.querySelectorAll("[data-sha]")].map((row) => row.dataset.sha)
    );
    if (this.hovered && !visible.has(this.hovered.sha)) {
      clearTimeout(this.hoverTimer);
      this.hoverTimer = null;
      this.hovered = null;
      this.requestCanvasUpdate();
    }
    if (this.pinned && !visible.has(this.pinned.sha)) {
      this.disarmRestore();
      this.pinned = null;
      this.requestCanvasUpdate();
    }
  }

  // Hover and click behaviour shared by commit and snapshot rows.
  attachPreviewHandlers(row, sha, glyphName, isCurrent) {
    row.dataset.sha = sha;
    if (isCurrent) return;
    row.addEventListener("mouseenter", () => this.hover(sha, glyphName));
    row.addEventListener("mouseleave", () => this.unhover(sha));
    row.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      if (this.pinned?.sha === sha) this.clearPreview();
      else this.startPreview(sha, glyphName);
    });
  }

  // One line: author · message … date. The rest is in the tooltip.
  commitRow(commit, glyphName, nested) {
    const isCurrent = commit.sha === this.head;
    const isPinned = this.pinned?.sha === commit.sha;
    const title = (commit.message || "").split("\n")[0];
    const classes = ["commit"];
    if (nested) classes.push("nested");
    if (isCurrent) classes.push("current");
    if (isPinned) classes.push("previewing");
    const details =
      `${commit.author || "?"} — ${formatFullDate(commit.time)}\n` +
      `${commit.sha.slice(0, 10)}${isCurrent ? " (current)" : ""}\n\n${commit.message || ""}`;
    const row = el(
      "div",
      {
        class: classes.join(" "),
        title: isCurrent
          ? details
          : `${details}\n\nHover to preview this version on the canvas, click to keep it`,
      },
      [
        el("span", { class: "author" }, [commit.author || "?"]),
        el("span", { class: "message" }, [title]),
        ...(isCurrent ? [el("span", { class: "current-mark" }, ["current"])] : []),
        el("span", { class: "when" }, [formatDate(commit.time)]),
      ]
    );
    this.attachPreviewHandlers(row, commit.sha, glyphName, isCurrent);
    return row;
  }

  // One line: ▸ title … versions of the glyph · date.
  snapshotRow(snapshot, grouped, glyphName) {
    const isCurrent = snapshot.sha === this.head;
    const isPinned = this.pinned?.sha === snapshot.sha;
    const isOpen = this.expanded.has(snapshot.name);
    const classes = ["snapshot"];
    if (isCurrent) classes.push("current");
    if (isPinned) classes.push("previewing");
    const count = grouped.length;
    const caret = el(
      "button",
      {
        class: "caret",
        title: count ? (isOpen ? "Hide the versions" : "Show the versions") : "No change to this glyph",
        onclick: (event) => {
          event.stopPropagation();
          if (this.expanded.has(snapshot.name)) this.expanded.delete(snapshot.name);
          else this.expanded.add(snapshot.name);
          this.render(this.lastGlyph, null);
        },
      },
      [isOpen ? "▾" : "▸"]
    );
    if (!count) caret.disabled = true;
    const row = el(
      "div",
      {
        class: classes.join(" "),
        title:
          `Snapshot “${snapshot.title}” (snapshot/${snapshot.name})\n` +
          `${snapshot.author} — ${formatFullDate(snapshot.time)}\n` +
          `${count} version${count === 1 ? "" : "s"} of ${glyphName}, ` +
          `${snapshot.changes} change${snapshot.changes === 1 ? "" : "s"} in the project` +
          (isCurrent ? " (current)" : "\n\nHover to preview the glyph at this snapshot, click to keep it"),
      },
      [
        caret,
        el("span", { class: "name" }, [snapshot.title]),
        ...(isCurrent ? [el("span", { class: "current-mark" }, ["current"])] : []),
        el("span", { class: "count" }, [`${count}`]),
        el("span", { class: "when" }, [formatDate(snapshot.time)]),
      ]
    );
    this.attachPreviewHandlers(row, snapshot.sha, glyphName, isCurrent);
    return row;
  }

  // Classes that follow the hover without rebuilding the list (rebuilding the
  // row under the pointer would fire mouseleave/mouseenter again).
  updateRowHighlights() {
    const hoveredSha = this.hovered?.sha;
    for (const row of this.listElement.querySelectorAll("[data-sha]")) {
      const sha = row.dataset.sha;
      row.classList.toggle("hovering", sha === hoveredSha && sha !== this.pinned?.sha);
    }
  }

  // The preview bar under the list. It is always there and never changes
  // height, so that hovering does not move the rows under the pointer; the
  // "Restore" of the pinned version lives here too (rows stay one line).
  renderStatus() {
    const p = this.activePreview;
    const isHover = !!p && p !== this.pinned;
    this.statusElement.replaceChildren();
    this.statusElement.classList.toggle("hover", isHover);
    this.statusElement.classList.toggle("pinned", !!p && !isHover);
    if (!p) {
      this.statusElement.append(
        el("span", { class: "text hint" }, [
          this.lastGlyph ? "Hover a version to preview it, click to keep it" : "",
        ])
      );
      return;
    }
    let text = `${isHover ? "Hovering" : "Previewing"} ${this.describeRef(p.sha)}`;
    if (!p.ready) text += " — loading…";
    else if (p.error) text += ` — ${p.error}`;
    else if (p.shownLayer) text += ` — layer “${p.shownLayer}”`;
    if (p.ready && p.layerNote) text += ` (${p.layerNote})`;
    this.statusElement.append(
      el("span", { class: p.error ? "text error" : "text", title: text }, [text])
    );
    if (!isHover) {
      if (p.sha !== this.head && !p.error) {
        this.statusElement.append(this.restoreButton(p.sha, p.glyphName));
      }
      this.statusElement.append(
        el("button", { class: "close", title: "Stop previewing", onclick: () => this.clearPreview() }, ["×"])
      );
    }
  }

  describeRef(sha) {
    const snapshot = this.snapshots.find((s) => s.sha === sha);
    return snapshot ? `snapshot “${snapshot.title}”` : sha.slice(0, 10);
  }

  // --- snapshots -----------------------------------------------------------

  toggleSnapshotForm() {
    if (this.snapshotForm) {
      this.snapshotForm = null;
      this.renderSnapshotForm();
      return;
    }
    this.snapshotForm = { info: null, error: null, busy: false };
    this.renderSnapshotForm();
    this.loadSnapshotInfo();
  }

  async loadSnapshotInfo() {
    const form = this.snapshotForm;
    if (!form) return;
    try {
      const response = await fetch(this.apiURL("snapshots", {}));
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      form.info = await response.json();
    } catch (error) {
      form.error = `Could not load the snapshots (${error.message}).`;
    }
    if (this.snapshotForm === form) this.renderSnapshotForm();
  }

  renderSnapshotForm() {
    const form = this.snapshotForm;
    this.snapshotButton.classList.toggle("active", !!form);
    const previousInput = this.formElement.querySelector("input");
    const typed = previousInput ? previousInput.value : "";
    this.formElement.replaceChildren();
    if (!form) return;
    const info = form.info;
    const last = info?.snapshots?.[0];
    let summary;
    if (!info) summary = "Loading…";
    else if (!info.pending) summary = `Nothing changed since snapshot “${last.title}”.`;
    else
      summary =
        `Groups the ${info.pending} change${info.pending === 1 ? "" : "s"} made to the project ` +
        (last ? `since snapshot “${last.title}”` : "so far") +
        (info.pendingAuthors?.length ? ` (by ${info.pendingAuthors.join(", ")})` : "") +
        ". Nothing is deleted: the versions stay in the history, grouped under this name.";
    const input = el("input", {
      type: "text",
      placeholder: "Snapshot name, e.g. Proofs sent to client",
      maxlength: "120",
    });
    input.value = typed;
    const create = el(
      "button",
      { class: "create", onclick: () => this.createSnapshot(input.value) },
      [form.busy ? "Creating…" : "Create snapshot"]
    );
    const canCreate = () => !!(info?.pending && input.value.trim() && !form.busy);
    create.disabled = !canCreate();
    // Keys typed here must not reach the editor's shortcuts (window listener).
    for (const type of ["keydown", "keyup", "keypress"]) {
      input.addEventListener(type, (event) => event.stopPropagation());
    }
    input.addEventListener("input", () => (create.disabled = !canCreate()));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && canCreate()) this.createSnapshot(input.value);
      else if (event.key === "Escape") this.toggleSnapshotForm();
    });
    this.formElement.append(
      el("div", { class: "summary" }, [summary]),
      input,
      el("div", { class: "form-actions" }, [
        create,
        el("button", { class: "cancel", onclick: () => this.toggleSnapshotForm() }, ["Cancel"]),
      ])
    );
    if (form.error) this.formElement.append(el("div", { class: "error" }, [form.error]));
    if (!form.busy && !previousInput) setTimeout(() => input.focus(), 0);
  }

  async createSnapshot(title) {
    const form = this.snapshotForm;
    title = (title || "").trim();
    if (!form || !title || form.busy) return;
    form.busy = true;
    form.error = null;
    this.renderSnapshotForm();
    try {
      const response = await fetch(this.apiURL("snapshot", { name: title }), { method: "POST" });
      if (!response.ok) {
        const text = (await response.text()) || response.statusText;
        throw new Error(`${response.status} ${text}`);
      }
      const data = await response.json();
      this.head = data.head;
      this.snapshotForm = null;
      this.clearAllPreviews(); // the versions just got grouped under the snapshot
      this.renderSnapshotForm();
      await this.refresh(true);
    } catch (error) {
      form.busy = false;
      form.error = `Snapshot failed (${error.message}).`;
      if (this.snapshotForm === form) this.renderSnapshotForm();
    }
  }

  // --- restore -------------------------------------------------------------

  restoreButton(sha, glyphName) {
    const armed = this.armedRestore?.sha === sha;
    const button = el(
      "button",
      {
        class: `restore${armed ? " armed" : ""}`,
        title: armed
          ? "Click again to confirm. A new commit brings the glyph back to this version; nothing is deleted from the history."
          : "Bring the glyph back to this version (as a new commit)",
        onclick: (event) => {
          event.stopPropagation();
          if (this.armedRestore?.sha === sha) {
            this.disarmRestore();
            this.restore(sha, glyphName, button);
          } else {
            this.armRestore(sha);
          }
        },
      },
      [armed ? "Confirm?" : "Restore"]
    );
    return button;
  }

  armRestore(sha) {
    this.disarmRestore();
    this.armedRestore = {
      sha,
      timer: setTimeout(() => {
        this.armedRestore = null;
        this.renderStatus();
      }, CONFIRM_TIMEOUT_MS),
    };
    this.renderStatus();
  }

  disarmRestore() {
    if (this.armedRestore) {
      clearTimeout(this.armedRestore.timer);
      this.armedRestore = null;
    }
  }

  async restore(sha, glyphName, button) {
    button.disabled = true;
    button.textContent = "Restoring…";
    try {
      const response = await fetch(this.apiURL("restore", { glyph: glyphName, ref: sha }), {
        method: "POST",
      });
      if (!response.ok) {
        const text = (await response.text()) || response.statusText;
        throw new Error(`${response.status} ${text}`);
      }
      const data = await response.json();
      this.clearAllPreviews();
      this.head = data.head;
      await this.refresh(true);
    } catch (error) {
      this.render(glyphName, `Restore failed (${error.message}).`, true);
    }
  }

  // --- canvas preview ------------------------------------------------------

  // What the canvas shows: the hovered version, else the pinned one.
  get activePreview() {
    return this.hovered ?? this.pinned;
  }

  installPreviewLayer() {
    const layers = this.editor.visualizationLayers;
    if (!layers?.definitions || layers.definitions.some((d) => d.identifier === PREVIEW_LAYER_ID)) {
      return;
    }
    const definition = {
      identifier: PREVIEW_LAYER_ID,
      name: "Hive: version preview",
      userSwitchable: false,
      zIndex: 250, // above the glyph fill (200), below the editing nodes (500)
      screenParameters: { strokeWidth: 1.5 },
      colors: { fillColor: "rgba(232, 120, 30, 0.28)", strokeColor: "#E8781E" },
      colorsDarkMode: { fillColor: "rgba(255, 150, 60, 0.32)", strokeColor: "#FF9A3C" },
      selectionFunc: (visContext, layer) => {
        const glyphName = this.activePreview?.glyphName;
        if (!glyphName) return [];
        const modes = visContext.glyphsBySelectionMode;
        return [...(modes.editing || []), ...(modes.selected || [])].filter(
          (positionedGlyph) => positionedGlyph.glyphName === glyphName
        );
      },
      draw: (context, positionedGlyph, parameters, model, controller) => {
        const path = this.previewPathFor(positionedGlyph.glyph?.layerName);
        if (!path) return;
        context.save();
        // A hovered version is drawn like a pinned one, only paler.
        if (this.activePreview !== this.pinned) context.globalAlpha = HOVER_ALPHA;
        context.fillStyle = parameters.fillColor;
        context.strokeStyle = parameters.strokeColor;
        context.lineWidth = parameters.strokeWidth;
        context.fill(path);
        context.stroke(path);
        context.restore();
      },
    };
    // Keep the definitions sorted by zIndex, like registerVisualizationLayerDefinition().
    let index = layers.definitions.findIndex((d) => definition.zIndex < d.zIndex);
    if (index === -1) index = layers.definitions.length;
    layers.definitions.splice(index, 0, definition);
    layers.toggle(PREVIEW_LAYER_ID, true);
  }

  requestCanvasUpdate() {
    this.editor.canvasController?.requestUpdate();
  }

  // Pin a version (click): it stays on the canvas and offers "Restore".
  startPreview(sha, glyphName) {
    this.disarmRestore();
    this.pinned = this.getPreview(sha, glyphName);
    this.render(this.lastGlyph, null);
    this.requestCanvasUpdate();
    return this.pinned.loaded;
  }

  // Hover: shown after a short delay, so that sweeping over the list does not
  // fetch every version; immediately when the version is already loaded.
  hover(sha, glyphName) {
    clearTimeout(this.hoverTimer);
    const cached = this.previews.get(previewKey(sha, glyphName));
    if (cached?.ready) {
      this.setHovered(cached);
      return;
    }
    this.hoverTimer = setTimeout(() => {
      this.hoverTimer = null;
      this.setHovered(this.getPreview(sha, glyphName));
    }, HOVER_DELAY_MS);
  }

  // Leaving a row (sha) or the list (null). The pinned preview, and an armed
  // "Restore", are left alone.
  unhover(sha) {
    clearTimeout(this.hoverTimer);
    this.hoverTimer = null;
    if (!this.hovered || (sha !== null && this.hovered.sha !== sha)) return;
    this.setHovered(null);
  }

  setHovered(preview) {
    if (this.hovered === preview) return;
    this.hovered = preview;
    this.updateRowHighlights();
    this.renderStatus();
    this.requestCanvasUpdate();
  }

  // One preview object per (version, glyph), loaded once and kept: glyph
  // files at a commit never change, so hovering again is instant.
  getPreview(sha, glyphName) {
    const key = previewKey(sha, glyphName);
    let preview = this.previews.get(key);
    if (preview) {
      this.previews.delete(key); // most recently used goes last
      this.previews.set(key, preview);
      return preview;
    }
    preview = {
      sha,
      glyphName,
      glyphs: new Map(),
      paths: new Map(),
      ready: false,
      error: null,
      shownLayer: null,
      layerNote: null,
    };
    preview.loaded = this.loadPreview(preview);
    this.previews.set(key, preview);
    while (this.previews.size > MAX_CACHED_PREVIEWS) {
      this.previews.delete(this.previews.keys().next().value);
    }
    return preview;
  }

  async loadPreview(preview) {
    try {
      await this.loadVersion(preview.sha, preview.glyphName, preview.glyphs);
      if (!preview.glyphs.get(preview.glyphName)) {
        throw new Error(`${preview.glyphName} does not exist in this version`);
      }
    } catch (error) {
      preview.error = error.message;
      // A failed load is not kept: hovering again retries.
      this.previews.delete(previewKey(preview.sha, preview.glyphName));
    }
    preview.ready = true;
    if (preview === this.activePreview) {
      this.renderStatus();
      this.requestCanvasUpdate();
    }
  }

  // The glyph and, recursively, its components at one version, into `glyphs`
  // (name → JSON, or null when absent), so drawing needs no further request.
  async loadVersion(sha, glyphName, glyphs) {
    glyphs.set(glyphName, await this.fetchGlyph(sha, glyphName));
    const main = glyphs.get(glyphName);
    if (!main) return;
    const queue = [...componentNames(main)];
    let depth = 0;
    while (queue.length && depth < MAX_COMPONENT_DEPTH) {
      const batch = queue.splice(0, queue.length).filter((n) => !glyphs.has(n));
      const loaded = await Promise.all(batch.map((n) => this.fetchGlyph(sha, n)));
      batch.forEach((n, i) => {
        glyphs.set(n, loaded[i]);
        if (loaded[i]) queue.push(...componentNames(loaded[i]));
      });
      depth++;
    }
  }

  // Glyph JSON at a version, one request per (sha, glyph) however many
  // previews or preloads ask for it.
  fetchGlyph(sha, glyphName) {
    const key = previewKey(sha, glyphName);
    let request = this.glyphRequests.get(key);
    if (!request) {
      request = (async () => {
        const response = await fetch(this.apiURL("glyph", { glyph: glyphName, ref: sha }));
        if (response.status === 404) return null;
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        return await response.json();
      })();
      request.catch(() => this.glyphRequests.delete(key));
      this.glyphRequests.set(key, request);
      while (this.glyphRequests.size > MAX_CACHED_GLYPHS) {
        this.glyphRequests.delete(this.glyphRequests.keys().next().value);
      }
    }
    return request;
  }

  // After the list is shown: load the most recent versions in the background,
  // one after the other, so the first hovers are instant.
  async preload(glyphName) {
    const generation = ++this.preloadGeneration;
    const shas = [
      ...this.commits.filter((c) => c.sha !== this.head).map((c) => c.sha),
      ...this.snapshots.filter((s) => s.sha !== this.head).map((s) => s.sha),
    ].slice(0, PRELOAD_COUNT);
    for (const sha of shas) {
      if (generation !== this.preloadGeneration || glyphName !== this.lastGlyph) return;
      try {
        await this.getPreview(sha, glyphName).loaded;
      } catch (error) {
        return;
      }
    }
  }

  // Called from the draw callback: must be synchronous and cheap.
  previewPathFor(wantedLayerName) {
    const p = this.activePreview;
    if (!p?.ready || p.error) return null;
    const main = p.glyphs.get(p.glyphName);
    if (!main) return null;
    const layerName = pickLayerName(main, wantedLayerName);
    if (!layerName) return null;
    let path = p.paths.get(layerName);
    if (!path) {
      path = buildLayerPath(main, layerName, (name) => p.glyphs.get(name));
      p.paths.set(layerName, path);
    }
    if (p.shownLayer !== layerName) {
      p.shownLayer = layerName;
      p.layerNote = !wantedLayerName
        ? "default layer; the current position is interpolated"
        : wantedLayerName !== layerName
          ? `this version has no layer “${wantedLayerName}”`
          : null;
      // The status line is DOM; update it outside the draw call.
      setTimeout(() => this.renderStatus(), 0);
    }
    return path;
  }

  // Unpin (the × of the status line, a second click on the pinned row).
  clearPreview() {
    this.disarmRestore();
    if (!this.pinned) return;
    this.pinned = null;
    this.render(this.lastGlyph, null);
    this.requestCanvasUpdate();
  }

  // Another glyph was selected, or the glyph was restored.
  clearAllPreviews() {
    clearTimeout(this.hoverTimer);
    this.hoverTimer = null;
    this.hovered = null;
    this.preloadGeneration++;
    this.clearPreview();
    this.updateRowHighlights();
    this.renderStatus();
    this.requestCanvasUpdate();
  }
}

if (!customElements.get("hive-history-panel")) {
  customElements.define("hive-history-panel", HiveHistoryPanel);
}

// Exported for tests (tests/test_plugin_js.py renders them in a headless browser).
export { appendContour, buildLayerPath, pickLayerName, transformFromDecomposed };

export function init(editor, pluginPath) {
  // Which file the editor actually loaded (handy when a browser cache is suspected).
  console.info(`Fontra Hive plug-in: ${import.meta.url}`);
  const panel = new HiveHistoryPanel(editor);
  panel.iconPath = `${pluginPath}/history.svg`;
  editor.addSidebarPanel(panel, "right");
}
