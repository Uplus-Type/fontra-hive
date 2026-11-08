// Fontra Hive, "Try Fontra": fontc (googlefonts/fontc) in the browser, to
// export compiled fonts without a server. A module worker of its own (a
// compile takes seconds, and must not hold up the editor's Python).
//
// fontc is built for wasm32-wasip1 (tools/build-fontc-wasm.sh, the
// revision the server runs too) and run here on an in-memory file system
// (try-wasi.js): the font goes in as files (the .fontra package, which fontc
// reads itself), fontc writes the TrueType font, which goes back.
//
// Request: {id, url, stem, main, files: [[path, ArrayBuffer]]} (main: the
// .fontra folder among them to compile; else the first .fontra or .ufo
// folder, .designspace or .glyphs)
// → {id, data: ArrayBuffer, log} or {id, error, log}; {id, progress}.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { MemFS, WASI } from "./try-wasi.js";

const modules = new Map(); // url → Promise<WebAssembly.Module>

function fontcModule(url) {
  if (!modules.has(url)) {
    const loading = (async () => {
      const response = await fetch(url);
      if (response.status === 404) throw new Error("Compiled fonts cannot be made on this server.");
      if (!response.ok) throw new Error(`fontc: HTTP ${response.status}`);
      if (WebAssembly.compileStreaming && (response.headers.get("Content-Type") || "").includes("wasm")) {
        return WebAssembly.compileStreaming(response);
      }
      return WebAssembly.compile(await response.arrayBuffer());
    })();
    loading.catch(() => modules.delete(url));
    modules.set(url, loading);
  }
  return modules.get(url);
}

// fontc's messages, without the colour codes of a terminal.
function plain(text) {
  return text.replace(/\x1b\[[0-9;]*m/g, "").trim();
}

// What fontc printed. Its errors and warnings go to stdout (tracing's fmt layer
// writes there), a panic to stderr: read both, stdout first.
function said(wasi) {
  return plain([wasi.stdout, wasi.stderr].filter(Boolean).join("\n"));
}

async function compile(request, say) {
  say("Loading fontc (the first time takes a moment)…");
  const module = await fontcModule(request.url);
  say("Compiling " + request.stem + "…");
  const fs = new MemFS();
  for (const [path, data] of request.files) fs.writeFile(`/font/${path}`, data);
  const main =
    request.main ||
    request.files.map((f) => f[0].split("/")[0]).find((p) => /\.(fontra|ufo)$/i.test(p)) ||
    request.files.map((f) => f[0]).find((p) => /^[^/]+\.(designspace|glyphs)$/i.test(p));
  if (!main) throw new Error("No .fontra package, UFO, designspace or Glyphs file to compile.");
  const source = `/font/${main}`;
  fs.mkdirp("/out");
  fs.mkdirp("/build");
  const output = `/out/${request.stem}.ttf`;
  const wasi = new WASI({
    args: ["fontc", source, "--output-file", output, "--build-dir", "/build"],
    env: { RUST_BACKTRACE: "1", NO_COLOR: "1" },
    fs,
  });
  const instance = await WebAssembly.instantiate(module, { wasi_snapshot_preview1: wasi.imports });
  let code;
  try {
    code = wasi.start(instance);
  } catch (error) {
    // A panic (unreachable) or a missing host function.
    throw Object.assign(new Error("fontc stopped: " + (said(wasi) || error.message)), { log: said(wasi) });
  }
  const data = fs.readFile(output);
  if (code !== 0 || !data) {
    const log = said(wasi);
    const last = log.split("\n").filter(Boolean).pop() || `exit code ${code}`;
    throw Object.assign(new Error(last), { log });
  }
  return { data: data.buffer, log: said(wasi) };
}

onmessage = async (event) => {
  const request = event.data;
  const say = (progress) => postMessage({ id: request.id, progress });
  try {
    const reply = await compile(request, say);
    postMessage({ id: request.id, ...reply }, [reply.data]);
  } catch (error) {
    postMessage({ id: request.id, error: error.message || String(error), log: error.log || "" });
  }
};
