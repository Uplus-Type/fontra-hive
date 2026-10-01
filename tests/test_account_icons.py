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


def test_sign_in_and_home_link_the_legal_pages():
    # /legal and /terms are served by the deployment (hive-api/deploy/site,
    # through Caddy), not by this plug-in: they are U+ TYPE's, not the plug-in's.
    for page in ("login.html", "home.html", "invitation.html"):
        html = (ACCOUNT / page).read_text(encoding="utf-8")
        assert 'href="/terms' in html and 'href="/legal' in html, page
    for page in ("login.html", "home.html"):
        assert 'href="/pricing"' in (ACCOUNT / page).read_text(encoding="utf-8"), page


def test_the_sign_in_page_is_the_plugins_own():
    # fontrahive.com's landing (features, screenshots) is U+ TYPE's, in
    # hive-api (deploy/site/landing.html), shown through $HIVE_LANDING_FILE.
    html = (ACCOUNT / "login.html").read_text(encoding="utf-8")
    assert 'class="features"' not in html and "fontrahive.com" not in html
    for element in ("login", "request", "request-open", "request-sent", "waitlist"):
        assert f'id="{element}"' in html, element
    assert not (ACCOUNT.parent / "landing").exists()
