// Fontra Hive: runs first on every Fontra page served by Hive (a classic
// script at the start of <head>, before Fontra's modules).
//
// Registers the "Glyph history" editor plug-in in the browser, so nobody has
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
