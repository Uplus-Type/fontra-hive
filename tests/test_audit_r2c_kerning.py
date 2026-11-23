"""R2C-4 (audit of 23 Nov 2026): a restoration never leaves the kerning
with values for a source the font doesn't have: 409, the source named."""

import json

import pytest

pytest.importorskip("fontra")

from conftest import run  # noqa: E402
from r2c_bench import ME, body, make_manager, request, store_of  # noqa: E402


# --- R2C-4: the kerning's sources ------------------------------------------------


def kerning_csv(sids, value):
    import pathlib
    import tempfile

    from fontra.backends.fontra import writeKerningFile
    from fontra.core.classes import Kerning

    table = Kerning(
        groupsSide1={},
        groupsSide2={},
        sourceIdentifiers=sids,
        values={"A": {"B": [value] * len(sids)}},
    )
    with tempfile.TemporaryDirectory() as folder:
        path = pathlib.Path(folder) / "kerning.csv"
        writeKerningFile(path, {"kern": table})
        return path.read_bytes()


def font_data(store, sha=None):
    return json.loads(store.read_file(sha or store.head(), "font-data.json"))


def test_kerning_restored_for_a_source_that_is_gone(tmp_path, fixture_fontra):
    """At v1 the kerning has a column for a source removed since (Black):
    restoring Kerning is refused (409), the source named."""
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        data = font_data(store)
        keep = next(iter(data["sources"]))
        # v1: a source Black, and kerning for it.
        withBlack = json.loads(json.dumps(data))
        black = dict(withBlack["sources"][keep], name="Black")
        withBlack["sources"]["black001"] = black
        store.commit(
            {
                "font-data.json": (json.dumps(withBlack, indent=0) + "\n").encode(),
                "kerning.csv": kerning_csv([keep, "black001"], -20),
            },
            message="v1",
            author=ME,
        )
        v1 = store.head()
        store.commit(
            {
                "font-data.json": (json.dumps(data, indent=0) + "\n").encode(),
                "kerning.csv": kerning_csv([keep], -30),
            },
            message="Black goes",
            author=ME,
        )
        before = store.head()
    finally:
        store.close()

    async def go():
        r = await manager.restoreFontHandler(
            request("Mutator", "mei", method="POST", ref=v1, parts="kerning")
        )
        assert r.status == 409
        assert (await body(r))["message"] == (
            "The kerning of this version has values for the source Black, which "
            "the font doesn't have now. Restore Axes and sources too, or the whole font."
        )
        store = store_of(manager)
        try:
            assert store.head() == before  # nothing written
        finally:
            store.close()
        # Restoring both brings Black and its kerning back: coherent.
        r = await manager.restoreFontHandler(
            request("Mutator", "mei", method="POST", ref=v1, parts="kerning,axesAndSources")
        )
        assert r.status == 200, await body(r)

    run(go())


def test_axes_and_sources_restored_without_a_source_the_kerning_uses(
    tmp_path, fixture_fontra
):
    """The plausible case of the audit: Axes and sources restored to a
    version without a source (no glyph drawn in it) that the kerning of now
    uses: refused, the source named."""
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    try:
        data = font_data(store)
        v0 = store.head()
        keep = next(iter(data["sources"]))
        withBlack = json.loads(json.dumps(data))
        withBlack["sources"]["black001"] = dict(withBlack["sources"][keep], name="Black")
        store.commit(
            {
                "font-data.json": (json.dumps(withBlack, indent=0) + "\n").encode(),
                "kerning.csv": kerning_csv([keep, "black001"], -20),
            },
            message="Black, sparse, with kerning",
            author=ME,
        )
    finally:
        store.close()

    async def go():
        r = await manager.restoreFontHandler(
            request("Mutator", "mei", method="POST", ref=v0, parts="axesAndSources")
        )
        assert r.status == 409
        assert (await body(r))["message"] == (
            "The kerning has values for the source Black, which this version "
            "doesn't have. Restore Kerning too, or the whole font."
        )

    run(go())
