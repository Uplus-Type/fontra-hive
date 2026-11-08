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
