"""The recipe runner: one process per run, limits, what is recorded, and the
Download panel as a recipe."""

import asyncio
import datetime
import json
import os
import pathlib
import shutil
import sys
import types
import zipfile

import pytest
from fontra.backends.fontra import FontraBackend
from fontra.workflow.actions import ActionError

from conftest import run
from fontra_hive import export, runner
from fontra_hive.access import DevDirectory, token_for
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.hiveapi import HiveApiUnavailable
from fontra_hive.projectmanager import DevHiveProjectManager
from fontra_hive.recipe import HiveArtifact, projectResolverFor

AUTHOR = Signature("Importer", "importer@example.com")
USERS = {
    "users": {
        "jeremie": {"name": "Jérémie Hornus", "email": "j@example.com"},
        "ana": {"name": "Ana", "email": "ana@example.com"},
    },
    "projects": {
        "Latin": {"owner": "jeremie", "collaborators": {"ana": "observer"}},
        "Extra": {"owner": "jeremie", "collaborators": {"ana": "observer"}},
        "Target": {"owner": "jeremie", "collaborators": {"ana": "observer"}},
        "Build": {"owner": "jeremie", "collaborators": {"ana": "observer"}},
    },
}

TWO_INPUTS_TWO_OUTPUTS = """
steps:
- input: hive-read
  project: Latin
- input: hive-read
  project: Extra
- output: hive-write
  project: Target
- output: hive-artifact
  format: {format}
  name: Merged
"""


async def addGlyph(fontraPath, source, newName):
    backend = FontraBackend.fromPath(fontraPath)
    glyph = await backend.getGlyph(source)
    glyph.name = newName
    await backend.putGlyph(newName, glyph, [])
    await backend.aclose()


def addRecipe(hive, text, files=None):
    """Commit a recipe to the recipe's own repository; returns the commit."""
    store = GitRepoStore.open(hive.root / "Build.git")
    try:
        return store.commit(
            {"recipe.yaml": text.encode("utf-8"), **(files or {})},
            message="The recipe",
            author=AUTHOR,
        )
    finally:
        store.close()


@pytest.fixture
def hive(tmp_path, fixture_fontra):
    root = tmp_path / "repos"
    root.mkdir()
    extra = tmp_path / "extra.fontra"
    shutil.copytree(fixture_fontra, extra)
    run(addGlyph(extra, "A", "Extra"))
    for name, font in [
        ("Latin", fixture_fontra),
        ("Extra", extra),
        ("Target", fixture_fontra),
    ]:
        store = GitRepoStore.create(root / f"{name}.git")
        store.import_directory(font, message="Import", author=AUTHOR)
        store.close()
    GitRepoStore.create(root / "Build.git").close()
    usersPath = tmp_path / "users.json"
    usersPath.write_text(json.dumps(USERS))
    manager = DevHiveProjectManager(root, directory=DevDirectory(usersPath))
    return types.SimpleNamespace(root=root, manager=manager, runs=manager.runsRoot)


def start(hive, username="jeremie", ref=None, substitutions=None, limits=None, **kw):
    """Run the Build recipe as ``username``."""

    async def go():
        resolver = projectResolverFor(hive.manager, token_for(username))
        source = await runner.recipeSource(resolver, "Build", ref)
        assert source is not None
        request = runner.RunRequest(
            name="build", source=source, substitutions=substitutions or {}
        )
        return await runner.runRecipe(
            request,
            resolver=resolver,
            registry=hive.manager.recipeRuns,
            runsRoot=hive.runs,
            limits=limits or runner.RunLimits(),
            **kw,
        )

    return run(go())


def workFolders(hive):
    return [p for p in hive.runs.iterdir() if p.name.startswith("work-")]


def recorded(hive, result):
    return hive.manager.recipeRuns.get(result.id)


def assertRecordedAndClean(hive, result, state):
    assert result.state == state, result.message
    record = recorded(hive, result)
    assert record["state"] == state
    assert record["message"] == result.message
    assert record["durationSeconds"] > 0
    assert workFolders(hive) == []


needsValidation = pytest.mark.skipif(
    pytest.importorskip("importlib.util").find_spec("fontra_workflow_ui") is None,
    reason="needs fontra-workflow-ui",
)
needsFonts = pytest.mark.skipif(
    not export.can_build_fonts(), reason="needs fontra-compile and fontmake"
)


# --- end to end ------------------------------------------------------------------


@needsValidation
@needsFonts
def test_two_inputs_and_two_outputs_from_the_recipe_repository(hive):
    commit = addRecipe(hive, TWO_INPUTS_TWO_OUTPUTS.format(format="ttf"))
    events = []
    result = start(hive, onProgress=events.append)
    assertRecordedAndClean(hive, result, "succeeded")
    assert [r["project"] for r in result.reads] == ["Latin", "Extra"]
    assert all(len(r["commit"]) == 40 and r["repository"] for r in result.reads)
    assert len(result.writes) == 1
    assert result.writes[0]["branch"] == "workflow/build"
    assert result.writes[0]["repository"] == "Target"
    artifact = result.artifacts[0]
    assert (artifact["name"], artifact["format"]) == ("Merged.ttf", "ttf")
    path = result.directory / artifact["name"]
    assert path.stat().st_size == artifact["size"]
    import hashlib

    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    assert path.read_bytes()[:4] in (b"\0\1\0\0", b"true")
    assert result.outputBytes >= artifact["size"]
    assert recorded(hive, result)["commit"] == commit
    assert recorded(hive, result)["repository"] == "Build"
    assert [e.get("action") for e in events if e.get("state") == "done"] == [
        "hive-write",
        "hive-artifact",
    ]
    assert [e["stage"] for e in events if "stage" in e] == [
        "validate",
        "access",
        "run",
    ]
    store = GitRepoStore.open(hive.root / "Target.git")
    assert store.head("workflow/build") == result.writes[0]["commit"]
    extra = "glyphs/Extra^1.json"
    assert extra in store.list_tree(store.head("workflow/build"))
    assert extra not in store.list_tree(store.head("main"))
    store.close()


