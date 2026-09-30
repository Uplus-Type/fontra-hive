""""Try Fontra" in a headless browser: the engine (client/try/try-engine.js)
answers Fontra's WebSocket calls from the demo font; the .fontra reader and
writer (fontra-format.js) give back Fontra's own files; and, when Fontra's
built client is installed, the real editor opens the demo, and a .fontra.zip
opened in the browser is edited, kept across a reload and downloaded.
Skipped without Playwright."""

import base64
import http.server
import io
import json
import pathlib
import threading
import urllib.parse
import zipfile
from importlib import resources

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

CLIENT_DIR = pathlib.Path(__file__).parent.parent / "src" / "fontra_hive" / "client"
DATA_DIR = pathlib.Path(__file__).parent / "data"
FIXTURE = DATA_DIR / "MutatorSansLocationBase.fontra"
CHROMIUM = "/opt/pw-browsers/chromium"
TRY_PAGE_QUERY = "project=demo%3AMutatorSans&text=%22HAMBURGEFONSTIV%22"

BARE_PAGE = """<!doctype html><html><head>
<script src="/hive/try/fontra-format.js"></script>
<script src="/hive/try/try-store.js"></script>
<script src="/hive/try/try-engine.js"></script>
</head><body></body></html>"""


def _fontraClient():
    try:
        client = resources.files("fontra") / "client"
        if (client / "editor.html").is_file():
            return pathlib.Path(str(client))
    except (ModuleNotFoundError, TypeError):
        pass
    return None


class Handler(http.server.SimpleHTTPRequestHandler):
    """/hive/… from Hive's client folder; Fontra's pages with the try scripts
    (as tryViewHandler serves them); the rest from Fontra's client, if any."""

    fontraClient = None

    def log_message(self, *args):
        pass

    def _send(self, body: bytes, contentType: str):
        self.send_response(200)
        self.send_header("Content-Type", contentType)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from fontra_hive.projectmanager import injectTryScripts

        path = self.path.split("?")[0]
        if path == "/bare.html":
            return self._send(BARE_PAGE.encode(), "text/html")
        if path == "/editor.html" and self.fontraClient:
            html = (self.fontraClient / "editor.html").read_text(encoding="utf-8")
            return self._send(injectTryScripts(html).encode(), "text/html")
        if path.startswith("/hive/"):
            file = CLIENT_DIR / path[len("/hive/") :]
        elif path.startswith("/testdata/"):
            file = DATA_DIR / urllib.parse.unquote(path[len("/testdata/") :])
        elif self.fontraClient:
            file = self.fontraClient / path.lstrip("/")
        else:
            file = None
        if file is None or not file.is_file():
            self.send_error(404)
            return
        types = {".js": "text/javascript", ".wasm": "application/wasm"}
        contentType = types.get(file.suffix) or self.guess_type(str(file))
        self._send(file.read_bytes(), contentType)


@pytest.fixture(scope="module")
def server():
    Handler.fontraClient = _fontraClient()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as p:
        options = {}
        if pathlib.Path(CHROMIUM).exists():
            options["executable_path"] = CHROMIUM
        try:
            browser = p.chromium.launch(**options)
        except Exception as error:  # no browser available
            pytest.skip(f"no Chromium: {error}")
        yield browser
        browser.close()


# A client for the fake WebSocket, as remote.js speaks to it.
CALL = """async ([method, args]) => {
  if (!window.__ws) {
    window.__replies = {};
    window.__ws = new WebSocket("ws://" + location.host + "/websocket?project=demo");
    window.__ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      window.__replies[message["client-call-id"]](message);
    };
    await new Promise((resolve) => (window.__ws.onopen = resolve));
    window.__ws.send(JSON.stringify({ "client-uuid": "test" }));
    window.__id = 0;
  }
  const id = window.__id++;
  const reply = new Promise((resolve) => (window.__replies[id] = resolve));
  window.__ws.send(
    JSON.stringify({ "client-call-id": id, "method-name": method, arguments: args })
  );
  return await reply;
}"""


