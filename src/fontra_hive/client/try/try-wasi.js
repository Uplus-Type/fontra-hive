// Fontra Hive, "Try Fontra": a WASI (preview 1) host in the browser, with an
// in-memory file system. Enough of WASI for a Rust program built for
// wasm32-wasip1 (fontc) to read a font from files and write its result:
// the page puts the files in, runs the program, and reads the output back.
//
//   const fs = new MemFS();
//   fs.writeFile("/font/A.fontra/font-data.json", bytes);
//   const wasi = new WASI({ args: ["fontc", "/font/A.fontra", "-o", "/out/A.ttf"], fs });
//   const instance = await WebAssembly.instantiate(module, { wasi_snapshot_preview1: wasi.imports });
//   const code = wasi.start(instance);  // exit code; wasi.stderr: what it printed
//   fs.readFile("/out/A.ttf");
//
// One directory is opened for the program: "/" (fd 3). No sockets, no
// threads; what is not implemented answers ENOSYS.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const E = {
  SUCCESS: 0,
  BADF: 8,
  EXIST: 20,
  INVAL: 28,
  ISDIR: 31,
  NOENT: 44,
  NOSYS: 52,
  NOTDIR: 54,
  NOTEMPTY: 55,
  SPIPE: 70,
};
const FILETYPE = { CHAR: 2, DIR: 3, FILE: 4 };
const OFLAGS = { CREAT: 1, DIRECTORY: 2, EXCL: 4, TRUNC: 8 };
const FDFLAGS = { APPEND: 1 };
const RIGHTS_ALL = 0x1fffffffn;

class WasiExit extends Error {
  constructor(code) {
    super("exit " + code);
    this.code = code;
  }
}

// ---- the file system ----

let nextIno = 1n;

function dirNode() {
  return { kind: "dir", entries: new Map(), ino: nextIno++ };
}

function fileNode(data) {
  return { kind: "file", data: data || new Uint8Array(0), size: data ? data.length : 0, ino: nextIno++ };
}

export class MemFS {
  constructor() {
    this.root = dirNode();
  }

  // The node at an absolute path ("/a/b"), or null.
  lookup(path) {
    return this.resolve(this.root, path).node;
  }

  // Walk `path` from the directory `start`: {node, parent, name, errno}.
  resolve(start, path) {
    const parts = path.split("/").filter((p) => p && p !== ".");
    const stack = [start];
    if (path.startsWith("/")) stack.splice(0, stack.length, this.root);
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      const dir = stack[stack.length - 1];
      if (part === "..") {
        if (stack.length > 1) stack.pop();
        continue;
      }
      if (dir.kind !== "dir") return { errno: E.NOTDIR };
      const child = dir.entries.get(part);
      if (i === parts.length - 1) return { node: child || null, parent: dir, name: part };
      if (!child) return { errno: E.NOENT };
      stack.push(child);
    }
    const node = stack[stack.length - 1];
    return { node, parent: stack.length > 1 ? stack[stack.length - 2] : null, name: parts[parts.length - 1] || "" };
  }

  mkdirp(path) {
    let dir = this.root;
    for (const part of path.split("/").filter(Boolean)) {
      let child = dir.entries.get(part);
      if (!child) {
        child = dirNode();
        dir.entries.set(part, child);
      }
      if (child.kind !== "dir") throw new Error("not a directory: " + path);
      dir = child;
    }
    return dir;
  }

  writeFile(path, data) {
    const cut = path.lastIndexOf("/");
    const dir = this.mkdirp(path.slice(0, cut));
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : new Uint8Array(data);
    dir.entries.set(path.slice(cut + 1), fileNode(bytes));
  }

  readFile(path) {
    const node = this.lookup(path);
    if (!node || node.kind !== "file") return null;
    return node.data.slice(0, node.size);
  }
}

// ---- the host ----

