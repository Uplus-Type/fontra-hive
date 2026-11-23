// Fontra Hive: the history of the whole font, beyond the glyph.
//
// Shared by the editor's "Font history" panel (init.js) and the History
// section of Font info (views/hive-views.js). Plain JavaScript, no Fontra
// import (see init.js):
//
//   - each change of the font's history carries what it changed beyond the
//     glyph, in words ("Axes: wght 100–900 → 200–900"): the server's
//     fontdiff.py, one sentence per change (commit.font: area, domain,
//     summary). summaryBlock() shows two lines, the rest behind a ▸;
//   - openFontDiff() asks the server for the detail of a version, of a
//     snapshot or of a snapshot compared with now (GET …/font-diff): before
//     and after, field by field; the features as lines; the glyphs changed,
//     each a link to the glyph.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

// The font's history, filtered (the log route's "kind").
export const FILTERS = [
  { key: "all", label: "All changes", title: "Every change of the font" },
  { key: "glyphs", label: "Glyphs", title: "The changes to the glyphs and their info" },
  {
    key: "font",
    label: "Beyond the glyph",
    title: "The changes to the font info, axes, sources, features, kerning and plug-ins' data",
  },
];

// Font info's panels (Fontra's panel ids) and the areas of fontdiff.py
// each one sets.
export const PANEL_AREAS = {
  "font-info-panel": ["fontInfo"],
  "axes-panel": ["axes"],
  "sources-panel": ["sources"],
  "opentype-feature-code-panel": ["features"],
  "conditional-substitutions-panel": ["conditionalSubstitutions"],
  "cross-axis-mapping-panel": ["mappings"],
  "development-status-definitions-panel": ["statusDefinitions"],
};

// How many sentences show before the ▸.
export const SHOWN_LINES = 2;

// What a commit changed beyond the glyph (the glyph info is the glyphs').
export function beyondTheGlyph(commit) {
  return (commit?.font || []).filter((change) => change.domain !== "edit");
}

// ``areas``: only the changes of those areas (null: all of them).
export function summaryLines(commit, areas = null) {
  return beyondTheGlyph(commit)
    .filter((change) => !areas || areas.includes(change.area))
    .map((change) => change.summary);
}

function el(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") element.className = value;
    else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) element.setAttribute(key, value);
  }
  for (const child of children) {
    if (child !== null && child !== undefined && child !== false) element.append(child);
  }
  return element;
}

// The sentences of a change, under its row: SHOWN_LINES of them, the rest
// behind "▸ N more" (open: ``isOpen``; ``onToggle`` is told, the caller
// keeps the state so that a refreshed list stays as it was).
export function summaryBlock(lines, { isOpen = false, onToggle = null } = {}) {
  if (!lines.length) return null;
  const hidden = lines.length - SHOWN_LINES;
  const shown = isOpen || hidden <= 0 ? lines : lines.slice(0, SHOWN_LINES);
  const block = el(
    "div",
    { class: "font-summary" },
    shown.map((line) => el("div", { class: "font-summary-line", title: line }, [line]))
  );
  if (hidden > 0) {
    block.append(
      el(
        "button",
        {
          class: "font-summary-more",
          title: isOpen ? "Show less" : `Show the ${hidden} other change${hidden === 1 ? "" : "s"}`,
          onclick: (event) => {
            event.stopPropagation();
            onToggle?.(!isOpen);
          },
        },
        [isOpen ? "▾ less" : `▸ ${hidden} more`]
      )
    );
  }
  return block;
}

