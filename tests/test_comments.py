"""Comments on glyphs: the store on refs/hive/comments and its routes."""

import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from conftest import run
from fontra_hive.access import DevDirectory
from fontra_hive.comments import (
    COMMENTS_REF,
    CommentError,
    CommentNotFound,
    CommentStore,
    person,
)
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.projectmanager import DEV_USER_COOKIE, DevHiveProjectManager, glyphPath

ME = Signature("Jérémie", "j@example.com")
ANA = person("ana", "Ana")
BOB = person("bob", "Bob")
SOURCE = {"layer": "MM-Bold", "name": "Bold", "location": {"wght": 700}}


@pytest.fixture
def root(tmp_path, fixture_fontra):
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    store.close()
    return root


@pytest.fixture
def comments(root):
    store = GitRepoStore.open(root / "Mutator.git")
    yield CommentStore(store)
    store.close()


def new(comments, glyph="H", by=ANA, text="The bar is too low", **kwargs):
    return comments.create(
        glyph=glyph,
        source=kwargs.pop("source", SOURCE),
        point=kwargs.pop("point", {"x": 312, "y": 540.5}),
        text=text,
        branch="main",
        commit="abc",
        by=by,
        **kwargs,
    )


# --- the store ------------------------------------------------------------------


def test_topics_are_numbered_and_kept_out_of_the_font(comments):
    head = comments.store.head("main")
    first = new(comments)
    second = new(comments, glyph="O", by=BOB, text="Too round")
    assert (first["number"], second["number"]) == (1, 2)
    assert first["state"] == "open" and first["point"] == {"x": 312, "y": 540.5}
    assert first["messages"][0]["text"] == "The bar is too low"
    assert first["source"] == SOURCE
    # The font's branch did not move, and its tree knows nothing of it.
    assert comments.store.head("main") == head
    assert comments.store.branches() == ["main"]
    assert [i["number"] for i in comments.issues()] == [1, 2]
    assert [i["number"] for i in comments.issues("O")] == [2]
    assert comments.issue(2)["author"] == BOB


def test_numbers_are_not_reused_after_a_deletion(comments):
    new(comments)
    new(comments)
    comments.delete(2)
    assert new(comments)["number"] == 3
    with pytest.raises(CommentNotFound):
        comments.issue(2)


def test_replies_state_moves_and_edits(comments):
    new(comments)
    issue = comments.reply(1, "  Fixed?  ", by=BOB)
    assert [m["id"] for m in issue["messages"]] == [1, 2]
    assert issue["messages"][1]["text"] == "Fixed?"
    issue = comments.set_state(1, "resolved", by=BOB, branch="main", commit="def")
    assert issue["state"] == "resolved"
    assert issue["resolved"]["by"] == BOB and issue["resolved"]["commit"] == "def"
    with pytest.raises(CommentError):
        comments.set_state(1, "resolved", by=BOB)
    issue = comments.set_state(1, "open", by=ANA)
    assert issue["state"] == "open" and issue["resolved"] is None
    assert comments.move(1, {"x": 1, "y": -2})["point"] == {"x": 1, "y": -2}
    issue = comments.edit_message(1, 2, "Fixed.")
    assert issue["messages"][1]["text"] == "Fixed." and issue["messages"][1]["edited"]
    issue = comments.delete_message(1, 2)
    assert [m["id"] for m in issue["messages"]] == [1]
    with pytest.raises(CommentError):
        comments.delete_message(1, 1)  # the opening message: delete the topic
    assert new(comments)["messages"][0]["id"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"text": ""},
        {"text": "   "},
        {"text": "x" * 10_001},
        {"text": "a\x00b"},
        {"glyph": ""},
        {"point": {"x": "1", "y": 2}},
        {"point": {"x": float("nan"), "y": 2}},
        {"point": {"x": 1e9, "y": 2}},
        {"point": {"x": True, "y": 2}},
        {"source": {"name": "Bold"}},
        {"source": {"layer": "a", "location": {"wght": "bold"}}},
    ],
)
def test_bad_input_is_refused(comments, bad):
    with pytest.raises(CommentError):
        new(comments, **bad)
    assert comments.head() is None