export class WASI {
  constructor({ args = [], env = {}, fs = new MemFS() } = {}) {
    this.args = args;
    this.env = Object.entries(env).map(([k, v]) => `${k}=${v}`);
    this.fs = fs;
    this.stdout = "";
    this.stderr = "";
    this.onOutput = null; // (stream, text) as the program writes
    this.fds = new Map([
      [0, { kind: "stdin" }],
      [1, { kind: "stdout" }],
      [2, { kind: "stderr" }],
      [3, { kind: "dir", node: fs.root, preopen: "/", pos: 0n }],
    ]);
    this.nextFd = 4;
    this.memory = null;
    this.imports = new Proxy(this.makeImports(), {
      get: (target, name) => target[name] || (() => E.NOSYS),
    });
  }

  // Run the program's _start; its exit code.
  start(instance) {
    this.memory = instance.exports.memory;
    try {
      instance.exports._start();
      return 0;
    } catch (error) {
      if (error instanceof WasiExit) return error.code;
      throw error;
    }
  }

  view() {
    return new DataView(this.memory.buffer);
  }

  bytes() {
    return new Uint8Array(this.memory.buffer);
  }

  string(ptr, len) {
    return new TextDecoder().decode(this.bytes().subarray(ptr, ptr + len));
  }

  // Strings as C strings: pointers into ptrsPtr, bytes from bufPtr.
  writeStrings(list, ptrsPtr, bufPtr) {
    const view = this.view();
    const mem = this.bytes();
    let at = bufPtr;
    list.forEach((text, i) => {
      const encoded = new TextEncoder().encode(text + "\0");
      view.setUint32(ptrsPtr + i * 4, at, true);
      mem.set(encoded, at);
      at += encoded.length;
    });
    return E.SUCCESS;
  }

  sizes(list, countPtr, sizePtr) {
    const view = this.view();
    view.setUint32(countPtr, list.length, true);
    view.setUint32(sizePtr, list.reduce((n, s) => n + new TextEncoder().encode(s).length + 1, 0), true);
    return E.SUCCESS;
  }

  output(stream, data) {
    const text = new TextDecoder().decode(data);
    this[stream] += text;
    if (this.onOutput) this.onOutput(stream, text);
  }

  filestat(ptr, node) {
    const view = this.view();
    for (let i = 0; i < 64; i += 8) view.setBigUint64(ptr + i, 0n, true);
    view.setBigUint64(ptr + 8, node.ino || 0n, true);
    view.setUint8(ptr + 16, node.kind === "dir" ? FILETYPE.DIR : node.kind === "file" ? FILETYPE.FILE : FILETYPE.CHAR);
    view.setBigUint64(ptr + 24, 1n, true);
    view.setBigUint64(ptr + 32, BigInt(node.kind === "file" ? node.size : 0), true);
    return E.SUCCESS;
  }

  // The node a path names, from a directory fd.
  at(dirfd, pathPtr, pathLen) {
    const dir = this.fds.get(dirfd);
    if (!dir || dir.kind !== "dir") return { errno: E.BADF };
    return this.fs.resolve(dir.node, this.string(pathPtr, pathLen));
  }

  // File contents grow to hold `end` bytes.
  reserve(node, end) {
    if (end <= node.data.length) return;
    const grown = new Uint8Array(Math.max(end, node.data.length * 2, 1024));
    grown.set(node.data.subarray(0, node.size));
    node.data = grown;
  }

