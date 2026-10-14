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
        if path == "/bare.html" or path.startswith("/mock/"):
            # /mock/<view>.html: a bare page at the address of one of
            # Fontra's pages, for the tours' moves between pages.
            return self._send(BARE_PAGE.encode(), "text/html")
        if path == "/try":
            from fontra_hive.projectmanager import injectTryMeta

            html = (CLIENT_DIR / "try" / "try-start.html").read_text(encoding="utf-8")
            pyodide = "/hive/pyodide/" if PYODIDE_DIR else None
            fontc = "/hive/try/fontc.wasm" if FONTC_WASM else None
            return self._send(injectTryMeta(html, pyodide, fontc).encode(), "text/html")
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


START_STUBS = """(() => {
  window.__hiveTryNoAutoStart = true;
  window.__kept = [];
  window.__stubStore = (available, projects) => ({
    available: () => available,
    list: async () => projects,
  });
  window.__stubAPI = {
    saveCopy: async (name, progress) => {
      window.__kept.push(name);
      progress("Loading Python…");
      return "abcdef012345";
    },
  };
})()"""


def test_the_start_page_opens_the_latest_font_or_keeps_the_example(server, browser):
    """/try: the font opened most recently in this browser; on a first visit
    the example is kept here first, under a free name; where nothing can be
    kept, the plain demo."""
    context = browser.new_context()
    context.add_init_script(START_STUBS)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def start(available, projects):
        page.goto(f"{server}/try")
        page.wait_for_function("() => !!window.hiveTryStart")
        assert page.locator("#status").inner_text() == "Starting…"
        # [what start() did, the names it kept]: read before the page leaves.
        return page.evaluate(
            "([available, projects]) => window.hiveTryStart("
            "{ store: window.__stubStore(available, projects), tryAPI: window.__stubAPI })"
            ".then((done) => [done, window.__kept])",
            [available, projects],
        )

    # Nothing can be kept: the demo, in JavaScript, nothing saved.
    assert start(False, []) == ["demo", []]
    page.wait_for_url("**/editor.html?project=demo%3AMutatorSans*")

    # A first visit: the example is kept, and opened with its sample text.
    assert start(True, []) == ["local:abcdef012345", ["MutatorSans"]]
    page.wait_for_url("**/editor.html?project=local%3Aabcdef012345&text=*")
    assert "HAMBURGEFONSTIV" in urllib.parse.unquote(page.url)

    # Fonts kept here: the one last opened, else the one last changed.
    projects = [
        {"id": "111111111111", "name": "Old", "modified": "2026-10-01T10:00:00Z"},
        {"id": "222222222222", "name": "Recent", "modified": "2026-10-02T10:00:00Z"},
        {
            "id": "333333333333",
            "name": "Opened",
            "modified": "2026-09-01T10:00:00Z",
            "opened": "2026-10-03T10:00:00Z",
        },
    ]
    assert start(True, projects) == ["local:333333333333", []]
    page.wait_for_url("**/editor.html?project=local%3A333333333333")
    assert start(True, projects[:2]) == ["local:222222222222", []]
    page.wait_for_url("**/editor.html?project=local%3A222222222222")

    # A name already taken by a font kept here (the panel's "Example font"):
    # the next free one.
    assert page.evaluate(
        "() => import('/hive/try/try-common.js').then((m) => ["
        " m.freeName('MutatorSans', []),"
        " m.freeName('MutatorSans', [{ name: 'MutatorSans' }, { name: 'MutatorSans 2' }]),"
        " m.latestProject([]) ])"
    ) == ["MutatorSans", "MutatorSans 3", None]
    assert not errors
    context.close()


def test_the_start_page_fails_towards_the_demo(server, browser):
    context = browser.new_context()
    context.add_init_script(START_STUBS)
    page = context.new_page()
    page.goto(f"{server}/try")
    page.wait_for_function("() => !!window.hiveTryStart")
    assert page.evaluate(
        "() => window.hiveTryStart({ store: window.__stubStore(true, []),"
        " tryAPI: { saveCopy: async () => { throw new Error('no Python'); } } })"
    ) == "error"
    assert "no Python" in page.locator("#status").inner_text()
    assert page.locator("#actions a").first.get_attribute("href").startswith(
        "/editor.html?project=demo%3AMutatorSans"
    )
    context.close()