// The styles of the summaries, for a shadow root or a page.
export const SUMMARY_STYLES = `
  .font-summary {
    flex-basis: 100%;
    min-width: 0;
    padding: 0 0 0.35em 0.2em;
    font-size: 0.92em;
    line-height: 1.35em;
    white-space: nowrap;
  }
  .font-summary-line {
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .font-summary-line::before {
    content: "›";
    display: inline-block;
    width: 0.9em;
    opacity: 0.5;
  }
  button.font-summary-more {
    border: none;
    padding: 0 0 0 0.9em;
    font-size: 0.95em;
    opacity: 0.65;
    background: none;
    color: inherit;
    cursor: pointer;
  }
  button.font-summary-more:hover {
    opacity: 1;
  }
  .filter {
    display: flex;
    gap: 0;
    border: 1px solid rgba(128, 128, 128, 0.45);
    border-radius: 0.4em;
    overflow: hidden;
    flex: none;
  }
  .filter button {
    flex: 1;
    border: none;
    border-radius: 0;
    padding: 0.2em 0.5em;
    opacity: 0.75;
  }
  .filter button + button {
    border-left: 1px solid rgba(128, 128, 128, 0.45);
  }
  .filter button.selected {
    background: rgba(var(--hive-accent-rgb, 200, 146, 34), 0.22);
    opacity: 1;
  }
`;

// --- the detail ------------------------------------------------------------------------

const DIALOG_STYLES = `
  .hive-fontdiff-backdrop { position: fixed; inset: 0; background: #8888; z-index: 1000; }
  .hive-fontdiff { position: fixed; left: 50%; top: 60px; transform: translateX(-50%); z-index: 1001;
    width: min(760px, calc(100vw - 40px)); max-height: calc(100vh - 100px); display: flex;
    flex-direction: column; background: var(--ui-element-background-color, white);
    color: var(--ui-element-foreground-color, black); border-radius: 0.5em; box-shadow: 1px 3px 12px #0007;
    font-family: fontra-ui-regular, sans-serif; font-size: 13px; box-sizing: border-box; }
  .hive-fontdiff header { display: flex; align-items: center; gap: 0.8em; padding: 16px 20px 10px; }
  .hive-fontdiff header h3 { margin: 0; font-size: 1.2em; flex: 1; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hive-fontdiff header .who { opacity: 0.65; font-size: 0.92em; white-space: nowrap; }
  .hive-fontdiff button { font: inherit; font-size: 0.92em; color: inherit; cursor: pointer;
    background: none; border: 1px solid rgba(128,128,128,0.5); border-radius: 0.35em; padding: 2px 10px; }
  .hive-fontdiff button:hover { border-color: currentColor; }
  .hive-fontdiff button.close { border: none; font-size: 1.3em; padding: 0 4px; opacity: 0.6; }
  .hive-fontdiff button.close:hover { opacity: 1; }
  .hive-fontdiff .modes { display: flex; gap: 0; padding: 0 20px 8px; }
  .hive-fontdiff .modes button { border-radius: 0; }
  .hive-fontdiff .modes button:first-child { border-radius: 0.35em 0 0 0.35em; }
  .hive-fontdiff .modes button:last-child { border-radius: 0 0.35em 0.35em 0; border-left: none; }
  .hive-fontdiff .modes button.selected { background: rgba(200, 146, 34, 0.22); }
  .hive-fontdiff .content { overflow: auto; padding: 4px 20px 20px; }
  .hive-fontdiff .note { opacity: 0.65; margin: 6px 0; }
  .hive-fontdiff .error { color: var(--fontra-red-color, #d33); }
  .hive-fontdiff section { margin: 14px 0 0; }
  .hive-fontdiff section.other h4 { opacity: 0.75; }
  .hive-fontdiff h4 { margin: 0 0 6px; font-size: 1em; }
  .hive-fontdiff .group-title { margin: 18px 0 0; font-size: 0.85em; opacity: 0.6;
    text-transform: uppercase; letter-spacing: 0.04em; }
  .hive-fontdiff table { border-collapse: collapse; width: 100%; table-layout: fixed; }
  .hive-fontdiff th { text-align: left; font-weight: normal; opacity: 0.6; font-size: 0.88em; padding: 2px 8px; }
  .hive-fontdiff td { padding: 3px 8px; vertical-align: top; border-top: 1px solid rgba(128,128,128,0.18);
    overflow-wrap: anywhere; }
  .hive-fontdiff td.label { width: 30%; }
  .hive-fontdiff td.before { background: rgba(220, 70, 70, 0.10); }
  .hive-fontdiff td.after { background: rgba(60, 160, 90, 0.12); }
  .hive-fontdiff td.absent { background: none; }
  .hive-fontdiff td .absent { opacity: 0.45; }
  .hive-fontdiff pre.lines { margin: 0; padding: 6px 0; font-family: ui-monospace, Menlo, monospace;
    font-size: 0.92em; line-height: 1.45; border-radius: 0.35em; background: rgba(128,128,128,0.08);
    overflow-x: auto; }
  .hive-fontdiff pre.lines div { padding: 0 10px; white-space: pre; }
  .hive-fontdiff pre.lines .add { background: rgba(60, 160, 90, 0.16); }
  .hive-fontdiff pre.lines .del { background: rgba(220, 70, 70, 0.14); }
  .hive-fontdiff pre.lines .hunk { opacity: 0.5; }
  .hive-fontdiff .glyphs { display: flex; flex-wrap: wrap; gap: 5px; }
  .hive-fontdiff .glyphs button { padding: 1px 8px; border-radius: 1em; }
  .hive-fontdiff .glyphs button.added { border-color: rgba(60, 160, 90, 0.8); }
  .hive-fontdiff .glyphs button.removed { opacity: 0.6; text-decoration: line-through; cursor: default; }
`;

