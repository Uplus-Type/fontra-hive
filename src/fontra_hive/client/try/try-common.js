// Fontra Hive, "Try Fontra": what the start page (try-start.js) and the bar
// at the bottom of the editor (try-banner.js) share: the pages' addresses,
// and a way to make elements.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

// Try's start page: opens the font last kept in this browser, or keeps the
// example font first.
export const START_URL = "/try";

// The example font with nothing kept: Fontra alone, in JavaScript, where
// this browser cannot keep files (a private window, say).
export const DEMO_URL = "/editor.html?project=demo%3AMutatorSans&text=%22HAMBURGEFONSTIV%22";

export const EXAMPLE_NAME = "MutatorSans";
export const EXAMPLE_TEXT = "HAMBURGEFONSTIV";

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child !== null && child !== undefined));
  return node;
}

// The editor on a font kept here, with a line of text to start from.
export function editorURL(id, text) {
  const url = new URL("/editor.html", location.href);
  url.searchParams.set("project", "local:" + id);
  if (text) url.searchParams.set("text", JSON.stringify(text));
  return url.pathname + url.search;
}

// The font opened most recently in this browser, from Store.list(): the one
// last opened, or failing that the one last changed.
export function latestProject(projects) {
  let latest = null;
  for (const project of projects) {
    const when = project.opened || project.modified || "";
    if (!latest || when > (latest.opened || latest.modified || "")) latest = project;
  }
  return latest;
}

// Glyph sets a new font can start from: Google Fonts' Latin sets, as Fontra
// lists them (glyphset-presets.json); the editor fetches the list itself.
const GLYPH_SETS_BASE = "https://cdn.jsdelivr.net/gh/googlefonts/glyphsets/data/results/txt/nice-names/";
export const GLYPH_SETS = [
  { key: "latin-core", name: "GF Latin Core", url: GLYPH_SETS_BASE + "GF_Latin_Core.txt", about: "324 glyphs: Western and Central European languages" },
  { key: "latin-kernel", name: "GF Latin Kernel", url: GLYPH_SETS_BASE + "GF_Latin_Kernel.txt", about: "116 glyphs: ASCII and the basics" },
  { key: "none", name: "No glyph set", url: null, about: "an empty glyph list" },
];
export const DEFAULT_GLYPH_SET = "latin-core";

// Fontra's line metrics for a new font (fontra.backends.populate).
const LINE_METRICS = {
  ascender: [750, 16],
  descender: [-250, -16],
  xHeight: [500, 16],
  capHeight: [750, 16],
  baseline: [0, -16],
};

// A new, empty font, as Fontra's "New font" makes one: one source,
// Regular, with Fontra's default line metrics, 1000 units per em, no axis,
// no glyph; and the chosen glyph set as the project's, so the glyph list
// shows what there is to draw. The shape is what fontra-format.js writes.
export function emptyFont(familyName, glyphSetKey = DEFAULT_GLYPH_SET) {
  const glyphSet = GLYPH_SETS.find((set) => set.key === glyphSetKey) || GLYPH_SETS[0];
  const sourceId = Array.from(crypto.getRandomValues(new Uint8Array(4)), (b) =>
    b.toString(16).padStart(2, "0")
  ).join("");
  const lineMetricsHorizontalLayout = {};
  for (const [name, [value, zone]] of Object.entries(LINE_METRICS)) {
    lineMetricsHorizontalLayout[name] = { value, zone };
  }
  const customData = {};
  if (glyphSet.url) {
    customData["fontra.projectGlyphSets"] = [
      { name: glyphSet.name, url: glyphSet.url, dataFormat: "glyph-names", commentChars: "#" },
    ];
  }
  return {
    fontInfo: { familyName },
    axes: { axes: [] },
    sources: { [sourceId]: { name: "Regular", location: {}, lineMetricsHorizontalLayout } },
    unitsPerEm: 1000,
    glyphMap: {},
    glyphs: {},
    glyphInfos: {},
    kerning: {},
    customData,
  };
}

// A name not yet used by a font kept here: "MutatorSans", "MutatorSans 2"…
export function freeName(name, projects) {
  const taken = new Set(projects.map((project) => project.name));
  if (!taken.has(name)) return name;
  for (let n = 2; ; n++) {
    if (!taken.has(`${name} ${n}`)) return `${name} ${n}`;
  }
}
