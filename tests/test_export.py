import pathlib
import zipfile
from types import SimpleNamespace

import pytest
from aiohttp import web
from fontTools.feaLib.error import IncludedFeaNotFound

from conftest import run
from fontra_hive import export, runner
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.projectmanager import DevHiveProjectManager
from fontra_hive.recipe import projectResolverFor

ME = Signature("Jérémie", "j@example.com")


def test_formats_without_a_font_compiler():
    formats = export.supported_formats()
    assert formats[:2] == ["fontra", "designspace"]
    assert ("otf" in formats) == export.can_build_fonts()
    assert export.download_name("Mutator", "designspace") == "Mutator.designspace.zip"
    assert export.download_name("Mutator", "otf") == "Mutator.otf"


def test_fontra_package_zipped(tmp_path, fixture_fontra):
    result = run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "fontra"))
    assert result.name == "Mutator.fontra.zip"
    names = zipfile.ZipFile(result).namelist()
    assert "Mutator.fontra/font-data.json" in names
    assert "Mutator.fontra/glyphs/A^1.json" in names


def test_designspace_with_its_ufos(tmp_path, fixture_fontra):
    result = run(
        export.build(fixture_fontra, tmp_path / "work", "Mutator", "designspace")
    )
    names = zipfile.ZipFile(result).namelist()
    assert "Mutator.designspace" in names
    ufos = {n.split("/")[0] for n in names if ".ufo/" in n}
    assert ufos and all(u.endswith(".ufo") for u in ufos)
    assert any(n.endswith("/glyphs/A_.glif") for n in names)


def write_features(package, text):
    (package / "features.txt").write_text(text, encoding="utf-8")


def fail_if_compiled(monkeypatch):
    def compile(*args, **kwargs):
        pytest.fail("fontmake was called")

    from fontra_compile.compile_fontmake_action import CompileFontMakeAction

    monkeypatch.setattr(CompileFontMakeAction, "compileFromDesignspace", compile)


@pytest.mark.parametrize(
    "features",
    [
        "include(/etc/hostname);",
        "include (/etc/hostname);",
        "include\t( /etc/hostname );",
        "include\n(\n/etc/hostname\n);",
        "include\r\n(/etc/hostname);",
        "INCLUDE(/etc/hostname);",
        'include("/etc/hostname");',
        "include('/etc/hostname');",
        "include # a word\n(/etc/hostname);",
        "feature liga {\n  include(/etc/hostname);\n} liga;",
        "# a word\ninclude(/etc/hostname);",
        'languagesystem DFLT dflt; "#" include(/etc/hostname);',
        "anon x {\n# word\n} x;\ninclude(/etc/hostname);",
        'anon x {\n"\n} x;\ninclude(/etc/hostname); # "',
        "include(~/hostname);",
        "include(~root/hostname);",
        "include(C:/hostname);",
        "include(c:\\hostname);",
        "include(\\\\server\\hostname);",
        "include(\\etc\\hostname);",
        "include(../hostname);",
        "include(a/../../hostname);",
        "include(a\\..\\hostname);",
        "include(/etc/hostname",
    ],
)
def test_includes_refused(features):
    with pytest.raises(export.FeaturesRefused) as error:
        export.check_feature_includes(features)
    assert str(error.value) == "Feature code can only include files from the project."


@pytest.mark.parametrize(
    "features",
    [
        "",
        "feature liga { sub f i by f_i; } liga;",
        "include(family.fea);",
        "include (shared/family.fea);",
        "include(./family.fea);",
        "include(a.b/c-d_e.fea);",
        "# include(/etc/hostname);\ninclude(family.fea);",
        "feature liga { # include(../hostname);\n} liga;",
        'name "include(/etc/hostname)";',
    ],
)
def test_includes_allowed(features):
    export.check_feature_includes(features)


def test_refused_before_compiling(tmp_path, fixture_fontra, monkeypatch):
    pytest.importorskip("fontra_compile")
    fail_if_compiled(monkeypatch)
    write_features(fixture_fontra, "include(/etc/hostname);\n")
    with pytest.raises(export.FeaturesRefused):
        run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))


