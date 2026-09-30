"""The editor plug-in (``client/plugin/init.js``) in a headless browser.

The panel runs against a fake ``editor`` object and a mocked ``fetch``, so no
Fontra client is needed. Skipped when Playwright or a Chromium build is not
available (``pip install playwright && playwright install chromium``).
"""

import functools
import http.server
import json
import os
import pathlib
import threading

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

PLUGIN_DIR = (
    pathlib.Path(__file__).parent.parent / "src" / "fontra_hive" / "client" / "plugin"
)
CHROMIUM = "/opt/pw-browsers/chromium"  # the cloud sandbox's build, if present


@pytest.fixture(scope="module")
def page():
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=os.fspath(PLUGIN_DIR)
    )
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with playwright_sync.sync_playwright() as p:
            options = {"executable_path": CHROMIUM} if os.path.exists(CHROMIUM) else {}
            try:
                browser = p.chromium.launch(**options)
            except Exception as error:  # no browser installed
                pytest.skip(f"no Chromium for Playwright: {error}")
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(error))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/plugin.json")
            page.errors = errors
            yield page
            browser.close()
    finally:
        server.shutdown()


# Fake editor + fake server. Three commits touch glyph A: c3 (the branch
# head) and c2 since A's last snapshot, c1 grouped under A's snapshot "V1"
# (s5), with the font's snapshot "F1" (s4) as a landmark before it. In the
# font's history (no glyph), "V1" (s5) is a snapshot of the whole font.
SETUP = """
async () => {
  const sha = (c) => c.repeat(40);
  const glyph = (size) => ({
    name: "A",
    sources: [{ name: "default", layerName: "default", location: {} }],
    layers: { default: { glyph: { path: { contours: [
      { points: [{ x: 0, y: 0 }, { x: size, y: 0 }, { x: size, y: size }], isClosed: true },
    ] } } } },
  });
  const state = {
    requests: [], posts: [], updates: 0, windowKeys: 0, listeners: [],
    glyphs: { [sha("1")]: glyph(100), [sha("2")]: glyph(200), [sha("3")]: glyph(300),
              [sha("5")]: glyph(100), [sha("4")]: glyph(150) },
  };
  window.state = state;
  window.sha = sha;
  window.addEventListener("keydown", () => state.windowKeys++);
  const json = (data) => new Response(JSON.stringify(data), {
    headers: { "Content-Type": "application/json" } });
  const commit = (c, message, snapshot) => ({
    sha: sha(c), author: "J", email: "j@x", time: 1790000000, message, parents: [],
    snapshot });
  const snapshot = { name: "v1", title: "V1", sha: sha("5"), author: "J",
    time: 1790000000, base: null, changes: 4, glyphs: ["A", "B"] };
  window.fetch = async (url, options = {}) => {
    url = new URL(url);
    const route = url.pathname.split("/").pop();
    const q = Object.fromEntries(url.searchParams);
    state.requests.push({ route, ...q });
    if (options.method === "POST") {
      state.posts.push({ route, ...q });
      return json({ head: sha("6"), snapshot: { ...snapshot, name: "x", sha: sha("6") } });
    }
    if (route === "head") return json({ head: sha("3") });
    if (route === "log") return json(q.glyph ? {
      head: sha("3"),
      commits: [{ ...commit("3", "Edit A", null), sources: ["Bold"] },
                { ...commit("2", "Edit A", null), sources: ["Light", "Bold"] },
                { ...commit("1", "Import", "f1"), glyph_snapshot: "v1",
                  sources: ["Wide"] }],
      snapshots: [{ ...snapshot, name: "f1", title: "F1", sha: sha("4") }],
      glyphSnapshots: [{ name: "v1", title: "V1", sha: sha("5"), author: "J",
                         time: 1790000000, glyphs: ["A"] }],
      order: [["commit", sha("3")], ["commit", sha("2")], ["glyph-snapshot", "v1"],
              ["snapshot", "f1"], ["commit", sha("1")]],
    } : {
      head: sha("3"),
      commits: [{ ...commit("3", "Edit A", null), sources: ["Bold"] },
                commit("4", "Edit B", null),
                commit("2", "Edit A", null), commit("5", "Snapshot V1", "v1"),
                commit("1", "Import", "v1")],
      snapshots: [snapshot],
    });
    if (route === "snapshots") return json({
      head: sha("3"), snapshots: [snapshot], pending: 2, pendingAuthors: ["J", "K"] });
    if (route === "glyph") {
      const data = state.glyphs[q.ref];
      return data && q.glyph === "A" ? json(data) : new Response("", { status: 404 });
    }
    return new Response("", { status: 404 });
  };
  const sceneSettings = { selectedGlyphName: "A", editingLayers: {}, backgroundLayers: {} };
  const editor = {
    projectIdentifier: "Mutator",
    sceneController: {
      sceneSettings,
      sceneSettingsController: { addKeyListener: (keys, f) => state.listeners.push(f) },
    },
    visualizationLayers: { definitions: [], toggle: () => {} },
    canvasController: { requestUpdate: () => state.updates++ },
    addSidebarPanel: (panel) => {
      document.body.append(panel);
      if (panel.identifier === "hive-history") window.panel = panel;
    },
  };
  window.editor = editor;
  const plugin = await import("/init.js");
  plugin.init(editor, "/hive/plugin");
  await window.panel.toggle(true);
  window.panel.stopPolling();
  window.rows = () => [...window.panel.shadowRoot.querySelectorAll(".list > *")].map(
    (e) => e.className + (e.dataset.sha ? ":" + e.dataset.sha[0] : ""));
  window.rowFor = (c) => window.panel.shadowRoot.querySelector(`[data-sha="${sha(c)}"]`);
  window.restoreButton = () => window.panel.statusElement.querySelector("button.restore");
  window.statusText = () => window.panel.shadowRoot.querySelector(".status").textContent;
  window.drawCall = (positional = false) => {
    const def = editor.visualizationLayers.definitions[0];
    const selected = def.selectionFunc({ glyphsBySelectionMode: {
      editing: [{ glyphName: sceneSettings.selectedGlyphName }] } });
    if (!selected.length) return null;
    const calls = { alpha: null, fills: 0 };
    window.strokes = 0;
    const ctx = {
      save() {}, restore() {}, stroke() { window.strokes++; },
      set globalAlpha(v) { calls.alpha = v; },
      fill(path) { calls.fills++; },
    };
    const positionedGlyph = { glyph: { layerName: "default" } };
    const parameters = { fillColor: "f", strokeColor: "s", strokeWidth: 1 };
    if (positional) {
      def.draw(ctx, positionedGlyph, parameters);  // Fontra before fontra/fontra#2785
    } else {
      def.draw({ context: ctx, positionedGlyph, parameters, model: {}, controller: {} });
    }
    return calls;
  };
  window.wait = (ms) => new Promise((r) => setTimeout(r, ms));
}
"""


