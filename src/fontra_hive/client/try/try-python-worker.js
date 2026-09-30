// Fontra Hive, "Try Fontra": Python in the browser. A module worker, shared
// by the tabs of this site when the browser has SharedWorker (so two windows
// on the same font are served by one server, and see each other's edits),
// dedicated to its page otherwise. Loads
// Pyodide from the address it is given, and Hive's Python bundle
// (/hive/try/python.zip), on first use only. Two jobs:
//
// 1. Conversions (hive_try_convert.py): formats the JavaScript side does not
//    read (UFO, designspace, TrueType/OpenType, TTX), and downloads as
//    designspace + UFOs.
// 2. Hive's server for the fonts kept in the browser (hive_try_server.py):
//    each font is a bare git repository (/repos/local:<id>.git) kept in
//    IndexedDB (Emscripten's IDBFS); the editor's WebSocket and the
//    plug-in's /api/hive/* requests are served by the same code as on
//    fontrahive.com (Fontra's FontHandler, Hive's git backend and routes).
//
// Requests: {id, op, base, ...} → {id, ...} or {id, error}; progress
// {id, progress}. Unsolicited: {op: "ws", socket, text} (a message for the
// editor), {op: "ws-closed", socket}, {op: "status", text} (saved / saving,
// to every tab). A tab says {op: "hello", tab} once it holds the Web Lock
// "fontra-hive-try-tab:<tab>": when the lock is free, the tab is gone and
// its editor connections are closed. Socket ids are the page's own; each
// tab's are numbered apart here.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const BUNDLE_URL = "/hive/try/python.zip";
const SITE = "/home/pyodide/hive";
const REPOS = "/repos";
const PERSIST_DELAY = 2500; // ms after an edit: the git backend commits after 1 s
let pyodidePromise = null;
let pyclipperPromise = null;
let brotliPromise = null;
let hivePromise = null;
let counter = 0;
const mounts = new Map(); // mount point → promise (loaded from IndexedDB)
const socketsReady = new Map(); // socket (here) → promise (opened in Python)
const ports = new Set(); // one per tab
let nextSocket = 1;

function broadcast(message) {
  for (const port of ports) port.postMessage(message);
}

function ready(base, say) {
  if (!pyodidePromise) {
    pyodidePromise = (async () => {
      say("Loading Python (the first time takes a moment)…");
      const { loadPyodide } = await import(base + "pyodide.mjs");
      const py = await loadPyodide({ indexURL: base });
      say("Loading Fontra's code…");
      const response = await fetch(BUNDLE_URL);
      if (!response.ok) throw new Error("python.zip: HTTP " + response.status);
      py.unpackArchive(new Uint8Array(await response.arrayBuffer()), "zip", {
        extractDir: SITE,
      });
      py.runPython(`import sys; sys.path.insert(0, ${JSON.stringify(SITE)}); import hive_try_convert`);
      return py;
    })();
    pyodidePromise.catch(() => {
      pyodidePromise = null;
    });
  }
  return pyodidePromise;
}

// A second bundle, loaded when first needed: "glyphs" (reading Glyphs files).
const extras = new Map();
function extra(py, name, say) {
  if (!extras.has(name)) {
    extras.set(
      name,
      (async () => {
        say("Loading what reads Glyphs files…");
        const response = await fetch(`/hive/try/python-${name}.zip`);
        if (response.status === 404) throw new Error("Glyphs files cannot be read on this server.");
        if (!response.ok) throw new Error(`python-${name}.zip: HTTP ${response.status}`);
        py.unpackArchive(new Uint8Array(await response.arrayBuffer()), "zip", { extractDir: SITE });
        py.runPython("import importlib; importlib.invalidate_caches()");
      })()
    );
    extras.get(name).catch(() => extras.delete(name));
  }
  return extras.get(name);
}

