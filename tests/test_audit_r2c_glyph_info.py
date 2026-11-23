"""R2C-5 (audit of 23 Nov 2026): restoring the glyph info never gives a
code point to two glyphs; what stays where it is, is said."""

import json

import pytest

pytest.importorskip("fontra")

from conftest import run  # noqa: E402
from fontra_hive import fontdiff  # noqa: E402
from r2c_bench import ME, body, make_manager, request, store_of  # noqa: E402


# --- R2C-5: one glyph per code point ----------------------------------------------


@pytest.mark.parametrize("bChanged", [False, True])
def test_glyph_info_gives_a_code_point_to_one_glyph(tmp_path, fixture_fontra, bChanged):
    """At v1, A has U+0041 and U+0061. Later, U+0061 moves to a new glyph
    'a'. Restoring Glyph info: U+0061 stays with a, and it is said (with
    another change, B's code points, or without)."""
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        info = store.read_file(store.head(), "glyph-info.csv").decode()
        rows = info.splitlines()
        rows = [r if not r.startswith("A;") else "A;U+0041,U+0061" for r in rows]
        if bChanged:
            rows = [r if not r.startswith("B;") else "B;U+0042,U+0062,U+00DF" for r in rows]
        store.commit(
            {"glyph-info.csv": ("\n".join(rows) + "\n").encode()}, message="v1", author=ME
        )
        v1 = store.head()
        rows2 = [r if not r.startswith("A;") else "A;U+0041" for r in rows] + ["a;U+0061"]
        rows2 = [r if not r.startswith("B;") else "B;U+0042,U+0062" for r in rows2]
        data = json.loads(store.read_file(v1, "glyphs/A^1.json"))
        data["name"] = "a"
        store.commit(
            {
                "glyph-info.csv": ("\n".join(rows2) + "\n").encode(),
                "glyphs/a.json": (json.dumps(data) + "\n").encode(),
            },
            message="a gets U+0061",
            author=ME,
        )
    finally:
        store.close()

    async def go():
        r = await manager.restoreFontHandler(
            request("Mutator", "fabio", method="POST", ref=v1, parts="glyphInfo")
        )
        return r.status, await body(r)

    status, answer = run(go())
    store = store_of(manager)
    try:
        glyphMap, _ = fontdiff.read_glyph_info(
            store.read_file(store.head(), "glyph-info.csv")
        )
    finally:
        store.close()
    owners = [name for name, cps in glyphMap.items() if 0x61 in cps]
    assert owners == ["a"]
    assert 0x41 in glyphMap["A"]
    assert status == 200 and answer["changed"] == bChanged, answer
    assert answer["result"].endswith(" U+0061 stays with a."), answer
    if bChanged:
        assert 0xDF in glyphMap["B"]
    else:  # A's row as at v1, U+0061 left with a: as it is now
        assert "nothing to restore" in answer["result"]


def test_kept_sentence():
    from fontra_hive.fontrestore import kept_sentence

    assert kept_sentence({}) == ""
    assert kept_sentence({0x61: "a"}) == "U+0061 stays with a."
    assert kept_sentence({0x61: "a", 0x62: "b", 0x63: "c", 0x1F600: "smile"}) == (
        "U+0061 stays with a; U+0062 stays with b; U+0063 stays with c; and 1 more."
    )
