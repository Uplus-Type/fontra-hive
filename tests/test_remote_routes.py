"""The HTTP routes of remote repositories in the Hive project manager:
roles, error answers, the default branch only. hive-api is a fake; the
remote, a local bare repository."""

import asyncio
import json

import pytest

pytest.importorskip("aiohttp.test_utils")
pytest.importorskip("multidict")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402
from conftest import FIXTURE, files_of  # noqa: E402
from test_remote_sync import FakeApi  # noqa: E402

from fontra_hive.access import CAPABILITIES, Access, HiveUser  # noqa: E402
from fontra_hive.gitstore import GitRepoStore, Signature  # noqa: E402
from fontra_hive.hiveapi import ProjectAccess  # noqa: E402
from fontra_hive.hivemanager import HiveProjectManager  # noqa: E402

PROJECT = "uplustype/Mutator"


def access(role):
    user = HiveUser("jeremie", "Jérémie Hornus", "", "uid-1")
    return Access(user, role, CAPABILITIES[role])


@pytest.fixture
def manager(tmp_path):
    remote_store = GitRepoStore.create(tmp_path / "remote.git")
    remote_store.commit(
        {f"Font.fontra/{p}": d for p, d in files_of(FIXTURE).items()},
        message="Initial",
        author=Signature("Outsider", "o@example.com"),
    )
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "abc.git")
    store.import_directory(FIXTURE, message="Import", author=Signature("A", "a@hive"))
    store.close()
    api = FakeApi(remote_store.path)
    m = HiveProjectManager(root, api=api, proxy=False, remoteAllowLocal=True)
    m.role = "admin"

    async def projectAccess(request, name):
        return ProjectAccess(access(m.role), "abc.git", "main", PROJECT)

    m._projectAccess = projectAccess
    return m, api, remote_store


def call(handler, method, body=None):
    async def go():
        request = make_mocked_request(
            method,
            f"/api/hive/projects/{PROJECT}/remote",
            match_info={"name": PROJECT},
        )
        if body is None:

            async def read_text():
                return ""

            request.text = read_text
        else:

            async def read_text():
                return json.dumps(body)

            request.text = read_text
        try:
            return await handler(request)
        except web.HTTPException as error:
            return error

    return asyncio.run(go())


def test_status_pull_push(manager):
    m, api, remote_store = manager
    response = call(m.remotePullHandler, "POST")
    assert response.status == 200, response.text
    assert json.loads(response.text)["branch"] == "upstream/main"
    status = json.loads(call(m.remoteStatusHandler, "GET").text)
    assert status["unmerged"] is True
    # Not merged: refused with a reason the interface can show.
    response = call(m.remotePushHandler, "POST", {})
    assert response.status == 409
    assert json.loads(response.text)["error"] == "not-merged"


def test_roles_and_branches(manager):
    m, api, _ = manager
    m.role = "designer"
    assert call(m.remotePushHandler, "POST", {}).status == 403
    assert call(m.remotePullHandler, "POST").status == 200
    m.role = "observer"
    assert call(m.remotePullHandler, "POST").status == 403
    assert call(m.remoteStatusHandler, "GET").status == 200
    m.role = "manager"
    assert call(m.remotePushHandler, "POST", {"branch": "bold"}).status == 422


def test_unusable_remote(manager):
    m, api, _ = manager
    api.not_usable = "The GitHub app was uninstalled."
    response = call(m.remoteStatusHandler, "GET")
    assert response.status == 409
    assert json.loads(response.text)["message"] == "The GitHub app was uninstalled."
    api.not_usable = None
    api.remote_info = None

    async def none(project):
        return None

    api.remote = none
    assert call(m.remoteStatusHandler, "GET").status == 404
