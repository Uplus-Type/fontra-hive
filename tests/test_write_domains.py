"""Beyond the glyph: what a change touches (fontra_hive.writedomains), and
who may write it (the font handler's guard, the font's snapshot, Font info
opened read-only)."""

import dataclasses
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fontra")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402
from fontra.core.classes import Font  # noqa: E402

from conftest import run  # noqa: E402
from fontra_hive.access import (  # noqa: E402
    CAPABILITIES,
    DevDirectory,
    closedDomains,
    markReadOnlyConnection,
    splitReadOnlyMark,
    token_for,
)
from fontra_hive.fonthandler import ReadOnlyError  # noqa: E402
from fontra_hive.gitstore import GitRepoStore, Signature  # noqa: E402
from fontra_hive.hiveapi import allowedCapabilities  # noqa: E402
from fontra_hive.projectmanager import (  # noqa: E402
    DEV_USER_COOKIE,
    DevHiveProjectManager,
)
from fontra_hive.writedomains import (  # noqa: E402
    EDIT,
    EVERYTHING,
    FONTSETTINGS,
    KERNING,
    PLUGINDATA,
    TOP_LEVEL,
    WRITE_DOMAINS,
    changeDomains,
)

DOMAINS = {FONTSETTINGS, KERNING, PLUGINDATA}

# Where each top-level key of Fontra's Font belongs (customData: by key).
EXPECTED = {
    "unitsPerEm": FONTSETTINGS,
    "fontInfo": FONTSETTINGS,
    "glyphs": EDIT,
    "glyphMap": EDIT,
    "glyphInfos": EDIT,
    "axes": FONTSETTINGS,
    "sources": FONTSETTINGS,
    "kerning": KERNING,
    "features": FONTSETTINGS,
    "conditionalSubstitutions": FONTSETTINGS,
    "customData": None,
}


# --- the classification -------------------------------------------------------------


def test_every_key_of_fontras_font_is_in_a_domain():
    """A key Fontra adds later fails here: put it in TOP_LEVEL on purpose
    (until then it counts as font settings)."""
    keys = {f.name for f in dataclasses.fields(Font)}
    assert keys == set(TOP_LEVEL) == set(EXPECTED)


@pytest.mark.parametrize("key", sorted(k for k, v in EXPECTED.items() if v))
def test_one_top_level_key_at_a_time(key):
    domain = EXPECTED[key]
    # Below the key, as Fontra's panels and tools send it...
    assert changeDomains({"p": [key, "x"], "f": "=", "a": ["y", 1]}) == {domain}
    assert changeDomains({"p": [key], "f": "=", "a": ["x", 1]}) == {domain}
    # ...the key itself set or deleted at the root...
    assert changeDomains({"f": "=", "a": [key, 1]}) == {domain}
    assert changeDomains({"f": "d", "a": [key]}) == {domain}
    # ...and through children, whose paths continue their parent's.
    assert changeDomains({"c": [{"p": [key], "c": [{"f": "=", "a": ["z", 2]}]}]}) == {
        domain
    }


def test_an_unknown_key_is_font_settings():
    assert changeDomains({"p": ["somethingNew"], "f": "=", "a": ["x", 1]}) == {
        FONTSETTINGS
    }
    assert changeDomains({"f": "=", "a": ["somethingNew", 1]}) == {FONTSETTINGS}
    assert changeDomains({"p": [3], "f": "=", "a": ["x", 1]}) == {FONTSETTINGS}


def test_custom_data_by_key():
    def at(key):
        return changeDomains({"p": ["customData"], "f": "=", "a": [key, 1]})

    assert at("umarks.rules") == {PLUGINDATA}
    assert at("umarks.anchors.top") == {PLUGINDATA}
    assert at("fontra.projectGlyphSets") == {PLUGINDATA}
    assert at("fontra.sourceStatusFieldDefinitions") == {FONTSETTINGS}
    assert at("openTypeOS2WeightClass") == {FONTSETTINGS}
    assert at("umarksish") == {FONTSETTINGS}  # the prefix is "umarks."
    # Deeper, inside a plug-in's value.
    assert changeDomains(
        {"p": ["customData", "umarks.rules", 0], "f": "=", "a": ["x", 1]}
    ) == {PLUGINDATA}
    # All of customData at once: both.
    assert changeDomains({"f": "=", "a": ["customData", {}]}) == {
        FONTSETTINGS,
        PLUGINDATA,
    }
    assert changeDomains({"p": ["customData"], "f": "-", "a": [0]}) == {
        FONTSETTINGS,
        PLUGINDATA,
    }


