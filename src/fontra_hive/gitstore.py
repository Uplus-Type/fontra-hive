"""Git storage for Fontra Hive projects.

One *bare* git repository per project. The tree of every commit is a complete
``.fontra`` package (``font-data.json``, ``glyph-info.csv``, ``kerning.csv``,
``features.txt``, ``glyphs/*.json``, ``background-images/*``), so a plain
``git clone`` of a project gives a folder Fontra can open as is.

This module talks to git objects directly (blobs, trees, commits, refs) and
never uses a working tree nor shells out to the ``git`` command. It is written
against dulwich (pure Python); the API is deliberately small so that a pygit2
implementation can replace it without touching the backend.

Concurrency model: a commit is built from a known parent and the branch
reference is updated with compare-and-swap. If the reference moved in the
meantime, :class:`RefMovedError` is raised and the caller re-applies its
changes on top of the new head.
"""

from __future__ import annotations

import os
import pathlib
import re
import stat
import time
import unicodedata
from dataclasses import dataclass, replace
from typing import Iterable, Iterator, Mapping

from dulwich import diff_tree
from dulwich.index import commit_tree
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit, Tag
from dulwich.repo import Repo

DEFAULT_BRANCH = "main"
FILE_MODE = 0o100644
DIR_MODE = stat.S_IFDIR


class RefMovedError(Exception):
    """The branch moved while a commit was being prepared (compare-and-swap failed)."""

    def __init__(self, branch: str, expected: str | None, actual: str | None):
        super().__init__(
            f"branch {branch!r} moved: expected {expected}, found {actual}"
        )
        self.branch = branch
        self.expected = expected
        self.actual = actual


class Signature:
    """A git author or committer."""

    __slots__ = ("name", "email")

    def __init__(self, name: str, email: str):
        self.name = name
        self.email = email

    def encode(self) -> bytes:
        return f"{self.name} <{self.email}>".encode("utf-8")

    def __repr__(self) -> str:
        return f"Signature({self.name!r}, {self.email!r})"


SERVER_SIGNATURE = Signature("Fontra Hive", "hive@fontrahive.com")


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author: str
    email: str
    time: int
    message: str
    parents: tuple[str, ...]
    # Name of the snapshot this commit belongs to (the first snapshot made
    # after it on the branch), or None for changes since the last snapshot.
    # Only filled in by ``log(..., snapshots=...)``.
    snapshot: str | None = None
    # The same for the glyph snapshots of the glyph asked for (the first one
    # made after it), with ``log(..., glyph_snapshots=...)``.
    glyph_snapshot: str | None = None


@dataclass(frozen=True)
class SnapshotInfo:
    """A named point of a branch: an empty commit (same tree as its parent)
    whose message carries a ``Hive-Snapshot:`` trailer, plus an annotated tag
    ``snapshot/<name>`` pointing at it. Nothing is rewritten: the commits
    since the previous snapshot stay in the history, the snapshot groups them.
    """

    name: str  # tag-safe slug, unique in the repository
    title: str  # as typed by the user
    sha: str
    author: str
    time: int
    base: str | None  # sha of the previous snapshot on the branch, if any
    changes: int  # commits grouped by this snapshot (since ``base``)
    glyphs: tuple[str, ...]  # glyphs changed by those commits, sorted


@dataclass(frozen=True)
class GlyphSnapshotInfo:
    """A named version of one or more glyphs (not of the whole font): an
    annotated tag ``glyph-snapshot/<name>`` on the commit that was the
    branch head, whose message lists the glyphs. No commit is added. In a
    glyph's history, it groups that glyph's versions made since its previous
    glyph snapshot."""

    name: str  # tag-safe slug, unique in the repository
    title: str
    sha: str  # the commit it names
    author: str
    time: int
    glyphs: tuple[str, ...]


@dataclass(frozen=True)
class TreeChange:
    kind: str  # "add" | "modify" | "delete"
    path: str


