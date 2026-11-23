"""A person's display name with ``<…>`` (re-check of the R2c audit, outside
its surface): git and GitHub read the first ``<…>`` of an author line as the
address, Hive the part after " <". A name "X <someone@studio>" never gives a
commit, a tag or a pushed commit's ``Co-authored-by`` to someone else's
address: ``<`` and ``>`` become ``‹`` and ``›`` in names (and addresses)."""

import re

from fontra_hive.gitstore import GitRepoStore, Signature, person_name
from fontra_hive.remote import _remote_commit

FORGED = "Fabio <boss@studio.example>"
# How git reads an identity line: the name, then the first <…>.
GIT_IDENT = re.compile(rb"^(?P<name>[^<]*)<(?P<email>[^>]*)>")


def test_person_name():
    assert person_name(FORGED) == "Fabio ‹boss@studio.example›"
    assert person_name("Ana\nHive-Snapshot: x") == "Ana Hive-Snapshot: x"
    assert person_name("Mei Ling") == "Mei Ling"


def test_the_author_and_the_tagger_keep_their_own_address(tmp_path):
    store = GitRepoStore.create(tmp_path / "f.git")
    author = Signature(FORGED, "fabio@hive")
    sha = store.commit({"a.txt": b"1"}, message="Edit\n", author=author)
    raw = store.repo[sha.encode()].author
    assert GIT_IDENT.match(raw).group("email") == b"fabio@hive"
    info = store.commit_info(sha)
    assert info.email == "fabio@hive"
    assert info.author == "Fabio ‹boss@studio.example›"
    snap = store.create_snapshot("Round 1", author=author)
    assert store.commit_info(snap.sha).email == "fabio@hive"
    tag = store.repo[store.repo.refs[b"refs/tags/snapshot/round-1"]]
    assert GIT_IDENT.match(tag.tagger).group("email") == b"fabio@hive"


def test_a_pushed_commit_credits_no_one_else(tmp_path):
    store = GitRepoStore.create(tmp_path / "f.git")
    sha = _remote_commit(
        store,
        {},
        {"a.txt": b"1"},
        None,
        "Push from Hive\n",
        Signature("Ana", "ana@hive"),
        [Signature(FORGED, "fabio@hive"), Signature("Mei <mei@x>", "mei@hive")],
        Signature("Fontra Hive", "hive@fontrahive.com"),
    )
    message = store.repo[sha.encode()].message.decode()
    lines = [line for line in message.splitlines() if line.startswith("Co-authored-by:")]
    assert lines == [
        "Co-authored-by: Fabio ‹boss@studio.example› <fabio@hive>",
        "Co-authored-by: Mei ‹mei@x› <mei@hive>",
    ]
    for line in lines:
        ident = GIT_IDENT.match(line[len("Co-authored-by: ") :].encode())
        assert ident.group("email").endswith(b"@hive")
