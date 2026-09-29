"""``GitFontraBackend``: a Fontra backend whose storage is a git branch.

It subclasses Fontra's own ``FontraBackend`` and only swaps the file system
for a virtual one over a git commit (:mod:`fontra_hive.vfs`). Everything about
the ``.fontra`` format (glyph JSON, ``glyph-info.csv``, ``kerning.csv``,
``features.txt``, ``font-data.json``, background images) is therefore the
upstream code, unchanged, and the files committed are byte-for-byte what
Fontra writes on disk.

What this class adds:

* a commit scheduler: writes are collected and committed together after a
  short delay (``commit_delay``), or immediately on ``flush()`` / ``aclose()``;
* attribution: each commit is authored by the user the edit came from
  (``set_author`` / the ``current_author`` context variable);
* external change detection: the branch head is polled and, when someone else
  moved it (a merge, a restore, another server), the affected glyphs are
  reloaded and the FontHandler is notified exactly as the file-system backend
  notifies it for external file changes.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Awaitable, Callable

from fontra.backends.filewatcher import Change
from fontra.backends.fontra import FontraBackend, componentNamesFromGlyphData
from fontra.core.glyphdependencies import GlyphDependencies
from fontra.core.protocols import WritableFontBackend

from .gitstore import DEFAULT_BRANCH, GitRepoStore, RefMovedError, Signature
from .vfs import GitPath, GitTree

logger = logging.getLogger(__name__)

# The author of the edits currently being written. The Hive project manager
# sets it per connection before the FontHandler writes to the backend.
current_author: contextvars.ContextVar[Signature | None] = contextvars.ContextVar(
    "fontra_hive_current_author", default=None
)

ANONYMOUS = Signature("Unknown user", "unknown@fontrahive.com")


class GitFontraBackend(FontraBackend):
    """Fontra backend reading and writing a branch of a bare git repository."""

    def __init__(
        self,
        store: GitRepoStore,
        branch: str = DEFAULT_BRANCH,
        *,
        author: Signature | None = None,
        commit_delay: float = 2.0,
        poll_interval: float = 2.0,
        read_only: bool = False,
    ):
        self.store = store
        self.branch = branch
        self.default_author = author
        self.commit_delay = commit_delay
        self.poll_interval = poll_interval
        self.read_only = read_only

        head = store.head(branch)
        if head is None:
            raise FileNotFoundError(f"branch {branch!r} does not exist in {store.path}")
        self.tree = GitTree(store, head, label=f"{store.path.name}@{branch}")
        self.tree.read_only = read_only
        self._commit_handle: asyncio.TimerHandle | None = None
        self._pending_authors: dict[str, Signature] = {}
        self._poll_task: asyncio.Task | None = None
        self._last_commit_by_us: str | None = None

        super().__init__(path=GitPath(self.tree))

    # --- construction helpers -------------------------------------------------

    @classmethod
    def fromPath(cls, path) -> WritableFontBackend:  # type: ignore[override]
        """``path`` is ``<repo dir>`` or ``<repo dir>@<branch>``."""
        path = str(path)
        repo, _, branch = (
            path.rpartition("@") if "@" in path else (path, "", DEFAULT_BRANCH)
        )
        return cls(GitRepoStore.open(repo), branch or DEFAULT_BRANCH)

    @classmethod
    def createFromPath(cls, path) -> WritableFontBackend:  # type: ignore[override]
        raise NotImplementedError(
            "create a project with GitRepoStore.create() and import_directory()"
        )

    @property
    def head(self) -> str | None:
        return self.tree.commit_sha

    # --- attribution ----------------------------------------------------------

    def set_author(self, author: Signature | None) -> None:
        self.default_author = author

    def _author(self) -> Signature:
        return current_author.get() or self.default_author or ANONYMOUS

    # --- commit scheduling ----------------------------------------------------

    def fileWatcherIgnoreNextChange(self, path) -> None:
        # FontraBackend calls this after every file it writes: the natural
        # place to notice that the tree changed and to schedule a commit.
        super().fileWatcherIgnoreNextChange(path)
        if isinstance(path, GitPath):
            self._pending_authors[path.relative] = self._author()
            self._schedule_commit()

    def _schedule_commit(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop: flush() will commit
        if self._commit_handle is not None:
            self._commit_handle.cancel()
        self._commit_handle = loop.call_later(self.commit_delay, self.flush)

    def flush(self) -> None:
        """Write scheduled files (FontraBackend's own scheduler) and commit."""
        super().flush()
        if self._commit_handle is not None:
            self._commit_handle.cancel()
            self._commit_handle = None
        self.commit_pending()

    def commit_pending(self) -> str | None:
        """Commit pending changes now. Returns the new commit sha, or None."""
        if not self.tree.has_pending():
            return None
        changes = self.tree.take_pending()
        authors, self._pending_authors = self._pending_authors, {}

        # One commit per author, so that attribution stays exact even when two
        # users' edits fell into the same window.
        by_author: dict[str, tuple[Signature, dict[str, bytes | None]]] = {}
        for path, data in changes.items():
            author = authors.get(path) or self._author()
            by_author.setdefault(author.email, (author, {}))[1][path] = data

        new_sha = None
        for author, subset in by_author.values():
            message = _commit_message(subset)
            for attempt in range(3):
                try:
                    new_sha = self.store.commit(
                        subset,
                        branch=self.branch,
                        message=message,
                        author=author,
                        expected_head=self.tree.commit_sha,
                    )
                    break
                except RefMovedError as e:
                    # Someone else moved the branch: rebase our pending files on
                    # the new head (file-level: our files win) and try again.
                    logger.info(
                        "branch %s moved (%s), re-applying pending changes",
                        self.branch,
                        e,
                    )
                    self.tree.apply_committed(e.actual, {})
            else:
                raise RuntimeError(
                    f"could not commit to {self.branch!r} after 3 attempts"
                )
            self.tree.apply_committed(new_sha, subset)
            self._last_commit_by_us = new_sha
        return new_sha

    async def aclose(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await super().aclose()

    # --- glyph dependencies (in-process; the tree is not a real directory) ------

    async def _computeGlyphDependencies(self) -> GlyphDependencies:
        import json

        dependencies = GlyphDependencies()
        for glyphPath in self.glyphsDir.glob("*.json"):
            try:
                glyphData = json.loads(glyphPath.read_text())
            except json.JSONDecodeError as e:
                logger.error(
                    "error while extracting component info from %s: %r", glyphPath, e
                )
                continue
            from fontra.backends.filenames import fileNameToString

            dependencies.update(
                fileNameToString(glyphPath.stem), componentNamesFromGlyphData(glyphData)
            )
        return dependencies

    @property
    def glyphDependencies(self):  # type: ignore[override]
        async def get():
            if self._glyphDependencies is None:
                self._glyphDependencies = await self._computeGlyphDependencies()
            return self._glyphDependencies

        return get()

    def _updateGlyphDependencies(self, changes) -> None:
        """Keep "glyphs using this glyph" true after a change made elsewhere
        (a Restore, an import, another server): FontraBackend only updates it
        for edits made through putGlyph()/deleteGlyph(). Only the glyph files
        that changed are read again; not built yet = nothing to do."""
        import json

        from fontra.backends.filenames import fileNameToString

        if self._glyphDependencies is None:
            return
        glyphsDir = str(self.glyphsDir)
        for _, path in changes:
            if not (path.startswith(glyphsDir + "/") and path.endswith(".json")):
                continue
            glyphPath = GitPath(self.tree, path[len(str(self.path)) + 1 :])
            glyphName = fileNameToString(glyphPath.stem)
            try:
                glyphData = json.loads(glyphPath.read_text())
            except FileNotFoundError:
                componentNames = ()  # deleted
            except json.JSONDecodeError as e:
                logger.error("error while reading components of %s: %r", path, e)
                continue
            else:
                componentNames = componentNamesFromGlyphData(glyphData)
            self._glyphDependencies.update(glyphName, componentNames)

    async def findGlyphsThatUseGlyph(self, glyphName):
        return sorted((await self.glyphDependencies).usedBy.get(glyphName, []))

    def startOptionalBackgroundTasks(self) -> None:
        pass

    # --- external changes: poll the branch head ------------------------------------

    async def watchExternalChanges(
        self, callback: Callable[[Any], Awaitable[None]]
    ) -> None:
        self.fileWatcherCallbacks.append(callback)
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def fileWatcherClose(self) -> None:
        pass

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.check_external_changes()
            except Exception:  # pragma: no cover - keep polling whatever happens
                logger.exception("error while checking the branch head")

    async def check_external_changes(self) -> dict[str, Any] | None:
        """Compare the branch head with what we hold; reload what changed and
        notify the FontHandler. Returns the reload pattern (None = reload all)
        or an empty dict when nothing changed."""
        head = self.store.head(self.branch)
        if head == self.tree.commit_sha or head is None:
            return {}
        old = self.tree.commit_sha
        changes = {
            (
                Change.deleted if c.kind == "delete" else Change.modified,
                str(self.path / c.path),
            )
            for c in self.store.diff(old, head)
        }
        # Pending (uncommitted) edits of ours are kept on top of the new head.
        pending = self.tree.pending
        self.tree.reset(head)
        self.tree.pending = pending
        self._updateGlyphDependencies(changes)
        reloadPattern = await self.fileWatcherProcessChanges(changes)
        if reloadPattern or reloadPattern is None:
            await self.fileWatcherNotifyCallbacks(reloadPattern)
        return reloadPattern


def _commit_message(changes: dict[str, bytes | None]) -> str:
    from fontra.backends.filenames import fileNameToString

    glyphs = sorted(
        fileNameToString(p[len("glyphs/") : -len(".json")])
        for p in changes
        if p.startswith("glyphs/") and p.endswith(".json")
    )
    others = sorted(p for p in changes if not p.startswith("glyphs/"))
    parts = []
    if glyphs:
        shown = ", ".join(glyphs[:8]) + (
            f" (+{len(glyphs) - 8})" if len(glyphs) > 8 else ""
        )
        parts.append(f"Edit {shown}")
    if others:
        parts.append("Update " + ", ".join(others))
    title = "; ".join(parts) or "Update"
    trailer = f"\n\nHive-Glyphs: {' '.join(glyphs)}" if glyphs else ""
    return title + trailer + "\n"
