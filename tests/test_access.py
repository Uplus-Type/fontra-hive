import json
import os
import time
from types import SimpleNamespace

import pytest
from aiohttp import web

from conftest import run
from fontra_hive.access import CAPABILITIES, DevDirectory, highest_role, token_for
from fontra_hive.fonthandler import ReadOnlyError
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.projectmanager import DEV_USER_COOKIE, DevHiveProjectManager

ME = Signature("Import", "import@example.com")

USERS = {
    "users": {
        "jeremie": {"name": "Jérémie Hornus", "email": "jeremie@uplustype.com"},
        "fabio": {"name": "Fabio Caccamo", "email": "fabio@example.com"},
        "ana": {"name": "Ana López", "email": "ana@example.com"},
        "zoe": {"name": "Zoé", "email": "zoe@example.com"},
    },
    "organizations": {
        "uplustype": {
            "name": "U+Type",
            "members": {"jeremie": "owner", "fabio": "member"},
            "base_role": "designer",
        }
    },
    "projects": {
        "Mutator": {"owner": "uplustype", "collaborators": {"ana": "observer"}},
        "Solo": {"owner": "zoe", "collaborators": {"fabio": "manager"}},
    },
}


@pytest.fixture
def directory(tmp_path):
    path = tmp_path / "hive-dev-users.json"
    path.write_text(json.dumps(USERS), encoding="utf-8")
    return DevDirectory(path)


def test_capabilities_grow_with_the_role():
    assert CAPABILITIES["observer"] == {"read"}
    assert (
        "comment" in CAPABILITIES["reviewer"] and "edit" not in CAPABILITIES["reviewer"]
    )
    assert {"edit", "branch", "snapshot"} <= CAPABILITIES["designer"]
    assert "merge" not in CAPABILITIES["designer"]
    assert {"merge", "tag", "export", "invite"} <= CAPABILITIES["manager"]
    assert (
        "administer" in CAPABILITIES["admin"]
        and "administer" not in CAPABILITIES["manager"]
    )
    assert highest_role("observer", None, "manager", "designer") == "manager"
    assert highest_role(None, "nonsense") is None


def test_roles_follow_the_github_model(directory):
    # organization owner -> admin; member -> the organization's base role;
    # outside collaborator -> their role; the highest one wins.
    assert directory.role("jeremie", "Mutator") == "admin"
    assert directory.role("fabio", "Mutator") == "designer"
    assert directory.role("ana", "Mutator") == "observer"
    assert directory.role("zoe", "Mutator") is None
    assert directory.role("zoe", "Solo") == "admin"  # personal project
    assert directory.role("fabio", "Solo") == "manager"
    assert directory.role("jeremie", "Solo") is None
    assert directory.role("jeremie", "Unlisted") is None
    assert directory.role("nobody", "Mutator") is None
    assert directory.projects_for("fabio") == ["Mutator", "Solo"]
    access = directory.access("ana", "Mutator")
    assert access.read_only and access.can("read") and not access.can("edit")
    assert access.user.signature.email == "ana@hive"  # never the real email
    assert access.to_json()["user"]["email"] == "ana@example.com"


def test_directory_is_reread_when_the_file_changes(directory):
    data = json.loads(directory.path.read_text())
    data["projects"]["Mutator"]["collaborators"]["ana"] = "designer"
    directory.path.write_text(json.dumps(data))
    later = time.time() + 5
    os.utime(directory.path, (later, later))
    assert directory.role("ana", "Mutator") == "designer"
    directory.path.write_text(
        "{ broken"
    )  # a typo while editing: keep the last good one
    os.utime(directory.path, (later + 5, later + 5))
    assert directory.role("ana", "Mutator") == "designer"


# --- the development project manager with accounts ------------------------------


def request(
    project=None, user=None, method="GET", origin=None, host="localhost:8000", **q
):
    cookies = {DEV_USER_COOKIE: user} if user else {}
    headers = {"Origin": origin} if origin else {}
    return SimpleNamespace(
        match_info={"name": project} if project else {},
        query=q,
        cookies=cookies,
        headers=headers,
        host=host,
        method=method,
    )


