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
//   from a .fontra package), answered here in JavaScript; edits stay in the
//   tab, a reload starts over. Fontra applies each change to its own copy
//   first, then sends it ("editFinal"); this engine keeps its copy in step by
//   reading the edited data back from the page's FontController when it can,
//   and otherwise applies the plain changes ("=", "d", "+", "-", ":").
// - "local:<id>": a font the visitor opened, kept in this browser as a git
//   repository, and served by Hive's own server code running in Python
//   (try-python-worker.js, hive_try_server.py): the WebSocket messages go
//   there, and so do the Hive plug-in's /api/hive/* requests (history,
//   restore, snapshots, comments). The project's name is in try-store.js.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
(function () {
  "use strict";

  var FONT_URL = "/hive/try/demo-font.json";
  var PLUGINS_KEY = "fontra.pluginsplugins";
  var NativeWebSocket = window.WebSocket;

  var project = new URL(location.href).searchParams.get("project") || "";
  var LOCAL_PREFIX = "local:";
  var localName = project.startsWith(LOCAL_PREFIX) ? project.split("@")[0] : null;
  var localId = localName ? localName.slice(LOCAL_PREFIX.length) : null;

  // The editor's plug-ins: none on the demo; on a font kept here, the Hive
  // plug-in (history, snapshots, comments), served by Python in the browser.
  // Fontra's own list is left untouched for the real editor.
  var PLUGINS = localId ? JSON.stringify([{ address: "/hive/plugin" }]) : "[]";
  try {
    var getItem = Storage.prototype.getItem;
    var setItem = Storage.prototype.setItem;
    Storage.prototype.getItem = function (key) {
      if (this === window.localStorage && key === PLUGINS_KEY) return PLUGINS;
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

  var projectName = localId ? null : "MutatorSans (demo)";
  var images = new Map(); // "<id>.<ext>" → Blob (the demo's: none)

  // The demo font (a local font lives in Python: see python() below).
  var fontPromise = null;
  function loadFont() {
    if (!fontPromise) fontPromise = loadDemo();
    return fontPromise;
  }

  // A local font's name (and a check that it is kept here).
  var infoPromise = null;
  function localInfo() {
    if (!infoPromise) {
      infoPromise = Store.info(localId).then(function (info) {
        projectName = info.name;
        Store.persist();
        return info;
      });
    }
    return infoPromise;
  }

  function loadDemo() {
    return fetch(FONT_URL).then(function (response) {
      if (!response.ok) throw new Error("demo font: HTTP " + response.status);
      return response.json();
    });
  }

  // The font as a .fontra package, zipped.
  function download() {
    return downloadAs("fontra");
  }

  // ---- Python in the browser (try-python-worker.js), on first use ----

  var DEFAULT_PYODIDE = "https://cdn.jsdelivr.net/pyodide/v314.0.7/full/";
  var pythonWorker = null;
  var pythonCalls = {};
  var pythonId = 1;

  function pyodideBase() {
    var meta = document.querySelector('meta[name="hive-pyodide"]');
    var base = (meta && meta.content) || DEFAULT_PYODIDE;
    return new URL(base.endsWith("/") ? base : base + "/", location.href).href;
  }

  var sockets = {}; // id → PythonWebSocket
  var status = { text: "", detail: "" };

  function worker() {
    if (!pythonWorker) {
      pythonWorker = new Worker("/hive/try/try-python-worker.js", { type: "module" });
      pythonWorker.onmessage = function (event) {
        var reply = event.data;
        if (reply.op === "ws") {
          if (sockets[reply.socket]) sockets[reply.socket].deliver(reply.text);
          return;
        }
        if (reply.op === "ws-closed") {
          if (sockets[reply.socket]) sockets[reply.socket].closed();
          return;
        }
        if (reply.op === "status") {
          status = { text: reply.text, detail: reply.detail || "" };
          window.dispatchEvent(new CustomEvent("hive-try-status", { detail: status }));
          return;
        }
        var call = pythonCalls[reply.id];
        if (!call) return;
        if (reply.progress) {
          if (call.onProgress) call.onProgress(reply.progress);
          return;
        }
        delete pythonCalls[reply.id];
        if (reply.error) call.reject(new Error(reply.error));
        else call.resolve(reply);
      };
    }
    return pythonWorker;
  }

  // A request to the worker; onProgress(text) is optional.
  function python(op, payload, transfer, onProgress) {
    var w = worker();
    return new Promise(function (resolve, reject) {
      var id = pythonId++;
      pythonCalls[id] = { resolve: resolve, reject: reject, onProgress: onProgress };
      var message = Object.assign({ id: id, op: op, base: pyodideBase() }, payload);
      w.postMessage(message, transfer || []);
    });
  }

  // A message to the worker that needs no answer.
  function tell(op, payload) {
    worker().postMessage(Object.assign({ op: op, base: pyodideBase() }, payload));
  }

  function progress(text) {
    window.dispatchEvent(new CustomEvent("hive-try-progress", { detail: text }));
  }

  // ---- fonts kept in this browser ----

  async function toBuffers(files) {
    var entries = [];
    for (var [path, data] of files) {
      var blob = typeof data === "string" ? new Blob([data]) : data;
      entries.push([path, await blob.arrayBuffer()]);
    }
    return entries;
  }

  // files: a .fontra package (Map of path → string | Blob). Returns the id.
  async function createLocal(name, files, onProgress) {
    var id = await Store.create(name);
    var entries = await toBuffers(files);
    try {
      await python(
        "createProject",
        { name: LOCAL_PREFIX + id, files: entries },
        entries.map(function (e) {
          return e[1];
        }),
        onProgress
      );
    } catch (error) {
      await Store.removeProject(id);
      throw error;
    }
    return id;
  }

  async function deleteLocal(id) {
    await python("deleteProject", { name: LOCAL_PREFIX + id });
    await Store.removeProject(id);
  }

  // A picked file (or a zip made of a picked folder) as .fontra files.
  // options.glyphs: it is (or holds) a Glyphs file, which needs more Python.
  async function convertToFontra(name, blob, onProgress, options) {
    var data = await blob.arrayBuffer();
    var extras = options && options.glyphs ? ["glyphs"] : [];
    var reply = await python("toFontra", { name: name, data: data, extras: extras }, [data], onProgress);
    var files = new Map();
    reply.files.forEach(function (entry) {
      files.set("converted.fontra/" + entry[0], new Blob([entry[1]]));
    });
    return files;
  }

  function fileStem() {
    var name = (projectName || "font").replace(/ \(demo\)$/, "");
    return name.replace(/[\\/:*?"<>|]/g, "_") || "font";
  }

  function saveBlob(blob, fileName) {
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = fileName;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(function () {
      URL.revokeObjectURL(link.href);
    }, 10000);
  }

  async function packageFiles(onProgress) {
    if (localId) {
      var reply = await python("exportFontra", { name: localName }, [], onProgress);
      return new Map(
        reply.files.map(function (entry) {
          return [entry[0], new Blob([entry[1]])];
        })
      );
    }
    var font = await loadFont();
    var files = Format.writePackage(font);
    images.forEach(function (blob, fileName) {
      files.set(Format.IMAGES_DIR + fileName, blob);
    });
    return files;
  }

  // format: "fontra" (zipped package) | "designspace" (zipped, with UFOs).
  async function downloadAs(format, onProgress) {
    if (localId) await localInfo();
    var stem = fileStem();
    if (format === "designspace" && localId) {
      var result = await python("exportDesignspace", { name: localName, stem: stem }, [], onProgress);
      saveBlob(new Blob([result.data], { type: "application/zip" }), stem + ".designspace.zip");
      return;
    }
    var files = await packageFiles(onProgress);
    if (format === "designspace") {
      var entries = [];
      var transfer = [];
      for (var [path, data] of files) {
        var buffer = await (typeof data === "string"
          ? new Blob([data])
          : data
        ).arrayBuffer();
        entries.push([path, buffer]);
        transfer.push(buffer);
      }
      var reply = await python(
        "toDesignspaceZip",
        { stem: stem, files: entries },
        transfer,
        onProgress
      );
      saveBlob(new Blob([reply.data], { type: "application/zip" }), stem + ".designspace.zip");
      return;
    }
    var zipped = new Map();
    files.forEach(function (data, rel) {
      zipped.set(stem + ".fontra/" + rel, data);
    });
    saveBlob(await Format.zip(zipped), stem + ".fontra.zip");
  }

  // Keep the current font (the demo, edits included) in this browser.
  async function saveCopy(name, onProgress) {
    var font = await loadFont();
    var files = Format.writePackage(font);
    images.forEach(function (blob, fileName) {
      files.set(Format.IMAGES_DIR + fileName, blob);
    });
    return createLocal(name, files, onProgress);
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
      // A glyph removed through the glyph map only.
      if (what.keys.indexOf("glyphMap") !== -1) {
        Object.keys(font.glyphs).forEach(function (name) {
          if (!(name in font.glyphMap)) delete font.glyphs[name];
        });
      }
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

  // For a font kept here: the same WebSocket protocol, served by Hive's
  // server in Python (try-python-worker.js).
  var nextSocket = 1;
  function PythonWebSocket(url) {
    this.url = String(url);
    this.readyState = 0;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    this.id = nextSocket++;
    sockets[this.id] = this;
    var self = this;
    var projectId = new URL(this.url, location.href).searchParams.get("project");
    localInfo()
      .catch(function () {
        return {};
      })
      .then(function (info) {
        return python("wsOpen", { socket: self.id, project: projectId, label: info.name }, [], progress);
      })
      .then(
      function () {
        self.readyState = 1;
        progress("");
        if (self.onopen) self.onopen({ type: "open", target: self });
      },
      function (error) {
        console.error(error);
        progress("");
        self.deliver(JSON.stringify({ "initialization-error": String(error.message || error) }));
      }
    );
  }
  PythonWebSocket.prototype.send = function (text) {
    // The handshake remote.js sends on opening: the server side has made its
    // own (hive_try_server.openSocket).
    if (/^\{\s*"client-uuid"/.test(text)) return;
    tell("wsSend", { socket: this.id, text: text });
  };
  PythonWebSocket.prototype.deliver = function (text) {
    if (this.onmessage) this.onmessage({ data: text });
  };
  PythonWebSocket.prototype.closed = function () {
    if (this.readyState === 3) return;
    this.readyState = 3;
    delete sockets[this.id];
    if (this.onclose) this.onclose({ type: "close", code: 1000 });
  };
  PythonWebSocket.prototype.close = function () {
    tell("wsClose", { socket: this.id });
    this.closed();
  };

  window.WebSocket = function (url, protocols) {
    if (new URL(url, location.href).pathname === "/websocket") {
      return localId ? new PythonWebSocket(url) : new TryWebSocket(url);
    }
    return protocols === undefined
      ? new NativeWebSocket(url)
      : new NativeWebSocket(url, protocols);
  };
  window.WebSocket.CONNECTING = 0;
  window.WebSocket.OPEN = 1;
  window.WebSocket.CLOSING = 2;
  window.WebSocket.CLOSED = 3;

  // The Hive plug-in's requests, for a font kept here: served by Python.
  if (localId) {
    var nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      var url = new URL(typeof input === "string" || input instanceof URL ? input : input.url, location.href);
      if (url.origin !== location.origin || !url.pathname.startsWith("/api/hive/")) {
        return nativeFetch(input, init);
      }
      var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
      var body = init && init.body !== undefined ? String(init.body) : null;
      return python("http", { method: method, url: url.pathname + url.search, body: body }).then(
        function (reply) {
          var r = reply.response;
          var headers = Object.assign({ "Content-Type": r.contentType || "text/plain" }, r.headers || {});
          var noBody = r.status === 204 || r.status === 304;
          return new Response(noBody ? null : r.body, { status: r.status, headers: headers });
        }
      );
    };
  }

  // Leaving: the demo's edits would be lost; a local font's last edits are
  // written to the browser's storage now.
  window.addEventListener("beforeunload", function (event) {
    if (!localId && TryFont.edited) {
      event.preventDefault();
      event.returnValue = "";
    } else if (localId && status.text === "saving") {
      tell("flush", {});
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pagehide", function () {
    if (localId && pythonWorker) tell("flush", {});
  });

  window.hiveTry = {
    font: loadFont,
    touched: touched,
    TryFont: TryFont,
    localId: localId,
    localName: localName,
    localInfo: localInfo,
    projectName: function () {
      return projectName;
    },
    createLocal: createLocal,
    deleteLocal: deleteLocal,
    flush: function () {
      return localId ? python("flush", {}) : Promise.resolve();
    },
    status: function () {
      return status;
    },
    download: download,
    downloadAs: downloadAs,
    convertToFontra: convertToFontra,
    python: python,
    saveCopy: saveCopy,
    format: Format,
    store: Store,
  };

  if (localId) {
    localInfo().catch(function (error) {
      console.error(error);
    });
  } else {
    loadFont().catch(function (error) {
      console.error(error);
    });
  }
})();
