"""Branches: names, comparison with the default branch, information kept in
git, and the routes that list, create and delete them."""

import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from conftest import run
from fontra_hive.access import DevDirectory
from fontra_hive.gitstore import GitRepoStore, Signature, branch_name_error
from fontra_hive.projectmanager import (
    DEV_USER_COOKIE,
    DevHiveProjectManager,
    glyphPath,
)

ME = Signature("Jérémie", "j@example.com")


def request(project="Mutator", user=None, **query):
    return SimpleNamespace(
        match_info={"name": project},
        query=query,
        cookies={DEV_USER_COOKIE: user} if user else {},
        headers={},
        host="localhost:8000",
        method="GET",
    )


async def body(response):
    return json.loads(response.body)


@pytest.fixture
def root(tmp_path, fixture_fontra):
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    store.commit({glyphPath("A"): b"{}\n"}, message="Edit A", author=ME)
    store.close()
    return root


@pytest.fixture
def manager(root):
    return DevHiveProjectManager(root, commitDelay=0.05)


# --- the store ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["bold", "Bold-Extension_2", "jeremie/italic", "v1.2", "a.b/c-d"],
)
def test_acceptable_branch_names(name):
    assert branch_name_error(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        "with space",
        "é",
        "a@b",
        "a..b",
        ".hidden",
        "a/.b",
        "a/",
        "/a",
        "a//b",
        "a.",
        "a.lock",
        "-a",
        "HEAD",
        "0123456789abcdef0123456789abcdef01234567",
        "snapshot/x",
        "glyph-snapshot/x",
        "archive/x",
        "x" * 101,
    ],
)
def test_refused_branch_names(name):
    assert branch_name_error(name)


def test_a_and_a_slash_b_cannot_coexist(root):
    store = GitRepoStore.open(root / "Mutator.git")
    store.create_branch("team")
    assert "clashes" in store.branch_name_error("team/ana")
    store.create_branch("x/y")
    assert "clashes" in store.branch_name_error("x")
    assert "already" in store.branch_name_error("team")
    assert store.branch_name_error("teams") is None
    store.close()


def test_ahead_behind(root):
    store = GitRepoStore.open(root / "Mutator.git")
    store.create_branch("bold")
    assert store.ahead_behind("bold", "main") == (0, 0)
    for i in range(3):
        store.commit({"b.txt": b"%d" % i}, branch="bold", message="b", author=ME)
    store.commit({"m.txt": b"m"}, message="m", author=ME)
    assert store.ahead_behind("bold", "main") == (3, 1)
    assert store.ahead_behind("main", "bold") == (1, 3)
    store.close()


def test_branch_information_stays_out_of_the_font(root):
    store = GitRepoStore.open(root / "Mutator.git")
    head = store.head()
    store.set_branch_info("bold", {"createdBy": "ana"})
    store.set_branch_info("light", {"createdBy": "zoe"})
    store.set_branch_info("light", None)
    store.set_branch_info("never-there", None)
    assert store.branch_info() == {"bold": {"createdBy": "ana"}}
    # Neither a branch nor a tag, and the font's history did not move.
    assert store.branches() == ["main"] and store.tags() == []
    assert store.head() == head
    store.close()


# --- routes, without accounts ------------------------------------------------------


def test_list_compares_with_the_default_branch(manager, root):
    store = GitRepoStore.open(root / "Mutator.git")
    store.create_branch("bold")
    store.commit({glyphPath("B"): b"{}\n"}, branch="bold", message="bold B", author=ME)
    store.create_branch("merged-one")
    store.commit({glyphPath("A"): b"{ }\n"}, message="Edit A again", author=ME)
    store.close()

    async def go():
        data = await body(await manager.branchesHandler(request()))
        assert data["default"] == "main"
        assert data["can"] == {"create": True, "delete": True, "merge": True}
        byName = {b["name"]: b for b in data["branches"]}
        assert data["branches"][0]["name"] == "main"
        assert byName["main"]["isDefault"] and not byName["main"]["merged"]
        assert (byName["bold"]["ahead"], byName["bold"]["behind"]) == (1, 1)
        assert byName["bold"]["message"] == "bold B" and not byName["bold"]["merged"]
        assert byName["merged-one"]["merged"] and byName["merged-one"]["behind"] == 1

    run(go())


