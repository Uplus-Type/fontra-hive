"""Who may do what on a project: roles, capabilities, and the development directory.

The account model follows GitHub's: a *user* has a unique username and an
email address; an *organization* has owners and members; a *project* is owned
by a user or an organization and may have outside *collaborators*, each with a
role. A person's role on a project is the highest of:

- ``admin`` if they own the project, or own the organization that owns it;
- the organization's ``base_role`` if they are a member of that organization;
- their role as a collaborator on the project.

Roles, from least to most: observer, reviewer, designer, manager, admin.
The capabilities of each role are in :data:`CAPABILITIES`; the Fontra server
checks them for every connection (see :mod:`fontra_hive.fonthandler`) and for
every Hive route.

In production the directory is hive-api (:mod:`fontra_hive.hiveapi`). For
local development,
:class:`DevDirectory` reads the same model from a JSON file::

    {
      "users": {
        "jeremie": {"name": "Jérémie Hornus", "email": "jeremie@example.com"},
        "fabio": {"name": "Fabio Rossi", "email": "fabio@example.com"}
      },
      "organizations": {
        "uplustype": {"name": "U+Type", "members": {"jeremie": "owner", "fabio": "member"},
                      "base_role": "designer"}
      },
      "projects": {
        "MutatorSans": {"owner": "uplustype", "collaborators": {"ana": "observer"}}
      }
    }

A project that is not listed is visible to nobody. The file is re-read when it
changes, so roles can be edited while the server runs.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from dataclasses import dataclass, field

from .gitstore import Signature

logger = logging.getLogger(__name__)

ROLES = ("observer", "reviewer", "designer", "manager", "admin")

# capability -> least role that has it
_MINIMUM_ROLE = {
    "read": "observer",
    "comment": "reviewer",
    "edit": "designer",  # edit glyphs, restore a version
    "branch": "designer",
    "snapshot": "designer",
    "merge": "manager",
    "tag": "manager",
    "export": "manager",
    "invite": "manager",  # invite, change roles
    "moderate": "manager",  # delete others' comments
    "administer": "admin",  # rename, trash, delete, transfer
}

CAPABILITIES: dict[str, frozenset[str]] = {
    role: frozenset(
        cap
        for cap, minimum in _MINIMUM_ROLE.items()
        if ROLES.index(role) >= ROLES.index(minimum)
    )
    for role in ROLES
}


def role_rank(role: str | None) -> int:
    return ROLES.index(role) if role in ROLES else -1


def highest_role(*roles: str | None) -> str | None:
    best = max(roles, key=role_rank, default=None)
    return best if role_rank(best) >= 0 else None


@dataclass(frozen=True)
class HiveUser:
    username: str
    name: str
    email: str
    uid: str = ""  # hive-api's stable id (empty in development)

    @property
    def signature(self) -> Signature:
        """The git author for this user's commits: the display name and a
        stable pseudonymous address (never the real email, which a clone of
        the repository would expose)."""
        return Signature(self.name or self.username, f"{self.username}@hive")


@dataclass(frozen=True)
class Access:
    user: HiveUser
    role: str
    capabilities: frozenset[str] = field(default=frozenset())

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def read_only(self) -> bool:
        return not self.can("edit")

    def to_json(self) -> dict:
        return {
            "user": {
                "username": self.user.username,
                "name": self.user.name,
                "email": self.user.email,
            },
            "role": self.role,
            "capabilities": sorted(self.capabilities),
        }


TOKEN_PREFIX = "hive-user:"


def token_for(username: str) -> str:
    return TOKEN_PREFIX + username


def username_from_token(token: str | None) -> str | None:
    if token and token.startswith(TOKEN_PREFIX):
        return token[len(TOKEN_PREFIX) :] or None
    return None


class DevDirectory:
    """Users, organizations and project roles read from a JSON file."""

    def __init__(self, path: os.PathLike | str):
        self.path = pathlib.Path(path)
        self._mtime: float | None = None
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._data, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.error(
                "cannot read %s: %s (keeping the previous one)", self.path, error
            )
            return
        self._data, self._mtime = data, mtime

    @property
    def data(self) -> dict:
        self._load()  # cheap: one stat() per call
        return self._data

    def users(self) -> list[HiveUser]:
        return [
            self._user(name, info) for name, info in self.data.get("users", {}).items()
        ]

    def user(self, username: str | None) -> HiveUser | None:
        info = self.data.get("users", {}).get(username or "")
        return self._user(username, info) if info is not None else None

    @staticmethod
    def _user(username, info) -> HiveUser:
        return HiveUser(username, info.get("name") or username, info.get("email", ""))

    def role(self, username: str, project: str) -> str | None:
        data = self.data
        if username not in data.get("users", {}):
            return None
        info = data.get("projects", {}).get(project)
        if info is None:
            return None
        owner = info.get("owner")
        roles = [info.get("collaborators", {}).get(username)]
        if owner == username:
            roles.append("admin")
        org = data.get("organizations", {}).get(owner or "")
        if org is not None:
            membership = org.get("members", {}).get(username)
            if membership == "owner":
                roles.append("admin")
            elif membership == "member":
                roles.append(org.get("base_role", "observer"))
        return highest_role(*roles)

    def access(self, username: str | None, project: str) -> Access | None:
        user = self.user(username)
        if user is None:
            return None
        role = self.role(user.username, project)
        if role is None:
            return None
        return Access(user, role, CAPABILITIES[role])

    def members(self, project: str) -> list[dict]:
        """Everyone with a role on the project, highest role first, with where
        the role comes from (``owner``, ``organization``, ``collaborator``)."""
        data = self.data
        info = data.get("projects", {}).get(project)
        if info is None:
            return []
        owner = info.get("owner")
        org = data.get("organizations", {}).get(owner or "")
        result = []
        for username in data.get("users", {}):
            role = self.role(username, project)
            if role is None:
                continue
            collaborator = info.get("collaborators", {}).get(username)
            if username == owner:
                via = "owner"
            elif (
                org is not None
                and username in org.get("members", {})
                and (role_rank(role) > role_rank(collaborator))
            ):
                via = f"organization {org.get('name') or owner}"
            else:
                via = "collaborator"
            user = self.user(username)
            result.append(
                {
                    "username": username,
                    "name": user.name,
                    "email": user.email,
                    "role": role,
                    "via": via,
                    "collaboratorRole": collaborator,
                }
            )
        result.sort(key=lambda m: (-role_rank(m["role"]), m["name"].lower()))
        return result

    def set_collaborator(self, project: str, username: str, role: str | None) -> None:
        """Add, change or remove (``role=None``) an outside collaborator, and
        write the file back. Refuses to leave the project without an admin."""
        data = json.loads(json.dumps(self.data))  # a copy to edit
        if username not in data.get("users", {}):
            raise KeyError(f"unknown user {username!r}")
        if role is not None and role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        info = data.get("projects", {}).get(project)
        if info is None:
            raise KeyError(f"unknown project {project!r}")
        collaborators = info.setdefault("collaborators", {})
        if role is None:
            collaborators.pop(username, None)
        else:
            collaborators[username] = role
        previous = self._data
        self._data = data
        try:
            if not any(m["role"] == "admin" for m in self.members(project)):
                raise ValueError("a project must keep at least one admin")
        finally:
            self._data = previous
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.path)
        self._mtime = None
        self._load()

    def projects_for(self, username: str) -> list[str]:
        return [p for p in self.data.get("projects", {}) if self.role(username, p)]
