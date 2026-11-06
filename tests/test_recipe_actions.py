"""hive-read and hive-write: recipes that read and write Hive projects, with
local repositories and a development directory of users."""

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
from fontra_hive.access import DevDirectory, token_for
from fontra_hive.backend_git import GitFontraBackend
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.projectmanager import DevHiveProjectManager
from fontra_hive.recipe import (
    HiveProjectRef,
    HiveRead,
    HiveRecipeContext,
    RecipeAccessDenied,
    checkRecipeAccess,
    hiveRecipeContext,
    projectResolverFor,
    requireRecipeAccess,
)

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
    return HiveRecipeContext(
        resolver=projectResolverFor(hive.manager, token_for(username)),
        recipe=recipe,
    )


async def runRecipe(text, ctx, workDir):
    """As a runner does: check every cited project first, then run."""
    config = yaml.safe_load(text)
    await requireRecipeAccess(config, ctx)
    with hiveRecipeContext(ctx):
        async with Workflow(config=config, parentDir=workDir).endPoints() as ends:
            for output in ends.outputs:
                await output.process(workDir)
            return ends


async def glyphMapAt(hive, project, ref):
    store = GitRepoStore.open(hive.root / f"{project}.git")
    try:
        backend = GitFontraBackend(store, commit=store.resolve(ref))
        glyphMap = await backend.getGlyphMap()
        await backend.aclose()
        return glyphMap
    finally:
        store.close()


def refs(hive, project):
    store = GitRepoStore.open(hive.root / f"{project}.git")
    try:
        return sorted(store.repo.refs.keys())
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
    refsBefore = refs(hive, "Target")
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
        assert "Hive-Recipe: merge" in info.message
        assert "Hive-Glyphs: Extra" in info.message
        # files that are not the font's are kept
        assert store.read_file(head, "workflows/merge.yaml") == b"steps: []\n"
        branchInfo = store.branch_info()["workflow/merge"]
        assert branchInfo["createdBy"] == "jeremie"
        assert branchInfo["recipe"] == "merge"
        assert branchInfo["base"] == hive.shas["Target"]
    finally:
        store.close()

    # a local branch and nothing else: no remote ref, no upstream/ branch;
    # the existing synchronisation takes it from there once merged
    added = set(refs(hive, "Target")) - set(refsBefore)
    assert b"refs/heads/workflow/merge" in added
    assert not [
        r for r in added if r.startswith((b"refs/remotes/", b"refs/heads/upstream/"))
    ]

    glyphs = run(glyphMapAt(hive, "Target", "workflow/merge"))
    assert "Extra" in glyphs and "A" in glyphs
    assert "Extra" not in run(glyphMapAt(hive, "Target", "main"))

    # provenance: the repository's uid and the commit, not the name alone
    assert [(r.repository, r.commit, r.ref) for r in ctx.reads] == [
        ("Latin", hive.shas["Latin"], "main"),
        ("Extra", hive.shas["Extra"], "main"),
    ]
    (write,) = ctx.writes
    assert (write.repository, write.branch, write.created) == (
        "Target",
        "workflow/merge",
        True,
    )
    assert write.commit == run(_head(hive, "Target", "workflow/merge"))


async def _head(hive, project, branch):
    store = GitRepoStore.open(hive.root / f"{project}.git")
    try:
        return store.head(branch)
    finally:
        store.close()


def test_hive_read_exposes_the_commit_it_resolved(hive):
    ctx = context(hive)
    config = {"steps": [{"input": "hive-read", "project": "Extra", "ref": "main"}]}
    run(requireRecipeAccess(config, ctx))
    action = HiveRead(project="Extra", ref="main")
    assert action.commit is None

    async def go():
        with hiveRecipeContext(ctx):
            async with action.prepare() as backend:
                return await backend.getGlyphMap()

    assert "Extra" in run(go())
    assert action.repository == "Extra"
    assert action.commit == hive.shas["Extra"]
    assert len(action.commit) == 40


def test_three_reads_each_checked_one_refused_before_any_read(hive, tmp_path):
    text = """
steps:
- input: hive-read
  project: Latin
- input: hive-read
  project: Extra
- input: hive-read
  project: Target@main
- output: hive-write
  project: Target
  branch: three
"""
    # jeremie reads all three, each on its own
    ctx = context(hive, recipe="three")
    run(runRecipe(text, ctx, tmp_path))
    assert [r.repository for r in ctx.reads] == ["Latin", "Extra", "Target"]

    # ana may read Latin and Target, not Extra: refused before anything is read
    ctx = context(hive, "ana", recipe="three")
    with pytest.raises(RecipeAccessDenied) as info:
        run(runRecipe(text, ctx, tmp_path))
    assert info.value.problems == [
        "steps[1] (hive-read): no project 'Extra'",
        "steps[3] (hive-write): observer cannot edit 'Target'",
    ]
    assert ctx.reads == [] and ctx.writes == []
    assert not ctx.checked

    # and running without the check is refused too, at the first step
    async def unchecked():
        with hiveRecipeContext(ctx):
            async with Workflow(config=yaml.safe_load(text)).endPoints():
                pass

    with pytest.raises(ActionError) as info:
        run(unchecked())
    assert str(info.value) == (
        "hive-read: the recipe's access was not checked before running"
    )
    assert ctx.reads == []


