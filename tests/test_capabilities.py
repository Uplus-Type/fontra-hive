"""What the accounts service may take away: the capabilities it lists are
intersected with the role's, and every route that writes into a project's
fonts refuses an access without the capabilities that writing takes.

The inventory below walks every POST, PATCH, PUT and DELETE route under
/api/hive/projects/: a route added later without a check makes it fail."""

import asyncio
import json

import pytest

pytest.importorskip("aiohttp.test_utils")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402
from conftest import FIXTURE  # noqa: E402
from test_remote_sync import FakeApi  # noqa: E402

from fontra_hive.access import CAPABILITIES, Access, HiveUser  # noqa: E402
from fontra_hive.gitstore import GitRepoStore, Signature  # noqa: E402
from fontra_hive.hiveapi import (  # noqa: E402
    HiveApi,
    ProjectAccess,
    allowedCapabilities,
)
from fontra_hive.hivemanager import HiveProjectManager  # noqa: E402

PROJECT = "ana/AnaSans"
USER = HiveUser("ana", "Ana", "", "uid-ana")

# The owner of a project who may not change fonts at the moment: a
# reviewer's capabilities, plus download and administration (as hive-api
# gives them).
OWNER_READ_ONLY = CAPABILITIES["reviewer"] | {"export", "administer"}
# A project whose fonts cannot change at the moment: the role's
# capabilities, without those that write the fonts.
FONT_WRITE = frozenset({"edit", "branch", "snapshot", "merge", "tag"})
READ_ONLY_SPACE = CAPABILITIES["admin"] - FONT_WRITE

# Routes that write nothing into the fonts: presence, comments (open to
# reviewers whatever else they may do), and the members route that hive-api
# answers instead of this server.
NOT_FONT_WRITES = {
    "/api/hive/projects/{name}/presence",
    "/api/hive/projects/{name}/members",
}
COMMENTS = "/api/hive/projects/{name}/comments"


def test_hive_api_can_only_take_away():
    assert allowedCapabilities("admin", ["read", "comment"]) == {"read", "comment"}
    # Never more than the role gives, whatever the list says.
    assert allowedCapabilities("reviewer", ["read", "comment", "edit"]) == {
        "read",
        "comment",
    }
    # An older hive-api that lists nothing: the role's.
    assert allowedCapabilities("designer", None) == CAPABILITIES["designer"]
    assert allowedCapabilities("designer", "edit") == CAPABILITIES["designer"]


class FakeResponse:
    def __init__(self, data):
        self.status = 200
        self._data = data

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, data):
        self.data = data

    def get(self, url, **kwargs):
        return FakeResponse(self.data)


def test_access_keeps_the_listed_capabilities_and_the_notice():
    notice = {
        "code": "guest",
        "text": "Fonts cannot change now.",
        "url": "https://example.com/why",
    }
    data = {
        "user": {"username": "ana", "name": "Ana"},
        "id": PROJECT,
        "repo": "abc.git",
        "defaultBranch": "main",
        "role": "admin",
        "capabilities": sorted(OWNER_READ_ONLY),
        "notice": notice,
    }
    api = HiveApi("http://hive-api", "key", session=FakeSession(data))
    result = asyncio.run(api.access("hive:uid-ana", PROJECT))
    assert result.access.role == "admin"
    assert result.access.capabilities == OWNER_READ_ONLY
    assert not result.access.can("edit")
    assert result.access.notice == notice
    assert result.access.to_json()["notice"] == notice
    # The editor's synchronous check reads the same answer.
    assert not api.cachedAccess("hive:uid-ana", PROJECT).can("edit")
    # A notice link that is not a web address is dropped.
    data["notice"] = {"code": "x", "text": "t", "url": "javascript:alert(1)"}
    api = HiveApi("http://hive-api", "key", session=FakeSession(data))
    assert asyncio.run(api.access("hive:uid-ana", PROJECT)).access.notice["url"] is None


