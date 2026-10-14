// Fontra Hive, "Try Fontra": the tours and lessons, in the order the ?
// menu lists them. Each lives in a file of its own, in tours/; the
// mechanics are in try-tour.js, the menu in try-tour-menu.js.
//
// A tour: {title, kind: "tour" | "lesson", minutes, about (one line for
// the menu), view (the page it starts on, "editor" by default), anyFont
// (it goes on in another font, the one it makes), then (the tour offered
// after the last step), steps}.
//
// A step:
//   id          its name, remembered when the page reloads mid-way
//   title       the element's name as Fontra shows it in English
//   text        what the bubble says
//   anchor      a CSS selector ("a >>> b": b in a's shadow root); the step is
//               left out when it is absent from its page (unless it has
//               `until`); none: the bubble is centred
//   placement   right | left | top | bottom | inside-top | inside-bottom |
//               inside-right; by default beside the anchor, towards the middle
//   offset      [dx, dy] added to the bubble's place
//   ring        false: no ring around the anchor
//   open        the editor's sidebar panel to open first
//   view, hash  the page it is on (editor, fontoverview, fontinfo,
//               applicationsettings) and its panel (#axes-panel…): from
//               another page, the bubble offers to open it
//   until       a lesson's step: ({editor, fontController, sceneSettings,
//               hiveTry}) => true (or a promise of it) once it is done; see
//               tours/lib.js
//   learn       the page of docs.fontra.xyz that says more
//   teams       what Fontra Hive for teams does more
//   next, skip, go   the buttons' words, when not Next, Skip, "Open …"
//
// How to write them:
// - One bubble, one idea, 25 to 45 words, in the imperative; the title is
//   the element's name as Fontra shows it in English.
// - The first sentence says what it is for, the second how to use it, the
//   third (if any) the trap to avoid.
// - `learn` only to a page that really adds something; `teams` only where
//   the server version really does more — never selling in the main text.
// - What Try lacks is said once, where one would look for it: no OTF
//   (export), no live collaboration (branches), fonts in this browser only
//   (Your fonts).
// - Anchors: ids and data-* attributes, never the words (Fontra is
//   translated); check each one in the tests' mock pages and in the real
//   editor.
// - No clicks made for the visitor on Fontra's menus: say where to click.
// - English only for now; the Display language step of Settings says so.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { editor } from "./tours/editor.js";
import { firstFont } from "./tours/first-font.js";
import { fontInfo } from "./tours/font-info.js";
import { fontOverview } from "./tours/font-overview.js";
import { fromGlyphs } from "./tours/from-glyphs.js";
import { fromRoboFont } from "./tours/from-robofont.js";

export const TOURS = {
  editor,
  "first-font": firstFont,
  "font-info": fontInfo,
  "font-overview": fontOverview,
  "from-glyphs": fromGlyphs,
  "from-robofont": fromRoboFont,
};