def test_create_a_branch(manager, root):
    async def go():
        created = await body(
            await manager.createBranchHandler(request(name="bold-extension"))
        )
        branch = created["branch"]
        assert branch["name"] == "bold-extension" and branch["from"] == "main"
        assert (branch["ahead"], branch["behind"]) == (0, 0)
        store = GitRepoStore.open(root / "Mutator.git")
        assert store.head("bold-extension") == store.head("main")
        assert store.branch_info()["bold-extension"]["base"] == store.head()
        store.close()
        # It opens in the editor as a project of its own.
        assert await manager.projectAvailable("Mutator@bold-extension", "dev")

        with pytest.raises(web.HTTPConflict):
            await manager.createBranchHandler(request(name="bold-extension"))
        with pytest.raises(web.HTTPBadRequest):
            await manager.createBranchHandler(request(name="no spaces"))
        with pytest.raises(web.HTTPBadRequest):
            await manager.createBranchHandler(request(name=""))
        with pytest.raises(web.HTTPNotFound):
            await manager.createBranchHandler(request(name="x", **{"from": "nope"}))

    run(go())


def test_create_a_branch_from_a_snapshot(manager, root):
    store = GitRepoStore.open(root / "Mutator.git")
    snapshot = store.create_snapshot("Client review", author=ME)
    store.commit({glyphPath("A"): b"{ }\n"}, message="After", author=ME)
    store.close()

    async def go():
        created = await body(
            await manager.createBranchHandler(
                request(name="from-review", **{"from": "snapshot/client-review"})
            )
        )
        assert created["branch"]["head"] == snapshot.sha
        assert created["branch"]["behind"] == 1

    run(go())


def test_create_from_an_open_branch_includes_pending_edits(manager):
    async def go():
        fontHandler = await manager.getRemoteSubject("Mutator", "dev")
        backend = fontHandler.backend
        glyph = await backend.getGlyph("B")
        await backend.putGlyph("B", glyph, [66])
        assert backend.tree.pending  # not committed yet

        created = await body(
            await manager.createBranchHandler(request(name="now", **{"from": "main"}))
        )
        assert created["branch"]["head"] == backend.tree.commit_sha
        await manager.aclose()

    run(go())


def test_delete_a_branch(manager, root):
    store = GitRepoStore.open(root / "Mutator.git")
    store.create_branch("merged")
    store.create_branch("wip")
    store.commit({glyphPath("B"): b"{}\n"}, branch="wip", message="wip", author=ME)
    wip = store.head("wip")
    store.close()

    async def go():
        with pytest.raises(web.HTTPConflict):
            await manager.deleteBranchHandler(request(branch="main"))
        with pytest.raises(web.HTTPNotFound):
            await manager.deleteBranchHandler(request(branch="ghost"))

        deleted = await body(
            await manager.deleteBranchHandler(request(branch="merged"))
        )
        assert deleted["merged"] and deleted["archived"] is None

        # Not merged: kept as a tag, nothing is lost.
        deleted = await body(await manager.deleteBranchHandler(request(branch="wip")))
        assert not deleted["merged"] and deleted["archived"] == "archive/wip"
        store = GitRepoStore.open(root / "Mutator.git")
        assert store.branches() == ["main"]
        assert store.resolve("archive/wip") == wip
        store.create_branch("wip", wip)  # made again, deleted again
        store.close()
        deleted = await body(await manager.deleteBranchHandler(request(branch="wip")))
        assert deleted["archived"] == "archive/wip-2"

        # Open in the editor: not now.
        await manager.createBranchHandler(request(name="open"))
        await manager.getRemoteSubject("Mutator@open", "dev")
        listed = await body(await manager.branchesHandler(request()))
        assert [b["open"] for b in listed["branches"] if b["name"] == "open"] == [True]
        with pytest.raises(web.HTTPConflict):
            await manager.deleteBranchHandler(request(branch="open"))
        await manager.aclose()

    run(go())


# --- routes, with accounts ------------------------------------------------------------

