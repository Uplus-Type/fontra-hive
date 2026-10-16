"""No page of Hive inside another site's frame (security review, S-3)."""

from types import SimpleNamespace

from aiohttp import web

from conftest import run
from fontra_hive import hivemanager


def test_no_page_in_another_sites_frame(tmp_path):
    response = web.Response(text="page")
    run(hivemanager.noFraming(None, response))
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    # A page that says more keeps its own policy.
    response = web.Response(text="page", headers={"Content-Security-Policy": "x"})
    run(hivemanager.noFraming(None, response))
    assert response.headers["Content-Security-Policy"] == "x"


def test_the_server_adds_them(tmp_path):
    from fontra_hive.hivemanager import HiveProjectManager

    class Api:
        async def aclose(self):
            pass

    root = tmp_path / "repos"
    root.mkdir()
    manager = HiveProjectManager(root, api=Api(), proxy=False)
    app = web.Application()
    manager.setupWebRoutes(SimpleNamespace(httpApp=app))
    assert hivemanager.noFraming in app.on_response_prepare
