"""Hive's home page (client/account/home.js) in a headless browser, against a
mocked hive-api. Skipped without Playwright."""

import json

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from test_account_js import CLIENT_DIR, MOCK, browser_and_url  # noqa: E402,F401

ME = {
    "username": "jeremie",
    "name": "Jérémie Hornus",
    "email": "jeremie@example.com",
    "avatar": None,
    "emailVerified": True,
    "staff": True,
}


def project(owner, name, role, description=""):
    return {
        "id": f"{owner}/{name}",
        "owner": owner,
        "name": name,
        "description": description,
        "role": role,
        "trashed": False,
        "capabilities": (
            ["read", "edit", "invite", "administer"] if role == "admin" else ["read"]
        ),
    }


def answers():
    return {
        "GET /api/me": [200, {"user": ME}],
        "GET /api/hive/me": [
            200,
            {
                "user": {"username": "jeremie", "name": "Jérémie Hornus"},
                "accounts": True,
                "source": "hive-api",
            },
        ],
        "GET /api/invitations/mine": [
            200,
            {
                "invitations": [
                    {
                        "id": 3,
                        "project": "ana/Sketches",
                        "organization": None,
                        "role": "reviewer",
                        "invitedBy": {"username": "ana", "name": "Ana López"},
                    }
                ]
            },
        ],
        "POST /api/invitations/3/accept": [200, {"invitation": {}}],
        "GET /api/me/projects": [
            200,
            {
                "projects": [
                    project("jeremie", "Sketches", "admin", "Ideas"),
                    project("uplustype", "Mutator", "admin"),
                    project("uplustype", "Proofs", "observer"),
                ]
            },
        ],
        "GET /api/me/projects?trashed=true": [
            200,
            {"projects": [project("uplustype", "Old", "admin")]},
        ],
        "GET /api/me/organizations": [
            200,
            {
                "organizations": [
                    {"login": "uplustype", "name": "U+Type", "role": "owner"}
                ]
            },
        ],
        "POST /api/projects": [
            201,
            {"project": project("uplustype", "New-One", "admin")},
        ],
        "GET /api/orgs/uplustype": [
            200,
            {
                "organization": {
                    "login": "uplustype",
                    "name": "U+Type",
                    "baseRole": "designer",
                }
            },
        ],
        "GET /api/orgs/uplustype/members": [
            200,
            {
                "members": [
                    {
                        "username": "jeremie",
                        "name": "Jérémie Hornus",
                        "email": "j@x",
                        "role": "owner",
                    },
                    {
                        "username": "fabio",
                        "name": "Fabio Caccamo",
                        "email": "f@x",
                        "role": "member",
                    },
                ],
                "canManage": True,
            },
        ],
        "GET /api/orgs/uplustype/invitations": [200, {"invitations": []}],
        "POST /api/orgs/uplustype/invitations": [201, {"invitation": {}}],
        "PATCH /api/orgs/uplustype/members/fabio": [200, {}],
    }


