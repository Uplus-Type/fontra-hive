"""The Hive project manager: git-backed projects, people from hive-api.

Usage::

    fontra hive /path/to/repos --api http://127.0.0.1:8001 --service-key …

- Projects are ``owner/name`` (an organization's or a person's), as in
  hive-api; ``owner/name@branch`` opens a branch. Their repositories are
  ``<root>/<uid>.git``, named after hive-api's stable project id, so renaming
  or transferring a project moves nothing. A project opened for the first
  time gets its repository, with an empty font.
- Who is signed in comes from the ``hive_access`` cookie that hive-api sets;
  roles come from hive-api (see :mod:`fontra_hive.hiveapi`).
- With ``--proxy`` (the default), this server also relays ``/api/*`` (except
  its own ``/api/hive/*``) to hive-api: one address for the browser, as in
  production behind the reverse proxy, and the cookies just work.
- Sign-in, invitation and password pages are served here (``client/account``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import shutil
import tempfile
from importlib import resources
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from fontra.core.fonthandler import FontHandler
from multidict import CIMultiDict

from . import importer
from .access import Access
from .gitstore import DEFAULT_BRANCH, SERVER_SIGNATURE, GitRepoStore
from .hiveapi import (
    ACCESS_COOKIE,
    HiveApi,
    HiveApiUnavailable,
    ProjectAccess,
    token_for_uid,
)
from .projectmanager import DevHiveProjectManager, _htmlResponse, _sameOrigin

logger = logging.getLogger(__name__)

ACCOUNT_PAGES = {
    "/invitation": "invitation.html",
    "/forgot-password": "forgot-password.html",
    "/reset-password": "reset-password.html",
}

# Not relayed: hop-by-hop headers, and what the relay recomputes.
_SKIP_REQUEST_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
}
_SKIP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-length",
    "content-encoding",  # the client session already decoded the body
    "server",
    "date",
}


class HiveProjectManagerFactory:
    @staticmethod
    def addArguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "root", type=pathlib.Path, help="Folder of the projects' repositories"
        )
        parser.add_argument(
            "--api",
            default=os.environ.get("HIVE_API_URL", "http://127.0.0.1:8001"),
            help="hive-api's address (default: $HIVE_API_URL or http://127.0.0.1:8001)",
        )
        parser.add_argument(
            "--service-key",
            default=os.environ.get("HIVE_SERVICE_KEY", "dev-service-key"),
            help="shared with hive-api (default: $HIVE_SERVICE_KEY, or hive-api's "
            "development key)",
        )
        parser.add_argument(
            "--issuer",
            default=os.environ.get("HIVE_PUBLIC_URL"),
            help="expected issuer of the access tokens: hive-api's HIVE_PUBLIC_URL",
        )
        parser.add_argument(
            "--no-proxy",
            action="store_true",
            help="do not relay /api/* to hive-api (a reverse proxy does it)",
        )
        parser.add_argument("--commit-delay", type=float, default=2.0, help="seconds")

    @staticmethod
    def getProjectManager(arguments: SimpleNamespace) -> HiveProjectManager:
        root = arguments.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        logger.info("Hive: projects in %s, accounts from %s", root, arguments.api)
        return HiveProjectManager(
            rootPath=root,
            api=HiveApi(arguments.api, arguments.service_key, issuer=arguments.issuer),
            commitDelay=arguments.commit_delay,
            proxy=not arguments.no_proxy,
        )


SWEEP_INTERVAL = 15 * 60  # seconds


def _isRepoName(repo: str) -> bool:
    return (
        repo.endswith(".git")
        and len(repo) > 4
        and "/" not in repo
        and "\\" not in repo
        and not repo.startswith(".")
    )


def splitIdentifier(identifier: str) -> tuple[str, str | None]:
    """``owner/name@branch`` -> ("owner/name", "branch"); no @: branch None."""
    name, at, branch = identifier.partition("@")
    return name, (branch or None) if at else None


class HiveProjectManager(DevHiveProjectManager):
    def __init__(
        self,
        rootPath: pathlib.Path,
        *,
        api: HiveApi,
        commitDelay: float = 2.0,
        proxy: bool = True,
    ):
        super().__init__(rootPath, commitDelay=commitDelay, author=SERVER_SIGNATURE)
        self.api = api
        self.directory = api  # accounts are on (the base class checks this)
        self.proxy = proxy
        self._proxySession: aiohttp.ClientSession | None = None
        self._sweepLock = asyncio.Lock()
        self._sweepTask: asyncio.Task | None = None

    async def aclose(self) -> None:
        if self._sweepTask is not None:
            self._sweepTask.cancel()
        await super().aclose()
        await self.api.aclose()
        if self._proxySession is not None:
            await self._proxySession.close()

    # --- identity and roles ---------------------------------------------------

    async def authorize(self, request: web.Request) -> str | None:
        if not _sameOrigin(request):
            return None
        try:
            user = await self.api.userFromAccessToken(
                request.cookies.get(ACCESS_COOKIE)
            )
        except HiveApiUnavailable as error:
            logger.warning("hive-api unavailable: %s", error)
            return None
        return token_for_uid(user.uid) if user is not None else None

    def accessFor(self, token: str | None, projectName: str) -> Access | None:
        return self.api.cachedAccess(token, projectName)

    async def _projectAccess(self, request: web.Request, name: str) -> ProjectAccess:
        token = await self.authorize(request)
        try:
            projectAccess = await self.api.access(token, name)
        except HiveApiUnavailable as error:
            logger.warning("hive-api unavailable: %s", error)
            raise web.HTTPServiceUnavailable(
                text="The accounts service does not answer."
            )
        if projectAccess is None or not projectAccess.access.can("read"):
            raise web.HTTPNotFound()  # not a member: looks absent
        return projectAccess

    async def _require(
        self, request: web.Request, projectName: str, capability: str
    ) -> Access:
        access = (await self._projectAccess(request, projectName)).access
        if not access.can(capability):
            raise web.HTTPForbidden(text=f"{access.role} cannot {capability}")
        return access

    async def _defaultBranch(self, request: web.Request, name: str) -> str:
        return (await self._projectAccess(request, name)).defaultBranch

    async def _project(self, request: web.Request, name: str, capability: str):
        projectAccess = await self._projectAccess(request, name)
        if not projectAccess.access.can(capability):
            raise web.HTTPForbidden(
                text=f"{projectAccess.access.role} cannot {capability}"
            )
        return self._ensureRepo(projectAccess), projectAccess.access

    # --- repositories -----------------------------------------------------------

    def _repoPathFor(self, projectAccess: ProjectAccess) -> pathlib.Path:
        repo = projectAccess.repo
        if not _isRepoName(repo):
            raise web.HTTPInternalServerError(text="unexpected repository name")
        return self.rootPath / repo

    def _ensureRepo(
        self,
        projectAccess: ProjectAccess,
        font: pathlib.Path | None = None,
        message: str | None = None,
    ) -> pathlib.Path:
        """The project's repository, created the first time with ``font``
        (a .fontra package), or an empty font. Built aside, then moved in
        place: never half-made."""
        path = self._repoPathFor(projectAccess)
        if path.is_dir():
            return path
        from fontra.backends.fontra import FontraBackend

        with tempfile.TemporaryDirectory(dir=self.rootPath, prefix=".new-") as tmp:
            if font is None:
                font = pathlib.Path(tmp) / "font.fontra"
                FontraBackend.createFromPath(font)
            building = pathlib.Path(tmp) / projectAccess.repo
            store = GitRepoStore.create(building)
            try:
                store.import_directory(
                    font,
                    branch=projectAccess.defaultBranch,
                    message=message or f"New project {projectAccess.projectId}",
                    author=projectAccess.access.user.signature,
                )
            finally:
                store.close()
            try:
                building.rename(path)
                logger.info("created %s for %s", path, projectAccess.projectId)
            except OSError:
                if not path.is_dir():  # not just someone faster than us
                    raise
                shutil.rmtree(building, ignore_errors=True)
        return path

    # A project deleted for good in hive-api (from the trash) leaves its
    # repository here: hive-api lists those, and we remove them, at start,
    # every SWEEP_INTERVAL, and when the home page asks (right after a
    # deletion). A repository still open (someone in the editor) waits for
    # the next sweep. The nightly backups keep them for their retention time.

    async def sweepDeletedRepositories(self) -> list[str]:
        async with self._sweepLock:
            try:
                repos = await self.api.deletedRepositories()
            except HiveApiUnavailable as error:
                logger.warning("hive-api unavailable: %s", error)
                return []
            removed = []
            for repo in repos:
                if not isinstance(repo, str) or not _isRepoName(repo):
                    logger.warning("not removing unexpected repository %r", repo)
                    continue
                path = self.rootPath / repo
                if not path.is_dir():
                    continue
                if any(key.startswith(repo + "@") for key in self.fontHandlers):
                    continue  # open: next time
                trash = self.rootPath / f".deleted-{repo[:-4]}"
                shutil.rmtree(trash, ignore_errors=True)
                path.rename(trash)  # gone at once, even if the removal takes time
                await asyncio.to_thread(shutil.rmtree, trash, ignore_errors=True)
                logger.info("removed %s (project deleted)", path)
                removed.append(repo)
            return removed

    async def _sweepPeriodically(self) -> None:
        while True:
            try:
                await self.sweepDeletedRepositories()
            except Exception:  # never stop sweeping
                logger.exception("sweeping deleted repositories")
            await asyncio.sleep(SWEEP_INTERVAL)

    def setupWebRoutes(self, server) -> None:
        super().setupWebRoutes(server)

        async def startSweeping(app):
            self._sweepTask = asyncio.create_task(self._sweepPeriodically())

        server.httpApp.on_startup.append(startSweeping)

    async def sweepHandler(self, request: web.Request) -> web.Response:
        if await self.authorize(request) is None:
            raise web.HTTPUnauthorized()
        return web.json_response({"removed": await self.sweepDeletedRepositories()})

    async def repositoryHandler(self, request: web.Request) -> web.Response:
        """Whether the project has its repository yet, without creating it
        (every other route creates it, with an empty font, when missing): a
        project made "from a font" whose import failed has none, and the
        home page offers to import again or start empty rather than letting
        it open on an empty font."""
        projectAccess = await self._projectAccess(request, request.match_info["name"])
        exists = self._repoPathFor(projectAccess).is_dir()
        return web.json_response({"exists": exists})

    async def _projectIfExists(self, request: web.Request, name: str):
        projectAccess = await self._projectAccess(request, name)
        path = self._repoPathFor(projectAccess)
        return (path if path.is_dir() else None), projectAccess.access

    async def importHandler(self, request: web.Request) -> web.Response:
        """Replace a project's font with an uploaded one (multipart field
        ``file``), converted from any format Fontra reads. A new commit:
        the history before it stays. Admins only."""
        name = request.match_info["name"]
        projectAccess = await self._projectAccess(request, name)
        if not projectAccess.access.can("administer"):
            raise web.HTTPForbidden(
                text="Only the admins of a project can import a font."
            )
        reader = await request.multipart()
        part = await reader.next()
        while part is not None and part.name != "file":
            part = await reader.next()
        if part is None or not part.filename:
            raise web.HTTPBadRequest(text="Choose a font file.")
        filename = pathlib.PurePath(part.filename).name
        with tempfile.TemporaryDirectory(dir=self.rootPath, prefix=".import-") as tmp:
            work = pathlib.Path(tmp)
            upload = work / "upload"
            size = 0
            with open(upload, "wb") as out:
                while chunk := await part.read_chunk(1 << 20):
                    size += len(chunk)
                    if size > importer.MAX_UPLOAD:
                        raise web.HTTPRequestEntityTooLarge(
                            max_size=importer.MAX_UPLOAD, actual_size=size
                        )
                    out.write(chunk)
            try:
                font = await asyncio.to_thread(
                    importer.convertToFontra, upload, filename, work
                )
            except importer.ImportError_ as error:
                raise web.HTTPUnprocessableEntity(text=str(error))
            message = f"Import {filename}"
            author = projectAccess.access.user.signature
            path = self._repoPathFor(projectAccess)
            if not path.is_dir():
                self._ensureRepo(projectAccess, font, message)
                store = GitRepoStore.open(path)
                try:
                    head = store.head(projectAccess.defaultBranch)
                finally:
                    store.close()
                return web.json_response({"head": head, "created": True})
            branch = projectAccess.defaultBranch
            fontHandler = self.fontHandlers.get(self._handlerKey(path, branch))
            backend = fontHandler.backend if fontHandler is not None else None
            store = backend.store if backend is not None else GitRepoStore.open(path)
            try:
                if backend is not None:
                    backend.flush()  # pending edits first: they stay in the history
                head = store.import_directory(
                    font, branch=branch, message=message, author=author
                )
                if backend is not None:
                    await backend.check_external_changes()
            finally:
                if backend is None:
                    store.close()
        return web.json_response({"head": head, "created": False})

    # --- Fontra's project manager protocol --------------------------------------

    async def projectAvailable(self, projectIdentifier: str, token: str) -> bool:
        name, branch = splitIdentifier(projectIdentifier)
        try:
            projectAccess = await self.api.access(token, name)
        except HiveApiUnavailable:
            return False
        if projectAccess is None:
            return False
        repoPath = self._ensureRepo(projectAccess)
        store = GitRepoStore.open(repoPath)
        try:
            return store.head(branch or projectAccess.defaultBranch) is not None
        finally:
            store.close()

    async def getProjectList(self, token: str) -> list[str]:
        try:
            projects = await self.api.projects(token)
        except HiveApiUnavailable as error:
            logger.warning("hive-api unavailable: %s", error)
            return []
        result = []
        for project in projects:
            result.append(project["id"])
            repoPath = self.rootPath / project["repo"]
            if not repoPath.is_dir():
                continue  # created when first opened
            store = GitRepoStore.open(repoPath)
            try:
                default = project.get("defaultBranch", DEFAULT_BRANCH)
                result.extend(
                    f"{project['id']}@{b}" for b in store.branches() if b != default
                )
            finally:
                store.close()
        return result

    async def getRemoteSubject(
        self, projectIdentifier: str, token: str, readOnly: bool = False
    ) -> FontHandler | None:
        name, branch = splitIdentifier(projectIdentifier)
        try:
            projectAccess = await self.api.access(token, name)
        except HiveApiUnavailable as error:
            logger.warning("hive-api unavailable: %s", error)
            return None
        if projectAccess is None:
            return None  # not a member: the connection is refused
        branch = branch or projectAccess.defaultBranch
        projectId = projectAccess.projectId
        return await self._openFontHandler(
            self._ensureRepo(projectAccess),
            branch,
            identifier=f"{projectId}@{branch}",
            accessForToken=lambda token: self.api.cachedAccess(token, projectId),
            readOnly=readOnly,
        )

    # --- pages ---------------------------------------------------------------------

    def webRoutes(self) -> list:
        routes = [
            web.get("/hive/logout", self.logoutPageHandler),
            *(web.get(path, self.accountPageHandler) for path in ACCOUNT_PAGES),
            web.post("/api/hive/projects/{name}/import", self.importHandler),
            web.get("/api/hive/projects/{name}/repository", self.repositoryHandler),
            web.post("/api/hive/sweep-deleted", self.sweepHandler),
            *self.projectRoutes(),
        ]
        if self.proxy:
            routes.append(web.route("*", "/api/{path:.*}", self.apiProxyHandler))
        return routes

    def accountFile(self, name: str) -> str:
        return (
            resources.files("fontra_hive")
            .joinpath("client", "account", name)
            .read_text(encoding="utf-8")
        )

    async def rootDocumentHandler(self, request: web.Request) -> web.Response:
        """Signed in: Hive's home (projects, organizations, profile);
        otherwise the sign-in page."""
        if await self.authorize(request) is None:
            return _htmlResponse(self.accountFile("login.html"))
        return _htmlResponse(self.accountFile("home.html"))

    async def accountPageHandler(self, request: web.Request) -> web.Response:
        return _htmlResponse(self.accountFile(ACCOUNT_PAGES[request.path]))

    async def logoutPageHandler(self, request: web.Request) -> web.Response:
        return _htmlResponse(self.accountFile("logout.html"))

    async def meHandler(self, request: web.Request) -> web.Response:
        user = self.api.user(await self.authorize(request))
        if user is None:
            raise web.HTTPUnauthorized()
        return web.json_response(
            {
                "user": {"username": user.username, "name": user.name, "email": ""},
                "accounts": True,
                # The Share dialog and the account pages talk to hive-api.
                "source": "hive-api",
            }
        )

    async def membersHandler(self, request: web.Request) -> web.Response:
        raise web.HTTPNotFound(text="members are managed by hive-api: /api/projects/…")

    async def projectMembers(self, name: str) -> list[dict]:
        try:
            return await self.api.members(name)
        except HiveApiUnavailable as error:
            logger.warning("hive-api unavailable: %s", error)
            return []

    async def setMemberHandler(self, request: web.Request) -> web.Response:
        raise web.HTTPNotFound(text="members are managed by hive-api: /api/projects/…")

    # --- relay to hive-api ----------------------------------------------------------

    async def apiProxyHandler(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info["path"]
        first = path.split("/", 1)[0]
        if first in ("hive", "internal"):
            # Ours (a route that did not match), or hive-api's internal
            # routes, which are for this server only.
            raise web.HTTPNotFound()
        if self._proxySession is None:
            self._proxySession = aiohttp.ClientSession(
                # Never keep cookies between requests: they are each browser's.
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=True,
                timeout=aiohttp.ClientTimeout(total=60),
            )
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _SKIP_REQUEST_HEADERS
        }
        headers["Host"] = request.host
        headers["X-Forwarded-Host"] = request.host
        headers["X-Forwarded-Proto"] = request.scheme
        forwarded = request.headers.get("X-Forwarded-For")
        remote = request.remote or ""
        headers["X-Forwarded-For"] = f"{forwarded}, {remote}" if forwarded else remote
        url = self.api.url + request.rel_url.raw_path_qs
        try:
            async with self._proxySession.request(
                request.method,
                url,
                headers=headers,
                data=await request.read() if request.body_exists else None,
                allow_redirects=False,
            ) as upstream:
                body = await upstream.read()
                responseHeaders = CIMultiDict(
                    (k, v)
                    for k, v in upstream.headers.items()
                    if k.lower() not in _SKIP_RESPONSE_HEADERS
                )
                return web.Response(
                    status=upstream.status, body=body, headers=responseHeaders
                )
        except aiohttp.ClientError as error:
            logger.warning("hive-api unavailable: %s", error)
            raise web.HTTPBadGateway(text="The accounts service does not answer.")
