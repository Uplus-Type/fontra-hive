"""Repositories of projects deleted for good in hive-api are removed from disk."""

import asyncio

from fontra_hive.hiveapi import HiveApiUnavailable
from fontra_hive.hivemanager import HiveProjectManager, _isRepoName


class FakeApi:
    def __init__(self, repos):
        self.repos = repos

    async def deletedRepositories(self):
        if self.repos is None:
            raise HiveApiUnavailable("down")
        return self.repos

    async def aclose(self):
        pass


def test_repo_names():
    assert _isRepoName("0b7c1e2a-1111-2222-3333-444455556666.git")
    for bad in (".git", "../x.git", "a/b.git", ".hidden.git", "x", "a\\b.git", ""):
        assert not _isRepoName(bad), bad


def test_sweep_removes_only_listed_closed_repositories(tmp_path):
    for name in ("gone.git", "open.git", "kept.git"):
        (tmp_path / name / "objects").mkdir(parents=True)
    outside = tmp_path.parent / "outside.git"
    outside.mkdir(exist_ok=True)
    api = FakeApi(["gone.git", "open.git", "missing.git", "../outside.git", ".x.git"])
    manager = HiveProjectManager(tmp_path, api=api, proxy=False)
    manager.fontHandlers["open.git@main"] = object()  # someone is in the editor

    removed = asyncio.run(manager.sweepDeletedRepositories())
    assert removed == ["gone.git"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kept.git", "open.git"]
    assert outside.is_dir()

    del manager.fontHandlers["open.git@main"]  # closed: next sweep
    assert asyncio.run(manager.sweepDeletedRepositories()) == ["open.git"]
    assert [p.name for p in tmp_path.iterdir()] == ["kept.git"]

    manager.api = FakeApi(None)  # hive-api down: nothing happens
    assert asyncio.run(manager.sweepDeletedRepositories()) == []
