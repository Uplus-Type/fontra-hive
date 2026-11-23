"""VF-1 (final check of R2c, 23 Nov 2026).

VF-1: a refused merge no longer describes up to 201 states of main. The
states a branch may hold (fontdiff.default_branch_origins) are compared by
their fingerprint (git blob id of features.txt and kerning.csv, the JSON of
font-data.json normalized and hashed), never described; one person never
holds every thread of the history (THREADS = 3, PER_PERSON = 2).

The final check's bench (rv-final/bench_vf_origins.py), short: main has a
long line of states of features.txt; Mei's branches exp and exp2 have their
own. Fabio (designer, no font settings) asks to set exp back to main."""

import asyncio
import time

import pytest
from aiohttp import web

from fontra_hive import fontdiff, historywork

pytest.importorskip("fontra")

from conftest import run  # noqa: E402
from fontra_hive.gitstore import Signature  # noqa: E402
from r2c_bench import ME, make_manager, request, store_of  # noqa: E402

MEI = Signature("Mei", "mei@example.com")


def clear_caches():
    fontdiff._commitSteps.clear()
    fontdiff._fontDataPrints.clear()
    fontdiff._mergeBases.clear()


class Counted:
    """fontdiff.file_changes, counted."""

    def __init__(self, monkeypatch):
        self.n = 0
        self.original = fontdiff.file_changes
        monkeypatch.setattr(fontdiff, "file_changes", self)

    def __call__(self, *args, **kwargs):
        self.n += 1
        return self.original(*args, **kwargs)


# --- fingerprints ----------------------------------------------------------------


