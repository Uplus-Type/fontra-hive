"""R2C-2 (audit of 23 Nov 2026): describing a change of features.txt never
stops the server. The summary and the title count lines (multisets, linear);
the detail is a unified diff only within fontdiff.MAX_DIFF_CELLS; every
description read for the history or font-diff is computed off the event
loop; the history's cache is bounded in bytes."""

import asyncio
import os
import time

import pytest

from fontra_hive import fontdiff

N = int(os.environ.get("R2C_FEATURE_LINES", "20000"))


def features(n, changed):
    lines = [f"  sub g{i} by g{i}.alt;" for i in range(n)]
    if changed:
        lines = [line if i % 2 else line + " # renamed" for i, line in enumerate(lines)]
    return ("feature liga {\n" + "\n".join(lines) + "\n} liga;\n").encode()


def test_the_summary_counts_lines_in_linear_time():
    old, new = features(N, False), features(N, True)
    t = time.perf_counter()
    [change] = fontdiff.features_changes(old, new)
    assert time.perf_counter() - t < 1.0
    half = (N + 1) // 2
    assert change.summary == f"Features: {half} lines added, {half} removed"
    assert change.lines is None and change.note is None


def test_the_detail_says_when_there_are_too_many_lines():
    [change] = fontdiff.features_changes(
        features(N, False), features(N, True), detail=True
    )
    assert change.lines is None
    assert change.note == "Too many lines to show the difference."
    assert change.to_json()["note"] == "Too many lines to show the difference."
    # A small change keeps its lines.
    [small] = fontdiff.features_changes(
        features(50, False), features(50, True), detail=True
    )
    assert small.note is None and ("+", "  sub g0 by g0.alt; # renamed") in small.lines


def test_lines_only_moved():
    [change] = fontdiff.features_changes(b"a\nb\n", b"b\na\n")
    assert change.summary == "Features: lines moved"


def test_the_cache_is_bounded_in_bytes():
    pytest.importorskip("fontra")
    from fontra_hive.projectmanager import SizedCache, approx_size

    cache = SizedCache(maxBytes=100_000, maxEntries=1000)
    big = ["x" * 1000] * 30  # about 31 kB
    for i in range(10):
        cache[("k", i)] = big
    assert cache.size <= 100_000
    assert len(cache) == 3
    assert cache.get(("k", 0)) is None and cache.get(("k", 9)) == big
    cache[("huge",)] = ["x" * 200_000]  # larger than the whole cache: not kept
    assert cache.get(("huge",)) is None
    assert approx_size(big) > 30_000


# --- the audit's proofs, their assertions turned round --------------------------

pytest.importorskip("fontra")
from conftest import run  # noqa: E402
from r2c_bench import ME, body, make_manager, request, store_of  # noqa: E402

MAX_STALL = 0.2


async def longest_stall(coro):
    """Run ``coro`` beside a ticker; the longest the ticker waited."""
    stall = 0.0
    done = False

    async def ticker():
        nonlocal stall
        last = time.perf_counter()
        while not done:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            stall = max(stall, now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    try:
        result = await coro
    finally:
        done = True
        await task
    return result, stall


def test_an_observer_does_not_freeze_the_server(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        first = store.commit(
            {"features.txt": features(N, False)}, message="Features", author=ME
        )
        second = store.commit(
            {"features.txt": features(N, True)}, message="Rename", author=ME
        )
    finally:
        store.close()

    async def go():
        # Olga is an observer ("read" only).
        response, stall = await longest_stall(
            manager.fontDiffHandler(
                request("Mutator", "olga", **{"from": first, "to": second})
            )
        )
        assert response.status == 200, response.text
        assert stall < MAX_STALL, stall
        [change] = (await body(response))["changes"]
        assert change["note"] == "Too many lines to show the difference."
        assert "lines" not in change
        # The history of the font, from an empty cache.
        manager.__dict__.pop("_changedSourcesCache", None)
        response, stall = await longest_stall(
            manager.logHandler(request("Mutator", "olga"))
        )
        assert stall < MAX_STALL, stall
        log = await body(response)
        assert log["commits"][0]["font"][0]["summary"].startswith("Features: ")
        response, stall = await longest_stall(
            manager.logHandler(request("Mutator", "olga", kind="font"))
        )
        assert stall < MAX_STALL, stall
        assert [c["sha"] for c in (await body(response))["commits"]][:2] == [
            second,
            first,
        ]

    run(go())


def test_saving_the_features_does_not_freeze_the_server(tmp_path, fixture_fontra):
    """The commit message is said in words at commit time, by counting."""
    from fontra_hive.backend_git import GitFontraBackend

    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        store.commit({"features.txt": features(N, False)}, message="Features", author=ME)
    finally:
        store.close()

    async def go():
        store = store_of(manager)
        backend = GitFontraBackend(store, "main", author=ME, commit_delay=0.05)
        feats = await backend.getFeatures()
        feats.text = features(N, True).decode()
        await backend.putFeatures(feats)

        async def wait_commit():
            await asyncio.sleep(1.0)

        _, stall = await longest_stall(wait_commit())
        message = store.commit_info(store.head()).message
        await backend.aclose()
        store.close()
        assert stall < MAX_STALL, stall
        half = (N + 1) // 2
        assert f"Features: {half} lines added, {half} removed" in message

    run(go())
