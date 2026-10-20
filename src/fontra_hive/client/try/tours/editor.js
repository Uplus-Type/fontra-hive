// Fontra Hive, "Try Fontra" — tour 1, "Around the editor": two minutes on
// the example font, Fontra first, then what Hive adds. Rules for the text:
// try-tour-steps.js.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { DOCS } from "./lib.js";

export const editor = {
  title: "Around the editor",
  kind: "tour",
  minutes: 2,
  about: "The canvas, the tools, the panels, and what Hive adds.",
  steps: [
    {
      id: "welcome",
      title: "Welcome to Fontra",
      text:
        "This is Fontra, the font editor, running entirely in your browser, with Fontra Hive " +
        "for history, snapshots, branches and comments. Nothing leaves your computer. " +
        "A two-minute tour?",
      next: "Take the tour",
      skip: "Not now",
    },
    {
      id: "canvas",
      anchor: "#edit-canvas",
      placement: "inside-bottom",
      ring: false,
      title: "The canvas",
      text:
        "Here is MutatorSans, a small variable font to play with. Double-click a glyph to " +
        "edit its outline; double-click an empty grey place to create that glyph. Scroll to " +
        "pan, pinch or ⌘-scroll to zoom.",
      learn: DOCS + "/reference/editor-view/canvas/glyph-editor/",
    },
    {
      id: "text",
      anchor: '.sidebar-tab[data-sidebar-name="text-entry"]',
      open: "text-entry",
      title: "Preview text",
      text:
        "Type any text here and the canvas shows it in your font — including the glyphs " +
        "you have not drawn yet.",
      learn: DOCS + "/reference/editor-view/panels/preview-text",
    },
    {
      id: "tools",
      anchor: "#edit-tools",
      placement: "bottom",
      title: "Tools",
      text:
        "Pointer, pen, shapes, knife, ruler, hand, metrics — click and hold a tool for its " +
        "variants. The shape tool draws rectangles and ellipses: the quickest way to a first " +
        "letter.",
      learn: DOCS + "/reference/editor-view/tools/",
    },
    {
      id: "designspace",
      anchor: '.sidebar-tab[data-sidebar-name="designspace-navigation"]',
      open: "designspace-navigation",
      title: "Designspace",
      text:
        "MutatorSans has Weight and Width axes. Drag the sliders: the canvas interpolates " +
        "between the sources. Each source is a drawing; edit any of them.",
      learn: DOCS + "/reference/editor-view/panels/designspace",
    },
    {
      id: "history",
      anchor: '.sidebar-tab[data-sidebar-name="hive-history"]',
      open: "hive-history",
      title: "Glyph history — Fontra Hive",
      text:
        "Every edit is a commit, kept in this browser. Hover a version to see it on the " +
        "canvas, click to restore it. Name a snapshot when a glyph is in a state worth " +
        "coming back to.",
      teams: "the history says who changed what, and the font lives on a server you share.",
    },
    {
      id: "branches",
      anchor: ".hive-branch",
      // Its menu drops below the pill, over the sidebar: the bubble goes to
      // the left of the open panels and of the menu (try-tour.js, avoided).
      placement: "left",
      title: "Branches",
      text:
        "Try a bolder M on a branch of your own — open the menu and make one now, the tour " +
        "follows. Merge it into main when it works, or drop it and nothing is lost.",
      teams: "everyone draws on their own branch, and a merge is reviewed before it reaches main.",
    },
    {
      id: "comments",
      anchor: '.sidebar-tab[data-sidebar-name="hive-comments"]',
      open: "hive-comments",
      title: "Comments",
      text: "Pin a note to a point in a glyph with the comment tool — here, notes to yourself.",
      teams: "comments are the review: a reviewer pins them, the designer answers and resolves them.",
    },
    {
      id: "bar",
      anchor: ".hive-try",
      placement: "top",
      title: "Your fonts",
      text:
        "Open your own .fontra, UFO, Glyphs or TrueType files here, or start a new font with " +
        "a Latin glyph set. File › Export as saves what you did: .fontra, UFOs, or a compiled " +
        "TTF. This tour is behind the ? whenever you want it again.",
      learn: DOCS + "/reference/menu/file/export-as",
      next: "Done",
    },
  ],
};