def test_the_start_page_keeps_the_example_and_comes_back_to_it(server, browser):
    """A first visit to /try keeps MutatorSans in the browser and opens it
    with Hive; the next visit opens the same font; "Example font" in the
    panel makes a second copy under a free name."""
    _needsHiveInTheBrowser()
    context = browser.new_context(viewport={"width": 1400, "height": 850})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/try")
    page.wait_for_url("**project=local*", timeout=120000)
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    first = urllib.parse.parse_qs(urllib.parse.urlsplit(page.url).query)
    assert first["project"][0].startswith("local:") and first["text"] == ['"HAMBURGEFONSTIV"']
    assert page.locator(".hive-try .name").inner_text() == "MutatorSans"
    assert page.locator(".hive-try img").get_attribute("src") == "/hive/icons/hive-icon.png"
    assert page.get_by_role("button", name="Keep a copy and try Hive").count() == 0

    page.goto(f"{server}/try")
    page.wait_for_url("**project=local*", timeout=60000)
    again = urllib.parse.parse_qs(urllib.parse.urlsplit(page.url).query)
    assert again["project"] == first["project"]
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )

    page.get_by_role("button", name="Your fonts").click()
    page.get_by_role("button", name="Example font").click()
    page.wait_for_url("**project=local*", timeout=90000)
    page.wait_for_function(
        "window.editorController?.fontController?.glyphMap?.A", timeout=90000
    )
    second = urllib.parse.parse_qs(urllib.parse.urlsplit(page.url).query)
    assert second["project"] != first["project"]
    page.wait_for_function(
        "document.querySelector('.hive-try .name')?.textContent === 'MutatorSans 2'"
    )
    assert not errors
    context.close()


def test_a_new_font_is_empty_with_a_glyph_set(server, browser):
    """"New font…": an empty font as Fontra's own New font makes one (one
    source, Regular, Fontra's line metrics, no axis, no glyph), with the
    chosen Google Fonts glyph set as the project's, written as a .fontra."""
    page = browser.new_page()
    page.goto(f"{server}/bare.html")
    result = page.evaluate(
        """async () => {
          const { emptyFont, GLYPH_SETS, DEFAULT_GLYPH_SET } = await import("/hive/try/try-common.js");
          const Format = window.HiveTryFormat;
          const files = Format.writePackage(emptyFont("Hello"));
          const kernel = Format.writePackage(emptyFont("K", "latin-kernel"));
          const bare = Format.writePackage(emptyFont("B", "none"));
          return {
            keys: [...files.keys()],
            fontData: JSON.parse(files.get("font-data.json")),
            glyphInfo: files.get("glyph-info.csv"),
            kernelSet: JSON.parse(kernel.get("font-data.json")).customData,
            bareSet: JSON.parse(bare.get("font-data.json")).customData,
            sets: GLYPH_SETS.map((s) => s.key),
            defaultSet: DEFAULT_GLYPH_SET,
          };
        }"""
    )
    assert sorted(result["keys"]) == ["font-data.json", "glyph-info.csv"]
    data = result["fontData"]
    assert data["fontInfo"] == {"familyName": "Hello"}
    assert data["axes"] == {"axes": []}
    assert "unitsPerEm" not in data  # 1000, Fontra's default
    (source,) = data["sources"].values()
    assert source["name"] == "Regular" and source["location"] == {}
    assert source["lineMetricsHorizontalLayout"]["ascender"] == {"value": 750, "zone": 16}
    assert source["lineMetricsHorizontalLayout"]["descender"] == {"value": -250, "zone": -16}
    (glyphSet,) = data["customData"]["fontra.projectGlyphSets"]
    assert glyphSet == {
        "name": "GF Latin Core",
        "url": "https://cdn.jsdelivr.net/gh/googlefonts/glyphsets/data/results/txt/nice-names/GF_Latin_Core.txt",
        "dataFormat": "glyph-names",
        "commentChars": "#",
    }
    assert result["glyphInfo"].splitlines() == ["glyph name;code points"]
    assert result["kernelSet"]["fontra.projectGlyphSets"][0]["name"] == "GF Latin Kernel"
    assert result["bareSet"] is None
    assert result["sets"] == ["latin-core", "latin-kernel", "none"]
    assert result["defaultSet"] == "latin-core"
    page.close()


def test_a_new_font_opens_in_the_editor(server, browser):
    _needsHiveInTheBrowser()
    context = browser.new_context(viewport={"width": 1400, "height": 850})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/editor.html?{TRY_PAGE_QUERY}")
    page.wait_for_function("window.editorController?.fontController?.glyphMap?.A")
    page.get_by_role("button", name="Your fonts").click()
    page.get_by_role("button", name="New font…").click()
    page.locator(".hive-try-panel input[name=name]").fill("Hello")
    page.get_by_role("button", name="Create").click()
    page.wait_for_url("**project=local*", timeout=90000)
    page.wait_for_function("() => !!window.editorController?.fontController", timeout=90000)
    page.wait_for_function(
        "document.querySelector('.hive-try .name')?.textContent === 'Hello'"
    )
    assert "HAMBURGEFONSTIV" in urllib.parse.unquote(page.url)
    font = page.evaluate(
        """() => {
          const fc = window.editorController.fontController;
          return {
            glyphs: Object.keys(fc.glyphMap),
            sources: Object.values(fc.sources).map((s) => s.name),
            axes: fc.axes.axes.length,
            glyphSets: fc.customData["fontra.projectGlyphSets"].map((s) => s.name),
          };
        }"""
    )
    assert font == {"glyphs": [], "sources": ["Regular"], "axes": 0, "glyphSets": ["GF Latin Core"]}
    assert not errors
    context.close()


