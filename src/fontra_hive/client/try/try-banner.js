// Fontra Hive, "Try Fontra": the bar at the bottom of the editor, and the
// "Your fonts" panel: open a font (.fontra, UFO, designspace with its UFOs,
// TrueType/OpenType; a file, a .zip or a folder), keep it in this browser,
// download it (.fontra, or designspace + UFOs), delete it; or a fresh copy
// of the example font. .fontra is read in JavaScript; other formats by
// Fontra's own Python code, in the browser (try-python-worker.js).
// Loaded at the end of <body>; the engine is try-engine.js; the start page
// (/try) is try-start.js.
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
.hive-try-panel .new-font { margin: 10px 0 4px; padding: 12px 14px; border-radius: 10px; background: #2c2c2c; }
.hive-try-panel .new-font label { display: block; margin: 6px 0; color: #cfc9bd; }
.hive-try-panel .new-font input, .hive-try-panel .new-font select {
  display: block; width: 100%; box-sizing: border-box; margin-top: 3px; font: inherit;
  color: #f4f1ea; background: #1c1c1c; border: 1px solid #555; border-radius: 6px; padding: 6px 8px;
}
.hive-try-panel .new-font .actions { margin: 10px 0 0; }
`;

import {
  DEFAULT_GLYPH_SET,
  EXAMPLE_NAME,
  EXAMPLE_TEXT,
  GLYPH_SETS,
  START_URL,
  editorURL,
  el,
  emptyFont,
  freeName,
} from "./try-common.js";
import { hereView, tourOnLoad } from "./try-tour.js";
import { openTourMenu } from "./try-tour-menu.js";

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
// Fonts that are folders, picked as a file (a package on a Mac).
const FOLDER_FONTS = /\.(ufo|fontra|glyphspackage)$/i;

// What is said where a font is picked, and when a designspace comes alone.
export const DESIGNSPACE_HINT = "Designspace: upload a .zip of the folder with its UFOs, or open that folder.";
export const DESIGNSPACE_ALONE =
  "A .designspace needs its UFOs. Make a .zip of the folder that holds the .designspace and its UFOs, " +
  "and open the .zip (or open the folder itself with “Open a folder…”).";

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
    if (/\.designspace$/i.test(file.name)) throw new Error(DESIGNSPACE_ALONE);
    if (FOLDER_FONTS.test(file.name)) {
      const ext = file.name.match(FOLDER_FONTS)[0].toLowerCase();
      throw new Error(`A ${ext} is a folder: open it with “Open a folder…”, or open a .zip of it.`);
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

// A new, empty font kept here: a family name and a glyph set to draw.
export async function createNewFont(name, glyphSetKey, onProgress) {
  const Format = window.HiveTryFormat;
  const familyName = (name || "").trim() || "Untitled";
  const font = emptyFont(familyName, glyphSetKey);
  const id = await window.hiveTry.createLocal(familyName, Format.writePackage(font), onProgress);
  return { id, text: EXAMPLE_TEXT };
}

// `newFont`: with the "New font…" form open (/try?new).
function openPanel({ newFont = false } = {}) {
  const Store = window.HiveTryStore;
  const current = window.hiveTry?.localId;
  const error = el("p", { class: "error", role: "alert" });
  const progress = el("p", { class: "note", role: "status" });
  const list = el("ul");
  const fileInput = el("input", {
    type: "file",
    // .designspace and .ufo too: not greyed out, the message says what to do.
    accept: ".zip,.glyphs,.ttf,.otf,.woff,.woff2,.ttx,.designspace,.ufo,application/zip",
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

  // A fresh copy of the example font, kept here under a free name.
  async function openExample() {
    error.textContent = "";
    progress.textContent = "Setting up " + EXAMPLE_NAME + "…";
    try {
      const projects = Store.available() ? await Store.list() : [];
      const id = await window.hiveTry.saveCopy(freeName(EXAMPLE_NAME, projects), (text) => {
        progress.textContent = text;
      });
      go(editorURL(id, EXAMPLE_TEXT));
    } catch (e) {
      console.error(e);
      progress.textContent = "";
      error.textContent = e.message || String(e);
    }
  }

  // "New font…": a name and a glyph set, then the empty font opens in the
  // editor, where its sample text shows what there is to draw.
  const nameInput = el("input", { type: "text", name: "name", value: "Untitled", maxlength: "80", autocomplete: "off" });
  const glyphSetSelect = el(
    "select",
    { name: "glyphset" },
    ...GLYPH_SETS.map((set) =>
      el("option", { value: set.key, ...(set.key === DEFAULT_GLYPH_SET ? { selected: "" } : {}) }, set.name + " — " + set.about)
    )
  );
  const newForm = el(
    "form",
    {
      class: "new-font",
      hidden: "",
      onsubmit: async (event) => {
        event.preventDefault();
        error.textContent = "";
        progress.textContent = "Setting up " + (nameInput.value.trim() || "Untitled") + "…";
        try {
          const { id, text } = await createNewFont(nameInput.value, glyphSetSelect.value, (text) => {
            progress.textContent = text;
          });
          go(editorURL(id, text));
        } catch (e) {
          console.error(e);
          progress.textContent = "";
          error.textContent = e.message || String(e);
        }
      },
    },
    el("label", {}, "Family name", nameInput),
    el("label", {}, "Glyph set", glyphSetSelect),
    el("p", { class: "note" }, "Fontra lists the glyph set's glyphs as empty places, to draw one by one. You can change the set later, in the sidebar's “Project glyph sets”."),
    el("div", { class: "actions" }, el("button", { type: "submit" }, "Create"), el("button", { type: "button", class: "plain", onclick: () => (newForm.hidden = true) }, "Cancel"))
  );

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
              title: "Remove from this browser (your own files are not touched)",
              onclick: async () => {
                if (!confirm(`Remove “${project.name}” from this browser? Its history, branches and comments here go with it; the files you opened it from are not touched. To keep what you did, export it first (File › Export as).`)) return;
                await window.hiveTry.deleteLocal(project.id);
                // The start page opens the next font, or the example again.
                if (project.id === current) go(START_URL);
                else fill();
              },
            },
            "Remove"
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
          { type: "button", title: "A .glyphs, .ttf, .otf, .woff or .woff2 file, or a .zip of any font (a designspace: the folder with its UFOs, zipped)", onclick: () => fileInput.click() },
          "Open a file…"
        ),
        el(
          "button",
          { type: "button", class: "plain", title: "A .fontra, .ufo or .glyphspackage (these are folders), or the folder of a designspace and its UFOs", onclick: () => folderInput.click() },
          "Open a folder…"
        ),
        el(
          "button",
          { type: "button", class: "plain", title: "A fresh copy of the example font, MutatorSans", onclick: openExample },
          "Example font"
        ),
        el(
          "button",
          {
            type: "button",
            class: "plain",
            title: "An empty font with a glyph set to draw",
            onclick: () => {
              newForm.hidden = !newForm.hidden;
              if (!newForm.hidden) nameInput.select();
            },
          },
          "New font…"
        )
      ),
      el("p", { class: "note designspace" }, DESIGNSPACE_HINT),
      newForm,
      progress,
      error,
      el(
        "p",
        { class: "note" },
        el("strong", {}, "Open a file…"),
        " for a .glyphs, .ttf, .otf, .woff2, or a .zip of any font. ",
        el("strong", {}, "Open a folder…"),
        " for a .fontra, .ufo or .glyphspackage: they look like files on a Mac but are folders. " +
          "For a designspace, the folder that holds it with its UFOs: open it, or a .zip of it.",
        el("br"),
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
  if (newFont) {
    newForm.hidden = false;
    nameInput.select();
  }
  fill();
}

// Hive's branch pill (⎇ main ▾) in the top bar, as online: branches of the
// font kept here, served by Hive's routes in the browser.
async function startBranches() {
  window.__hiveViewsNoAutoStart = true;
  try {
    const views = await import("/hive/views/hive-views.js");
    await views.startTry();
  } catch (error) {
    console.error(error);
  }
}

// The tab's icon follows the bar's: Fontra's on the demo.
function useFontraIcon() {
  for (const link of document.querySelectorAll('link[rel="icon"]')) link.remove();
  document.head.append(el("link", { rel: "icon", href: "/images/fontra-icon.svg", type: "image/svg+xml" }));
}

// Settings, which is about no font: a slim bar — the way back, and the
// tutorials (the Settings tour goes on here).
function startFontless() {
  useFontraIcon();
  const back = () => {
    let opener = null;
    try {
      opener = window.opener && !window.opener.closed && window.opener.location.origin === location.origin ? window.opener : null;
    } catch (error) {
      opener = null;
    }
    if (opener) opener.focus();
    else if (history.length > 1) history.back();
    else location.href = START_URL;
  };
  const bar = el(
    "div",
    { class: "hive-try", role: "region", "aria-label": "Try Fontra" },
    el("img", { src: "/images/fontra-icon.svg", alt: "", title: "Fontra" }),
    el("span", { class: "name" }, "Try Fontra"),
    el("button", { type: "button", class: "plain", onclick: back }, "Back to the editor"),
    el(
      "button",
      { class: "close help", type: "button", title: "Tutorials", "aria-label": "Tutorials", "aria-haspopup": "menu", onclick: (event) => openTourMenu(event.currentTarget) },
      "?"
    ),
    el(
      "button",
      { class: "close", type: "button", title: "Hide", "aria-label": "Hide", onclick: () => bar.classList.toggle("small") },
      "–"
    )
  );
  document.body.append(bar);
  tourOnLoad();
}

function start() {
  document.head.append(el("style", {}, STYLE));
  if (hereView() === "applicationsettings") {
    startFontless();
    return;
  }
  if (!window.hiveTry?.localId) useFontraIcon();
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
      title:
        "Keep this font, with your edits, in this browser, and try Fontra Hive on it: " +
        "history of every glyph, snapshots, branches, comments. No account needed.",
      onclick: async () => {
        try {
          const name = freeName(EXAMPLE_NAME, await window.HiveTryStore.list());
          const id = await tryAPI.saveCopy(name, (text) => (status.textContent = text));
          tryAPI.TryFont.edited = false;
          location.href = editorURL(id, EXAMPLE_TEXT);
        } catch (e) {
          console.error(e);
          status.classList.add("error");
          status.textContent = "could not keep it: " + (e.message || e);
        }
      },
    },
    "Keep a copy and try Hive"
  );
  const bar = el(
    "div",
    { class: "hive-try", role: "region", "aria-label": "Try Fontra" },
    // Fontra's icon on the demo (Fontra alone); Hive's once a font is kept
    // here, with Hive's history, branches and comments.
    el("img", {
      src: isLocal ? "/hive/icons/hive-icon.png" : "/images/fontra-icon.svg",
      alt: "",
      title: isLocal ? "Fontra with Fontra Hive" : "Fontra",
    }),
    name,
    status,
    el("button", { type: "button", class: "plain fonts", onclick: () => openPanel() }, "Your fonts"),
    // The demo without Hive is only seen where nothing can be kept (a
    // private window), or at its own address: the offer is for the latter.
    isLocal || !window.HiveTryStore.available() ? null : keep,
    el(
      "a",
      {
        // Hive's home page, where accounts are made (on a development
        // server, "/" is only its list of projects).
        href: "https://fontrahive.com/",
        class: "extra",
        title: "With a Fontra Hive account, your fonts are online: share them, work on them together, with branches and reviews.",
      },
      "Fontra Hive for teams"
    ),
    isLocal
      ? el(
          "button",
          { class: "close help", type: "button", title: "Tutorials", "aria-label": "Tutorials", "aria-haspopup": "menu", onclick: (event) => openTourMenu(event.currentTarget) },
          "?"
        )
      : null,
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
        startBranches();
        // A tour going on, one asked for (?tour=), or, for someone new,
        // the welcome bubble — once the page is up.
        tourOnLoad();
        // /try?new: straight to "New font…".
        const params = new URLSearchParams(location.search);
        if (params.has("new")) {
          params.delete("new");
          const url = new URL(location.href);
          url.search = params.toString();
          history.replaceState(history.state, "", url);
          openPanel({ newFont: true });
        }
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
  window.addEventListener("hive-try-error", (event) => {
    setTimeout(() => {
      status.classList.add("error");
      status.textContent = event.detail.split("\n")[0].slice(0, 160);
      status.title = event.detail;
    }, 0);
  });
  window.addEventListener("hive-try-status", (event) => {
    const { text } = event.detail;
    status.classList.toggle("error", text === "error");
    status.textContent =
      text === "saving" ? "saving…" : text === "saved" ? "saved in this browser" : "could not save: export a copy (File › Export as)";
  });
  document.body.append(bar);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
