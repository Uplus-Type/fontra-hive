// Fontra Hive, "Try Fontra": the start page (/try, try-start.html).
//
// The visitor lands on Fontra with Hive from the first second: the font
// opened most recently in this browser, or, on a first visit, the example
// font (MutatorSans) kept here first — Python loads, the font becomes a git
// repository in the browser — and opened in the editor with Hive's history,
// branches and comments. Where this browser cannot keep files (a private
// window), the example opens as a plain demo instead, nothing saved.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { DEMO_URL, EXAMPLE_NAME, EXAMPLE_TEXT, editorURL, el, latestProject } from "./try-common.js";

const status = document.getElementById("status");
const actions = document.getElementById("actions");

function say(text) {
  status.classList.remove("error");
  status.textContent = text.replace(/…$/, "") + "…";
}

function fail(text) {
  status.classList.add("error");
  status.textContent = text;
  actions.replaceChildren(
    el("a", { href: DEMO_URL }, "Open the example without saving"),
    el("a", { href: "/try", class: "plain" }, "Try again")
  );
  actions.classList.add("shown");
}

function go(url) {
  location.replace(url);
}

// Which font to open: the last one opened here; a new copy of the example
// on a first visit.
export async function start({ store = window.HiveTryStore, tryAPI = window.hiveTry } = {}) {
  if (!store.available()) {
    // Nothing can be kept: Fontra alone on the example, in this tab.
    go(DEMO_URL);
    return "demo";
  }
  let projects;
  try {
    projects = await store.list();
  } catch (error) {
    console.error(error);
    projects = [];
  }
  const latest = latestProject(projects);
  if (latest) {
    go(editorURL(latest.id));
    return "local:" + latest.id;
  }
  say("Setting up the example font, " + EXAMPLE_NAME);
  try {
    const id = await tryAPI.saveCopy(EXAMPLE_NAME, say);
    go(editorURL(id, EXAMPLE_TEXT));
    return "local:" + id;
  } catch (error) {
    console.error(error);
    fail("Could not set up the font here: " + (error.message || error));
    return "error";
  }
}

window.hiveTryStart = start; // for the tests
if (!window.__hiveTryNoAutoStart) start();
