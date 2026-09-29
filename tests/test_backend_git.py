import json
import asyncio
import copy

import pytest
from fontra.backends.fontra import FontraBackend
from fontra.core.classes import Kerning, OpenTypeFeatures

from conftest import files_of, run
from fontra_hive.backend_git import GitFontraBackend, current_author
from fontra_hive.gitstore import GitRepoStore, Signature

ME = Signature("Jérémie", "j@example.com")


def make(tmp_path, fixture_fontra, **kwargs):
    store = GitRepoStore.create(tmp_path / "proj.git")
    sha = store.import_directory(fixture_fontra, message="Import", author=ME)
    kwargs.setdefault("commit_delay", 0.05)
    backend = GitFontraBackend(store, "main", author=ME, **kwargs)
    return store, sha, backend


async def edited_glyph(backend, name="A"):
    glyph = copy.deepcopy(await backend.getGlyph(name))
    layer = next(iter(glyph.layers.values()))
    layer.glyph.xAdvance = (layer.glyph.xAdvance or 0) + 10
    return glyph


async def apply_edits(backend, glyph):
    """The same set of edits, applied to any writable backend."""
    await backend.putGlyph("A", glyph, [0x41, 0x61])
    await backend.putGlyph("Z", glyph, [0x5A])
    await backend.deleteGlyph("B")
    await backend.putKerning(
        {
            "kern": Kerning(
                groupsSide1={"A": ["A", "A.alt"]},
                groupsSide2={},
                sourceIdentifiers=["light"],
                values={"@A": {"Z": [-20]}},
            )
        }
    )
    await backend.putFeatures(OpenTypeFeatures(text="# features\n"))
    await backend.putCustomData({"hive": True})


def test_reads_like_the_filesystem_backend(tmp_path, fixture_fontra):
    async def go():
        store, sha, backend = make(tmp_path, fixture_fontra)
        reference = FontraBackend.fromPath(fixture_fontra)
        assert await backend.getGlyphMap() == await reference.getGlyphMap()
        for name in await reference.getGlyphMap():
            assert await backend.getGlyph(name) == await reference.getGlyph(name)
        assert await backend.getAxes() == await reference.getAxes()
        assert await backend.getSources() == await reference.getSources()
        assert await backend.getFontInfo() == await reference.getFontInfo()
        assert await backend.getUnitsPerEm() == await reference.getUnitsPerEm()
        assert await backend.getKerning() == await reference.getKerning()
        assert await backend.getGlyph("nope") is None
        assert await backend.findGlyphsThatUseGlyph(
            "A"
        ) == await reference.findGlyphsThatUseGlyph("A")
        await backend.aclose()
        await reference.aclose()

    run(go())


def test_edits_are_committed_and_byte_identical(tmp_path, fixture_fontra):
    async def go():
        store, sha0, backend = make(tmp_path, fixture_fontra)
        glyph = await edited_glyph(backend)
        await apply_edits(backend, glyph)
        assert store.head() == sha0  # not yet: the commit is scheduled
        await asyncio.sleep(0.5)  # FontraBackend scheduler (0.2 s) + commit delay
        head = store.head()
        assert head != sha0 and not backend.tree.has_pending()
        info = store.commit_info(head)
        assert info.author == "Jérémie" and info.parents == (sha0,)
        assert info.message.splitlines()[0].startswith("Edit A, B, Z")
        assert "Hive-Glyphs: A B Z" in info.message

        # Reopen from git: same data.
        reopened = GitFontraBackend(store, "main")
        assert await reopened.getGlyphMap() == {
            "A": [0x41, 0x61],
            "A.alt": [],
            "Z": [0x5A],
        }
        assert await reopened.getGlyph("A") == glyph
        assert (await reopened.getFeatures()).text == "# features\n"
        assert await reopened.getCustomData() == {"hive": True}

        # Byte-for-byte: a FontraBackend on disk given the same edits writes the same files.
        disk = tmp_path / "disk.fontra"
        import shutil

        shutil.copytree(fixture_fontra, disk)
        fs = FontraBackend.fromPath(disk)
        await apply_edits(fs, glyph)
        await fs.aclose()
        exported = tmp_path / "export.fontra"
        store.export("main", exported)
        assert files_of(exported) == files_of(disk)

        await backend.aclose()
        await reopened.aclose()

    run(go())


def test_flush_and_aclose_commit_immediately(tmp_path, fixture_fontra):
    async def go():
        store, sha0, backend = make(tmp_path, fixture_fontra, commit_delay=60)
        glyph = await edited_glyph(backend)
        await backend.putGlyph("A", glyph, [0x41, 0x61])
        backend.flush()
        sha1 = store.head()
        assert sha1 != sha0
        await backend.putGlyph("A.alt", glyph, [])
        await backend.aclose()
        assert store.head() != sha1

    run(go())


