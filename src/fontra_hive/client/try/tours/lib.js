// Fontra Hive, "Try Fontra": what the tours share — the documentation's
// address, and the questions a lesson's `until` asks of the font.
//
// A step's `until` gets {editor, fontController, sceneSettings, hiveTry}
// (Fontra's public objects, on whichever of Fontra's pages the step is;
// hiveTry is Try's own) and returns true, or a promise of true, once the
// step is done. These helpers read Fontra's font and glyph objects only;
// anything missing reads as "not done yet".
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

export const DOCS = "https://docs.fontra.xyz";
export const HIVE = "https://fontrahive.com/";

export function hasGlyph(fontController, name) {
  return !!fontController?.glyphMap?.[name];
}

export function axisOf(fontController, tag) {
  return (fontController?.axes?.axes || []).find((axis) => axis.tag === tag || axis.name === tag) || null;
}

export function sourceCount(fontController) {
  return Object.keys(fontController?.sources || {}).length;
}

// The glyph's sources (Fontra's VariableGlyph), or [] when not there.
export async function glyphSources(fontController, name) {
  if (!hasGlyph(fontController, name)) return [];
  const glyph = await fontController.getGlyph(name);
  return glyph?.sources || [];
}

// The drawing of a glyph's first source (Fontra's StaticGlyph), or null.
export async function firstLayer(fontController, name) {
  if (!hasGlyph(fontController, name)) return null;
  const glyph = await fontController.getGlyph(name);
  const source = glyph?.sources?.[0];
  return (source && glyph.layers?.[source.layerName]?.glyph) || null;
}

export function contourCount(layer) {
  return layer?.path?.contourInfo?.length || 0;
}

// The points' bounding box, off-curve points included: {xMin, yMin, xMax, yMax}.
export function pathBounds(layer) {
  const c = layer?.path?.coordinates;
  if (!c || c.length < 2) return null;
  const box = { xMin: Infinity, yMin: Infinity, xMax: -Infinity, yMax: -Infinity };
  for (let i = 0; i + 1 < c.length; i += 2) {
    box.xMin = Math.min(box.xMin, c[i]);
    box.xMax = Math.max(box.xMax, c[i]);
    box.yMin = Math.min(box.yMin, c[i + 1]);
    box.yMax = Math.max(box.yMax, c[i + 1]);
  }
  return box;
}

export function near(box, expected, tolerance = 2) {
  return !!box && Object.entries(expected).every(([key, value]) => Math.abs(box[key] - value) <= tolerance);
}

// Each contour's turning, from the points' polygon: +1 counter-clockwise,
// -1 clockwise. A counter cut by Subtract turns the other way round.
export function turnings(layer) {
  const path = layer?.path;
  if (!path?.contourInfo) return [];
  const c = path.coordinates;
  const result = [];
  let start = 0;
  for (const { endPoint } of path.contourInfo) {
    let area = 0;
    for (let i = start; i <= endPoint; i++) {
      const j = i === endPoint ? start : i + 1;
      area += c[2 * i] * c[2 * j + 1] - c[2 * j] * c[2 * i + 1];
    }
    result.push(Math.sign(area));
    start = endPoint + 1;
  }
  return result;
}
