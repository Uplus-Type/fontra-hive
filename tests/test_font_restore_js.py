"""Restoring the font from the detail of a version or a snapshot
(``fonthistory.js``: the editor's Font history and Font info's History). In
a headless browser, against a mocked server (the Download panel's snapshot:
``test_home_js.py``). Skipped without Playwright."""

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from test_font_history_js import browser_and_url, page  # noqa: E402,F401

MANAGER = ["read", "comment", "edit", "fontsettings", "kerning", "plugindata",
           "branch", "snapshot", "merge", "export"]
# A designer to whom the project keeps the font settings closed.
DESIGNER = ["read", "comment", "edit", "kerning", "plugindata", "branch", "snapshot"]
REVIEWER = ["read", "comment"]

SERVER = """() => {
  window.requests = [];
  window.restored = [];
  window.answer = null; // the next restore-font answer: [status, body]
  const json = (data, status = 200) => new Response(JSON.stringify(data), { status });
  window.fetch = async (url, options = {}) => {
    const u = new URL(url, location.href);
    const route = u.pathname.split("/").pop();
    const q = Object.fromEntries(u.searchParams);
    requests.push({ route, method: options.method || "GET", ...q });
    if (route === "font-diff") return json({ changes: [
      { area: "features", domain: "fontsettings", summary: "Features: 1 line added", fields: [] },
      { area: "kerning", domain: "kerning", summary: "Kerning: 1 pair changed (Bold)", fields: [] },
    ], glyphs: [] });
    if (route === "restore-font") {
      const [status, body] = window.answer || [200, { changed: true, head: "9".repeat(40),
        result: "Features are back as at “Round 1”, in a new version. Nothing was deleted from the history." }];
      return json(body, status);
    }
    return new Response("", { status: 404 });
  };
}"""


def open_detail(page, capabilities, panelPart=None, wait=True):
    page.evaluate(SERVER)
    page.evaluate(
        """async ([caps, panelPart]) => {
          window.fh = await import('/plugin/fonthistory.js');
          document.querySelectorAll('.hive-fontdiff, .hive-fontdiff-backdrop').forEach((e) => e.remove());
          window.d = fh.openFontDiff({ projectName: 'Mutator', branch: 'main', title: 'Snapshot “Round 1”',
            modes: [{ label: 'This snapshot', from: 'first', to: '5'.repeat(40) }],
            restore: { ref: '5'.repeat(40), label: '“Round 1”', capabilities: caps, panelPart,
                       onRestored: (data) => restored.push(data.head) } });
          await d.ready;
        }""",
        [capabilities, panelPart],
    )


FOOTER = "document.querySelector('.hive-fontdiff footer.restore')"


def buttons(page):
    return page.evaluate(
        f"[...{FOOTER}.querySelectorAll('button')].map(b => [b.textContent, b.disabled])"
    )


def parts(page):
    return page.evaluate(
        f"[...{FOOTER}.querySelectorAll('label.part')].map(l => "
        "[l.textContent, l.querySelector('input').disabled, l.querySelector('input').checked, l.title])"
    )


def test_the_parts_are_the_servers():
    pytest.importorskip("fontra")
    from fontra_hive import fontrestore

    import json
    import pathlib
    import re

    source = (
        pathlib.Path(__file__).parent.parent
        / "src/fontra_hive/client/plugin/fonthistory.js"
    ).read_text()
    block = source.split("export const RESTORE_PARTS = ", 1)[1].split("];", 1)[0] + "]"
    as_json = re.sub(r"(\w+):", r'"\1":', block).replace(",\n]", "\n]")
    as_json = re.sub(r",\s*([}\]])", r"\1", as_json)
    js = json.loads(as_json)
    assert [(p["key"], p["label"], tuple(p["areas"]), p["capability"]) for p in js] == [
        (p.key, p.label, p.areas, p.domain) for p in fontrestore.PARTS.values()
    ]


def test_a_manager_sees_every_restoration(page):
    open_detail(page, MANAGER)
    assert buttons(page) == [["Restore…", False], ["Restore whole font…", False]]
    page.evaluate(f"{FOOTER}.querySelector('.restore-choose').click()")
    # Every part open; those the detail shows changed are ticked.
    assert [(label, disabled, checked) for label, disabled, checked, _ in parts(page)] == [
        ("Font info", False, False),
        ("Axes and sources", False, False),
        ("Features", False, True),
        ("Development status", False, False),
        ("Kerning", False, True),
        ("Plug-in data", False, False),
        ("Glyph info", False, False),
    ]
    assert page.errors == []


