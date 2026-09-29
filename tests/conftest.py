import asyncio
import pathlib
import shutil

import pytest

DATA_DIR = pathlib.Path(__file__).parent / "data"
FIXTURE = DATA_DIR / "MutatorSansLocationBase.fontra"


@pytest.fixture
def fixture_fontra(tmp_path) -> pathlib.Path:
    """A private copy of the .fontra test package."""
    dest = tmp_path / "source.fontra"
    shutil.copytree(FIXTURE, dest)
    return dest


def run(coro):
    """Run a coroutine in a fresh event loop (no pytest-asyncio needed)."""
    return asyncio.run(coro)


def files_of(folder: pathlib.Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and not p.name.startswith(".")
    }
