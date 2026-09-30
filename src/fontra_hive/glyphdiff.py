"""Which sources of a glyph a change touched, from two versions of its
``.fontra`` glyph file (JSON: ``sources`` with their ``layerName``, and
``layers``).

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import json
from typing import Any


def _load(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    try:
        glyph = json.loads(data)
    except ValueError:
        return None
    if not isinstance(glyph, dict) or not (glyph.get("sources") or glyph.get("layers")):
        return None  # not a glyph (yet): like a new one
    return glyph


def _sources(glyph: dict[str, Any]) -> list[dict[str, Any]]:
    sources = glyph.get("sources")
    return (
        [s for s in sources if isinstance(s, dict)] if isinstance(sources, list) else []
    )


def _layers(glyph: dict[str, Any]) -> dict[str, Any]:
    layers = glyph.get("layers")
    return layers if isinstance(layers, dict) else {}


def changed_sources(old: bytes | None, new: bytes | None) -> list[str]:
    """The names of the sources whose outline, metrics or definition differ
    between two versions of a glyph file, in the order of the new version
    (removed ones last); a changed layer that no source uses (a background
    layer, say) is named by its layer name. Empty for a new or deleted glyph,
    or unreadable files."""
    before, after = _load(old), _load(new)
    if before is None or after is None:
        return []
    oldLayers, newLayers = _layers(before), _layers(after)
    changedLayers = {
        name
        for name in set(oldLayers) | set(newLayers)
        if oldLayers.get(name) != newLayers.get(name)
    }
    oldSources = {s.get("name"): s for s in _sources(before)}
    result: list[str] = []
    used: set[str] = set()
    for source in _sources(after):
        name, layer = source.get("name"), source.get("layerName")
        used.add(layer)
        if layer in changedLayers or oldSources.get(name) != source:
            result.append(str(name))
    newNames = {s.get("name") for s in _sources(after)}
    for name, source in oldSources.items():
        used.add(source.get("layerName"))
        if name not in newNames:
            result.append(str(name))
    result += sorted(str(layer) for layer in changedLayers - used)
    return list(dict.fromkeys(result))