USERS = {
    "users": {
        "owner": {"name": "Owner", "email": "o@example.com"},
        "mona": {"name": "Mona Manager", "email": "m@example.com"},
        "dan": {"name": "Dan Designer", "email": "d@example.com"},
        "dora": {"name": "Dora Designer", "email": "dd@example.com"},
        "ria": {"name": "Ria Reviewer", "email": "r@example.com"},
    },
    "projects": {
        "Mutator": {
            "owner": "owner",
            "collaborators": {
                "mona": "manager",
                "dan": "designer",
                "dora": "designer",
                "ria": "reviewer",
            },
        }
    },
}


@pytest.fixture
def team(root, tmp_path):
    path = tmp_path / "hive-dev-users.json"
    path.write_text(json.dumps(USERS), encoding="utf-8")
    return DevHiveProjectManager(root, directory=DevDirectory(path), commitDelay=0.05)


def test_who_may_create_and_delete(team, root):
    async def go():
        seen = await body(await team.branchesHandler(request(user="ria")))
        assert seen["can"] == {"create": False, "delete": False, "merge": False}
        assert seen["you"] == "ria"
        with pytest.raises(web.HTTPForbidden):
            await team.createBranchHandler(request(user="ria", name="nope"))

        seen = await body(await team.branchesHandler(request(user="dan")))
        assert seen["can"] == {"create": True, "delete": False, "merge": False}
        created = await body(
            await team.createBranchHandler(request(user="dan", name="dan-test"))
        )
        assert created["branch"]["createdBy"] == "dan"
        assert created["branch"]["createdByName"] == "Dan Designer"
        await team.createBranchHandler(request(user="dan", name="dan-2"))
        await team.createBranchHandler(request(user="dan", name="dan-3"))

        # Another designer cannot delete it; its maker and managers can.
        with pytest.raises(web.HTTPForbidden):
            await team.deleteBranchHandler(request(user="dora", branch="dan-test"))
        await team.deleteBranchHandler(request(user="dan", branch="dan-test"))
        await team.deleteBranchHandler(request(user="mona", branch="dan-2"))
        await team.deleteBranchHandler(request(user="owner", branch="dan-3"))

        store = GitRepoStore.open(root / "Mutator.git")
        assert store.branches() == ["main"]
        # The branch information was written by the people who acted.
        log = store.repo.get_walker(include=[store.repo.refs[b"refs/hive/meta"]])
        authors = [entry.commit.author.decode() for entry in log]
        assert authors[0].startswith("Owner <owner@hive>")
        assert authors[-1].startswith("Dan Designer <dan@hive>")
        store.close()

    run(go())


# --- with hive-api: the project's default branch comes from there ------------------


class FakeHiveApi:
    def __init__(self):
        from fontra_hive.access import CAPABILITIES, Access, HiveUser
        from fontra_hive.hiveapi import ProjectAccess

        user = HiveUser("dan", "Dan Designer", "d@example.com", uid="u-dan")
        self.projectAccess = ProjectAccess(
            access=Access(user, "designer", CAPABILITIES["designer"]),
            repo="p-1.git",
            defaultBranch="trunk",
            projectId="uplustype/Mutator",
        )
        self.user = user

    async def userFromAccessToken(self, token):
        return self.user if token == "cookie" else None

    async def access(self, token, project):
        return self.projectAccess if project == "uplustype/Mutator" else None

    def cachedAccess(self, token, project):
        return self.projectAccess.access

    async def aclose(self):
        pass


def test_hive_projects_have_their_own_default_branch(tmp_path, fixture_fontra):
    from fontra_hive.hiveapi import ACCESS_COOKIE
    from fontra_hive.hivemanager import HiveProjectManager

    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "p-1.git")
    store.import_directory(fixture_fontra, branch="trunk", message="Import", author=ME)
    store.close()
    manager = HiveProjectManager(root, api=FakeHiveApi(), proxy=False)

    def hiveRequest(**query):
        r = request("uplustype/Mutator", **query)
        r.cookies = {ACCESS_COOKIE: "cookie"}
        return r

    async def go():
        data = await body(await manager.branchesHandler(hiveRequest()))
        assert data["default"] == "trunk" and data["you"] == "dan"
        assert [b["name"] for b in data["branches"]] == ["trunk"]
        created = await body(await manager.createBranchHandler(hiveRequest(name="dan")))
        assert created["branch"]["from"] == "trunk"
        assert created["branch"]["createdBy"] == "dan"
        with pytest.raises(web.HTTPConflict):
            await manager.deleteBranchHandler(hiveRequest(branch="trunk"))
        await manager.deleteBranchHandler(hiveRequest(branch="dan"))
        await manager.aclose()

    run(go())


