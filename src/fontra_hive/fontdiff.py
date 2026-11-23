"""What changed beyond the glyph between two versions of a ``.fontra``
package, in words.

The four files of a ``.fontra`` package that are not glyphs are compared:
``font-data.json`` (font info, units per em, axes and cross-axis mappings,
sources with their line metrics and guidelines, the font's custom data),
``features.txt``, ``kerning.csv`` and ``glyph-info.csv``. They are read with
Fontra's own functions (``fontra.core.classes.structure``,
``fontra.backends.fontra.readKerningFile``, ``readGlyphInfo``), never with a
parser of ours, so a file Fontra reads is read the same way here.

The result is a list of :class:`FontChange`, each in one *area* — what the
designer sets in one place: a panel of Font info, the kerning, the plug-ins'
data, the glyph info — with a short English sentence (``summary``) and, for
the detail view, the fields before and after (``fields``) or, for the
features, the lines (``lines``)::

    Font info: version 1.001 → 1.002
    Axes: wght 100–900 → 200–900
    Sources: Black added
    Line metrics (Regular): xHeight 500 → 510
    Features: 12 lines added, 3 removed
    Kerning: 34 pairs changed (Bold)

Each area belongs to the write domain of :mod:`fontra_hive.writedomains`
that guards it (``fontsettings``, ``kerning``, ``plugindata``; the glyph info
is ``edit``, like the glyphs).

Pure functions over bytes: :mod:`fontra_hive.backend_git` uses them to write
the commit messages, :mod:`fontra_hive.projectmanager` to describe the
commits of the font's history and to answer ``font-diff``.

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import csv
import dataclasses
import difflib
from collections import Counter
import io
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .writedomains import (
    EDIT,
    FONTSETTINGS,
    KERNING,
    PLUGINDATA,
    WRITE_DOMAINS,
    customDataDomain,
)

FONT_DATA = "font-data.json"
FEATURES = "features.txt"
KERNING_FILE = "kerning.csv"
GLYPH_INFO = "glyph-info.csv"
# The files of a .fontra package that are not glyphs (nor background images).
FONT_FILES = (FONT_DATA, FEATURES, KERNING_FILE, GLYPH_INFO)
# Those that hold only what goes beyond the glyph.
BEYOND_THE_GLYPH_FILES = (FONT_DATA, FEATURES, KERNING_FILE)

STATUS_DEFINITIONS_KEY = "fontra.sourceStatusFieldDefinitions"
SOURCE_STATUS_KEY = "fontra.development.status"

# area -> (domain, words for a commit title), in the order they are told.
AREAS: dict[str, tuple[str, str]] = {
    "fontInfo": (FONTSETTINGS, "font info"),
    "axes": (FONTSETTINGS, "axes"),
    "mappings": (FONTSETTINGS, "cross-axis mapping"),
    "sources": (FONTSETTINGS, "sources"),
    "features": (FONTSETTINGS, "features"),
    "conditionalSubstitutions": (FONTSETTINGS, "conditional substitutions"),
    "statusDefinitions": (FONTSETTINGS, "development status"),
    "customData": (FONTSETTINGS, "custom data"),
    "kerning": (KERNING, "kerning"),
    "pluginData": (PLUGINDATA, "plug-in data"),
    "glyphInfo": (EDIT, "glyph info"),
}

# How many items a sentence names before "and N more".
MAX_ITEMS = 3
# A value longer than this is "changed", not shown in the sentence.
SHORT = 28
# The detail view's limits (a font with 40 000 kerning pairs changed).
MAX_FIELDS = 300
MAX_LINES = 2000
# The features' detail is a unified diff (difflib, quadratic in the worst
# case) only while the two versions' line counts multiplied stay under this:
# 2 000 × 2 000 lines, about a quarter of a second. Beyond, the counts only.
MAX_DIFF_CELLS = 4_000_000
TOO_MANY_LINES = "Too many lines to show the difference."

FONT_INFO_LABELS = {
    "familyName": "family name",
    "copyright": "copyright",
    "trademark": "trademark",
    "description": "description",
    "sampleText": "sample text",
    "designer": "designer",
    "designerURL": "designer URL",
    "manufacturer": "manufacturer",
    "manufacturerURL": "manufacturer URL",
    "licenseDescription": "license",
    "licenseInfoURL": "license URL",
    "vendorID": "vendor ID",
}

PLUGIN_LABELS = {"fontra.projectGlyphSets": "project glyph sets"}


@dataclass
class Field:
    """One value before and after, as text (None: absent)."""

    label: str
    before: str | None
    after: str | None

    def to_json(self) -> dict:
        return {"label": self.label, "before": self.before, "after": self.after}


@dataclass
class FontChange:
    area: str
    summary: str
    fields: list[Field] = field(default_factory=list)
    # Features: the lines of a unified diff, [(" " | "+" | "-" | "@", text)].
    lines: list[tuple[str, str]] | None = None
    truncated: bool = False
    # Words for a commit title, when not the area's ("line metrics").
    words: str | None = None
    # Said in the detail view instead of what it cannot show.
    note: str | None = None

    @property
    def domain(self) -> str:
        return AREAS[self.area][0]

    def to_json(self, detail: bool = True) -> dict:
        data: dict[str, Any] = {
            "area": self.area,
            "domain": self.domain,
            "summary": self.summary,
        }
        if detail:
            data["fields"] = [f.to_json() for f in self.fields[:MAX_FIELDS]]
            if self.lines is not None:
                data["lines"] = [list(line) for line in self.lines]
            data["truncated"] = self.truncated or len(self.fields) > MAX_FIELDS
            if self.note:
                data["note"] = self.note
        return data


# --- formatting --------------------------------------------------------------------


def _num(value) -> str:
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _text(value) -> str | None:
    """A value as the detail view shows it."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return _num(value)
    if dataclasses.is_dataclass(value):
        value = _unstructure(value)
    # A list or a dict may hold Fontra objects too (an axis's value labels).
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_unstructure)


