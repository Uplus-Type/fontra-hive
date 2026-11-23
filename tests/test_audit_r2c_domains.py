"""R2C-3 (audit of 23 Nov 2026): a recipe (hive-write) and a merge write
beyond the glyph only the domains the person may write, on any branch. The
merge preview lists the changes beyond the glyph with their domain, and
says why the person cannot merge before they click. What the default branch
already holds may come along (a branch brought up to date)."""

import json

import pytest

from fontra_hive import fontdiff

pytest.importorskip("fontra")
import yaml  # noqa: E402
from aiohttp import web  # noqa: E402
from fontra.workflow.actions import ActionError  # noqa: E402
from fontra.workflow.workflow import Workflow  # noqa: E402

from conftest import run  # noqa: E402
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
FONT_SETTINGS_REFUSAL = (
    "Font settings are kept to some people on this project. Ask a manager."
)

RECIPE = """
steps:
- input: hive-read
  project: Mutator
- filter: set-font-info
  fontInfo:
    familyName: Written by Fabio
- filter: drop-kerning
- output: hive-write
  project: Mutator
  branch: fabio-work
  message: Tidy
"""

GLYPHS_ONLY = """
steps:
- input: hive-read
  project: Mutator
- filter: decompose-composites
- output: hive-write
  project: Mutator
  branch: fabio-glyphs
  message: Decompose
"""


def font_data_at(manager, ref):
    store = store_of(manager)
    try:
        sha = store.resolve(ref)
        return json.loads(store.read_file(sha, "font-data.json")), store.list_tree(sha)
    finally:
        store.close()


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


def test_a_recipe_does_not_write_font_settings_for_a_designer(tmp_path, fixture_fontra):
    """Fabio: designer, font settings and kerning kept to Ana. His recipe
    that sets the font info and drops the kerning is refused, by name."""
    manager = make_manager(tmp_path, fixture_fontra)
    with pytest.raises(ActionError) as raised:
        run(run_recipe(manager, tmp_path, "fabio", RECIPE, "tidy"))
    assert FONT_SETTINGS_REFUSAL in str(raised.value)
    store = store_of(manager)
    try:
        written = store.head("fabio-work")
        if written is not None:  # the branch may exist, never with the change
            data = json.loads(store.read_file(written, "font-data.json"))
            assert data.get("fontInfo", {}).get("familyName") != "Written by Fabio"
            assert "kerning.csv" in store.list_tree(written)
    finally:
        store.close()


def test_a_recipe_writes_what_the_person_may_write(tmp_path, fixture_fontra):
    """Ana may write the three domains: the same recipe goes through. Fabio's
    recipe that changes glyphs only goes through too."""
    manager = make_manager(tmp_path, fixture_fontra)
    run(run_recipe(manager, tmp_path, "ana", RECIPE, "tidy"))
    data, files = font_data_at(manager, "fabio-work")
    assert data["fontInfo"]["familyName"] == "Written by Fabio"
    assert "kerning.csv" not in files
    run(run_recipe(manager, tmp_path, "fabio", GLYPHS_ONLY, "decompose"))


def branches_with_meis_font_info(manager, on="exp"):
    """Mei (manager) tries new font settings on ``on``; ``fabio`` is
    Fabio's branch, made from main before."""
    store = store_of(manager)
    try:
        head = store.head()
        for branch in {"exp", "fabio"} - {on}:
            store.create_branch(branch, head)
        if on != "main":
            store.create_branch(on, head)
        data = json.loads(store.read_file(head, "font-data.json"))
        data.setdefault("fontInfo", {})["familyName"] = "Mei's experiment"
        store.commit(
            {"font-data.json": (json.dumps(data, indent=0) + "\n").encode()},
            branch=on,
            message="Edit font info",
            author=MEI,
        )
    finally:
        store.close()


