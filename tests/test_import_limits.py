"""Imports, against abuse (security review of 16 Oct 2026, L-1): imports a
day counted by hive-api, one import at a time per project, a few
conversions at once, none when the disk runs short."""

import asyncio
from collections import namedtuple
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web

from conftest import run
from fontra_hive import hivemanager
from fontra_hive.access import CAPABILITIES, Access, HiveUser
from fontra_hive.hiveapi import HiveApiUnavailable, ProjectAccess
from fontra_hive.hivemanager import HiveProjectManager

ANA = HiveUser("ana", "Ana López", "ana@example.com", uid="u-ana")
Usage = namedtuple("Usage", "total used free")


class FakeApi:
    def __init__(self, refuse=None):
        self.refuse = refuse
        self.counted = []

    async def importStarted(self, uid, project):
        self.counted.append((uid, project))
        if self.refuse == "down":
            raise HiveApiUnavailable("down")
        return self.refuse

    async def aclose(self):
        pass


def make_manager(tmp_path, api):
    root = tmp_path / "repos"
    root.mkdir()
    manager = HiveProjectManager(root, api=api, proxy=False)
    access = ProjectAccess(
        access=Access(ANA, "admin", CAPABILITIES["admin"]),
        repo="uid.git",
        defaultBranch="main",
        projectId="ana/AnaSans",
    )

    async def projectAccess(request, name):
        return access

    manager._projectAccess = projectAccess
    return manager


def request():
    return SimpleNamespace(match_info={"name": "ana/AnaSans"})


def test_over_the_daily_limit_nothing_is_read(tmp_path):
    api = FakeApi(refuse=(429, "Free includes 10 imports a day."))
    manager = make_manager(tmp_path, api)
    manager._import = mock.AsyncMock()

    async def go():
        with pytest.raises(web.HTTPTooManyRequests) as refused:
            await manager.importHandler(request())
        assert refused.value.text == "Free includes 10 imports a day."
        manager._import.assert_not_called()
        assert api.counted == [("u-ana", "ana/AnaSans")]
        assert manager._importing == set()

    run(go())


def test_gone_since_the_access_was_cached(tmp_path):
    manager = make_manager(tmp_path, FakeApi(refuse=(403, "Not now.")))
    manager._import = mock.AsyncMock()

    async def go():
        with pytest.raises(web.HTTPForbidden):
            await manager.importHandler(request())
        manager._import.assert_not_called()

    run(go())


def test_hive_api_down_refuses(tmp_path):
    manager = make_manager(tmp_path, FakeApi(refuse="down"))
    manager._import = mock.AsyncMock()

    async def go():
        with pytest.raises(web.HTTPServiceUnavailable):
            await manager.importHandler(request())
        manager._import.assert_not_called()

    run(go())


def test_one_import_at_a_time_per_project(tmp_path):
    api = FakeApi()
    manager = make_manager(tmp_path, api)
    started, release = asyncio.Event(), None

    async def slow(request, projectAccess):
        started.set()
        await release.wait()
        return web.json_response({"ok": True})

    manager._import = slow

    async def go():
        nonlocal release
        release = asyncio.Event()
        first = asyncio.ensure_future(manager.importHandler(request()))
        await started.wait()
        with pytest.raises(web.HTTPConflict):
            await manager.importHandler(request())
        release.set()
        assert (await first).status == 200
        # Done: the next one goes.
        started.clear()
        release.set()
        assert (await manager.importHandler(request())).status == 200
        assert len(api.counted) == 2  # the refused one was not counted

    run(go())


def test_short_of_disk(tmp_path):
    api = FakeApi()
    manager = make_manager(tmp_path, api)
    manager._import = mock.AsyncMock()

    async def go():
        with mock.patch.object(
            hivemanager.shutil, "disk_usage", return_value=Usage(100, 99, 1)
        ):
            with pytest.raises(web.HTTPInsufficientStorage):
                await manager.importHandler(request())
        manager._import.assert_not_called()
        assert api.counted == []

    run(go())


def test_min_free_disk_setting(monkeypatch):
    monkeypatch.setenv("HIVE_MIN_FREE_DISK_GB", "0.5")
    assert hivemanager.minFreeDisk() == 512 * 1024**2
    monkeypatch.setenv("HIVE_MIN_FREE_DISK_GB", "x")
    assert hivemanager.minFreeDisk() == 5 * 1024**3
