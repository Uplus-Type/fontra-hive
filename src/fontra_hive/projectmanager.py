"""Development project manager: serve git-backed projects without authentication.

Usage::

    fontra hive-dev /path/to/repos

where ``/path/to/repos`` contains one bare repository per project
(``<name>.git``). Project identifiers are ``<name>@<branch>``; a bare
``<name>`` means the default branch.

This manager is for local development and tests. The Hive service uses
:class:`fontra_hive.hivemanager.HiveProjectManager` (``fontra hive``), a
subclass that checks hive-api's access token and asks hive-api for roles;
the git side is the same.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import logging
import time
from functools import partial
from urllib.parse import quote
import pathlib
import re
import shutil
import sys
import tempfile
from importlib import resources
from types import SimpleNamespace
from typing import Any

from aiohttp import web
from fontra.backends.filenames import stringToFileName
from fontra.core.fonthandler import FontHandler
from fontra.core.protocols import ProjectManager

from .access import ROLES, Access, DevDirectory, token_for, username_from_token
from .backend_git import GitFontraBackend
from .fonthandler import HiveFontHandler
from .glyphdiff import changed_sources, font_source_names
from .gitstore import (
    ARCHIVE_TAG_PREFIX,
    DEFAULT_BRANCH,
    SERVER_SIGNATURE,
    GitRepoStore,
    GlyphSnapshotInfo,
    RefMovedError,
    hive_glyphs,
    Signature,
    SnapshotInfo,
)

logger = logging.getLogger(__name__)

# The font's history names the sources changed in commits touching at most
# this many glyphs (a larger change is read as "many").
MAX_GLYPHS_FOR_SOURCES = 20


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

# Fontra's view pages, served by Hive with its script added (see viewHandler).
FONTRA_VIEWS = ("editor", "fontoverview", "fontinfo", "applicationsettings")
PRESENCE_TIMEOUT = 20.0  # seconds without a heartbeat before someone is gone


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
        # project name -> client id -> presence entry (see presenceHandler)
        self.presence: dict[str, dict[str, dict]] = {}

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

    async def _project(
        self, request: web.Request, name: str, capability: str
    ) -> tuple[pathlib.Path, Access | None]:
        """For the Hive routes: the project's repository, and the requester's
        access, which must include ``capability``."""
        repoPath = self._repoPath(name)
        if repoPath is None:
            raise web.HTTPNotFound()
        return repoPath, await self._require(request, name, capability)

    async def rootDocumentHandler(self, request: web.Request) -> web.Response:
        if self.directory is not None and await self.authorize(request) is None:
            return _htmlResponse(
                _devLoginPage(
                    self.directory, getattr(request, "query", {}).get("ref", "/")
                )
            )
        text = self.fontraClientFile("landing.html").read_text(encoding="utf-8")
        return _htmlResponse(injectHiveScripts(text))

    def fontraClientFile(self, fileName: str):
        """One of Fontra's built client files (a page of the editor)."""
        return resources.files("fontra") / "client" / fileName

    async def viewHandler(self, request: web.Request, *, view: str) -> web.Response:
        """Fontra's own view page, with the Hive script added. With accounts,
        a visitor who is not signed in goes to the sign-in page first and
        comes back here afterwards."""
        if self.directory is not None and await self.authorize(request) is None:
            ref = quote(str(getattr(request, "path_qs", f"/{view}.html")), safe="")
            raise web.HTTPFound(f"/?ref={ref}")
        try:
            text = self.fontraClientFile(f"{view}.html").read_text(encoding="utf-8")
        except FileNotFoundError:
            raise web.HTTPNotFound()
        return _htmlResponse(injectHiveScripts(text))

    async def devLoginHandler(self, request: web.Request) -> web.Response:
        form = await request.post()
        username = form.get("user", "")
        if self.directory is None or self.directory.user(username) is None:
            raise web.HTTPBadRequest(text="unknown user")
        response = web.HTTPFound(_safeRef(form.get("ref")))
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
        _, access = await self._project(request, name, "read")
        if access is None:
            return web.json_response({"role": None, "capabilities": ["*"]})
        return web.json_response(access.to_json())

    def _repoPath(self, name: str) -> pathlib.Path | None:
        if not name or "/" in name or name.startswith("."):
            return None
        path = self.rootPath / f"{name}.git"
        return path if path.is_dir() else None

    async def presenceHandler(self, request: web.Request) -> web.Response:
        """Heartbeat of one open view: who, where (view, branch, glyph).
        Answers with the others currently on the project."""
        name = request.match_info["name"]
        _, access = await self._project(request, name, "read")
        body = await request.json()
        client = str(body.get("client", ""))[:64]
        if not client:
            raise web.HTTPBadRequest(text="client is required")
        user = access.user if access is not None else None
        now = time.monotonic()
        entries = self.presence.setdefault(name, {})
        entries[client] = {
            "username": user.username if user else "dev",
            "name": user.name if user else "Anonymous",
            "role": access.role if access else None,
            "view": str(body.get("view", ""))[:32],
            "branch": str(body.get("branch", ""))[:64],
            "glyph": body.get("glyph") if isinstance(body.get("glyph"), str) else None,
            "seen": now,
        }
        for key in [
            k for k, e in entries.items() if now - e["seen"] > PRESENCE_TIMEOUT
        ]:
            del entries[key]
        others = [
            {k: v for k, v in e.items() if k != "seen"}
            for k, e in sorted(entries.items(), key=lambda item: item[1]["name"])
            if k != client
        ]
        return web.json_response({"others": others})

    async def membersHandler(self, request: web.Request) -> web.Response:
        """Who has a role on the project, and whether the requester may
        change it (in development: outside collaborators in the users file)."""
        name = request.match_info["name"]
        if self._repoPath(name) is None:
            raise web.HTTPNotFound()
        if self.directory is None:
            return web.json_response(
                {"members": [], "canManage": False, "accounts": False}
            )
        access = await self._require(request, name, "read")
        members = self.directory.members(name)
        canManage = access.can("invite")
        memberNames = {m["username"] for m in members}
        users = (
            [
                {"username": u.username, "name": u.name}
                for u in self.directory.users()
                if u.username not in memberNames
            ]
            if canManage
            else []
        )
        return web.json_response(
            {
                "members": members,
                "canManage": canManage,
                "you": access.user.username,
                "users": users,
                "roles": list(ROLES),
                "accounts": True,
            }
        )

    async def setMemberHandler(self, request: web.Request) -> web.Response:
        """Add, change or remove an outside collaborator (managers, admins)."""
        name = request.match_info["name"]
        if self._repoPath(name) is None or self.directory is None:
            raise web.HTTPNotFound()
        await self._require(request, name, "invite")
        body = await request.json()
        try:
            self.directory.set_collaborator(
                name, body.get("username"), body.get("role")
            )
        except KeyError as error:
            raise web.HTTPNotFound(text=str(error))
        except ValueError as error:
            raise web.HTTPConflict(text=str(error))
        return await self.membersHandler(request)

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
        repoPath = self._repoPath(name)
        if repoPath is None:
            return None
        return await self._openFontHandler(
            repoPath,
            branch,
            identifier=f"{name}@{branch}",
            accessForToken=(
                (lambda token, name=name: self.accessFor(token, name))
                if self.directory is not None
                else None
            ),
            readOnly=readOnly,
        )

    @staticmethod
    def _handlerKey(repoPath: pathlib.Path, branch: str) -> str:
        return f"{repoPath.name}@{branch}"

    async def _openFontHandler(
        self,
        repoPath: pathlib.Path,
        branch: str,
        *,
        identifier: str,
        accessForToken,
        readOnly: bool,
    ) -> FontHandler | None:
        """The one shared handler of a project's branch, opened if needed."""
        key = self._handlerKey(repoPath, branch)
        fontHandler = self.fontHandlers.get(key)
        if fontHandler is not None:
            return fontHandler
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
            logger.info("closing FontHandler for %r", identifier)
            self.fontHandlers.pop(key, None)
            assert fontHandler is not None
            await fontHandler.aclose()
            store.close()

        logger.info("new FontHandler for %r (head %s)", identifier, backend.head)
        fontHandler = HiveFontHandler(
            backend=backend,
            projectIdentifier=identifier,
            metaInfoProvider=self,
            # With accounts, read-only is decided per connection by the
            # role; the handler itself is shared by everyone.
            readOnly=self.readOnly or (readOnly and accessForToken is None),
            allConnectionsClosedCallback=closeFontHandler,
            accessForToken=accessForToken,
            # "File › Export as" lists these; Hive's page script turns the
            # choice into a download from /api/hive/projects/<id>/export.
            exportManager=HiveExportManager(),
        )
        await fontHandler.startTasks()
        self.fontHandlers[key] = fontHandler
        return fontHandler

    def setupWebRoutes(self, server) -> None:
        server.httpApp.add_routes(self.webRoutes())

    def webRoutes(self) -> list:
        return [
            web.post("/hive/dev-login", self.devLoginHandler),
            web.get("/hive/logout", self.devLogoutHandler),
            *self.projectRoutes(),
        ]

    def projectRoutes(self) -> list:
        return [
            # Registered before Fontra adds its own route for the
            # "fontra.webcontent" entry point, so this one answers.
            *(
                web.get(f"/{view}.html", partial(self.viewHandler, view=view))
                for view in FONTRA_VIEWS
            ),
            web.get("/hive/{path:.*}", self.clientFileHandler),
            web.get("/favicon.ico", self.faviconHandler),
            web.get("/api/hive/me", self.meHandler),
            web.get("/api/hive/projects/{name}/access", self.accessHandler),
            web.post("/api/hive/projects/{name}/presence", self.presenceHandler),
            web.get("/api/hive/projects/{name}/members", self.membersHandler),
            web.post("/api/hive/projects/{name}/members", self.setMemberHandler),
            web.get("/api/hive/projects/{name}/branches", self.branchesHandler),
            web.post("/api/hive/projects/{name}/branches", self.createBranchHandler),
            web.delete("/api/hive/projects/{name}/branches", self.deleteBranchHandler),
            web.get("/api/hive/projects/{name}/log", self.logHandler),
            web.get("/api/hive/projects/{name}/glyph", self.glyphHandler),
            web.get("/api/hive/projects/{name}/head", self.headHandler),
            web.post("/api/hive/projects/{name}/restore", self.restoreHandler),
            web.get("/api/hive/projects/{name}/snapshots", self.snapshotsHandler),
            web.post("/api/hive/projects/{name}/snapshot", self.snapshotHandler),
            web.post(
                "/api/hive/projects/{name}/glyph-snapshot", self.glyphSnapshotHandler
            ),
            web.get("/api/hive/projects/{name}/export", self.exportHandler),
            web.get("/api/hive/export-formats", self.exportFormatsHandler),
        ]

    async def exportFormatsHandler(self, request: web.Request) -> web.Response:
        """What this server can export: sources always, fonts when it can build them."""
        from . import export

        labels = {**export.SOURCE_FORMATS, **export.FONT_FORMATS}
        return web.json_response(
            {
                "formats": [
                    {"format": f, "label": labels[f][0]}
                    for f in export.supported_formats()
                ]
            }
        )

    async def exportHandler(self, request: web.Request) -> web.StreamResponse:
        """Download a branch of a project: its sources (.fontra or
        .designspace + UFOs, zipped) or, when the server can build them,
        fonts. Needs the "export" capability (managers and admins). Edits not
        yet committed are committed first; the build runs in its own process
        (see export.py), one at a time."""
        from . import export

        name = request.match_info["name"]
        repoPath, _ = await self._project(request, name, "export")
        format = request.query.get("format", "fontra")
        if format not in export.supported_formats():
            raise web.HTTPBadRequest(text=f"Cannot export as {format!r} here.")
        branch = request.query.get("branch", DEFAULT_BRANCH)
        fontHandler = self.fontHandlers.get(self._handlerKey(repoPath, branch))
        if fontHandler is not None:
            fontHandler.backend.flush()
        store = GitRepoStore.open(repoPath)
        try:
            head = store.head(branch)
            if head is None:
                raise web.HTTPNotFound(text=f"No branch {branch!r}.")
            projectName = name.split("/")[-1]
            stem = _safeStem(
                projectName + ("" if branch == DEFAULT_BRANCH else f"-{branch}")
            )
            work = pathlib.Path(
                tempfile.mkdtemp(dir=repoPath.parent, prefix=".export-")
            )
            source = work / "source" / f"{stem}.fontra"
            store.export(head, source)
        finally:
            store.close()
        try:
            async with _exportSlot:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "fontra_hive.export",
                    str(source),
                    str(work),
                    stem,
                    format,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out, err = await asyncio.wait_for(
                        process.communicate(), timeout=EXPORT_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    raise web.HTTPGatewayTimeout(text="The export took too long.")
            if process.returncode != 0:
                logger.error(
                    "export of %s as %s failed: %s", name, format, err.decode()[-2000:]
                )
                lastLine = (err.decode().strip().splitlines() or ["unknown error"])[-1]
                raise web.HTTPInternalServerError(text=f"The export failed: {lastLine}")
            result = pathlib.Path(out.decode().strip().splitlines()[-1])
            response = web.StreamResponse(
                headers={
                    "Content-Type": (
                        "application/zip"
                        if result.suffix == ".zip"
                        else "application/octet-stream"
                    ),
                    "Content-Disposition": f'attachment; filename="{result.name}"',
                    "Content-Length": str(result.stat().st_size),
                    "Cache-Control": "no-store",
                }
            )
            await response.prepare(request)
            with result.open("rb") as file:
                while chunk := file.read(1 << 20):
                    await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def faviconHandler(self, request: web.Request) -> web.Response:
        """/favicon.ico, which browsers ask for on every site."""
        data = resources.files("fontra_hive").joinpath("client", "icons", "favicon.ico")
        return web.Response(
            body=data.read_bytes(),
            content_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=86400"},
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
        glyphName = request.query.get("glyph")
        ref = request.query.get("ref")
        branch = request.query.get("branch", DEFAULT_BRANCH)
        repoPath, access = await self._project(request, name, "read")
        if not glyphName or not ref:
            raise web.HTTPBadRequest(text="glyph and ref are required")
        if self.readOnly:
            raise web.HTTPForbidden(text="read-only server")
        access = await self._require(request, name, "edit")

        fontHandler = self.fontHandlers.get(self._handlerKey(repoPath, branch))
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
        repoPath, _ = await self._project(request, request.match_info["name"], "read")
        branch = request.query.get("branch", DEFAULT_BRANCH)
        store = GitRepoStore.open(repoPath)
        try:
            head = store.head(branch)
        finally:
            store.close()
        if head is None:
            raise web.HTTPNotFound()
        return web.json_response({"branch": branch, "head": head})

    # --- branches -------------------------------------------------------------

    async def _defaultBranch(self, request: web.Request, name: str) -> str:
        """The project's default branch (hive-api knows it; "main" here)."""
        return DEFAULT_BRANCH

    def _aheadBehind(self, store: GitRepoStore, head: str, base: str):
        """``store.ahead_behind``, cached by pair of commits (the answer for
        two given commits never changes)."""
        cache = self.__dict__.setdefault("_aheadBehindCache", {})
        if len(cache) > 5000:
            cache.clear()
        key = (head, base)
        if key not in cache:
            cache[key] = store.ahead_behind(head, base)
        return cache[key]

    def _branchJSON(
        self, store: GitRepoStore, branch: str, default: str, info: dict
    ) -> dict:
        head = store.head(branch)
        commit = store.commit_info(head)
        defaultHead = store.head(default)
        if branch == default or defaultHead is None:
            ahead, behind = 0, 0
        else:
            ahead, behind = self._aheadBehind(store, head, defaultHead)
        meta = info.get(branch) or {}
        return {
            "name": branch,
            "head": head,
            "isDefault": branch == default,
            # The latest change: when, by whom, what.
            "time": commit.time,
            "author": commit.author,
            "message": commit.message.split("\n", 1)[0],
            # Compared with the default branch.
            "ahead": ahead,
            "behind": behind,
            "merged": branch != default and ahead == 0,
            "createdBy": meta.get("createdBy"),
            "createdByName": meta.get("createdByName"),
            "created": meta.get("created"),
            "from": meta.get("from"),
            # Someone has it open in the editor (on this server).
            "open": any(
                key == self._handlerKey(store.path, branch) for key in self.fontHandlers
            ),
        }

    async def branchesHandler(self, request: web.Request) -> web.Response:
        """The branches of a project, the default one first, then the most
        recently changed; each compared with the default branch. Also what
        the requester may do with them."""
        name = request.match_info["name"]
        repoPath, access = await self._project(request, name, "read")
        default = await self._defaultBranch(request, name)
        store = GitRepoStore.open(repoPath)
        try:
            info = store.branch_info()
            branches = [
                self._branchJSON(store, b, default, info) for b in store.branches()
            ]
        finally:
            store.close()
        branches.sort(key=lambda b: (not b["isDefault"], -b["time"], b["name"]))
        can = (lambda cap: access.can(cap)) if access is not None else (lambda _: True)
        return web.json_response(
            {
                "default": default,
                "branches": branches,
                "you": access.user.username if access is not None else None,
                "can": {
                    "create": can("branch") and not self.readOnly,
                    # Delete any branch (else only those one made), merge.
                    "delete": can("merge") and not self.readOnly,
                    "merge": can("merge") and not self.readOnly,
                },
            }
        )

    async def createBranchHandler(self, request: web.Request) -> web.Response:
        """A new branch ``name``, starting at ``from`` (a branch, a snapshot
        tag or a commit; the default branch if not given). Pending edits of
        the branch it starts from are committed first: the new branch starts
        from what people see."""
        name = request.match_info["name"]
        branch = (request.query.get("name") or "").strip()
        repoPath, access = await self._project(request, name, "branch")
        if self.readOnly:
            raise web.HTTPForbidden(text="read-only server")
        default = await self._defaultBranch(request, name)
        fromRef = (request.query.get("from") or "").strip() or default

        fontHandler = self.fontHandlers.get(self._handlerKey(repoPath, fromRef))
        backend = fontHandler.backend if fontHandler is not None else None
        store = backend.store if backend is not None else GitRepoStore.open(repoPath)
        try:
            if branch in store.branches():
                raise web.HTTPConflict(text=f"There is already a branch {branch!r}.")
            error = store.branch_name_error(branch)
            if error:
                raise web.HTTPBadRequest(text=error)
            if backend is not None:
                backend.flush()
            try:
                sha = store.resolve(fromRef)
            except KeyError:
                raise web.HTTPNotFound(
                    text=f"Nothing called {fromRef!r} to start from."
                )
            try:
                store.create_branch(branch, sha)
            except ValueError as error:
                raise web.HTTPConflict(text=str(error))
            user = access.user if access is not None else None
            store.set_branch_info(
                branch,
                {
                    "createdBy": user.username if user else None,
                    "createdByName": user.name if user else None,
                    "created": int(time.time()),
                    "from": fromRef,
                    "base": sha,
                },
                author=_author(access, self.author),
            )
            data = self._branchJSON(store, branch, default, store.branch_info())
        finally:
            if backend is None:
                store.close()
        return web.json_response({"branch": data})

    async def deleteBranchHandler(self, request: web.Request) -> web.Response:
        """Delete a branch (``branch``): managers and admins, or whoever made
        it. Never the default branch, nor a branch open in the editor. A
        branch that was not merged is kept as an ``archive/<name>`` tag, so
        nothing is lost."""
        name = request.match_info["name"]
        branch = request.query.get("branch") or ""
        repoPath, access = await self._project(request, name, "branch")
        if self.readOnly:
            raise web.HTTPForbidden(text="read-only server")
        default = await self._defaultBranch(request, name)
        if branch == default:
            raise web.HTTPConflict(text="The default branch cannot be deleted.")
        if self._handlerKey(repoPath, branch) in self.fontHandlers:
            raise web.HTTPConflict(
                text=f"Someone has {branch} open: it can be deleted once it is closed."
            )
        store = GitRepoStore.open(repoPath)
        try:
            # Only a listed branch: the name goes into a reference path.
            if branch not in store.branches():
                raise web.HTTPNotFound(text=f"No branch {branch!r}.")
            head = store.head(branch)
            meta = store.branch_info().get(branch) or {}
            if access is not None and not access.can("merge"):
                if meta.get("createdBy") != access.user.username:
                    raise web.HTTPForbidden(
                        text="Only managers, admins and whoever made a branch can delete it."
                    )
            defaultHead = store.head(default)
            merged = (
                defaultHead is not None
                and self._aheadBehind(store, head, defaultHead)[0] == 0
            )
            archived = None
            if not merged:
                who = access.user.name if access is not None else "someone"
                existing = set(store.tags())
                tag = f"{ARCHIVE_TAG_PREFIX}{branch}"
                n = 2
                while tag in existing:
                    tag = f"{ARCHIVE_TAG_PREFIX}{branch}-{n}"
                    n += 1
                store.create_tag(
                    tag,
                    head,
                    message=f"Branch {branch}, deleted by {who} before being merged",
                    tagger=_author(access, self.author),
                )
                archived = tag
            store.delete_branch(branch, default)
            store.set_branch_info(branch, None, author=_author(access, self.author))
        finally:
            store.close()
        return web.json_response(
            {"deleted": branch, "head": head, "merged": merged, "archived": archived}
        )

    async def logHandler(self, request: web.Request) -> web.Response:
        repoPath, _ = await self._project(request, request.match_info["name"], "read")
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
            # A glyph's own snapshots group its versions; the font's
            # snapshots are then landmarks between them ("order").
            glyphSnapshots = store.glyph_snapshots(glyphName) if glyphName else None
            order: list[tuple[str, str]] = []
            commits = [
                dataclasses.asdict(c)
                for c in store.log(
                    branch,
                    path=path,
                    glyph=glyphName,
                    limit=limit,
                    snapshots=snapshots,
                    glyph_snapshots=glyphSnapshots,
                    order=order,
                )
            ]
            if glyphName:
                self._addChangedSources(store, commits, path)
            else:
                self._addChangedSourcesOfFont(store, commits)
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
                "glyphSnapshots": [_snapshotJSON(s) for s in glyphSnapshots or ()],
                "order": [list(entry) for entry in order],
            }
        )

    def _sourcesCache(self) -> dict:
        cache = self.__dict__.setdefault("_changedSourcesCache", {})
        if len(cache) > 20000:
            cache.clear()
        return cache

    def _fontSources(self, store: GitRepoStore, commits: list[dict]):
        """The font's source names, as of the newest commit listed, and the
        blob they come from (part of the cache keys)."""
        cache = self._sourcesCache()
        fontData = (
            store.file_sha(commits[0]["sha"], "font-data.json") if commits else None
        )
        if ("font", fontData) not in cache:
            cache[("font", fontData)] = font_source_names(
                store.read_blob(fontData) if fontData else None
            )
        return fontData, cache[("font", fontData)]

    def _sourcesBetween(self, store, old, new, fontData, fontSources) -> list[str]:
        if new is None or old is None or new == old:
            return []
        cache = self._sourcesCache()
        key = (old, new, fontData)
        if key not in cache:
            cache[key] = changed_sources(
                store.read_blob(old), store.read_blob(new), fontSources
            )
        return cache[key]

    def _addChangedSources(self, store: GitRepoStore, commits: list[dict], path: str):
        """Each version of a glyph gets ``sources``: the names of the glyph's
        sources it changed (compared with the version before it). Cached by
        pair of file versions: the list is reloaded at every change."""
        fontData, fontSources = self._fontSources(store, commits)
        blobs = [store.file_sha(c["sha"], path) for c in commits]
        if commits:
            parents = commits[-1]["parents"]
            blobs.append(store.file_sha(parents[0], path) if parents else None)
        for i, commit in enumerate(commits):
            commit["sources"] = self._sourcesBetween(
                store, blobs[i + 1], blobs[i], fontData, fontSources
            )

    def _addChangedSourcesOfFont(self, store: GitRepoStore, commits: list[dict]):
        """The same for the font's history: the sources changed in the glyphs a
        commit lists (``Hive-Glyphs:``; not for imports or large changes)."""
        fontData, fontSources = self._fontSources(store, commits)
        for commit in commits:
            glyphs = hive_glyphs(commit["message"]) or ()
            parents = commit["parents"]
            sources: list[str] = []
            if parents and len(glyphs) <= MAX_GLYPHS_FOR_SOURCES:
                for glyphName in sorted(glyphs):
                    path = glyphPath(glyphName)
                    sources += self._sourcesBetween(
                        store,
                        store.file_sha(parents[0], path),
                        store.file_sha(commit["sha"], path),
                        fontData,
                        fontSources,
                    )
            commit["sources"] = list(dict.fromkeys(sources))

    async def snapshotsHandler(self, request: web.Request) -> web.Response:
        """The snapshots of a branch, newest first, and how many commits were
        made since the latest one (what a new snapshot would group)."""
        repoPath, _ = await self._project(request, request.match_info["name"], "read")
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
        title = (request.query.get("name") or "").strip()
        branch = request.query.get("branch", DEFAULT_BRANCH)
        repoPath, access = await self._project(request, name, "read")
        if not title:
            raise web.HTTPBadRequest(text="name is required")
        if self.readOnly:
            raise web.HTTPForbidden(text="read-only server")
        access = await self._require(request, name, "snapshot")

        fontHandler = self.fontHandlers.get(self._handlerKey(repoPath, branch))
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

    async def glyphSnapshotHandler(self, request: web.Request) -> web.Response:
        """Name the current version of one glyph (``glyph``): groups its
        versions made since its previous glyph snapshot. A tag only: the
        branch does not move, and the font's history is not touched."""
        name = request.match_info["name"]
        title = (request.query.get("name") or "").strip()
        glyphName = request.query.get("glyph")
        branch = request.query.get("branch", DEFAULT_BRANCH)
        repoPath, access = await self._project(request, name, "read")
        if not title or not glyphName:
            raise web.HTTPBadRequest(text="glyph and name are required")
        if self.readOnly:
            raise web.HTTPForbidden(text="read-only server")
        access = await self._require(request, name, "snapshot")

        fontHandler = self.fontHandlers.get(self._handlerKey(repoPath, branch))
        backend = fontHandler.backend if fontHandler is not None else None
        store = backend.store if backend is not None else GitRepoStore.open(repoPath)
        try:
            if store.head(branch) is None:
                raise web.HTTPNotFound(text=f"no branch {branch}")
            if backend is not None:
                backend.flush()  # the snapshot names the glyph as it is now
            head = store.head(branch)
            # Something to name: its newest version is not in a glyph snapshot yet.
            newest = store.log(
                head,
                path=glyphPath(glyphName),
                glyph=glyphName,
                limit=1,
                glyph_snapshots=store.glyph_snapshots(glyphName),
            )
            if not newest:
                raise web.HTTPConflict(text=f"{glyphName} has no history")
            if newest[0].glyph_snapshot is not None:
                raise web.HTTPConflict(
                    text=f"{glyphName} did not change since its last snapshot"
                )
            try:
                snapshot = store.create_glyph_snapshot(
                    title, [glyphName], ref=head, author=_author(access, self.author)
                )
            except ValueError as error:
                raise web.HTTPConflict(text=str(error))
        finally:
            if backend is None:
                store.close()
        return web.json_response(
            {"branch": branch, "head": head, "glyphSnapshot": _snapshotJSON(snapshot)}
        )

    async def glyphHandler(self, request: web.Request) -> web.Response:
        """The JSON of one glyph at a given ref (commit sha, branch or tag)."""
        glyphName = request.query.get("glyph")
        repoPath, _ = await self._project(request, request.match_info["name"], "read")
        if not glyphName:
            raise web.HTTPNotFound()
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
    "jpg": "image/jpeg",
    "ico": "image/x-icon",
    "webmanifest": "application/manifest+json",
}


