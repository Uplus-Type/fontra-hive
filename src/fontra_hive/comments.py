"""Comments on glyphs: numbered topics pinned to a point of a glyph's source.

Kept in the project's git repository, outside the font's history: a commit
chain on ``refs/hive/comments`` whose tree holds one JSON file per topic
(``issues/<n>.json``) and a counter (``counter.json``) so that numbers are
never reused, even after a topic is deleted. Like ``refs/hive/meta`` it
travels with the backups (bundles of all references) but a plain ``git
clone``, which fetches branches and tags only, does not see it.

Every change is one commit whose author is the person who made it, and the
reference is moved with compare-and-swap: two people commenting at the same
time both get their comment, one after the other.

A topic::

    {
      "number": 12,
      "glyph": "H",
      "source": {"layer": "MM-Bold", "name": "Bold", "location": {"wght": 700}},
      "point": {"x": 312, "y": 540},
      "branch": "main",
      "commit": "<head of the branch when the topic was opened>",
      "state": "open" | "resolved",
      "author": {"username": "ana", "name": "Ana", "uid": "…"},
      "created": "2026-09-30T10:12:00Z",
      "resolved": null | {"by": {...}, "at": "...", "branch": "...", "commit": "..."},
      "assignee": null,
      "labels": [],
      "messages": [
        {"id": 1, "author": {...}, "created": "...", "edited": null, "text": "..."}
      ]
    }
"""

from __future__ import annotations

import datetime
import json
import math
import time
from typing import Any, Callable

from dulwich.file import FileLocked
from dulwich.index import commit_tree
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit

from .gitstore import (
    FILE_MODE,
    SERVER_SIGNATURE,
    GitRepoStore,
    RefMovedError,
    Signature,
    swap_ref,
)

COMMENTS_REF = b"refs/hive/comments"
COUNTER_FILE = "counter.json"
ISSUES_DIR = "issues/"

MAX_TEXT = 10_000
MAX_MESSAGES = 500  # per topic
MAX_NAME = 200
MAX_COORDINATE = 1_000_000
MAX_LOCATION_AXES = 64
STATES = ("open", "resolved")


class CommentError(ValueError):
    """A request that cannot be applied (bad input or unknown topic)."""


class CommentNotFound(CommentError):
    pass


class CommentForbidden(Exception):
    """The requester may not make this change (raised by the ``check``
    callbacks of the routes, which know who is asking)."""


def issue_path(number: int) -> str:
    return f"{ISSUES_DIR}{number:04d}.json"


def now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# --- validation ---------------------------------------------------------------


def clean_text(text: Any) -> str:
    if not isinstance(text, str):
        raise CommentError("text is required")
    text = text.strip()
    if not text:
        raise CommentError("text is required")
    if len(text) > MAX_TEXT:
        raise CommentError(f"text is longer than {MAX_TEXT} characters")
    if "\x00" in text:
        raise CommentError("text contains a NUL character")
    return text


