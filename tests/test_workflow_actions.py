"""hive-read and hive-write: recipes that read and write Hive projects, with
two (or three) local repositories and a development directory of users."""

import json
import pathlib
import shutil
import types

import pytest
import yaml
from fontra.backends.fontra import FontraBackend
from fontra.workflow.actions import ActionError
from fontra.workflow.workflow import Workflow

from conftest import run
from fontra_hive.access import DevDirectory
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.projectmanager import DevHiveProjectManager
from fontra_hive.workflow import (
    HiveWorkflowContext,
    checkRecipeAccess,
    hiveWorkflowContext,
    projectResolverFor,
    splitProjectRef,
)
from fontra_hive.access import token_for

IMPORTER = Signature("Importer", "importer@example.com")

USERS = {
    "users": {
        "jeremie": {"name": "Jérémie Hornus", "email": "j@example.com"},
        "ana": {"name": "Ana", "email": "ana@example.com"},
    },
    "projects": {
        "Latin": {"owner": "jeremie", "collaborators": {"ana": "observer"}},
        "Extra": {"owner": "jeremie"},
        "Target": {"owner": "jeremie", "collaborators": {"ana": "observer"}},
    },
}


async def addGlyph(fontraPath: pathlib.Path, source: str, newName: str):
    backend = FontraBackend.fromPath(fontraPath)
    glyph = await backend.getGlyph(source)
    glyph.name = newName
    await backend.putGlyph(newName, glyph, [])
    await backend.aclose()


@pytest.fixture
def hive(tmp_path, fixture_fontra):
    """Three projects: Latin (the fixture), Extra (the fixture plus a glyph
    "Extra"), Target (the fixture plus a recipe file)."""
    root = tmp_path / "repos"
    root.mkdir()

    extra = tmp_path / "extra.fontra"
    shutil.copytree(fixture_fontra, extra)
    run(addGlyph(extra, "A", "Extra"))

    shas = {}
    for name, font in [("Latin", fixture_fontra), ("Extra", extra)]:
        store = GitRepoStore.create(root / f"{name}.git")
        shas[name] = store.import_directory(font, message="Import", author=IMPORTER)
        store.close()

    store = GitRepoStore.create(root / "Target.git")
    store.import_directory(fixture_fontra, message="Import", author=IMPORTER)
    shas["Target"] = store.commit(
        {"workflows/merge.yaml": b"steps: []\n"},
        message="A recipe",
        author=IMPORTER,
    )
    store.close()

    usersPath = tmp_path / "users.json"
    usersPath.write_text(json.dumps(USERS))
    manager = DevHiveProjectManager(root, directory=DevDirectory(usersPath))
    return types.SimpleNamespace(root=root, manager=manager, shas=shas)


def context(hive, username="jeremie", recipe="merge"):
    return HiveWorkflowContext(
        resolver=projectResolverFor(hive.manager, token_for(username)),
        recipe=recipe,
    )


async def runRecipe(text, ctx, workDir):
    config = yaml.safe_load(text)
    with hiveWorkflowContext(ctx):
        async with Workflow(config=config, parentDir=workDir).endPoints() as ends:
            for output in ends.outputs:
                await output.process(workDir)
            return ends


async def glyphMapAt(hive, project, ref):
    store = GitRepoStore.open(hive.root / f"{project}.git")
    try:
        from fontra_hive.backend_git import GitFontraBackend

        backend = GitFontraBackend(store, commit=store.resolve(ref))
        glyphMap = await backend.getGlyphMap()
        await backend.aclose()
        return glyphMap
    finally:
        store.close()


MERGE = """
steps:
- input: hive-read
  project: Latin
- input: hive-read
  project: Extra@main
- output: hive-write
  project: Target
  message: Merge Latin and Extra
"""


