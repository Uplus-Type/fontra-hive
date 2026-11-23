"""Hive's script in Fontra's views, beyond the glyph: the net under an edit
the server refuses, the project's glyph sets greyed without "plugindata",
and the History section of Font info. In a headless browser, against fake
Fontra objects and a mocked server. Skipped without Playwright."""

import functools
import http.server
import os
import pathlib
import threading

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

CLIENT_DIR = pathlib.Path(__file__).parent.parent / "src" / "fontra_hive" / "client"
CHROMIUM = "/opt/pw-browsers/chromium"

PAGE = """<!doctype html><html><head>
<script>window.__hiveViewsNoAutoStart = true;</script>
</head><body>
<div class="top-bar-container"></div>
<div class="multi-panel-main-container">
  <div id="multi-panel-header-container"><ui-accordion></ui-accordion></div>
  <div id="multi-panel-panel-container"></div>
</div>
</body></html>"""


@pytest.fixture(scope="module")
def browser_and_url():
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=os.fspath(CLIENT_DIR)
    )
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with playwright_sync.sync_playwright() as p:
            options = {"executable_path": CHROMIUM} if os.path.exists(CHROMIUM) else {}
            try:
                browser = p.chromium.launch(**options)
            except Exception as error:
                pytest.skip(f"no Chromium for Playwright: {error}")
            yield browser, f"http://127.0.0.1:{server.server_address[1]}"
            browser.close()
    finally:
        server.shutdown()


@pytest.fixture
def page(browser_and_url):
    browser, base = browser_and_url
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route(
        "**/fontinfo.html*",
        lambda route: route.fulfill(status=200, content_type="text/html", body=PAGE),
    )
    page.goto(f"{base}/fontinfo.html?project=Mutator")
    page.evaluate("async () => { window.hive = await import('/views/hive-views.js'); }")
    page.errors = errors
    yield page
    page.close()


# A Fontra-like font controller whose server refuses what goes beyond the
# glyph, the way Fontra's remote call does (RemoteError with the repr of the
# server's exception), and a view controller with externalChange/reloadData.
FONT = """() => {
  class RemoteError extends Error {}
  window.events = [];
  const state = { axes: ["wght"], glyphSets: [] };
  window.fontState = state;
  window.fontController = {
    async editFinal(change, rollback, label) {
      events.push(["editFinal", label]);
      if (label === "allowed") return undefined;
      throw new RemoteError(
        "ReadOnlyError('Font settings are kept to some people on this project. Ask a manager.')"
      );
    },
    editIncremental(change) {
      Promise.reject(new RemoteError(
        "ReadOnlyError('Kerning is kept to some people on this project. Ask a manager.')"));
    },
  };
  window.viewController = {
    fontController,
    async externalChange(change) {
      events.push(["externalChange", change.name]);
      state.axes = change.axes;
    },
    async reloadData(pattern) {
      events.push(["reloadData", pattern]);
    },
  };
}"""


def test_a_refused_edit_is_said_and_taken_back(page):
    page.evaluate(FONT)
    assert page.evaluate("hive.installEditGuard(viewController)") is True
    assert page.evaluate("hive.installEditGuard(viewController)") is False  # once
    # Fontra applied the edit on the page, then the server refuses it.
    result = page.evaluate("""async () => {
          fontState.axes = ["wght", "opsz"];
          const error = await fontController.editFinal(
            { name: "add opsz" }, { name: "take back", axes: ["wght"] }, "add axis", true);
          return { returned: String(error), axesRightAfter: [...fontState.axes] };
        }""")
    # editFinal returns the error (Fontra's callers: "const error = await
    # editFinal(…)") and the caller finishes before the change is taken back.
    assert "ReadOnlyError" in result["returned"]
    assert result["axesRightAfter"] == ["wght", "opsz"]
    page.wait_for_function("fontState.axes.length === 1")
    assert page.evaluate("events") == [
        ["editFinal", "add axis"],
        ["externalChange", "take back"],
    ]
    # One sentence: the server's.
    assert page.evaluate("document.querySelector('.hive-refusal .message').textContent") == (
        "Not saved: Font settings are kept to some people on this project. Ask a manager."
    )
    assert page.evaluate("document.querySelectorAll('.hive-refusal').length") == 1
    assert page.errors == []


