"""VF-2 (final check of R2c, 23 Nov 2026): the guard of the names was
bypassed through the *shape* of a change (a path ``p`` or arguments ``a``
given as a JSON object, which Fontra reads by their keys). HiveFontHandler
now refuses, before the domains and the names, any change that does not have
the shape Fontra gives its changes: ``p`` a list of strings and integers,
``a`` a list, ``c`` a list of such changes, ``f`` one of Fontra's change
functions, no other key. ``layerName`` and ``elidedFallBackname`` are names.

The final check's proofs (rv-final/test_vf_names.py, test_vf_followup.py),
inverted: each is refused, and nothing changes in memory nor in git. Real
changes of each kind the editor makes still pass.

Ana may write every domain; Fabio has "edit" only."""

import asyncio
import json

import pytest

from fontra_hive.writedomains import (
    NAME_FIELDS,
    SHAPE_REFUSAL,
    changeShapeError,
)

pytest.importorskip("fontra")

from fontra_hive.fonthandler import CHANGE_FUNCTIONS, InvalidNameError  # noqa: E402
from r2c_bench import body, connection, make_manager, request, store_of  # noqa: E402

FORGED = "Hive-Snapshot: Forged by a dict path"
LAYER = "MutatorSansLightCondensed/foreground"
PATH = ["glyphs", "A", "layers", LAYER, "glyph", "path"]


def state(manager):
    store = store_of(manager)
    try:
        head = store.head()
        data = json.loads(store.read_file(head, "font-data.json"))
        files = sorted(store.list_tree(head))
        return head, data, files
    finally:
        store.close()


async def settle(handler):
    await handler.finishWriting()
    await asyncio.sleep(0.2)
    handler.backend.flush()


# --- the shape ------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"p": {"sources": 0, "3aa9223b": 0}, "f": "=", "a": ["name", "x"]},
        {"p": ["glyphMap"], "f": "=", "a": {"zz": 0, "66": 0}},
        {"p": "glyphs", "f": "=", "a": ["x", 1]},
        {"p": ["glyphs", None], "f": "=", "a": ["x", 1]},
        {"p": ["glyphs", True], "f": "=", "a": ["x", 1]},
        {"p": ["glyphs", 1.5], "f": "=", "a": ["x", 1]},
        {"p": ["glyphs"], "f": "unknown", "a": ["x", 1]},
        {"p": ["glyphs"], "f": None},
        {"p": ["glyphs"], "f": "=", "a": ["x", 1], "x": 1},
        {"c": {"p": ["glyphs"]}},
        {"c": ["x", {"p": ["glyphs"]}]},
        {"c": [{"c": [{"p": {"glyphMap": 0}, "f": "=", "a": ["zz", []]}]}]},
        "change",
        [],
    ],
)
def test_a_change_of_another_shape_is_refused(change):
    assert changeShapeError(change, CHANGE_FUNCTIONS) is not None


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"p": []},
        {"p": PATH, "f": "=xy", "a": [0, 1, 2]},
        {"p": ["glyphMap"], "f": "d", "a": ["A"]},
        {"c": [{"p": ["fontInfo"], "f": "=", "a": ["familyName", "X"]}, {"c": []}]},
        {"p": ["axes", "axes"], "f": "+", "a": [0, {"name": "w", "tag": "wdth"}]},
    ],
)
def test_a_change_as_fontra_makes_it_has_the_shape(change):
    assert changeShapeError(change, CHANGE_FUNCTIONS) is None


def test_fontra_change_functions_are_known():
    # The base functions and the path functions the editor uses.
    assert {"=", "d", "+", "-", ":", "=xy"} <= CHANGE_FUNCTIONS


def test_layer_names_and_the_elided_fallback_name_are_names():
    assert {"layerName", "elidedFallBackname"} <= NAME_FIELDS


# --- the final check's proofs, inverted ----------------------------------------------


async def refused(handler, user, change, *, live=False):
    conn = connection(user)
    with pytest.raises(InvalidNameError) as error:
        if live:
            await handler.editIncremental(change, connection=conn)
        else:
            await handler.editFinal(change, {}, "edit", False, connection=conn)
    return str(error.value)


