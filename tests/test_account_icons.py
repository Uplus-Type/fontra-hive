"""Every account page shows Hive's icon and links it for the browser."""

import pathlib
import re

ACCOUNT = (
    pathlib.Path(__file__).parent.parent / "src" / "fontra_hive" / "client" / "account"
)


def test_account_pages_link_hives_icon():
    pages = sorted(ACCOUNT.glob("*.html"))
    assert pages
    for page in pages:
        html = page.read_text(encoding="utf-8")
        assert 'rel="icon" href="/hive/icons/hive-icon.svg"' in html, page.name
        assert "fontra-icon.svg" not in html, page.name
        for src in re.findall(r'(?:src|href)="/hive/icons/([^"]+)"', html):
            assert (ACCOUNT.parent / "icons" / src).is_file(), (page.name, src)
