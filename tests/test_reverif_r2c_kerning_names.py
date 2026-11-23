"""A change of the kerning alone names its sources by their names in the
commit message ("Kerning: 1 pair changed (LightCondensed)"), not by their
identifiers (re-check of the R2c audit, outside its surface): the names are
read in the head's ``font-data.json``."""

import asyncio

import pytest

from fontra_hive.backend_git import _commit_message

pytest.importorskip("fontra")

from conftest import FIXTURE  # noqa: E402
from r2c_bench import connection, make_manager, store_of  # noqa: E402
from test_fontdiff import kerning_csv  # noqa: E402

FONT_DATA = (FIXTURE / "font-data.json").read_bytes()


def test_the_commit_message_names_the_kerning_sources():
    kerning = kerning_csv({"A": {"V": [-40]}}, sources=("5bea6334",))
    message = _commit_message(
        {"kerning.csv": kerning}, {"kerning.csv": None}, fontData=FONT_DATA
    )
    assert "(LightCondensed)" in message
    assert "5bea6334" not in message


def test_an_edit_of_the_kerning_alone_names_its_source(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)

    async def go():
        handler = await manager.getRemoteSubject("Mutator", "hive-user:ana", False)
        change = {
            "p": ["kerning"],
            "f": "=",
            "a": [
                "kern",
                {
                    "groupsSide1": {},
                    "groupsSide2": {},
                    "sourceIdentifiers": ["5bea6334"],
                    "values": {"A": {"V": [-40]}},
                },
            ],
        }
        await handler.editFinal(change, {}, "kern", False, connection=connection("ana"))
        await handler.finishWriting()
        await asyncio.sleep(0.3)
        handler.backend.flush()
        await manager.aclose()

    asyncio.run(go())
    store = store_of(manager)
    try:
        info = store.commit_info(store.head())
    finally:
        store.close()
    assert info.author == "Ana"
    assert "Kerning: 1 pair changed (LightCondensed)" in info.message
    assert "5bea6334" not in info.message
