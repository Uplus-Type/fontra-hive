"""Comments in the editor (``client/plugin/comments.js``), in a headless browser.

A fake editor (a real canvas in a container, a scene with one positioned
glyph) and a fake server kept in the page. Skipped without Playwright.
"""

import functools
import http.server
import os
import pathlib
import threading

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

PLUGIN_DIR = (
    pathlib.Path(__file__).parent.parent / "src" / "fontra_hive" / "client" / "plugin"
)
CHROMIUM = "/opt/pw-browsers/chromium"


@pytest.fixture(scope="module")
def browser_page():
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
            page = browser.new_page(viewport={"width": 1000, "height": 700})
            page.base = f"http://127.0.0.1:{server.server_address[1]}"
            yield page
            browser.close()
    finally:
        server.shutdown()


# The canvas: origin (100, 500), magnification 1, so glyph point (x, y) of
# the glyph at (0, 0) is at screen (100 + x, 500 - y) in the canvas.
SETUP = """
async ({ you, can, issues }) => {
  document.body.innerHTML = "";
  document.body.style.margin = "0";
  const container = document.createElement("div");
  container.style.cssText = "position:relative;width:900px;height:650px";
  const canvas = document.createElement("canvas");
  canvas.width = 900; canvas.height = 650;
  canvas.style.cssText = "position:absolute;left:0;top:0";
  container.append(canvas);
  document.body.append(container);

  const server = { issues: structuredClone(issues), head: "h0", requests: [], you, can };
  window.server = server;
  window.errors = [];
  window.addEventListener("error", (e) => window.errors.push(String(e.message)));
  window.windowKeys = 0;
  window.addEventListener("keydown", () => window.windowKeys++);
  const json = (data, status = 200) => new Response(JSON.stringify(data), {
    status, headers: { "Content-Type": "application/json" } });
  const bump = () => (server.head = "h" + (parseInt(server.head.slice(1)) + 1));
  window.fetch = async (url, options = {}) => {
    url = new URL(url);
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    const path = url.pathname.replace("/api/hive/projects/Mutator/comments", "");
    server.requests.push({ method, path, body });
    if (method === "GET" && path === "/head") return json({ head: server.head });
    if (method === "GET" && path === "") return json({
      head: server.head, issues: server.issues, you: server.you, can: server.can,
      members: [
        { username: "ana", name: "Ana Reviewer" },
        { username: "bob", name: "Bob Designer" },
      ] });
    const person = { username: server.you.username, name: server.you.name };
    const now = "2026-09-30T10:00:00Z";
    if (method === "POST" && path === "") {
      const issue = { number: server.issues.length + 1, glyph: body.glyph ?? null,
        source: body.source ?? null, point: body.point ?? null, title: body.title ?? null,
        branch: body.branch, commit: "c", state: "open", author: person, created: now, resolved: null,
        assignee: null, labels: [],
        messages: [{ id: 1, author: person, created: now, edited: null, text: body.text,
                     ...(body.sketch ? { sketch: body.sketch } : {}) }] };
      server.issues.push(issue);
      return json({ head: bump(), issue });
    }
    if (method === "GET" && path === "/deleted") return json({ deleted: server.deleted || [] });
    if (method === "GET" && path.endsWith("/history")) {
      return json({ history: server.history || [] });
    }
    if (method === "POST" && path.endsWith("/restore")) {
      const number = +path.split("/")[1];
      const found = (server.deleted || []).find((d) => d.number === number);
      server.deleted = (server.deleted || []).filter((d) => d !== found);
      server.issues.push(found.issue);
      return json({ head: bump(), issue: found.issue });
    }
    const m = path.match(/^\\/(\\d+)(\\/messages(?:\\/(\\d+))?)?$/);
    const issue = m && server.issues.find((i) => i.number === +m[1]);
    if (!issue) return new Response("no such comment", { status: 404 });
    if (server.refuse) return new Response(server.refuse, { status: 403 });
    if (method === "POST" && m[2]) {
      issue.messages.push({ id: issue.messages.length + 1, author: person,
        created: now, edited: null, text: body.text,
        ...(body.sketch ? { sketch: body.sketch } : {}) });
    } else if (method === "PATCH" && !m[2]) {
      if (body.point) issue.point = body.point;
      if ("assignee" in body) {
        issue.assignee = body.assignee
          ? { username: body.assignee, name: body.assignee.toUpperCase() }
          : null;
      }
      if (body.labels) issue.labels = body.labels;
      if ("title" in body) issue.title = body.title;
      if (body.state) {
        issue.state = body.state;
        issue.resolved = body.state === "resolved" ? { by: person, at: now } : null;
      }
    } else if (method === "PATCH") {
      const message = issue.messages.find((x) => x.id === +m[3]);
      message.text = body.text; message.edited = now;
      if ("sketch" in body) {
        if (body.sketch) message.sketch = body.sketch;
        else delete message.sketch;
      }
    } else if (method === "DELETE" && m[3]) {
      issue.messages = issue.messages.filter((x) => x.id !== +m[3]);
    } else if (method === "DELETE") {
      server.issues = server.issues.filter((i) => i !== issue);
      return json({ head: bump(), issue: null });
    }
    return json({ head: bump(), issue });
  };

  const varGlyph = {
    sources: [
      { name: "Light", layerName: "Light" },
      { name: "", layerName: "Bold", locationBase: "b" },
    ],
    getSourceName: (s) => s.name || "Bold",
    getSourceLocation: (s) => (s.layerName === "Bold" ? { wght: 700 } : { wght: 100 }),
  };
  const glyphAt = (layerName, sourceIndex) => ({ layerName, sourceIndex, xAdvance: 600, varGlyph });
  const positioned = { glyphName: "H", x: 0, y: 0, glyph: glyphAt("Bold", 1) };
  const other = { glyphName: "O", x: 600, y: 0, glyph: glyphAt("Bold", 1) };
  window.positioned = positioned;
  const sceneSettings = {
    selectedGlyphName: "H",
    selectedGlyph: { lineIndex: 0, glyphIndex: 0, isEditing: true },
    positionedLines: [{ glyphs: [positioned, other] }],
  };
  const listeners = [];
  window.fireKey = (key) => listeners.filter((l) => l.keys.includes(key)).forEach((l) => l.f());
  const canvasController = {
    canvas, magnification: 1, origin: { x: 100, y: 500 }, updates: 0,
    canvasWidth: 900, canvasHeight: 650,
    canvasPoint(p) { return { x: p.x * this.magnification + this.origin.x,
                              y: -p.y * this.magnification + this.origin.y }; },
    localPoint(e) { return { x: (e.x - this.origin.x) / this.magnification,
                             y: -(e.y - this.origin.y) / this.magnification }; },
    requestUpdate() { this.updates++; },
  };
  const tools = {
    "pointer-tool": {
      handleDrag: async () => (window.pointerToolUsed = (window.pointerToolUsed || 0) + 1),
    },
  };
  const visible = new Set();
  const editor = {
    projectIdentifier: "Mutator",
    canvasController,
    tools,
    selectedToolIdentifier: "pointer-tool",
    addEditTool(tool) {
      tools[tool.identifier] = tool;
      window.tool = tool;
      // Like Fontra: the new button is created "selected".
      const button = document.createElement("div");
      button.className = "tool-button selected";
      button.dataset.tool = tool.identifier;
      document.body.append(button);
    },
    sceneController: {
      sceneSettings,
      sceneModel: {
        get positionedLines() { return sceneSettings.positionedLines; },
        get selectedGlyph() { return sceneSettings.selectedGlyph; },
        getSelectedPositionedGlyph: () => positioned,
        getSelectedVariableGlyphController: async () => varGlyph,
      },
      sceneSettingsController: {
        addKeyListener: (keys, f) => listeners.push({ keys: [keys].flat(), f }),
      },
      setLocationFromSourceIndex: async (i) => { window.wentToSource = i; },
    },
    visualizationLayers: {
      definitions: [], darkTheme: false, visibleLayerIds: visible,
      toggle: (id, on) => (on ? visible.add(id) : visible.delete(id)),
    },
    addSidebarPanel: (panel) => { document.body.append(panel); window.panel = panel; },
  };
  window.editor = editor;
  const module = await import("/comments.js");
  window.module = module;
  const { comments } = module.initComments(editor, "/hive/plugin");
  comments.stopPolling();
  window.comments = comments;
  await comments.refresh();
  await window.panel.toggle(true);
  window.frame = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  window.card = () => comments.overlayRoot.querySelector(".card");
  window.cardText = () => window.card()?.textContent || "";
  window.textarea = (role) => comments.overlayRoot.querySelector(`textarea[data-role="${role}"]`);
  window.press = (x, y, dx = 0, dy = 0) => {
    const rect = canvas.getBoundingClientRect();
    const opts = (px, py) => ({ bubbles: true, cancelable: true, button: 0,
      clientX: rect.left + px, clientY: rect.top + py });
    canvas.dispatchEvent(new MouseEvent("mousedown", opts(x, y)));
    if (dx || dy) window.dispatchEvent(new MouseEvent("mousemove", opts(x + dx, y + dy)));
    window.dispatchEvent(new MouseEvent("mouseup", opts(x + dx, y + dy)));
  };
  window.useTool = async (gx, gy) => {
    async function* stream() {}
    const x = 100 + gx, y = 500 - gy;
    await window.tool.handleDrag(stream(), { x, y, pageX: x, pageY: y });
  };
  window.drawn = (layerName = "Bold") => {
    const def = editor.visualizationLayers.definitions.find(
      (d) => d.identifier === "hive.comments");
    const glyphs = def.selectionFunc({ glyphsBySelectionMode: { all: [positioned, other] } });
    const log = [];
    const ctx = new Proxy({}, {
      get: (t, k) => (k in t ? t[k] : (...a) => log.push([k, ...a])),
      set: (t, k, v) => { log.push(["set", k, v]); return true; },
    });
    const pg = { ...positioned, glyph: { ...positioned.glyph, layerName } };
    def.draw({ context: ctx, positionedGlyph: pg, parameters: {}, controller: canvasController });
    return { glyphs: glyphs.map((g) => g.glyphName), log };
  };
}
"""

