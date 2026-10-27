"""Telling a test deployment from the real one at a glance.

On the development deployment of Hive (hive-api's ``install.sh --dev``), the
environment variable ``HIVE_ENVIRONMENT`` names it (``development``). Every
page Hive serves then gets a small badge in a corner and its title starts
with ``[dev]``, so that a browser tab of the test deployment is never taken
for production. Unset (production): pages are left as they are.

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import html as htmllib
import os

BADGE_ID = "hive-environment-badge"


def environmentName() -> str:
    return os.environ.get("HIVE_ENVIRONMENT", "").strip()


def markEnvironment(page: str, name: str | None = None) -> str:
    """The page with the environment's badge and title prefix, or unchanged
    when no environment is named."""
    name = environmentName() if name is None else name.strip()
    if not name:
        return page
    label = htmllib.escape(name)
    short = htmllib.escape(name[:3].lower())
    lower = page.lower()

    title = lower.find("<title>")
    if title != -1:
        cut = title + len("<title>")
        page = page[:cut] + f"[{short}] " + page[cut:]
        lower = page.lower()

    badge = (
        f'<div id="{BADGE_ID}" role="status" aria-label="{label} deployment" '
        'style="position:fixed;left:8px;bottom:8px;z-index:2147483647;'
        "pointer-events:none;padding:3px 8px;border-radius:4px;"
        "background:#7b2cbf;color:#fff;font:600 11px/1.4 system-ui,sans-serif;"
        'letter-spacing:.04em;text-transform:uppercase;opacity:.9">'
        f"{label}</div>"
    )
    body = lower.rfind("</body>")
    if body != -1:
        return page[:body] + badge + page[body:]
    return page + badge