def test_a_second_run_commits_on_the_same_branch(hive, tmp_path):
    run(runRecipe(MERGE, context(hive), tmp_path))
    first = run(_head(hive, "Target", "workflow/merge"))

    ctx = context(hive)
    run(runRecipe(MERGE, ctx, tmp_path))
    # same inputs, same font: nothing to commit
    assert ctx.writes[0].commit is None
    assert ctx.writes[0].created is False

    # without Extra this time
    text = MERGE.replace("- input: hive-read\n  project: Extra@main\n", "")
    run(runRecipe(text, context(hive), tmp_path))
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
            "steps[0] (hive-read): no project 'Nope'",
        ),
        (
            "steps: [{input: hive-read, project: Latin, ref: nowhere}]",
            "steps[0] (hive-read): nothing called 'nowhere' in 'Latin' "
            "(a branch, a tag or a full commit id)",
        ),
        (
            "steps: [{input: hive-read, project: Latin@main, ref: other}]",
            "steps[0] (hive-read): two refs for Latin: 'main' and 'other'",
        ),
        (
            "steps: [{input: hive-read, project: a/b/c}]",
            "steps[0] (hive-read): not a project: 'a/b/c' (owner/name, a "
            "repository uid, or name on a development server)",
        ),
        (
            "steps: [{output: hive-write, project: Target, branch: main}]",
            "steps[0] (hive-write): never writes to the default branch",
        ),
        (
            "steps: [{output: hive-write, project: Target@dev}]",
            "steps[0] (hive-write): give the branch with 'branch', not '@'",
        ),
    ],
)
def test_mistakes_are_refused_before_running(hive, tmp_path, text, message):
    with pytest.raises(RecipeAccessDenied) as info:
        run(runRecipe(text, context(hive), tmp_path))
    assert info.value.problems == [message]


def test_the_default_branch_is_refused_when_running_too(hive, tmp_path):
    ctx = context(hive)
    ctx.checked = True  # as if a runner had skipped part of the check
    text = "steps: [{output: hive-write, project: Target, branch: main}]"

    async def go():
        with hiveRecipeContext(ctx):
            async with Workflow(config=yaml.safe_load(text)).endPoints() as ends:
                for output in ends.outputs:
                    await output.process(tmp_path)

    with pytest.raises(ActionError) as info:
        run(go())
    assert str(info.value) == (
        "hive-write: never writes to the default branch (main); write to "
        "another branch and merge it from the branches panel"
    )


def test_reserved_branch_names_are_refused(hive, tmp_path):
    text = "steps: [{output: hive-write, project: Target, branch: snapshot/x}]"
    with pytest.raises(ActionError) as info:
        run(runRecipe(text, context(hive), tmp_path))
    assert str(info.value) == (
        "hive-write: branch 'snapshot/x': Branch names cannot start with snapshot/."
    )


def test_writing_needs_edit(hive, tmp_path):
    text = """
steps:
- input: hive-read
  project: Latin
- output: hive-write
  project: Target
"""
    with pytest.raises(RecipeAccessDenied) as info:
        run(runRecipe(text, context(hive, "ana"), tmp_path))
    assert info.value.problems == [
        "steps[1] (hive-write): observer cannot edit 'Target'"
    ]
    store = GitRepoStore.open(hive.root / "Target.git")
    assert store.branches() == ["main"]
    store.close()


def test_unknown_user_reads_nothing(hive, tmp_path):
    with pytest.raises(RecipeAccessDenied):
        run(runRecipe(MERGE, context(hive, "nobody"), tmp_path))


def test_actions_need_a_hive_context(tmp_path):
    config = yaml.safe_load("steps: [{input: hive-read, project: Latin}]")

    async def go():
        async with Workflow(config=config, parentDir=tmp_path).endPoints():
            pass

    with pytest.raises(ActionError) as info:
        run(go())
    assert "only available in recipes run by a Fontra Hive server" in str(info.value)


def test_check_recipe_access_finds_nested_steps(hive):
    nested = yaml.safe_load("""
steps:
- fork:
  steps:
  - output: hive-write
    project: Target
    branch: main
""")
    ctx = context(hive)
    assert run(checkRecipeAccess(nested, ctx)) == [
        "steps[0].steps[0] (hive-write): never writes to the default branch"
    ]
    assert not ctx.checked


# --- naming a project ----------------------------------------------------------------

UID = "0f8fad5b-d9cb-469f-a165-70867728950e"


