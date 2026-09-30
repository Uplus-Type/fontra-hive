"""HTTP routes for comments on glyphs (see :mod:`fontra_hive.comments`).

A mixin of the project managers: it uses their ``_project`` (repository and
access, with a capability), ``_defaultBranch``, ``readOnly`` and ``author``.

Who may do what:

- ``comment`` (reviewers and up): open a topic, reply, edit their own
  messages, resolve or reopen their own topics, move their own pins;
- ``edit`` (designers and up): resolve, reopen and move any topic, assign
  it to a member of the project, label it;
- ``moderate`` (managers and up): delete any topic or message.

A person may also delete their own reply, and their own topic as long as
nobody else has written in it. Without accounts (development), everyone may
do everything.
"""

from __future__ import annotations

from aiohttp import web

from .access import Access
from .comments import (
    CommentError,
    CommentForbidden,
    CommentNotFound,
    CommentStore,
    person,
    same_person,
)
from .gitstore import SERVER_SIGNATURE, GitRepoStore, RefMovedError, Signature


def _who(access: Access | None, fallback: Signature | None) -> dict:
    if access is None:
        return person("dev", (fallback or SERVER_SIGNATURE).name)
    return person(access.user.username, access.user.name, access.user.uid)


def _signature(access: Access | None, fallback: Signature | None) -> Signature:
    if access is not None:
        return access.user.signature
    return fallback or SERVER_SIGNATURE


def _can(access: Access | None, capability: str) -> bool:
    return access is None or access.can(capability)


def _isAuthor(item: dict, who: dict) -> bool:
    return same_person(item.get("author"), who)


def _number(request) -> int:
    try:
        number = int(request.match_info["number"])
    except (KeyError, ValueError):
        raise web.HTTPNotFound()
    if number < 1:
        raise web.HTTPNotFound()
    return number


def _messageId(request) -> int:
    try:
        return int(request.match_info["message"])
    except (KeyError, ValueError):
        raise web.HTTPNotFound()


async def _jsonBody(request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="a JSON body is required")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="a JSON object is required")
    return data