@needsValidation
def test_sources_as_artifacts_and_several_formats(hive):
    addRecipe(
        hive,
        """
steps:
- input: hive-read
  project: Latin
- output: hive-artifact
  format: fontra
- output: hive-artifact
  format: designspace
  name: "../../etc/passwd"
""",
    )
    result = start(hive)
    assert result.state == "invalid"  # ".." is refused before anything runs

    addRecipe(
        hive,
        """
steps:
- input: hive-read
  project: Latin
- output: hive-artifact
  format: fontra
- output: hive-artifact
  format: designspace
  name: "a name / with things"
""",
    )
    result = start(hive)
    assert result.succeeded, result.message
    names = [a["name"] for a in result.artifacts]
    assert names == ["build.fontra.zip", "a-name-with-things.designspace.zip"]


@needsValidation
def test_substitutions_are_checked_once_applied(hive):
    addRecipe(
        hive,
        """
parameters:
  list: {}
  project: {}
steps:
- input: hive-read
  project: "{project}"
- filter: subset-glyphs
  glyphNamesFile: "{list}"
- output: hive-artifact
  format: fontra
""",
        {"assets/glyphs.txt": b"A\n"},
    )
    result = start(hive, substitutions={"list": "../../secret", "project": "Latin"})
    assertRecordedAndClean(hive, result, "invalid")
    assert "secret" in result.message or ".." in result.message
    result = start(hive, substitutions={"list": "/etc/passwd", "project": "Latin"})
    assert result.state == "invalid"
    result = start(
        hive, substitutions={"list": "assets/glyphs.txt", "project": "Latin"}
    )
    assert result.succeeded, result.message
    names = zipfile.ZipFile(result.directory / "build.fontra.zip").namelist()
    assert "build.fontra/glyphs/A^1.json" in names
    assert not any(n.startswith("build.fontra/glyphs/B") for n in names)


@needsValidation
def test_symbolic_links_in_assets_are_refused(hive):
    from dulwich.objects import Blob, Commit, Tree

    commit = addRecipe(hive, "steps: []\n", {"assets/ok.txt": b"A\n"})
    store = GitRepoStore.open(hive.root / "Build.git")
    repo = store.repo
    link = Blob.from_string(b"/etc/passwd")
    repo.object_store.add_object(link)
    root = repo[repo[commit.encode()].tree]
    assets = repo[root[b"assets"][1]]
    assets[b"link"] = (0o120000, link.id)
    repo.object_store.add_object(assets)
    root[b"assets"] = (0o040000, assets.id)
    repo.object_store.add_object(root)
    new = Commit()
    new.tree, new.parents = root.id, [commit.encode()]
    new.author = new.committer = b"T <t@example.com>"
    new.author_time = new.commit_time = 1
    new.author_timezone = new.commit_timezone = 0
    new.message = b"link"
    repo.object_store.add_object(new)
    sha = new.id.decode()
    store.close()
    result = start(hive, ref=sha)
    assertRecordedAndClean(hive, result, "invalid")
    assert "assets/link" in result.message


# --- refusals, before anything is read ----------------------------------------------


@needsValidation
def test_an_unreadable_input_stops_the_run_before_any_read(hive):
    data = json.loads((hive.root.parent / "users.json").read_text())
    data["projects"]["Extra"]["collaborators"] = {}
    (hive.root.parent / "users.json").write_text(json.dumps(data))
    addRecipe(hive, TWO_INPUTS_TWO_OUTPUTS.format(format="fontra"))
    result = start(hive, username="ana")
    assertRecordedAndClean(hive, result, "denied")
    assert "Extra" in result.message
    assert result.reads == [] and result.writes == [] and result.artifacts == []


@needsValidation
def test_an_output_that_cannot_be_changed_stops_the_run_before_any_read(hive):
    addRecipe(hive, TWO_INPUTS_TWO_OUTPUTS.format(format="fontra"))
    result = start(hive, username="ana")  # observer everywhere
    assertRecordedAndClean(hive, result, "denied")
    assert "Target" in result.message
    assert result.reads == [] and result.writes == []
    store = GitRepoStore.open(hive.root / "Target.git")
    assert store.head("workflow/build") is None
    store.close()


@needsValidation
@pytest.mark.parametrize(
    "text, expected",
    [
        ("steps: [", "not YAML"),
        ("steps:\n- input: fontra-read\n  source: /etc\n", "fontra-read"),
        (
            "steps:\n- input: hive-read\n  project: Latin\n"
            "- output: compile-fontmake\n  destination: a.ttf\n",
            "compile-fontmake",
        ),
        ("steps:\n- filter: no-such-action\n", "no-such-action"),
    ],
)
def test_an_invalid_recipe_ends_properly(hive, text, expected):
    addRecipe(hive, text)
    result = start(hive)
    assertRecordedAndClean(hive, result, "invalid")
    assert expected in result.message


@needsValidation
def test_a_repository_without_a_recipe(hive):
    store = GitRepoStore.open(hive.root / "Build.git")
    store.commit({"notes.txt": b"x"}, message="m", author=AUTHOR)
    store.close()
    result = start(hive)
    assertRecordedAndClean(hive, result, "invalid")
    assert "recipe.yaml" in result.message


@needsValidation
def test_every_output_action_but_ours_is_denied():
    from fontra_workflow_ui.introspect import buildCatalog

    from fontra_hive.recipe import HIVE_DENIED_ACTIONS

    outputs = set(buildCatalog().actions["output"])
    assert outputs - HIVE_DENIED_ACTIONS == {"hive-write", "hive-artifact"}


