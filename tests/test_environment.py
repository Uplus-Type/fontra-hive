"""A test deployment marks its pages (HIVE_ENVIRONMENT); production does not."""

from fontra_hive.environment import BADGE_ID, markEnvironment

PAGE = (
    "<!doctype html><html><head><TITLE>Fontra</TITLE></head>"
    "<body><p>x</p></BODY></html>"
)


def test_production_pages_are_left_alone(monkeypatch):
    monkeypatch.delenv("HIVE_ENVIRONMENT", raising=False)
    assert markEnvironment(PAGE) == PAGE
    monkeypatch.setenv("HIVE_ENVIRONMENT", "  ")
    assert markEnvironment(PAGE) == PAGE


def test_development_pages_get_a_badge_and_a_title_prefix(monkeypatch):
    monkeypatch.setenv("HIVE_ENVIRONMENT", "development")
    page = markEnvironment(PAGE)
    assert "<TITLE>[dev] Fontra</TITLE>" in page
    assert page.index(BADGE_ID) > page.index("<p>x</p>")
    assert page.endswith("</BODY></html>")
    assert ">development</div>" in page
    # Never in the way: no clicks, above everything.
    assert "pointer-events:none" in page


def test_a_fragment_and_an_odd_name(monkeypatch):
    monkeypatch.delenv("HIVE_ENVIRONMENT", raising=False)
    page = markEnvironment("<p>fragment</p>", name="<staging>")
    assert page.startswith("<p>fragment</p>")
    assert "&lt;staging&gt;" in page and "<staging>" not in page
