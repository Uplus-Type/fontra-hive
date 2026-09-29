"""Hive's script in Fontra's views (client/views/*.js), in a headless browser,
against a fake Fontra page and a mocked server. Skipped without Playwright."""

import functools
import http.server
import json
import os
import pathlib
import threading

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

CLIENT_DIR = pathlib.Path(__file__).parent.parent / "src" / "fontra_hive" / "client"
CHROMIUM = "/opt/pw-browsers/chromium"

MEMBERS = {
    "members": [
        {
            "username": "jeremie",
            "name": "Jérémie Hornus",
            "email": "j@x",
            "role": "admin",
            "via": "organization U+Type",
            "collaboratorRole": None,
        },
        {
            "username": "ana",
            "name": "Ana López",
            "email": "a@x",
            "role": "observer",
            "via": "collaborator",
            "collaboratorRole": "observer",
        },
    ],
    "canManage": True,
    "you": "jeremie",
    "users": [{"username": "zoe", "name": "Zoé"}],
    "roles": ["observer", "reviewer", "designer", "manager", "admin"],
    "accounts": True,
}

# A Fontra-like editor page: the top bar with the project name Fontra adds,
# a view controller on window, and fetch answered by a fake server.
PAGE = """<!doctype html><html><head>
<script src="/views/register.js"></script>
<script>
  window.__hiveViewsNoAutoStart = true;
  window.calls = [];
  window.fake = %(fake)s;
  window.fetch = async (url, options = {}) => {
    const path = new URL(url, location.href).pathname;
    const body = options.body ? JSON.parse(options.body) : null;
    window.calls.push({ path, method: options.method || "GET", body });
    const json = (data, status = 200) => new Response(JSON.stringify(data), { status });
    if (path === "/api/hive/me") return json(fake.me);
    if (path.endsWith("/access")) return json(fake.access);
    if (path.endsWith("/presence")) return json({ others: fake.others });
    if (path.endsWith("/members")) {
      if (body && body.username === "boss") return new Response("no", { status: 409 });
      return json(fake.members);
    }
    return new Response("", { status: 404 });
  };
  window.editorController = {
    sceneSettings: { selectedGlyphName: "B" },
    fontController: { backendInfo: { projectManagerFeatures: {} } },
  };
</script>
</head><body>
<style>
  .top-bar-container { position: relative; display: grid; height: 35px;
                       grid-template-columns: auto auto; justify-content: space-between; }
</style>
<div class="top-bar-container">
  <div class="menubar">Fontra File Edit</div>
  <div id="fontra-project-name">Mutator</div>
</div>
</body></html>"""


def fake(role="admin"):
    return {
        "me": {
            "user": {"username": "jeremie", "name": "Jérémie Hornus", "email": "j@x"},
            "accounts": True,
        },
        "access": {
            "role": role,
            "capabilities": (
                ["read"] if role == "observer" else ["read", "edit", "invite"]
            ),
        },
        "others": [
            {
                "username": "fabio",
                "name": "Fabio Caccamo",
                "role": "designer",
                "view": "editor",
                "branch": "main",
                "glyph": "A",
            },
            {
                "username": "fabio",
                "name": "Fabio Caccamo",
                "role": "designer",
                "view": "fontoverview",
                "branch": "main",
                "glyph": None,
            },
        ],
        "members": MEMBERS,
    }


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


def open_page(browser_and_url, role="admin", storage=None):
    browser, base = browser_and_url
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route(
        "**/editor.html*",
        lambda route: route.fulfill(
            content_type="text/html", body=PAGE % {"fake": json.dumps(fake(role))}
        ),
    )
    if storage is not None:
        page.add_init_script(
            f"localStorage.setItem('fontra.pluginsplugins', {json.dumps(json.dumps(storage))})"
        )
    page.goto(f"{base}/editor.html?project=Mutator")
    page.evaluate("""async () => {
          const m = await import('/views/hive-views.js');
          window.hiveModule = m;
          window.hive = await m.start();
        }""")
    page.errors = errors
    return page


def test_the_history_plugin_registers_itself(browser_and_url):
    page = open_page(browser_and_url, storage=[{"address": "someone/else"}])
    plugins = json.loads(page.evaluate("localStorage.getItem('fontra.pluginsplugins')"))
    assert plugins == [{"address": "someone/else"}, {"address": "/hive/plugin"}]
    # Idempotent: loading again does not add it twice.
    page.reload()
    plugins = json.loads(page.evaluate("localStorage.getItem('fontra.pluginsplugins')"))
    assert [p["address"] for p in plugins].count("/hive/plugin") == 1
    page.close()