def test_each_change_is_a_commit_by_its_author(comments):
    new(comments, author=Signature("Ana", "ana@hive"))
    comments.reply(1, "ok", by=BOB, author=Signature("Bob", "bob@hive"))
    repo = comments.repo
    log = list(repo.get_walker(include=[repo.refs[COMMENTS_REF]]))
    assert [e.commit.author.decode() for e in log] == [
        "Bob <bob@hive>",
        "Ana <ana@hive>",
    ]
    assert log[1].commit.message.decode().startswith("Comment #1 on H")
    assert "Hive-Comment: 1" in log[0].commit.message.decode()


def test_a_comment_made_meanwhile_is_kept(root):
    """Two people comment at the same time: the reference moved between
    reading and writing, the second change is re-applied on top."""
    other = GitRepoStore.open(root / "Mutator.git")
    store = GitRepoStore.open(root / "Mutator.git")

    class Racing(CommentStore):
        raced = False

        def _files(self, commit_sha):
            if not Racing.raced:
                Racing.raced = True
                new(CommentStore(other), glyph="O", by=BOB)
            return super()._files(commit_sha)

    issue = new(Racing(store), glyph="H")
    assert Racing.raced and issue["number"] == 2
    listed = CommentStore(store).issues()
    assert [(i["number"], i["glyph"]) for i in listed] == [(1, "O"), (2, "H")]
    other.close()
    store.close()


# --- routes ---------------------------------------------------------------------

USERS = {
    "users": {
        "owner": {"name": "Owner", "email": "o@example.com"},
        "mona": {"name": "Mona Manager", "email": "m@example.com"},
        "dan": {"name": "Dan Designer", "email": "d@example.com"},
        "ria": {"name": "Ria Reviewer", "email": "r@example.com"},
        "rex": {"name": "Rex Reviewer", "email": "rx@example.com"},
        "otto": {"name": "Otto Observer", "email": "ot@example.com"},
    },
    "projects": {
        "Mutator": {
            "owner": "owner",
            "collaborators": {
                "mona": "manager",
                "dan": "designer",
                "ria": "reviewer",
                "rex": "reviewer",
                "otto": "observer",
            },
        }
    },
}


def request(user=None, body=None, number=None, message=None, **query):
    match_info = {"name": "Mutator"}
    if number is not None:
        match_info["number"] = str(number)
    if message is not None:
        match_info["message"] = str(message)

    async def read_json():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        match_info=match_info,
        query=query,
        cookies={DEV_USER_COOKIE: user} if user else {},
        headers={},
        host="localhost:8000",
        method="POST",
        json=read_json,
    )


async def answer(response):
    return json.loads(response.body)


@pytest.fixture
def team(root, tmp_path):
    path = tmp_path / "hive-dev-users.json"
    path.write_text(json.dumps(USERS), encoding="utf-8")
    return DevHiveProjectManager(root, directory=DevDirectory(path))


NEW = {"glyph": "H", "source": SOURCE, "point": {"x": 10, "y": 20}, "text": "Hm"}


def test_routes_are_registered(team):
    paths = {(r.method, r.path) for r in team.webRoutes() if hasattr(r, "path")}
    base = "/api/hive/projects/{name}/comments"
    assert ("GET", base) in paths and ("POST", base) in paths
    assert ("PATCH", base + "/{number}/messages/{message}") in paths