def test_a_dict_path_cannot_write_a_source_name(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    before = state(manager)
    change = {"p": {"sources": 0, "3aa9223b": 0}, "f": "=", "a": ["name", "Light\n" + FORGED]}

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:ana", False)
        message = await refused(handler, "ana", change)
        live = await refused(handler, "ana", change, live=True)
        sources = await handler.getSources()
        await settle(handler)
        snaps = await body(await manager.snapshotsHandler(request("Mutator", "ana")))
        await manager.aclose()
        return message, live, sources, snaps

    message, live, sources, snaps = asyncio.run(go())
    assert message == live == SHAPE_REFUSAL
    assert sources["3aa9223b"].name == "Support"  # not in memory
    assert snaps["snapshots"] == []
    assert state(manager) == before  # nothing committed


def test_a_dict_path_cannot_make_a_glyph(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    before = state(manager)
    glyph = "zz\nHive-Glyphs: B"
    change = {
        "c": [
            {"p": {"glyphMap": 0}, "f": "=", "a": [glyph, []]},
            {"p": {"glyphs": 0}, "f": "=", "a": [glyph, {"name": glyph, "layers": {}}]},
        ]
    }

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:ana", False)
        await refused(handler, "ana", change)
        glyphMap = await handler.getGlyphMap()
        await settle(handler)
        await manager.aclose()
        return glyphMap

    glyphMap = asyncio.run(go())
    assert glyph not in glyphMap
    assert state(manager) == before


@pytest.mark.parametrize("live", [False, True])
def test_fabio_cannot_write_a_glyph_map_name_with_dict_arguments(
    tmp_path, fixture_fontra, live
):
    """The final check's VF-2 proof for Fabio, inverted: the glyph map in
    memory does not change (it held "zz⏎Hive-Snapshot: …" → [6, 6])."""
    manager = make_manager(tmp_path, fixture_fontra)
    before = state(manager)
    name = "zz\nHive-Snapshot: Forged by dict arguments"
    change = {"p": ["glyphMap"], "f": "=", "a": {name: 0, "66": 0}}

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:fabio", False)
        message = await refused(handler, "fabio", change, live=live)
        glyphMap = await handler.getGlyphMap()
        await settle(handler)
        await manager.aclose()
        return message, glyphMap

    message, glyphMap = asyncio.run(go())
    assert message == SHAPE_REFUSAL
    assert name not in glyphMap and not any("\n" in g for g in glyphMap)
    assert state(manager) == before


@pytest.mark.parametrize(
    "who, change",
    [
        ("fabio", {"p": ["glyphs", "A", "sources", 0], "f": "=", "a": ["layerName", "x\nHive-Snapshot: y"]}),
        ("ana", {"p": ["axes"], "f": "=", "a": ["elidedFallBackname", "Reg\nular"]}),
    ],
    ids=["glyph-layerName", "elidedFallBackname"],
)
def test_the_new_name_fields_are_refused(tmp_path, fixture_fontra, who, change):
    manager = make_manager(tmp_path, fixture_fontra)
    before = state(manager)

    async def go():
        handler = await manager.getRemoteSubject("Mutator", f"hive-user:{who}", False)
        message = await refused(handler, who, change)
        await settle(handler)
        await manager.aclose()
        return message

    assert asyncio.run(go()).startswith("A name cannot contain")
    assert state(manager) == before


# --- what the editor sends still passes ------------------------------------------------


def editor_changes():
    """One change of each kind, as the editor sends them (final, rollback)."""
    move = {"p": PATH, "f": "=xy", "a": [0, -12, 0]}
    moveBack = {"p": PATH, "f": "=xy", "a": [0, -10, 0]}
    return {
        "draw": (
            {"p": ["glyphs", "A"], "c": [{"p": ["layers", LAYER, "glyph", "path"], "f": "=xy", "a": [0, -12, 0]}]},
            {"p": ["glyphs", "A"], "c": [{"p": ["layers", LAYER, "glyph", "path"], "f": "=xy", "a": [0, -10, 0]}]},
        ),
        "draw-live": (move, moveBack),
        "rename-glyph": (
            {
                "c": [
                    {"p": ["glyphMap"], "f": "=", "a": ["A.ss01", []]},
                    {"p": ["glyphMap"], "f": "d", "a": ["A.alt"]},
                ]
            },
            {
                "c": [
                    {"p": ["glyphMap"], "f": "=", "a": ["A.alt", []]},
                    {"p": ["glyphMap"], "f": "d", "a": ["A.ss01"]},
                ]
            },
        ),
        "font-info": (
            {"p": ["fontInfo"], "f": "=", "a": ["familyName", "Mutator Test"]},
            {"p": ["fontInfo"], "f": "=", "a": ["familyName", None]},
        ),
        "kerning": (
            {
                "p": ["kerning"],
                "f": "=",
                "a": [
                    "kern",
                    {
                        "groupsSide1": {},
                        "groupsSide2": {},
                        "sourceIdentifiers": ["3aa9223b"],
                        "values": {"A": {"B": [-10]}},
                    },
                ],
            },
            {"p": ["kerning"], "f": "d", "a": ["kern"]},
        ),
        "axes": (
            {"p": ["axes", "axes", 0], "f": "=", "a": ["label", "Width"]},
            {"p": ["axes", "axes", 0], "f": "=", "a": ["label", "width"]},
        ),
        "sources": (
            {"p": ["sources", "3aa9223b"], "f": "=", "a": ["name", "Support 2"]},
            {"p": ["sources", "3aa9223b"], "f": "=", "a": ["name", "Support"]},
        ),
        "background-image": (
            {
                "p": ["glyphs", "A", "layers", LAYER, "glyph"],
                "f": "=",
                "a": ["backgroundImage", {"identifier": "7b0c1f3e-0000-4000-8000-000000000001"}],
            },
            {"p": ["glyphs", "A", "layers", LAYER, "glyph"], "f": "=", "a": ["backgroundImage", None]},
        ),
    }


@pytest.mark.parametrize("kind", sorted(editor_changes()))
def test_the_editors_changes_still_pass(tmp_path, fixture_fontra, kind):
    """Ana sends the change live, then final, then undoes and redoes it (the
    rollback sent as a final change, and back): every step passes."""
    manager = make_manager(tmp_path, fixture_fontra)
    final, rollback = editor_changes()[kind]

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:ana", False)
        ana = connection("ana")
        await handler.getGlyph("A")
        await handler.editIncremental(final, connection=ana)
        await handler.editFinal(final, rollback, "edit", False, connection=ana)
        await handler.editFinal(rollback, final, "undo", False, connection=ana)  # undo
        await handler.editFinal(final, rollback, "redo", False, connection=ana)  # redo
        await settle(handler)
        await manager.aclose()

    asyncio.run(go())
