"""R2C-6 (audit of 23 Nov 2026): font-diff, restore-font and hive-read
answer 404 (ActionError) for an id that is not a commit; the first commit
of from=first is cached."""


import pytest

pytest.importorskip("fontra")
from aiohttp import web  # noqa: E402

from conftest import run  # noqa: E402
from r2c_bench import ME, body, make_manager, request, store_of  # noqa: E402


# --- R2C-6: an id that is not a commit ------------------------------------------


def test_an_id_that_is_not_a_commit_is_not_found(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        head = store.head()
        tree = store.tree_sha(head)
        blob = store.file_sha(head, "font-data.json")
    finally:
        store.close()

    async def go():
        for sha in (tree, blob, "0" * 40):
            for query in ({"to": sha}, {"from": sha}):
                with pytest.raises(web.HTTPNotFound):
                    await manager.fontDiffHandler(request("Mutator", "olga", **query))
            with pytest.raises(web.HTTPNotFound):
                await manager.restoreFontHandler(
                    request("Mutator", "mei", method="POST", ref=sha, parts="kerning")
                )
        # from=first: the first commit, cached by commit.
        answer = await body(
            await manager.fontDiffHandler(request("Mutator", "olga", **{"from": "first"}))
        )
        assert answer["from"] == answer["to"] == head
        assert any(key[0] == "first" for key in manager._sourcesCache()._data)

    run(go())


def test_hive_read_refuses_an_id_that_is_not_a_commit(tmp_path):
    from fontra.workflow.actions import ActionError

    from fontra_hive.gitstore import GitRepoStore
    from fontra_hive.recipe import _resolveCommit

    store = GitRepoStore.create(tmp_path / "f.git")
    try:
        head = store.commit({"a.txt": b"1"}, message="Import", author=ME)
        assert store.resolve_commit(head) == head
        assert store.first_commit(head) == head
        with pytest.raises(ActionError):
            _resolveCommit(store, store.tree_sha(head), "Mutator")
        with pytest.raises(KeyError):
            store.resolve_commit(store.file_sha(head, "a.txt"))
    finally:
        store.close()
