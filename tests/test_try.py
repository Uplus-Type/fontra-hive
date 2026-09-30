""""Try Fontra" (client/try): Fontra's pages open to anyone for the demo
project, with the in-browser engine instead of Hive's scripts."""

import asyncio
import json
import pathlib
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from aiohttp import web
from test_views import manager  # noqa: F401  (fixture)

from fontra_hive.projectmanager import (
    CLIENT_CONTENT_TYPES,
    TRY_PROJECT,
    TRY_START,
    _isTryRequest,
    injectTryScripts,
)


def request(project=None, path_qs="/"):
    """A request for one of Fontra's pages, ?project=<project>."""
    return SimpleNamespace(
        match_info={},
        query={"project": project} if project else {},
        cookies={},
        headers={},
        host="localhost:8000",
        path_qs=path_qs,
    )


TRY_DIR = (
    pathlib.Path(__file__).parent.parent / "src" / "fontra_hive" / "client" / "try"
)


def test_inject_try_scripts():
    page = injectTryScripts(
        "<html><HEAD><title>x</title></HEAD><body><p>x</p></BODY></html>"
    )
    assert page.index('src="/hive/try/fontra-format.js"') < page.index(
        'src="/hive/try/try-store.js"'
    )
    assert page.index('src="/hive/try/try-store.js"') < page.index(
        'src="/hive/try/try-engine.js"'
    )
    assert page.index('src="/hive/try/try-engine.js"') < page.index("<title>")
    assert page.index('src="/hive/try/try-banner.js"') > page.index("<p>x</p>")
    assert page.index('href="/hive/icons/hive-icon.svg"') < page.index("<title>")
    assert "register.js" not in page and "hive-views.js" not in page
    assert injectTryScripts("<p>x</p>").startswith(
        '<script src="/hive/try/fontra-format.js">'
    )


def test_only_the_demo_project_is_a_try_request():
    assert TRY_PROJECT == "demo:MutatorSans" and "/" not in TRY_PROJECT
    assert _isTryRequest(request(TRY_PROJECT), "editor")
    assert _isTryRequest(request(TRY_PROJECT), "fontoverview")
    assert not _isTryRequest(request(TRY_PROJECT), "applicationsettings")
    assert not _isTryRequest(request("jeremie/Demo"), "editor")
    assert not _isTryRequest(request(), "editor")
    # Fonts kept in the visitor's browser.
    assert _isTryRequest(request("local:0123456789ab"), "editor")
    assert _isTryRequest(request("local:0123456789ab"), "fontinfo")
    assert not _isTryRequest(request("local:../x"), "editor")
    assert not _isTryRequest(request("local:0123456789AB"), "editor")


def test_try_pages_need_no_session(manager):  # noqa: F811
    async def go():
        response = await manager.viewHandler(
            request(TRY_PROJECT), view="editor"
        )
        assert "try-engine.js" in response.text and "try-banner.js" in response.text
        assert "hive-views.js" not in response.text
        assert response.headers["Cache-Control"] == "no-cache"
        # Any other project still asks for a session.
        with pytest.raises(web.HTTPFound):
            await manager.viewHandler(
                request("Mutator", path_qs="/editor.html?project=Mutator"),
                view="editor",
            )
        with pytest.raises(web.HTTPFound) as raised:
            await manager.tryHandler(request())
        assert unquote(str(raised.value.location)) == unquote(TRY_START)
        assert TRY_START.startswith("/editor.html?project=demo%3AMutatorSans&text=")

    asyncio.run(go())


def test_try_route_is_registered(manager):  # noqa: F811
    paths = [getattr(route, "path", None) for route in manager.projectRoutes()]
    assert "/try" in paths
    assert paths.index("/try") < paths.index("/hive/{path:.*}")