def _short(value: str | None) -> bool:
    return value is not None and len(value) <= SHORT and "\n" not in value


def _item(label: str, before: str | None, after: str | None) -> str:
    """``version 1.001 → 1.002``, ``designer changed``, ``vendor ID set to UTYP``."""
    if before is None:
        return f"{label} set to {after}" if _short(after) else f"{label} set"
    if after is None:
        return f"{label} removed"
    if _short(before) and _short(after):
        return f"{label} {before} → {after}"
    return f"{label} changed"


def _sentence(prefix: str, items: list[str]) -> str:
    shown = items[:MAX_ITEMS]
    rest = len(items) - len(shown)
    return f"{prefix}: {', '.join(shown)}" + (f" and {rest} more" if rest else "")


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_ITEMS])
    rest = len(names) - MAX_ITEMS
    return shown + (f" and {rest} more" if rest > 0 else "")


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


# --- reading, with Fontra's functions ------------------------------------------------


class Unreadable(ValueError):
    pass


def _unstructure(value):
    from fontra.core.classes import unstructure

    return unstructure(value)


def read_font_data(data: bytes | None):
    """``font-data.json`` as Fontra reads it (a ``fontra.core.classes.Font``);
    an absent file is an empty font."""
    from fontra.core.classes import Font, structure

    if data is None:
        return Font()
    try:
        return structure(json.loads(data.decode("utf-8")), Font)
    except Exception as error:  # Fontra could not read it either
        raise Unreadable(f"font-data.json: {error}") from error


def read_glyph_info(data: bytes | None) -> tuple[dict, dict]:
    """``glyph-info.csv`` as ``FontraBackend._readGlyphInfo`` reads it:
    ``(glyphMap, glyphInfos)``."""
    from fontra.backends.fontra import _parseCodePoints, readGlyphInfo

    glyphMap: dict[str, list[int]] = {}
    glyphInfos: dict[str, dict] = {}
    if data is None:
        return glyphMap, glyphInfos
    try:
        reader = csv.reader(io.StringIO(data.decode("utf-8"), newline=""), delimiter=";")
        header = next(reader, None)
        if header is None or header[:2] != ["glyph name", "code points"]:
            raise ValueError("unexpected header")
        infoKeys = header[2:]
        for row in reader:
            if not row:
                continue
            glyphName, *rest = row
            codePoints: list[int] = []
            if rest:
                codePoints = _parseCodePoints(rest[0])
                if infoKeys:
                    info = readGlyphInfo(infoKeys, rest[1:])
                    if info:
                        glyphInfos[glyphName] = info
            glyphMap[glyphName] = codePoints
    except Exception as error:
        raise Unreadable(f"glyph-info.csv: {error}") from error
    return glyphMap, glyphInfos


def read_kerning(data: bytes | None) -> dict:
    """``kerning.csv`` as Fontra reads it (``readKerningFile``), by pair:
    ``{kernType: {"groups1", "groups2", "values": {(left, right): {source: value}}}}``."""
    from .merge import _read_kerning

    try:
        kerning = _read_kerning(data)
    except Exception as error:
        raise Unreadable(f"kerning.csv: {error}") from error
    return {
        kernType: {
            "groups1": table["groups1"],
            "groups2": table["groups2"],
            "values": {
                (left, right): row
                for left, rights in table["values"].items()
                for right, row in rights.items()
            },
        }
        for kernType, table in kerning.items()
    }


