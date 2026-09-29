"""Downloading a project: its sources, or fonts built on the server.

Sources are always available: the ``.fontra`` package as stored in git, or a
``.designspace`` with its UFOs (converted by Fontra's ``copyFont``), each as a
``.zip``. Fonts (``.otf``, ``.ttf``, ``.woff2``) are built like Fontra Pak
builds them (the ``compile-fontmake`` workflow of fontra-compile), only when
fontra-compile and fontmake are installed on the server.

A build runs in its own process (``python -m fontra_hive.export``): a large
font can take minutes and a lot of memory, and must not stall the server.

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import shutil
import sys
import zipfile
from contextlib import aclosing

SOURCE_FORMATS = {
    # format: (label, extension of what is built, zipped)
    "fontra": ("Fontra package (.fontra, zipped)", ".fontra", True),
    "designspace": ("Designspace + UFOs (zipped)", ".designspace", True),
}
FONT_FORMATS = {
    "otf": ("OpenType (.otf)", ".otf", False),
    "ttf": ("TrueType (.ttf)", ".ttf", False),
    "woff2": ("Webfont (.woff2)", ".woff2", False),
}


def can_build_fonts() -> bool:
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("fontra_compile", "fontmake")
    )


def supported_formats() -> list[str]:
    formats = list(SOURCE_FORMATS)
    if can_build_fonts():
        formats += list(FONT_FORMATS)
    return formats


def download_name(stem: str, format: str) -> str:
    label, extension, zipped = {**SOURCE_FORMATS, **FONT_FORMATS}[format]
    return stem + extension + (".zip" if zipped else "")


async def _copy(source: pathlib.Path, destination: pathlib.Path) -> None:
    from fontra.backends import getFileSystemBackend, newFileSystemBackend
    from fontra.backends.copy import copyFont

    sourceBackend = getFileSystemBackend(source)
    destBackend = newFileSystemBackend(destination)
    async with aclosing(sourceBackend), aclosing(destBackend):
        await copyFont(sourceBackend, destBackend)


async def _compile(source: pathlib.Path, destination: pathlib.Path) -> None:
    """As Fontra Pak does: discrete axes dropped (the default is built),
    variable composites decomposed, anchors propagated, then fontmake."""
    from fontra.backends import getFileSystemBackend
    from fontra.core.classes import DiscreteFontAxis
    from fontra.workflow.workflow import Workflow

    sourceBackend = getFileSystemBackend(source)
    axes = await sourceBackend.getAxes()
    discrete = [a.name for a in axes.axes if isinstance(a, DiscreteFontAxis)]
    steps = [dict(filter="subset-axes", dropAxisNames=discrete)] if discrete else []
    steps += [
        dict(filter="decompose-composites", onlyVariableComposites=True),
        dict(filter="propagate-anchors"),
        dict(filter="drop-unreachable-glyphs"),
        dict(
            output="compile-fontmake",
            destination=destination.name,
            options={"overlaps-backend": "pathops"},
        ),
    ]
    workflow = Workflow(config=dict(steps=steps), parentDir=source.parent)
    async with workflow.endPoints(sourceBackend) as endPoints:
        for output in endPoints.outputs:
            await output.process(destination.parent, continueOnError=False)


def _zip(folder: pathlib.Path, destination: pathlib.Path, *extra: pathlib.Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for root in (folder, *extra):
            if root.is_file():
                archive.write(root, root.name)
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root.parent).as_posix())


async def build(
    source: pathlib.Path, work: pathlib.Path, stem: str, format: str
) -> pathlib.Path:
    """``source``: a ``.fontra`` folder. Returns the file to download, in ``work``."""
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    result = work / download_name(stem, format)
    if format == "fontra":
        package = out / f"{stem}.fontra"
        shutil.copytree(source, package)
        _zip(package, result)
    elif format == "designspace":
        designspace = out / f"{stem}.designspace"
        await _copy(source, designspace)
        ufos = sorted(out.glob("*.ufo"))
        _zip(designspace, result, *ufos)
    elif format in ("otf", "ttf"):
        await _compile(source, out / f"{stem}.{format}")
        shutil.move(out / f"{stem}.{format}", result)
    elif format == "woff2":
        from fontTools.ttLib import woff2

        ttf = out / f"{stem}.ttf"
        await _compile(source, ttf)
        woff2.compress(str(ttf), str(result))
    else:
        raise ValueError(f"unknown format {format!r}")
    return result


def main(argv: list[str]) -> int:
    source, work, stem, format = argv
    result = asyncio.run(build(pathlib.Path(source), pathlib.Path(work), stem, format))
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