def glyph_requests(page, c):
    return page.evaluate(
        f"state.requests.filter(r => r.route === 'glyph' && r.ref === sha('{c}')).length"
    )


def test_pure_helpers_render_paths(page):
    page.evaluate(SETUP)
    result = page.evaluate("""async () => {
          const m = await import("/init.js");
          const ctx = document.createElement("canvas").getContext("2d");
          const glyph = {
            sources: [{ layerName: "bold", location: { wght: 700 } },
                      { layerName: "reg", location: {} }],
            layers: { reg: { glyph: { path: { contours: [
              { points: [{ x: 10, y: 10 }, { x: 90, y: 10 }, { x: 50, y: 90 }], isClosed: true },
            ] } } }, bold: {} },
          };
          const path = m.buildLayerPath(glyph, "reg", () => null);
          return {
            picked: m.pickLayerName(glyph, "missing"),
            inside: ctx.isPointInPath(path, 50, 30),
            outside: ctx.isPointInPath(path, 5, 80),
            moved: m.transformFromDecomposed({ translateX: 5 }).e,
          };
        }""")
    assert result == {"picked": "reg", "inside": True, "outside": False, "moved": 5}


def test_rows_are_grouped_under_snapshots(page):
    page.evaluate(SETUP)
    assert page.evaluate("rows()") == [
        "summary-line",
        "group-label",
        "commit current:3",
        "commit:2",
        "snapshot:5",
    ]
    page.evaluate("panel.shadowRoot.querySelector('.snapshot .caret').click()")
    # Inside A's snapshot: the font's snapshot, as a landmark, then c1.
    assert page.evaluate("rows()")[-3:] == [
        "snapshot:5",
        "snapshot landmark nested:4",
        "commit nested:1",
    ]
    assert page.evaluate("rowFor('5').querySelector('.count').textContent") == "1"
    assert page.evaluate("rowFor('5').title").startswith("Snapshot of A “V1”")
    assert "Snapshot of the whole font “F1”" in page.evaluate("rowFor('4').title")
    # Which of the glyph's sources each version, snapshot and landmark changed.
    assert page.evaluate("rowFor('3').querySelector('.message').textContent") == (
        "Edit A · Bold"
    )
    assert "Sources changed: Light, Bold" in page.evaluate("rowFor('2').title")
    assert (
        page.evaluate("rowFor('5').querySelector('.name').textContent") == "V1 · Wide"
    )
    assert (
        page.evaluate("rowFor('4').querySelector('.name').textContent") == "F1 · Wide"
    )
    assert page.evaluate("panel.shadowRoot.querySelector('.sources').textContent") == (
        " · Bold"
    )
    assert page.evaluate(
        "panel.shadowRoot.querySelector('.group-label').textContent"
    ) == ("Since the last snapshot of A · 2")