# The parts of the editor's page the tour points at, without the editor.
MOCK_EDITOR = """() => {
  const add = (html) => document.body.insertAdjacentHTML("beforeend", html);
  add('<canvas id="edit-canvas" style="position:fixed;left:200px;top:40px;width:800px;height:600px"></canvas>');
  add('<div id="edit-tools" style="position:fixed;left:400px;top:50px;width:300px;height:30px"><div class="tool-button"></div></div>');
  for (const [name, left] of [["text-entry", 10], ["glyph-search", 10], ["designspace-navigation", 10],
                              ["hive-history", 1300], ["hive-comments", 1300]]) {
    add(`<div class="sidebar-tab" data-sidebar-name="${name}" style="position:fixed;left:${left}px;top:${100 + left % 7 * 40}px;width:30px;height:30px"></div>`);
  }
  add('<button class="hive-branch" style="position:fixed;left:900px;top:5px;width:80px;height:24px">main</button>');
  add('<div class="hive-try" style="position:fixed;left:400px;bottom:14px;width:500px;height:40px"><button class="fonts">Your fonts</button></div>');
  window.__opened = [];
  window.editorController = {
    fontController: {},
    toggleSidebar: (name) => {
      window.__opened.push(name);
      document.querySelector(`.sidebar-tab[data-sidebar-name="${name}"]`).classList.add("selected");
    },
  };
}"""


def test_the_tour_goes_round_the_editor(server, browser):
    """The guided tour: offered once, a bubble beside each part of the
    interface (a step whose part is absent is skipped), Back and Next, the
    panels opened as it goes, remembered when done or skipped."""
    context = browser.new_context(viewport={"width": 1400, "height": 850})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/bare.html")
    page.evaluate(
        "() => import('/hive/try/try-tour.js').then((m) => { window.__tour = m; })"
    )
    page.wait_for_function("() => !!window.__tour")
    # Not offered while the editor is still loading (or never comes up).
    assert page.evaluate("() => window.__tour.offerTour('editor', 600)") is None
    assert page.locator(".hive-tour").count() == 0
    page.evaluate(MOCK_EDITOR)
    # Offered on a first visit: the welcome bubble, centred, no ring.
    page.evaluate("() => window.__tour.offerTour('editor').then((t) => { window.__t = t; })")
    page.wait_for_selector(".hive-tour")
    bubble = page.locator(".hive-tour")
    assert bubble.locator("h3").inner_text() == "Welcome to Fontra"
    assert page.locator(".hive-tour-ring").is_hidden()
    assert bubble.get_by_role("button", name="Take the tour").count() == 1
    assert bubble.get_by_role("button", name="Not now").count() == 1
    box = bubble.bounding_box()
    assert abs(box["x"] + box["width"] / 2 - 700) < 2
    assert bubble.locator(".count").count() == 0

    steps = page.evaluate("() => window.__t.steps.map((s) => s.id)")
    assert steps == [
        "welcome", "canvas", "text", "tools", "designspace", "history", "branches", "comments", "bar",
    ]

    # The canvas: the bubble inside it, near the bottom, no ring.
    bubble.get_by_role("button", name="Take the tour").click()
    assert bubble.locator("h3").inner_text() == "The canvas"
    assert bubble.locator(".count").inner_text() == "1/8"
    assert page.locator(".hive-tour-ring").is_hidden()
    box = bubble.bounding_box()
    assert 200 < box["x"] and box["x"] + box["width"] < 1000 and box["y"] + box["height"] < 640
    assert bubble.locator("a.learn").get_attribute("href").startswith("https://docs.fontra.xyz/")
    assert bubble.get_by_role("button", name="Back").count() == 0  # right after the welcome

    # Preview text: the panel is opened, the ring is on its tab, the bubble to its right.
    bubble.get_by_role("button", name="Next").click()
    assert bubble.locator("h3").inner_text() == "Preview text"
    assert page.evaluate("window.__opened") == ["text-entry"]
    ring = page.locator(".hive-tour-ring").bounding_box()
    assert abs(ring["x"] - (10 - 4)) < 1 and abs(ring["width"] - 38) < 1
    assert bubble.get_attribute("data-side") == "right"
    assert bubble.bounding_box()["x"] > 40

    # Back, then on to the end; the keyboard works while the bubble has the focus.
    bubble.get_by_role("button", name="Back").click()
    assert bubble.locator("h3").inner_text() == "The canvas"
    page.keyboard.press("ArrowRight")
    assert bubble.locator("h3").inner_text() == "Preview text"
    page.keyboard.press("ArrowRight")
    assert bubble.locator("h3").inner_text() == "Tools"
    assert bubble.get_attribute("data-side") == "bottom"
    for _ in range(4):
        page.keyboard.press("ArrowRight")
    assert bubble.locator("h3").inner_text() == "Comments"
    assert page.evaluate("window.__opened") == [
        "text-entry", "designspace-navigation", "hive-history", "hive-comments",
    ]
    # Opening a panel already open does nothing.
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("window.__opened.length") == 4
    assert bubble.locator("p.teams").inner_text().startswith("Fontra Hive for teams: ")
    # Mid-way, the page may reload (a branch switch): the tour goes on from
    # its step, not from the welcome.
    assert page.evaluate("JSON.parse(localStorage.getItem('hive.try.tour'))") == {
        "editor": {"step": "comments"}
    }
    page.keyboard.press("ArrowLeft")
    assert bubble.locator("h3").inner_text() == "Branches"
    assert bubble.get_attribute("data-side") == "left"  # its menu drops below the pill
    page.evaluate("() => window.__t.end(null)")
    assert page.locator(".hive-tour").count() == 0
    page.evaluate("() => window.__tour.offerTour('editor').then((t) => { window.__t = t; })")
    page.wait_for_selector(".hive-tour")
    assert bubble.locator("h3").inner_text() == "Branches"
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert bubble.locator("h3").inner_text() == "Your fonts"
    assert bubble.get_attribute("data-side") == "top"
    assert bubble.locator(".count").inner_text() == "8/8"
    assert bubble.get_by_role("button", name="Skip").count() == 0
    bubble.get_by_role("button", name="Done").click()
    assert page.locator(".hive-tour").count() == 0
    assert page.locator(".hive-tour-ring").count() == 0
    assert page.evaluate("localStorage.getItem('hive.try.tour')") == '{"editor":"done"}'

    # Not offered again; the ? starts it again all the same; Escape skips.
    assert page.evaluate("() => window.__tour.offerTour('editor')") is None
    page.evaluate("() => window.__tour.startTour('editor')")
    assert page.locator(".hive-tour h3").inner_text() == "Welcome to Fontra"
    page.keyboard.press("Escape")
    assert page.locator(".hive-tour").count() == 0
    assert page.evaluate("localStorage.getItem('hive.try.tour')") == '{"editor":"skipped"}'

    # A part of the interface that is absent: its step is left out.
    page.evaluate("() => document.querySelector('.sidebar-tab[data-sidebar-name=\"hive-comments\"]').remove()")
    page.evaluate("() => { window.__t = window.__tour.startTour('editor'); }")
    assert page.evaluate("() => window.__t.steps.map((s) => s.id)") == [
        "welcome", "canvas", "text", "tools", "designspace", "history", "branches", "bar",
    ]
    page.evaluate("() => window.__t.end('skipped')")
    assert not errors
    context.close()


