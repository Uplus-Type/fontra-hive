// Fontra Hive, "Try Fontra" — tour 11b, "Coming from RoboFont": where
// RoboFont's windows and habits are in Fontra, one bubble per thing, each
// on the Fontra element that takes its place. From the "Rosetta" page of
// the documentation's redesign (drafts/fontra-for-robofont-users.md),
// which says more. Rules for the text: try-tour-steps.js.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { DOCS } from "./lib.js";

export const fromRoboFont = {
  title: "Coming from RoboFont",
  kind: "tour",
  minutes: 3,
  about: "UFOs, the designspace, Space Center, the Inspector: where they are in Fontra.",
  steps: [
    {
      id: "welcome",
      title: "Coming from RoboFont",
      text:
        "Fontra reads your UFOs and designspaces as they are. Three minutes on where RoboFont's " +
        "windows went — on MutatorSans, itself a designspace of UFOs, or on your own files.",
      next: "Show me",
      skip: "Not now",
    },
    {
      id: "open",
      anchor: ".hive-try .fonts",
      placement: "top",
      title: "Your fonts",
      text:
        "A UFO is a folder: Open a folder… and pick it. For a designspace, pick the folder that " +
        "holds it with its UFOs. Everything is converted here, and your files are never touched.",
    },
    {
      id: "glyph-window",
      anchor: "#edit-canvas",
      placement: "inside-bottom",
      ring: false,
      title: "The canvas",
      text:
        "The Glyph window and Space Center are one: type your text in Preview text, in the left " +
        "column, and double-click a glyph on the canvas to edit it in place, its neighbours " +
        "live around it. Hold the space bar to preview.",
      learn: DOCS + "/reference/editor-view/canvas/glyph-editor/",
    },
    {
      id: "designspace",
      anchor: '.sidebar-tab[data-sidebar-name="designspace-navigation"]',
      open: "designspace-navigation",
      title: "Designspace",
      text:
        "No extension needed: each UFO of your designspace is a source here, with a slider per " +
        "axis. Drag them to see every weight in between, and edit any source in place.",
      learn: DOCS + "/reference/editor-view/panels/designspace",
    },
    {
      id: "inspector",
      anchor: '.sidebar-tab[data-sidebar-name="selection-transformation"]',
      open: "selection-transformation",
      title: "Transformation",
      text:
        "The Inspector's transform section is here: move, scale, rotate, flip, align, exact " +
        "dimensions, and the path operations. Glyph info, beside it, has the width and sidebearings.",
      learn: DOCS + "/reference/editor-view/panels/transformations",
    },
    {
      id: "metrics",
      anchor: '#edit-tools .tool-button[data-tool="metrics-tool"]',
      placement: "bottom",
      title: "Metrics tools",
      text:
        "Spacing and kerning happen in the text, not in a separate window: hold this tool for " +
        "the sidebearing and kerning tools. Kerning is per source, interpolated live.",
      learn: DOCS + "/reference/editor-view/tools/",
    },
    {
      id: "font-window",
      view: "fontoverview",
      anchor: "#glyph-cell-view-container",
      placement: "inside-top",
      ring: false,
      title: "Font overview",
      text:
        "The Font window is the Font overview: glyph cells, search, grouping. Smart sets become " +
        "glyph sets, from a list like GF Latin Core or your own. Double-click a cell to edit it.",
      learn: DOCS + "/reference/font-overview/workspace",
    },
    {
      id: "no-scripting",
      view: "fontoverview",
      title: "Scripting",
      text:
        "No scripting window, fontParts or extensions yet: a Python API is on Fontra's roadmap. " +
        "Keep RoboFont for your scripts; Fontra writes UFOs back with File › Export as.",
      learn: DOCS + "/reference/menu/file/export-as",
      next: "Done",
    },
  ],
};
