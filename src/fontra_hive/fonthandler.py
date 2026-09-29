"""A FontHandler that knows who is on each connection.

Fontra keeps one ``FontHandler`` per open project, shared by every connection
to it: that is what makes live collaboration work. Two things are therefore
per *connection*, not per handler, and this subclass adds them without
changing Fontra:

- **attribution**: each write is wrapped so that the git backend commits it
  under the author of the connection it came from
  (:data:`fontra_hive.backend_git.current_author`);
- **roles**: a connection whose role cannot edit gets ``isReadOnly() == True``
  (Fontra then locks its UI) and its edits are refused before they are
  written or broadcast to anyone else.

Who is on a connection comes from ``connection.authorizationToken``, the
value the project manager's ``authorize()`` returned for that websocket.

If Fontra gains a backend hook for this (``WritableBaseBackend.userInfo()``,
proposed by Just van Rossum on 29 Sep 2026), the attribution part moves there
and ``scheduleDataWrite`` stops being overridden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from fontra.core.fonthandler import FontHandler, funcName, remoteMethod

from .access import Access
from .backend_git import current_author

logger = logging.getLogger(__name__)


class ReadOnlyError(PermissionError):
    pass


@dataclass(kw_only=True)
class HiveFontHandler(FontHandler):
    # token -> Access for this project (None: no access). Called often: must
    # be cheap (the project manager caches).
    accessForToken: Callable[[str | None], Access | None] | None = None

    def accessFor(self, connection) -> Access | None:
        if connection is None or self.accessForToken is None:
            return None
        return self.accessForToken(getattr(connection, "authorizationToken", None))

    def _checkCanEdit(self, connection) -> None:
        if self.accessForToken is None:
            return  # no directory: everyone may edit (development default)
        access = self.accessFor(connection)
        if access is None or not access.can("edit"):
            role = access.role if access else "no access"
            raise ReadOnlyError(f"read-only ({role})")

    # --- read-only per connection ----------------------------------------------

    @remoteMethod
    async def isReadOnly(self, *, connection=None) -> bool:
        if await super().isReadOnly(connection=connection):
            return True
        if self.accessForToken is None:
            return False
        access = self.accessFor(connection)
        return access is None or access.read_only

    @remoteMethod
    async def editIncremental(self, liveChange, *, connection) -> None:
        self._checkCanEdit(connection)
        await super().editIncremental(liveChange, connection=connection)

    @remoteMethod
    async def editFinal(
        self, finalChange, rollbackChange, editLabel, broadcast=False, *, connection
    ) -> None:
        self._checkCanEdit(connection)
        await super().editFinal(
            finalChange, rollbackChange, editLabel, broadcast, connection=connection
        )

    @remoteMethod
    async def putBackgroundImage(self, imageIdentifier, data, *, connection) -> None:
        self._checkCanEdit(connection)
        await super().putBackgroundImage(imageIdentifier, data, connection=connection)

    # --- attribution -----------------------------------------------------------

    async def scheduleDataWrite(
        self, writeKey, writeFunc, connection, reloadPattern=None
    ):
        access = self.accessFor(connection)
        if access is not None:
            author = access.user.signature
            original = writeFunc

            async def writeFunc():
                token = current_author.set(author)
                try:
                    await original()
                finally:
                    current_author.reset(token)

            # Keep Fontra's log line readable (it logs the function's name).
            writeFunc.__name__ = writeFunc.__qualname__ = funcName(original, "write")
        await super().scheduleDataWrite(writeKey, writeFunc, connection, reloadPattern)
