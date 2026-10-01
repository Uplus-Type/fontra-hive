"""Remote git repositories: pull into upstream/<branch>, push back, for
.fontra and UFO/designspace remotes (fontra_hive.remote)."""

import asyncio
import pathlib
import re
import threading
from contextlib import aclosing

import pytest
from conftest import FIXTURE, files_of

from fontra_hive import remote as hive_remote
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.remote import (
    NotMergedError,
    Remote,
    RemoteMovedError,
    UnsafeURLError,
    check_url,
    detect_font_paths,
    match_formatting,
    pull,
    push,
    restore_glif_details,
    status,
)

ALICE = Signature("Alice", "alice@example.com")
BOB = Signature("Bob", "bob@example.com")
OUTSIDER = Signature("Outsider", "outsider@example.com")


def _have_designspace() -> bool:
    try:
        import fontra.backends.designspace  # noqa: F401
    except Exception:
        return False
    return True


needs_designspace = pytest.mark.skipif(
    not _have_designspace(), reason="Fontra's designspace backend is not importable"
)


# --- helpers ------------------------------------------------------------------


def make_project(tmp_path) -> GitRepoStore:
    store = GitRepoStore.create(tmp_path / "project.git")
    store.import_directory(FIXTURE, message="Import", author=ALICE)
    return store


def make_remote(tmp_path, files: dict[str, bytes] | None, name="remote.git"):
    """A bare repository standing for GitHub; ``files`` its first commit."""
    path = tmp_path / name
    store = GitRepoStore.create(path)
    if files:
        store.commit(files, message="Initial commit", author=OUTSIDER)
    return store


def folder_files(folder: pathlib.Path, prefix: str) -> dict[str, bytes]:
    return {f"{prefix}/{p}": data for p, data in files_of(folder).items()}


def remote_changed_files(remote_store: GitRepoStore, ref="main") -> list[str]:
    head = remote_store.head(ref)
    parent = remote_store.commit_info(head).parents[0]
    return sorted(c.path for c in remote_store.diff(parent, head))


def edit_branch(store: GitRepoStore, branch: str, tmp_path, edit, message="Edit"):
    """Apply ``edit(backend)`` (async, on a Fontra backend of the branch) and
    commit the result on the branch."""
    from fontra.backends import getFileSystemBackend

    work = tmp_path / f"edit-{len(list(tmp_path.glob('edit-*')))}.fontra"
    store.export(branch, work)

    async def run():
        backend = getFileSystemBackend(work)
        async with aclosing(backend):
            await edit(backend)

    asyncio.run(run())
    return store.import_directory(work, branch=branch, message=message, author=BOB)


async def move_first_point(backend, glyph="A", source_index=0):
    glyph_obj = await backend.getGlyph(glyph)
    layer = glyph_obj.layers[glyph_obj.sources[source_index].layerName]
    layer.glyph.path.coordinates[0] += 7
    glyph_map = await backend.getGlyphMap()
    await backend.putGlyph(glyph, glyph_obj, glyph_map[glyph])


# --- addresses ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b.git",
        "git@github.com:a/b.git",
        "file:///etc",
        "https://127.0.0.1/a.git",
        "https://10.1.2.3/a.git",
        "https://169.254.169.254/latest",
        "https://[::1]/a.git",
        "https://user:secret@github.com/a/b.git",
    ],
)
def test_unsafe_urls_are_refused(url):
    with pytest.raises(UnsafeURLError):
        check_url(url)


def test_local_urls_when_allowed():
    check_url("/srv/repos/a.git", allow_local=True)
    check_url("http://127.0.0.1:8000/a.git", allow_local=True)


def test_remote_format_and_names():
    assert Remote("https://h/x.git").format == "fontra"
    assert Remote("https://h/x.git", path="sources/A.designspace").format == "ufo"
    assert Remote("https://h/x.git", path="A.ufo/").path == "A.ufo"
    r = Remote("https://h/x.git", branch="dev", password="tok")
    assert r.upstream_branch == "upstream/dev"
    assert "tok" not in repr(r)
    with pytest.raises(hive_remote.RemoteError):
        Remote("https://h/x.git", path="../x")


def test_upstream_is_a_reserved_branch_prefix(tmp_path):
    from fontra_hive.gitstore import branch_name_error

    assert branch_name_error("upstream/main")
    assert branch_name_error("upstreamish") is None


# --- .fontra remotes -----------------------------------------------------------


