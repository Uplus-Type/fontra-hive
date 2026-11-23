"""VF-5 (final check of R2c, 23 Nov 2026; older than R2c): the
``snapshots`` route walked the whole history on the event loop (0.95 s for
5,200 commits). It now runs in the history's executor (HistoryWork: its
limit per person, identical requests shared), and its answer is kept by
branch head and snapshot tags."""

import asyncio
import time

import pytest

pytest.importorskip("fontra")

from conftest import run  # noqa: E402
from fontra_hive.gitstore import Signature  # noqa: E402
from r2c_bench import ME, body, make_manager, request, store_of  # noqa: E402
from test_reverif_r2c_load import Ticker  # noqa: E402

MEI = Signature("Mei", "mei@example.com")
COMMITS = 5200


def build(manager):
    store = store_of(manager)
    try:
        for i in range(COMMITS):
            store.commit({"bench.txt": str(i).encode()}, message=f"c{i}", author=ME)
            if i == COMMITS // 2:
                store.create_snapshot("Round 1", author=MEI)
    finally:
        store.close()


def test_the_snapshots_of_a_long_history_leave_the_loop_free(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    build(manager)
    computed = []
    original = manager._snapshots

    def spy(*args):
        computed.append(time.perf_counter())
        return original(*args)

    manager._snapshots = spy
    used = []
    originalRun = manager.historyWork.run

    async def runSpy(person, key, function, *args, **kwargs):
        used.append(key[0] if key else None)
        return await originalRun(person, key, function, *args, **kwargs)

    manager.historyWork.run = runSpy

    async def go():
        ticker = Ticker()
        tick = asyncio.create_task(ticker.run())
        await asyncio.sleep(0.02)
        t = time.perf_counter()
        first = await body(await manager.snapshotsHandler(request("Mutator", "ana")))
        firstTime = time.perf_counter() - t
        t = time.perf_counter()
        second = await body(await manager.snapshotsHandler(request("Mutator", "olga")))
        secondTime = time.perf_counter() - t
        ticker.done = True
        await tick
        # A new commit: a new head, computed again.
        store = store_of(manager)
        try:
            store.commit({"bench.txt": b"new"}, message="new", author=ME)
        finally:
            store.close()
        third = await body(await manager.snapshotsHandler(request("Mutator", "ana")))
        # A new snapshot tag: computed again.
        store = store_of(manager)
        try:
            store.create_snapshot("Round 2", author=MEI)
        finally:
            store.close()
        fourth = await body(await manager.snapshotsHandler(request("Mutator", "ana")))
        await manager.aclose()
        return first, firstTime, second, secondTime, ticker.stall, third, fourth

    first, firstTime, second, secondTime, stall, third, fourth = run(go())
    print(
        f"first {firstTime:.3f} s, second {secondTime:.4f} s (cache), "
        f"loop held at most {stall:.3f} s"
    )
    assert [s["title"] for s in first["snapshots"]] == ["Round 1"]
    assert first == second
    assert first["pending"] == COMMITS - COMMITS // 2 - 1
    assert stall < 0.05
    assert len(computed) == 3  # the second answer came from the cache
    assert used == ["snapshots", "snapshots", "snapshots"]
    assert third["pending"] == first["pending"] + 1
    assert [s["title"] for s in fourth["snapshots"]] == ["Round 2", "Round 1"]
    assert fourth["pending"] == 0
    assert secondTime < 0.05
