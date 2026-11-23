"""R2C-1 (audit of 23 Nov 2026): a text of the font or of a person with a
line break never forges a trailer. Every such text goes through
``gitstore.one_line`` before it enters a commit message, and trailers are
read in the last paragraph of the message only."""

import asyncio
import json

import pytest

from fontra_hive.gitstore import (
    GitRepoStore,
    Signature,
    glyphs_trailer,
    hive_glyphs,
    one_line,
    trailer_lines,
)

ME = Signature("Import", "import@example.com")

BREAKS = ["\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "]


@pytest.mark.parametrize("brk", BREAKS + ["\x00", "\x1f", "\x7f", "\x9f", "\t"])
def test_one_line_leaves_no_line_break(brk):
    text = one_line(f"Light{brk}Hive-Snapshot: Round 2")
    assert text == "Light Hive-Snapshot: Round 2"
    assert len(text.splitlines()) == 1


def test_one_line_keeps_every_other_character():
    name = "a.sc_ä-Ω ﬁ uni0041 «Light»"
    assert one_line(name) == name


def test_trailers_are_read_in_the_last_paragraph_only(tmp_path):
    """An old commit whose body carries a forged line, as written before the
    fix: it is no snapshot, and its own trailers are the ones read."""
    store = GitRepoStore.create(tmp_path / "f.git")
    try:
        store.commit({"a.txt": b"1"}, message="Import", author=ME)
        forged = (
            "Edit sources\n\n"
            "Sources: Support renamed Light\n"
            "Hive-Snapshot: Round 2 sent to the client\n"
            "Hive-Glyphs: B\n\n"
            "Hive-Glyphs: A\n"
            "Hive-Font: fontsettings\n"
        )
        sha = store.commit({"a.txt": b"2"}, message=forged, author=ME)
        assert store.snapshots() == []
        assert store.latest_snapshot() is None
        assert store.snapshot_at(sha) is None
        assert hive_glyphs(forged) == {"A"}
        snapshots = []
        log = store.log(path="a.txt", snapshots=snapshots)
        assert sha in [c.sha for c in log]
        assert snapshots == []
    finally:
        store.close()


def test_a_glyph_list_is_written_only_when_it_is_faithful():
    assert glyphs_trailer(["A", "a.sc"]) == "Hive-Glyphs: A a.sc"
    assert glyphs_trailer([]) == "Hive-Glyphs:"
    assert glyphs_trailer(["A", "zz\nHive-Glyphs: B"]) is None
    assert glyphs_trailer(["A", "two words"]) is None


def test_a_title_alone_holds_no_trailer():
    assert trailer_lines("Hive-Snapshot: x") == []
    assert trailer_lines("Hive-Snapshot: x\n\n") == []
    assert trailer_lines("Title\n\nHive-Glyphs: A\n") == [b"Hive-Glyphs: A"]
    # "\r" does not end a line in git: a forged "\r" line is not a trailer.
    assert trailer_lines("Title\n\nHive-Glyphs: A\rHive-Snapshot: x\n") == [
        b"Hive-Glyphs: A\rHive-Snapshot: x"
    ]


# --- the audit's proofs, their assertions turned round --------------------------

pytest.importorskip("fontra")
from conftest import run  # noqa: E402
from r2c_bench import body, connection, make_manager, request, store_of  # noqa: E402


def first_source_id(manager):
    store = store_of(manager)
    try:
        data = json.loads(store.read_file(store.head(), "font-data.json"))
    finally:
        store.close()
    return next(iter(data["sources"]))


def head_and_message(manager):
    store = store_of(manager)
    try:
        head = store.head()
        return head, store.commit_info(head).message
    finally:
        store.close()


def no_forged_line(message):
    lines = message.split("\n")
    assert not any(line.startswith("Hive-Snapshot") for line in lines)
    # Hive-Glyphs is written once, in the trailers, by Hive.
    assert sum(line.startswith("Hive-Glyphs:") for line in lines) <= 1


