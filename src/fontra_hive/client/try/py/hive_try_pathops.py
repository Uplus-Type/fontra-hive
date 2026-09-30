"""Fontra Hive, "Try Fontra": Fontra's path operations in the browser.

Fontra's server does Remove overlap, Union, Subtract, Intersect and Exclude
with skia-pathops (``fontra.core.pathops``), compiled code that Pyodide does
not have. In the browser, ``fontra/core/pathops.py`` is this module instead:
the same functions, done by booleanOperations (pure Python, MIT, vendored in
``client/try/py/booleanOperations``) over pyclipper (Pyodide's own build,
loaded by the worker before the first call).

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import booleanOperations
from fontTools.pens.pointPen import GuessSmoothPointPen
from fontTools.pens.recordingPen import RecordingPointPen

from fontra.core.path import PackedPath, PackedPathPointPen


class _Contour:
    """One contour, as booleanOperations wants it: its points (len) and
    drawPoints."""

    def __init__(self, recording: RecordingPointPen):
        self.recording = recording
        self.points = [v for v in recording.value if v[0] == "addPoint"]

    def __len__(self):
        return len(self.points)

    def drawPoints(self, pen):
        self.recording.replay(pen)


def _contours(path: PackedPath) -> tuple[list[_Contour], list[RecordingPointPen]]:
    """The closed contours, and the open ones (left as they are)."""
    closed, open_ = [], []

    class Splitter:
        def beginPath(self, **kwargs):
            self.current = RecordingPointPen()
            self.current.beginPath(**kwargs)

        def addPoint(self, *args, **kwargs):
            self.current.addPoint(*args, **kwargs)

        def endPath(self):
            self.current.endPath()
            points = [v for v in self.current.value if v[0] == "addPoint"]
            isOpen = bool(points) and points[0][1][1] == "move"
            (open_ if isOpen else closed).append(self.current)

        def addComponent(self, *args, **kwargs):
            pass

    path.drawPoints(Splitter())
    return [_Contour(c) for c in closed], open_


def _run(operation, pathA: PackedPath, pathB: PackedPath | None) -> PackedPath:
    contoursA, openA = _contours(pathA)
    pen = PackedPathPointPen()
    smooth = GuessSmoothPointPen(pen)
    if pathB is None:
        booleanOperations.union(contoursA, smooth)
        rest = openA
    else:
        contoursB, openB = _contours(pathB)
        operation(contoursA, contoursB, smooth)
        rest = openA + openB
    for recording in rest:
        recording.replay(pen)
    return pen.getPath()


def unionPath(path: PackedPath) -> PackedPath:
    return _run(None, path, None)


def subtractPath(pathA: PackedPath, pathB: PackedPath) -> PackedPath:
    return _run(booleanOperations.difference, pathA, pathB)


def intersectPath(pathA: PackedPath, pathB: PackedPath) -> PackedPath:
    return _run(booleanOperations.intersection, pathA, pathB)


def excludePath(pathA: PackedPath, pathB: PackedPath) -> PackedPath:
    return _run(booleanOperations.xor, pathA, pathB)
