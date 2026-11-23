"""RV-1 (re-check of the R2c audit, 23 Nov 2026): a snapshot is known by
its tag ``snapshot/<name>`` on the commit, never by the ``Hive-Snapshot``
trailer alone. A commit forged before the fix with a blank line in a glyph
name ends with a paragraph its author wrote entirely: it has no tag, so it
stays an ordinary commit, in the snapshots, in the history and in a new
snapshot's count."""

import asyncio
import json

import pytest

from fontra_hive.gitstore import GitRepoStore, Signature, hive_glyphs

pytest.importorskip("fontra")

from r2c_bench import body, make_manager, request, store_of  # noqa: E402

ME = Signature("Import", "import@example.com")


def old_commit_message(changes):
    """``_commit_message`` as it was before R2c (4d2c8e8): what a repository
    in service may have received before the fix."""
    glyphs = sorted(
        p[len("glyphs/") : -len(".json")] for p in changes if p.startswith("glyphs/")
    )
    others = sorted(p for p in changes if not p.startswith("glyphs/"))
    parts = []
    if glyphs:
        parts.append("Edit " + ", ".join(glyphs[:8]))
    if others:
        parts.append("Update " + ", ".join(others))
    title = "; ".join(parts) or "Update"
    trailer = f"\n\nHive-Glyphs: {' '.join(glyphs)}" if glyphs else ""
    return title + trailer + "\n"


def edited_a(store):
    a = store.read_file(store.head(), "glyphs/A^1.json")
    glyph = json.loads(a)
    layer = next(iter(glyph["layers"].values()))
    layer["glyph"]["xAdvance"] = layer["glyph"].get("xAdvance", 0) + 1
    return a, (json.dumps(glyph) + "\n").encode()


def forge(store, title):
    """Fabio's edit of A, with a new glyph whose name holds a blank line and
    a ``Hive-Snapshot`` trailer, committed as before the fix."""
    a, edited = edited_a(store)
    names = {"glyphs/A.json": edited, f"glyphs/zz\n\nHive-Snapshot: {title}.json": a}
    message = old_commit_message(names)
    assert message.endswith(f"\n\nHive-Snapshot: {title}\n")
    return store.commit(
        {"glyphs/A^1.json": edited}, message=message, author=Signature("Fabio", "f@x")
    ), message


def test_an_old_commit_forged_with_a_blank_line_is_no_snapshot(tmp_path, fixture_fontra):
    """The re-check's proof, inverted."""
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        forged, message = forge(store, "Approved before the fix")
        assert store.snapshots() == []
        assert store.latest_snapshot() is None
        assert store.snapshot_at(forged) is None
    finally:
        store.close()

    async def go():
        snaps = await body(await manager.snapshotsHandler(request("Mutator", "ana")))
        hist = await body(
            await manager.logHandler(request("Mutator", "ana", glyph="A"))
        )
        return snaps, hist

    snaps, hist = asyncio.run(go())
    assert snaps["snapshots"] == []
    assert snaps["pending"] == 2  # the import and the forged commit
    assert forged in [c["sha"] for c in hist["commits"]]
    assert hive_glyphs(message) is None


def test_a_real_snapshot_groups_the_forged_commit(tmp_path, fixture_fontra):
    """A snapshot made afterwards counts the forged commit among the changes
    it groups, and starts from the beginning (no false base)."""
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        forged, _ = forge(store, "Approved before the fix")
        snap = store.create_snapshot("Round 1", author=Signature("Mei", "m@x"))
        assert snap.base is None
        assert snap.changes == 2
        assert [s.name for s in store.snapshots()] == ["round-1"]
        assert store.latest_snapshot().sha == snap.sha
        assert store.snapshot_at(snap.sha).name == "round-1"
        # In the history of A: the forged edit is there, the snapshot (no
        # file changed) is skipped and groups it.
        found: list = []
        log = store.log(path="glyphs/A^1.json", glyph="A", snapshots=found)
        assert forged in [c.sha for c in log]
        assert snap.sha not in [c.sha for c in log]
        assert [s.name for s in found] == ["round-1"]
        assert next(c for c in log if c.sha == forged).snapshot == "round-1"
    finally:
        store.close()


def test_a_forged_trailer_that_names_a_real_snapshot_is_not_it(tmp_path, fixture_fontra):
    """The trailer names a snapshot that exists: its tag points elsewhere,
    so the forged commit is still an ordinary commit."""
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        a, edited = edited_a(store)
        store.commit({"glyphs/A^1.json": edited}, message="Edit A\n", author=ME)
        real = store.create_snapshot("Round 1", author=Signature("Mei", "m@x"))
        forged, _ = forge(store, "round-1")
        assert store.snapshot_at(forged) is None
        assert [s.sha for s in store.snapshots()] == [real.sha]
        assert store.latest_snapshot().sha == real.sha
        log = store.log(path="glyphs/A^1.json", glyph="A", snapshots=[])
        assert forged in [c.sha for c in log]
    finally:
        store.close()


def test_a_tagged_snapshot_that_changes_files_is_shown_in_path_mode(tmp_path):
    """In path mode, a snapshot is skipped only when its tree is its parent's:
    a commit that changes the file is never hidden, whatever its message."""
    store = GitRepoStore.create(tmp_path / "f.git")
    store.commit({"glyphs/A.json": b"1"}, message="Import\n", author=ME)
    odd = store.commit(
        {"glyphs/A.json": b"2"},
        message="Snapshot: odd\n\nHive-Snapshot: odd\nHive-Snapshot-Base: none\n",
        author=ME,
    )
    store.create_tag("snapshot/odd", odd)
    found: list = []
    log = store.log(path="glyphs/A.json", snapshots=found)
    assert odd in [c.sha for c in log]
    assert [s.name for s in found] == ["odd"]
