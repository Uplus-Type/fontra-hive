"""Restoring the whole font, or a part of it, from a version or a snapshot
(``restore-font``), and downloading a snapshot (``export?ref=snapshot/…``)."""

import json
import pathlib
import tempfile
import types

import pytest
from aiohttp import web

pytest.importorskip("fontra")

from conftest import FIXTURE, run  # noqa: E402
from fontra_hive import fontdiff, fontrestore  # noqa: E402
from fontra_hive.access import DevDirectory, token_for  # noqa: E402
from fontra_hive.gitstore import GitRepoStore, Signature  # noqa: E402
from fontra_hive.projectmanager import (  # noqa: E402
    DEV_USER_COOKIE,
    DevHiveProjectManager,
    glyphPath,
)

ME = Signature("Import", "import@example.com")
FONT_DATA = json.loads((FIXTURE / "font-data.json").read_text())
GLYPH_A = json.loads((FIXTURE / "glyphs" / "A^1.json").read_text())
BLACK = "blk00001"

USERS = {
    "users": {
        "jeremie": {"name": "Jérémie Hornus"},
        "fabio": {"name": "Fabio Rossi"},
        "ana": {"name": "Ana López"},
        "mei": {"name": "Mei Chen"},
        "rita": {"name": "Rita Reviewer"},
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
            "collaborators": {"mei": "manager", "rita": "reviewer"},
            # Font settings and plug-in data: managers only; the kerning: Ana.
            "writers": {
                "fontsettings": "managers",
                "plugindata": "managers",
                "kerning": ["ana"],
            },
        },
    },
}


# --- a history: an import, "Round 1", then a day of work ------------------------------


def font_data(version=1, rules=1, status="In progress", wght_max=900, black=False):
    data = json.loads(json.dumps(FONT_DATA))
    data["fontInfo"] = {"versionMajor": 1, "versionMinor": version}
    data["axes"]["axes"][1]["maxValue"] = wght_max
    data["customData"] = {
        "umarks.rules": {"version": rules},
        "fontra.sourceStatusFieldDefinitions": [
            {"label": status, "color": [1, 0, 0, 1], "value": 0, "isDefault": True}
        ],
        "com.example.note": "kept",
    }
    if black:
        data["sources"][BLACK] = {"name": "Black", "location": {"weight": 850, "width": 500}}
    return (json.dumps(data, indent=0) + "\n").encode()


def kerning(value):
    from fontra.backends.fontra import writeKerningFile
    from fontra.core.classes import Kerning

    table = Kerning(
        groupsSide1={},
        groupsSide2={},
        sourceIdentifiers=["5bea6334"],
        values={"A": {"B": [value]}},
    )
    with tempfile.TemporaryDirectory() as folder:
        path = pathlib.Path(folder) / "kerning.csv"
        writeKerningFile(path, {"kern": table})
        return path.read_bytes()


def glyph_info(rows):
    return ("glyph name;code points\n" + "".join(f"{r}\n" for r in rows)).encode()


def glyph(name, width):
    data = json.loads(json.dumps(GLYPH_A))
    data["name"] = name
    for layer in data["layers"].values():
        layer["glyph"]["xAdvance"] = width
    return (json.dumps(data, indent=0) + "\n").encode()


ROUND_1 = {
    "font-data.json": font_data(),
    "features.txt": b"feature liga {\n  sub f i by f_i;\n} liga;\n",
    "kerning.csv": kerning(-20),
    "glyph-info.csv": glyph_info(["A;U+0041", "A.alt", "B;U+0042"]),
}

LATER = {
    "font-data.json": font_data(version=2, rules=2, status="Done", wght_max=950),
    "features.txt": b"feature liga {\n  sub f l by f_l;\n} liga;\n",
    "kerning.csv": kerning(-35),
    "glyph-info.csv": glyph_info(["A;U+0041,U+0061", "B;U+0042", "C;U+0043"]),
    glyphPath("B"): glyph("B", 777),
    glyphPath("C"): glyph("C", 500),
    glyphPath("A.alt"): None,
    "workflows/build.yaml": b"steps: []\n",
}