def _snapshotJSON(snapshot: SnapshotInfo | GlyphSnapshotInfo) -> dict[str, Any]:
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


def _devLoginPage(directory: DevDirectory, ref: str = "/") -> str:
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
  <form method="post" action="/hive/dev-login">
    <input type="hidden" name="ref" value="{escape(_safeRef(ref))}">{buttons}</form>
</body></html>
"""


EXPORT_TIMEOUT = 30 * 60  # seconds: a large CJK font can take long to build
_exportSlot = asyncio.Semaphore(1)  # one build at a time on the server


def _safeStem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return stem or "font"


class HiveExportManager:
    """Fontra's ExportManager protocol: the formats "File › Export as" lists.
    The download itself goes through exportHandler (a browser needs a URL,
    not a file written on the server)."""

    def getSupportedExportFormats(self) -> list[str]:
        from .export import supported_formats

        return supported_formats()

    async def exportAs(self, projectIdentifier: str, options: dict) -> None:
        raise NotImplementedError("Hive exports through /api/hive/projects/<id>/export")


HIVE_HEAD_SCRIPT = '<script src="/hive/views/register.js"></script>'
# Hive's icon in the browser tab, on a phone's home screen, as an app. Also
# written by hand in the account pages (client/account/*.html).
HIVE_ICON_LINKS = (
    '<link rel="icon" href="/hive/icons/hive-icon.svg" type="image/svg+xml">'
    '<link rel="icon" href="/favicon.ico" sizes="48x48">'
    '<link rel="apple-touch-icon" href="/hive/icons/apple-touch-icon.png">'
    '<link rel="manifest" href="/hive/icons/site.webmanifest">'
)
HIVE_BODY_SCRIPT = '<script type="module" src="/hive/views/hive-views.js"></script>'


def injectHiveScripts(html: str) -> str:
    """Add Hive's scripts to one of Fontra's pages: a small classic script at
    the start of <head> (it must run before Fontra's modules: it registers
    the history plug-in before the editor reads its plug-in list), and the
    Hive UI module at the end of <body>; and Hive's icon links."""
    lower = html.lower()
    head = lower.find("<head>")
    if head != -1:
        cut = head + len("<head>")
        html = html[:cut] + HIVE_HEAD_SCRIPT + HIVE_ICON_LINKS + html[cut:]
    else:
        html = HIVE_HEAD_SCRIPT + HIVE_ICON_LINKS + html
    lower = html.lower()
    body = lower.rfind("</body>")
    if body != -1:
        html = html[:body] + HIVE_BODY_SCRIPT + html[body:]
    else:
        html = html + HIVE_BODY_SCRIPT
    return html


def _htmlResponse(text: str) -> web.Response:
    return web.Response(
        text=text, content_type="text/html", headers={"Cache-Control": "no-cache"}
    )


def _safeRef(ref) -> str:
    """Where to go after signing in: a path on this site, never elsewhere."""
    if isinstance(ref, str) and ref.startswith("/") and not ref.startswith("//"):
        return ref
    return "/"
