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
  written or broadcast to anyone else;
- **beyond the glyph**: each change is read path by path
  (:func:`fontra_hive.writedomains.changeDomains`) and refused as a whole if
  one part falls in a domain (font settings, kerning, plug-in data) the
  person may not write. The check is in ``editIncremental`` and
  ``editFinal``, not in ``scheduleDataWrite``: by then Fontra has already
  applied the change in memory and sent it to the others.

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

from .access import Access, splitReadOnlyMark
from .backend_git import current_author
from .gitstore import FOLLOWS_REMOTE
from .writedomains import EDIT, WRITE_DOMAINS, changeDomains, refusal

logger = logging.getLogger(__name__)


class ReadOnlyError(PermissionError):
    pass


@dataclass(kw_only=True)
class HiveFontHandler(FontHandler):
    # token -> Access for this project (None: no access). Called often: must
    # be cheap (the project manager caches).
    accessForToken: Callable[[str | None], Access | None] | None = None
    # A branch that mirrors the remote repository (upstream/…): read only
    # for everyone, only the pull writes it.
    followsRemote: bool = False

    def accessFor(self, connection) -> Access | None:
        if connection is None or self.accessForToken is None:
            return None
        token, _ = splitReadOnlyMark(getattr(connection, "authorizationToken", None))
        return self.accessForToken(token)

    @staticmethod
    def _openedReadOnly(connection) -> bool:
        """A connection opened with Fontra's ``read-only=true`` (Hive's Font
        info for a person without "fontsettings")."""
        return splitReadOnlyMark(getattr(connection, "authorizationToken", None))[1]

    def _checkCanEdit(self, connection, *changes) -> None:
        """Refuse unless the connection may edit, and may write every domain
        the ``changes`` touch."""
        if self.followsRemote:
            raise ReadOnlyError(FOLLOWS_REMOTE)
        if self._openedReadOnly(connection):
            raise ReadOnlyError("read-only (opened read-only)")
        if self.accessForToken is None:
            return  # no directory: everyone may edit (development default)
        access = self.accessFor(connection)
        if access is None or not access.can("edit"):
            role = access.role if access else "no access"
            raise ReadOnlyError(f"read-only ({role})")
        touched = set()
        for change in changes:
            if change is not None:
                touched |= changeDomains(change)
        for domain in (EDIT, *WRITE_DOMAINS):
            if domain in touched and not access.can(domain):
                raise ReadOnlyError(refusal(domain))

    # --- read-only per connection ----------------------------------------------

    @remoteMethod
    async def isReadOnly(self, *, connection=None) -> bool:
        if self.followsRemote or await super().isReadOnly(connection=connection):
            return True
        if self._openedReadOnly(connection):
            return True
        if self.accessForToken is None:
            return False
        access = self.accessFor(connection)
        return access is None or access.read_only

    @remoteMethod
    async def editIncremental(self, liveChange, *, connection) -> None:
        self._checkCanEdit(connection, liveChange)
        await super().editIncremental(liveChange, connection=connection)

    @remoteMethod
    async def editFinal(
        self, finalChange, rollbackChange, editLabel, broadcast=False, *, connection
    ) -> None:
        # The rollback is checked too: the client sends it back as an edit
        # to undo.
        self._checkCanEdit(connection, finalChange, rollbackChange)
        await super().editFinal(
            finalChange, rollbackChange, editLabel, broadcast, connection=connection
        )

    @remoteMethod
    async def putBackgroundImage(self, imageIdentifier, data, *, connection) -> None:
        # A background image belongs to a glyph's layer: "edit".
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