def clean_name(value: Any, what: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_NAME:
        raise CommentError(f"{what} is required (at most {MAX_NAME} characters)")
    if "\x00" in value:
        raise CommentError(f"{what} contains a NUL character")
    return value


def clean_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommentError(f"{what} must be a number")
    if not math.isfinite(value) or abs(value) > MAX_COORDINATE:
        raise CommentError(f"{what} is out of range")
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def clean_point(point: Any) -> dict:
    if not isinstance(point, dict):
        raise CommentError("point is required")
    return {
        "x": clean_number(point.get("x"), "x"),
        "y": clean_number(point.get("y"), "y"),
    }


def clean_source(source: Any) -> dict:
    if not isinstance(source, dict):
        raise CommentError("source is required")
    location = source.get("location") or {}
    if not isinstance(location, dict) or len(location) > MAX_LOCATION_AXES:
        raise CommentError("source location must be an object of axis values")
    return {
        "layer": clean_name(source.get("layer"), "source layer"),
        "name": clean_name(source.get("name") or source.get("layer"), "source name"),
        "location": {
            clean_name(axis, "axis name"): clean_number(value, f"axis {axis}")
            for axis, value in sorted(location.items())
        },
    }


def person(username: str, name: str | None, uid: str | None = None) -> dict:
    """Who wrote something. ``uid`` (hive-api's stable id) survives a change
    of username; the username and name are what is shown."""
    who = {"username": username, "name": name or username}
    if uid:
        who["uid"] = uid
    return who


def same_person(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    if a.get("uid") and b.get("uid"):
        return a["uid"] == b["uid"]
    return a.get("username") == b.get("username")


# --- the store ----------------------------------------------------------------


class CommentStore:
    """The topics of one project, on ``refs/hive/comments`` of its repository."""

    def __init__(self, store: GitRepoStore):
        self.store = store
        self.repo = store.repo

    def head(self) -> str | None:
        sha = self.repo.refs.read_ref(COMMENTS_REF)
        return sha.decode("ascii") if sha else None

    def _files(self, commit_sha: bytes | None) -> dict[str, bytes]:
        """Path -> blob id of every file under the reference's commit."""
        if not commit_sha:
            return {}
        tree = self.repo[commit_sha].tree
        return {
            entry.path.decode("utf-8"): entry.sha
            for entry in iter_tree_contents(self.repo.object_store, tree)
        }

    def _read(self, blob_id: bytes) -> Any:
        return json.loads(self.repo[blob_id].data)

    def issues(self, glyph: str | None = None) -> list[dict]:
        """Every topic (or the topics of one glyph), by number."""
        files = self._files(self.repo.refs.read_ref(COMMENTS_REF))
        result = []
        for path, blob_id in sorted(files.items()):
            if not path.startswith(ISSUES_DIR):
                continue
            try:
                issue = self._read(blob_id)
            except ValueError:
                continue
            if glyph is None or issue.get("glyph") == glyph:
                result.append(issue)
        result.sort(key=lambda issue: issue.get("number", 0))
        return result

    def issue(self, number: int) -> dict:
        files = self._files(self.repo.refs.read_ref(COMMENTS_REF))
        blob_id = files.get(issue_path(number))
        if blob_id is None:
            raise CommentNotFound(f"no comment #{number}")
        return self._read(blob_id)

    def _change(
        self,
        apply: Callable[
            [dict[str, bytes], Callable[[str], Any]], tuple[dict, str, Any]
        ],
        author: Signature,
        _attempts: int = 8,
    ) -> Any:
        """Commit a change under compare-and-swap. ``apply(files, read)``
        returns ``(changes, message, result)`` where ``changes`` maps paths to
        new JSON values (None deletes the file)."""
        for _ in range(_attempts):
            old = self.repo.refs.read_ref(COMMENTS_REF)
            files = self._files(old)

            def read(path, files=files):
                blob_id = files.get(path)
                return None if blob_id is None else self._read(blob_id)

            changes, message, result = apply(files, read)
            store = self.repo.object_store
            entries = dict(files)
            for path, value in changes.items():
                if value is None:
                    entries.pop(path, None)
                    continue
                blob = Blob.from_string(
                    (json.dumps(value, indent=1, ensure_ascii=False) + "\n").encode(
                        "utf-8"
                    )
                )
                store.add_object(blob)
                entries[path] = blob.id
            tree_id = commit_tree(
                store,
                [
                    (path.encode("utf-8"), sha, FILE_MODE)
                    for path, sha in sorted(entries.items())
                ],
            )
            now = int(time.time())
            c = Commit()
            c.tree = tree_id
            c.parents = [old] if old else []
            c.author = author.encode()
            c.committer = SERVER_SIGNATURE.encode()
            c.author_time = c.commit_time = now
            c.author_timezone = c.commit_timezone = 0
            c.encoding = b"UTF-8"
            c.message = message.encode("utf-8")
            store.add_object(c)
            try:
                if swap_ref(self.repo.refs, COMMENTS_REF, old, c.id):
                    return result
            except FileLocked:  # someone else is moving it right now
                time.sleep(0.01)
        raise RefMovedError("refs/hive/comments", None, None)

    # --- operations ---------------------------------------------------------

    def create(
        self,
        *,
        glyph: str,
        source: dict,
        point: dict,
        text: str,
        branch: str,
        commit: str | None,
        by: dict,
        author: Signature = SERVER_SIGNATURE,
    ) -> dict:
        glyph = clean_name(glyph, "glyph")
        source = clean_source(source)
        point = clean_point(point)
        text = clean_text(text)
        branch = clean_name(branch, "branch")

        def apply(files, read):
            counter = read(COUNTER_FILE) or {}
            existing = [
                int(path[len(ISSUES_DIR) : -len(".json")])
                for path in files
                if path.startswith(ISSUES_DIR) and path.endswith(".json")
            ]
            number = max([counter.get("last", 0), *existing]) + 1
            created = now_iso()
            issue = {
                "number": number,
                "glyph": glyph,
                "source": source,
                "point": point,
                "branch": branch,
                "commit": commit,
                "state": "open",
                "author": by,
                "created": created,
                "resolved": None,
                "assignee": None,
                "labels": [],
                "messages": [
                    {
                        "id": 1,
                        "author": by,
                        "created": created,
                        "edited": None,
                        "text": text,
                    }
                ],
            }
            message = f"Comment #{number} on {glyph}\n\nHive-Comment: {number}\n"
            return (
                {issue_path(number): issue, COUNTER_FILE: {"last": number}},
                message,
                issue,
            )

        return self._change(apply, author)

    def _update_issue(
        self,
        number: int,
        edit: Callable[[dict], str],
        author: Signature,
    ) -> dict:
        """Change one topic: ``edit(issue)`` changes it in place and returns
        the first line of the commit message (it may raise CommentError)."""

        def apply(files, read):
            issue = read(issue_path(number))
            if issue is None:
                raise CommentNotFound(f"no comment #{number}")
            summary = edit(issue)
            message = f"{summary}\n\nHive-Comment: {number}\n"
            return {issue_path(number): issue}, message, issue

        return self._change(apply, author)

    def reply(
        self, number: int, text: str, *, by: dict, author: Signature = SERVER_SIGNATURE
    ) -> dict:
        text = clean_text(text)

        def edit(issue):
            if len(issue["messages"]) >= MAX_MESSAGES:
                raise CommentError(
                    f"#{number} has {MAX_MESSAGES} messages: open a new topic"
                )
            ids = [m.get("id", 0) for m in issue["messages"]]
            issue["messages"].append(
                {
                    "id": max(ids, default=0) + 1,
                    "author": by,
                    "created": now_iso(),
                    "edited": None,
                    "text": text,
                }
            )
            return f"Reply to #{number}"

        return self._update_issue(number, edit, author)

    def set_state(
        self,
        number: int,
        state: str,
        *,
        by: dict,
        branch: str | None = None,
        commit: str | None = None,
        check: Callable[[dict], None] | None = None,
        author: Signature = SERVER_SIGNATURE,
    ) -> dict:
        if state not in STATES:
            raise CommentError(f"state must be one of {', '.join(STATES)}")

        def edit(issue):
            if check is not None:
                check(issue)
            if issue["state"] == state:
                raise CommentError(f"#{number} is already {state}")
            issue["state"] = state
            if state == "resolved":
                issue["resolved"] = {
                    "by": by,
                    "at": now_iso(),
                    "branch": branch,
                    "commit": commit,
                }
                return f"Resolve #{number}"
            issue["resolved"] = None
            return f"Reopen #{number}"

        return self._update_issue(number, edit, author)

    def move(
        self,
        number: int,
        point: dict,
        *,
        check: Callable[[dict], None] | None = None,
        author: Signature = SERVER_SIGNATURE,
    ) -> dict:
        point = clean_point(point)

        def edit(issue):
            if check is not None:
                check(issue)
            issue["point"] = point
            return f"Move #{number}"

        return self._update_issue(number, edit, author)

    def edit_message(
        self,
        number: int,
        message_id: int,
        text: str,
        *,
        check: Callable[[dict, dict], None] | None = None,
        author: Signature = SERVER_SIGNATURE,
    ) -> dict:
        text = clean_text(text)

        def edit(issue):
            message = _message(issue, message_id)
            if check is not None:
                check(issue, message)
            message["text"] = text
            message["edited"] = now_iso()
            return f"Edit a message of #{number}"

        return self._update_issue(number, edit, author)

    def delete_message(
        self,
        number: int,
        message_id: int,
        *,
        check: Callable[[dict, dict], None] | None = None,
        author: Signature = SERVER_SIGNATURE,
    ) -> dict:
        def edit(issue):
            message = _message(issue, message_id)
            if issue["messages"][0] is message:
                raise CommentError(
                    "the first message opens the topic: delete the topic instead"
                )
            if check is not None:
                check(issue, message)
            issue["messages"].remove(message)
            return f"Delete a message of #{number}"

        return self._update_issue(number, edit, author)

    def delete(
        self,
        number: int,
        *,
        check: Callable[[dict], None] | None = None,
        author: Signature = SERVER_SIGNATURE,
    ) -> None:
        def apply(files, read):
            issue = read(issue_path(number))
            if issue is None:
                raise CommentNotFound(f"no comment #{number}")
            if check is not None:
                check(issue)
            message = (
                f"Delete #{number} on {issue.get('glyph')}\n\nHive-Comment: {number}\n"
            )
            return {issue_path(number): None}, message, None

        self._change(apply, author)


def _message(issue: dict, message_id: int) -> dict:
    for message in issue["messages"]:
        if message.get("id") == message_id:
            return message
    raise CommentNotFound(f"no message {message_id} in #{issue['number']}")
