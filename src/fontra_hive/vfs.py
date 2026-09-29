"""A tiny virtual file system over a git commit.

:class:`GitTree` holds the file listing of one commit (``path -> blob sha``)
plus a set of pending changes. :class:`GitPath` is the ``pathlib.Path``-like
object handed to Fontra's ``FontraBackend``: it reads blobs lazily from the
repository and records writes as pending changes, so the ``.fontra``
serialisation code of Fontra is reused untouched and the files it writes are
byte-for-byte what a ``FontraBackend`` on disk would write.

Only the operations ``FontraBackend`` performs are implemented.
"""

from __future__ import annotations

import fnmatch
import io
import posixpath
from typing import Iterator

from .gitstore import GitRepoStore


class GitTree:
    def __init__(self, store: GitRepoStore, commit_sha: str | None, *, label: str = ""):
        self.store = store
        self.commit_sha = commit_sha
        self.label = label  # used to build informative fspath strings
        self.entries: dict[str, str] = store.list_tree(commit_sha) if commit_sha else {}
        self.pending: dict[str, bytes | None] = {}
        self._cache: dict[str, bytes] = {}
        self.read_only = False

    # --- state ------------------------------------------------------------

    def reset(self, commit_sha: str | None) -> None:
        """Forget pending changes and reload the listing from ``commit_sha``."""
        self.commit_sha = commit_sha
        self.entries = self.store.list_tree(commit_sha) if commit_sha else {}
        self.pending = {}
        self._cache = {}

    def take_pending(self) -> dict[str, bytes | None]:
        changes, self.pending = self.pending, {}
        return changes

    def apply_committed(
        self, commit_sha: str, changes: dict[str, bytes | None]
    ) -> None:
        """Update the listing after ``changes`` were committed as ``commit_sha``."""
        self.commit_sha = commit_sha
        self.entries = self.store.list_tree(commit_sha)
        for path in changes:
            self._cache.pop(path, None)

    # --- file operations ----------------------------------------------------

    def exists(self, path: str) -> bool:
        if path in self.pending:
            return self.pending[path] is not None
        return path in self.entries

    def is_dir(self, path: str) -> bool:
        if not path:
            return True
        prefix = path + "/"
        return any(
            p.startswith(prefix)
            for p in (
                *self.entries,
                *(q for q, d in self.pending.items() if d is not None),
            )
        )

    def read(self, path: str) -> bytes:
        if path in self.pending:
            data = self.pending[path]
            if data is None:
                raise FileNotFoundError(path)
            return data
        try:
            sha = self.entries[path]
        except KeyError:
            raise FileNotFoundError(path) from None
        data = self._cache.get(path)
        if data is None:
            data = self.store.read_blob(sha)
            self._cache[path] = data
        return data

    def write(self, path: str, data: bytes) -> None:
        self._check_writable(path)
        self.pending[path] = data

    def _check_writable(self, path: str) -> None:
        if self.read_only:
            raise PermissionError(f"{self.label} is read-only ({path})")

    def delete(self, path: str) -> None:
        self._check_writable(path)
        if path in self.pending and self.pending[path] is None:
            raise FileNotFoundError(path)
        if path not in self.entries and path not in self.pending:
            raise FileNotFoundError(path)
        self.pending[path] = None

    def listdir(self, path: str) -> list[str]:
        prefix = path + "/" if path else ""
        names = set()
        for p in (*self.entries, *self.pending):
            if not p.startswith(prefix) or (
                p in self.pending and self.pending[p] is None
            ):
                continue
            rest = p[len(prefix) :]
            names.add(rest.split("/", 1)[0])
        return sorted(names)

    def has_pending(self) -> bool:
        return bool(self.pending)


class GitPath:
    """``pathlib.Path``-like view on a :class:`GitTree`."""

    __slots__ = ("tree", "_path")

    def __init__(self, tree: GitTree, path: str = ""):
        self.tree = tree
        self._path = path.strip("/")

    # --- pathlib surface ----------------------------------------------------

    def __truediv__(self, other: str) -> "GitPath":
        return GitPath(
            self.tree,
            posixpath.join(self._path, str(other)) if self._path else str(other),
        )

    def __fspath__(self) -> str:
        return (
            f"/{self.tree.label}/{self._path}" if self._path else f"/{self.tree.label}"
        )

    def __str__(self) -> str:
        return self.__fspath__()

    def __repr__(self) -> str:
        return f"GitPath({self.__fspath__()!r})"

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, GitPath)
            and other.tree is self.tree
            and other._path == self._path
        )

    def __hash__(self) -> int:
        return hash((id(self.tree), self._path))

    @property
    def name(self) -> str:
        return posixpath.basename(self._path)

    @property
    def stem(self) -> str:
        return posixpath.splitext(self.name)[0]

    @property
    def suffix(self) -> str:
        return posixpath.splitext(self.name)[1]

    @property
    def parent(self) -> "GitPath":
        return GitPath(self.tree, posixpath.dirname(self._path))

    @property
    def relative(self) -> str:
        return self._path

    def resolve(self) -> "GitPath":
        return self

    def exists(self) -> bool:
        return self.tree.exists(self._path) or self.tree.is_dir(self._path)

    def is_file(self) -> bool:
        return self.tree.exists(self._path)

    def is_dir(self) -> bool:
        return self.tree.is_dir(self._path)

    def mkdir(self, parents: bool = False, exist_ok: bool = False) -> None:
        pass  # git has no empty directories; nothing to do

    def iterdir(self) -> Iterator["GitPath"]:
        for name in self.tree.listdir(self._path):
            yield self / name

    def glob(self, pattern: str) -> Iterator["GitPath"]:
        for child in self.iterdir():
            if fnmatch.fnmatchcase(child.name, pattern):
                yield child

    def read_bytes(self) -> bytes:
        return self.tree.read(self._path)

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.read_bytes().decode(encoding)

    def write_bytes(self, data: bytes) -> None:
        self.tree.write(self._path, bytes(data))

    def write_text(self, text: str, encoding: str = "utf-8") -> None:
        # FontraBackend passes encoding="utf=8" (sic) in one place; treat any
        # utf-8 spelling the same way pathlib would fail on it: be lenient.
        if encoding.lower().replace("=", "-") in ("utf-8", "utf8"):
            encoding = "utf-8"
        self.tree.write(self._path, text.encode(encoding))

    def unlink(self) -> None:
        self.tree.delete(self._path)

    def open(
        self, mode: str = "r", encoding: str | None = None, newline: str | None = None
    ):
        if "r" in mode:
            data = self.read_bytes()
            if "b" in mode:
                return io.BytesIO(data)
            return io.StringIO(data.decode(encoding or "utf-8"), newline=newline)
        if "w" in mode:
            if "b" in mode:
                return _WriteBack(self, binary=True)
            return _WriteBack(
                self, binary=False, encoding=encoding or "utf-8", newline=newline
            )
        raise ValueError(f"unsupported mode {mode!r}")


class _WriteBack:
    """File object that stores its content in the tree when closed."""

    def __init__(
        self, path: GitPath, *, binary: bool, encoding: str = "utf-8", newline=None
    ):
        self.path = path
        self.binary = binary
        self.encoding = encoding
        self.buffer = io.BytesIO() if binary else io.StringIO(newline=newline)

    def write(self, data):
        return self.buffer.write(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        value = self.buffer.getvalue()
        if not self.binary:
            value = value.encode(self.encoding)
        self.path.write_bytes(value)