def test_hover_previews_and_leaving_clears(page):
    page.evaluate(SETUP)
    page.evaluate("wait(300)")  # background preload of the listed versions
    assert glyph_requests(page, "2") == 1 and glyph_requests(page, "5") == 1
    assert page.evaluate("drawCall()") is None  # nothing previewed yet

    page.evaluate("rowFor('2').dispatchEvent(new MouseEvent('mouseenter'))")
    # Already loaded by the preload: shown at once, drawn paler.
    assert page.evaluate("panel.hovered?.sha") == "2" * 40
    assert page.evaluate("drawCall()") == {"alpha": 0.45, "fills": 1}
    assert "hovering" in page.evaluate("rowFor('2').className")
    assert page.evaluate("statusText()").startswith(
        "Hovering 2222222222 — layer “default”"
    )

    page.evaluate("rowFor('2').dispatchEvent(new MouseEvent('mouseleave'))")
    assert page.evaluate("panel.hovered") is None
    assert page.evaluate("drawCall()") is None
    assert "hovering" not in page.evaluate("rowFor('2').className")
    assert page.evaluate("statusText()") == (
        "Hover a version to preview it, click to keep it"
    )
    # Hovering again uses the cache: still one request for that version.
    page.evaluate("rowFor('2').dispatchEvent(new MouseEvent('mouseenter'))")
    page.evaluate("rowFor('2').dispatchEvent(new MouseEvent('mouseleave'))")
    assert glyph_requests(page, "2") == 1
    # The current version is not previewable.
    page.evaluate("rowFor('3').dispatchEvent(new MouseEvent('mouseenter'))")
    page.evaluate("wait(200)")
    assert page.evaluate("panel.hovered") is None