def test_a_change_that_mixes_a_glyph_and_an_axis():
    change = {
        "c": [
            {
                "p": ["glyphs", "A", "layers", "x", "glyph"],
                "f": "=",
                "a": ["xAdvance", 5],
            },
            {"p": ["axes", "axes", 0], "f": "=", "a": ["maxValue", 800]},
        ]
    }
    assert changeDomains(change) == {EDIT, FONTSETTINGS}


def test_the_whole_font_or_a_change_fontra_does_not_make():
    assert changeDomains({"f": "=", "a": []}) == EVERYTHING
    assert changeDomains({"f": "something"}) == EVERYTHING
    assert changeDomains("nonsense") == EVERYTHING
    assert changeDomains({"p": "glyphs"}) == EVERYTHING
    assert changeDomains({"c": [None]}) == EVERYTHING
    assert changeDomains({}) == set()  # nothing at all


# --- who has what -------------------------------------------------------------------


def test_designers_and_up_have_the_three_by_role():
    # The same table as hive-api's hive/roles.py (hive/tests/test_projects.py).
    assert CAPABILITIES["designer"] == {
        "read",
        "comment",
        "edit",
        "fontsettings",
        "kerning",
        "plugindata",
        "branch",
        "snapshot",
    }
    for role in ("manager", "admin"):
        assert DOMAINS <= CAPABILITIES[role]
    for role in ("observer", "reviewer"):
        assert not DOMAINS & CAPABILITIES[role]
    assert set(WRITE_DOMAINS) == DOMAINS


def test_the_development_directory_setting():
    writers = {"fontsettings": "managers", "kerning": ["fabio"]}
    assert closedDomains(writers, "fabio", "designer") == {FONTSETTINGS}
    assert closedDomains(writers, "ana", "designer") == {FONTSETTINGS, KERNING}
    assert closedDomains(writers, "ana", "manager") == set()
    assert closedDomains(None, "ana", "designer") == set()
    assert closedDomains({"plugindata": "all"}, "ana", "designer") == set()


def test_hive_api_decides_the_domains_it_knows():
    designer = CAPABILITIES["designer"]
    listed = sorted(designer - {"kerning"})
    known = ["fontsettings", "kerning", "plugindata"]
    assert allowedCapabilities("designer", listed, known) == designer - {"kerning"}
    # An older hive-api: the domains follow "edit".
    older = sorted(designer - DOMAINS)
    assert allowedCapabilities("designer", older) == designer
    assert allowedCapabilities("designer", older, None) == designer
    # ...but never without "edit" (no plan: read-only).
    assert allowedCapabilities("designer", ["comment", "read"]) == {"comment", "read"}
    # Never more than the role.
    assert allowedCapabilities("reviewer", ["fontsettings", "read"], known) == {"read"}


def test_the_read_only_mark():
    ws = SimpleNamespace(path="/websocket", query={"read-only": "true"})
    page = SimpleNamespace(path="/fontinfo.html", query={"read-only": "true"})
    plain = SimpleNamespace(path="/websocket", query={})
    assert markReadOnlyConnection("t", ws) == "t read-only"
    assert markReadOnlyConnection("t", page) == "t"
    assert markReadOnlyConnection("t", plain) == "t"
    assert markReadOnlyConnection(None, ws) is None
    assert splitReadOnlyMark("t read-only") == ("t", True)
    assert splitReadOnlyMark("t") == ("t", False)
    assert splitReadOnlyMark(None) == (None, False)


# --- the guard, with the development directory ---------------------------------------

ME = Signature("Import", "import@example.com")

USERS = {
    "users": {
        "jeremie": {"name": "Jérémie Hornus"},
        "fabio": {"name": "Fabio Rossi"},
        "ana": {"name": "Ana López"},
        "mei": {"name": "Mei Chen"},
    },
    "organizations": {
        "uplustype": {
            "name": "U+Type",
            "members": {"jeremie": "owner", "fabio": "member", "ana": "member"},
            "base_role": "designer",
        }
    },
    "projects": {
        "Mutator": {
            "owner": "uplustype",
            "collaborators": {"mei": "manager"},
            "writers": {},
        },
    },
}


@pytest.fixture
def directory(tmp_path):
    path = tmp_path / "hive-dev-users.json"
    path.write_text(json.dumps(USERS), encoding="utf-8")
    return DevDirectory(path)