@pytest.mark.parametrize(
    "text, kind, project, ref",
    [
        ("owner/name", "path", "owner/name", None),
        ("owner/name@v1.2", "path", "owner/name", "v1.2"),
        (UID, "uid", UID, None),
        (UID.upper() + "@main", "uid", UID, "main"),
        ("Latin", "name", "Latin", None),
        ("Latin@feature/x", "name", "Latin", "feature/x"),
    ],
)
def test_project_refs(text, kind, project, ref):
    parsed = HiveProjectRef(text)
    assert isinstance(parsed, str) and parsed == text
    assert (parsed.kind, parsed.project, parsed.ref) == (kind, project, ref)
    assert HiveProjectRef(parsed) is parsed


@pytest.mark.parametrize("text", ["", "a/b/c", "owner/", "x@", "a b", 3])
def test_bad_project_refs(text):
    with pytest.raises(ActionError):
        HiveProjectRef(text)


def test_actions_hold_a_project_ref():
    action = HiveRead(project="owner/name@v1")
    assert isinstance(action.project, HiveProjectRef)
    assert action.project.withRef(None) == "v1"
    with pytest.raises(ActionError):
        HiveRead(project="owner/name@v1", ref="v2")


def test_a_repository_uid_names_a_dev_project_too(hive, tmp_path):
    # In development the repository is Name.git: its uid is the name.
    shutil.copytree(hive.root / "Latin.git", hive.root / f"{UID}.git")
    manager = DevHiveProjectManager(hive.root)  # no directory: no accounts
    ctx = HiveRecipeContext(resolver=projectResolverFor(manager, None), recipe="uid")
    text = f"""
steps:
- input: hive-read
  project: {UID}
- output: hive-write
  project: Target
"""
    run(runRecipe(text, ctx, tmp_path))
    assert ctx.reads[0].repository == UID


# --- production: roles from hive-api ---------------------------------------------------


def test_hive_api_resolver(tmp_path, fixture_fontra):
    from fontra_hive.access import CAPABILITIES, Access, HiveUser
    from fontra_hive.recipe import HiveApiProjectResolver

    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / f"{UID}.git")
    store.import_directory(fixture_fontra, message="Import", author=IMPORTER)
    store.close()

    user = HiveUser("jeremie", "Jérémie", "", uid="u1")
    asked = []

    class FakeApi:
        async def access(self, token, project):
            asked.append(("access", project))
            if project.lower() != "owner/latin":
                return None
            return types.SimpleNamespace(
                access=Access(user, "designer", CAPABILITIES["designer"]),
                repo=f"{UID}.git",
                defaultBranch="trunk",
                projectId="owner/Latin",
            )

        async def projects(self, token):
            asked.append(("projects", token))
            return [{"id": "owner/Latin", "uid": UID, "repo": f"{UID}.git"}]

    manager = types.SimpleNamespace(
        api=FakeApi(),
        rootPath=root,
        _ensureRepo=lambda projectAccess: root / projectAccess.repo,
    )
    resolver = projectResolverFor(manager, "hive:u1")
    assert isinstance(resolver, HiveApiProjectResolver)

    project = run(resolver.resolve(HiveProjectRef("owner/latin")))
    assert project.repository == UID
    assert project.path == "owner/Latin"
    assert project.repoPath == root / f"{UID}.git"
    assert project.defaultBranch == "trunk"
    assert project.can("edit") and not project.can("merge")
    assert project.author(IMPORTER).email == "jeremie@hive"

    # by repository uid: found among the person's projects, then checked
    byUid = run(resolver.resolve(HiveProjectRef(UID)))
    assert byUid.repository == UID and byUid.path == "owner/Latin"
    other = "11111111-2222-3333-4444-555555555555"
    assert run(resolver.resolve(HiveProjectRef(other))) is None

    assert run(resolver.resolve(HiveProjectRef("someone/else"))) is None
    assert run(resolver.resolve(HiveProjectRef("no-owner"))) is None  # never asked
    assert asked == [
        ("access", "owner/latin"),
        ("projects", "hive:u1"),
        ("access", "owner/Latin"),
        ("projects", "hive:u1"),
        ("access", "someone/else"),
    ]


# --- validation and the catalog (fontra-workflow-ui) ------------------------------------


def test_hive_validation_refuses_file_actions():
    pytest.importorskip("fontra_workflow_ui")
    from fontra_hive.recipe import validateHiveRecipe

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
    from fontra_hive.recipe import validateHiveRecipe

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
    # "project" is a Hive project, not any string
    assert read.properties["project"]["format"] == "hive-project"
    assert write.properties["project"]["format"] == "hive-project"
    assert read.descriptionSource == "docstring"
    assert all(s.get("description") for s in write.properties.values())
    # set by the engine or by the action, not by a recipe
    for name in ("input", "repository", "commit"):
        assert name not in write.properties
    assert "commit" not in read.properties