def test_the_tour_is_offered_in_the_editor(server, browser):
    """On a font kept in the browser, the welcome bubble shows once the
    editor and Hive's panels are up; the ? in the bar brings it back."""
    _needsHiveInTheBrowser()
    context = browser.new_context(viewport={"width": 1400, "height": 850})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/try")
    page.wait_for_url("**project=local*", timeout=120000)
    page.wait_for_selector(".hive-tour", timeout=90000)
    assert page.locator(".hive-tour h3").inner_text() == "Welcome to Fontra"
    page.get_by_role("button", name="Take the tour").click()
    page.get_by_role("button", name="Next").click()
    assert page.locator(".hive-tour h3").inner_text() == "Preview text"
    assert page.locator('.sidebar-tab[data-sidebar-name="text-entry"]').get_attribute("class").find("selected") >= 0
    page.keyboard.press("Escape")
    assert page.locator(".hive-tour").count() == 0
    page.reload()
    page.wait_for_function("() => !!window.editorController?.fontController", timeout=90000)
    page.wait_for_selector(".hive-branch", timeout=30000)
    page.wait_for_timeout(1500)
    assert page.locator(".hive-tour").count() == 0
    page.get_by_role("button", name="Tutorials").click()
    page.locator('.hive-tour-menu .row[data-tour="editor"]').click()
    assert page.locator(".hive-tour h3").inner_text() == "Welcome to Fontra"
    assert not errors
    context.close()


# The editor's other parts the lessons point at, and a font to watch: Fontra's
# FontController as the steps' `until` read it (glyphMap, axes, sources,
# getGlyph), filled by the test as the visitor would by drawing.
MOCK_LESSON = """() => {
  const add = (html) => document.body.insertAdjacentHTML("beforeend", html);
  const tools = document.getElementById("edit-tools");
  for (const tool of ["pointer-tools", "pen-tool", "shape-tool", "metrics-tool"]) {
    tools.insertAdjacentHTML("beforeend", `<div class="tool-button" data-tool="${tool}" style="display:inline-block;width:30px;height:30px"></div>`);
  }
  for (const [name, top] of [["selection-info", 300], ["selection-transformation", 340]]) {
    add(`<div class="sidebar-tab" data-sidebar-name="${name}" style="position:fixed;left:1300px;top:${top}px;width:30px;height:30px"></div>`);
  }
  window.__glyphs = {};
  const fc = window.editorController.fontController;
  Object.assign(fc, {
    glyphMap: {},
    axes: { axes: [] },
    sources: { r: { name: "Regular" } },
    getGlyph: async (name) => window.__glyphs[name] || null,
  });
  // A glyph of rectangles [x, y, w, h], counter-clockwise, or clockwise when w < 0.
  window.__draw = (name, rects, xAdvance = 500, sources = 1) => {
    const coordinates = [];
    const contourInfo = [];
    for (const [x, y, w, h] of rects) {
      const pts = w > 0 ? [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
                        : [[x, y], [x, y + h], [x - w, y + h], [x - w, y]];
      for (const [px, py] of pts) coordinates.push(px, py);
      contourInfo.push({ endPoint: coordinates.length / 2 - 1, isClosed: true });
    }
    fc.glyphMap[name] = [name.codePointAt(0)];
    const layer = { glyph: { xAdvance, path: { coordinates, contourInfo } } };
    window.__glyphs[name] = {
      sources: Array.from({ length: sources }, (_, i) => ({ layerName: "L" + i })),
      layers: Object.fromEntries(Array.from({ length: sources }, (_, i) => ["L" + i, layer])),
    };
  };
}"""

