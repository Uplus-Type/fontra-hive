"""Downloading a project: its sources, or fonts built on the server.

Sources are always available: the ``.fontra`` package as stored in git, or a
``.designspace`` with its UFOs (converted by Fontra's ``copyFont``), each as a
``.zip``; a font without axes and with a single source is its UFO alone
(``<name>-ufo.zip``, else ``<name>-designspace.zip``). Fonts (``.otf``,
``.ttf``, ``.woff2``) are built only when fontra-compile and fontmake are
installed on the server: TrueType and WOFF2 by fontc when it is installed
(googlefonts/fontc at the revision of ``tools/build-fontc-wasm.sh``, which
reads the ``.fontra`` package itself), with fontmake (as Fontra Pak builds, the
``compile-fontmake`` workflow of fontra-compile) when fontc is missing or stops
on the font; OpenType (CFF) by fontmake, which fontc cannot write.

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
# Where install.sh puts fontc ($HIVE/fontc/current, set by fontra-hive.service).
FONTC_VARIABLE = "HIVE_FONTC"
# Glyphs that a font says not to export (the UFO convention, in the font's
# custom data): fontc honours it in a designspace, not in a .fontra package.
SKIP_EXPORT_GLYPHS = "public.skipExportGlyphs"
# All fontc sees of the environment (it needs no network, no secret).
_FONTC_ENVIRONMENT = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")

EXPORT_FAILED = "The export failed."
EXPORT_TOO_LONG = "The export took too long."
COMPILER_STOPPED = "The font compiler was stopped."
FEATURES_REFUSED = "Feature code can only include files from the project."
EXPORT_IN_PROGRESS = "You already have an export in progress."

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
    """The ``fontc`` program: ``$HIVE_FONTC`` (install.sh sets it), else
    ``fontc/current`` beside the Python environment (``$HIVE/fontc/current``
    for ``$HIVE/venv``), else one installed beside the interpreter (pip, before
    November 2026), else the one on the PATH; None when there is none."""
    candidates = []
    configured = os.environ.get(FONTC_VARIABLE, "").strip()
    if configured:
        candidates.append(pathlib.Path(configured))
    candidates.append(pathlib.Path(sys.prefix).parent / "fontc" / "current")
    candidates.append(pathlib.Path(sys.executable).with_name("fontc"))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("fontc")
    return pathlib.Path(found) if found else None


def supported_formats() -> list[str]:
    formats = list(SOURCE_FORMATS)
    if can_build_fonts():
        formats += list(FONT_FORMATS)
    return formats


def download_name(stem: str, format: str, *, ufo_only: bool = False) -> str:
    """The file's name. Sources as a designspace are ``<stem>-designspace.zip``
    (macOS unzips ``X.designspace.zip`` to a folder ``X.designspace``), or
    ``<stem>-ufo.zip`` when the font has no axis and a single source (``ufo_only``,
    known once the sources are written: :func:`build` names its result)."""
    if format == "designspace":
        return stem + ("-ufo.zip" if ufo_only else "-designspace.zip")
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
    (fontc 1.0.0 refused the designspace of such a font, a source without
    ``<location>``, and took the UFO alone.)"""
    return not doc.axes and len(doc.sources) == 1


def _package_features(package: pathlib.Path) -> str:
    """The feature code of a ``.fontra`` package, as fontc reads it."""
    features = package / "features.txt"
    return features.read_text("utf-8", "replace") if features.is_file() else ""


def _skipped_glyphs(package: pathlib.Path) -> list:
    """The glyphs a ``.fontra`` package says not to export
    (``public.skipExportGlyphs`` in its custom data), if any."""
    import json

    try:
        data = json.loads((package / "font-data.json").read_text("utf-8"))
    except (OSError, ValueError):
        return []
    custom = data.get("customData") if isinstance(data, dict) else None
    skipped = custom.get(SKIP_EXPORT_GLYPHS) if isinstance(custom, dict) else None
    return skipped if isinstance(skipped, list) else []


