// Fontra Hive: "Try Fontra", Fontra's editor with no font server.
//
// A classic script at the start of <head> of Fontra's own pages, before
// Fontra's modules. Fontra's client talks to its Python server over a
// WebSocket (fontra-core/src/remote.js: JSON messages with "client-call-id",
// "method-name", "arguments"). Here the WebSocket to /websocket is replaced
// by an object that answers the same calls in the browser. Nothing is sent
// anywhere.
//
// Two kinds of project (the "project" in the page's address):
// - "demo:MutatorSans": the demo font, one JSON file (demo-font.json, made
//   from a .fontra package); edits stay in the tab, a reload starts over.
// - "local:<id>": a .fontra package the visitor opened, kept in this browser
//   (try-store.js); each edit is written back to its files (fontra-format.js)
//   a moment later.
//
// Edits: Fontra applies each change to its own copy first, then sends it
// ("editFinal"). This engine keeps its copy in step by reading the edited
// data back from the page's FontController when it can (so path edits need
// no reimplementation here), and otherwise applies the plain changes
// ("=", "d", "+", "-", ":") itself.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
(function () {
  "use strict";

  var FONT_URL = "/hive/try/demo-font.json";
  var PLUGINS_KEY = "fontra.pluginsplugins";
  var NativeWebSocket = window.WebSocket;

  // No editor plug-ins here: the Hive plug-in (history, comments) needs a
  // Hive project. Fontra's list is left untouched for the real editor.
  try {
    var getItem = Storage.prototype.getItem;
    var setItem = Storage.prototype.setItem;
    Storage.prototype.getItem = function (key) {
      if (this === window.localStorage && key === PLUGINS_KEY) return "[]";
      return getItem.apply(this, arguments);
    };
    Storage.prototype.setItem = function (key) {
      if (this === window.localStorage && key === PLUGINS_KEY) return;
      return setItem.apply(this, arguments);
    };
  } catch (error) {
    // no localStorage: nothing to hide
  }

  var Format = window.HiveTryFormat;
  var Store = window.HiveTryStore;

  var project = new URL(location.href).searchParams.get("project") || "";
  var LOCAL_PREFIX = "local:";
  var localId = project.startsWith(LOCAL_PREFIX) ? project.slice(LOCAL_PREFIX.length) : null;
  var projectName = localId ? null : "MutatorSans (demo)";
  var images = new Map(); // "<id>.<ext>" → Blob

  var fontPromise = null;
  function loadFont() {
    if (!fontPromise) {
      fontPromise = localId ? loadLocal(localId) : loadDemo();
    }
    return fontPromise;
  }

  function loadDemo() {
    return fetch(FONT_URL).then(function (response) {
      if (!response.ok) throw new Error("demo font: HTTP " + response.status);
      return response.json();
    });
  }

  async function loadLocal(id) {
    var info = await Store.info(id);
    projectName = info.name;
    var result = await Format.readPackage(await Store.readFiles(id));
    images = result.images;
    Store.persist();
    return result.font;
  }

  // ---- writing a local project back, a moment after each edit ----

  var dirty = { glyphs: new Set(), keys: new Set(), images: new Set() };
  var saveTimer = null;
  var saving = Promise.resolve();
  var SAVE_DELAY = 300;

  function scheduleSave() {
    if (!localId) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, SAVE_DELAY);
    window.dispatchEvent(new CustomEvent("hive-try-saving"));
  }

  var FONT_DATA_KEYS = {
    axes: true,
    sources: true,
    unitsPerEm: true,
    fontInfo: true,
    customData: true,
    conditionalSubstitutions: true,
  };

  function flush() {
    clearTimeout(saveTimer);
    saveTimer = null;
    if (!localId) return saving;
    var glyphs = Array.from(dirty.glyphs);
    var keys = Array.from(dirty.keys);
    var newImages = Array.from(dirty.images);
    dirty = { glyphs: new Set(), keys: new Set(), images: new Set() };
    if (!glyphs.length && !keys.length && !newImages.length) return saving;
    saving = saving.then(async function () {
      var font = await loadFont();
      var writes = [];
      glyphs.forEach(function (name) {
        var rel = Format.glyphFileName(name);
        writes.push(
          font.glyphs[name]
            ? Store.write(localId, rel, Format.glyphJSON(font, name))
            : Store.remove(localId, rel)
        );
      });
      var needFontData = false;
      keys.forEach(function (key) {
        if (FONT_DATA_KEYS[key]) needFontData = true;
        if (key === "glyphMap" || key === "glyphInfos") {
          writes.push(
            Store.write(
              localId,
              Format.GLYPH_INFO,
              Format.writeGlyphInfo(font.glyphMap, font.glyphInfos)
            )
          );
        }
        if (key === "kerning") {
          var kerning = Format.writeKerning(font.kerning);
          writes.push(
            kerning
              ? Store.write(localId, Format.KERNING, kerning)
              : Store.remove(localId, Format.KERNING)
          );
        }
        if (key === "features") {
          needFontData = true;
          var text = font.features && font.features.language === "fea" && font.features.text;
          writes.push(
            text
              ? Store.write(localId, Format.FEATURES, text)
              : Store.remove(localId, Format.FEATURES)
          );
        }
      });
      if (needFontData) {
        writes.push(Store.write(localId, Format.FONT_DATA, Format.fontDataJSON(font)));
      }
      newImages.forEach(function (fileName) {
        writes.push(Store.write(localId, Format.IMAGES_DIR + fileName, images.get(fileName)));
      });
      await Promise.all(writes);
      await Store.touch(localId);
      window.dispatchEvent(new CustomEvent("hive-try-saved"));
    });
    saving = saving.catch(function (error) {
      console.error(error);
      window.dispatchEvent(new CustomEvent("hive-try-save-error", { detail: String(error) }));
    });
    return saving;
  }

  // The font as a .fontra package, zipped (after pending writes).
  async function download() {
    await flush();
    var font = await loadFont();
    var name = (projectName || "font").replace(/ \(demo\)$/, "");
    var stem = name.replace(/[\\/:*?"<>|]/g, "_") || "font";
    var files = new Map();
    Format.writePackage(font).forEach(function (data, rel) {
      files.set(stem + ".fontra/" + rel, data);
    });
    images.forEach(function (blob, fileName) {
      files.set(stem + ".fontra/" + Format.IMAGES_DIR + fileName, blob);
    });
    var blob = await Format.zip(files);
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = stem + ".fontra.zip";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(function () {
      URL.revokeObjectURL(link.href);
    }, 10000);
  }

  // Keep the current font (the demo, edits included) in this browser.
  async function saveCopy(name) {
    var font = await loadFont();
    var files = Format.writePackage(font);
    images.forEach(function (blob, fileName) {
      files.set(Format.IMAGES_DIR + fileName, blob);
    });
    return Store.create(name, files);
  }

  function base64(blob) {
    return blob.arrayBuffer().then(function (buffer) {
      var bytes = new Uint8Array(buffer);
      var text = "";
      for (var i = 0; i < bytes.length; i += 0x8000) {
        text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }
      return btoa(text);
    });
  }

  function copy(value) {
    return value === undefined ? null : JSON.parse(JSON.stringify(value));
  }

  // Plain changes, as in fontra-core/src/changes.js (baseChangeFunctions).
  var BASE = {
    "=": function (s, key, item) {
      s[key] = item;
    },
    d: function (s, key) {
      delete s[key];
    },
    "-": function (s, index, count) {
      s.splice(index, count === undefined ? 1 : count);
    },
    "+": function (s, index) {
      s.splice.apply(s, [index, 0].concat([].slice.call(arguments, 2)));
    },
    ":": function (s, index, count) {
      s.splice.apply(s, [index, count].concat([].slice.call(arguments, 3)));
    },
  };

  // Flatten a change into its leaves: [{path, f, a}].
  function leaves(change, prefix, out) {
    var path = prefix.concat(change.p || []);
    if (change.f) out.push({ path: path, f: change.f, a: change.a || [] });
    (change.c || []).forEach(function (child) {
      leaves(child, path, out);
    });
    return out;
  }

  function applyPlain(root, leaf) {
    var func = BASE[leaf.f];
    if (!func) return false;
    var subject = root;
    for (var i = 0; i < leaf.path.length; i++) {
      subject = subject[leaf.path[i]];
      if (subject === undefined || subject === null) return false;
    }
    func.apply(null, [subject].concat(copy(leaf.a)));
    return true;
  }

  // What a change touches: top-level keys, and glyph names under "glyphs".
  function touched(change) {
    var keys = {};
    var glyphs = {};
    leaves(change, [], []).forEach(function (leaf) {
      var key = leaf.path.length ? leaf.path[0] : leaf.a[0];
      if (key === undefined) return;
      if (key === "glyphs") {
        var name = leaf.path.length > 1 ? leaf.path[1] : leaf.a[0];
        if (name !== undefined) glyphs[name] = true;
      } else {
        keys[key] = true;
      }
    });
    return { keys: Object.keys(keys), glyphs: Object.keys(glyphs) };
  }

  var ROOT_KEYS = {
    glyphMap: true,
    axes: true,
    sources: true,
    unitsPerEm: true,
    customData: true,
    fontInfo: true,
    kerning: true,
    features: true,
  };

  var TryFont = {
    edited: false,

    // Keep our copy in step with the page's after an edit.
    async follow(change) {
      var font = await loadFont();
      var what = touched(change);
      var controller = window.editorController || window.fontOverviewController;
      var fc = controller && controller.fontController;
      var plain = leaves(change, [], []);
      var synced = {};
      await new Promise(function (resolve) {
        setTimeout(resolve, 0);
      });
      if (fc) {
        for (var i = 0; i < what.glyphs.length; i++) {
          var name = what.glyphs[i];
          try {
            if (fc.glyphMap && !(name in fc.glyphMap)) {
              delete font.glyphs[name];
              synced["glyphs/" + name] = true;
              continue;
            }
            var cache = fc._glyphsPromiseCache;
            var promise = cache && cache.get(name);
            var glyphController = promise && (await promise);
            if (glyphController && glyphController.glyph) {
              font.glyphs[name] = copy(glyphController.glyph);
              synced["glyphs/" + name] = true;
            }
          } catch (error) {
            // fall back on the plain changes below
          }
        }
        var root = fc._rootObject || {};
        what.keys.forEach(function (key) {
          if (ROOT_KEYS[key] && root[key] !== undefined) {
            font[key] = copy(root[key]);
            synced[key] = true;
          }
        });
      }
      plain.forEach(function (leaf) {
        var key = leaf.path.length ? leaf.path[0] : leaf.a[0];
        var id = key === "glyphs" ? "glyphs/" + (leaf.path[1] || leaf.a[0]) : key;
        if (!synced[id]) applyPlain(font, leaf);
      });
      what.glyphs.forEach(function (name) {
        dirty.glyphs.add(name);
      });
      what.keys.forEach(function (key) {
        dirty.keys.add(key);
        // A glyph added or removed through the glyph map only.
        if (key === "glyphMap") {
          Object.keys(font.glyphs).forEach(function (name) {
            if (!(name in font.glyphMap)) {
              delete font.glyphs[name];
              dirty.glyphs.add(name);
            }
          });
        }
      });
      scheduleSave();
    },
  };

  var METHODS = {
    isReadOnly: function () {
      return false;
    },
    getBackEndInfo: function () {
      return {
        name: "FontraHiveTry",
        features: { "background-image": true, "find-glyphs-that-use-glyph": true },
        projectManagerFeatures: {},
      };
    },
    getGlyphMap: function (font) {
      return font.glyphMap;
    },
    getGlyphInfos: function (font) {
      return font.glyphInfos || {};
    },
    getGlyph: function (font, name) {
      return font.glyphs[name] || null;
    },
    getAxes: function (font) {
      return font.axes;
    },
    getSources: function (font) {
      return font.sources;
    },
    getUnitsPerEm: function (font) {
      return font.unitsPerEm;
    },
    getFontInfo: function (font) {
      return font.fontInfo || {};
    },
    getCustomData: function (font) {
      return font.customData || {};
    },
    getKerning: function (font) {
      return font.kerning || {};
    },
    getFeatures: function (font) {
      return font.features || { language: "fea", text: "" };
    },
    getConditionalSubstitutions: function (font) {
      return font.conditionalSubstitutions || { featureTags: ["rclt"], rules: [] };
    },
    getMetaInfo: function () {
      return { projectName: projectName };
    },
    getBackgroundImage: function (font, identifier) {
      var fileName = Array.from(images.keys()).find(function (name) {
        return name.slice(0, name.lastIndexOf(".")) === identifier;
      });
      if (!fileName) return null;
      var type = fileName.slice(fileName.lastIndexOf(".") + 1).toUpperCase();
      return base64(images.get(fileName)).then(function (data) {
        return { type: type === "JPG" ? "JPEG" : type, data: data };
      });
    },
    getShaperFontData: function () {
      return null;
    },
    putBackgroundImage: function (font, identifier, image) {
      var bytes = Uint8Array.from(atob(image.data), function (c) {
        return c.charCodeAt(0);
      });
      var fileName = identifier + "." + String(image.type).toLowerCase();
      images.set(fileName, new Blob([bytes]));
      dirty.images.add(fileName);
      scheduleSave();
      return null;
    },
    findGlyphsThatUseGlyph: function (font, name) {
      return Object.keys(font.glyphs).filter(function (other) {
        var glyph = font.glyphs[other];
        return Object.keys(glyph.layers || {}).some(function (layer) {
          return (glyph.layers[layer].glyph.components || []).some(function (c) {
            return c.name === name;
          });
        });
      });
    },
    subscribeChanges: function () {
      return null;
    },
    unsubscribeChanges: function () {
      return null;
    },
    editIncremental: function () {
      return null;
    },
    editFinal: function (font, finalChange) {
      TryFont.edited = true;
      window.dispatchEvent(new CustomEvent("hive-try-edit"));
      return TryFont.follow(finalChange).then(function () {
        return null;
      });
    },
    exportAs: function () {
      throw new Error("Export is not available in the demo");
    },
  };

  // Stands in for the WebSocket that remote.js opens; any other WebSocket is
  // a real one.
  function TryWebSocket(url) {
    this.url = String(url);
    this.readyState = 0;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    var self = this;
    setTimeout(function () {
      self.readyState = 1;
      if (self.onopen) self.onopen({ type: "open", target: self });
    }, 0);
  }
  TryWebSocket.CONNECTING = 0;
  TryWebSocket.OPEN = 1;
  TryWebSocket.CLOSING = 2;
  TryWebSocket.CLOSED = 3;
  TryWebSocket.prototype.send = function (text) {
    var self = this;
    var message = JSON.parse(text);
    var id = message["client-call-id"];
    if (id === undefined) return; // "client-uuid", or a reply to the server
    var method = METHODS[message["method-name"]];
    var reply = function (payload) {
      payload["client-call-id"] = id;
      setTimeout(function () {
        if (self.onmessage) self.onmessage({ data: JSON.stringify(payload) });
      }, 0);
    };
    if (!method) {
      reply({ exception: "not available in the demo: " + message["method-name"] });
      return;
    }
    loadFont()
      .then(function (font) {
        return method.apply(null, [font].concat(message.arguments || []));
      })
      .then(function (value) {
        reply({ "return-value": copy(value) });
      })
      .catch(function (error) {
        console.error(error);
        reply({ exception: String(error && error.message ? error.message : error) });
      });
  };
  TryWebSocket.prototype.close = function () {
    this.readyState = 3;
  };

  window.WebSocket = function (url, protocols) {
    if (new URL(url, location.href).pathname === "/websocket") {
      return new TryWebSocket(url);
    }
    return protocols === undefined
      ? new NativeWebSocket(url)
      : new NativeWebSocket(url, protocols);
  };
  window.WebSocket.CONNECTING = 0;
  window.WebSocket.OPEN = 1;
  window.WebSocket.CLOSING = 2;
  window.WebSocket.CLOSED = 3;

  // Leaving with edits that would be lost: the demo's, or writes not done.
  window.addEventListener("beforeunload", function (event) {
    if ((!localId && TryFont.edited) || (localId && saveTimer !== null)) {
      if (localId) flush();
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pagehide", function () {
    if (localId) flush();
  });

  window.hiveTry = {
    font: loadFont,
    touched: touched,
    TryFont: TryFont,
    localId: localId,
    projectName: function () {
      return projectName;
    },
    flush: flush,
    download: download,
    saveCopy: saveCopy,
    format: Format,
    store: Store,
  };

  loadFont().catch(function (error) {
    console.error(error);
  });
})();
