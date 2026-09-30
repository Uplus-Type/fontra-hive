import json

from fontra_hive.glyphdiff import changed_sources, font_source_names


def glyph(**layers):
    sources = [
        {"name": "Light", "layerName": "light"},
        {"name": "Bold", "layerName": "bold"},
    ]
    base = {"light": {"glyph": {"xAdvance": 500}}, "bold": {"glyph": {"xAdvance": 600}}}
    return {"name": "H", "sources": sources, "layers": {**base, **layers}}


def dump(data):
    return json.dumps(data).encode()


def test_changed_sources():
    old = glyph()
    assert changed_sources(dump(old), dump(old)) == []
    new = glyph(bold={"glyph": {"xAdvance": 620}})
    assert changed_sources(dump(old), dump(new)) == ["Bold"]
    both = glyph(light={"glyph": {"xAdvance": 1}}, bold={"glyph": {"xAdvance": 2}})
    assert changed_sources(dump(old), dump(both)) == ["Light", "Bold"]
    # A background layer no source uses: named by its layer.
    assert changed_sources(dump(old), dump(glyph(sketch={"glyph": {}}))) == ["sketch"]
    # A source renamed, added or removed.
    renamed = glyph()
    renamed["sources"][0] = {"name": "Thin", "layerName": "light"}
    assert changed_sources(dump(old), dump(renamed)) == ["Thin", "Light"]
    # New glyph, or unreadable: nothing to say.
    assert changed_sources(None, dump(old)) == []
    assert changed_sources(b"not json", dump(old)) == []
    assert changed_sources(b"{}", dump(old)) == []


def test_sources_based_on_font_sources_take_their_names():
    # As Fontra writes them: no name, the font source's id as base and layer.
    fontData = dump(
        {"sources": {"5bea": {"name": "LightCondensed"}, "f22d": {"name": "Bold"}}}
    )
    names = font_source_names(fontData)
    assert names == {"5bea": "LightCondensed", "f22d": "Bold"}
    sources = [
        {"name": "", "layerName": "5bea", "locationBase": "5bea"},
        {"name": "", "layerName": "f22d", "locationBase": "f22d"},
    ]
    old = {"sources": sources, "layers": {"5bea": {"glyph": {}}, "f22d": {"glyph": {}}}}
    new = {
        "sources": sources,
        "layers": {"5bea": {"glyph": {"xAdvance": 1}}, "f22d": {"glyph": {}}},
    }
    assert changed_sources(dump(old), dump(new), names) == ["LightCondensed"]
    assert changed_sources(dump(old), dump(new)) == ["5bea"]  # no font data: the layer
    assert font_source_names(b"nonsense") == {}
