import subprocess

import pytest

from conftest import files_of
from fontra_hive.gitstore import GitRepoStore, RefMovedError, Signature

ME = Signature("Jérémie", "j@example.com")


def make_store(tmp_path, fixture_fontra):
    store = GitRepoStore.create(tmp_path / "proj.git")
    sha = store.import_directory(fixture_fontra, message="Import", author=ME)
    return store, sha


def test_import_and_export_are_identical(tmp_path, fixture_fontra):
    store, sha = make_store(tmp_path, fixture_fontra)
    assert store.head() == sha
    assert store.branches() == ["main"]
    out = tmp_path / "out.fontra"
    store.export("main", out)
    assert files_of(out) == files_of(fixture_fontra)


def test_commit_diff_log_and_cas(tmp_path, fixture_fontra):
    store, sha1 = make_store(tmp_path, fixture_fontra)
    sha2 = store.commit(
        {"glyphs/A^1.json": b"{}\n", "features.txt": b"# fea\n"},
        message="Edit A",
        author=ME,
        expected_head=sha1,
    )
    assert store.head() == sha2
    assert {(c.kind, c.path) for c in store.diff(sha1, sha2)} == {
        ("modify", "glyphs/A^1.json"),
        ("add", "features.txt"),
    }
    log = store.log(path="glyphs/A^1.json")
    assert [c.message for c in log] == ["Edit A", "Import"]
    assert log[0].author == "Jérémie" and log[0].email == "j@example.com"
    assert store.read_file(sha2, "features.txt") == b"# fea\n"

    with pytest.raises(RefMovedError):
        store.commit({"x": b"y"}, message="stale", author=ME, expected_head=sha1)
    assert store.head() == sha2  # nothing moved

    sha3 = store.commit(
        {"features.txt": None}, message="rm", author=ME, expected_head=sha2
    )
    assert "features.txt" not in store.list_tree(sha3)


def test_branches_tags_and_fast_forward(tmp_path, fixture_fontra):
    store, sha1 = make_store(tmp_path, fixture_fontra)
    store.create_branch("bold")
    sha2 = store.commit(
        {"glyphs/A^1.json": b"{}\n"}, branch="bold", message="bold A", author=ME
    )
    assert store.branches() == ["bold", "main"]
    assert store.head("bold") == sha2 and store.head("main") == sha1
    with pytest.raises(ValueError):
        store.create_branch("bold")

    store.create_tag("v0.1", "bold", "first version", tagger=ME)
    assert store.tags() == ["v0.1"]
    assert store.resolve("v0.1") == sha2

    assert store.is_ancestor(sha1, sha2)
    assert not store.is_ancestor(sha2, sha1)
    assert store.fast_forward("main", "bold") == sha2
    assert store.head("main") == sha2

    # Diverge: a commit on each branch, then neither can fast-forward to the other.
    store.commit({"glyphs/B^1.json": None}, branch="main", message="rm B", author=ME)
    store.commit(
        {"features.txt": b"# bold\n"}, branch="bold", message="bold fea", author=ME
    )
    with pytest.raises(ValueError):
        store.fast_forward("main", "bold")
    with pytest.raises(ValueError):
        store.fast_forward("bold", "main")


def test_repository_is_readable_by_git(tmp_path, fixture_fontra):
    """The bare repository must be a normal git repository."""
    store, sha = make_store(tmp_path, fixture_fontra)
    store.commit({"glyphs/A^1.json": b"{}\n"}, message="Edit A", author=ME)
    git = subprocess.run(
        ["git", "--git-dir", str(store.path), "log", "--format=%an|%s"],
        capture_output=True,
        text=True,
    )
    assert git.returncode == 0, git.stderr
    assert git.stdout.splitlines() == ["Jérémie|Edit A", "Jérémie|Import"]
    fsck = subprocess.run(
        ["git", "--git-dir", str(store.path), "fsck", "--strict"],
        capture_output=True,
        text=True,
    )
    assert fsck.returncode == 0, fsck.stderr
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(store.path), str(clone)], check=True)
    assert (clone / "font-data.json").is_file() and (clone / "glyphs").is_dir()


