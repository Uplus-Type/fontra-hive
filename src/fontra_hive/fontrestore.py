"""Bring the font, or a part of it, back to an older version, as a new commit.

Two kinds of restoration (``POST …/restore-font``, in
:mod:`fontra_hive.projectmanager`):

- **the whole font**: the tree of the ``.fontra`` package as it was at the
  older version (:func:`is_font_path`): the glyphs added since go, the
  glyphs deleted since come back. The other files of the repository
  (``workflows/``, a README…) are not touched. For the managers (``merge``);
- **some parts** (:data:`PARTS`): each part replaces only its own keys of
  ``font-data.json``, or its own file, and keeps the rest of the branch head.
  For whoever has the capability of the part's domain
  (:mod:`fontra_hive.writedomains`).

History is never rewritten: what is computed here is the set of file
changes of one new commit on top of the branch. ``font-data.json`` is read
and written as Fontra reads and writes it (``structure``/``unstructure`` of
``fontra.core.classes.Font``, ``FontraBackend._writeFontData``), and so is
``glyph-info.csv`` (``FontraBackend._writeGlyphInfo``): the bytes committed
are those the editor would write.

:func:`coherence_problem` is the guard for the axes and the sources: after
the restoration every glyph source must still name a font source that
exists (``locationBase``), and every location must stay on the axes, within
their bounds. A font source without a drawing in a glyph is normal (sparse
sources): it is never refused.

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
from dataclasses import dataclass
from typing import Iterable

from . import fontdiff
from .fontdiff import (
    AREAS,
    FEATURES,
    FONT_DATA,
    FONT_FILES,
    GLYPH_INFO,
    KERNING_FILE,
    STATUS_DEFINITIONS_KEY,
)
from .gitstore import glyphs_trailer, one_line
from .writedomains import EDIT, LABELS, PLUGINDATA, customDataDomain


@dataclass(frozen=True)
class Part:
    key: str
    label: str  # "Axes and sources": the button, the refusal
    words: str  # "axes and sources": the commit title, the result
    areas: tuple[str, ...]  # fontdiff's areas it brings back

    @property
    def domain(self) -> str:
        return AREAS[self.areas[0]][0]


# The parts that can be restored alone, in the order they are told. The
# axes, their mappings and the sources go together: apart, the sources'
# locations would no longer match the axes.
PARTS: dict[str, Part] = {
    part.key: part
    for part in (
        Part("fontInfo", "Font info", "font info", ("fontInfo",)),
        Part(
            "axesAndSources",
            "Axes and sources",
            "axes and sources",
            ("axes", "mappings", "sources"),
        ),
        Part(
            "features",
            "Features",
            "features",
            ("features", "conditionalSubstitutions"),
        ),
        Part(
            "statusDefinitions",
            "Development status",
            "development status",
            ("statusDefinitions",),
        ),
        Part("kerning", "Kerning", "kerning", ("kerning",)),
        Part("pluginData", "Plug-in data", "plug-in data", ("pluginData",)),
        Part("glyphInfo", "Glyph info", "glyph info", ("glyphInfo",)),
    )
}
# Every part is in one domain (checked by the tests): its capability.
assert all(len({AREAS[a][0] for a in p.areas}) == 1 for p in PARTS.values())

# The parts whose restoration may leave glyphs without their axes or sources.
GUARDED_PARTS = frozenset({"axesAndSources"})

# How many glyphs the Hive-Glyphs trailer of a restoration lists at most
# (more: no trailer, the glyph history reads the trees).
MAX_TRAILER_GLYPHS = 200

WHOLE_FONT_CAPABILITY = "merge"
WHOLE_FONT_REFUSAL = "Restoring the whole font is for the managers of this project."


class UnknownPart(ValueError):
    pass


def parse_parts(text: str) -> list[str]:
    """``"features,kerning"`` -> the parts, in :data:`PARTS` order."""
    keys = {k.strip() for k in text.split(",") if k.strip()}
    if not keys:
        raise UnknownPart("Choose what to restore.")
    unknown = sorted(keys - set(PARTS))
    if unknown:
        raise UnknownPart(f"Nothing called {unknown[0]!r} can be restored.")
    return [key for key in PARTS if key in keys]


def part_refusal(key: str) -> str:
    """Why someone without the capability of a part cannot restore it."""
    part = PARTS[key]
    if part.domain == EDIT:
        return f"You cannot restore {part.label}: your role cannot change this font."
    verb = "are" if part.domain == "fontsettings" else "is"
    return (
        f"You cannot restore {part.label}: {LABELS[part.domain].lower()} {verb} "
        "kept to some people on this project. Ask a manager."
    )


def is_font_path(path: str) -> bool:
    """A file of the ``.fontra`` package (what "the whole font" is)."""
    return (
        path in FONT_FILES
        or path.startswith("glyphs/")
        or path.startswith("background-images/")
    )


def is_glyph_path(path: str) -> bool:
    return path.startswith("glyphs/") and path.endswith(".json")


def glyph_name(path: str) -> str:
    from fontra.backends.filenames import fileNameToString

    return fileNameToString(path[len("glyphs/") : -len(".json")])


# --- reading and writing as Fontra does ----------------------------------------------


def write_font_data(font) -> bytes:
    """``font-data.json`` as ``FontraBackend._writeFontData`` writes it."""
    from fontra.backends.fontra import serialize
    from fontra.core.classes import unstructure

    data = unstructure(font)
    for key in ("glyphs", "glyphMap", "glyphInfos", "kerning"):
        data.pop(key, None)
    if "features" in data and data["features"].get("language", "fea") == "fea":
        del data["features"]  # the default feature format is not written
    return (serialize(data) + "\n").encode("utf-8")


def write_glyph_info(glyphMap: dict, glyphInfos: dict) -> bytes:
    """``glyph-info.csv`` as ``FontraBackend._writeGlyphInfo`` writes it."""
    infoKeys: set[str] = set()
    for info in glyphInfos.values():
        infoKeys.update(info)
    infoKeyList = sorted(infoKeys)
    file = io.StringIO(newline="")
    writer = csv.writer(file, delimiter=";")
    writer.writerow(["glyph name", "code points"] + infoKeyList)
    for name, codePoints in sorted(glyphMap.items()):
        row = [name, ",".join(f"U+{cp:04X}" for cp in codePoints)]
        info = glyphInfos.get(name)
        if info:
            for key in infoKeyList:
                value = info.get(key)
                row.append(
                    ""
                    if value is None
                    else value
                    if isinstance(value, str)
                    else json.dumps(value, separators=(",", ":"), ensure_ascii=False)
                )
        while len(row) > 2 and not row[-1]:
            del row[-1]
        writer.writerow(row)
    return file.getvalue().encode("utf-8")


# --- what a restoration changes ----------------------------------------------------


class Tree:
    """The files of one commit, read on demand."""

    def __init__(self, store, sha: str):
        self.store = store
        self.sha = sha
        self.files = store.list_tree(sha)  # path -> blob sha

    def read(self, path: str) -> bytes | None:
        blob = self.files.get(path)
        return self.store.read_blob(blob) if blob else None


def whole_font_changes(head: Tree, old: Tree) -> dict[str, bytes | None]:
    """The tree of the font as it was at ``old``; other files untouched."""
    changes: dict[str, bytes | None] = {}
    for path in head.files:
        if is_font_path(path) and path not in old.files:
            changes[path] = None
    for path, blob in old.files.items():
        if is_font_path(path) and head.files.get(path) != blob:
            changes[path] = old.read(path)
    return changes


def _set_custom_data(font, old, keep) -> None:
    """The font's custom data keys for which ``keep(key)`` is true, as in
    ``old``; the others as they are."""
    customData = {k: v for k, v in font.customData.items() if not keep(k)}
    customData.update({k: v for k, v in old.customData.items() if keep(k)})
    font.customData = customData


def _font_data_changes(head: Tree, old: Tree, parts: set[str]) -> bytes | None:
    """The new ``font-data.json`` (None: unchanged)."""
    font = fontdiff.read_font_data(head.read(FONT_DATA))
    before = fontdiff.read_font_data(head.read(FONT_DATA))
    then = fontdiff.read_font_data(old.read(FONT_DATA))
    if "fontInfo" in parts:
        font.fontInfo = then.fontInfo
        font.unitsPerEm = then.unitsPerEm
    if "axesAndSources" in parts:
        font.axes = then.axes
        font.sources = then.sources
    if "features" in parts:
        # The text is features.txt; font-data.json has the language and
        # the custom data of the features.
        font.features = then.features
        font.conditionalSubstitutions = then.conditionalSubstitutions
    if "statusDefinitions" in parts:
        _set_custom_data(font, then, lambda key: key == STATUS_DEFINITIONS_KEY)
    if "pluginData" in parts:
        _set_custom_data(font, then, lambda key: customDataDomain(key) == PLUGINDATA)
    if font == before:
        return None
    return write_font_data(font)


def _glyph_info_changes(head: Tree, old: Tree) -> bytes | None:
    """The new ``glyph-info.csv``: the code points and the infos of the
    glyphs that exist now, as they were at ``old``. A glyph deleted since is
    not brought back (it would have no drawing); one made since keeps its row."""
    headMap, headInfos = fontdiff.read_glyph_info(head.read(GLYPH_INFO))
    oldMap, oldInfos = fontdiff.read_glyph_info(old.read(GLYPH_INFO))
    glyphMap: dict[str, list[int]] = {}
    glyphInfos: dict[str, dict] = {}
    for name, codePoints in headMap.items():
        if name in oldMap:
            glyphMap[name] = oldMap[name]
            info = oldInfos.get(name)
        else:
            glyphMap[name] = codePoints
            info = headInfos.get(name)
        if info:
            glyphInfos[name] = info
    if glyphMap == headMap and glyphInfos == headInfos:
        return None
    return write_glyph_info(glyphMap, glyphInfos)


def part_changes(head: Tree, old: Tree, parts: Iterable[str]) -> dict[str, bytes | None]:
    """The files of the parts, as they were at ``old``; the rest of ``head``
    as it is."""
    parts = set(parts)
    changes: dict[str, bytes | None] = {}
    if parts & {"fontInfo", "axesAndSources", "features", "statusDefinitions", "pluginData"}:
        data = _font_data_changes(head, old, parts)
        if data is not None:
            changes[FONT_DATA] = data

    def whole_file(path):
        if head.files.get(path) != old.files.get(path):
            changes[path] = old.read(path)  # None: absent then, deleted now

    if "features" in parts:
        whole_file(FEATURES)
    if "kerning" in parts:
        whole_file(KERNING_FILE)
    if "glyphInfo" in parts:
        data = _glyph_info_changes(head, old)
        if data is not None:
            changes[GLYPH_INFO] = data
    return changes


def restore_changes(store, head: str, old: str, parts: list[str] | None):
    """``(changes, head tree, old tree)`` of restoring ``parts`` (None: the
    whole font) from commit ``old`` on top of commit ``head``."""
    headTree, oldTree = Tree(store, head), Tree(store, old)
    if parts is None:
        changes = whole_font_changes(headTree, oldTree)
    else:
        changes = part_changes(headTree, oldTree, parts)
    return changes, headTree, oldTree


# --- the guard ------------------------------------------------------------------------


def _design_bounds(axis):
    """``(minimum, maximum, values)`` of a font axis in the sources'
    coordinates (through its mapping); ``values`` for a discrete axis."""
    from fontTools.varLib.models import piecewiseLinearMap

    mapping = {user: source for user, source in axis.mapping} if axis.mapping else None

    def mapped(value):
        return piecewiseLinearMap(value, mapping) if mapping else value

    if hasattr(axis, "values"):
        values = [mapped(v) for v in axis.values]
        return min(values), max(values), values
    return mapped(axis.minValue), mapped(axis.maxValue), None


def _on_axis(value, bounds, tolerance=1e-6) -> bool:
    low, high, values = bounds
    if not isinstance(value, (int, float)):
        return False
    if values is not None:
        return any(abs(value - v) <= tolerance for v in values)
    return low - tolerance <= value <= high + tolerance


def _range_text(axis) -> str:
    return fontdiff._axis_range(axis)


def _glyphs_text(n: int) -> str:
    return "1 glyph has" if n == 1 else f"{n} glyphs have"


def _names_text(names: list[str]) -> str:
    names = sorted(names)
    if len(names) == 1:
        return names[0]
    shown = names[:3]
    rest = len(names) - len(shown)
    if rest:
        return ", ".join(shown) + f" and {rest} more"
    return ", ".join(shown[:-1]) + " and " + shown[-1]


def coherence_problem(
    head: Tree,
    old: Tree,
    changes: dict[str, bytes | None],
    *,
    wholeFont: bool,
) -> str | None:
    """Why the font after the restoration would not hold together (None:
    it does): glyph sources naming a font source that this version doesn't
    have, or locations off its axes."""
    def after(path):
        if path in changes:
            return changes[path]
        return head.read(path)

    try:
        font = fontdiff.read_font_data(after(FONT_DATA))
    except fontdiff.Unreadable:
        return "The font data of this version cannot be read."
    # The names of the sources the glyphs may name, now or then.
    names: dict[str, str] = {}
    for tree in (old, head):
        try:
            names.update(
                (key, source.name)
                for key, source in fontdiff.read_font_data(
                    tree.read(FONT_DATA)
                ).sources.items()
            )
        except fontdiff.Unreadable:
            pass
    sources = set(font.sources)
    axes = {axis.name: axis for axis in font.axes.axes}
    bounds = {name: _design_bounds(axis) for name, axis in axes.items()}

    missingSource: dict[str, set[str]] = {}
    missingAxis: dict[str, set[str]] = {}
    offAxis: dict[str, set[str]] = {}

    def check_location(location, owner, localAxes=()):
        for axisName, value in (location or {}).items():
            if axisName in localAxes:
                continue
            if axisName not in bounds:
                missingAxis.setdefault(axisName, set()).add(owner)
            elif not _on_axis(value, bounds[axisName]):
                offAxis.setdefault(axisName, set()).add(owner)

    paths = set(head.files) - {p for p, d in changes.items() if d is None}
    paths |= {p for p, d in changes.items() if d is not None}
    for path in sorted(p for p in paths if is_glyph_path(p)):
        data = after(path)
        try:
            glyph = json.loads(data)
        except (TypeError, ValueError):
            continue  # not ours to judge: Fontra says it when it reads it
        name = glyph.get("name") or glyph_name(path)
        localAxes = {a.get("name") for a in glyph.get("axes") or [] if isinstance(a, dict)}
        for source in glyph.get("sources") or []:
            if not isinstance(source, dict):
                continue
            base = source.get("locationBase")
            if base and base not in sources:
                missingSource.setdefault(base, set()).add(name)
            check_location(source.get("location"), name, localAxes)
    for source in font.sources.values():
        check_location(source.location, f"source:{source.name}")

    advice = "" if wholeFont else " Restore the whole font instead."
    if missingSource:
        glyphs = set().union(*missingSource.values())
        sourceNames = [names.get(key, key) for key in missingSource]
        which = "the source" if len(sourceNames) == 1 else "the sources"
        return (
            f"{_glyphs_text(len(glyphs))} layers for {which} "
            f"{_names_text(sourceNames)}, which this version doesn't have.{advice}"
        )
    if missingAxis:
        glyphs = {g for g in set().union(*missingAxis.values()) if not g.startswith("source:")}
        axisNames = list(missingAxis)
        which = "the axis" if len(axisNames) == 1 else "the axes"
        who = _glyphs_text(len(glyphs)) + " sources" if glyphs else "Some font sources are"
        return (
            f"{who} on {which} {_names_text(axisNames)}, which this version "
            f"doesn't have.{advice}"
        )
    if offAxis:
        glyphs = {g for g in set().union(*offAxis.values()) if not g.startswith("source:")}
        axisName = sorted(offAxis)[0]
        axis = axes[axisName]
        label = axis.tag or axis.name
        who = _glyphs_text(len(glyphs)) + " sources" if glyphs else "Some font sources are"
        return (
            f"{who} outside the {label} axis of this version ({_range_text(axis)})."
            f"{advice}"
        )
    return None


# --- the commit -----------------------------------------------------------------------


def ref_label(store, sha: str, tzOffset: int | None = None) -> str:
    """``“Round 1”`` for a snapshot; otherwise the version's date,
    ``the version of 23 Nov 2026, 09:50 UTC``, or in the person's time
    when ``tzOffset`` is given (minutes, as JavaScript's
    ``getTimezoneOffset``: UTC minus local time) and then without "UTC"."""
    snapshot = store.snapshot_at(sha)
    if snapshot is not None:
        return f"“{snapshot.title}”"
    when = datetime.datetime.fromtimestamp(
        store.commit_info(sha).time, datetime.timezone.utc
    )
    if tzOffset is not None:
        when -= datetime.timedelta(minutes=tzOffset)
    text = f"{when.day} {when:%b %Y, %H:%M}"
    return f"the version of {text}" + ("" if tzOffset is not None else " UTC")


def parse_tz_offset(text: str | None) -> int | None:
    """The browser's ``getTimezoneOffset()`` (None: absent or not one)."""
    try:
        value = int(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if -900 <= value <= 900 else None


def _words(parts: list[str]) -> str:
    return ", ".join(PARTS[key].words for key in parts)


def title(parts: list[str] | None, label: str) -> str:
    if parts is None:
        return f"Restore the whole font to {label}"
    return f"Restore {_words(parts)} from {label}"


def result_sentence(parts: list[str] | None, label: str, changed: bool) -> str:
    """What the person reads once it is done."""
    if parts is None:
        what, are = "The whole font", "is"
    else:
        names = [PARTS[key].words for key in parts]
        what = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        what = what[0].upper() + what[1:]
        are = "is" if len(names) == 1 and names[0] not in ("axes and sources", "features") else "are"
    if not changed:
        return f"{what} {are} already as at {label}: nothing to restore."
    return f"{what} {are} back as at {label}, in a new version. Nothing was deleted from the history."


def commit_message(
    store,
    head: Tree,
    old: Tree,
    parts: list[str] | None,
    changes: dict[str, bytes | None],
    maxBodyLines: int = 12,
) -> tuple[str, list]:
    """The message of the restoration, and its changes beyond the glyph
    (fontdiff), for the answer."""
    label = ref_label(store, old.sha)
    before = {path: head.read(path) for path in FONT_FILES}
    afterFiles = {
        path: changes[path] if path in changes else before[path] for path in FONT_FILES
    }
    try:
        summary = fontdiff.font_changes(before, afterFiles)
    except Exception:  # never lose a restoration over its description
        summary = []
    body = [one_line(c.summary) for c in summary[:maxBodyLines]]
    if len(summary) > maxBodyLines:
        body.append(f"… and {len(summary) - maxBodyLines} more")
    glyphPaths = [p for p in changes if is_glyph_path(p)]
    if glyphPaths:
        back = sum(1 for p in glyphPaths if p not in head.files)
        gone = sum(1 for p in glyphPaths if changes[p] is None)
        edited = len(glyphPaths) - back - gone
        said = []
        if edited:
            said.append(f"{edited} changed")
        if back:
            said.append(f"{back} brought back")
        if gone:
            said.append(f"{gone} removed")
        body.append("Glyphs: " + ", ".join(said))
    trailers = [f"Hive-Restore: {old.sha}"]
    domains = fontdiff.domains_of(summary)
    if domains:
        trailers.append(f"Hive-Font: {' '.join(domains)}")
    if len(glyphPaths) <= MAX_TRAILER_GLYPHS:
        line = glyphs_trailer(sorted(glyph_name(p) for p in glyphPaths))
        if line is not None:
            trailers.append(line)
    message = one_line(title(parts, label))
    if body:
        message += "\n\n" + "\n".join(body)
    message += "\n\n" + "\n".join(trailers) + "\n"
    return message, summary
