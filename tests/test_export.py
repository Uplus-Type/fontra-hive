import json
import logging
import pathlib
import sys
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
    assert export.download_name("Mutator", "designspace") == "Mutator-designspace.zip"
    assert export.download_name("Mutator", "designspace", ufo_only=True) == (
        "Mutator-ufo.zip"
    )
    assert export.download_name("Mutator", "fontra") == "Mutator.fontra.zip"
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
    monkeypatch.setattr(export, "can_run_fontc", lambda: None)  # fontmake's way
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
    monkeypatch.setattr(export, "can_run_fontc", lambda: None)  # fontmake's way
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


# --- fontc, the first engine of ttf and woff2 -----------------------------------------

FONTC_SCRIPT = """#!PYTHON
import json, os, pathlib, sys
args = sys.argv[1:]
with open("CALLS", "a") as file:
    call = {"args": args, "cwd": str(pathlib.Path.cwd()), "env": sorted(os.environ)}
    file.write(json.dumps(call) + "\\n")
mode = "MODE"
if mode == "panic":
    sys.stderr.write("thread 'main' panicked at fontc/src/lib.rs:1:1:\\n")
    sys.stderr.write("not yet implemented: " + args[0] + "\\n")
    sys.exit(101)
if mode == "error":
    print("x" * 5000)
    print("ERROR failed to deserialize designspace: missing field `location`")
    sys.stderr.write("y" * 5000 + "STDERR-END")
    sys.exit(1)
output = pathlib.Path(args[args.index("--output-file") + 1])
if mode == "empty":
    output.write_bytes(b"")
    sys.exit(0)
if mode == "nothing":
    sys.exit(0)
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
builder = FontBuilder(1000, isTTF=True)
builder.setupGlyphOrder([".notdef"])
builder.setupCharacterMap({})
builder.setupGlyf({".notdef": TTGlyphPen(None).glyph()})
builder.setupHorizontalMetrics({".notdef": (500, 0)})
builder.setupHorizontalHeader(ascent=800, descent=-200)
builder.setupNameTable({"familyName": "Double", "styleName": "Regular"})
builder.setupOS2()
builder.setupPost()
builder.save(output)
"""


def a_ttf(path):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef"])
    builder.setupCharacterMap({})
    builder.setupGlyf({".notdef": TTGlyphPen(None).glyph()})
    builder.setupHorizontalMetrics({".notdef": (500, 0)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Double", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    builder.save(path)


def fake_fontc(tmp_path, monkeypatch, mode="ok"):
    """A program called ``fontc`` that writes a small TrueType font (or fails as
    ``mode`` says: panic, error, empty, nothing). Returns the file where each
    call is written, one JSON line."""
    calls = tmp_path / "fontc-calls.jsonl"
    script = tmp_path / "bin" / "fontc"
    script.parent.mkdir(exist_ok=True)
    script.write_text(
        FONTC_SCRIPT.replace("PYTHON", sys.executable)
        .replace("CALLS", calls.as_posix())
        .replace("MODE", mode)
    )
    script.chmod(0o755)
    monkeypatch.setattr(export, "can_run_fontc", lambda: script)
    return calls


def called(calls):
    if not calls.exists():
        return []
    return [json.loads(line) for line in calls.read_text().splitlines()]


@pytest.fixture
def doubles(monkeypatch):
    """The sources and fontmake, as doubles: what is called, in order."""
    seen = []

    async def prepare(source, out):
        seen.append("prepare")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{source.stem}.designspace"
        path.write_text("<designspace/>")
        return path

    async def compile(source, destination):
        seen.append("fontmake")
        if destination.suffix == ".ttf":
            a_ttf(destination)
        else:
            destination.write_bytes(b"OTTO")

    monkeypatch.setattr(export, "_prepare_sources", prepare)
    monkeypatch.setattr(export, "_compile", compile)
    return seen


def engine_line(caplog):
    return [r.getMessage() for r in caplog.records if "engine=" in r.getMessage()]


def test_fontc_builds_the_ttf(tmp_path, fixture_fontra, monkeypatch, doubles, caplog):
    from fontTools.ttLib import TTFont

    calls = fake_fontc(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="fontra_hive.export"):
        result = run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))
    assert result == tmp_path / "work" / "Mutator.ttf"
    assert "glyf" in TTFont(result)
    assert doubles == ["prepare"]  # fontmake was not called
    (call,) = called(calls)
    source, flag, output = call["args"]
    assert source.endswith("source.designspace") and flag == "--output-file"
    assert pathlib.Path(output).name == "Mutator.ttf"
    assert pathlib.Path(call["cwd"]).resolve() == pathlib.Path(source).parent.resolve()
    assert len(engine_line(caplog)) == 1
    assert engine_line(caplog)[0].startswith("engine=fontc format=ttf seconds=")
    assert engine_line(caplog)[0].endswith("fallback=no")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    "mode, said",
    [
        ("panic", "panicked at fontc/src/lib.rs"),
        ("error", "missing field `location`"),
        ("empty", "exit code 0"),
        ("nothing", "exit code 0"),
    ],
)
def test_fontc_failing_falls_back_to_fontmake(
    tmp_path, fixture_fontra, monkeypatch, doubles, caplog, mode, said
):
    from fontTools.ttLib import TTFont

    calls = fake_fontc(tmp_path, monkeypatch, mode)
    with caplog.at_level(logging.INFO, logger="fontra_hive.export"):
        result = run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))
    assert "glyf" in TTFont(result)  # made by the double of fontmake
    assert doubles == ["prepare", "fontmake"]
    assert len(called(calls)) == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and said in warnings[0].getMessage()
    assert "fontmake takes over" in warnings[0].getMessage()
    assert str(tmp_path) not in caplog.text  # no path of the server in the log
    assert engine_line(caplog)[0].startswith("engine=fontmake format=ttf seconds=")
    assert engine_line(caplog)[0].endswith("fallback=yes")