def with_black():
    """A source "Black" added, and A drawn in it."""
    data = json.loads(GLYPH_A_TEXT())
    data["sources"].append(
        {"name": "Black", "layerName": "Black/foreground", "locationBase": BLACK}
    )
    data["layers"]["Black/foreground"] = next(iter(data["layers"].values()))
    return {
        "font-data.json": font_data(version=2, rules=2, status="Done", wght_max=950, black=True),
        glyphPath("A"): (json.dumps(data, indent=0) + "\n").encode(),
    }


def GLYPH_A_TEXT():
    return json.dumps(GLYPH_A)


def directory_file(tmp_path, users=USERS):
    path = tmp_path / "hive-dev-users.json"
    path.write_text(json.dumps(users), encoding="utf-8")
    return path


@pytest.fixture
def hive(tmp_path, fixture_fontra):
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    store.commit(ROUND_1, message="Round 1 work", author=ME)
    round1 = store.create_snapshot("Round 1", author=ME)
    store.commit(LATER, message="A day of work", author=ME)
    store.close()
    manager = DevHiveProjectManager(
        root, directory=DevDirectory(directory_file(tmp_path)), commitDelay=0.05
    )
    return types.SimpleNamespace(root=root, manager=manager, round1=round1.sha)


def request(user, **query):
    return types.SimpleNamespace(
        match_info={"name": "Mutator"},
        query=query,
        cookies={DEV_USER_COOKIE: user} if user else {},
        headers={},
        host="localhost:8000",
        method="POST",
    )


def restore(hive, user="mei", **query):
    query.setdefault("ref", hive.round1)
    response = run(hive.manager.restoreFontHandler(request(user, **query)))
    return response.status, json.loads(response.body)


def store_of(hive):
    return GitRepoStore.open(hive.root / "Mutator.git")


def tree(hive, ref="main"):
    store = store_of(hive)
    try:
        sha = store.resolve(ref)
        return {path: store.read_file(sha, path) for path in store.list_tree(sha)}
    finally:
        store.close()


def areas_between(old, new):
    keys = fontdiff.FONT_FILES
    return {c.area for c in fontdiff.font_changes({k: old.get(k) for k in keys}, {k: new.get(k) for k in keys})}


# --- the whole font -------------------------------------------------------------------


def test_the_whole_font_comes_back_and_the_other_files_stay(hive):
    head = tree(hive)
    store = store_of(hive)
    before = store.head("main")
    store.close()
    status, data = restore(hive)
    assert status == 200 and data["changed"] and data["parts"] is None
    assert data["title"] == "Restore the whole font to “Round 1”"
    assert data["result"].startswith("The whole font is back as at “Round 1”")
    after, then = tree(hive), tree(hive, hive.round1)
    font = lambda files: {p: d for p, d in files.items() if fontrestore.is_font_path(p)}  # noqa: E731
    assert font(after) == font(then)
    assert glyphPath("C") not in after  # made since: gone
    assert after[glyphPath("A.alt")] == then[glyphPath("A.alt")]  # deleted since: back
    assert after[glyphPath("B")] == then[glyphPath("B")]  # changed since: as then
    assert after["workflows/build.yaml"] == head["workflows/build.yaml"]  # not the font's

    store = store_of(hive)
    try:
        info = store.commit_info(data["head"])
    finally:
        store.close()
    assert info.parents == (before,)  # a new commit on top: nothing rewritten
    message = info.message
    assert f"Hive-Restore: {hive.round1}" in message
    assert "Hive-Font: fontsettings kerning plugindata" in message
    assert "Hive-Glyphs: A.alt B C" in message
    assert "Glyphs: 1 changed, 1 brought back, 1 removed" in message


def test_the_whole_font_is_the_managers(hive):
    for user in ("fabio", "ana", "rita"):
        with pytest.raises(web.HTTPForbidden) as error:
            restore(hive, user=user)
        assert error.value.text == "Restoring the whole font is for the managers of this project."
    status, data = restore(hive, user="jeremie")  # the owner: an admin
    assert status == 200 and data["changed"]


