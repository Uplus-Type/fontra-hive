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
//   - click a version to preview it as an overlay on the glyph canvas itself
//     (a visualization layer added at runtime, drawn in glyph coordinates);
//   - "Restore this version" asks the server to commit that glyph file again
//     on top of the branch: history is never rewritten, and the editor picks
//     the change up like any external change.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const POLL_INTERVAL_MS = 1500; // how often the branch head is checked (a tiny request)
const CONFIRM_TIMEOUT_MS = 5000; // how long "Confirm restore?" stays armed
const PREVIEW_LAYER_ID = "hive.history.preview";
const MAX_COMPONENT_DEPTH = 8;

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
    gap: 0.5em;
    padding: 1em;
    box-sizing: border-box;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.5em;
  }
  .header .title {
    font-weight: bold;
  }
  .header .branch {
    opacity: 0.7;
    font-size: 0.9em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .status {
    display: flex;
    align-items: baseline;
    gap: 0.5em;
    font-size: 0.9em;
    padding: 0.35em 0.6em;
    border-radius: 0.4em;
    background: rgba(232, 120, 30, 0.15);
    border-left: 3px solid #e8781e;
  }
  .status:empty {
    display: none;
  }
  .status .text {
    flex: 1;
  }
  .list {
    flex: 1;
    overflow: hidden auto;
    display: flex;
    flex-direction: column;
    gap: 0.35em;
  }
  .commit {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 0.1em 0.6em;
    padding: 0.45em 0.6em;
    border-radius: 0.4em;
    background: var(--text-input-background-color, rgba(128,128,128,0.12));
    line-height: 1.3;
    cursor: pointer;
  }
  .commit:hover {
    background: var(--text-input-background-color-hover, rgba(128,128,128,0.2));
  }
  .commit.current {
    outline: 1.5px solid var(--foreground-color, currentColor);
    cursor: default;
  }
  .commit.previewing {
    outline: 2px solid #e8781e;
    background: rgba(232, 120, 30, 0.12);
  }
  .commit .message {
    grid-column: 1 / span 2;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .commit .author {
    opacity: 0.75;
    font-size: 0.9em;
  }
  .commit .when {
    opacity: 0.75;
    font-size: 0.9em;
    white-space: nowrap;
  }
  .commit .sha {
    grid-column: 1 / span 2;
    font-family: fontra-ui-mono, monospace;
    font-size: 0.8em;
    opacity: 0.5;
  }
  .commit .actions {
    grid-column: 1 / span 2;
    display: flex;
    gap: 0.4em;
    margin-top: 0.3em;
  }
  .empty {
    opacity: 0.6;
    padding: 0.5em 0;
  }
  .error {
    color: var(--fontra-red-color, #d33);
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
  }
  button:hover {
    opacity: 1;
  }
  button:disabled {
    opacity: 0.4;
    cursor: default;
  }
  button.restore {
    border-color: #e8781e;
    color: #e8781e;
  }
  button.restore.armed {
    background: #e8781e;
    color: white;
  }
  button.close {
    border: none;
    padding: 0 0.2em;
  }
`;

function parseProjectIdentifier(identifier) {
  const at = identifier.indexOf("@");
  return at === -1
    ? { name: identifier, branch: "main" }
    : { name: identifier.slice(0, at), branch: identifier.slice(at + 1) };
}

function formatDate(unixSeconds) {
  const date = new Date(unixSeconds * 1000);
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  const time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return time;
  return `${date.toLocaleDateString(undefined, { day: "numeric", month: "short" })} ${time}`;
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
    this.preview = null; // {sha, glyphName, glyphs: Map, ready, paths: Map, shownLayer}
    this.armedRestore = null; // {sha, timer}

    const shadow = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLES;
    shadow.append(style);

    this.branchElement = el("span", { class: "branch" });
    this.statusElement = el("div", { class: "status" });
    this.listElement = el("div", { class: "list" });
    shadow.append(
      el("div", { class: "panel" }, [
        el("div", { class: "header" }, [
          el("span", { class: "title" }, ["Glyph history"]),
          this.branchElement,
          el("button", { class: "refresh", title: "Refresh", onclick: () => this.refresh(true) }, ["↻"]),
        ]),
        this.statusElement,
        this.listElement,
      ])
    );

    const { name, branch } = parseProjectIdentifier(editor.projectIdentifier || "");
    this.projectName = name;
    this.branch = branch;
    this.branchElement.textContent = `${name} · ${branch}`;

    const settings = editor.sceneController?.sceneSettingsController;
    if (settings) {
      settings.addKeyListener(["selectedGlyphName"], () => {
        if (this.preview && this.preview.glyphName !== this.selectedGlyphName) {
          this.clearPreview();
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
    this.lastGlyph = glyphName;
    this.head = data.head;
    this.commits = data.commits;
    this.render(glyphName, data.commits.length ? null : `No history yet for ${glyphName}.`);
  }

  render(glyphName, note, isError = false) {
    const commits = glyphName ? this.commits : [];
    const head = this.head;
    this.listElement.replaceChildren();
    if (glyphName) {
      this.listElement.append(
        el("div", { class: "empty" }, [
          `${glyphName} — ${commits.length} version${commits.length === 1 ? "" : "s"}`,
        ])
      );
    }
    if (note) {
      this.listElement.append(el("div", { class: isError ? "empty error" : "empty" }, [note]));
    }
    for (const commit of commits) {
      const isCurrent = commit.sha === head;
      const isPreviewing = this.preview?.sha === commit.sha;
      const title = (commit.message || "").split("\n")[0];
      const classes = ["commit"];
      if (isCurrent) classes.push("current");
      if (isPreviewing) classes.push("previewing");
      const row = el(
        "div",
        {
          class: classes.join(" "),
          title: isCurrent ? commit.message || "" : `${commit.message || ""}\n\nClick to preview this version on the canvas`,
          onclick: (event) => {
            if (event.target.closest("button")) return;
            if (isCurrent) return;
            if (isPreviewing) this.clearPreview();
            else this.startPreview(commit.sha, glyphName);
          },
        },
        [
          el("span", { class: "author" }, [commit.author || "?"]),
          el("span", { class: "when" }, [formatDate(commit.time)]),
          el("span", { class: "message" }, [title]),
          el("span", { class: "sha" }, [commit.sha.slice(0, 10) + (isCurrent ? " · current" : "")]),
        ]
      );
      if (isPreviewing && !isCurrent) {
        row.append(el("div", { class: "actions" }, [this.restoreButton(commit.sha, glyphName)]));
      }
      this.listElement.append(row);
    }
    this.renderStatus();
  }

  renderStatus() {
    const p = this.preview;
    this.statusElement.replaceChildren();
    if (!p) return;
    let text = `Previewing ${p.sha.slice(0, 10)}`;
    if (!p.ready) text += " — loading…";
    else if (p.error) text += ` — ${p.error}`;
    else if (p.shownLayer) text += ` — layer “${p.shownLayer}”`;
    if (p.ready && p.layerNote) text += ` (${p.layerNote})`;
    this.statusElement.append(
      el("span", { class: p.error ? "text error" : "text" }, [text]),
      el("button", { class: "close", title: "Stop previewing", onclick: () => this.clearPreview() }, ["×"])
    );
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
      [armed ? "Confirm restore?" : "Restore this version"]
    );
    return button;
  }

  armRestore(sha) {
    this.disarmRestore();
    this.armedRestore = {
      sha,
      timer: setTimeout(() => {
        this.armedRestore = null;
        this.render(this.lastGlyph, null);
      }, CONFIRM_TIMEOUT_MS),
    };
    this.render(this.lastGlyph, null);
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
      this.clearPreview();
      this.head = data.head;
      await this.refresh(true);
    } catch (error) {
      this.render(glyphName, `Restore failed (${error.message}).`, true);
    }
  }

  // --- canvas preview ------------------------------------------------------

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
        const glyphName = this.preview?.glyphName;
        if (!glyphName) return [];
        const modes = visContext.glyphsBySelectionMode;
        return [...(modes.editing || []), ...(modes.selected || [])].filter(
          (positionedGlyph) => positionedGlyph.glyphName === glyphName
        );
      },
      draw: (context, positionedGlyph, parameters, model, controller) => {
        const path = this.previewPathFor(positionedGlyph.glyph?.layerName);
        if (!path) return;
        context.fillStyle = parameters.fillColor;
        context.strokeStyle = parameters.strokeColor;
        context.lineWidth = parameters.strokeWidth;
        context.fill(path);
        context.stroke(path);
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

  async startPreview(sha, glyphName) {
    this.disarmRestore();
    const preview = {
      sha,
      glyphName,
      glyphs: new Map(),
      paths: new Map(),
      ready: false,
      error: null,
      shownLayer: null,
      layerNote: null,
    };
    this.preview = preview;
    this.render(this.lastGlyph, null);
    try {
      await this.loadGlyphAtRef(preview, glyphName);
      const main = preview.glyphs.get(glyphName);
      if (!main) throw new Error(`${glyphName} does not exist in this version`);
      // Components, recursively, so that drawing needs no further request.
      const queue = [...componentNames(main)];
      let depth = 0;
      while (queue.length && depth < MAX_COMPONENT_DEPTH) {
        const batch = queue.splice(0, queue.length).filter((n) => !preview.glyphs.has(n));
        await Promise.all(batch.map((n) => this.loadGlyphAtRef(preview, n)));
        for (const n of batch) {
          const g = preview.glyphs.get(n);
          if (g) queue.push(...componentNames(g));
        }
        depth++;
      }
    } catch (error) {
      preview.error = error.message;
    }
    if (this.preview !== preview) return; // cancelled meanwhile
    preview.ready = true;
    this.renderStatus();
    this.requestCanvasUpdate();
  }

  async loadGlyphAtRef(preview, glyphName) {
    const response = await fetch(this.apiURL("glyph", { glyph: glyphName, ref: preview.sha }));
    if (response.status === 404) {
      preview.glyphs.set(glyphName, null);
      return;
    }
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    preview.glyphs.set(glyphName, await response.json());
  }

  // Called from the draw callback: must be synchronous and cheap.
  previewPathFor(wantedLayerName) {
    const p = this.preview;
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

  clearPreview() {
    this.disarmRestore();
    if (!this.preview) return;
    this.preview = null;
    this.render(this.lastGlyph, null);
    this.requestCanvasUpdate();
  }
}

if (!customElements.get("hive-history-panel")) {
  customElements.define("hive-history-panel", HiveHistoryPanel);
}

// Exported for tests (tests/test_plugin_js.py renders them in a headless browser).
export { appendContour, buildLayerPath, pickLayerName, transformFromDecomposed };

export function init(editor, pluginPath) {
  const panel = new HiveHistoryPanel(editor);
  panel.iconPath = `${pluginPath}/history.svg`;
  editor.addSidebarPanel(panel, "right");
}
