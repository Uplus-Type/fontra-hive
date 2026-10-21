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
        assert 'rel="icon" href="/hive/icons/hive-icon.png"' in html, page.name
        assert "fontra-icon.svg" not in html, page.name
        for src in re.findall(r'(?:src|href)="/hive/icons/([^"]+)"', html):
            assert (ACCOUNT.parent / "icons" / src).is_file(), (page.name, src)


def test_sign_in_and_home_link_the_legal_pages():
    # The legal pages are served by the deployment (hive-api/deploy/site,
    # through Caddy), not by this plug-in: they are U+ TYPE's, not the plug-in's.
    # One page per document (U+ TYPE's lawyer, 21 October 2026), the same
    # footer as hive-api's application pages (deploy/site-chrome.py).
    for page in ("login.html", "home.html"):
        html = (ACCOUNT / page).read_text(encoding="utf-8")
        for path in ("/legal-notice", "/terms", "/terms-of-sale", "/privacy", "/charter"):
            assert f'href="{path}"' in html, (page, path)
        assert "data-cookie-notice" in html, page
    # Signing up from an invitation: the terms of use and the privacy policy,
    # next to their boxes (U+ TYPE's lawyer, 21 October 2026).
    html = (ACCOUNT / "invitation.html").read_text(encoding="utf-8")
    assert 'href="/terms' in html and 'href="/privacy"' in html
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


def test_try_fontra_opens_in_a_new_tab():
    # Try Fontra is another site (try.fontrahive.com): the account pages stay
    # open behind it. rel="noopener": the new tab cannot reach this one.
    found = 0
    for page in sorted(ACCOUNT.glob("*.html")):
        for link in re.findall(
            r'<a [^>]*href="/try[^"]*"[^>]*>', page.read_text("utf-8")
        ):
            found += 1
            assert 'target="_blank"' in link and 'rel="noopener"' in link, (
                page.name,
                link,
            )
    assert found


def test_hives_pages_are_dark_with_the_sites_bar():
    # Hive's pages are dark whatever the system theme (core.css: html.dark-theme);
    # only Fontra's editor follows its own theme setting. The home page's bar has
    # the height and look of a deployment's site header (fontrahive.com).
    for page in sorted(ACCOUNT.glob("*.html")):
        assert '<html class="dark-theme">' in page.read_text(
            encoding="utf-8"
        ), page.name
    css = (ACCOUNT / "home.css").read_text(encoding="utf-8")
    assert "min-height: 3.75rem" in css
    assert 'body[data-page="home"] { background: #222' in css
