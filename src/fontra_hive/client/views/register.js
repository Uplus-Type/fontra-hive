// Fontra Hive: runs first on every Fontra page served by Hive (a classic
// script at the start of <head>, before Fontra's modules).
//
// Registers the editor plug-ins Hive gives everyone in the browser — the Hive
// plug-in (history, comments) and the ones served from a folder, U+Marks at
// /umarks/ — so nobody has to add them by hand in Application settings →
// Plugins, on every browser and every port. Fontra keeps its plug-in list in
// localStorage under "fontra.plugins" + "plugins"
// (ObservableController.synchronizeWithLocalStorage), as a JSON list of
// {address}. If that format ever changes, this does nothing and the manual
// registration still works.
//
// The list comes from the server, written into this file as it is served
// (projectmanager.hivePlugins): no request before Fontra starts. What is
// missing is added; what the person added themselves is never touched. A
// plug-in Hive added and no longer serves (removed on the server) is taken
// out again: Hive remembers what it added under "hive.pluginsAdded".
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
(function () {
  var KEY = "fontra.pluginsplugins";
  var ADDED_KEY = "hive.pluginsAdded";
  var PLUGINS = ["/hive/plugin"]; // written by the server (renderRegisterScript)
  function readList(key) {
    var raw = window.localStorage.getItem(key);
    var list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list : null;
  }
  try {
    var plugins = readList(KEY);
    if (!plugins) return;
    var added = (readList(ADDED_KEY) || []).filter(function (address) {
      return typeof address === "string";
    });
    var changed = false;
    // Removed on the server: out, if Hive put it there.
    plugins = plugins.filter(function (plugin) {
      var address = plugin && plugin.address;
      var gone = added.indexOf(address) !== -1 && PLUGINS.indexOf(address) === -1;
      if (gone) changed = true;
      return !gone;
    });
    added = added.filter(function (address) {
      return PLUGINS.indexOf(address) !== -1;
    });
    // Missing: in, at the end, after the person's own.
    PLUGINS.forEach(function (address) {
      for (var i = 0; i < plugins.length; i++) {
        if (plugins[i] && plugins[i].address === address) return;
      }
      plugins.push({ address: address });
      if (added.indexOf(address) === -1) added.push(address);
      changed = true;
    });
    if (changed) window.localStorage.setItem(KEY, JSON.stringify(plugins));
    window.localStorage.setItem(ADDED_KEY, JSON.stringify(added));
  } catch (error) {
    // private mode, unexpected format: leave Fontra's list alone
  }
})();

// A link to a comment (…/editor.html?project=…&hive-issue=12): keep the
// number for the plug-in (comments.js), and take it out of the address,
// which Fontra rewrites once the editor is set up.
(function () {
  try {
    var url = new URL(window.location.href);
    var number = url.searchParams.get("hive-issue");
    if (!number) return;
    window.sessionStorage.setItem(
      "hive.openIssue",
      JSON.stringify({ project: url.searchParams.get("project"), number: number })
    );
    url.searchParams.delete("hive-issue");
    window.history.replaceState(window.history.state, "", url.toString());
  } catch (error) {
    // no storage: the plug-in reads the parameter itself if it is still there
  }
})();