def _b(s: str) -> bytes:
    return s.encode("utf-8")


def _s(b: bytes) -> str:
    return b.decode("utf-8")


def _branch_ref(branch: str) -> bytes:
    return _b(f"refs/heads/{branch}")


def _tag_ref(tag: str) -> bytes:
    return _b(f"refs/tags/{tag}")


SNAPSHOT_TAG_PREFIX = "snapshot/"
GLYPH_SNAPSHOT_TAG_PREFIX = "glyph-snapshot/"


def snapshot_slug(title: str) -> str:
    """A tag-safe name for a snapshot title: ASCII letters, digits, ``.``,
    ``_`` and ``-`` (``"Relecture client n°1"`` -> ``"relecture-client-n1"``)."""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    text = re.sub(r"\.{2,}", ".", text)
    if text.endswith(".lock"):
        text = text[: -len(".lock")]
    return text[:64].strip("-.")


class GitRepoStore:
    """A bare git repository holding one Fontra Hive project."""

    def __init__(self, repo: Repo):
        self.repo = repo

    # --- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: os.PathLike | str) -> "GitRepoStore":
        return cls(Repo(os.fspath(path)))

    @classmethod
    def create(cls, path: os.PathLike | str) -> "GitRepoStore":
        path = pathlib.Path(path)
        path.mkdir(parents=True, exist_ok=False)
        repo = Repo.init_bare(os.fspath(path))
        repo.refs.set_symbolic_ref(b"HEAD", _branch_ref(DEFAULT_BRANCH))
        return cls(repo)

    @property
    def path(self) -> pathlib.Path:
        return pathlib.Path(self.repo.path)

    def close(self) -> None:
        self.repo.close()

    # --- references --------------------------------------------------------

    def head(self, branch: str = DEFAULT_BRANCH) -> str | None:
        sha = self.repo.refs.read_ref(_branch_ref(branch))
        return _s(sha) if sha else None

    def branches(self) -> list[str]:
        prefix = b"refs/heads/"
        return sorted(
            _s(ref[len(prefix) :])
            for ref in self.repo.refs.keys()
            if ref.startswith(prefix)
        )

    def create_branch(self, name: str, from_ref: str = DEFAULT_BRANCH) -> str:
        sha = self.resolve(from_ref)
        ok = self.repo.refs.add_if_new(_branch_ref(name), _b(sha))
        if not ok:
            raise ValueError(f"branch {name!r} already exists")
        return sha

    def delete_branch(self, name: str) -> None:
        if name == DEFAULT_BRANCH:
            raise ValueError("the default branch cannot be deleted")
        del self.repo.refs[_branch_ref(name)]

    def tags(self) -> list[str]:
        prefix = b"refs/tags/"
        return sorted(
            _s(ref[len(prefix) :])
            for ref in self.repo.refs.keys()
            if ref.startswith(prefix)
        )

    def create_tag(
        self,
        name: str,
        ref: str = DEFAULT_BRANCH,
        message: str = "",
        tagger: Signature = SERVER_SIGNATURE,
    ) -> str:
        sha = self.resolve(ref)
        tag = Tag()
        tag.name = _b(name)
        tag.object = (Commit, _b(sha))
        tag.tagger = tagger.encode()
        tag.tag_time = int(time.time())
        tag.tag_timezone = 0
        tag.message = _b(message or name)
        self.repo.object_store.add_object(tag)
        ok = self.repo.refs.add_if_new(_tag_ref(name), tag.id)
        if not ok:
            raise ValueError(f"tag {name!r} already exists")
        return _s(tag.id)

    def resolve(self, ref: str) -> str:
        """Resolve a branch name, tag name or commit sha to a commit sha."""
        if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref):
            return ref
        for candidate in (_branch_ref(ref), _tag_ref(ref)):
            sha = self.repo.refs.read_ref(candidate)
            if sha:
                obj = self.repo[sha]
                if isinstance(obj, Tag):
                    obj = self.repo[obj.object[1]]
                return _s(obj.id)
        raise KeyError(f"unknown ref {ref!r}")

    # --- reading -----------------------------------------------------------

    def tree_sha(self, commit_sha: str) -> str:
        return _s(self.repo[_b(commit_sha)].tree)

    def list_tree(self, commit_sha: str) -> dict[str, str]:
        """Return a flat mapping ``path -> blob sha`` for the whole tree of a commit."""
        result: dict[str, str] = {}
        store = self.repo.object_store
        tree = self.repo[self.repo[_b(commit_sha)].tree]
        for entry in iter_tree_contents(store, tree.id):
            result[_s(entry.path)] = _s(entry.sha)
        return result

    def read_blob(self, blob_sha: str) -> bytes:
        return self.repo[_b(blob_sha)].data

    def read_file(self, commit_sha: str, path: str) -> bytes:
        tree = self.repo[self.repo[_b(commit_sha)].tree]
        _mode, sha = tree.lookup_path(self.repo.object_store.__getitem__, _b(path))
        return self.repo[sha].data

    def commit_info(self, commit_sha: str) -> CommitInfo:
        c = self.repo[_b(commit_sha)]
        author = _s(c.author)
        name, _, email = author.partition(" <")
        return CommitInfo(
            sha=_s(c.id),
            author=name,
            email=email.rstrip(">"),
            time=c.author_time,
            message=_s(c.message).rstrip("\n"),
            parents=tuple(_s(p) for p in c.parents),
        )

    # --- writing -----------------------------------------------------------

    def commit(
        self,
        changes: Mapping[str, bytes | None],
        *,
        branch: str = DEFAULT_BRANCH,
        message: str,
        author: Signature,
        committer: Signature = SERVER_SIGNATURE,
        expected_head: str | None | object = ...,
        timestamp: int | None = None,
    ) -> str:
        """Apply ``changes`` (``path -> bytes`` to write, ``path -> None`` to
        delete) on top of the branch head and move the branch to the new commit.

        ``expected_head`` is the parent the caller built its changes against
        (``None`` for a new, empty branch). When given and the branch has moved,
        nothing is written to the reference and :class:`RefMovedError` is raised.
        """
        ref = _branch_ref(branch)
        current = self.head(branch)
        if expected_head is not ... and expected_head != current:
            raise RefMovedError(branch, expected_head, current)

        entries: dict[str, str] = self.list_tree(current) if current else {}
        store = self.repo.object_store

        for path, data in changes.items():
            path = _normalize_path(path)
            if data is None:
                entries.pop(path, None)
            else:
                blob = Blob.from_string(data)
                store.add_object(blob)
                entries[path] = _s(blob.id)

        tree_id = commit_tree(
            store, ((_b(p), _b(sha), FILE_MODE) for p, sha in sorted(entries.items()))
        )

        now = int(time.time()) if timestamp is None else timestamp
        c = Commit()
        c.tree = tree_id
        c.parents = [_b(current)] if current else []
        c.author = author.encode()
        c.committer = committer.encode()
        c.author_time = c.commit_time = now
        c.author_timezone = c.commit_timezone = 0
        c.encoding = b"UTF-8"
        c.message = _b(message if message.endswith("\n") else message + "\n")
        store.add_object(c)

        old = _b(current) if current else None
        if not self.repo.refs.set_if_equals(ref, old, c.id):
            raise RefMovedError(branch, current, self.head(branch))
        return _s(c.id)

    def import_directory(
        self,
        source: os.PathLike | str,
        *,
        branch: str = DEFAULT_BRANCH,
        message: str = "Import",
        author: Signature = SERVER_SIGNATURE,
    ) -> str:
        """Commit every file of a directory (a ``.fontra`` package) as the tree
        of a new commit on ``branch``. Files already in the branch and absent
        from the directory are removed."""
        source = pathlib.Path(source)
        changes: dict[str, bytes | None] = {}
        current = self.head(branch)
        if current:
            for path in self.list_tree(current):
                changes[path] = None
        for file in sorted(source.rglob("*")):
            if file.is_file() and not _is_ignored(file.relative_to(source)):
                changes[file.relative_to(source).as_posix()] = file.read_bytes()
        return self.commit(changes, branch=branch, message=message, author=author)

    def export(self, ref: str, destination: os.PathLike | str) -> None:
        """Write the full tree of ``ref`` to ``destination`` (a ``.fontra`` folder)."""
        destination = pathlib.Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        sha = self.resolve(ref)
        for path, blob_sha in self.list_tree(sha).items():
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.read_blob(blob_sha))

    # --- history -----------------------------------------------------------

    def log(
        self,
        ref: str = DEFAULT_BRANCH,
        *,
        path: str | None = None,
        glyph: str | None = None,
        limit: int | None = None,
        snapshots: list[SnapshotInfo] | None = None,
        glyph_snapshots: list[GlyphSnapshotInfo] | None = None,
        order: list[tuple[str, str]] | None = None,
    ) -> list[CommitInfo]:
        """Commits reachable from ``ref``, newest first, optionally only those
        that changed ``path`` (a file, or a directory: its subtree).

        ``glyph`` is the glyph name stored at ``path``: commits written by
        Fontra Hive carry a ``Hive-Glyphs:`` trailer listing the glyphs they
        changed, which answers the question without reading any tree.

        When ``snapshots`` is a list, the walk follows first parents only,
        every returned commit gets the name of the snapshot it belongs to
        (``CommitInfo.snapshot``) and the snapshots met on the way are
        appended to the list, newest first. Snapshot commits themselves are
        not returned in path mode (they change nothing).

        ``glyph_snapshots``: the glyph snapshots of ``glyph`` (from
        :meth:`glyph_snapshots`); those met on the way are kept in the list
        (the others removed), newest first, and each commit gets the one it
        belongs to (``CommitInfo.glyph_snapshot``). ``order``, when a list,
        receives the walk as ``(kind, key)`` pairs, newest first: ``("commit",
        sha)``, ``("snapshot", name)``, ``("glyph-snapshot", name)``.
        """
        sha = self.resolve(ref)
        if path is None and snapshots is None and glyph_snapshots is None:
            walker = self.repo.get_walker(include=[_b(sha)], max_entries=limit)
            return [self.commit_info(_s(entry.commit.id)) for entry in walker]
        return self._log_first_parent(
            sha,
            _normalize_path(path) if path is not None else None,
            glyph,
            limit,
            snapshots,
            glyph_snapshots,
            order,
        )

    def _log_first_parent(
        self,
        sha: str,
        path: str | None,
        glyph: str | None,
        limit: int | None,
        snapshots: list[SnapshotInfo] | None,
        glyph_snapshots: list[GlyphSnapshotInfo] | None = None,
        order: list[tuple[str, str]] | None = None,
    ) -> list[CommitInfo]:
        """History along the first-parent line, of one path (or of everything
        when ``path`` is None), optionally annotated with snapshots.

        For each commit, the ``Hive-Glyphs`` trailer decides when it is
        present and ``glyph`` is given (tiny commit objects only). Otherwise
        the object at ``path`` (a blob, or a subtree) is compared with the one
        in the first parent: two tree lookups, no full tree diff, which keeps
        this usable on repositories with tens of thousands of glyph files.
        """
        store = self.repo.object_store
        parts = _b(path) if path is not None else b""
        cache: dict[bytes, bytes | None] = {}

        def object_at(commit_id: bytes) -> bytes | None:
            if commit_id not in cache:
                tree = store[store[commit_id].tree]
                try:
                    _mode, obj = tree.lookup_path(store.__getitem__, parts)
                except KeyError:
                    obj = None
                cache[commit_id] = obj
            return cache[commit_id]

        result: list[CommitInfo] = []
        current_snapshot: str | None = None
        current_glyph_snapshot: str | None = None
        by_target: dict[bytes, list[GlyphSnapshotInfo]] = {}
        for gs in glyph_snapshots or ():
            by_target.setdefault(_b(gs.sha), []).append(gs)
        met: list[GlyphSnapshotInfo] = []
        commit_id: bytes | None = _b(sha)
        while commit_id is not None and (limit is None or len(result) < limit):
            commit = store[commit_id]
            parent = commit.parents[0] if commit.parents else None
            # A glyph snapshot names the state at this commit: it comes
            # before it (newest first), and groups it.
            for gs in sorted(by_target.get(commit_id, ()), key=lambda g: -g.time):
                met.append(gs)
                current_glyph_snapshot = gs.name
                if order is not None:
                    order.append(("glyph-snapshot", gs.name))
            if snapshots is not None:
                snapshot_name = _trailer(commit.message, HIVE_SNAPSHOT_TRAILER)
                if snapshot_name is not None:
                    snapshots.append(self._snapshot_info(commit))
                    current_snapshot = snapshot_name
                    if order is not None:
                        order.append(("snapshot", snapshot_name))
                    if path is not None:
                        commit_id = parent
                        continue
            if path is None:
                touched = True
            else:
                glyphs = (
                    _hive_glyphs_trailer(commit.message) if glyph is not None else None
                )
                if glyphs is not None:
                    touched = glyph in glyphs
                else:
                    touched = object_at(commit_id) != (
                        object_at(parent) if parent else None
                    )
            if touched:
                info = self.commit_info(_s(commit_id))
                if snapshots is not None:
                    info = replace(info, snapshot=current_snapshot)
                if glyph_snapshots is not None:
                    info = replace(info, glyph_snapshot=current_glyph_snapshot)
                result.append(info)
                if order is not None:
                    order.append(("commit", info.sha))
            commit_id = parent
        if glyph_snapshots is not None:
            glyph_snapshots[:] = met
        return result

    # --- snapshots ---------------------------------------------------------

    def snapshots(self, ref: str = DEFAULT_BRANCH) -> list[SnapshotInfo]:
        """Snapshots on the first-parent line of ``ref``, newest first.

        Only commit objects are read (no trees): the same cost as the
        per-glyph history.
        """
        store = self.repo.object_store
        result = []
        commit_id: bytes | None = _b(self.resolve(ref))
        while commit_id is not None:
            commit = store[commit_id]
            if _trailer(commit.message, HIVE_SNAPSHOT_TRAILER) is not None:
                result.append(self._snapshot_info(commit))
            commit_id = commit.parents[0] if commit.parents else None
        return result

    def latest_snapshot(self, ref: str = DEFAULT_BRANCH) -> SnapshotInfo | None:
        """The newest snapshot on the first-parent line of ``ref``."""
        store = self.repo.object_store
        commit_id: bytes | None = _b(self.resolve(ref))
        while commit_id is not None:
            commit = store[commit_id]
            if _trailer(commit.message, HIVE_SNAPSHOT_TRAILER) is not None:
                return self._snapshot_info(commit)
            commit_id = commit.parents[0] if commit.parents else None
        return None

    def commits_between(self, base: str | None, head: str) -> list[CommitInfo]:
        """First-parent commits after ``base`` up to ``head`` included, newest
        first (all of them down to the root when ``base`` is None)."""
        store = self.repo.object_store
        stop = _b(self.resolve(base)) if base else None
        result = []
        commit_id: bytes | None = _b(self.resolve(head))
        while commit_id is not None and commit_id != stop:
            commit = store[commit_id]
            result.append(self.commit_info(_s(commit_id)))
            commit_id = commit.parents[0] if commit.parents else None
        if stop is not None and commit_id != stop:
            raise ValueError(f"{base} is not on the first-parent line of {head}")
        return result

    def create_snapshot(
        self,
        title: str,
        *,
        branch: str = DEFAULT_BRANCH,
        author: Signature,
        expected_head: str | None | object = ...,
        timestamp: int | None = None,
    ) -> SnapshotInfo:
        """Name the current state of ``branch``, grouping the commits made
        since the previous snapshot.

        Adds an empty commit (same tree as the head) whose message summarises
        the grouped commits (count, authors, glyphs) and carries the
        ``Hive-Snapshot:`` trailer, then an annotated tag
        ``snapshot/<name>`` on it. History is not rewritten.

        Raises ``ValueError`` for an empty or already used name, or when
        nothing was committed since the previous snapshot;
        :class:`RefMovedError` when the branch moved (compare-and-swap).
        """
        title = " ".join(title.split())
        name = snapshot_slug(title)
        if not name:
            raise ValueError("a snapshot needs a name")
        if self.repo.refs.read_ref(_tag_ref(SNAPSHOT_TAG_PREFIX + name)):
            raise ValueError(f"snapshot {name!r} already exists")
        head = self.head(branch)
        if head is None:
            raise KeyError(f"no branch {branch!r}")
        if expected_head is not ... and expected_head != head:
            raise RefMovedError(branch, expected_head, head)
        previous = self.latest_snapshot(head)
        grouped = self.commits_between(previous.sha if previous else None, head)
        if not grouped:
            raise ValueError(
                f"nothing changed since snapshot {previous.name!r}"
                if previous
                else "nothing to snapshot"
            )

        store = self.repo.object_store
        glyphs: set[str] = set()
        authors: list[str] = []
        unknown = 0
        for info in reversed(grouped):  # authors in order of first change
            if info.author not in authors:
                authors.append(info.author)
            listed = _hive_glyphs_trailer(_b(info.message))
            if listed is None:
                unknown += 1
            else:
                glyphs |= listed
        count = len(grouped)
        since = f"snapshot {previous.title!r}" if previous else "the beginning"
        body = [
            f"{count} change{'s' if count != 1 else ''} since {since}, "
            f"by {', '.join(authors)}."
        ]
        if glyphs:
            shown = sorted(glyphs)
            more = f" (+{len(shown) - 40})" if len(shown) > 40 else ""
            body.append(f"Glyphs: {' '.join(shown[:40])}{more}")
        if unknown:
            body.append(f"{unknown} commit(s) without a glyph list (import, external).")
        message = (
            f"Snapshot: {title}\n\n"
            + "\n".join(body)
            + "\n\n"
            + f"Hive-Snapshot: {name}\n"
            + f"Hive-Snapshot-Base: {previous.sha if previous else 'none'}\n"
            + f"Hive-Snapshot-Changes: {count}\n"
            + f"Hive-Snapshot-Glyphs: {' '.join(sorted(glyphs))}\n"
            # An empty glyph list: the snapshot changes no glyph, and the
            # per-glyph history skips it without reading any tree.
            + "Hive-Glyphs:\n"
        )

        now = int(time.time()) if timestamp is None else timestamp
        c = Commit()
        c.tree = store[_b(head)].tree
        c.parents = [_b(head)]
        c.author = author.encode()
        c.committer = SERVER_SIGNATURE.encode()
        c.author_time = c.commit_time = now
        c.author_timezone = c.commit_timezone = 0
        c.encoding = b"UTF-8"
        c.message = _b(message)
        store.add_object(c)
        if not self.repo.refs.set_if_equals(_branch_ref(branch), _b(head), c.id):
            raise RefMovedError(branch, head, self.head(branch))
        self.create_tag(
            SNAPSHOT_TAG_PREFIX + name, _s(c.id), message=title, tagger=author
        )
        return self._snapshot_info(c)

    # --- glyph snapshots ---------------------------------------------------

    def glyph_snapshots(self, glyph: str | None = None) -> list[GlyphSnapshotInfo]:
        """All glyph snapshots (of ``glyph`` only, if given), newest first.
        Reads the ``glyph-snapshot/*`` tags only (small objects)."""
        prefix = _b("refs/tags/" + GLYPH_SNAPSHOT_TAG_PREFIX)
        result = []
        for ref in self.repo.refs.keys():
            if not ref.startswith(prefix):
                continue
            tag = self.repo[self.repo.refs[ref]]
            if not isinstance(tag, Tag):
                continue
            glyphs = tuple(
                (
                    _trailer(tag.message, HIVE_GLYPH_SNAPSHOT_GLYPHS_TRAILER) or ""
                ).split()
            )
            if glyph is not None and glyph not in glyphs:
                continue
            tagger = _s(tag.tagger)
            result.append(
                GlyphSnapshotInfo(
                    name=_s(ref[len(prefix) :]),
                    title=_s(tag.message).split("\n", 1)[0],
                    sha=_s(tag.object[1]),
                    author=tagger.split(" <", 1)[0],
                    time=tag.tag_time,
                    glyphs=glyphs,
                )
            )
        result.sort(key=lambda g: (-g.time, g.name))
        return result

    def create_glyph_snapshot(
        self,
        title: str,
        glyphs: Iterable[str],
        *,
        ref: str = DEFAULT_BRANCH,
        author: Signature,
        timestamp: int | None = None,
    ) -> GlyphSnapshotInfo:
        """Name the current version of ``glyphs``: an annotated tag
        ``glyph-snapshot/<name>`` on ``ref`` (the branch head), no commit.
        The name is made unique (``-2``, ``-3``…): the same title can name
        versions of different glyphs. Whether the glyphs changed since their
        previous glyph snapshot is for the caller to check (it knows their
        paths)."""
        title = " ".join(title.split())
        glyphs = sorted(set(glyphs))
        base = snapshot_slug(title)
        if not base:
            raise ValueError("a snapshot needs a name")
        if not glyphs or any(not g or any(c.isspace() for c in g) for g in glyphs):
            raise ValueError("unexpected glyph names")
        sha = self.resolve(ref)
        name, n = base, 1
        while self.repo.refs.read_ref(_tag_ref(GLYPH_SNAPSHOT_TAG_PREFIX + name)):
            n += 1
            name = f"{base}-{n}"
        tag = Tag()
        tag.name = _b(GLYPH_SNAPSHOT_TAG_PREFIX + name)
        tag.object = (Commit, _b(sha))
        tag.tagger = author.encode()
        tag.tag_time = int(time.time()) if timestamp is None else timestamp
        tag.tag_timezone = 0
        tag.message = _b(f"{title}\n\nHive-Glyph-Snapshot-Glyphs: {' '.join(glyphs)}\n")
        self.repo.object_store.add_object(tag)
        if not self.repo.refs.add_if_new(
            _tag_ref(GLYPH_SNAPSHOT_TAG_PREFIX + name), tag.id
        ):
            raise ValueError(f"glyph snapshot {name!r} already exists")
        return GlyphSnapshotInfo(
            name=name,
            title=title,
            sha=sha,
            author=author.name,
            time=tag.tag_time,
            glyphs=tuple(glyphs),
        )

    def _snapshot_info(self, commit: Commit) -> SnapshotInfo:
        info = self.commit_info(_s(commit.id))
        title = info.message.splitlines()[0]
        if title.startswith("Snapshot: "):
            title = title[len("Snapshot: ") :]
        base = _trailer(commit.message, HIVE_SNAPSHOT_BASE_TRAILER)
        glyphs = _trailer(commit.message, HIVE_SNAPSHOT_GLYPHS_TRAILER) or ""
        changes = _trailer(commit.message, HIVE_SNAPSHOT_CHANGES_TRAILER) or "0"
        return SnapshotInfo(
            name=_trailer(commit.message, HIVE_SNAPSHOT_TRAILER) or "",
            title=title,
            sha=info.sha,
            author=info.author,
            time=info.time,
            base=None if base in (None, "none") else base,
            changes=int(changes) if changes.isdigit() else 0,
            glyphs=tuple(glyphs.split()),
        )

    def diff(self, old_ref: str | None, new_ref: str) -> list[TreeChange]:
        """Paths that differ between two commits (``old_ref`` may be ``None``
        for an empty tree)."""
        store = self.repo.object_store
        old_tree = _b(self.tree_sha(self.resolve(old_ref))) if old_ref else None
        new_tree = _b(self.tree_sha(self.resolve(new_ref)))
        result = []
        for change in diff_tree.tree_changes(store, old_tree, new_tree):
            if change.type == diff_tree.CHANGE_ADD:
                result.append(TreeChange("add", _s(change.new.path)))
            elif change.type == diff_tree.CHANGE_DELETE:
                result.append(TreeChange("delete", _s(change.old.path)))
            else:
                result.append(TreeChange("modify", _s(change.new.path)))
        return result

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        a = _b(self.resolve(ancestor))
        for entry in self.repo.get_walker(include=[_b(self.resolve(descendant))]):
            if entry.commit.id == a:
                return True
        return False

    def fast_forward(self, branch: str, to_ref: str) -> str:
        """Move ``branch`` to ``to_ref`` if it is a descendant of the branch head."""
        current = self.head(branch)
        target = self.resolve(to_ref)
        if current is not None and not self.is_ancestor(current, target):
            raise ValueError(f"{to_ref!r} is not a fast-forward of {branch!r}")
        if not self.repo.refs.set_if_equals(
            _branch_ref(branch), _b(current) if current else None, _b(target)
        ):
            raise RefMovedError(branch, current, self.head(branch))
        return target