ANA = {"username": "ana", "name": "Ana Reviewer"}
BOB = {"username": "bob", "name": "Bob Designer"}
ALL = {
    "comment": True,
    "resolveAny": True,
    "organize": True,
    "moderate": True,
    "administer": True,
}
REVIEWER = {"comment": True, "resolveAny": False, "organize": False, "moderate": False}


def topic(number, author=BOB, layer="Bold", state="open", replies=(), glyph="H"):
    messages = [
        {
            "id": 1,
            "author": author,
            "created": "2026-09-30T09:00:00Z",
            "edited": None,
            "text": f"Topic {number}",
        }
    ]
    for i, (who, text) in enumerate(replies, start=2):
        messages.append(
            {
                "id": i,
                "author": who,
                "created": "2026-09-30T09:30:00Z",
                "edited": None,
                "text": text,
            }
        )
    return {
        "number": number,
        "glyph": glyph,
        "source": {"layer": layer, "name": layer, "location": {}},
        "point": {"x": 200, "y": 300},
        "branch": "main",
        "commit": "c",
        "state": state,
        "author": author,
        "created": "2026-09-30T09:00:00Z",
        "resolved": None,
        "assignee": None,
        "labels": [],
        "messages": messages,
    }


@pytest.fixture
def page(browser_page):
    def setup(you=ANA, can=ALL, issues=()):
        browser_page.goto(f"{browser_page.base}/plugin.json")
        browser_page.evaluate(SETUP, {"you": you, "can": can, "issues": list(issues)})
        return browser_page

    return setup


def requests(page, method=None):
    reqs = page.evaluate("server.requests")
    return [r for r in reqs if method is None or r["method"] == method]


def test_installs_a_tool_a_layer_and_a_panel(page):
    p = page()
    assert p.evaluate("window.tool.identifier") == "hive-comment-tool"
    assert p.evaluate("window.tool.iconPath") == "/hive/plugin/comment-tool.svg"
    # Not shown as selected: the pointer tool is.
    assert (
        p.evaluate("document.querySelector('[data-tool=hive-comment-tool]').className")
        == "tool-button"
    )
    layer = p.evaluate(
        "editor.visualizationLayers.definitions.map(d => [d.identifier, d.userSwitchable])"
    )
    assert layer == [["hive.comments", True]]
    assert p.evaluate("editor.visualizationLayers.visibleLayerIds.has('hive.comments')")
    assert p.evaluate("panel.identifier") == "hive-comments"
    assert "No comments on this glyph" in p.evaluate("panel.shadowRoot.textContent")


def test_pins_are_solid_on_their_source_and_pale_elsewhere(page):
    p = page(
        issues=[topic(1, layer="Bold"), topic(2, state="resolved"), topic(3, glyph="X")]
    )
    drawn = p.evaluate("drawn('Bold')")
    assert drawn["glyphs"] == ["H"]  # O has none; resolved ones are hidden
    arcs = [e for e in drawn["log"] if e[0] == "arc"]
    assert len(arcs) == 1
    texts = [e[1] for e in drawn["log"] if e[0] == "fillText"]
    assert texts == ["1"]
    alphas = [e for e in drawn["log"] if e[:2] == ["set", "globalAlpha"]]
    assert alphas == []
    pale = p.evaluate("drawn('Light')")
    assert ["set", "globalAlpha", 0.35] in pale["log"]
    # Shown resolved: two pins.
    p.evaluate("comments.showResolved = true")
    texts = [e[1] for e in p.evaluate("drawn('Bold')")["log"] if e[0] == "fillText"]
    assert texts == ["1", "2"]


def test_new_comment_with_the_tool(page):
    p = page(you=ANA, can=REVIEWER)
    p.evaluate("useTool(120, 450)")
    p.evaluate("frame()")
    assert "New comment" in p.evaluate("cardText()")
    assert "H · Bold" in p.evaluate("cardText()")
    # Typing does not reach the editor's shortcuts; Enter sends.
    p.evaluate("textarea('draft').focus()")
    p.keyboard.type("Stem too thin")
    assert p.evaluate("windowKeys") == 0
    p.keyboard.press("Enter")
    p.wait_for_function("server.issues.length === 1")
    posted = requests(p, "POST")[0]["body"]
    assert posted["glyph"] == "H" and posted["point"] == {"x": 120, "y": 450}
    assert posted["source"] == {
        "layer": "Bold",
        "name": "Bold",
        "location": {"wght": 700},
    }
    assert posted["text"] == "Stem too thin"
    assert (
        "branch" not in posted
    )  # "Mutator" alone: the server picks the default branch
    p.wait_for_function("cardText().includes('#1')")
    text = p.evaluate("cardText()")
    assert "Stem too thin" in text and "Ana Reviewer" in text
    # Ana wrote it: she may resolve it.
    assert "Resolve" in text
    assert p.evaluate("panel.shadowRoot.querySelectorAll('.issue').length") == 1


def test_the_tool_needs_a_source(page):
    p = page()
    p.evaluate("positioned.glyph = { ...positioned.glyph, layerName: undefined }")
    p.evaluate("useTool(120, 450)")
    p.evaluate("frame()")
    assert p.evaluate("card()") is None
    toast = "comments.overlayRoot.querySelector('.toast')"
    assert "pinned to a source" in p.evaluate(f"{toast}.textContent")
    # "Go to the nearest source" (Fontra's), then the comment starts there.
    p.evaluate("""editor.goToNearestSource = async () => {
      window.wentNearest = true;
      setTimeout(() => (positioned.glyph = { ...positioned.glyph, layerName: 'Bold' }), 150);
    }""")
    p.evaluate(f"{toast}.querySelector('button').click()")
    p.wait_for_function("comments.draft !== null")
    assert p.evaluate("window.wentNearest")
    assert p.evaluate("comments.draft.point") == {"x": 120, "y": 450}
    assert p.evaluate("comments.draft.source.layer") == "Bold"


def test_the_tool_leaves_other_glyphs_to_fontra(page):
    p = page()
    p.evaluate("useTool(900, 300)")  # over O, the next glyph of the line
    assert p.evaluate("window.pointerToolUsed") == 1
    assert p.evaluate("comments.draft") is None


def test_click_on_a_pin_opens_and_closes_the_post_it(page):
    p = page(issues=[topic(1, replies=[(ANA, "Agreed")])])
    # Point (200, 300) → screen (300, 200); the pin circle is 16 px above.
    p.evaluate("press(300, 184)")
    p.evaluate("frame()")
    text = p.evaluate("cardText()")
    assert "#1" in text and "Topic 1" in text and "Agreed" in text
    assert p.evaluate("editor.canvasController.updates") > 0
    p.evaluate("press(300, 184)")
    p.evaluate("frame()")
    assert p.evaluate("card()") is None
    # Missing the pin does nothing here (Fontra's tool gets the click).
    p.evaluate("press(340, 184)")
    p.evaluate("frame()")
    assert p.evaluate("card()") is None


