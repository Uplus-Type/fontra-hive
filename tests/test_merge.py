"""Three-way merge of .fontra files (fontra_hive.merge)."""

import copy
import json

import pytest

from conftest import FIXTURE
from fontra_hive import merge
from fontra_hive.merge import (
    merge_font_data,
    merge_glyph,
    merge_glyph_info,
    merge_kerning,
    merge_trees,
)

GLYPH_A = (FIXTURE / "glyphs" / "A^1.json").read_bytes()
GLYPH_INFO = (FIXTURE / "glyph-info.csv").read_bytes()
FONT_DATA = (FIXTURE / "font-data.json").read_bytes()
BOLD = "MutatorSansBoldCondensed/foreground"
LIGHT = "MutatorSansLightCondensed/foreground"


def dump(data):
    return merge._dump_json(data)


def edit(glyph_bytes, change):
    glyph = json.loads(glyph_bytes)
    change(glyph)
    return dump(glyph)


def move_first_point(layer, dx):
    def change(glyph):
        glyph["layers"][layer]["glyph"]["path"]["contours"][0]["points"][0]["x"] += dx

    return change


def test_serialisation_is_fontras():
    assert dump(json.loads(GLYPH_A)) == GLYPH_A
    assert dump(json.loads(FONT_DATA)) == FONT_DATA
    assert merge._write_glyph_info(merge._read_glyph_info(GLYPH_INFO)) == GLYPH_INFO


def test_two_masters_of_one_glyph_merge():
    ours = edit(GLYPH_A, move_first_point(BOLD, 10))
    theirs = edit(GLYPH_A, move_first_point(LIGHT, -7))
    merged, parts = merge_glyph(GLYPH_A, ours, theirs)
    assert parts == []
    assert merged == edit(ours, move_first_point(LIGHT, -7))


def test_the_same_master_changed_twice_is_a_conflict():
    ours = edit(
        GLYPH_A,
        lambda g: (move_first_point(BOLD, 10)(g), move_first_point(LIGHT, 1)(g)),
    )
    theirs = edit(GLYPH_A, move_first_point(BOLD, 20))
    merged, parts = merge_glyph(GLYPH_A, ours, theirs)
    assert parts == [f"layer {BOLD}"]
    assert merged == ours  # ours wins by default
    merged, parts = merge_glyph(GLYPH_A, ours, theirs, prefer="theirs")
    # Theirs on the part in conflict, and what merged cleanly stays merged.
    assert merged == edit(theirs, move_first_point(LIGHT, 1))


def test_a_new_source_on_one_side():
    def add_source(glyph):
        glyph["sources"].append(
            {"name": "Medium", "layerName": "Medium", "location": {"weight": 500}}
        )
        glyph["layers"]["Medium"] = copy.deepcopy(glyph["layers"][BOLD])

    ours = edit(GLYPH_A, move_first_point(BOLD, 10))
    theirs = edit(GLYPH_A, add_source)
    merged, parts = merge_glyph(GLYPH_A, ours, theirs)
    assert parts == []
    glyph = json.loads(merged)
    assert [s["name"] for s in glyph["sources"]][-1] == "Medium"
    assert "Medium" in glyph["layers"]
    # The same source changed on both sides.
    ours = edit(GLYPH_A, lambda g: g["sources"][0].update(name="Thin"))
    theirs = edit(GLYPH_A, lambda g: g["sources"][0].update(name="Hairline"))
    merged, parts = merge_glyph(GLYPH_A, ours, theirs)
    assert parts == ["source Thin"]


def test_glyph_info_one_row_at_a_time():
    ours = GLYPH_INFO + b"Ours;U+E000\r\n"
    theirs = GLYPH_INFO.replace(b"B;U+0042,U+0062", b"B;U+0042") + b"Zed;\r\n"
    merged, parts = merge_glyph_info(GLYPH_INFO, ours, theirs)
    assert parts == []
    rows = merge._read_glyph_info(merged)
    assert rows["Ours"] == {"code points": "U+E000"} and "Zed" in rows
    assert rows["B"] == {"code points": "U+0042"}
    assert list(rows) == sorted(rows)
    ours = GLYPH_INFO.replace(b"A;U+0041,U+0061", b"A;U+0041")
    theirs = GLYPH_INFO.replace(b"A;U+0041,U+0061", b"A;U+0061")
    assert merge_glyph_info(GLYPH_INFO, ours, theirs)[1] == ["glyph A"]