def test_artifact_has_no_path_argument():
    import dataclasses

    names = {f.name for f in dataclasses.fields(HiveArtifact) if f.init}
    assert names == {"format", "name"}
    with pytest.raises(ActionError):
        HiveArtifact(format="exe")
    artifact = HiveArtifact(format="ttf", name="../../a b")
    assert artifact.fileNameFor("x") == "a-b.ttf"


@needsValidation
def test_two_artifacts_with_one_name_are_refused_first(hive):
    addRecipe(
        hive,
        """
steps:
- input: hive-read
  project: Latin
- output: hive-artifact
  format: fontra
- output: hive-artifact
  format: fontra
""",
    )
    result = start(hive)
    assertRecordedAndClean(hive, result, "denied")
    assert result.reads == []
    assert "two artifacts" in result.message


# --- limits ----------------------------------------------------------------------


def slowRecipe(hive):
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: designspace\n",
    )


@needsValidation
def test_a_run_that_takes_too_long_is_stopped(hive):
    slowRecipe(hive)
    result = start(hive, limits=runner.RunLimits(timeSeconds=0.05))
    assertRecordedAndClean(hive, result, "timeout")
    assert "longer than" in result.message
    assert result.artifacts == [] and result.directory is None


@needsValidation
def test_outputs_larger_than_allowed(hive):
    slowRecipe(hive)
    result = start(hive, limits=runner.RunLimits(outputBytes=200))  # one file
    assertRecordedAndClean(hive, result, "too-large")
    assert result.artifacts == []

    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: fontra\n  name: one\n"
        "- output: hive-artifact\n  format: fontra\n  name: two\n",
    )
    both = start(hive)
    assert both.succeeded, both.message
    limit = both.artifacts[0]["size"] * 3 // 2  # the two together
    result = start(hive, limits=runner.RunLimits(outputBytes=limit))
    assertRecordedAndClean(hive, result, "too-large")
    assert result.outputBytes > limit
    assert recorded(hive, result)["outputBytes"] == result.outputBytes
    assert result.artifacts == [] and result.directory is None


@needsValidation
def test_a_write_larger_than_allowed_commits_nothing(hive):
    addRecipe(hive, TWO_INPUTS_TWO_OUTPUTS.format(format="fontra"))
    result = start(hive, limits=runner.RunLimits(outputBytes=100))
    assertRecordedAndClean(hive, result, "too-large")
    assert result.writes == []
    store = GitRepoStore.open(hive.root / "Target.git")
    assert store.head("workflow/build") is None
    store.close()


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS: Linux only")
@needsValidation
def test_memory_is_bounded_on_linux(hive):
    """Under macOS the limit is only indicative: not tested there."""
    slowRecipe(hive)
    # Far below what the interpreter already mapped: the first large
    # allocation fails.
    result = start(hive, limits=runner.RunLimits(memoryBytes=64 << 20))
    assert result.state in ("out-of-memory", "failed"), result.message
    assertRecordedAndClean(hive, result, result.state)


def test_limits_can_only_be_lowered():
    limits = runner.RunLimits(timeSeconds=10, memoryBytes=1000, outputBytes=1000)
    lowered = limits.lowered(
        {"timeSeconds": 5, "memoryBytes": 5000, "outputBytes": None, "other": 1}
    )
    assert lowered == runner.RunLimits(5, 1000, 1000)
    assert limits.lowered(None) == limits
    assert limits.lowered({"timeSeconds": 0, "memoryBytes": True}) == limits


def test_the_registry_gives_lower_limits(hive, fixture_fontra):
    class Registry(runner.LocalRunRegistry):
        async def open(self, request):
            ticket = await super().open(request)
            return runner.RunTicket(ticket.id, {"timeSeconds": 0.01})

    store = GitRepoStore.open(hive.root / "Latin.git")
    head = store.head("main")
    store.close()
    result = run(
        runner.runRecipe(
            runner.downloadRequest("Latin", head, "Latin", "designspace"),
            resolver=projectResolverFor(hive.manager, token_for("jeremie")),
            registry=Registry(hive.runs),
            runsRoot=hive.runs,
        )
    )
    assert result.state == "timeout"


def test_a_refused_run_starts_nothing(hive):
    class Registry(runner.LocalRunRegistry):
        async def open(self, request):
            raise runner.RunRefused(429, "Too many runs.")

    with pytest.raises(runner.RunRefused):
        run(
            runner.runRecipe(
                runner.downloadRequest("Latin", "x", "Latin", "fontra"),
                resolver=projectResolverFor(hive.manager, token_for("jeremie")),
                registry=Registry(hive.runs),
                runsRoot=hive.runs,
            )
        )
    assert not hive.runs.exists()


def test_runs_wait_for_a_free_slot(hive):
    store = GitRepoStore.open(hive.root / "Latin.git")
    head = store.head("main")
    store.close()

    async def go():
        slot = asyncio.Semaphore(1)
        peak = 0

        async def progress(event):
            nonlocal peak
            peak = max(peak, len(workFolders(hive)))

        resolver = projectResolverFor(hive.manager, token_for("jeremie"))
        return (
            await asyncio.gather(
                *[
                    runner.runRecipe(
                        runner.downloadRequest("Latin", head, f"L{i}", "fontra"),
                        resolver=resolver,
                        registry=hive.manager.recipeRuns,
                        runsRoot=hive.runs,
                        onProgress=progress,
                        slot=slot,
                    )
                    for i in range(3)
                ]
            ),
            peak,
        )

    results, peak = run(go())
    assert all(r.succeeded for r in results)
    assert peak == 1


# --- compilers -------------------------------------------------------------------


FAILING_FONTMAKE = """
import sys
def main(args=None):
    print("thread 'main' panicked at src/lib.rs:12:5:", file=sys.stderr)
    print("index out of bounds", file=sys.stderr)
    print("note: run with RUST_BACKTRACE=1", file=sys.stderr)
    sys.exit(101)
"""


