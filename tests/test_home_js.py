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
            ["read", "edit", "export", "invite", "administer"]
            if role == "admin"
            else ["read"]
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
                        "name": "Fabio Rossi",
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
        "PATCH /api/orgs/uplustype": [200, {"organization": {}}],
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
    page.select_option("select[aria-label='Role of Fabio Rossi']", "owner")
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
    # Settings: projects shared with members only.
    page.check("input[name=membersOnly]")
    page.click("text=Save")
    page.wait_for_function(
        "calls.some(c => c.path === '/api/orgs/uplustype' && c.method === 'PATCH')"
    )
    settings = page.evaluate(
        "calls.find(c => c.path === '/api/orgs/uplustype' && c.method === 'PATCH')"
    )
    assert settings["body"]["membersOnly"] is True
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
            "GET /api/hive/export-formats": [
                200,
                {
                    "formats": [
                        {"format": "fontra", "label": "Fontra package"},
                        {"format": "otf", "label": "OpenType (.otf)"},
                    ]
                },
            ],
            "GET /api/hive/projects/uplustype%2FMutator/export": [200, {"zip": 1}],
        },
    )
    page.wait_for_selector("h2 button")
    assert page.query_selector(".no-font") is None
    assert page.query_selector(".notice") is None
    # Download: one button per format the server offers.
    page.wait_for_selector(".downloads button")
    assert page.eval_on_selector_all(
        ".downloads button", "bs => bs.map(b => b.textContent)"
    ) == [
        "Fontra package",
        "OpenType (.otf)",
    ]
    page.click("text=OpenType (.otf)")
    page.wait_for_function("calls.some(c => c.path.endsWith('/export'))")
    assert page.errors == []
    page.close()


def test_empty_the_trash(browser_and_url):  # noqa: F811
    page = open_home(
        browser_and_url,
        extra={
            "DELETE /api/projects/uplustype/Old/permanently": [
                200,
                {"deleted": "uplustype/Old"},
            ],
            "POST /api/hive/sweep-deleted": [200, {"removed": []}],
        },
    )
    page.wait_for_selector(".project-card")
    page.click("details summary")
    page.on("dialog", lambda dialog: dialog.accept())
    page.click("text=Empty the trash")
    page.wait_for_function("calls.some(c => c.path === '/api/hive/sweep-deleted')")
    methods = page.evaluate(
        "calls.filter(c => c.path.includes('permanently') || c.path.includes('sweep'))"
        ".map(c => [c.method, c.path])"
    )
    assert methods == [
        ["DELETE", "/api/projects/uplustype/Old/permanently"],
        ["POST", "/api/hive/sweep-deleted"],
    ]
    assert page.errors == []
    page.close()


BRANCHES = {
    "default": "main",
    "you": "jeremie",
    "can": {"create": True, "delete": True, "merge": True},
    "branches": [
        {
            "name": "main",
            "isDefault": True,
            "ahead": 0,
            "behind": 0,
            "time": 1,
            "author": "Fabio Rossi",
            "createdBy": None,
            "open": False,
        },
        {
            "name": "bold",
            "isDefault": False,
            "ahead": 2,
            "behind": 0,
            "time": 1,
            "author": "Ana López",
            "createdBy": "ana",
            "createdByName": "Ana López",
            "open": False,
        },
        {
            "name": "wide",
            "isDefault": False,
            "ahead": 0,
            "behind": 1,
            "time": 1,
            "author": "Zoé",
            "createdBy": "zoe",
            "open": True,
        },
    ],
    "archived": [
        {
            "tag": "archive/light",
            "name": "light",
            "head": "a" * 40,
            "deleted": 1,
            "deletedBy": "Mona",
            "deletedByUsername": "mona",
            "createdBy": "ana",
            "ahead": 4,
        },
    ],
}


def test_project_branches(browser_and_url):  # noqa: F811
    hive = "/api/hive/projects/uplustype%2FMutator"
    page = open_home(
        browser_and_url,
        "#project/uplustype/Mutator",
        {
            "GET /api/projects/uplustype/Mutator": [
                200,
                {"project": project("uplustype", "Mutator", "admin")},
            ],
            f"GET {hive}/repository": [200, {"exists": True}],
            f"GET {hive}/branches": [200, BRANCHES],
            f"POST {hive}/branches": [200, {"branch": {"name": "x"}}],
            f"DELETE {hive}/branches": [200, {"deleted": "bold"}],
            f"POST {hive}/branches/restore": [200, {"branch": {"name": "light"}}],
            "GET /api/hive/export-formats": [
                200,
                {"formats": [{"format": "fontra", "label": "Fontra package"}]},
            ],
            f"GET {hive}/export": [200, {"zip": 1}],
        },
    )
    page.on("dialog", lambda dialog: dialog.accept())
    page.wait_for_selector("table.branches tr")
    rows = page.eval_on_selector_all(
        "table.branches tr",
        """rows => rows.map(r => [r.dataset.branch, r.querySelector('small').textContent,
                 r.querySelector('a').getAttribute('href'),
                 r.querySelector('.danger') ? !r.querySelector('.danger').disabled : null])""",
    )
    assert [r[0] for r in rows] == ["main", "bold", "wide"]
    assert rows[1][1].startswith("2 ahead of main · changed ")
    assert rows[1][1].endswith("by Ana López · made by Ana López")
    assert [r[2] for r in rows] == [
        "/fontoverview.html?project=uplustype%2FMutator",
        "/fontoverview.html?project=uplustype%2FMutator%40bold",
        "/fontoverview.html?project=uplustype%2FMutator%40wide",
    ]
    # Not the default branch, not one someone has open.
    assert [r[3] for r in rows] == [None, True, False]

    page.click("tr[data-branch='bold'] .danger")
    page.wait_for_function("calls.some(c => c.method === 'DELETE')")
    deleted = page.evaluate("calls.find(c => c.method === 'DELETE')")
    assert deleted["query"] == "?branch=bold"

    # A new branch, from the branch chosen.
    page.wait_for_selector("table.branches tr")
    page.fill("input[placeholder='e.g. bold-extension']", "ana/italic")
    page.select_option("select:not([aria-label])", "bold")
    page.click("text=Create")
    page.wait_for_function(
        "calls.some(c => c.method === 'POST' && c.path.endsWith('/branches'))"
    )
    post = page.evaluate(
        "calls.find(c => c.method === 'POST' && c.path.endsWith('/branches'))"
    )
    assert post["query"] == "?name=ana%2Fitalic&from=bold"

    # Deleted branches, folded; restored under their name.
    page.wait_for_selector("details.archived")
    assert not page.is_visible("tr[data-tag='archive/light']")
    page.click("details.archived summary")
    assert "4 changes not in main" in page.inner_text("tr[data-tag='archive/light']")
    page.click("tr[data-tag='archive/light'] button")
    page.wait_for_selector(".hive-dialog input")
    page.click(".hive-dialog button.blue")
    page.wait_for_function("calls.some(c => c.path.endsWith('/restore'))")
    restore = page.evaluate("calls.find(c => c.path.endsWith('/restore'))")
    assert restore["query"] == "?tag=archive%2Flight&name=light"

    # Download: from the branch chosen.
    page.wait_for_selector("select[aria-label='Branch to download']")
    page.select_option("select[aria-label='Branch to download']", "wide")
    page.click("text=Fontra package")
    page.wait_for_function("calls.some(c => c.path.endsWith('/export'))")
    export = page.evaluate("calls.find(c => c.path.endsWith('/export'))")
    assert export["query"] == "?format=fontra&branch=wide"
    assert page.errors == []
    page.close()
