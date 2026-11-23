"""The history of the whole font: commits said in words, the font's history
filtered, and the detail of a change (``font-diff``)."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

pytest.importorskip("fontra")

from conftest import FIXTURE, run  # noqa: E402
from fontra_hive.access import DevDirectory  # noqa: E402
from fontra_hive.backend_git import GitFontraBackend, _commit_message  # noqa: E402
from fontra_hive.gitstore import GitRepoStore, Signature  # noqa: E402
from fontra_hive.projectmanager import (  # noqa: E402
    DEV_USER_COOKIE,
    DevHiveProjectManager,
    glyphPath,
)

ME = Signature("Jérémie", "j@example.com")
FONT_DATA = (FIXTURE / "font-data.json").read_bytes()


def font_data(change) -> bytes:
    data = json.loads(FONT_DATA)
    change(data)
    return (json.dumps(data, indent=0) + "\n").encode()


def wider_weight(d):
    d["axes"]["axes"][1]["minValue"] = 200


def request(project, user=None, **query):
    return SimpleNamespace(
        match_info={"name": project},
        query=query,
        cookies={DEV_USER_COOKIE: user} if user else {},
        headers={},
        host="localhost:8000",
        method="GET",
    )


async def body(response):
    return json.loads(response.body)


@pytest.fixture
def manager(tmp_path, fixture_fontra):
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    store.close()
    return DevHiveProjectManager(root)


def commit(manager, changes, message, **kwargs):
    store = GitRepoStore.open(manager.rootPath / "Mutator.git")
    try:
        return store.commit(changes, message=message, author=ME, **kwargs)
    finally:
        store.close()


# --- the commit message ------------------------------------------------------------


def test_the_title_and_the_trailers_say_what_changed():
    before = {"font-data.json": FONT_DATA, "features.txt": None}
    changes = {
        "font-data.json": font_data(wider_weight),
        "features.txt": b"languagesystem DFLT dflt;\n",
        glyphPath("A"): b"{}\n",
        glyphPath("B"): b"{}\n",
    }
    message = _commit_message(changes, before)
    lines = message.splitlines()
    assert lines[0] == "Edit axes, features; Edit A, B"
    assert "Axes: wght 100–900 → 200–900" in lines
    assert "Features: 1 line added" in lines
    assert lines[-2:] == ["Hive-Glyphs: A B", "Hive-Font: fontsettings"]


def test_kerning_and_plugin_data_are_their_own_domains():
    before = {"font-data.json": FONT_DATA}
    changes = {"font-data.json": font_data(lambda d: d.update(customData={"umarks.rules": 1}))}
    message = _commit_message(changes, before)
    assert message.splitlines()[0] == "Edit plug-in data"
    assert message.rstrip().endswith("Hive-Font: plugindata")


def test_without_the_parent_the_files_are_named():
    message = _commit_message({"font-data.json": FONT_DATA})
    assert message == "Update font-data.json\n"
    # Nothing that Fontra reads differently: said by file name too.
    message = _commit_message(
        {"font-data.json": FONT_DATA + b"\n"}, {"font-data.json": FONT_DATA}
    )
    assert message == "Update font-data.json\n"


def test_the_backend_writes_the_words(tmp_path, fixture_fontra):
    async def go():
        store = GitRepoStore.create(tmp_path / "repo.git")
        sha = store.import_directory(fixture_fontra, message="Import", author=ME)
        backend = GitFontraBackend(store, "main", author=ME, commit_delay=0.05)
        axes = await backend.getAxes()
        axes.axes[1].minValue = 200
        await backend.putAxes(axes)
        await asyncio.sleep(0.5)
        info = store.commit_info(store.head())
        assert info.parents == (sha,)
        assert info.message.splitlines()[0] == "Edit axes"
        assert "Hive-Font: fontsettings" in info.message
        await backend.aclose()
        store.close()

    run(go())


# --- the font's history ------------------------------------------------------------


def test_old_commits_are_described_too(manager):
    """A commit written before the words ("Update font-data.json") gets its
    summary from the files themselves, at reading."""
    commit(manager, {"font-data.json": font_data(wider_weight)}, "Update font-data.json")
    commit(manager, {glyphPath("A"): b"{}\n"}, "Edit A\n\nHive-Glyphs: A\n")

    async def go():
        log = await body(await manager.logHandler(request("Mutator")))
        font = {c["message"].split("\n")[0]: c["font"] for c in log["commits"]}
        assert font["Update font-data.json"] == [
            {
                "area": "axes",
                "domain": "fontsettings",
                "summary": "Axes: wght 100–900 → 200–900",
            }
        ]
        assert font["Edit A"] == []
        assert font["Import"] == []  # the first commit: nothing to compare with

    run(go())


def test_the_history_filtered_by_kind_and_area(manager):
    commit(manager, {"font-data.json": font_data(wider_weight)}, "Axes")
    commit(manager, {glyphPath("A"): b"{}\n"}, "Edit A\n\nHive-Glyphs: A\n")
    commit(manager, {"features.txt": b"# fea\n"}, "Features")
    commit(
        manager,
        {"glyph-info.csv": (FIXTURE / "glyph-info.csv").read_bytes() + b"Z;U+005A\r\n"},
        "Glyph info",
    )

    async def titles(**query):
        log = await body(await manager.logHandler(request("Mutator", **query)))
        return [c["message"].split("\n")[0] for c in log["commits"]]

    async def go():
        assert await titles() == ["Glyph info", "Features", "Edit A", "Axes", "Import"]
        assert await titles(kind="font") == ["Features", "Axes"]
        assert await titles(kind="glyphs") == ["Glyph info", "Edit A", "Import"]
        assert await titles(area="axes") == ["Axes"]
        assert await titles(area="features,sources") == ["Features"]
        assert await titles(kind="font", limit="1") == ["Features"]

    run(go())


# --- the detail of a change ---------------------------------------------------------


def test_the_detail_of_a_version(manager):
    sha = commit(
        manager,
        {
            "font-data.json": font_data(wider_weight),
            "features.txt": b"feature liga {\n} liga;\n",
            glyphPath("A"): b"{}\n",
        },
        "Edit axes, features; Edit A",
    )

    async def go():
        diff = await body(await manager.fontDiffHandler(request("Mutator", to=sha)))
        assert diff["to"] == sha
        assert [(c["area"], c["summary"]) for c in diff["changes"]] == [
            ("axes", "Axes: wght 100–900 → 200–900"),
            ("features", "Features: 2 lines added"),
        ]
        axes, features = diff["changes"]
        assert axes["fields"] == [
            {"label": "wght range", "before": "100–900", "after": "200–900"}
        ]
        assert features["lines"][-2:] == [["+", "feature liga {"], ["+", "} liga;"]]
        assert diff["glyphs"] == [{"name": "A", "kind": "changed"}]

    run(go())


def test_compare_a_snapshot_with_now(manager):
    store = GitRepoStore.open(manager.rootPath / "Mutator.git")
    snapshot = store.create_snapshot("Proofs", author=ME)
    store.close()
    commit(manager, {"font-data.json": font_data(wider_weight)}, "Axes")
    commit(manager, {glyphPath("B"): b"{}\n"}, "Edit B\n\nHive-Glyphs: B\n")

    async def go():
        diff = await body(
            await manager.fontDiffHandler(
                request("Mutator", **{"from": "snapshot/" + snapshot.name})
            )
        )
        assert diff["from"] == snapshot.sha
        assert [c["summary"] for c in diff["changes"]] == ["Axes: wght 100–900 → 200–900"]
        assert diff["glyphs"] == [{"name": "B", "kind": "changed"}]
        with pytest.raises(web.HTTPNotFound):
            await manager.fontDiffHandler(request("Mutator", **{"from": "snapshot/nope"}))

    run(go())


def test_the_detail_needs_read(tmp_path, fixture_fontra):
    users = tmp_path / "users.json"
    users.write_text(
        json.dumps(
            {
                "users": {"ana": {"name": "Ana"}, "zoe": {"name": "Zoé"}},
                "projects": {"Mutator": {"owner": "zoe", "collaborators": {"ana": "observer"}}},
            }
        )
    )
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    sha = store.commit(
        {"font-data.json": font_data(wider_weight)}, message="Axes", author=ME
    )
    store.close()
    manager = DevHiveProjectManager(root, directory=DevDirectory(users))

    async def go():
        # An observer reads the history, its words and its detail.
        diff = await body(await manager.fontDiffHandler(request("Mutator", "ana", to=sha)))
        assert diff["changes"][0]["area"] == "axes"
        log = await body(await manager.logHandler(request("Mutator", "ana", kind="font")))
        assert log["commits"][0]["font"][0]["summary"].startswith("Axes:")
        # Someone who cannot read the project: it looks absent.
        for user in (None, "ghost"):
            with pytest.raises(web.HTTPNotFound):
                await manager.fontDiffHandler(request("Mutator", user, to=sha))

    run(go())
