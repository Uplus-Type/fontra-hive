"""The Fontra server's side of remote git repositories: pull, push and
status for a project, with the credentials hive-api hands out.

:mod:`fontra_hive.remote` does the git work and knows nothing of accounts;
this module asks hive-api (through an ``api`` object, see
:class:`RemoteApi`) for the project's remote and a short-lived token, runs
the git work in a thread, one operation at a time per repository, and
reports to hive-api which remote commit the project now matches.

Kept free of aiohttp so that it can be tested on its own; the HTTP routes
are in :mod:`fontra_hive.hivemanager`.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Callable, Protocol

from . import remote as git_remote
from .gitstore import SERVER_SIGNATURE, GitRepoStore, Signature, _trailer

logger = logging.getLogger(__name__)

DEFAULT_MESSAGE = "Update from Fontra Hive"
PENDING_LIMIT = 50  # paths listed in a status (the count is always given)


class RemoteNotUsable(Exception):
    """hive-api knows the project's remote cannot be used; the message says why."""


class NoRemote(Exception):
    """The project has no remote repository."""


class RemoteApi(Protocol):
    async def remote(self, project: str) -> dict | None:
        raise NotImplementedError

    async def remoteSynced(self, project: str, sha: str) -> None:
        raise NotImplementedError

    async def remotesMoved(self) -> list[dict]:
        raise NotImplementedError

    async def project(self, project: str) -> dict | None:
        raise NotImplementedError


def remote_from(info: dict) -> git_remote.Remote:
    """A :class:`fontra_hive.remote.Remote` from hive-api's answer."""
    return git_remote.Remote(
        info["url"],
        branch=info.get("branch") or "main",
        path=info.get("path") or "",
        username=info.get("username"),
        password=info.get("password"),
    )


class RemoteSync:
    def __init__(
        self,
        api: RemoteApi,
        *,
        allowLocal: bool = False,
        beforeWrite: Callable[[pathlib.Path, str], None] | None = None,
        afterWrite: Callable[[pathlib.Path, str], object] | None = None,
    ):
        """``beforeWrite(repoPath, branch)``: flush pending edits of a branch
        about to be read (push); ``afterWrite(repoPath, branch)``: tell an
        open branch it changed under it (pull). Both optional."""
        self.api = api
        self.allowLocal = allowLocal
        self.beforeWrite = beforeWrite
        self.afterWrite = afterWrite
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, repoPath: pathlib.Path) -> asyncio.Lock:
        return self._locks.setdefault(str(repoPath), asyncio.Lock())

    async def _remote(self, project: str) -> tuple[git_remote.Remote, dict]:
        info = await self.api.remote(project)
        if info is None:
            raise NoRemote(f"{project} has no remote repository.")
        return remote_from(info), info

    # --- status ----------------------------------------------------------------

    async def status(self, repoPath: pathlib.Path, project: str, branch: str) -> dict:
        remote, info = await self._remote(project)

        def work():
            store = GitRepoStore.open(repoPath)
            try:
                return git_remote.status(
                    store, remote, branch, allow_local=self.allowLocal
                )
            finally:
                store.close()

        state = await asyncio.to_thread(work)
        return {
            "provider": info.get("provider"),
            "repository": info.get("repository"),
            "url": info.get("url"),
            "branch": remote.branch,
            "path": remote.path,
            "format": remote.format,
            "upstreamBranch": remote.upstream_branch,
            "remoteHead": state.remote_sha,
            "syncedHead": state.synced_sha,
            "remoteMoved": state.remote_moved,
            "unmerged": state.unmerged,
            "pending": state.pending[:PENDING_LIMIT],
            "pendingCount": len(state.pending),
            "canPush": state.can_push,
        }

    # --- pull --------------------------------------------------------------------

    async def pull(
        self,
        repoPath: pathlib.Path,
        project: str,
        baseBranch: str,
        author: Signature = SERVER_SIGNATURE,
    ) -> dict:
        remote, _ = await self._remote(project)
        async with self._lock(repoPath):

            def work():
                store = GitRepoStore.open(repoPath)
                try:
                    result = git_remote.pull(
                        store,
                        remote,
                        base_branch=baseBranch,
                        author=author,
                        allow_local=self.allowLocal,
                    )
                    adopted = False
                    if result.commit and is_fresh_project(store, baseBranch):
                        # A project made to hold this repository: nothing
                        # to review, the repository's font becomes its own.
                        store.fast_forward(baseBranch, result.branch)
                        adopted = True
                    return result, adopted
                finally:
                    store.close()

            result, adopted = await asyncio.to_thread(work)
            if result.commit and self.afterWrite is not None:
                await _maybe_await(self.afterWrite(repoPath, result.branch))
                if adopted:
                    await _maybe_await(self.afterWrite(repoPath, baseBranch))
        if result.remote_sha:
            await self.api.remoteSynced(project, result.remote_sha)
        return {
            "branch": result.branch,
            "remoteHead": result.remote_sha,
            "commit": result.commit,
            "glyphs": list(result.glyphs),
            "merged": baseBranch if adopted else None,
        }

    # --- push --------------------------------------------------------------------

    async def push(
        self,
        repoPath: pathlib.Path,
        project: str,
        branch: str,
        author: Signature,
        message: str | None = None,
    ) -> dict:
        remote, _ = await self._remote(project)
        async with self._lock(repoPath):
            if self.beforeWrite is not None:
                await _maybe_await(self.beforeWrite(repoPath, branch))

            def work():
                store = GitRepoStore.open(repoPath)
                try:
                    text = message or default_message(store, remote, branch)
                    return git_remote.push(
                        store,
                        remote,
                        branch=branch,
                        message=text,
                        author=author,
                        co_authors=co_authors(store, remote, branch, author),
                        allow_local=self.allowLocal,
                    )
                finally:
                    store.close()

            result = await asyncio.to_thread(work)
            if result.commit and self.afterWrite is not None:
                await _maybe_await(self.afterWrite(repoPath, remote.upstream_branch))
        if result.pushed and result.remote_sha:
            await self.api.remoteSynced(project, result.remote_sha)
        return {
            "pushed": result.pushed,
            "remoteHead": result.remote_sha,
            "previous": result.previous,
            "files": list(result.files),
            "glyphs": list(result.glyphs),
            "skipped": list(result.skipped),
        }

    # --- webhooks ----------------------------------------------------------------

    async def pullMoved(
        self, repoPathFor: Callable[[str], pathlib.Path | None]
    ) -> list[str]:
        """Pull every remote GitHub says moved (``repoPathFor(repo)``: the
        repository's folder, None when it is not here). The projects pulled."""
        pulled = []
        for entry in await self.api.remotesMoved():
            project, repo = entry.get("project"), entry.get("repo")
            repoPath = repoPathFor(repo) if repo else None
            if not project or repoPath is None or not repoPath.is_dir():
                continue
            try:
                info = await self.api.project(project) or {}
                await self.pull(repoPath, project, info.get("defaultBranch") or "main")
                pulled.append(project)
            except (git_remote.RemoteError, RemoteNotUsable, NoRemote) as error:
                logger.warning(
                    "pull of %s after a push on its remote: %s", project, error
                )
        return pulled