def test_fontc_output_is_kept_at_its_end(tmp_path, fixture_fontra, monkeypatch):
    fake_fontc(tmp_path, monkeypatch, "error")
    designspace = tmp_path / "sources" / "x.designspace"
    designspace.parent.mkdir()
    designspace.write_text("<designspace/>")
    with pytest.raises(export.FontcFailed) as error:
        run(export._compile_fontc(designspace, tmp_path / "out" / "x.ttf"))
    failure = error.value
    assert failure.returncode == 1
    # stdout, where fontc prints its errors, and stderr, where it panics
    assert len(failure.stdout) <= export.FONTC_TAIL_CHARACTERS
    assert failure.stdout.endswith("missing field `location`")
    assert failure.stderr.endswith("STDERR-END")
    assert len(failure.stderr) <= export.FONTC_TAIL_CHARACTERS
    assert len(failure.tail) <= export.FONTC_TAIL_CHARACTERS + 1
    assert "missing field `location`" in failure.tail  # stdout is not lost
    assert failure.tail.endswith("STDERR-END")
    assert str(tmp_path) not in failure.tail


def test_a_panic_is_an_exit_code_like_any_other(tmp_path, monkeypatch):
    fake_fontc(tmp_path, monkeypatch, "panic")
    designspace = tmp_path / "sources" / "x.designspace"
    designspace.parent.mkdir()
    designspace.write_text("<designspace/>")
    with pytest.raises(export.FontcFailed) as error:
        run(export._compile_fontc(designspace, tmp_path / "x.ttf"))
    assert error.value.returncode == 101
    assert "panicked" in error.value.stderr
    assert str(tmp_path) not in error.value.stderr


def test_fontc_gets_a_short_environment(tmp_path, monkeypatch):
    calls = fake_fontc(tmp_path, monkeypatch)
    monkeypatch.setenv("HIVE_SERVICE_KEY", "s3cret")
    monkeypatch.setenv("AWS_SECRET", "s3cret")
    designspace = tmp_path / "sources" / "x.designspace"
    designspace.parent.mkdir()
    designspace.write_text("<designspace/>")
    run(export._compile_fontc(designspace, tmp_path / "x.ttf"))
    (call,) = called(calls)
    assert "HIVE_SERVICE_KEY" not in call["env"] and "AWS_SECRET" not in call["env"]


def test_otf_is_fontmakes_alone(tmp_path, fixture_fontra, monkeypatch, doubles, caplog):
    calls = fake_fontc(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="fontra_hive.export"):
        result = run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "otf"))
    assert result.read_bytes() == b"OTTO"
    assert doubles == ["fontmake"]  # no sources made for fontc
    assert called(calls) == []
    assert engine_line(caplog)[0].startswith("engine=fontmake format=otf seconds=")
    assert engine_line(caplog)[0].endswith("fallback=no")


