// Fontra Hive, "Try Fontra" — tour 3, "Font info and designspace": the
// panels of Font Info, on whatever font is open.
// Rules for the text: try-tour-steps.js.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { DOCS } from "./lib.js";

const panel = (id) => ({
  view: "fontinfo",
  hash: id,
  anchor: "#" + id,
  placement: "inside-right",
  ring: false,
});

export const fontInfo = {
  title: "Font info and designspace",
  kind: "tour",
  minutes: 4,
  about: "Names, axes, sources, features and statuses: the font as a whole.",
  steps: [
    {
      id: "welcome",
      view: "editor",
      title: "Font Info",
      text:
        "What belongs to the whole font, not to a glyph, is in Font Info: names, axes, sources, " +
        "features. The Font menu opens it, in a tab of its own; so does the button below.",
      next: "Show me",
      skip: "Not now",
    },
    {
      id: "font-info",
      ...panel("font-info-panel"),
      title: "Font Info",
      text:
        "The family name, units per em, version, designer, licence and the rest of the font's " +
        "names. Each field is saved as you leave it; Undo works here too.",
      learn: DOCS + "/reference/menu/font/font-info",
    },
    {
      id: "axes",
      ...panel("axes-panel"),
      title: "Axes",
      text:
        "An axis is a dimension the design varies along, like Weight. Give it a range and a " +
        "default; Axis values name places on it, the exported instances. The mapping graph bends " +
        "user values into design values: in the font, that is avar, the axis variations table.",
      learn: DOCS + "/reference/menu/font/axes",
    },
    {
      id: "sources",
      ...panel("sources-panel"),
      title: "Sources",
      text:
        "A source is a whole drawing at one place of the designspace, given by its axis values; " +
        "Fontra interpolates between sources. Line metrics are set per source. A sparse source " +
        "draws only some glyphs, where they need it.",
      learn: DOCS + "/reference/menu/font/sources",
    },
    {
      id: "cross-axis-mapping",
      ...panel("cross-axis-mapping-panel"),
      title: "Cross-axis mapping",
      text:
        "Advanced: one axis moving others (avar 2) — an optical size that also changes the " +
        "weight, say. Most fonts need none; leave it empty until you do.",
      learn: DOCS + "/reference/menu/font/cross-axis-mapping",
    },
    {
      id: "conditional-substitutions",
      ...panel("conditional-substitutions-panel"),
      title: "Conditional substitutions",
      text:
        "Advanced: a glyph swapped for another in a region of the designspace — a dollar sign " +
        "losing its stroke in the heaviest weights. These are the designspace's rules.",
    },
    {
      id: "features",
      ...panel("opentype-feature-code-panel"),
      title: "OpenType Features",
      text:
        "The font's features, written in .fea: ligatures, alternates, anything the font does. " +
        "Fontra compiles them at export; a mistake in them shows when you export.",
      learn: DOCS + "/reference/menu/font/features",
    },
    {
      id: "statuses",
      ...panel("development-status-definitions-panel"),
      title: "Status definitions",
      text:
        "Empty at first: create the statuses your workflow needs, each with a name and a colour — " +
        "in progress, to check, finished, or your own. Give a glyph source its status in the " +
        "Designspace panel; the Font overview colours its cells.",
      learn: DOCS + "/reference/menu/font/status-colors",
    },
    {
      id: "back",
      view: "editor",
      title: "The Font menu",
      text:
        "Every panel you saw is in the editor's Font menu too: Font Info, Axes, Sources, " +
        "OpenType Features… each one opens this page right at its panel.",
      next: "Done",
    },
  ],
};
