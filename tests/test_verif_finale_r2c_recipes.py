"""VF-4 (final check of R2c, 23 Nov 2026): a recurring recipe whose last
copy of main is 200 states old or more is still refused (the walk of main's
states stops there: fontdiff.ORIGIN_STATES), and the sentence says why.
Fabio: designer, no font settings."""

import pytest

pytest.importorskip("fontra")
from fontra.workflow.actions import ActionError  # noqa: E402

from conftest import run  # noqa: E402
from fontra_hive.gitstore import Signature  # noqa: E402
from r2c_bench import make_manager, store_of  # noqa: E402
from test_reverif_r2c_domains import copy_recipe, run_recipe  # noqa: E402
from test_verif_finale_r2c_origins import build, features  # noqa: E402

MEI = Signature("Mei", "mei@example.com")
REFUSAL = "Font settings are kept to some people on this project. Ask a manager."
TOO_FAR = (
    "Font settings are kept to some people on this project, and main has "
    "changed too much since this branch last copied it. Ask a manager."
)


@pytest.mark.parametrize("ago, refused", [(199, False), (200, True)])
def test_a_recurring_recipe_says_when_main_moved_too_much(
    tmp_path, fixture_fontra, ago, refused
):
    """Fabio's recurring recipe copies main onto the branch ``copy``, whose
    last copy of main was made ``ago`` states of features.txt ago. 199: it
    passes; 200: refused, and the sentence says why."""
    manager = make_manager(tmp_path, fixture_fontra)
    base, states = build(manager, commits=220, states=220, lines=20)
    store = store_of(manager)
    try:
        store.create_branch("copy", base)
        copied = store.read_file(states[-1 - ago], "features.txt")
        store.commit({"features.txt": copied}, branch="copy", message="copy", author=MEI)
        before = store.head("copy")
    finally:
        store.close()

    async def go():
        await run_recipe(
            manager, tmp_path, "fabio", copy_recipe("Mutator", "copy"), "copy-main"
        )

    if refused:
        with pytest.raises(ActionError) as error:
            run(go())
        assert TOO_FAR in str(error.value)
        store = store_of(manager)
        try:
            assert store.head("copy") == before
        finally:
            store.close()
    else:
        run(go())
        store = store_of(manager)
        try:
            assert store.head("copy") != before
            main = store.read_file(store.head("main"), "features.txt")
            assert store.read_file(store.head("copy"), "features.txt") == main
        finally:
            store.close()
    run(manager.aclose())


def test_a_branch_of_its_own_gets_the_short_sentence(tmp_path, fixture_fontra):
    """A branch that changed the font settings itself, behind a long main:
    the plain refusal (VF-4's sentence is for a copy of main only)."""
    manager = make_manager(tmp_path, fixture_fontra)
    base, states = build(manager, commits=220, states=220, lines=20)
    store = store_of(manager)
    try:
        store.create_branch("own", base)
        store.commit(
            {"features.txt": features("own", 20)}, branch="own", message="own", author=MEI
        )
    finally:
        store.close()
    # Fabio copies exp onto it: not main's state, so not a copy of main.

    async def go():
        await run_recipe(
            manager, tmp_path, "fabio", copy_recipe("Mutator@exp", "own"), "copy-exp"
        )

    with pytest.raises(ActionError) as error:
        run(go())
    assert REFUSAL in str(error.value)
    run(manager.aclose())