# Font Info as the tours see it: its panels, one shown at a time.
MOCK_FONTINFO = """() => {
  const container = document.createElement("div");
  container.id = "multi-panel-panel-container";
  container.style = "position:fixed;left:200px;top:40px;width:900px;height:700px";
  for (const id of ["font-info-panel", "axes-panel", "sources-panel", "opentype-feature-code-panel"]) {
    const panel = document.createElement("div");
    panel.id = id;
    panel.className = "multi-panel-panel";
    panel.style = "width:100%;height:100%";
    panel.hidden = "#" + id !== (location.hash || "#font-info-panel");
    container.append(panel);
  }
  document.body.append(container);
  window.__selected = [];
  window.fontInfoController = {
    fontController: { glyphMap: {}, axes: { axes: [] }, sources: { r: {} } },
    multiPanelController: {
      selectPanel: (id) => {
        window.__selected.push(id);
        for (const panel of container.children) panel.hidden = panel.id !== id;
        history.replaceState({}, "", "#" + id);
      },
    },
  };
}"""

TOUR_MODULES = """() => Promise.all([
  import('/hive/try/try-tour.js'), import('/hive/try/try-tour-steps.js'), import('/hive/try/try-tour-menu.js'),
]).then(([tour, steps, menu]) => { window.__tour = tour; window.__steps = steps; window.__menu = menu; })"""

LOCAL = "project=local%3A0123456789ab"


def _tourPage(browser, server, path="/mock/editor.html", query=LOCAL, viewport=(1400, 850)):
    context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}{path}?{query}")
    page.evaluate(TOUR_MODULES)
    page.wait_for_function("() => !!window.__menu")
    return context, page, errors


# The words of every bubble: one idea, 25 to 45 words (a few short ones of
# the first tour stray a little).
def test_every_tour_follows_the_rules(server, browser):
    context, page, errors = _tourPage(browser, server)
    tours = page.evaluate(
        """() => Object.entries(window.__steps.TOURS).map(([name, tour]) => ({
          name, title: tour.title, kind: tour.kind, minutes: tour.minutes, about: tour.about || "",
          steps: tour.steps.map((s) => ({
            id: s.id, title: s.title, words: s.text.trim().split(/\\s+/).length, anchor: s.anchor || "",
            placement: s.placement || "", view: s.view || tour.view || "editor", hash: s.hash || "",
            until: typeof s.until === "function", learn: s.learn || "", teams: s.teams || "",
          })),
        }))"""
    )
    assert [t["name"] for t in tours] == ["editor", "first-font", "from-glyphs", "from-robofont"]
    ids = {t["name"]: [s["id"] for s in t["steps"]] for t in tours}
    assert ids["first-font"] == [
        "welcome", "new-font", "create-h", "rectangles", "exact", "remove-overlaps", "width-h",
        "create-o", "ellipses", "width-o", "axis", "black-source", "draw-black", "slider", "export",
    ]
    assert ids["from-glyphs"] == [
        "welcome", "open", "text-view", "masters", "smart-components", "palette", "kerning",
        "instances", "features",
    ]
    assert ids["from-robofont"] == [
        "welcome", "open", "glyph-window", "designspace", "inspector", "metrics", "font-window",
        "no-scripting",
    ]
    placements = {"", "right", "left", "top", "bottom", "inside-top", "inside-bottom", "inside-right"}
    for tour in tours:
        assert tour["kind"] in ("tour", "lesson") and tour["minutes"] > 0 and tour["about"]
        assert len(set(s["id"] for s in tour["steps"])) == len(tour["steps"])
        assert tour["steps"][0]["id"] == "welcome"
        for step in tour["steps"]:
            where = f"{tour['name']}/{step['id']}"
            assert 15 <= step["words"] <= 48, (where, step["words"])
            assert step["placement"] in placements, where
            assert step["view"] in ("editor", "fontoverview", "fontinfo", "applicationsettings"), where
            assert not step["hash"] or step["view"] in ("fontinfo", "applicationsettings"), where
            # Anchors by ids, classes and data-* attributes, never by words.
            assert '"' not in step["anchor"].replace('[data-sidebar-name="', "").replace('[data-tool="', "").replace('"]', ""), where
            assert not step["learn"] or step["learn"].startswith("https://docs.fontra.xyz/"), where
            assert "Fontra Hive" not in step["teams"], where
    lesson = next(t for t in tours if t["name"] == "first-font")
    assert lesson["kind"] == "lesson"
    assert [s["id"] for s in lesson["steps"] if not s["until"]] == ["welcome", "slider"]
    assert not errors
    context.close()


