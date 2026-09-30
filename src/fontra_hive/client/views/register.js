// Fontra Hive: runs first on every Fontra page served by Hive (a classic
// script at the start of <head>, before Fontra's modules).
//
// Registers the Hive editor plug-in (history, comments) in the browser, so nobody has
// to add /hive/plugin by hand in Application settings → Plugins, on every
// browser and every port. Fontra keeps its plug-in list in localStorage under
// "fontra.plugins" + "plugins" (ObservableController.synchronizeWithLocalStorage),
// as a JSON list of {address}. If that format ever changes, this does
// nothing and the manual registration still works.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
(function () {
  var KEY = "fontra.pluginsplugins";
  var ADDRESS = "/hive/plugin";
  try {
    var raw = window.localStorage.getItem(KEY);
    var plugins = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(plugins)) return;
    for (var i = 0; i < plugins.length; i++) {
      if (plugins[i] && plugins[i].address === ADDRESS) return;
    }
    plugins.push({ address: ADDRESS });
    window.localStorage.setItem(KEY, JSON.stringify(plugins));
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
