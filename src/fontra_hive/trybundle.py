"""The Python that "Try Fontra" runs in the visitor's browser (Pyodide).

Opening a UFO, a designspace or a TrueType/OpenType font, and downloading a
designspace with its UFOs, are done in the browser with the same code as on
the server: Fontra's backends and Hive's ``importer`` and ``export``. This
module zips, once per server process, the pure-Python packages they need,
taken from this server's own environment (so the browser runs the same
versions), plus:

- small stand-ins for what the backends import but does not run here
  (``watchfiles``, part of ``ufo2ft``);
- an ``entry_points.txt`` listing only the backends that work in the browser,
  so that Fontra's ``getFileSystemBackend`` finds them there too;
- ``hive_try_convert.py`` and ``hive_try_server.py`` (``client/try/py``),
  what the worker calls: conversions, and Hive's server (git history,
  snapshots, comments) for the fonts kept in the browser.

Served at ``/hive/try/python.zip``; unpacked by ``try-python-worker.js``.

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import pathlib
import zipfile
from functools import lru_cache
from importlib import resources

# Top-level packages (or modules) copied from this environment.
PACKAGES = [
    "fontra",
    "fontra_hive",
    "fontTools",
    "ufoLib2",
    "cattrs",
    "cattr",
    "attr",
    "attrs",
    "typing_extensions",
    # Hive's server in the browser (hive_try_server.py): aiohttp and what it
    # needs, in their pure-Python versions, and dulwich for git.
    "aiohttp",
    "multidict",
    "yarl",
    "propcache",
    "frozenlist",
    "aiosignal",
    "aiohappyeyeballs",
    "idna",
    "dulwich",
]

# Never shipped: compiled code (it would not run in the browser), caches,
# Fontra's own web client (the page already has it), tests.
SKIP_DIRS = {"__pycache__", "tests", "test"}
SKIP_PATHS = {("fontra", "client"), ("fontra_hive", "client")}
SKIP_SUFFIXES = {".so", ".pyd", ".dylib", ".dll", ".pyc", ".pyx", ".c", ".cpp", ".h"}

# Backends that work in the browser, as in Fontra's pyproject.toml.
BACKENDS = {
    "designspace": "fontra.backends.designspace:DesignspaceBackend",
    "ufo": "fontra.backends.designspace:UFOBackend",
    "ttf": "fontra.backends.opentype:OTFBackend",
    "otf": "fontra.backends.opentype:OTFBackend",
    "woff": "fontra.backends.opentype:OTFBackend",
    "woff2": "fontra.backends.opentype:OTFBackend",
    "ttx": "fontra.backends.opentype:TTXBackend",
    "fontra": "fontra.backends.fontra:FontraBackend",
}

STUBS = {
    # The file watcher of Fontra's backends: nothing to watch here.
    "watchfiles/__init__.py": '''"""Stand-in for watchfiles in the browser."""


class Change:
    added = 1
    modified = 2
    deleted = 3


async def awatch(*args, **kwargs):
    if False:
        yield None
''',
    # fontra.core.kernutils uses two small functions of ufo2ft.
    "ufo2ft/__init__.py": "",
    "ufo2ft/featureWriters/__init__.py": "",
    "ufo2ft/featureWriters/kernFeatureWriter.py": '''"""From ufo2ft (MIT)."""
import unicodedata

RTL_BIDI_TYPES = {"R", "AL"}
LTR_BIDI_TYPES = {"L", "AN", "EN"}


def unicodeBidiType(uv):
    bidiType = unicodedata.bidirectional(chr(uv))
    if bidiType in RTL_BIDI_TYPES:
        return "R"
    elif bidiType in LTR_BIDI_TYPES:
        return "L"
    return None
''',
    "ufo2ft/util.py": '''"""From ufo2ft (MIT); without the GSUB closure."""


def classifyGlyphs(unicodeFunc, cmap, gsub=None, extra_substitutions=None):
    glyphSets = {}
    neutralGlyphs = set()
    for uv, glyphName in cmap.items():
        key_or_keys = unicodeFunc(uv)
        if key_or_keys is None:
            neutralGlyphs.add(glyphName)
        elif isinstance(key_or_keys, (list, set, tuple)):
            for key in key_or_keys:
                glyphSets.setdefault(key, set()).add(glyphName)
        else:
            glyphSets.setdefault(key_or_keys, set()).add(glyphName)
    if extra_substitutions:
        for glyphs in glyphSets.values():
            to_append = set()
            for glyph in glyphs:
                to_append |= extra_substitutions.get(glyph, set())
            glyphs.update(to_append)
    return glyphSets
''',
}

DIST_INFO = "hive_try_backends-0.dist-info"
GLYPHS_DIST_INFO = "hive_try_glyphs-0.dist-info"

# Fontra runs some work in threads or other processes; there are none in the
# browser: there, the work is done right away.
OVERRIDES = {
    "fontra/core/threading.py": '''"""In the browser (Pyodide): no threads, the work is done right away."""


async def runInThread(func, *args):
    return func(*args)


def shutdownThreadPool():
    pass
''',
    # skia-pathops is compiled: booleanOperations and pyclipper instead.
    "fontra/core/pathops.py": '''"""In the browser (Pyodide): see hive_try_pathops."""

from hive_try_pathops import excludePath, intersectPath, subtractPath, unionPath

__all__ = ["unionPath", "subtractPath", "intersectPath", "excludePath"]
''',
    "fontra/core/subprocess.py": '''"""In the browser (Pyodide): no processes, the work is done right away."""


async def runInSubProcess(func, *args):
    return func(*args)


def shutdownProcessPool():
    pass
''',
}

# Reading Glyphs files, in a second bundle loaded only when one is opened
# (glyphsLib's glyph data weighs 8 MB): glyphsLib, fontra-glyphs, and a
# pure-Python openstep_plist (client/try/py/openstep_plist: the real one is
# compiled code).
GLYPHS_PACKAGES = ["glyphsLib", "fontra_glyphs"]
GLYPHS_BACKENDS = {
    "glyphs": "fontra_glyphs.backend:GlyphsBackend",
    "glyphspackage": "fontra_glyphs.backend:GlyphsPackageBackend",
}


def _entryPoints(backends: dict[str, str]) -> str:
    lines = ["[fontra.filesystem.backends]"]
    lines += [f"{name} = {target}" for name, target in backends.items()]
    return "\n".join(lines) + "\n"


def _packageFiles(name: str):
    """(path in the zip, file) for a package or module of this environment."""
    spec = importlib.util.find_spec(name)
    if spec is None:
        raise ModuleNotFoundError(name)
    if spec.submodule_search_locations:
        for location in spec.submodule_search_locations:
            root = pathlib.Path(location)
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(root)
                if any(part in SKIP_DIRS for part in rel.parts[:-1]):
                    continue
                if rel.parts and (name, rel.parts[0]) in SKIP_PATHS:
                    continue
                if path.suffix in SKIP_SUFFIXES or path.name.startswith("."):
                    continue
                arcname = f"{name}/{rel.as_posix()}"
                if arcname in OVERRIDES:
                    continue
                yield arcname, path
    else:
        path = pathlib.Path(spec.origin)
        yield path.name, path


def _tryPy():
    return resources.files("fontra_hive") / "client" / "try" / "py"


def _writeDistInfo(z, distInfo: str, name: str, backends: dict[str, str]) -> None:
    z.writestr(
        f"{distInfo}/METADATA",
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 0\n",
    )
    z.writestr(f"{distInfo}/entry_points.txt", _entryPoints(backends))


def buildBundle() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in PACKAGES:
            for arcname, path in _packageFiles(name):
                z.write(path, arcname)
        for arcname, text in {**STUBS, **OVERRIDES}.items():
            z.writestr(arcname, text)
        _writeDistInfo(z, DIST_INFO, "hive-try-backends", BACKENDS)
        py = _tryPy()
        for name in ("hive_try_convert.py", "hive_try_server.py", "hive_try_pathops.py"):
            z.writestr(name, (py / name).read_text())
        # Path operations (hive_try_pathops): booleanOperations, pure Python.
        for entry in sorted((py / "booleanOperations").iterdir(), key=lambda e: e.name):
            if entry.name.endswith(".py") or entry.name == "LICENSE":
                z.writestr(f"booleanOperations/{entry.name}", entry.read_text())
    return buffer.getvalue()


def glyphsAvailable() -> bool:
    return all(importlib.util.find_spec(name) for name in GLYPHS_PACKAGES)


def buildGlyphsBundle() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in GLYPHS_PACKAGES:
            for arcname, path in _packageFiles(name):
                z.write(path, arcname)
        plist = _tryPy() / "openstep_plist"
        for entry in sorted(plist.iterdir(), key=lambda e: e.name):
            if entry.name.endswith(".py") or entry.name == "LICENSE":
                z.writestr(f"openstep_plist/{entry.name}", entry.read_text())
        _writeDistInfo(z, GLYPHS_DIST_INFO, "hive-try-glyphs", GLYPHS_BACKENDS)
    return buffer.getvalue()


def _withETag(data: bytes) -> tuple[bytes, str]:
    return data, '"' + hashlib.sha1(data).hexdigest()[:20] + '"'


@lru_cache(maxsize=1)
def bundle() -> tuple[bytes, str]:
    """The zip, and its ETag."""
    return _withETag(buildBundle())


@lru_cache(maxsize=1)
def glyphsBundle() -> tuple[bytes, str] | None:
    """The Glyphs zip, and its ETag; None when this server cannot read
    Glyphs files itself (glyphsLib or fontra-glyphs missing)."""
    return _withETag(buildGlyphsBundle()) if glyphsAvailable() else None