  makeImports() {
    const self = this;
    return {
      args_get: (argv, buf) => self.writeStrings(self.args, argv, buf),
      args_sizes_get: (count, size) => self.sizes(self.args, count, size),
      environ_get: (envp, buf) => self.writeStrings(self.env, envp, buf),
      environ_sizes_get: (count, size) => self.sizes(self.env, count, size),

      clock_res_get: (id, ptr) => {
        self.view().setBigUint64(ptr, 1000n, true);
        return E.SUCCESS;
      },
      clock_time_get: (id, precision, ptr) => {
        const ms = id === 0 ? Date.now() : performance.now();
        self.view().setBigUint64(ptr, BigInt(Math.round(ms * 1e6)), true);
        return E.SUCCESS;
      },
      random_get: (ptr, len) => {
        const mem = self.bytes();
        for (let at = 0; at < len; at += 65536) {
          crypto.getRandomValues(mem.subarray(ptr + at, ptr + Math.min(len, at + 65536)));
        }
        return E.SUCCESS;
      },
      sched_yield: () => E.SUCCESS,
      proc_exit: (code) => {
        throw new WasiExit(code);
      },
      proc_raise: () => E.NOSYS,
      poll_oneoff: (inPtr, outPtr, count, neventsPtr) => {
        // Only clock subscriptions (sleep): answered at once.
        const view = self.view();
        for (let i = 0; i < count; i++) {
          const userdata = view.getBigUint64(inPtr + i * 48, true);
          view.setBigUint64(outPtr + i * 32, userdata, true);
          view.setUint16(outPtr + i * 32 + 8, 0, true);
          view.setUint8(outPtr + i * 32 + 10, view.getUint8(inPtr + i * 48 + 8));
        }
        view.setUint32(neventsPtr, count, true);
        return E.SUCCESS;
      },

      fd_prestat_get: (fd, ptr) => {
        const entry = self.fds.get(fd);
        if (!entry || !entry.preopen) return E.BADF;
        const view = self.view();
        view.setUint8(ptr, 0);
        view.setUint32(ptr + 4, new TextEncoder().encode(entry.preopen).length, true);
        return E.SUCCESS;
      },
      fd_prestat_dir_name: (fd, ptr, len) => {
        const entry = self.fds.get(fd);
        if (!entry || !entry.preopen) return E.BADF;
        self.bytes().set(new TextEncoder().encode(entry.preopen).subarray(0, len), ptr);
        return E.SUCCESS;
      },

      fd_fdstat_get: (fd, ptr) => {
        const entry = self.fds.get(fd);
        if (!entry) return E.BADF;
        const view = self.view();
        const type = entry.kind === "dir" ? FILETYPE.DIR : entry.kind === "file" ? FILETYPE.FILE : FILETYPE.CHAR;
        view.setUint8(ptr, type);
        view.setUint16(ptr + 2, entry.append ? FDFLAGS.APPEND : 0, true);
        view.setBigUint64(ptr + 8, RIGHTS_ALL, true);
        view.setBigUint64(ptr + 16, RIGHTS_ALL, true);
        return E.SUCCESS;
      },
      fd_fdstat_set_flags: () => E.SUCCESS,
      fd_fdstat_set_rights: () => E.SUCCESS,
      fd_filestat_get: (fd, ptr) => {
        const entry = self.fds.get(fd);
        if (!entry) return E.BADF;
        return self.filestat(ptr, entry.node || { kind: "char" });
      },
      fd_filestat_set_size: (fd, size) => {
        const entry = self.fds.get(fd);
        if (!entry || entry.kind !== "file") return E.BADF;
        const n = Number(size);
        self.reserve(entry.node, n);
        if (n > entry.node.size) entry.node.data.fill(0, entry.node.size, n);
        entry.node.size = n;
        return E.SUCCESS;
      },
      fd_filestat_set_times: () => E.SUCCESS,
      fd_advise: () => E.SUCCESS,
      fd_allocate: () => E.SUCCESS,
      fd_datasync: () => E.SUCCESS,
      fd_sync: () => E.SUCCESS,
      fd_close: (fd) => {
        if (!self.fds.has(fd)) return E.BADF;
        self.fds.delete(fd);
        return E.SUCCESS;
      },
      fd_renumber: (from, to) => {
        if (!self.fds.has(from)) return E.BADF;
        self.fds.set(to, self.fds.get(from));
        self.fds.delete(from);
        return E.SUCCESS;
      },

      fd_write: (fd, iovs, iovsLen, nwrittenPtr) => self.rw(fd, iovs, iovsLen, nwrittenPtr, true, null),
      fd_pwrite: (fd, iovs, iovsLen, offset, nwrittenPtr) => self.rw(fd, iovs, iovsLen, nwrittenPtr, true, offset),
      fd_read: (fd, iovs, iovsLen, nreadPtr) => self.rw(fd, iovs, iovsLen, nreadPtr, false, null),
      fd_pread: (fd, iovs, iovsLen, offset, nreadPtr) => self.rw(fd, iovs, iovsLen, nreadPtr, false, offset),

      fd_seek: (fd, offset, whence, ptr) => {
        const entry = self.fds.get(fd);
        if (!entry) return E.BADF;
        if (entry.kind !== "file") return E.SPIPE;
        const base = whence === 0 ? 0n : whence === 1 ? entry.pos : BigInt(entry.node.size);
        const pos = base + BigInt(offset);
        if (pos < 0n) return E.INVAL;
        entry.pos = pos;
        self.view().setBigUint64(ptr, pos, true);
        return E.SUCCESS;
      },
      fd_tell: (fd, ptr) => {
        const entry = self.fds.get(fd);
        if (!entry || entry.kind !== "file") return E.BADF;
        self.view().setBigUint64(ptr, entry.pos, true);
        return E.SUCCESS;
      },

      fd_readdir: (fd, buf, bufLen, cookie, usedPtr) => {
        const entry = self.fds.get(fd);
        if (!entry || entry.kind !== "dir") return E.BADF;
        const names = [".", "..", ...entry.node.entries.keys()];
        const view = self.view();
        const mem = self.bytes();
        let used = 0;
        for (let i = Number(cookie); i < names.length && used < bufLen; i++) {
          const name = new TextEncoder().encode(names[i]);
          const child = i < 2 ? entry.node : entry.node.entries.get(names[i]);
          const header = new Uint8Array(24);
          const hv = new DataView(header.buffer);
          hv.setBigUint64(0, BigInt(i + 1), true);
          hv.setBigUint64(8, child.ino || 0n, true);
          hv.setUint32(16, name.length, true);
          hv.setUint8(20, child.kind === "dir" ? FILETYPE.DIR : FILETYPE.FILE);
          const record = new Uint8Array(24 + name.length);
          record.set(header);
          record.set(name, 24);
          const take = Math.min(record.length, bufLen - used);
          mem.set(record.subarray(0, take), buf + used);
          used += take;
        }
        view.setUint32(usedPtr, used, true);
        return E.SUCCESS;
      },

      path_open: (dirfd, dirflags, pathPtr, pathLen, oflags, rightsBase, rightsInh, fdflags, fdPtr) => {
        const found = self.at(dirfd, pathPtr, pathLen);
        if (found.errno) return found.errno;
        let node = found.node;
        if (node && oflags & OFLAGS.EXCL && oflags & OFLAGS.CREAT) return E.EXIST;
        if (!node) {
          if (!(oflags & OFLAGS.CREAT)) return E.NOENT;
          if (!found.parent || found.parent.kind !== "dir") return E.NOENT;
          node = fileNode();
          found.parent.entries.set(found.name, node);
        }
        if (oflags & OFLAGS.DIRECTORY && node.kind !== "dir") return E.NOTDIR;
        if (node.kind === "file" && oflags & OFLAGS.TRUNC) node.size = 0;
        const fd = self.nextFd++;
        self.fds.set(fd, { kind: node.kind, node, pos: 0n, append: !!(fdflags & FDFLAGS.APPEND) });
        self.view().setUint32(fdPtr, fd, true);
        return E.SUCCESS;
      },
      path_filestat_get: (dirfd, flags, pathPtr, pathLen, ptr) => {
        const found = self.at(dirfd, pathPtr, pathLen);
        if (found.errno) return found.errno;
        if (!found.node) return E.NOENT;
        return self.filestat(ptr, found.node);
      },
      path_filestat_set_times: () => E.SUCCESS,
      path_create_directory: (dirfd, pathPtr, pathLen) => {
        const found = self.at(dirfd, pathPtr, pathLen);
        if (found.errno) return found.errno;
        if (found.node) return E.EXIST;
        if (!found.parent) return E.NOENT;
        found.parent.entries.set(found.name, dirNode());
        return E.SUCCESS;
      },
      path_remove_directory: (dirfd, pathPtr, pathLen) => {
        const found = self.at(dirfd, pathPtr, pathLen);
        if (found.errno) return found.errno;
        if (!found.node) return E.NOENT;
        if (found.node.kind !== "dir") return E.NOTDIR;
        if (found.node.entries.size) return E.NOTEMPTY;
        found.parent.entries.delete(found.name);
        return E.SUCCESS;
      },
      path_unlink_file: (dirfd, pathPtr, pathLen) => {
        const found = self.at(dirfd, pathPtr, pathLen);
        if (found.errno) return found.errno;
        if (!found.node) return E.NOENT;
        if (found.node.kind === "dir") return E.ISDIR;
        found.parent.entries.delete(found.name);
        return E.SUCCESS;
      },
      path_rename: (fromfd, fromPtr, fromLen, tofd, toPtr, toLen) => {
        const from = self.at(fromfd, fromPtr, fromLen);
        if (from.errno) return from.errno;
        if (!from.node) return E.NOENT;
        const to = self.at(tofd, toPtr, toLen);
        if (to.errno) return to.errno;
        if (!to.parent) return E.NOENT;
        from.parent.entries.delete(from.name);
        to.parent.entries.set(to.name, from.node);
        return E.SUCCESS;
      },
      path_readlink: () => E.INVAL,
      path_symlink: () => E.NOSYS,
      path_link: () => E.NOSYS,
    };
  }

