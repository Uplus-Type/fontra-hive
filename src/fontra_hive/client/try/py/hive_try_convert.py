"""Fontra Hive, "Try Fontra": conversions run in the browser (Pyodide).

Called by try-python-worker.js. The same code as Hive's server: an upload is
read by Fontra's backends and written as a .fontra package
(``fontra_hive.importer``); a download as designspace + UFOs is written by
``fontra_hive.export``. Files live in Pyodide's memory file system.

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import itertools
import pathlib
import shutil

from fontra_hive import export, importer

WORK = pathlib.Path("/tmp/hive-try")
_counter = itertools.count()


def _workDir() -> pathlib.Path:
    work = WORK / str(next(_counter))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    return work


def readableExtensions() -> list[str]:
    return sorted(importer.readableExtensions())


async def toFontra(upload: str, filename: str) -> str:
    """The upload (a .zip, or a single font file) as a .fontra package; its
    path. As importer.convertToFontra, awaited instead of asyncio.run."""
    upload = pathlib.Path(upload)
    work = _workDir()
    extensions = importer.readableExtensions()
    suffix = pathlib.PurePath(filename).suffix.lstrip(".").lower()
    unpacked = work / "unpacked"
    unpacked.mkdir()
    if suffix == "zip":
        importer.unzip(upload, unpacked)
    elif suffix in extensions:
        shutil.copyfile(upload, unpacked / pathlib.PurePath(filename).name)
    else:
        raise importer.ImportError_(f"Cannot read .{suffix} files here.")
    source = importer.findFont(unpacked, extensions)
    if source.suffix.lower() == ".fontra":
        return str(source)
    destination = work / "converted.fontra"
    try:
        await importer._convert(source, destination)
    except importer.ImportError_:
        raise
    except Exception as error:
        raise importer.ImportError_(f"Could not read {source.name}: {error}")
    return str(destination)


def sourceName(upload: str, filename: str) -> str:
    """The font's name from what was opened, for a font without a family name."""
    return pathlib.PurePath(filename).stem


async def toDesignspaceZip(package: str, stem: str) -> str:
    """A .fontra package as a .zip of a .designspace and its UFOs; its path."""
    work = _workDir()
    result = await export.build(pathlib.Path(package), work, stem, "designspace")
    return str(result)


def fontcSources(archive: str, stem: str) -> dict:
    """A designspace + UFOs .zip (toDesignspaceZip) made ready for fontc,
    which compiles one variable font per designspace and does not take
    discrete axes (italic as 0/1, say): one designspace per combination of
    their values, next to the same UFOs, as fontmake does. A font without
    axes is handed over as its UFO alone.
    Returns {"zip": path, "parts": [[source file, font file stem]]}."""
    import zipfile

    from fontTools.designspaceLib import DesignSpaceDocument
    from fontTools.designspaceLib.split import splitInterpolable

    work = _workDir()
    folder = work / "sources"
    with zipfile.ZipFile(archive) as z:
        z.extractall(folder)
    main = next(folder.glob("*.designspace"), None)
    if main is None:
        # The sources of a font without axes are its UFO alone
        # (export.build, "<name>-ufo.zip"): what fontc takes.
        ufos = sorted(folder.glob("*.ufo"))
        if len(ufos) != 1:
            raise ValueError("No designspace, and not a single UFO, in the sources.")
        parts = [[ufos[0].name, stem]]
        return {"zip": str(_zipFolder(folder, work)), "parts": parts}
    doc = DesignSpaceDocument.fromfile(main)
    discrete = [axis for axis in doc.axes if getattr(axis, "values", None)]
    parts = []
    if export.has_one_source_without_axes(doc):
        # A font without axes: fontc 1.0.0 refuses its designspace (a source
        # without <location>, "missing field `location`"), but takes the UFO
        # on its own and makes the same static font of it.
        parts.append([doc.sources[0].filename, stem])
        main.unlink()
    elif not discrete:
        export.keep_rules_with_glyphs(doc, folder)
        doc.write(main)
        parts.append([main.name, stem])
    else:
        for location, sub in splitInterpolable(doc):
            export.keep_rules_with_glyphs(sub, folder)
            names = []
            for axis in discrete:
                value = location.get(axis.name)
                label = next(
                    (lb.name for lb in axis.axisLabels if lb.userValue == value), None
                )
                names.append(label or f"{axis.tag}{value:g}")
            suffix = "-".join(names)
            name = f"{main.stem}-{suffix}.designspace"
            sub.write(folder / name)
            parts.append([name, f"{stem}-{suffix}"])
        main.unlink()
    return {"zip": str(_zipFolder(folder, work)), "parts": parts}


def _zipFolder(folder, work):
    import zipfile

    result = work / "fontc-sources.zip"
    with zipfile.ZipFile(result, "w", zipfile.ZIP_STORED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(folder).as_posix())
    return result
