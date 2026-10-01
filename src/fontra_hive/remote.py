"""Remote git repositories (GitHub, GitLab, any git host) for Hive projects.

A Hive project can be connected to a branch of a remote repository holding
the same font, either as a ``.fontra`` package or as UFO/designspace sources.
Hive keeps its own ``.fontra`` history; the remote is a mirror kept in step by
two operations:

- :func:`pull` fetches the remote branch and records its state as a new
  commit on the Hive branch ``upstream/<remote branch>`` ("Pull from …"),
  converted to ``.fontra`` when the remote holds UFOs. The first pull forks
  ``upstream/…`` from the Hive branch it is pulled for, so that its changes
  are reviewed and merged with Hive's usual three-way merge (the branch menu's
  "Merge into…"), like any other branch.
- :func:`push` sends what changed on a Hive branch since the last sync as one
  commit on top of the remote branch (a snapshot's title is a good message),
  with ``Co-authored-by:`` lines for the Hive authors. It refuses when the
  remote moved since the last pull (pull first) and when the last pull is not
  merged into the branch (the push would undo the remote's changes).

What Hive believes the remote holds is the tree of the head of
``upstream/<branch>``; its ``Hive-Upstream:`` trailer is the remote commit it
corresponds to. A push adds a commit there too (``Hive-Push:``), with the
files that were sent.

**UFO/designspace remotes.** Measured on MutatorSans and Source Serif
(doc « Depots git distants - mesure de fidelite »): Fontra's designspace
backend writes glyphs faithfully but in its own formatting, and its
font-level writers lose data (non-kerning groups, feature ``include``\\ s,
per-master font info, the designspace's comments). So a push to UFO sends
**glyphs and kerning only**, with these safeguards:

- only the glyphs that changed are written, and a file whose content (as
  ufoLib reads it) is unchanged keeps its original bytes;
- a changed ``.glif`` or ``.plist`` keeps the original's XML declaration,
  indentation and final newline; ``<unicode>`` elements of the other masters
  and guideline identifiers, which Fontra does not keep, are put back;
- kerning is written key by key: ``kerning.plist`` and ``groups.plist`` get
  only the pairs and kerning groups Hive changed, other groups untouched.

Other font-level changes (font info, features, axes and sources, background
images) are not sent; :class:`PushResult` lists them in ``skipped``, for the
interface to say so. A ``.fontra`` remote gets every change, file by file.

Credentials (``Remote.username``/``password``: for GitHub a token as
password) are given by the caller and never stored here. URLs are checked
against SSRF (:func:`check_url`): HTTPS to public addresses only, unless
``allow_local`` (tests, development).
"""

from __future__ import annotations

import asyncio
import ipaddress
import pathlib
import posixpath
import re
import socket
import stat
import tempfile
import time
import urllib.parse
from contextlib import aclosing, contextmanager
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from dulwich.client import get_transport_and_path
from dulwich.index import commit_tree
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit

from .gitstore import (
    FILE_MODE,
    SERVER_SIGNATURE,
    GitRepoStore,
    Signature,
    _is_ignored,
    _trailer,
)
from .merge import GLYPH_INFO_FILE, KERNING_FILE, _read_glyph_info, glyph_name_of

UPSTREAM_PREFIX = "upstream/"
REMOTE_REF_PREFIX = "refs/hive-remote/"
HIVE_UPSTREAM_TRAILER = b"Hive-Upstream:"
HIVE_PULL_TRAILER = b"Hive-Pull:"
HIVE_PUSH_TRAILER = b"Hive-Push:"

UFO_SUFFIXES = (".designspace", ".ufo")
GITLINK_MODE = 0o160000


class RemoteError(Exception):
    """A remote operation could not be done; the message is for the user."""


class UnsafeURLError(RemoteError):
    pass


class RemoteMovedError(RemoteError):
    """The remote branch moved since the last pull: pull (and merge) first."""


class NotMergedError(RemoteError):
    """The last pull is not merged into the branch to push."""


@dataclass(frozen=True)
class Remote:
    """A branch of a remote repository and where the font is in it.

    ``path``: the font inside the repository: ``Font.designspace``,
    ``sources/Font.ufo``, ``Font.fontra``, or ``""`` when the repository root
    is itself a ``.fontra`` package."""

    url: str
    branch: str = "main"
    path: str = ""
    username: str | None = None
    password: str | None = field(default=None, repr=False)

    def __post_init__(self):
        path = self.path.strip("/")
        if ".." in path.split("/"):
            raise RemoteError(f"invalid font path {self.path!r}")
        object.__setattr__(self, "path", path)
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", self.branch) or (
            ".." in self.branch
        ):
            raise RemoteError(f"invalid branch name {self.branch!r}")

    @property
    def format(self) -> str:
        return "ufo" if self.path.lower().endswith(UFO_SUFFIXES) else "fontra"

    @property
    def upstream_branch(self) -> str:
        return UPSTREAM_PREFIX + self.branch

    @property
    def ref(self) -> bytes:
        return f"refs/heads/{self.branch}".encode()

    @property
    def tracking_ref(self) -> bytes:
        """Where the fetched remote head is kept in the Hive repository (so
        that its objects are not garbage)."""
        return f"{REMOTE_REF_PREFIX}{self.branch}".encode()


