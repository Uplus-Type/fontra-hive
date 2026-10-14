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

// A name not yet used by a font kept here: "MutatorSans", "MutatorSans 2"…
export function freeName(name, projects) {
  const taken = new Set(projects.map((project) => project.name));
  if (!taken.has(name)) return name;
  for (let n = 2; ; n++) {
    if (!taken.has(`${name} ${n}`)) return `${name} ${n}`;
  }
}
