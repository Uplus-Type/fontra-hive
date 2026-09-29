import pathlib
import zipfile

import pytest

from fontra_hive import importer
from fontra_hive.gitstore import GitRepoStore, Signature

ME = Signature("Jérémie Hornus", "jeremie@hive")


def zipFolder(folder: pathlib.Path, archive: pathlib.Path, prefix: str) -> None:
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(folder.rglob("*")):
            zf.write(path, f"{prefix}/{path.relative_to(folder).as_posix()}")
        zf.writestr("__MACOSX/._junk", b"mac")
        zf.writestr(f"{prefix}/.DS_Store", b"mac")


def test_a_zipped_fontra_package_is_taken_as_is(tmp_path, fixture_fontra):
    archive = tmp_path / "upload"
    zipFolder(fixture_fontra, archive, "MyFont/MyFont.fontra")
    work = tmp_path / "work"
    work.mkdir()
    font = importer.convertToFontra(archive, "MyFont.zip", work)
    assert font.name == "MyFont.fontra" and (font / "font-data.json").exists()
    assert (
        not list(work.rglob(".DS_Store"))
        and not (work / "unpacked" / "__MACOSX").exists()
    )
    store = GitRepoStore.create(tmp_path / "p.git")
    head = store.import_directory(font, message="Import MyFont.zip", author=ME)
    tree = store.list_tree(head)
    assert "font-data.json" in tree and any(p.startswith("glyphs/") for p in tree)
    assert not any(".DS_Store" in p or "__MACOSX" in p for p in tree)
    store.close()


def makeTTF(path: pathlib.Path) -> None:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    fb = FontBuilder(1000, isTTF=True)
    glyphs = [".notdef", "A"]
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap({0x41: "A"})
    pen = TTGlyphPen(None)
    pen.moveTo((100, 0))
    pen.lineTo((300, 700))
    pen.lineTo((500, 0))
    pen.closePath()
    triangle = pen.glyph()
    fb.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "A": triangle})
    fb.setupHorizontalMetrics({".notdef": (500, 0), "A": (600, 100)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Test", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    fb.save(path)


def test_a_single_font_file_is_converted(tmp_path):
    ttf = tmp_path / "upload"
    makeTTF(ttf)
    work = tmp_path / "work"
    work.mkdir()
    font = importer.convertToFontra(ttf, "Test-Regular.ttf", work)
    assert font.suffix == ".fontra"
    names = {p.name for p in (font / "glyphs").iterdir()}
    assert any(n.startswith("A") for n in names), names
    assert '"xAdvance": 600' in next((font / "glyphs").glob("A*.json")).read_text()


def test_refusals(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    bad = tmp_path / "evil"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../outside.txt", b"x")
    with pytest.raises(importer.ImportError_, match="outside"):
        importer.convertToFontra(bad, "evil.zip", work)

    work2 = tmp_path / "work2"
    work2.mkdir()
    with pytest.raises(importer.ImportError_, match="cannot read .pdf"):
        importer.convertToFontra(bad, "font.pdf", work2)

    work3 = tmp_path / "work3"
    work3.mkdir()
    nothing = tmp_path / "nothing"
    with zipfile.ZipFile(nothing, "w") as zf:
        zf.writestr("readme.txt", b"hello")
    with pytest.raises(importer.ImportError_, match="No font found"):
        importer.convertToFontra(nothing, "nothing.zip", work3)

    work4 = tmp_path / "work4"
    work4.mkdir()
    (tmp_path / "broken").write_bytes(b"PK not really")
    with pytest.raises(importer.ImportError_, match="damaged"):
        importer.convertToFontra(tmp_path / "broken", "broken.zip", work4)


def test_the_designspace_wins_over_its_ufos(tmp_path):
    (tmp_path / "Family" / "masters" / "Light.ufo").mkdir(parents=True)
    (tmp_path / "Family" / "masters" / "Bold.ufo").mkdir(parents=True)
    (tmp_path / "Family" / "Family.designspace").write_text("<designspace/>")
    extensions = {"designspace", "ufo", "fontra", "ttf"}
    assert importer.findFont(tmp_path, extensions).name == "Family.designspace"
    # A UFO inside a .fontra package is part of that package, not a font.
    (tmp_path / "Other.fontra" / "x.ufo").mkdir(parents=True)
    assert importer.findFont(tmp_path, extensions).name == "Other.fontra"
