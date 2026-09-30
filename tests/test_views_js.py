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
    const parsed = new URL(url, location.href);
    const path = parsed.pathname;
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : null;
    window.calls.push({ path, method, body, query: Object.fromEntries(parsed.searchParams) });
    const json = (data, status = 200) => new Response(JSON.stringify(data), { status });
    if (path === "/api/hive/me") return json(fake.me);
    if (path.endsWith("/branches") && fake.branches) {
      if (method === "GET") return json(fake.branches);
      const name = parsed.searchParams.get("name");
      if (method === "POST" && name === "taken") {
        return new Response("There is already a branch 'taken'.", { status: 409 });
      }
      if (method === "POST") return json({ branch: { name } });
      return json({ deleted: parsed.searchParams.get("branch") });
    }
    if (path.endsWith("/snapshots")) return json({ snapshots: fake.snapshots || [] });
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
                "name": "Fabio Rossi",
                "role": "designer",
                "view": "editor",
                "branch": "main",
                "glyph": "A",
            },
            {
                "username": "fabio",
                "name": "Fabio Rossi",
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


def open_page(
    browser_and_url, role="admin", storage=None, project="Mutator", data=None
):
    browser, base = browser_and_url
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route(
        "**/editor.html*",
        lambda route: route.fulfill(
            content_type="text/html",
            body=PAGE % {"fake": json.dumps({**fake(role), **(data or {})})},
        ),
    )
    if storage is not None:
        page.add_init_script(
            f"localStorage.setItem('fontra.pluginsplugins', {json.dumps(json.dumps(storage))})"
        )
    page.goto(f"{base}/editor.html?project={project}")
    start_hive(page)
    page.errors = errors
    return page


def start_hive(page, projectName=None):
    """Run Hive's view script, as at the end of Fontra's page; Fontra would
    have written ``projectName`` in the top bar."""
    if projectName:
        page.evaluate(
            "document.getElementById('fontra-project-name').textContent = "
            + json.dumps(projectName)
        )
    page.evaluate("""async () => {
          const m = await import('/views/hive-views.js');
          window.hiveModule = m;
          window.hive = await m.start();
        }""")


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
        ["FR", "Fabio Rossi · editing “A”\nFabio Rossi · in the font overview"]
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
    assert "Fabio Rossi · editing “A”" in menu and "Sign out" in menu
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


def test_labels_translated_too_early_are_fixed(browser_and_url):
    browser, base = browser_and_url
    page = browser.new_page()
    page.route(
        "**/lang/fr.js*",
        lambda route: route.fulfill(
            content_type="text/javascript",
            body="export const strings = "
            + '{"glyph-organizing.group-by.script": "Système d’écriture"};',
        ),
    )
    page.route(
        "**/overview.html*",
        lambda route: route.fulfill(
            content_type="text/html",
            body="""<!doctype html><html><head><script>
              window.__hiveViewsNoAutoStart = true;
              localStorage.setItem("fontra-language-language", JSON.stringify("fr"));
            </script></head><body><div id=host></div>
            <label> glyph-organizing.group-by.script </label>
            <p>glyph-organizing.unknown</p><p>Some text.</p></body></html>""",
        ),
    )
    page.goto(f"{base}/overview.html")
    page.evaluate("""async () => {
          const m = await import('/views/hive-views.js');
          await m.fixEarlyTranslations();
          // A component rendering later, in its shadow root.
          const host = document.createElement('div');
          host.id = 'host2';
          const shadow = host.attachShadow({ mode: 'open' });
          const box = document.createElement('div');
          shadow.append(box);
          document.getElementById('host').append(host);
          document.body.append(document.createElement('span'));  // noise
          await new Promise((r) => setTimeout(r, 50));
          box.append(document.createTextNode('glyph-organizing.group-by.script'));
          await new Promise((r) => setTimeout(r, 50));
        }""")
    assert (
        page.evaluate("document.querySelector('label').textContent")
        == " Système d’écriture "
    )
    assert (
        page.evaluate("document.querySelector('p').textContent")
        == "glyph-organizing.unknown"
    )
    assert (
        page.evaluate("document.getElementById('host2').shadowRoot.textContent")
        == "Système d’écriture"
    )
    page.close()


# --- branches -----------------------------------------------------------------------

NOW = 1_790_000_000


def branches(can_delete=False):
    return {
        "default": "main",
        "you": "jeremie",
        "can": {"create": True, "delete": can_delete, "merge": can_delete},
        "branches": [
            {
                "name": "main",
                "isDefault": True,
                "ahead": 0,
                "behind": 0,
                "author": "Fabio Rossi",
                "time": NOW - 120,
                "message": "Edit A",
                "createdBy": None,
                "open": True,
            },
            {
                "name": "bold",
                "isDefault": False,
                "ahead": 2,
                "behind": 1,
                "author": "Ana López",
                "time": NOW - 7200,
                "message": "Bolder B",
                "createdBy": "jeremie",
                "open": False,
            },
            {
                "name": "zoe/wide",
                "isDefault": False,
                "ahead": 0,
                "behind": 3,
                "author": "Zoé",
                "time": NOW - 86400 * 3,
                "message": "Wide",
                "createdBy": "zoe",
                "open": True,
            },
        ],
    }


def branch_data(**kwargs):
    others = fake()["others"] + [
        {
            "username": "zoe",
            "name": "Zoé",
            "role": "designer",
            "view": "editor",
            "branch": "bold",
            "glyph": "C",
        },
    ]
    return {
        "branches": branches(**kwargs),
        "others": others,
        "snapshots": [
            {"name": "client-review", "title": "Client review", "time": NOW - 60}
        ],
    }


def freeze_time(page):
    page.evaluate(f"Date.now = () => {NOW * 1000}")


def menu_rows(page):
    return page.evaluate("""[...document.querySelectorAll('.branch-row')].map(r => ({
        name: r.dataset.branch,
        check: r.querySelector('.check').textContent,
        text: r.querySelector('small').textContent,
        faces: [...r.querySelectorAll('.faces .avatar')].map(a => a.textContent),
        del: r.querySelector('button.delete') ? !r.querySelector('button.delete').disabled : null,
      }))""")


def test_branch_pill_and_menu(browser_and_url):
    page = open_page(browser_and_url, data=branch_data())
    freeze_time(page)
    order = page.evaluate(
        "[...document.querySelector('.hive-right').children].map(e => e.className || e.id)"
    )
    assert order == ["hive-stack", "fontra-project-name", "hive-branch", "hive-chip"]
    page.wait_for_function("document.querySelector('.hive-branch span:nth-child(2)')")
    assert page.inner_text(".hive-branch").startswith("main")
    assert "off-default" not in page.get_attribute(".hive-branch", "class")

    page.click(".hive-branch")
    page.wait_for_selector(".branch-row")
    rows = menu_rows(page)
    assert [r["name"] for r in rows] == ["main", "bold", "zoe/wide"]
    assert [r["check"] for r in rows] == ["✓", "", ""]
    assert rows[0]["text"] == "default branch · 2 min ago · Fabio Rossi"
    assert rows[1]["text"] == "2 ahead · 1 behind main · 2 h ago · Ana López"
    assert rows[2]["text"] == "nothing new · 3 behind main · 3 days ago · Zoé"
    # Who is where: Fabio on main, Zoé on bold.
    assert rows[0]["faces"] == ["FR"] and rows[1]["faces"] == ["ZO"]
    # A designer deletes the branches they made, not the others'.
    assert [r["del"] for r in rows] == [None, True, None]
    # The same pill closes it; the user chip opens its own menu instead.
    page.click(".hive-branch")
    assert page.evaluate("document.querySelector('.hive-menu')") is None
    page.click(".hive-branch")
    page.wait_for_selector(".branch-row")
    page.click(".hive-chip")
    assert page.evaluate("document.querySelector('.branch-row')") is None
    assert "Sign out" in page.inner_text(".hive-menu")
    page.mouse.click(5, 300)

    # Another branch: the same page, with only the project changed.
    page.evaluate("history.replaceState(null, '', location.href + '&text=AB#x')")
    page.click(".hive-branch")
    page.wait_for_selector(".branch-row")
    page.click(".branch-row[data-branch='bold']")
    page.wait_for_url("**project=Mutator%40bold**")
    assert page.url.endswith("/editor.html?project=Mutator%40bold&text=AB#x")
    assert page.errors == []
    page.close()


def test_managers_delete_any_branch_but_not_the_one_open(browser_and_url):
    page = open_page(browser_and_url, data=branch_data(can_delete=True))
    page.click(".hive-branch")
    page.wait_for_selector(".branch-row")
    # main: never; zoe/wide: someone has it open.
    assert [r["del"] for r in menu_rows(page)] == [None, True, False]
    page.close()


def test_off_the_default_branch(browser_and_url):
    page = open_page(browser_and_url, project="Mutator@bold", data=branch_data())
    start_hive(page, "Mutator · bold")  # what Fontra writes off the default branch
    page.wait_for_function(
        "document.querySelectorAll('.hive-branch.off-default').length === 2"
    )
    # Fontra's name no longer repeats the branch: the pill says it.
    names = page.evaluate(
        "[...document.querySelectorAll('#fontra-project-name')].map(e => e.textContent)"
    )
    assert names[-1] == "Mutator"
    beat = page.evaluate("calls.filter(c => c.path.endsWith('/presence')).pop()")
    assert beat["body"]["branch"] == "bold"
    page.close()


def test_new_branch_dialog(browser_and_url):
    page = open_page(browser_and_url, data=branch_data())
    page.wait_for_function("hive.branches")
    page.click(".hive-branch")
    page.wait_for_selector(".branch-row")
    page.click("text=New branch…")
    page.wait_for_selector(
        "select[aria-label='Start from'] option:nth-child(2)", state="attached"
    )
    options = page.evaluate(
        "[...document.querySelectorAll('select[aria-label=\"Start from\"] option')]"
        ".map(o => [o.value, o.textContent])"
    )
    assert options[0] == ["main", "main, as it is now"]
    assert options[1][0] == "snapshot/client-review"
    assert options[1][1].startswith("Snapshot “Client review”")
    create = ".hive-dialog button.blue"
    assert page.is_disabled(create)
    page.fill("input[aria-label='Branch name']", "a b")
    assert page.is_disabled(create)
    assert "letters" in page.inner_text(".hive-dialog .error")
    # Keys typed there do not reach Fontra's shortcuts.
    page.evaluate(
        "window.keys = 0; window.addEventListener('keydown', () => window.keys++)"
    )
    page.fill("input[aria-label='Branch name']", "")
    page.type("input[aria-label='Branch name']", "taken")
    assert page.evaluate("window.keys") == 0
    page.click(create)
    page.wait_for_function("document.querySelector('.hive-dialog .error').textContent")
    assert "already a branch" in page.inner_text(".hive-dialog .error")
    page.fill("input[aria-label='Branch name']", "ana/italic")
    page.select_option("select[aria-label='Start from']", "snapshot/client-review")
    page.press("input[aria-label='Branch name']", "Enter")
    # Made: the page opens on the new branch.
    page.wait_for_url("**project=Mutator%40ana%2Fitalic**")
    page.close()


def test_new_branch_request(browser_and_url):
    page = open_page(browser_and_url, data=branch_data())
    page.wait_for_function("hive.branches")
    page.evaluate("hive.gotoBranch = (name) => { window.went = name; }")
    page.evaluate("hive.openNewBranch()")
    page.fill("input[aria-label='Branch name']", "ana/italic")
    page.click(".hive-dialog button.blue")
    page.wait_for_function("window.went")
    assert page.evaluate("window.went") == "ana/italic"
    post = page.evaluate(
        "calls.find(c => c.method === 'POST' && c.path.endsWith('/branches'))"
    )
    assert post["query"] == {"name": "ana/italic", "from": "main"}
    assert page.evaluate("document.querySelector('.hive-dialog')") is None
    page.close()


def test_delete_branch_dialog(browser_and_url):
    page = open_page(browser_and_url, data=branch_data())
    page.click(".hive-branch")
    page.wait_for_selector(".branch-row")
    page.click(".branch-row[data-branch='bold'] button.delete")
    text = page.inner_text(".hive-dialog")
    assert "Delete the branch “bold”?" in text
    assert "2 of its changes are not in main" in text and "archive/bold" in text
    page.click(".hive-dialog button.red")
    page.wait_for_function("!document.querySelector('.hive-dialog')")
    deleted = page.evaluate("calls.find(c => c.method === 'DELETE')")
    assert deleted["path"] == "/api/hive/projects/Mutator/branches"
    assert deleted["query"] == {"branch": "bold"}
    assert page.errors == []
    page.close()


def test_branch_helpers(browser_and_url):
    page = open_page(browser_and_url)
    result = page.evaluate("""() => {
        const m = window.hiveModule;
        const now = 1790000000;
        return {
          url: [
            m.branchURL('http://x/editor.html?project=o%2Fp%40old&text=A', 'o/p', 'new', 'main'),
            m.branchURL('http://x/fontoverview.html?project=o%2Fp%40old', 'o/p', 'main', 'main'),
          ],
          times: [5, 600, 3 * 3600, 30 * 3600, 5 * 86400].map((d) => m.timeAgo(now - d, now)),
          compare: [m.compareWithDefault({isDefault: false, ahead: 3, behind: 0}, 'main'),
                    m.compareWithDefault({isDefault: false, ahead: 0, behind: 0}, 'main')],
          names: ['bold', 'ana/italic', '', 'a b', 'a..b', 'archive/x', 'x/']
            .map(m.branchNameProblem),
        };
      }""")
    assert result["url"] == [
        "http://x/editor.html?project=o%2Fp%40new&text=A",
        "http://x/fontoverview.html?project=o%2Fp",
    ]
    assert result["times"] == [
        "just now",
        "10 min ago",
        "3 h ago",
        "yesterday",
        "5 days ago",
    ]
    assert result["compare"] == ["3 ahead of main", "same as main"]
    names = result["names"]
    assert names[:2] == [None, None] and all(names[2:])
    page.close()
