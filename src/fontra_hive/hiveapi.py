"""Who is who, from hive-api (the accounts service).

- **Identity** comes from the ``hive_access`` cookie: a short EdDSA JWT that
  hive-api signs and this server checks with hive-api's public key alone
  (``/api/.well-known/jwks.json``, fetched once and again when the key
  changes). Its ``sub`` is the user's stable id.
- **Roles** come from ``/api/internal/access`` (with the service key), kept a
  few seconds: every Hive route asks through :meth:`HiveApi.access`, which
  refreshes an old answer; the editor's connections read the last answer
  synchronously (:meth:`HiveApi.cachedAccess`) and refresh it in the
  background, so an edit never waits on the network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp
import jwt

from .access import CAPABILITIES, Access, HiveUser
from .remotesync import RemoteNotUsable

logger = logging.getLogger(__name__)

ACCESS_COOKIE = "hive_access"
TOKEN_PREFIX = "hive:"
AUDIENCE = "hive"


class HiveApiUnavailable(Exception):
    """hive-api did not answer (or answered something unexpected)."""


@dataclass(frozen=True)
class ProjectAccess:
    access: Access
    repo: str  # repository file name under the server's root, "<uid>.git"
    defaultBranch: str
    projectId: str  # "owner/name", as hive-api spells it


def token_for_uid(uid: str) -> str:
    return TOKEN_PREFIX + uid


def uid_from_token(token: str | None) -> str | None:
    if token and token.startswith(TOKEN_PREFIX):
        return token[len(TOKEN_PREFIX) :] or None
    return None


class HiveApi:
    def __init__(
        self,
        url: str,
        serviceKey: str,
        *,
        issuer: str | None = None,
        accessTTL: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ):
        self.url = url.rstrip("/")
        self.serviceKey = serviceKey
        self.issuer = issuer
        self.accessTTL = accessTTL
        self._session = session
        self._keys: dict[str, object] = {}  # kid -> public key
        self._keysFetchedAt = 0.0
        # uid -> the user as last seen in a valid token
        self._users: dict[str, HiveUser] = {}
        # (uid, project) -> (ProjectAccess | None, fetched at)
        self._access: dict[tuple[str, str], tuple[ProjectAccess | None, float]] = {}
        self._refreshing: set[tuple[str, str]] = set()
        # project -> (members, fetched at)
        self._members: dict[str, tuple[list[dict], float]] = {}

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                cookie_jar=aiohttp.DummyCookieJar(),  # never keep anyone's cookies
            )
        return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, **params) -> dict | None:
        """GET an internal route; None for a 404."""
        try:
            async with self.session.get(
                self.url + path,
                params=params,
                headers={"Authorization": f"Bearer {self.serviceKey}"},
            ) as response:
                if response.status == 404:
                    return None
                if response.status != 200:
                    raise HiveApiUnavailable(f"{path}: HTTP {response.status}")
                return await response.json()
        except aiohttp.ClientError as error:
            raise HiveApiUnavailable(f"{path}: {error}") from error

    # --- identity ------------------------------------------------------------

    async def _publicKeys(self, *, refresh: bool = False) -> dict[str, object]:
        # At most one refetch every 30 s, even with a flood of unknown kids.
        if self._keys and not (refresh and time.monotonic() - self._keysFetchedAt > 30):
            return self._keys
        data = await self._get("/api/.well-known/jwks.json")
        keys = {}
        for jwk in (data or {}).get("keys", []):
            if jwk.get("alg") == "EdDSA":
                keys[jwk.get("kid", "")] = jwt.PyJWK(jwk).key
        self._keys, self._keysFetchedAt = keys, time.monotonic()
        return keys

    async def userFromAccessToken(self, token: str | None) -> HiveUser | None:
        """The user of a valid access token (signature, expiry, audience)."""
        if not token:
            return None
        try:
            kid = jwt.get_unverified_header(token).get("kid", "")
        except jwt.PyJWTError:
            return None
        keys = await self._publicKeys()
        if kid not in keys:
            keys = await self._publicKeys(refresh=True)
        key = keys.get(kid)
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["EdDSA"],
                audience=AUDIENCE,
                issuer=self.issuer,
                options={
                    "require": ["exp", "iat", "sub"],
                    "verify_iss": bool(self.issuer),
                },
            )
        except jwt.PyJWTError:
            return None
        user = HiveUser(
            str(claims.get("username", "")),
            str(claims.get("name") or claims.get("username", "")),
            "",
            uid=str(claims["sub"]),
        )
        self._users[user.uid] = user
        return user

    def user(self, token: str | None) -> HiveUser | None:
        uid = uid_from_token(token)
        return self._users.get(uid) if uid else None

    # --- roles ---------------------------------------------------------------

    async def access(self, token: str | None, project: str) -> ProjectAccess | None:
        """The role of the token's user on ``project`` ("owner/name"), fresh
        (at most ``accessTTL`` seconds old)."""
        uid = uid_from_token(token)
        if uid is None or "/" not in project:
            return None
        key = (uid, project.lower())
        cached = self._access.get(key)
        if cached is not None and time.monotonic() - cached[1] < self.accessTTL:
            return cached[0]
        return await self._fetchAccess(uid, project)

    async def _fetchAccess(self, uid: str, project: str) -> ProjectAccess | None:
        data = await self._get("/api/internal/access", user=uid, project=project)
        result = None
        if data is not None and data.get("role") in CAPABILITIES:
            info = data.get("user", {})
            known = self._users.get(uid)
            user = HiveUser(
                info.get("username") or (known.username if known else ""),
                info.get("name") or (known.name if known else ""),
                "",
                uid=uid,
            )
            self._users[uid] = user
            role = data["role"]
            result = ProjectAccess(
                access=Access(user, role, CAPABILITIES[role]),
                repo=data["repo"],
                defaultBranch=data.get("defaultBranch", "main"),
                projectId=data["id"],
            )
        self._access[(uid, project.lower())] = (result, time.monotonic())
        return result

    def cachedAccess(self, token: str | None, project: str) -> Access | None:
        """The last known role, without waiting (for each edit of an open
        project). An old answer is refreshed in the background."""
        uid = uid_from_token(token)
        if uid is None:
            return None
        key = (uid, project.lower())
        cached = self._access.get(key)
        if cached is None:
            return None
        if (
            time.monotonic() - cached[1] >= self.accessTTL
            and key not in self._refreshing
        ):
            self._refreshing.add(key)

            async def refresh():
                try:
                    await self._fetchAccess(uid, project)
                except HiveApiUnavailable as error:
                    logger.warning("could not refresh a role: %s", error)
                finally:
                    self._refreshing.discard(key)

            try:
                asyncio.get_running_loop().create_task(refresh())
            except RuntimeError:  # no loop: nothing to refresh with
                self._refreshing.discard(key)
        return cached[0].access if cached[0] is not None else None

    async def projects(self, token: str | None) -> list[dict]:
        uid = uid_from_token(token)
        if uid is None:
            return []
        data = await self._get("/api/internal/projects", user=uid)
        return (data or {}).get("projects", [])

    async def deletedRepositories(self) -> list[str]:
        """Repositories of projects deleted for good (to remove from disk)."""
        data = await self._get("/api/internal/deleted-repositories")
        return (data or {}).get("repositories", [])

    async def project(self, project: str) -> dict | None:
        """A project's id, repository and default branch (no user needed)."""
        return await self._get("/api/internal/project", project=project)

    # --- remote git repositories ---------------------------------------------

    async def _call(self, method: str, path: str, *, params=None, json=None):
        """An internal route: (status, decoded JSON or text)."""
        try:
            async with self.session.request(
                method,
                self.url + path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {self.serviceKey}"},
            ) as response:
                if response.content_type == "application/json":
                    data = await response.json()
                else:
                    data = await response.text()
                return response.status, data
        except aiohttp.ClientError as error:
            raise HiveApiUnavailable(f"{path}: {error}") from error

    async def remote(self, project: str) -> dict | None:
        """A project's remote repository with credentials to use now (None:
        no remote). Raises :class:`RemoteNotUsable` when hive-api knows it
        cannot be used (app uninstalled, access lost)."""
        status, data = await self._call(
            "GET", "/api/internal/remote", params={"project": project}
        )
        if status == 404:
            return None
        if status == 409:
            detail = data.get("detail") if isinstance(data, dict) else data
            raise RemoteNotUsable(str(detail or "The remote cannot be used."))
        if status != 200:
            raise HiveApiUnavailable(f"/api/internal/remote: HTTP {status}")
        return data

    async def remotesMoved(self) -> list[dict]:
        """Remotes GitHub says have commits not pulled yet."""
        data = await self._get("/api/internal/remotes/moved")
        return (data or {}).get("remotes", [])

    async def remoteSynced(self, project: str, sha: str) -> None:
        status, _ = await self._call(
            "POST", "/api/internal/remote/synced", json={"project": project, "sha": sha}
        )
        if status not in (200, 404):
            raise HiveApiUnavailable(f"/api/internal/remote/synced: HTTP {status}")

    async def members(self, project: str, ttl: float = 30.0) -> list[dict]:
        """Everyone with a role on a project: username, name, uid, avatar,
        role (kept ``ttl`` seconds: comments list them at each change)."""
        key = project.lower()
        cached = self._members.get(key)
        if cached is not None and time.monotonic() - cached[1] < ttl:
            return cached[0]
        data = await self._get("/api/internal/members", project=project)
        members = [
            {
                k: m.get(k)
                for k in ("username", "name", "uid", "avatar", "role")
                if m.get(k) is not None
            }
            for m in (data or {}).get("members", [])
        ]
        self._members[key] = (members, time.monotonic())
        return members
