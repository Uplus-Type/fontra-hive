""""Try Fontra" in a headless browser: the engine (client/try/try-engine.js)
answers Fontra's WebSocket calls from the demo font; the .fontra reader and
writer (fontra-format.js) give back Fontra's own files; and, when Fontra's
built client is installed, the real editor opens the demo, and a .fontra.zip
opened in the browser is edited, kept across a reload and downloaded. With
Pyodide too ($HIVE_TEST_PYODIDE_DIR: an unpacked pyodide-core release), a
designspace and a TrueType font are converted in the browser, and a font is
downloaded as designspace + UFOs. Skipped without Playwright."""

import asyncio
import base64
import http.server
import io
import json
import os
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


PYODIDE_DIR = os.environ.get("HIVE_TEST_PYODIDE_DIR")
# fontc built for the browser (tools/build-fontc-wasm.sh): compiled exports.
FONTC_WASM = os.environ.get("HIVE_TEST_FONTC_WASM")


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
            pyodide = "/hive/pyodide/" if PYODIDE_DIR else None
            fontc = "/hive/try/fontc.wasm" if FONTC_WASM else None
            return self._send(injectTryScripts(html, pyodide, fontc).encode(), "text/html")
        if path == "/hive/try/fontc.wasm" and FONTC_WASM:
            return self._send(pathlib.Path(FONTC_WASM).read_bytes(), "application/wasm")
        if path == "/hive/try/python.zip":
            from fontra_hive import trybundle

            return self._send(trybundle.bundle()[0], "application/zip")
        if path == "/hive/try/python-glyphs.zip":
            from fontra_hive import trybundle

            glyphs = trybundle.glyphsBundle()
            if glyphs is None:
                self.send_error(404)
                return
            return self._send(glyphs[0], "application/zip")
        if path.startswith("/hive/pyodide/") and PYODIDE_DIR:
            file = pathlib.Path(PYODIDE_DIR) / path[len("/hive/pyodide/") :]
        elif path.startswith("/hive/"):
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
        types = {
            ".js": "text/javascript",
            ".mjs": "text/javascript",
            ".wasm": "application/wasm",
        }
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


EDIT_A = """async (dy) => {
  const fc = editorController.fontController;
  const glyph = (await fc.getGlyph("A")).glyph;
  const layer = Object.keys(glyph.layers)[0];
  const path = glyph.layers[layer].glyph.path;
  const [x, y] = [path.coordinates[0], path.coordinates[1]];
  const p = ["glyphs", "A", "layers", layer, "glyph", "path"];
  const change = { p, f: "=xy", a: [0, x, y + dy] };
  const rollback = { p, f: "=xy", a: [0, x, y] };
  await fc.applyChange(change);
  await fc.editFinal(change, rollback, "test", true);
}"""

LAYERS_A = (
    "editorController.fontController.getGlyph('A')"
    ".then(g => JSON.stringify(g.glyph.layers))"
)

API = """async ([method, route, body]) => {
  const url = '/api/hive/projects/' + encodeURIComponent(hiveTry.localName) + route;
  const init = { method };
  if (body !== null) init.body = JSON.stringify(body);
  const r = await fetch(url, init);
  return [r.status, await r.text()];
}"""


def _exportAs(page, item):
    """Fontra's File › Export as: a download in Try Fontra."""
    page.get_by_text("File", exact=True).click()
    page.get_by_text("Export as", exact=True).hover()
    page.get_by_text(item, exact=True).click()


def _needsHiveInTheBrowser():
    if Handler.fontraClient is None:
        pytest.skip("Fontra's built client is not installed")
    if not PYODIDE_DIR:
        pytest.skip("$HIVE_TEST_PYODIDE_DIR is not set (an unpacked pyodide-core)")