def test_engine_answers_fontras_calls(server, browser):
    page = browser.new_page()
    page.goto(server + "/bare.html")

    def call(method, *args):
        return page.evaluate(CALL, [method, list(args)])

    glyphMap = call("getGlyphMap")["return-value"]
    assert 65 in glyphMap["A"] and len(glyphMap) >= 40
    glyph = call("getGlyph", "A")["return-value"]
    assert glyph["name"] == "A" and glyph["layers"]
    assert call("getGlyph", "nope")["return-value"] is None
    assert call("isReadOnly")["return-value"] is False
    assert call("getAxes")["return-value"]["axes"]
    assert call("getMetaInfo")["return-value"]["projectName"] == "MutatorSans (demo)"
    assert "Adieresis" in call("findGlyphsThatUseGlyph", "A")["return-value"]
    assert "exception" in call("exportAs", {})
    assert "not available" in call("somethingElse")["exception"]

    # A plain change, with no editor on the page to read back from.
    change = {"p": ["glyphMap"], "f": "=", "a": ["A.new", [0xE000]]}
    assert call("editFinal", change, {}, "test", False)["return-value"] is None
    assert call("getGlyphMap")["return-value"]["A.new"] == [0xE000]
    call("editFinal", {"p": ["glyphs"], "f": "d", "a": ["B"]}, {}, "test", False)
    assert call("getGlyph", "B")["return-value"] is None
    assert page.evaluate("window.hiveTry.TryFont.edited") is True

    # Other WebSockets are real ones; the Hive plug-in is hidden from Fontra.
    assert page.evaluate(
        "localStorage.setItem('fontra.pluginsplugins', '[1]'),"
        " localStorage.getItem('fontra.pluginsplugins')"
    ) == "[]"
    other = "localStorage.setItem('x', '1'), localStorage.getItem('x')"
    assert page.evaluate(other) == "1"
    page.close()


def test_the_real_editor_opens_and_edits_the_demo(server, browser):
    if Handler.fontraClient is None:
        pytest.skip("Fontra's built client is not installed")
    page = browser.new_page(viewport={"width": 1400, "height": 850})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.H", timeout=15000
    )
    page.wait_for_timeout(1500)
    assert page.title().endswith("MutatorSans (demo)")
    assert page.locator(".hive-try").inner_text().startswith("Try Fontra")

    before = page.evaluate(
        "hiveTry.font().then(f => JSON.stringify(f.glyphs.H.layers))"
    )
    # Edit H in the editor: double-click it, select all its points, nudge up.
    page.mouse.click(700, 650)
    page.mouse.dblclick(145, 440)
    page.wait_for_timeout(500)
    page.mouse.move(80, 300)
    page.mouse.down()
    page.mouse.move(250, 600, steps=5)
    page.mouse.up()
    page.keyboard.press("Shift+ArrowUp")
    page.wait_for_function("window.hiveTry.TryFont.edited", timeout=5000)
    page.wait_for_timeout(500)
    after = page.evaluate("hiveTry.font().then(f => JSON.stringify(f.glyphs.H.layers))")
    client = page.evaluate(
        "editorController.fontController.getGlyph('H')"
        ".then(g => JSON.stringify(g.glyph.layers))"
    )
    assert after != before
    assert after == client  # the engine's copy follows the editor's
    assert not errors
    page.close()


READ_WRITE = """async ([base, paths]) => {
  const F = window.HiveTryFormat;
  const files = new Map();
  for (const path of paths) {
    const url = base + path.split("/").map(encodeURIComponent).join("/");
    const response = await fetch(url);
    files.set("Font.fontra/" + path, await response.blob());
  }
  const { font } = await F.readPackage(files);
  const written = Object.fromEntries(F.writePackage(font));
  const zipped = await F.zip(new Map(Object.entries(written)));
  const again = await F.readPackage(await F.unzip(zipped));
  const bytes = new Uint8Array(await zipped.arrayBuffer());
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return {
    written,
    glyphs: Object.keys(font.glyphs),
    firstPath: font.glyphs.A,
    roundTrip: JSON.stringify(again.font) === JSON.stringify(font),
    zip: btoa(binary),
  };
}"""