def test_an_allowed_edit_is_untouched_and_other_errors_still_throw(page):
    page.evaluate(FONT)
    page.evaluate("""() => {
          const editFinal = fontController.editFinal;
          fontController.editFinal = async (c, r, label) => {
            if (label === "broken") throw new Error("network down");
            return editFinal(c, r, label);
          };
          hive.installEditGuard(viewController);
        }""")
    assert page.evaluate("fontController.editFinal({}, {}, 'allowed')") is None
    thrown = page.evaluate("""async () => {
          try { await fontController.editFinal({}, {}, 'broken'); return null; }
          catch (error) { return error.message; }
        }""")
    assert thrown == "network down"
    assert page.evaluate("document.querySelector('.hive-refusal')") is None


def test_a_refused_drag_is_said_once_then_reloaded(page):
    page.evaluate(FONT)
    page.evaluate("hive.installEditGuard(viewController, { incrementalGraceMs: 100 })")
    page.evaluate("fontController.editIncremental({}); fontController.editIncremental({});")
    page.wait_for_function("events.some((e) => e[0] === 'reloadData')")
    assert page.evaluate("events") == [["reloadData", None]]
    assert page.evaluate("document.querySelectorAll('.hive-refusal').length") == 1
    assert page.evaluate("document.querySelector('.hive-refusal .message').textContent") == (
        "Not saved: Kerning is kept to some people on this project. Ask a manager."
    )
    assert page.errors == []


def test_the_servers_sentence_in_a_refusal(page):
    reason = page.evaluate(
        """[
          hive.readOnlyReason(new Error("ReadOnlyError('It\\\\'s kept. Ask a manager.')")),
          hive.readOnlyReason(new Error('ReadOnlyError("read-only (observer)")')),
          hive.readOnlyReason(new Error("KeyError('x')")),
          hive.refusalText("read-only (observer)"),
        ]"""
    )
    assert reason == [
        "It's kept. Ask a manager.",
        "read-only (observer)",
        None,
        "Not saved: your role cannot change this font.",
    ]


def test_the_project_glyph_sets_are_greyed_without_plugindata(page):
    page.evaluate("""() => {
          const accordion = document.createElement("div");
          accordion.id = "panel-navigation-accordion";
          const root = accordion.attachShadow({ mode: "open" });
          const plus = document.createElement("icon-button");
          accordion.items = [{ id: "project-glyph-sets", auxiliaryHeaderElement: plus }];
          root.innerHTML = `
            <div class="ui-accordion-item" id="project-glyph-sets">
              <div class="ui-accordion-item-header"></div>
              <div class="glyphset-button-group"><icon-button class="pencil"></icon-button></div>
            </div>
            <div class="ui-accordion-item" id="my-glyph-sets">
              <div class="ui-accordion-item-header"><icon-button class="mine"></icon-button></div>
            </div>`;
          root.querySelector("#project-glyph-sets .ui-accordion-item-header").append(plus);
          window.clicks = [];
          for (const b of root.querySelectorAll("icon-button")) {
            b.addEventListener("click", () => clicks.push(b.className || "plus"));
          }
          document.body.append(accordion);
        }""")
    assert page.evaluate("hive.closeProjectGlyphSets({ timeout: 500 })") == 1
    root = "document.getElementById('panel-navigation-accordion').shadowRoot"
    page.evaluate(f"{root}.querySelector('.pencil').click()")
    page.evaluate(f"{root}.querySelector('#project-glyph-sets icon-button').click()")
    page.evaluate(f"{root}.querySelector('.mine').click()")
    assert page.evaluate("clicks") == ["mine"]  # my own glyph sets stay open
    assert page.evaluate(
        f"getComputedStyle({root}.querySelector('.pencil')).opacity"
    ) == "0.35"
    assert page.evaluate(f"{root}.querySelector('#project-glyph-sets icon-button').title") == (
        "Plug-in data · ask a manager: on this project, the project's glyph sets are kept"
        " to some people"
    )
    assert page.evaluate("document.querySelector('.hive-refusal .message').textContent") == (
        "Plug-in data: on this project, the project's glyph sets are kept to some people."
        " Ask a manager."
    )


