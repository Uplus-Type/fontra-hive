"""Fontra Hive, "Try Fontra": Hive's server, run in the visitor's browser.

The same code as on fontrahive.com: Fontra's ``FontHandler`` over Hive's git
backend, and Hive's routes (history, restore, snapshots, comments), through
``DevHiveProjectManager`` without accounts (one author, "You"). Only the
transport changes: the editor's WebSocket messages and the plug-in's
``/api/hive/*`` requests arrive from try-python-worker.js instead of a
network, and the answers go back the same way.

Projects are bare git repositories under /repos (``<name>.git``, the name
being the page's project, "local:<id>"), each kept in the browser's
IndexedDB by the worker (Emscripten's IDBFS), so they survive a reload.

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import os

# Pure-Python versions of aiohttp and its helpers: no compiled code here.
for _name in (
    "AIOHTTP_NO_EXTENSIONS",
    "MULTIDICT_NO_EXTENSIONS",
    "YARL_NO_EXTENSIONS",
    "PROPCACHE_NO_EXTENSIONS",
    "FROZENLIST_NO_EXTENSIONS",
):
    os.environ.setdefault(_name, "1")

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from urllib.parse import parse_qsl, unquote, urlsplit  # noqa: E402

from aiohttp import WSMsgType, web  # noqa: E402
from dulwich.repo import Repo  # noqa: E402
from fontra.core.remote import RemoteObjectConnection  # noqa: E402
from multidict import CIMultiDict, MultiDict  # noqa: E402

from fontra_hive.gitstore import DEFAULT_BRANCH, GitRepoStore, Signature  # noqa: E402
from fontra_hive.projectmanager import DevHiveProjectManager  # noqa: E402

logger = logging.getLogger("hive-try")

ROOT = pathlib.Path("/repos")
AUTHOR = Signature("You", "you@fontrahive.com")
HOST = "fontrahive.try"


def repoPath(name: str) -> pathlib.Path:
    if not re.fullmatch(r"local:[0-9a-f]{12}", name):
        raise ValueError(f"not a local project: {name!r}")
    return ROOT / f"{name}.git"


def _routePattern(path: str) -> re.Pattern:
    """aiohttp's "{name}" / "{name:regex}" route syntax, as a regex."""
    out, pos = "", 0
    for m in re.finditer(r"\{(\w+)(?::([^{}]+))?\}", path):
        out += re.escape(path[pos : m.start()])
        out += f"(?P<{m.group(1)}>{m.group(2) or '[^/]+'})"
        pos = m.end()
    return re.compile(out + re.escape(path[pos:]) + "$")


class _Socket:
    """What RemoteObjectConnection expects of an aiohttp WebSocket: messages
    in (from the page, through the worker), JSON out (back to it)."""

    def __init__(self, send):
        self._send = send
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def feed(self, text: str | None) -> None:
        self._queue.put_nowait(text)

    def __aiter__(self):
        return self

    async def __anext__(self):
        text = await self._queue.get()
        if text is None:
            self.closed = True
            raise StopAsyncIteration
        return SimpleNamespace(
            type=WSMsgType.TEXT, data=text, json=lambda text=text: json.loads(text)
        )

    async def send_json(self, obj) -> None:
        if not self.closed:
            self._send(json.dumps(obj))

    async def close(self) -> None:
        self.closed = True


class _Request:
    """Enough of aiohttp's Request for Hive's route handlers."""

    def __init__(self, method, path, query, body, match_info):
        self.method = method
        self.path = path
        self.path_qs = path + ("?" + query if query else "")
        self.query = MultiDict(parse_qsl(query, keep_blank_values=True))
        self.match_info = match_info
        self.headers = CIMultiDict({"Host": HOST})
        self.host = HOST
        self.cookies = {}
        self._body = body

    async def json(self):
        return json.loads(self._body) if self._body else {}

    async def text(self):
        return self._body or ""

    async def read(self):
        return (self._body or "").encode()


class _Manager(DevHiveProjectManager):
    """Hive's development project manager, with the fonts' own names."""

    names: dict[str, str] = {}

    async def getMetaInfo(self, projectIdentifier, authorizationToken):
        info = await super().getMetaInfo(projectIdentifier, authorizationToken)
        name, _, branch = projectIdentifier.partition("@")
        if name in self.names:
            label = self.names[name]
            onMain = branch in ("", DEFAULT_BRANCH)
            info["projectName"] = label if onMain else f"{label} · {branch}"
        return info