def source_names(fontData: bytes | None) -> dict[str, str]:
    """``{source identifier: name}`` (to name the sources of the kerning)."""
    try:
        font = read_font_data(fontData)
    except Unreadable:
        return {}
    return {key: source.name for key, source in font.sources.items()}


# --- font-data.json ----------------------------------------------------------------


def _version(info) -> str | None:
    if info.versionMajor is None and info.versionMinor is None:
        return None
    if info.versionMinor is None:
        return str(info.versionMajor)
    return f"{info.versionMajor or 0}.{info.versionMinor:03d}"


def _font_info_changes(old, new) -> list[FontChange]:
    fields: list[Field] = []
    before, after = _version(old.fontInfo), _version(new.fontInfo)
    if before != after:
        fields.append(Field("version", before, after))
    for key, label in FONT_INFO_LABELS.items():
        a, b = getattr(old.fontInfo, key), getattr(new.fontInfo, key)
        if a != b:
            fields.append(Field(label, _text(a), _text(b)))
    for key in sorted(set(old.fontInfo.customData) | set(new.fontInfo.customData)):
        a, b = old.fontInfo.customData.get(key), new.fontInfo.customData.get(key)
        if a != b:
            fields.append(Field(key, _text(a), _text(b)))
    if old.unitsPerEm != new.unitsPerEm:
        fields.append(Field("units per em", _num(old.unitsPerEm), _num(new.unitsPerEm)))
    if not fields:
        return []
    items = [_item(f.label, f.before, f.after) for f in fields]
    return [FontChange("fontInfo", _sentence("Font info", items), fields)]


def _axis_name(axis) -> str:
    return axis.tag or axis.name


def _axis_range(axis) -> str:
    if hasattr(axis, "values"):
        return ", ".join(_num(v) for v in axis.values)
    return f"{_num(axis.minValue)}–{_num(axis.maxValue)}"


def _axes_changes(old, new) -> list[FontChange]:
    oldAxes = {axis.name: axis for axis in old.axes.axes}
    newAxes = {axis.name: axis for axis in new.axes.axes}
    items: list[str] = []
    fields: list[Field] = []
    for name, axis in newAxes.items():
        if name not in oldAxes:
            items.append(f"{_axis_name(axis)} added")
            fields.append(Field(_axis_name(axis), None, _axis_range(axis)))
    for name, axis in oldAxes.items():
        if name not in newAxes:
            items.append(f"{_axis_name(axis)} removed")
            fields.append(Field(_axis_name(axis), _axis_range(axis), None))
    for name, a in oldAxes.items():
        b = newAxes.get(name)
        if b is None or a == b:
            continue
        label = _axis_name(b)
        if _axis_range(a) != _axis_range(b):
            items.append(f"{label} {_axis_range(a)} → {_axis_range(b)}")
            fields.append(Field(f"{label} range", _axis_range(a), _axis_range(b)))
        for attr, words in (
            ("defaultValue", "default"),
            ("tag", "tag"),
            ("label", "label"),
            ("mapping", "mapping"),
            ("valueLabels", "labels"),
            ("hidden", "hidden"),
            ("customData", "custom data"),
        ):
            x, y = getattr(a, attr, None), getattr(b, attr, None)
            if x == y:
                continue
            fields.append(Field(f"{label} {words}", _text(x), _text(y)))
            if attr in ("defaultValue", "tag", "label"):
                items.append(_item(f"{label} {words}", _text(x), _text(y)))
            elif attr == "hidden":
                items.append(f"{label} {'hidden' if y else 'shown'}")
            else:
                items.append(f"{label} {words} changed")
        if type(a) is not type(b):
            items.append(f"{label} {'discrete' if hasattr(b, 'values') else 'continuous'}")
    if not items and [a.name for a in old.axes.axes] != [b.name for b in new.axes.axes]:
        items.append("order changed")
        fields.append(
            Field(
                "order",
                ", ".join(_axis_name(a) for a in old.axes.axes),
                ", ".join(_axis_name(b) for b in new.axes.axes),
            )
        )
    if old.axes.elidedFallBackname != new.axes.elidedFallBackname:
        before, after = old.axes.elidedFallBackname, new.axes.elidedFallBackname
        items.append(_item("elided fallback name", before, after))
        fields.append(Field("elided fallback name", before, after))
    if old.axes.customData != new.axes.customData:
        items.append("custom data changed")
        fields.append(
            Field("custom data", _text(old.axes.customData), _text(new.axes.customData))
        )
    changes = []
    if items:
        changes.append(FontChange("axes", _sentence("Axes", items), fields))
    changes += _mappings_changes(old.axes.mappings, new.axes.mappings)
    return changes