@needsValidation
@needsFonts
def test_a_compiler_that_fails_ends_properly(hive, tmp_path, monkeypatch):
    shim = tmp_path / "shim" / "fontmake"
    shim.mkdir(parents=True)
    (shim / "__init__.py").write_text("")
    (shim / "__main__.py").write_text(FAILING_FONTMAKE)
    path = os.pathsep.join([str(shim.parent), *sys.path])
    monkeypatch.setenv("PYTHONPATH", path)
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: ttf\n",
    )
    result = start(hive)
    assertRecordedAndClean(hive, result, "failed")
    assert result.message == runner.COMPILER_CRASHED
    assert "index out of bounds" in result.log  # the whole text stays in the log


@needsValidation
@needsFonts
def test_the_process_holds_no_secret(hive, tmp_path, monkeypatch):
    shim = tmp_path / "shim" / "fontmake"
    shim.mkdir(parents=True)
    (shim / "__init__.py").write_text("")
    (shim / "__main__.py").write_text(
        "import os, sys\n"
        "def main(args=None):\n"
        "    sys.exit('env ' + ' '.join(sorted(os.environ)) + ' tmp ' + os.getcwd())\n"
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(shim.parent), *sys.path]))
    monkeypatch.setenv("HIVE_SERVICE_KEY", "s3cret")
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: ttf\n",
    )
    result = start(hive)
    assert result.state == "failed"
    assert result.message == runner.RUN_FAILED
    assert "TMPDIR" in result.log  # what fontmake said is in the log
    assert "HIVE_SERVICE_KEY" not in result.log
    assert str(hive.runs) not in result.log  # no path of the server


@needsValidation
@needsFonts
def test_refused_feature_code_is_said_so(hive):
    store = GitRepoStore.open(hive.root / "Latin.git")
    store.commit(
        {"features.txt": b"include(/etc/hostname);\n"}, message="F", author=AUTHOR
    )
    store.close()
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: ttf\n",
    )
    result = start(hive)
    assertRecordedAndClean(hive, result, "invalid")
    assert result.reason == runner.REFUSED_FEATURES
    assert result.message == export.FEATURES_REFUSED
    assert result.artifacts == []


def test_a_failed_run_says_one_of_our_sentences():
    def failed(message="", log="", reason=""):
        result = runner.RunResult("x", runner.FAILED, message, log=log)
        result.reason = reason
        return runner.failedMessage(result)

    assert failed('got NAME "word"', "word\n") == runner.RUN_FAILED
    assert failed("", "x\nthread 'a' panicked at b.rs:1:2:\nword") == (
        runner.COMPILER_CRASHED
    )
    assert failed("word", reason=runner.STOPPED) == runner.RUN_STOPPED
    for message in runner.FAILED_MESSAGES:
        assert failed(message, "thread 'a' panicked at b.rs:1:2:") == message


@needsValidation
def test_what_an_action_says_stays_in_the_log(hive):
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- filter: add-features\n  featureFile: assets/extra.fea\n"
        "- output: hive-artifact\n  format: fontra\n",
        {"assets/extra.fea": b"feature liga {\n  word-of-the-witness\n} liga;\n"},
    )
    result = start(hive)
    assert result.state == "failed", result.message
    assert result.message in runner.FAILED_MESSAGES
    assert "word-of-the-witness" not in result.message
    assert "word-of-the-witness" in result.log
    assert recorded(hive, result)["message"] == result.message


@needsValidation
def test_feature_code_in_an_asset_is_checked_before_running(hive):
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- filter: add-features\n  featureFile: assets/extra.fea\n"
        "- output: hive-artifact\n  format: fontra\n",
        {"assets/extra.fea": b"include(/etc/hostname);\n"},
    )
    result = start(hive)
    assertRecordedAndClean(hive, result, "invalid")
    assert result.reason == runner.REFUSED_FEATURES
    assert result.message == export.FEATURES_REFUSED
    assert result.reads == []  # before anything is read


@needsValidation
def test_any_asset_is_checked(hive):
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: fontra\n",
        {"assets/shared/more.txt": b"feature liga {\n include ( ../../x );\n} liga;"},
    )
    result = start(hive)
    assert (result.state, result.reason) == ("invalid", runner.REFUSED_FEATURES)


@needsValidation
def test_assets_with_relative_includes_are_fine(hive):
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: fontra\n",
        {
            "assets/extra.fea": b"include(shared/more.fea); # include(/etc/x);\n",
            "assets/shared/more.fea": b"feature liga { sub f i by f_i; } liga;\n",
        },
    )
    result = start(hive)
    assert result.succeeded, result.message


def includeElsewhere(hive, project="Latin"):
    store = GitRepoStore.open(hive.root / f"{project}.git")
    try:
        store.commit(
            {"features.txt": b"include(/etc/hostname);\n"}, message="F", author=AUTHOR
        )
    finally:
        store.close()


@needsValidation
def test_a_recipe_does_not_read_a_project_that_includes_files_from_elsewhere(hive):
    includeElsewhere(hive)
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: fontra\n",
    )
    result = start(hive)
    assertRecordedAndClean(hive, result, "invalid")
    assert result.reason == runner.REFUSED_FEATURES
    assert result.message == export.FEATURES_REFUSED
    assert result.reads == [] and result.artifacts == []


def test_its_sources_are_still_downloaded(hive):
    includeElsewhere(hive)
    result = download(hive, "Latin", "Latin", "fontra")
    assert result.succeeded, result.message
    result.discard()


def test_no_path_of_the_server_in_what_people_read():
    assert (
        runner._scrub("/srv/w/x/font.ttf and /srv/w", ["/srv/w"]) == "x/font.ttf and "
    )


