"""The bell and the bubble (client/views/hive-notifications.js) on Hive's home
page, against a mocked hive-api. Skipped without Playwright."""

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from test_account_js import browser_and_url  # noqa: E402,F401
from test_home_js import open_home  # noqa: E402

ANA = {"username": "ana", "name": "Ana López", "avatar": None}
CREATED = "2026-11-04T10:00:00+00:00"
SEEN = "hive.notifications.seen.jeremie"
BUBBLE = "hive.notifications.bubble.jeremie"
COUNT = "hive.notifications.count.jeremie"


def notification(number, kind="mentioned", glyph="H", read=False, issue=12):
    return {
        "id": number,
        "type": kind,
        "project": "uplustype/Gotham",
        "issue": issue,
        "glyph": glyph,
        "actor": ANA,
        "created": CREATED,
        "read": read,
    }


def answers(items, latest=None):
    unread = sum(1 for n in items if not n["read"])
    latest = latest if latest is not None else max((n["id"] for n in items), default=None)
    return {
        "GET /api/notifications/count": [200, {"count": unread, "latest": latest}],
        "GET /api/notifications": [200, {"count": unread, "notifications": items}],
        "POST /api/notifications/read": [200, {"count": max(0, unread - 1)}],
    }


def update(page, items, latest=None):
    page.evaluate(
        "(answers) => Object.assign(window.answers, answers)", answers(items, latest)
    )


@pytest.fixture
def home(browser_and_url):  # noqa: F811
    pages = []

    def open_page(items=(), seen=None):
        init = f"localStorage.setItem('{SEEN}', '{seen}');"
        page = open_home(
            browser_and_url,
            "",
            answers(list(items)),
            init if seen is not None else None,
        )
        page.wait_for_selector(".hive-bell")
        page.wait_for_function("window.hiveViews?.bell")
        page.evaluate(
            """() => {
              window.bell = window.hiveViews.bell;
              window.setHidden = (hidden) => {
                Object.defineProperty(document, "visibilityState", {
                  configurable: true, get: () => (hidden ? "hidden" : "visible") });
                document.dispatchEvent(new Event("visibilitychange"));
              };
            }"""
        )
        page.evaluate("bell.poll()")
        pages.append(page)
        return page

    yield open_page
    for page in pages:
        page.close()


def badge(page):
    return page.evaluate(
        "(() => { const b = document.querySelector('.hive-bell-count');"
        " return b.hidden ? null : b.textContent; })()"
    )


def bubble(page):
    return page.evaluate("document.querySelector('.hive-bubble .text')?.textContent ?? null")


def test_the_badge_counts_what_is_unread(home):
    page = home([notification(1), notification(2, read=True)])
    assert badge(page) == "1"
    update(page, [notification(n) for n in range(1, 121)])
    page.evaluate("bell.poll()")
    assert badge(page) == "99+"
    update(page, [])
    page.evaluate("bell.poll()")
    assert badge(page) is None
    assert page.errors == []


def test_what_came_before_is_no_news(home):
    page = home([notification(1)])
    assert bubble(page) is None
    assert page.evaluate(f"localStorage.getItem('{SEEN}')") == "1"


def test_something_new_shows_a_bubble(home):
    page = home([notification(1, read=True)], seen=1)
    assert bubble(page) is None
    update(page, [notification(2), notification(1, read=True)])
    page.evaluate("bell.poll()")
    assert bubble(page) == "Ana López mentioned you in #12 · H · Gotham"
    update(page, [notification(3, "replied", glyph=None, issue=13), notification(2)])
    page.evaluate("bell.poll()")
    assert bubble(page) == "Ana López replied in #13 · Font · Gothamand 1 more"
    assert page.errors == []


@pytest.mark.parametrize(
    "kind, text",
    [
        ("assigned", "Ana López assigned you #12"),
        ("resolved", "Ana López resolved #12"),
        ("reopened", "Ana López reopened #12"),
    ],
)
def test_the_words_of_each_kind(home, kind, text):
    page = home([], seen=0)
    update(page, [notification(1, kind)])
    page.evaluate("bell.poll()")
    assert bubble(page).startswith(text)


