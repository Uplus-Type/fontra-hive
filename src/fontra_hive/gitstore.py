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
import stat
import time
from dataclasses import dataclass
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
    ) -> list[CommitInfo]:
        """Commits reachable from ``ref``, newest first, optionally only those
        that changed ``path`` (a file, or a directory: its subtree).

        ``glyph`` is the glyph name stored at ``path``: commits written by
        Fontra Hive carry a ``Hive-Glyphs:`` trailer listing the glyphs they
        changed, which answers the question without reading any tree.
        """
        sha = self.resolve(ref)
        if path is None:
            walker = self.repo.get_walker(include=[_b(sha)], max_entries=limit)
            return [self.commit_info(_s(entry.commit.id)) for entry in walker]
        return self._log_for_path(sha, _normalize_path(path), glyph, limit)

    def _log_for_path(
        self, sha: str, path: str, glyph: str | None, limit: int | None
    ) -> list[CommitInfo]:
        """History of one path along the first-parent line.

        For each commit, the ``Hive-Glyphs`` trailer decides when it is
        present and ``glyph`` is given (tiny commit objects only). Otherwise
        the object at ``path`` (a blob, or a subtree) is compared with the one
        in the first parent: two tree lookups, no full tree diff, which keeps
        this usable on repositories with tens of thousands of glyph files.
        """
        store = self.repo.object_store
        parts = _b(path)
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
        commit_id: bytes | None = _b(sha)
        while commit_id is not None and (limit is None or len(result) < limit):
            commit = store[commit_id]
            parent = commit.parents[0] if commit.parents else None
            glyphs = _hive_glyphs_trailer(commit.message) if glyph is not None else None
            if glyphs is not None:
                touched = glyph in glyphs
            else:
                touched = object_at(commit_id) != (
                    object_at(parent) if parent else None
                )
            if touched:
                result.append(self.commit_info(_s(commit_id)))
            commit_id = parent
        return result

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
