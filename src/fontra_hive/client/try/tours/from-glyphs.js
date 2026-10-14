// Fontra Hive, "Try Fontra" — tour 11a, "Coming from Glyphs": where
// Glyphs' things are in Fontra, one bubble per thing, each on the Fontra
// element that takes its place. From the "Rosetta" page of the
// documentation's redesign (drafts/fontra-for-glyphs-users.md), which
// says more. Rules for the text: try-tour-steps.js.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { DOCS } from "./lib.js";

export const fromGlyphs = {
  title: "Coming from Glyphs",
  kind: "tour",
  minutes: 3,
  about: "Masters, brace layers, smart components, instances: where they are in Fontra.",
  steps: [
    {
      id: "welcome",
      title: "Coming from Glyphs",
      text:
        "Most of what you know has a place in Fontra, under another name. Three minutes, one " +
        "bubble per thing — on MutatorSans, or on your own .glyphs file.",
      next: "Show me",
      skip: "Not now",
    },
    {
      id: "open",
      anchor: ".hive-try .fonts",
      placement: "top",
      title: "Your fonts",
      text:
        "Open a file… takes your .glyphs; a .glyphspackage is a folder, so Open a folder…. It " +
        "is converted here, in your browser, and the file on your disk is never touched.",
    },
    {
      id: "text-view",
      anchor: "#edit-canvas",
      placement: "inside-bottom",
      ring: false,
      title: "The canvas",
      text:
        "No Text tool and no Edit view: the canvas is both. Type in it, then double-click a " +
        "glyph to edit it in place, among its neighbours. Hold the space bar for the filled preview.",
      learn: DOCS + "/reference/editor-view/canvas/glyph-editor/",
    },
    {
      id: "masters",
      anchor: '.sidebar-tab[data-sidebar-name="designspace-navigation"]',
      open: "designspace-navigation",
      title: "Designspace",
      text:
        "Masters are font sources: here, with a slider per axis, always live. A brace layer is a " +
        "glyph source at any location — add it with + under the glyph's sources, no name syntax. " +
        "⌘↑ and ⌘↓ step through sources.",
      learn: DOCS + "/reference/editor-view/panels/designspace",
    },
    {
      id: "smart-components",
      anchor: '.sidebar-tab[data-sidebar-name="selection-info"]',
      open: "selection-info",
      title: "Glyph info",
      text:
        "Smart components are variable components: select one and set its axes here. They nest, " +
        "and their axes vary across the whole designspace. Glyph › Edit glyph axes gives a glyph " +
        "axes of its own.",
      learn: DOCS + "/reference/editor-view/panels/glyph-info",
    },
    {
      id: "palette",
      anchor: '.sidebar-tab[data-sidebar-name="selection-transformation"]',
      open: "selection-transformation",
      title: "Transformation",
      text:
        "The Palette's dimensions, alignment and Remove Overlap are here, with union, subtract, " +
        "intersect and exclude. Filters like Offset Curve or Round Corners have no equivalent yet.",
      learn: DOCS + "/reference/editor-view/panels/transformations",
    },
    {
      id: "kerning",
      anchor: '#edit-tools .tool-button[data-tool="metrics-tool"]',
      placement: "bottom",
      title: "Kerning Tool",
      text:
        "The Kerning window is a tool: hold the metrics tool for the kerning tool and kern in " +
        "the text, per source, with the interpolation live. Tools take the keys 1 to 9, in the " +
        "toolbar's order.",
      learn: DOCS + "/reference/editor-view/tools/",
    },
    {
      id: "instances",
      view: "fontinfo",
      hash: "axes-panel",
      anchor: "#axes-panel",
      placement: "inside-right",
      ring: false,
      title: "Axes",
      text:
        "Axes are in Font Info, with a mapping graph for avar. There is no instance list: the " +
        "Axis values you name on each axis become the exported instances.",
      learn: DOCS + "/reference/menu/font/axes",
    },
    {
      id: "features",
      view: "fontinfo",
      hash: "opentype-feature-code-panel",
      anchor: "#opentype-feature-code-panel",
      placement: "inside-right",
      ring: false,
      title: "OpenType Feature Code",
      text:
        "Features are written here by hand: nothing is generated from glyph name suffixes. " +
        "Kerning and mark positioning are written at export, from your kerning and anchors.",
      learn: DOCS + "/reference/menu/font/features",
      next: "Done",
    },
  ],
};