# --- each part alone ------------------------------------------------------------------


@pytest.mark.parametrize("part", [p for p in fontrestore.PARTS if p != "glyphInfo"])
def test_each_part_alone_and_the_rest_of_the_head_intact(hive, part):
    head = tree(hive)
    status, data = restore(hive, parts=part)
    assert status == 200, data
    assert data["changed"], part
    after, then = tree(hive), tree(hive, hive.round1)
    areas = set(fontrestore.PARTS[part].areas)
    # Only the part changed…
    assert areas_between(head, after) and areas_between(head, after) <= areas
    # …and it is as it was at "Round 1".
    assert not areas_between(then, after) & areas
    # Nothing else of the repository moved.
    for path in set(head) | set(after):
        if path not in fontdiff.FONT_FILES:
            assert head.get(path) == after.get(path), path
    words = fontrestore.PARTS[part].words
    assert data["title"] == f"Restore {words} from “Round 1”"


def test_the_glyph_info_of_the_glyphs_there_now(hive):
    status, data = restore(hive, user="fabio", parts="glyphInfo")
    assert status == 200 and data["changed"]
    glyphMap, _ = fontdiff.read_glyph_info(tree(hive)["glyph-info.csv"])
    # A as then; C (made since) keeps its row; A.alt (deleted since) stays out.
    assert glyphMap == {"A": [0x41], "B": [0x42], "C": [0x43]}


def test_several_parts_and_their_custom_data_keys(hive):
    status, data = restore(hive, parts="kerning,features,pluginData")
    assert status == 200
    assert data["parts"] == ["features", "kerning", "pluginData"]  # in PARTS order
    assert data["title"] == "Restore features, kerning, plug-in data from “Round 1”"
    font = json.loads(tree(hive)["font-data.json"])
    assert font["customData"]["umarks.rules"] == {"version": 1}  # restored
    assert font["customData"]["fontra.sourceStatusFieldDefinitions"][0]["label"] == "Done"  # kept
    assert font["customData"]["com.example.note"] == "kept"
    assert font["fontInfo"]["versionMinor"] == 2  # kept


def test_nothing_to_restore(hive):
    restore(hive, parts="features")
    status, data = restore(hive, parts="features")
    assert status == 200 and data["changed"] is False
    assert data["result"] == "Features are already as at “Round 1”: nothing to restore."


def test_a_version_is_said_by_its_date(hive):
    """Not a snapshot: the commit dates it in UTC, the sentence in the
    person's time (the browser's getTimezoneOffset)."""
    import datetime

    store = store_of(hive)
    day = int(datetime.datetime(2026, 11, 19, 15, 40, tzinfo=datetime.timezone.utc).timestamp())
    version = store.commit(
        {"features.txt": b"# then\n"}, message="Features then", author=ME, timestamp=day
    )
    store.commit({"features.txt": b"# now\n"}, message="Features now", author=ME)
    store.close()
    status, data = restore(hive, ref=version, parts="features", tzOffset="-60")
    assert status == 200
    assert data["title"] == "Restore features from the version of 19 Nov 2026, 15:40 UTC"
    assert data["result"].startswith(
        "Features are back as at the version of 19 Nov 2026, 16:40, in a new version."
    )
    # Without the browser's time zone, or with nonsense: UTC.
    restore(hive, parts="features")
    status, data = restore(hive, ref=version, parts="features", tzOffset="lots")
    assert "19 Nov 2026, 15:40 UTC" in data["result"]


# --- who may restore what -------------------------------------------------------------