def test_fingerprints_by_domain():
    a = {
        "font-data.json": b'{"fontInfo": {"familyName": "A"}, "customData":'
        b' {"umarks.rules": [1], "x": 1}}',
        "features.txt": b"f",
        "kerning.csv": b"k",
    }
    reordered = dict(a)
    reordered["font-data.json"] = (
        b'{"customData": {"x": 1, "umarks.rules": [1]},\n "fontInfo": {"familyName": "A"}}'
    )
    plugin = dict(a)
    plugin["font-data.json"] = (
        b'{"fontInfo": {"familyName": "A"}, "customData": {"umarks.rules": [2], "x": 1}}'
    )
    settings = dict(a)
    settings["font-data.json"] = (
        b'{"fontInfo": {"familyName": "A"}, "customData": {"umarks.rules": [1], "x": 2}}'
    )
    features = dict(a, **{"features.txt": b"g"})
    kerning = dict(a, **{"kerning.csv": b"l"})

    def differ(x, y):
        fx, fy = fontdiff.fingerprints(x), fontdiff.fingerprints(y)
        return {d for d in fx if fx[d] != fy[d]}

    assert differ(a, reordered) == set()
    assert differ(a, plugin) == {"plugindata"}
    assert differ(a, settings) == {"fontsettings"}
    assert differ(a, features) == {"fontsettings"}
    assert differ(a, kerning) == {"kerning"}
    broken = dict(a, **{"font-data.json": b"{not json"})
    assert differ(a, broken) == {"fontsettings", "plugindata"}
    assert fontdiff.blob_id(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_the_origins_are_never_read_when_their_blobs_say_enough():
    """A state of main is compared by its blob ids: neither features.txt nor
    kerning.csv is read."""

    class Store:
        def read_blob(self, sha):
            raise AssertionError(f"read {sha}")

    before = {"features.txt": b"mine", "kerning.csv": b"k"}
    candidates = [
        fontdiff.FileState(
            Store(),
            {
                "font-data.json": None,
                "features.txt": fontdiff.blob_id(b"main %d" % i),
                "kerning.csv": fontdiff.blob_id(b"k"),
            },
        )
        for i in range(300)
    ]
    held = fontdiff.domains_held(before, candidates, {"fontsettings", "kerning"})
    assert held == {"kerning"}


def test_one_person_never_holds_every_thread():
    assert historywork.PER_PERSON < historywork.THREADS
    assert (historywork.THREADS, historywork.PER_PERSON) == (3, 2)


# --- the bench, short -----------------------------------------------------------


# The final check's second bench (600 commits), with more states than are
# read: the whole walk is read (ORIGIN_STATES).
COMMITS = 600
STATES = 210
LINES = 20_000


def features(tag, lines=LINES):
    return "".join(
        f"  sub g{i} by g{i}.alt{f' # {tag}' if i % 2 else ''};\n" for i in range(lines)
    ).encode()


def build(manager, commits=COMMITS, states=STATES, lines=LINES):
    """main: ``commits`` commits, ``states`` of which change features.txt;
    exp and exp2 (Mei) start from the base with features of their own.
    Returns the base and main's states, oldest first."""
    store = store_of(manager)
    try:
        store.commit({"features.txt": features("base", lines)}, message="base", author=ME)
        base = store.head()
        for name in ("exp", "exp2"):
            store.create_branch(name, base)
            store.commit(
                {"features.txt": features(name, lines)},
                branch=name,
                message=name,
                author=MEI,
            )
        every = max(1, commits // states)
        made = []
        for i in range(commits):
            if i % every == 0 and len(made) < states:
                sha = store.commit(
                    {"features.txt": features(f"s{len(made)}", lines)},
                    message=f"state {len(made)}",
                    author=MEI,
                )
                made.append(sha)
            else:
                store.commit({"bench.txt": str(i).encode()}, message=f"c{i}", author=ME)
        return base, made
    finally:
        store.close()


def refused_merge(manager, into):
    return manager.mergeHandler(
        request(
            "Mutator",
            "fabio",
            method="POST",
            body={"resolutions": {"features.txt": "theirs"}},
            **{"from": "main", "into": into},
        )
    )


async def timed(coro):
    t = time.perf_counter()
    try:
        response = await coro
        out = response.status
    except web.HTTPException as error:
        out = error.status
    return out, time.perf_counter() - t


def test_a_refused_merge_costs_little_and_holds_up_nobody(
    tmp_path, fixture_fontra, monkeypatch
):
    """The final check's VF-1 proof, inverted. A refused merge read 201
    states of main and described them (202 descriptions, 5 s); two at once
    held both threads of the history, and Ana's history waited 10 s."""
    manager = make_manager(tmp_path, fixture_fontra)
    build(manager)
    counted = Counted(monkeypatch)

    async def go():
        anaAlone = await timed(manager.logHandler(request("Mutator", "ana", glyph="A")))
        clear_caches()
        counted.n = 0
        alone = await timed(refused_merge(manager, "exp"))
        described = counted.n

        clear_caches()
        jobs = [
            asyncio.ensure_future(timed(refused_merge(manager, "exp"))),
            asyncio.ensure_future(timed(refused_merge(manager, "exp2"))),
        ]
        await asyncio.sleep(0.02)
        ana = await timed(manager.logHandler(request("Mutator", "ana", glyph="A")))
        both = await asyncio.gather(*jobs)
        again = await timed(refused_merge(manager, "exp"))
        await manager.aclose()
        return anaAlone, alone, described, both, ana, again

    anaAlone, alone, described, both, ana, again = run(go())
    print(
        f"Ana's history alone {anaAlone[1]:.3f} s; "
        f"refused alone {alone[1]:.3f} s ({described} descriptions); "
        f"two at once {[f'{d:.3f} s' for _, d in both]}; Ana's history {ana[1]:.3f} s; "
        f"again {again[1]:.3f} s"
    )
    assert alone[0] == 403 and [o for o, _ in both] == [403, 403] and again[0] == 403
    assert described <= 4  # before, after and the reference: never main's states
    assert alone[1] < 0.5
    assert ana[0] == 200 and ana[1] < 0.5
    assert again[1] < 0.5
