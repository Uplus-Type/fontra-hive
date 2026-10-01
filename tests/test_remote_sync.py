"""The Fontra server's side of remote repositories (fontra_hive.remotesync):
credentials from hive-api, one operation at a time, messages, co-authors,
pulls after webhooks. hive-api is a fake here; the remote, a local bare
repository holding a .fontra package."""

import asyncio
import pathlib

import pytest
from conftest import FIXTURE, files_of

from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.remotesync import (
    DEFAULT_MESSAGE,
    NoRemote,
    RemoteNotUsable,
    RemoteSync,
)

ALICE = Signature("Alice", "alice@hive")
BOB = Signature("Bob", "bob@hive")
OUTSIDER = Signature("Outsider", "outsider@example.com")
PROJECT = "uplustype/Mutator"


class FakeApi:
    def __init__(self, remote_path: pathlib.Path):
        self.remote_info = {
            "provider": "git",
            "url": str(remote_path),
            "branch": "main",
            "path": "Font.fontra",
            "username": None,
            "password": None,
        }
        self.synced = []
        self.moved = []
        self.not_usable = None

    async def remote(self, project):
        if self.not_usable:
            raise RemoteNotUsable(self.not_usable)
        return self.remote_info if project == PROJECT else None

    async def remoteSynced(self, project, sha):
        self.synced.append((project, sha))

    async def remotesMoved(self):
        return self.moved

    async def project(self, project):
        return {"defaultBranch": "main"}


@pytest.fixture
def setup(tmp_path):
    remote_store = GitRepoStore.create(tmp_path / "remote.git")
    remote_store.commit(
        {f"Font.fontra/{p}": d for p, d in files_of(FIXTURE).items()},
        message="Initial",
        author=OUTSIDER,
    )
    repo = tmp_path / "project.git"
    store = GitRepoStore.create(repo)
    store.import_directory(FIXTURE, message="Import", author=ALICE)
    store.close()
    api = FakeApi(remote_store.path)
    calls = []
    sync = RemoteSync(
        api,
        allowLocal=True,
        beforeWrite=lambda path, branch: calls.append(("flush", branch)),
        afterWrite=lambda path, branch: calls.append(("changed", branch)),
    )
    return sync, api, repo, remote_store, calls


def run(coro):
    return asyncio.run(coro)


def edit_glyph(repo, author, factor=1):
    store = GitRepoStore.open(repo)
    try:
        data = store.read_file(store.head("main"), "glyphs/A^1.json")
        changed = data.replace(b'"xAdvance": ', b'"xAdvance": 1' + b"0" * factor, 1)
        store.commit(
            {"glyphs/A^1.json": changed}, message=f"Edit {factor}", author=author
        )
    finally:
        store.close()


def merge_upstream(repo):
    store = GitRepoStore.open(repo)
    try:
        store.fast_forward("main", "upstream/main")
    finally:
        store.close()


def test_no_remote_and_not_usable(setup):
    sync, api, repo, _, _ = setup
    with pytest.raises(NoRemote):
        run(sync.status(repo, "someone/else", "main"))
    api.not_usable = "The GitHub app was uninstalled."
    with pytest.raises(RemoteNotUsable):
        run(sync.pull(repo, PROJECT, "main"))


def test_pull_reports_the_remote_commit(setup):
    sync, api, repo, remote_store, calls = setup
    result = run(sync.pull(repo, PROJECT, "main"))
    assert result["branch"] == "upstream/main"
    assert result["remoteHead"] == remote_store.head("main")
    assert result["commit"]
    assert api.synced == [(PROJECT, remote_store.head("main"))]
    assert ("changed", "upstream/main") in calls
    status = run(sync.status(repo, PROJECT, "main"))
    assert status["unmerged"] is True and status["remoteMoved"] is False
    assert status["upstreamBranch"] == "upstream/main"
    assert status["format"] == "fontra"


def test_push_with_snapshot_title_and_co_authors(setup):
    sync, api, repo, remote_store, calls = setup
    run(sync.pull(repo, PROJECT, "main"))
    merge_upstream(repo)
    edit_glyph(repo, ALICE, 1)
    edit_glyph(repo, BOB, 2)
    store = GitRepoStore.open(repo)
    store.create_snapshot("Proofs for the client", branch="main", author=BOB)
    store.close()

    status = run(sync.status(repo, PROJECT, "main"))
    assert status["pending"] == ["glyphs/A^1.json"] and status["canPush"]

    result = run(sync.push(repo, PROJECT, "main", author=BOB))
    assert result["pushed"] and result["files"] == ["Font.fontra/glyphs/A^1.json"]
    assert ("flush", "main") in calls
    info = remote_store.commit_info(remote_store.head("main"))
    assert info.message.startswith("Proofs for the client")
    assert "Co-authored-by: Alice <alice@hive>" in info.message
    assert "Co-authored-by: Bob" not in info.message  # the pusher is the author
    assert info.author == "Bob"
    assert api.synced[-1] == (PROJECT, remote_store.head("main"))

    # Next push: only the new commits count, no new snapshot.
    edit_glyph(repo, BOB, 3)
    result = run(sync.push(repo, PROJECT, "main", author=BOB))
    assert result["pushed"]
    info = remote_store.commit_info(remote_store.head("main"))
    assert info.message.strip() == DEFAULT_MESSAGE
    assert "Co-authored-by" not in info.message

    # A message given wins.
    edit_glyph(repo, ALICE, 4)
    run(sync.push(repo, PROJECT, "main", author=BOB, message="Wider A"))
    info = remote_store.commit_info(remote_store.head("main"))
    assert info.message.startswith("Wider A")
    assert "Co-authored-by: Alice <alice@hive>" in info.message


def test_pull_moved_after_a_webhook(setup):
    sync, api, repo, remote_store, _ = setup
    run(sync.pull(repo, PROJECT, "main"))
    path = "Font.fontra/glyphs/B^1.json"
    data = remote_store.read_file(remote_store.head("main"), path)
    remote_store.commit(
        {path: data.replace(b'"xAdvance": ', b'"xAdvance": 1', 1)},
        message="Wider B",
        author=OUTSIDER,
    )
    api.moved = [
        {
            "project": PROJECT,
            "repo": "project.git",
            "remoteHead": remote_store.head("main"),
        },
        {"project": "gone/project", "repo": "nowhere.git", "remoteHead": "a" * 40},
    ]
    pulled = run(sync.pullMoved(lambda name: repo.parent / name))
    assert pulled == [PROJECT]
    assert api.synced[-1] == (PROJECT, remote_store.head("main"))
    store = GitRepoStore.open(repo)
    try:
        message = store.commit_info(store.head("upstream/main")).message
    finally:
        store.close()
    assert "Hive-Glyphs: B" in message


def test_one_operation_at_a_time(setup):
    sync, api, repo, remote_store, _ = setup

    async def both():
        return await asyncio.gather(
            sync.pull(repo, PROJECT, "main"), sync.pull(repo, PROJECT, "main")
        )

    first, second = run(both())
    # The second waited for the first, then found nothing new.
    assert [bool(first["commit"]), bool(second["commit"])].count(True) == 1
