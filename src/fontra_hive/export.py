"""Downloading a project: its sources, or fonts built on the server.

Sources are always available: the ``.fontra`` package as stored in git, or a
``.designspace`` with its UFOs (converted by Fontra's ``copyFont``), each as a
``.zip``. Fonts (``.otf``,
``.ttf``, ``.woff2``) are built only when fontra-compile and fontmake are
installed on the server: TrueType and WOFF2 by fontc when it is installed,
with fontmake (as Fontra Pak builds, the ``compile-fontmake`` workflow of
fontra-compile) when fontc is missing or stops on the font; OpenType (CFF) by
fontmake, which fontc cannot write.

A build runs in its own process, the recipe runner's: a large font can take
minutes and a lot of memory, and must not stall the server. The Download
panel is the built-in recipe "download" (:func:`fontra_hive.runner.downloadRequest`),
and the ``hive-artifact`` action builds with :func:`build`.

What a failed build tells the browser is :func:`failure_message`; its log stays
on the server.

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import os
import pathlib
import plistlib
import re
import shutil
import string
import sys
import time
import zipfile
from contextlib import aclosing

logger = logging.getLogger(__name__)

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


# What fontc printed, kept at most: the end is where it says why it stopped.
FONTC_TAIL_CHARACTERS = 2000
# All fontc sees of the environment (it needs no network, no secret).
_FONTC_ENVIRONMENT = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")

EXPORT_FAILED = "The export failed."
EXPORT_TOO_LONG = "The export took too long."
COMPILER_STOPPED = "The font compiler was stopped."
FEATURES_REFUSED = "Feature code can only include files from the project."

_INCLUDE = r"include(?:\s|#[^\r\n]*)*\(([^)]*)"
_ANY_INCLUDE = re.compile(_INCLUDE, re.IGNORECASE)
# Comments and strings, read as feaLib reads them, or an include.
_FEATURE_TOKEN = re.compile(r'#[^\r\n]*|"[^"]*"?|' + _INCLUDE, re.IGNORECASE)
_DRIVE = re.compile(r"[A-Za-z]:")


class FeaturesRefused(Exception):
    """Feature code that this server does not compile."""


class FontcFailed(Exception):
    """fontc did not make the font: it stopped (``returncode`` is its exit
    code, 101 for a Rust panic, negative when a signal ended it), made no
    file, or did not start (``returncode`` is None). ``stdout`` and ``stderr``
    are the last characters of what it printed (its errors go to stdout, a
    panic to stderr), without the server's paths."""

    def __init__(self, returncode: int | None, stdout: str = "", stderr: str = ""):
        super().__init__(f"fontc failed (exit code {returncode})")
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def tail(self) -> str:
        """The end of both, together at most FONTC_TAIL_CHARACTERS long (half
        each when both have something to say)."""
        parts = [part for part in (self.stdout, self.stderr) if part]
        limit = FONTC_TAIL_CHARACTERS // max(len(parts), 1)
        return "\n".join(part[-limit:] for part in parts)


def _included_paths(features: str) -> list[str]:
    outside = _FEATURE_TOKEN.sub("", features)
    if "anon" in outside.lower():
        # An anonymous block is free text, so "#" and quotes around it mean
        # nothing certain: every include of the text counts.
        return _ANY_INCLUDE.findall(features)
    return [
        match[1]
        for match in _FEATURE_TOKEN.finditer(features)
        if match[1] is not None
    ]


def _stays_in_folder(path: str) -> bool:
    path = path.strip(string.whitespace + "\"'")
    return not (
        path.startswith(("/", "\\", "~")) or ".." in path or _DRIVE.match(path)
    )


def check_feature_includes(features: str) -> None:
    """Raise :class:`FeaturesRefused` unless every ``include`` of this feature
    code stays in the folder it is compiled in."""
    if not all(_stays_in_folder(path) for path in _included_paths(features)):
        raise FeaturesRefused(FEATURES_REFUSED)


def failure_message(result) -> str:
    """What the browser is told about a build that did not succeed, from its
    :class:`fontra_hive.runner.RunResult`: one of our sentences, never its log."""
    from . import runner

    if result.reason == runner.REFUSED_FEATURES:
        return FEATURES_REFUSED
    if result.state == runner.TIMEOUT:
        return EXPORT_TOO_LONG
    if result.reason == runner.STOPPED:
        return COMPILER_STOPPED
    return EXPORT_FAILED


def can_build_fonts() -> bool:
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("fontra_compile", "fontmake")
    )


