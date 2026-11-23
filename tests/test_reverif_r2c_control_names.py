"""A name with a control character or a line break is refused at the guard
(re-check of the R2c audit, outside its surface): a glyph, a source, an
axis, a kerning group, a layer, a key of the custom data or of a plug-in.
HiveFontHandler refuses it in editIncremental and editFinal, before it is
written or sent to anyone: a defence on top of one_line, which keeps
commit messages safe whatever comes through. Texts that may hold line
breaks (font info, features, notes) are untouched, and an old name with a
line break can still be fixed."""

import asyncio

import pytest

from fontra_hive.writedomains import NAME_REFUSAL, nameWithControlCharacter

pytest.importorskip("fontra")

from fontra_hive.fonthandler import InvalidNameError  # noqa: E402
from r2c_bench import connection, make_manager, store_of  # noqa: E402

FORGED = "Hive-Snapshot: Forged"

REFUSED = {
    "axis-tag-U+0085": {"p": ["axes", "axes", 1], "f": "=", "a": ["tag", "wg\x85" + FORGED]},
    "axis-label-U+2028": {"p": ["axes", "axes", 0], "f": "=", "a": ["label", "w " + FORGED]},
    "source-CR": {"p": ["sources", "3aa9223b"], "f": "=", "a": ["name", "Light\r" + FORGED]},
    "source-U+2029": {"p": ["sources", "3aa9223b"], "f": "=", "a": ["name", "Light x"]},
    "source-tab": {"p": ["sources", "3aa9223b"], "f": "=", "a": ["name", "Light\tx"]},
    "plugin-key-U+0085": {"p": ["customData"], "f": "=", "a": ["umarks.x\x85" + FORGED, {"a": 1}]},
    "custom-data-key-VT": {"p": ["customData"], "f": "=", "a": ["com.x\x0b" + FORGED, 1]},
    "font-info-custom-key-FS": {
        "p": ["fontInfo", "customData"],
        "f": "=",
        "a": ["k\x1c" + FORGED, "v"],
    },
    "kerning-group-LF": {
        "p": ["kerning"],
        "f": "=",
        "a": [
            "kern",
            {
                "groupsSide1": {"O\n" + FORGED: ["O"]},
                "groupsSide2": {},
                "sourceIdentifiers": ["3aa9223b"],
                "values": {},
            },
        ],
    },
    "kerning-group-member": {
        "p": ["kerning", "kern", "groupsSide1"],
        "f": "=",
        "a": ["O", ["O", "Q\nx"]],
    },
    "kerning-group-insert": {
        "p": ["kerning", "kern", "groupsSide1", "O"],
        "f": "+",
        "a": [0, "Q\x00"],
    },
    "glyph-map": {"p": ["glyphMap"], "f": "=", "a": ["zz\nHive-Glyphs: B", []]},
    "glyph-in-children": {
        "c": [{"p": ["glyphs"], "f": "=", "a": ["zz\x85", {"name": "zz\x85", "layers": {}}]}]
    },
    "axis-inserted": {"p": ["axes", "axes"], "f": "+", "a": [0, {"name": "w\nx", "tag": "wdth"}]},
    "component": {
        "p": ["glyphs", "A", "layers", "L", "glyph", "components"],
        "f": "+",
        "a": [0, {"name": "B\r", "location": {}}],
    },
}

ALLOWED = {
    "copyright": {"p": ["fontInfo"], "f": "=", "a": ["copyright", "© 2026\nAll rights"]},
    "features": {"p": ["features"], "f": "=", "a": ["text", "feature kern {\n} kern;\n"]},
    "glyph-note": {"p": ["glyphs", "A", "customData"], "f": "=", "a": ["note", "a\nb"]},
    "source-name": {"p": ["sources", "3aa9223b"], "f": "=", "a": ["name", "Light Wide"]},
    "point-move": {"p": ["glyphs", "A", "layers", "L", "glyph", "path"], "f": "=xy", "a": [0, 1, 2]},
    # What exists already: edited inside, or deleted.
    "inside-an-old-glyph": {"p": ["glyphs", "zz\n", "layers", "L"], "f": "=", "a": ["x", 1]},
    "delete-an-old-glyph": {"p": ["glyphMap"], "f": "d", "a": ["zz\n"]},
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_name_with_a_control_character_is_found(case):
    assert nameWithControlCharacter(REFUSED[case]) is not None


@pytest.mark.parametrize("case", sorted(ALLOWED))
def test_other_texts_may_hold_line_breaks(case):
    assert nameWithControlCharacter(ALLOWED[case]) is None


EDITOR_CASES = [
    "axis-tag-U+0085",
    "axis-label-U+2028",
    "source-CR",
    "plugin-key-U+0085",
    "custom-data-key-VT",
    "font-info-custom-key-FS",
    "kerning-group-LF",
]


@pytest.mark.parametrize("case", EDITOR_CASES)
def test_the_editor_refuses_it(tmp_path, fixture_fontra, case):
    """Ana (who may write every domain) sends the change: refused, with the
    sentence, and nothing is committed."""
    manager = make_manager(tmp_path, fixture_fontra)
    head = store_of(manager).head()

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:ana", False)
        ana = connection("ana")
        change = REFUSED[case]
        with pytest.raises(InvalidNameError) as refused:
            await handler.editIncremental(change, connection=ana)
        assert str(refused.value).startswith(NAME_REFUSAL)
        with pytest.raises(InvalidNameError):
            await handler.editFinal(change, {}, "edit", False, connection=ana)
        await handler.finishWriting()
        await asyncio.sleep(0.2)
        handler.backend.flush()
        await manager.aclose()

    asyncio.run(go())
    assert store_of(manager).head() == head


def test_a_glyph_with_a_line_break_in_its_name_is_refused(tmp_path, fixture_fontra):
    """Fabio (edit only) makes a glyph named "zz⏎Hive-Snapshot: …": refused."""
    manager = make_manager(tmp_path, fixture_fontra)
    head = store_of(manager).head()

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:fabio", False)
        fabio = connection("fabio")
        name = "zz\nHive-Snapshot: Approved"
        change = {
            "c": [
                {"p": ["glyphMap"], "f": "=", "a": [name, []]},
                {"p": ["glyphs"], "f": "=", "a": [name, {"name": name, "layers": {}}]},
            ]
        }
        with pytest.raises(InvalidNameError, match="Approved"):
            await handler.editFinal(change, {}, "new glyph", False, connection=fabio)
        await manager.aclose()

    asyncio.run(go())
    assert store_of(manager).head() == head


def test_an_old_name_with_a_line_break_can_be_fixed(tmp_path, fixture_fontra):
    """The rollback of a rename holds the old name: it is not checked, so a
    source named with a line break before the guard can be renamed."""
    manager = make_manager(tmp_path, fixture_fontra)

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:ana", False)
        ana = connection("ana")
        await handler.editFinal(
            {"p": ["sources", "3aa9223b"], "f": "=", "a": ["name", "Light"]},
            {"p": ["sources", "3aa9223b"], "f": "=", "a": ["name", "Old\nname"]},
            "rename",
            False,
            connection=ana,
        )
        await handler.finishWriting()
        await asyncio.sleep(0.3)
        handler.backend.flush()
        await manager.aclose()

    asyncio.run(go())
    store = store_of(manager)
    try:
        assert "Light" in store.commit_info(store.head()).message
    finally:
        store.close()