def test_a_local_font_has_hive_in_the_browser(server, browser, tmp_path):
    """A font opened in Try Fontra is a git repository in the browser, served
    by Hive's own code: edits are commits, kept across a reload; the history
    panel's routes, restore and comments work; download and delete."""
    _needsHiveInTheBrowser()
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
    page.wait_for_url("**project=local*", timeout=90000)
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    page.wait_for_function(
        "document.querySelector('.hive-try .name').textContent === 'Mutator'"
    )
    assert page.title().endswith("Mutator")  # the font's name, not local:<id>
    before = page.evaluate(LAYERS_A)

    # An edit as the editor makes one: a commit, then saved in IndexedDB.
    page.evaluate(EDIT_A, 10)
    page.wait_for_function(
        "document.querySelector('.hive-try .status').textContent"
        " === 'saved in this browser'",
        timeout=20000,
    )
    edited = page.evaluate(LAYERS_A)
    status, body = page.evaluate(API, ["GET", "/log?glyph=A", None])
    assert status == 200
    commits = json.loads(body)["commits"]
    assert [c["message"].split("\n")[0] for c in commits] == [
        "Edit A",
        "Opened in Try Fontra",
    ]
    assert commits[0]["author"] == "You"

    # Kept across a reload.
    page.reload()
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    assert page.evaluate(LAYERS_A) == edited != before

    # Restore the first version: a new commit, and the editor reloads A.
    first = commits[-1]["sha"]
    status, body = page.evaluate(API, ["POST", f"/restore?glyph=A&ref={first}", None])
    assert status == 200 and json.loads(body)["changed"] is True
    page.wait_for_function(f"({LAYERS_A}).then(l => l === {json.dumps(before)})")

    # A comment, and a snapshot.
    layer = next(iter(json.loads(before)))
    comment = {
        "glyph": "A",
        "source": {"layer": layer, "name": layer, "location": {}},
        "point": {"x": 100, "y": 200},
        "text": "Tighter apex?",
        "branch": "main",
    }
    status, body = page.evaluate(API, ["POST", "/comments", comment])
    assert status == 200, body
    status, body = page.evaluate(API, ["GET", "/comments?glyph=A", None])
    assert [i["messages"][0]["text"] for i in json.loads(body)["issues"]] == [
        "Tighter apex?"
    ]
    status, body = page.evaluate(API, ["POST", "/snapshot?name=First+draft", None])
    assert status == 200, body

    # Downloaded as the repository's latest state.
    with page.expect_download(timeout=60000) as info:
        _exportAs(page, "Fontra (*.fontra)")
    download = info.value
    assert download.suggested_filename == "Mutator.fontra.zip"
    with zipfile.ZipFile(download.path()) as z:
        names = z.namelist()
        assert "Mutator.fontra/font-data.json" in names
        glyph = json.loads(z.read("Mutator.fontra/glyphs/A^1.json"))
        assert "contours" in next(iter(glyph["layers"].values()))["glyph"]["path"]

    # Deleted: gone from the list, and its storage with it.
    page.get_by_role("button", name="Your fonts").click()
    page.locator(".hive-try-panel li", has_text="Mutator").get_by_role(
        "button", name="Remove"
    ).click()
    page.wait_for_url("**project=demo*", timeout=30000)
    page.get_by_role("button", name="Your fonts").click()
    page.wait_for_function(
        "document.querySelector('.hive-try-panel ul').textContent.includes('No fonts')"
    )
    assert not errors
    context.close()


def _designspaceZip(tmp_path) -> pathlib.Path:
    """The fixture as a designspace with its UFOs, zipped, made by Fontra."""
    backends = pytest.importorskip("fontra.backends.designspace")
    from contextlib import aclosing

    from fontra.backends.copy import copyFont
    from fontra.backends.fontra import FontraBackend

    folder = tmp_path / "Mutator"
    folder.mkdir()

    async def convert():
        source = FontraBackend.fromPath(FIXTURE)
        path = folder / "Mutator.designspace"
        dest = backends.DesignspaceBackend.createFromPath(path)
        async with aclosing(source), aclosing(dest):
            await copyFont(source, dest)

    # In a thread: Playwright's sync API keeps an event loop in this one.
    worker = threading.Thread(target=asyncio.run, args=(convert(),))
    worker.start()
    worker.join()
    archive = tmp_path / "Mutator.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(tmp_path).as_posix())
    return archive