HIVE_GLYPHS_TRAILER = b"Hive-Glyphs:"
HIVE_SNAPSHOT_TRAILER = b"Hive-Snapshot:"
HIVE_SNAPSHOT_BASE_TRAILER = b"Hive-Snapshot-Base:"
HIVE_SNAPSHOT_GLYPHS_TRAILER = b"Hive-Snapshot-Glyphs:"
HIVE_SNAPSHOT_CHANGES_TRAILER = b"Hive-Snapshot-Changes:"
HIVE_GLYPH_SNAPSHOT_GLYPHS_TRAILER = b"Hive-Glyph-Snapshot-Glyphs:"


def _trailer(message: bytes, key: bytes) -> str | None:
    """The value of the last ``key`` line of a commit message, stripped, or
    None when there is no such line."""
    value = None
    for line in message.splitlines():
        if line.startswith(key):
            value = _s(line[len(key) :]).strip()
    return value


def _hive_glyphs_trailer(message: bytes) -> set[str] | None:
    """The glyph names listed in a commit's ``Hive-Glyphs:`` trailer, or None
    when the commit has no such trailer (an import, an external commit)."""
    for line in message.splitlines():
        if line.startswith(HIVE_GLYPHS_TRAILER):
            return set(_s(line[len(HIVE_GLYPHS_TRAILER) :]).split())
    return None


def _normalize_path(path: str) -> str:
    path = path.replace(os.sep, "/").lstrip("/")
    if not path or ".." in path.split("/"):
        raise ValueError(f"invalid path {path!r}")
    return path


def _is_ignored(relative: pathlib.Path) -> bool:
    return (
        any(part.startswith(".") for part in relative.parts)
        or relative.name == ".DS_Store"
    )


def iter_glyph_paths(paths: Iterable[str]) -> Iterator[str]:
    for path in paths:
        if path.startswith("glyphs/") and path.endswith(".json"):
            yield path