def set_writers(directory, writers):
    data = json.loads(directory.path.read_text())
    data["projects"]["Mutator"]["writers"] = writers
    directory.path.write_text(json.dumps(data), encoding="utf-8")
    directory._mtime = None  # same second: read it again


@pytest.fixture
def manager(tmp_path, fixture_fontra, directory):
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    store.close()
    return DevHiveProjectManager(root, directory=directory, commitDelay=0.05)


def connection(user, uuid=None, readOnly=False):
    async def externalChange(change, isLiveChange):
        pass

    token = token_for(user) + (" read-only" if readOnly else "")
    return SimpleNamespace(
        authorizationToken=token,
        clientUUID=uuid or f"c-{user}{'-ro' if readOnly else ''}",
        proxy=SimpleNamespace(externalChange=externalChange),
    )


# One change per domain, as Fontra's views send them.
CHANGES = {
    FONTSETTINGS: {"p": ["fontInfo"], "f": "=", "a": ["familyName", "Mutator Test"]},
    KERNING: {
        "p": ["kerning"],
        "f": "=",
        "a": [
            "kern",
            {
                "groupsSide1": {},
                "groupsSide2": {},
                "sourceIdentifiers": [],
                "values": {},
            },
        ],
    },
    PLUGINDATA: {"p": ["customData"], "f": "=", "a": ["umarks.rules", []]},
}


def glyph_change(path, value):
    return {"p": path, "f": "=", "a": ["xAdvance", value]}


async def glyph_path(handler, who):
    glyph = await handler.getGlyph("B", connection=who)
    return ["glyphs", "B", "layers", next(iter(glyph.layers)), "glyph"]


@pytest.mark.parametrize("domain", sorted(DOMAINS))
def test_a_designer_off_the_list_is_refused_domain_by_domain(
    manager, directory, domain
):
    set_writers(directory, {domain: ["fabio"]})

    async def go():
        handler = await manager.getRemoteSubject("Mutator", token_for("ana"), False)
        ana = connection("ana")
        with pytest.raises(ReadOnlyError, match="Ask a manager") as refused:
            await handler.editFinal(CHANGES[domain], {}, "edit", False, connection=ana)
        label = {"fontsettings": "Font settings", "kerning": "Kerning"}.get(
            domain, "Plug-in data"
        )
        assert label in str(refused.value)
        with pytest.raises(ReadOnlyError):
            await handler.editIncremental(CHANGES[domain], connection=ana)
        # The undo of a change in that domain, sent as the rollback, too.
        with pytest.raises(ReadOnlyError):
            await handler.editFinal({}, CHANGES[domain], "undo", False, connection=ana)
        # The other domains and the glyphs stay hers.
        for other in sorted(DOMAINS - {domain}):
            await handler.editFinal(CHANGES[other], {}, "edit", False, connection=ana)
        path = await glyph_path(handler, ana)
        await handler.editFinal(
            glyph_change(path, 600), {}, "edit", False, connection=ana
        )
        # The listed designer, a manager and an admin may.
        for who in ("fabio", "mei", "jeremie"):
            await handler.editFinal(
                CHANGES[domain], {}, "edit", False, connection=connection(who)
            )
        await handler.finishWriting()
        await manager.aclose()

    run(go())


def test_managers_only(manager, directory):
    set_writers(directory, {"fontsettings": "managers"})

    async def go():
        handler = await manager.getRemoteSubject("Mutator", token_for("fabio"), False)
        with pytest.raises(ReadOnlyError, match="Font settings"):
            await handler.editFinal(
                CHANGES[FONTSETTINGS], {}, "edit", False, connection=connection("fabio")
            )
        await handler.editFinal(
            CHANGES[FONTSETTINGS], {}, "edit", False, connection=connection("mei")
        )
        await handler.finishWriting()
        await manager.aclose()

    run(go())


def test_a_change_with_a_glyph_and_an_axis_is_refused_whole(manager, directory):
    set_writers(directory, {"fontsettings": "managers"})

    async def go():
        handler = await manager.getRemoteSubject("Mutator", token_for("fabio"), False)
        fabio = connection("fabio")
        path = await glyph_path(handler, fabio)
        before = (await handler.getGlyph("B", connection=fabio)).layers[path[3]].glyph
        mixed = {
            "c": [
                glyph_change(path, 999),
                {"p": ["axes", "axes", 0], "f": "=", "a": ["maxValue", 800]},
            ]
        }
        with pytest.raises(ReadOnlyError, match="Font settings"):
            await handler.editFinal(mixed, {}, "edit", False, connection=fabio)
        await handler.finishWriting()
        after = (await handler.getGlyph("B", connection=fabio)).layers[path[3]].glyph
        assert after.xAdvance == before.xAdvance != 999  # not even the glyph
        head = handler.backend.store.head("main")
        assert handler.backend.store.commit_info(head).message.startswith("Import")
        await manager.aclose()

    run(go())