def _smallTTF(tmp_path) -> pathlib.Path:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "O"])
    fb.setupCharacterMap({ord("O"): "O"})
    pen = TTGlyphPen(None)
    pen.moveTo((100, 0))
    pen.lineTo((500, 0))
    pen.lineTo((500, 700))
    pen.lineTo((100, 700))
    pen.closePath()
    fb.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "O": pen.glyph()})
    fb.setupHorizontalMetrics({".notdef": (600, 0), "O": (600, 100)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Tiny", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    path = tmp_path / "Tiny.ttf"
    fb.save(str(path))
    return path


def test_other_formats_are_converted_in_the_browser(server, browser, tmp_path):
    if Handler.fontraClient is None:
        pytest.skip("Fontra's built client is not installed")
    if not PYODIDE_DIR:
        pytest.skip("$HIVE_TEST_PYODIDE_DIR is not set (an unpacked pyodide-core)")
    designspace = _designspaceZip(tmp_path)
    context = browser.new_context(
        viewport={"width": 1400, "height": 850}, accept_downloads=True
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")

    # A designspace with its UFOs, zipped: converted by Fontra in Pyodide.
    page.get_by_role("button", name="Your fonts").click()
    page.locator(".hive-try-panel input[accept]").set_input_files(str(designspace))
    page.wait_for_url("**project=local*", timeout=90000)
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.A")
    glyphs = page.evaluate("Object.keys(editorController.fontController.glyphMap)")
    assert set(glyphs) == {"A", "A.alt", "B"}

    # Downloaded as designspace + UFOs, converted back in Pyodide.
    with page.expect_download(timeout=60000) as info:
        _exportAs(page, "Designspace + UFO (*.designspace)")
    download = info.value
    assert download.suggested_filename == "Mutator.designspace.zip"
    with zipfile.ZipFile(download.path()) as z:
        names = z.namelist()
    assert "Mutator.designspace" in names
    assert any(n.endswith(".ufo/glyphs/A_.glif") for n in names)

    # A TrueType font.
    page.get_by_role("button", name="Your fonts").click()
    before = page.url
    page.locator(".hive-try-panel input[accept]").set_input_files(
        str(_smallTTF(tmp_path))
    )
    page.wait_for_function(f"location.href !== {json.dumps(before)}", timeout=60000)
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.O")
    assert page.locator(".hive-try .name").inner_text() == "Tiny"

    # Not a font: said plainly.
    page.get_by_role("button", name="Your fonts").click()
    bad = tmp_path / "notes.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("notes.txt", "hello")
    page.locator(".hive-try-panel input[accept]").set_input_files(str(bad))
    page.wait_for_function(
        "document.querySelector('.hive-try-panel .error').textContent.length > 0",
        timeout=30000,
    )
    assert "No font found" in page.locator(".hive-try-panel .error").inner_text()
    assert not errors
    context.close()


GLYPHS_SOURCE = """{
.appVersion = "3260";
.formatVersion = 3;
familyName = "Hexa";
fontMaster = (
{
id = m01;
name = Regular;
metricValues = ({pos = 700;},{},{pos = 500;},{pos = -200;},{});
}
);
glyphs = (
{
glyphname = O;
layers = (
{
layerId = m01;
shapes = (
{
closed = 1;
nodes = ((100,0,l),(500,0,l),(500,700,l),(100,700,l));
}
);
width = 600;
}
);
unicode = 79;
}
);
metrics = ({type = ascender;},{type = baseline;},{type = "x-height";},{type = descender;},{type = "cap height";});
unitsPerEm = 1000;
versionMajor = 1;
versionMinor = 0;
}
"""


def test_glyphs_files_are_read_in_the_browser(server, browser, tmp_path):
    _needsHiveInTheBrowser()
    from fontra_hive import trybundle

    if not trybundle.glyphsAvailable():
        pytest.skip("glyphsLib and fontra-glyphs are not installed")
    source = tmp_path / "Hexa.glyphs"
    source.write_text(GLYPHS_SOURCE, encoding="utf-8")
    context = browser.new_context(viewport={"width": 1400, "height": 850})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    page.get_by_role("button", name="Your fonts").click()
    page.locator(".hive-try-panel input[accept]").set_input_files(str(source))
    page.wait_for_url("**project=local*", timeout=120000)
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.O")
    assert page.locator(".hive-try .name").inner_text() == "Hexa"
    assert page.evaluate("editorController.fontController.glyphMap.O") == [79]
    assert not errors
    context.close()


def test_two_windows_on_one_font_see_each_others_edits(server, browser, tmp_path):
    """Tabs share one Python (a SharedWorker): the same server serves both
    windows, as online, and a closed tab lets go of its connection."""
    _needsHiveInTheBrowser()
    archive = tmp_path / "Mutator.fontra.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(FIXTURE.rglob("*")):
            if path.is_file():
                z.write(path, "Mutator.fontra/" + path.relative_to(FIXTURE).as_posix())
    context = browser.new_context(viewport={"width": 1400, "height": 850})
    first = context.new_page()
    errors = []
    first.on("pageerror", lambda error: errors.append(str(error)))
    first.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    first.get_by_role("button", name="Your fonts").click()
    first.locator(".hive-try-panel input[accept]").set_input_files(str(archive))
    first.wait_for_url("**project=local*", timeout=90000)
    first.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    second = context.new_page()
    second.on("pageerror", lambda error: errors.append(str(error)))
    second.goto(first.url)
    second.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=30000
    )
    before = second.evaluate(LAYERS_A)

    first.evaluate(EDIT_A, 25)
    edited = first.evaluate(LAYERS_A)
    assert edited != before
    second.wait_for_function(f"({LAYERS_A}).then(l => l === {json.dumps(edited)})")

    # The first window closed: the second one goes on, and is saved.
    first.close()
    second.evaluate(EDIT_A, 5)
    second.wait_for_function(
        "document.querySelector('.hive-try .status').textContent"
        " === 'saved in this browser'",
        timeout=20000,
    )
    status, body = second.evaluate(API, ["GET", "/log?glyph=A", None])
    assert status == 200
    messages = [c["message"].split("\n")[0] for c in json.loads(body)["commits"]]
    assert messages[-1] == "Opened in Try Fontra" and len(messages) >= 2
    assert not errors
    context.close()


def test_the_demo_is_exported_from_fontras_menu(server, browser):
    if Handler.fontraClient is None:
        pytest.skip("Fontra's built client is not installed")
    context = browser.new_context(
        viewport={"width": 1400, "height": 850}, accept_downloads=True
    )
    page = context.new_page()
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.A")
    with page.expect_download(timeout=30000) as info:
        _exportAs(page, "Fontra (*.fontra)")
    download = info.value
    assert download.suggested_filename == "MutatorSans.fontra.zip"
    with zipfile.ZipFile(download.path()) as z:
        assert "MutatorSans.fontra/font-data.json" in z.namelist()
    context.close()


UNION = """async () => {
  // Two overlapping squares, as Fontra's editor sends them to its server.
  const path = {
    coordinates: [0, 0, 0, 100, 100, 100, 100, 0, 50, 50, 50, 150, 150, 150, 150, 50],
    pointTypes: [0, 0, 0, 0, 0, 0, 0, 0],
    contourInfo: [{ endPoint: 3, isClosed: true }, { endPoint: 7, isClosed: true }],
  };
  const r = await fetch("/api/unionPath", { method: "POST", body: JSON.stringify({ path }) });
  return await r.json();
}"""


def test_remove_overlap_is_done_in_the_browser(server, browser):
    """Fontra's path operations (its server uses skia-pathops): answered in
    the browser by booleanOperations over Pyodide's pyclipper."""
    _needsHiveInTheBrowser()
    if not (pathlib.Path(PYODIDE_DIR) / "pyodide-lock.json").read_text().count(
        "pyclipper"
    ) or not list(pathlib.Path(PYODIDE_DIR).glob("pyclipper-*.whl")):
        pytest.skip("pyclipper's wheel is not next to Pyodide")
    context = browser.new_context(viewport={"width": 1400, "height": 850})
    page = context.new_page()
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.A")
    reply = page.evaluate(UNION)
    assert "error" not in reply, reply
    path = reply["returnValue"]
    assert len(path["contourInfo"]) == 1
    points = set(zip(path["coordinates"][::2], path["coordinates"][1::2]))
    assert points == {
        (0, 0), (100, 0), (100, 50), (150, 50), (150, 150), (50, 150), (50, 100), (0, 100)
    }
    reply = page.evaluate(
        """fetch("/api/parseClipboard", {method: "POST", body: JSON.stringify({data:
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0 L10 0 L10 10 Z"/></svg>'})})
        .then(r => r.json())"""
    )
    assert reply["returnValue"]["path"]["pointTypes"] == [0, 0, 0]
    context.close()


def test_branches_of_a_font_kept_in_the_browser(server, browser, tmp_path):
    """Hive's branch pill on a font kept in the browser: a new branch, an
    edit on it, merged into main, all served by Hive's routes in Pyodide."""
    _needsHiveInTheBrowser()
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
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    page.get_by_role("button", name="Your fonts").click()
    page.locator(".hive-try-panel input[accept]").set_input_files(str(archive))
    page.wait_for_url("**project=local*", timeout=90000)
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    before = page.evaluate(LAYERS_A)
    page.wait_for_function(
        "document.querySelector('.hive-branch span')?.textContent === 'main'",
        timeout=30000,
    )

    # A new branch: the same view, on it.
    page.locator(".hive-branch").click()
    page.get_by_text("New branch…").click()
    page.get_by_label("Branch name").fill("wider")
    page.get_by_role("button", name="Create").click()
    page.wait_for_function(
        "new URLSearchParams(location.search).get('project').endsWith('@wider')",
        timeout=30000,
    )
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    page.wait_for_function(
        "document.querySelector('.hive-branch span')?.textContent === 'wider'",
        timeout=30000,
    )
    page.evaluate(EDIT_A, 40)
    page.wait_for_function(
        "document.querySelector('.hive-try .status').textContent"
        " === 'saved in this browser'",
        timeout=20000,
    )
    edited = page.evaluate(LAYERS_A)
    assert edited != before

    # Exported: the branch, named after it.
    with page.expect_download(timeout=60000) as info:
        _exportAs(page, "Fontra (*.fontra)")
    assert info.value.suggested_filename == "Mutator-wider.fontra.zip"
    with zipfile.ZipFile(info.value.path()) as z:
        exported = z.read("Mutator-wider.fontra/glyphs/A^1.json")
    assert exported != (FIXTURE / "glyphs" / "A^1.json").read_bytes()  # the edit

    # Merged into main: main has the edit.
    page.locator(".hive-branch").click()
    page.get_by_text("Merge into main…").click()
    page.get_by_role("button", name="Merge", exact=True).click()
    page.get_by_role("button", name="Open main").click(timeout=30000)
    page.wait_for_function(
        "!new URLSearchParams(location.search).get('project').includes('@')",
        timeout=30000,
    )
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    page.wait_for_function(f"({LAYERS_A}).then(l => l === {json.dumps(edited)})")
    assert not errors
    context.close()


def test_compiled_fonts_are_exported_from_the_browser(server, browser, tmp_path):
    """File › Export as TrueType and WOFF2: fontc in WebAssembly on the
    .fontra package, WOFF2 by fontTools in Pyodide; on the demo and on a
    font kept in the browser."""
    _needsHiveInTheBrowser()
    if not FONTC_WASM:
        pytest.skip("$HIVE_TEST_FONTC_WASM is not set (tools/build-fontc-wasm.sh)")
    from fontTools.ttLib import TTFont

    context = browser.new_context(
        viewport={"width": 1400, "height": 850}, accept_downloads=True
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.A")

    with page.expect_download(timeout=120000) as info:
        _exportAs(page, "TrueType (*.ttf)")
    # The demo has a discrete axis (italic): one variable font per value.
    assert info.value.suggested_filename == "MutatorSans.ttf.zip"
    with zipfile.ZipFile(info.value.path()) as z:
        assert sorted(z.namelist()) == ["MutatorSans-Italic.ttf", "MutatorSans-Upright.ttf"]
        upright = z.read("MutatorSans-Upright.ttf")
    font = TTFont(io.BytesIO(upright))
    assert [a.axisTag for a in font["fvar"].axes] == ["wght", "wdth"]
    assert font["cmap"].getBestCmap()[ord("A")] == "A"

    archive = tmp_path / "Mutator.fontra.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(FIXTURE.rglob("*")):
            if path.is_file():
                z.write(path, "Mutator.fontra/" + path.relative_to(FIXTURE).as_posix())
    page.get_by_role("button", name="Your fonts").click()
    page.locator(".hive-try-panel input[accept]").set_input_files(str(archive))
    page.wait_for_function(
        "new URLSearchParams(location.search).get('project').startsWith('local:')",
        timeout=90000,
    )
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    with page.expect_download(timeout=120000) as info:
        _exportAs(page, "Webfont (*.woff2)")
    assert info.value.suggested_filename == "Mutator.woff2"
    data = pathlib.Path(info.value.path()).read_bytes()
    assert data[:4] == b"wOF2" and len(data) > 1000
    assert not errors
    context.close()


def test_the_demo_is_kept_with_its_edits(server, browser):
    _needsHiveInTheBrowser()
    context = browser.new_context(viewport={"width": 1400, "height": 850})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.A")
    page.evaluate(EDIT_A, 40)
    edited = page.evaluate(LAYERS_A)
    page.get_by_role("button", name="Keep a copy and try Hive").click()
    page.wait_for_url("**project=local*", timeout=90000)
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    assert page.evaluate(LAYERS_A) == edited
    assert page.locator(".hive-try .name").inner_text() == "MutatorSans"
    assert not errors
    context.close()
