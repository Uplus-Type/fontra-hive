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
            "organize": False,
            "moderate": False,
            "administer": False,
        }
        assert (await answer(await team.commentsHandler(request("ria"))))["can"] == {
            "comment": True,
            "resolveAny": False,
            "organize": False,
            "moderate": False,
            "administer": False,
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
        assert listed["can"] == {
            "comment": True,
            "resolveAny": True,
            "organize": True,
            "moderate": True,
            "administer": True,
        }
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


# --- step B: assignee and labels -------------------------------------------------


def test_labels_are_cleaned():
    from fontra_hive.comments import clean_labels

    assert clean_labels(["  curve ", "Curve", "client  review", ""]) == [
        "curve",
        "client review",
    ]
    for bad in ["curve", [1], ["x" * 41], [f"l{i}" for i in range(11)]]:
        with pytest.raises(CommentError):
            clean_labels(bad)


def test_designers_assign_and_label(team):
    async def go():
        await team.createCommentHandler(request("ria", body=NEW))
        listed = await answer(await team.commentsHandler(request("ria")))
        assert {m["username"] for m in listed["members"]} >= {"dan", "ria", "owner"}
        assert listed["can"]["organize"] is False
        with pytest.raises(web.HTTPForbidden):
            await team.updateCommentHandler(
                request("ria", body={"assignee": "dan"}, number=1)
            )
        done = await answer(
            await team.updateCommentHandler(
                request(
                    "dan",
                    body={"assignee": "dan", "labels": ["curve", "Curve"]},
                    number=1,
                )
            )
        )
        assert done["issue"]["assignee"] == {"username": "dan", "name": "Dan Designer"}
        assert done["issue"]["labels"] == ["curve"]
        # Labels alone keep the assignee; null unassigns.
        done = await answer(
            await team.updateCommentHandler(
                request("dan", body={"labels": []}, number=1)
            )
        )
        assert done["issue"]["assignee"]["username"] == "dan"
        done = await answer(
            await team.updateCommentHandler(
                request("dan", body={"assignee": None}, number=1)
            )
        )
        assert done["issue"]["assignee"] is None
        with pytest.raises(web.HTTPBadRequest):
            await team.updateCommentHandler(
                request("dan", body={"assignee": "stranger"}, number=1)
            )
        with pytest.raises(web.HTTPBadRequest):
            await team.updateCommentHandler(
                request("dan", body={"assignee": "dan", "state": "resolved"}, number=1)
            )

    run(go())


def test_hive_api_members_are_cached():
    from fontra_hive.hiveapi import HiveApi

    api = HiveApi("http://hive-api", "key")
    calls = []

    async def fake_get(path, **params):
        calls.append((path, params))
        return {
            "members": [
                {
                    "username": "ana",
                    "name": "Ana",
                    "uid": "u1",
                    "avatar": None,
                    "role": "reviewer",
                    "email": "never@shown",
                }
            ]
        }

    api._get = fake_get

    async def go():
        first = await api.members("uplustype/Mutator")
        again = await api.members("UplusType/Mutator")
        assert (
            first
            == again
            == [{"username": "ana", "name": "Ana", "uid": "u1", "role": "reviewer"}]
        )
        assert calls == [("/api/internal/members", {"project": "uplustype/Mutator"})]

    run(go())


def test_a_topic_may_be_given_a_title(team):
    async def go():
        await team.createCommentHandler(request("ria", body=NEW))
        issue = (await answer(await team.commentsHandler(request("ria"))))["issues"][0]
        assert issue["title"] is None
        with pytest.raises(web.HTTPForbidden):  # another reviewer
            await team.updateCommentHandler(
                request("rex", body={"title": "Mine"}, number=1)
            )
        done = await answer(
            await team.updateCommentHandler(
                request("ria", body={"title": "  Terminal   too heavy "}, number=1)
            )
        )
        assert done["issue"]["title"] == "Terminal too heavy"
        for other in ["dan", "mona"]:  # a designer, a manager
            with pytest.raises(web.HTTPForbidden):
                await team.updateCommentHandler(
                    request(other, body={"title": "x"}, number=1)
                )
        done = await answer(  # the project's owner (admin)
            await team.updateCommentHandler(
                request("owner", body={"title": ""}, number=1)
            )
        )
        assert done["issue"]["title"] is None
        for bad in [
            {"title": 3},
            {"title": "x" * 201},
            {"title": "a", "state": "open"},
        ]:
            with pytest.raises(web.HTTPBadRequest):
                await team.updateCommentHandler(request("ria", body=bad, number=1))

    run(go())


# --- the past of a topic, deleted topics ------------------------------------------


def test_history_of_a_topic_and_restoring_a_deleted_one(comments):
    new(comments, author=Signature("Ana", "ana@hive"))
    comments.reply(1, "first try", by=BOB, author=Signature("Bob", "bob@hive"))
    comments.edit_message(1, 2, "second try")
    new(comments, glyph="O")
    history = comments.history(1)
    assert [h["summary"] for h in history] == [
        "Edit a message of #1",
        "Reply to #1",
        "Comment #1 on H",
    ]
    assert history[-1]["author"] == "Ana"
    assert history[1]["issue"]["messages"][1]["text"] == "first try"
    assert comments.deleted() == []

    comments.delete(1)
    deleted = comments.deleted()
    assert [d["number"] for d in deleted] == [1]
    assert deleted[0]["issue"]["messages"][1]["text"] == "second try"
    assert comments.history(1)[0]["issue"] is None
    restored = comments.restore(1)
    assert restored["messages"][1]["text"] == "second try"
    assert comments.deleted() == []
    assert [i["number"] for i in comments.issues()] == [1, 2]
    with pytest.raises(CommentNotFound):
        comments.restore(1)
    with pytest.raises(CommentNotFound):
        comments.history(9)


def test_routes_for_the_past(team):
    async def go():
        await team.createCommentHandler(request("ria", body=NEW))
        await team.createCommentHandler(request("ria", body=NEW))
        await team.updateCommentHandler(
            request("ria", body={"state": "resolved"}, number=2)
        )
        summary = await answer(await team.commentsSummaryHandler(request("otto")))
        assert summary["open"] == 1 and summary["resolved"] == 1
        history = await answer(
            await team.commentHistoryHandler(request("otto", number=1))
        )
        assert history["history"][0]["summary"] == "Comment #1 on H"
        await team.deleteCommentHandler(request("mona", number=1))
        with pytest.raises(web.HTTPForbidden):
            await team.deletedCommentsHandler(request("dan"))
        deleted = await answer(await team.deletedCommentsHandler(request("mona")))
        assert [d["number"] for d in deleted["deleted"]] == [1]
        assert deleted["deleted"][0]["deletedBy"] == "Mona Manager"
        with pytest.raises(web.HTTPForbidden):
            await team.restoreCommentHandler(request("ria", number=1))
        back = await answer(await team.restoreCommentHandler(request("mona", number=1)))
        assert back["issue"]["number"] == 1

    run(go())


def test_the_summary_never_creates_a_repository(tmp_path, fixture_fontra):
    from test_branches import FakeHiveApi

    from fontra_hive.hiveapi import ACCESS_COOKIE
    from fontra_hive.hivemanager import HiveProjectManager

    root = tmp_path / "repos"
    root.mkdir()
    api = FakeHiveApi()

    async def members(project):
        return [{"username": "dan", "name": "Dan Designer", "uid": "u-dan"}]

    api.members = members
    manager = HiveProjectManager(root, api=api, proxy=False)

    def hive(body=None, **match):
        r = request(body=body, **match)
        r.match_info["name"] = "uplustype/Mutator"
        r.cookies = {ACCESS_COOKIE: "cookie"}
        return r

    async def go():
        summary = await answer(await manager.commentsSummaryHandler(hive()))
        assert summary == {"open": 0, "resolved": 0, "font": {"open": 0, "resolved": 0}}
        assert not (root / "p-1.git").exists()
        store = GitRepoStore.create(root / "p-1.git")
        store.import_directory(
            fixture_fontra, branch="trunk", message="Import", author=ME
        )
        store.close()
        created = await answer(await manager.createCommentHandler(hive(body=NEW)))
        # hive-api's stable id is kept with the author; the default branch is the project's.
        assert created["issue"]["author"] == {
            "username": "dan",
            "name": "Dan Designer",
            "uid": "u-dan",
        }
        assert created["issue"]["branch"] == "trunk"
        done = await answer(
            await manager.updateCommentHandler(hive(body={"assignee": "dan"}, number=1))
        )
        assert done["issue"]["assignee"]["uid"] == "u-dan"
        summary = await answer(await manager.commentsSummaryHandler(hive()))
        assert summary == {
            "open": 1,
            "resolved": 0,
            "font": {"open": 0, "resolved": 0},
        }
        await manager.aclose()

    run(go())


# --- step C: sketches ---------------------------------------------------------------


def test_sketches_are_cleaned():
    from fontra_hive.comments import clean_sketch

    assert clean_sketch(None) is None
    assert clean_sketch({"strokes": []}) is None
    assert clean_sketch({"strokes": [[[1.234, 2], [3, 4.56789]]]}) == {
        "strokes": [[[1.23, 2], [3, 4.57]]]
    }
    for bad in [
        [],
        {"strokes": "x"},
        {"strokes": [[[1, 2]]]},  # one point
        {"strokes": [[[1, 2], [3]]]},
        {"strokes": [[[1, 2], ["a", 3]]]},
        {"strokes": [[[1, 2], [3, 4]]] * 101},
        {"strokes": [[[i, i] for i in range(2600)]] * 2},
    ]:
        with pytest.raises(CommentError):
            clean_sketch(bad)


def test_messages_carry_sketches(comments):
    stroke = [[0, 0], [10, 20], [30, 5]]
    issue = new(comments, text="", sketch={"strokes": [stroke]})
    assert issue["messages"][0] == {
        **issue["messages"][0],
        "text": "",
        "sketch": {"strokes": [stroke]},
    }
    with pytest.raises(CommentError):  # neither text nor sketch
        comments.reply(1, "", by=BOB)
    issue = comments.reply(1, "like this", by=BOB, sketch={"strokes": [stroke]})
    assert issue["messages"][1]["sketch"] == {"strokes": [stroke]}
    # Editing the text keeps the sketch; a sketch None removes it.
    issue = comments.edit_message(1, 2, "like that")
    assert issue["messages"][1]["sketch"] == {"strokes": [stroke]}
    issue = comments.edit_message(1, 2, "no drawing", sketch=None)
    assert "sketch" not in issue["messages"][1]
    # A sketch-only message may keep an empty text; one without, not.
    issue = comments.edit_message(1, 1, "")
    assert issue["messages"][0]["text"] == ""
    with pytest.raises(CommentError):
        comments.edit_message(1, 2, "")


def test_sketch_routes(team):
    async def go():
        sketch = {"strokes": [[[0, 0], [5, 5]]]}
        created = await answer(
            await team.createCommentHandler(
                request("ria", body={**NEW, "text": "", "sketch": sketch})
            )
        )
        assert created["issue"]["messages"][0]["sketch"] == sketch
        replied = await answer(
            await team.replyCommentHandler(
                request("rex", body={"text": "ok", "sketch": sketch}, number=1)
            )
        )
        assert replied["issue"]["messages"][1]["sketch"] == sketch
        edited = await answer(
            await team.editMessageHandler(
                request("rex", body={"text": "ok", "sketch": None}, number=1, message=2)
            )
        )
        assert "sketch" not in edited["issue"]["messages"][1]
        with pytest.raises(web.HTTPBadRequest):
            await team.replyCommentHandler(
                request(
                    "rex",
                    body={"text": "x", "sketch": {"strokes": [[[1, 2]]]}},
                    number=1,
                )
            )

    run(go())


# --- step D: topics about the font ---------------------------------------------------


def font_topic(comments, by=ANA, text="Is the family name final?", **kwargs):
    return comments.create(
        glyph=None,
        source=None,
        point=None,
        text=text,
        branch="main",
        commit="abc",
        by=by,
        **kwargs,
    )


def test_a_font_topic_is_a_topic_without_a_glyph(comments):
    new(comments)
    topic = font_topic(comments, title="  Family   name ", anchor="fontinfo")
    new(comments, glyph="O")
    assert topic["number"] == 2  # the same numbers as the glyphs' topics
    assert (topic["glyph"], topic["source"], topic["point"]) == (None, None, None)
    assert topic["title"] == "Family name" and topic["anchor"] == "fontinfo"
    assert (topic["branch"], topic["commit"]) == ("main", "abc")
    assert new(comments)["anchor"] is None
    assert font_topic(comments)["anchor"] is None
    assert [i["number"] for i in comments.issues()] == [1, 2, 3, 4, 5]
    assert [i["number"] for i in comments.issues("H")] == [1, 4]
    assert [i["number"] for i in comments.issues(font_only=True)] == [2, 5]
    assert comments.issues("H", font_only=True) == []
    log = list(comments.repo.get_walker(include=[comments.repo.refs[COMMENTS_REF]]))
    assert log[-2].commit.message.decode().startswith("Comment #2 on the font\n")


def test_a_font_topic_lives_like_the_others(comments):
    font_topic(comments)
    assert comments.reply(1, "Yes", by=BOB)["messages"][1]["text"] == "Yes"
    assert comments.set_title(1, "Name")["title"] == "Name"
    organized = comments.organize(1, assignee=BOB, labels=["naming"])
    assert organized["assignee"] == BOB and organized["labels"] == ["naming"]
    resolved = comments.set_state(1, "resolved", by=BOB, branch="main", commit="d")
    assert resolved["state"] == "resolved"
    assert comments.set_state(1, "open", by=ANA)["resolved"] is None
    assert comments.edit_message(1, 2, "Yes!")["messages"][1]["edited"]
    assert len(comments.history(1)) == 7
    comments.delete(1)
    assert comments.issues() == []
    found = comments.deleted()
    assert [d["number"] for d in found] == [1] and found[0]["issue"]["glyph"] is None
    assert comments.restore(1)["glyph"] is None
    assert comments.issue(1)["labels"] == ["naming"]
    log = list(comments.repo.get_walker(include=[comments.repo.refs[COMMENTS_REF]]))
    assert log[0].commit.message.decode().startswith("Restore #1 on the font\n")
    assert log[1].commit.message.decode().startswith("Delete #1 on the font\n")


STROKES = {"strokes": [[[0, 0], [10, 10]]]}


@pytest.mark.parametrize(
    "bad",
    [
        {"source": SOURCE},
        {"point": {"x": 1, "y": 2}},
        {"sketch": STROKES},
        {"title": 12},
        {"title": "x" * 201},
        {"anchor": ""},
        {"anchor": 3},
        {"anchor": "x" * 201},
    ],
)
def test_what_a_font_topic_cannot_have(comments, bad):
    with pytest.raises(CommentError):
        comments.create(
            **{
                "glyph": None,
                "source": None,
                "point": None,
                "text": "Hm",
                "branch": "main",
                "commit": "abc",
                "by": ANA,
                **bad,
            }
        )
    assert comments.head() is None


def test_a_font_topic_has_no_place_and_no_sketch(comments):
    font_topic(comments)
    with pytest.raises(CommentError):
        comments.move(1, {"x": 1, "y": 2})
    with pytest.raises(CommentError):
        comments.reply(1, "Look", by=BOB, sketch=STROKES)
    with pytest.raises(CommentError):
        comments.edit_message(1, 1, "Look", sketch=STROKES)
    with pytest.raises(CommentError):
        comments.reply(1, "", by=BOB)
    assert len(comments.issue(1)["messages"]) == 1


@pytest.mark.parametrize(
    "missing", [{"point": None}, {"source": None}, {"point": None, "source": None}]
)
def test_a_glyphs_topic_still_needs_its_place(comments, missing):
    with pytest.raises(CommentError):
        new(comments, **missing)


FONT = {"text": "Is the family name final?"}


def test_every_role_on_a_font_topic(team):
    async def status(handler, req):
        try:
            return (await handler(req)).status
        except web.HTTPException as error:
            return error.status

    async def go():
        for user, expected in [
            ("otto", 403),  # observer
            ("nobody", 404),  # not in the project: it does not exist for them
            ("ria", 200),  # reviewer
            ("dan", 200),  # designer
            ("mona", 200),  # manager
            ("owner", 200),
        ]:
            got = await status(team.createCommentHandler, request(user, body=FONT))
            assert got == expected, user
        # #1 is Ria's. Another reviewer answers, but cannot close or sort it.
        reply = request("rex", body={"text": "No"}, number=1)
        assert await status(team.replyCommentHandler, reply) == 200
        for body in ({"state": "resolved"}, {"title": "Mine"}):
            forbidden = request("rex", body=body, number=1)
            assert await status(team.updateCommentHandler, forbidden) == 403
        for body in ({"assignee": "dan"}, {"labels": ["naming"]}):
            forbidden = request("ria", body=body, number=1)
            assert await status(team.updateCommentHandler, forbidden) == 403
        delete = request("rex", number=1)
        assert await status(team.deleteCommentHandler, delete) == 403
        # Its author titles and resolves it; designers sort it.
        for user, body in [
            ("ria", {"title": "Name"}),
            ("ria", {"state": "resolved"}),
            ("dan", {"state": "open"}),
            ("dan", {"assignee": "ria", "labels": ["naming"]}),
        ]:
            allowed = request(user, body=body, number=1)
            assert await status(team.updateCommentHandler, allowed) == 200
        # Only managers delete a topic others have written in.
        assert await status(team.deleteCommentHandler, request("dan", number=1)) == 403
        assert await status(team.deleteCommentHandler, request("mona", number=1)) == 200
        restored = request("mona", number=1)
        assert await status(team.restoreCommentHandler, restored) == 200

    run(go())


def test_font_topics_through_the_routes(team):
    async def go():
        opened = await answer(
            await team.createCommentHandler(
                request(
                    "ria",
                    body={**FONT, "title": "Family name", "anchor": "fontinfo"},
                )
            )
        )
        topic = opened["issue"]
        assert topic["number"] == 1 and topic["glyph"] is None
        assert topic["title"] == "Family name" and topic["anchor"] == "fontinfo"
        # The head of the default branch, even without a glyph.
        assert topic["branch"] == "main" and len(topic["commit"]) == 40
        await team.createCommentHandler(request("ria", body=NEW))
        await team.createCommentHandler(request("dan", body=FONT))
        await team.updateCommentHandler(
            request("dan", body={"state": "resolved"}, number=3)
        )

        async def numbers(**query):
            data = await answer(await team.commentsHandler(request("ria", **query)))
            return [issue["number"] for issue in data["issues"]]

        assert await numbers() == [1, 2, 3]
        assert await numbers(glyph="H") == [2]
        assert await numbers(scope="font") == [1, 3]
        assert await numbers(scope="font", glyph="H") == []
        with pytest.raises(web.HTTPBadRequest):
            await team.commentsHandler(request("ria", scope="everything"))

        summary = await answer(await team.commentsSummaryHandler(request("ria")))
        assert summary == {
            "open": 2,
            "resolved": 1,
            "font": {"open": 1, "resolved": 1},
        }

        refused = [
            ({**FONT, "point": {"x": 1, "y": 2}}, team.createCommentHandler, {}),
            ({**FONT, "source": SOURCE}, team.createCommentHandler, {}),
            ({**FONT, "sketch": STROKES}, team.createCommentHandler, {}),
            ({"title": "No message"}, team.createCommentHandler, {}),
            ({**NEW, "point": None}, team.createCommentHandler, {}),
            ({"text": "Look", "sketch": STROKES}, team.replyCommentHandler, {"number": 1}),
            ({"point": {"x": 1, "y": 2}}, team.updateCommentHandler, {"number": 1}),
        ]
        for body, handler, match in refused:
            with pytest.raises(web.HTTPBadRequest):
                await handler(request("owner", body=body, **match))

    run(go())


def test_font_topics_do_not_disturb_the_font_history(team, root):
    store = GitRepoStore.open(root / "Mutator.git")
    head = store.head("main")
    store.close()
    run(team.createCommentHandler(request("ria", body=FONT)))
    store = GitRepoStore.open(root / "Mutator.git")
    assert store.head("main") == head
    store.close()
