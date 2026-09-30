// Fontra Hive, "Try Fontra": writes to the browser's private file system
// (OPFS), one after the other. A worker because Safari only writes there
// with createSyncAccessHandle(), which exists only in workers.
//
// Messages: {id, op: "write", path: [..., name], data: Blob | string}
//           {id, op: "remove", path: [...], recursive: bool}
// Replies:  {id} or {id, error}
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"use strict";

let queue = Promise.resolve();

onmessage = (event) => {
  const message = event.data;
  queue = queue.then(async () => {
    try {
      if (message.op === "write") await write(message.path, message.data);
      else if (message.op === "remove") await remove(message.path, message.recursive);
      else throw new Error("unknown operation " + message.op);
      postMessage({ id: message.id });
    } catch (error) {
      postMessage({ id: message.id, error: String(error && error.message ? error.message : error) });
    }
  });
};

async function directory(path, create) {
  let dir = await navigator.storage.getDirectory();
  for (const name of path) dir = await dir.getDirectoryHandle(name, { create });
  return dir;
}

async function write(path, data) {
  const dir = await directory(path.slice(0, -1), true);
  const handle = await dir.getFileHandle(path.at(-1), { create: true });
  const bytes =
    typeof data === "string"
      ? new TextEncoder().encode(data)
      : new Uint8Array(await data.arrayBuffer());
  if (handle.createSyncAccessHandle) {
    const access = await handle.createSyncAccessHandle();
    try {
      access.truncate(0);
      access.write(bytes, { at: 0 });
      access.flush();
    } finally {
      access.close();
    }
  } else {
    const writable = await handle.createWritable();
    await writable.write(bytes);
    await writable.close();
  }
}

async function remove(path, recursive) {
  let dir;
  try {
    dir = await directory(path.slice(0, -1), false);
  } catch (error) {
    return; // already gone
  }
  try {
    await dir.removeEntry(path.at(-1), { recursive: !!recursive });
  } catch (error) {
    if (error.name !== "NotFoundError") throw error;
  }
}