@pytest.mark.parametrize(
    "user, part, refused",
    [
        ("fabio", "fontInfo", "You cannot restore Font info: font settings are kept to some people on this project. Ask a manager."),
        ("fabio", "axesAndSources", "You cannot restore Axes and sources: font settings are kept"),
        ("fabio", "features", "You cannot restore Features: font settings are kept"),
        ("fabio", "statusDefinitions", "You cannot restore Development status: font settings are kept"),
        ("fabio", "kerning", "You cannot restore Kerning: kerning is kept to some people"),
        ("fabio", "pluginData", "You cannot restore Plug-in data: plug-in data is kept"),
        ("rita", "glyphInfo", "You cannot restore Glyph info: your role cannot change this font."),
        ("ana", "kerning", None),
        ("fabio", "glyphInfo", None),
        ("mei", "features", None),
    ],
)
def test_each_part_takes_the_capability_of_its_domain(hive, user, part, refused):
    if refused is None:
        status, data = restore(hive, user=user, parts=part)
        assert status == 200 and data["changed"]
        return
    head = tree(hive)
    with pytest.raises(web.HTTPForbidden) as error:
        # With a part the person may restore: the refusal names the other.
        restore(hive, user=user, parts=f"glyphInfo,{part}" if user != "rita" else part)
    assert error.value.text.startswith(refused)
    assert tree(hive) == head  # nothing committed


def test_one_refused_part_refuses_the_restoration(hive):
    head = tree(hive)
    with pytest.raises(web.HTTPForbidden) as error:
        restore(hive, user="ana", parts="kerning,features")
    assert "Features" in error.value.text
    assert tree(hive) == head


def test_unknown_ref_part_and_missing_ref(hive):
    with pytest.raises(web.HTTPNotFound):
        restore(hive, ref="0" * 40)
    with pytest.raises(web.HTTPNotFound):
        restore(hive, ref="snapshot/nope")
    with pytest.raises(web.HTTPBadRequest):
        restore(hive, parts="everything")
    with pytest.raises(web.HTTPBadRequest):
        restore(hive, parts="")
    with pytest.raises(web.HTTPBadRequest):
        run(hive.manager.restoreFontHandler(request("mei")))
    hive.manager.readOnly = True
    with pytest.raises(web.HTTPForbidden):
        restore(hive, parts="features")


# --- the guard ------------------------------------------------------------------------


def test_axes_and_sources_refused_when_glyphs_use_a_source_gone(hive):
    store = store_of(hive)
    store.commit(with_black(), message="Black", author=ME)
    store.close()
    head = tree(hive)
    status, data = restore(hive, parts="axesAndSources")
    assert status == 409
    assert data == {
        "message": "1 glyph has layers for the source Black, which this version "
        "doesn't have. Restore the whole font instead.",
        "wholeFont": True,
    }
    assert tree(hive) == head  # nothing committed
    # The whole font, glyphs included, holds together.
    status, data = restore(hive)
    assert status == 200 and data["changed"]


def test_a_sparse_source_is_not_refused(hive):
    """A version with a font source "Black" that no glyph draws: restoring
    its axes and sources onto a head without it is fine."""
    store = store_of(hive)
    sparse = store.commit(
        {"font-data.json": font_data(version=2, rules=2, status="Done", black=True)},
        message="Black, not drawn yet",
        author=ME,
    )
    store.commit({"font-data.json": LATER["font-data.json"]}, message="No Black", author=ME)
    store.close()
    status, data = restore(hive, parts="axesAndSources", ref=sparse)
    assert status == 200 and data["changed"]
    assert BLACK in json.loads(tree(hive)["font-data.json"])["sources"]


class FakeStore:
    def __init__(self, commits):
        self.commits = commits  # sha -> {path: bytes}

    def list_tree(self, sha):
        return {path: f"{sha}:{path}" for path in self.commits[sha]}

    def read_blob(self, blob):
        sha, path = blob.split(":", 1)
        return self.commits[sha][path]


