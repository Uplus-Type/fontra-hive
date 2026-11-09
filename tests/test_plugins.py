"""Editor plug-ins Hive gives everyone: the ones served from a folder of
$HIVE_PLUGINS_DIR (U+Marks at /umarks/), and the list the pages hand to the
browser (register.js on Hive's pages, <meta name="hive-plugins"> on Try's)."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_views import manager  # noqa: F401  (fixture)

from fontra_hive.projectmanager import (
    FOLDER_PLUGINS,
    TRY_PROJECT,
    hivePlugins,
    renderRegisterScript,
)


@pytest.fixture
def plugins_dir(tmp_path, monkeypatch):
    """A $HIVE_PLUGINS_DIR with a U+Marks checkout in it (its files as the
    public repository has them), and a file outside it."""
    root = tmp_path / "plugins"
    umarks = root / "umarks"
    (umarks / "guide").mkdir(parents=True)
    (umarks / ".git").mkdir()
    (umarks / ".git" / "config").write_text("[core]\n")
    (umarks / "plugin.json").write_text(
        json.dumps({"name": "U+Marks", "init": "init.js", "function": "init", "version": "0.1.2"})
    )
    (umarks / "init.js").write_text("export function init() {}\n")
    (umarks / "marks.css").write_text(".x {}\n")
    (umarks / "marks-icon.svg").write_text("<svg/>")
    (umarks / "guide" / "index.html").write_text("<!doctype html><title>U+Marks</title>")
    (umarks / "guide" / "guide-panel.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (umarks / "README.md").write_text("# U+Marks\n")
    (umarks / ".hidden.js").write_text("secret")
    (tmp_path / "secret.js").write_text("secret")
    os.symlink(tmp_path / "secret.js", umarks / "elsewhere.js")
    monkeypatch.setenv("HIVE_PLUGINS_DIR", os.fspath(root))
    return root


def get(manager, path, **headers):  # noqa: F811
    request = SimpleNamespace(match_info={"path": path}, headers=headers, query={})
    return manager.folderPluginHandler(request, plugin="umarks")


def test_umarks_is_a_folder_plugin():
    assert FOLDER_PLUGINS == ("umarks",)


def test_the_folder_is_served_revalidated(manager, plugins_dir):  # noqa: F811
    async def go():
        response = await get(manager, "plugin.json")
        assert response.content_type == "application/json"
        assert json.loads(response.body)["name"] == "U+Marks"
        assert response.headers["Cache-Control"] == "no-cache"
        etag = response.headers["ETag"]
        with pytest.raises(web.HTTPNotModified) as notModified:
            await get(manager, "plugin.json", **{"If-None-Match": etag})
        assert notModified.value.headers["Cache-Control"] == "no-cache"
        again = await get(manager, "plugin.json", **{"If-None-Match": '"old"'})
        assert again.body == response.body
        for path, contentType in [
            ("init.js", "text/javascript"),
            ("marks.css", "text/css"),
            ("marks-icon.svg", "image/svg+xml"),
            ("guide/index.html", "text/html"),
            ("guide/guide-panel.png", "image/png"),
        ]:
            response = await get(manager, path)
            assert response.content_type == contentType, path
            assert response.headers["Cache-Control"] == "no-cache", path
            assert response.headers["ETag"], path

    asyncio.run(go())


def test_only_files_of_the_folder(manager, plugins_dir):  # noqa: F811
    async def go():
        for bad in [
            "",  # no listing of the folder
            "guide/",
            "guide",
            "../secret.js",
            "guide/../../secret.js",
            "./init.js",
            ".git/config",
            ".hidden.js",
            "elsewhere.js",  # a link out of the folder
            "README.md",  # not a type a plug-in serves
            "missing.js",
            "init.js/x.js",
        ]:
            with pytest.raises(web.HTTPNotFound):
                await get(manager, bad)

    asyncio.run(go())


def test_no_folder_no_plugin(manager, plugins_dir, monkeypatch):  # noqa: F811
    async def go():
        assert hivePlugins() == ["/hive/plugin", "/umarks/"]
        # A checkout without its manifest (half fetched): no plug-in.
        (plugins_dir / "umarks" / "plugin.json").unlink()
        assert hivePlugins() == ["/hive/plugin"]
        with pytest.raises(web.HTTPNotFound):
            await get(manager, "init.js")
        # Removed (an empty deploy/umarks-version): a clean 404.
        monkeypatch.setenv("HIVE_PLUGINS_DIR", os.fspath(plugins_dir / "none"))
        assert hivePlugins() == ["/hive/plugin"]
        with pytest.raises(web.HTTPNotFound):
            await get(manager, "plugin.json")
        # A server without the variable (Fontra Pak-like, tests): the same.
        monkeypatch.delenv("HIVE_PLUGINS_DIR")
        assert hivePlugins() == ["/hive/plugin"]
        with pytest.raises(web.HTTPNotFound):
            await get(manager, "plugin.json")

    asyncio.run(go())


def test_the_routes_through_aiohttp(manager, plugins_dir):  # noqa: F811
    """The real routes, in their order: /umarks/ before anything of Fontra's,
    register.js before the other client files, and the server starts and
    answers whether or not the folder is there."""

    async def go():
        app = web.Application()
        app.add_routes(manager.webRoutes())
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/umarks/plugin.json")
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-cache"
            assert (await response.json(content_type=None))["name"] == "U+Marks"
            assert (await client.get("/umarks/guide/index.html")).status == 200
            for bad in ["/umarks/", "/umarks/guide/", "/umarks/%2e%2e/secret.js"]:
                assert (await client.get(bad)).status == 404, bad
            script = await client.get("/hive/views/register.js")
            assert script.status == 200
            assert script.headers["Cache-Control"] == "no-cache"
            text = await script.text()
            assert 'var PLUGINS = ["/hive/plugin", "/umarks/"];' in text
            etag = script.headers["ETag"]
            same = await client.get(
                "/hive/views/register.js", headers={"If-None-Match": etag}
            )
            assert same.status == 304
            # Removed on the server: gone at once, without a restart.
            (plugins_dir / "umarks" / "plugin.json").unlink()
            assert (await client.get("/umarks/plugin.json")).status == 404
            script = await client.get(
                "/hive/views/register.js", headers={"If-None-Match": etag}
            )
            assert script.status == 200  # a new list, a new ETag
            assert 'var PLUGINS = ["/hive/plugin"];' in await script.text()

    asyncio.run(go())


def test_register_script_gets_the_list():
    source = (
        "(function () {\n"
        '  var PLUGINS = ["/hive/plugin"]; // written by the server (renderRegisterScript)\n'
        "})();\n"
    )
    rendered = renderRegisterScript(source, ["/hive/plugin", "/umarks/"])
    assert (
        '  var PLUGINS = ["/hive/plugin", "/umarks/"]; // written by the server' in rendered
    )
    # The file itself has the line the server rewrites.
    from importlib import resources

    real = (
        resources.files("fontra_hive")
        .joinpath("client", "views", "register.js")
        .read_text(encoding="utf-8")
    )
    assert renderRegisterScript(real, ["/x/"]) != real
    assert 'var PLUGINS = ["/x/"];' in renderRegisterScript(real, ["/x/"])


def try_request():
    return SimpleNamespace(
        match_info={},
        query={"project": TRY_PROJECT},
        cookies={},
        headers={},
        host="localhost:8000",
        path_qs="/editor.html",
    )


def test_try_pages_list_the_plugins(manager, plugins_dir, monkeypatch):  # noqa: F811
    async def go():
        page = (await manager.viewHandler(try_request(), view="editor")).text
        meta = '<meta name="hive-plugins" content="[&quot;/hive/plugin&quot;, &quot;/umarks/&quot;]">'
        assert meta in page
        assert page.index(meta) < page.index("try-engine.js")
        monkeypatch.delenv("HIVE_PLUGINS_DIR")
        page = (await manager.viewHandler(try_request(), view="editor")).text
        assert "/umarks/" not in page
        assert 'name="hive-plugins" content="[&quot;/hive/plugin&quot;]"' in page

    asyncio.run(go())
