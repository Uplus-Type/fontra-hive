import zipfile

from conftest import run
from fontra_hive import export


def test_formats_without_a_font_compiler():
    formats = export.supported_formats()
    assert formats[:2] == ["fontra", "designspace"]
    assert ("otf" in formats) == export.can_build_fonts()
    assert export.download_name("Mutator", "designspace") == "Mutator.designspace.zip"
    assert export.download_name("Mutator", "otf") == "Mutator.otf"


def test_fontra_package_zipped(tmp_path, fixture_fontra):
    result = run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "fontra"))
    assert result.name == "Mutator.fontra.zip"
    names = zipfile.ZipFile(result).namelist()
    assert "Mutator.fontra/font-data.json" in names
    assert "Mutator.fontra/glyphs/A^1.json" in names


def test_designspace_with_its_ufos(tmp_path, fixture_fontra):
    result = run(
        export.build(fixture_fontra, tmp_path / "work", "Mutator", "designspace")
    )
    names = zipfile.ZipFile(result).namelist()
    assert "Mutator.designspace" in names
    ufos = {n.split("/")[0] for n in names if ".ufo/" in n}
    assert ufos and all(u.endswith(".ufo") for u in ufos)
    assert any(n.endswith("/glyphs/A_.glif") for n in names)