def test_demo_font_and_its_licence():
    font = json.loads((TRY_DIR / "demo-font.json").read_text(encoding="utf-8"))
    assert set(font["glyphMap"]) == set(font["glyphs"])
    assert len(font["glyphs"]) >= 40 and "HAMBURGEFONSTIV" == "".join(
        name for name in "HAMBURGEFONSTIV" if name in font["glyphs"]
    )
    assert font["axes"]["axes"] and font["sources"] and font["unitsPerEm"] == 1000
    assert "MUTATORSANS-LICENSE.txt" in font["fontInfo"]["licenseDescription"]
    licence = (TRY_DIR / "MUTATORSANS-LICENSE.txt").read_text(encoding="utf-8")
    assert "LettError" in licence and "Redistribution" in licence
    for name in TRY_DIR.iterdir():
        if name.is_file():
            assert name.suffix[1:] in CLIENT_CONTENT_TYPES, name.name


def test_python_bundle_for_the_browser():
    """What "Try Fontra" runs in Pyodide: pure Python, Fontra's backends found
    through entry points, stand-ins for what the browser does not have."""
    import io
    import zipfile

    from fontra_hive import trybundle

    data = trybundle.buildBundle()
    assert len(data) < 8 * 1024 * 1024
    archive = zipfile.ZipFile(io.BytesIO(data))
    names = set(archive.namelist())
    assert "hive_try_convert.py" in names
    assert "fontra/backends/designspace.py" in names
    assert "fontra_hive/importer.py" in names and "fontra_hive/export.py" in names
    assert "fontTools/ufoLib/__init__.py" in names and "ufoLib2/__init__.py" in names
    assert "aiohttp/__init__.py" in names and "watchfiles/__init__.py" in names
    assert not any(n.endswith((".so", ".pyd", ".pyc")) for n in names)
    clients = ("fontra/client/", "fontra_hive/client/")
    assert not any(n.startswith(clients) for n in names)
    entryPoints = archive.read(f"{trybundle.DIST_INFO}/entry_points.txt").decode()
    assert "[fontra.filesystem.backends]" in entryPoints
    assert "designspace = fontra.backends.designspace:DesignspaceBackend" in entryPoints
    assert "glyphs" not in entryPoints  # not readable in the browser (yet)


def test_pyodide_is_served_here(manager, tmp_path, monkeypatch):  # noqa: F811
    from fontra_hive.projectmanager import pyodideURL

    async def go():
        get = lambda name: manager.pyodideHandler(  # noqa: E731
            SimpleNamespace(match_info={"name": name}, headers={})
        )
        monkeypatch.delenv("HIVE_PYODIDE_DIR", raising=False)
        monkeypatch.delenv("HIVE_PYODIDE_URL", raising=False)
        assert pyodideURL() is None
        with pytest.raises(web.HTTPNotFound):
            await get("pyodide.mjs")
        page = (await manager.viewHandler(request(TRY_PROJECT), view="editor")).text
        assert "hive-pyodide" not in page  # the page's default: the CDN

        (tmp_path / "pyodide.asm.wasm").write_bytes(b"\0asm")
        (tmp_path / "secret.txt").write_text("x")
        monkeypatch.setenv("HIVE_PYODIDE_DIR", str(tmp_path))
        assert pyodideURL() == "/hive/pyodide/"
        response = await get("pyodide.asm.wasm")
        assert response.headers["Content-Type"] == "application/wasm"
        for name in ("secret.txt", "../x.wasm", ".hidden.js", "missing.mjs"):
            with pytest.raises(web.HTTPNotFound):
                await get(name)
        page = (await manager.viewHandler(request(TRY_PROJECT), view="editor")).text
        assert page.index('name="hive-pyodide" content="/hive/pyodide/"') < page.index(
            "try-engine.js"
        )
        monkeypatch.delenv("HIVE_PYODIDE_DIR")
        monkeypatch.setenv("HIVE_PYODIDE_URL", "https://example.org/py/")
        assert pyodideURL() == "https://example.org/py/"

    asyncio.run(go())


def test_python_bundle_route(manager):  # noqa: F811
    async def go():
        response = await manager.pythonBundleHandler(SimpleNamespace(headers={}))
        assert response.content_type == "application/zip"
        etag = response.headers["ETag"]
        with pytest.raises(web.HTTPNotModified):
            await manager.pythonBundleHandler(
                SimpleNamespace(headers={"If-None-Match": etag})
            )

    asyncio.run(go())
