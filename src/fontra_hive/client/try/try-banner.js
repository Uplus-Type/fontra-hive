// Fontra Hive: the "Try Fontra" notice, at the bottom of the demo editor.
// Says that the font lives in the browser and is not saved, and links to
// Hive. Loaded at the end of <body> (see try-engine.js for the engine).
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

const STYLE = `
.hive-try {
  position: fixed; left: 50%; bottom: 14px; transform: translateX(-50%);
  z-index: 1000; display: flex; align-items: center; gap: 10px; flex-wrap: nowrap; white-space: nowrap;
  justify-content: center; max-width: calc(100vw - 32px); box-sizing: border-box;
  padding: 7px 8px 7px 14px; border-radius: 999px;
  background: #222; color: #f4f1ea; box-shadow: 0 4px 18px rgba(0,0,0,.25);
  font: 13px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
.hive-try img { width: 20px; height: 20px; }
.hive-try b { font-weight: 600; }
.hive-try .muted { color: #b9b3a8; }
.hive-try a, .hive-try button {
  font: inherit; color: #222; background: #f3c744; border: 0; border-radius: 999px;
  padding: 5px 11px; cursor: pointer; text-decoration: none; white-space: nowrap;
}
.hive-try button.plain { background: transparent; color: #f4f1ea; border: 1px solid #555; }
.hive-try .close { background: transparent; color: #b9b3a8; padding: 5px 7px; }
.hive-try.small .muted, .hive-try.small .extra { display: none; }
@media (max-width: 700px) { .hive-try .muted, .hive-try .extra { display: none; } }
`;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "onclick") node.addEventListener("click", value);
    else node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

function start() {
  document.head.append(el("style", {}, STYLE));
  const edited = el("span", { class: "muted" });
  const bar = el(
    "div",
    { class: "hive-try", role: "note" },
    el("img", { src: "/hive/icons/hive-icon.svg", alt: "" }),
    el("span", {}, el("b", {}, "Try Fontra"), " in your browser · nothing is saved"),
    edited,
    el(
      "button",
      {
        class: "plain extra",
        type: "button",
        title: "Reload the demo font",
        onclick: () => {
          if (!window.hiveTry?.TryFont.edited || confirm("Start over? Your edits will be lost.")) {
            window.hiveTry && (window.hiveTry.TryFont.edited = false);
            location.reload();
          }
        },
      },
      "Start over"
    ),
    el("a", { href: "/", class: "extra" }, "Fontra Hive for teams"),
    el(
      "button",
      {
        class: "close",
        type: "button",
        title: "Hide",
        "aria-label": "Hide",
        onclick: () => bar.classList.toggle("small"),
      },
      "–"
    )
  );
  window.addEventListener("hive-try-edit", () => {
    edited.textContent = "(a reload starts over)";
  });
  document.body.append(bar);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