def test_format_writes_fontras_own_files(server, browser):
    page = browser.new_page()
    page.goto(server + "/bare.html")
    paths = sorted(
        p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*") if p.is_file()
    )
    result = page.evaluate(
        READ_WRITE, [f"/testdata/{FIXTURE.name}/", [p for p in paths]]
    )
    # Fontra's files come back byte for byte (glyph names ↔ file names, CSV,
    # JSON layout, contours ↔ packed paths).
    for path in paths:
        # Fontra writes CSV with "\r\n" (Python's csv); the fixture may have "\n".
        original = (FIXTURE / path).read_text("utf-8")
        if path.endswith(".csv"):
            original = original.replace("\r\n", "\n").replace("\n", "\r\n")
        assert result["written"][path] == original, path
    assert set(result["written"]) == set(paths)
    layer = next(iter(result["firstPath"]["layers"].values()))["glyph"]
    assert set(layer["path"]) >= {"coordinates", "pointTypes", "contourInfo"}
    assert result["roundTrip"]
    # Python reads the browser's zip.
    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(result["zip"])))
    assert archive.testzip() is None
    assert sorted(archive.namelist()) == paths
    fontData = archive.read("font-data.json").decode()
    assert fontData == result["written"]["font-data.json"]
    page.close()


def test_glyph_file_names_match_fontras(server, browser):
    """The JS file names against Fontra's Python ones, when Fontra is here."""
    filenames = pytest.importorskip("fontra.backends.filenames")
    names = ["A", "a", "A.alt", "con", "CON.alt", ".notdef", "a/b", "Ab^C", "ÉÈ"]
    names.append("x" * 12)
    expected = {name: filenames.stringToFileName(name) for name in names}
    page = browser.new_page()
    page.goto(server + "/bare.html")
    got = page.evaluate(
        "names => Object.fromEntries(names.map(n => "
        "[n, HiveTryFormat.stringToFileName(n)]))",
        names,
    )
    back = page.evaluate(
        "stems => stems.map(s => HiveTryFormat.fileNameToString(s))",
        list(expected.values()),
    )
    page.close()
    assert got == expected
    assert back == names


def test_a_local_font_is_kept_edited_and_downloaded(server, browser, tmp_path):
    if Handler.fontraClient is None:
        pytest.skip("Fontra's built client is not installed")
    archive = tmp_path / "Mutator.fontra.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(FIXTURE.rglob("*")):
            if path.is_file():
                z.write(path, "Mutator.fontra/" + path.relative_to(FIXTURE).as_posix())
    context = browser.new_context(
        viewport={"width": 1400, "height": 850}, accept_downloads=True
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    page.get_by_role("button", name="Your fonts").click()
    page.locator(".hive-try-panel input[accept]").set_input_files(str(archive))
    page.wait_for_url("**project=local*", timeout=15000)
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.A")
    page.wait_for_function(
        "document.querySelector('.hive-try .name').textContent === 'Mutator'"
    )

    before = page.evaluate("hiveTry.font().then(f => JSON.stringify(f.glyphs.A))")
    # An edit as the editor makes one: applied to Fontra's copy, then sent.
    page.evaluate(
        """async () => {
          const fc = editorController.fontController;
          const glyph = (await fc.getGlyph("A")).glyph;
          const layer = Object.keys(glyph.layers)[0];
          const path = glyph.layers[layer].glyph.path;
          const [x, y] = [path.coordinates[0], path.coordinates[1]];
          const p = ["glyphs", "A", "layers", layer, "glyph", "path"];
          const change = { p, f: "=xy", a: [0, x, y + 10] };
          const rollback = { p, f: "=xy", a: [0, x, y] };
          await fc.applyChange(change);
          await fc.editFinal(change, rollback, "test", true);
        }"""
    )
    page.wait_for_function(
        "document.querySelector('.hive-try .status').textContent"
        " === 'saved in this browser'",
        timeout=5000,
    )
    edited = page.evaluate(
        "editorController.fontController.getGlyph('A')"
        ".then(g => JSON.stringify(g.glyph.layers))"
    )
    page.reload()
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.A")
    kept = page.evaluate(
        "editorController.fontController.getGlyph('A')"
        ".then(g => JSON.stringify(g.glyph.layers))"
    )
    assert kept == edited
    assert json.loads(before)["layers"] != json.loads(kept)

    with page.expect_download() as info:
        page.get_by_role("button", name="Download").click()
    download = info.value
    assert download.suggested_filename == "Mutator.fontra.zip"
    with zipfile.ZipFile(download.path()) as z:
        names = z.namelist()
        assert "Mutator.fontra/font-data.json" in names
        glyph = json.loads(z.read("Mutator.fontra/glyphs/A^1.json"))
        assert "contours" in next(iter(glyph["layers"].values()))["glyph"]["path"]
    assert not errors
    context.close()
