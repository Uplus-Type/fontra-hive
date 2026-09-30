// Fontra Hive, "Try Fontra": the .fontra format in the browser.
//
// Reads and writes a .fontra package as Fontra's Python backend does
// (fontra/backends/fontra.py): font-data.json, glyph-info.csv, kerning.csv,
// features.txt, glyphs/<file name>.json, background-images/. Glyph files keep
// their paths as contours of points; Fontra's editor wants them packed
// (coordinates, pointTypes, contourInfo), so paths are converted both ways.
// Also a small .zip reader and writer (CompressionStream "deflate-raw").
//
// A classic script: sets window.HiveTryFormat (and works in a worker, or in
// Node for tests, through globalThis).
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
(function (global) {
  "use strict";

  var FONT_DATA = "font-data.json";
  var GLYPH_INFO = "glyph-info.csv";
  var KERNING = "kerning.csv";
  var FEATURES = "features.txt";
  var GLYPHS_DIR = "glyphs/";
  var IMAGES_DIR = "background-images/";

  // Point types, as in fontra/core/path.py (PointType).
  var ON_CURVE = 0x00;
  var OFF_CURVE_QUAD = 0x01;
  var OFF_CURVE_CUBIC = 0x02;
  var ON_CURVE_SMOOTH = 0x08;

  // ---- glyph file names (fontra/backends/filenames.py) ----

  var SEPARATOR = "^";
  var RESERVED = new Set(' " % * + / : < > ? [ \\ ] | ^'.split(" ").concat(["\x7f"]));
  for (var i = 0; i < 32; i++) RESERVED.add(String.fromCharCode(i));
  var RESERVED_NAMES = new Set(
    "con prn aux clock$ nul com1 lpt1 lpt2 lpt3 com2 com3 com4".split(" ")
  );
  var BASE32 = "0123456789ABCDEFGHIJKLMNOPQRSTUV";

  function isUpper(c) {
    return c !== c.toLowerCase() && c === c.toUpperCase();
  }

  function stringToFileName(string) {
    var chars = Array.from(string);
    var digits = [];
    for (var i = 0; i < chars.length; i += 5) {
      var digit = 0;
      var bit = 1;
      chars.slice(i, i + 5).forEach(function (c) {
        if (isUpper(c)) digit |= bit;
        bit <<= 1;
      });
      digits.push(digit);
    }
    while (digits.length && digits[digits.length - 1] === 0) digits.pop();
    var fileName = chars
      .map(function (c) {
        return RESERVED.has(c)
          ? "%" + c.charCodeAt(0).toString(16).toUpperCase().padStart(2, "0")
          : c;
      })
      .join("");
    if (fileName[0] === ".") {
      fileName = "%2E" + fileName.slice(1);
    } else if (fileName.indexOf(".") !== -1) {
      var dot = fileName.indexOf(".");
      var base = fileName.slice(0, dot);
      if (RESERVED_NAMES.has(base.toLowerCase())) {
        fileName = base + "%2E" + fileName.slice(dot + 1);
      }
    }
    if (!digits.length && RESERVED_NAMES.has(fileName.toLowerCase())) digits = [0];
    if (digits.length) {
      fileName +=
        SEPARATOR +
        digits
          .map(function (d) {
            return BASE32[d];
          })
          .join("");
    }
    return fileName;
  }

  function fileNameToString(stem) {
    return decodeURIComponent(stem.split(SEPARATOR, 1)[0]);
  }

  // ---- CSV, ";"-separated, as Python's csv module writes it ----

  function parseCSV(text) {
    var rows = [];
    var row = [];
    var cell = "";
    var quoted = false;
    var i = 0;
    var started = false;
    while (i < text.length) {
      var c = text[i];
      if (quoted) {
        if (c === '"') {
          if (text[i + 1] === '"') {
            cell += '"';
            i += 2;
            continue;
          }
          quoted = false;
          i++;
          continue;
        }
        cell += c;
        i++;
        continue;
      }
      if (c === '"' && cell === "") {
        quoted = true;
        started = true;
        i++;
      } else if (c === ";") {
        row.push(cell);
        cell = "";
        started = true;
        i++;
      } else if (c === "\r" || c === "\n") {
        if (started || cell !== "") row.push(cell);
        rows.push(row);
        row = [];
        cell = "";
        started = false;
        i += c === "\r" && text[i + 1] === "\n" ? 2 : 1;
      } else {
        cell += c;
        started = true;
        i++;
      }
    }
    if (started || cell !== "") {
      row.push(cell);
      rows.push(row);
    }
    return rows;
  }

  function csvCell(value) {
    var s = value === null || value === undefined ? "" : String(value);
    return /[;"\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  function writeCSV(rows) {
    return rows
      .map(function (row) {
        return row.map(csvCell).join(";") + "\r\n";
      })
      .join("");
  }

  // ---- JSON as Python's json.dumps(indent=0, ensure_ascii=False) ----

  function dumpJSON(value) {
    if (value === null || value === undefined) return "null";
    if (Array.isArray(value)) {
      if (!value.length) return "[]";
      return "[\n" + value.map(dumpJSON).join(",\n") + "\n]";
    }
    if (typeof value === "object") {
      var keys = Object.keys(value).filter(function (k) {
        return value[k] !== undefined;
      });
      if (!keys.length) return "{}";
      return (
        "{\n" +
        keys
          .map(function (k) {
            return JSON.stringify(k) + ": " + dumpJSON(value[k]);
          })
          .join(",\n") +
        "\n}"
      );
    }
    return JSON.stringify(value);
  }

  // ---- paths ----

  function packPath(path) {
    if (!path || path.coordinates) return path || {};
    var coordinates = [];
    var pointTypes = [];
    var attributes = [];
    var contourInfo = [];
    (path.contours || []).forEach(function (contour) {
      (contour.points || []).forEach(function (point) {
        coordinates.push(point.x, point.y);
        var type = ON_CURVE;
        if (point.type) {
          type = point.type === "cubic" ? OFF_CURVE_CUBIC : OFF_CURVE_QUAD;
        } else if (point.smooth) {
          type = ON_CURVE_SMOOTH;
        }
        pointTypes.push(type);
        attributes.push(point.attrs && Object.keys(point.attrs).length ? point.attrs : null);
      });
      contourInfo.push({
        endPoint: pointTypes.length - 1,
        isClosed: !!contour.isClosed,
      });
    });
    if (!contourInfo.length) return {};
    var packed = {
      coordinates: coordinates,
      pointTypes: pointTypes,
      contourInfo: contourInfo,
    };
    if (attributes.some(Boolean)) packed.pointAttributes = attributes;
    return packed;
  }

  function unpackPath(path) {
    if (!path || !path.coordinates) return path && path.contours ? path : {};
    var contours = [];
    var start = 0;
    (path.contourInfo || []).forEach(function (info) {
      var points = [];
      for (var i = start; i <= info.endPoint; i++) {
        var point = { x: path.coordinates[i * 2], y: path.coordinates[i * 2 + 1] };
        var type = path.pointTypes[i];
        if (type === OFF_CURVE_CUBIC) point.type = "cubic";
        else if (type === OFF_CURVE_QUAD) point.type = "quad";
        else if (type === ON_CURVE_SMOOTH) point.smooth = true;
        var attrs = path.pointAttributes && path.pointAttributes[i];
        if (attrs && Object.keys(attrs).length) point.attrs = attrs;
        points.push(point);
      }
      contours.push({ points: points, isClosed: !!info.isClosed });
      start = info.endPoint + 1;
    });
    return contours.length ? { contours: contours } : {};
  }

  function mapLayers(glyph, convert) {
    var copy = JSON.parse(JSON.stringify(glyph));
    Object.keys(copy.layers || {}).forEach(function (name) {
      var layerGlyph = copy.layers[name].glyph;
      if (layerGlyph && layerGlyph.path !== undefined) {
        var path = convert(layerGlyph.path);
        if (Object.keys(path).length) layerGlyph.path = path;
        else delete layerGlyph.path;
      }
    });
    return copy;
  }

  function packGlyph(glyph) {
    return mapLayers(glyph, packPath);
  }

  function unpackGlyph(glyph) {
    return mapLayers(glyph, unpackPath);
  }

  // ---- code points, glyph infos, kerning ----

  function parseCodePoints(cell) {
    return (cell || "")
      .split(",")
      .map(function (s) {
        return s.trim();
      })
      .filter(Boolean)
      .map(function (s) {
        if (s.slice(0, 2) !== "U+") throw new Error("bad code point: " + s);
        return parseInt(s.slice(2), 16);
      });
  }

  function formatCodePoint(cp) {
    return "U+" + cp.toString(16).toUpperCase().padStart(4, "0");
  }

  function readGlyphInfo(text) {
    var rows = parseCSV(text);
    var header = rows.shift() || [];
    if (header[0] !== "glyph name" || header[1] !== "code points") {
      throw new Error("glyph-info.csv: unexpected header");
    }
    var infoKeys = header.slice(2);
    var glyphMap = {};
    var glyphInfos = {};
    rows.forEach(function (row) {
      if (!row.length || !row[0]) return;
      glyphMap[row[0]] = parseCodePoints(row[1]);
      var info = {};
      infoKeys.forEach(function (key, i) {
        var cell = row[i + 2];
        if (!cell) return;
        try {
          info[key] = JSON.parse(cell);
        } catch (error) {
          info[key] = cell;
        }
      });
      if (Object.keys(info).length) glyphInfos[row[0]] = info;
    });
    return { glyphMap: glyphMap, glyphInfos: glyphInfos };
  }

  function writeGlyphInfo(glyphMap, glyphInfos) {
    var keys = new Set();
    Object.values(glyphInfos || {}).forEach(function (info) {
      Object.keys(info).forEach(function (k) {
        keys.add(k);
      });
    });
    var infoKeys = Array.from(keys).sort();
    var rows = [["glyph name", "code points"].concat(infoKeys)];
    Object.keys(glyphMap)
      .sort(pythonOrder)
      .forEach(function (name) {
        var row = [name, (glyphMap[name] || []).map(formatCodePoint).join(",")];
        var info = glyphInfos && glyphInfos[name];
        if (info) {
          infoKeys.forEach(function (key) {
            var value = info[key];
            row.push(
              value === null || value === undefined
                ? ""
                : typeof value === "string"
                  ? value
                  : JSON.stringify(value)
            );
          });
        }
        while (row.length > 2 && !row[row.length - 1]) row.pop();
        rows.push(row);
      });
    return writeCSV(rows);
  }

  // Python sorts strings by code point; so does this.
  function pythonOrder(a, b) {
    var ca = Array.from(a);
    var cb = Array.from(b);
    for (var i = 0; i < Math.min(ca.length, cb.length); i++) {
      var d = ca[i].codePointAt(0) - cb[i].codePointAt(0);
      if (d) return d;
    }
    return ca.length - cb.length;
  }

  function readKerning(text) {
    var rows = parseCSV(text);
    var i = 0;
    var kerning = {};
    function nextNonBlank() {
      while (i < rows.length && !(rows[i].length && rows[i][0])) i++;
      return i < rows.length ? rows[i++] : null;
    }
    function readGroups(keyword) {
      var row = nextNonBlank();
      if (!row || row[0] !== keyword) throw new Error("kerning.csv: expected " + keyword);
      var groups = {};
      while (i < rows.length && rows[i].length && rows[i][0]) {
        groups[rows[i][0]] = rows[i].slice(1);
        i++;
      }
      return groups;
    }
    for (;;) {
      var row = nextNonBlank();
      if (!row) break;
      if (row[0] !== "TYPE") throw new Error("kerning.csv: expected TYPE");
      var type = (rows[i++] || [])[0];
      var groupsSide1 = readGroups("GROUPS1");
      var groupsSide2 = readGroups("GROUPS2");
      row = nextNonBlank();
      if (!row || row[0] !== "VALUES") throw new Error("kerning.csv: expected VALUES");
      var header = rows[i++] || [];
      var sourceIdentifiers = header.slice(2);
      var values = {};
      while (i < rows.length && rows[i].length && rows[i][0]) {
        var r = rows[i++];
        values[r[0]] = values[r[0]] || {};
        values[r[0]][r[1]] = r.slice(2).map(function (v) {
          return v === "" ? null : parseFloat(v);
        });
        while (values[r[0]][r[1]].length < sourceIdentifiers.length) {
          values[r[0]][r[1]].push(null);
        }
      }
      kerning[type] = {
        groupsSide1: groupsSide1,
        groupsSide2: groupsSide2,
        sourceIdentifiers: sourceIdentifiers,
        values: values,
      };
    }
    return kerning;
  }

  function writeKerning(kerning) {
    var rows = [];
    Object.keys(kerning || {}).forEach(function (type) {
      var table = kerning[type];
      var hasContent =
        Object.keys(table.values || {}).length ||
        Object.keys(table.groupsSide1 || {}).length ||
        Object.keys(table.groupsSide2 || {}).length;
      if (!hasContent) return;
      if (rows.length) rows.push([]);
      rows.push(["TYPE"], [type], [], ["GROUPS1"]);
      Object.keys(table.groupsSide1 || {})
        .sort(pythonOrder)
        .forEach(function (g) {
          rows.push([g].concat(table.groupsSide1[g]));
        });
      rows.push([], ["GROUPS2"]);
      Object.keys(table.groupsSide2 || {})
        .sort(pythonOrder)
        .forEach(function (g) {
          rows.push([g].concat(table.groupsSide2[g]));
        });
      rows.push([], ["VALUES"], ["side1", "side2"].concat(table.sourceIdentifiers || []));
      Object.keys(table.values || {}).forEach(function (left) {
        Object.keys(table.values[left]).forEach(function (right) {
          rows.push(
            [left, right].concat(
              table.values[left][right].map(function (v) {
                return v === null || v === undefined ? "" : v;
              })
            )
          );
        });
      });
    });
    return rows.length ? writeCSV(rows) : null;
  }

  // ---- whole packages ----

  // The folder of the .fontra package in a list of paths (from a .zip or a
  // folder the visitor picked): the shallowest one holding font-data.json.
  function findRoot(paths) {
    var best = null;
    paths.forEach(function (path) {
      var parts = path.split("/");
      if (parts[parts.length - 1] !== FONT_DATA) return;
      if (parts.some(function (p) { return p.startsWith("__MACOSX") || p.startsWith("."); })) return;
      var root = parts.slice(0, -1).join("/");
      if (best === null || root.split("/").length < best.split("/").length) best = root;
    });
    if (best === null) {
      throw new Error("No .fontra package found (no font-data.json).");
    }
    return best ? best + "/" : "";
  }

  function blobText(blob) {
    return typeof blob === "string" ? Promise.resolve(blob) : blob.text();
  }

  // files: Map of path → Blob (or string), paths relative to anything above
  // the package. Returns {font, images}: font in the engine's form (glyphs
  // with packed paths), images: Map of "<id>.<ext>" → Blob.
  async function readPackage(files) {
    var paths = Array.from(files.keys());
    var root = findRoot(paths);
    function get(name) {
      return files.get(root + name);
    }
    var fontData = JSON.parse(await blobText(get(FONT_DATA)));
    var info = get(GLYPH_INFO)
      ? readGlyphInfo(await blobText(get(GLYPH_INFO)))
      : { glyphMap: {}, glyphInfos: {} };
    var font = {
      format: "fontra-hive-try/1",
      glyphMap: info.glyphMap,
      glyphInfos: info.glyphInfos,
      glyphs: {},
      axes: fontData.axes || { axes: [], mappings: [] },
      sources: fontData.sources || {},
      unitsPerEm: fontData.unitsPerEm || 1000,
      fontInfo: fontData.fontInfo || {},
      customData: fontData.customData || {},
      kerning: get(KERNING) ? readKerning(await blobText(get(KERNING))) : {},
      features: {
        language: (fontData.features && fontData.features.language) || "fea",
        text: get(FEATURES) ? await blobText(get(FEATURES)) : "",
      },
    };
    if (fontData.conditionalSubstitutions) {
      font.conditionalSubstitutions = fontData.conditionalSubstitutions;
    }
    var images = new Map();
    for (var i = 0; i < paths.length; i++) {
      var path = paths[i];
      if (!path.startsWith(root)) continue;
      var rel = path.slice(root.length);
      if (rel.startsWith(GLYPHS_DIR) && rel.endsWith(".json")) {
        var stem = rel.slice(GLYPHS_DIR.length, -5);
        if (stem.indexOf("/") !== -1) continue;
        var glyph = JSON.parse(await blobText(files.get(path)));
        var name = fileNameToString(stem);
        glyph.name = name;
        font.glyphs[name] = packGlyph(glyph);
        if (!(name in font.glyphMap)) font.glyphMap[name] = [];
      } else if (rel.startsWith(IMAGES_DIR) && rel.indexOf("/", IMAGES_DIR.length) === -1) {
        images.set(rel.slice(IMAGES_DIR.length), files.get(path));
      }
    }
    // A glyph listed without its file: an empty glyph, as Fontra would show.
    Object.keys(font.glyphMap).forEach(function (name) {
      if (!font.glyphs[name]) delete font.glyphMap[name];
    });
    return { font: font, images: images };
  }

  function fontDataJSON(font) {
    var data = {};
    if (font.fontInfo && Object.keys(font.fontInfo).length) data.fontInfo = font.fontInfo;
    data.axes = font.axes || { axes: [] };
    data.sources = font.sources || {};
    if (font.unitsPerEm && font.unitsPerEm !== 1000) data.unitsPerEm = font.unitsPerEm;
    if (font.customData && Object.keys(font.customData).length) {
      data.customData = font.customData;
    }
    if (font.conditionalSubstitutions) {
      data.conditionalSubstitutions = font.conditionalSubstitutions;
    }
    if (font.features && font.features.language && font.features.language !== "fea") {
      data.features = font.features;
    }
    return dumpJSON(data) + "\n";
  }

  function glyphFileName(name) {
    return GLYPHS_DIR + stringToFileName(name) + ".json";
  }

  function glyphJSON(font, name) {
    var glyph = unpackGlyph(font.glyphs[name]);
    glyph.name = name;
    return dumpJSON(glyph) + "\n";
  }

  // The package's files (Map of relative path → string), without images.
  function writePackage(font) {
    var files = new Map();
    files.set(FONT_DATA, fontDataJSON(font));
    files.set(GLYPH_INFO, writeGlyphInfo(font.glyphMap, font.glyphInfos));
    var kerning = writeKerning(font.kerning);
    if (kerning) files.set(KERNING, kerning);
    var features = font.features && font.features.language !== "fea" ? null : font.features;
    if (features && features.text) files.set(FEATURES, features.text);
    Object.keys(font.glyphs).forEach(function (name) {
      files.set(glyphFileName(name), glyphJSON(font, name));
    });
    return files;
  }

  // ---- zip ----

  var CRC_TABLE = (function () {
    var table = new Uint32Array(256);
    for (var n = 0; n < 256; n++) {
      var c = n;
      for (var k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      table[n] = c >>> 0;
    }
    return table;
  })();

  function crc32(bytes) {
    var crc = 0xffffffff;
    for (var i = 0; i < bytes.length; i++) {
      crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
    }
    return (crc ^ 0xffffffff) >>> 0;
  }

  async function streamBytes(bytes, stream) {
    var response = new Response(new Blob([bytes]).stream().pipeThrough(stream));
    return new Uint8Array(await response.arrayBuffer());
  }

  async function toBytes(data) {
    if (typeof data === "string") return new TextEncoder().encode(data);
    if (data instanceof Uint8Array) return data;
    return new Uint8Array(await data.arrayBuffer());
  }

  async function unzip(blob) {
    var bytes = await toBytes(blob);
    var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    var end = -1;
    for (var i = bytes.length - 22; i >= Math.max(0, bytes.length - 65557); i--) {
      if (view.getUint32(i, true) === 0x06054b50) {
        end = i;
        break;
      }
    }
    if (end < 0) throw new Error("This is not a .zip file.");
    var count = view.getUint16(end + 10, true);
    var offset = view.getUint32(end + 16, true);
    if (count === 0xffff || offset === 0xffffffff) {
      throw new Error("This .zip is too large (zip64 is not supported).");
    }
    var files = new Map();
    var decoder = new TextDecoder();
    for (var n = 0; n < count; n++) {
      if (view.getUint32(offset, true) !== 0x02014b50) throw new Error("Damaged .zip file.");
      var method = view.getUint16(offset + 10, true);
      var compressedSize = view.getUint32(offset + 20, true);
      var nameLength = view.getUint16(offset + 28, true);
      var extraLength = view.getUint16(offset + 30, true);
      var commentLength = view.getUint16(offset + 32, true);
      var local = view.getUint32(offset + 42, true);
      var name = decoder.decode(bytes.subarray(offset + 46, offset + 46 + nameLength));
      offset += 46 + nameLength + extraLength + commentLength;
      if (name.endsWith("/")) continue;
      if (name.split("/").indexOf("..") !== -1) throw new Error("Unsafe path in .zip.");
      var dataStart =
        local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
      var data = bytes.subarray(dataStart, dataStart + compressedSize);
      if (method === 8) {
        data = await streamBytes(data, new DecompressionStream("deflate-raw"));
      } else if (method !== 0) {
        throw new Error("Unsupported compression in .zip: " + name);
      }
      files.set(name, new Blob([data]));
    }
    return files;
  }

  // files: Map of path → string | Blob | Uint8Array. Returns a Blob.
  async function zip(files) {
    var encoder = new TextEncoder();
    var chunks = [];
    var central = [];
    var offset = 0;
    var now = new Date();
    var time = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
    var date =
      ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
    var entries = Array.from(files.entries());
    for (var n = 0; n < entries.length; n++) {
      var name = encoder.encode(entries[n][0]);
      var raw = await toBytes(entries[n][1]);
      var deflated = await streamBytes(raw, new CompressionStream("deflate-raw"));
      var crc = crc32(raw);
      var header = new DataView(new ArrayBuffer(30));
      header.setUint32(0, 0x04034b50, true);
      header.setUint16(4, 20, true);
      header.setUint16(6, 0x0800, true); // UTF-8 names
      header.setUint16(8, 8, true);
      header.setUint16(10, time, true);
      header.setUint16(12, date, true);
      header.setUint32(14, crc, true);
      header.setUint32(18, deflated.length, true);
      header.setUint32(22, raw.length, true);
      header.setUint16(26, name.length, true);
      chunks.push(header.buffer, name, deflated);
      var entry = new DataView(new ArrayBuffer(46));
      entry.setUint32(0, 0x02014b50, true);
      entry.setUint16(4, 20, true);
      entry.setUint16(6, 20, true);
      entry.setUint16(8, 0x0800, true);
      entry.setUint16(10, 8, true);
      entry.setUint16(12, time, true);
      entry.setUint16(14, date, true);
      entry.setUint32(16, crc, true);
      entry.setUint32(20, deflated.length, true);
      entry.setUint32(24, raw.length, true);
      entry.setUint16(28, name.length, true);
      entry.setUint32(42, offset, true);
      central.push(entry.buffer, name);
      offset += 30 + name.length + deflated.length;
    }
    var centralSize = central.reduce(function (sum, part) {
      return sum + part.byteLength;
    }, 0);
    var end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, entries.length, true);
    end.setUint16(10, entries.length, true);
    end.setUint32(12, centralSize, true);
    end.setUint32(16, offset, true);
    return new Blob(chunks.concat(central, [end.buffer]), { type: "application/zip" });
  }

  global.HiveTryFormat = {
    stringToFileName: stringToFileName,
    fileNameToString: fileNameToString,
    parseCSV: parseCSV,
    writeCSV: writeCSV,
    dumpJSON: dumpJSON,
    packPath: packPath,
    unpackPath: unpackPath,
    packGlyph: packGlyph,
    unpackGlyph: unpackGlyph,
    readGlyphInfo: readGlyphInfo,
    writeGlyphInfo: writeGlyphInfo,
    readKerning: readKerning,
    writeKerning: writeKerning,
    findRoot: findRoot,
    readPackage: readPackage,
    writePackage: writePackage,
    fontDataJSON: fontDataJSON,
    glyphFileName: glyphFileName,
    glyphJSON: glyphJSON,
    zip: zip,
    unzip: unzip,
    crc32: crc32,
    FONT_DATA: FONT_DATA,
    GLYPH_INFO: GLYPH_INFO,
    KERNING: KERNING,
    FEATURES: FEATURES,
    IMAGES_DIR: IMAGES_DIR,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