function ensureDialogStyle() {
  if (!document.getElementById("hive-fontdiff-style")) {
    const style = document.createElement("style");
    style.id = "hive-fontdiff-style";
    style.textContent = DIALOG_STYLES;
    document.head.append(style);
  }
}

function valueCell(value, kind) {
  const absent = value === null || value === undefined;
  return el("td", { class: absent ? `${kind} absent` : kind }, [
    value === null || value === undefined ? el("span", { class: "absent" }, ["—"]) : String(value),
  ]);
}

function changeSection(change, { other = false } = {}) {
  const section = el("section", { class: other ? "other" : "" }, [el("h4", {}, [change.summary])]);
  if (change.lines) {
    const lines = el("pre", { class: "lines" });
    for (const [op, text] of change.lines) {
      if (op === "@") {
        lines.append(el("div", { class: "hunk" }, ["⋯"]));
      } else {
        const kind = op === "+" ? "add" : op === "-" ? "del" : "same";
        lines.append(el("div", { class: kind }, [`${op === " " ? " " : op} ${text}`]));
      }
    }
    section.append(lines);
  }
  if (change.fields?.length) {
    section.append(
      el("table", {}, [
        el("tr", {}, [el("th", {}, [""]), el("th", {}, ["Before"]), el("th", {}, ["After"])]),
        ...change.fields.map((f) =>
          el("tr", {}, [
            el("td", { class: "label" }, [f.label]),
            valueCell(f.before, "before"),
            valueCell(f.after, "after"),
          ])
        ),
      ])
    );
  }
  if (change.truncated) {
    section.append(el("div", { class: "note" }, ["Too long to show whole: the first part only."]));
  }
  return section;
}