def open_home(browser_and_url, hash="", extra=None):  # noqa: F811
    browser, base = browser_and_url
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    data = answers()
    data.update(extra or {})
    html = (CLIENT_DIR / "account" / "home.html").read_text()
    html = html.replace("/hive/account/", "/account/").replace(
        "/hive/views/", "/views/"
    )
    html = html.replace(
        "<head>",
        "<head><script>" + MOCK + f"window.answers = {json.dumps(data)};"
        # The trash list: same path with ?trashed=true, answered by the mock's path only.
        + "</script>",
    )
    page.route(
        lambda url: "/home.html" in url,
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    page.route(
        lambda url: "/fontoverview.html" in url,
        lambda route: route.fulfill(
            content_type="text/html", body="<p>font overview</p>"
        ),
    )
    page.goto(f"{base}/home.html{hash}")
    page.errors = errors
    return page


def test_projects_and_invitations(browser_and_url):  # noqa: F811
    page = open_home(browser_and_url)
    page.wait_for_selector(".project-card")
    groups = page.evaluate(
        "[...document.querySelectorAll('main h3')].map(h => h.textContent)"
    )
    assert groups[:2] == ["Yours", "uplustype"]
    assert page.inner_text("details summary") == "Trash (1)"
    cards = page.evaluate(
        "[...document.querySelectorAll('.project-card')]"
        ".map(c => [c.dataset.project, c.getAttribute('href')])"
    )
    assert cards[0] == [
        "jeremie/Sketches",
        "/fontoverview.html?project=jeremie%2FSketches",
    ]
    assert page.inner_text("#invitations").startswith(
        "Ana López invited you to ana/Sketches as reviewer."
    )
    page.click("#invitations button")
    page.wait_for_function("calls.some(c => c.path === '/api/invitations/3/accept')")
    # The chip of hive-views sits in the bar.
    page.wait_for_selector(".home-bar .hive-chip")
    assert page.errors == []
    page.close()


def test_parse_hash(browser_and_url):  # noqa: F811
    page = open_home(browser_and_url)
    page.wait_for_selector(".project-card")
    result = page.evaluate("""async () => {
          const m = await import('/account/home.js');
          return ['', '#new', '#project/uplustype/Mutator%20Sans', '#orgs', '#org/uplustype',
                  '#profile', '#nonsense'].map(m.parseHash);
        }""")
    assert result == [
        {"section": "projects"},
        {"section": "projects", "view": "new"},
        {"section": "projects", "view": "project", "project": "uplustype/Mutator Sans"},
        {"section": "orgs"},
        {"section": "orgs", "view": "org", "login": "uplustype"},
        {"section": "profile"},
        {"section": "projects"},
    ]
    page.close()


def test_new_project_then_open_it(browser_and_url):  # noqa: F811
    page = open_home(browser_and_url, "#new")
    page.wait_for_selector("form select[name=owner]")
    owners = page.evaluate(
        "[...document.querySelectorAll('select[name=owner] option')].map(o => o.value)"
    )
    assert owners == ["jeremie", "uplustype"]
    page.select_option("select[name=owner]", "uplustype")
    page.fill("input[name=name]", "New-One")
    page.fill("input[name=description]", "A test")
    # Importing without a file is refused before anything is created.
    page.check("input[value=font]")
    page.click("form button[type=submit]")
    page.wait_for_function(
        "document.querySelector('form .error').textContent.length > 0"
    )
    assert not page.evaluate("calls.some(c => c.path === '/api/projects')")
    page.check("input[value=empty]")
    page.click("form button[type=submit]")
    page.wait_for_url("**/fontoverview.html?project=uplustype%2FNew-One")
    page.close()


def test_organization_page(browser_and_url):  # noqa: F811
    page = open_home(browser_and_url, "#org/uplustype")
    page.wait_for_selector("table.people tr[data-username=fabio]")
    page.select_option("select[aria-label='Role of Fabio Caccamo']", "owner")
    page.wait_for_function("calls.some(c => c.method === 'PATCH')")
    patch = page.evaluate("calls.find(c => c.method === 'PATCH')")
    assert patch["path"] == "/api/orgs/uplustype/members/fabio" and patch["body"] == {
        "role": "owner"
    }
    page.wait_for_selector("input[type=email]")
    page.fill("input[type=email]", "zoe@example.com")
    page.click("text=Send the invitation")
    page.wait_for_function(
        "calls.some(c => c.path === '/api/orgs/uplustype/invitations' && c.method === 'POST')"
    )
    invite = page.evaluate(
        "calls.find(c => c.path === '/api/orgs/uplustype/invitations' && c.method === 'POST')"
    )
    assert invite["body"] == {"email": "zoe@example.com", "role": "member"}
    assert page.errors == []
    page.close()


def test_a_failed_import_leaves_a_project_without_a_font(browser_and_url):  # noqa: F811
    """New project from a font, the import fails: the project page says so,
    offers to import again or start empty, and has no "Open" (which would
    make an empty font without saying anything)."""
    new_one = project("uplustype", "New-One", "admin")
    page = open_home(
        browser_and_url,
        "#new",
        {
            "GET /api/projects/uplustype/New-One": [200, {"project": new_one}],
            "GET /api/hive/projects/uplustype%2FNew-One/repository": [
                200,
                {"exists": False},
            ],
            "GET /api/hive/projects/uplustype%2FNew-One/head": [
                200,
                {"branch": "main", "head": "a" * 40},
            ],
        },
    )
    page.route(
        lambda url: url.endswith("/import"),
        lambda route: route.fulfill(status=500, body="PermissionError(13)"),
    )
    page.wait_for_selector("form select[name=owner]")
    page.select_option("select[name=owner]", "uplustype")
    page.fill("input[name=name]", "New-One")
    page.check("input[value=font]")
    page.set_input_files(
        "input[type=file]",
        files=[{"name": "font.zip", "mimeType": "application/zip", "buffer": b"PK"}],
    )
    page.click("form button[type=submit]")
    page.wait_for_selector(".no-font")
    assert "could not be imported: PermissionError(13)" in page.inner_text(".notice")
    assert "No font yet" in page.inner_text(".no-font")
    assert page.evaluate("[...document.querySelectorAll('h2 button')].length") == 0
    # Start with an empty font instead: the server makes it, then the editor opens.
    page.click(".no-font button")
    page.wait_for_url("**/fontoverview.html?project=uplustype%2FNew-One")
    assert page.errors == []
    page.close()


def test_a_project_with_its_font_can_be_opened(browser_and_url):  # noqa: F811
    page = open_home(
        browser_and_url,
        "#project/uplustype/Mutator",
        {
            "GET /api/projects/uplustype/Mutator": [
                200,
                {"project": project("uplustype", "Mutator", "admin")},
            ],
            "GET /api/hive/projects/uplustype%2FMutator/repository": [
                200,
                {"exists": True},
            ],
        },
    )
    page.wait_for_selector("h2 button")
    assert page.query_selector(".no-font") is None
    assert page.query_selector(".notice") is None
    page.close()
