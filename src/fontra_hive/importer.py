"""Import an uploaded font into a project: any format Fontra reads, converted
to a ``.fontra`` package, then committed.

A font that is a folder (``.ufo``, ``.fontra``, a ``.designspace`` with its
UFOs, ``.glyphspackage``…) comes as a ``.zip``; a single-file font (``.ttf``,
``.otf``, ``.woff2``, ``.glyphs``…) comes as is. Zips are unpacked with
limits (size, file count, no path outside the folder, no links).

The feature code of the UFOs is checked before Fontra reads it
(:func:`check_features`): an ``include`` can only name a file of the upload,
of bounded size.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import pathlib
import re
import shutil
import stat
import string
import zipfile
from contextlib import aclosing
from importlib.metadata import entry_points

from .export import (
    FEATURES_REFUSED,
    FeaturesRefused,
    _included_paths,
    _stays_in_folder,
    check_feature_includes,
)
from .fontracompat import guard_feature_reading

# Fontra reading OpenType features with an outside program (sr-aef): best
# effort, in Try (no program in the browser) and on the server.
guard_feature_reading()

logger = logging.getLogger(__name__)

MAX_UPLOAD = 300 * 1024 * 1024  # bytes, as sent
MAX_UNZIPPED = 1024 * 1024 * 1024  # bytes, once unpacked
MAX_FILES = 500_000  # a pan-CJK family: several UFOs of 50 000 glyphs
# Folder formats, most complete first: a designspace brings its UFOs along.
FOLDER_PREFERENCE = ["fontra", "designspace", "glyphspackage", "ufo"]
# Feature files that a UFO's feature code includes: bytes read, all files
# together, per UFO; and how deep includes may nest (feaLib stops at 5).
MAX_INCLUDED = 5 * 1024 * 1024
MAX_INCLUDE_DEPTH = 5
MIN_INCLUDED_COST = 256  # bytes taken off for each file read, even empty
DESIGNSPACE_REFUSED = "A .designspace can only use the UFOs that come with it."


class ImportError_(ValueError):
    """The upload cannot be imported (the message says why, for people)."""


def readableExtensions() -> set[str]:
    return {ep.name.lower() for ep in entry_points(group="fontra.filesystem.backends")}


def _skipped(name: str) -> bool:
    """What the Finder adds to a .zip and an import leaves out, uncounted."""
    return name.startswith("__MACOSX/") or pathlib.PurePosixPath(name).name == ".DS_Store"


def unzip(archive: pathlib.Path, destination: pathlib.Path) -> None:
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise ImportError_("This .zip file is damaged.")
    with zf:
        infos = [i for i in zf.infolist() if not _skipped(i.filename)]
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


# How Fontra lists included feature files: each line that matches, parsed
# on its own.
_LINE_INCLUDE = re.compile(r"include\s*\(([^)]+)\)")


def _fontra_include_names(features: str) -> set[str]:
    from fontTools.feaLib.ast import IncludeStatement
    from fontTools.feaLib.parser import Parser

    names = set()
    for match in _LINE_INCLUDE.finditer(features):
        start = features.rfind("\n", 0, match.start()) + 1
        end = features.find("\n", match.end())
        line = features[start : end if end != -1 else len(features)]
        try:
            statements = Parser(io.StringIO(line), followIncludes=False).parse()
        except Exception:
            names.add(match[1])  # not parsed: taken as a file name
            continue
        names.update(
            st.filename
            for st in statements.statements
            if isinstance(st, IncludeStatement)
        )
    return names


def _include_names(features: str) -> set[str]:
    """Every file name that an ``include`` of ``features`` may open, read as
    feaLib reads it and as Fontra lists included files. Raises
    :class:`FeaturesRefused` for a name that leaves the folder."""
    check_feature_includes(features)
    names = set(_included_paths(features)) | _fontra_include_names(features)
    if not all(_stays_in_folder(name) for name in names):
        raise FeaturesRefused(FEATURES_REFUSED)
    return names


def _inside(path: pathlib.Path, root: pathlib.Path) -> pathlib.Path:
    """The real path of ``path`` (links followed), if it is in ``root``."""
    try:
        real = pathlib.Path(os.path.realpath(path))
    except (OSError, ValueError):
        raise FeaturesRefused(FEATURES_REFUSED)
    if real != root and root not in real.parents:
        raise FeaturesRefused(FEATURES_REFUSED)
    return real


def _read_feature_file(
    path: pathlib.Path, root: pathlib.Path, budget: list[int] | None
) -> str:
    """The text of a feature file of the upload, at most ``budget[0]`` bytes
    (taken off the budget), or the whole file if ``budget`` is None."""
    real = _inside(path, root)
    try:
        info = os.stat(real)
        if not stat.S_ISREG(info.st_mode):
            raise FeaturesRefused(FEATURES_REFUSED)
        if budget is None:
            data = real.read_bytes()
        else:
            if info.st_size > budget[0]:
                raise FeaturesRefused(FEATURES_REFUSED)
            with open(real, "rb") as f:
                data = f.read(budget[0] + 1)
            if len(data) > budget[0]:
                raise FeaturesRefused(FEATURES_REFUSED)
            budget[0] -= max(len(data), MIN_INCLUDED_COST)
    except OSError:
        raise FeaturesRefused(FEATURES_REFUSED)
    return data.decode("utf-8", "replace")


def _check_included(
    features: str,
    folders: list[pathlib.Path],
    parent: pathlib.Path | None,
    root: pathlib.Path,
    budget: list[int],
    depth: int,
    chain: frozenset[pathlib.Path] = frozenset(),
) -> None:
    """Check the files that ``features`` includes, and theirs in turn. A name
    is looked for in ``folders`` (where feaLib and Fontra start from) and in
    ``parent``, the folder of the including file (where Fontra also looks).
    ``chain``: the real paths of the files being read, from the UFO's own
    ``features.fea`` down to ``features``: a file already on it is not read
    again. A family keeps one ``features.fea`` next to its UFOs, and each
    UFO's ``features.fea`` says ``include(features.fea);``: looked for in
    the UFO too, that name is the including file itself, not a loop."""
    names = _include_names(features)
    if not names:
        return
    if depth >= MAX_INCLUDE_DEPTH:
        raise FeaturesRefused(FEATURES_REFUSED)
    lookIn = [*folders, parent] if parent is not None else folders
    for name in sorted(names):
        found = []
        for spelling in {name, name.strip(string.whitespace + "\"'")}:
            for folder in lookIn:
                path = folder / spelling
                if os.path.lexists(path):
                    found.append(_inside(path, root))
        if found and all(included in chain for included in found):
            # Every file this name can mean is already open on the chain: a
            # loop, which Fontra would follow again and again.
            raise FeaturesRefused(FEATURES_REFUSED)
        for included in found:
            if included in chain:
                continue  # the including file itself (a family's shared name)
            text = _read_feature_file(included, root, budget)
            _check_included(
                text,
                folders,
                included.parent,
                root,
                budget,
                depth + 1,
                chain | {included},
            )


def _designspace_ufos(
    designspace: pathlib.Path, root: pathlib.Path
) -> list[pathlib.Path]:
    from fontTools.designspaceLib import DesignSpaceDocument

    try:
        document = DesignSpaceDocument.fromfile(designspace)
    except Exception:
        raise ImportError_(f"Could not read {designspace.name}.")
    ufos = []
    for source in document.sources:
        if source.path is None:
            continue
        try:
            ufos.append(_inside(pathlib.Path(source.path), root))
        except FeaturesRefused:
            raise ImportError_(DESIGNSPACE_REFUSED)
    return ufos


def check_features(unpacked: pathlib.Path, source: pathlib.Path) -> None:
    """Raise :class:`ImportError_` unless the feature code of every UFO of the
    upload (and of the designspace being imported) only includes files of
    the upload, of bounded size. Nothing is converted before this."""
    root = pathlib.Path(os.path.realpath(unpacked))
    ufos = {
        pathlib.Path(os.path.realpath(path))
        for path in root.rglob("*")
        if path.suffix.lower() == ".ufo" and path.is_dir()
    }
    folders = []
    if source.suffix.lower() == ".designspace":
        ufos.update(_designspace_ufos(source, root))
        folders.append(pathlib.Path(os.path.realpath(source)).parent)
    for ufo in sorted(ufos):
        features = ufo / "features.fea"
        if not os.path.lexists(features):
            continue
        try:
            budget = [MAX_INCLUDED]  # the UFO's own features.fea counts too
            text = _read_feature_file(features, root, budget)
            _check_included(
                text,
                [ufo.parent, ufo, *folders],
                None,
                root,
                budget,
                0,
                frozenset({_inside(features, root)}),
            )
        except FeaturesRefused:
            logger.warning("import refused: feature code of %s", ufo.name)
            raise ImportError_(FEATURES_REFUSED)


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
    if suffix == "designspace":
        raise ImportError_(
            "A .designspace needs its UFOs: upload a .zip of the folder that holds "
            "the .designspace and its UFOs."
        )
    if suffix == "zip":
        unzip(upload, unpacked)
    elif suffix in extensions:
        shutil.copyfile(upload, unpacked / pathlib.PurePath(filename).name)
    else:
        raise ImportError_(f"Hive cannot read .{suffix} files.")
    source = findFont(unpacked, extensions)
    if source.suffix.lower() == ".fontra":
        return source  # already the right format
    check_features(unpacked, source)
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
