"""A branch that follows the remote repository (``upstream/…``) is written
by the pull only (re-check of the R2c audit, outside its surface): the
editor opens it read only, and restore, restore-font, snapshot, a merge into
it, deleting it and hive-write answer 403 with "This branch follows the
remote repository: pull to update it." Reading it, and merging it into
another branch, still work."""

import asyncio

import pytest
from aiohttp import web

pytest.importorskip("fontra")
import yaml  # noqa: E402
from fontra.workflow.actions import ActionError  # noqa: E402
from fontra.workflow.workflow import Workflow  # noqa: E402

from conftest import run  # noqa: E402
from fontra_hive.access import token_for  # noqa: E402
from fontra_hive.fonthandler import ReadOnlyError  # noqa: E402
from fontra_hive.gitstore import FOLLOWS_REMOTE, follows_remote  # noqa: E402
from fontra_hive.recipe import (  # noqa: E402
    HiveRecipeContext,
    hiveRecipeContext,
    projectResolverFor,
    requireRecipeAccess,
)
from r2c_bench import connection, make_manager, request, store_of  # noqa: E402

UP = "upstream/main"


def manager_with_upstream(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        head = store.head()
        store.create_branch(UP, head)
        store.create_branch("fabio", head)
        return manager, head
    finally:
        store.close()


def head_of(manager, branch):
    store = store_of(manager)
    try:
        return store.head(branch)
    finally:
        store.close()


def test_follows_remote():
    assert follows_remote("upstream/main")
    assert not follows_remote("main")
    assert not follows_remote("fabio/upstream")
    assert not follows_remote(None)


@pytest.mark.parametrize(
    "handler, user, query",
    [
        ("restoreHandler", "mei", {"glyph": "A", "ref": "main", "branch": UP}),
        ("restoreFontHandler", "mei", {"ref": "main", "branch": UP}),
        ("restoreFontHandler", "mei", {"ref": "main", "branch": UP, "parts": "fontInfo"}),
        ("snapshotHandler", "mei", {"name": "Round 1", "branch": UP}),
        ("mergeHandler", "mei", {"from": "fabio", "into": UP}),
        ("deleteBranchHandler", "mei", {"branch": UP}),
        ("restoreHandler", "fabio", {"glyph": "A", "ref": "main", "branch": UP}),
        ("mergeHandler", "fabio", {"from": "fabio", "into": UP}),
    ],
)
def test_no_route_writes_a_branch_that_follows_the_remote(
    tmp_path, fixture_fontra, handler, user, query
):
    manager, head = manager_with_upstream(tmp_path, fixture_fontra)

    async def go():
        with pytest.raises(web.HTTPForbidden) as refused:
            await getattr(manager, handler)(
                request("Mutator", user, method="POST", body={}, **query)
            )
        return refused.value.text

    assert run(go()) == FOLLOWS_REMOTE
    assert head_of(manager, UP) == head


def test_the_editor_opens_it_read_only(tmp_path, fixture_fontra):
    manager, head = manager_with_upstream(tmp_path, fixture_fontra)

    async def go():
        handler = await manager.getRemoteSubject(f"Mutator@{UP}", "hive-user:mei", False)
        assert await handler.isReadOnly(connection=connection("mei")) is True
        change = {"p": ["glyphs", "A", "layers"], "f": "=", "a": ["x", None]}
        with pytest.raises(ReadOnlyError, match="follows the remote repository"):
            await handler.editFinal(change, change, "x", False, connection=connection("mei"))
        # The same people may edit another branch.
        other = await manager.getRemoteSubject("Mutator@fabio", "hive-user:mei", False)
        assert await other.isReadOnly(connection=connection("mei")) is False
        await manager.aclose()

    run(go())
    assert head_of(manager, UP) == head


def test_hive_write_refuses_it(tmp_path, fixture_fontra):
    manager, head = manager_with_upstream(tmp_path, fixture_fontra)
    recipe = f"""
steps:
- input: hive-read
  project: Mutator
- output: hive-write
  project: Mutator
  branch: {UP}
  message: Copy
"""

    async def go():
        ctx = HiveRecipeContext(
            resolver=projectResolverFor(manager, token_for("mei")), recipe="copy"
        )
        config = yaml.safe_load(recipe)
        await requireRecipeAccess(config, ctx)
        work = tmp_path / "work"
        work.mkdir()
        with hiveRecipeContext(ctx):
            async with Workflow(config=config, parentDir=work).endPoints() as ends:
                for output in ends.outputs:
                    await output.process(work)

    with pytest.raises(ActionError, match="follows the remote repository"):
        asyncio.run(go())
    assert head_of(manager, UP) == head


def test_it_is_still_read_and_merged_elsewhere(tmp_path, fixture_fontra):
    manager, head = manager_with_upstream(tmp_path, fixture_fontra)

    async def go():
        r = await manager.logHandler(request("Mutator", "mei", branch=UP))
        assert r.status == 200
        q = {"from": UP, "into": "fabio"}
        preview = await manager.mergePreviewHandler(request("Mutator", "mei", **q))
        assert preview.status == 200
        await manager.aclose()

    run(go())