def test_delete_only_touches_listed_branches(manager, root):
    async def go():
        for name in ["../../HEAD", "../tags/x", "main/../main"]:
            with pytest.raises(web.HTTPNotFound):
                await manager.deleteBranchHandler(request(branch=name))
        assert (root / "Mutator.git" / "HEAD").exists()

    run(go())


def test_deleted_branches_are_listed_and_restored(team, root):
    async def go():
        await team.createBranchHandler(request(user="dan", name="dan-test"))
        store = GitRepoStore.open(root / "Mutator.git")
        store.commit(
            {glyphPath("B"): b"{}\n"}, branch="dan-test", message="B", author=ME
        )
        head = store.head("dan-test")
        store.close()
        deleted = await body(
            await team.deleteBranchHandler(request(user="mona", branch="dan-test"))
        )
        assert deleted["archived"] == "archive/dan-test"

        listed = await body(await team.branchesHandler(request(user="dan")))
        assert [b["name"] for b in listed["branches"]] == ["main"]
        (archived,) = listed["archived"]
        assert archived["tag"] == "archive/dan-test"
        assert archived["name"] == "dan-test" and archived["head"] == head
        assert archived["deletedBy"] == "Mona Manager"
        assert archived["deletedByUsername"] == "mona"
        assert archived["createdBy"] == "dan" and archived["ahead"] == 1

        # Another designer cannot; its maker can, under another name if taken.
        with pytest.raises(web.HTTPForbidden):
            await team.restoreBranchHandler(
                request(user="dora", tag="archive/dan-test")
            )
        with pytest.raises(web.HTTPNotFound):
            await team.restoreBranchHandler(request(user="dan", tag="archive/nope"))
        await team.createBranchHandler(request(user="dan", name="dan-test"))
        with pytest.raises(web.HTTPConflict):
            await team.restoreBranchHandler(request(user="dan", tag="archive/dan-test"))
        restored = await body(
            await team.restoreBranchHandler(
                request(user="dan", tag="archive/dan-test", name="dan-test-back")
            )
        )
        branch = restored["branch"]
        assert branch["name"] == "dan-test-back" and branch["head"] == head
        assert branch["createdBy"] == "dan" and branch["ahead"] == 1

        listed = await body(await team.branchesHandler(request(user="dan")))
        assert listed["archived"] == []
        store = GitRepoStore.open(root / "Mutator.git")
        assert store.tags() == []
        store.close()

    run(go())


def test_whoever_deleted_a_branch_may_restore_it(team, root):
    async def go():
        await team.createBranchHandler(request(user="dan", name="x"))
        store = GitRepoStore.open(root / "Mutator.git")
        store.commit({"x.txt": b"x"}, branch="x", message="x", author=ME)
        store.close()
        # dora cannot delete dan's branch; a manager deletes it, and dora
        # (neither maker nor deleter) cannot restore it; mona can.
        await team.deleteBranchHandler(request(user="mona", branch="x"))
        with pytest.raises(web.HTTPForbidden):
            await team.restoreBranchHandler(request(user="dora", tag="archive/x"))
        await team.restoreBranchHandler(request(user="mona", tag="archive/x"))
        # Deleted again, twice: two archives, the newest first.
        await team.deleteBranchHandler(request(user="dan", branch="x"))
        await team.createBranchHandler(request(user="dan", name="x"))
        store = GitRepoStore.open(root / "Mutator.git")
        store.commit({"y.txt": b"y"}, branch="x", message="y", author=ME)
        store.close()
        await team.deleteBranchHandler(request(user="dan", branch="x"))
        listed = await body(await team.branchesHandler(request(user="dan")))
        assert sorted(a["tag"] for a in listed["archived"]) == [
            "archive/x",
            "archive/x-2",
        ]
        assert all(a["deletedByUsername"] == "dan" for a in listed["archived"])

    run(go())