def test_read_two_projects_merge_and_write_a_third(hive, tmp_path):
    ctx = context(hive)
    run(runRecipe(MERGE, ctx, tmp_path))

    store = GitRepoStore.open(hive.root / "Target.git")
    try:
        # the default branch has not moved; the result is on its own branch
        assert store.head("main") == hive.shas["Target"]
        head = store.head("workflow/merge")
        info = store.commit_info(head)
        assert info.parents == (hive.shas["Target"],)
        assert info.author == "Jérémie Hornus"
        assert info.email == "jeremie@hive"
        assert info.message.splitlines()[0] == "Merge Latin and Extra"
        assert "Hive-Workflow: merge" in info.message
        assert "Hive-Glyphs: Extra" in info.message
        # files that are not the font's are kept
        assert store.read_file(head, "workflows/merge.yaml") == b"steps: []\n"
        branchInfo = store.branch_info()["workflow/merge"]
        assert branchInfo["createdBy"] == "jeremie"
        assert branchInfo["workflow"] == "merge"
        assert branchInfo["base"] == hive.shas["Target"]
    finally:
        store.close()

    glyphs = run(glyphMapAt(hive, "Target", "workflow/merge"))
    assert "Extra" in glyphs and "A" in glyphs
    assert "Extra" not in run(glyphMapAt(hive, "Target", "main"))

    # provenance: what was read, at which commit, and what was written
    assert [(r.project, r.ref, r.commit) for r in ctx.reads] == [
        ("Latin", "main", hive.shas["Latin"]),
        ("Extra", "main", hive.shas["Extra"]),
    ]
    (write,) = ctx.writes
    assert (write.project, write.branch, write.created) == (
        "Target",
        "workflow/merge",
        True,
    )


def test_a_second_run_commits_on_the_same_branch(hive, tmp_path):
    run(runRecipe(MERGE, context(hive), tmp_path))
    store = GitRepoStore.open(hive.root / "Target.git")
    first = store.head("workflow/merge")
    store.close()

    ctx = context(hive)
    run(runRecipe(MERGE, ctx, tmp_path))
    # same inputs, same font: nothing to commit
    assert ctx.writes[0].commit is None
    assert ctx.writes[0].created is False

    # without Extra this time
    text = MERGE.replace("- input: hive-read\n  project: Extra@main\n", "")
    ctx = context(hive)
    run(runRecipe(text, ctx, tmp_path))
    store = GitRepoStore.open(hive.root / "Target.git")
    try:
        second = store.head("workflow/merge")
        assert store.commit_info(second).parents == (first,)
        # the glyph that is no longer in the result is deleted
        assert "Hive-Glyphs: Extra" in store.commit_info(second).message
    finally:
        store.close()
    assert "Extra" not in run(glyphMapAt(hive, "Target", "workflow/merge"))


def test_reading_and_writing_back_the_same_font_changes_nothing(hive, tmp_path):
    ctx = context(hive, recipe="copy")
    text = """
steps:
- input: hive-read
  project: Target
- output: hive-write
  project: Target
"""
    run(runRecipe(text, ctx, tmp_path))
    (write,) = ctx.writes
    assert write.branch == "workflow/copy"
    assert write.created and write.commit is None


def test_read_at_a_tag_or_a_commit(hive, tmp_path):
    from fontra_hive.backend_git import GitFontraBackend

    store = GitRepoStore.open(hive.root / "Extra.git")
    store.create_tag("v1", hive.shas["Extra"])

    async def addNewer():
        backend = GitFontraBackend(store, "main", author=IMPORTER)
        glyph = await backend.getGlyph("A")
        glyph.name = "Newer"
        await backend.putGlyph("Newer", glyph, [])
        await backend.aclose()

    run(addNewer())
    newer = store.head("main")
    store.close()
    assert newer != hive.shas["Extra"]
    assert "Newer" in run(glyphMapAt(hive, "Extra", "main"))

    for ref in ["v1", hive.shas["Extra"]]:
        ctx = context(hive, recipe="old")
        text = f"""
steps:
- input: hive-read
  project: Extra
  ref: {ref}
- output: hive-write
  project: Target
"""
        run(runRecipe(text, ctx, tmp_path))
        assert ctx.reads[0].commit == hive.shas["Extra"]
    assert "Newer" not in run(glyphMapAt(hive, "Target", "workflow/old"))


