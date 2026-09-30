"""The Python that "Try Fontra" runs in the visitor's browser (Pyodide).

Opening a UFO, a designspace or a TrueType/OpenType font, and downloading a
designspace with its UFOs, are done in the browser with the same code as on
the server: Fontra's backends and Hive's ``importer`` and ``export``. This
module zips, once per server process, the pure-Python packages they need,
taken from this server's own environment (so the browser runs the same
versions), plus:

- small stand-ins for what the backends import but these conversions do not
  use (``aiohttp``, ``watchfiles``, part of ``ufo2ft``);
- an ``entry_points.txt`` listing only the backends that work in the browser,
  so that Fontra's ``getFileSystemBackend`` finds them there too;
- ``hive_try_convert.py`` (``client/try/py``), what the worker calls.

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
    # fontra.core.protocols imports aiohttp.web for type hints only.
    "aiohttp/__init__.py": '''"""Stand-in for aiohttp in the browser (type hints)."""


class _Anything:
    def __getattr__(self, name):
        return _Anything()

    def __call__(self, *args, **kwargs):
        return _Anything()

    def __mro_entries__(self, bases):
        return (object,)

    def __getitem__(self, key):
        return _Anything()


web = _Anything()
ClientSession = _Anything()
''',
    "aiohttp/web.py": "from . import _Anything\n\n\ndef __getattr__(name):\n"
    "    return _Anything()\n",
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


def _entryPoints() -> str:
    lines = ["[fontra.filesystem.backends]"]
    lines += [f"{name} = {target}" for name, target in BACKENDS.items()]
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
                yield f"{name}/{rel.as_posix()}", path
    else:
        path = pathlib.Path(spec.origin)
        yield path.name, path


def buildBundle() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in PACKAGES:
            for arcname, path in _packageFiles(name):
                z.write(path, arcname)
        for arcname, text in STUBS.items():
            z.writestr(arcname, text)
        z.writestr(
            f"{DIST_INFO}/METADATA",
            "Metadata-Version: 2.1\nName: hive-try-backends\nVersion: 0\n",
        )
        z.writestr(f"{DIST_INFO}/entry_points.txt", _entryPoints())
        convert = resources.files("fontra_hive") / "client" / "try" / "py"
        z.writestr("hive_try_convert.py", (convert / "hive_try_convert.py").read_text())
    return buffer.getvalue()


@lru_cache(maxsize=1)
def bundle() -> tuple[bytes, str]:
    """The zip, and its ETag."""
    data = buildBundle()
    return data, '"' + hashlib.sha1(data).hexdigest()[:20] + '"'
