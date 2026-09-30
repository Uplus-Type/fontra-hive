// Fontra Hive, "Try Fontra": fontc (googlefonts/fontc) in the browser, to
// export compiled fonts without a server. A module worker of its own (a
// compile takes seconds, and must not hold up the editor's Python).
//
// fontc is built for wasm32-wasip1 (tools/build-fontc-wasm.sh) and run
// here on an in-memory file system (try-wasi.js): the font's .fontra
// package goes in as files, fontc writes the TrueType font, which goes back.
//
// Request: {id, url, stem, files: [[path in the package, ArrayBuffer]]}
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

async function compile(request, say) {
  say("Loading fontc (the first time takes a moment)…");
  const module = await fontcModule(request.url);
  say("Compiling " + request.stem + "…");
  const fs = new MemFS();
  const source = `/font/${request.stem}.fontra`;
  for (const [path, data] of request.files) fs.writeFile(`${source}/${path}`, data);
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
    throw Object.assign(new Error("fontc stopped: " + (plain(wasi.stderr) || error.message)), { log: wasi.stderr });
  }
  const data = fs.readFile(output);
  if (code !== 0 || !data) {
    const log = plain(wasi.stderr);
    const last = log.split("\n").filter(Boolean).pop() || `exit code ${code}`;
    throw Object.assign(new Error(last), { log });
  }
  return { data: data.buffer, log: plain(wasi.stderr) };
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