def test_hover_waits_before_fetching_a_version(page):
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    # A version never loaded: sweeping over it quickly fetches nothing.
    page.evaluate("""() => {
          state.glyphs[sha("7")] = state.glyphs[sha("2")];
          const row = rowFor('2').cloneNode(true);
          row.dataset.sha = sha("7");
          row.addEventListener("mouseenter", () => panel.hover(sha("7"), "A"));
          row.addEventListener("mouseleave", () => panel.unhover(sha("7")));
          panel.listElement.append(row);
          row.dispatchEvent(new MouseEvent('mouseenter'));
          row.dispatchEvent(new MouseEvent('mouseleave'));
        }""")
    page.evaluate("wait(250)")
    assert glyph_requests(page, "7") == 0 and page.evaluate("panel.hovered") is None
    page.evaluate("rowFor('7').dispatchEvent(new MouseEvent('mouseenter'))")
    page.evaluate("wait(250)")
    assert glyph_requests(page, "7") == 1
    assert page.evaluate("panel.hovered?.sha") == "7" * 40


def test_click_pins_and_hover_does_not_disturb_it(page):
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    page.evaluate("""() => {
          rowFor('2').dispatchEvent(new MouseEvent('mouseenter'));
          rowFor('2').click();
        }""")
    assert page.evaluate("panel.pinned?.sha") == "2" * 40
    assert "previewing" in page.evaluate("rowFor('2').className")
    # Pinned and hovered are the same version: drawn at full strength.
    assert page.evaluate("drawCall()") == {"alpha": None, "fills": 1}
    page.evaluate("restoreButton().click()")  # arm
    assert page.evaluate("panel.armedRestore?.sha") == "2" * 40

    # Hovering the snapshot row shows it (paler) instead; leaving comes back
    # to the pinned version, and the armed restore is still there.
    page.evaluate("""() => {
          rowFor('2').dispatchEvent(new MouseEvent('mouseleave'));
          rowFor('5').dispatchEvent(new MouseEvent('mouseenter'));
        }""")
    assert page.evaluate("statusText()").startswith("Hovering snapshot “V1”")
    assert page.evaluate("drawCall()") == {"alpha": 0.45, "fills": 1}
    page.evaluate("rowFor('5').dispatchEvent(new MouseEvent('mouseleave'))")
    assert page.evaluate("statusText()").startswith("Previewing 2222222222")
    assert page.evaluate("drawCall()") == {"alpha": None, "fills": 1}
    assert page.evaluate("panel.armedRestore?.sha") == "2" * 40
    assert page.evaluate("restoreButton().classList.contains('armed')")

    # A snapshot can be pinned and restored like a commit.
    page.evaluate("rowFor('5').click()")
    assert page.evaluate("panel.pinned?.sha") == "5" * 40
    page.evaluate("restoreButton().click()")
    page.evaluate("restoreButton().click()")
    page.evaluate("wait(100)")
    assert page.evaluate("state.posts") == [
        {"route": "restore", "branch": "main", "glyph": "A", "ref": "5" * 40}
    ]
    assert page.evaluate("panel.pinned") is None


def test_selecting_another_glyph_clears_everything(page):
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    page.evaluate("""() => {
          rowFor('2').click();
          rowFor('5').dispatchEvent(new MouseEvent('mouseenter'));
          editor.sceneController.sceneSettings.selectedGlyphName = "B";
          state.listeners.forEach((f) => f());
        }""")
    assert page.evaluate("[panel.pinned, panel.hovered]") == [None, None]
    assert page.evaluate("drawCall()") is None


def select(page, glyph):
    page.evaluate(f"""async () => {{
          editor.sceneController.sceneSettings.selectedGlyphName = {glyph!r};
          state.listeners.forEach((f) => f());
          await wait(50);
        }}""".replace("None", "null"))