@pytest.mark.parametrize(
    "text, message",
    [
        (
            "steps: [{input: hive-read, project: Nope}]",
            "hive-read: no project 'Nope'",
        ),
        (
            "steps: [{input: hive-read, project: Latin, ref: nowhere}]",
            "hive-read: nothing called 'nowhere' in 'Latin' "
            "(a branch, a tag or a full commit id)",
        ),
        (
            "steps: [{input: hive-read, project: Latin@main, ref: other}]",
            "two refs for Latin: 'main' and 'other'",
        ),
        (
            "steps: [{output: hive-write, project: Target, branch: main}]",
            "hive-write: never writes to the default branch (main); write to "
            "another branch and merge it from the branches panel",
        ),
        (
            "steps: [{output: hive-write, project: Target@dev}]",
            "hive-write: give the branch with 'branch', not '@'",
        ),
        (
            "steps: [{output: hive-write, project: Target, branch: snapshot/x}]",
            "hive-write: branch 'snapshot/x': Branch names cannot start with "
            "snapshot/.",
        ),
    ],
)
def test_mistakes_are_refused(hive, tmp_path, text, message):
    with pytest.raises(ActionError) as info:
        run(runRecipe(text, context(hive), tmp_path))
    assert str(info.value) == message


def test_no_read_access_looks_like_no_project(hive, tmp_path):
    # ana is not a member of Extra
    with pytest.raises(ActionError) as info:
        run(runRecipe(MERGE, context(hive, "ana"), tmp_path))
    assert str(info.value) == "hive-read: no project 'Extra'"


def test_writing_needs_edit(hive, tmp_path):
    text = """
steps:
- input: hive-read
  project: Latin
- output: hive-write
  project: Target
"""
    with pytest.raises(ActionError) as info:
        run(runRecipe(text, context(hive, "ana"), tmp_path))
    assert str(info.value) == "hive-write: observer cannot edit 'Target'"
    store = GitRepoStore.open(hive.root / "Target.git")
    assert store.branches() == ["main"]
    store.close()


def test_unknown_user_reads_nothing(hive, tmp_path):
    with pytest.raises(ActionError):
        run(runRecipe(MERGE, context(hive, "nobody"), tmp_path))


def test_actions_need_a_hive_context(tmp_path):
    config = yaml.safe_load("steps: [{input: hive-read, project: Latin}]")

    async def go():
        async with Workflow(config=config, parentDir=tmp_path).endPoints():
            pass

    with pytest.raises(ActionError) as info:
        run(go())
    assert "only available in recipes run by a Fontra Hive server" in str(info.value)


def test_split_project_ref():
    assert splitProjectRef("owner/name", None) == ("owner/name", None)
    assert splitProjectRef("owner/name@v1", None) == ("owner/name", "v1")
    assert splitProjectRef("owner/name@v1", "v1") == ("owner/name", "v1")
    with pytest.raises(ActionError):
        splitProjectRef("owner/name@", None)
    with pytest.raises(ActionError):
        splitProjectRef("", None)


# --- before running: every cited project, for this person ---------------------


def test_check_recipe_access_before_running(hive):
    config = yaml.safe_load(MERGE)
    assert run(checkRecipeAccess(config, context(hive))) == []
    assert run(checkRecipeAccess(config, context(hive, "ana"))) == [
        "steps[1] (hive-read): no project 'Extra'",
        "steps[2] (hive-write): observer cannot edit 'Target'",
    ]
    nested = yaml.safe_load("""
steps:
- fork:
  steps:
  - output: hive-write
    project: Target
    branch: main
""")
    assert run(checkRecipeAccess(nested, context(hive))) == [
        "steps[0].steps[0] (hive-write): never writes to the default branch"
    ]


# --- production: roles from hive-api ------------------------------------------


