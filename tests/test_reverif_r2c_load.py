"""RV-2 and RV-4 (re-check of the R2c audit, 23 Nov 2026).

RV-2: a refused merge (403) no longer describes the domains on the event
loop: the domains are checked in the history's executor, on the three files
beyond the glyph, before the trees are merged.

RV-4: the history, the font diff, the merge preview and the merge's domain
check run in a small executor of their own (historywork.HistoryWork), at
most two at once per person (429 beyond, with a sentence), identical
requests in progress share one computation, and the font diff's time limit
counts from the start of the computation. One reader who floods the font
diff no longer holds up the others."""

import asyncio
import threading
import time

import pytest
from aiohttp import web

from fontra_hive.historywork import BUSY, HistoryWork, TooLong

pytest.importorskip("fontra")

from conftest import run  # noqa: E402
from fontra_hive import projectmanager  # noqa: E402
from fontra_hive.gitstore import Signature  # noqa: E402
from r2c_bench import make_manager, request, store_of  # noqa: E402

MEI = Signature("Mei", "mei@example.com")


class Ticker:
    """The longest the event loop went without running a task."""

    def __init__(self):
        self.stall = 0.0
        self.done = False

    async def run(self):
        last = time.perf_counter()
        while not self.done:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            self.stall = max(self.stall, now - last)
            last = now


# --- HistoryWork --------------------------------------------------------------


def test_two_at_once_per_person_and_429_beyond():
    work = HistoryWork(threads=2, perPerson=2)
    gate = threading.Event()

    async def go():
        a = asyncio.ensure_future(work.run("olga", None, gate.wait, 5))
        b = asyncio.ensure_future(work.run("olga", None, gate.wait, 5))
        await asyncio.sleep(0.05)
        with pytest.raises(web.HTTPTooManyRequests) as refused:
            await work.run("olga", None, lambda: 1)
        assert refused.value.text == BUSY
        # Another person is not refused (she waits for a thread).
        c = asyncio.ensure_future(work.run("ana", None, lambda: "ana"))
        await asyncio.sleep(0.05)
        gate.set()
        assert await asyncio.gather(a, b, c) == [True, True, "ana"]
        # Done: Olga may ask again.
        assert await work.run("olga", None, lambda: 2) == 2
        assert work.busy("olga") == 0

    try:
        asyncio.run(go())
    finally:
        work.shutdown()


def test_identical_requests_in_progress_share_one_computation():
    work = HistoryWork(threads=2, perPerson=2)
    calls = []

    def compute():
        calls.append(1)
        time.sleep(0.2)
        return "diff"

    async def go():
        return await asyncio.gather(
            work.run("olga", "k", compute),
            work.run("ana", "k", compute),
            work.run("mei", "k", compute),
        )

    try:
        assert asyncio.run(go()) == ["diff"] * 3
        assert len(calls) == 1
    finally:
        work.shutdown()


def test_the_time_limit_counts_from_the_start_of_the_computation():
    """A quick request that waited behind a slow one gets its answer; a
    computation that outruns the limit is said to."""
    work = HistoryWork(threads=1, perPerson=2)

    async def go():
        slow = asyncio.ensure_future(work.run("olga", None, time.sleep, 0.6))
        await asyncio.sleep(0.02)
        t = time.perf_counter()
        quick = await work.run("ana", None, lambda: "quick", timeout=0.3)
        waited = time.perf_counter() - t
        await slow
        with pytest.raises(TooLong):
            await work.run("ana", None, time.sleep, 0.5, timeout=0.1)
        return quick, waited

    try:
        quick, waited = asyncio.run(go())
        assert quick == "quick"
        assert waited > 0.3  # it waited longer than the limit, and was answered
    finally:
        work.shutdown()


def test_push_and_pull_keep_the_default_pool():
    """The history's threads all busy: asyncio.to_thread (push, pull,
    import, Try) still answers at once."""
    work = HistoryWork(threads=2, perPerson=2)
    gate = threading.Event()

    async def go():
        busy = [
            asyncio.ensure_future(work.run(p, None, gate.wait, 5))
            for p in ("olga", "olga", "ana", "ana")
        ]
        await asyncio.sleep(0.05)
        t = time.perf_counter()
        assert await asyncio.to_thread(lambda: "push") == "push"
        elapsed = time.perf_counter() - t
        gate.set()
        await asyncio.gather(*busy)
        return elapsed

    try:
        assert asyncio.run(go()) < 0.2
    finally:
        work.shutdown()


# --- the routes -----------------------------------------------------------------


def features(n, version):
    return "".join(
        f"  sub g{i} by g{i}.alt{'' if (i + version) % 2 else f' # v{version}'};\n"
        for i in range(n)
    ).encode()


