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
      const issue = { number: server.issues.length + 1, glyph: body.glyph,
        source: body.source, point: body.point, branch: body.branch, commit: "c",
        state: "open", author: person, created: now, resolved: null,
        assignee: null, labels: [],
        messages: [{ id: 1, author: person, created: now, edited: null, text: body.text }] };
      server.issues.push(issue);
      return json({ head: bump(), issue });
    }
    const m = path.match(/^\\/(\\d+)(\\/messages(?:\\/(\\d+))?)?$/);
    const issue = m && server.issues.find((i) => i.number === +m[1]);
    if (!issue) return new Response("no such comment", { status: 404 });
    if (server.refuse) return new Response(server.refuse, { status: 403 });
    if (method === "POST" && m[2]) {
      issue.messages.push({ id: issue.messages.length + 1, author: person,
        created: now, edited: null, text: body.text });
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
ALL = {"comment": True, "resolveAny": True, "organize": True, "moderate": True}
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
    assert "pinned to a source" in p.evaluate(
        "comments.overlayRoot.querySelector('.toast').textContent"
    )


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
    p.evaluate("card().querySelector('.show-version').click()")
    assert p.evaluate("window.shownVersion") == [
        "abcdef1234",
        "H",
        "the version commented in #1",
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