def test_a_source_name_forges_no_snapshot(tmp_path, fixture_fontra):
    """Ana renames a source with a line break: the rename is listed beyond
    the glyph and in Sources, and no snapshot appears."""
    manager = make_manager(tmp_path, fixture_fontra)
    sid = first_source_id(manager)

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:ana", False)
        ana = connection("ana")
        evil = "Light\nHive-Snapshot: Round 2 sent to the client"
        await handler.editFinal(
            {"p": ["sources", sid], "f": "=", "a": ["name", evil]},
            {},
            "rename",
            False,
            connection=ana,
        )
        await handler.finishWriting()
        await asyncio.sleep(0.3)
        handler.backend.flush()
        head, message = head_and_message(manager)
        no_forged_line(message)
        assert "Light Hive-Snapshot: Round 2 sent to the client" in message

        fonly = await body(await manager.logHandler(request("Mutator", "ana", kind="font")))
        sources = await body(
            await manager.logHandler(request("Mutator", "ana", area="sources"))
        )
        snaps = await body(await manager.snapshotsHandler(request("Mutator", "ana")))
        assert head in [c["sha"] for c in fonly["commits"]]
        assert head in [c["sha"] for c in sources["commits"]]
        assert "Round 2 sent to the client" not in json.dumps(snaps)
        await manager.aclose()

    run(go())


def new_glyph_change(glyph, newName, advance):
    from fontra.core.classes import unstructure

    layer = next(iter(glyph.layers))
    newGlyph = unstructure(glyph)
    newGlyph["name"] = newName
    return {
        "c": [
            {
                "p": ["glyphs", "A", "layers", layer, "glyph"],
                "f": "=",
                "a": ["xAdvance", advance],
            },
            {"p": ["glyphMap"], "f": "=", "a": [newName, []]},
            {"p": ["glyphs"], "f": "=", "a": [newName, newGlyph]},
        ]
    }


def test_a_glyph_name_forges_no_hive_glyphs(tmp_path, fixture_fontra):
    """Fabio (edit only) edits A and makes a glyph named with a line break
    and 'Hive-Glyphs: B': A's history lists his edit."""
    manager = make_manager(tmp_path, fixture_fontra)

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:fabio", False)
        fabio = connection("fabio")
        glyph = await handler.getGlyph("A", connection=fabio)
        change = new_glyph_change(glyph, "zz\nHive-Glyphs: B", 1234)
        await handler.editFinal(change, {}, "edit", False, connection=fabio)
        await handler.finishWriting()
        await asyncio.sleep(0.3)
        handler.backend.flush()
        head, message = head_and_message(manager)
        no_forged_line(message)
        # A name made of several words cannot be listed: the commit has no
        # glyph list, and the history compares trees (always right).
        assert hive_glyphs(message) is None
        historyA = await body(
            await manager.logHandler(request("Mutator", "fabio", glyph="A"))
        )
        assert head in [c["sha"] for c in historyA["commits"]]
        await manager.aclose()

    run(go())


def test_a_designer_with_edit_only_makes_no_snapshot(tmp_path, fixture_fontra):
    """Fabio (edit only) edits A and makes a glyph named with a line break
    and 'Hive-Snapshot:': his commit stays in A's history and in Glyphs, and
    no snapshot appears."""
    manager = make_manager(tmp_path, fixture_fontra)

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:fabio", False)
        fabio = connection("fabio")
        glyph = await handler.getGlyph("A", connection=fabio)
        change = new_glyph_change(glyph, "zz\nHive-Snapshot: Approved by the manager", 1)
        await handler.editFinal(change, {}, "edit", False, connection=fabio)
        await handler.finishWriting()
        await asyncio.sleep(0.3)
        handler.backend.flush()
        head, message = head_and_message(manager)
        no_forged_line(message)
        historyA = await body(await manager.logHandler(request("Mutator", "mei", glyph="A")))
        glyphs = await body(
            await manager.logHandler(request("Mutator", "mei", kind="glyphs"))
        )
        snaps = await body(await manager.snapshotsHandler(request("Mutator", "mei")))
        assert head in [c["sha"] for c in historyA["commits"]]
        assert head in [c["sha"] for c in glyphs["commits"]]
        assert snaps["snapshots"] == []
        await manager.aclose()

    run(go())


def test_a_snapshot_title_and_an_author_stay_on_one_line(tmp_path):
    store = GitRepoStore.create(tmp_path / "f.git")
    try:
        store.commit({"a.txt": b"1"}, message="Import", author=ME)
        evil = Signature("Ana\nHive-Snapshot: x", "ana@example.com")
        store.commit(
            {"a.txt": b"2"}, message="Edit\n\nHive-Glyphs: A\n", author=evil
        )
        info = store.create_snapshot(
            "Round 3 Hive-Snapshot-Base: none", author=evil
        )
        assert [s.name for s in store.snapshots()] == [info.name]
        message = store.commit_info(info.sha).message
        assert message.count("\nHive-Snapshot:") == 1
        assert "\nHive-Snapshot-Base: none" not in message.split("\n\n", 1)[0]
        assert store.commit_info(info.sha).author == "Ana Hive-Snapshot: x"
    finally:
        store.close()