def test_a_hidden_tab_asks_nothing_then_asks_at_once(home):
    page = home([], seen=0)
    page.evaluate("setHidden(true); window.calls.length = 0")
    update(page, [notification(1)])
    page.evaluate("bell.poll()")
    assert page.evaluate("window.calls.length") == 0
    page.evaluate("setHidden(false)")
    page.wait_for_selector(".hive-bubble")
    assert bubble(page).startswith("Ana López mentioned")
    assert badge(page) == "1"


def test_the_bubble_comes_back_in_the_next_tab(home):
    page = home([], seen=0)
    update(page, [notification(1)])
    page.evaluate("bell.poll()")
    page.wait_for_selector(".hive-bubble")
    # Leaving: it goes with the tab, and stays to be shown.
    page.evaluate("setHidden(true)")
    assert bubble(page) is None
    assert page.evaluate(f"JSON.parse(localStorage['{BUBBLE}']).items.length") == 1
    page.evaluate("setHidden(false)")
    assert bubble(page).startswith("Ana López")


def test_its_time_runs_only_when_it_is_seen(home):
    page = home([], seen=0)
    update(page, [notification(1)])
    page.evaluate("bell.poll()")
    page.wait_for_selector(".hive-bubble")
    shown = f"JSON.parse(localStorage['{BUBBLE}']).shown"
    start = page.evaluate(shown)
    clock = 2_000_000_000_000  # far from the real ticks of this page
    page.evaluate(f"setHidden(true); bell.tick({clock}); bell.tick({clock + 1000})")
    assert page.evaluate(shown) == start
    page.evaluate(f"setHidden(false); bell.hovering = true; bell.tick({clock + 2000})")
    assert page.evaluate(shown) == start
    page.evaluate(f"bell.hovering = false; bell.tick({clock + 3000})")
    assert page.evaluate(shown) == start + 1000
    # Another window at the same moment: it counts once.
    page.evaluate(f"bell.tick({clock + 3100})")
    assert page.evaluate(shown) == start + 1000
    page.evaluate(
        f"for (let i = 1; i <= 8; i++) bell.tick({clock + 3000} + i * 1000)"
    )
    assert bubble(page) is None
    assert page.evaluate(f"localStorage['{BUBBLE}']") is None
    # The bell still says it.
    assert badge(page) == "1"


def test_closing_it_leaves_it_unread_everywhere(home):
    page = home([], seen=0)
    update(page, [notification(1)])
    page.evaluate("bell.poll()")
    page.click(".hive-bubble button")
    assert bubble(page) is None
    assert page.evaluate(f"localStorage['{BUBBLE}']") is None
    assert page.evaluate(
        "window.calls.filter(c => c.path.endsWith('/read')).length"
    ) == 0
    assert badge(page) == "1"


def test_another_tab_closing_it_takes_it_away_here(home):
    page = home([], seen=0)
    update(page, [notification(1)])
    page.evaluate("bell.poll()")
    page.wait_for_selector(".hive-bubble")
    page.evaluate(
        f"""localStorage.removeItem('{BUBBLE}');
        window.dispatchEvent(new StorageEvent('storage', {{ key: '{BUBBLE}' }}));"""
    )
    assert bubble(page) is None


def test_a_click_reads_it_and_goes_there(home):
    page = home([], seen=0)
    update(page, [notification(7)])
    page.evaluate("bell.poll()")
    page.evaluate("window.open = (url, name) => (window.opened = [url, name]); 0")
    page.evaluate("window.answers['POST /api/notifications/read'] = [200, { count: 0 }]")
    page.click(".hive-bubble .text")
    page.wait_for_function("window.opened")
    read = page.evaluate("window.calls.find(c => c.path.endsWith('/read'))")
    assert read["body"] == {"id": 7}
    url, tab = page.evaluate("window.opened")
    assert "project=uplustype%2FGotham" in url and "hive-issue=12" in url
    assert tab == "hive-editor-uplustype/Gotham"
    assert bubble(page) is None
    assert badge(page) is None  # the count goes down at once
    assert page.evaluate(f"localStorage['{BUBBLE}']") is None
    assert page.errors == []