def test_a_designer_without_font_settings(page):
    open_detail(page, DESIGNER)
    # No whole font: that is the managers'.
    assert buttons(page) == [["Restore…", False]]
    page.evaluate(f"{FOOTER}.querySelector('.restore-choose').click()")
    shown = {label: (disabled, checked, title) for label, disabled, checked, title in parts(page)}
    closed = "Font settings are kept to some people on this project. Ask a manager."
    for label in ("Font info", "Axes and sources", "Features", "Development status"):
        assert shown[label] == (True, False, closed)
    assert shown["Kerning"] == (False, True, "Restore kerning only")
    assert shown["Plug-in data"][0] is False and shown["Glyph info"][0] is False
    # A reviewer: nothing to restore.
    open_detail(page, REVIEWER)
    assert buttons(page) == [["Restore…", False]]
    page.evaluate(f"{FOOTER}.querySelector('.restore-choose').click()")
    assert all(disabled for _, disabled, _, _ in parts(page))
    assert parts(page)[-1][3] == "Your role cannot change this font."
    assert buttons(page)[0] == ["Restore", True]  # nothing chosen
    # Unknown capabilities (no accounts): open, the server decides.
    open_detail(page, None)
    assert buttons(page) == [["Restore…", False], ["Restore whole font…", False]]
    assert page.errors == []


def test_parts_restored_after_confirm(page):
    open_detail(page, MANAGER)
    page.evaluate(f"{FOOTER}.querySelector('.restore-choose').click()")
    page.evaluate(f"{FOOTER}.querySelector('input[value=kerning]').click()")  # untick
    restore = f"{FOOTER}.querySelector('.restore-parts')"
    page.evaluate(f"{restore}.click()")
    assert page.evaluate(f"{restore}.textContent") == "Confirm?"
    assert page.evaluate("requests.filter(r => r.route === 'restore-font').length") == 0
    page.evaluate(f"{restore}.click()")
    page.wait_for_function("restored.length === 1")
    post = page.evaluate("requests.find(r => r.route === 'restore-font')")
    assert post == {
        "route": "restore-font", "method": "POST", "branch": "main",
        "ref": "5" * 40, "parts": "features",
    }
    assert page.evaluate(f"{FOOTER}.querySelector('.said.done').textContent") == (
        "Features are back as at “Round 1”, in a new version. Nothing was deleted from the history."
    )
    assert page.evaluate("restored") == ["9" * 40]
    assert page.errors == []


def test_the_whole_font_and_an_armed_confirm_that_times_out(page):
    open_detail(page, MANAGER)
    page.evaluate("fh.CONFIRM_TIMEOUT_MS")  # 5 s: too long to wait here
    whole = f"{FOOTER}.querySelector('.restore-whole')"
    page.evaluate(f"{whole}.click()")
    assert page.evaluate(f"{whole}.textContent") == "Confirm?"
    assert page.evaluate(f"{FOOTER}.querySelector('.note').textContent").startswith(
        "Bring the whole font back as at “Round 1”"
    )
    page.evaluate(f"{whole}.click()")
    page.wait_for_function("restored.length === 1")
    post = page.evaluate("requests.find(r => r.route === 'restore-font')")
    assert "parts" not in post and post["ref"] == "5" * 40
    assert page.errors == []


def test_a_refusal_of_coherence_as_the_server_says_it(page):
    message = (
        "14 glyphs have layers for the source Black, which this version doesn't have."
        " Restore the whole font instead."
    )
    for capabilities, offered in ((MANAGER, True), (["read", "edit", "fontsettings"], False)):
        open_detail(page, capabilities)
        page.evaluate(f"answer = [409, {{ message: {message!r}, wholeFont: true }}]")
        page.evaluate(f"{FOOTER}.querySelector('.restore-choose').click()")
        page.evaluate(f"{FOOTER}.querySelectorAll('input:checked').forEach((b) => b.click())")
        page.evaluate(f"{FOOTER}.querySelector('input[value=axesAndSources]').click()")
        page.evaluate(f"{FOOTER}.querySelector('.restore-parts').click()")
        page.evaluate(f"{FOOTER}.querySelector('.restore-parts').click()")
        page.wait_for_selector(".hive-fontdiff .said.error")
        assert page.evaluate(f"{FOOTER}.querySelector('.said.error').textContent") == message
        has_whole = page.evaluate(f"!!{FOOTER}.querySelector('.restore-whole')")
        assert has_whole is offered
        assert page.evaluate("restored") == []
    # A refusal by capability: the server's sentence too.
    open_detail(page, MANAGER)
    page.evaluate("answer = [403, { message: 'You cannot restore Kerning: kerning is kept to some people on this project. Ask a manager.' }]")
    page.evaluate(f"{FOOTER}.querySelector('.restore-whole').click()")
    page.evaluate(f"{FOOTER}.querySelector('.restore-whole').click()")
    page.wait_for_selector(".hive-fontdiff .said.error")
    assert page.evaluate(f"{FOOTER}.querySelector('.said.error').textContent").startswith(
        "You cannot restore Kerning"
    )
    assert page.errors == []


