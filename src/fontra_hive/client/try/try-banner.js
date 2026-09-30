// Fontra Hive, "Try Fontra": the bar at the bottom of the editor, and the
// "Your fonts" panel: open a font (.fontra, UFO, designspace with its UFOs,
// TrueType/OpenType; a file, a .zip or a folder), keep it in this browser,
// download it (.fontra, or designspace + UFOs), delete it; or go back to the
// demo font. .fontra is read in JavaScript; other formats by Fontra's own
// Python code, in the browser (try-python-worker.js).
// Loaded at the end of <body>; the engine is try-engine.js.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const STYLE = `
.hive-try, .hive-try-panel {
  font: 13px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  color: #f4f1ea; box-sizing: border-box;
}
.hive-try {
  position: fixed; left: 50%; bottom: 14px; transform: translateX(-50%); z-index: 1000;
  display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; white-space: nowrap;
  max-width: calc(100vw - 32px); padding: 6px 7px 6px 12px; border-radius: 999px;
  background: #222; box-shadow: 0 4px 18px rgba(0,0,0,.25);
}
.hive-try img { width: 20px; height: 20px; }
.hive-try .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; max-width: 22ch; }
.hive-try .status { color: #b9b3a8; }
.hive-try .status.error { color: #ff8a80; }
.hive-try a, .hive-try button, .hive-try-panel button, .hive-try-panel a.button {
  font: inherit; color: #222; background: #f3c744; border: 0; border-radius: 999px;
  padding: 5px 11px; cursor: pointer; text-decoration: none; white-space: nowrap;
}
.hive-try button.plain, .hive-try-panel button.plain {
  background: transparent; color: #f4f1ea; border: 1px solid #555;
}
.hive-try .close { background: transparent; color: #b9b3a8; padding: 5px 7px; }
.hive-try.small .extra, .hive-try.small .status { display: none; }
@media (max-width: 760px) { .hive-try .extra, .hive-try .status { display: none; } }
.hive-try-backdrop {
  position: fixed; inset: 0; z-index: 1001; background: rgba(0,0,0,.35);
  display: flex; align-items: center; justify-content: center; padding: 16px;
}
.hive-try-panel {
  width: min(560px, 100%); max-height: calc(100vh - 32px); overflow: auto;
  background: #222; border-radius: 14px; padding: 20px 22px; box-shadow: 0 10px 40px rgba(0,0,0,.4);
}
.hive-try-panel h2 { font-size: 17px; margin: 0 0 4px; font-weight: 600; }
.hive-try-panel p { margin: 6px 0; color: #cfc9bd; }
.hive-try-panel .note { font-size: 12px; color: #a9a397; }
.hive-try-panel ul { list-style: none; padding: 0; margin: 14px 0; }
.hive-try-panel li {
  display: flex; align-items: center; gap: 8px; padding: 8px 0; border-top: 1px solid #3a3a3a;
}
.hive-try-panel li:last-child { border-bottom: 1px solid #3a3a3a; }
.hive-try-panel li .what { flex: 1; min-width: 0; }
.hive-try-panel li .what b { display: block; overflow: hidden; text-overflow: ellipsis; }
.hive-try-panel li .what span { font-size: 12px; color: #a9a397; }
.hive-try-panel li.current b::after { content: " · open"; font-weight: 400; color: #f3c744; }
.hive-try-panel .actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 6px; }
.hive-try-panel .error { color: #ff8a80; min-height: 1.2em; }
.hive-try-panel .top { display: flex; justify-content: space-between; align-items: start; gap: 8px; }
`;

const DEMO_URL = "/editor.html?project=demo%3AMutatorSans&text=%22HAMBURGEFONSTIV%22";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child !== null && child !== undefined));
  return node;
}

function editorURL(id, text) {
  const url = new URL("/editor.html", location.href);
  url.searchParams.set("project", "local:" + id);
  if (text) url.searchParams.set("text", JSON.stringify(text));
  return url.pathname + url.search;
}

// Something to show on the canvas: the font's own letters.
export function sampleText(glyphMap) {
  const chars = new Set();
  for (const codePoints of Object.values(glyphMap)) {
    for (const cp of codePoints) chars.add(String.fromCodePoint(cp));
  }
  for (const word of ["Hamburgefonstiv", "HAMBURGEFONSTIV", "hamburgefonstiv"]) {
    if ([...word].every((c) => chars.has(c))) return word;
  }
  const letters = [...chars].filter((c) => /\p{L}|\p{N}/u.test(c)).sort();
  return letters.slice(0, 16).join("");
}

function when(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch (error) {
    return "";
  }
}

const FONT_FILES = /\.(ttf|otf|woff2?|ttx|glyphs)$/i;
const GLYPHS = /\.glyphs(package)?(\/|$)/i;

function hasFontra(paths) {
  return paths.some((p) => p.split("/").pop() === "font-data.json");
}

function stemOf(name) {
  return name
    .replace(/\.zip$/i, "")
    .replace(/\.(fontra|ufo|designspace|ttf|otf|woff2?|ttx|glyphs|glyphspackage)$/i, "");
}

// What the visitor picked, as the files of a .fontra package, and a name
// for it: read here for .fontra, converted by Python for anything else.
async function packageFromPick(fileList, onProgress) {
  const Format = window.HiveTryFormat;
  const tryAPI = window.hiveTry;
  const picked = [...fileList];
  if (!picked.length) throw new Error("Nothing was picked.");
  if (picked.length === 1 && !picked[0].webkitRelativePath) {
    const file = picked[0];
    if (/\.zip$/i.test(file.name)) {
      const files = await Format.unzip(file);
      if (hasFontra([...files.keys()])) return { files, label: stemOf(file.name) };
      const glyphs = [...files.keys()].some((p) => GLYPHS.test(p));
      return {
        files: await tryAPI.convertToFontra(file.name, file, onProgress, { glyphs }),
        label: stemOf(file.name),
      };
    }
    if (FONT_FILES.test(file.name)) {
      const glyphs = GLYPHS.test(file.name);
      return {
        files: await tryAPI.convertToFontra(file.name, file, onProgress, { glyphs }),
        label: stemOf(file.name),
      };
    }
    if (/\.designspace$/i.test(file.name)) {
      throw new Error("A designspace needs its UFOs: open the folder that holds them, or a .zip of it.");
    }
    throw new Error("Open a .zip, a .glyphs, .ttf, .otf, .woff, .woff2 or .ttx file, or a folder.");
  }
  // A folder: a .fontra package is read here; anything else is zipped and
  // converted, the whole folder (a designspace needs its UFOs).
  const files = new Map();
  for (const file of picked) files.set(file.webkitRelativePath || file.name, file);
  const folder = (picked[0].webkitRelativePath || "").split("/")[0] || "Font";
  if (hasFontra([...files.keys()])) return { files, label: stemOf(folder) };
  onProgress?.("Reading the folder…");
  const zipped = await Format.zip(files);
  const glyphs = [...files.keys()].some((p) => GLYPHS.test(p));
  return {
    files: await tryAPI.convertToFontra(folder + ".zip", zipped, onProgress, { glyphs }),
    label: stemOf(folder),
  };
}

function packageName(files, label) {
  const root = window.HiveTryFormat.findRoot([...files.keys()]);
  const folder = root.replace(/\/$/, "").split("/").pop();
  if (folder && folder !== "converted.fontra") return stemOf(folder) || label || "Font";
  return label || "Font";
}

export async function importPicked(fileList, onProgress) {
  const Format = window.HiveTryFormat;
  const { files, label } = await packageFromPick(fileList, onProgress);
  const { font, images } = await Format.readPackage(files);
  const name = (font.fontInfo && font.fontInfo.familyName) || packageName(files, label);
  const normalized = Format.writePackage(font);
  images.forEach((blob, fileName) => normalized.set(Format.IMAGES_DIR + fileName, blob));
  const id = await window.hiveTry.createLocal(name, normalized, onProgress);
  return { id, text: sampleText(font.glyphMap) };
}

function openPanel() {
  const Store = window.HiveTryStore;
  const current = window.hiveTry?.localId;
  const error = el("p", { class: "error", role: "alert" });
  const progress = el("p", { class: "note", role: "status" });
  const list = el("ul");
  const fileInput = el("input", {
    type: "file",
    accept: ".zip,.glyphs,.ttf,.otf,.woff,.woff2,.ttx,application/zip",
    hidden: "",
  });
  const folderInput = el("input", { type: "file", webkitdirectory: "", hidden: "" });

  const close = () => backdrop.remove();
  const go = (url) => {
    if (window.hiveTry) window.hiveTry.TryFont.edited = false;
    location.href = url;
  };

  async function onPick(input) {
    if (!input.files.length) return;
    error.textContent = "";
    progress.textContent = "Opening…";
    try {
      const { id, text } = await importPicked(input.files, (text) => {
        progress.textContent = text;
      });
      go(editorURL(id, text));
    } catch (e) {
      console.error(e);
      progress.textContent = "";
      error.textContent = e.message || String(e);
    } finally {
      input.value = "";
    }
  }
  fileInput.addEventListener("change", () => onPick(fileInput));
  folderInput.addEventListener("change", () => onPick(folderInput));

  async function fill() {
    list.replaceChildren();
    const projects = Store.available() ? await Store.list() : [];
    if (!projects.length) {
      list.append(el("li", {}, el("span", { class: "what" }, el("span", {}, "No fonts in this browser yet."))));
    }
    for (const project of projects) {
      list.append(
        el(
          "li",
          { class: project.id === current ? "current" : "" },
          el("span", { class: "what" }, el("b", {}, project.name), el("span", {}, "Changed " + when(project.modified))),
          project.id === current
            ? null
            : el("button", { type: "button", onclick: () => go(editorURL(project.id)) }, "Open"),
          el(
            "button",
            {
              type: "button",
              class: "plain",
              title: "Delete from this browser",
              onclick: async () => {
                if (!confirm(`Delete “${project.name}” from this browser? Export it first (File › Export as) to keep it.`)) return;
                await window.hiveTry.deleteLocal(project.id);
                if (project.id === current) go(DEMO_URL);
                else fill();
              },
            },
            "Delete"
          )
        )
      );
    }
  }

  const backdrop = el(
    "div",
    { class: "hive-try-backdrop", onclick: (event) => event.target === backdrop && close() },
    el(
      "div",
      { class: "hive-try-panel", role: "dialog", "aria-label": "Your fonts" },
      el(
        "div",
        { class: "top" },
        el("h2", {}, "Your fonts in this browser"),
        el("button", { type: "button", class: "plain", onclick: close }, "Close")
      ),
      el(
        "p",
        {},
        "Open a font, work on it, download it when you like. It never leaves your computer: " +
          "even converting a UFO or a TrueType font happens here, in your browser."
      ),
      list,
      el(
        "div",
        { class: "actions" },
        el(
          "button",
          { type: "button", title: ".zip (of a .fontra, a UFO or a designspace with its UFOs), .ttf, .otf, .woff2", onclick: () => fileInput.click() },
          "Open a file…"
        ),
        el(
          "button",
          { type: "button", class: "plain", title: "a .fontra, a .ufo, a .glyphspackage, or the folder of a designspace and its UFOs", onclick: () => folderInput.click() },
          "Open a folder…"
        ),
        el("button", { type: "button", class: "plain", onclick: () => go(DEMO_URL) }, "Demo font")
      ),
      progress,
      error,
      el(
        "p",
        { class: "note" },
        "Opens .fontra, Glyphs (.glyphs, .glyphspackage), UFO, designspace + UFOs, TrueType and OpenType. " +
          "Fonts are kept by this browser only: clearing this site's data deletes them. File › Export as saves a copy on your computer."
      ),
      fileInput,
      folderInput
    )
  );
  document.body.append(backdrop);
  if (!Store.available()) {
    error.textContent = "This browser cannot keep files for this site (private window?).";
  }
  fill();
}

function start() {
  document.head.append(el("style", {}, STYLE));
  const tryAPI = window.hiveTry;
  const isLocal = !!tryAPI?.localId;
  const name = el("span", { class: "name" }, isLocal ? "…" : "Try Fontra");
  const status = el(
    "span",
    { class: "status", role: "status" },
    isLocal ? "loading…" : "demo · nothing is saved"
  );
  const keep = el(
    "button",
    {
      type: "button",
      class: "plain extra",
      title: "Keep this font, with your edits, in this browser",
      onclick: async () => {
        try {
          const id = await tryAPI.saveCopy("MutatorSans", (text) => (status.textContent = text));
          tryAPI.TryFont.edited = false;
          location.href = editorURL(id, "HAMBURGEFONSTIV");
        } catch (e) {
          console.error(e);
          status.classList.add("error");
          status.textContent = "could not keep it: " + (e.message || e);
        }
      },
    },
    "Keep a copy"
  );
  const bar = el(
    "div",
    { class: "hive-try", role: "region", "aria-label": "Try Fontra" },
    el("img", { src: "/hive/icons/hive-icon.svg", alt: "" }),
    name,
    status,
    el("button", { type: "button", class: "plain", onclick: openPanel }, "Your fonts"),
    isLocal ? null : keep,
    el(
      "a",
      {
        href: "/",
        class: "extra",
        title: "With a Fontra Hive account, your fonts are online: share them, work on them together, with branches and reviews.",
      },
      "Collaborate online"
    ),
    el(
      "button",
      { class: "close", type: "button", title: "Hide", "aria-label": "Hide", onclick: () => bar.classList.toggle("small") },
      "–"
    )
  );
  if (isLocal) {
    tryAPI
      .localInfo()
      .then((info) => {
        name.textContent = info.name;
      })
      .catch(() => {
        name.textContent = "Font not found";
        status.textContent = "not in this browser";
        status.classList.add("error");
        openPanel();
      });
  }
  window.addEventListener("hive-try-edit", () => {
    if (!isLocal) status.textContent = "demo · a reload starts over";
  });
  // A font kept here: loading Python, then saving to the browser's storage.
  window.addEventListener("hive-try-progress", (event) => {
    if (event.detail) status.textContent = event.detail.replace(/…$/, "") + "…";
    else if (isLocal) status.textContent = "kept in this browser";
  });
  window.addEventListener("hive-try-status", (event) => {
    const { text } = event.detail;
    status.classList.toggle("error", text === "error");
    status.textContent =
      text === "saving" ? "saving…" : text === "saved" ? "saved in this browser" : "could not save: download a copy";
  });
  document.body.append(bar);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