def _mapping_label(index: int, mapping) -> str:
    return mapping.description or f"mapping {index + 1}"


def _mappings_changes(old: list, new: list) -> list[FontChange]:
    if old == new:
        return []
    fields = []
    changed = 0
    for i in range(max(len(old), len(new))):
        a = old[i] if i < len(old) else None
        b = new[i] if i < len(new) else None
        if a == b:
            continue
        if a is not None and b is not None:
            changed += 1
        fields.append(Field(_mapping_label(i, b or a), _text(a), _text(b)))
    items = []
    if len(new) > len(old):
        items.append(f"{_plural(len(new) - len(old), 'mapping')} added")
    elif len(old) > len(new):
        items.append(f"{_plural(len(old) - len(new), 'mapping')} removed")
    if changed:
        items.append(f"{_plural(changed, 'mapping')} changed")
    return [FontChange("mappings", _sentence("Cross-axis mapping", items), fields)]


def _location_text(location: dict, tags: dict[str, str]) -> str:
    """``wdth 0, wght 150``, in the order of the font's axes."""
    order = {axis: i for i, axis in enumerate(tags)}
    return ", ".join(
        f"{tags.get(axis, axis)} {_num(value)}"
        for axis, value in sorted(
            location.items(), key=lambda item: (order.get(item[0], len(order)), item[0])
        )
    )


METRIC_LAYOUTS = (
    ("lineMetricsHorizontalLayout", "Line metrics"),
    ("lineMetricsVerticalLayout", "Vertical line metrics"),
)


def _sources_changes(old, new) -> list[FontChange]:
    tags = {axis.name: axis.tag for axis in [*new.axes.axes, *old.axes.axes]}
    items: list[str] = []
    fields: list[Field] = []
    perSource: list[FontChange] = []
    for key, source in new.sources.items():
        if key not in old.sources:
            items.append(f"{source.name} added")
            fields.append(Field(source.name, None, _location_text(source.location, tags)))
    for key, source in old.sources.items():
        if key not in new.sources:
            items.append(f"{source.name} removed")
            fields.append(Field(source.name, _location_text(source.location, tags), None))
    for key, a in old.sources.items():
        b = new.sources.get(key)
        if b is None or a == b:
            continue
        name = b.name
        if a.name != b.name:
            items.append(f"{a.name} renamed {b.name}")
            fields.append(Field(f"{b.name} name", a.name, b.name))
        if a.location != b.location:
            before = _location_text(a.location, tags)
            after = _location_text(b.location, tags)
            items.append(f"{name} moved ({before} → {after})")
            fields.append(Field(f"{name} location", before, after))
        if a.isSparse != b.isSparse:
            items.append(f"{name} {'sparse' if b.isSparse else 'not sparse'}")
            fields.append(Field(f"{name} sparse", _text(a.isSparse), _text(b.isSparse)))
        if a.italicAngle != b.italicAngle:
            before, after = _num(a.italicAngle), _num(b.italicAngle)
            items.append(f"{name} italic angle {before} → {after}")
            fields.append(Field(f"{name} italic angle", before, after))
        oldStatus = a.customData.get(SOURCE_STATUS_KEY)
        newStatus = b.customData.get(SOURCE_STATUS_KEY)
        if oldStatus != newStatus:
            labels = _status_labels(new)
            before = labels.get(oldStatus, _text(oldStatus))
            after = labels.get(newStatus, _text(newStatus))
            items.append(_item(f"{name} status", before, after))
            fields.append(Field(f"{name} status", before, after))
        otherA = {k: v for k, v in a.customData.items() if k != SOURCE_STATUS_KEY}
        otherB = {k: v for k, v in b.customData.items() if k != SOURCE_STATUS_KEY}
        if otherA != otherB:
            items.append(f"{name} custom data changed")
            fields.append(Field(f"{name} custom data", _text(otherA), _text(otherB)))
        for attr, prefix in METRIC_LAYOUTS:
            change = _metrics_change(name, prefix, getattr(a, attr), getattr(b, attr))
            if change is not None:
                perSource.append(change)
        if a.guidelines != b.guidelines:
            perSource.append(_guidelines_change(name, a.guidelines, b.guidelines))
    changes = []
    if items:
        changes.append(FontChange("sources", _sentence("Sources", items), fields))
    return changes + perSource


