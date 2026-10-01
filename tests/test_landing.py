"""The page at / for a visitor who is not signed in: the deployment's landing
($HIVE_LANDING_FILE) or the plug-in's plain sign-in page."""

from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")
from fontra_hive.hivemanager import HiveProjectManager  # noqa: E402


def test_a_landing_file_replaces_the_sign_in_page(tmp_path, monkeypatch):
    manager = SimpleNamespace(accountFile=lambda name: f"<{name}>")
    monkeypatch.delenv("HIVE_LANDING_FILE", raising=False)
    assert HiveProjectManager.landingPage(manager) == "<login.html>"
    landing = tmp_path / "landing.html"
    landing.write_text("<p>Fontra for Teams</p>", encoding="utf-8")
    monkeypatch.setenv("HIVE_LANDING_FILE", str(landing))
    assert HiveProjectManager.landingPage(manager) == "<p>Fontra for Teams</p>"
    # Missing (not installed yet): the sign-in page rather than an error.
    monkeypatch.setenv("HIVE_LANDING_FILE", str(tmp_path / "missing.html"))
    assert HiveProjectManager.landingPage(manager) == "<login.html>"