# Font info: the History section follows the panel open.
HISTORY = """() => {
  window.requests = [];
  const sha = (c) => c.repeat(40);
  window.sha = sha;
  window.head = sha("3");
  const font = (area, summary) => ({ area, domain: "fontsettings", summary });
  const commits = [
    { sha: sha("3"), author: "Fabio Rossi", time: 1790000000, parents: [sha("2")],
      message: "Edit axes, sources", font: [font("axes", "Axes: wght 100–900 → 200–900"),
                                             font("sources", "Sources: Black added")] },
    { sha: sha("2"), author: "Ana López", time: 1789990000, parents: [sha("1")],
      message: "Edit axes", font: [font("axes", "Axes: opsz added"),
                                   font("axes", "Axes: wdth hidden"),
                                   font("axes", "Axes: wght default 400 → 350")] },
  ];
  window.fetch = async (url) => {
    const u = new URL(url, location.href);
    const route = u.pathname.split("/").pop();
    const q = Object.fromEntries(u.searchParams);
    requests.push({ route, ...q });
    const json = (data) => new Response(JSON.stringify(data));
    if (route === "head") return json({ head: window.head });
    if (route === "log") {
      const areas = q.area.split(",");
      return json({ head: window.head, snapshots: [],
        commits: commits.filter((c) => c.font.some((f) => areas.includes(f.area))) });
    }
    if (route === "font-diff") return json({ changes: [
      { area: "sources", domain: "fontsettings", summary: "Sources: Black added", fields: [] },
      { area: "axes", domain: "fontsettings", summary: "Axes: wght 100–900 → 200–900",
        fields: [] },
    ], glyphs: [{ name: "A", kind: "changed" }] });
    return new Response("", { status: 404 });
  };
  const panels = {
    selectedPanelIdentifier: "axes-panel",
    selectPanel(id) { this.selectedPanelIdentifier = id; },
  };
  window.fontInfoController = { multiPanelController: panels };
}"""


def test_the_history_section_of_font_info(page):
    page.evaluate(HISTORY)
    page.evaluate(
        "async () => { window.section = await hive.mountFontInfoHistory("
        "{ identifier: 'Mutator', name: 'Mutator', branch: 'main' }, { timeout: 1000 }); }"
    )
    root = "section.host.shadowRoot"
    assert page.evaluate("requests.at(-1)") == {
        "route": "log", "branch": "main", "area": "axes", "limit": "30"
    }
    assert page.evaluate(f"{root}.querySelector('.scope').textContent") == (
        "Changes to the axes"
    )
    rows = (
        f"[...{root}.querySelectorAll('.change')].map("
        "r => [...r.querySelectorAll('.font-summary-line')].map(l => l.textContent))"
    )
    # Only the panel's sentences; two, the rest behind the ▸.
    assert page.evaluate(rows) == [
        ["Axes: wght 100–900 → 200–900"],
        ["Axes: opsz added", "Axes: wdth hidden"],
    ]
    assert page.evaluate(f"{root}.querySelectorAll('.font-summary-more').length") == 1
    # Fontra's panel list: another panel, another history.
    page.evaluate("fontInfoController.multiPanelController.selectPanel('sources-panel')")
    page.wait_for_function(f"{root}.querySelector('.scope').textContent.includes('sources')")
    assert page.evaluate("requests.at(-1).area") == "sources"
    assert page.evaluate(rows) == [["Sources: Black added"]]
    select = "fontInfoController.multiPanelController.selectPanel"
    page.evaluate(f"{select}('features-panel-unknown')")
    page.wait_for_function("section.host.hidden")
    page.evaluate(f"{select}('opentype-feature-code-panel')")
    page.wait_for_function(f"{root}.querySelector('.empty')")
    assert page.evaluate(f"{root}.querySelector('.empty').textContent") == (
        "No change to the features yet."
    )
    # The detail: the panel's changes first, the others after.
    page.evaluate("fontInfoController.multiPanelController.selectPanel('axes-panel')")
    page.wait_for_function(f"{root}.querySelectorAll('.change').length === 2")
    page.evaluate(f"{root}.querySelector('.change').click()")
    page.wait_for_selector(".hive-fontdiff h4")
    titles = ".hive-fontdiff h4, .hive-fontdiff .group-title"
    assert page.evaluate(f"[...document.querySelectorAll('{titles}')].map(e => e.textContent)") == [
        "Axes: wght 100–900 → 200–900",
        "Also in this version",
        "Sources: Black added",
        "Glyphs changed · 1",
    ]
    assert page.evaluate("requests.at(-1)") == {
        "route": "font-diff", "branch": "main", "to": "3" * 40
    }
    # The branch moved: the list follows.
    page.evaluate("window.head = sha('4'); section.poll()")
    page.wait_for_function("requests.filter(r => r.route === 'log').length >= 5")
    assert page.errors == []


def test_no_history_for_a_font_kept_in_the_browser(page):
    page.evaluate(HISTORY)
    local = "local:0123456789ab"
    assert page.evaluate(
        f"hive.mountFontInfoHistory({{ identifier: '{local}', name: '{local}', branch: 'main' }})"
    ) is None