def _metrics_change(sourceName: str, prefix: str, old: dict, new: dict):
    items: list[str] = []
    fields: list[Field] = []
    for name in [*new, *(n for n in old if n not in new)]:
        a, b = old.get(name), new.get(name)
        if a == b:
            continue
        if a is None or b is None:
            value = _num((a or b).value)
            items.append(f"{name} {'added' if a is None else 'removed'}")
            fields.append(
                Field(name, None if a is None else value, value if a is None else None)
            )
            continue
        if a.value != b.value:
            items.append(f"{name} {_num(a.value)} → {_num(b.value)}")
            fields.append(Field(name, _num(a.value), _num(b.value)))
        if a.zone != b.zone:
            items.append(f"{name} zone {_num(a.zone)} → {_num(b.zone)}")
            fields.append(Field(f"{name} zone", _num(a.zone), _num(b.zone)))
        if a.customData != b.customData:
            items.append(f"{name} custom data changed")
            fields.append(Field(f"{name} custom data", _text(a.customData), _text(b.customData)))
    if not items:
        return None
    return FontChange(
        "sources", _sentence(f"{prefix} ({sourceName})", items), fields, words="line metrics"
    )


def _guidelines_change(sourceName: str, old: list, new: list) -> FontChange:
    fields = []
    changed = 0
    for i in range(max(len(old), len(new))):
        a = old[i] if i < len(old) else None
        b = new[i] if i < len(new) else None
        if a == b:
            continue
        if a is not None and b is not None:
            changed += 1
        guideline = b or a
        fields.append(Field(guideline.name or f"guideline {i + 1}", _text(a), _text(b)))
    items = []
    if len(new) > len(old):
        items.append(f"{len(new) - len(old)} added")
    elif len(old) > len(new):
        items.append(f"{len(old) - len(new)} removed")
    if changed:
        items.append(f"{changed} changed")
    return FontChange(
        "sources",
        _sentence(f"Font guidelines ({sourceName})", items),
        fields,
        words="font guidelines",
    )


def _status_labels(font) -> dict:
    definitions = font.customData.get(STATUS_DEFINITIONS_KEY)
    if not isinstance(definitions, list):
        return {}
    return {
        d.get("value"): str(d.get("label"))
        for d in definitions
        if isinstance(d, dict) and d.get("label") is not None
    }


def _status_text(definition: dict) -> str:
    """``Approved · value 4 · #00FF80`` (a status definition in words)."""
    parts = [str(definition.get("label", "?")), f"value {_text(definition.get('value'))}"]
    color = definition.get("color")
    if isinstance(color, list) and len(color) >= 3:
        try:
            parts.append("#" + "".join(f"{round(float(c) * 255):02X}" for c in color[:3]))
        except (TypeError, ValueError):
            pass
    if definition.get("isDefault"):
        parts.append("default")
    return " · ".join(parts)


def _status_definitions_change(old, new) -> FontChange | None:
    a = old if isinstance(old, list) else []
    b = new if isinstance(new, list) else []
    if a == b:
        return None
    byValueA = {d.get("value"): d for d in a if isinstance(d, dict)}
    byValueB = {d.get("value"): d for d in b if isinstance(d, dict)}
    items: list[str] = []
    fields: list[Field] = []
    for value, d in byValueB.items():
        label = str(d.get("label", value))
        if value not in byValueA:
            items.append(f"{label} added")
            fields.append(Field(label, None, _status_text(d)))
        elif byValueA[value] != d:
            before = str(byValueA[value].get("label", value))
            if before != label:
                items.append(f"{before} renamed {label}")
            else:
                items.append(f"{label} changed")
            fields.append(Field(label, _status_text(byValueA[value]), _status_text(d)))
    for value, d in byValueA.items():
        if value not in byValueB:
            label = str(d.get("label", value))
            items.append(f"{label} removed")
            fields.append(Field(label, _status_text(d), None))
    if not items:
        items.append("order changed")
        fields.append(Field("definitions", _text(a), _text(b)))
    return FontChange("statusDefinitions", _sentence("Development status", items), fields)


def _what_happened(f: Field) -> str:
    return "added" if f.before is None else "removed" if f.after is None else "changed"


def _custom_data_changes(old: dict, new: dict) -> list[FontChange]:
    changes: list[FontChange] = []
    status = _status_definitions_change(
        old.get(STATUS_DEFINITIONS_KEY), new.get(STATUS_DEFINITIONS_KEY)
    )
    if status is not None:
        changes.append(status)
    settings: list[Field] = []
    plugins: list[Field] = []
    for key in sorted(set(old) | set(new)):
        if key == STATUS_DEFINITIONS_KEY:
            continue
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if customDataDomain(key) == PLUGINDATA:
            plugins.append(Field(PLUGIN_LABELS.get(key, key), _text(a), _text(b)))
        else:
            settings.append(Field(key, _text(a), _text(b)))
    if settings:
        items = [_item(f.label, f.before, f.after) for f in settings]
        changes.append(FontChange("customData", _sentence("Font custom data", items), settings))
    if plugins:
        items = [f"{f.label} {_what_happened(f)}" for f in plugins]
        changes.append(FontChange("pluginData", _sentence("Plug-in data", items), plugins))
    return changes