def test_woff2_is_made_from_the_ttf_of_fontc(
    tmp_path, fixture_fontra, monkeypatch, doubles, caplog
):
    pytest.importorskip("brotli")
    from fontTools.ttLib import TTFont

    calls = fake_fontc(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="fontra_hive.export"):
        result = run(
            export.build(fixture_fontra, tmp_path / "work", "Mutator", "woff2")
        )
    assert result == tmp_path / "work" / "Mutator.woff2"
    assert result.read_bytes()[:4] == b"wOF2"
    assert TTFont(result).flavor == "woff2"
    assert doubles == ["prepare"] and len(called(calls)) == 1
    assert engine_line(caplog)[0].startswith("engine=fontc format=woff2 seconds=")


def test_without_fontc_it_is_fontmake_as_before(
    tmp_path, fixture_fontra, monkeypatch, doubles, caplog
):
    from fontTools.ttLib import TTFont

    monkeypatch.setattr(export, "can_run_fontc", lambda: None)
    with caplog.at_level(logging.INFO, logger="fontra_hive.export"):
        result = run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))
    assert "glyf" in TTFont(result)
    assert doubles == ["fontmake"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert engine_line(caplog)[0].endswith("fallback=no")
    assert engine_line(caplog)[0].startswith("engine=fontmake format=ttf")


def test_both_engines_failing_gives_the_error_of_fontmake(
    tmp_path, fixture_fontra, monkeypatch, doubles
):
    fake_fontc(tmp_path, monkeypatch, "panic")

    async def compile(source, destination):
        raise RuntimeError("fontmake said no")

    monkeypatch.setattr(export, "_compile", compile)
    with pytest.raises(RuntimeError, match="fontmake said no"):
        run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))


def test_refused_features_do_not_fall_back(tmp_path, fixture_fontra, monkeypatch):
    fake_fontc(tmp_path, monkeypatch)
    seen = []

    async def prepare(source, out):
        raise export.FeaturesRefused(export.FEATURES_REFUSED)

    async def compile(source, destination):
        seen.append("fontmake")

    monkeypatch.setattr(export, "_prepare_sources", prepare)
    monkeypatch.setattr(export, "_compile", compile)
    with pytest.raises(export.FeaturesRefused):
        run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))
    assert seen == []


def test_fontc_is_looked_for_beside_the_interpreter_then_on_the_path(
    tmp_path, monkeypatch
):
    beside = tmp_path / "venv" / "bin"
    beside.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    for folder in (beside, other):
        (folder / "fontc").write_text("#!/bin/sh\n")
        (folder / "fontc").chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(beside / "python"))
    monkeypatch.setenv("PATH", str(other))
    assert export.can_run_fontc() == beside / "fontc"
    (beside / "fontc").unlink()
    assert export.can_run_fontc() == other / "fontc"
    (other / "fontc").unlink()
    assert export.can_run_fontc() is None


def test_fontc_does_not_change_the_formats_offered(monkeypatch):
    before = export.supported_formats()
    monkeypatch.setattr(export, "can_run_fontc", lambda: None)
    assert export.supported_formats() == before


def test_rules_naming_a_missing_glyph_are_left_out(tmp_path):
    import plistlib

    from fontTools.designspaceLib import DesignSpaceDocument, RuleDescriptor

    doc = DesignSpaceDocument()
    doc.addSourceDescriptor(filename="a.ufo", location={})
    glyphs = tmp_path / "a.ufo" / "glyphs"
    glyphs.mkdir(parents=True)
    (glyphs / "contents.plist").write_bytes(
        plistlib.dumps({"I": "I_.glif", "I.narrow": "I_.narrow.glif", "A": "A_.glif"})
    )
    kept = RuleDescriptor(name="kept", subs=[("I", "I.narrow")])
    mixed = RuleDescriptor(name="mixed", subs=[("I", "I.narrow"), ("A", "A.alt")])
    gone = RuleDescriptor(name="gone", subs=[("B", "B.alt")])
    doc.rules.extend([kept, mixed, gone])
    export.keep_rules_with_glyphs(doc, tmp_path)
    assert [r.name for r in doc.rules] == ["kept", "mixed"]
    assert doc.rules[1].subs == [("I", "I.narrow")]


