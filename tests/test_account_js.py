"""Hive with hive-api, in a headless browser: the account pages
(client/account) and the Share dialog in hive-api mode, against a mocked
server. Skipped without Playwright."""

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

# fetch answered from window.answers: {"METHOD path": [status, body]}; every
# call is recorded in window.calls. location.replace is recorded, not done.
MOCK = """
window.calls = [];
window.went = null;
window.fetch = async (url, options = {}) => {
  const parsed = new URL(url, location.href);
  const path = parsed.pathname;
  const method = options.method || "GET";
  const body = options.body ? JSON.parse(options.body) : null;
  window.calls.push({ method, path, body });
  const [status, data] = window.answers[`${method} ${path}${parsed.search}`] ||
    window.answers[`${method} ${path}`] || [404, { detail: "Not found." }];
  return new Response(data === null ? null : JSON.stringify(data), { status });
};
"""


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


def account_page(browser_and_url, name, answers, fragment=""):
    """One of the account pages, its module started by hand after the mock."""
    browser, base = browser_and_url
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    html = (CLIENT_DIR / "account" / name).read_text()
    html = html.replace(
        '<script type="module" src="/hive/account/account.js"></script>',
        "<script>window.__hiveAccountNoAutoStart = true;"
        + MOCK
        + f"window.answers = {json.dumps(answers)};</script>",
    )
    page.route(
        "**/page.html*",
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    page.goto(f"{base}/page.html{fragment}")
    page.evaluate("""async () => {
        const m = await import('/account/account.js');
        window.accountModule = m;
      }""")
    page.errors = errors
    return page


def test_login_page(browser_and_url):
    answers = {
        "POST /api/auth/refresh": [401, {"detail": "Please sign in again."}],
        "POST /api/auth/login": [401, {"detail": "Incorrect username or password."}],
    }
    page = account_page(browser_and_url, "login.html", answers)
    # Run the page's code with location.replace captured.
    page.evaluate("""() => {
          const m = window.accountModule;
          window.safe = [m.safeRef('/editor.html?project=a/b'), m.safeRef('//evil.example'),
                         m.safeRef('https://evil.example'), m.safeRef(null)];
        }""")
    assert page.evaluate("window.safe") == ["/editor.html?project=a/b", "/", "/", "/"]
    # Run the page itself: a second copy of the module, which starts.
    page.evaluate("""async () => {
          window.__hiveAccountNoAutoStart = false;
          await import('/account/account.js?start=1');
        }""")
    page.wait_for_selector("#login:not(.hidden)")
    page.fill("input[name=login]", "fabio")
    page.fill("#login input[name=password]", "nope")
    page.click("button[type=submit]")
    page.wait_for_function("document.querySelector('.error').textContent.length > 0")
    assert page.inner_text(".error") == "Incorrect username or password."
    login = page.evaluate("calls.find(c => c.path === '/api/auth/login')")
    assert login["body"] == {"login": "fabio", "password": "nope"}
    assert page.errors == []
    page.close()


def test_request_an_invitation_from_the_sign_in_page(browser_and_url):
    answers = {
        "POST /api/auth/refresh": [401, {"detail": "Please sign in again."}],
        "POST /api/waitlist": [202, None],
    }
    page = account_page(browser_and_url, "login.html", answers, "#request")
    page.evaluate("""async () => {
          window.__hiveAccountNoAutoStart = false;
          await import('/account/account.js?start=1');
        }""")
    # Opened directly by /#request (a link from a landing page).
    page.wait_for_selector("#request:not(.hidden)")
    assert page.evaluate("document.activeElement.name") == "email"
    # The bots' field: out of sight (account.css) and out of the tab order.
    assert page.get_attribute(".hp input", "tabindex") == "-1"
    assert page.get_attribute(".hp", "aria-hidden") == "true"
    page.fill("#request input[name=email]", "zoe@example.com")
    page.fill("#request input[name=fullname]", "Zoé")
    page.fill("#request input[name=organization]", "Zoé Type")
    page.fill("#request textarea[name=message]", "Two designers, one variable family.")
    page.click("#request button[type=submit]")
    page.wait_for_selector("#request-sent:not(.hidden)")
    assert "zoe@example.com" in page.inner_text("#request-sent")
    sent = page.evaluate("calls.find(c => c.path === '/api/waitlist')")
    assert sent["body"] == {
        "email": "zoe@example.com",
        "name": "Zoé",
        "organization": "Zoé Type",
        "message": "Two designers, one variable family.",
        "website": "",
    }
    # The "More features to come" link opens it too.
    page.evaluate("location.hash = ''")
    assert (
        page.evaluate("document.querySelector('.coming a').getAttribute('href')")
        == "#request"
    )
    assert page.errors == []
    page.close()


def test_invitation_page_signs_up(browser_and_url):
    answers = {
        "POST /api/invitations/lookup": [
            200,
            {
                "invitation": {
                    "email": "zoe@example.com",
                    "project": "uplustype/Mutator",
                    "organization": None,
                    "role": "observer",
                    "target": "project uplustype/Mutator",
                    "invitedBy": {"username": "jeremie", "name": "Jérémie Hornus"},
                },
                "hasAccount": False,
            },
        ],
        "GET /api/me": [401, {"detail": "Unauthorized"}],
        "POST /api/auth/refresh": [401, {"detail": "Please sign in again."}],
        "POST /api/invitations/signup": [
            422,
            {"detail": "This name is already taken."},
        ],
    }
    page = account_page(browser_and_url, "invitation.html", answers, "#tok%2Fen")
    page.evaluate("""async () => {
          document.body.dataset.page = 'invitation';
          window.__hiveAccountNoAutoStart = false;
          await import('/account/account.js?start=2');
        }""")
    page.wait_for_selector("#signup:not(.hidden)")
    assert page.inner_text(".lead") == (
        "Jérémie Hornus invited zoe@example.com to work on uplustype/Mutator as observer."
    )
    assert page.inner_text("#signup .email") == "zoe@example.com"
    lookup = page.evaluate("calls.find(c => c.path === '/api/invitations/lookup')")
    assert lookup["body"] == {"token": "tok/en"}  # from the fragment, decoded
    page.fill("#signup input[name=username]", "zoe")
    page.fill("#signup input[name=fullname]", "Zoé")
    page.fill("#signup input[name=password]", "a good passphrase")
    page.fill("#signup input[name=password2]", "another one!!")
    page.click("#signup button[type=submit]")
    page.wait_for_function(
        "document.querySelector('#signup .error').textContent.length > 0"
    )
    assert page.inner_text("#signup .error") == "The two passwords differ."
    page.fill("#signup input[name=password2]", "a good passphrase")
    page.click("#signup button[type=submit]")
    page.wait_for_function(
        "document.querySelector('#signup .error').textContent.includes('taken')"
    )
    signup = page.evaluate("calls.find(c => c.path === '/api/invitations/signup')")
    assert signup["body"] == {
        "token": "tok/en",
        "username": "zoe",
        "name": "Zoé",
        "password": "a good passphrase",
    }
    assert page.errors == []
    page.close()


# --- the Share dialog with hive-api -------------------------------------------------

VIEW_PAGE = """<!doctype html><html><head>
<script>
  window.__hiveViewsNoAutoStart = true;
  %(mock)s
  window.answers = %(answers)s;
  window.editorController = {
    sceneSettings: { selectedGlyphName: "B" },
    fontController: { backendInfo: { projectManagerFeatures: {} } },
  };
</script>
</head><body>
<div class="top-bar-container" style="display:grid;height:35px">
  <div class="menubar">Fontra</div>
  <div id="fontra-project-name">uplustype/Mutator</div>
</div>
</body></html>"""

BASE = "/api/projects/uplustype/Mutator"


def share_answers():
    return {
        "GET /api/hive/me": [
            200,
            {
                "user": {"username": "fabio", "name": "Fabio Caccamo", "email": ""},
                "accounts": True,
                "source": "hive-api",
            },
        ],
        "GET /api/hive/projects/uplustype%2FMutator/access": [
            200,
            {"role": "manager", "capabilities": ["read", "edit", "invite"]},
        ],
        "POST /api/hive/projects/uplustype%2FMutator/presence": [200, {"others": []}],
        f"GET {BASE}/members": [
            200,
            {
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
                        "username": "fabio",
                        "name": "Fabio Caccamo",
                        "email": "f@x",
                        "role": "manager",
                        "via": "collaborator",
                        "collaboratorRole": "manager",
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
                "you": "fabio",
                "roles": ["observer", "reviewer", "designer", "manager"],
                "accounts": True,
            },
        ],
        f"GET {BASE}/invitations": [
            200,
            {
                "invitations": [
                    {
                        "id": 7,
                        "email": "zoe@example.com",
                        "role": "designer",
                        "invitee": None,
                    }
                ]
            },
        ],
        f"PUT {BASE}/collaborators/ana": [200, {}],
        f"DELETE {BASE}/invitations/7": [204, None],
        f"POST {BASE}/invitations": [201, {"invitation": {}}],
    }


def test_share_dialog_with_hive_api(browser_and_url):
    browser, base = browser_and_url
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    html = VIEW_PAGE % {"mock": MOCK, "answers": json.dumps(share_answers())}
    page.route(
        lambda url: "/editor.html" in url,  # a glob's * stops at the "/" of owner/name
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    page.goto(f"{base}/editor.html?project=uplustype/Mutator")
    page.evaluate("""async () => {
        const m = await import('/views/hive-views.js');
        window.hive = await m.start();
        window.hiveModule = m;
      }""")
    assert page.evaluate("hive.source") == "hive-api"
    assert page.evaluate("hiveModule.hiveApiProjectPath('uplustype/Mutator')") == BASE
    page.evaluate("editorController.getFileMenuItems()[0].callback()")
    page.wait_for_selector(".hive-dialog .hive-member.pending")
    rows = page.evaluate(
        "[...document.querySelectorAll('.hive-member')]"
        ".map(r => r.innerText.replace(/\\s+/g, ' ').trim())"
    )
    assert (
        rows[0].startswith("JH Jérémie Hornus")
        and "admin · organization U+Type" in rows[0]
    )
    assert "Fabio Caccamo (you)" in rows[1]
    assert "zoe@example.com" in rows[3] and "pending" in rows[3]
    # Jérémie's role comes from the organization: no menu, no ×.
    assert (
        page.evaluate(
            "document.querySelector(\".hive-member[data-username='jeremie'] select\")"
        )
        is None
    )
    page.select_option("select[aria-label='Role of Ana López']", "designer")
    page.wait_for_timeout(50)
    page.click(".hive-member[data-invitation='7'] .remove")
    page.wait_for_timeout(50)
    page.fill("input[aria-label='Username or email']", "new@example.com")
    page.select_option("select[aria-label='Role']", "reviewer")
    page.click(".hive-dialog .add button")
    page.wait_for_selector(".hive-dialog .done")
    assert (
        page.inner_text(".hive-dialog .done") == "Invitation sent to new@example.com."
    )
    page.fill("input[aria-label='Username or email']", "zoe")
    page.press("input[aria-label='Username or email']", "Enter")
    page.wait_for_timeout(50)
    writes = page.evaluate(
        "calls.filter(c => c.path.startsWith('/api/projects') && c.method !== 'GET')"
        ".map(c => [c.method, c.path, c.body])"
    )
    assert writes == [
        ["PUT", f"{BASE}/collaborators/ana", {"role": "designer"}],
        ["DELETE", f"{BASE}/invitations/7", None],
        [
            "POST",
            f"{BASE}/invitations",
            {"email": "new@example.com", "role": "reviewer"},
        ],
        ["POST", f"{BASE}/invitations", {"username": "zoe", "role": "designer"}],
    ]
    assert errors == []
    page.close()