def test_the_log_keeps_the_end(hive):
    log = runner._Log()

    class Stream:
        chunks = [b"a" * runner.LOG_BYTES, b"b" * 10]

        async def read(self, size):
            return self.chunks.pop(0) if self.chunks else b""

    run(log.follow(Stream()))
    assert log.text().startswith("[…]\n") and log.text().endswith("b" * 10)
    assert len(log.data) == runner.LOG_BYTES


# --- artifacts belong to the run ---------------------------------------------------


@needsValidation
def test_an_artifact_is_read_by_who_reads_the_recipe(hive):
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: fontra\n",
    )
    result = start(hive)
    assert result.succeeded, result.message
    assert result.directory.parent == hive.runs
    registry = hive.manager.recipeRuns

    def get(username, name="build.fontra.zip", runId=result.id):
        token = token_for(username)
        resolver = projectResolverFor(hive.manager, token)
        return run(registry.artifact(runId, name, token, resolver))

    assert get("jeremie")["sha256"] == result.artifacts[0]["sha256"]
    assert get("ana")["size"] == result.artifacts[0]["size"]
    data = json.loads((hive.root.parent / "users.json").read_text())
    data["projects"]["Build"]["collaborators"] = {}
    (hive.root.parent / "users.json").write_text(json.dumps(data))
    assert get("ana") is None
    assert get("jeremie", name="other.ttf") is None
    assert get("jeremie", runId="../x") is None


# --- the Download panel --------------------------------------------------------------


def legacyBuild(hive, project, stem, format, tmp_path):
    """What the route did before: the tree of the commit, then export.build."""
    store = GitRepoStore.open(hive.root / f"{project}.git")
    work = tmp_path / f"legacy-{format}"
    source = work / "source" / f"{stem}.fontra"
    store.export(store.head("main"), source)
    store.close()
    return run(export.build(source, work, stem, format))


def download(hive, project, stem, format):
    store = GitRepoStore.open(hive.root / f"{project}.git")
    head = store.head("main")
    store.close()
    return run(
        runner.runRecipe(
            runner.downloadRequest(project, head, stem, format),
            resolver=projectResolverFor(hive.manager, token_for("jeremie")),
            registry=hive.manager.recipeRuns,
            runsRoot=hive.runs,
        )
    )


def entries(path):
    with zipfile.ZipFile(path) as archive:
        return [(i.filename, archive.read(i)) for i in archive.infolist()]


@pytest.mark.parametrize("format", ["fontra", "designspace"])
def test_the_download_is_what_it_was_byte_for_byte(hive, tmp_path, format):
    store = GitRepoStore.open(hive.root / "Latin.git")
    # A file that is not part of the font, and is delivered all the same.
    store.commit({"notes/readme.txt": b"hello"}, message="m", author=AUTHOR)
    store.close()
    legacy = legacyBuild(hive, "Latin", "Latin", format, tmp_path)
    result = download(hive, "Latin", "Latin", format)
    assert result.succeeded, result.message
    new = result.directory / result.artifacts[0]["name"]
    assert new.name == legacy.name
    assert entries(new) == entries(legacy)
    result.discard()
    assert not result.directory.exists()
    assert recorded(hive, result)["builtin"] == "download"
    assert workFolders(hive) == []


@needsFonts
@pytest.mark.parametrize("format", ["ttf", "woff2"])
def test_the_download_builds_fonts(hive, tmp_path, format):
    if format == "woff2":
        pytest.importorskip("brotli")
    result = download(hive, "Latin", "Latin", format)
    assert result.succeeded, result.message
    assert result.artifacts[0]["name"] == f"Latin.{format}"
    new = (result.directory / result.artifacts[0]["name"]).read_bytes()
    legacy = legacyBuild(hive, "Latin", "Latin", format, tmp_path).read_bytes()
    assert len(new) > 0 and new[:4] == legacy[:4]


def test_a_filtered_font_is_not_the_stored_package(hive):
    store = GitRepoStore.open(hive.root / "Latin.git")
    head = store.head("main")
    store.close()
    request = runner.RunRequest(
        name="sub",
        builtin="test",
        config={
            "steps": [
                {"input": "hive-read", "project": "Latin", "ref": head},
                {"filter": "subset-glyphs", "glyphNames": ["A"]},
                {"output": "hive-artifact", "format": "fontra"},
            ]
        },
    )
    result = run(
        runner.runRecipe(
            request,
            resolver=projectResolverFor(hive.manager, token_for("jeremie")),
            registry=hive.manager.recipeRuns,
            runsRoot=hive.runs,
        )
    )
    assert result.succeeded, result.message
    names = [n for n, _ in entries(result.directory / "sub.fontra.zip")]
    assert "sub.fontra/glyphs/A^1.json" in names
    assert "sub.fontra/glyphs/B^1.json" not in names


def test_a_project_name_with_a_space_on_a_development_server(hive, fixture_fontra):
    store = GitRepoStore.create(hive.root / "My Font.git")
    store.import_directory(fixture_fontra, message="Import", author=AUTHOR)
    head = store.head("main")
    store.close()
    data = json.loads((hive.root.parent / "users.json").read_text())
    data["projects"]["My Font"] = {"owner": "jeremie"}
    (hive.root.parent / "users.json").write_text(json.dumps(data))
    result = run(
        runner.runRecipe(
            runner.downloadRequest("My Font", head, "My-Font", "fontra"),
            resolver=projectResolverFor(hive.manager, token_for("jeremie")),
            registry=hive.manager.recipeRuns,
            runsRoot=hive.runs,
        )
    )
    assert result.succeeded, result.message


# --- hive-api ---------------------------------------------------------------------


class FakeApi:
    def __init__(self, answer):
        self.answer = answer
        self.closed = []

    async def recipeRunOpen(self, opening):
        self.opening = opening
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def recipeRunClose(self, run, record):
        self.closed.append((run, record))