def test_the_guard_counts_glyphs_off_the_axes():
    def located(name, weight):
        return json.dumps(
            {"name": name, "sources": [{"name": "x", "layerName": "x", "location": {"weight": weight}}], "layers": {}}
        ).encode()

    narrow = json.loads(json.dumps(FONT_DATA))
    narrow["axes"]["axes"][1]["minValue"] = 400  # 150 (design) is off now
    for source in narrow["sources"].values():
        source["location"]["weight"] = max(source["location"]["weight"], 450)  # in the sources' coordinates
    store = FakeStore(
        {
            "head": {
                "font-data.json": json.dumps(FONT_DATA).encode(),
                glyphPath("a"): located("a", 150),
                glyphPath("b"): located("b", 150),
                glyphPath("c"): located("c", 850),
            },
            "old": {"font-data.json": json.dumps(narrow).encode()},
        }
    )
    changes, head, old = fontrestore.restore_changes(store, "head", "old", ["axesAndSources"])
    problem = fontrestore.coherence_problem(head, old, changes, wholeFont=False)
    assert problem == (
        "2 glyphs have sources outside the wght axis of this version (400–900). "
        "Restore the whole font instead."
    )
    # A glyph's own axis is its own business.
    store.commits["head"][glyphPath("a")] = json.dumps(
        {"name": "a", "axes": [{"name": "weight", "minValue": 0, "defaultValue": 0, "maxValue": 1}],
         "sources": [{"name": "x", "layerName": "x", "location": {"weight": 0.5}}], "layers": {}}
    ).encode()
    store.commits["head"][glyphPath("b")] = located("b", 850)
    changes, head, old = fontrestore.restore_changes(store, "head", "old", ["axesAndSources"])
    assert fontrestore.coherence_problem(head, old, changes, wholeFont=False) is None
    # An axis this version doesn't have.
    gone = json.loads(json.dumps(FONT_DATA))
    gone["axes"]["axes"] = gone["axes"]["axes"][1:]
    for source in gone["sources"].values():
        source["location"].pop("width")
    store.commits["old"] = {"font-data.json": json.dumps(gone).encode()}
    store.commits["head"][glyphPath("c")] = json.dumps(
        {"name": "c", "sources": [{"name": "x", "layerName": "x", "location": {"width": 0}}], "layers": {}}
    ).encode()
    changes, head, old = fontrestore.restore_changes(store, "head", "old", ["axesAndSources"])
    assert fontrestore.coherence_problem(head, old, changes, wholeFont=True) == (
        "1 glyph has sources on the axis width, which this version doesn't have."
    )


# --- with the editor open -------------------------------------------------------------


def test_pending_edits_are_committed_first_and_editors_reload(hive):
    async def go():
        manager = hive.manager
        token = token_for("mei")
        fontHandler = await manager.getRemoteSubject("Mutator", token)
        backend = fontHandler.backend
        received = []

        async def callback(pattern):
            received.append(pattern)

        await backend.watchExternalChanges(callback)
        glyphB = await backend.getGlyph("B")
        for layer in glyphB.layers.values():
            layer.glyph.xAdvance = 888
        await backend.putGlyph("B", glyphB, [0x42])  # pending, not committed yet
        assert (await backend.getFeatures()).text.startswith("feature liga {\n  sub f l")

        response = await manager.restoreFontHandler(request("mei", ref=hive.round1, parts="features,kerning"))
        data = json.loads(response.body)
        assert data["changed"]
        store = store_of(hive)
        try:
            log = store.commits_between(hive.round1, data["head"])
        finally:
            store.close()
        # The edit, then the restoration on top of it.
        assert [c.message.splitlines()[0] for c in log][:2] == [
            "Restore features, kerning from “Round 1”",
            "Edit B",
        ]
        assert json.loads(tree(hive)[glyphPath("B")])["layers"]
        assert all(
            layer["glyph"]["xAdvance"] == 888
            for layer in json.loads(tree(hive)[glyphPath("B")])["layers"].values()
        )
        # The open backend reloaded what changed.
        assert backend.tree.commit_sha == data["head"]
        assert (await backend.getFeatures()).text == ROUND_1["features.txt"].decode()
        kerning = await backend.getKerning()
        assert kerning["kern"].values["A"]["B"] == [-20]
        assert received
        await manager.aclose()

    run(go())


