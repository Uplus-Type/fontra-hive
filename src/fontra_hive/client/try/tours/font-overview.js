// Fontra Hive, "Try Fontra" — tour 4, "The font overview": every glyph of
// the font at once, its sidebar (search, glyph sets, grouping, location),
// and the way back to the editor. Rules for the text: try-tour-steps.js.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { DOCS } from "./lib.js";

const sidebar = {
  view: "fontoverview",
  anchor: "#sidebar-container",
  placement: "right",
};

export const fontOverview = {
  title: "The font overview",
  kind: "tour",
  minutes: 2,
  about: "Every glyph at once: glyph sets, grouping, statuses.",
  steps: [
    {
      id: "welcome",
      view: "editor",
      title: "Font overview",
      text:
        "The font overview shows every glyph of the font at once, drawn or still to draw. The " +
        "Font menu opens it, in a tab of its own; so does the button below.",
      next: "Show me",
      skip: "Not now",
    },
    {
      id: "cells",
      view: "fontoverview",
      anchor: "#glyph-cell-view-container",
      placement: "inside-top",
      ring: false,
      title: "Font overview",
      text:
        "One cell per glyph, the empty places of the glyph set in grey. Double-click a cell to " +
        "open that glyph in the editor; a grey one waits there for a double-click to create it.",
      learn: DOCS + "/reference/font-overview/workspace",
    },
    {
      id: "search",
      ...sidebar,
      title: "Search",
      text:
        "Type a glyph name or a character at the top of the sidebar: the cells narrow down as " +
        "you type. Clear the field to see the whole font again.",
    },
    {
      id: "glyph-sets",
      ...sidebar,
      title: "Glyph sets",
      text:
        "Project glyph sets belong to the font — a new font's GF Latin Core, its empty places " +
        "included. My glyph sets are yours, offered in every font. Tick the ones to show.",
      learn: DOCS + "/reference/font-overview/panels/navigation/glyph-sets",
    },
    {
      id: "group-by",
      ...sidebar,
      title: "Group by",
      text:
        "Group the cells by script, category or case to find your way in a large font. Each " +
        "cell takes the colour of its status, as Font Info defines them.",
      learn: DOCS + "/reference/font-overview/panels/navigation",
    },
    {
      id: "location",
      ...sidebar,
      title: "Source",
      text:
        "The cells show the font at one place of the designspace: pick another source, or move " +
        "the sliders, to see every glyph in Bold, say, or anywhere in between.",
      next: "Done",
    },
  ],
};
