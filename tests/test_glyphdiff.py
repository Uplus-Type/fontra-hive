import json

from fontra_hive.glyphdiff import changed_sources


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