def kerning_file(values, groups1=None):
    from fontra.core.classes import Kerning

    table = Kerning(
        groupsSide1=groups1 or {},
        groupsSide2={},
        sourceIdentifiers=["s1", "s2"],
        values=values,
    )
    return merge._write_kerning(
        {
            "kern": {
                "groups1": table.groupsSide1,
                "groups2": {},
                "sources": table.sourceIdentifiers,
                "values": {
                    left: {r: dict(zip(["s1", "s2"], row)) for r, row in rights.items()}
                    for left, rights in values.items()
                },
            }
        }
    )


def test_kerning_one_pair_at_a_time():
    base = kerning_file({"A": {"V": [-50, -80]}})
    ours = kerning_file({"A": {"V": [-50, -80], "W": [-20, -30]}})
    theirs = kerning_file({"A": {"V": [-60, -80]}, "T": {"o": [-40, None]}})
    merged, parts = merge_kerning(base, ours, theirs)
    assert parts == []
    table = merge._read_kerning(merged)["kern"]["values"]
    assert table == {
        "A": {"V": {"s1": -60, "s2": -80}, "W": {"s1": -20, "s2": -30}},
        "T": {"o": {"s1": -40}},
    }
    ours = kerning_file({"A": {"V": [-55, -80]}})
    merged, parts = merge_kerning(base, ours, theirs)
    assert parts == ["kern A V"]


def test_font_data_key_by_key():
    base = json.loads(FONT_DATA)
    ours = copy.deepcopy(base)
    ours["unitsPerEm"] = 2000
    theirs = copy.deepcopy(base)
    theirs["customData"] = {"x": 1}
    merged, parts = merge_font_data(FONT_DATA, dump(ours), dump(theirs))
    assert parts == []
    assert json.loads(merged) == {**ours, "customData": {"x": 1}}
    ours["axes"]["axes"] = []
    theirs["axes"]["axes"] = theirs["axes"]["axes"][:1]
    merged, parts = merge_font_data(FONT_DATA, dump(ours), dump(theirs))
    assert parts == ["axes › axes"]


def test_merge_trees():
    blobs = {
        "a0": GLYPH_A,
        "a1": edit(GLYPH_A, move_first_point(BOLD, 10)),
        "a2": edit(GLYPH_A, move_first_point(LIGHT, 5)),
        "b0": b"{}",
        "b1": b'{"name": "B", "x": 1}',
        "f0": b"languagesystem DFLT dflt;",
        "f1": b"# ours",
        "f2": b"# theirs",
        "n": b'{"name": "N"}',
    }
    base = {"glyphs/A.json": "a0", "glyphs/B.json": "b0", "features.txt": "f0"}
    ours = {"glyphs/A.json": "a1", "features.txt": "f1"}  # B deleted
    theirs = {
        "glyphs/A.json": "a2",
        "glyphs/B.json": "b1",
        "features.txt": "f2",
        "glyphs/N.json": "n",
    }
    result = merge_trees(base, ours, theirs, blobs.__getitem__)
    assert result.theirs_only == ["glyphs/N.json"]
    assert result.merged == ["glyphs/A.json"]
    assert sorted((c.path, c.kind, c.deleted) for c in result.conflicts) == [
        ("features.txt", "file", None),
        ("glyphs/B.json", "glyph", "ours"),
    ]
    assert [c.glyph for c in result.conflicts if c.kind == "glyph"] == ["B"]
    assert result.changes["glyphs/N.json"] == blobs["n"]

    result = merge_trees(
        base,
        ours,
        theirs,
        blobs.__getitem__,
        {"features.txt": "theirs", "glyphs/B.json": "ours"},
    )
    assert result.conflicts == [] and len(result.resolved) == 2
    assert result.changes["features.txt"] == blobs["f2"]
    assert "glyphs/B.json" not in result.changes  # stays deleted, as ours


@pytest.mark.parametrize("prefer", [None, "ours", "theirs"])
def test_nothing_changed_on_one_side(prefer):
    result = merge_trees(
        {"x": "1"}, {"x": "1"}, {"x": "2"}, {"1": b"1", "2": b"2"}.get, {"x": prefer}
    )
    assert result.changes == {"x": b"2"} and result.theirs_only == ["x"]