def test_a_lesson_waits_until_each_step_is_done(server, browser):
    """A lesson's step waits: "I did it" goes on anyway; once the thing is
    done, a check and Next. The steps read Fontra's font as it changes."""
    context, page, errors = _tourPage(browser, server)
    page.evaluate(MOCK_EDITOR)
    page.evaluate(MOCK_LESSON)
    page.evaluate("() => { window.__t = window.__tour.startTour('first-font'); }")
    bubble = page.locator(".hive-tour")
    assert bubble.locator("h3").inner_text().startswith("Your first font")
    assert page.evaluate("() => window.__t.steps.length") == 15  # none left out: they wait
    next_ = bubble.locator("button.next")

    # An empty font is there: done at once.
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("New font")
    page.wait_for_function("document.querySelector('.hive-tour').classList.contains('done')")
    assert next_.inner_text() == "Next"
    ring = page.locator(".hive-tour-ring").bounding_box()
    assert ring and ring["y"] > 700  # around the bar's Your fonts

    # The H: not yet, then created.
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("Create the H")
    page.wait_for_timeout(400)
    assert next_.inner_text() == "I did it"
    assert "done" not in bubble.get_attribute("class")
    page.evaluate("() => window.__draw('H', [])")
    page.wait_for_function("document.querySelector('.hive-tour button.next').textContent === 'Next'")
    assert "done" in bubble.get_attribute("class")
    assert bubble.locator("h3 .check").is_visible()

    # Three rectangles, rough; then exact; then fused; then the width.
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("Shape Tool")
    assert bubble.get_attribute("data-side") == "right"  # its menu drops below the tool
    assert bubble.locator("p.more a").inner_text() == "Learn more ↗"
    page.evaluate("() => window.__draw('H', [[50, 0, 100, 690], [400, 0, 120, 700], [50, 300, 480, 90]])")
    page.wait_for_function("document.querySelector('.hive-tour').classList.contains('done')")
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("Transformation")
    assert page.evaluate("window.__opened").count("selection-transformation") == 1
    page.wait_for_timeout(400)
    assert next_.inner_text() == "I did it"
    page.evaluate("() => window.__draw('H', [[60, 0, 110, 700], [430, 0, 110, 700], [60, 300, 480, 100]])")
    page.wait_for_function("document.querySelector('.hive-tour').classList.contains('done')")
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("Remove overlaps")
    page.evaluate("() => window.__draw('H', [[60, 0, 480, 700]])")
    page.wait_for_function("document.querySelector('.hive-tour').classList.contains('done')")
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("Sidebearings")
    page.wait_for_timeout(400)
    assert next_.inner_text() == "I did it"
    page.evaluate("() => window.__draw('H', [[60, 0, 480, 700]], 600)")
    page.wait_for_function("document.querySelector('.hive-tour').classList.contains('done')")

    # The o: created; two ovals turning the same way are not yet a counter,
    # the inner one drawn the other way round is.
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("Preview text")
    page.evaluate("() => window.__draw('o', [])")
    page.wait_for_function("document.querySelector('.hive-tour').classList.contains('done')")
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("Ellipse Tool")
    page.evaluate("() => window.__draw('o', [[40, -10, 420, 520], [140, 90, 220, 320]])")
    page.wait_for_timeout(400)
    assert next_.inner_text() == "I did it"
    page.evaluate("() => window.__draw('o', [[40, -10, 420, 520], [140, 90, -220, 320]])")
    page.wait_for_function("document.querySelector('.hive-tour').classList.contains('done')")
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("Sidebearings")
    page.evaluate("() => window.__draw('o', [[40, -10, 420, 520], [140, 90, -220, 320]], 500)")
    page.wait_for_function("document.querySelector('.hive-tour').classList.contains('done')")

    # The axis is in Font Info: from the editor, a button opens it.
    next_.click()
    assert bubble.locator("h3").inner_text().startswith("Axes")
    assert next_.inner_text() == "Open Font Info"
    # Not the step's text yet (it is read on its page), nor its link.
    assert bubble.locator("p").first.inner_text() == "The next step is in Font Info: open it to go on."
    assert bubble.locator("p.more").count() == 0
    # The buttons stay inside the bubble.
    box = bubble.bounding_box()
    row = bubble.locator(".row button.next").bounding_box()
    assert row["x"] + row["width"] <= box["x"] + box["width"]
    assert page.locator(".hive-tour-ring").is_hidden()
    assert bubble.get_by_role("button", name="Back").count() == 1
    # "I did it" was never needed to go back and forth.
    bubble.get_by_role("button", name="Back").click()
    assert bubble.locator("h3").inner_text().startswith("Sidebearings")
    page.keyboard.press("Escape")
    assert page.evaluate("JSON.parse(localStorage.getItem('hive.try.tour'))") == {"first-font": "skipped"}
    assert page.evaluate("localStorage.getItem('hive.try.tour.now')") is None
    assert not errors
    context.close()