def _conditional_substitutions_changes(old, new) -> list[FontChange]:
    a, b = old.conditionalSubstitutions, new.conditionalSubstitutions
    if a == b:
        return []
    items: list[str] = []
    fields: list[Field] = []
    if a.featureTags != b.featureTags:
        before, after = " ".join(a.featureTags), " ".join(b.featureTags)
        items.append(_item("feature tags", before, after))
        fields.append(Field("feature tags", before, after))
    changed = 0
    for i in range(max(len(a.rules), len(b.rules))):
        x = a.rules[i] if i < len(a.rules) else None
        y = b.rules[i] if i < len(b.rules) else None
        if x == y:
            continue
        if x is not None and y is not None:
            changed += 1
        rule = y or x
        fields.append(Field(rule.name or f"rule {i + 1}", _text(x), _text(y)))
    if len(b.rules) > len(a.rules):
        items.append(f"{_plural(len(b.rules) - len(a.rules), 'rule')} added")
    elif len(a.rules) > len(b.rules):
        items.append(f"{_plural(len(a.rules) - len(b.rules), 'rule')} removed")
    if changed:
        items.append(f"{_plural(changed, 'rule')} changed")
    return [
        FontChange(
            "conditionalSubstitutions", _sentence("Conditional substitutions", items), fields
        )
    ]


def font_data_changes(old: bytes | None, new: bytes | None) -> list[FontChange]:
    """What changed in ``font-data.json``."""
    try:
        a, b = read_font_data(old), read_font_data(new)
    except Unreadable as error:
        return [
            FontChange(
                "customData",
                "Font settings: changed (the file could not be read)",
                [Field("font-data.json", None, str(error))],
            )
        ]
    changes = _font_info_changes(a, b)
    changes += _axes_changes(a, b)
    changes += _sources_changes(a, b)
    # The features' text is in features.txt; their language and custom data
    # in font-data.json.
    if (a.features.language, a.features.customData) != (
        b.features.language,
        b.features.customData,
    ):
        changes.append(
            FontChange(
                "features",
                "Features: settings changed",
                [
                    Field("language", a.features.language, b.features.language),
                    Field(
                        "custom data",
                        _text(a.features.customData),
                        _text(b.features.customData),
                    ),
                ],
            )
        )
    changes += _conditional_substitutions_changes(a, b)
    changes += _custom_data_changes(a.customData, b.customData)
    return changes


# --- features.txt ----------------------------------------------------------------


def _lines(data: bytes | None) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines() if data else []


def features_changes(
    old: bytes | None, new: bytes | None, detail: bool = False
) -> list[FontChange]:
    """What changed in ``features.txt``: lines added and removed, counted as
    multisets (linear, whatever the size: the only way a commit and the
    history describe it), and with ``detail`` the lines themselves (a
    unified diff) for the detail view, while :data:`MAX_DIFF_CELLS` allows
    it; beyond, the counts and :data:`TOO_MANY_LINES`."""
    a, b = _lines(old), _lines(new)
    if a == b:
        return []
    before, after = Counter(a), Counter(b)
    added = sum((after - before).values())
    removed = sum((before - after).values())
    lines: list[tuple[str, str]] | None = None
    note = None
    if detail and len(a) * len(b) > MAX_DIFF_CELLS:
        note = TOO_MANY_LINES
    elif detail:
        lines = []
        for line in difflib.unified_diff(a, b, lineterm="", n=3):
            if line.startswith(("---", "+++")):
                continue
            if line.startswith("@@"):
                lines.append(("@", line))
                continue
            lines.append((line[:1], line[1:]))
    if added and removed:
        summary = f"Features: {_plural(added, 'line')} added, {removed} removed"
    elif added:
        summary = f"Features: {_plural(added, 'line')} added"
    elif removed:
        summary = f"Features: {_plural(removed, 'line')} removed"
    else:
        summary = "Features: lines moved"
    if lines is None:
        return [FontChange("features", summary, note=note)]
    truncated = len(lines) > MAX_LINES
    return [FontChange("features", summary, lines=lines[:MAX_LINES], truncated=truncated)]


# --- kerning.csv ------------------------------------------------------------------


def _kern_value(value) -> str | None:
    return None if value is None else _num(value)