def fontra_remote(tmp_path):
    remote_store = make_remote(tmp_path, folder_files(FIXTURE, "Font.fontra"))
    remote = Remote(str(remote_store.path), path="Font.fontra")
    return remote_store, remote


def test_pull_forks_upstream_and_fast_forwards(tmp_path):
    store = make_project(tmp_path)
    remote_store, remote = fontra_remote(tmp_path)
    main = store.head("main")

    result = pull(store, remote, allow_local=True)
    assert result.branch == "upstream/main"
    assert result.remote_sha == remote_store.head("main")
    info = store.commit_info(result.commit)
    assert info.parents == (main,)
    assert "Hive-Upstream: " + result.remote_sha in info.message
    assert "Hive-Pull: " in info.message
    # Same font: nothing changed but the pull is recorded.
    assert store.list_tree(result.commit) == store.list_tree(main)

    # Nothing new on the remote: no new commit.
    again = pull(store, remote, allow_local=True)
    assert again.commit is None


def test_push_sends_only_the_changed_files(tmp_path):
    store = make_project(tmp_path)
    remote_store, remote = fontra_remote(tmp_path)
    pull(store, remote, allow_local=True)
    store.fast_forward("main", "upstream/main")
    before = remote_store.head("main")

    edit_branch(store, "main", tmp_path, move_first_point)
    st = status(store, remote, "main", allow_local=True)
    assert st.pending == ["glyphs/A^1.json"] and st.can_push

    result = push(
        store,
        remote,
        message="Proofs for the client",
        author=BOB,
        co_authors=[ALICE, BOB],
        allow_local=True,
    )
    assert result.pushed and result.previous == before
    assert result.files == ("Font.fontra/glyphs/A^1.json",)
    assert result.glyphs == ("A",)
    assert remote_store.head("main") == result.remote_sha
    info = remote_store.commit_info(result.remote_sha)
    assert info.parents == (before,)
    assert info.author == "Bob"
    assert info.message.startswith("Proofs for the client")
    assert "Co-authored-by: Alice <alice@example.com>" in info.message
    assert "Co-authored-by: Bob" not in info.message  # the author is not repeated
    assert remote_changed_files(remote_store) == ["Font.fontra/glyphs/A^1.json"]
    assert remote_store.read_file(
        result.remote_sha, "Font.fontra/glyphs/A^1.json"
    ) == store.read_file(store.head("main"), "glyphs/A^1.json")

    # Recorded on upstream/main; nothing left to push; the next pull is a no-op.
    record = store.commit_info(store.head("upstream/main"))
    assert f"Hive-Upstream: {result.remote_sha}" in record.message
    assert f"Hive-Push: {store.head('main')}" in record.message
    st = status(store, remote, "main", allow_local=True)
    assert st.pending == [] and not st.remote_moved and not st.unmerged
    assert pull(store, remote, allow_local=True).commit is None
    again = push(store, remote, message="Nothing", author=BOB, allow_local=True)
    assert not again.pushed


def test_push_refuses_when_the_remote_moved_then_pull_and_merge(tmp_path):
    store = make_project(tmp_path)
    remote_store, remote = fontra_remote(tmp_path)
    pull(store, remote, allow_local=True)
    store.fast_forward("main", "upstream/main")
    edit_branch(store, "main", tmp_path, move_first_point)

    # Someone pushes to GitHub meanwhile (glyph B).
    b_path = "Font.fontra/glyphs/B^1.json"
    b = remote_store.read_file(remote_store.head("main"), b_path)
    remote_store.commit(
        {b_path: b.replace(b'"xAdvance": ', b'"xAdvance": 1', 1)},
        message="Wider B",
        author=OUTSIDER,
    )
    assert status(store, remote, "main", allow_local=True).remote_moved
    with pytest.raises(RemoteMovedError):
        push(store, remote, message="x", author=BOB, allow_local=True)

    pulled = pull(store, remote, allow_local=True)
    assert pulled.glyphs == ("B",)
    with pytest.raises(NotMergedError):
        push(store, remote, message="x", author=BOB, allow_local=True)

    # Merge upstream/main into main, as the branch menu does.
    from fontra_hive.merge import merge_trees

    base = store.merge_base("main", "upstream/main")
    merged = merge_trees(
        store.list_tree(base),
        store.list_tree(store.head("main")),
        store.list_tree(pulled.commit),
        store.read_blob,
    )
    assert not merged.conflicts
    store.commit(
        merged.changes,
        message="Merge upstream/main",
        author=BOB,
        merge_parents=[pulled.commit],
    )
    result = push(store, remote, message="A and B", author=BOB, allow_local=True)
    assert result.pushed
    assert result.files == ("Font.fontra/glyphs/A^1.json",)
    tip = remote_store.head("main")
    assert remote_store.read_file(tip, b_path) == remote_store.read_file(
        remote_store.commit_info(tip).parents[0], b_path
    )


