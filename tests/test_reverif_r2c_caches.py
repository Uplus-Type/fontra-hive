"""The branches' "ahead, behind" cache is read from the event loop and from
the merge preview's thread (re-check of the R2c audit): a SizedCache,
bounded, behind its lock."""

import threading

import pytest

pytest.importorskip("fontra")

from fontra_hive.projectmanager import SizedCache  # noqa: E402
from r2c_bench import make_manager  # noqa: E402


class FakeStore:
    def __init__(self):
        self.calls = 0

    def ahead_behind(self, head, base):
        self.calls += 1
        return (len(head), len(base))


def test_ahead_behind_is_a_sized_cache(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    assert isinstance(manager._aheadBehindCache, SizedCache)
    store = FakeStore()
    assert manager._aheadBehind(store, "aa", "b") == (2, 1)
    assert manager._aheadBehind(store, "aa", "b") == (2, 1)
    assert store.calls == 1


def test_ahead_behind_from_several_threads(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    cache = manager._aheadBehindCache
    cache.maxEntries = 50
    errors = []

    def hammer(seed):
        store = FakeStore()
        try:
            for i in range(3000):
                key = f"{seed}-{i % 97}"
                assert manager._aheadBehind(store, key, "b") == (len(key), 1)
        except Exception as error:  # pragma: no cover - the failure itself
            errors.append(error)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(cache) <= 50


def test_a_manager_built_without_init_gets_its_cache():
    from fontra_hive.projectmanager import DevHiveProjectManager

    manager = DevHiveProjectManager.__new__(DevHiveProjectManager)
    assert manager._aheadBehind(FakeStore(), "aa", "b") == (2, 1)
    assert isinstance(manager._aheadBehindCache, SizedCache)