class CommentRoutesMixin:
    def commentRoutes(self) -> list:
        base = "/api/hive/projects/{name}/comments"
        return [
            web.get(base, self.commentsHandler),
            web.post(base, self.createCommentHandler),
            web.get(base + "/head", self.commentsHeadHandler),
            web.patch(base + "/{number}", self.updateCommentHandler),
            web.delete(base + "/{number}", self.deleteCommentHandler),
            web.post(base + "/{number}/messages", self.replyCommentHandler),
            web.patch(base + "/{number}/messages/{message}", self.editMessageHandler),
            web.delete(
                base + "/{number}/messages/{message}", self.deleteMessageHandler
            ),
        ]

    # --- helpers --------------------------------------------------------------

    async def _commentContext(self, request, capability: str):
        name = request.match_info["name"]
        repoPath, access = await self._project(request, name, capability)
        if capability != "read" and self.readOnly:
            raise web.HTTPForbidden(text="read-only server")
        return name, repoPath, access

    def _commentPermissions(self, access: Access | None) -> dict:
        return {
            "comment": _can(access, "comment") and not self.readOnly,
            "resolveAny": _can(access, "edit") and not self.readOnly,
            "organize": _can(access, "edit") and not self.readOnly,  # assign, label
            "moderate": _can(access, "moderate") and not self.readOnly,
        }

    async def _commentChange(self, request, capability: str, change):
        """Run ``change(comments, access, who, store)`` on the project's
        comments and answer with its result (a topic, or None when one was
        deleted) and the new head of the comments."""
        _, repoPath, access = await self._commentContext(request, capability)
        store = GitRepoStore.open(repoPath)
        try:
            comments = CommentStore(store)
            try:
                result = change(comments, access, _who(access, self.author), store)
            except CommentNotFound as error:
                raise web.HTTPNotFound(text=str(error))
            except CommentForbidden as error:
                raise web.HTTPForbidden(text=str(error))
            except CommentError as error:
                raise web.HTTPBadRequest(text=str(error))
            except RefMovedError:
                raise web.HTTPConflict(text="too many comments at once, try again")
            return web.json_response({"head": comments.head(), "issue": result})
        finally:
            store.close()

    # --- reading --------------------------------------------------------------

    async def commentsHandler(self, request) -> web.Response:
        """The project's topics (``glyph=`` for one glyph's), who is asking and
        what they may do."""
        _, repoPath, access = await self._commentContext(request, "read")
        glyph = request.query.get("glyph") or None
        store = GitRepoStore.open(repoPath)
        try:
            comments = CommentStore(store)
            head, issues = comments.head(), comments.issues(glyph)
        finally:
            store.close()
        members = await self.projectMembers(request.match_info["name"])
        return web.json_response(
            {
                "head": head,
                "issues": issues,
                "you": _who(access, self.author),
                "can": self._commentPermissions(access),
                # For assigning, and for showing avatars.
                "members": [
                    {k: m[k] for k in ("username", "name", "avatar", "role") if k in m}
                    for m in members
                ],
            }
        )

    async def commentsHeadHandler(self, request) -> web.Response:
        """The head of the comments: it changes with every comment (polled)."""
        _, repoPath, _ = await self._commentContext(request, "read")
        store = GitRepoStore.open(repoPath)
        try:
            return web.json_response({"head": CommentStore(store).head()})
        finally:
            store.close()

    # --- writing --------------------------------------------------------------

    async def createCommentHandler(self, request) -> web.Response:
        body = await _jsonBody(request)
        name = request.match_info["name"]
        branch = body.get("branch") or await self._defaultBranch(request, name)

        def change(comments, access, who, store):
            if not isinstance(branch, str) or store.head(branch) is None:
                raise CommentError(f"no branch {branch}")
            return comments.create(
                glyph=body.get("glyph"),
                source=body.get("source"),
                point=body.get("point"),
                text=body.get("text"),
                branch=branch,
                commit=store.head(branch),
                by=who,
                author=_signature(access, self.author),
            )

        return await self._commentChange(request, "comment", change)

    async def replyCommentHandler(self, request) -> web.Response:
        number = _number(request)
        body = await _jsonBody(request)

        def change(comments, access, who, store):
            return comments.reply(
                number,
                body.get("text"),
                by=who,
                author=_signature(access, self.author),
            )

        return await self._commentChange(request, "comment", change)

    async def updateCommentHandler(self, request) -> web.Response:
        """Resolve or reopen (``state``), move the pin (``point``), or
        organize: ``assignee`` (a member's username, or null) and/or
        ``labels`` (designers and up)."""
        number = _number(request)
        body = await _jsonBody(request)
        name = request.match_info["name"]
        state = body.get("state")
        point = body.get("point")
        organizing = "assignee" in body or "labels" in body
        if [state is not None, point is not None, organizing].count(True) != 1:
            raise web.HTTPBadRequest(
                text="give one of: state, point, or assignee and labels"
            )
        if organizing:
            return await self._organizeComment(request, number, body)
        branch = body.get("branch") or await self._defaultBranch(request, name)

        def change(comments, access, who, store):
            def check(issue):
                if not (_can(access, "edit") or _isAuthor(issue, who)):
                    raise CommentForbidden(
                        "only its author or a designer can change this topic"
                    )

            signature = _signature(access, self.author)
            if point is not None:
                return comments.move(number, point, check=check, author=signature)
            commit = store.head(branch) if isinstance(branch, str) else None
            return comments.set_state(
                number,
                state,
                by=who,
                branch=branch if commit else None,
                commit=commit,
                check=check,
                author=signature,
            )

        return await self._commentChange(request, "comment", change)

    async def _organizeComment(self, request, number: int, body: dict):
        assignee = ...
        if "assignee" in body:
            username = body["assignee"]
            if username is None or username == "":
                assignee = None
            elif not isinstance(username, str):
                raise web.HTTPBadRequest(text="assignee must be a username")
            elif self.directory is None:
                assignee = person(username[:100], username[:100])
            else:
                members = await self.projectMembers(request.match_info["name"])
                member = next((m for m in members if m["username"] == username), None)
                if member is None:
                    raise web.HTTPBadRequest(
                        text=f"{username} is not a member of this project"
                    )
                assignee = person(
                    member["username"], member.get("name"), member.get("uid")
                )
        labels = body["labels"] if "labels" in body else ...

        def change(comments, access, who, store):
            return comments.organize(
                number,
                assignee=assignee,
                labels=labels,
                author=_signature(access, self.author),
            )

        return await self._commentChange(request, "edit", change)

    async def editMessageHandler(self, request) -> web.Response:
        number = _number(request)
        messageId = _messageId(request)
        body = await _jsonBody(request)

        def change(comments, access, who, store):
            def check(issue, message):
                if not _isAuthor(message, who):
                    raise CommentForbidden("only its author can edit a message")

            return comments.edit_message(
                number,
                messageId,
                body.get("text"),
                check=check,
                author=_signature(access, self.author),
            )

        return await self._commentChange(request, "comment", change)

    async def deleteMessageHandler(self, request) -> web.Response:
        number = _number(request)
        messageId = _messageId(request)

        def change(comments, access, who, store):
            def check(issue, message):
                if not (_can(access, "moderate") or _isAuthor(message, who)):
                    raise CommentForbidden(
                        "only its author or a manager can delete a message"
                    )

            return comments.delete_message(
                number, messageId, check=check, author=_signature(access, self.author)
            )

        return await self._commentChange(request, "comment", change)

    async def deleteCommentHandler(self, request) -> web.Response:
        number = _number(request)

        def change(comments, access, who, store):
            def check(issue):
                if _can(access, "moderate"):
                    return
                if all(_isAuthor(m, who) for m in issue["messages"]):
                    return
                raise CommentForbidden(
                    "only a manager can delete a topic others have written in"
                )

            comments.delete(number, check=check, author=_signature(access, self.author))
            return None

        return await self._commentChange(request, "comment", change)
