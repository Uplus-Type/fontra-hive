// Fontra Hive, "Try Fontra": fonts kept in this browser.
//
// Each font is a .fontra package in the browser's private file system (OPFS):
//   fontra-hive-try/projects/<id>/project.json   {name, created, modified}
//   fontra-hive-try/projects/<id>/font/…          the package's files
// Nothing leaves the browser. Reads happen here; writes go through
// try-opfs-worker.js, in order.
//
// A classic script: sets window.HiveTryStore.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
(function () {
  "use strict";

  var ROOT = ["fontra-hive-try", "projects"];
  var WORKER_URL = "/hive/try/try-opfs-worker.js";
  var worker = null;
  var nextId = 1;
  var waiting = {};

  function available() {
    return !!(navigator.storage && navigator.storage.getDirectory);
  }

  function send(message) {
    if (!worker) {
      worker = new Worker(WORKER_URL);
      worker.onmessage = function (event) {
        var reply = event.data;
        var callbacks = waiting[reply.id];
        delete waiting[reply.id];
        if (!callbacks) return;
        if (reply.error) callbacks.reject(new Error(reply.error));
        else callbacks.resolve();
      };
    }
    return new Promise(function (resolve, reject) {
      message.id = nextId++;
      waiting[message.id] = { resolve: resolve, reject: reject };
      worker.postMessage(message);
    });
  }

  async function directory(path) {
    var dir = await navigator.storage.getDirectory();
    for (var i = 0; i < path.length; i++) dir = await dir.getDirectoryHandle(path[i]);
    return dir;
  }

  async function readText(path) {
    var dir = await directory(path.slice(0, -1));
    var handle = await dir.getFileHandle(path[path.length - 1]);
    return (await handle.getFile()).text();
  }

  function newId() {
    var bytes = crypto.getRandomValues(new Uint8Array(6));
    return Array.from(bytes, function (b) {
      return b.toString(16).padStart(2, "0");
    }).join("");
  }

  function projectPath(id) {
    if (!/^[0-9a-f]{12}$/.test(id)) throw new Error("bad project id");
    return ROOT.concat([id]);
  }

  var Store = {
    available: available,

    async list() {
      var projects = [];
      var dir;
      try {
        dir = await directory(ROOT);
      } catch (error) {
        return projects;
      }
      for await (var entry of dir.values()) {
        if (entry.kind !== "directory") continue;
        try {
          var info = JSON.parse(await readText(ROOT.concat([entry.name, "project.json"])));
          info.id = entry.name;
          projects.push(info);
        } catch (error) {
          // a project being written, or damaged: not listed
        }
      }
      projects.sort(function (a, b) {
        return (b.modified || "").localeCompare(a.modified || "");
      });
      return projects;
    },

    async info(id) {
      var info = JSON.parse(await readText(projectPath(id).concat(["project.json"])));
      info.id = id;
      return info;
    },

    // files: Map of package-relative path → string | Blob.
    async create(name, files) {
      var id = newId();
      var base = projectPath(id);
      var writes = [];
      files.forEach(function (data, rel) {
        writes.push(send({ op: "write", path: base.concat(["font"], rel.split("/")), data: data }));
      });
      await Promise.all(writes);
      var now = new Date().toISOString();
      await send({
        op: "write",
        path: base.concat(["project.json"]),
        data: JSON.stringify({ name: name, created: now, modified: now }),
      });
      return id;
    },

    async readFiles(id) {
      var files = new Map();
      var root = await directory(projectPath(id).concat(["font"]));
      async function walk(dir, prefix) {
        for await (var entry of dir.values()) {
          if (entry.kind === "directory") await walk(entry, prefix + entry.name + "/");
          else files.set(prefix + entry.name, await entry.getFile());
        }
      }
      await walk(root, "");
      return files;
    },

    write(id, rel, data) {
      return send({ op: "write", path: projectPath(id).concat(["font"], rel.split("/")), data: data });
    },

    remove(id, rel) {
      return send({ op: "remove", path: projectPath(id).concat(["font"], rel.split("/")) });
    },

    async touch(id, changes) {
      var info = await Store.info(id);
      delete info.id;
      Object.assign(info, changes || {}, { modified: new Date().toISOString() });
      await send({ op: "write", path: projectPath(id).concat(["project.json"]), data: JSON.stringify(info) });
    },

    removeProject(id) {
      return send({ op: "remove", path: projectPath(id), recursive: true });
    },

    // Ask the browser not to clear this storage on its own.
    async persist() {
      try {
        if (navigator.storage.persisted && (await navigator.storage.persisted())) return true;
        return navigator.storage.persist ? await navigator.storage.persist() : false;
      } catch (error) {
        return false;
      }
    },
  };

  window.HiveTryStore = Store;
})();
