"""VF-3 (final check of R2c, 23 Nov 2026): on a disk that ignores case
(a Mac), ``Upstream/main`` was the file of ``upstream/main``, and the
editor and hive-write wrote the branch that follows the remote repository.
Now ``follows_remote`` ignores case, and a branch is read, opened or
written only by its exact name (``GitRepoStore.head`` answers only for a
name ``branches()`` lists). A new branch whose name differs from an
existing one only by case is refused.

On Linux the case variants do not exist: the same answers hold."""

import asyncio

import pytest
from aiohttp import web

from fontra_hive.gitstore import follows_remote

pytest.importorskip("fontra")
from fontra.workflow.actions import ActionError  # noqa: E402

from conftest import run  # noqa: E402
from r2c_bench import make_manager, request, store_of  # noqa: E402
from test_reverif_r2c_domains import copy_recipe, run_recipe  # noqa: E402

UP = "upstream/main"
VARIANTS = ["Upstream/main", "UPSTREAM/main", "upstream/MAIN"]


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


def heads(manager):
    store = store_of(manager)
    try:
        return {b: store.head(b) for b in store.branches()}
    finally:
        store.close()


def test_follows_remote_whatever_the_case():
    for name in [UP, *VARIANTS]:
        assert follows_remote(name)
    assert not follows_remote("main")
    assert not follows_remote("fabio/upstream")


def test_a_branch_is_read_by_its_exact_name_only(tmp_path, fixture_fontra):
    manager, head = manager_with_upstream(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        assert store.head(UP) == head and store.head("fabio") == head
        for name in [*VARIANTS, "Fabio", "FABIO"]:
            assert store.head(name) is None
        assert store.branch_name_error("Upstream/main") == (
            "There is already a branch 'upstream/main'."
        )
        assert store.branch_name_error("Fabio") == "There is already a branch 'fabio'."
        assert "clashes" in store.branch_name_error("Fabio/x")
        assert store.branch_name_error("fabio2") is None
    finally:
        store.close()


@pytest.mark.parametrize("name", [*VARIANTS, "Fabio"])
def test_the_editor_does_not_open_a_case_variant(tmp_path, fixture_fontra, name):
    manager, _ = manager_with_upstream(tmp_path, fixture_fontra)
    before = heads(manager)

    async def go():
        handler = await manager.getRemoteSubject(f"Mutator@{name}", "hive-user:mei", False)
        await manager.aclose()
        return handler

    assert run(go()) is None
    assert heads(manager) == before


@pytest.mark.parametrize("name", ["Upstream/main", "UPSTREAM/main"])
def test_hive_write_refuses_a_case_variant_of_upstream(tmp_path, fixture_fontra, name):
    manager, head = manager_with_upstream(tmp_path, fixture_fontra)

    async def go():
        await run_recipe(manager, tmp_path, "mei", copy_recipe("Mutator", name), "copy")

    with pytest.raises(ActionError, match="follows the remote repository"):
        asyncio.run(go())
    assert heads(manager)[UP] == head
    assert sorted(heads(manager)) == ["fabio", "main", UP]


def test_hive_write_refuses_a_case_variant_of_a_branch(tmp_path, fixture_fontra):
    manager, head = manager_with_upstream(tmp_path, fixture_fontra)

    async def go():
        await run_recipe(manager, tmp_path, "mei", copy_recipe("Mutator", "Fabio"), "copy")

    with pytest.raises(ActionError, match="There is already a branch 'fabio'"):
        asyncio.run(go())
    assert heads(manager)["fabio"] == head
    assert sorted(heads(manager)) == ["fabio", "main", UP]


@pytest.mark.parametrize(
    "handler, query",
    [
        ("restoreHandler", {"glyph": "A", "ref": "main", "branch": "Fabio"}),
        ("restoreFontHandler", {"ref": "main", "branch": "Fabio", "parts": "fontInfo"}),
        ("snapshotHandler", {"name": "Round 1", "branch": "Fabio"}),
        ("mergeHandler", {"from": "main", "into": "Fabio"}),
        ("deleteBranchHandler", {"branch": "Fabio"}),
    ],
)
def test_no_route_writes_a_case_variant(tmp_path, fixture_fontra, handler, query):
    manager, _ = manager_with_upstream(tmp_path, fixture_fontra)
    before = heads(manager)

    async def go():
        with pytest.raises((web.HTTPNotFound, web.HTTPConflict, web.HTTPForbidden)) as error:
            await getattr(manager, handler)(
                request("Mutator", "mei", method="POST", body={}, **query)
            )
        await manager.aclose()
        return error.value

    error = run(go())
    assert isinstance(error, web.HTTPNotFound), (type(error), error.text)
    assert heads(manager) == before
