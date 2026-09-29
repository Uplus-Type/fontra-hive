import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from conftest import run
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.projectmanager import DevHiveProjectManager, glyphPath

ME = Signature("Jérémie", "j@example.com")


def fake_request(project, **query):
    return SimpleNamespace(match_info={"name": project}, query=query)


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


def test_restore_route_commits_the_old_glyph_on_top(manager, tmp_path):
    async def go():
        log = json.loads(
            (await manager.logHandler(fake_request("Mutator", glyph="A"))).body
        )
        head, first = log["head"], log["commits"][-1]["sha"]
        original = (
            await manager.glyphHandler(fake_request("Mutator", glyph="A", ref=first))
        ).body
        assert original != b"{}\n"

        response = await manager.restoreHandler(
            fake_request("Mutator", glyph="A", ref=first)
        )
        data = json.loads(response.body)
        assert data["changed"] and data["restored"] == first
        assert data["head"] != head and len(data["head"]) == 40

        # History is not rewritten: a new commit sits on top of the branch.
        log = json.loads(
            (await manager.logHandler(fake_request("Mutator", glyph="A"))).body
        )
        assert log["head"] == data["head"]
        assert [c["message"].splitlines()[0] for c in log["commits"]] == [
            f"Restore A to {first[:10]}",
            "Edit A",
            "Import",
        ]
        assert f"Hive-Restore: {first}" in log["commits"][0]["message"]
        assert log["commits"][0]["author"] == "Fontra Hive"  # no --author-name
        assert (
            await manager.glyphHandler(fake_request("Mutator", glyph="A"))
        ).body == original

        # Restoring what is already current changes nothing.
        again = json.loads(
            (
                await manager.restoreHandler(
                    fake_request("Mutator", glyph="A", ref=first)
                )
            ).body
        )
        assert again == {**data, "changed": False}

        # Missing parameters, unknown glyph or ref, read-only server.
        with pytest.raises(web.HTTPBadRequest):
            await manager.restoreHandler(fake_request("Mutator", glyph="A"))
        with pytest.raises(web.HTTPNotFound):
            await manager.restoreHandler(
                fake_request("Mutator", glyph="nope", ref=first)
            )
        with pytest.raises(web.HTTPNotFound):
            await manager.restoreHandler(
                fake_request("Mutator", glyph="A", ref="0" * 40)
            )
        manager.readOnly = True
        with pytest.raises(web.HTTPForbidden):
            await manager.restoreHandler(fake_request("Mutator", glyph="A", ref=first))

    run(go())


def test_restore_route_reloads_the_open_backend(manager):
    """When the project is open in the editor, the restored glyph reaches the
    running backend through the external-changes path."""

    async def go():
        fontHandler = await manager.getRemoteSubject("Mutator", "dev")
        backend = fontHandler.backend
        received = []

        async def callback(pattern):
            received.append(pattern)

        await backend.watchExternalChanges(callback)
        assert (await backend.getGlyph("A")).layers == {}  # "Edit A" wrote "{}"
        first = json.loads(
            (await manager.logHandler(fake_request("Mutator", glyph="A"))).body
        )["commits"][-1]["sha"]

        data = json.loads(
            (
                await manager.restoreHandler(
                    fake_request("Mutator", glyph="A", ref=first)
                )
            ).body
        )
        assert data["changed"]
        assert received == [{"glyphs": {"A": None}}]
        glyph = await backend.getGlyph("A")
        assert glyph.name == "A" and glyph.layers  # the imported glyph is back
        assert backend.tree.commit_sha == data["head"]
        await manager.aclose()

    run(go())