@pytest.fixture
def manager(tmp_path, fixture_fontra, directory):
    root = tmp_path / "repos"
    root.mkdir()
    for name in ["Mutator", "Solo", "Unlisted"]:
        store = GitRepoStore.create(root / f"{name}.git")
        store.import_directory(fixture_fontra, message="Import", author=ME)
        store.close()
    return DevHiveProjectManager(root, directory=directory, commitDelay=0.05)


def test_login_identity_and_project_list(manager):
    async def go():
        assert await manager.authorize(request()) is None
        assert await manager.authorize(request(user="ghost")) is None
        token = await manager.authorize(request(user="fabio"))
        assert token == token_for("fabio")
        # Cross-site: refused even with a valid cookie.
        assert (
            await manager.authorize(
                request(user="fabio", origin="https://evil.example")
            )
            is None
        )
        assert await manager.authorize(
            request(user="fabio", origin="http://localhost:8000")
        )

        assert await manager.getProjectList(token_for("fabio")) == ["Mutator", "Solo"]
        assert await manager.getProjectList(token_for("ana")) == ["Mutator"]
        assert await manager.getProjectList(token_for("zoe")) == ["Solo"]
        assert await manager.projectAvailable("Mutator", token_for("ana"))
        assert not await manager.projectAvailable("Solo", token_for("ana"))
        assert (
            await manager.getRemoteSubject("Unlisted", token_for("jeremie"), False)
            is None
        )

        # Not logged in: the root page is the development login page.
        page = (await manager.rootDocumentHandler(request())).text
        assert "Jérémie Hornus" in page and 'action="/hive/dev-login"' in page

        me = json.loads((await manager.meHandler(request(user="ana"))).body)
        assert me["user"]["username"] == "ana" and me["accounts"]
        with pytest.raises(web.HTTPUnauthorized):
            await manager.meHandler(request())
        access = json.loads(
            (await manager.accessHandler(request("Mutator", user="ana"))).body
        )
        assert access["role"] == "observer" and access["capabilities"] == ["read"]
        with pytest.raises(web.HTTPNotFound):  # not a member: looks absent
            await manager.accessHandler(request("Solo", user="ana"))

    run(go())


def test_hive_routes_check_the_role(manager):
    async def go():
        log = json.loads(
            (await manager.logHandler(request("Mutator", user="ana", glyph="B"))).body
        )
        first = log["commits"][-1]["sha"]
        with pytest.raises(web.HTTPNotFound):
            await manager.logHandler(request("Solo", user="ana", glyph="B"))
        with pytest.raises(web.HTTPNotFound):
            await manager.glyphHandler(request("Solo", user="ana", glyph="B"))
        with pytest.raises(web.HTTPNotFound):
            await manager.headHandler(request("Mutator"))  # not logged in

        # An observer can read but not restore nor snapshot.
        with pytest.raises(web.HTTPForbidden):
            await manager.restoreHandler(
                request("Mutator", user="ana", method="POST", glyph="B", ref=first)
            )
        with pytest.raises(web.HTTPForbidden):
            await manager.snapshotHandler(
                request("Mutator", user="ana", method="POST", name="v1")
            )

        # A designer can snapshot; the commit is theirs (pseudonymous email).
        created = json.loads(
            (
                await manager.snapshotHandler(
                    request("Mutator", user="fabio", method="POST", name="v1")
                )
            ).body
        )
        assert created["snapshot"]["author"] == "Fabio Caccamo"
        store = GitRepoStore.open(manager.rootPath / "Mutator.git")
        try:
            assert store.commit_info(created["head"]).email == "fabio@hive"
        finally:
            store.close()

    run(go())


def connection(user, uuid):
    async def externalChange(change, isLiveChange):
        pass

    return SimpleNamespace(
        authorizationToken=token_for(user),
        clientUUID=uuid,
        proxy=SimpleNamespace(externalChange=externalChange),
    )