def test_refused_by_the_build_process(tmp_path, fixture_fontra):
    pytest.importorskip("fontra_compile")
    if "ttf" not in export.supported_formats():
        pytest.skip("needs fontra-compile and fontmake")
    root = tmp_path / "repos"
    root.mkdir()
    write_features(fixture_fontra, "include(/etc/hostname);\n")
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    head = store.head("main")
    store.close()
    manager = DevHiveProjectManager(root)
    result = run(
        runner.runRecipe(
            runner.downloadRequest("Mutator", head, "Mutator", "ttf"),
            resolver=projectResolverFor(manager, "dev"),
            registry=manager.recipeRuns,
            runsRoot=manager.runsRoot,
        )
    )
    assert (result.state, result.reason) == (runner.INVALID, runner.REFUSED_FEATURES)
    assert result.message == export.FEATURES_REFUSED
    assert result.artifacts == [] and result.directory is None
    hostname = pathlib.Path("/etc/hostname")
    if hostname.is_file() and hostname.read_text().strip():
        word = hostname.read_text().strip()
        assert word not in result.message and word not in result.log


def failed(state, reason=""):
    log = 'Traceback\n  File x\nFeatureLibError: got NAME "word-of-the-log"\n'
    return runner.RunResult(
        "0b7c1e2a-1111-2222-3333-444455556666",
        state,
        message='got NAME "word-of-the-log"',
        log=log,
        reason=reason,
    )


def test_failure_messages():
    assert export.failure_message(failed(runner.FAILED)) == "The export failed."
    assert export.failure_message(failed(runner.TIMEOUT)) == export.EXPORT_TOO_LONG
    assert export.failure_message(failed(runner.FAILED, runner.STOPPED)) == (
        export.COMPILER_STOPPED
    )
    assert export.failure_message(
        failed(runner.INVALID, runner.REFUSED_FEATURES)
    ) == (export.FEATURES_REFUSED)
    for state in (runner.INVALID, runner.DENIED, runner.TOO_LARGE):
        assert export.failure_message(failed(state)) == export.EXPORT_FAILED


ANSWERS = {
    export.EXPORT_FAILED,
    export.EXPORT_TOO_LONG,
    export.COMPILER_STOPPED,
    export.FEATURES_REFUSED,
}


@pytest.fixture
def route(tmp_path, fixture_fontra, monkeypatch):
    """The export route on a project: ``route(format)`` gives its error."""
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fixture_fontra, message="Import", author=ME)
    store.close()
    manager = DevHiveProjectManager(root)

    async def project(request, name, capability):
        return root / "Mutator.git", None

    async def authorize(request):
        return "dev"

    manager._project = project
    manager.authorize = authorize

    def answer(format):
        request = SimpleNamespace(
            match_info={"name": "Mutator"}, query={"format": format}
        )
        with pytest.raises(web.HTTPException) as error:
            run(manager.exportHandler(request))
        return error.value

    answer.store = lambda: GitRepoStore.open(root / "Mutator.git")
    return answer


def stub_build(monkeypatch, result):
    async def runRecipe(request, **kwargs):
        return result

    monkeypatch.setattr(runner, "runRecipe", runRecipe)
    monkeypatch.setattr(export, "supported_formats", lambda: ["ttf"])


@pytest.mark.parametrize(
    "state, reason, status, text",
    [
        (runner.FAILED, "", 500, export.EXPORT_FAILED),
        (runner.TIMEOUT, "", 504, export.EXPORT_TOO_LONG),
        (runner.FAILED, runner.STOPPED, 500, export.COMPILER_STOPPED),
        (runner.INVALID, runner.REFUSED_FEATURES, 422, export.FEATURES_REFUSED),
        (runner.OUT_OF_MEMORY, "", 500, export.EXPORT_FAILED),
    ],
)
def test_a_failed_build_answers_from_our_list(
    route, caplog, monkeypatch, state, reason, status, text
):
    stub_build(monkeypatch, failed(state, reason))
    answer = route("ttf")
    assert (answer.status, answer.text) == (status, text)
    assert answer.text in ANSWERS
    assert "word-of-the-log" not in answer.text
    assert "word-of-the-log" in caplog.text  # the log stays on the server