def test_font_history_when_no_glyph_is_selected(page):
    page.evaluate(SETUP)
    title = "panel.shadowRoot.querySelector('.title').textContent"
    assert page.evaluate(title) == "Glyph history"
    assert page.evaluate("panel.snapshotButton.title").startswith(
        "Name this version of A"
    )
    select(page, None)
    assert page.evaluate(title) == "Font history"
    assert page.evaluate("panel.snapshotButton.title").startswith(
        "Name the current state of the whole font"
    )
    assert (
        page.evaluate("state.requests.filter(r => r.route === 'log').at(-1).glyph")
        is None
    )
    # Every change, the snapshot commit only as its row; nothing to preview.
    assert page.evaluate("rows()") == [
        "summary-line",
        "group-label",
        "commit current:3",
        "commit:4",
        "commit:2",
        "snapshot:5",
    ]
    assert page.evaluate(
        "panel.shadowRoot.querySelector('.summary-line').textContent"
    ) == ("The whole font — 4 changes")
    assert page.evaluate("rowFor('3').querySelector('.message').textContent") == (
        "Edit A · Bold"
    )
    page.evaluate(
        "rowFor('4').dispatchEvent(new MouseEvent('mouseenter')); rowFor('4').click()"
    )
    page.evaluate("wait(200)")
    assert page.evaluate("[panel.pinned, panel.hovered]") == [None, None]
    assert "Select a glyph" in page.evaluate("statusText()")
    # Selecting a glyph again: its history, and an open snapshot form closes.
    page.evaluate("panel.snapshotButton.click()")
    select(page, "A")
    assert page.evaluate(title) == "Glyph history"
    assert page.evaluate("panel.snapshotForm") is None
    assert page.evaluate("rows()")[0] == "summary-line"
    assert page.errors == []


def test_snapshot_form(page):
    page.evaluate(SETUP)
    select(page, None)
    page.evaluate("panel.snapshotButton.click()")
    page.evaluate("wait(50)")
    form = "panel.shadowRoot.querySelector('.snapshot-form')"
    summary = page.evaluate(f"{form}.querySelector('.summary').textContent")
    assert summary.startswith(
        "Names the state of the whole font: groups the 2 changes made to the project "
        "since snapshot “V1” (by J, K)."
    )
    assert page.evaluate(f"{form}.querySelector('button.create').disabled")
    page.evaluate(f"""() => {{
          const input = {form}.querySelector('input');
          input.value = "Proofs sent";
          input.dispatchEvent(new Event('input'));
          const opts = {{ bubbles: true, composed: true }};
          const key = (k) => new KeyboardEvent('keydown', {{ key: k, ...opts }});
          input.dispatchEvent(key('v'));
          input.dispatchEvent(key('Enter'));
        }}""")
    page.evaluate("wait(100)")
    # Keys typed in the field never reach the editor's shortcuts.
    assert page.evaluate("state.windowKeys") == 0
    assert page.evaluate("state.posts") == [
        {"route": "snapshot", "branch": "main", "name": "Proofs sent"}
    ]
    assert page.evaluate(f"{form}.children.length") == 0  # closed after success
    assert page.errors == []


def test_plugin_manifest_points_at_init():
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text())
    assert manifest["init"] == "init.js" and manifest["function"] == "init"


def test_hovering_and_pinning_never_move_the_rows(page):
    """The preview bar has a fixed place and height, and rows stay one line:
    nothing under the pointer moves (no flicker)."""
    page.evaluate(SETUP)
    page.evaluate(
        "() => { document.body.style.cssText = 'margin:0;height:600px;width:320px';"
        " panel.style.height = '600px'; }"
    )
    page.evaluate("wait(300)")
    layout = """() => [...panel.shadowRoot.querySelectorAll('.list > *, .status')].map(
        (e) => { const r = e.getBoundingClientRect(); return [r.top, r.height]; })"""
    before = page.evaluate(layout)
    heights = page.evaluate(
        "[...panel.shadowRoot.querySelectorAll('.commit, .snapshot')]"
        ".map((e) => e.getBoundingClientRect().height)"
    )
    assert (
        len(set(heights)) == 1 and heights[0] < 30
    )  # one line each (16px font here), all the same
    for script in [
        "rowFor('2').dispatchEvent(new MouseEvent('mouseenter'))",
        "rowFor('2').click()",
        "restoreButton().click()",  # armed
        "rowFor('2').dispatchEvent(new MouseEvent('mouseleave'))",
        "rowFor('5').dispatchEvent(new MouseEvent('mouseenter'))",
        "rowFor('5').dispatchEvent(new MouseEvent('mouseleave'))",
        "panel.clearPreview()",
    ]:
        page.evaluate(script)
        page.evaluate("wait(20)")
        assert page.evaluate(layout) == before, script