def test_reply_keeps_typing_across_refreshes_and_clears_once_sent(page):
    p = page(issues=[topic(1)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    p.evaluate("textarea('reply').focus()")
    p.keyboard.type("Half")
    # Someone else answers meanwhile: the card is rebuilt, the text stays.
    p.evaluate(
        "server.issues[0].messages.push({id: 2, author: {username: 'bob', name: 'Bob'},"
        " created: '2026-09-30T09:40:00Z', edited: null, text: 'Me too'});"
        " server.head = 'h9'"
    )
    p.evaluate("comments.checkHead()")
    p.wait_for_function("cardText().includes('Me too')")
    assert p.evaluate("textarea('reply').value") == "Half"
    assert p.evaluate("comments.overlayRoot.activeElement?.dataset.role") == "reply"
    p.keyboard.type(" done")
    p.keyboard.press("Enter")
    p.wait_for_function("cardText().includes('Half done')")
    p.evaluate("frame()")
    assert p.evaluate("textarea('reply').value") == ""
    assert requests(p, "POST")[-1] == {
        "method": "POST",
        "path": "/1/messages",
        "body": {"text": "Half done"},
    }


def test_a_refused_reply_keeps_the_text_and_says_why(page):
    p = page(issues=[topic(1)])
    p.evaluate("server.refuse = 'observer cannot comment'")
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    p.evaluate("textarea('reply').focus()")
    p.keyboard.type("Please")
    p.keyboard.press("Enter")
    p.wait_for_function("cardText().includes('observer cannot comment')")
    assert p.evaluate("textarea('reply').value") == "Please"


def test_what_a_reviewer_sees_on_someone_elses_topic(page):
    p = page(
        you=ANA, can=REVIEWER, issues=[topic(1, author=BOB, replies=[(ANA, "Mine")])]
    )
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    buttons = p.evaluate(
        "[...card().querySelectorAll('button')].map(b => b.textContent.trim())"
    )
    assert "Resolve" not in buttons
    titles = p.evaluate("[...card().querySelectorAll('button')].map(b => b.title)")
    assert "Delete this topic" not in titles
    # Her own reply: edit and delete; Bob's opening message: nothing.
    first, second = p.evaluate(
        "[...card().querySelectorAll('.message')].map(m => m.querySelector('.actions')"
        "?.textContent || '')"
    )
    assert first == "" and second == "EditDelete"
    # Without the comment capability: no reply box.
    p = page(
        you=ANA,
        can={"comment": False, "resolveAny": False, "moderate": False},
        issues=[topic(1)],
    )
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    assert p.evaluate("textarea('reply')") is None


def test_resolve_edit_and_delete(page):
    p = page(you=BOB, issues=[topic(1, replies=[(BOB, "typo")])])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    p.evaluate(
        "[...card().querySelectorAll('.message')][1].querySelector('.link').click()"
    )
    p.evaluate("frame()")
    p.evaluate("textarea('edit-2').focus()")
    p.keyboard.press("End")
    p.keyboard.type("s")
    p.keyboard.press("Enter")
    p.wait_for_function("cardText().includes('typos')")
    assert "edited" in p.evaluate("cardText()")
    p.evaluate(
        "[...card().querySelectorAll('button')].find(b => b.textContent === 'Resolve').click()"
    )
    p.wait_for_function("cardText().includes('Resolved by')")
    assert requests(p, "PATCH")[-1]["body"] == {"state": "resolved"}
    p.evaluate("window.confirm = () => true")
    p.evaluate(
        "[...card().querySelectorAll('button')].find(b => b.title === 'Delete this topic').click()"
    )
    p.wait_for_function("server.issues.length === 0")
    p.evaluate("frame()")
    assert p.evaluate("card()") is None


def test_dragging_a_pin_moves_it(page):
    p = page(you=BOB, issues=[topic(1)])
    p.evaluate("press(300, 184, 30, -20)")
    p.wait_for_function("server.requests.some(r => r.method === 'PATCH')")
    assert requests(p, "PATCH")[-1]["body"] == {"point": {"x": 230, "y": 320}}
    assert p.evaluate("card()") is None  # a drag does not open it
    # Someone who may not move it just opens it.
    p = page(you=ANA, can=REVIEWER, issues=[topic(1)])
    p.evaluate("press(300, 184, 30, -20)")
    p.evaluate("frame()")
    assert requests(p, "PATCH") == []
    assert "#1" in p.evaluate("cardText()")


def test_panel_lists_and_shows_topics(page):
    p = page(issues=[topic(1), topic(2, state="resolved"), topic(3, glyph="O")])
    rows = p.evaluate(
        "[...panel.shadowRoot.querySelectorAll('.issue')].map(r => r.dataset.number)"
    )
    assert rows == ["1", "2"]  # glyph H: open first, then resolved
    assert "Resolved (1)" in p.evaluate("panel.shadowRoot.textContent")
    p.evaluate("panel.shadowRoot.querySelector('.issue').click()")
    p.wait_for_function("comments.openNumber === 1")
    assert p.evaluate("window.wentToSource") == 1  # the Bold source
    # No glyph selected: the whole project's.
    p.evaluate(
        "editor.sceneController.sceneSettings.selectedGlyphName = null;"
        " fireKey('selectedGlyphName')"
    )
    rows = p.evaluate(
        "[...panel.shadowRoot.querySelectorAll('.issue')].map(r => r.dataset.number)"
    )
    assert rows == ["1", "3", "2"]
    assert p.evaluate("errors") == []


def test_card_follows_the_view(page):
    p = page(issues=[topic(1)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    before = p.evaluate("[card().offsetLeft, card().offsetTop]")
    p.evaluate("editor.canvasController.origin.x += 50; fireKey('viewBox')")
    p.evaluate("frame()")
    after = p.evaluate("[card().offsetLeft, card().offsetTop]")
    assert after[0] == before[0] + 50 and after[1] == before[1]


def test_helpers(page):
    p = page()
    assert (
        p.evaluate(
            "module.relativeTime('2026-09-30T10:00:00Z', Date.parse('2026-09-30T10:00:20Z'))"
        )
        == "just now"
    )
    assert (
        p.evaluate(
            "module.relativeTime('2026-09-30T08:00:00Z', Date.parse('2026-09-30T10:00:00Z'))"
        )
        == "2 h ago"
    )
    assert p.evaluate("module.hitsPin({x: 10, y: 50}, {x: 10, y: 34})")
    assert not p.evaluate("module.hitsPin({x: 10, y: 50}, {x: 10, y: 50})")


def test_a_refused_move_puts_the_pin_back(page):
    p = page(you=BOB, issues=[topic(1)])
    p.evaluate("server.refuse = 'reviewer cannot move this topic'")
    p.evaluate("press(300, 184, 30, -20)")
    p.wait_for_function(
        "comments.overlayRoot.querySelector('.toast')?.textContent.includes('could not')"
    )
    assert p.evaluate("comments.issue(1).point") == {"x": 200, "y": 300}


def test_a_stale_refresh_does_not_undo_our_change(page):
    p = page(you=BOB, issues=[topic(1)])
    # A poll starts, then our reply lands before the poll's answer.
    p.evaluate("""async () => {
          const realFetch = window.fetch;
          let release;
          const gate = new Promise((r) => (release = r));
          window.fetch = async (url, options) => {
            const path = new URL(url).pathname;
            if (!options?.method && path.endsWith('/comments')) {
              const answer = await realFetch(url, options);  // the old state
              await gate;
              return answer;
            }
            return realFetch(url, options);
          };
          const polling = comments.refresh();
          await comments.reply(1, 'Fresh');
          release();
          await polling;
          window.fetch = realFetch;
        }""")
    assert [m["text"] for m in p.evaluate("comments.issue(1).messages")] == [
        "Topic 1",
        "Fresh",
    ]


def test_opening_a_topic_asks_before_dropping_a_draft(page):
    p = page(issues=[topic(1)])
    p.evaluate("useTool(120, 450)")
    p.evaluate("frame()")
    p.evaluate("textarea('draft').focus()")
    p.keyboard.type("Not finished")
    p.evaluate("window.confirm = () => false")
    p.evaluate("comments.open(1)")
    assert p.evaluate("comments.draft.text") == "Not finished"
    assert p.evaluate("comments.openNumber") is None
    p.evaluate("window.confirm = () => true")
    p.evaluate("comments.open(1)")
    assert p.evaluate("comments.draft") is None
    assert p.evaluate("comments.openNumber") == 1


def test_the_card_follows_a_pan_even_with_the_layer_off(page):
    p = page(issues=[topic(1)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    before = p.evaluate("card().offsetLeft")
    p.evaluate("editor.visualizationLayers.toggle('hive.comments', false)")
    p.evaluate("editor.canvasController.origin.x += 40")  # the Hand tool: no event
    p.evaluate("frame()")
    p.evaluate("frame()")
    assert p.evaluate("card().offsetLeft") == before + 40


# --- step B ---------------------------------------------------------------------


def test_designers_assign_and_label_in_the_post_it(page):
    p = page(you=BOB, issues=[topic(1), {**topic(2), "labels": ["curve"]}])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    options = p.evaluate(
        "[...card().querySelectorAll('.assignee-select option')].map(o => o.textContent)"
    )
    assert options == ["Nobody", "Ana Reviewer", "Bob Designer"]
    p.evaluate("""() => {
      const select = card().querySelector('.assignee-select');
      select.value = 'ana';
      select.dispatchEvent(new Event('change'));
    }""")
    p.wait_for_function("comments.issue(1).assignee?.username === 'ana'")
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.assignee-select').value") == "ana"
    assert requests(p, "PATCH")[-1]["body"] == {"assignee": "ana"}
    # Suggestions come from the project's labels.
    assert p.evaluate(
        "[...card().querySelectorAll('datalist option')].map(o => o.value)"
    ) == ["curve"]
    p.evaluate("card().querySelector('.label-input').focus()")
    p.keyboard.type("spacing")
    assert p.evaluate("windowKeys") == 0
    p.keyboard.press("Enter")
    p.wait_for_function("card().querySelectorAll('.label-chip').length === 1")
    assert requests(p, "PATCH")[-1]["body"] == {"labels": ["spacing"]}
    p.evaluate("card().querySelector('.chip-remove').click()")
    p.wait_for_function("card().querySelectorAll('.label-chip').length === 0")
    assert requests(p, "PATCH")[-1]["body"] == {"labels": []}


def test_reviewers_see_the_assignee_and_labels(page):
    labelled = {**topic(1), "labels": ["client"], "assignee": BOB}
    p = page(you=ANA, can=REVIEWER, issues=[labelled, topic(2)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.assignee-select')") is None
    assert p.evaluate("card().querySelector('.organize').textContent") == (
        "Assigned to Bob Designerclient"
    )
    p.evaluate("comments.open(2)")
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.organize')") is None


def test_a_link_opens_the_editor_on_the_comment(page):
    p = page()
    link = p.evaluate(
        "module.issueLink('uplustype/Mutator', {glyph: 'a.alt', number: 7},"
        " 'https://fontrahive.com')"
    )
    from urllib.parse import parse_qs, urlparse

    url = urlparse(link)
    assert url.path == "/editor.html"
    query = parse_qs(url.query)
    assert query["project"] == ["uplustype/Mutator"]
    assert query["text"] == ['"/a.alt"']
    assert query["hive-issue"] == ["7"]
    assert query["selectedGlyph"] == ['{"lineIndex":0,"glyphIndex":0,"isEditing":true}']


def test_a_pending_link_opens_its_comment_once_loaded(page):
    p = page(issues=[topic(1), topic(2)])
    p.evaluate("""async () => {
      sessionStorage.setItem('hive.openIssue',
        JSON.stringify({project: 'Mutator@main', number: '2'}));
      comments.pendingIssue = comments.takePendingIssue();
      await comments.refresh();
    }""")
    p.wait_for_function("comments.openNumber === 2")
    assert p.evaluate("sessionStorage.getItem('hive.openIssue')") is None
    assert p.evaluate("window.wentToSource") == 1


def test_show_the_commented_version(page):
    p = page(issues=[{**topic(1), "commit": "abcdef1234"}])
    p.evaluate(
        "comments.history = {showExternalVersion: (...a) => (window.shownVersion = a)}"
    )
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    assert "Written on version abcdef1 of main" in p.evaluate("cardText()")
    # The canvas is on another source: it goes to the comment's first.
    p.evaluate(
        "positioned.glyph = { ...positioned.glyph, layerName: 'Light', sourceIndex: 0 }"
    )
    p.evaluate("card().querySelector('.show-version').click()")
    p.wait_for_function("window.shownVersion")
    assert p.evaluate("window.wentToSource") == 1
    assert p.evaluate("window.shownVersion") == [
        "abcdef1234",
        "H",
        "the version commented in #1 (Bold)",
        "Bold",
    ]


def test_copy_link(page):
    p = page(issues=[topic(1)])
    p.evaluate("navigator.clipboard.writeText = async (t) => (window.copied = t)")
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    p.evaluate("card().querySelector('[title^=\"Copy a link\"]').click()")
    p.wait_for_function("window.copied")
    assert "hive-issue=1" in p.evaluate("window.copied")


def test_a_click_outside_closes_the_post_it(page):
    p = page(issues=[topic(1)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    # Inside: stays open.
    p.evaluate(
        "card().querySelector('.messages').dispatchEvent("
        "new MouseEvent('mousedown', {bubbles: true, composed: true}))"
    )
    assert p.evaluate("comments.openNumber") == 1
    # On its own pin: the pin's click decides (here: closes, once).
    p.evaluate("press(300, 184)")
    assert p.evaluate("comments.openNumber") is None
    p.evaluate("comments.open(1)")
    # Elsewhere on the canvas, with another tool, or elsewhere on the page.
    p.evaluate("press(600, 500)")
    assert p.evaluate("comments.openNumber") is None
    p.evaluate("comments.open(1)")
    p.evaluate(
        "document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))"
    )
    assert p.evaluate("comments.openNumber") is None
    # An empty draft goes; one with text stays.
    p.evaluate("useTool(120, 450)")
    p.evaluate(
        "document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))"
    )
    assert p.evaluate("comments.draft") is None
    p.evaluate("useTool(120, 450); comments.draft.text = 'keep me'")
    p.evaluate(
        "document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))"
    )
    assert p.evaluate("comments.draft.text") == "keep me"


def test_an_optional_title(page):
    # A designer who is not the author cannot title it either.
    p = page(you=ANA, can={**ALL, "administer": False}, issues=[topic(2, author=BOB)])
    p.evaluate("comments.open(2)")
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.title-row')") is None
    # An admin can.
    p = page(you=ANA, can=ALL, issues=[topic(2, author=BOB)])
    p.evaluate("comments.open(2)")
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.add-title')") is not None
    p = page(you=ANA, can=REVIEWER, issues=[topic(1, author=ANA), topic(2, author=BOB)])
    p.evaluate("comments.open(2)")  # not hers: no way to name it
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.title-row')") is None
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    p.evaluate("card().querySelector('.add-title').click()")
    p.evaluate("frame()")
    p.evaluate("card().querySelector('.title-input').focus()")
    p.keyboard.type("Terminal too heavy")
    assert p.evaluate("windowKeys") == 0
    p.keyboard.press("Enter")
    p.wait_for_function("comments.issue(1).title === 'Terminal too heavy'")
    assert requests(p, "PATCH")[-1]["body"] == {"title": "Terminal too heavy"}
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.title-row .title').textContent") == (
        "Terminal too heavy"
    )
    # The panel lists it by its title; the other one by its first message.
    texts = p.evaluate(
        "[...panel.shadowRoot.querySelectorAll('.issue .text')].map(e => e.textContent)"
    )
    assert texts == ["Terminal too heavy", "Topic 2"]
    # Emptied: the first message is the title again.
    p.evaluate("card().querySelector('.rename').click()")
    p.evaluate("frame()")
    p.evaluate("card().querySelector('.title-input').value = ''")
    p.evaluate("card().querySelector('.title-row button.primary').click()")
    p.wait_for_function("comments.issue(1).title === null")


def test_a_glyph_not_on_the_canvas_can_be_added_from_the_message(page):
    p = page(issues=[topic(1), topic(4, glyph="C")])
    p.evaluate("""() => {
      const settings = editor.sceneController.sceneSettings;
      window.lines = [[{ glyphName: "H" }, { glyphName: "O" }]];
      Object.defineProperty(settings, "characterLines", {
        get: () => window.lines,
        set: (value) => {
          window.lines = value;
          const added = value.at(-1).at(-1);
          settings.positionedLines[0].glyphs.push({
            glyphName: added.glyphName, x: 1200, y: 0, glyph: positioned.glyph });
        },
      });
      editor.sceneController.glyphInfoFromGlyphName = (name) => ({ glyphName: name });
      editor.sceneController.sceneSettingsController.waitForKeyChange = async () => null;
      window.selections = [];
      Object.defineProperty(settings, "selectedGlyph", {
        get: () => window.selections.at(-1),
        set: (value) => window.selections.push(value),
      });
    }""")
    p.evaluate("comments.show(comments.issue(4))")
    toast = "comments.overlayRoot.querySelector('.toast')"
    assert "C is not on the canvas." in p.evaluate(f"{toast}.textContent")
    assert p.evaluate("window.lines.at(-1).length") == 2  # nothing added yet
    p.evaluate(f"{toast}.querySelector('button').click()")
    p.wait_for_function("comments.openNumber === 4")
    assert p.evaluate("window.lines.at(-1).map(g => g.glyphName)") == ["H", "O", "C"]
    assert p.evaluate("window.selections.at(-1)") == {
        "lineIndex": 0,
        "glyphIndex": 2,
        "isEditing": True,
    }


# --- the past: edited messages, deleted topics ----------------------------------------


def test_earlier_versions_of_a_message(page):
    p = page()
    history = [
        {"time": 300, "issue": {"messages": [{"id": 2, "text": "v3"}]}},
        {"time": 200, "issue": {"messages": [{"id": 2, "text": "v2"}]}},
        {"time": 150, "issue": {"messages": [{"id": 2, "text": "v2"}]}},
        {"time": 100, "issue": {"messages": [{"id": 2, "text": "v1"}]}},
        {"time": 50, "issue": {"messages": [{"id": 1, "text": "first"}]}},
    ]
    versions = p.evaluate("(h) => module.earlierVersions(h, 2)", history)
    # v2 replaced at 300 (by v3), v1 replaced at 150 (by v2).
    assert versions == [{"text": "v2", "time": 300}, {"text": "v1", "time": 150}]


def test_an_edited_message_shows_what_it_said(page):
    edited = topic(1, replies=[(ANA, "second")])
    edited["messages"][1]["edited"] = "2026-09-30T09:40:00Z"
    p = page(issues=[edited])
    p.evaluate("""server.history = [
      { time: 1790000300, issue: { messages: [{ id: 2, text: 'second' }] } },
      { time: 1790000200, issue: { messages: [{ id: 2, text: 'first' }] } },
    ]""")
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    p.evaluate("card().querySelector('.edited').click()")
    p.wait_for_function("card().querySelector('.earlier-version')")
    assert p.evaluate("card().querySelector('.earlier .text').textContent") == "first"
    assert requests(p, "GET")[-1]["path"] == "/1/history"
    p.evaluate("card().querySelector('.edited').click()")
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.earlier')") is None


def test_managers_see_and_restore_deleted_topics(page):
    gone = {
        "number": 5,
        "deletedBy": "Mona",
        "deletedAt": 1790000000,
        "issue": {**topic(5), "title": "Gone"},
    }
    p = page(you=BOB, can=ALL, issues=[topic(1)])
    p.evaluate(
        "(d) => { server.deleted = [d]; comments.dataVersion++; panel.render(); }", gone
    )
    p.wait_for_function("panel.shadowRoot.querySelector('details.deleted')")
    text = p.evaluate("panel.shadowRoot.querySelector('details.deleted').textContent")
    assert "Deleted (1)" in text and "Gone" in text and "deleted by Mona" in text
    p.evaluate("panel.shadowRoot.querySelector('.restore-deleted').click()")
    p.wait_for_function("comments.issue(5) !== null")
    p.wait_for_function("!panel.shadowRoot.querySelector('details.deleted')")
    # Reviewers do not see deleted topics (nor ask for them).
    p = page(you=ANA, can=REVIEWER, issues=[topic(1)])
    p.evaluate("panel.render()")
    assert not any(r["path"] == "/deleted" for r in requests(p, "GET"))


# --- step C: the red pen -------------------------------------------------------------

DRAW = """(points) => {
  const canvas = editor.canvasController.canvas;
  const rect = canvas.getBoundingClientRect();
  const event = (type, [x, y]) => new PointerEvent(type, {
    bubbles: true, cancelable: true, composed: true, pointerId: 7, button: 0,
    clientX: rect.left + x, clientY: rect.top + y });
  canvas.dispatchEvent(event("pointerdown", points[0]));
  for (const p of points.slice(1)) window.dispatchEvent(event("pointermove", p));
  window.dispatchEvent(event("pointerup", points.at(-1)));
}"""


def test_simplify_a_stroke(page):
    p = page()
    straight = [[i, 0] for i in range(10)] + [[9, 9]]
    assert p.evaluate("(pts) => module.simplifyStroke(pts, 0.5)", straight) == [
        [0, 0],
        [9, 0],
        [9, 9],
    ]
    assert p.evaluate("module.simplifyStroke([[0, 0], [1, 1]], 1)") == [[0, 0], [1, 1]]


def test_a_new_comment_can_be_a_drawing(page):
    p = page(you=ANA, can=REVIEWER)
    p.evaluate("useTool(120, 450)")
    p.evaluate("frame()")
    p.evaluate("card().querySelector('.pen').click()")
    p.evaluate("frame()")
    assert "Drawing" in p.evaluate("card().querySelector('.pen').textContent")
    # Screen (300, 200) is glyph (200, 300): the canvas origin is (100, 500).
    p.evaluate(DRAW, [[300, 200], [310, 190], [320, 200], [340, 240]])
    p.evaluate("frame()")
    strokes = p.evaluate("comments.sketch.strokes")
    assert (
        len(strokes) == 1
        and strokes[0][0] == [200, 300]
        and strokes[0][-1] == [240, 260]
    )
    assert "1 stroke" in p.evaluate("cardText()")
    # The pen does not reach Fontra's tools nor close the post-it.
    assert p.evaluate("window.pointerToolUsed") is None
    assert p.evaluate("comments.draft") is not None
    # No text needed: Comment is enabled by the drawing.
    assert p.evaluate("card().querySelector('button.primary').disabled") is False
    p.evaluate("card().querySelector('button.primary').click()")
    p.wait_for_function("server.issues.length === 1")
    posted = requests(p, "POST")[-1]["body"]
    assert posted["text"] == "" and posted["sketch"] == {"strokes": strokes}
    p.wait_for_function("comments.sketch === null && comments.openNumber === 1")
    # The post-it is drawn again on the next frame, with the posted topic.
    p.wait_for_function("!!card()?.querySelector('.sketch-mark')")


def test_undo_clear_and_draw_in_a_reply(page):
    p = page(issues=[topic(1)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    p.evaluate("card().querySelector('.pen').click()")
    p.evaluate(DRAW, [[300, 200], [340, 240]])
    p.evaluate(DRAW, [[300, 240], [340, 200]])
    assert p.evaluate("comments.sketch.strokes.length") == 2
    p.evaluate("frame()")
    p.evaluate(
        "[...card().querySelectorAll('.pen-row .link')].find(b => b.textContent === 'Undo').click()"
    )
    assert p.evaluate("comments.sketch.strokes.length") == 1
    p.evaluate("frame()")
    p.evaluate("textarea('reply').focus()")
    p.keyboard.type("like this")
    p.keyboard.press("Enter")
    p.wait_for_function("comments.issue(1).messages.length === 2")
    body = requests(p, "POST")[-1]["body"]
    assert body["text"] == "like this" and len(body["sketch"]["strokes"]) == 1


def test_sketches_on_the_canvas(page):
    drawn = {
        **topic(1),
        "messages": [
            {**topic(1)["messages"][0], "sketch": {"strokes": [[[0, 0], [10, 10]]]}}
        ],
    }
    p = page(issues=[drawn])

    def strokes(layer):
        log = p.evaluate(f"drawn('{layer}').log")
        alphas = [e[2] for e in log if e[:2] == ["set", "globalAlpha"]]
        return [e for e in log if e == ["lineTo", 10, 10]], alphas  # not the pin's

    lines, alphas = strokes("Bold")  # closed post-it, its own source: pale
    assert len(lines) == 1 and 0.3 in alphas
    lines, _ = strokes("Light")  # another source: not drawn
    assert lines == []
    p.evaluate("comments.open(1)")
    lines, alphas = strokes("Light")  # open: drawn, full
    assert len(lines) == 1 and 1 in alphas


def test_editing_a_message_keeps_its_strokes_unless_redrawn(page):
    mine = {
        **topic(1, author=ANA),
        "messages": [
            {
                **topic(1)["messages"][0],
                "author": ANA,
                "sketch": {"strokes": [[[0, 0], [9, 9]]]},
            }
        ],
    }
    p = page(you=ANA, can=REVIEWER, issues=[mine])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    edit = "[...card().querySelectorAll('.message .link')].find(b => b.textContent === 'Edit')"
    p.evaluate(f"{edit}.click()")
    p.evaluate("frame()")
    p.evaluate("textarea('edit-1').focus()")
    p.keyboard.press("End")
    p.keyboard.type("!")
    p.keyboard.press("Enter")
    p.wait_for_function("server.requests.some(r => r.method === 'PATCH')")
    assert "sketch" not in requests(p, "PATCH")[-1]["body"]
    # With the pen: the strokes are sent (here, cleared).
    p.wait_for_function("comments.editing === null")
    p.evaluate("frame()")
    p.evaluate(f"{edit}.click()")
    p.evaluate("frame()")
    p.evaluate("card().querySelector('.pen').click()")
    assert p.evaluate("comments.sketch.strokes") == [[[0, 0], [9, 9]]]
    p.evaluate("frame()")
    p.evaluate(
        "[...card().querySelectorAll('.pen-row .link')]"
        ".find(b => b.textContent === 'Clear').click()"
    )
    p.evaluate("frame()")
    p.evaluate("card().querySelector('.compose .primary').click()")
    p.wait_for_function(
        "server.requests.filter(r => r.method === 'PATCH').length === 2"
    )
    assert requests(p, "PATCH")[-1]["body"]["sketch"] is None


# --- step D: topics about the font ---------------------------------------------------


def font_topic(number, author=BOB, state="open", title=None):
    return {
        **topic(number, author=author, state=state),
        "glyph": None,
        "source": None,
        "point": None,
        "title": title,
    }


def test_a_new_font_topic_from_the_panel(page):
    p = page(issues=[topic(1)])
    assert not p.evaluate("panel.newTopicButton.hidden")
    p.evaluate("panel.newTopicButton.click()")
    p.evaluate("frame()")
    assert p.evaluate("card().classList.contains('docked')")
    assert "New topic" in p.evaluate("cardText()")
    assert p.evaluate("card().querySelector('.pen')") is None
    # Docked at the top right of the canvas, whatever its size, clear of
    # Fontra's sidebar tabs (3em wide, over the canvas).
    box = """(() => {
        const c = card().getBoundingClientRect();
        const h = card().parentNode.host.getBoundingClientRect();
        return [c.top - h.top, h.right - c.right];
    })()"""
    assert p.evaluate(box) == [16, 56]
    p.evaluate("document.body.firstChild.style.width = '700px'")
    assert p.evaluate(box) == [16, 56]
    p.evaluate("document.body.firstChild.style.width = '300px'")
    assert p.evaluate("card().getBoundingClientRect().left") >= 0
    p.evaluate("document.body.firstChild.style.width = '900px'")
    p.evaluate(
        """
        const title = card().querySelector('.title-input');
        title.value = ' Family name ';
        title.dispatchEvent(new Event('input'));
        textarea('draft').value = 'Is it final?';
        textarea('draft').dispatchEvent(new Event('input'));
        """
    )
    # Changing glyph does not cancel it: it is about no glyph.
    p.evaluate(
        "editor.sceneController.sceneSettings.selectedGlyphName = 'O';"
        "fireKey('selectedGlyphName')"
    )
    assert p.evaluate("comments.draft.text") == "Is it final?"
    p.evaluate("card().querySelector('button.primary').click()")
    p.wait_for_function("comments.openNumber === 2")
    sent = requests(p, "POST")[-1]
    assert sent["body"] == {"title": "Family name", "text": "Is it final?"}
    assert p.evaluate("comments.issue(2).glyph") is None
    p.evaluate("frame()")
    assert p.evaluate("card().classList.contains('docked')")
    assert "Family name" in p.evaluate("cardText()")
    assert p.evaluate("window.errors") == []


def test_a_reviewer_may_open_one_an_observer_may_not(page):
    p = page(can=REVIEWER)
    assert not p.evaluate("panel.newTopicButton.hidden")
    p = page(can={"comment": False})
    assert p.evaluate("panel.newTopicButton.hidden")
    p.evaluate("comments.startFontTopic()")
    assert p.evaluate("comments.draft") is None


def test_the_card_of_a_font_topic(page):
    p = page(issues=[font_topic(1, author=ANA, title="Name")])
    p.evaluate("comments.show(comments.issue(1))")
    p.evaluate("frame()")
    text = p.evaluate("cardText()")
    assert "#1" in text and "Font" in text and "Name" in text
    assert "undefined" not in text and "null" not in text
    assert "Resolve" in text and "Assigned to" in text
    # No place: no version of a glyph to show, no pen, no pin to draw.
    assert "Written on version" not in text
    assert p.evaluate("card().querySelector('.pen')") is None
    assert p.evaluate("window.drawn().glyphs") == []
    assert p.evaluate("window.wentToSource === undefined")
    # Same thread as a post-it: a reply, a resolution.
    p.evaluate(
        "textarea('reply').value = 'Yes';"
        "textarea('reply').dispatchEvent(new Event('input'))"
    )
    p.evaluate("card().querySelector('.compose button.primary').click()")
    p.wait_for_function("comments.issue(1).messages.length === 2")
    # Disabled while sending: wait for the card redrawn after the reply.
    resolve = "[...card().querySelectorAll('button')].find(b => b.textContent === 'Resolve')"
    p.wait_for_function(f"!{resolve}.disabled")
    p.evaluate(f"{resolve}.click()")
    p.wait_for_function("comments.issue(1).state === 'resolved'")
    p.evaluate("frame()")
    assert p.evaluate("card().classList.contains('resolved')")
    assert p.evaluate("window.errors") == []


def test_font_labels_are_suggested_on_font_topics(page):
    p = page(issues=[font_topic(1), {**topic(2), "labels": ["curve", "Naming"]}])
    labels = "comments.suggestedLabels(comments.issue(%d))"
    assert p.evaluate(labels % 2) == ["curve", "Naming"]
    # "naming" is known already, with its own spelling.
    assert p.evaluate(labels % 1) == ["curve", "Naming", "font info", "production"]


SECTIONS = """[...panel.listElement.querySelectorAll('details.topics')].map((d) => [
    d.querySelector('summary').textContent, d.open,
    [...d.querySelectorAll('.issue')].map((r) => +r.dataset.number)])"""


def test_the_sections_of_the_panel(page):
    p = page(
        issues=[
            topic(1),
            topic(2, state="resolved"),
            font_topic(3, state="resolved"),
            font_topic(4),
            topic(5, glyph="O"),
        ]
    )
    p.evaluate("frame()")
    # A glyph selected: the font first, unfolded, whatever the glyph.
    assert p.evaluate(SECTIONS) == [
        ["Font (2)", True, [4, 3]],
        ["Glyph (1)", True, [1]],
        ["Resolved (1)", True, [2]],
    ]
    # Without a glyph: the same sections, for the whole project.
    p.evaluate(
        "editor.sceneController.sceneSettings.selectedGlyphName = null; panel.render()"
    )
    assert p.evaluate(SECTIONS) == [
        ["Font (2)", True, [4, 3]],
        ["Glyph (2)", True, [1, 5]],
        ["Resolved (1)", True, [2]],
    ]
    row = "panel.listElement.querySelector('.issue[data-number=\"4\"]')"
    assert p.evaluate(f"{row}.querySelector('.icon-typography')")
    assert p.evaluate("window.errors") == []


def test_a_section_stays_folded(page):
    p = page(issues=[topic(1), font_topic(2)])
    p.evaluate("frame()")
    fold = "panel.listElement.querySelector('details.glyph-topics').open = false"
    p.evaluate(f"{fold}; 0")
    p.evaluate("frame()")
    p.evaluate("panel.render()")
    assert p.evaluate(SECTIONS)[1][1] is False
    assert p.evaluate(SECTIONS)[0][1] is True
    # Unless the topic being read is in it.
    p.evaluate("comments.open(1)")
    p.evaluate("panel.render()")
    assert p.evaluate(SECTIONS)[1][1] is True
    assert p.evaluate("window.errors") == []


def test_a_section_without_topics_is_not_shown(page):
    p = page(issues=[font_topic(1)])
    p.evaluate("frame()")
    assert p.evaluate(SECTIONS) == [["Font (1)", True, [1]]]


def test_a_font_topic_has_no_pin_and_leaves_the_canvas_alone(page):
    p = page(issues=[font_topic(1)])
    assert p.evaluate("window.drawn().glyphs") == []
    assert p.evaluate("comments.pinAt({x: 300, y: 184})") is None
    # While one is written, the Comment tool does not start another comment.
    p.evaluate("comments.startFontTopic(); comments.draft.title = 'Kept'")
    p.evaluate("useTool(120, 450)")
    assert p.evaluate("comments.draft.glyph") is None
    assert p.evaluate("comments.draft.title") == "Kept"
    # A click elsewhere keeps what was typed, even a title alone.
    outside = "document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}))"
    p.evaluate(outside)
    assert p.evaluate("comments.draft.title") == "Kept"
    p.evaluate("comments.cancelDraft(); comments.startFontTopic()")
    p.evaluate(outside)
    assert p.evaluate("comments.draft") is None


def test_a_discarded_comment_does_not_follow_into_a_font_topic(page):
    p = page()
    p.evaluate("useTool(120, 450)")
    p.evaluate("frame()")
    p.evaluate(
        """
        textarea('draft').value = 'About the stem of H';
        textarea('draft').dispatchEvent(new Event('input'));
        window.confirm = () => true;
        comments.startFontTopic();
        """
    )
    p.evaluate("frame()")
    assert p.evaluate("textarea('draft').value") == ""
    assert p.evaluate("comments.draft.text") == ""
    assert p.evaluate("comments.draft.glyph") is None


def test_a_title_being_written_is_not_lost_without_asking(page):
    p = page(issues=[font_topic(1)])
    p.evaluate("comments.startFontTopic(); comments.draft.title = 'Kept'")
    p.evaluate("window.asked = 0; window.confirm = () => (window.asked++, false); 0")
    p.evaluate("comments.open(1)")
    assert p.evaluate("window.asked") == 1
    assert p.evaluate("comments.draft.title") == "Kept"
    assert p.evaluate("comments.openNumber") is None


def test_the_hint_of_an_observer_on_a_glyph_without_comments(page):
    p = page(can={"comment": False}, issues=[font_topic(1)])
    p.evaluate("frame()")
    assert p.evaluate("panel.hintElement.textContent") == "No comments on this glyph."
    assert "Font (1)" in p.evaluate("panel.listElement.textContent")


def test_links_to_a_font_topic(page):
    p = page(issues=[font_topic(7)])
    link = "module.issueLink(%r, comments.issue(7), 'https://fontrahive.com')"
    assert (
        p.evaluate(link % "uplustype/My font")
        == "https://fontrahive.com/#project/uplustype/My%20font/comments/7"
    )
    # Without accounts there is no such page: the editor opens it too.
    assert p.evaluate(link % "Mutator") == (
        "https://fontrahive.com/editor.html?project=Mutator&hive-issue=7"
    )


def test_a_link_opens_a_font_topic_without_a_glyph(page):
    p = page(issues=[font_topic(7)])
    p.evaluate(
        """
        sessionStorage.setItem('hive.openIssue',
          JSON.stringify({project: 'Mutator', number: '7'}));
        comments.pendingIssue = comments.takePendingIssue();
        editor.sceneController.sceneSettings.positionedLines = [{glyphs: []}];
        comments.openPendingIssue();
        """
    )
    p.wait_for_function("comments.openNumber === 7")
    p.evaluate("frame()")
    assert p.evaluate("card().classList.contains('docked')")
    assert p.evaluate("comments.pendingIssue") is None


def test_a_deleted_font_topic_is_named_in_the_panel(page):
    p = page()
    p.evaluate(
        """
        server.deleted = [{number: 1, deletedBy: 'Bob', deletedAt: 1790000000,
          issue: {glyph: null, source: null, point: null, messages: [{text: 'Gone'}]}}];
        editor.sceneController.sceneSettings.selectedGlyphName = null;
        comments.dataVersion++;
        panel.render();
        """
    )
    p.wait_for_function("panel.listElement.querySelector('.deleted-issue')")
    assert "Font" in p.evaluate(
        "panel.listElement.querySelector('.deleted-issue .meta').textContent"
    )


# --- step E: mentions and links between topics --------------------------------------


def said(*texts, number=1, **kwargs):
    """A topic whose messages say these things."""
    issue = topic(number, **kwargs)
    issue["messages"] = [
        {**issue["messages"][0], "id": i, "text": text}
        for i, text in enumerate(texts, start=1)
    ]
    return issue


def type_in(p, role, text):
    p.evaluate(f"textarea('{role}').focus()")
    p.keyboard.type(text)


def suggestions(p):
    return p.evaluate(
        "[...comments.overlayRoot.querySelectorAll('.suggestions:not([hidden]) .suggestion')]"
        ".map(s => s.textContent)"
    )


PARTS = """(text) => JSON.parse(JSON.stringify(window.comments.partsOf(text).map(
    (p) => typeof p === "string" ? p : p.kind + ":" + (p.issue?.number ?? p.text))))"""


@pytest.mark.parametrize(
    "text, expected",
    [
        ("plain", ["plain"]),
        ("hi @ana.", ["hi ", "mention:@ana", "."]),
        ("@ANA", ["mention:@ANA"]),
        ("@nobody and a@ana", ["@nobody and a@ana"]),
        ("see #2, then", ["see ", "issue:2", ", then"]),
        ("#99 #000 a#2 #2x", ["#99 #000 a#2 #2x"]),  # no such topic, or not a number
        ("(#2)", ["(", "issue:2", ")"]),
        (
            "go to https://example.com/a?b=1. Now",
            ["go to ", "url:https://example.com/a?b=1", ". Now"],
        ),
        ("mail@ana.fr", ["mail@ana.fr"]),
    ],
)
def test_what_a_message_says(page, text, expected):
    p = page(issues=[topic(1), topic(2)])
    assert p.evaluate(PARTS, text) == expected


def test_a_copied_link_reads_as_the_topic(page):
    p = page(issues=[topic(1), topic(2)])
    origin = p.evaluate("location.origin")
    here = f"{origin}/editor.html?project=Mutator&hive-issue=2"
    assert p.evaluate(PARTS, f"see {here}") == ["see ", "issue:2"]
    elsewhere = f"{origin}/editor.html?project=Other&hive-issue=2"
    assert p.evaluate(PARTS, elsewhere) == [f"url:{elsewhere}"]
    other_site = "https://example.com/editor.html?project=Mutator&hive-issue=2"
    assert p.evaluate(PARTS, other_site) == [f"url:{other_site}"]
    page_link = f"{origin}/#project/o/My%20font/comments/2"
    number = "module.issueNumberInLink(%r, 'o/My font')" % page_link
    assert p.evaluate(number) == 2


PASTE = """(text) => {
    const box = window.textarea("reply");
    box.focus();
    const clipboardData = new DataTransfer();
    clipboardData.setData("text", text);
    const event = new ClipboardEvent("paste", { clipboardData, bubbles: true, cancelable: true });
    box.dispatchEvent(event);
    return [event.defaultPrevented, box.value];
}"""


def test_a_pasted_link_is_a_number_at_once(page):
    p = page(issues=[topic(1), topic(2)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    origin = p.evaluate("location.origin")
    link = f"{origin}/editor.html?project=Mutator&text=%22/H%22&hive-issue=2"
    p.evaluate("textarea('reply').value = 'see '")
    assert p.evaluate(PASTE, link) == [True, "see #2"]
    # And the reply can be sent: the box knows it has changed.
    assert p.evaluate("card().querySelector('.compose button.primary').disabled") is False
    # In the middle of a text; its punctuation stays.
    p.evaluate("textarea('reply').value = ''")
    assert p.evaluate(PASTE, f"a {link}, b") == [True, "a #2, b"]
    # What is no link to a topic here is pasted as it is, by the browser.
    p.evaluate("textarea('reply').value = ''")
    for other in (
        "https://example.com/a",
        f"{origin}/editor.html?project=Other&hive-issue=2",
        f"{origin}/editor.html?project=Mutator&hive-issue=99",
        "just text",
    ):
        assert p.evaluate(PASTE, other)[0] is False
    assert p.evaluate("window.errors") == []


def test_a_message_shows_mentions_and_links(page):
    p = page(
        issues=[said("Ask @ana about #2 and https://example.com"), topic(2, glyph="O")]
    )
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    mention = "card().querySelector('.mention')"
    assert p.evaluate(f"{mention}.textContent") == "@ana"
    assert p.evaluate(f"{mention}.title") == "Ana Reviewer"
    assert p.evaluate("card().querySelector('.text a').rel") == "noopener noreferrer"
    link = "card().querySelector('.issue-link')"
    assert p.evaluate(f"{link}.textContent") == "#2 · O · Topic 2"
    # Text stays text: nothing in a message becomes markup.
    p.evaluate("comments.issues[0].messages[0].text = '<img src=x onerror=boom> @ana'")
    p.evaluate("comments.dataVersion++; comments.changed()")
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('img')") is None
    assert p.evaluate("window.errors") == []


def test_a_link_goes_to_its_topic_and_back(page):
    p = page(issues=[said("See #2"), font_topic(2), said("Also #2", number=3)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    p.evaluate("card().querySelector('.issue-link').click()")
    p.wait_for_function("comments.openNumber === 2")
    p.evaluate("frame()")
    # The way back, from the topic mentioned.
    links = "[...card().querySelectorAll('.mentioned-in')]"
    assert p.evaluate(f"{links}.map(b => b.textContent)") == [
        "#1 · H · See #2",
        "#3 · H · Also #2",
    ]
    assert "Mentioned in" in p.evaluate("cardText()")
    p.evaluate(f"{links}[1].click()")
    p.wait_for_function("comments.openNumber === 3")
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.references')") is None
    # In the panel too; a topic does not mention itself.
    p.evaluate("comments.issues[0].messages[0].text = 'See #2 and #1'")
    p.evaluate("comments.dataVersion++; panel.render()")
    assert "mentioned in #1, #3" in p.evaluate("panel.listElement.textContent")
    assert p.evaluate("comments.mentionedIn(1)") == []
    assert p.evaluate("window.errors") == []


def test_mentioned_in_is_on_its_own_line_in_the_panel(page):
    p = page(issues=[said("See #2"), topic(2, replies=[(ANA, "x")])])
    p.evaluate("frame()")
    line = "panel.listElement.querySelector('.issue[data-number=\"2\"] .mentioned')"
    assert p.evaluate(f"{line}.textContent") == "mentioned in #1"
    # Beside the badge, under the line that a long text cuts.
    box = f"{line}.getBoundingClientRect()"
    meta = "panel.listElement.querySelector('.issue[data-number=\"2\"] .meta')"
    assert p.evaluate(f"{box}.top") > p.evaluate(f"{meta}.getBoundingClientRect().top")
    badge = "panel.listElement.querySelector('.issue[data-number=\"2\"] .badge')"
    assert p.evaluate(f"{box}.left") > p.evaluate(f"{badge}.getBoundingClientRect().right")


def test_see_all_comments_from_the_panel(page):
    p = page()
    # Without accounts there is no such page.
    assert p.evaluate("panel.seeAllLink") is None
    assert p.evaluate("panel.shadowRoot.querySelector('.see-all')") is None
    p.evaluate("editor.projectIdentifier = 'uplustype/My font@wide'")
    assert p.evaluate(
        "new module.HiveCommentsPanel(new module.HiveComments(editor)).seeAllLink.href"
    ).endswith("/#project/uplustype/My%20font/comments")
    assert p.evaluate(
        "new module.HiveCommentsPanel(new module.HiveComments(editor)).seeAllLink.target"
    ) == "hive-comments-uplustype/My font"


def test_mentioned_in_follows_the_messages(page):
    p = page(issues=[topic(1), topic(2)])
    assert p.evaluate("comments.mentionedIn(2)") == []
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    p.evaluate("comments.reply(1, 'See #2')")
    p.wait_for_function("comments.mentionedIn(2).length === 1")
    assert p.evaluate("comments.mentionedIn(2)") == [1]


def test_typing_at_proposes_the_members(page):
    p = page(issues=[topic(1)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    type_in(p, "reply", "Ask @")
    # Not oneself (Ana).
    assert suggestions(p) == ["bobBob Designer"]
    p.keyboard.type("x")
    assert suggestions(p) == []
    p.keyboard.press("Backspace")
    p.keyboard.type("Bo")
    assert suggestions(p) == ["bobBob Designer"]
    p.keyboard.press("Enter")  # takes the name, sends nothing
    assert p.evaluate("textarea('reply').value") == "Ask @bob "
    assert requests(p, "POST") == []
    assert suggestions(p) == []
    p.keyboard.type("now")
    p.keyboard.press("Enter")
    p.wait_for_function("comments.issue(1).messages.length === 2")
    assert requests(p, "POST")[-1]["body"]["text"] == "Ask @bob now"


def test_typing_hash_proposes_the_topics(page):
    p = page(issues=[topic(n) for n in (1, 2, 12)] + [font_topic(13, title="Family")])
    p.evaluate("comments.open(2)")
    p.evaluate("frame()")
    type_in(p, "reply", "See #")
    # Newest first, not the topic being read.
    assert suggestions(p) == [
        "#13Font · Family",
        "#12H · Topic 12",
        "#1H · Topic 1",
    ]
    p.keyboard.type("1")
    # What is typed exactly comes first.
    assert suggestions(p)[0] == "#1H · Topic 1"
    p.keyboard.press("Backspace")
    p.keyboard.type("fam")
    assert suggestions(p) == ["#13Font · Family"]
    p.keyboard.press("Tab")
    assert p.evaluate("textarea('reply').value") == "See #13 "


def test_the_keys_and_the_mouse_on_the_list(page):
    p = page(issues=[topic(1), topic(2), topic(3)])
    p.evaluate("comments.open(3)")
    p.evaluate("frame()")
    type_in(p, "reply", "#")
    p.keyboard.press("ArrowDown")
    chosen = "comments.overlayRoot.querySelector('.suggestion.chosen').textContent"
    assert p.evaluate(chosen).startswith("#1")
    p.keyboard.press("ArrowDown")  # around
    assert p.evaluate(chosen).startswith("#2")
    p.keyboard.press("ArrowUp")
    assert p.evaluate(chosen).startswith("#1")
    # Escape closes the list, not the post-it.
    p.keyboard.press("Escape")
    assert suggestions(p) == []
    assert p.evaluate("comments.openNumber") == 3
    p.keyboard.type("2")
    assert suggestions(p) == ["#2H · Topic 2"]
    p.evaluate(
        "comments.overlayRoot.querySelector('.suggestion')"
        ".dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}))"
    )
    assert p.evaluate("textarea('reply').value") == "#2 "
    # Not a mention in the middle of a word; nor out of the box.
    p.keyboard.type("C#")
    assert suggestions(p) == []
    p.keyboard.type(" @")
    assert suggestions(p) != []
    p.evaluate("textarea('reply').blur()")
    assert suggestions(p) == []
    assert p.evaluate("window.errors") == []


def test_a_list_stays_in_the_canvas_near_its_top(page):
    p = page(issues=[font_topic(1), font_topic(2)])
    p.evaluate("comments.startFontTopic()")
    p.evaluate("frame()")
    type_in(p, "draft", "#")
    box = p.evaluate(
        "comments.overlayRoot.querySelector('.suggestions').getBoundingClientRect().top"
    )
    assert box >= 0


def test_a_broken_address_in_a_message_breaks_nothing(page):
    origin = "location.origin + '/#project/%ff/comments/1'"
    p = page(issues=[topic(1), topic(2)])
    p.evaluate(f"comments.issues[1].messages[0].text = {origin}; comments.dataVersion++")
    p.evaluate("comments.open(2)")
    p.evaluate("frame()")
    assert p.evaluate("card().querySelector('.text a')")
    assert p.evaluate("comments.mentionedIn(1)") == []
    assert p.evaluate("window.errors") == []


def test_addresses_keep_what_belongs_to_them(page):
    p = page()
    wiki = "https://en.wikipedia.org/wiki/Garamond_(typeface)"
    assert p.evaluate(PARTS, f"see {wiki}.") == ["see ", f"url:{wiki}", "."]
    assert p.evaluate(PARTS, f"({wiki})") == ["(", f"url:{wiki}", ")"]
    assert p.evaluate(PARTS, "(https://a.fr/b)") == ["(", "url:https://a.fr/b", ")"]


def test_the_list_follows_the_caret(page):
    p = page(issues=[topic(1)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    type_in(p, "reply", "Look @bo at this")
    for _ in " at this":
        p.keyboard.press("ArrowLeft")
    assert suggestions(p) == ["bobBob Designer"]
    # Out of the word (with the arrows: End moves nothing on a Mac): Enter
    # sends the whole text.
    for _ in " at this":
        p.keyboard.press("ArrowRight")
    assert suggestions(p) == []
    p.keyboard.press("Enter")
    p.wait_for_function("comments.issue(1).messages.length === 2")
    assert requests(p, "POST")[-1]["body"]["text"] == "Look @bo at this"


def test_enter_with_the_caret_moved_without_a_key_event(page):
    p = page(issues=[topic(1)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    type_in(p, "reply", "@bo rest")
    for _ in " rest":
        p.keyboard.press("ArrowLeft")
    assert suggestions(p) != []
    p.evaluate("textarea('reply').setSelectionRange(8, 8)")  # as a keyboard shortcut may
    p.keyboard.press("Enter")
    p.wait_for_function("comments.issue(1).messages.length === 2")
    assert requests(p, "POST")[-1]["body"]["text"] == "@bo rest"


def test_the_list_stays_through_a_refresh_of_the_card(page):
    p = page(issues=[topic(1), {**topic(2)}])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    type_in(p, "reply", "Ask @bo")
    p.evaluate("comments.you = {...comments.you, username: 'someone'}; comments.changed()")
    p.evaluate("frame()")
    assert p.evaluate("textarea('reply').value") == "Ask @bo"
    assert suggestions(p) == ["bobBob Designer"]
    p.keyboard.press("Enter")
    assert p.evaluate("textarea('reply').value") == "Ask @bob "
    assert requests(p, "POST") == []


def test_the_list_starts_again_at_its_top(page):
    p = page(issues=[topic(n) for n in (1, 2, 3, 12)])
    p.evaluate("comments.open(3)")
    p.evaluate("frame()")
    type_in(p, "reply", "#")
    p.keyboard.press("ArrowDown")
    p.keyboard.press("ArrowDown")
    p.keyboard.press("Enter")
    assert p.evaluate("textarea('reply').value") == "#1 "
    p.keyboard.type("#1")
    p.keyboard.press("Enter")  # #1, not the third line of before
    assert p.evaluate("textarea('reply').value") == "#1 #1 "


def test_shift_enter_is_a_new_line_even_with_the_list(page):
    p = page(issues=[topic(1)])
    p.evaluate("comments.open(1)")
    p.evaluate("frame()")
    type_in(p, "reply", "@bo")
    p.keyboard.press("Shift+Enter")
    assert p.evaluate("textarea('reply').value") == "@bo\n"
    assert suggestions(p) == []
