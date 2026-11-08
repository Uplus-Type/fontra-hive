"""Feature code of an imported UFO or designspace: an ``include`` names a
file of the upload, of bounded size, checked before Fontra reads anything."""

import os
import pathlib
import plistlib
import zipfile
from types import SimpleNamespace
from unittest import mock

import pytest

from fontra_hive import importer
from fontra_hive.export import FEATURES_REFUSED

GLIF_A = """<?xml version="1.0" encoding="UTF-8"?>
<glyph name="A" format="2">
  <advance width="600"/>
  <unicode hex="0041"/>
  <outline>
    <contour>
      <point x="100" y="0" type="line"/>
      <point x="300" y="700" type="line"/>
      <point x="500" y="0" type="line"/>
    </contour>
  </outline>
</glyph>
"""


def makeUFO(path: pathlib.Path, features: str | None = None) -> pathlib.Path:
    (path / "glyphs").mkdir(parents=True)
    plist = {
        "metainfo.plist": {"creator": "test", "formatVersion": 3},
        "fontinfo.plist": {
            "familyName": "Test",
            "styleName": "Regular",
            "unitsPerEm": 1000,
        },
        "layercontents.plist": [["public.default", "glyphs"]],
        "glyphs/contents.plist": {"A": "A_.glif"},
    }
    for name, value in plist.items():
        (path / name).write_bytes(plistlib.dumps(value))
    (path / "glyphs" / "A_.glif").write_text(GLIF_A)
    if features is not None:
        (path / "features.fea").write_text(features)
    return path


def zipFolder(folder: pathlib.Path, archive: pathlib.Path) -> pathlib.Path:
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(folder.rglob("*")):
            zf.write(path, path.relative_to(folder.parent).as_posix())
    return archive


def importZip(tmp_path, folder, monkeypatch=None):
    work = tmp_path / "work"
    work.mkdir()
    if monkeypatch is not None:

        async def notConverted(source, destination):
            raise AssertionError("converted")

        monkeypatch.setattr(importer, "_convert", notConverted)
        monkeypatch.setattr(
            importer, "readableExtensions", lambda: {"designspace", "fontra", "ufo"}
        )
    archive = zipFolder(folder, tmp_path / "upload")
    return importer.convertToFontra(archive, "MyFont.zip", work), work


def refused(tmp_path, folder, monkeypatch):
    with pytest.raises(importer.ImportError_) as error:
        importZip(tmp_path, folder, monkeypatch)
    assert str(error.value) == FEATURES_REFUSED
    work = tmp_path / "work"
    assert not (work / "converted.fontra").exists()
    return work


def contents(folder: pathlib.Path) -> bytes:
    return b"".join(p.read_bytes() for p in folder.rglob("*") if p.is_file())


# (1)