def test_glyph_log_uses_trailers_and_falls_back_to_trees(tmp_path, fixture_fontra):
    store, sha1 = make_store(tmp_path, fixture_fontra)  # "Import": no trailer
    store.commit(
        {"glyphs/A^1.json": b"{}\n"}, message="Edit A\n\nHive-Glyphs: A\n", author=ME
    )
    store.commit(
        {"glyphs/B^1.json": b"{}\n"}, message="Edit B\n\nHive-Glyphs: B\n", author=ME
    )
    store.commit({"glyphs/A^1.json": b"[]\n"}, message="external edit of A", author=ME)
    messages = [
        c.message.splitlines()[0] for c in store.log(path="glyphs/A^1.json", glyph="A")
    ]
    assert messages == ["external edit of A", "Edit A", "Import"]
    assert [c.message for c in store.log(path="glyphs/B^1.json", glyph="B")] == [
        "Edit B\n\nHive-Glyphs: B",
        "Import",
    ]
    # A commit whose trailer lists a glyph but which did not change its file is trusted as is.
    assert len(store.log(path="glyphs/A^1.json", glyph="A", limit=2)) == 2


def test_snapshot_slug():
    from fontra_hive.gitstore import snapshot_slug

    assert snapshot_slug("Relecture client n°1") == "relecture-client-n1"
    assert snapshot_slug("  v0.2 — épreuves  ") == "v0.2-epreuves"
    assert snapshot_slug("a..b.lock") == "a.b"
    assert snapshot_slug("***") == ""


def test_snapshots_group_commits_without_rewriting(tmp_path, fixture_fontra):
    other = Signature("Fabio", "f@example.com")
    store, imported = make_store(tmp_path, fixture_fontra)
    edit_a = store.commit(
        {"glyphs/A^1.json": b"{}\n"}, message="Edit A\n\nHive-Glyphs: A\n", author=ME
    )
    edit_b = store.commit(
        {"glyphs/B^1.json": b"{}\n"},
        message="Edit B\n\nHive-Glyphs: B\n",
        author=other,
    )
    head = store.head()

    first = store.create_snapshot("Relecture n°1", author=ME)
    assert first.name == "relecture-n1" and first.title == "Relecture n°1"
    assert first.base is None and first.changes == 3  # Import, Edit A, Edit B
    assert first.glyphs == ("A", "B") and first.author == "Jérémie"
    # An empty commit on top of the branch: same tree, nothing rewritten.
    assert store.head() == first.sha
    assert store.commit_info(first.sha).parents == (head,)
    assert store.tree_sha(first.sha) == store.tree_sha(head)
    assert store.diff(head, first.sha) == []
    assert [c.sha for c in store.log()][1:] == [edit_b, edit_a, imported]
    assert store.resolve("snapshot/relecture-n1") == first.sha
    message = store.commit_info(first.sha).message
    assert message.startswith(
        "Snapshot: Relecture n°1\n\n3 changes since the beginning"
    )
    assert "by Jérémie, Fabio." in message and "Hive-Snapshot: relecture-n1" in message

    # Nothing new: refused. Same name: refused.
    with pytest.raises(ValueError, match="nothing changed"):
        store.create_snapshot("again", author=ME)
    edit_a2 = store.commit(
        {"glyphs/A^1.json": b"[]\n"}, message="Edit A\n\nHive-Glyphs: A\n", author=ME
    )
    with pytest.raises(ValueError, match="already exists"):
        store.create_snapshot("Relecture  n°1", author=ME)
    with pytest.raises(ValueError, match="needs a name"):
        store.create_snapshot(" -- ", author=ME)
    with pytest.raises(RefMovedError):
        store.create_snapshot("v2", author=ME, expected_head=edit_a)

    second = store.create_snapshot("v2", author=other)
    assert second.base == first.sha and second.changes == 1 and second.glyphs == ("A",)
    assert [s.name for s in store.snapshots()] == ["v2", "relecture-n1"]
    assert store.latest_snapshot().sha == second.sha
    assert [c.sha for c in store.commits_between(first.sha, second.sha)] == [
        second.sha,
        edit_a2,
    ]

    # Per-glyph history, annotated with the snapshot each commit belongs to;
    # the snapshot commits themselves are not in it (they change no glyph).
    store.commit(
        {"glyphs/A^1.json": b"[1]\n"}, message="Edit A\n\nHive-Glyphs: A\n", author=ME
    )
    met = []
    log = store.log(path="glyphs/A^1.json", glyph="A", snapshots=met)
    assert [(c.message.splitlines()[0], c.snapshot) for c in log] == [
        ("Edit A", None),
        ("Edit A", "v2"),
        ("Edit A", "relecture-n1"),
        ("Import", "relecture-n1"),
    ]
    assert [s.name for s in met] == ["v2", "relecture-n1"]
    # The whole branch too, snapshot commits included.
    met = []
    full = store.log(snapshots=met)
    assert [c.snapshot for c in full] == [
        None,
        "v2",
        "v2",
        "relecture-n1",
        "relecture-n1",
        "relecture-n1",
        "relecture-n1",
    ]
    # Snapshots are ordinary tags and commits for git.
    assert "snapshot/v2" in store.tags()
