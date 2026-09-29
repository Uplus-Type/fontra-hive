"""Development project manager: serve git-backed projects without authentication.

Usage::

    fontra hive-dev /path/to/repos

where ``/path/to/repos`` contains one bare repository per project
(``<name>.git``). Project identifiers are ``<name>@<branch>``; a bare
``<name>`` means the default branch.

This manager is for local development and tests. The Hive service will use a
``HiveProjectManager`` that checks a JWT and asks hive-api for the user's role
on the project (see the architecture document); the git side is the same.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import logging
import pathlib
from importlib import resources
from types import SimpleNamespace
from typing import Any

from aiohttp import web
from fontra.backends.filenames import stringToFileName
from fontra.core.fonthandler import FontHandler
from fontra.core.protocols import ProjectManager

from .access import Access, DevDirectory, token_for, username_from_token
from .backend_git import GitFontraBackend
from .fonthandler import HiveFontHandler
from .gitstore import (
    DEFAULT_BRANCH,
    SERVER_SIGNATURE,
    GitRepoStore,
    RefMovedError,
    Signature,
    SnapshotInfo,
)

logger = logging.getLogger(__name__)


class DevHiveProjectManagerFactory:
    @staticmethod
    def addArguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "root",
            type=pathlib.Path,
            help="Folder containing <name>.git bare repositories",
        )
        parser.add_argument("--read-only", action="store_true")
        parser.add_argument("--author-name", default="Fontra Hive dev")
        parser.add_argument("--author-email", default="dev@fontrahive.local")
        parser.add_argument("--commit-delay", type=float, default=2.0, help="seconds")
        parser.add_argument(
            "--users",
            type=pathlib.Path,
            help="JSON file of users, organizations and project roles (default: "
            "<root>/hive-dev-users.json if it exists; without it, no login and "
            "everyone may edit everything)",
        )

    @staticmethod
    def getProjectManager(arguments: SimpleNamespace) -> ProjectManager:
        root = arguments.root.resolve()
        usersPath = getattr(arguments, "users", None) or root / DEV_USERS_FILE
        _logAccounts(root, usersPath)
        return DevHiveProjectManager(
            rootPath=root,
            readOnly=arguments.read_only,
            author=Signature(arguments.author_name, arguments.author_email),
            commitDelay=arguments.commit_delay,
            directory=DevDirectory(usersPath) if usersPath.exists() else None,
        )


DEV_USERS_FILE = "hive-dev-users.json"


def _logAccounts(root: pathlib.Path, usersPath: pathlib.Path) -> None:
    """Say at startup whether accounts are on: a misnamed users file would
    otherwise silently leave the server without sign-in."""
    if usersPath.exists():
        directory = DevDirectory(usersPath)
        logger.info(
            "Hive accounts: on (%s: %d users, %d projects)",
            usersPath,
            len(directory.users()),
            len(directory.data.get("projects", {})),
        )
        return
    logger.info("Hive accounts: off (no %s)", usersPath)
    lookalikes = sorted(p.name for p in root.glob("*users*.json"))
    if lookalikes:
        logger.warning(
            "Hive accounts: found %s in %s, but only %s is read: rename it to enable accounts",
            ", ".join(lookalikes),
            root,
            DEV_USERS_FILE,
        )


DEV_USER_COOKIE = "hive-dev-user"


def glyphPath(glyphName: str) -> str:
    return f"glyphs/{stringToFileName(glyphName)}.json"


def splitProjectIdentifier(identifier: str) -> tuple[str, str]:
    name, _, branch = identifier.partition("@")
    return name, branch or DEFAULT_BRANCH


class DevHiveProjectManager:
    def __init__(
        self,
        rootPath: pathlib.Path,
        *,
        readOnly: bool = False,
        author: Signature | None = None,
        commitDelay: float = 2.0,
        directory: DevDirectory | None = None,
    ):
        self.rootPath = rootPath
        self.readOnly = readOnly
        self.author = author
        self.commitDelay = commitDelay
        # None: no accounts (single author, everyone may edit everything).
        self.directory = directory
        self.fontHandlers: dict[str, FontHandler] = {}

    async def aclose(self) -> None:
        for fontHandler in list(self.fontHandlers.values()):
            await fontHandler.aclose()

    # --- identity -------------------------------------------------------------

    async def authorize(self, request: web.Request) -> str | None:
        """The token of the request: ``"dev"`` without a directory, else the
        user chosen on the development login page (a plain cookie: this is
        for local development only, there is no password)."""
        if not _sameOrigin(request):
            return None
        if self.directory is None:
            return "dev"
        username = request.cookies.get(DEV_USER_COOKIE)
        if self.directory.user(username) is None:
            return None
        return token_for(username)

    def accessFor(self, token: str | None, projectName: str) -> Access | None:
        """What the holder of ``token`` may do on a project (None: nothing).
        Without a directory everyone is an anonymous admin."""
        if self.directory is None:
            return None
        return self.directory.access(username_from_token(token), projectName)

    async def _require(
        self, request: web.Request, projectName: str, capability: str
    ) -> Access | None:
        """For the Hive routes: raise unless the requester has ``capability``
        on the project. A project the requester cannot read looks absent."""
        if self.directory is None:
            return None
        access = self.accessFor(await self.authorize(request), projectName)
        if access is None or not access.can("read"):
            raise web.HTTPNotFound()
        if not access.can(capability):
            raise web.HTTPForbidden(text=f"{access.role} cannot {capability}")
        return access

    async def rootDocumentHandler(self, request: web.Request) -> web.Response:
        if self.directory is not None and await self.authorize(request) is None:
            return web.Response(
                text=_devLoginPage(self.directory), content_type="text/html"
            )
        htmlPath = resources.files("fontra") / "client" / "landing.html"
        return web.Response(body=htmlPath.read_bytes(), content_type="text/html")

    async def devLoginHandler(self, request: web.Request) -> web.Response:
        form = await request.post()
        username = form.get("user", "")
        if self.directory is None or self.directory.user(username) is None:
            raise web.HTTPBadRequest(text="unknown user")
        response = web.HTTPFound("/")
        response.set_cookie(DEV_USER_COOKIE, username, httponly=True, samesite="Lax")
        raise response

    async def devLogoutHandler(self, request: web.Request) -> web.Response:
        response = web.HTTPFound("/")
        response.del_cookie(DEV_USER_COOKIE)
        raise response

    async def meHandler(self, request: web.Request) -> web.Response:
        """Who is logged in (for the Hive UI in Fontra's views)."""
        if self.directory is None:
            return web.json_response({"user": None, "accounts": False})
        user = self.directory.user(username_from_token(await self.authorize(request)))
        if user is None:
            raise web.HTTPUnauthorized()
        return web.json_response(
            {
                "user": {
                    "username": user.username,
                    "name": user.name,
                    "email": user.email,
                },
                "accounts": True,
            }
        )

    async def accessHandler(self, request: web.Request) -> web.Response:
        """The requester's role and capabilities on a project."""
        name = request.match_info["name"]
        if self._repoPath(name) is None:
            raise web.HTTPNotFound()
        if self.directory is None:
            return web.json_response({"role": None, "capabilities": ["*"]})
        access = await self._require(request, name, "read")
        return web.json_response(access.to_json())

    def _repoPath(self, name: str) -> pathlib.Path | None:
        if not name or "/" in name or name.startswith("."):
            return None
        path = self.rootPath / f"{name}.git"
        return path if path.is_dir() else None

    async def projectAvailable(self, projectIdentifier: str, token: str) -> bool:
        name, branch = splitProjectIdentifier(projectIdentifier)
        repoPath = self._repoPath(name)
        if repoPath is None:
            return False
        if self.directory is not None and self.accessFor(token, name) is None:
            return False
        store = GitRepoStore.open(repoPath)
        try:
            return store.head(branch) is not None
        finally:
            store.close()

    async def getProjectList(self, token: str) -> list[str]:
        projects = []
        for repoPath in sorted(self.rootPath.glob("*.git")):
            if not repoPath.is_dir():
                continue
            name = repoPath.name[: -len(".git")]
            if self.directory is not None and self.accessFor(token, name) is None:
                continue
            store = GitRepoStore.open(repoPath)
            try:
                branches = store.branches()
                if DEFAULT_BRANCH in branches:  # the default branch comes first
                    projects.append(name)
                projects.extend(f"{name}@{b}" for b in branches if b != DEFAULT_BRANCH)
            finally:
                store.close()
        return projects

    async def getRemoteSubject(
        self, projectIdentifier: str, token: str, readOnly: bool = False
    ) -> FontHandler | None:
        name, branch = splitProjectIdentifier(projectIdentifier)
        if self.directory is not None and self.accessFor(token, name) is None:
            return None  # not a member: the connection is refused
        key = f"{name}@{branch}"
        fontHandler = self.fontHandlers.get(key)
        if fontHandler is None:
            repoPath = self._repoPath(name)
            if repoPath is None:
                return None
            store = GitRepoStore.open(repoPath)
            if store.head(branch) is None:
                store.close()
                return None
            backend = GitFontraBackend(
                store,
                branch,
                author=self.author,
                commit_delay=self.commitDelay,
                read_only=self.readOnly or readOnly,
            )

            async def closeFontHandler():
                logger.info("closing FontHandler for %r", key)
                self.fontHandlers.pop(key, None)
                assert fontHandler is not None
                await fontHandler.aclose()
                store.close()

            logger.info("new FontHandler for %r (head %s)", key, backend.head)
            fontHandler = HiveFontHandler(
                backend=backend,
                projectIdentifier=key,
                metaInfoProvider=self,
                # With accounts, read-only is decided per connection by the
                # role; the handler itself is shared by everyone.
                readOnly=self.readOnly or (readOnly and self.directory is None),
                allConnectionsClosedCallback=closeFontHandler,
                accessForToken=(
                    (lambda token, name=name: self.accessFor(token, name))
                    if self.directory is not None
                    else None
                ),
            )
            await fontHandler.startTasks()
            self.fontHandlers[key] = fontHandler
        return fontHandler

    def setupWebRoutes(self, server) -> None:
        server.httpApp.add_routes(
            [
                # Registered before Fontra adds its own route for the
                # "fontra.webcontent" entry point, so this one answers.
                web.post("/hive/dev-login", self.devLoginHandler),
                web.get("/hive/logout", self.devLogoutHandler),
                web.get("/hive/{path:.*}", self.clientFileHandler),
                web.get("/api/hive/me", self.meHandler),
                web.get("/api/hive/projects/{name}/access", self.accessHandler),
                web.get("/api/hive/projects/{name}/branches", self.branchesHandler),
                web.get("/api/hive/projects/{name}/log", self.logHandler),
                web.get("/api/hive/projects/{name}/glyph", self.glyphHandler),
                web.get("/api/hive/projects/{name}/head", self.headHandler),
                web.post("/api/hive/projects/{name}/restore", self.restoreHandler),
                web.get("/api/hive/projects/{name}/snapshots", self.snapshotsHandler),
                web.post("/api/hive/projects/{name}/snapshot", self.snapshotHandler),
            ]
        )

    async def clientFileHandler(self, request: web.Request) -> web.Response:
        """The editor plug-in's files, always revalidated by the browser.

        Fontra's own static handler sends ``Last-Modified`` (server start) and
        no ``Cache-Control``: browsers then reuse a cached ``init.js`` without
        asking, and a reload does not refresh a module the editor loads with
        ``import()``, so a new plug-in version may never show. Here every
        request is revalidated (``no-cache``) against an ETag of the content:
        a 304 when nothing changed, the new file as soon as it did.
        """
        parts = request.match_info.get("path", "").split("/")
        if not parts or any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            raise web.HTTPNotFound()
        contentType = CLIENT_CONTENT_TYPES.get(parts[-1].rsplit(".", 1)[-1].lower())
        if contentType is None:
            raise web.HTTPNotFound()
        resource = resources.files("fontra_hive").joinpath("client", *parts)
        try:
            data = resource.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise web.HTTPNotFound()
        etag = '"' + hashlib.sha1(data).hexdigest()[:20] + '"'
        headers = {"Cache-Control": "no-cache", "ETag": etag}
        if request.headers.get("If-None-Match") == etag:
            raise web.HTTPNotModified(headers=headers)
        return web.Response(body=data, content_type=contentType, headers=headers)

    async def restoreHandler(self, request: web.Request) -> web.Response:
        """Bring one glyph back to the state it had at ``ref``, as a new commit.

        History is never rewritten: the old glyph file is committed again on
        top of the branch. If the project is open in the editor, the running
        backend picks the commit up as an external change and every connected
        client reloads the glyph.
        """
        name = request.match_info["name"]
        repoPath = self._repoPath(name)
        glyphName = request.query.get("glyph")
        ref = request.query.get("ref")
        branch = request.query.get("branch", DEFAULT_BRANCH)
        if repoPath is None:
            raise web.HTTPNotFound()
        if not glyphName or not ref:
            raise web.HTTPBadRequest(text="glyph and ref are required")
        if self.readOnly:
            raise web.HTTPForbidden(text="read-only server")
        access = await self._require(request, name, "edit")

        fontHandler = self.fontHandlers.get(f"{name}@{branch}")
        backend = fontHandler.backend if fontHandler is not None else None
        store = backend.store if backend is not None else GitRepoStore.open(repoPath)
        path = glyphPath(glyphName)
        try:
            head = store.head(branch)
            if head is None:
                raise web.HTTPNotFound(text=f"no branch {branch}")
            try:
                sha = store.resolve(ref)
                data = store.read_file(sha, path)
            except KeyError:
                raise web.HTTPNotFound(text=f"{glyphName} does not exist at {ref}")
            if backend is not None:
                backend.flush()  # commit pending edits first, so nothing is lost
                head = store.head(branch)
            try:
                current = store.read_file(head, path)
            except KeyError:
                current = None
            if current == data:
                return web.json_response(
                    {"branch": branch, "head": head, "restored": sha, "changed": False}
                )
            message = (
                f"Restore {glyphName} to {sha[:10]}\n\n"
                f"Hive-Glyphs: {glyphName}\nHive-Restore: {sha}\n"
            )
            newHead = store.commit(
                {path: data},
                branch=branch,
                message=message,
                author=_author(access, self.author),
                expected_head=head,
            )
            if backend is not None:
                # Same code path as a commit made by someone else: the glyph is
                # reloaded from git and every connected editor is notified.
                await backend.check_external_changes()
        finally:
            if backend is None:
                store.close()
        return web.json_response(
            {"branch": branch, "head": newHead, "restored": sha, "changed": True}
        )

    async def headHandler(self, request: web.Request) -> web.Response:
        """The current commit of a branch: cheap to poll."""
        repoPath = self._repoPath(request.match_info["name"])
        if repoPath is None:
            raise web.HTTPNotFound()
        await self._require(request, request.match_info["name"], "read")
        branch = request.query.get("branch", DEFAULT_BRANCH)
        store = GitRepoStore.open(repoPath)
        try:
            head = store.head(branch)
        finally:
            store.close()
        if head is None:
            raise web.HTTPNotFound()
        return web.json_response({"branch": branch, "head": head})

    async def branchesHandler(self, request: web.Request) -> web.Response:
        repoPath = self._repoPath(request.match_info["name"])
        if repoPath is None:
            raise web.HTTPNotFound()
        await self._require(request, request.match_info["name"], "read")
        store = GitRepoStore.open(repoPath)
        try:
            data = {b: store.head(b) for b in store.branches()}
        finally:
            store.close()
        return web.json_response({"branches": data, "tags": []})

    async def logHandler(self, request: web.Request) -> web.Response:
        repoPath = self._repoPath(request.match_info["name"])
        if repoPath is None:
            raise web.HTTPNotFound()
        await self._require(request, request.match_info["name"], "read")
        branch = request.query.get("branch", DEFAULT_BRANCH)
        path = request.query.get("path")
        glyphName = request.query.get("glyph")
        if glyphName and not path:
            path = glyphPath(glyphName)
        limit = min(int(request.query.get("limit", "50")), 500)
        store = GitRepoStore.open(repoPath)
        try:
            head = store.head(branch)
            if head is None:
                raise web.HTTPNotFound()
            snapshots: list[SnapshotInfo] = []
            commits = [
                dataclasses.asdict(c)
                for c in store.log(
                    branch, path=path, glyph=glyphName, limit=limit, snapshots=snapshots
                )
            ]
        finally:
            store.close()
        return web.json_response(
            {
                "branch": branch,
                "head": head,
                "path": path,
                "commits": commits,
                # The snapshots met while walking back to the oldest commit
                # returned, newest first; each commit names its snapshot.
                "snapshots": [_snapshotJSON(s) for s in snapshots],
            }
        )

    async def snapshotsHandler(self, request: web.Request) -> web.Response:
        """The snapshots of a branch, newest first, and how many commits were
        made since the latest one (what a new snapshot would group)."""
        repoPath = self._repoPath(request.match_info["name"])
        if repoPath is None:
            raise web.HTTPNotFound()
        await self._require(request, request.match_info["name"], "read")
        branch = request.query.get("branch", DEFAULT_BRANCH)
        store = GitRepoStore.open(repoPath)
        try:
            head = store.head(branch)
            if head is None:
                raise web.HTTPNotFound()
            snapshots = store.snapshots(head)
            base = snapshots[0].sha if snapshots else None
            pending = store.commits_between(base, head)
        finally:
            store.close()
        return web.json_response(
            {
                "branch": branch,
                "head": head,
                "snapshots": [_snapshotJSON(s) for s in snapshots],
                "pending": len(pending),
                "pendingAuthors": sorted({c.author for c in pending}),
            }
        )

    async def snapshotHandler(self, request: web.Request) -> web.Response:
        """Name the current state of a branch: groups the commits made since
        the previous snapshot. Adds an empty commit and a ``snapshot/<name>``
        tag; history is never rewritten."""
        name = request.match_info["name"]
        repoPath = self._repoPath(name)
        title = (request.query.get("name") or "").strip()
        branch = request.query.get("branch", DEFAULT_BRANCH)
        if repoPath is None:
            raise web.HTTPNotFound()
        if not title:
            raise web.HTTPBadRequest(text="name is required")
        if self.readOnly:
            raise web.HTTPForbidden(text="read-only server")
        access = await self._require(request, name, "snapshot")

        fontHandler = self.fontHandlers.get(f"{name}@{branch}")
        backend = fontHandler.backend if fontHandler is not None else None
        store = backend.store if backend is not None else GitRepoStore.open(repoPath)
        try:
            if store.head(branch) is None:
                raise web.HTTPNotFound(text=f"no branch {branch}")
            if backend is not None:
                backend.flush()  # the snapshot includes edits not yet committed
            try:
                snapshot = store.create_snapshot(
                    title,
                    branch=branch,
                    author=_author(access, self.author),
                    expected_head=store.head(branch),
                )
            except (ValueError, RefMovedError) as error:
                raise web.HTTPConflict(text=str(error))
            if backend is not None:
                # The tree did not change: this only moves the backend's head.
                await backend.check_external_changes()
        finally:
            if backend is None:
                store.close()
        return web.json_response(
            {
                "branch": branch,
                "head": snapshot.sha,
                "snapshot": _snapshotJSON(snapshot),
            }
        )

    async def glyphHandler(self, request: web.Request) -> web.Response:
        """The JSON of one glyph at a given ref (commit sha, branch or tag)."""
        repoPath = self._repoPath(request.match_info["name"])
        glyphName = request.query.get("glyph")
        if repoPath is None or not glyphName:
            raise web.HTTPNotFound()
        await self._require(request, request.match_info["name"], "read")
        ref = request.query.get("ref", DEFAULT_BRANCH)
        store = GitRepoStore.open(repoPath)
        try:
            try:
                data = store.read_file(store.resolve(ref), glyphPath(glyphName))
            except KeyError:
                raise web.HTTPNotFound()
        finally:
            store.close()
        return web.Response(body=data, content_type="application/json")

    async def getMetaInfo(
        self, projectIdentifier: str, authorizationToken: str
    ) -> dict[str, Any]:
        name, branch = splitProjectIdentifier(projectIdentifier)
        return {
            "projectName": name if branch == DEFAULT_BRANCH else f"{name} · {branch}",
            "projectIdentifier": projectIdentifier,
            "branch": branch,
        }

    async def putMetaInfo(
        self, projectIdentifier: str, metaInfo: dict[str, Any], authorizationToken: str
    ) -> None:
        pass


CLIENT_CONTENT_TYPES = {
    "js": "text/javascript",
    "json": "application/json",
    "svg": "image/svg+xml",
    "css": "text/css",
    "html": "text/html",
    "png": "image/png",
}


def _snapshotJSON(snapshot: SnapshotInfo) -> dict[str, Any]:
    data = dataclasses.asdict(snapshot)
    data["glyphs"] = list(snapshot.glyphs)
    return data


def _author(access: Access | None, fallback: Signature | None) -> Signature:
    if access is not None:
        return access.user.signature
    return fallback or SERVER_SIGNATURE


def _sameOrigin(request) -> bool:
    """Refuse cross-site requests (a page on another site opening our
    websocket or posting to our routes with the user's cookies). Requests
    without an Origin header (same-origin GETs, tools) are accepted."""
    origin = getattr(request, "headers", {}).get("Origin")
    if not origin or origin == "null":
        return True
    host = origin.split("://", 1)[-1].rstrip("/")
    return host == getattr(request, "host", host)


def _devLoginPage(directory: DevDirectory) -> str:
    """A minimal login page for local development: pick who you are."""
    from html import escape

    buttons = "\n".join(
        f'<button name="user" value="{escape(u.username)}">'
        f"<b>{escape(u.name)}</b><small>{escape(u.username)} · {escape(u.email)}</small>"
        "</button>"
        for u in directory.users()
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Fontra Hive — sign in (development)</title>
<link rel="stylesheet" href="/css/core.css">
<style>
  body {{ display: flex; flex-direction: column; align-items: center;
          font-family: fontra-ui-regular; }}
  img {{ width: 84px; margin-top: 56px; }}
  h1 {{ font-size: 1.5em; margin: 0.5em 0 0.2em; }}
  p {{ opacity: 0.6; margin: 0 0 1.5em; }}
  form {{ display: flex; flex-direction: column; gap: 0.5em; width: 22em; }}
  button {{ font: inherit; text-align: left; border: none; border-radius: 0.5em;
            padding: 0.7em 1em; background: var(--ui-element-background-color);
            color: var(--ui-element-foreground-color);
            box-shadow: 1px 2px 6px #0002; cursor: pointer; }}
  button:hover {{ outline: 2px solid #46f; }}
  small {{ display: block; opacity: 0.6; }}
</style></head>
<body>
  <img src="/images/fontra-icon.svg" alt="">
  <h1>Fontra Hive</h1>
  <p>Development server: choose who you are (no password).</p>
  <form method="post" action="/hive/dev-login">{buttons}</form>
</body></html>
"""
