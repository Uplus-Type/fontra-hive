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