def test_a_part_deleted_then_is_deleted_in_the_open_editor_too(hive):
    """A version without kerning: the kerning file goes, and the open
    backend forgets it (Fontra reads it again only when it exists)."""

    async def go():
        manager = hive.manager
        fontHandler = await manager.getRemoteSubject("Mutator", token_for("mei"))
        backend = fontHandler.backend
        async def callback(pattern):
            pass

        await backend.watchExternalChanges(callback)
        assert (await backend.getKerning())["kern"].values
        store = store_of(hive)
        importSha = store.commits_between(None, "main")[-1].sha
        store.close()
        response = await manager.restoreFontHandler(request("mei", ref=importSha, parts="kerning,features"))
        assert json.loads(response.body)["changed"]
        assert "kerning.csv" not in tree(hive) and "features.txt" not in tree(hive)
        assert await backend.getKerning() == {}
        assert (await backend.getFeatures()).text == ""
        await manager.aclose()

    run(go())


# --- the history says it --------------------------------------------------------------


def test_the_history_tells_the_restoration(hive):
    restore(hive, parts="features,kerning")

    async def go():
        response = await hive.manager.logHandler(request("mei", kind="font"))
        return json.loads(response.body)

    log = run(go())
    first = log["commits"][0]
    assert first["message"].splitlines()[0] == "Restore features, kerning from “Round 1”"
    said = [c["summary"] for c in first["font"]]
    assert said == ["Features: 1 line added, 1 removed", "Kerning: 1 pair changed (LightCondensed)"]
    assert {c["area"] for c in first["font"]} == {"features", "kerning"}


# --- downloading a snapshot -----------------------------------------------------------


def call_export(hive, monkeypatch, **query):
    from fontra_hive import projectmanager

    sent = {}

    async def sendFile(request, path):
        sent.update(name=path.name, data=path.read_bytes())
        return "sent"

    async def authorize(request):
        return token_for("mei")

    monkeypatch.setattr(projectmanager, "_sendFile", sendFile)
    monkeypatch.setattr(hive.manager, "authorize", authorize)
    req = types.SimpleNamespace(match_info={"name": "Mutator"}, query=query)
    return run(hive.manager.exportHandler(req)), sent


def test_download_a_snapshot_as_it_was(hive, monkeypatch):
    import io
    import zipfile

    response, sent = call_export(hive, monkeypatch, format="fontra", ref="snapshot/round-1")
    assert response == "sent"
    assert sent["name"] == "Mutator-round-1.fontra.zip"
    files = zipfile.ZipFile(io.BytesIO(sent["data"]))
    names = files.namelist()
    features = next(n for n in names if n.endswith("features.txt"))
    assert files.read(features) == ROUND_1["features.txt"]  # not the head's
    assert not any(n.endswith("C^1.json") for n in names)
    assert any(n.endswith("A.alt^1.json") for n in names)


@pytest.mark.parametrize(
    "query, error",
    [
        ({"ref": "main"}, web.HTTPBadRequest),
        ({"ref": hex(0)[2:] * 40}, web.HTTPBadRequest),
        ({"ref": "glyph-snapshot/x"}, web.HTTPBadRequest),
        ({"ref": "archive/old"}, web.HTTPBadRequest),
        ({"ref": "snapshot/"}, web.HTTPBadRequest),
        ({"ref": "snapshot/round-1", "branch": "main"}, web.HTTPBadRequest),
        ({"ref": "snapshot/nope"}, web.HTTPNotFound),
        ({"ref": "snapshot/../heads/main"}, web.HTTPNotFound),
    ],
)
def test_download_refuses_any_other_reference(hive, monkeypatch, query, error):
    with pytest.raises(error):
        call_export(hive, monkeypatch, format="fontra", **query)


def test_download_of_a_snapshot_takes_export(hive, monkeypatch):
    from fontra_hive import projectmanager

    async def authorize(request):
        return token_for("fabio")

    monkeypatch.setattr(hive.manager, "authorize", authorize)
    req = types.SimpleNamespace(match_info={"name": "Mutator"}, query={"format": "fontra", "ref": "snapshot/round-1"})
    with pytest.raises(web.HTTPForbidden):
        run(hive.manager.exportHandler(req))
    assert projectmanager  # imported for the monkeypatch of the other tests