RUN_ID = "0b7c1e2a-1111-2222-3333-444455556666"
OF_A_RECIPE = runner.RunRequest(
    name="build",
    user="u",
    source=runner.RecipeSource("r", "o/build", pathlib.Path("x"), "a" * 40),
)
BUILT_IN = runner.downloadRequest("o/font", "a" * 40, "font", "fontra", user="u")


def test_hive_api_opens_and_closes():
    api = FakeApi((201, {"id": RUN_ID, "limits": {"timeSeconds": 5}}))
    registry = runner.HiveApiRunRegistry(api)
    ticket = run(registry.open(OF_A_RECIPE))
    assert (ticket.id, ticket.limits) == (RUN_ID, {"timeSeconds": 5})
    assert api.opening["recipe"] == "o/build" and api.opening["builtin"] is None
    run(registry.close(ticket, {"state": "succeeded"}))
    assert api.closed == [(RUN_ID, {"state": "succeeded"})]
    run(registry.open(BUILT_IN))
    assert api.opening["builtin"] == "download" and api.opening["project"] == "o/font"
    assert api.opening["commit"] == "a" * 40 and api.opening["recipe"] is None


def test_hive_api_refuses():
    registry = runner.HiveApiRunRegistry(FakeApi((403, {"detail": "No."})))
    with pytest.raises(runner.RunRefused) as refused:
        run(registry.open(OF_A_RECIPE))
    assert (refused.value.status, refused.value.text) == (403, "No.")
    with pytest.raises(runner.RunRefused):  # a download too
        run(registry.open(BUILT_IN))
    registry = runner.HiveApiRunRegistry(FakeApi((201, {"id": "../etc"})))
    with pytest.raises(runner.RunRefused):
        run(registry.open(OF_A_RECIPE))


@pytest.mark.parametrize(
    "answer", [HiveApiUnavailable("down"), (404, "<html>Not found</html>")]
)
def test_a_download_goes_on_without_the_registry(answer):
    api = FakeApi(answer)
    registry = runner.HiveApiRunRegistry(api)
    ticket = run(registry.open(BUILT_IN))
    assert not ticket.registered
    run(registry.close(ticket, {}))
    assert api.closed == []


def test_a_recipe_does_not_go_on_without_the_registry():
    registry = runner.HiveApiRunRegistry(FakeApi(HiveApiUnavailable("down")))
    with pytest.raises(runner.RunRefused) as refused:
        run(registry.open(OF_A_RECIPE))
    assert refused.value.status == 503


def test_a_registry_that_fails_at_the_end_loses_nothing(hive):
    class Registry(runner.LocalRunRegistry):
        async def close(self, ticket, record):
            raise HiveApiUnavailable("down")

    store = GitRepoStore.open(hive.root / "Latin.git")
    head = store.head("main")
    store.close()
    result = run(
        runner.runRecipe(
            runner.downloadRequest("Latin", head, "Latin", "fontra"),
            resolver=projectResolverFor(hive.manager, token_for("jeremie")),
            registry=Registry(hive.runs),
            runsRoot=hive.runs,
        )
    )
    assert result.succeeded and (result.directory / "Latin.fontra.zip").exists()


# --- the route -------------------------------------------------------------------


def callExport(hive, monkeypatch, project="Latin", **query):
    from fontra_hive import projectmanager

    sent = {}

    async def sendFile(request, path):
        sent.update(name=path.name, data=path.read_bytes())
        return "sent"

    async def authorize(request):
        return token_for("jeremie")

    monkeypatch.setattr(projectmanager, "_sendFile", sendFile)
    monkeypatch.setattr(hive.manager, "authorize", authorize)
    request = types.SimpleNamespace(match_info={"name": project}, query=query)
    return run(hive.manager.exportHandler(request)), sent


def test_the_route_sends_the_file_and_keeps_nothing(hive, monkeypatch):
    response, sent = callExport(hive, monkeypatch, format="fontra")
    assert response == "sent"
    assert sent["name"] == "Latin.fontra.zip"
    assert sent["data"][:2] == b"PK"
    leftovers = [p for p in hive.runs.iterdir() if p.is_dir()]
    assert leftovers == []  # no working folder, no artifact
    records = [p for p in hive.runs.iterdir() if p.suffix == ".json"]
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["builtin"] == "download" and record["state"] == "succeeded"
    assert record["inputs"][0]["project"] == "Latin"
    assert record["artifacts"][0]["name"] == "Latin.fontra.zip"


def test_the_route_names_a_branch_as_before(hive, monkeypatch):
    store = GitRepoStore.open(hive.root / "Latin.git")
    store.create_branch("light/one")
    store.close()
    _, sent = callExport(hive, monkeypatch, format="fontra", branch="light/one")
    assert sent["name"] == "Latin-light-one.fontra.zip"


def test_the_route_refuses_as_before(hive, monkeypatch):
    from aiohttp import web

    with pytest.raises(web.HTTPBadRequest):
        callExport(hive, monkeypatch, format="exe")
    with pytest.raises(web.HTTPNotFound):
        callExport(hive, monkeypatch, format="fontra", branch="nope")


def test_the_route_says_when_it_takes_too_long(hive, monkeypatch):
    from aiohttp import web

    from fontra_hive import projectmanager

    monkeypatch.setattr(
        projectmanager.runner, "runRecipe", _withLimits(runner.runRecipe, 0.01)
    )
    with pytest.raises(web.HTTPGatewayTimeout):
        callExport(hive, monkeypatch, format="designspace")


def _withLimits(runRecipe, seconds):
    async def limited(*args, **kwargs):
        return await runRecipe(
            *args, limits=runner.RunLimits(timeSeconds=seconds), **kwargs
        )

    return limited


def test_the_route_passes_a_refusal_on(hive, monkeypatch):
    class Registry(runner.LocalRunRegistry):
        async def open(self, request):
            raise runner.RunRefused(429, "Too many downloads.")

    monkeypatch.setattr(
        type(hive.manager), "recipeRuns", property(lambda self: Registry(self.runsRoot))
    )
    response, sent = callExport(hive, monkeypatch, format="fontra")
    assert (response.status, response.text) == (429, "Too many downloads.")
    assert sent == {}


