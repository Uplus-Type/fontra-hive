// Fontra Hive, "Try Fontra" — lesson 2, "Your first font": a variable font
// of two letters, H and o, with a Weight axis, from an empty font to a TTF.
// The story and its values are those of "Your first variable font" in the
// documentation (fontra-docs-redesign, docs-redesign/tutorial-first-font.md):
// the long text stays there, the lesson keeps the gestures. Rules for the
// text: try-tour-steps.js.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import {
  DOCS,
  axisOf,
  contourCount,
  firstLayer,
  glyphSources,
  hasGlyph,
  near,
  pathBounds,
  sourceCount,
  turnings,
} from "./lib.js";

// The H of three rectangles, once fused: stems at 60 and 430, 110 wide, 700 high.
const H_BOX = { xMin: 60, yMin: 0, xMax: 540, yMax: 700 };

export const firstFont = {
  title: "Your first font",
  kind: "lesson",
  minutes: 20,
  about: "From an empty font to a variable TTF: an H, an o, a Weight axis.",
  // The lesson starts on one font and goes on in the new one it makes.
  anyFont: true,
  then: "drawing",
  steps: [
    {
      id: "welcome",
      title: "Your first font",
      text:
        "In twenty minutes, a real variable font: two letters, H and o, drawn with rectangles " +
        "and ellipses, a Weight axis from Regular to Black, and a TTF to install. Each step " +
        "waits until you have done it.",
      next: "Start",
      skip: "Not now",
    },
    {
      id: "new-font",
      anchor: ".hive-try .fonts",
      placement: "top",
      title: "New font",
      text:
        "Open Your fonts and click New font…. Give it a family name, keep GF Latin Core, and " +
        "Create: an empty font opens, every glyph of the set waiting as a grey place.",
      until: ({ fontController }) =>
        !!fontController && Object.keys(fontController.glyphMap).length === 0 && !fontController.axes?.axes?.length,
    },
    {
      id: "create-h",
      anchor: '.sidebar-tab[data-sidebar-name="text-entry"]',
      open: "text-entry",
      title: "Create the H",
      text:
        "Type H in the preview text. The canvas shows a grey placeholder: double-click it and " +
        "confirm. Fontra gives the glyph its Unicode value for you.",
      until: ({ fontController }) => hasGlyph(fontController, "H"),
    },
    {
      id: "rectangles",
      anchor: '#edit-tools .tool-button[data-tool="shape-tool"]',
      placement: "right", // below the tool is its menu
      title: "Shape Tool",
      text:
        "Pick the Shape Tool, rectangle. Drag three rectangles on the H: a left stem, a right " +
        "stem, a crossbar. Rough is fine — the next step makes them exact.",
      learn: DOCS + "/reference/editor-view/tools/shapes",
      until: async ({ fontController }) => contourCount(await firstLayer(fontController, "H")) >= 3,
    },
    {
      id: "exact",
      anchor: '.sidebar-tab[data-sidebar-name="selection-transformation"]',
      open: "selection-transformation",
      title: "Transformation",
      text:
        "Take the Pointer Tool and double-click a rectangle to select it whole. Here, type its " +
        "Dimensions and move it: stems 110 × 700 at x 60 and x 430, y 0; crossbar 480 × 100 " +
        "at x 60, y 300.",
      learn: DOCS + "/reference/editor-view/panels/transformations",
      until: async ({ fontController }) => near(pathBounds(await firstLayer(fontController, "H")), H_BOX),
    },
    {
      id: "remove-overlaps",
      anchor: '.sidebar-tab[data-sidebar-name="selection-transformation"]',
      open: "selection-transformation",
      title: "Remove overlaps",
      text:
        "Select all (⌘A or Ctrl+A), then Remove overlaps under Path Operations: the three boxes " +
        "fuse into one outline, the H.",
      until: async ({ fontController }) => contourCount(await firstLayer(fontController, "H")) === 1,
    },
    {
      id: "width-h",
      anchor: '.sidebar-tab[data-sidebar-name="selection-info"]',
      open: "selection-info",
      title: "Sidebearings",
      text:
        "In Glyph info, set both Sidebearings to 60: the Advance width follows by itself, 600. " +
        "There is no need to select anything first.",
      learn: DOCS + "/reference/editor-view/panels/glyph-info",
      until: async ({ fontController }) => (await firstLayer(fontController, "H"))?.xAdvance === 600,
    },
    {
      id: "create-o",
      anchor: '.sidebar-tab[data-sidebar-name="text-entry"]',
      open: "text-entry",
      title: "Preview text",
      text:
        "Click in the preview text, in the left column, and type o. Double-click its grey place " +
        "once to create the glyph, then again to edit it. Any name works too: type a slash and " +
        "the name, /a.alt.",
      learn: DOCS + "/reference/editor-view/panels/preview-text",
      until: ({ fontController }) => hasGlyph(fontController, "o"),
    },
    {
      id: "ellipses",
      anchor: '#edit-tools .tool-button[data-tool="shape-tool"]',
      placement: "right", // below the tool is its menu
      title: "Ellipse Tool",
      text:
        "Hold the Shape Tool down for its menu and pick the ellipse. Drag the outer oval, " +
        "420 × 520 at x 40, y −10. Hold ⌘ while dragging the inner one, 220 × 320 at x 140, " +
        "y 90: drawn the other way round, it is the counter.",
      until: async ({ fontController }) => {
        const turns = turnings(await firstLayer(fontController, "o"));
        return turns.length === 2 && turns[0] !== turns[1];
      },
    },
    {
      id: "width-o",
      anchor: '.sidebar-tab[data-sidebar-name="selection-info"]',
      open: "selection-info",
      title: "Sidebearings",
      text:
        "Same for the o, in Glyph info: Sidebearings of 40 and 40, an Advance width of 500. " +
        "Round letters sit a little closer to their neighbours than straight ones.",
      until: async ({ fontController }) => (await firstLayer(fontController, "o"))?.xAdvance === 500,
    },
    {
      id: "axis",
      view: "fontinfo",
      hash: "axes-panel",
      anchor: "#axes-panel",
      placement: "inside-right",
      ring: false,
      title: "Axes",
      text:
        "Click New axis…, pick the Weight preset, and make its range 400 to 900, default 400. " +
        "Under Axis values, add Regular at 400 and Black at 900: they become the font's styles.",
      learn: DOCS + "/reference/menu/font/axes",
      until: ({ fontController }) => {
        const axis = axisOf(fontController, "wght");
        return !!axis && axis.maxValue === 900 && axis.defaultValue === 400;
      },
    },
    {
      id: "black-source",
      view: "fontinfo",
      hash: "sources-panel",
      anchor: "#sources-panel",
      placement: "inside-right",
      ring: false,
      title: "Sources",
      text:
        "Click New font source…, name it Black and put it at Weight 900. A source is a place " +
        "in the designspace where you draw; Fontra interpolates between them.",
      learn: DOCS + "/reference/menu/font/sources",
      until: ({ fontController }) => sourceCount(fontController) >= 2,
    },
    {
      id: "draw-black",
      view: "editor",
      anchor: '.sidebar-tab[data-sidebar-name="designspace-navigation"]',
      open: "designspace-navigation",
      title: "Designspace",
      text:
        "Black is grey under Glyph sources: a virtual source. Double-click it to copy the o " +
        "there. Hold the Pointer Tool down for Pointer Tool (scaling), select the counter and " +
        "shrink it. Then the H: its stems' inner edges, outward.",
      learn: DOCS + "/reference/editor-view/panels/designspace",
      until: async ({ fontController }) =>
        (await glyphSources(fontController, "H")).length >= 2 &&
        (await glyphSources(fontController, "o")).length >= 2,
    },
    {
      id: "slider",
      anchor: '.sidebar-tab[data-sidebar-name="designspace-navigation"]',
      open: "designspace-navigation",
      title: "The slider",
      text:
        "Type HoHoHo in the preview text and drag the Weight slider. Stop anywhere between 400 " +
        "and 900: what you see is interpolated, not editable — but + under Glyph sources adds " +
        "an editable source right there.",
    },
    {
      id: "export",
      anchor: ".hive-try",
      placement: "top",
      title: "Export as",
      text:
        "File › Export as › TrueType: your variable font, compiled here, in the browser. Install " +
        "it, or drop it on wakamaifondue.com, and type HoHoHo at every weight. OTF is not offered in Try.",
      learn: DOCS + "/reference/menu/file/export-as",
      until: ({ hiveTry }) => hiveTry?.lastExport?.format === "ttf",
      next: "Done",
    },
  ],
};