def test_hive_api_resolver(tmp_path, fixture_fontra):
    from fontra_hive.access import CAPABILITIES, Access, HiveUser
    from fontra_hive.workflow import HiveApiProjectResolver

    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "uid-1.git")
    store.import_directory(fixture_fontra, message="Import", author=IMPORTER)
    store.close()

    user = HiveUser("jeremie", "Jérémie", "", uid="u1")
    asked = []

    class FakeApi:
        async def access(self, token, project):
            asked.append((token, project))
            if project.lower() != "owner/latin":
                return None
            return types.SimpleNamespace(
                access=Access(user, "designer", CAPABILITIES["designer"]),
                repo="uid-1.git",
                defaultBranch="trunk",
                projectId="owner/Latin",
            )

    manager = types.SimpleNamespace(
        api=FakeApi(),
        rootPath=root,
        _ensureRepo=lambda projectAccess: root / projectAccess.repo,
    )
    resolver = projectResolverFor(manager, "hive:u1")
    assert isinstance(resolver, HiveApiProjectResolver)

    project = run(resolver.resolve("owner/latin"))
    assert project.projectId == "owner/Latin"
    assert project.repoPath == root / "uid-1.git"
    assert project.defaultBranch == "trunk"
    assert project.can("edit") and not project.can("merge")
    assert project.author(IMPORTER).email == "jeremie@hive"
    assert run(resolver.resolve("someone/else")) is None
    assert run(resolver.resolve("no-slash")) is None  # never asked
    assert asked == [("hive:u1", "owner/latin"), ("hive:u1", "someone/else")]


# --- validation: what a Hive server refuses -----------------------------------


def test_hive_validation_refuses_file_actions():
    pytest.importorskip("fontra_workflow_ui")
    from fontra_hive.workflow import validateHiveRecipe

    text = """
steps:
- input: fontra-read
  source: /srv/hive/repos/x.fontra
- input: hive-read
  project: Latin
- output: fontra-write
  destination: ../../out.fontra
"""
    check = validateHiveRecipe(yaml.safe_load(text))
    reason = (
        "recipes on Fontra Hive read and write projects with hive-read and "
        "hive-write, not files of the server"
    )
    assert [str(e) for e in check.errors] == [
        f"steps[0] (input: fontra-read): this action is not allowed here: {reason}",
        f"steps[2] (output: fontra-write): this action is not allowed here: {reason}",
    ]


def test_hive_validation_of_a_shareable_recipe():
    pytest.importorskip("fontra_workflow_ui")
    from fontra_hive.workflow import validateHiveRecipe

    text = """
shareable: true
inputs:
  latin:
  cjk: {project: owner/cjk}
outputs: [target]
steps:
- input: hive-read
  project: "{latin}"
- input: hive-read
  project: owner/extra
- filter: add-features
  featureFile: /etc/x.fea
- output: hive-write
  project: "{target}"
  branch: "{branch}"
"""
    check = validateHiveRecipe(yaml.safe_load(text))
    literal = (
        "a shareable recipe cannot name a project literally: use a placeholder "
        "and declare it in inputs"
    )
    assert [str(e) for e in check.errors] == [
        f"inputs.cjk: {literal}",
        "steps[3] (output: hive-write), argument 'branch': placeholder {branch} "
        "is not declared in inputs, outputs, parameters, assets",
        f"steps[1] (input: hive-read), argument 'project': {literal}",
        "steps[2] (filter: add-features), argument 'featureFile': absolute paths "
        "and '..' are not allowed here: '/etc/x.fea'",
    ]


def test_hive_actions_are_in_the_catalog():
    pytest.importorskip("fontra_workflow_ui")
    from fontra_workflow_ui.introspect import buildCatalog

    catalog = buildCatalog()
    read = catalog.get("input", "hive-read")
    write = catalog.get("output", "hive-write")
    assert read.package == "fontra-hive" and write.package == "fontra-hive"
    assert read.required == ["project"]
    assert write.required == ["project"]
    assert read.descriptionSource == "docstring"
    assert all(s.get("description") for s in write.properties.values())
    assert "input" not in write.properties
