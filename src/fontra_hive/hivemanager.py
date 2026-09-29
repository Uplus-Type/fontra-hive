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

    async def aclose(self) -> None:
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

    async def _project(self, request: web.Request, name: str, capability: str):
        projectAccess = await self._projectAccess(request, name)
        if not projectAccess.access.can(capability):
            raise web.HTTPForbidden(
                text=f"{projectAccess.access.role} cannot {capability}"
            )
        return self._ensureRepo(projectAccess), projectAccess.access

    # --- repositories -----------------------------------------------------------

    def _ensureRepo(self, projectAccess: ProjectAccess) -> pathlib.Path:
        """The project's repository, created with an empty font the first
        time (built aside, then moved in place: never half-made)."""
        repo = projectAccess.repo
        if not repo.endswith(".git") or "/" in repo or repo.startswith("."):
            raise web.HTTPInternalServerError(text="unexpected repository name")
        path = self.rootPath / repo
        if path.is_dir():
            return path
        from fontra.backends.fontra import FontraBackend

        with tempfile.TemporaryDirectory(dir=self.rootPath, prefix=".new-") as tmp:
            font = pathlib.Path(tmp) / "font.fontra"
            FontraBackend.createFromPath(font)
            building = pathlib.Path(tmp) / repo
            store = GitRepoStore.create(building)
            try:
                store.import_directory(
                    font,
                    branch=projectAccess.defaultBranch,
                    message=f"New project {projectAccess.projectId}",
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
        if await self.authorize(request) is None:
            return _htmlResponse(self.accountFile("login.html"))
        return await super().rootDocumentHandler(request)

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
