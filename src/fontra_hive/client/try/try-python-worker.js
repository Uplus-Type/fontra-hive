// Fontra Hive, "Try Fontra": Python in the browser, for the formats that the
// JavaScript side does not read (UFO, designspace, TrueType/OpenType, TTX)
// and for downloads as designspace + UFOs. A module worker: loads Pyodide
// from the address it is given, and Hive's Python bundle (Fontra's backends,
// Hive's importer and export: /hive/try/python.zip), on first use only.
//
// Messages: {id, op: "toFontra", base, name, data: ArrayBuffer}
//             → {id, files: [[path, ArrayBuffer], ...]}  (the .fontra package)
//           {id, op: "toDesignspaceZip", base, stem, files: [[path, ArrayBuffer], ...]}
//             → {id, data: ArrayBuffer}
//           Progress: {id, progress: "text"}; failure: {id, error: "text"}.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const BUNDLE_URL = "/hive/try/python.zip";
const SITE = "/home/pyodide/hive";
let pyodidePromise = null;
let counter = 0;

function ready(base, say) {
  if (!pyodidePromise) {
    pyodidePromise = (async () => {
      say("Loading Python (the first time takes a moment)…");
      const { loadPyodide } = await import(base + "pyodide.mjs");
      const py = await loadPyodide({ indexURL: base });
      say("Loading Fontra's converters…");
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

async function call(py, code, args) {
  const pyArgs = py.toPy(args);
  py.globals.set("hive_args", pyArgs);
  try {
    return await py.runPythonAsync(code);
  } finally {
    py.globals.delete("hive_args");
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

onmessage = async (event) => {
  const request = event.data;
  const say = (progress) => postMessage({ id: request.id, progress });
  try {
    const py = await ready(request.base, say);
    const work = "/tmp/hive-try-in/" + counter++;
    if (request.op === "toFontra") {
      say("Converting " + request.name + "…");
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
      postMessage({ id: request.id, files }, files.map((f) => f[1]));
    } else if (request.op === "toDesignspaceZip") {
      say("Writing the designspace and its UFOs…");
      const stem = request.stem;
      const pkg = work + "/" + stem + ".fontra";
      writeTree(py, pkg, request.files);
      const result = await call(
        py,
        "await hive_try_convert.toDesignspaceZip(hive_args['pkg'], hive_args['stem'])",
        { pkg, stem }
      );
      const data = py.FS.readFile(result).slice().buffer;
      cleanUp(py, work, result);
      postMessage({ id: request.id, data }, [data]);
    } else {
      throw new Error("unknown operation " + request.op);
    }
  } catch (error) {
    console.error(error);
    postMessage({ id: request.id, error: message(error) });
  }
};
