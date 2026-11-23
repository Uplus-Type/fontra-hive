"""RV-3 (re-check of the R2c audit, 23 Nov 2026): what the default branch
holds may come along to a branch only in a domain the branch never changed
itself (the non-divergence rule). A designer without the font settings can
no longer set a manager's branch back to main's settings; *Update from
main* and a recurring recipe that copies main still pass. A copy of main
that brings back a setting main went back on is said in the merge preview.

Fabio: designer, no font settings, no kerning, no plug-in data. Mei:
manager."""

import json

import pytest

pytest.importorskip("fontra")
import yaml  # noqa: E402
from fontra.workflow.actions import ActionError  # noqa: E402
from fontra.workflow.workflow import Workflow  # noqa: E402

from conftest import run  # noqa: E402
from fontra_hive import fontdiff  # noqa: E402
from fontra_hive.access import token_for  # noqa: E402
from fontra_hive.gitstore import Signature  # noqa: E402
from fontra_hive.recipe import (  # noqa: E402
    HiveRecipeContext,
    hiveRecipeContext,
    projectResolverFor,
    requireRecipeAccess,
)
from r2c_bench import body, make_manager, request, store_of  # noqa: E402

MEI = Signature("Mei", "mei@example.com")
REFUSAL = "Font settings are kept to some people on this project. Ask a manager."


def copy_recipe(source, target, message="Copy"):
    return f"""
steps:
- input: hive-read
  project: {source}
- output: hive-write
  project: Mutator
  branch: {target}
  message: {message}
"""


async def run_recipe(manager, tmp_path, user, text, name):
    ctx = HiveRecipeContext(
        resolver=projectResolverFor(manager, token_for(user)), recipe=name
    )
    config = yaml.safe_load(text)
    await requireRecipeAccess(config, ctx)
    work = tmp_path / f"work-{name}"
    work.mkdir()
    with hiveRecipeContext(ctx):
        async with Workflow(config=config, parentDir=work).endPoints() as ends:
            for output in ends.outputs:
                await output.process(work)


def set_family(manager, branch, family, create_from=None):
    """Mei writes a family name on ``branch`` (None: removes it)."""
    store = store_of(manager)
    try:
        if create_from is not None:
            store.create_branch(branch, store.resolve(create_from))
        data = json.loads(store.read_file(store.head(branch), "font-data.json"))
        if family is None:
            data.get("fontInfo", {}).pop("familyName", None)
        else:
            data.setdefault("fontInfo", {})["familyName"] = family
        return store.commit(
            {"font-data.json": (json.dumps(data, indent=0) + "\n").encode()},
            branch=branch,
            message=f"Family {family}",
            author=MEI,
        )
    finally:
        store.close()


def family(manager, ref):
    store = store_of(manager)
    try:
        data = json.loads(store.read_file(store.resolve(ref), "font-data.json"))
    finally:
        store.close()
    return data.get("fontInfo", {}).get("familyName")


def test_a_designer_cannot_undo_a_managers_settings_on_her_branch(
    tmp_path, fixture_fontra
):
    """The re-check's first proof, inverted: Fabio copies main into "exp",
    where Mei tries settings. Before: Mei's settings (the branch diverged);
    after: main's. Refused, and exp keeps Mei's work."""
    manager = make_manager(tmp_path, fixture_fontra)
    set_family(manager, "exp", "Mei's experiment", create_from="main")
    head = store_of(manager).head("exp")
    with pytest.raises(ActionError, match=REFUSAL):
        run(
            run_recipe(
                manager, tmp_path, "fabio", copy_recipe("Mutator", "exp", "Tidy"), "tidy"
            )
        )
    assert family(manager, "exp") == "Mei's experiment"
    assert store_of(manager).head("exp") == head
    # Mei, who has the font settings, may.
    run(run_recipe(manager, tmp_path, "mei", copy_recipe("Mutator", "exp"), "mei"))
    assert family(manager, "exp") is None