def test_no_preview_is_left_without_its_row(page):
    """A pinned version whose row disappears is dropped: nothing stays orange."""
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    # A version inside a snapshot group, then the group is collapsed.
    page.evaluate("""() => {
          panel.shadowRoot.querySelector('.snapshot .caret').click();
          rowFor('1').click();
        }""")
    assert page.evaluate("panel.pinned?.sha") == "1" * 40
    page.evaluate("panel.shadowRoot.querySelector('.snapshot .caret').click()")
    assert page.evaluate("[panel.pinned, panel.hovered]") == [None, None]
    assert page.evaluate("drawCall()") is None
    assert "Hover a version" in page.evaluate("statusText()")

    # A version pinned (and one hovered) when a snapshot is created: the
    # versions are grouped under the new snapshot, the previews go.
    page.evaluate("""async () => {
          rowFor('2').click();
          rowFor('5').dispatchEvent(new MouseEvent('mouseenter'));
          panel.snapshotButton.click();
          await wait(50);
          await panel.createSnapshot("Proofs");
          await wait(50);
        }""")
    assert page.evaluate("state.posts.at(-1).route") == "glyph-snapshot"
    assert page.evaluate("[panel.pinned, panel.hovered]") == [None, None]
    assert page.evaluate("drawCall()") is None
    assert "Hover a version" in page.evaluate("statusText()")
    assert page.evaluate("panel.shadowRoot.querySelectorAll('.previewing').length") == 0


def test_click_on_empty_space_or_current_deselects(page):
    page.evaluate(SETUP)
    page.evaluate(
        "() => { document.body.style.cssText = 'margin:0;height:600px;width:320px';"
        " panel.style.height = '600px'; }"
    )
    page.evaluate("wait(300)")
    pin = "rowFor('2').click()"
    pinned = "panel.pinned?.sha ?? null"
    # The current version.
    page.evaluate(pin)
    assert page.evaluate(pinned) == "2" * 40
    page.evaluate("rowFor('3').click()")
    assert page.evaluate(pinned) is None
    assert page.evaluate("drawCall()") is None
    # Empty space below the rows, and the labels above them.
    for target in [
        "panel.listElement",
        "panel.shadowRoot.querySelector('.summary-line')",
        "panel.shadowRoot.querySelector('.group-label')",
        "panel.shadowRoot.querySelector('.panel')",
    ]:
        page.evaluate(pin)
        assert page.evaluate(pinned) == "2" * 40
        page.evaluate(f"{target}.click()")
        assert page.evaluate(pinned) is None, target
    # But not the preview bar, its buttons, the header buttons or a row.
    page.evaluate(pin)
    page.evaluate("panel.statusElement.querySelector('.text').click()")
    page.evaluate("panel.shadowRoot.querySelector('.refresh').click()")
    assert page.evaluate(pinned) == "2" * 40
    page.evaluate("rowFor('5').click()")
    assert page.evaluate(pinned) == "5" * 40