@pytest.fixture
def manager(tmp_path):
    remote = GitRepoStore.create(tmp_path / "remote.git")
    remote.import_directory(FIXTURE, message="Initial", author=Signature("O", "o@x"))
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "abc.git")
    store.import_directory(FIXTURE, message="Import", author=Signature("A", "a@hive"))
    store.create_branch("draft", "main")
    store.close()
    m = HiveProjectManager(
        root, api=FakeApi(remote.path), proxy=False, remoteAllowLocal=True
    )
    m.capabilities = OWNER_READ_ONLY

    async def projectAccess(request, name):
        return ProjectAccess(
            Access(
                USER,
                "admin",
                m.capabilities,
                notice={"code": "x", "text": "Not now."},
            ),
            "abc.git",
            "main",
            PROJECT,
        )

    m._projectAccess = projectAccess
    return m


def font_write_routes(m):
    for route in m.webRoutes():
        if route.method not in ("POST", "PATCH", "PUT", "DELETE"):
            continue
        path = route.path
        if not path.startswith("/api/hive/projects/{name}"):
            continue
        if path in NOT_FONT_WRITES or path.startswith(COMMENTS):
            continue
        yield route


def call(route, body=None):
    async def go():
        # Every parameter a route may want, so that each one reaches its check.
        query = "?branch=draft&from=draft&into=main&glyph=A&ref=main&name=v1"
        request = make_mocked_request(
            route.method,
            route.path.replace("{name}", PROJECT) + query,
            match_info={"name": PROJECT},
        )
        payload = json.dumps(
            body or {"branch": "draft", "name": "draft", "from": "main"}
        )

        async def text():
            return payload

        async def as_json(*args, **kwargs):
            return json.loads(payload)

        async def multipart():
            raise AssertionError("read the upload before checking the capabilities")

        request.text = text
        request.json = as_json
        request.multipart = multipart
        try:
            return await route.handler(request)
        except web.HTTPException as error:
            return error

    return asyncio.run(go())


def test_the_inventory_is_not_empty(manager):
    paths = {r.path for r in font_write_routes(manager)}
    for expected in (
        "import",
        "restore",
        "branches",
        "merge",
        "snapshot",
        "remote/pull",
    ):
        assert any(p.endswith(expected) for p in paths), expected


@pytest.mark.parametrize("capabilities", [OWNER_READ_ONLY, READ_ONLY_SPACE])
def test_every_font_write_route_refuses(manager, capabilities):
    manager.capabilities = capabilities
    answers = {}
    for route in font_write_routes(manager):
        answers[f"{route.method} {route.path}"] = call(route).status
    refused = {k: v for k, v in answers.items() if v != 403}
    assert not refused, refused


def test_comments_stay_open(manager):
    manager.capabilities = OWNER_READ_ONLY
    route = next(
        r for r in manager.webRoutes() if r.method == "POST" and r.path == COMMENTS
    )
    body = {"glyph": "A", "text": "Looks good", "x": 0, "y": 0}
    assert call(route, body).status != 403


def test_an_open_editor_loses_edit_at_the_next_refresh():
    """A connection opened while the person could edit: the editor reads the
    cached answer, refreshed in the background once it is ``accessTTL`` old
    (10 s in production), and the next edit is refused."""
    data = {
        "user": {"username": "ana", "name": "Ana"},
        "id": PROJECT,
        "repo": "abc.git",
        "defaultBranch": "main",
        "role": "designer",
        "capabilities": sorted(CAPABILITIES["designer"]),
    }
    session = FakeSession(data)
    api = HiveApi("http://hive-api", "key", session=session, accessTTL=0)
    token = "hive:uid-ana"

    async def go():
        await api.access(token, PROJECT)
        assert api.cachedAccess(token, PROJECT).can("edit")
        session.data = {**data, "capabilities": ["comment", "read"]}
        api.cachedAccess(token, PROJECT)  # old: starts a refresh
        await asyncio.sleep(0.01)
        return api.cachedAccess(token, PROJECT)

    assert not asyncio.run(go()).can("edit")