def test_the_server_log_has_no_server_paths(route, caplog, monkeypatch, tmp_path):
    # The runner scrubs its log (tests/test_runner.py); the route logs that.
    log = runner._scrub(f"cannot read {tmp_path}/repos/x\n", [str(tmp_path)])
    stub_build(monkeypatch, runner.RunResult("x", runner.FAILED, log=log))
    with caplog.at_level("WARNING"):
        assert route("ttf").text == export.EXPORT_FAILED
    assert "cannot read repos/x" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_a_refused_font_through_the_route(route, tmp_path):
    pytest.importorskip("fontra_compile")
    if "ttf" not in export.supported_formats():
        pytest.skip("needs fontra-compile and fontmake")
    witness = tmp_path / "witness"
    witness.write_text("word-of-the-witness\n")
    store = route.store()
    store.commit(
        {"features.txt": f"include({witness});\n".encode()},
        message="Features",
        author=ME,
    )
    store.close()
    answer = route("ttf")
    assert answer.status == 422
    assert answer.text == export.FEATURES_REFUSED


def test_fontmake_works_in_a_folder_of_its_own(tmp_path, fixture_fontra, monkeypatch):
    pytest.importorskip("fontra_compile")
    from fontra_compile.compile_fontmake_action import CompileFontMakeAction

    seen = {}

    def compile(self, sourcePath, outputFontPath, extraArguments):
        seen["ufos"] = sourcePath.parent
        seen["cwd"] = pathlib.Path.cwd()
        outputFontPath.write_bytes(b"font")

    monkeypatch.setattr(CompileFontMakeAction, "compileFromDesignspace", compile)
    before = pathlib.Path.cwd()
    run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))
    folder = (tmp_path / "work" / "out" / "compile").resolve()
    assert seen == {"ufos": folder, "cwd": folder}
    assert pathlib.Path.cwd() == before


def test_a_relative_include_is_looked_for_in_that_folder(
    tmp_path, fixture_fontra, monkeypatch
):
    pytest.importorskip("fontra_compile")
    server = tmp_path / "server"
    server.mkdir()
    (server / "family.fea").write_text("feature kern { pos A B -10; } kern;\n")
    monkeypatch.chdir(server)
    write_features(fixture_fontra, "include(family.fea);\n")
    with pytest.raises(IncludedFeaNotFound):
        run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))


def test_font_without_include_builds_as_before(tmp_path, fixture_fontra):
    pytest.importorskip("fontra_compile")
    pytest.importorskip("fontmake")
    from fontTools.ttLib import TTFont

    write_features(fixture_fontra, "feature kern {\n  pos A B -10;\n} kern;\n")
    result = run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))
    assert result == tmp_path / "work" / "Mutator.ttf"
    assert "GPOS" in TTFont(result)


def test_sources_stay_byte_for_byte(tmp_path, fixture_fontra):
    result = run(export.build(fixture_fontra, tmp_path / "a", "Mutator", "fontra"))
    with zipfile.ZipFile(result) as archive:
        for path in fixture_fontra.rglob("*"):
            if path.is_file():
                name = path.relative_to(fixture_fontra).as_posix()
                assert archive.read(f"Mutator.fontra/{name}") == path.read_bytes()

    result = run(export.build(fixture_fontra, tmp_path / "b", "M", "designspace"))
    plain = tmp_path / "plain"
    plain.mkdir()
    run(export._copy(fixture_fontra, plain / "M.designspace"))
    with zipfile.ZipFile(result) as archive:
        assert archive.read("M.designspace") == (plain / "M.designspace").read_bytes()
        for ufo in plain.glob("*.ufo"):
            for path in ufo.rglob("*"):
                if path.is_file():
                    name = path.relative_to(plain).as_posix()
                    assert archive.read(name) == path.read_bytes()