def test_other_sources_shown_on_the_canvas_are_previewed_too(page):
    """Sources shown with the edited one (several edited at once, or in the
    background) get the old version's outline as well."""
    page.evaluate(SETUP)
    page.evaluate("""() => {
          const layer = (size) => ({ glyph: { path: { contours: [
            { points: [{ x: 0, y: 0 }, { x: size, y: 0 }, { x: size, y: size }],
              isClosed: true },
          ] } } });
          state.glyphs[sha("2")] = {
            name: "A",
            sources: [
              { name: "Regular", layerName: "default", location: {} },
              { name: "Bold", layerName: "bold^1", locationBase: "font-bold" },
            ],
            layers: { default: layer(200), "bold^1": layer(260), wide: layer(300) },
          };
          panel.previews.clear();  // drop what the preload already fetched
          panel.glyphRequests.clear();
        }""")
    page.evaluate("wait(300)")
    page.evaluate("rowFor('2').click()")
    settings = "editor.sceneController.sceneSettings"
    assert page.evaluate("drawCall()") == {"alpha": None, "fills": 1}
    assert page.evaluate("strokes") == 1  # the edited source only
    # A background layer by name, a source by font source id (locationBase),
    # one the old version does not have (skipped), the edited one again.
    page.evaluate(f"""() => {{
          {settings}.backgroundLayers = {{ wide: "", "font-bold": "", light: "" }};
          {settings}.editingLayers = {{ default: "" }};
        }}""")
    assert page.evaluate("drawCall()") == {"alpha": None, "fills": 1}
    assert page.evaluate("strokes") == 3  # default (filled) + wide + bold outlines
    page.evaluate("wait(20)")
    assert page.evaluate("statusText()").startswith(
        "Previewing 2222222222 — layer “default” + 2 other sources"
    )
    helpers = page.evaluate("""async () => {
          const m = await import("/init.js");
          const g = state.glyphs[sha("2")];
          return [m.matchLayerName(g, "wide"), m.matchLayerName(g, "font-bold"),
                  m.matchLayerName(g, "light")];
        }""")
    assert helpers == ["wide", "bold^1", None]
    # Back to no background source: only the edited one.
    page.evaluate(f"() => {{ {settings}.backgroundLayers = {{}}; }}")
    page.evaluate("drawCall()")
    assert page.evaluate("strokes") == 1


def test_draw_accepts_both_of_fontras_signatures(page):
    """Fontra passes one object to draw() since fontra/fontra#2785; before,
    positional arguments. The preview draws with either."""
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    page.evaluate("rowFor('2').click()")
    page.evaluate("panel.pinned.loaded")
    assert page.evaluate("drawCall()") == {"alpha": None, "fills": 1}
    assert page.evaluate("drawCall(true)") == {"alpha": None, "fills": 1}


def test_glyph_snapshot_form(page):
    page.evaluate(SETUP)
    page.evaluate("panel.snapshotButton.click()")
    page.evaluate("wait(50)")
    form = "panel.shadowRoot.querySelector('.snapshot-form')"
    assert page.evaluate(f"{form}.querySelector('.summary').textContent").startswith(
        "Names this version of A (only this glyph): groups its 2 versions "
        "since its snapshot “V1” (by J)."
    )
    # No request: the numbers come from the list.
    assert page.evaluate("state.requests.some(r => r.route === 'snapshots')") is False
    page.evaluate(f"""async () => {{
          const input = {form}.querySelector('input');
          input.value = "Approved";
          input.dispatchEvent(new Event('input'));
          {form}.querySelector('button.create').click();
          await wait(100);
        }}""")
    assert page.evaluate("state.posts") == [
        {"route": "glyph-snapshot", "branch": "main", "glyph": "A", "name": "Approved"}
    ]
    assert page.evaluate("panel.snapshotForm") is None
    assert page.errors == []