def test_a_tour_goes_on_in_another_page(server, browser):
    """A step on another of Fontra's pages: the bubble opens it the way
    Fontra does (Font Info in a window named after the font), the tour goes
    on there, and the tab left behind has no bubble any more."""
    context, page, errors = _tourPage(browser, server)
    page.evaluate(MOCK_EDITOR)
    page.evaluate(MOCK_LESSON)
    page.evaluate("() => { window.__t = window.__tour.startTour('from-glyphs', 'kerning'); }")
    bubble = page.locator(".hive-tour")
    assert bubble.locator("h3").inner_text().startswith("Kerning Tool")
    page.keyboard.press("ArrowRight")
    assert bubble.locator("h3").inner_text().startswith("Axes")
    page.keyboard.press("ArrowRight")  # not from here: the page must be opened
    assert bubble.locator("h3").inner_text().startswith("Axes")
    with context.expect_page() as opened:
        bubble.get_by_role("button", name="Open Font Info").click()
    info = opened.value
    info.on("pageerror", lambda error: errors.append(str(error)))
    assert page.locator(".hive-tour").count() == 0
    now = page.evaluate("JSON.parse(localStorage.getItem('hive.try.tour.now'))")
    assert now["tour"] == "from-glyphs" and now["step"] == "instances" and now["tab"] is None
    assert now["view"] == "fontinfo"
    info.wait_for_load_state()
    url = urllib.parse.urlsplit(info.url)
    assert url.path == "/mock/fontinfo.html" and url.fragment == "axes-panel"
    assert urllib.parse.parse_qs(url.query)["project"] == ["local:0123456789ab"]
    assert info.evaluate("window.name") == "fontra.fontinfo.local:0123456789ab"
    info.evaluate(MOCK_FONTINFO)
    info.evaluate(TOUR_MODULES)
    info.wait_for_function("() => !!window.__menu")
    info.evaluate("() => window.__tour.tourOnLoad(5000).then((t) => { window.__t = t; })")
    info.wait_for_selector(".hive-tour")
    there = info.locator(".hive-tour")
    assert there.locator("h3").inner_text().startswith("Axes")
    assert there.locator("button.next").inner_text() == "Next"
    tab = info.evaluate("JSON.parse(localStorage.getItem('hive.try.tour.now')).tab")
    assert tab and tab != page.evaluate("window.__tour.TAB")
    # Its next step is on the same page, another panel: shown there.
    there.locator("button.next").click()
    assert there.locator("h3").inner_text().startswith("OpenType Feature Code")
    assert info.evaluate("window.__selected") == ["opentype-feature-code-panel"]
    assert info.locator("#opentype-feature-code-panel").is_visible()
    # No Back to a step of another page: that is the other tab's.
    there.get_by_role("button", name="Back").click()
    assert there.locator("h3").inner_text().startswith("Axes")
    assert there.get_by_role("button", name="Back").count() == 0
    # Back to the editor: its own tab, as it was (not a new one), takes the
    # tour back; this one lets it go.
    info.evaluate("() => window.__tour.startTour('first-font', 'black-source')")
    assert there.locator("h3").inner_text().startswith("Sources")
    there.locator("button.next").click()  # "I did it"
    assert there.locator("h3").inner_text().startswith("Designspace")
    assert there.locator("button.next").inner_text() == "Back to the editor"
    assert there.locator("p").first.inner_text() == "The next step is in the editor: open it to go on."
    assert page.evaluate("window.name") == "hive.try.editor.local:0123456789ab"
    page.evaluate("() => window.__marker = 'still here'")
    there.locator("button.next").click()
    page.wait_for_selector(".hive-tour")
    assert page.locator(".hive-tour h3").inner_text().startswith("Designspace")
    assert page.evaluate("window.__marker") == "still here"
    assert len(context.pages) == 2
    info.wait_for_timeout(300)
    assert info.locator(".hive-tour").count() == 0
    # Ending it anywhere ends it everywhere.
    page.keyboard.press("Escape")
    assert info.evaluate("localStorage.getItem('hive.try.tour.now')") is None
    assert not errors
    context.close()


def test_another_tab_takes_the_tour_over(server, browser):
    """Two tabs: the one that shows the tour now has the only bubble."""
    context, page, errors = _tourPage(browser, server)
    page.evaluate(MOCK_EDITOR)
    page.evaluate("() => window.__tour.startTour('editor', 'tools')")
    other = context.new_page()
    other.goto(f"{server}/mock/editor.html?{LOCAL}%40bold")  # same font, another branch
    other.evaluate(TOUR_MODULES)
    other.wait_for_function("() => !!window.__menu")
    other.evaluate(MOCK_EDITOR)
    # Not this tab's: it was shown in the first one.
    assert other.evaluate("() => window.__tour.tourOnLoad(1000)") is None
    other.evaluate("() => window.__tour.startTour('editor', 'designspace')")
    page.wait_for_function("() => !document.querySelector('.hive-tour')")
    assert other.locator(".hive-tour h3").inner_text() == "Designspace"
    assert not errors
    context.close()