def test_restore_this_panel(page):
    open_detail(page, MANAGER, panelPart="axesAndSources")
    assert buttons(page) == [
        ["Restore this panel", False],
        ["Restore…", False],
        ["Restore whole font…", False],
    ]
    panel = f"{FOOTER}.querySelector('.restore-panel')"
    page.evaluate(f"{panel}.click()")
    page.evaluate(f"{panel}.click()")
    page.wait_for_function("restored.length === 1")
    assert page.evaluate("requests.find(r => r.route === 'restore-font').parts") == "axesAndSources"
    # Without the right to change the font settings: greyed, and why.
    open_detail(page, DESIGNER, panelPart="features")
    assert buttons(page)[0] == ["Restore this panel", True]
    assert page.evaluate(f"{panel}.title") == (
        "Font settings are kept to some people on this project. Ask a manager."
    )
    assert page.errors == []


# Font info's History: "Restore this panel" on an older version of the panel.
FONTINFO = """() => {
  window.requests = [];
  window.restores = [];
  const sha = (c) => c.repeat(40);
  const font = (area, summary) => ({ area, domain: "fontsettings", summary });
  window.fetch = async (url, options = {}) => {
    const u = new URL(url, location.href);
    const route = u.pathname.split("/").pop();
    const q = Object.fromEntries(u.searchParams);
    requests.push({ route, ...q });
    const json = (data, status = 200) => new Response(JSON.stringify(data), { status });
    if (route === "access") return json({ role: "designer", capabilities: window.capabilities });
    if (route === "head") return json({ head: sha("3") });
    if (route === "log") return json({ head: sha("3"), snapshots: [], commits: [
      { sha: sha("3"), author: "Fabio", time: 1790000000, parents: [sha("2")], message: "Edit axes",
        font: [font("axes", "Axes: wght 100–900 → 200–900")] },
      { sha: sha("2"), author: "Ana", time: 1789990000, parents: [sha("1")], message: "Edit axes",
        font: [font("axes", "Axes: opsz added")] },
    ] });
    if (route === "font-diff") return json({ changes: [
      { area: "axes", domain: "fontsettings", summary: "Axes: opsz added", fields: [] }], glyphs: [] });
    if (route === "restore-font") {
      restores.push(q);
      return json({ changed: true, head: sha("4"), result: "Axes and sources are back as at this version, in a new version." });
    }
    return new Response("", { status: 404 });
  };
  const panels = { selectedPanelIdentifier: "axes-panel", selectPanel(id) { this.selectedPanelIdentifier = id; } };
  window.fontInfoController = { multiPanelController: panels };
}"""


def test_font_info_restores_its_panel(page):
    page.evaluate(f"window.capabilities = {MANAGER!r}")
    page.evaluate(FONTINFO)
    page.evaluate(
        "async () => { window.section = await hive.mountFontInfoHistory("
        "{ identifier: 'Mutator', name: 'Mutator', branch: 'main' }, { timeout: 1000 }); }"
    )
    root = "section.host.shadowRoot"
    page.wait_for_function(f"{root}.querySelectorAll('.change').length === 2")
    # The version the branch is at: nothing to bring back.
    page.evaluate(f"{root}.querySelectorAll('.change')[0].click()")
    page.wait_for_selector(".hive-fontdiff h4")
    assert page.evaluate(FOOTER) is None
    page.keyboard.press("Escape")
    # An older one: "Restore this panel" brings the axes and sources back.
    page.evaluate(f"{root}.querySelectorAll('.change')[1].click()")
    page.wait_for_selector(".hive-fontdiff footer.restore")
    page.evaluate(f"{FOOTER}.querySelector('.restore-panel').click()")
    page.evaluate(f"{FOOTER}.querySelector('.restore-panel').click()")
    page.wait_for_function("restores.length === 1")
    assert page.evaluate("restores[0]") == {
        "branch": "main", "ref": "2" * 40, "parts": "axesAndSources"
    }
    page.wait_for_function("requests.filter(r => r.route === 'log').length >= 2")
    page.keyboard.press("Escape")
    # A designer to whom the project keeps the font settings: greyed.
    page.evaluate(f"window.capabilities = {DESIGNER!r}")
    page.evaluate("section.loadCapabilities()")
    page.wait_for_function("section.capabilities && !section.capabilities.includes('fontsettings')")
    page.evaluate(f"{root}.querySelectorAll('.change')[1].click()")
    page.wait_for_selector(".hive-fontdiff footer.restore")
    assert buttons(page) == [["Restore this panel", True], ["Restore…", False]]
    assert page.errors == []