def test_an_absolute_include_is_refused_before_conversion(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.fea"
    elsewhere.write_text("languagesystem DFLT dflt; # ELSEWHERE-1\n")
    for i, target in enumerate(["/etc/hostname", str(elsewhere)]):
        case = tmp_path / f"case{i}"
        makeUFO(case / "MyFont" / "MyFont.ufo", f"include({target});\n")
        work = refused(case, case / "MyFont", monkeypatch)
        assert b"ELSEWHERE-1" not in contents(work)


def test_the_import_route_answers_with_the_refusal_and_creates_nothing(
    tmp_path, monkeypatch
):
    pytest.importorskip("aiohttp")
    from aiohttp import web

    from conftest import run
    from test_import_limits import FakeApi, make_manager

    makeUFO(tmp_path / "MyFont" / "MyFont.ufo", "include(/etc/hostname);\n")
    data = zipFolder(tmp_path / "MyFont", tmp_path / "upload.zip").read_bytes()

    class Part:
        name = "file"
        filename = "MyFont.zip"

        async def read_chunk(self, size):
            nonlocal data
            chunk, data = data[:size], data[size:]
            return chunk

    class Reader:
        part = Part()

        async def next(self):
            part, self.part = self.part, None
            return part

    async def notConverted(source, destination):
        raise AssertionError("converted")

    monkeypatch.setattr(importer, "_convert", notConverted)
    manager = make_manager(tmp_path, FakeApi())
    access = run(manager._projectAccess(None, "ana/AnaSans"))
    request = SimpleNamespace(multipart=mock.AsyncMock(return_value=Reader()))
    with pytest.raises(web.HTTPUnprocessableEntity) as answer:
        run(manager._import(request, access))
    assert answer.value.text == FEATURES_REFUSED
    assert not manager._repoPathFor(access).exists()
    assert list((tmp_path / "repos").iterdir()) == []


def test_every_include_that_fontra_lists_is_checked(tmp_path, monkeypatch):
    makeUFO(
        tmp_path / "MyFont" / "MyFont.ufo",
        'featureNames { name "x\ninclude(/etc/hostname);\n"; };\n',
    )
    refused(tmp_path, tmp_path / "MyFont", monkeypatch)


def test_an_include_in_a_comment_is_not_refused(tmp_path):
    ufo = makeUFO(tmp_path / "MyFont.ufo", "# include(/etc/hostname);\n")
    importer.check_features(tmp_path, ufo)


# (2)


def test_an_include_that_stays_in_the_upload_is_converted(tmp_path):
    pytest.importorskip("fontra.backends.designspace")
    ufo = makeUFO(
        tmp_path / "MyFont" / "MyFont.ufo",
        "include(MyFont.ufo/kern.fea);\ninclude(shared.fea);\n",
    )
    (ufo / "kern.fea").write_text("feature kern {\n    pos A A -10;\n} kern;\n")
    shared = "feature liga {\n    sub A A by A;\n} liga;\n"
    (ufo.parent / "shared.fea").write_text(shared)
    font, work = importZip(tmp_path, tmp_path / "MyFont")
    features = (font / "features.txt").read_text()
    assert "pos A A -10;" in features and "sub A A by A;" in features
    assert any(p.name.startswith("A") for p in (font / "glyphs").iterdir())


def test_includes_that_stay_in_the_upload_pass_the_check(tmp_path):
    ufo = makeUFO(
        tmp_path / "MyFont.ufo",
        "include(MyFont.ufo/kern.fea);\ninclude(shared.fea);\ninclude(missing.fea);\n",
    )
    (ufo / "kern.fea").write_text("include(MyFont.ufo/more.fea);\n")
    (ufo / "more.fea").write_text("feature kern { pos A A -10; } kern;\n")
    (tmp_path / "shared.fea").write_text("feature liga { sub A A by A; } liga;\n")
    importer.check_features(tmp_path, ufo)


def test_a_ufo_without_features_passes_the_check(tmp_path):
    importer.check_features(tmp_path, makeUFO(tmp_path / "MyFont.ufo"))


# (3)


def test_an_endless_or_huge_included_file_is_refused_unread(tmp_path, monkeypatch):
    def unread(file, mode="r", *args, **kwargs):
        assert pathlib.Path(file).name != "huge.fea" or "w" in mode, "read"
        return open(file, mode, *args, **kwargs)

    case = tmp_path / "zero"
    makeUFO(case / "MyFont" / "MyFont.ufo", "include(/dev/zero);\n")
    refused(case, case / "MyFont", monkeypatch)

    case = tmp_path / "huge"
    ufo = makeUFO(case / "MyFont" / "MyFont.ufo", "include(huge.fea);\n")
    with open(ufo.parent / "huge.fea", "wb") as f:
        f.truncate(importer.MAX_INCLUDED + 1)
    monkeypatch.setattr(importer, "open", unread, raising=False)
    refused(case, case / "MyFont", monkeypatch)


def test_links_and_pipes_to_endless_files_are_refused(tmp_path):
    ufo = makeUFO(tmp_path / "MyFont.ufo", "include(zero.fea);\n")
    os.symlink("/dev/zero", tmp_path / "zero.fea")
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(tmp_path, ufo)

    other = tmp_path / "other"
    ufo = makeUFO(other / "MyFont.ufo", "include(pipe.fea);\n")
    os.mkfifo(other / "pipe.fea")
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(other, ufo)


def test_included_files_are_bounded_together_and_in_depth(tmp_path):
    third = importer.MAX_INCLUDED // 3 + 1
    ufo = makeUFO(
        tmp_path / "MyFont.ufo", "include(a.fea);\ninclude(b.fea);\ninclude(c.fea);\n"
    )
    for name in "abc":
        (tmp_path / f"{name}.fea").write_text("#" * third)
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(tmp_path, ufo)

    for depth, accepted in [(importer.MAX_INCLUDE_DEPTH, True), (8, False)]:
        folder = tmp_path / f"depth{depth}"
        ufo = makeUFO(folder / "MyFont.ufo", "include(f1.fea);\n")
        for i in range(1, depth + 1):
            (folder / f"f{i}.fea").write_text(
                f"include(f{i + 1}.fea);\n" if i < depth else "# end\n"
            )
        if accepted:
            importer.check_features(folder, ufo)
        else:
            with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
                importer.check_features(folder, ufo)

    # A file that includes itself, or two that include each other: Fontra
    # would read them again and again, so the check refuses them.
    folder = tmp_path / "loop"
    ufo = makeUFO(folder / "MyFont.ufo", "include(loop.fea);\n")
    (folder / "loop.fea").write_text("include(loop.fea);\n")
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(folder, ufo)
    folder = tmp_path / "mutual"
    ufo = makeUFO(folder / "MyFont.ufo", "include(a.fea);\n")
    (folder / "a.fea").write_text("include(b.fea);\n")
    (folder / "b.fea").write_text("include(a.fea);\n")
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(folder, ufo)
    # The UFO's own features.fea counts in the budget of bytes read.
    folder = tmp_path / "bighead"
    ufo = makeUFO(folder / "MyFont.ufo", "#" * (importer.MAX_INCLUDED + 1) + "\n")
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(folder, ufo)


def test_a_family_s_shared_features_fea_passes_the_check(tmp_path):
    """Each UFO of a family says ``include(features.fea);`` in its own
    ``features.fea``: feaLib reads the family's file next to the UFOs. The
    check looks in the UFO too, where that name is the including file."""
    folder = tmp_path / "Family"
    folder.mkdir()
    (folder / "features.fea").write_text("feature liga { sub A A by A; } liga;\n")
    for style in ("Light", "Bold"):
        makeUFO(folder / f"Family-{style}.ufo", "include(features.fea);\n")
    importer.check_features(folder, folder / "Family-Bold.ufo")


# (4)


def test_an_include_that_leaves_the_upload_is_refused(tmp_path, monkeypatch):
    for i, include in enumerate(["../outside.fea", "MyFont.ufo/../../outside.fea"]):
        case = tmp_path / f"case{i}"
        (case / "outside.fea").parent.mkdir(parents=True)
        (case / "outside.fea").write_text("languagesystem DFLT dflt; # OUTSIDE-4\n")
        makeUFO(case / "MyFont" / "MyFont.ufo", f"include({include});\n")
        work = refused(case, case / "MyFont", monkeypatch)
        assert b"OUTSIDE-4" not in contents(work)


def test_an_include_through_a_link_that_leaves_the_upload_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.fea").write_text("languagesystem DFLT dflt;\n")
    upload = tmp_path / "upload"

    ufo = makeUFO(upload / "MyFont.ufo", "include(x.fea);\n")
    os.symlink(outside / "x.fea", upload / "x.fea")
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(upload, ufo)

    (upload / "x.fea").unlink()
    (ufo / "features.fea").write_text("include(lib/x.fea);\n")
    os.symlink(outside, upload / "lib")
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(upload, ufo)

    (ufo / "features.fea").unlink()
    os.symlink(outside / "x.fea", ufo / "features.fea")
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(upload, ufo)


def test_a_designspace_is_checked_with_its_ufos(tmp_path):
    def designspace(folder, ufoPath):
        doc = folder / "Family.designspace"
        doc.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<designspace format="5.0">\n'
            '<axes><axis tag="wght" name="Weight" minimum="100" maximum="900"'
            ' default="400"/></axes>\n<sources>\n'
            f'<source filename="{ufoPath}"><location>'
            '<dimension name="Weight" xvalue="400"/></location></source>\n'
            "</sources>\n</designspace>\n"
        )
        return doc

    folder = tmp_path / "Family"
    makeUFO(folder / "masters" / "Regular.ufo", "include(/etc/hostname);\n")
    doc = designspace(folder, "masters/Regular.ufo")
    with pytest.raises(importer.ImportError_, match=FEATURES_REFUSED):
        importer.check_features(folder, doc)

    (folder / "masters" / "Regular.ufo" / "features.fea").write_text(
        "include(shared.fea);\n"
    )
    (folder / "masters" / "shared.fea").write_text("# shared\n")
    importer.check_features(folder, doc)

    makeUFO(tmp_path / "Elsewhere.ufo")
    doc = designspace(folder, "../Elsewhere.ufo")
    with pytest.raises(importer.ImportError_, match="UFOs that come with it"):
        importer.check_features(folder, doc)
