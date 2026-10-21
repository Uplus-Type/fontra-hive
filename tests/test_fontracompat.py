"""Fontra reading OpenType features with an outside program (sr-aef, Fontra
main of 20 Oct. 2026): opening or importing a font must not depend on it."""

import subprocess
import sys

from fontra_hive import importer
from fontra_hive.fontracompat import guard_feature_reading

from test_importer import makeTTF  # noqa: E402


class FakeBackend:
    path = "Test.otf"

    def __init__(self, run):
        self.run = run

    def _getFeaturesFromFont(self) -> str:
        return self.run()


def test_features_read_when_the_program_works():
    class Backend(FakeBackend):
        pass

    assert guard_feature_reading(Backend)
    assert Backend(lambda: "feature kern {} kern;")._getFeaturesFromFont() == (
        "feature kern {} kern;"
    )


def test_no_features_when_the_program_is_missing_or_fails(caplog):
    class Backend(FakeBackend):
        pass

    guard_feature_reading(Backend)

    def missing():  # no sr-aef on the PATH
        return subprocess.run(
            ["hive-no-such-program-sr-aef"], capture_output=True, check=True
        ).stdout

    def failing():  # sr-aef cannot read the font
        return subprocess.run(
            [sys.executable, "-c", "raise SystemExit(2)"],
            capture_output=True,
            check=True,
        ).stdout

    def no_subprocess():  # Try Fontra: Python in the browser
        raise OSError("subprocess is not supported in Pyodide")

    for run in (missing, failing, no_subprocess):
        assert Backend(run)._getFeaturesFromFont() == ""
    assert "the font opens without them" in caplog.text


def test_guarding_twice_or_nothing_to_guard():
    class Backend(FakeBackend):
        pass

    assert guard_feature_reading(Backend)
    wrapped = Backend._getFeaturesFromFont
    assert not guard_feature_reading(Backend)  # already guarded
    assert Backend._getFeaturesFromFont is wrapped

    class Older:  # Fontra before 20 Oct. 2026: features not read
        pass

    assert not guard_feature_reading(Older)


def test_a_font_file_imports_without_sr_aef(tmp_path, monkeypatch):
    # Whichever Fontra is installed: with the program out of reach, the
    # import still gives a .fontra.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    ttf = tmp_path / "upload"
    makeTTF(ttf)
    work = tmp_path / "work"
    work.mkdir()
    font = importer.convertToFontra(ttf, "Test-Regular.ttf", work)
    assert (font / "font-data.json").exists()
