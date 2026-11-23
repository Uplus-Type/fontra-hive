"""fontra_hive.fontdiff: what changed beyond the glyph, in words."""

import copy
import json
import pathlib
import tempfile

import pytest

pytest.importorskip("fontra")

from conftest import FIXTURE  # noqa: E402
from fontra_hive import fontdiff  # noqa: E402

FONT_DATA = (FIXTURE / "font-data.json").read_bytes()
GLYPH_INFO = (FIXTURE / "glyph-info.csv").read_bytes()
LIGHT = "5bea6334"  # LightCondensed


def edited(change) -> bytes:
    data = json.loads(FONT_DATA)
    change(data)
    return json.dumps(data, indent=0).encode()


def summaries(old, new, path="font-data.json"):
    return [c.summary for c in fontdiff.font_changes({path: old}, {path: new})]


def kerning_csv(values, groups1=None, sources=(LIGHT,)) -> bytes:
    from fontra.backends.fontra import writeKerningFile
    from fontra.core.classes import Kerning

    kerning = {
        "hkern": Kerning(
            groupsSide1=groups1 or {},
            groupsSide2={},
            sourceIdentifiers=list(sources),
            values=values,
        )
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "kerning.csv"
        writeKerningFile(path, kerning)
        return path.read_bytes()


# --- one case per kind of change ------------------------------------------------------


def test_font_info_version_and_designer():
    old = edited(lambda d: d.setdefault("fontInfo", {}).update(versionMajor=1, versionMinor=1))
    new = edited(
        lambda d: d.setdefault("fontInfo", {}).update(
            versionMajor=1, versionMinor=2, designer="Ana"
        )
    )
    assert summaries(old, new) == ["Font info: version 1.001 → 1.002, designer set to Ana"]
    [change] = fontdiff.font_changes({"font-data.json": old}, {"font-data.json": new})
    assert change.area == "fontInfo" and change.domain == "fontsettings"
    assert [(f.label, f.before, f.after) for f in change.fields] == [
        ("version", "1.001", "1.002"),
        ("designer", None, "Ana"),
    ]


def test_units_per_em_is_font_info():
    new = edited(lambda d: d.update(unitsPerEm=2048))
    assert summaries(FONT_DATA, new) == ["Font info: units per em 1000 → 2048"]


def test_axis_range_default_and_added():
    def change(d):
        d["axes"]["axes"][1]["minValue"] = 200
        d["axes"]["axes"][0]["defaultValue"] = 500
        d["axes"]["axes"].append(
            {"name": "optical", "label": "Optical", "tag": "opsz", "minValue": 8,
             "defaultValue": 12, "maxValue": 72}
        )

    assert summaries(FONT_DATA, edited(change)) == [
        "Axes: opsz added, wdth default 0 → 500, wght 100–900 → 200–900"
    ]


def test_cross_axis_mapping_has_its_own_area():
    new = edited(
        lambda d: d["axes"].update(
            mappings=[{"inputLocation": {"weight": 900}, "outputLocation": {"width": 800}}]
        )
    )
    [change] = fontdiff.font_changes({"font-data.json": FONT_DATA}, {"font-data.json": new})
    assert change.area == "mappings"
    assert change.summary == "Cross-axis mapping: 1 mapping added"


def test_sources_added_removed_renamed_moved():
    def change(d):
        d["sources"]["black"] = {"name": "Black", "location": {"weight": 900, "width": 0}}
        del d["sources"]["d7abd222"]  # LightWide
        d["sources"][LIGHT]["name"] = "Thin"
        d["sources"][LIGHT]["location"]["weight"] = 100

    assert summaries(FONT_DATA, edited(change)) == [
        "Sources: Black added, LightWide removed, LightCondensed renamed Thin and 1 more"
    ]
    [change] = fontdiff.font_changes(
        {"font-data.json": FONT_DATA}, {"font-data.json": edited(change)}
    )
    assert ("Thin location", "wdth 0, wght 150", "wdth 0, wght 100") in [
        (f.label, f.before, f.after) for f in change.fields
    ]


def test_line_metrics_by_source():
    def change(d):
        metrics = d["sources"][LIGHT]["lineMetricsHorizontalLayout"]
        metrics["xHeight"]["value"] = 510
        metrics["capHeight"]["zone"] = 12

    changes = fontdiff.font_changes(
        {"font-data.json": FONT_DATA}, {"font-data.json": edited(change)}
    )
    assert [c.summary for c in changes] == [
        "Line metrics (LightCondensed): capHeight zone 16 → 12, xHeight 500 → 510"
    ]
    assert changes[0].area == "sources"
    assert fontdiff.title_words(changes) == "line metrics"


def test_font_guidelines_and_italic_angle():
    def change(d):
        d["sources"][LIGHT]["guidelines"] = [{"name": "overshoot", "y": -10}]
        d["sources"][LIGHT]["italicAngle"] = -12

    assert summaries(FONT_DATA, edited(change)) == [
        "Sources: LightCondensed italic angle 0 → -12",
        "Font guidelines (LightCondensed): 1 added",
    ]


def test_features_lines_added_and_removed():
    old = b"feature liga {\n  sub f i by fi;\n} liga;\n"
    new = b"feature liga {\n  sub f l by fl;\n  sub f f by ff;\n} liga;\n"
    [change] = fontdiff.font_changes({"features.txt": old}, {"features.txt": new})
    assert change.summary == "Features: 2 lines added, 1 removed"
    assert change.area == "features" and change.domain == "fontsettings"
    assert ("-", "  sub f i by fi;") in change.lines
    assert ("+", "  sub f f by ff;") in change.lines


def test_kerning_pairs_by_source_and_groups():
    old = kerning_csv({"A": {"V": [-50]}, "T": {"o": [-30]}})
    new = kerning_csv(
        {"A": {"V": [-60]}, "T": {"o": [-30]}, "V": {"A": [-40]}},
        groups1={"public.kern1.O": ["O", "Q"]},
    )
    changes = fontdiff.font_changes(
        {"font-data.json": FONT_DATA, "kerning.csv": old},
        {"font-data.json": FONT_DATA, "kerning.csv": new},
    )
    assert [c.summary for c in changes] == [
        "Kerning: 2 pairs changed (LightCondensed), 1 group changed"
    ]
    assert changes[0].domain == "kerning"
    assert ("A V (LightCondensed)", "-50", "-60") in [
        (f.label, f.before, f.after) for f in changes[0].fields
    ]


def test_status_definitions_and_plugin_data():
    def change(d):
        d["customData"] = {
            "fontra.sourceStatusFieldDefinitions": [{"label": "Approved", "value": 5}],
            "umarks.rules": {"a": 1},
            "fontra.projectGlyphSets": [{"name": "Latin"}],
            "com.example.note": "x",
        }

    changes = fontdiff.font_changes(
        {"font-data.json": FONT_DATA}, {"font-data.json": edited(change)}
    )
    assert [(c.area, c.domain, c.summary) for c in changes] == [
        ("statusDefinitions", "fontsettings", "Development status: Approved added"),
        ("customData", "fontsettings", "Font custom data: com.example.note set to x"),
        (
            "pluginData",
            "plugindata",
            "Plug-in data: project glyph sets added, umarks.rules added",
        ),
    ]
    assert fontdiff.domains_of(changes) == ["fontsettings", "plugindata"]


def test_glyph_info_code_points_and_new_glyphs():
    new = GLYPH_INFO.replace(b"A;U+0041,U+0061", b"A;U+0041,U+00C0") + b"Aring;U+00C5\r\n"
    changes = fontdiff.font_changes({"glyph-info.csv": GLYPH_INFO}, {"glyph-info.csv": new})
    assert [(c.area, c.domain) for c in changes] == [("glyphInfo", "edit")]
    assert changes[0].summary == "Glyph info: Aring added; A U+0041 U+0061 → U+0041 U+00C0"


# --- edges ---------------------------------------------------------------------------


def test_a_font_without_axes():
    def no_axes(d):
        d["axes"] = {"axes": []}
        d["sources"] = {"only": {"name": "Regular"}}

    old = edited(no_axes)

    def renamed(d):
        no_axes(d)
        d["sources"]["only"]["name"] = "Book"
        d["sources"]["only"]["lineMetricsHorizontalLayout"] = {"xHeight": {"value": 480}}

    assert summaries(old, edited(renamed)) == [
        "Sources: Regular renamed Book",
        "Line metrics (Book): xHeight added",
    ]
    # Adding the first axis.
    def first_axis(d):
        no_axes(d)
        d["axes"]["axes"] = [
            {"name": "weight", "label": "Weight", "tag": "wght", "minValue": 100,
             "defaultValue": 400, "maxValue": 900}
        ]

    assert summaries(old, edited(first_axis)) == ["Axes: wght added"]


def test_a_file_absent_on_one_side():
    features = b"languagesystem DFLT dflt;\n"
    assert summaries(None, features, "features.txt") == ["Features: 1 line added"]
    assert summaries(features, None, "features.txt") == ["Features: 1 line removed"]
    kerning = kerning_csv({"A": {"V": [-50]}})
    assert summaries(None, kerning, "kerning.csv") == ["Kerning: 1 pair changed (5bea6334)"]
    # A font-data.json that appears: everything in it is new.
    added = fontdiff.font_changes({}, {"font-data.json": FONT_DATA})
    assert added[0].summary.startswith("Axes: wdth added, wght added")


def test_identical_features_say_nothing():
    text = b"feature kern {\n} kern;\n"
    assert fontdiff.font_changes({"features.txt": text}, {"features.txt": text}) == []
    # Even when only the line endings differ.
    assert summaries(text, text.replace(b"\n", b"\r\n"), "features.txt") == []


def test_an_unreadable_file_is_said_not_raised():
    [change] = fontdiff.font_changes(
        {"font-data.json": FONT_DATA}, {"font-data.json": b"{not json"}
    )
    assert change.summary == "Font settings: changed (the file could not be read)"


def test_the_detail_is_json():
    new = edited(lambda d: d["axes"]["axes"][1].update(maxValue=1000))
    [change] = fontdiff.font_changes({"font-data.json": FONT_DATA}, {"font-data.json": new})
    data = change.to_json()
    assert data["summary"] == "Axes: wght 100–900 → 100–1000"
    assert data["fields"] == [{"label": "wght range", "before": "100–900", "after": "100–1000"}]
    assert change.to_json(detail=False) == {
        "area": "axes",
        "domain": "fontsettings",
        "summary": "Axes: wght 100–900 → 100–1000",
    }
    json.dumps(data)


def test_every_font_key_has_an_area():
    """A key Fontra adds to the font is noticed here (like writedomains)."""
    import dataclasses

    from fontra.core.classes import Font

    described = {
        "unitsPerEm", "fontInfo", "axes", "sources", "features",
        "conditionalSubstitutions", "customData", "kerning", "glyphInfos",
        "glyphs", "glyphMap",
    }
    assert {f.name for f in dataclasses.fields(Font)} == described
    assert copy.deepcopy(fontdiff.AREAS)["kerning"][0] == "kerning"