# --- where files live, and for how long -----------------------------------------------


def latinHead(hive):
    store = GitRepoStore.open(hive.root / "Latin.git")
    try:
        return store.head("main")
    finally:
        store.close()


ARTIFACT_RECIPE = (
    "steps:\n- input: hive-read\n  project: Latin\n"
    "- output: hive-artifact\n  format: fontra\n"
)


def test_the_runs_folder_is_out_of_the_repositories(hive, tmp_path, monkeypatch):
    assert hive.runs.parent == hive.root.parent
    monkeypatch.setenv("HIVE_RUNS_DIR", str(tmp_path / "elsewhere"))
    manager = DevHiveProjectManager(hive.root)
    assert manager.runsRoot == tmp_path / "elsewhere"


def test_nothing_of_a_run_lands_among_the_repositories(hive):
    before = sorted(p.name for p in hive.root.iterdir())
    download(hive, "Latin", "Latin", "fontra").discard()
    assert sorted(p.name for p in hive.root.iterdir()) == before


def test_a_download_is_kept_nowhere_once_sent(hive, monkeypatch):
    response, sent = callExport(hive, monkeypatch, format="fontra")
    assert sent["data"][:2] == b"PK"
    assert [p for p in hive.runs.iterdir() if p.is_dir()] == []
    records = list(hive.runs.glob("*.json"))
    assert json.loads(records[0].read_text())["state"] == "succeeded"


def test_a_download_not_sent_leaves_nothing_after_a_restart(hive):
    """The server dies between the build and the sending."""
    result = download(hive, "Latin", "Latin", "fontra")
    assert result.directory.name == f"send-{result.id}"
    assert runner.removeWorkFolders(hive.runs) == [result.directory.name]
    assert not result.directory.exists()


def test_the_paths_of_the_repositories_are_scrubbed(hive):
    class Resolver:
        async def resolve(self, ref):
            raise RuntimeError(f"cannot read {hive.root}/Latin.git")

    result = run(
        runner.runRecipe(
            runner.downloadRequest("Latin", latinHead(hive), "Latin", "fontra"),
            resolver=Resolver(),
            registry=hive.manager.recipeRuns,
            runsRoot=hive.runs,
            serverPaths=[hive.root],
        )
    )
    assert "Latin.git" in result.message
    assert str(hive.root) not in result.message + result.log


class SweptRegistry(runner.LocalRunRegistry):
    """A registry that lists runs, as hive-api does."""

    def __init__(self, runsRoot, expired=(), interrupted=()):
        super().__init__(runsRoot)
        self.toExpire = list(expired)
        self.toInterrupt = list(interrupted)
        self.marked = []
        self.before = None

    async def expired(self):
        return list(self.toExpire)

    async def markExpired(self, runs):
        self.marked += runs

    async def interrupted(self, before):
        self.before = before
        return list(self.toInterrupt)


def kept(hive, recipe=ARTIFACT_RECIPE):
    addRecipe(hive, recipe)
    result = start(hive)
    assert result.succeeded, result.message
    return result


@needsValidation
def test_expired_runs_lose_their_files_and_stay_recorded(hive):
    old, recent = kept(hive), kept(hive)
    registry = SweptRegistry(hive.runs, expired=[old.id])
    assert run(runner.sweepExpiredRuns(registry, hive.runs)) == [old.id]
    assert not old.directory.exists()
    assert recent.directory.exists()
    assert registry.marked == [old.id]
    assert recorded(hive, old)["state"] == "succeeded"  # the local registry


def test_a_sweep_refuses_what_is_not_a_run(hive):
    hive.runs.mkdir()
    precious = hive.runs / "precious"
    precious.mkdir()
    registry = SweptRegistry(hive.runs, expired=["../repos", "precious", 3, ""])
    assert run(runner.sweepExpiredRuns(registry, hive.runs)) == []
    assert precious.exists() and hive.root.exists()
    assert registry.marked == []


def test_a_sweep_with_nothing_to_do_says_nothing(hive):
    registry = SweptRegistry(hive.runs)
    assert run(runner.sweepExpiredRuns(registry, hive.runs)) == []
    assert registry.marked == []


def test_a_run_already_without_files_is_marked_all_the_same(hive):
    registry = SweptRegistry(hive.runs, expired=[RUN_ID])
    assert run(runner.sweepExpiredRuns(registry, hive.runs)) == [RUN_ID]
    assert registry.marked == [RUN_ID]


def test_files_that_cannot_be_erased_stay_listed(hive, monkeypatch):
    (hive.runs / RUN_ID).mkdir(parents=True)
    monkeypatch.setattr(runner.shutil, "rmtree", lambda *a, **k: None)
    registry = SweptRegistry(hive.runs, expired=[RUN_ID])
    assert run(runner.sweepExpiredRuns(registry, hive.runs)) == []
    assert registry.marked == []


@needsValidation
def test_every_run_sweeps_when_it_ends(hive):
    old = kept(hive)
    swept = SweptRegistry(hive.runs, expired=[old.id])

    async def go():
        resolver = projectResolverFor(hive.manager, token_for("jeremie"))
        return await runner.runRecipe(
            runner.downloadRequest("Latin", latinHead(hive), "Latin", "fontra"),
            resolver=resolver,
            registry=swept,
            runsRoot=hive.runs,
        )

    result = run(go())
    assert result.succeeded
    assert not old.directory.exists()
    assert swept.marked == [old.id]


