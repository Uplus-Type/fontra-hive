// Fontra Hive, "Try Fontra": what the guided tours say, step by step.
//
// Each step points at a part of Fontra's or Hive's interface (a CSS
// selector in the editor's page; the step is skipped when it is absent),
// may open a sidebar panel first, and may link to the page of Fontra's
// documentation that says more. Selectors use the editor's ids and data
// attributes, never its words, so the tour survives Fontra's languages.
// The mechanics are in try-tour.js.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const DOCS = "https://docs.fontra.xyz";

export const TOURS = {
  // Around the editor: five minutes on the example font, Fontra first,
  // then what Hive adds.
  editor: {
    title: "Around the editor",
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
      },
      {
        id: "branches",
        anchor: ".hive-branch",
        placement: "bottom",
        title: "Branches",
        text:
          "Try a bolder H on a branch of your own. Merge it into main when it works, or drop " +
          "it and nothing is lost.",
      },
      {
        id: "comments",
        anchor: '.sidebar-tab[data-sidebar-name="hive-comments"]',
        open: "hive-comments",
        title: "Comments",
        text:
          "Pin a note to a point in a glyph with the comment tool. In Fontra Hive for teams, " +
          "that is how reviews happen — here, they are notes to yourself.",
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
  },
};
