"""Small guards around Fontra, for versions Hive is about to move to.

Fontra's main (20 October 2026, PR 2810) reads the OpenType features of a
.ttf/.otf/.woff/.ttx by running an external program, ``sr-aef``
(``subprocess.run(["sr-aef", path], check=True)``). Where that program is
missing or fails, reading the features raises, and with it opening or
importing the font:

- in Try Fontra, Python runs in the browser (Pyodide): no program can run;
- on a server whose service does not have the venv's ``bin`` on its PATH;
- on a font that ``sr-aef`` cannot read.

:func:`guard_feature_reading` makes that step best effort: the font opens
or imports without its features (as before that change), and a warning
says why. It does nothing on a Fontra without that step (the one Hive runs
today), so it is safe on both.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_GUARDED = "_hive_guarded"


def guard_feature_reading(backend_class=None) -> bool:
    """Wrap ``OTFBackend._getFeaturesFromFont`` (or ``backend_class``'s) so
    that a failure gives no features instead of an error. True if it wrapped
    something; idempotent."""
    if backend_class is None:
        try:
            from fontra.backends.opentype import OTFBackend as backend_class
        except Exception:  # Fontra without that backend: nothing to guard
            return False
    original = getattr(backend_class, "_getFeaturesFromFont", None)
    if original is None or getattr(original, _GUARDED, False):
        return False

    def _getFeaturesFromFont(self) -> str:
        try:
            return original(self)
        except Exception as error:  # missing program, no subprocess, bad font
            logger.warning(
                "OpenType features of %s not read (%s: %s): the font opens without them",
                getattr(self, "path", "a font"),
                type(error).__name__,
                error,
            )
            return ""

    setattr(_getFeaturesFromFont, _GUARDED, True)
    _getFeaturesFromFont.__doc__ = original.__doc__
    backend_class._getFeaturesFromFont = _getFeaturesFromFont
    return True