function hive(base, say) {
  if (!hivePromise) {
    hivePromise = (async () => {
      const py = await ready(base, say);
      say("Starting Hive…");
      py.FS.mkdirTree(REPOS);
      py.runPython("import hive_try_server; hive = hive_try_server.BrowserHive()");
      return py;
    })();
    hivePromise.catch(() => {
      hivePromise = null;
    });
  }
  return hivePromise;
}

// ---- repositories kept in IndexedDB ----

function repoPath(name) {
  if (!/^local:[0-9a-f]{12}$/.test(name)) throw new Error("not a local project: " + name);
  return `${REPOS}/${name}.git`;
}

function syncfs(py, populate) {
  return new Promise((resolve, reject) =>
    py.FS.syncfs(populate, (error) => (error ? reject(error) : resolve()))
  );
}

// Load a project's repository from IndexedDB (its own database, named after
// the mount point) the first time it is used in this worker.
async function mount(py, name) {
  const path = repoPath(name);
  if (!mounts.has(path)) {
    mounts.set(
      path,
      (async () => {
        py.FS.mkdirTree(path);
        py.FS.mount(py.FS.filesystems.IDBFS, { autoPersist: true }, path);
        await syncfs(py, true);
      })()
    );
  }
  await mounts.get(path);
  return path;
}

let persistTimer = null;
let persisting = Promise.resolve();

function persistSoon(py) {
  clearTimeout(persistTimer);
  broadcast({ op: "status", text: "saving" });
  persistTimer = setTimeout(() => persistNow(py), PERSIST_DELAY);
}

function persistNow(py) {
  clearTimeout(persistTimer);
  persistTimer = null;
  persisting = persisting
    .then(async () => {
      await py.runPythonAsync("await hive.flush()");
      await syncfs(py, false);
      broadcast({ op: "status", text: "saved" });
    })
    .catch((error) => {
      console.error(error);
      broadcast({ op: "status", text: "error", detail: message(error) });
    });
  return persisting;
}

function deleteDatabase(name) {
  return new Promise((resolve) => {
    const request = indexedDB.deleteDatabase(name);
    request.onsuccess = request.onerror = request.onblocked = () => resolve();
  });
}

// ---- files in and out of Pyodide's file system ----

function readTree(py, root) {
  const files = [];
  const walk = (dir, prefix) => {
    for (const name of py.FS.readdir(dir)) {
      if (name === "." || name === "..") continue;
      const path = dir + "/" + name;
      if (py.FS.isDir(py.FS.stat(path).mode)) walk(path, prefix + name + "/");
      else files.push([prefix + name, py.FS.readFile(path).slice().buffer]);
    }
  };
  walk(root, "");
  return files;
}

function writeTree(py, root, files) {
  for (const [path, data] of files) {
    const full = root + "/" + path;
    py.FS.mkdirTree(full.slice(0, full.lastIndexOf("/")));
    py.FS.writeFile(full, new Uint8Array(data));
  }
}

// Run Python code with arguments, as a global of its own: calls may overlap
// (an editor's connection lasts as long as the page).
let callCounter = 0;
async function call(py, code, args) {
  const key = "hive_args_" + callCounter++;
  const pyArgs = py.toPy(args);
  py.globals.set(key, pyArgs);
  try {
    return await py.runPythonAsync(code.replaceAll("hive_args", key));
  } finally {
    py.globals.delete(key);
    pyArgs.destroy();
  }
}

// Pyodide's memory file system keeps what is left in it: remove the work
// folders (/tmp/hive-try-in/<n> here, /tmp/hive-try/<n> in Python).
function cleanUp(py, ...paths) {
  py.globals.set("hive_paths", py.toPy(paths));
  py.runPython(`
import pathlib, shutil
roots = {pathlib.Path("/tmp/hive-try"), pathlib.Path("/tmp/hive-try-in")}
for p in map(pathlib.Path, hive_paths):
    while p.parent not in roots and p != p.parent:
        p = p.parent
    if p.parent in roots:
        shutil.rmtree(p, ignore_errors=True)
`);
  py.globals.delete("hive_paths");
}