def test_a_sweep_that_fails_after_a_run_changes_nothing(hive):
    class Registry(runner.LocalRunRegistry):
        async def expired(self):
            raise HiveApiUnavailable("down")

    result = run(
        runner.runRecipe(
            runner.downloadRequest("Latin", latinHead(hive), "Latin", "fontra"),
            resolver=projectResolverFor(hive.manager, token_for("jeremie")),
            registry=Registry(hive.runs),
            runsRoot=hive.runs,
        )
    )
    assert result.succeeded and (result.directory / "Latin.fontra.zip").exists()


@needsValidation
def test_a_failed_run_keeps_its_log_and_no_file(hive):
    addRecipe(
        hive,
        "steps:\n- input: hive-read\n  project: Latin\n"
        "- output: hive-artifact\n  format: fontra\n  name: one\n"
        "- output: hive-artifact\n  format: fontra\n  name: two\n",
    )
    both = start(hive)
    limit = both.artifacts[0]["size"] * 3 // 2
    result = start(hive, limits=runner.RunLimits(outputBytes=limit))
    assert result.state == "too-large"
    assert result.directory is None and result.artifacts == []
    assert not (hive.runs / result.id).exists()
    assert recorded(hive, result)["message"] == result.message


def test_a_stopped_server_leaves_no_working_folder(hive):
    hive.runs.mkdir()
    (hive.runs / "work-abc" / "tmp").mkdir(parents=True)
    (hive.runs / "send-abc").mkdir()
    (hive.runs / RUN_ID).mkdir()
    assert runner.removeWorkFolders(hive.runs) == ["send-abc", "work-abc"]
    assert not (hive.runs / "work-abc").exists()
    assert (hive.runs / RUN_ID).exists()  # a run's files are the registry's


def test_no_runs_folder_yet(tmp_path):
    assert runner.removeWorkFolders(tmp_path / "none") == []


def test_interrupted_runs_lose_what_they_set_aside(hive):
    (hive.runs / RUN_ID).mkdir(parents=True)
    registry = SweptRegistry(hive.runs, interrupted=[RUN_ID])
    before = "2026-11-06T08:00:00+00:00"
    assert run(runner.sweepInterruptedRuns(registry, hive.runs, before)) == [RUN_ID]
    assert registry.before == before
    assert not (hive.runs / RUN_ID).exists()


def test_the_local_registry_closes_what_a_stopped_server_left(hive):
    registry = hive.manager.recipeRuns
    request = runner.downloadRequest("Latin", "a" * 40, "Latin", "fontra")
    left = run(registry.open(request)).id
    after = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    earlier = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    assert run(registry.interrupted(earlier.isoformat())) == []
    assert registry.get(left)["state"] == "running"
    assert run(registry.interrupted(after.isoformat())) == [left]
    assert registry.get(left)["state"] == "interrupted"
    assert registry.get(left)["message"] == runner.INTERRUPTED_MESSAGE
    assert run(registry.interrupted(after.isoformat())) == []


def test_hive_api_says_which_runs_expire():
    class Api:
        async def recipeRunsExpired(self):
            return [RUN_ID]

        async def recipeRunsErased(self, runs):
            self.erased = runs

        async def recipeRunsInterrupted(self, before):
            self.before = before
            return [RUN_ID]

    api = Api()
    registry = runner.HiveApiRunRegistry(api)
    assert run(registry.expired()) == [RUN_ID]
    run(registry.markExpired([RUN_ID]))
    assert api.erased == [RUN_ID]
    assert run(registry.interrupted("now")) == [RUN_ID]
    assert api.before == "now"


def test_a_sweep_retries_the_interrupted_runs_while_the_api_is_down(hive, monkeypatch):
    calls = []

    class Registry(runner.LocalRunRegistry):
        async def interrupted(self, before):
            calls.append(before)
            if len(calls) == 1:
                raise HiveApiUnavailable("down")
            return []

    monkeypatch.setattr(
        type(hive.manager), "recipeRuns", property(lambda self: Registry(self.runsRoot))
    )
    assert run(hive.manager.sweepRuns("t")) == "t"
    assert run(hive.manager.sweepRuns("t")) is None
    assert run(hive.manager.sweepRuns(None)) is None
    assert calls == ["t", "t"]


def test_the_server_starts_by_clearing_and_sweeps_every_quarter_of_an_hour(
    hive, monkeypatch
):
    assert runner.SWEEP_SECONDS == 15 * 60
    (hive.runs / "work-old").mkdir(parents=True)
    naps = []

    async def sleep(seconds):
        naps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def go():
        hive.manager.startRunSweeps()
        assert not (hive.runs / "work-old").exists()
        with pytest.raises(asyncio.CancelledError):
            await hive.manager._runSweepTask

    run(go())
    assert naps == [15 * 60]


def test_the_sweep_is_cancelled_with_the_server(hive):
    async def go():
        hive.manager.startRunSweeps()
        task = hive.manager._runSweepTask
        await asyncio.sleep(0)
        await hive.manager.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())


def test_the_client_of_the_routes_of_the_registry():
    from fontra_hive.hiveapi import HiveApi

    api = HiveApi("http://hive-api.invalid", "key")
    calls = []
    answer = (200, {"runs": [RUN_ID]})

    async def call(method, path, *, params=None, json=None):
        calls.append((method, path, json))
        return answer

    api._call = call
    assert run(api.recipeRunsExpired()) == [RUN_ID]
    run(api.recipeRunsErased([RUN_ID]))
    assert run(api.recipeRunsInterrupted("t")) == [RUN_ID]
    assert calls == [
        ("GET", "/api/internal/recipe-runs/expired", None),
        ("POST", "/api/internal/recipe-runs/expired", {"runs": [RUN_ID]}),
        ("POST", "/api/internal/recipe-runs/interrupted", {"before": "t"}),
    ]
    answer = (404, "<html>")
    for method, args in [
        (api.recipeRunsExpired, ()),
        (api.recipeRunsErased, ([RUN_ID],)),
        (api.recipeRunsInterrupted, ("t",)),
    ]:
        with pytest.raises(HiveApiUnavailable):
            run(method(*args))
