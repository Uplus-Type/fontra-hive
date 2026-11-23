"""What a change to a font touches: the glyphs, or a domain beyond the glyph.

A designer's ``edit`` covers the glyphs. What goes beyond the glyph is split
in three domains, each with its own capability, which a project can keep to
some people (the accounts service decides; :mod:`fontra_hive.access` has the
development directory's version):

- ``fontsettings``: font info, units per em, axes (cross-axis mapping
  included), sources (line metrics and zones included), features,
  conditional substitutions, and the font's custom data except the plug-ins'
  keys (status definitions included);
- ``kerning``: the kerning, groups included;
- ``plugindata``: the font's custom data written by plug-ins
  (:data:`PLUGIN_KEY_PREFIXES`, :data:`PLUGIN_KEYS`).

The glyphs themselves, the glyph map (creating and renaming glyphs, their
code points), the glyph infos and the background images stay ``edit``.

:func:`changeDomains` reads a change as Fontra sends it (``fontra.core.changes``:
a path ``p`` from the font, a function ``f`` with arguments ``a``, children
``c`` whose paths continue their parent's). A top-level key of the font that
is not listed here (one Fontra adds later) counts as ``fontsettings``: it is
never open by oversight. ``fontra_hive.fonthandler`` refuses a change as a
whole as soon as one of its parts falls in a domain the person may not write.
"""

from __future__ import annotations

EDIT = "edit"
FONTSETTINGS = "fontsettings"
KERNING = "kerning"
PLUGINDATA = "plugindata"

WRITE_DOMAINS = (FONTSETTINGS, KERNING, PLUGINDATA)
EVERYTHING = frozenset({EDIT, *WRITE_DOMAINS})

LABELS = {
    EDIT: "Glyphs",
    FONTSETTINGS: "Font settings",
    KERNING: "Kerning",
    PLUGINDATA: "Plug-in data",
}

# The top-level attributes of fontra.core.classes.Font (Fontra 2026.10.2),
# each in its domain; customData is split by key (customDataDomain).
TOP_LEVEL: dict[str, str | None] = {
    "glyphs": EDIT,
    "glyphMap": EDIT,
    "glyphInfos": EDIT,
    "unitsPerEm": FONTSETTINGS,
    "fontInfo": FONTSETTINGS,
    "axes": FONTSETTINGS,
    "sources": FONTSETTINGS,
    "features": FONTSETTINGS,
    "conditionalSubstitutions": FONTSETTINGS,
    "kerning": KERNING,
    "customData": None,
}

# The font's custom data written by plug-ins: U+Marks' rules, and the
# project's glyph sets (Fontra's font overview).
PLUGIN_KEY_PREFIXES = ("umarks.",)
PLUGIN_KEYS = frozenset({"fontra.projectGlyphSets"})

# Fontra's change functions whose first argument is a key of the subject
# (setItem, delAttr); the others act on the subject itself (a list).
_KEYED_FUNCTIONS = frozenset({"=", "d"})


def customDataDomain(key) -> str:
    if isinstance(key, str) and (
        key in PLUGIN_KEYS or key.startswith(PLUGIN_KEY_PREFIXES)
    ):
        return PLUGINDATA
    return FONTSETTINGS


def pathDomains(path: tuple) -> frozenset[str]:
    """The domains a change at ``path`` (from the font) may touch."""
    if not path:
        return EVERYTHING  # the whole font
    key = path[0]
    if key == "customData":
        if len(path) < 2:
            return frozenset({FONTSETTINGS, PLUGINDATA})  # all of it
        return frozenset({customDataDomain(path[1])})
    domain = TOP_LEVEL.get(key) if isinstance(key, str) else None
    return frozenset({domain or FONTSETTINGS})


def changeDomains(change) -> frozenset[str]:
    """Every domain the change touches (``edit`` for the glyphs)."""
    found: set[str] = set()
    _walk(change, (), found)
    return frozenset(found)


def _walk(change, prefix: tuple, found: set[str]) -> None:
    if not isinstance(change, dict):
        found.update(EVERYTHING)  # not a change Fontra makes: assume the worst
        return
    path = change.get("p") or []
    children = change.get("c") or []
    if not isinstance(path, list) or not isinstance(children, list):
        found.update(EVERYTHING)
        return
    path = prefix + tuple(path)
    function = change.get("f")
    if function is not None:
        args = change.get("a") or []
        if function in _KEYED_FUNCTIONS and isinstance(args, list) and args:
            found.update(pathDomains(path + (args[0],)))
        else:
            found.update(pathDomains(path))
    for child in children:
        _walk(child, path, found)


def refusal(domain: str) -> str:
    """Why a change in ``domain`` is refused, in the person's words."""
    if domain == EDIT:
        return "Read only: your role cannot change this font."
    return (
        f"{LABELS[domain]} are kept to some people on this project. Ask a manager."
        if domain == FONTSETTINGS
        else f"{LABELS[domain]} is kept to some people on this project. Ask a manager."
    )