def test_the_count_is_told_to_the_other_tabs(home):
    page = home([notification(1), notification(2)])
    assert badge(page) == "2"
    page.evaluate(
        f"""localStorage.setItem('{COUNT}', '1');
        window.dispatchEvent(new StorageEvent('storage', {{ key: '{COUNT}' }}));"""
    )
    assert badge(page) == "1"
    # And this tab tells them.
    page.evaluate("bell.markRead({ id: 2 })")
    assert page.evaluate(f"localStorage['{COUNT}']") == "1"


def test_it_goes_there_after_the_read_is_known(home):
    page = home([], seen=0)
    page.evaluate(
        """() => {
          window.open = () => (window.order = [...(window.order || []), 'open']);
          const original = window.fetch;
          window.fetch = async (url, options) => {
            const answer = await original(url, options);
            if (String(url).endsWith('/read')) window.order = ['read'];
            return answer;
          };
        }"""
    )
    page.evaluate("bell.open({ id: 1, project: 'uplustype/Gotham', issue: 12, glyph: 'H' })")
    assert page.evaluate("window.order") == ["read", "open"]


def test_a_read_that_hangs_does_not_stop_the_link(home):
    page = home([], seen=0)
    page.evaluate(
        """() => {
          window.open = () => (window.opened = true);
          window.fetch = () => new Promise(() => {});
        }"""
    )
    page.evaluate("bell.open({ id: 1, project: 'uplustype/Gotham', issue: 12, glyph: 'H' })")
    assert page.evaluate("window.opened") is True


def test_a_font_topic_opens_on_the_comments_page(home):
    page = home([], seen=0)
    update(page, [notification(7, glyph=None, issue=13)])
    page.evaluate("bell.poll()")
    page.evaluate("window.open = (url, name) => (window.opened = [url, name]); 0")
    page.click(".hive-bubble .text")
    page.wait_for_function("window.opened")
    url, tab = page.evaluate("window.opened")
    assert url.endswith("/#project/uplustype/Gotham/comments/13")
    assert tab == "hive-comments-uplustype/Gotham"


def test_in_the_editor_of_the_project_it_stays_there(home):
    page = home([], seen=0)
    page.evaluate(
        """window.shown = null;
        window.hiveComments = { projectName: 'uplustype/Gotham',
          issue: (n) => (n === 12 ? { number: 12 } : null),
          show: (issue) => (window.shown = issue.number) };
        window.open = () => (window.opened = true); 0"""
    )
    page.evaluate("bell.open({ id: 1, project: 'uplustype/Gotham', issue: 12, glyph: 'H' })")
    assert page.evaluate("window.shown") == 12
    assert page.evaluate("window.opened ?? null") is None
    # A topic it does not know yet, or another project: the usual way.
    page.evaluate("bell.open({ id: 1, project: 'uplustype/Gotham', issue: 99, glyph: 'H' })")
    assert page.evaluate("window.opened") is True


def test_the_menu(home):
    page = home([notification(2), notification(1, "assigned", read=True)])
    page.evaluate("window.open = (url, name) => (window.opened = [url, name]); 0")
    page.click(".hive-bell")
    page.wait_for_selector(".hive-notifications")
    rows = "[...document.querySelectorAll('.hive-notifications a.notification')]"
    assert page.evaluate(f"{rows}.map(a => a.classList.contains('unread'))") == [True, False]
    assert "Ana López assigned you #12" in page.evaluate(f"{rows}[1].textContent")
    page.click(".hive-bell")  # the same button closes it
    assert page.query_selector(".hive-notifications") is None
    page.click(".hive-bell")
    page.wait_for_selector(".hive-notifications")
    page.click(".hive-notifications a.notification")
    assert page.query_selector(".hive-notifications") is None
    page.wait_for_function("window.opened")
    assert page.evaluate("window.opened[0]").count("hive-issue=12") == 1
    assert page.errors == []