class BrowserHive:
    def __init__(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.manager = _Manager(ROOT, author=AUTHOR, commitDelay=1.0)
        self.routes = [
            (route.method, _routePattern(route.path), route.handler)
            for route in self.manager.projectRoutes()
            if route.path.startswith("/api/hive/")
        ]
        self.sockets: dict[int, _Socket] = {}

    # --- projects ----------------------------------------------------------

    def createProject(self, name: str, package: str) -> str:
        """A new repository for ``name`` holding the .fontra package at
        ``package``; its first commit."""
        path = repoPath(name)
        # The folder is the mount point of the project's storage (the worker
        # mounts it first): empty it, and make the repository right there.
        path.mkdir(parents=True, exist_ok=True)
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        repo = Repo.init_bare(os.fspath(path))
        repo.refs.set_symbolic_ref(b"HEAD", f"refs/heads/{DEFAULT_BRANCH}".encode())
        store = GitRepoStore(repo)
        try:
            return store.import_directory(
                package, message="Opened in Try Fontra", author=AUTHOR
            )
        finally:
            store.close()

    def exportProject(self, name: str, destination: str, ref: str | None = None) -> str:
        """The project's .fontra package, as its branch's head (after the
        edits not yet committed: call flush first)."""
        project, _, branch = name.partition("@")
        store = GitRepoStore.open(repoPath(project))
        try:
            store.export(ref or branch or DEFAULT_BRANCH, destination)
        finally:
            store.close()
        return destination

    async def flush(self) -> None:
        """Commit what the open projects have not committed yet."""
        for handler in list(self.manager.fontHandlers.values()):
            await handler.finishWriting()
            backend = handler.backend
            if hasattr(backend, "flush"):
                result = backend.flush()
                if asyncio.iscoroutine(result):
                    await result

    # --- the editor's WebSocket ---------------------------------------------

    def openSocket(self, socketId: int, send) -> None:
        """Before connect: messages from the page may arrive from now on, and
        wait in line behind the handshake RemoteObjectConnection expects."""
        socket = _Socket(send)
        socket.feed(json.dumps({"client-uuid": f"browser-{socketId}"}))
        self.sockets[socketId] = socket

    def setName(self, name: str, label: str) -> None:
        """The font's name, shown by the editor instead of "local:<id>"."""
        self.manager.names[name] = label

    async def connect(self, socketId: int, project: str) -> None:
        """Serve one WebSocket of the editor (opened with openSocket) until
        the page closes it."""
        socket = self.sockets[socketId]
        try:
            subject = await self.manager.getRemoteSubject(project, "dev")
            if subject is None:
                await socket.send_json({"initialization-error": "no such project"})
                return
            connection = RemoteObjectConnection(
                socket, project, subject, True, authorizationToken="dev"
            )
            async with subject.useConnection(connection):
                await connection.handleConnection()
        finally:
            self.sockets.pop(socketId, None)

    def socketMessage(self, socketId: int, text: str | None) -> None:
        socket = self.sockets.get(socketId)
        if socket is not None:
            socket.feed(text)

    # --- the plug-in's requests ---------------------------------------------

    async def http(self, method: str, url: str, body: str | None) -> dict:
        parts = urlsplit(url)
        path = unquote(parts.path)
        for routeMethod, pattern, handler in self.routes:
            m = pattern.match(path)
            if m is None or routeMethod not in (method, "*"):
                continue
            request = _Request(method, path, parts.query, body, m.groupdict())
            try:
                response = await handler(request)
            except web.HTTPException as error:
                return {
                    "status": error.status,
                    "contentType": "text/plain",
                    "body": error.text or error.reason,
                    "headers": {},
                }
            return {
                "status": response.status,
                "contentType": response.content_type,
                "body": _body(response),
                "headers": {
                    k: v
                    for k, v in response.headers.items()
                    if k.lower() in ("etag", "content-disposition", "location")
                },
            }
        return {"status": 404, "contentType": "text/plain", "body": "Not Found"}


def _body(response) -> str:
    body = response.body
    if body is None:
        return getattr(response, "text", None) or ""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", "replace")
    if hasattr(body, "_value"):  # aiohttp payloads
        value = body._value
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)
    return str(body)