NEW_PROJECT_MESSAGE = "New project"


def is_fresh_project(store: GitRepoStore, branch: str) -> bool:
    """The branch is still the empty font a project starts with (one commit,
    made by the server when the project was opened for the first time)."""
    head = store.head(branch)
    if head is None:
        return False
    info = store.commit_info(head)
    return not info.parents and info.message.startswith(NEW_PROJECT_MESSAGE)


async def _maybe_await(value):
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        await value


def _since_last_sync(store: GitRepoStore, remote: git_remote.Remote, branch: str):
    """The commits of ``branch`` not yet sent: after the last sync, first
    parent line; all of them on a first push."""
    state = git_remote.upstream_state(store, remote)
    head = store.head(branch)
    if head is None:
        return []
    base = None
    if state.head is not None:
        # After a push: the Hive commit it sent; after a pull: where the
        # branch and upstream/… last met.
        pushed = _trailer(
            store.repo[state.head.encode()].message, git_remote.HIVE_PUSH_TRAILER
        )
        if pushed and store.is_ancestor(pushed, head):
            base = pushed
        else:
            base = store.merge_base(state.head, head)
    try:
        return store.commits_between(base, head)
    except ValueError:
        return store.commits_between(None, head)[:500]


def default_message(store: GitRepoStore, remote: git_remote.Remote, branch: str) -> str:
    """The title of the latest snapshot made since the last sync, if any."""
    snapshot = store.latest_snapshot(branch)
    if snapshot is None:
        return DEFAULT_MESSAGE
    since = {c.sha for c in _since_last_sync(store, remote, branch)}
    return snapshot.title if snapshot.sha in since else DEFAULT_MESSAGE


def co_authors(
    store: GitRepoStore, remote: git_remote.Remote, branch: str, author: Signature
) -> list[Signature]:
    """Everyone whose commits are in the push, oldest first, the server and
    the pusher left out."""
    seen = {SERVER_SIGNATURE.email.lower(), author.email.lower()}
    result = []
    for commit in reversed(_since_last_sync(store, remote, branch)):
        email = commit.email.lower()
        if commit.email and email not in seen:
            seen.add(email)
            result.append(Signature(commit.author, commit.email))
    return result


__all__ = [
    "NoRemote",
    "RemoteApi",
    "RemoteNotUsable",
    "RemoteSync",
    "co_authors",
    "default_message",
    "remote_from",
]