def test_by_default_every_designer_writes_everything(manager):
    async def go():
        handler = await manager.getRemoteSubject("Mutator", token_for("ana"), False)
        for change in CHANGES.values():
            await handler.editFinal(
                change, {}, "edit", False, connection=connection("ana")
            )
        await handler.finishWriting()
        await manager.aclose()

    run(go())


def test_a_connection_opened_read_only(manager):
    """Hive's Font info for someone without "fontsettings": Fontra's own
    read-only mode, for that connection only."""

    async def go():
        ws = make_mocked_request(
            "GET",
            "/websocket?project=Mutator&read-only=true",
            headers={"Cookie": f"{DEV_USER_COOKIE}=fabio"},
        )
        token = await manager.authorize(ws)
        assert token == token_for("fabio") + " read-only"
        handler = await manager.getRemoteSubject("Mutator", token, True)
        assert handler is not None
        assert await manager.projectAvailable("Mutator", token)
        readOnly, normal = connection("fabio", readOnly=True), connection("fabio")
        assert await handler.isReadOnly(connection=readOnly) is True
        assert await handler.isReadOnly(connection=normal) is False
        with pytest.raises(ReadOnlyError, match="opened read-only"):
            await handler.editFinal(
                CHANGES[KERNING], {}, "edit", False, connection=readOnly
            )
        await handler.editFinal(CHANGES[KERNING], {}, "edit", False, connection=normal)
        await handler.finishWriting()
        await manager.aclose()

    run(go())


def fontinfo(user, query="project=Mutator"):
    return make_mocked_request(
        "GET",
        f"/fontinfo.html?{query}",
        headers={"Cookie": f"{DEV_USER_COOKIE}={user}"},
    )


def test_font_info_opens_read_only_without_font_settings(manager, directory):
    set_writers(directory, {"fontsettings": ["fabio"]})

    async def go():
        with pytest.raises(web.HTTPFound) as moved:
            await manager.viewHandler(fontinfo("ana"), view="fontinfo")
        assert "read-only=true" in str(moved.value.location)
        assert "project=Mutator" in str(moved.value.location)
        # Not again once there, nor for the listed designer, a manager, or
        # the editor.
        for user, query, view in [
            ("ana", "project=Mutator&read-only=true", "fontinfo"),
            ("fabio", "project=Mutator", "fontinfo"),
            ("mei", "project=Mutator", "fontinfo"),
            ("ana", "project=Mutator", "editor"),
        ]:
            try:
                await manager.viewHandler(fontinfo(user, query), view=view)
            except web.HTTPFound:  # pragma: no cover
                pytest.fail(f"{user} {view} redirected")
            except web.HTTPNotFound:
                pass  # no Fontra client built here: the redirect check passed

    run(go())


# --- the font's snapshot ------------------------------------------------------------


def snapshot(user, name="v1", glyph=None):
    query = f"name={name}" + (f"&glyph={glyph}" if glyph else "")
    return make_mocked_request(
        "POST",
        f"/api/hive/projects/Mutator/snapshot?{query}",
        match_info={"name": "Mutator"},
        headers={"Cookie": f"{DEV_USER_COOKIE}={user}"},
    )


def test_the_fonts_snapshot_takes_font_settings(manager, directory):
    set_writers(directory, {"fontsettings": ["fabio"]})

    async def go():
        with pytest.raises(web.HTTPForbidden) as refused:
            await manager.snapshotHandler(snapshot("ana"))
        assert "Font settings" in refused.value.text
        # A glyph's snapshot stays any designer's.
        made = await manager.glyphSnapshotHandler(snapshot("ana", "a1", glyph="B"))
        assert made.status == 200
        assert (await manager.snapshotHandler(snapshot("fabio"))).status == 200
        # A manager and an admin pass the check too (then nothing is new
        # since "v1": the conflict says so).
        for user, name in (("mei", "v2"), ("jeremie", "v3")):
            with pytest.raises(web.HTTPConflict):
                await manager.snapshotHandler(snapshot(user, name))
        await manager.aclose()

    run(go())