def test_one_shared_handler_with_roles_and_authors_per_connection(manager):
    async def go():
        handler = await manager.getRemoteSubject("Mutator", token_for("fabio"), False)
        # The same handler for everyone: that is what keeps live collaboration.
        assert (
            await manager.getRemoteSubject("Mutator", token_for("ana"), False)
            is handler
        )
        assert (
            await manager.getRemoteSubject("Mutator", token_for("zoe"), False) is None
        )

        fabio, ana = connection("fabio", "c-fabio"), connection("ana", "c-ana")
        assert await handler.isReadOnly(connection=fabio) is False
        assert await handler.isReadOnly(connection=ana) is True

        glyph = await handler.getGlyph("B", connection=fabio)
        layerName = next(iter(glyph.layers))
        path = ["glyphs", "B", "layers", layerName, "glyph"]

        def change(value):
            return {"p": path, "f": "=", "a": ["xAdvance", value]}

        # The observer's edit is refused before anything is written or broadcast.
        with pytest.raises(ReadOnlyError, match="observer"):
            await handler.editFinal(change(111), {}, "edit", False, connection=ana)
        with pytest.raises(ReadOnlyError):
            await handler.editIncremental(change(111), connection=ana)

        await handler.editFinal(change(777), {}, "edit", False, connection=fabio)
        await handler.finishWriting()
        handler.backend.flush()

        store = handler.backend.store
        head = store.head("main")
        info = store.commit_info(head)
        assert (info.author, info.email) == ("Fabio Caccamo", "fabio@hive")
        assert "Hive-Glyphs: B" in info.message
        assert b'"xAdvance": 777' in store.read_file(head, "glyphs/B^1.json")
        await manager.aclose()

    run(go())


def test_without_a_directory_nothing_changes(tmp_path, fixture_fontra):
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    store.close()
    manager = DevHiveProjectManager(root)

    async def go():
        assert await manager.authorize(request()) == "dev"
        assert await manager.getProjectList("dev") == ["Mutator"]
        handler = await manager.getRemoteSubject("Mutator", "dev", False)
        assert await handler.isReadOnly(connection=connection("x", "c")) is False
        me = json.loads((await manager.meHandler(request())).body)
        assert me == {"user": None, "accounts": False}
        await manager.aclose()

    run(go())


def test_dev_login_and_logout(manager):
    def post(**form):
        async def read():
            return form

        return SimpleNamespace(post=read, cookies={}, headers={}, host="localhost:8000")

    async def go():
        with pytest.raises(web.HTTPFound) as raised:
            await manager.devLoginHandler(post(user="ana"))
        assert raised.value.location == "/"
        value, options = raised.value.cookies[DEV_USER_COOKIE]
        assert value == "ana" and options["httponly"] and options["samesite"] == "Lax"
        with pytest.raises(web.HTTPBadRequest):
            await manager.devLoginHandler(post(user="ghost"))
        with pytest.raises(web.HTTPFound) as raised:
            await manager.devLogoutHandler(request(user="ana"))
        assert raised.value.cookies[DEV_USER_COOKIE][0] is None

    run(go())


def test_startup_says_whether_accounts_are_on(tmp_path, caplog):
    import logging

    from fontra_hive.projectmanager import _logAccounts

    caplog.set_level(logging.INFO)
    (tmp_path / "hive-dev-users.example.json").write_text(json.dumps(USERS))
    _logAccounts(tmp_path, tmp_path / "hive-dev-users.json")
    text = caplog.text
    assert "Hive accounts: off" in text
    assert "found hive-dev-users.example.json" in text and "rename it" in text
    caplog.clear()
    (tmp_path / "hive-dev-users.json").write_text(json.dumps(USERS))
    _logAccounts(tmp_path, tmp_path / "hive-dev-users.json")
    assert "Hive accounts: on" in caplog.text and "4 users, 2 projects" in caplog.text