def test_snapshot_routes_group_commits(manager):
    async def go():
        data = json.loads(
            (await manager.snapshotsHandler(fake_request("Mutator"))).body
        )
        assert data["snapshots"] == [] and data["pending"] == 2  # Import, Edit A

        response = await manager.snapshotHandler(
            fake_request("Mutator", name="Épreuves 1")
        )
        created = json.loads(response.body)
        snap = created["snapshot"]
        assert snap["name"] == "epreuves-1" and snap["title"] == "Épreuves 1"
        assert snap["changes"] == 2 and snap["glyphs"] == []  # no trailers here
        assert created["head"] == snap["sha"]

        data = json.loads(
            (await manager.snapshotsHandler(fake_request("Mutator"))).body
        )
        assert [s["name"] for s in data["snapshots"]] == ["epreuves-1"]
        assert data["pending"] == 0 and data["head"] == snap["sha"]

        # Nothing new since: 409. Same name later: 409. Missing name: 400.
        with pytest.raises(web.HTTPConflict):
            await manager.snapshotHandler(fake_request("Mutator", name="again"))
        with pytest.raises(web.HTTPBadRequest):
            await manager.snapshotHandler(fake_request("Mutator"))

        # The glyph log says which snapshot each version belongs to.
        first = json.loads(
            (await manager.logHandler(fake_request("Mutator", glyph="A"))).body
        )["commits"][-1]["sha"]
        await manager.restoreHandler(fake_request("Mutator", glyph="A", ref=first))
        with pytest.raises(web.HTTPConflict):
            await manager.snapshotHandler(fake_request("Mutator", name="Épreuves-1"))
        log = json.loads(
            (await manager.logHandler(fake_request("Mutator", glyph="A"))).body
        )
        assert [
            (c["message"].splitlines()[0], c["snapshot"]) for c in log["commits"]
        ] == [
            (f"Restore A to {first[:10]}", None),
            ("Edit A", "epreuves-1"),
            ("Import", "epreuves-1"),
        ]
        assert [s["name"] for s in log["snapshots"]] == ["epreuves-1"]
        # Other branches are independent.
        bold = json.loads(
            (
                await manager.snapshotsHandler(fake_request("Mutator", branch="bold"))
            ).body
        )
        assert bold["snapshots"] == [] and bold["pending"] == 3

        manager.readOnly = True
        with pytest.raises(web.HTTPForbidden):
            await manager.snapshotHandler(fake_request("Mutator", name="v2"))

    run(go())


def test_snapshot_route_with_the_project_open(manager):
    """Pending edits are committed first; the open backend follows the new
    head without reloading anything (the tree did not change)."""

    async def go():
        fontHandler = await manager.getRemoteSubject("Mutator", "dev")
        backend = fontHandler.backend
        received = []

        async def callback(pattern):
            received.append(pattern)

        await backend.watchExternalChanges(callback)
        glyph = await backend.getGlyph("B")
        await backend.putGlyph("B", glyph, [66])
        assert backend.tree.pending  # not committed yet (commit delay)

        created = json.loads(
            (await manager.snapshotHandler(fake_request("Mutator", name="v1"))).body
        )
        assert created["snapshot"]["glyphs"] == ["B"]  # "Edit A" has no trailer
        assert created["snapshot"]["changes"] == 3
        assert backend.tree.commit_sha == created["head"]
        assert received == []  # nothing to reload
        await manager.aclose()

    run(go())


def test_plugin_files_are_served_with_revalidation(manager):
    """The plug-in files are served by Hive itself, never cached blindly."""

    def request(path, **headers):
        return SimpleNamespace(match_info={"path": path}, headers=headers, query={})

    async def go():
        response = await manager.clientFileHandler(request("plugin/init.js"))
        assert response.content_type == "text/javascript"
        assert b"export function init" in response.body
        assert response.headers["Cache-Control"] == "no-cache"
        etag = response.headers["ETag"]
        with pytest.raises(web.HTTPNotModified):
            await manager.clientFileHandler(
                request("plugin/init.js", **{"If-None-Match": etag})
            )
        again = await manager.clientFileHandler(
            request("plugin/init.js", **{"If-None-Match": '"old"'})
        )
        assert again.body == response.body
        manifest = await manager.clientFileHandler(request("plugin/plugin.json"))
        assert manifest.content_type == "application/json"
        for bad in [
            "plugin/../__init__.py",
            "__init__.py",
            "plugin/",
            "plugin/nope.js",
        ]:
            with pytest.raises(web.HTTPNotFound):
                await manager.clientFileHandler(request(bad))

    run(go())
