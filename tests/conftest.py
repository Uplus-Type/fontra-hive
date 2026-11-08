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


@pytest.fixture
def fontra_without_axes(tmp_path, fixture_fontra):
    """MutatorSans with every axis dropped: a single source, no axis."""
    pytest.importorskip("fontra")
    from fontra.backends import getFileSystemBackend
    from fontra.workflow.workflow import Workflow

    async def reduce():
        source = fixture_fontra.resolve()
        backend = getFileSystemBackend(source)
        names = [axis.name for axis in (await backend.getAxes()).axes]
        steps = [
            dict(filter="subset-axes", dropAxisNames=names),
            dict(output="fontra-write", destination="plain.fontra"),
        ]
        workflow = Workflow(config=dict(steps=steps), parentDir=source.parent)
        async with workflow.endPoints(backend) as endPoints:
            for output in endPoints.outputs:
                await output.process(tmp_path / "reduced", continueOnError=False)

    (tmp_path / "reduced").mkdir()
    run(reduce())
    return tmp_path / "reduced" / "plain.fontra"