def test_a_designer_cannot_merge_font_settings_into_a_branch(tmp_path, fixture_fontra):
    """Fabio (font settings closed) merges 'exp' into 'fabio': the preview
    lists the font info change with its domain and says he cannot merge;
    the merge answers 403 with the domain's sentence."""
    manager = make_manager(tmp_path, fixture_fontra)
    branches_with_meis_font_info(manager)

    async def go():
        preview = await body(
            await manager.mergePreviewHandler(
                request("Mutator", "fabio", **{"from": "exp", "into": "fabio"})
            )
        )
        assert [(c["area"], c["domain"], c["domainLabel"]) for c in preview["beyondTheGlyph"]] == [
            ("fontInfo", "fontsettings", "Font settings")
        ]
        assert "Mei's experiment" in preview["beyondTheGlyph"][0]["summary"]
        assert preview["closedDomains"] == ["fontsettings"]
        assert preview["canMerge"] is False
        assert preview["cannotMerge"] == FONT_SETTINGS_REFUSAL
        with pytest.raises(web.HTTPForbidden) as raised:
            await manager.mergeHandler(
                request(
                    "Mutator",
                    "fabio",
                    method="POST",
                    body={},
                    **{"from": "exp", "into": "fabio"},
                )
            )
        assert raised.value.text == FONT_SETTINGS_REFUSAL

    run(go())
    data, _ = font_data_at(manager, "fabio")
    assert data.get("fontInfo", {}).get("familyName") != "Mei's experiment"


def test_a_manager_merges_font_settings_into_a_branch(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    branches_with_meis_font_info(manager)

    async def go():
        preview = await body(
            await manager.mergePreviewHandler(
                request("Mutator", "mei", **{"from": "exp", "into": "fabio"})
            )
        )
        assert preview["canMerge"] is True and preview["closedDomains"] == []
        assert preview["cannotMerge"] is None
        response = await manager.mergeHandler(
            request("Mutator", "mei", method="POST", body={}, **{"from": "exp", "into": "fabio"})
        )
        assert response.status == 200

    run(go())
    data, _ = font_data_at(manager, "fabio")
    assert data["fontInfo"]["familyName"] == "Mei's experiment"


def test_a_designer_brings_his_branch_up_to_date_from_main(tmp_path, fixture_fontra):
    """Mei changed the font info on main: Fabio may still update his branch
    from main. What the default branch holds is not his to refuse."""
    manager = make_manager(tmp_path, fixture_fontra)
    branches_with_meis_font_info(manager, on="main")

    async def go():
        preview = await body(
            await manager.mergePreviewHandler(
                request("Mutator", "fabio", **{"from": "main", "into": "fabio"})
            )
        )
        # Listed with its domain, and not refused.
        assert [c["domain"] for c in preview["beyondTheGlyph"]] == ["fontsettings"]
        assert preview["closedDomains"] == [] and preview["canMerge"] is True
        response = await manager.mergeHandler(
            request("Mutator", "fabio", method="POST", body={}, **{"from": "main", "into": "fabio"})
        )
        assert response.status == 200

    run(go())
    data, _ = font_data_at(manager, "fabio")
    assert data["fontInfo"]["familyName"] == "Mei's experiment"


def test_domains_written():
    old = {"font-data.json": b'{"fontInfo": {"familyName": "A"}}', "kerning.csv": b"x"}
    new = {"font-data.json": b'{"fontInfo": {"familyName": "B"}}', "kerning.csv": None}
    changes, domains = fontdiff.domains_written(old, new)
    assert domains == ["fontsettings", "kerning"]
    assert [c.area for c in changes] == ["fontInfo", "kerning"]
    # What the reference (the default branch) holds already is not counted.
    _, domains = fontdiff.domains_written(old, new, reference=new)
    assert domains == []
    # A change Hive cannot read counts in its file's domain.
    _, domains = fontdiff.domains_written(
        {"font-data.json": b"{}"}, {"font-data.json": b"not json"}
    )
    assert domains == ["fontsettings"]
    # The same data, written otherwise: nothing.
    _, domains = fontdiff.domains_written(
        {"font-data.json": b'{"a": 1}'}, {"font-data.json": b'{ "a" : 1 }\n'}
    )
    assert domains == []
