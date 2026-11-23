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


# --- names ----------------------------------------------------------------------

# The fields whose text is a name (a source's, an axis's tag or label, a
# component's glyph), and the kerning's groups, whose members are glyph names.
NAME_FIELDS = frozenset({"name", "tag", "label", "baseGlyph", "glyphName"})
GROUP_FIELDS = frozenset({"groupsSide1", "groupsSide2"})

# Every control character (C0, DEL, C1) and the Unicode line and paragraph
# separators: never in a name.
_CONTROL = frozenset(chr(c) for c in [*range(0x00, 0x20), *range(0x7F, 0xA0), 0x2028, 0x2029])

NAME_REFUSAL = "A name cannot contain a line break or a control character"


def hasControlCharacter(text: str) -> bool:
    return any(c in _CONTROL for c in text)


def nameWithControlCharacter(change) -> str | None:
    """The first name the change writes that holds a control character or a
    line separator (a glyph, a source, an axis, a kerning group, a layer, a
    key of the custom data…), or None. Names are the keys written (of a
    ``=``, of every mapping written), the text of :data:`NAME_FIELDS`, and
    the kerning groups' members. Other texts (font info, features, notes)
    may hold line breaks; the path and what is deleted are not looked at."""
    found: list[str] = []
    _walkNames(change, (), found)
    return found[0] if found else None


def _walkNames(change, prefix: tuple, found: list) -> None:
    if found or not isinstance(change, dict):
        return
    path = change.get("p") or []
    if not isinstance(path, list):
        return
    # The path names what exists already: only what is written is checked
    # (a name made before can still be fixed, or deleted).
    path = prefix + tuple(path)
    function = change.get("f")
    args = change.get("a") or []
    if isinstance(args, list) and args:
        if function == "=":
            key = args[0]
            if isinstance(key, str) and hasControlCharacter(key):
                found.append(key)
                return
            for value in args[1:]:
                _walkValue(value, key, path + (key,), found)
        elif function == "+":
            for value in args[1:]:
                _walkValue(value, None, path, found)
        elif function == ":":
            for value in args[2:]:
                _walkValue(value, None, path, found)
    for child in change.get("c") or []:
        _walkNames(child, path, found)


def _walkValue(value, key, path: tuple, found: list) -> None:
    if found:
        return
    if isinstance(value, str):
        inGroup = any(p in GROUP_FIELDS for p in path)
        if (key in NAME_FIELDS or inGroup) and hasControlCharacter(value):
            found.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and hasControlCharacter(k):
                found.append(k)
                return
            _walkValue(v, k, path + (k,), found)
    elif isinstance(value, list):
        for v in value:
            _walkValue(v, key, path, found)


def refusal(domain: str) -> str:
    """Why a change in ``domain`` is refused, in the person's words."""
    if domain == EDIT:
        return "Read only: your role cannot change this font."
    return (
        f"{LABELS[domain]} are kept to some people on this project. Ask a manager."
        if domain == FONTSETTINGS
        else f"{LABELS[domain]} is kept to some people on this project. Ask a manager."
    )
