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
                  '#profile', '#nonsense', '#project/uplustype/Mutator/comments'].map(m.parseHash);
        }""")
    assert result == [
        {"section": "projects"},
        {"section": "projects", "view": "new"},
        {"section": "projects", "view": "project", "project": "uplustype/Mutator Sans"},
        {"section": "orgs"},
        {"section": "orgs", "view": "org", "login": "uplustype"},
        {"section": "profile"},
        {"section": "projects"},
        {"section": "projects", "view": "comments", "project": "uplustype/Mutator"},
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
            f"GET {hive}/merge-preview": [
                200,
                {
                    "from": "bold",
                    "into": "main",
                    "fromHead": "f",
                    "intoHead": "e",
                    "ahead": 2,
                    "behind": 0,
                    "upToDate": False,
                    "fastForward": True,
                    "changes": {"from": ["A"], "into": [], "files": []},
                    "merged": [],
                    "conflicts": [],
                    "canMerge": True,
                },
            ],
            f"POST {hive}/merge": [
                200,
                {
                    "from": "bold",
                    "into": "main",
                    "head": "1",
                    "fastForward": False,
                    "glyphs": ["A"],
                    "resolved": [],
                },
            ],
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

    # Merge a branch (managers): only those with something to merge.
    merges = page.eval_on_selector_all(
        "table.branches tr",
        "rows => rows.map(r => !![...r.querySelectorAll('button')]"
        ".find(b => b.textContent === 'Merge…'))",
    )
    assert merges == [False, True, False]
    page.click("tr[data-branch='bold'] >> text=Merge…")
    page.wait_for_selector(".hive-merge button.blue")
    page.click(".hive-merge button.blue")
    page.wait_for_selector("text=Merged: 1 glyph changed in main.")
    page.click(".hive-merge >> text=Close")

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


def comment(
    number, glyph, state="open", labels=(), assignee=None, text="Hm", branch="main"
):
    ana = {"username": "ana", "name": "Ana López"}
    return {
        "number": number,
        "glyph": glyph,
        "branch": branch,
        "state": state,
        "source": {"layer": "Bold", "name": "Bold", "location": {}},
        "point": {"x": 1, "y": 2},
        "commit": "c" * 40,
        "author": ana,
        "created": "2026-09-30T10:00:00Z",
        "resolved": (
            {"by": ana, "at": "2026-09-30T11:00:00Z"} if state == "resolved" else None
        ),
        "assignee": assignee,
        "labels": list(labels),
        "messages": [
            {
                "id": 1,
                "author": ana,
                "created": "2026-09-30T10:00:00Z",
                "edited": None,
                "text": text,
            }
        ],
    }


def test_project_comments(browser_and_url):  # noqa: F811
    hive = "/api/hive/projects/uplustype%2FMutator"
    jeremie = {"username": "jeremie", "name": "Jérémie Hornus"}
    comments = {
        "head": "h",
        "you": jeremie,
        "can": {
            "comment": True,
            "resolveAny": True,
            "organize": True,
            "moderate": True,
        },
        "members": [jeremie, {"username": "ana", "name": "Ana López"}],
        "issues": [
            comment(1, "A", labels=["curve"], assignee=jeremie, text="Stem too thin"),
            comment(2, "B", state="resolved"),
            comment(3, "A", labels=["spacing"], text="Too tight"),
        ],
    }
    extra = {
        "GET /api/projects/uplustype/Mutator": [
            200,
            {"project": project("uplustype", "Mutator", "admin")},
        ],
        f"GET {hive}/repository": [200, {"exists": True}],
        f"GET {hive}/branches": [200, BRANCHES],
        f"GET {hive}/comments": [200, comments],
        "GET /api/hive/export-formats": [200, {"formats": []}],
    }
    page = open_home(browser_and_url, "#project/uplustype/Mutator", extra)
    page.wait_for_selector(".comments-summary")
    assert page.text_content(".comments-summary .note") == (
        "2 open · 1 resolved · 1 assigned to you"
    )
    page.click(".comments-summary button")
    page.wait_for_selector("table.comments tr")
    numbers = lambda: page.eval_on_selector_all(  # noqa: E731
        "table.comments tr", "rows => rows.map(r => r.dataset.number)"
    )
    assert numbers() == ["3", "1"]  # open ones, newest first
    assert page.text_content("tr[data-number='1'] a button") == "Open glyph"
    href = page.get_attribute("tr[data-number='1'] a", "href")
    assert "editor.html?project=uplustype%2FMutator" in href and "hive-issue=1" in href
    page.select_option("[data-filter=label]", "curve")
    assert numbers() == ["1"]
    page.select_option("[data-filter=label]", "")
    page.select_option("[data-filter=person]", "me")
    page.select_option("[data-filter=role]", "assignee")
    assert numbers() == ["1"]
    page.select_option("[data-filter=person]", "")
    page.select_option("[data-filter=state]", "all")
    assert numbers() == ["3", "2", "1"]
    page.fill("[data-filter=q]", "tight")
    assert numbers() == ["3"]
    assert "1 of 3 comments" in page.text_content("main")
    page.close()


def test_project_cards_count_open_comments(browser_and_url):  # noqa: F811
    summary = "GET /api/hive/projects/jeremie%2FSketches/comments/summary"
    page = open_home(browser_and_url, "", {summary: [200, {"open": 3, "resolved": 1}]})
    page.wait_for_selector("[data-comments='jeremie/Sketches']:not(:empty)")
    assert page.text_content("[data-comments='jeremie/Sketches']") == "3 open comments"
    page.click("[data-comments='jeremie/Sketches']")
    page.wait_for_function("location.hash === '#project/jeremie/Sketches/comments'")
    page.close()


def remote_project(role="admin"):
    p = project("uplustype", "Mutator", role)
    p["capabilities"] = {
        "admin": ["read", "edit", "branch", "merge", "export", "invite", "administer"],
        "designer": ["read", "edit", "branch"],
    }[role]
    return p


# As hive-api orders them: .fontra, designspaces, then UFOs.
GALIEN_FONTS = [
    "",
    "Sources/Galien.fontra",
    "Sources/Galien.designspace",
    "Sources/Extra/Galien-Display.ufo",
]


def remote_answers(role="admin", remote=None, github=None, status=None):
    hive = "/api/hive/projects/uplustype%2FMutator"
    answers = {
        "GET /api/projects/uplustype/Mutator": [200, {"project": remote_project(role)}],
        f"GET {hive}/repository": [200, {"exists": True}],
        f"GET {hive}/branches": [200, BRANCHES],
        "GET /api/projects/uplustype/Mutator/remote": [200, {"remote": remote}],
        "GET /api/github/status": [
            200,
            github
            or {
                "configured": True,
                "connected": False,
                "login": None,
                "installUrl": "https://github.com/apps/fontra-hive",
            },
        ],
        "GET /api/github/repositories": [
            200,
            {
                "login": "jhornus",
                "repositories": [
                    {
                        "fullName": "Uplus-Type/Galien",
                        "private": True,
                        "defaultBranch": "main",
                    },
                    {
                        "fullName": "Uplus-Type/Tosh",
                        "private": False,
                        "defaultBranch": "main",
                    },
                ],
            },
        ],
        "GET /api/github/repositories/Uplus-Type/Galien/fonts?branch=main": [
            200,
            {"branch": "main", "fonts": GALIEN_FONTS, "found": True},
        ],
        "GET /api/github/repositories/Uplus-Type/Tosh/fonts?branch=main": [
            200,
            {"branch": "main", "fonts": ["Sources/Tosh.designspace"], "found": True},
        ],
        "PUT /api/projects/uplustype/Mutator/remote": [200, {"remote": remote}],
        "DELETE /api/projects/uplustype/Mutator/remote": [200, {"remote": None}],
        f"GET {hive}/remote": [200, status or {}],
        f"POST {hive}/remote/pull": [
            200,
            {
                "branch": "upstream/main",
                "remoteHead": "a" * 40,
                "commit": "b" * 40,
                "glyphs": ["A"],
            },
        ],
        f"POST {hive}/remote/push": [
            200,
            {
                "pushed": True,
                "remoteHead": "c" * 40,
                "previous": "a" * 40,
                "files": ["src/A.ufo/glyphs/A_.glif"],
                "glyphs": ["A"],
                "skipped": ["font-data.json"],
            },
        ],
    }
    return answers


GALIEN = {
    "provider": "github",
    "url": "https://github.com/Uplus-Type/Galien.git",
    "repository": "Uplus-Type/Galien",
    "branch": "main",
    "path": "Sources/Galien.designspace",
    "format": "ufo",
    "status": "ok",
    "statusReason": None,
}


def test_remote_connect_github(browser_and_url):  # noqa: F811
    """GitHub opens in a tab of its own; this page updates when that tab says
    it is done, or when one comes back to it."""
    page = open_home(browser_and_url, "#project/uplustype/Mutator", remote_answers())
    page.wait_for_selector("text=Connect GitHub")
    page.evaluate(
        "() => { window.opened = [];"
        " window.open = (url) => { window.opened.push(String(url)); return {}; }; }"
    )
    page.click(".panel.remote button:text('Connect GitHub')")
    opened = page.evaluate("() => window.opened")
    assert len(opened) == 1
    assert opened[0].startswith("/api/github/connect?project=uplustype%2FMutator&next=")
    assert "%23project%2Fuplustype%2FMutator" in opened[0] and "tab=1" in opened[0]
    assert page.get_attribute(".panel.remote a", "href") == "/git"  # How it works
    link = page.get_attribute(".panel.remote a[href^='/api/github']", "href")
    assert "mode=authorize" in link  # "Already installed the app? Sign in to GitHub"
    # Back to this tab: it asks again.
    before = page.evaluate("calls.filter(c => c.path === '/api/github/status').length")
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.wait_for_function(
        f"calls.filter(c => c.path === '/api/github/status').length > {before}"
    )
    # The GitHub tab says it is done.
    page.evaluate(
        "window.postMessage({hive: 'github', result: 'connected'}, location.origin)"
    )
    page.wait_for_selector("text=GitHub is connected")
    assert page.errors == []
    page.close()


def test_remote_choose_a_repository(browser_and_url):  # noqa: F811
    github = {
        "configured": True,
        "connected": True,
        "login": "jhornus",
        "installUrl": "https://github.com/apps/fontra-hive",
    }
    page = open_home(
        browser_and_url, "#project/uplustype/Mutator", remote_answers(github=github)
    )
    page.wait_for_selector("select[aria-label='Repository']")
    assert "Signed in to GitHub as jhornus" in page.inner_text(".panel.remote")
    menu = "select[aria-label='Font in the repository']"
    # The first repository's fonts, in hive-api's order, the first chosen.
    page.wait_for_selector(f"{menu}:not([disabled])")
    assert page.input_value("form input[placeholder^='the repository']") == "main"
    labels = page.eval_on_selector_all(
        f"{menu} option", "os => os.map(o => o.textContent)"
    )
    assert labels == [
        ".fontra package at the root (recommended)",
        "Sources/Galien.fontra (recommended)",
        "Sources/Galien.designspace (beta)",
        "Sources/Extra/Galien-Display.ufo (beta)",
        "Other path…",
    ]
    assert page.input_value(menu) == ""
    assert page.is_hidden("input[aria-label='Path of the font in the repository']")
    # Another repository: its default branch, its fonts.
    page.select_option("select[aria-label='Repository']", "Uplus-Type/Tosh")
    page.wait_for_function(
        "document.querySelector(\"select[aria-label='Font in the repository']\")"
        ".value === 'Sources/Tosh.designspace'"
    )
    page.click(".panel.remote form >> text=Connect this repository")
    page.wait_for_function("calls.some(c => c.method === 'PUT')")
    put = page.evaluate("calls.find(c => c.method === 'PUT')")
    assert put["body"] == {
        "repository": "Uplus-Type/Tosh",
        "branch": "main",
        "path": "Sources/Tosh.designspace",
    }
    assert page.errors == []
    page.close()


def test_remote_font_menu_choices(browser_and_url):  # noqa: F811
    """ "Other path…", a branch that does not exist, one without fonts, and
    GitHub to connect again."""
    github = {
        "configured": True,
        "connected": True,
        "login": "jhornus",
        "installUrl": "https://github.com/apps/fontra-hive",
    }
    answers = remote_answers(github=github)
    fonts = "GET /api/github/repositories/Uplus-Type/Galien/fonts"
    answers[f"{fonts}?branch=drafts"] = [
        200,
        {"branch": "drafts", "fonts": [], "found": False},
    ]
    answers[f"{fonts}?branch=empty"] = [
        200,
        {"branch": "empty", "fonts": [], "found": True},
    ]
    answers[f"{fonts}?branch=gone"] = [
        409,
        {"detail": "The GitHub connection expired: connect GitHub again."},
    ]
    page = open_home(browser_and_url, "#project/uplustype/Mutator", answers)
    menu = "select[aria-label='Font in the repository']"
    typed = "input[aria-label='Path of the font in the repository']"
    branch = "form input[placeholder^='the repository']"
    connect = ".panel.remote form button[type=submit]"
    page.wait_for_selector(f"{menu}:not([disabled])")
    # "Other path…" shows the text field, which is what is sent.
    page.select_option(menu, "*other*")
    assert page.is_visible(typed)
    page.fill(typed, "fonts/Galien-Italic.ufo")
    # A branch that does not exist: said in the menu, nothing to connect.
    page.fill(branch, "drafts")
    page.dispatch_event(branch, "change")
    page.wait_for_function(
        "document.querySelector(\"select[aria-label='Font in the repository']\")"
        ".textContent === 'No branch “drafts” in this repository'"
    )
    assert page.is_disabled(menu) and page.is_disabled(connect)
    assert page.is_hidden(typed)
    # A branch without fonts: the path is typed.
    page.fill(branch, "empty")
    page.dispatch_event(branch, "change")
    page.wait_for_selector("text=No font found in this branch")
    assert page.is_hidden(menu) and page.is_visible(typed)
    assert page.is_enabled(connect)
    # The font pushed meanwhile: "Look again", or back to this tab.
    answers_now = f"{fonts}?branch=empty"
    page.evaluate(
        "(key) => { window.answers[key] = [200, {branch: 'empty',"
        " fonts: ['Sources/HexaSerif.fontra'], found: true}]; }",
        answers_now,
    )
    page.click(".panel.remote a.look-again")
    page.wait_for_selector(f"{menu}:not([disabled])")
    assert page.input_value(menu) == "Sources/HexaSerif.fontra"
    page.evaluate(
        "(key) => { window.answers[key] = [200, {branch: 'empty',"
        " fonts: [], found: true}]; }",
        answers_now,
    )
    page.fill(branch, "main")
    page.dispatch_event(branch, "change")
    page.wait_for_selector(f"{menu}:not([disabled])")
    page.fill(branch, "empty")
    page.dispatch_event(branch, "change")
    page.wait_for_selector("text=No font found in this branch")
    page.evaluate(
        "(key) => { window.answers[key] = [200, {branch: 'empty',"
        " fonts: ['Sources/HexaSerif.fontra'], found: true}]; }",
        answers_now,
    )
    page.fill(typed, "")  # a path typed meanwhile is kept, not looked over
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.wait_for_selector(f"{menu}:not([disabled])")
    # GitHub must be connected again: said, nothing to connect.
    page.fill(branch, "gone")
    page.dispatch_event(branch, "change")
    page.wait_for_selector("text=The GitHub connection expired")
    assert page.is_disabled(connect) and page.is_hidden(typed)
    # Back to main, then a typed path.
    page.fill(branch, "main")
    page.dispatch_event(branch, "change")
    page.wait_for_selector(f"{menu}:not([disabled])")
    page.select_option(menu, "*other*")
    page.fill(typed, "fonts/Galien-Italic.ufo")
    page.click(connect)
    page.wait_for_function("calls.some(c => c.method === 'PUT')")
    put = page.evaluate("calls.find(c => c.method === 'PUT')")
    assert put["body"] == {
        "repository": "Uplus-Type/Galien",
        "branch": "main",
        "path": "fonts/Galien-Italic.ufo",
    }
    assert page.errors == []
    page.close()


# Holds the answers to the paths given until window.release() (a slow server).
HOLD = """(paths) => {
  const fetchNow = window.fetch;
  const held = [];
  window.release = () => held.splice(0).forEach((go) => go());
  window.fetch = (url, options) => {
    const path = new URL(url, location.href).pathname;
    if (!paths.some((p) => path.endsWith(p))) return fetchNow(url, options);
    return new Promise((go) => held.push(go)).then(() => fetchNow(url, options));
  };
}"""


def test_remote_busy_while_connecting(browser_and_url):  # noqa: F811
    github = {
        "configured": True,
        "connected": True,
        "login": "jhornus",
        "installUrl": "https://github.com/apps/fontra-hive",
    }
    answers = remote_answers(github=github)
    answers["PUT /api/projects/uplustype/Mutator/remote"] = [
        400,
        {"detail": "No such branch."},
    ]
    page = open_home(browser_and_url, "#project/uplustype/Mutator", answers)
    page.wait_for_selector(
        "select[aria-label='Font in the repository']:not([disabled])"
    )
    page.evaluate(HOLD, ["/remote"])
    connect = ".panel.remote form button[type=submit]"
    page.click(connect)
    page.wait_for_selector(f"{connect}.busy >> text=Saving the connection…")
    assert page.is_disabled(connect)
    assert page.get_attribute(".panel.remote", "aria-busy") == "true"
    assert page.query_selector(f"{connect} .spinner") is not None
    page.evaluate("window.release()")
    # An error: the button as it was.
    page.wait_for_selector(".panel.remote form .error >> text=No such branch.")
    assert page.inner_text(connect) == "Connect this repository"
    assert page.is_enabled(connect)
    assert page.get_attribute(".panel.remote", "aria-busy") is None
    assert page.errors == []
    page.close()


def test_remote_busy_pull_and_push(browser_and_url):  # noqa: F811
    status = {
        "upstreamBranch": "upstream/main",
        "remoteMoved": False,
        "unmerged": False,
        "pending": ["glyphs/A^1.json"],
        "pendingCount": 1,
        "canPush": True,
    }
    answers = remote_answers(remote=GALIEN, status=status)
    hive = "/api/hive/projects/uplustype%2FMutator"
    answers[f"POST {hive}/remote/push"] = [
        409,
        {"error": "remote-moved", "message": "The repository moved: pull first."},
    ]
    page = open_home(browser_and_url, "#project/uplustype/Mutator", answers)
    page.wait_for_selector("text=1 file changed in Hive since the last sync.")
    page.evaluate(HOLD, ["/remote/push", "/remote/pull"])
    push = ".panel.remote button:has-text('Push to GitHub')"
    page.click(push)
    page.wait_for_selector("button.busy >> text=Pushing to GitHub…")
    assert page.is_disabled("button.busy")
    page.evaluate("window.release()")
    page.wait_for_selector("text=The repository moved: pull first.")
    assert page.is_enabled(push) and page.query_selector("button.busy") is None
    page.click(".panel.remote button:text('Pull')")
    page.wait_for_selector("button.busy >> text=Pulling…")
    page.evaluate("window.release()")
    page.wait_for_selector("text=Pulled into upstream/main (1 glyphs changed).")
    assert page.errors == []
    page.close()


def test_remote_status_merge_pull_push(browser_and_url):  # noqa: F811
    status = {
        "upstreamBranch": "upstream/main",
        "remoteMoved": False,
        "unmerged": False,
        "pending": ["font-data.json", "glyphs/A^1.json"],
        "pendingCount": 2,
        "canPush": True,
    }
    page = open_home(
        browser_and_url,
        "#project/uplustype/Mutator",
        remote_answers(remote=GALIEN, status=status),
    )
    page.on("dialog", lambda dialog: dialog.accept())
    page.wait_for_selector("text=2 files changed in Hive since the last sync.")
    text = page.inner_text(".panel.remote")
    assert "Uplus-Type/Galien" in text and "Sources/Galien.designspace" in text
    assert "Not sent to the repository: font-data.json." in text
    page.fill("input[aria-label='Commit message']", "Proofs")
    page.click("text=Push to GitHub")
    page.wait_for_function("calls.some(c => c.path.endsWith('/remote/push'))")
    push = page.evaluate("calls.find(c => c.path.endsWith('/remote/push'))")
    assert push["body"] == {"message": "Proofs"}
    page.wait_for_selector(
        "text=Pushed 1 file to Uplus-Type/Galien. Not sent: font-data.json."
    )
    page.click(".panel.remote >> text=Pull")
    page.wait_for_selector("text=Pulled into upstream/main (1 glyphs changed).")
    page.click(".panel.remote >> text=Disconnect")
    page.wait_for_function("calls.some(c => c.method === 'DELETE')")
    assert page.errors == []
    page.close()


def test_remote_unmerged_and_designer(browser_and_url):  # noqa: F811
    status = {
        "upstreamBranch": "upstream/main",
        "remoteMoved": False,
        "unmerged": True,
        "pending": [],
        "pendingCount": 0,
        "canPush": False,
    }
    page = open_home(
        browser_and_url,
        "#project/uplustype/Mutator",
        remote_answers(role="designer", remote=GALIEN, status=status),
    )
    page.wait_for_selector("text=not merged into main yet")
    text = page.inner_text(".panel.remote")
    assert "Merge upstream/main" not in text  # designers do not merge
    assert "Pull" in text and "Push" not in text and "Disconnect" not in text
    assert page.errors == []
    page.close()


def test_github_notice_after_the_trip(browser_and_url):  # noqa: F811
    page = open_home(
        browser_and_url,
        "?project=uplustype%2FMutator&github=requested#project/uplustype/Mutator",
        remote_answers(),
    )
    page.wait_for_selector("text=sent to an owner of the GitHub organization")
    assert "github=" not in page.url
    assert page.errors == []
    page.close()


def test_new_project_from_a_git_repository(browser_and_url):  # noqa: F811
    """New project "from a Git repository": its page, without a font, shows
    the Git repository panel first; connecting pulls the font at once."""
    new_one = project("uplustype", "New-One", "admin")
    new_one["capabilities"] = [
        "read",
        "edit",
        "branch",
        "merge",
        "export",
        "invite",
        "administer",
    ]
    hive = "/api/hive/projects/uplustype%2FNew-One"
    github = {
        "configured": True,
        "connected": True,
        "login": "jhornus",
        "installUrl": "https://github.com/apps/fontra-hive",
    }
    extra = remote_answers(github=github)
    extra.update(
        {
            "GET /api/projects/uplustype/New-One": [200, {"project": new_one}],
            f"GET {hive}/repository": [200, {"exists": False}],
            "GET /api/projects/uplustype/New-One/remote": [200, {"remote": None}],
            "PUT /api/projects/uplustype/New-One/remote": [200, {"remote": GALIEN}],
            f"POST {hive}/remote/pull": [
                200,
                {
                    "branch": "upstream/main",
                    "remoteHead": "a" * 40,
                    "commit": "b" * 40,
                    "glyphs": ["A", "B"],
                    "merged": "main",
                },
            ],
        }
    )
    page = open_home(browser_and_url, "#new", extra)
    page.wait_for_selector("form select[name=owner]")
    page.fill("input[name=name]", "New-One")
    page.check("input[value=git]")
    page.click("form button[type=submit]")
    page.wait_for_selector(".panel.remote select[aria-label='Repository']")
    assert "Connect the repository below" in page.inner_text(".notice")
    # Right under "No font yet", before People.
    titles = page.eval_on_selector_all(".panel h3", "hs => hs.map(h => h.textContent)")
    assert titles[:3] == ["No font yet", "Git repository", "People"]
    links = page.eval_on_selector_all(
        ".panel.remote a:not([href='/git'])",
        "as => as.map(a => a.getAttribute('href'))",
    )
    assert all(
        link.startswith("/api/github/connect?") for link in links
    )  # same tab, back here
    page.select_option(
        ".panel.remote select[aria-label='Font in the repository']",
        "Sources/Galien.designspace",
    )
    page.click(".panel.remote form >> text=Connect this repository")
    page.wait_for_function("calls.some(c => c.path.endsWith('/remote/pull'))")
    page.wait_for_selector("text=is now the project's (2 glyphs changed)")
    assert page.errors == []
    page.close()


def test_remote_fontra_repository_has_no_beta_note(browser_and_url):  # noqa: F811
    """A .fontra repository: no beta note, and nothing in its place."""
    hexa = dict(
        GALIEN,
        url="https://github.com/Uplus-Type/HexaSerif.git",
        repository="Uplus-Type/HexaSerif",
        path="Sources/HexaSerif.fontra",
        format="fontra",
    )
    status = {
        "upstreamBranch": "upstream/main",
        "remoteMoved": False,
        "unmerged": False,
        "pending": [],
        "pendingCount": 0,
        "canPush": True,
    }
    page = open_home(
        browser_and_url,
        "#project/uplustype/Mutator",
        remote_answers(remote=hexa, status=status),
    )
    page.wait_for_selector("text=Up to date.")
    text = page.inner_text(".panel.remote")
    assert "null" not in text and "beta" not in text
    assert page.errors == []
    page.close()