def test_a_reader_who_floods_the_font_diff_holds_up_nobody(tmp_path, fixture_fontra):
    """Olga (observer) sends 60 font-diffs at once, on pairs all different,
    20,000 lines of features. She gets two answers at a time and 429 for
    the rest; meanwhile Ana's snapshots, her history of A and her own font
    diff answer in less than half a second, never 503."""
    n = 20_000
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        shas = [
            store.commit({"features.txt": features(n, v)}, message=f"v{v}", author=MEI)
            for v in range(13)
        ]
    finally:
        store.close()
    pairs = [(a, b) for i, a in enumerate(shas) for b in shas[i + 1 :]]

    async def go():
        ticker = Ticker()
        tick = asyncio.create_task(ticker.run())
        outcome = {"ok": 0, "busy": 0, "503": 0}

        async def olga(a, b):
            try:
                r = await manager.fontDiffHandler(
                    request("Mutator", "olga", **{"from": a, "to": b})
                )
                assert r.status == 200
                outcome["ok"] += 1
            except web.HTTPTooManyRequests as error:
                assert error.text == BUSY
                outcome["busy"] += 1
            except web.HTTPServiceUnavailable:
                outcome["503"] += 1

        flood = [asyncio.create_task(olga(a, b)) for a, b in pairs[1:61]]
        await asyncio.sleep(0.02)
        timings = {}
        t = time.perf_counter()
        await manager.snapshotsHandler(request("Mutator", "ana"))
        timings["snapshots"] = time.perf_counter() - t
        t = time.perf_counter()
        await manager.logHandler(request("Mutator", "ana", glyph="A"))
        timings["history"] = time.perf_counter() - t
        t = time.perf_counter()
        a, b = pairs[0]
        r = await manager.fontDiffHandler(request("Mutator", "ana", **{"from": a, "to": b}))
        assert r.status == 200
        timings["font-diff"] = time.perf_counter() - t
        await asyncio.gather(*flood)
        ticker.done = True
        await tick
        await manager.aclose()
        return outcome, timings, ticker.stall

    outcome, timings, stall = run(go())
    print(outcome, {k: round(v, 3) for k, v in timings.items()}, round(stall, 3))
    assert outcome["503"] == 0
    assert outcome["ok"] == 2 and outcome["busy"] == 58
    assert timings["snapshots"] < 0.5
    assert timings["history"] < 0.5
    assert timings["font-diff"] < 1.0
    assert stall < 0.2


def test_a_refused_merge_leaves_the_loop_free(tmp_path, fixture_fontra):
    """The re-check's RV-2 proof, inverted: Fabio (designer, no font
    settings) asks five times to merge exp, whose features changed (50,000
    lines), into his branch. Five 403s; the event loop is never held more
    than 0.05 s; the trees are never merged for a refusal."""
    n = 50_000

    def lines(step):
        return "".join(
            f"  sub g{i} by g{i}.alt;{' # m' if step and i % step == 0 else ''}\n"
            for i in range(n)
        ).encode()

    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        store.commit({"features.txt": lines(0)}, message="features", author=MEI)
        head = store.head()
        store.create_branch("exp", head)
        store.create_branch("fabio", head)
        store.commit({"features.txt": lines(2)}, branch="exp", message="exp", author=MEI)
        store.commit({"features.txt": lines(3)}, message="main", author=MEI)
    finally:
        store.close()
    merged = []
    original = manager._mergeState

    def spy(*args, **kwargs):
        merged.append(1)
        return original(*args, **kwargs)

    manager._mergeState = spy

    async def go():
        ticker = Ticker()
        tick = asyncio.create_task(ticker.run())
        await asyncio.sleep(0.02)
        refused = 0
        for _ in range(5):
            try:
                await manager.mergeHandler(
                    request(
                        "Mutator",
                        "fabio",
                        method="POST",
                        body={},
                        **{"from": "exp", "into": "fabio"},
                    )
                )
            except web.HTTPForbidden as error:
                assert error.text.startswith("Font settings are kept")
                refused += 1
        ticker.done = True
        await tick
        return refused, ticker.stall

    refused, stall = run(go())
    print(f"5 refusals, the loop held at most {stall:.3f} s")
    assert refused == 5
    assert stall < 0.05
    assert merged == []


def test_the_history_and_the_preview_go_through_the_history_executor(
    tmp_path, fixture_fontra
):
    """No route of the history uses asyncio.to_thread (the default pool)."""
    manager = make_manager(tmp_path, fixture_fontra)
    used = []
    original = manager.historyWork.run

    async def spy(person, key, function, *args, **kwargs):
        used.append(getattr(function, "__name__", "?"))
        return await original(person, key, function, *args, **kwargs)

    manager.historyWork.run = spy
    store = store_of(manager)
    store.create_branch("fabio", store.head())
    store.close()

    async def go():
        await manager.logHandler(request("Mutator", "ana"))
        await manager.fontDiffHandler(request("Mutator", "ana"))
        await manager.mergePreviewHandler(
            request("Mutator", "ana", **{"from": "fabio", "into": "main"})
        )
        await manager.aclose()

    run(go())
    assert used == ["_log", "_fontDiff", "work"]
    assert projectmanager.FONT_DIFF_SECONDS == 20