def test_the_menu_lists_the_tours(server, browser):
    """The ? menu: every tour, its length, where you are in it; a tour going
    on resumes at its step; Start over forgets them all."""
    context, page, errors = _tourPage(browser, server)
    page.evaluate(MOCK_EDITOR)
    page.evaluate(MOCK_LESSON)
    page.evaluate(
        """() => localStorage.setItem('hive.try.tour', JSON.stringify(
             {editor: 'done', 'first-font': {step: 'width-h'}, 'from-glyphs': 'skipped'}))"""
    )
    page.evaluate(
        """() => { const b = document.createElement('button'); b.id = 'help';
                   b.style = 'position:fixed;right:20px;bottom:20px'; b.textContent = '?';
                   b.onclick = () => window.__menu.openTourMenu(b); document.body.append(b); }"""
    )
    page.click("#help")
    menu = page.locator(".hive-tour-menu")
    assert menu.locator(".title").inner_text() == "Tutorials"
    rows = menu.locator(".row[data-tour]")
    assert rows.count() == 4
    marks = [rows.nth(i).locator(".mark").inner_text() for i in range(4)]
    assert marks == ["✓", "●", "", ""]
    assert rows.nth(1).locator(".length").inner_text() == "lesson · 20 min"
    assert menu.get_by_role("menuitem", name="Documentation").get_attribute("href") == "https://docs.fontra.xyz"
    assert menu.get_by_role("menuitem", name="Fontra Hive for teams").count() == 1
    box = menu.bounding_box()
    assert box["y"] + box["height"] <= page.locator("#help").bounding_box()["y"]
    # The lesson going on: resumed at its step.
    rows.nth(1).click()
    assert menu.count() == 0
    assert page.locator(".hive-tour h3").inner_text().startswith("Sidebearings")
    # A tour done: from its start.
    page.click("#help")
    menu.locator('.row[data-tour="editor"]').click()
    assert page.locator(".hive-tour h3").inner_text() == "Welcome to Fontra"
    # Escape closes the menu; Start over forgets everything.
    page.click("#help")
    page.keyboard.press("Escape")
    assert menu.count() == 0
    page.click("#help")
    menu.locator('.row[data-action="start-over"]').click()
    assert page.evaluate("JSON.parse(localStorage.getItem('hive.try.tour'))") == {}
    assert page.locator(".hive-tour").count() == 0
    page.click("#help")
    assert [rows.nth(i).locator(".mark").inner_text() for i in range(4)] == ["", "", "", ""]
    page.keyboard.press("Escape")
    assert not errors
    context.close()


def test_a_tour_is_asked_for_in_the_address(server, browser):
    """/try?tour=<name> (the docs' "Try it"): the start page passes it on, and
    the page starts that tour from its start, even when done, once."""
    context, page, errors = _tourPage(browser, server, query=LOCAL + "&tour=from-robofont&text=%22H%22")
    page.evaluate("() => localStorage.setItem('hive.try.tour', JSON.stringify({'from-robofont': 'done'}))")
    page.evaluate(MOCK_EDITOR)
    page.evaluate("() => window.__tour.tourOnLoad(3000).then((t) => { window.__t = t; })")
    page.wait_for_selector(".hive-tour")
    assert page.locator(".hive-tour h3").inner_text() == "Coming from RoboFont"
    assert "tour=" not in page.url and "text=" in page.url
    # The start page passes ?tour= and ?new on to the editor.
    page.evaluate(
        "() => { window.__hiveTryNoAutoStart = true; return import('/hive/try/try-start.js').then((m) => { window.__start = m; }); }"
    )
    passed = page.evaluate(
        "() => window.__start.passOn('/editor.html?project=local%3Aabc&text=%22H%22', '?tour=first-font&new')"
    )
    assert passed == "/editor.html?project=local%3Aabc&text=%22H%22&tour=first-font&new"
    assert page.evaluate("() => window.__start.passOn('/editor.html?project=x', '')") == "/editor.html?project=x"
    assert not errors
    context.close()


def test_the_bubble_on_a_narrow_screen_and_in_shadow_roots(server, browser):
    """Below 900 px, the bubble sits at the bottom, the ring alone shows the
    place; anchors may go through open shadow roots (a >>> b)."""
    context, page, errors = _tourPage(browser, server, viewport=(800, 700))
    page.evaluate(MOCK_EDITOR)
    page.evaluate("() => window.__tour.startTour('editor', 'tools')")
    bubble = page.locator(".hive-tour")
    box = bubble.bounding_box()
    assert abs(box["x"] + box["width"] / 2 - 400) < 2
    assert box["y"] + box["height"] > 560
    assert page.locator(".hive-tour-ring").is_visible()
    assert bubble.locator(".arrow").is_hidden()
    found = page.evaluate(
        """() => { const host = document.createElement('div'); host.id = 'host';
                   host.attachShadow({mode: 'open'}).innerHTML = '<p class="deep">x</p>';
                   document.body.append(host);
                   const q = window.__tour.deepQuery;
                   return [q('#host >>> .deep')?.textContent, q('#host >>> .none'), q('#nothing >>> .deep'),
                           q('#edit-tools').id]; }"""
    )
    assert found == ["x", None, None, "edit-tools"]
    assert not errors
    context.close()