def test_a_recurring_recipe_that_copies_main_still_passes(tmp_path, fixture_fontra):
    """Fabio's recipe copies main into his branch each time main moves.
    Mei changes the settings on main twice: each run brings them (the
    branch holds an earlier state of main, not work of its own)."""
    manager = make_manager(tmp_path, fixture_fontra)
    recipe = copy_recipe("Mutator", "fabio-nightly", "Nightly")
    run(run_recipe(manager, tmp_path, "fabio", recipe, "n1"))
    for i, name in enumerate(["V1", "V2", "V3"], start=2):
        set_family(manager, "main", name)
        run(run_recipe(manager, tmp_path, "fabio", recipe, f"n{i}"))
        assert family(manager, "fabio-nightly") == name


def test_update_from_main_into_a_branch_that_never_changed_the_settings(
    tmp_path, fixture_fontra
):
    """*Update from main* by Fabio on his own branch, after Mei changed the
    settings on main twice: 200."""
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    store.create_branch("fabio", store.head())
    store.close()
    set_family(manager, "main", "V1")
    set_family(manager, "main", "V2")

    async def go():
        q = {"from": "main", "into": "fabio"}
        preview = await body(
            await manager.mergePreviewHandler(request("Mutator", "fabio", **q))
        )
        assert preview["closedDomains"] == []
        assert preview["canMerge"] is True
        response = await manager.mergeHandler(
            request("Mutator", "fabio", method="POST", body={}, **q)
        )
        assert response.status == 200

    run(go())
    assert family(manager, "fabio") == "V2"


def test_a_copy_of_main_that_brings_back_a_reverted_setting_is_said(
    tmp_path, fixture_fontra
):
    """The re-check's second proof: Fabio's branch copies "V1" from main
    (allowed: main's state); Mei goes back on V1 on main. The merge into
    main would bring V1 back: the rule cannot see it (the merge base did not
    move, and the manager has the domain), so the preview says it. A limit
    on purpose: the manager decides, and the merge stays possible."""
    manager = make_manager(tmp_path, fixture_fontra)
    store = store_of(manager)
    store.create_branch("fabio", store.head())
    store.close()
    set_family(manager, "main", "V1")
    run(run_recipe(manager, tmp_path, "fabio", copy_recipe("Mutator", "fabio"), "copy"))
    assert family(manager, "fabio") == "V1"
    set_family(manager, "main", None)

    async def go():
        q = {"from": "fabio", "into": "main"}
        preview = await body(
            await manager.mergePreviewHandler(request("Mutator", "mei", **q))
        )
        assert [c["domain"] for c in preview["beyondTheGlyph"]] == ["fontsettings"]
        assert preview["broughtBack"] == [
            {
                "domain": "fontsettings",
                "domainLabel": "Font settings",
                "text": "Font settings on fabio are as main had them before: "
                "merging brings them back.",
            }
        ]
        response = await manager.mergeHandler(
            request("Mutator", "mei", method="POST", body={}, **q)
        )
        assert response.status == 200

    run(go())
    assert family(manager, "main") == "V1"


def test_a_merge_of_new_settings_says_nothing_brought_back(tmp_path, fixture_fontra):
    """Settings made on the branch itself, never on main: no sentence."""
    manager = make_manager(tmp_path, fixture_fontra)
    set_family(manager, "exp", "New", create_from="main")

    async def go():
        q = {"from": "exp", "into": "main"}
        preview = await body(
            await manager.mergePreviewHandler(request("Mutator", "mei", **q))
        )
        assert [c["domain"] for c in preview["beyondTheGlyph"]] == ["fontsettings"]
        assert preview["broughtBack"] == []

    run(go())


def test_domains_held_and_the_origins():
    a = {"font-data.json": b'{"fontInfo": {"familyName": "A"}}', "kerning.csv": b"k"}
    b = {"font-data.json": b'{"fontInfo": {"familyName": "B"}}', "kerning.csv": b"k"}
    c = {"font-data.json": b'{"fontInfo": {"familyName": "B"}}', "kerning.csv": b"x"}
    domains = {"fontsettings", "kerning"}
    assert fontdiff.domains_held(a, [c], domains) == set()
    assert fontdiff.domains_held(a, [c, b], domains) == {"kerning"}
    assert fontdiff.domains_held(b, [c], domains) == {"fontsettings"}
    assert fontdiff.domains_held(b, iter([c, a]), domains) == domains
    # Read lazily: stops as soon as every domain is found.
    seen = []

    def candidates():
        for x in (b, a):
            seen.append(x)
            yield x

    assert fontdiff.domains_held(b, candidates(), domains) == domains
    assert seen == [b]
