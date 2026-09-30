"""A pure-Python openstep_plist, for Pyodide ("Try Fontra" in the browser).

The real openstep_plist (https://github.com/fonttools/openstep-plist, MIT) is
written in Cython, so it cannot run in the browser without being compiled
for it. This is a port of its parser (``load``, ``loads``, ``ParseError``),
line by line where it matters, with regular expressions for speed: enough
for glyphsLib and fontra-glyphs to read .glyphs and .glyphspackage files.
Writing (``dump``, ``dumps``) is not available here.

Port: Copyright (c) 2026 Jérémie Hornus / U+Type. openstep_plist:
Copyright 2018 The FontTools Organization, MIT licence (LICENSE).
"""

from .parser import ParseError, load, loads


def dump(*args, **kwargs):
    raise NotImplementedError("writing OpenStep plists is not available here")


def dumps(*args, **kwargs):
    raise NotImplementedError("writing OpenStep plists is not available here")


__version__ = "0.5.0+hive-pure-python"
__all__ = ["load", "loads", "dump", "dumps", "ParseError"]
