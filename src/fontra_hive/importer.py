"""Import an uploaded font into a project: any format Fontra reads, converted
to a ``.fontra`` package, then committed.

A font that is a folder (``.ufo``, ``.fontra``, a ``.designspace`` with its
UFOs, ``.glyphspackage``…) comes as a ``.zip``; a single-file font (``.ttf``,
``.otf``, ``.woff2``, ``.glyphs``…) comes as is. Zips are unpacked with
limits (size, file count, no path outside the folder, no links).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import shutil
import stat
import zipfile
from contextlib import aclosing
from importlib.metadata import entry_points

logger = logging.getLogger(__name__)

MAX_UPLOAD = 300 * 1024 * 1024  # bytes, as sent
MAX_UNZIPPED = 1024 * 1024 * 1024  # bytes, once unpacked
MAX_FILES = 100_000
# Folder formats, most complete first: a designspace brings its UFOs along.
FOLDER_PREFERENCE = ["fontra", "designspace", "glyphspackage", "ufo"]


class ImportError_(ValueError):
    """The upload cannot be imported (the message says why, for people)."""


def readableExtensions() -> set[str]:
    return {ep.name.lower() for ep in entry_points(group="fontra.filesystem.backends")}


def unzip(archive: pathlib.Path, destination: pathlib.Path) -> None:
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise ImportError_("This .zip file is damaged.")
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise ImportError_("Too many files in the .zip.")
        if sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise ImportError_("The .zip is too large once unpacked.")
        root = destination.resolve()
        for info in infos:
            name = info.filename
            if stat.S_ISLNK(info.external_attr >> 16):
                continue  # no symbolic links
            target = (destination / name).resolve()
            if root != target and root not in target.parents:
                raise ImportError_("The .zip contains a path outside of it.")
            if (
                name.startswith("__MACOSX/")
                or pathlib.PurePosixPath(name).name == ".DS_Store"
            ):
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)


def findFont(folder: pathlib.Path, extensions: set[str]) -> pathlib.Path:
    """The font to import in an unpacked upload: the shallowest one, in the
    order of FOLDER_PREFERENCE, else a single-file font."""
    candidates = []
    for path in folder.rglob("*"):
        suffix = path.suffix.lstrip(".").lower()
        if suffix not in extensions or path.name.startswith("."):
            continue
        # Nothing inside another font package (a UFO inside a .fontra…).
        if any(
            p.suffix.lstrip(".").lower() in extensions
            for p in path.relative_to(folder).parents
            if p.name
        ):
            continue
        rank = (
            FOLDER_PREFERENCE.index(suffix)
            if suffix in FOLDER_PREFERENCE
            else len(FOLDER_PREFERENCE)
        )
        candidates.append((rank, len(path.relative_to(folder).parts), str(path), path))
    if not candidates:
        raise ImportError_(
            "No font found. Upload a .zip of a .designspace (with its UFOs), .ufo or "
            ".fontra, or a single font file (" + ", ".join(sorted(extensions)) + ")."
        )
    candidates.sort()
    return candidates[0][3]


async def _convert(source: pathlib.Path, destination: pathlib.Path) -> None:
    from fontra.backends import getFileSystemBackend, newFileSystemBackend
    from fontra.backends.copy import copyFont

    sourceBackend = getFileSystemBackend(source)
    destBackend = newFileSystemBackend(destination)
    async with aclosing(sourceBackend), aclosing(destBackend):
        await copyFont(sourceBackend, destBackend, continueOnError=True)


def convertToFontra(
    upload: pathlib.Path, filename: str, work: pathlib.Path
) -> pathlib.Path:
    """The upload as a .fontra package in ``work`` (blocking: run in a thread)."""
    extensions = readableExtensions()
    suffix = pathlib.PurePath(filename).suffix.lstrip(".").lower()
    unpacked = work / "unpacked"
    unpacked.mkdir()
    if suffix == "zip":
        unzip(upload, unpacked)
    elif suffix in extensions:
        shutil.copyfile(upload, unpacked / pathlib.PurePath(filename).name)
    else:
        raise ImportError_(f"Hive cannot read .{suffix} files.")
    source = findFont(unpacked, extensions)
    if source.suffix.lower() == ".fontra":
        return source  # already the right format
    destination = work / "converted.fontra"
    try:
        asyncio.run(_convert(source, destination))
    except ImportError_:
        raise
    except Exception as error:
        logger.exception("could not convert %s", source.name)
        raise ImportError_(f"Could not read {source.name}: {error}")
    return destination


def newFont(path: pathlib.Path) -> None:
    """A new font to start from, as Fontra Pak makes one: a "Regular" source
    with usual line metrics, and the Google Fonts "Latin Kernel" glyph set
    as the project's glyph set, so that the font overview shows the glyphs
    to draw (each one made by a double-click) instead of an empty page.
    Blocking; safe to call from a running event loop."""
    try:
        from fontra.backends.populate import createNewFontAndPopulate
    except ImportError:  # an older Fontra: an empty font
        from fontra.backends.fontra import FontraBackend

        FontraBackend.createFromPath(path)
        return
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        pool.submit(asyncio.run, createNewFontAndPopulate(path)).result()