async def _fontc_input(source: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """What fontc is given: the ``.fontra`` package itself (fontc reads it,
    pins discrete axes to their default, decomposes variable components and
    propagates anchors, as :func:`_compile` does before fontmake), its
    feature code checked first. A font that names glyphs not to export goes
    by a designspace (:func:`_prepare_sources`): fontc leaves them out there,
    not in a package."""
    check_feature_includes(_package_features(source))
    if _skipped_glyphs(source):
        logger.info("fontc reads a designspace: the font has glyphs not to export")
        return await _prepare_sources(source, out)
    return source


async def _prepare_sources(source: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """The font as a designspace for fontc, written in ``out``: the filters
    of :func:`_compile` (same steps) and a designspace with its UFOs, rules
    that name a missing glyph left out. Returns what fontc is given: the
    designspace, or the UFO alone for a font without axes and a single source.
    Discrete axes are dropped (the default is built), as in :func:`_compile`.
    The feature code is checked first. Only for a font with glyphs not to
    export (:func:`_fontc_input`); the others go to fontc as they are."""
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
    """Run fontc on ``source`` (a ``.fontra`` package, a designspace or a
    UFO) and write ``destination``, in the folder of ``destination`` (the
    export's own). Anything but a font made raises :class:`FontcFailed`:
    an exit code other than 0 (a Rust panic ends with 101, and is no
    different), no file or an empty one, a program that does not start.
    ``timeout``: seconds; the runner's clock stops a build before that
    anyway, and kills what the build started."""
    fontc = can_run_fontc()
    if fontc is None:
        raise FontcFailed(None, stderr="fontc is not installed")
    source = source.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    folders = (source, source.parent, destination.parent, destination.parent.parent)
    environment = {k: os.environ[k] for k in _FONTC_ENVIRONMENT if k in os.environ}
    try:
        process = await asyncio.create_subprocess_exec(
            os.fspath(fontc),
            os.fspath(source),
            "--output-file",
            os.fspath(destination),
            cwd=destination.parent,
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


def _announce(progress, **event) -> None:
    """Tell the runner (and so the Download panel) what the build is doing:
    ``stage`` ``build`` when an engine starts, ``built`` when the font is
    made. Never a reason to stop the build."""
    if progress is None:
        return
    try:
        progress(event)
    except Exception:  # a channel that closed: the build goes on
        logger.debug("build event not sent", exc_info=True)


async def _build_font(
    source: pathlib.Path,
    out: pathlib.Path,
    stem: str,
    format: str,
    progress=None,
) -> pathlib.Path:
    """``otf`` or ``ttf`` in ``out`` (``woff2`` starts from the ``ttf``). A
    TrueType font is made by fontc, then by fontmake if fontc cannot; an
    OpenType font by fontmake. One line of the log says which, how long."""
    target = out / f"{stem}.{'otf' if format == 'otf' else 'ttf'}"
    started = time.monotonic()
    engine, fallback = "fontmake", False
    if format != "otf" and can_run_fontc() is not None:
        _announce(progress, stage="build", engine="fontc")
        try:
            given = await _fontc_input(source, out / "fontc")
            await _compile_fontc(given, target)
            engine = "fontc"
        except (FeaturesRefused, MemoryError):
            raise
        except Exception as error:
            fallback = True
            if isinstance(error, FontcFailed):
                detail = f"exit code {error.returncode}\n{error.tail}"
            else:
                detail = f"{type(error).__name__} before fontc ran"
            logger.warning(
                "fontc could not build %s, fontmake takes over: %s", format, detail
            )
            target.unlink(missing_ok=True)
    if engine == "fontmake":
        _announce(progress, stage="build", engine="fontmake", fallback=fallback)
        await _compile(source, target)
    seconds = time.monotonic() - started
    logger.info(
        "engine=%s format=%s seconds=%.1f fallback=%s",
        engine,
        format,
        seconds,
        "yes" if fallback else "no",
    )
    _announce(
        progress,
        stage="built",
        engine=engine,
        seconds=round(seconds, 1),
        fallback=fallback,
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
    source: pathlib.Path,
    work: pathlib.Path,
    stem: str,
    format: str,
    progress=None,
) -> pathlib.Path:
    """``source``: a ``.fontra`` folder. Returns the file to download, in
    ``work``. ``progress``: called with one dict per build event (see
    :func:`_announce`) while a font is built. The sources as ``designspace``
    of a font without axes and with a single source are its UFO alone, in
    ``<stem>-ufo.zip``."""
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    if format == "fontra":
        result = work / download_name(stem, format)
        package = out / f"{stem}.fontra"
        shutil.copytree(source, package)
        _zip(package, result)
    elif format == "designspace":
        from fontTools.designspaceLib import DesignSpaceDocument

        designspace = out / f"{stem}.designspace"
        await _copy(source, designspace)
        ufos = sorted(out.glob("*.ufo"))
        doc = DesignSpaceDocument.fromfile(designspace)
        if has_one_source_without_axes(doc) and len(ufos) == 1:
            result = work / download_name(stem, format, ufo_only=True)
            _zip(ufos[0], result)
        else:
            result = work / download_name(stem, format)
            _zip(designspace, result, *ufos)
    elif format in ("otf", "ttf"):
        result = work / download_name(stem, format)
        built = await _build_font(source, out, stem, format, progress)
        shutil.move(built, result)
    elif format == "woff2":
        from fontTools.ttLib import woff2

        result = work / download_name(stem, format)
        ttf = await _build_font(source, out, stem, format, progress)
        woff2.compress(str(ttf), str(result))
    else:
        raise ValueError(f"unknown format {format!r}")
    return result


# --- where the builds of the Download panel are ------------------------------------

RUN_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


class BuildTracker:
    """The last thing each Download build said, in the server's memory: the
    browser chooses an identifier (a version 4 uuid), gives it with the export,
    and asks for it every few seconds. An entry stays ``keep`` seconds after
    its build ended, long enough for the last question; nothing is stored."""

    LIMIT = 256

    def __init__(self, keep: float = 120.0, clock=time.monotonic):
        self.keep = keep
        self.clock = clock
        self.entries: dict[str, dict] = {}

    def start(self, run: str, project: str) -> None:
        """Raises ValueError for an identifier of the wrong form or already
        known."""
        if not RUN_ID.fullmatch(run):
            raise ValueError("not an identifier")
        self._forget()
        if run in self.entries or len(self.entries) >= self.LIMIT:
            raise ValueError("known identifier")
        self.entries[run] = {
            "project": project,
            "state": "running",
            "stage": "",
            "engine": "",
            "fallback": False,
            "seconds": None,
            "started": self.clock(),
            "ended": None,
        }

    def update(self, run: str, event: dict) -> None:
        """An event of the runner; the ones without a ``stage`` (the steps of
        the recipe) say nothing of the build."""
        entry = self.entries.get(run)
        if entry is None or entry["ended"] is not None:
            return
        stage = event.get("stage")
        if not isinstance(stage, str):
            return
        entry["stage"] = stage
        engine = event.get("engine")
        if engine in ("fontc", "fontmake"):
            entry["engine"] = engine
        if "fallback" in event:
            entry["fallback"] = bool(event["fallback"])
        seconds = event.get("seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            entry["seconds"] = round(float(seconds), 1)

    def finish(self, run: str, succeeded: bool) -> None:
        entry = self.entries.get(run)
        if entry is not None and entry["ended"] is None:
            entry["state"] = "done" if succeeded else "failed"
            entry["ended"] = self.clock()

    def project(self, run: str) -> str | None:
        self._forget()
        entry = self.entries.get(run)
        return None if entry is None else entry["project"]

    def snapshot(self, run: str) -> dict | None:
        """``{state, stage, engine, fallback, seconds}``; ``seconds`` is how
        long the font took once made, else how long the build has run."""
        self._forget()
        entry = self.entries.get(run)
        if entry is None:
            return None
        seconds = entry["seconds"]
        if seconds is None:
            seconds = round((entry["ended"] or self.clock()) - entry["started"], 1)
        return {
            "state": entry["state"],
            "stage": entry["stage"],
            "engine": entry["engine"],
            "fallback": entry["fallback"],
            "seconds": seconds,
        }

    def _forget(self) -> None:
        now = self.clock()
        for run, entry in list(self.entries.items()):
            if entry["ended"] is not None and now - entry["ended"] > self.keep:
                del self.entries[run]
