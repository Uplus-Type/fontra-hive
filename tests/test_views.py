"""Hive in Fontra's views: pages served with the Hive scripts, sign-in
redirects, presence heartbeats and project members."""

import json
import pathlib
from types import SimpleNamespace

import pytest
from aiohttp import web

from conftest import run
from fontra_hive.access import DevDirectory
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.projectmanager import (
    DEV_USER_COOKIE,
    DevHiveProjectManager,
    _safeRef,
    injectHiveScripts,
)
from test_access import USERS

ME = Signature("Import", "import@example.com")


def request(project=None, user=None, body=None, path_qs="/", **query):
    async def read_json():
        return body or {}

    return SimpleNamespace(
        match_info={"name": project} if project else {},
        query=query,
        cookies={DEV_USER_COOKIE: user} if user else {},
        headers={},
        host="localhost:8000",
        path_qs=path_qs,
        json=read_json,
    )


@pytest.fixture
def manager(tmp_path, fixture_fontra):
    root = tmp_path / "repos"
    root.mkdir()
    for name in ["Mutator", "Solo"]:
        store = GitRepoStore.create(root / f"{name}.git")
        store.import_directory(fixture_fontra, message="Import", author=ME)
        store.close()
    users = root / "hive-dev-users.json"
    users.write_text(json.dumps(USERS), encoding="utf-8")
    manager = DevHiveProjectManager(root, directory=DevDirectory(users))
    pages = tmp_path / "client"
    pages.mkdir()
    for view in [
        "editor",
        "fontoverview",
        "fontinfo",
        "applicationsettings",
        "landing",
    ]:
        (pages / f"{view}.html").write_text(
            f"<!doctype html><html><head><title>{view}</title></head>"
            f'<body><div class="top-bar-container"></div></body></html>'
        )
    manager.fontraClientFile = lambda name: pages / name
    return manager


def test_inject_hive_scripts():
    page = injectHiveScripts(
        "<html><HEAD><title>x</title></HEAD><body><p>x</p></BODY></html>"
    )
    assert page.index('src="/hive/views/register.js"') < page.index("<title>")
    assert page.index('src="/hive/views/hive-views.js"') > page.index("<p>x</p>")
    assert page.endswith("</BODY></html>")
    assert page.index('href="/hive/icons/hive-icon.svg"') < page.index("<title>")
    assert 'rel="apple-touch-icon"' in page and 'rel="manifest"' in page
    bare = injectHiveScripts("<p>fragment</p>")
    assert bare.startswith('<script src="/hive/views/register.js">')
    assert "hive-views.js" in bare


def test_views_need_a_session_and_get_the_scripts(manager):
    async def go():
        with pytest.raises(web.HTTPFound) as raised:
            await manager.viewHandler(
                request(path_qs="/editor.html?project=Mutator"), view="editor"
            )
        assert raised.value.location == "/?ref=%2Feditor.html%3Fproject%3DMutator"
        response = await manager.viewHandler(request(user="ana"), view="editor")
        assert "hive-views.js" in response.text and "register.js" in response.text
        assert response.headers["Cache-Control"] == "no-cache"
        # The sign-in page carries the page to come back to.
        page = (
            await manager.rootDocumentHandler(
                request(ref="/editor.html?project=Mutator")
            )
        ).text
        assert 'name="ref" value="/editor.html?project=Mutator"' in page
        # Signed in: the project list page, with the Hive scripts.
        landing = (await manager.rootDocumentHandler(request(user="ana"))).text
        assert "<title>landing</title>" in landing and "hive-views.js" in landing

    run(go())


def test_safe_ref():
    assert _safeRef("/editor.html?project=A") == "/editor.html?project=A"
    for bad in ["//evil.example/x", "https://evil.example", None, "", "editor.html"]:
        assert _safeRef(bad) == "/"


