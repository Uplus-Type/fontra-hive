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
# head) and c2 since the last snapshot, c1 grouped under snapshot "V1" (s1).
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
              [sha("5")]: glyph(100) },
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
    if (route === "log") return json({
      head: sha("3"),
      commits: [commit("3", "Edit A", null), commit("2", "Edit A", null),
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
  const sceneSettings = { selectedGlyphName: "A" };
  const editor = {
    projectIdentifier: "Mutator",
    sceneController: {
      sceneSettings,
      sceneSettingsController: { addKeyListener: (keys, f) => state.listeners.push(f) },
    },
    visualizationLayers: { definitions: [], toggle: () => {} },
    canvasController: { requestUpdate: () => state.updates++ },
    addSidebarPanel: (panel) => { document.body.append(panel); window.panel = panel; },
  };
  window.editor = editor;
  const plugin = await import("/init.js");
  plugin.init(editor, "/hive/plugin");
  await window.panel.toggle(true);
  window.panel.stopPolling();
  window.rows = () => [...window.panel.shadowRoot.querySelectorAll(".list > *")].map(
    (e) => e.className + (e.dataset.sha ? ":" + e.dataset.sha[0] : ""));
  window.rowFor = (c) => window.panel.shadowRoot.querySelector(`[data-sha="${sha(c)}"]`);
  window.statusText = () => window.panel.shadowRoot.querySelector(".status").textContent;
  window.drawCall = () => {
    const def = editor.visualizationLayers.definitions[0];
    const selected = def.selectionFunc({ glyphsBySelectionMode: {
      editing: [{ glyphName: sceneSettings.selectedGlyphName }] } });
    if (!selected.length) return null;
    const calls = { alpha: null, fills: 0 };
    const ctx = {
      save() {}, restore() {}, stroke() {},
      set globalAlpha(v) { calls.alpha = v; },
      fill(path) { calls.fills++; },
    };
    def.draw(ctx, { glyph: { layerName: "default" } },
      { fillColor: "f", strokeColor: "s", strokeWidth: 1 });
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
        "empty",
        "group-label",
        "commit current:3",
        "commit:2",
        "snapshot:5",
    ]
    page.evaluate("panel.shadowRoot.querySelector('.snapshot .caret').click()")
    assert page.evaluate("rows()")[-2:] == ["snapshot:5", "commit nested:1"]
    assert "1 version of A · 4 changes in the project" in page.evaluate(
        "panel.shadowRoot.querySelector('.snapshot .meta').textContent"
    )


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
    assert page.evaluate("statusText()") == ""
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
    page.evaluate("rowFor('2').querySelector('button.restore').click()")  # arm
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
    assert page.evaluate("rowFor('2').querySelector('button.restore.armed') !== null")

    # A snapshot can be pinned and restored like a commit.
    page.evaluate("rowFor('5').click()")
    assert page.evaluate("panel.pinned?.sha") == "5" * 40
    page.evaluate("rowFor('5').querySelector('button.restore').click()")
    page.evaluate("rowFor('5').querySelector('button.restore').click()")
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


def test_snapshot_form(page):
    page.evaluate(SETUP)
    page.evaluate("panel.snapshotButton.click()")
    page.evaluate("wait(50)")
    form = "panel.shadowRoot.querySelector('.snapshot-form')"
    summary = page.evaluate(f"{form}.querySelector('.summary').textContent")
    assert summary.startswith(
        "Groups the 2 changes made to the project since snapshot “V1” (by J, K)."
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