function message(error) {
  const text = String((error && error.message) || error);
  // A Python exception: its last line says what happened.
  const lines = text.trim().split("\n");
  const last = lines[lines.length - 1];
  return last.replace(/^[\w.]*(ImportError_|Error|Exception): /, "");
}

// ---- the operations ----

const OPS = {
  async toFontra(request, say) {
    const py = await ready(request.base, say);
    for (const name of request.extras || []) await extra(py, name, say);
    say("Converting " + request.name + "…");
    const work = "/tmp/hive-try-in/" + counter++;
    const upload = work + "/" + request.name.replace(/[\\/]/g, "_");
    py.FS.mkdirTree(work);
    py.FS.writeFile(upload, new Uint8Array(request.data));
    const result = await call(
      py,
      "await hive_try_convert.toFontra(hive_args['upload'], hive_args['name'])",
      { upload, name: request.name }
    );
    const files = readTree(py, result);
    cleanUp(py, work, result);
    return [{ files }, files.map((f) => f[1])];
  },

  async toDesignspaceZip(request, say) {
    const py = await ready(request.base, say);
    say("Writing the designspace and its UFOs…");
    const work = "/tmp/hive-try-in/" + counter++;
    const pkg = work + "/" + request.stem + ".fontra";
    writeTree(py, pkg, request.files);
    const data = await designspace(py, pkg, request.stem, work);
    return [{ data }, [data]];
  },

  // A new font kept in the browser: a repository whose first commit is the
  // .fontra package given.
  async createProject(request, say) {
    const py = await hive(request.base, say);
    say("Keeping the font in this browser…");
    await mount(py, request.name);
    const work = "/tmp/hive-try-in/" + counter++;
    const pkg = work + "/font.fontra";
    writeTree(py, pkg, request.files);
    await call(py, "hive.createProject(hive_args['name'], hive_args['pkg'])", {
      name: request.name,
      pkg,
    });
    cleanUp(py, work);
    await syncfs(py, false);
    return [{}];
  },

  async deleteProject(request, say) {
    const py = await hive(request.base, say);
    const path = repoPath(request.name);
    await call(
      py,
      "import shutil; shutil.rmtree(hive_args['path'], ignore_errors=True)",
      { path }
    );
    if (mounts.has(path)) {
      await mounts.get(path);
      py.FS.unmount(path);
      mounts.delete(path);
    }
    await deleteDatabase(path);
    return [{}];
  },

  // The editor's WebSocket: open (then served until closed), messages.
  // Messages are not queued behind other requests: a request may be waiting
  // for the editor's answer (a restore reloads the glyph in the editor).
  wsOpen(request, say, tab) {
    const socket = nextSocket++;
    tab.sockets.set(request.socket, socket);
    const opened = OPS._wsOpen(request, say, tab, socket);
    socketsReady.set(socket, opened);
    return opened;
  },

  async _wsOpen(request, say, tab, socket) {
    const py = await hive(request.base, say);
    const name = request.project.split("@")[0];
    await mount(py, name);
    const pageSocket = request.socket;
    const send = (text) => tab.port.postMessage({ op: "ws", socket: pageSocket, text });
    py.globals.set("hive_send", send);
    py.runPython(`hive.openSocket(${Number(socket)}, hive_send)`);
    py.globals.delete("hive_send");
    if (request.label) {
      await call(py, "hive.setName(hive_args['name'], hive_args['label'])", { name, label: request.label });
    }
    await call(py, "hive.setCompiledFormats(hive_args['formats'])", { formats: request.compiled || [] });
    say("");
    call(py, "await hive.connect(hive_args['socket'], hive_args['project'])", {
      socket,
      project: request.project,
    })
      .catch((error) => {
        console.error(error);
        send(JSON.stringify({ "initialization-error": message(error) }));
      })
      .finally(() => {
        tab.sockets.delete(pageSocket);
        tab.port.postMessage({ op: "ws-closed", socket: pageSocket });
      });
    return [{}];
  },

  async wsSend(request, say, tab) {
    const socket = tab.sockets.get(request.socket);
    if (!socket) return [{}];
    await socketsReady.get(socket);
    const py = await pyodidePromise;
    py.globals.set("hive_text", request.text);
    py.runPython(`hive.socketMessage(${Number(socket)}, hive_text)`);
    py.globals.delete("hive_text");
    if (request.text && request.text.includes('"editFinal"')) persistSoon(py);
    return [{}];
  },

  async wsClose(request, say, tab) {
    const socket = tab.sockets.get(request.socket);
    if (socket) await closeSocket(socket);
    return [{}];
  },

  // The plug-in's /api/hive/* requests.
  async http(request, say) {
    const py = await hive(request.base, say);
    const match = /^\/api\/hive\/projects\/([^/?]+)/.exec(new URL(request.url, "http://x").pathname);
    if (match) await mount(py, decodeURIComponent(match[1]).split("@")[0]);
    const result = await call(
      py,
      "await hive.http(hive_args['method'], hive_args['url'], hive_args['body'])",
      { method: request.method, url: request.url, body: request.body ?? null }
    );
    const response = result.toJs({ dict_converter: Object.fromEntries });
    result.destroy();
    if (request.method !== "GET") persistSoon(py);
    return [{ response }];
  },

  // A font kept in the browser, as a .fontra package (its latest state).
  async exportFontra(request, say) {
    const py = await hive(request.base, say);
    await mount(py, request.name.split("@")[0]);
    await py.runPythonAsync("await hive.flush()");
    const work = "/tmp/hive-try-in/" + counter++;
    await call(py, "hive.exportProject(hive_args['name'], hive_args['dest'])", {
      name: request.name,
      dest: work + "/font.fontra",
    });
    const files = readTree(py, work + "/font.fontra");
    cleanUp(py, work);
    return [{ files }, files.map((f) => f[1])];
  },

  async exportDesignspace(request, say) {
    const py = await hive(request.base, say);
    await mount(py, request.name.split("@")[0]);
    await py.runPythonAsync("await hive.flush()");
    say("Writing the designspace and its UFOs…");
    const work = "/tmp/hive-try-in/" + counter++;
    const pkg = work + "/" + request.stem + ".fontra";
    await call(py, "hive.exportProject(hive_args['name'], hive_args['dest'])", {
      name: request.name,
      dest: pkg,
    });
    const data = await designspace(py, pkg, request.stem, work);
    return [{ data }, [data]];
  },

  // What the editor asks Fontra's server over HTTP (POST /api/<name>):
  // path operations (Remove overlap, Union…) and pasting from other apps.
  // The same functions (fontra.core.serverutils), path operations done by
  // booleanOperations over Pyodide's pyclipper (hive_try_pathops).
  async api(request, say) {
    const py = await ready(request.base, say);
    if (request.name !== "parseClipboard") {
      if (!pyclipperPromise) {
        pyclipperPromise = py.loadPackage("pyclipper", { messageCallback: () => {} });
        pyclipperPromise.catch(() => (pyclipperPromise = null));
      }
      await pyclipperPromise;
    }
    const result = await call(
      py,
      `
import json
from fontra.core.serverutils import apiFunctions
_f = apiFunctions.get(hive_args["name"])
if _f is None:
    _r = {"error": "unknown function " + hive_args["name"]}
else:
    try:
        _r = {"returnValue": _f(**json.loads(hive_args["args"]))}
    except Exception as error:
        _r = {"error": repr(error)}
json.dumps(_r)
`,
      { name: request.name, args: request.args }
    );
    return [{ body: result }];
  },

  // A designspace + UFOs .zip made ready for fontc: one designspace per
  // combination of discrete axis values (hive_try_convert.fontcSources).
  async forFontc(request, say) {
    const py = await ready(request.base, say);
    const work = "/tmp/hive-try-in/" + counter++;
    py.FS.mkdirTree(work);
    py.FS.writeFile(work + "/sources.zip", new Uint8Array(request.data));
    const result = await call(
      py,
      "import json; json.dumps(hive_try_convert.fontcSources(hive_args['zip'], hive_args['stem']))",
      { zip: work + "/sources.zip", stem: request.stem }
    );
    const { zip, parts } = JSON.parse(result);
    const data = py.FS.readFile(zip).slice().buffer;
    cleanUp(py, work, zip);
    return [{ data, parts }, [data]];
  },

  // A TrueType font (from fontc) as WOFF2: fontTools, with Pyodide's brotli.
  async woff2(request, say) {
    const py = await ready(request.base, say);
    say("Compressing as WOFF2…");
    if (!brotliPromise) {
      brotliPromise = py.loadPackage("brotli", { messageCallback: () => {} });
      brotliPromise.catch(() => (brotliPromise = null));
    }
    await brotliPromise;
    const work = "/tmp/hive-try-in/" + counter++;
    py.FS.mkdirTree(work);
    py.FS.writeFile(work + "/in.ttf", new Uint8Array(request.data));
    await call(
      py,
      `
from fontTools.ttLib import woff2
woff2.compress(hive_args["src"], hive_args["dst"])
`,
      { src: work + "/in.ttf", dst: work + "/out.woff2" }
    );
    const data = py.FS.readFile(work + "/out.woff2").slice().buffer;
    cleanUp(py, work);
    return [{ data }, [data]];
  },

  async flush(request, say) {
    if (!hivePromise) return [{}];
    const py = await hive(request.base, say);
    await persistNow(py);
    return [{}];
  },
};

