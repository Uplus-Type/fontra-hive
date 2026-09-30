"""Three-way merge of two versions of a ``.fontra`` package.

A branch is merged into another by comparing, file by file, the state where
they parted (the merge base) with each side. A file changed on one side only
is taken from that side. A file changed on both sides is merged by what it
contains, so that two people working on different parts of it do not get in
each other's way:

- a glyph (``glyphs/*.json``): each layer and each source (by layer name)
  on its own, and the glyph's other keys one by one. Two designers editing
  two different masters of the same glyph merge without a question; a
  conflict is the same layer (or source) changed differently on both sides;
- ``glyph-info.csv``: one row (glyph) at a time;
- ``kerning.csv``: one pair, or one group, at a time;
- ``font-data.json``: key by key, down through nested objects (lists are
  taken whole);
- anything else (``features.txt``, background images): the whole file.

A conflict is resolved by choosing a side for the file ("ours": the branch
merged into, "theirs": the branch merged from): that side wins on the parts
in conflict, and what merged cleanly stays merged.

Pure functions over bytes; :mod:`fontra_hive.gitstore` provides the trees.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

MISSING = object()  # a key or file that is not there

GLYPH_INFO_FILE = "glyph-info.csv"
KERNING_FILE = "kerning.csv"
FONT_DATA_FILE = "font-data.json"


@dataclass
class Conflict:
    path: str
    kind: str  # "glyph", "glyph-info", "kerning", "font-data", "file"
    glyph: str | None = None
    # What is in conflict, in words ("layer Bold/foreground", "kern A V")
    parts: list[str] = field(default_factory=list)
    # A side deleted the file while the other changed it: "ours" / "theirs"
    deleted: str | None = None

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "kind": self.kind,
            "glyph": self.glyph,
            "parts": self.parts,
            "deleted": self.deleted,
        }


@dataclass
class MergeResult:
    # path -> new content (None: delete), relative to "ours"
    changes: dict[str, bytes | None] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    # Files changed on one side only, and on both sides but merged cleanly
    theirs_only: list[str] = field(default_factory=list)
    ours_only: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    # Conflicts settled by the resolutions given
    resolved: list[Conflict] = field(default_factory=list)


class _Conflicts:
    """Collects the paths (inside one file) where both sides disagree."""

    def __init__(self, prefer: str | None):
        self.prefer = prefer
        self.paths: list[tuple] = []


def merge_values(base, ours, theirs, path: tuple, atomic, conflicts: _Conflicts):
    """Three-way merge of JSON-like values. Dicts are merged key by key
    unless ``atomic(path)``; anything else is taken whole. MISSING stands for
    an absent key. On a conflict the preferred side wins (ours if none)."""
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if (
        isinstance(ours, dict)
        and isinstance(theirs, dict)
        and (base is MISSING or isinstance(base, dict))
        and not atomic(path)
    ):
        b = base if isinstance(base, dict) else {}
        result = {}
        for key in list(ours) + [k for k in theirs if k not in ours]:
            value = merge_values(
                b.get(key, MISSING),
                ours.get(key, MISSING),
                theirs.get(key, MISSING),
                path + (key,),
                atomic,
                conflicts,
            )
            if value is not MISSING:
                result[key] = value
        return result
    conflicts.paths.append(path)
    return theirs if conflicts.prefer == "theirs" else ours


# --- glyphs --------------------------------------------------------------------


def _sources_by_layer(glyph: dict):
    """The glyph's sources as an ordered dict keyed by layer name, or None
    when that is ambiguous (then the list is merged whole)."""
    sources = glyph.get("sources")
    if not isinstance(sources, list):
        return None
    keyed = {}
    for source in sources:
        if not isinstance(source, dict):
            return None
        key = source.get("layerName") or source.get("name")
        if not isinstance(key, str) or key in keyed:
            return None
        keyed[key] = source
    return keyed


def _glyphs_for_merge(*glyphs):
    """The glyphs with their sources keyed by layer name (plus a marker), if
    that works for all of them; else as they are (sources merged whole)."""
    keyed = [_sources_by_layer(g) if isinstance(g, dict) else {} for g in glyphs]
    if any(k is None for k in keyed):
        return glyphs
    return tuple(
        {**g, "sources": k, "\0keyed": True} if isinstance(g, dict) else g
        for g, k in zip(glyphs, keyed)
    )


def _glyph_atomic(path: tuple) -> bool:
    if len(path) == 1:
        return path[0] not in ("sources", "layers")
    return len(path) >= 2


def _describe_glyph_path(path: tuple, merged: dict) -> str:
    if path[0] == "layers" and len(path) > 1:
        return f"layer {path[1]}"
    if path[0] == "sources" and len(path) > 1:
        source = (merged.get("sources") or {}).get(path[1])
        name = source.get("name") if isinstance(source, dict) else None
        return f"source {name or path[1]}"
    return str(path[0])


def merge_glyph(base: bytes | None, ours: bytes, theirs: bytes, prefer=None):
    """-> (merged bytes, parts in conflict)."""
    b, o, t = _glyphs_for_merge(
        json.loads(base) if base is not None else MISSING,
        json.loads(ours),
        json.loads(theirs),
    )
    conflicts = _Conflicts(prefer)
    merged = merge_values(b, o, t, (), _glyph_atomic, conflicts)
    parts = [_describe_glyph_path(p, merged) for p in conflicts.paths]
    if merged.pop("\0keyed", False):
        merged["sources"] = list(merged["sources"].values())
    return _dump_json(merged), parts


def _dump_json(data) -> bytes:
    # The same serialisation as Fontra's .fontra backend.
    return (json.dumps(data, indent=0, ensure_ascii=False) + "\n").encode("utf-8")


def glyph_name_of(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        name = json.loads(data).get("name")
    except (ValueError, AttributeError):
        return None
    return name if isinstance(name, str) else None


# --- glyph-info.csv ----------------------------------------------------------------


def _read_glyph_info(data: bytes | None) -> dict:
    if data is None:
        return {}
    rows = list(csv.reader(io.StringIO(data.decode("utf-8")), delimiter=";"))
    if not rows:
        return {}
    header = rows[0]
    result = {}
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        result[row[0]] = {
            key: value for key, value in zip(header[1:], row[1:]) if value != ""
        }
    return result


def _write_glyph_info(rows: dict) -> bytes:
    infoKeys = sorted({k for row in rows.values() for k in row} - {"code points"})
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(["glyph name", "code points"] + infoKeys)
    for name in sorted(rows):
        row = [name, rows[name].get("code points", "")]
        row += [rows[name].get(key, "") for key in infoKeys]
        while len(row) > 2 and not row[-1]:
            del row[-1]
        writer.writerow(row)
    return out.getvalue().encode("utf-8")


def merge_glyph_info(base, ours, theirs, prefer=None):
    conflicts = _Conflicts(prefer)
    merged = merge_values(
        _read_glyph_info(base),
        _read_glyph_info(ours),
        _read_glyph_info(theirs),
        (),
        lambda path: len(path) >= 1,  # one row at a time
        conflicts,
    )
    return _write_glyph_info(merged), [f"glyph {p[0]}" for p in conflicts.paths]


# --- kerning.csv ------------------------------------------------------------------


def _read_kerning(data: bytes | None) -> dict:
    if data is None:
        return {}
    from fontra.backends.fontra import readKerningFile

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / KERNING_FILE
        path.write_bytes(data)
        kerning = readKerningFile(path)
    result = {}
    for kernType, table in kerning.items():
        values = {}
        for left, rights in table.values.items():
            for right, row in rights.items():
                values.setdefault(left, {})[right] = {
                    source: value
                    for source, value in zip(table.sourceIdentifiers, row)
                    if value is not None
                }
        result[kernType] = {
            "groups1": dict(table.groupsSide1),
            "groups2": dict(table.groupsSide2),
            "sources": list(table.sourceIdentifiers),
            "values": values,
        }
    return result


def _write_kerning(data: dict) -> bytes | None:
    from fontra.backends.fontra import writeKerningFile
    from fontra.core.classes import Kerning

    kerning = {}
    for kernType, table in data.items():
        sources = list(table.get("sources") or [])
        for rights in table.get("values", {}).values():
            for row in rights.values():
                sources += [s for s in row if s not in sources]
        kerning[kernType] = Kerning(
            groupsSide1=table.get("groups1", {}),
            groupsSide2=table.get("groups2", {}),
            sourceIdentifiers=sources,
            values={
                left: {
                    right: [row.get(s) for s in sources]
                    for right, row in rights.items()
                }
                for left, rights in table.get("values", {}).items()
            },
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / KERNING_FILE
        writeKerningFile(path, kerning)
        return path.read_bytes() if path.exists() else b""


def _kerning_atomic(path: tuple) -> bool:
    # kernType / groups1|groups2 / name, kernType / values / left / right
    if len(path) >= 2 and path[1] == "sources":
        return True
    if len(path) >= 3 and path[1] in ("groups1", "groups2"):
        return True
    return len(path) >= 4


def _describe_kerning_path(path: tuple) -> str:
    if len(path) >= 4 and path[1] == "values":
        return f"kern {path[2]} {path[3]}"
    if len(path) >= 3:
        return f"group {path[2]}"
    return "kerning " + " ".join(str(p) for p in path)


def merge_kerning(base, ours, theirs, prefer=None):
    conflicts = _Conflicts(prefer)
    b = _read_kerning(base)
    o = _read_kerning(ours)
    t = _read_kerning(theirs)
    # The list of sources is informative: merged as the union, never a conflict.
    for kernType in set(o) & set(t):
        union = o[kernType]["sources"] + [
            s for s in t[kernType]["sources"] if s not in o[kernType]["sources"]
        ]
        o[kernType]["sources"] = t[kernType]["sources"] = union
        if kernType in b:
            b[kernType]["sources"] = union
    merged = merge_values(b, o, t, (), _kerning_atomic, conflicts)
    parts = [_describe_kerning_path(p) for p in conflicts.paths]
    return _write_kerning(merged), parts


# --- font-data.json -------------------------------------------------------------


def merge_font_data(base, ours, theirs, prefer=None):
    conflicts = _Conflicts(prefer)
    merged = merge_values(
        json.loads(base) if base is not None else MISSING,
        json.loads(ours),
        json.loads(theirs),
        (),
        lambda path: False,
        conflicts,
    )
    parts = [
        " › ".join(str(p) for p in path) or "everything" for path in conflicts.paths
    ]
    return _dump_json(merged), parts


# --- whole packages ----------------------------------------------------------------


def _kind_of(path: str) -> str:
    if path.startswith("glyphs/") and path.endswith(".json"):
        return "glyph"
    return {
        GLYPH_INFO_FILE: "glyph-info",
        KERNING_FILE: "kerning",
        FONT_DATA_FILE: "font-data",
    }.get(path, "file")


_MERGERS = {
    "glyph": merge_glyph,
    "glyph-info": merge_glyph_info,
    "kerning": merge_kerning,
    "font-data": merge_font_data,
}


def merge_trees(
    base: Mapping[str, str],
    ours: Mapping[str, str],
    theirs: Mapping[str, str],
    read: Callable[[str], bytes],
    resolutions: Mapping[str, str] | None = None,
) -> MergeResult:
    """Merge two trees (``path -> blob id``) against their base. ``read``
    gives a blob's bytes. ``resolutions``: ``path -> "ours" | "theirs"`` for
    files in conflict; a conflict without one is reported in ``conflicts``
    (and ``changes`` is then not to be committed)."""
    resolutions = resolutions or {}
    result = MergeResult()
    for path in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(path), ours.get(path), theirs.get(path)
        if o == t or t == b:
            if o != b:
                result.ours_only.append(path)
            continue
        if o == b:
            result.theirs_only.append(path)
            result.changes[path] = read(t) if t is not None else None
            continue
        # Changed on both sides.
        kind = _kind_of(path)
        prefer = resolutions.get(path)
        bData = read(b) if b is not None else None
        oData = read(o) if o is not None else None
        tData = read(t) if t is not None else None
        glyph = (
            glyph_name_of(oData) or glyph_name_of(tData) or glyph_name_of(bData)
            if kind == "glyph"
            else None
        )
        if o is None or t is None:
            conflict = Conflict(
                path,
                kind,
                glyph,
                ["deleted"],
                deleted="ours" if o is None else "theirs",
            )
            merged = tData if prefer == "theirs" else oData
        elif kind in _MERGERS:
            try:
                merged, parts = _MERGERS[kind](bData, oData, tData, prefer)
            except Exception:  # unreadable on one side: the whole file
                merged, parts = (tData if prefer == "theirs" else oData), ["file"]
            conflict = Conflict(path, kind, glyph, parts) if parts else None
        else:
            conflict = Conflict(path, kind, glyph, ["file"])
            merged = tData if prefer == "theirs" else oData
        if conflict is not None:
            if prefer in ("ours", "theirs"):
                result.resolved.append(conflict)
            else:
                result.conflicts.append(conflict)
        else:
            result.merged.append(path)
        if merged != oData:
            result.changes[path] = merged
    return result


def glyph_names(paths: Iterable[str], contents: Callable[[str], bytes | None]):
    """The glyph names of glyph files (read from the files themselves)."""
    names = []
    for path in paths:
        if _kind_of(path) == "glyph":
            name = glyph_name_of(contents(path))
            if name:
                names.append(name)
    return names