  // fd_read / fd_write (and the p- versions, at an offset).
  rw(fd, iovs, iovsLen, resultPtr, write, offset) {
    const entry = this.fds.get(fd);
    if (!entry) return E.BADF;
    const view = this.view();
    const mem = this.bytes();
    let total = 0;
    if (entry.kind === "stdout" || entry.kind === "stderr") {
      if (!write) return E.BADF;
      for (let i = 0; i < iovsLen; i++) {
        const ptr = view.getUint32(iovs + i * 8, true);
        const len = view.getUint32(iovs + i * 8 + 4, true);
        this.output(entry.kind, mem.slice(ptr, ptr + len));
        total += len;
      }
      view.setUint32(resultPtr, total, true);
      return E.SUCCESS;
    }
    if (entry.kind === "stdin") {
      view.setUint32(resultPtr, 0, true);
      return E.SUCCESS;
    }
    if (entry.kind !== "file") return E.ISDIR;
    const node = entry.node;
    let pos = offset !== null ? Number(offset) : entry.append && write ? node.size : Number(entry.pos);
    for (let i = 0; i < iovsLen; i++) {
      const ptr = view.getUint32(iovs + i * 8, true);
      const len = view.getUint32(iovs + i * 8 + 4, true);
      if (write) {
        this.reserve(node, pos + len);
        node.data.set(mem.subarray(ptr, ptr + len), pos);
        pos += len;
        if (pos > node.size) node.size = pos;
        total += len;
      } else {
        const n = Math.max(0, Math.min(len, node.size - pos));
        mem.set(node.data.subarray(pos, pos + n), ptr);
        pos += n;
        total += n;
        if (n < len) break;
      }
    }
    if (offset === null) entry.pos = BigInt(pos);
    view.setUint32(resultPtr, total, true);
    return E.SUCCESS;
  }
}