# --- addresses ----------------------------------------------------------------------


def check_url(url: str, *, allow_local: bool = False) -> None:
    """Refuse URLs a server must not fetch on a user's behalf: anything but
    HTTPS, and hosts that resolve to private, loopback, link-local or
    reserved addresses (SSRF). ``allow_local``: also local paths, ``file://``,
    ``http://`` and private addresses (tests, development)."""
    parsed = urllib.parse.urlsplit(url)
    if allow_local:
        return
    if parsed.scheme != "https":
        raise UnsafeURLError("Only https:// remote URLs are accepted.")
    if parsed.username or parsed.password:
        raise UnsafeURLError("Put credentials in the token field, not in the URL.")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("The remote URL has no host.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeURLError(f"Unknown host {host}.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise UnsafeURLError(f"{host} is not a public address.")


def _client(remote: Remote, allow_local: bool):
    check_url(remote.url, allow_local=allow_local)
    kwargs = {}
    if remote.username is not None or remote.password is not None:
        kwargs = {"username": remote.username or "", "password": remote.password}
    try:
        return get_transport_and_path(remote.url, **kwargs)
    except TypeError:  # a transport without credentials (local path)
        return get_transport_and_path(remote.url)


def remote_head(remote: Remote, *, allow_local: bool = False) -> str | None:
    """The sha of the remote branch (None: the branch or repository is empty)."""
    client, path = _client(remote, allow_local)
    try:
        result = client.get_refs(path)
    except Exception as error:
        raise RemoteError(f"Could not reach {remote.url}: {error}") from error
    # dulwich >= 0.23 returns an LsRemoteResult (refs in .refs), older a dict.
    refs = getattr(result, "refs", result)
    sha = refs.get(remote.ref)
    return sha.decode() if sha else None


def _fetch(store: GitRepoStore, remote: Remote, allow_local: bool) -> str | None:
    """Fetch the remote branch's head commit (only its tree, when the
    transport allows a shallow fetch) and keep it under ``tracking_ref``."""
    client, path = _client(remote, allow_local)
    wanted = {}

    def determine_wants(refs, depth=None):
        sha = refs.get(remote.ref)
        wanted["sha"] = sha
        if not sha or sha in store.repo.object_store:
            return []
        return [sha]

    try:
        try:
            client.fetch(path, store.repo, determine_wants=determine_wants, depth=1)
        except NotImplementedError:
            client.fetch(path, store.repo, determine_wants=determine_wants)
    except RemoteError:
        raise
    except Exception as error:
        raise RemoteError(f"Could not fetch from {remote.url}: {error}") from error
    sha = wanted.get("sha")
    if sha:
        store.repo.refs[remote.tracking_ref] = sha
        return sha.decode()
    return None


# --- what Hive knows of the remote --------------------------------------------------


@dataclass(frozen=True)
class UpstreamState:
    head: str | None  # head of upstream/<branch> in Hive
    remote_sha: str | None  # the remote commit that head corresponds to
    last_pull: str | None  # the latest "Pull from" commit of upstream/<branch>


def upstream_state(store: GitRepoStore, remote: Remote) -> UpstreamState:
    head = store.head(remote.upstream_branch)
    if head is None:
        return UpstreamState(None, None, None)
    message = store.repo[head.encode()].message
    remote_sha = _trailer(message, HIVE_UPSTREAM_TRAILER)
    last_pull = None
    sha = head
    while sha:
        commit = store.repo[sha.encode()]
        if _trailer(commit.message, HIVE_PULL_TRAILER) is not None:
            last_pull = sha
            break
        if _trailer(commit.message, HIVE_UPSTREAM_TRAILER) is None:
            break  # below the first pull: the Hive branch it was forked from
        sha = commit.parents[0].decode() if commit.parents else None
    return UpstreamState(head, remote_sha or None, last_pull)


@dataclass(frozen=True)
class RemoteStatus:
    remote_sha: str | None  # the remote branch now
    synced_sha: str | None  # the remote commit Hive last pulled or pushed
    remote_moved: bool  # the remote has commits Hive has not pulled
    unmerged: bool  # the last pull is not merged into the branch
    pending: list[str]  # .fontra files changed on the branch since the last sync

    @property
    def can_push(self) -> bool:
        return bool(self.pending) and not self.remote_moved and not self.unmerged


def status(
    store: GitRepoStore,
    remote: Remote,
    branch: str = "main",
    *,
    allow_local: bool = False,
) -> RemoteStatus:
    """Where a Hive branch stands against the remote, for the interface
    (ahead: ``pending``; behind: ``remote_moved``; diverged: both)."""
    current = remote_head(remote, allow_local=allow_local)
    state = upstream_state(store, remote)
    head = store.head(branch)
    unmerged = bool(
        state.last_pull and head and not store.is_ancestor(state.last_pull, head)
    )
    pending = []
    if head:
        base = store.list_tree(state.head) if state.head else {}
        new = store.list_tree(head)
        pending = sorted(p for p in set(base) | set(new) if base.get(p) != new.get(p))
    return RemoteStatus(
        remote_sha=current,
        synced_sha=state.remote_sha,
        remote_moved=current != state.remote_sha,
        unmerged=unmerged,
        pending=pending,
    )


# --- trees ----------------------------------------------------------------------


def _tree_entries(store: GitRepoStore, commit_sha: str | None) -> dict[str, tuple]:
    """``path -> (mode, blob sha)`` for the whole tree of a commit."""
    if not commit_sha:
        return {}
    repo = store.repo
    result = {}
    for entry in iter_tree_contents(repo.object_store, repo[commit_sha.encode()].tree):
        result[entry.path.decode("utf-8")] = (entry.mode, entry.sha)
    return result


def _materialize(store: GitRepoStore, entries: Mapping[str, tuple], dest: pathlib.Path):
    for path, (mode, sha) in entries.items():
        if stat.S_ISLNK(mode) or mode == GITLINK_MODE:
            continue
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(store.repo[sha].data)
        if mode & 0o111:
            target.chmod(0o755)


def _under(path: str, prefix: str) -> str | None:
    """``path`` relative to the folder ``prefix`` ("" = the root), or None."""
    if not prefix:
        return path
    if path.startswith(prefix + "/"):
        return path[len(prefix) + 1 :]
    return None


def _font_folder(remote: Remote) -> str:
    """The folder of the remote holding the font's files: the ``.fontra``
    package itself, or the folder of the designspace / the ``.ufo``."""
    if remote.format == "fontra":
        return remote.path
    if remote.path.lower().endswith(".ufo"):
        return remote.path
    return remote.path.rpartition("/")[0]


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Called from a running loop (the server): run in a thread of its own.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _copy_font(source: pathlib.Path, destination: pathlib.Path) -> None:
    from fontra.backends import getFileSystemBackend, newFileSystemBackend
    from fontra.backends.copy import copyFont

    src = getFileSystemBackend(source)
    dst = newFileSystemBackend(destination)
    async with aclosing(src), aclosing(dst):
        await copyFont(src, dst, continueOnError=True)


def _read_folder(folder: pathlib.Path) -> dict[str, bytes]:
    return {
        f.relative_to(folder).as_posix(): f.read_bytes()
        for f in sorted(folder.rglob("*"))
        if f.is_file() and not _is_ignored(f.relative_to(folder))
    }


def _designspace_sources(data: bytes) -> list[str]:
    """The ``filename`` of each ``<source>`` of a designspace document."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise RemoteError(f"The designspace cannot be read: {error}")
    return [s.get("filename") for s in root.iter("source") if s.get("filename")]


def font_entries(
    store: GitRepoStore, remote: Remote, entries: Mapping[str, tuple]
) -> dict[str, tuple]:
    """The part of a remote tree a UFO/designspace font needs: the
    designspace, the UFOs it names (wherever they are in the repository),
    or the one ``.ufo``; plus every ``.fea`` file of the repository (feature
    files ``include`` each other across folders). Only this is unpacked:
    a repository may hold much else (proofs, archives, other fonts)."""
    keep: set[str] = set()

    def add_folder(folder: str) -> None:
        prefix = folder.strip("/") + "/"
        keep.update(p for p in entries if p.startswith(prefix))

    if remote.path.lower().endswith(".ufo"):
        add_folder(remote.path)
    else:
        if remote.path not in entries:
            raise RemoteError(f"{remote.path} is not in the remote repository.")
        keep.add(remote.path)
        base = posixpath.dirname(remote.path)
        data = store.repo[entries[remote.path][1]].data
        for filename in _designspace_sources(data):
            target = posixpath.normpath(posixpath.join(base, filename))
            if target == ".." or target.startswith("../") or target.startswith("/"):
                continue  # outside the repository
            add_folder(target)
    keep.update(p for p in entries if p.lower().endswith(".fea"))
    return {p: entries[p] for p in sorted(keep)}


def remote_as_fontra(
    store: GitRepoStore, remote: Remote, remote_sha: str
) -> dict[str, bytes]:
    """The font of a fetched remote commit as the files of a ``.fontra``
    package (converted with Fontra's own backends for UFO/designspace)."""
    entries = _tree_entries(store, remote_sha)
    if remote.format == "fontra":
        files = {}
        for path, (mode, sha) in entries.items():
            inner = _under(path, remote.path)
            if inner is not None and not _is_ignored(pathlib.PurePosixPath(inner)):
                if not stat.S_ISLNK(mode) and mode != GITLINK_MODE:
                    files[inner] = store.repo[sha].data
        if "font-data.json" not in files and not any(
            p.startswith("glyphs/") for p in files
        ):
            raise RemoteError(f"No .fontra package at {remote.path or 'the root'}.")
        return files
    if remote.path not in entries and not any(
        p.startswith(remote.path + "/") for p in entries
    ):
        raise RemoteError(f"{remote.path} is not in the remote repository.")
    with tempfile.TemporaryDirectory(prefix="hive-pull-") as tmp:
        work = pathlib.Path(tmp) / "repo"
        _materialize(store, font_entries(store, remote, entries), work)
        converted = pathlib.Path(tmp) / "converted.fontra"
        try:
            _run(_copy_font(work / remote.path, converted))
        except Exception as error:
            raise RemoteError(f"Could not read {remote.path}: {error}") from error
        return _read_folder(converted)


# --- pull ---------------------------------------------------------------------


@dataclass(frozen=True)
class PullResult:
    remote_sha: str | None
    commit: str | None  # the new "Pull from" commit (None: nothing new)
    branch: str  # upstream/<branch>
    glyphs: tuple[str, ...] = ()


def _glyph_names(
    paths: Iterable[str],
    old: Mapping[str, bytes | None],
    new: Mapping[str, bytes | None],
) -> list[str]:
    names = set()
    for path in paths:
        if path.startswith("glyphs/") and path.endswith(".json"):
            name = glyph_name_of(new.get(path)) or glyph_name_of(old.get(path))
            if name:
                names.add(name)
    return sorted(names)


def pull(
    store: GitRepoStore,
    remote: Remote,
    *,
    base_branch: str = "main",
    author: Signature = SERVER_SIGNATURE,
    allow_local: bool = False,
) -> PullResult:
    """Fetch the remote branch and record it on ``upstream/<branch>``. The
    first pull forks that branch from ``base_branch``; merging it there is
    then the usual branch merge."""
    remote_sha = _fetch(store, remote, allow_local)
    state = upstream_state(store, remote)
    branch = remote.upstream_branch
    if remote_sha is None or remote_sha == state.remote_sha:
        return PullResult(remote_sha, None, branch)

    files = remote_as_fontra(store, remote, remote_sha)
    if state.head is None:
        if store.head(base_branch) is None:
            raise RemoteError(f"No branch {base_branch!r} in this project.")
        store.create_branch(branch, base_branch)
    parent = store.head(branch)
    current = {
        path: store.read_blob(sha) for path, sha in store.list_tree(parent).items()
    }
    changes: dict[str, bytes | None] = {
        path: None for path in current if path not in files
    }
    changes.update({p: d for p, d in files.items() if current.get(p) != d})
    glyphs = _glyph_names(changes, current, files)
    short = remote_sha[:10]
    message = (
        f"Pull from {_display_url(remote.url)} {remote.branch}@{short}\n\n"
        + (f"Hive-Glyphs: {' '.join(glyphs)}\n" if glyphs else "")
        + f"Hive-Pull: {_display_url(remote.url)} {remote.branch}\n"
        + f"Hive-Upstream: {remote_sha}\n"
    )
    sha = store.commit(
        changes, branch=branch, message=message, author=author, expected_head=parent
    )
    return PullResult(remote_sha, sha, branch, tuple(glyphs))


def _display_url(url: str) -> str:
    """The URL without credentials, for commit messages."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    return urllib.parse.urlunsplit(parsed)


# --- push ---------------------------------------------------------------------


@dataclass(frozen=True)
class PushResult:
    pushed: bool
    remote_sha: str | None  # the remote branch after the push
    previous: str | None  # the remote branch before
    commit: str | None  # the "Hive-Push" commit on upstream/<branch>
    files: tuple[str, ...] = ()  # remote files written or deleted
    glyphs: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()  # .fontra files changed but not sent


def push(
    store: GitRepoStore,
    remote: Remote,
    *,
    branch: str = "main",
    message: str,
    author: Signature,
    co_authors: Iterable[Signature] = (),
    committer: Signature = SERVER_SIGNATURE,
    allow_local: bool = False,
) -> PushResult:
    """Send what changed on ``branch`` since the last sync as one commit on
    top of the remote branch."""
    remote_sha = _fetch(store, remote, allow_local)
    state = upstream_state(store, remote)
    if remote_sha != state.remote_sha:
        raise RemoteMovedError(
            f"{remote.branch} moved on the remote since the last pull: "
            "pull and merge first."
        )
    head = store.head(branch)
    if head is None:
        raise RemoteError(f"No branch {branch!r} in this project.")
    if state.last_pull and not store.is_ancestor(state.last_pull, head):
        raise NotMergedError(
            f"The last pull from {remote.branch} is not merged into {branch}: "
            "merge it first, or the push would undo the remote's changes."
        )

    base = store.list_tree(state.head) if state.head else {}
    new = store.list_tree(head)
    changed = sorted(p for p in set(base) | set(new) if base.get(p) != new.get(p))
    if not changed:
        return PushResult(False, remote_sha, remote_sha, None)
    old_files = {p: store.read_blob(base[p]) for p in changed if p in base}
    new_files = {p: store.read_blob(new[p]) for p in changed if p in new}

    remote_entries = _tree_entries(store, remote_sha)
    if remote.format == "fontra":
        remote_changes = {_join(remote.path, p): new_files.get(p) for p in changed}
        sent, skipped = changed, []
    else:
        remote_changes, sent, skipped = _ufo_changes(
            store,
            remote,
            remote_entries,
            head,
            state.head,
            changed,
            old_files,
            new_files,
        )
    remote_changes = {
        p: d
        for p, d in remote_changes.items()
        if (store.repo[remote_entries[p][1]].data if p in remote_entries else None) != d
    }
    glyphs = _glyph_names(sent, old_files, new_files)
    if not remote_changes:
        return PushResult(False, remote_sha, remote_sha, None, skipped=tuple(skipped))

    new_remote = _remote_commit(
        store,
        remote_entries,
        remote_changes,
        remote_sha,
        message,
        author,
        co_authors,
        committer,
    )
    _send(store, remote, remote_sha, new_remote, allow_local)
    store.repo.refs[remote.tracking_ref] = new_remote.encode()

    # Record what the remote now holds, in Hive's terms.
    record = {p: new_files.get(p) for p in sent}
    if state.head is None:
        store.create_branch(remote.upstream_branch, branch)
        record = {}  # forked from the branch pushed: it already holds `sent`
        for p in skipped:
            record[p] = old_files.get(p)
    record_message = (
        f"Push to {_display_url(remote.url)} {remote.branch}@{new_remote[:10]}\n\n"
        + (f"Hive-Glyphs: {' '.join(glyphs)}\n" if glyphs else "")
        + f"Hive-Push: {head}\n"
        + f"Hive-Upstream: {new_remote}\n"
    )
    record_sha = store.commit(
        record,
        branch=remote.upstream_branch,
        message=record_message,
        author=author,
    )
    return PushResult(
        True,
        new_remote,
        remote_sha,
        record_sha,
        files=tuple(sorted(remote_changes)),
        glyphs=tuple(glyphs),
        skipped=tuple(skipped),
    )


def _join(folder: str, path: str) -> str:
    return f"{folder}/{path}" if folder else path


def _remote_commit(
    store, entries, changes, parent, message, author, co_authors, committer
) -> str:
    repo = store.repo
    tree = dict(entries)
    for path, data in changes.items():
        if data is None:
            tree.pop(path, None)
        else:
            blob = Blob.from_string(data)
            repo.object_store.add_object(blob)
            mode = entries[path][0] if path in entries else FILE_MODE
            tree[path] = (mode, blob.id)
    tree_id = commit_tree(
        repo.object_store,
        ((p.encode("utf-8"), sha, mode) for p, (mode, sha) in sorted(tree.items())),
    )
    lines = message.rstrip("\n")
    seen = {author.email.lower()}
    trailers = []
    for co in co_authors:
        if co.email.lower() not in seen:
            seen.add(co.email.lower())
            trailers.append(f"Co-authored-by: {co.name} <{co.email}>")
    if trailers:
        lines += "\n\n" + "\n".join(trailers)
    commit = Commit()
    commit.tree = tree_id
    commit.parents = [parent.encode()] if parent else []
    commit.author = author.encode()
    commit.committer = committer.encode()
    commit.author_time = commit.commit_time = int(time.time())
    commit.author_timezone = commit.commit_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = (lines + "\n").encode("utf-8")
    repo.object_store.add_object(commit)
    return commit.id.decode()


def _send(store, remote: Remote, old: str | None, new: str, allow_local: bool):
    client, path = _client(remote, allow_local)
    ref = remote.ref
    old_b = old.encode() if old else None

    def update_refs(refs):
        current = refs.get(ref)
        if current != old_b and not (old_b is None and current in (None, b"0" * 40)):
            raise RemoteMovedError(
                f"{remote.branch} moved on the remote meanwhile: pull and merge first."
            )
        refs = dict(refs)
        refs[ref] = new.encode()
        return refs

    try:
        result = client.send_pack(path, update_refs, store.repo.generate_pack_data)
    except RemoteError:
        raise
    except Exception as error:
        raise RemoteError(f"Could not push to {remote.url}: {error}") from error
    statuses = getattr(result, "ref_status", None) or {}
    error = statuses.get(ref)
    if error:
        raise RemoteError(f"The remote refused the push: {error}")


# --- UFO / designspace ----------------------------------------------------------


def _ufo_changes(
    store, remote, remote_entries, head, base_head, changed, old_files, new_files
):
    """The remote files to write for a UFO/designspace remote: glyphs and
    kerning only, written by Fontra's designspace backend into a checkout of
    the remote, then cleaned up by the safeguards."""
    sent, skipped = [], []
    glyph_paths = [
        p for p in changed if p.startswith("glyphs/") and p.endswith(".json")
    ]
    sent += glyph_paths
    put_names = set(_glyph_names(glyph_paths, {}, new_files))
    delete_names = set(_glyph_names(glyph_paths, old_files, {})) - put_names

    old_info = _read_glyph_info(old_files.get(GLYPH_INFO_FILE))
    new_info = _read_glyph_info(new_files.get(GLYPH_INFO_FILE))
    codepoints_changed = set()
    if GLYPH_INFO_FILE in changed:
        if GLYPH_INFO_FILE not in old_files:  # first push: no base to compare
            old_info = new_info
        other_change = False
        for name in set(old_info) | set(new_info):
            o, n = old_info.get(name), new_info.get(name)
            if o == n:
                continue
            if o is None:
                put_names.add(name)
            elif n is None:
                delete_names.add(name)
            else:
                if o.get("code points") != n.get("code points"):
                    codepoints_changed.add(name)
                    put_names.add(name)
                if {k: v for k, v in o.items() if k != "code points"} != {
                    k: v for k, v in n.items() if k != "code points"
                }:
                    other_change = True
        (skipped if other_change else sent).append(GLYPH_INFO_FILE)
    put_names -= delete_names
    kerning_changed = KERNING_FILE in changed
    if kerning_changed:
        sent.append(KERNING_FILE)
    skipped += [p for p in changed if p not in sent]

    if not put_names and not delete_names and not kerning_changed:
        return {}, sent, skipped
    if not remote_entries:
        raise RemoteError(
            "The remote branch is empty: a first push to UFO is not supported yet."
        )

    with tempfile.TemporaryDirectory(prefix="hive-push-") as tmp:
        tmp = pathlib.Path(tmp)
        work = tmp / "repo"
        checked_out = font_entries(store, remote, remote_entries)
        _materialize(store, checked_out, work)
        new_fontra = tmp / "new.fontra"
        store.export(head, new_fontra)
        old_fontra = None
        if kerning_changed and base_head:
            old_fontra = tmp / "old.fontra"
            store.export(base_head, old_fontra)
        _run(
            _write_ufo(
                work,
                work / remote.path,
                new_fontra,
                old_fontra,
                sorted(put_names),
                sorted(delete_names),
                kerning_changed,
            )
        )
        changes = _collect(store, remote_entries, checked_out, work, codepoints_changed)
    return changes, sent, skipped


KERNING_PLIST = "kerning.plist"
GROUPS_PLIST = "groups.plist"


async def _write_ufo(
    root, target, new_fontra, old_fontra, put_names, delete_names, kerning_changed
):
    from fontra.backends import getFileSystemBackend

    hive = getFileSystemBackend(new_fontra)
    ufo = getFileSystemBackend(target)
    async with aclosing(hive), aclosing(ufo):
        glyph_map = await hive.getGlyphMap()
        if kerning_changed:
            folders = _ufo_folders(root, target)
            original = _read_plists(folders)
            old_kerning = {}
            if old_fontra is not None:
                old_backend = getFileSystemBackend(old_fontra)
                async with aclosing(old_backend):
                    old_kerning = await old_backend.getKerning()
            with _unvalidated_kerning_writes():
                if old_kerning:
                    await ufo.putKerning(old_kerning)
                else:
                    await ufo.putKerning(await _empty_kerning(hive))
                written_old = _read_plists(folders)
                await ufo.putKerning(await hive.getKerning())
                written_new = _read_plists(folders)
            _write_plists(original, written_old, written_new)
        for name in put_names:
            glyph = await hive.getGlyph(name)
            if glyph is not None:
                await ufo.putGlyph(name, glyph, list(glyph_map.get(name, [])))
        for name in delete_names:
            try:
                await ufo.deleteGlyph(name)
            except KeyError:
                pass


@contextmanager
def _unvalidated_kerning_writes():
    """Let Fontra's ``putKerning`` write what it was given even when ufoLib
    would refuse it (a glyph in two kerning groups of the same side: seen in
    real sources). Its output is only compared, old against new; the files
    Hive writes are patched from the originals by :func:`_write_plists`."""
    from fontTools.ufoLib import UFOWriter

    write_groups, write_kerning = UFOWriter.writeGroups, UFOWriter.writeKerning

    def groups(self, groups, validate=None):
        return write_groups(self, groups, validate=False)

    def kerning(self, kerning, validate=None):
        return write_kerning(self, kerning, validate=False)

    UFOWriter.writeGroups, UFOWriter.writeKerning = groups, kerning
    try:
        yield
    finally:
        UFOWriter.writeGroups, UFOWriter.writeKerning = write_groups, write_kerning


async def _empty_kerning(hive):
    from fontra.core.classes import Kerning

    kerning = await hive.getKerning()
    table = kerning.get("kern")
    ids = table.sourceIdentifiers if table else []
    return {
        "kern": Kerning(
            groupsSide1={}, groupsSide2={}, sourceIdentifiers=ids, values={}
        )
    }


def _ufo_folders(root: pathlib.Path, target: pathlib.Path) -> list[pathlib.Path]:
    """The UFOs unpacked for the push (only the font's: see font_entries)."""
    if target.suffix.lower() == ".ufo":
        return [target]
    return sorted(p for p in root.rglob("*.ufo") if p.is_dir())


def _read_plists(folders) -> dict[pathlib.Path, dict | None]:
    from fontTools.misc import plistlib

    result = {}
    for folder in folders:
        for name in (KERNING_PLIST, GROUPS_PLIST):
            path = folder / name
            result[path] = plistlib.loads(path.read_bytes()) if path.exists() else None
    return result


def _flat(kind: str, data: dict | None) -> dict:
    if not data:
        return {}
    if kind == KERNING_PLIST:
        return {(lt, rt): v for lt, rights in data.items() for rt, v in rights.items()}
    return dict(data)


def _write_plists(original, written_old, written_new) -> None:
    """Key-by-key patch: what Fontra writes for the old and the new kerning
    differs only where Hive changed something; apply those keys, and only
    them, to the original files."""
    from fontTools.misc import plistlib

    for path, before in original.items():
        kind = path.name
        o = _flat(kind, written_old.get(path))
        n = _flat(kind, written_new.get(path))
        result = _flat(kind, before)
        changed = False
        for key in set(o) | set(n):
            if o.get(key) == n.get(key):
                continue
            changed = True
            if key in n:
                result[key] = n[key]
            else:
                result.pop(key, None)
        if not changed:
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(plistlib.dumps(before))
            continue
        if kind == KERNING_PLIST:
            nested: dict = {}
            for (left, right), value in result.items():
                nested.setdefault(left, {})[right] = value
            result = nested
        if not result and before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(plistlib.dumps(result))


def _collect(
    store, remote_entries, checked_out, work, codepoints_changed
) -> dict[str, bytes | None]:
    """The files of the checkout that differ from the remote commit, after
    the safeguards. Only what was unpacked (``checked_out``) can have been
    deleted; the rest of the repository stays as it is."""
    changes: dict[str, bytes | None] = {}
    seen = set()
    for file in sorted(work.rglob("*")):
        if not file.is_file() or file.is_symlink():
            continue
        path = file.relative_to(work).as_posix()
        seen.add(path)
        data = file.read_bytes()
        if path in remote_entries:
            if Blob.from_string(data).id == remote_entries[path][1]:
                continue  # untouched (most files): no need to read the original
            old = store.repo[remote_entries[path][1]].data
            if same_content(path, old, data):
                continue
            data = match_formatting(old, data)
            if path.endswith(".glif"):
                data = restore_glif_details(old, data, codepoints_changed)
            if data != old:
                changes[path] = data
        else:
            template = _sibling(remote_entries, path)
            if template is not None:
                data = match_formatting(store.repo[template].data, data)
            changes[path] = data
    for path, (mode, _sha) in checked_out.items():
        if path not in seen and not stat.S_ISLNK(mode) and mode != GITLINK_MODE:
            changes[path] = None
    return changes


def _sibling(remote_entries, path: str):
    """A file of the same kind in the same folder (or, for a new layer
    folder, the same UFO): the formatting a new file should follow."""
    suffix = pathlib.PurePosixPath(path).suffix
    if suffix not in (".glif", ".plist"):
        return None
    folder = path.rpartition("/")[0]
    ufo = folder.rpartition("/")[0] if suffix == ".glif" else folder
    fallback = None
    for other, (_mode, sha) in remote_entries.items():
        if other.endswith(suffix):
            if other.rpartition("/")[0] == folder:
                return sha
            if fallback is None and other.startswith(ufo + "/"):
                fallback = sha
    return fallback


# --- formatting safeguards ------------------------------------------------------


class _Glyph:
    pass


def _glif_content(data: bytes):
    from fontTools.pens.recordingPen import RecordingPointPen
    from fontTools.ufoLib.glifLib import readGlyphFromString

    glyph = _Glyph()
    pen = RecordingPointPen()
    readGlyphFromString(data, glyph, pen, validate=False)
    return dict(vars(glyph)), pen.value


def same_content(path: str, old: bytes | None, new: bytes | None) -> bool:
    """The two versions of a file read the same (ufoLib, plistlib): only
    their formatting differs."""
    if old is None or new is None:
        return False
    try:
        if path.endswith(".glif"):
            return _glif_content(old) == _glif_content(new)
        if path.endswith(".plist"):
            from fontTools.misc import plistlib

            return plistlib.loads(old) == plistlib.loads(new)
    except Exception:
        return False
    return old.replace(b"\r\n", b"\n").strip() == new.replace(b"\r\n", b"\n").strip()


def _indent_unit(lines: list[bytes]) -> bytes | None:
    for line in lines[1:]:
        stripped = line.lstrip(b" \t")
        if stripped and len(stripped) < len(line):
            return line[: len(line) - len(stripped)]
    return None


def match_formatting(old: bytes, new: bytes) -> bytes:
    """``new`` with the XML declaration, indentation unit and final newline
    of ``old`` (form only, never content)."""
    if not old.lstrip().startswith(b"<") or not new.lstrip().startswith(b"<"):
        return new
    old_lines = old.split(b"\n")
    new_lines = new.split(b"\n")
    if old_lines[0].startswith(b"<?xml") and new_lines[0].startswith(b"<?xml"):
        new_lines[0] = old_lines[0].rstrip(b"\r")
    old_unit, new_unit = _indent_unit(old_lines), _indent_unit(new_lines)
    if old_unit and new_unit and old_unit != new_unit:
        for i, line in enumerate(new_lines):
            depth = 0
            while line.startswith(new_unit):
                line = line[len(new_unit) :]
                depth += 1
            new_lines[i] = old_unit * depth + line
    crlf = b"\r\n" in old
    result = b"\n".join(line.rstrip(b"\r") for line in new_lines)
    if crlf:
        result = result.replace(b"\n", b"\r\n")
    end = b"\r\n" if crlf else b"\n"
    result = result.rstrip(b"\r\n")
    if old.endswith(b"\n"):
        result += end
    return result


_UNICODE_RE = re.compile(rb"^[ \t]*<unicode\b[^>]*/>\r?$")
_GUIDELINE_RE = re.compile(rb"^[ \t]*<guideline\b[^>]*/>\r?$")


def _guideline_as_fontra(line: bytes):
    """A ``<guideline>`` as Fontra reads it (name, x, y, angle; identifier
    and color dropped, missing coordinates as 0), to tell a guideline Hive
    left alone from one it changed. Fontra writes a vertical guideline
    (``x`` only) back with ``y="0" angle="0"``: the original line is kept."""
    import xml.etree.ElementTree as ET

    try:
        attrs = ET.fromstring(line.strip()).attrib
        return (
            attrs.get("name"),
            float(attrs.get("x", 0)),
            float(attrs.get("y", 0)),
            float(attrs.get("angle", 0)) % 360,
        )
    except (ET.ParseError, ValueError):
        return None


def restore_glif_details(old: bytes, new: bytes, codepoints_changed=()) -> bytes:
    """Put back what Fontra's backend does not keep in a ``.glif``: the
    ``<unicode>`` elements of a master (it writes the default master's in
    every master) unless Hive changed the glyph's code points, and the
    guidelines Hive did not change (Fontra drops their identifiers and
    colors)."""
    old_lines = old.split(b"\n")
    new_lines = new.split(b"\n")
    try:
        name = _glif_content(new)[0].get("name")
    except Exception:
        name = None

    if name not in codepoints_changed:
        old_unicodes = [line for line in old_lines if _UNICODE_RE.match(line)]
        new_unicodes = [
            i for i, line in enumerate(new_lines) if _UNICODE_RE.match(line)
        ]
        if [new_lines[i].strip() for i in new_unicodes] != [
            line.strip() for line in old_unicodes
        ]:
            if new_unicodes:
                at = new_unicodes[0]
                for i in reversed(new_unicodes):
                    del new_lines[i]
            else:
                at = next(
                    (
                        i + 1
                        for i, line in enumerate(new_lines)
                        if line.lstrip().startswith(b"<advance")
                    ),
                    next(
                        (
                            i + 1
                            for i, line in enumerate(new_lines)
                            if line.lstrip().startswith(b"<glyph")
                        ),
                        1,
                    ),
                )
            new_lines[at:at] = old_unicodes

    old_guides = [line for line in old_lines if _GUIDELINE_RE.match(line)]
    new_guides = [i for i, line in enumerate(new_lines) if _GUIDELINE_RE.match(line)]
    if len(old_guides) == len(new_guides):
        for old_line, i in zip(old_guides, new_guides):
            if _guideline_as_fontra(old_line) == _guideline_as_fontra(new_lines[i]):
                new_lines[i] = old_line  # unchanged in Hive: keep it whole
    return b"\n".join(new_lines)


def detect_font_paths(store: GitRepoStore, remote_sha: str) -> list[str]:
    """Candidate font locations in a fetched remote commit, for the
    "Connect a repository" form: ``.fontra`` packages, designspaces, and
    UFOs not used by a designspace of the same folder."""
    paths = set(_tree_entries(store, remote_sha))
    found = set()
    if "font-data.json" in paths:
        found.add("")
    designspace_dirs = set()
    for path in paths:
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.lower().endswith(".fontra"):
                found.add("/".join(parts[: i + 1]))
                break
        if path.lower().endswith(".designspace"):
            found.add(path)
            designspace_dirs.add(path.rpartition("/")[0])
    for path in paths:
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.lower().endswith(".ufo"):
                folder = "/".join(parts[:i])
                if folder not in designspace_dirs:
                    found.add("/".join(parts[: i + 1]))
                break
    return sorted(found)


__all__ = [
    "NotMergedError",
    "PullResult",
    "PushResult",
    "Remote",
    "RemoteError",
    "RemoteMovedError",
    "RemoteStatus",
    "UnsafeURLError",
    "check_url",
    "detect_font_paths",
    "match_formatting",
    "pull",
    "push",
    "remote_as_fontra",
    "remote_head",
    "restore_glif_details",
    "same_content",
    "status",
    "upstream_state",
]
