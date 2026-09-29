// Fontra Hive editor plug-in: "Glyph history" sidebar panel.
//
// Loaded by Fontra's editor plug-in mechanism (Application settings → Plugins,
// address "/hive/plugin"): the editor fetches plugin.json, imports this module
// and calls init(editor, pluginPath). The module is plain JavaScript on
// purpose — a runtime plug-in cannot import Fontra's bundled modules — and only
// uses the editor's public objects: fontController, sceneSettingsController
// and addSidebarPanel().
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const POLL_INTERVAL_MS = 1500; // how often the branch head is checked (a tiny request)

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
  }
  .commit.current {
    outline: 1.5px solid var(--foreground-color, currentColor);
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
  .empty {
    opacity: 0.6;
    padding: 0.5em 0;
  }
  .error {
    color: var(--fontra-red-color, #d33);
  }
  button.refresh {
    font: inherit;
    font-size: 0.85em;
    background: none;
    border: 1px solid currentColor;
    border-radius: 0.3em;
    color: inherit;
    opacity: 0.7;
    padding: 0.1em 0.5em;
    cursor: pointer;
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

    const shadow = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLES;
    shadow.append(style);

    this.branchElement = el("span", { class: "branch" });
    this.listElement = el("div", { class: "list" });
    shadow.append(
      el("div", { class: "panel" }, [
        el("div", { class: "header" }, [
          el("span", { class: "title" }, ["Glyph history"]),
          this.branchElement,
          el("button", { class: "refresh", title: "Refresh", onclick: () => this.refresh(true) }, ["↻"]),
        ]),
        this.listElement,
      ])
    );

    const { name, branch } = parseProjectIdentifier(editor.projectIdentifier || "");
    this.projectName = name;
    this.branch = branch;
    this.branchElement.textContent = `${name} · ${branch}`;

    const settings = editor.sceneController?.sceneSettingsController;
    if (settings) {
      settings.addKeyListener(["selectedGlyphName"], () => this.refresh(true));
    }
  }

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
      this.render([], null, "Select a glyph to see its history.");
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
      this.render([], null, `Could not load history (${error.message}).`, true);
      return;
    } finally {
      this.loading = false;
    }
    if (glyphName !== this.selectedGlyphName) return; // the user moved on meanwhile
    this.lastGlyph = glyphName;
    this.head = data.head;
    this.render(
      data.commits,
      data.head,
      data.commits.length ? null : `No history yet for ${glyphName}.`,
      false,
      glyphName
    );
  }

  render(commits, head, note, isError = false, glyphName = null) {
    this.listElement.replaceChildren();
    if (glyphName) {
      this.listElement.append(el("div", { class: "empty" }, [`${glyphName} — ${commits.length} version${commits.length === 1 ? "" : "s"}`]));
    }
    if (note) {
      this.listElement.append(el("div", { class: isError ? "empty error" : "empty" }, [note]));
    }
    for (const commit of commits) {
      const isCurrent = commit.sha === head;
      const title = (commit.message || "").split("\n")[0];
      this.listElement.append(
        el("div", { class: `commit${isCurrent ? " current" : ""}`, title: commit.message || "" }, [
          el("span", { class: "author" }, [commit.author || "?"]),
          el("span", { class: "when" }, [formatDate(commit.time)]),
          el("span", { class: "message" }, [title]),
          el("span", { class: "sha" }, [commit.sha.slice(0, 10) + (isCurrent ? " · current" : "")]),
        ])
      );
    }
  }
}

if (!customElements.get("hive-history-panel")) {
  customElements.define("hive-history-panel", HiveHistoryPanel);
}

export function init(editor, pluginPath) {
  const panel = new HiveHistoryPanel(editor);
  panel.iconPath = `${pluginPath}/history.svg`;
  editor.addSidebarPanel(panel, "right");
}