def kerning_changes(
    old: bytes | None, new: bytes | None, sourceNames: Mapping[str, str] | None = None
) -> list[FontChange]:
    """What changed in ``kerning.csv``: pairs and groups, by source.
    ``sourceNames``: ``{source identifier: name}`` (:func:`source_names`)."""
    sourceNames = sourceNames or {}
    try:
        a, b = read_kerning(old), read_kerning(new)
    except Unreadable as error:
        return [
            FontChange(
                "kerning",
                "Kerning: changed (the file could not be read)",
                [Field("kerning.csv", None, str(error))],
            )
        ]
    changes: list[FontChange] = []
    for kernType in [*b, *(k for k in a if k not in b)]:
        prefix = "Vertical kerning" if kernType == "vkern" else "Kerning"
        x = a.get(kernType) or {"groups1": {}, "groups2": {}, "values": {}}
        y = b.get(kernType) or {"groups1": {}, "groups2": {}, "values": {}}
        items: list[str] = []
        fields: list[Field] = []
        pairs = 0
        sources: list[str] = []
        for pair in sorted(set(x["values"]) | set(y["values"])):
            before, after = x["values"].get(pair, {}), y["values"].get(pair, {})
            if before == after:
                continue
            pairs += 1
            for source in [*after, *(s for s in before if s not in after)]:
                if before.get(source) == after.get(source):
                    continue
                name = sourceNames.get(source, source)
                if name not in sources:
                    sources.append(name)
                fields.append(
                    Field(
                        f"{pair[0]} {pair[1]} ({name})",
                        _kern_value(before.get(source)),
                        _kern_value(after.get(source)),
                    )
                )
        if pairs:
            where = (
                f" ({', '.join(sorted(sources))})"
                if 0 < len(sources) <= MAX_ITEMS
                else f" ({len(sources)} sources)"
                if sources
                else ""
            )
            items.append(f"{_plural(pairs, 'pair')} changed{where}")
        groups = 0
        for side in ("groups1", "groups2"):
            for name in sorted(set(x[side]) | set(y[side])):
                g1, g2 = x[side].get(name), y[side].get(name)
                if g1 == g2:
                    continue
                groups += 1
                fields.append(
                    Field(
                        f"{'left' if side == 'groups1' else 'right'} group {name}",
                        " ".join(g1) if g1 is not None else None,
                        " ".join(g2) if g2 is not None else None,
                    )
                )
        if groups:
            items.append(f"{_plural(groups, 'group')} changed")
        if items:
            changes.append(FontChange("kerning", f"{prefix}: {', '.join(items)}", fields))
    return changes


# --- glyph-info.csv ---------------------------------------------------------------


def _code_points(codePoints: list[int] | None) -> str | None:
    if codePoints is None:
        return None
    return " ".join(f"U+{cp:04X}" for cp in codePoints) or "none"


def glyph_info_changes(old: bytes | None, new: bytes | None) -> list[FontChange]:
    """What changed in ``glyph-info.csv``: glyphs added and removed, code
    points, and the glyphs' other info."""
    try:
        mapA, infoA = read_glyph_info(old)
        mapB, infoB = read_glyph_info(new)
    except Unreadable as error:
        return [
            FontChange(
                "glyphInfo",
                "Glyph info: changed (the file could not be read)",
                [Field("glyph-info.csv", None, str(error))],
            )
        ]
    added = [g for g in mapB if g not in mapA]
    removed = [g for g in mapA if g not in mapB]
    codePoints = [g for g in mapB if g in mapA and mapA[g] != mapB[g]]
    info = [
        g
        for g in sorted(set(infoA) | set(infoB))
        if g in mapA and g in mapB and infoA.get(g) != infoB.get(g)
    ]
    fields: list[Field] = []
    for g in added:
        fields.append(Field(g, None, _code_points(mapB[g])))
    for g in removed:
        fields.append(Field(g, _code_points(mapA[g]), None))
    for g in codePoints:
        fields.append(Field(f"{g} code points", _code_points(mapA[g]), _code_points(mapB[g])))
    for g in info:
        fields.append(Field(f"{g} info", _text(infoA.get(g)), _text(infoB.get(g))))
    items = []
    if added:
        items.append(f"{_names(added)} added")
    if removed:
        items.append(f"{_names(removed)} removed")
    if codePoints:
        if len(codePoints) == 1:
            g = codePoints[0]
            items.append(f"{g} {_code_points(mapA[g])} → {_code_points(mapB[g])}")
        else:
            items.append(f"code points of {_names(codePoints)} changed")
    if info:
        items.append(f"info of {_names(info)} changed")
    if not items:
        return []
    return [FontChange("glyphInfo", f"Glyph info: {'; '.join(items)}", fields)]


# --- all of it ----------------------------------------------------------------------