def test_first_push_to_an_empty_repository(tmp_path):
    store = make_project(tmp_path)
    remote_store = make_remote(tmp_path, None)
    remote = Remote(str(remote_store.path))
    assert pull(store, remote, allow_local=True).commit is None
    result = push(store, remote, message="Hello GitHub", author=ALICE, allow_local=True)
    assert result.pushed and result.previous is None
    assert remote_store.list_tree(remote_store.head("main")) == store.list_tree(
        store.head("main")
    )
    assert store.head("upstream/main")
    assert status(store, remote, "main", allow_local=True).pending == []


def test_pull_of_a_missing_font_path(tmp_path):
    store = make_project(tmp_path)
    remote_store, _ = fontra_remote(tmp_path)
    remote = Remote(str(remote_store.path), path="Other.fontra")
    with pytest.raises(hive_remote.RemoteError):
        pull(store, remote, allow_local=True)
    assert store.head("upstream/main") is None


def test_detect_font_paths(tmp_path):
    remote_store = make_remote(
        tmp_path,
        {
            "README.md": b"x",
            "sources/A.fontra/font-data.json": b"{}",
            "sources/B.designspace": b"<designspace/>",
            "sources/B-Bold.ufo/metainfo.plist": b"",
            "loose/C.ufo/metainfo.plist": b"",
        },
    )
    store = make_project(tmp_path)
    remote = Remote(str(remote_store.path))
    sha = hive_remote._fetch(store, remote, True)
    assert detect_font_paths(store, sha) == [
        "loose/C.ufo",
        "sources/A.fontra",
        "sources/B.designspace",
    ]