def test_top_bar_chip_presence_and_menu(browser_and_url):
    page = open_page(browser_and_url)
    # Our block sits in the top bar, with Fontra's project name moved into it.
    assert (
        page.evaluate(
            "document.querySelector('.hive-right #fontra-project-name')?.textContent"
        )
        == "Mutator"
    )
    assert (
        page.evaluate("document.querySelector('.hive-chip .avatar').textContent")
        == "JH"
    )
    # Two tabs of the same person count once; the tooltip says where they are.
    stack = page.evaluate(
        "[...document.querySelectorAll('.hive-stack .avatar')].map(a => [a.textContent, a.title])"
    )
    assert stack == [
        ["FC", "Fabio Caccamo · editing “A”\nFabio Caccamo · in the font overview"]
    ]
    # The heartbeat says where we are.
    beat = page.evaluate("calls.find(c => c.path.endsWith('/presence'))")
    assert beat["method"] == "POST" and beat["body"]["view"] == "editor"
    assert beat["body"]["glyph"] == "B" and beat["body"]["branch"] == "main"
    assert page.evaluate("document.querySelector('.hive-badge')") is None  # admin
    # The user menu.
    page.click(".hive-chip")
    menu = page.evaluate("document.querySelector('.hive-menu').innerText")
    assert "Jérémie Hornus" in menu and "admin" in menu and "Also here" in menu
    assert "Fabio Caccamo · editing “A”" in menu and "Sign out" in menu
    assert page.evaluate(
        "[...document.querySelectorAll('.hive-menu a')].map(a => a.getAttribute('href'))"
    ) == ["/", None, "/hive/logout"]
    page.mouse.click(5, 300)  # a click elsewhere closes it
    assert page.evaluate("document.querySelector('.hive-menu')") is None
    assert page.errors == []
    page.close()


def test_read_only_badge(browser_and_url):
    page = open_page(browser_and_url, role="observer")
    assert page.evaluate("document.querySelector('.hive-badge').textContent") == (
        "Read only · observer"
    )
    page.close()


def test_file_menu_and_share_dialog(browser_and_url):
    page = open_page(browser_and_url)
    items = page.evaluate(
        "editorController.getFileMenuItems().map(i => [i.title, i.enabled ? i.enabled() : true])"
    )
    assert items == [["Share…", True], ["-", True], ["New", False], ["Open", False]]
    page.evaluate("editorController.getFileMenuItems()[0].callback()")
    page.wait_for_selector(".hive-dialog .hive-member")
    rows = page.evaluate(
        "[...document.querySelectorAll('.hive-member')].map(r => r.innerText.replace(/\\s+/g, ' '))"
    )
    assert (
        rows[0].startswith("JH Jérémie Hornus (you)")
        and "admin · organization U+Type" in rows[0]
    )
    assert "Ana López" in rows[1]
    # Change Ana's role, remove her, add Zoé: each is a POST to members.
    page.select_option("select[aria-label='Role of Ana López']", "designer")
    page.wait_for_timeout(50)
    page.click(".hive-member[data-username='ana'] .remove")
    page.wait_for_timeout(50)
    page.select_option("select[aria-label='Person to add']", "zoe")
    page.click(".hive-dialog .add button")
    page.wait_for_timeout(50)
    posts = page.evaluate(
        "calls.filter(c => c.path.endsWith('/members') && c.method === 'POST').map(c => c.body)"
    )
    assert posts == [
        {"username": "ana", "role": "designer"},
        {"username": "ana", "role": None},
        {"username": "zoe", "role": "designer"},
    ]
    # Keys typed in the dialog do not reach Fontra's shortcuts; Escape closes it.
    page.evaluate(
        """() => { window.keys = 0; window.addEventListener('keydown', () => window.keys++);
                   document.querySelector('.hive-dialog select').dispatchEvent(
                     new KeyboardEvent('keydown', { key: 'v', bubbles: true })); }"""
    )
    assert page.evaluate("window.keys") == 0
    page.keyboard.press("Escape")
    assert page.evaluate("document.querySelector('.hive-dialog')") is None
    assert page.errors == []
    page.close()


def test_helpers(browser_and_url):
    page = open_page(browser_and_url)
    result = page.evaluate("""() => {
          const m = window.hiveModule;
          return [m.initials('Jérémie Hornus'), m.initials('ana'), m.initials('Just van Rossum'),
                  m.avatarColor('ana') === m.avatarColor('ana'),
                  m.describePresence({name: 'A', view: 'fontinfo', branch: 'bold'})];
        }""")
    assert result == ["JH", "AN", "JR", True, "A · in font info · bold"]
    page.close()