// The detail of a font-diff answer. ``areas``: the changes of these areas
// first (Font info's open panel), the others under "Also in this version".
// ``onGlyph(name)``: a glyph's link (null: the glyphs are listed, not links).
export function renderFontDiff(data, { areas = null, onGlyph = null, otherLabel = "Also in this version" } = {}) {
  const content = el("div", { class: "content" });
  const beyond = (data.changes || []).filter((c) => c.domain !== "edit");
  const glyphInfo = (data.changes || []).filter((c) => c.domain === "edit");
  const first = areas ? beyond.filter((c) => areas.includes(c.area)) : beyond;
  const rest = areas ? beyond.filter((c) => !areas.includes(c.area)) : [];
  if (!beyond.length) {
    content.append(el("div", { class: "note" }, ["Nothing changed beyond the glyph."]));
  }
  for (const change of first) content.append(changeSection(change));
  if (rest.length) {
    content.append(el("div", { class: "group-title" }, [otherLabel]));
    for (const change of rest) content.append(changeSection(change, { other: true }));
  }
  const glyphs = data.glyphs || [];
  if (glyphs.length || glyphInfo.length) {
    const total = data.glyphsTotal ?? glyphs.length;
    content.append(
      el("div", { class: "group-title" }, [`Glyphs changed · ${total}`])
    );
    if (glyphs.length) {
      content.append(
        el(
          "div",
          { class: "glyphs" },
          glyphs.map((glyph) =>
            el(
              "button",
              {
                class: `glyph ${glyph.kind}`,
                title:
                  glyph.kind === "removed"
                    ? `${glyph.name} was deleted`
                    : onGlyph
                      ? `Show ${glyph.name}${glyph.kind === "added" ? " (new)" : ""}`
                      : glyph.name,
                onclick: () => glyph.kind !== "removed" && onGlyph?.(glyph.name),
              },
              [glyph.name]
            )
          )
        )
      );
      if (total > glyphs.length) {
        content.append(el("div", { class: "note" }, [`And ${total - glyphs.length} more.`]));
      }
    }
    for (const change of glyphInfo) content.append(changeSection(change, { other: true }));
  }
  return content;
}

// The detail in a dialog over the page. ``modes``: [{label, from, to,
// otherLabel}] — a version has one, a snapshot two ("This snapshot",
// "Compare with now"). Returns {element, close, ready, show(index)}
// (ready: the first one loaded).
export function openFontDiff({ projectName, branch, title, who = "", modes, areas = null, onGlyph = null }) {
  ensureDialogStyle();
  const backdrop = el("div", { class: "hive-fontdiff-backdrop", onclick: () => close() });
  let content = el("div", { class: "content" });
  const modeBar = el("div", { class: "modes" });
  const dialog = el("div", { class: "hive-fontdiff", role: "dialog" }, [
    el("header", {}, [
      el("h3", { title }, [title]),
      who ? el("span", { class: "who" }, [who]) : null,
      el("button", { class: "close", title: "Close", onclick: () => close() }, ["×"]),
    ]),
    modes.length > 1 ? modeBar : null,
    content,
  ]);
  // Keys typed here must not reach the editor's shortcuts.
  for (const type of ["keydown", "keyup", "keypress"]) {
    dialog.addEventListener(type, (event) => event.stopPropagation());
  }
  const onDocumentKey = (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
    }
  };
  document.addEventListener("keydown", onDocumentKey, true);
  const close = () => {
    backdrop.remove();
    dialog.remove();
    document.removeEventListener("keydown", onDocumentKey, true);
  };
  const replaceContent = (node) => {
    content.replaceWith(node);
    content = node;
  };
  let current = null;
  const show = async (mode) => {
    current = mode;
    modeBar.replaceChildren(
      ...modes.map((m) =>
        el(
          "button",
          { class: m === mode ? "selected" : "", title: m.title || "", onclick: () => m !== current && show(m) },
          [m.label]
        )
      )
    );
    replaceContent(el("div", { class: "content" }, [el("div", { class: "note" }, ["Loading…"])]));
    const url = new URL(
      `/api/hive/projects/${encodeURIComponent(projectName)}/font-diff`,
      window.location.origin
    );
    url.searchParams.set("branch", branch);
    if (mode.from) url.searchParams.set("from", mode.from);
    if (mode.to) url.searchParams.set("to", mode.to);
    let node;
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) {
        throw new Error(`${response.status} ${(await response.text()) || response.statusText}`);
      }
      node = renderFontDiff(await response.json(), {
        areas,
        otherLabel: mode.otherLabel,
        onGlyph: onGlyph
          ? (name) => {
              close();
              onGlyph(name);
            }
          : null,
      });
    } catch (error) {
      node = el("div", { class: "content" }, [
        el("div", { class: "note error" }, [`Could not load the changes (${error.message}).`]),
      ]);
    }
    if (current === mode) replaceContent(node);
  };
  document.body.append(backdrop, dialog);
  const ready = show(modes[0]);
  return { element: dialog, close, ready, show: (index) => show(modes[index]) };
}