def file_changes(
    path: str,
    old: bytes | None,
    new: bytes | None,
    sourceNames: Mapping[str, str] | None = None,
    detail: bool = False,
) -> list[FontChange]:
    """What changed in one of :data:`FONT_FILES` (``detail``: with the
    features' lines, for the detail view)."""
    if old == new:
        return []
    if path == FONT_DATA:
        return font_data_changes(old, new)
    if path == FEATURES:
        return features_changes(old, new, detail)
    if path == KERNING_FILE:
        return kerning_changes(old, new, sourceNames)
    if path == GLYPH_INFO:
        return glyph_info_changes(old, new)
    raise ValueError(f"not a font file: {path}")


def font_changes(
    old: Mapping[str, bytes | None],
    new: Mapping[str, bytes | None],
    detail: bool = False,
) -> list[FontChange]:
    """What changed beyond the glyphs between two versions of a package,
    given ``{file name: contents}`` for :data:`FONT_FILES` (a missing key or
    None: the file is absent), in the order of :data:`AREAS`. ``detail``:
    with the features' lines (never for a commit message)."""
    sourceNames = source_names(new.get(FONT_DATA) or old.get(FONT_DATA))
    changes: list[FontChange] = []
    for path in FONT_FILES:
        changes += file_changes(
            path, old.get(path), new.get(path), sourceNames, detail
        )
    return sort_changes(changes)


def _file_domains(path: str, old: bytes | None, new: bytes | None, described) -> set:
    """The write domains a change of a file beyond the glyph touches. What
    Hive cannot describe counts in the file's domain: nothing closed goes
    through unseen."""
    if old == new:
        return set()
    if path == KERNING_FILE:
        return {KERNING}
    if path == FEATURES:
        return {FONTSETTINGS}
    domains = {c.domain for c in described} - {EDIT}
    if not domains:
        try:
            same = json.loads(old or b"{}") == json.loads(new or b"{}")
        except ValueError:
            same = False
        if not same:
            domains.add(FONTSETTINGS)
    return domains


def domains_written(
    before: Mapping[str, bytes | None],
    after: Mapping[str, bytes | None],
    reference: Mapping[str, bytes | None] | None = None,
) -> tuple[list[FontChange], list[str]]:
    """What a write (a recipe's hive-write, a merge) changes beyond the
    glyph, from ``before`` to ``after`` (``{file name: contents}`` for
    :data:`BEYOND_THE_GLYPH_FILES`, None: absent), and the write domains it
    touches, in :data:`WRITE_DOMAINS` order: ``kerning.csv`` kerning,
    ``features.txt`` font settings, ``font-data.json`` the domains of its
    areas.

    ``reference``: the same files on the project's default branch. A domain
    whose state after the write is the reference's is not counted: a write
    may bring what the default branch already holds (a branch brought up to
    date), never what nobody allowed wrote there."""
    sourceNames = source_names(after.get(FONT_DATA) or before.get(FONT_DATA))

    def describe(old_files, new_files):
        changes: list[FontChange] = []
        domains: set[str] = set()
        for path in BEYOND_THE_GLYPH_FILES:
            old, new = old_files.get(path), new_files.get(path)
            if old == new:
                continue
            try:
                described = file_changes(path, old, new, sourceNames)
            except Exception:  # undescribable: still in its file's domain
                described = []
            changes += [c for c in described if c.domain != EDIT]
            domains |= _file_domains(path, old, new, described)
        return sort_changes(changes), domains

    changes, domains = describe(before, after)
    if reference is not None and domains:
        _, fromReference = describe(reference, after)
        domains &= fromReference
    return changes, [d for d in WRITE_DOMAINS if d in domains]


def sort_changes(changes: Iterable[FontChange]) -> list[FontChange]:
    order = {area: i for i, area in enumerate(AREAS)}
    return sorted(changes, key=lambda c: order[c.area])


def domains_of(changes: Iterable[FontChange]) -> list[str]:
    """The write domains beyond the glyph the changes touch, in
    :data:`WRITE_DOMAINS` order."""
    touched = {c.domain for c in changes}
    return [d for d in WRITE_DOMAINS if d in touched]


def title_words(changes: Iterable[FontChange]) -> str:
    """``axes, features``: what changed, for a commit's title."""
    words: list[str] = []
    for change in sort_changes(changes):
        word = change.words or AREAS[change.area][1]
        if change.area == "sources" and change.words is None:
            word = "sources"
        if word not in words:
            words.append(word)
    # "sources" says it all when a source changed too.
    if "sources" in words:
        words = [w for w in words if w not in ("line metrics", "font guidelines")]
    return ", ".join(words)