def test_mark_all_read(home):
    page = home([notification(1), notification(2)])
    page.evaluate(
        "window.answers['POST /api/notifications/read'] = [200, { count: 0 }]"
    )
    page.click(".hive-bell")
    page.wait_for_selector(".hive-notifications .head a")
    page.click(".hive-notifications .head a")
    page.wait_for_function("window.calls.some(c => c.path.endsWith('/read'))")
    assert page.evaluate("window.calls.find(c => c.path.endsWith('/read')).body") == {"all": True}
    page.wait_for_function("document.querySelector('.hive-bell-count').hidden")


def test_nothing_yet(home):
    page = home([])
    page.click(".hive-bell")
    page.wait_for_selector(".hive-notifications .note")
    assert page.text_content(".hive-notifications .note") == "Nothing yet."
    assert page.query_selector(".hive-notifications .head a") is None


def test_hive_api_away_leaves_the_page_alone(home):
    page = home([notification(1)])
    page.evaluate("window.answers['GET /api/notifications/count'] = [503, { detail: 'away' }]")
    page.evaluate("bell.poll()")
    assert badge(page) == "1"
    assert page.errors == []


def test_another_account_on_this_browser_sees_nothing_of_it(home):
    page = home([], seen=0)
    update(page, [notification(1)])
    page.evaluate("bell.poll()")
    page.wait_for_selector(".hive-bubble")
    other = """async () => {
      const { NotificationBell } = await import('/views/hive-notifications.js');
      const next = new NotificationBell({ username: 'ana' });
      await next.poll();
      return [localStorage.getItem('hive.notifications.seen.ana'),
              next.seenKey !== window.bell.seenKey, next.bubbleKey !== window.bell.bubbleKey,
              null];
    }"""
    seen, different_seen, different_bubble, _ = page.evaluate(other)
    # Its first look: what is there is not news, whatever the last person saw.
    assert seen == "1" and different_seen and different_bubble


def test_the_mouse_on_a_bubble_gone_does_not_stop_the_next(home):
    page = home([], seen=0)
    update(page, [notification(1)])
    page.evaluate("bell.poll()")
    page.hover(".hive-bubble")
    assert page.evaluate("bell.hovering") is True
    page.click(".hive-bubble button")
    assert page.evaluate("bell.hovering") is False


def test_two_tabs_telling_at_once_tell_it_once(home):
    page = home([], seen=0)
    update(page, [notification(6)])
    page.evaluate("Promise.all([bell.tell(6), bell.tell(6)])")
    assert page.evaluate(f"JSON.parse(localStorage['{BUBBLE}']).items.length") == 1
    assert bubble(page) == "Ana López mentioned you in #12 · H · Gotham"
    # The tab that answers late cannot bring ``seen`` back.
    update(page, [notification(7), notification(6)])
    page.evaluate("bell.tell(7)")
    page.evaluate("bell.tell(6)")
    assert page.evaluate(f"localStorage['{SEEN}']") == "7"


def test_the_bubble_keeps_only_the_latest(home):
    page = home([], seen=0)
    for n in range(1, 26):
        update(page, [notification(n)])
        page.evaluate("bell.poll()")
    assert page.evaluate(f"JSON.parse(localStorage['{BUBBLE}']).items.length") == 20


def test_mark_all_read_takes_the_bubble_away(home):
    page = home([], seen=0)
    update(page, [notification(1)])
    page.evaluate("bell.poll()")
    page.wait_for_selector(".hive-bubble")
    page.click(".hive-bell")
    page.wait_for_selector(".hive-notifications .head a")
    page.click(".hive-notifications .head a")
    assert bubble(page) is None


def test_a_menu_opened_meanwhile_stays(home):
    page = home([notification(1)])
    page.evaluate(
        """() => {
          const original = window.fetch;
          window.fetch = async (url, options) => {
            if (String(url).endsWith('/api/notifications')) {
              await new Promise((resolve) => (window.letItAnswer = resolve));
            }
            return original(url, options);
          };
        }"""
    )
    page.click(".hive-bell")
    page.wait_for_function("typeof window.letItAnswer === 'function'")
    page.click(".hive-chip")
    page.wait_for_selector(".hive-menu .who")
    page.evaluate("window.letItAnswer(); 0")
    page.wait_for_timeout(100)
    assert page.query_selector(".hive-menu .who")
    assert page.query_selector(".hive-notifications") is None
    assert page.errors == []