def test_a_reviewer_opens_and_answers_a_topic(team, root):
    async def go():
        created = await answer(
            await team.createCommentHandler(request("ria", body=NEW))
        )
        issue = created["issue"]
        assert issue["number"] == 1 and issue["branch"] == "main"
        assert issue["author"] == {"username": "ria", "name": "Ria Reviewer"}
        store = GitRepoStore.open(root / "Mutator.git")
        assert issue["commit"] == store.head("main")
        store.close()

        listed = await answer(await team.commentsHandler(request("otto", glyph="H")))
        assert [i["number"] for i in listed["issues"]] == [1]
        assert listed["head"] == created["head"]
        assert listed["can"] == {
            "comment": False,
            "resolveAny": False,
            "moderate": False,
        }
        assert (await answer(await team.commentsHandler(request("ria"))))["can"] == {
            "comment": True,
            "resolveAny": False,
            "moderate": False,
        }
        head = await answer(await team.commentsHeadHandler(request("otto")))
        assert head == {"head": created["head"]}

        reply = await answer(
            await team.replyCommentHandler(
                request("dan", body={"text": "Raised it"}, number=1)
            )
        )
        assert reply["issue"]["messages"][1]["author"]["username"] == "dan"

    run(go())


def test_observers_and_strangers_cannot_comment(team):
    async def go():
        with pytest.raises(web.HTTPForbidden):
            await team.createCommentHandler(request("otto", body=NEW))
        with pytest.raises(web.HTTPNotFound):
            await team.commentsHandler(request("nobody"))

    run(go())


def test_who_may_resolve_move_edit_and_delete(team):
    async def go():
        await team.createCommentHandler(request("ria", body=NEW))
        await team.replyCommentHandler(request("rex", body={"text": "+1"}, number=1))

        # Another reviewer can neither resolve nor move it; its author can.
        with pytest.raises(web.HTTPForbidden):
            await team.updateCommentHandler(
                request("rex", body={"state": "resolved"}, number=1)
            )
        with pytest.raises(web.HTTPForbidden):
            await team.updateCommentHandler(
                request("rex", body={"point": {"x": 0, "y": 0}}, number=1)
            )
        resolved = await answer(
            await team.updateCommentHandler(
                request("ria", body={"state": "resolved"}, number=1)
            )
        )
        assert resolved["issue"]["resolved"]["by"]["username"] == "ria"
        # A designer reopens it and moves it.
        await team.updateCommentHandler(
            request("dan", body={"state": "open"}, number=1)
        )
        moved = await answer(
            await team.updateCommentHandler(
                request("dan", body={"point": {"x": 5, "y": 6}}, number=1)
            )
        )
        assert moved["issue"]["point"] == {"x": 5, "y": 6}
        with pytest.raises(web.HTTPBadRequest):
            await team.updateCommentHandler(request("dan", body={}, number=1))

        # Only its author edits a message, even a manager cannot.
        with pytest.raises(web.HTTPForbidden):
            await team.editMessageHandler(
                request("mona", body={"text": "x"}, number=1, message=2)
            )
        await team.editMessageHandler(
            request("rex", body={"text": "+1!"}, number=1, message=2)
        )
        # Deleting: the author of a reply, or a manager.
        with pytest.raises(web.HTTPForbidden):
            await team.deleteMessageHandler(request("ria", number=1, message=2))
        with pytest.raises(web.HTTPBadRequest):
            await team.deleteMessageHandler(request("mona", number=1, message=1))
        # Ria cannot delete her topic: Rex wrote in it.
        with pytest.raises(web.HTTPForbidden):
            await team.deleteCommentHandler(request("ria", number=1))
        await team.deleteMessageHandler(request("rex", number=1, message=2))
        # Now she is its only writer, she may.
        await team.deleteCommentHandler(request("ria", number=1))
        with pytest.raises(web.HTTPNotFound):
            await team.deleteCommentHandler(request("mona", number=1))

        await team.createCommentHandler(request("ria", body=NEW))
        await team.replyCommentHandler(request("rex", body={"text": "+1"}, number=2))
        gone = await answer(await team.deleteCommentHandler(request("mona", number=2)))
        assert gone["issue"] is None
        listed = await answer(await team.commentsHandler(request("ria")))
        assert listed["issues"] == []

    run(go())