def test_presence_heartbeats(manager, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("fontra_hive.projectmanager.time.monotonic", lambda: clock[0])

    async def beat(user, client, **body):
        response = await manager.presenceHandler(
            request("Mutator", user=user, body={"client": client, **body})
        )
        return json.loads(response.body)["others"]

    async def go():
        assert await beat("fabio", "f1", view="editor", glyph="A") == []
        others = await beat("ana", "a1", view="fontoverview", branch="main")
        assert [(o["username"], o["view"], o["glyph"], o["role"]) for o in others] == [
            ("fabio", "editor", "A", "designer")
        ]
        clock[0] += 30  # fabio's tab went away without a word
        assert await beat("ana", "a1", view="fontoverview") == []
        with pytest.raises(web.HTTPBadRequest):
            await beat("ana", "", view="editor")

    async def solo():
        await manager.presenceHandler(request("Solo", user="ana", body={"client": "x"}))

    run(go())
    with pytest.raises(web.HTTPNotFound):
        run(solo())


def test_members_and_changing_them(manager):
    def members(data):
        return [(m["username"], m["role"], m["via"]) for m in data["members"]]

    async def go():
        data = json.loads(
            (await manager.membersHandler(request("Mutator", user="ana"))).body
        )
        assert members(data) == [
            ("jeremie", "admin", "organization U+Type"),
            ("fabio", "designer", "organization U+Type"),
            ("ana", "observer", "collaborator"),
        ]
        assert data["canManage"] is False and data["users"] == []
        with pytest.raises(web.HTTPForbidden):  # an observer cannot change members
            await manager.setMemberHandler(
                request(
                    "Mutator", user="ana", body={"username": "zoe", "role": "designer"}
                )
            )

        data = json.loads(
            (await manager.membersHandler(request("Mutator", user="jeremie"))).body
        )
        assert data["canManage"] and [u["username"] for u in data["users"]] == ["zoe"]
        data = json.loads(
            (
                await manager.setMemberHandler(
                    request(
                        "Mutator",
                        user="jeremie",
                        body={"username": "zoe", "role": "reviewer"},
                    )
                )
            ).body
        )
        assert ("zoe", "reviewer", "collaborator") in members(data)
        assert manager.directory.role("zoe", "Mutator") == "reviewer"
        saved = json.loads(pathlib.Path(manager.directory.path).read_text())
        assert saved["projects"]["Mutator"]["collaborators"]["zoe"] == "reviewer"
        # Promoted, then removed.
        await manager.setMemberHandler(
            request(
                "Mutator", user="jeremie", body={"username": "ana", "role": "designer"}
            )
        )
        assert manager.directory.role("ana", "Mutator") == "designer"
        await manager.setMemberHandler(
            request("Mutator", user="jeremie", body={"username": "ana", "role": None})
        )
        assert manager.directory.role("ana", "Mutator") is None
        with pytest.raises(web.HTTPNotFound):
            await manager.setMemberHandler(
                request(
                    "Mutator",
                    user="jeremie",
                    body={"username": "ghost", "role": "admin"},
                )
            )
        with pytest.raises(web.HTTPConflict):
            await manager.setMemberHandler(
                request(
                    "Mutator", user="jeremie", body={"username": "zoe", "role": "boss"}
                )
            )

    run(go())


def test_a_project_keeps_an_admin(tmp_path):
    data = {
        "users": {"a": {"name": "A"}, "b": {"name": "B"}},
        "projects": {
            "P": {"owner": None, "collaborators": {"a": "admin", "b": "designer"}}
        },
    }
    path = tmp_path / "users.json"
    path.write_text(json.dumps(data))
    directory = DevDirectory(path)
    with pytest.raises(ValueError, match="at least one admin"):
        directory.set_collaborator("P", "a", "designer")
    with pytest.raises(ValueError, match="at least one admin"):
        directory.set_collaborator("P", "a", None)
    directory.set_collaborator("P", "b", "admin")
    directory.set_collaborator("P", "a", None)
    assert directory.role("a", "P") is None and directory.role("b", "P") == "admin"