# --- the font without axes: its UFO alone -----------------------------------------------


def test_sources_of_a_font_without_axes_are_its_ufo_alone(
    tmp_path, fontra_without_axes
):
    result = run(
        export.build(fontra_without_axes, tmp_path / "work", "Plain", "designspace")
    )
    assert result.name == "Plain-ufo.zip"
    names = zipfile.ZipFile(result).namelist()
    assert not any(name.endswith(".designspace") for name in names)
    assert len({name.split("/")[0] for name in names}) == 1
    assert names[0].split("/")[0].endswith(".ufo")
    # the UFO that the designspace export writes, file for file
    plain = tmp_path / "plain"
    plain.mkdir()
    run(export._copy(fontra_without_axes, plain / "Plain.designspace"))
    (ufo,) = plain.glob("*.ufo")
    expected = {
        path.relative_to(plain).as_posix(): path.read_bytes()
        for path in sorted(ufo.rglob("*"))
        if path.is_file()
    }
    with zipfile.ZipFile(result) as archive:
        assert {name: archive.read(name) for name in names} == expected


def test_sources_with_axes_are_named_designspace(tmp_path, fixture_fontra):
    result = run(
        export.build(fixture_fontra, tmp_path / "work", "Mutator", "designspace")
    )
    assert result.name == "Mutator-designspace.zip"


def test_fontc_is_given_the_ufo_of_a_font_without_axes(
    tmp_path, fontra_without_axes
):
    sources = run(export._prepare_sources(fontra_without_axes, tmp_path / "fontc"))
    assert sources.suffix == ".ufo" and sources.is_dir()
    assert (sources / "glyphs" / "contents.plist").is_file()


def test_fontc_is_given_the_designspace_of_a_font_with_axes(tmp_path, fixture_fontra):
    pytest.importorskip("fontra")
    from fontTools.designspaceLib import DesignSpaceDocument

    sources = run(export._prepare_sources(fixture_fontra, tmp_path / "fontc"))
    assert sources.suffix == ".designspace"
    doc = DesignSpaceDocument.fromfile(sources)
    assert [axis.name for axis in doc.axes] == ["width", "weight"]
    assert len(doc.sources) == 5
    assert all((sources.parent / source.filename).is_dir() for source in doc.sources)


def test_features_are_checked_before_fontc_sees_them(tmp_path, fixture_fontra):
    pytest.importorskip("fontra")
    write_features(fixture_fontra, "include(/etc/hostname);\n")
    with pytest.raises(export.FeaturesRefused):
        run(export._prepare_sources(fixture_fontra, tmp_path / "fontc"))


def test_the_real_sources_reach_fontc(tmp_path, fontra_without_axes, monkeypatch):
    from fontTools.ttLib import TTFont

    calls = fake_fontc(tmp_path, monkeypatch)
    result = run(export.build(fontra_without_axes, tmp_path / "work", "Plain", "ttf"))
    assert "glyf" in TTFont(result)
    (call,) = called(calls)
    assert call["args"][0].endswith(".ufo")


# --- a real build, where fontc and fontmake are installed --------------------------------


def test_the_real_fontc_builds_mutatorsans(tmp_path, fixture_fontra, caplog):
    pytest.importorskip("fontra_compile")
    pytest.importorskip("fontmake")
    if export.can_run_fontc() is None:
        pytest.skip("needs fontc")
    from fontTools.ttLib import TTFont

    with caplog.at_level(logging.INFO, logger="fontra_hive.export"):
        result = run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "ttf"))
    font = TTFont(result)
    assert "glyf" in font and "fvar" in font
    assert engine_line(caplog)[0].startswith("engine=fontc format=ttf")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_real_otf_comes_from_fontmake(tmp_path, fixture_fontra, caplog):
    pytest.importorskip("fontra_compile")
    pytest.importorskip("fontmake")
    if export.can_run_fontc() is None:
        pytest.skip("needs fontc")
    with caplog.at_level(logging.INFO, logger="fontra_hive.export"):
        result = run(export.build(fixture_fontra, tmp_path / "work", "Mutator", "otf"))
    assert result.read_bytes()[:4] == b"OTTO"
    assert engine_line(caplog)[0].startswith("engine=fontmake format=otf")