def test_push_and_pull_over_http(tmp_path):
    from wsgiref.simple_server import WSGIRequestHandler, make_server

    from dulwich.repo import Repo
    from dulwich.server import DictBackend
    from dulwich.web import make_wsgi_chain

    remote_store, _ = fontra_remote(tmp_path)
    backend = DictBackend({"/": Repo(str(remote_store.path))})

    class Quiet(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    server = make_server("127.0.0.1", 0, make_wsgi_chain(backend), handler_class=Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        remote = Remote(url, path="Font.fontra", username="x", password="token")
        store = make_project(tmp_path)
        pull(store, remote, allow_local=True)
        store.fast_forward("main", "upstream/main")
        edit_branch(store, "main", tmp_path, move_first_point)
        result = push(store, remote, message="Over HTTP", author=BOB, allow_local=True)
        assert result.pushed
        assert Repo(str(remote_store.path)).refs[b"refs/heads/main"] == (
            result.remote_sha.encode()
        )
        with pytest.raises(UnsafeURLError):
            pull(store, remote)  # http:// to a local address, not allowed
    finally:
        server.shutdown()


# --- UFO / designspace remotes -------------------------------------------------

DEFAULT_UFO = "Font_LightCondensed.ufo"
OTHER_UFO = "Font_BoldCondensed.ufo"
GUIDELINE = '<guideline x="120" name="stem" identifier="g1"/>'
COMMENT = b"<!-- made by hand, keep this -->"


def foreign_ufo_files(tmp_path) -> dict[str, bytes]:
    """The fixture as UFOs, formatted as another tool would: tabs, double
    quotes in the XML declaration; plus what Fontra does not keep: a
    non-kerning group, guideline identifiers, masters without <unicode>, a
    comment in the designspace."""
    from fontTools.misc import plistlib

    folder = tmp_path / "foreign"
    folder.mkdir()
    asyncio.run(hive_remote._copy_font(FIXTURE, folder / "Font.designspace"))
    for path in folder.rglob("*"):
        if path.suffix not in (".glif", ".plist", ".designspace"):
            continue
        text = path.read_bytes()
        text = text.replace(
            b"<?xml version='1.0' encoding='UTF-8'?>",
            b'<?xml version="1.0" encoding="UTF-8"?>',
        )
        lines = []
        for line in text.split(b"\n"):
            stripped = line.lstrip(b" ")
            lines.append(b"\t" * ((len(line) - len(stripped)) // 2) + stripped)
        path.write_bytes(b"\n".join(lines))
    ds = folder / "Font.designspace"
    ds.write_bytes(ds.read_bytes().replace(b"<axes>", COMMENT + b"\n\t<axes>", 1))
    for ufo in folder.glob("*.ufo"):
        glif = ufo / "glyphs" / "A_.glif"
        if not glif.exists():
            continue
        text = glif.read_bytes()
        if ufo.name != DEFAULT_UFO:
            text = re.sub(rb"\t<unicode [^>]*/>\n", b"", text)
        else:
            text = text.replace(
                b'<unicode hex="0061"/>\n',
                b'<unicode hex="0061"/>\n\t' + GUIDELINE.encode() + b"\n",
            )
        glif.write_bytes(text)
        groups = {"accents": ["A", "B"], "public.kern1.A": ["A", "A.alt"]}
        kerning = {"public.kern1.A": {"B": -20}, "B": {"A": -10}}
        for name, data in (("groups.plist", groups), ("kerning.plist", kerning)):
            text = (
                plistlib.dumps(data)
                .replace(
                    b"<?xml version='1.0' encoding='UTF-8'?>",
                    b'<?xml version="1.0" encoding="UTF-8"?>',
                )
                .replace(b"  ", b"\t")
            )
            (ufo / name).write_bytes(text)
    return folder_files(folder, "src")


@pytest.fixture
def ufo_setup(tmp_path):
    remote_store = make_remote(tmp_path, foreign_ufo_files(tmp_path))
    remote = Remote(str(remote_store.path), path="src/Font.designspace")
    store = GitRepoStore.create(tmp_path / "project.git")
    store.commit({"README": b"empty project"}, message="New project", author=ALICE)
    pulled = pull(store, remote, allow_local=True)
    store.fast_forward("main", "upstream/main")
    return store, remote_store, remote, pulled


@needs_designspace
def test_pull_converts_the_ufos(ufo_setup):
    store, remote_store, remote, pulled = ufo_setup
    tree = store.list_tree(store.head("main"))
    assert "font-data.json" in tree and "kerning.csv" in tree
    assert "README" not in tree  # the project now is the remote's font
    assert {"A", "B"} <= set(pulled.glyphs)


@needs_designspace
def test_ufo_push_of_one_glyph_edit(ufo_setup, tmp_path):
    store, remote_store, remote, _ = ufo_setup
    before = remote_store.head("main")
    edit_branch(store, "main", tmp_path, move_first_point)  # default source
    result = push(store, remote, message="Fix A", author=BOB, allow_local=True)
    assert result.pushed and result.glyphs == ("A",) and result.skipped == ()
    path = f"src/{DEFAULT_UFO}/glyphs/A_.glif"
    assert remote_changed_files(remote_store) == [path]
    old = remote_store.read_file(before, path)
    new = remote_store.read_file(result.remote_sha, path)
    assert new.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    assert GUIDELINE.encode() in new  # identifier kept
    assert b"\n\t<advance" in new and b"\n  " not in new  # tabs kept
    import difflib

    diff = [
        line
        for line in difflib.unified_diff(
            old.decode().splitlines(), new.decode().splitlines(), lineterm="", n=0
        )
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    assert len(diff) == 2, diff


@needs_designspace
def test_ufo_push_keeps_masters_without_unicodes(ufo_setup, tmp_path):
    store, remote_store, remote, _ = ufo_setup

    async def edit_bold_condensed(backend):
        glyph = await backend.getGlyph("A")
        index = next(
            i for i, s in enumerate(glyph.sources) if s.locationBase == "f22d1bbd"
        )
        await move_first_point(backend, "A", index)

    edit_branch(store, "main", tmp_path, edit_bold_condensed)
    result = push(store, remote, message="Bold A", author=BOB, allow_local=True)
    assert result.pushed
    path = f"src/{OTHER_UFO}/glyphs/A_.glif"
    assert remote_changed_files(remote_store) == [path]
    assert b"<unicode" not in remote_store.read_file(result.remote_sha, path)


@needs_designspace
def test_ufo_push_of_a_kerning_change(ufo_setup, tmp_path):
    from fontTools.misc import plistlib

    store, remote_store, remote, _ = ufo_setup
    before = remote_store.head("main")

    async def edit_kerning(backend):
        kerning = await backend.getKerning()
        table = kerning["kern"]
        default = table.sourceIdentifiers.index(
            next(i for i in table.sourceIdentifiers if i == "5bea6334")
        )
        table.values["@A"]["B"][default] = -35
        await backend.putKerning(kerning)

    edit_branch(store, "main", tmp_path, edit_kerning)
    result = push(store, remote, message="Kern AB", author=BOB, allow_local=True)
    assert result.pushed and result.skipped == ()
    path = f"src/{DEFAULT_UFO}/kerning.plist"
    assert remote_changed_files(remote_store) == [path]
    new = remote_store.read_file(result.remote_sha, path)
    assert plistlib.loads(new) == {"public.kern1.A": {"B": -35}, "B": {"A": -10}}
    assert new.startswith(b'<?xml version="1.0" encoding="UTF-8"?>') and b"\t" in new
    groups = remote_store.read_file(
        result.remote_sha, f"src/{DEFAULT_UFO}/groups.plist"
    )
    assert groups == remote_store.read_file(before, f"src/{DEFAULT_UFO}/groups.plist")


@needs_designspace
def test_ufo_push_skips_font_level_changes(ufo_setup, tmp_path):
    store, remote_store, remote, _ = ufo_setup
    before = remote_store.head("main")

    async def rename(backend):
        info = await backend.getFontInfo()
        info.familyName = "Renamed"
        await backend.putFontInfo(info)

    edit_branch(store, "main", tmp_path, rename)
    result = push(store, remote, message="Rename", author=BOB, allow_local=True)
    assert not result.pushed
    assert result.skipped == ("font-data.json",)
    assert remote_store.head("main") == before
    ds = remote_store.read_file(before, "src/Font.designspace")
    assert COMMENT in ds

    # A glyph edit afterwards goes; the font info still does not.
    edit_branch(store, "main", tmp_path, move_first_point)
    result = push(store, remote, message="Fix A", author=BOB, allow_local=True)
    assert result.pushed and result.skipped == ("font-data.json",)
    assert remote_changed_files(remote_store) == [f"src/{DEFAULT_UFO}/glyphs/A_.glif"]
    assert status(store, remote, "main", allow_local=True).pending == ["font-data.json"]


@needs_designspace
def test_ufo_push_of_a_new_glyph(ufo_setup, tmp_path):
    store, remote_store, remote, _ = ufo_setup

    async def add_glyph(backend):
        glyph = await backend.getGlyph("B")
        glyph.name = "C"
        await backend.putGlyph("C", glyph, [0x43])

    edit_branch(store, "main", tmp_path, add_glyph)
    result = push(store, remote, message="Add C", author=BOB, allow_local=True)
    assert result.pushed and "C" in result.glyphs
    changed = remote_changed_files(remote_store)
    assert f"src/{DEFAULT_UFO}/glyphs/C_.glif" in changed
    assert f"src/{DEFAULT_UFO}/glyphs/contents.plist" in changed
    assert not any(p.endswith(".designspace") for p in changed)


# --- safeguards -------------------------------------------------------------------


def test_match_formatting():
    old = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b"<a>\n\t<b/>\n\t<c>\n\t\t<d/>\n\t</c>\n</a>\n"
    )
    new = (
        b"<?xml version='1.0' encoding='UTF-8'?>\n"
        b'<a>\n  <b x="1"/>\n  <c>\n    <d/>\n  </c>\n</a>'
    )
    assert match_formatting(old, new) == old.replace(b"<b/>", b'<b x="1"/>')
    assert match_formatting(b"plain text\n", b"other") == b"other"


def test_restore_glif_details():
    old = (
        b'<glyph name="A" format="2">\n\t<advance width="10"/>\n'
        b'\t<guideline x="1" identifier="g"/>\n\t<outline/>\n</glyph>\n'
    )
    new = (
        b'<glyph name="A" format="2">\n\t<advance width="12"/>\n'
        b'\t<unicode hex="0041"/>\n\t<guideline x="1"/>\n\t<outline/>\n</glyph>\n'
    )
    fixed = restore_glif_details(old, new)
    assert b"<unicode" not in fixed
    assert b'<guideline x="1" identifier="g"/>' in fixed
    assert b'<advance width="12"/>' in fixed
    # Code points changed in Hive: the new <unicode> stays.
    assert b"<unicode" in restore_glif_details(old, new, {"A"})