COMMENTS = """
() => {
  const person = (u) => ({ username: u, name: u.toUpperCase() });
  const issue = (number, created, resolvedAt, glyph = "A") => ({
    number, glyph, created, state: resolvedAt ? "resolved" : "open",
    author: person("ana"), messages: [{ id: 1, text: `Topic ${number}` }],
    resolved: resolvedAt ? { by: person("dan"), at: resolvedAt } : null,
  });
  const issues = [
    issue(1, "2027-01-01T00:00:00Z", "2027-01-02T00:00:00Z"),
    issue(2, "2019-01-01T00:00:00Z", null),
    issue(3, "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z", "B"),
  ];
  window.reopened = [];
  panel.comments = {
    issues,
    issuesOf: (g) => issues.filter((i) => i.glyph === g),
    show: (i) => (window.shown = i.number),
    mayChangeState: () => true,
    setState: async (n, s) => window.reopened.push([n, s]),
  };
}
"""


def test_comment_events_in_the_glyph_history(page):
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    page.evaluate(COMMENTS)
    page.evaluate("panel.render('A', null)")
    rows = [
        r for r in page.evaluate("rows()") if r not in ("summary-line", "group-label")
    ]
    assert rows[:3] == [
        "comment-event resolved",
        "comment-event opened",
        "commit current:3",
    ]
    assert rows[-1] == "comment-event opened"  # #2, older than every version
    assert "#1 resolved by DAN" in page.evaluate(
        "panel.shadowRoot.querySelector('.comment-event').textContent"
    )
    page.evaluate("panel.shadowRoot.querySelector('.comment-event').click()")
    assert page.evaluate("window.shown") == 1


def test_a_commented_version_stays_pinned_without_a_row(page):
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    page.evaluate(
        "panel.showExternalVersion(sha('1'), 'A', 'the version commented in #1')"
    )
    page.evaluate("wait(100)")
    page.evaluate(
        "panel.render('A', null)"
    )  # c1 is inside the collapsed snapshot group
    assert page.evaluate("panel.pinned?.sha") == "1" * 40
    assert page.evaluate("statusText()").startswith(
        "Previewing the version commented in #1"
    )
    assert page.evaluate("drawCall()") == {"alpha": None, "fills": 1}
    # Clicking a row replaces it, with its own label.
    page.evaluate("rowFor('2').click()")
    assert page.evaluate("statusText()").startswith("Previewing 2222222222")


def test_restoring_an_older_version_offers_to_reopen(page):
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    page.evaluate(COMMENTS)
    page.evaluate("window.confirm = (text) => { window.asked = text; return true; }")
    page.evaluate("rowFor('2').click()")
    page.evaluate("restoreButton().click()")
    page.evaluate("restoreButton().click()")
    page.wait_for_function("window.reopened.length === 1")
    assert page.evaluate("window.reopened") == [[1, "open"]]
    assert "older than the resolution of #1" in page.evaluate("window.asked")


def test_snapshots_count_the_comments_they_resolved(page):
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    page.evaluate(COMMENTS)
    page.evaluate(
        "editor.sceneController.sceneSettings.selectedGlyphName = null;"
        " state.listeners.forEach((f) => f())"
    )
    page.evaluate("wait(200)")
    counts = page.evaluate(
        "[...panel.shadowRoot.querySelectorAll('.resolved-count')].map(e => e.textContent)"
    )
    assert counts == ["✓ 1"]  # #3, resolved before the font snapshot "V1"
    assert page.errors == []


def test_a_commented_version_is_drawn_on_the_comments_source(page):
    page.evaluate(SETUP)
    page.evaluate("wait(300)")
    page.evaluate(
        "panel.showExternalVersion(sha('1'), 'A', 'the version commented in #1', 'default')"
    )
    page.evaluate("wait(100)")
    # The canvas shows another source ("Bold"): the comment's layer is drawn.
    assert page.evaluate("!!panel.previewPathFor('Bold')")
    assert page.evaluate("panel.pinned.shownLayer") == "default"
    assert page.evaluate("panel.pinned.layerNote") is None
    # A row clicked afterwards follows the canvas again.
    page.evaluate("rowFor('2').click()")
    page.evaluate("wait(100)")
    page.evaluate("panel.previewPathFor('Bold')")
    assert "no layer" in page.evaluate("panel.pinned.layerNote")
