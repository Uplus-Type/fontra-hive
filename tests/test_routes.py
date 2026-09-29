import json
from types import SimpleNamespace

import pytest

from conftest import run
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.projectmanager import DevHiveProjectManager, glyphPath

ME = Signature("Jérémie", "j@example.com")


def fake_request(name, **query):
    return SimpleNamespace(match_info={"name": name}, query=query)


@pytest.fixture
def manager(tmp_path, fixture_fontra):
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    store.commit({glyphPath("A"): b"{}\n"}, message="Edit A", author=ME)
    store.create_branch("bold")
    store.commit({glyphPath("B"): b"{}\n"}, branch="bold", message="bold B", author=ME)
    store.close()
    return DevHiveProjectManager(root)


def test_glyph_path_uses_fontra_file_name_encoding():
    assert glyphPath("A") == "glyphs/A^1.json"
    assert glyphPath("A.alt") == "glyphs/A.alt^1.json"


def test_log_route_filters_by_glyph(manager):
    async def go():
        response = await manager.logHandler(fake_request("Mutator", glyph="A"))
        data = json.loads(response.body)
        assert data["branch"] == "main" and data["path"] == "glyphs/A^1.json"
        assert [c["message"].splitlines()[0] for c in data["commits"]] == [
            "Edit A",
            "Import",
        ]
        assert data["head"] == data["commits"][0]["sha"]

        response = await manager.logHandler(
            fake_request("Mutator", glyph="B", branch="bold")
        )
        data = json.loads(response.body)
        assert [c["message"] for c in data["commits"]] == ["bold B", "Import"]

        response = await manager.logHandler(fake_request("Mutator", glyph="B"))
        assert [c["message"] for c in json.loads(response.body)["commits"]] == [
            "Import"
        ]

    run(go())


def test_glyph_route_returns_glyph_json_at_ref(manager):
    async def go():
        response = await manager.glyphHandler(fake_request("Mutator", glyph="A"))
        assert response.body == b"{}\n"
        log = json.loads(
            (await manager.logHandler(fake_request("Mutator", glyph="A"))).body
        )
        first = log["commits"][-1]["sha"]
        response = await manager.glyphHandler(
            fake_request("Mutator", glyph="A", ref=first)
        )
        assert json.loads(response.body)["name"] == "A"

    run(go())


def test_project_list_and_availability(manager):
    async def go():
        assert await manager.getProjectList("dev") == ["Mutator", "Mutator@bold"]
        assert await manager.projectAvailable("Mutator@bold", "dev")
        assert not await manager.projectAvailable("Mutator@nope", "dev")
        assert not await manager.projectAvailable("../etc", "dev")

    run(go())


def test_head_route(manager):
    async def go():
        response = await manager.headHandler(fake_request("Mutator"))
        data = json.loads(response.body)
        assert data["branch"] == "main" and len(data["head"]) == 40
        bold = json.loads(
            (await manager.headHandler(fake_request("Mutator", branch="bold"))).body
        )
        assert bold["head"] != data["head"]

    run(go())
