// Fontra Hive: "Try Fontra", Fontra's editor with no font server.
//
// A classic script at the start of <head> of Fontra's own pages, before
// Fontra's modules. Fontra's client talks to its Python server over a
// WebSocket (fontra-core/src/remote.js: JSON messages with "client-call-id",
// "method-name", "arguments"). Here the WebSocket to /websocket is replaced
// by an object that answers the same calls in the browser, from a demo font
// loaded as one JSON file (demo-font.json, made from a .fontra project).
// Nothing is sent anywhere and nothing is saved: a reload starts over.
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

  var fontPromise = null;
  function loadFont() {
    if (!fontPromise) {
      fontPromise = fetch(FONT_URL).then(function (response) {
        if (!response.ok) throw new Error("demo font: HTTP " + response.status);
        return response.json();
      });
    }
    return fontPromise;
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
    },
  };

  var METHODS = {
    isReadOnly: function () {
      return false;
    },
    getBackEndInfo: function () {
      return {
        name: "FontraHiveTry",
        features: {},
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
      return font.conditionalSubstitutions || null;
    },
    getMetaInfo: function (font) {
      return font.metaInfo || { projectName: "MutatorSans (demo)" };
    },
    getBackgroundImage: function () {
      return null;
    },
    getShaperFontData: function () {
      return null;
    },
    putBackgroundImage: function () {
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

  // Leaving with edits: say they will be lost.
  window.addEventListener("beforeunload", function (event) {
    if (TryFont.edited) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  window.hiveTry = { font: loadFont, touched: touched, TryFont: TryFont };

  loadFont().catch(function (error) {
    console.error(error);
  });
})();