def test_attribution_per_user(tmp_path, fixture_fontra):
    async def go():
        store, sha0, backend = make(tmp_path, fixture_fontra, commit_delay=60)
        glyph = await edited_glyph(backend)
        token = current_author.set(Signature("Gaëtan", "g@example.com"))
        await backend.putGlyph("A", glyph, [0x41, 0x61])
        current_author.reset(token)
        token = current_author.set(Signature("Olli", "o@example.com"))
        await backend.putGlyph("A.alt", glyph, [])
        current_author.reset(token)
        backend.flush()
        authors = [c.author for c in store.log()]
        assert sorted(authors[:2]) == ["Gaëtan", "Olli"] and authors[2] == "Jérémie"
        await backend.aclose()

    run(go())


def test_external_change_is_detected(tmp_path, fixture_fontra):
    async def go():
        store, sha0, backend = make(tmp_path, fixture_fontra)
        received = []

        async def callback(pattern):
            received.append(pattern)

        await backend.watchExternalChanges(callback)
        assert await backend.check_external_changes() == {}

        # Someone else deletes B (file + glyph-info row) and edits A.alt.
        glyph_info = store.read_file(sha0, "glyph-info.csv").replace(
            b"B;U+0042,U+0062\r\n", b""
        )
        store.commit(
            {
                "glyphs/B^1.json": None,
                "glyph-info.csv": glyph_info,
                "glyphs/A.alt^1.json": store.read_file(sha0, "glyphs/A^1.json"),
            },
            message="elsewhere",
            author=Signature("Other", "x@example.com"),
        )
        pattern = await backend.check_external_changes()
        assert pattern == {"glyphMap": None, "glyphs": {"A.alt": None, "B": None}}
        assert received == [pattern]
        assert await backend.getGlyph("B") is None
        assert "B" not in await backend.getGlyphMap()

        # A change to font-data.json means "reload everything" (None).
        import json

        font_data = json.loads(store.read_file(store.head(), "font-data.json"))
        font_data["unitsPerEm"] = 2000
        data = json.dumps(font_data).encode()
        store.commit(
            {"font-data.json": data},
            message="upm",
            author=Signature("Other", "x@example.com"),
        )
        assert await backend.check_external_changes() is None
        assert await backend.getUnitsPerEm() == 2000
        await backend.aclose()

    run(go())


def test_concurrent_commit_is_retried_on_new_head(tmp_path, fixture_fontra):
    async def go():
        store, sha0, backend = make(tmp_path, fixture_fontra, commit_delay=60)
        glyph = await edited_glyph(backend)
        await backend.putGlyph("A", glyph, [0x41, 0x61])
        other = store.commit(
            {"features.txt": b"# other\n"},
            message="other",
            author=Signature("Other", "x@example.com"),
        )
        backend.flush()  # our commit lands on top of the other one
        head = store.head()
        assert store.commit_info(head).parents == (other,)
        assert store.read_file(head, "features.txt") == b"# other\n"
        assert "glyphs/A^1.json" in {c.path for c in store.diff(other, head)}
        await backend.aclose()

    run(go())


def test_read_only_backend_refuses_writes(tmp_path, fixture_fontra):
    async def go():
        store, sha0, backend = make(tmp_path, fixture_fontra, read_only=True)
        glyph = await edited_glyph(backend)
        with pytest.raises(PermissionError):
            await backend.putGlyph("A", glyph, [0x41])
        await backend.aclose()
        assert store.head() == sha0

    run(go())


def test_from_path_with_branch(tmp_path, fixture_fontra):
    async def go():
        store, sha0, backend = make(tmp_path, fixture_fontra)
        store.create_branch("bold")
        b = GitFontraBackend.fromPath(f"{store.path}@bold")
        assert b.branch == "bold" and b.head == sha0
        with pytest.raises(FileNotFoundError):
            GitFontraBackend(store, "nope")
        await backend.aclose()
        await b.aclose()

    run(go())


def test_glyphs_using_a_glyph_follow_external_changes(tmp_path, fixture_fontra):
    """ "Glyphs using this glyph as a component" (Fontra's Related Glyphs
    panel) stays true after a change made elsewhere: a Restore, an import."""

    async def go():
        store, sha0, backend = make(tmp_path, fixture_fontra)
        assert await backend.findGlyphsThatUseGlyph("A") == ["A.alt"]
        other = Signature("Other", "x@example.com")
        # Elsewhere: A.alt no longer uses A; a new glyph Aring does.
        aalt = json.loads(store.read_file(sha0, "glyphs/A.alt^1.json"))
        for layer in aalt["layers"].values():
            layer["glyph"]["components"] = []
        aring = json.loads(store.read_file(sha0, "glyphs/A.alt^1.json"))
        aring["name"] = "Aring"
        store.commit(
            {
                "glyphs/A.alt^1.json": json.dumps(aalt).encode(),
                "glyphs/Aring^1.json": json.dumps(aring).encode(),
            },
            message="elsewhere",
            author=other,
        )
        await backend.check_external_changes()
        assert await backend.findGlyphsThatUseGlyph("A") == ["Aring"]
        # Elsewhere again: Aring deleted.
        store.commit({"glyphs/Aring^1.json": None}, message="delete", author=other)
        await backend.check_external_changes()
        assert await backend.findGlyphsThatUseGlyph("A") == []
        await backend.aclose()

    run(go())