async function designspace(py, pkg, stem, work) {
  const result = await call(
    py,
    "await hive_try_convert.toDesignspaceZip(hive_args['pkg'], hive_args['stem'])",
    { pkg, stem }
  );
  const data = py.FS.readFile(result).slice().buffer;
  cleanUp(py, work, result);
  return data;
}

async function closeSocket(socket) {
  const opened = socketsReady.get(socket);
  if (!opened) return;
  socketsReady.delete(socket);
  await opened.catch(() => {});
  const py = await pyodidePromise;
  py.runPython(`hive.socketMessage(${Number(socket)}, None)`);
  persistNow(py);
}

// A tab gone: its editors' connections are closed (the font's other windows
// stay served), and what they edited is saved.
function tabGone(tab) {
  ports.delete(tab.port);
  for (const socket of tab.sockets.values()) closeSocket(socket);
  tab.sockets.clear();
}

function serve(port) {
  const tab = { port, sockets: new Map() }; // page socket id → socket here
  ports.add(port);
  port.onmessage = (event) => {
    const request = event.data;
    if (request.op === "hello") {
      if (self.navigator.locks && request.tab) {
        self.navigator.locks.request("fontra-hive-try-tab:" + request.tab, () => tabGone(tab));
      }
      return;
    }
    const say = (progress) => request.id && port.postMessage({ id: request.id, progress });
    const run = async () => {
      try {
        const op = OPS[request.op];
        if (!op || request.op.startsWith("_")) throw new Error("unknown operation " + request.op);
        const [reply, transfer] = await op(request, say, tab);
        if (request.id) port.postMessage(Object.assign({ id: request.id }, reply), transfer || []);
      } catch (error) {
        console.error(error);
        if (request.id) port.postMessage({ id: request.id, error: message(error) });
      }
    };
    run();
  };
}

if ("onconnect" in self) {
  // A SharedWorker: one port per tab.
  self.onconnect = (event) => {
    const port = event.ports[0];
    serve(port);
    port.start();
  };
} else {
  serve(self);
}