def test_bad_requests(team):
    async def go():
        with pytest.raises(web.HTTPBadRequest):
            await team.createCommentHandler(request("ria", body={**NEW, "text": ""}))
        with pytest.raises(web.HTTPBadRequest):
            await team.createCommentHandler(
                request("ria", body={**NEW, "branch": "nope"})
            )
        with pytest.raises(web.HTTPBadRequest):
            await team.createCommentHandler(request("ria", body=None))
        with pytest.raises(web.HTTPNotFound):
            await team.replyCommentHandler(request("ria", body={"text": "a"}, number=9))
        with pytest.raises(web.HTTPNotFound):
            await team.replyCommentHandler(
                request("ria", body={"text": "a"}, number="x")
            )

    run(go())


def test_branch_names_cannot_reach_outside_the_repository(team, root, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("TOPSECRET" * 8)
    store = GitRepoStore.open(root / "Mutator.git")
    assert store.head("../../../secret") is None
    with pytest.raises(KeyError):
        store.resolve("../../../secret")
    store.close()

    async def go():
        for branch in ["../../../secret", "/etc/passwd", "a/../b", "x\x00"]:
            with pytest.raises(web.HTTPBadRequest):
                await team.createCommentHandler(
                    request("ria", body={**NEW, "branch": branch})
                )
        await team.createCommentHandler(request("ria", body=NEW))
        resolved = await answer(
            await team.updateCommentHandler(
                request(
                    "ria",
                    body={"state": "resolved", "branch": "../../../secret"},
                    number=1,
                )
            )
        )
        assert resolved["issue"]["resolved"]["commit"] is None
        assert resolved["issue"]["resolved"]["branch"] is None

    run(go())


def test_authors_are_recognised_by_their_stable_id(comments):
    old = person("ana", "Ana", uid="u-1")
    new(comments, by=old)
    renamed = person("ana-h", "Ana", uid="u-1")
    usurper = person("ana", "Other Ana", uid="u-2")
    from fontra_hive.comments import same_person

    issue = comments.issue(1)
    assert same_person(issue["author"], renamed)
    assert not same_person(issue["author"], usurper)
    assert same_person(person("dev", "Dev"), person("dev", "Someone"))


def test_a_topic_has_a_bounded_thread(comments, monkeypatch):
    import fontra_hive.comments as module

    monkeypatch.setattr(module, "MAX_MESSAGES", 3)
    new(comments)
    comments.reply(1, "two", by=BOB)
    comments.reply(1, "three", by=BOB)
    with pytest.raises(CommentError):
        comments.reply(1, "four", by=BOB)


def test_without_accounts_everyone_may_do_everything(root):
    manager = DevHiveProjectManager(root, author=ME)

    async def go():
        created = await answer(await manager.createCommentHandler(request(body=NEW)))
        assert created["issue"]["author"] == {"username": "dev", "name": "Jérémie"}
        listed = await answer(await manager.commentsHandler(request()))
        assert listed["can"] == {"comment": True, "resolveAny": True, "moderate": True}
        await manager.deleteCommentHandler(request(number=1))

    run(go())


def test_a_read_only_server_refuses_comments(root):
    manager = DevHiveProjectManager(root, readOnly=True)

    async def go():
        with pytest.raises(web.HTTPForbidden):
            await manager.createCommentHandler(request(body=NEW))
        listed = await answer(await manager.commentsHandler(request()))
        assert listed["can"]["comment"] is False

    run(go())


def test_comments_do_not_disturb_the_font_history(team, root):
    async def go():
        store = GitRepoStore.open(root / "Mutator.git")
        store.commit({glyphPath("A"): b"{}\n"}, message="Edit A", author=ME)
        before = store.head("main")
        store.close()
        await team.createCommentHandler(request("ria", body={**NEW, "glyph": "A"}))
        log = await answer(await team.logHandler(request("ria", glyph="A")))
        assert log["head"] == before
        assert [c["message"].splitlines()[0] for c in log["commits"]][0] == "Edit A"
        branches = await answer(await team.branchesHandler(request("ria")))
        assert [b["name"] for b in branches["branches"]] == ["main"]

    run(go())