def can_run_fontc() -> pathlib.Path | None:
    """The ``fontc`` program that pip installed beside the interpreter, else
    the one on the PATH; None when there is none."""
    beside = pathlib.Path(sys.executable).with_name("fontc")
    if beside.is_file() and os.access(beside, os.X_OK):
        return beside
    found = shutil.which("fontc")
    return pathlib.Path(found) if found else None


def supported_formats() -> list[str]:
    formats = list(SOURCE_FORMATS)
    if can_build_fonts():
        formats += list(FONT_FORMATS)
    return formats


def download_name(stem: str, format: str) -> str:
    label, extension, zipped = {**SOURCE_FORMATS, **FONT_FORMATS}[format]
    return stem + extension + (".zip" if zipped else "")


def safe_stem(name: str) -> str:
    """A file name without extension, whatever ``name`` is."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return stem or "font"


async def _copy(source: pathlib.Path, destination: pathlib.Path) -> None:
    from fontra.backends import getFileSystemBackend, newFileSystemBackend
    from fontra.backends.copy import copyFont

    sourceBackend = getFileSystemBackend(source)
    destBackend = newFileSystemBackend(destination)
    async with aclosing(sourceBackend), aclosing(destBackend):
        await copyFont(sourceBackend, destBackend)


async def _compile(source: pathlib.Path, destination: pathlib.Path) -> None:
    """As Fontra Pak does: discrete axes dropped (the default is built),
    variable composites decomposed, anchors propagated, then fontmake. The
    feature code is checked first; fontmake works in a folder of its own, where
    its includes are looked for."""
    from fontra.backends import getFileSystemBackend
    from fontra.core.classes import DiscreteFontAxis
    from fontra.workflow.workflow import Workflow

    source = source.resolve()
    destination = destination.resolve()
    sourceBackend = getFileSystemBackend(source)
    check_feature_includes((await sourceBackend.getFeatures()).text)
    folder = destination.parent / "compile"
    folder.mkdir(exist_ok=True)
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
            ufoTempDir=os.fspath(folder),
            options={"overlaps-backend": "pathops"},
        ),
    ]
    workflow = Workflow(config=dict(steps=steps), parentDir=source.parent)
    with contextlib.chdir(folder):
        async with workflow.endPoints(sourceBackend) as endPoints:
            for output in endPoints.outputs:
                await output.process(destination.parent, continueOnError=False)


def keep_rules_with_glyphs(doc, folder) -> None:
    """Rules (glyph substitutions) of a designspace whose glyphs are all in
    the font (the UFOs are in ``folder``): fontc stops on one that names a
    glyph the font does not have (an italic without I.narrow, say), where
    fontmake leaves it out."""
    names = set()
    for source in doc.sources:
        contents = folder / source.filename / "glyphs" / "contents.plist"
        if contents.is_file():
            names.update(plistlib.loads(contents.read_bytes()))
    if not names:
        return
    for rule in list(doc.rules):
        rule.subs = [(a, b) for a, b in rule.subs if a in names and b in names]
        if not rule.subs:
            doc.rules.remove(rule)


def has_one_source_without_axes(doc) -> bool:
    """A font without axes and with a single source: its UFO is all of it.
    (fontc 1.0.0 refuses the designspace of such a font, a source without
    ``<location>``, and takes the UFO alone.)"""
    return not doc.axes and len(doc.sources) == 1


async def _prepare_sources(source: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """The font as fontc reads it, written in ``out``: the filters of
    :func:`_compile` (same steps) and a designspace with its UFOs, rules that
    name a missing glyph left out. Returns what fontc is given: the
    designspace, or the UFO alone for a font without axes and a single source.
    Discrete axes are dropped (the default is built), as in :func:`_compile`.
    The feature code is checked first."""
    from fontTools.designspaceLib import DesignSpaceDocument

    from fontra.backends import getFileSystemBackend
    from fontra.core.classes import DiscreteFontAxis
    from fontra.workflow.workflow import Workflow

    source = source.resolve()
    out.mkdir(parents=True, exist_ok=True)
    out = out.resolve()
    sourceBackend = getFileSystemBackend(source)
    check_feature_includes((await sourceBackend.getFeatures()).text)
    axes = await sourceBackend.getAxes()
    discrete = [a.name for a in axes.axes if isinstance(a, DiscreteFontAxis)]
    steps = [dict(filter="subset-axes", dropAxisNames=discrete)] if discrete else []
    steps += [
        dict(filter="decompose-composites", onlyVariableComposites=True),
        dict(filter="propagate-anchors"),
        dict(filter="drop-unreachable-glyphs"),
        dict(output="fontra-write", destination=f"{source.stem}.designspace"),
    ]
    workflow = Workflow(config=dict(steps=steps), parentDir=source.parent)
    async with workflow.endPoints(sourceBackend) as endPoints:
        for output in endPoints.outputs:
            await output.process(out, continueOnError=False)
    designspace = out / f"{source.stem}.designspace"
    doc = DesignSpaceDocument.fromfile(designspace)
    if has_one_source_without_axes(doc):
        return out / doc.sources[0].filename
    keep_rules_with_glyphs(doc, out)
    doc.write(designspace)
    return designspace


def _without_paths(text: str, *folders: pathlib.Path) -> str:
    """No path of the server in what is logged or shown."""
    paths = set()
    for folder in folders:
        paths.update((os.fspath(folder), os.fspath(folder.resolve())))
    for path in sorted(paths, key=len, reverse=True):
        text = text.replace(path + os.sep, "").replace(path, "")
    return text


def _tail(data: bytes, *folders: pathlib.Path) -> str:
    text = _without_paths(data.decode("utf-8", "replace"), *folders)
    return text.strip()[-FONTC_TAIL_CHARACTERS:]


async def _compile_fontc(
    source: pathlib.Path,
    destination: pathlib.Path,
    timeout: float | None = None,
) -> None:
    """Run fontc on ``source`` (a designspace, or a UFO) and write
    ``destination``. Anything but a font made raises :class:`FontcFailed`:
    an exit code other than 0 (a Rust panic ends with 101, and is no
    different), no file or an empty one, a program that does not start.
    ``timeout``: seconds; the runner's clock stops a build before that
    anyway, and kills what the build started."""
    fontc = can_run_fontc()
    if fontc is None:
        raise FontcFailed(None, stderr="fontc is not installed")
    source = source.resolve()
    destination = destination.resolve()
    folders = (source.parent, destination.parent, destination.parent.parent)
    environment = {k: os.environ[k] for k in _FONTC_ENVIRONMENT if k in os.environ}
    try:
        process = await asyncio.create_subprocess_exec(
            os.fspath(fontc),
            os.fspath(source),
            "--output-file",
            os.fspath(destination),
            cwd=source.parent,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise FontcFailed(None, stderr=_without_paths(str(error), *folders)) from error
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError as error:
        raise FontcFailed(None, stderr="fontc took too long") from error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    failure = FontcFailed(
        process.returncode, _tail(stdout, *folders), _tail(stderr, *folders)
    )
    if process.returncode != 0:
        raise failure
    if not destination.is_file() or destination.stat().st_size == 0:
        raise failure


async def _build_font(
    source: pathlib.Path, out: pathlib.Path, stem: str, format: str
) -> pathlib.Path:
    """``otf`` or ``ttf`` in ``out`` (``woff2`` starts from the ``ttf``). A
    TrueType font is made by fontc, then by fontmake if fontc cannot; an
    OpenType font by fontmake. One line of the log says which, how long."""
    target = out / f"{stem}.{'otf' if format == 'otf' else 'ttf'}"
    started = time.monotonic()
    engine, fallback = "fontmake", False
    if format != "otf" and can_run_fontc() is not None:
        try:
            sources = await _prepare_sources(source, out / "fontc")
            await _compile_fontc(sources, target)
            engine = "fontc"
        except (FeaturesRefused, MemoryError):
            raise
        except Exception as error:
            fallback = True
            if isinstance(error, FontcFailed):
                detail = f"exit code {error.returncode}\n{error.tail}"
            else:
                detail = f"{type(error).__name__} while preparing the sources"
            logger.warning(
                "fontc could not build %s, fontmake takes over: %s", format, detail
            )
            target.unlink(missing_ok=True)
    if engine == "fontmake":
        await _compile(source, target)
    logger.info(
        "engine=%s format=%s seconds=%.1f fallback=%s",
        engine,
        format,
        time.monotonic() - started,
        "yes" if fallback else "no",
    )
    return target


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
    """``source``: a ``.fontra`` folder. Returns the file to download, in
    ``work``."""
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    if format == "fontra":
        result = work / download_name(stem, format)
        package = out / f"{stem}.fontra"
        shutil.copytree(source, package)
        _zip(package, result)
    elif format == "designspace":
        result = work / download_name(stem, format)
        designspace = out / f"{stem}.designspace"
        await _copy(source, designspace)
        ufos = sorted(out.glob("*.ufo"))
        _zip(designspace, result, *ufos)
    elif format in ("otf", "ttf"):
        result = work / download_name(stem, format)
        built = await _build_font(source, out, stem, format)
        shutil.move(built, result)
    elif format == "woff2":
        from fontTools.ttLib import woff2

        result = work / download_name(stem, format)
        ttf = await _build_font(source, out, stem, format)
        woff2.compress(str(ttf), str(result))
    else:
        raise ValueError(f"unknown format {format!r}")
    return result
