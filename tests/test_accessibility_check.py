# tests/test_accessibility_check.py
# The WCAG 2.2 AA checks in tools/accessibility_check.py (#298). The audit
# script runs in headless Microsoft Edge on synthetic page fragments: each
# check fails on a fragment that breaks its criterion and passes on one that
# meets it, and a clean fragment passes every check. No app window, workspace
# or bridge is involved; the tool itself runs on the real window (section 12 of
# docs/RELEASE_STANDARD.md). The report logic (which findings fail a run, and
# the page language, title and status region) is tested without Edge.

import html
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import accessibility_check  # noqa: E402
import design_check  # noqa: E402  (only its list of Edge locations)

EDGE = next((p for p in design_check.EDGE if p.is_file()), None)
GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
BASE = ("<style>body { margin: 0; font: 16px/1.4 sans-serif; background: #fff; color: #111; }"
        "button { font: inherit; } .gap { margin: 40px; }</style>")
SMALL = "width: 16px; height: 16px; padding: 0; margin: 0; border: 1px solid #333;"

# Fragments, each shown on its own in a 1100 by 760 px page.
FIXTURES = {
    "clean": """
      <style>input, button { border: 1px solid #555; min-height: 32px; margin: 8px; }
             button:focus, input:focus { outline: 2px solid #1a4fa0; outline-offset: 2px; }
      </style>
      <main><h1>Synthetic form</h1>
        <p>Read the <a href="#guide">guide</a> before you start.</p>
        <label for="who">Speaker name</label> <input id="who">
        <label><input type="checkbox"> Keep a copy</label>
        <button>Save</button> <button aria-label="Cancel and close">Cancel</button>
        <img src="GIF" alt="" width="20" height="20">
        <div style="width: 200px; min-height: 20px; overflow: hidden">Grows with its text</div>
      </main>""".replace("GIF", GIF),
    # 2.5.8 Target Size (Minimum)
    "small_adjacent": f'<div class="gap"><button aria-label="Up" style="{SMALL}">&uarr;</button>'
                      f'<button aria-label="Down" style="{SMALL}">&darr;</button></div>',
    "small_spaced": f'<div class="gap"><button aria-label="Up" style="{SMALL}">&uarr;</button>'
                    f'<button aria-label="Down" style="{SMALL} margin-left: 30px">&darr;'
                    '</button></div>',
    "small_inline_link": '<p class="gap">Read the <a href="#g" style="font-size: 12px">guide</a>'
                         ' first, then the <a href="#n" style="font-size: 12px">notes</a>.</p>',
    "checkbox_native": '<div class="gap"><input type="checkbox" aria-label="One">'
                       '<input type="checkbox" aria-label="Two"></div>',
    "checkbox_styled": '<style>.c { width: 16px; height: 16px; margin: 0; }</style>'
                       '<div class="gap">'
                       '<input class="c" type="checkbox" aria-label="One">'
                       '<input class="c" type="checkbox" aria-label="Two"></div>',
    "checkbox_labelled": '<style>.c { width: 16px; height: 16px; margin: 0; } label { display: '
                         'inline-block; padding: 6px; }</style><div class="gap">'
                         '<label><input class="c" type="checkbox"> One</label>'
                         '<label><input class="c" type="checkbox"> Two</label></div>',
    # 2.4.11 Focus Not Obscured (Minimum)
    "obscured": '<div style="position: fixed; inset: 0 0 auto 0; height: 120px; z-index: 2; '
                'background: #eee">Header</div><button style="position: absolute; top: 30px; '
                'left: 30px">Under the header</button>',
    "partly_obscured": '<div style="position: fixed; inset: 0 0 auto 0; height: 120px; z-index: 2;'
                       ' background: #eee">Header</div><button style="position: absolute; '
                       'top: 110px; left: 30px; height: 40px">Half under</button>',
    # 1.4.11 Non-text Contrast: the focus ring and field boundaries
    "pale_ring": '<style>button:focus, button:focus-visible { outline: 2px solid #ddd; '
                 'outline-offset: 2px; }</style><button class="gap">Pale ring</button>',
    "dark_ring": '<style>button:focus, button:focus-visible { outline: 2px solid #1a1a1a; '
                 'outline-offset: 2px; }</style><button class="gap">Dark ring</button>',
    "pale_field": '<input class="gap" aria-label="Title" style="border: 1px solid #eee; '
                  'background: #fff">',
    "dark_field": '<input class="gap" aria-label="Title" style="border: 1px solid #767676; '
                  'background: #fff">',
    "wrapped_field": '<div class="gap" style="border: 1px solid #333; width: 220px">'
                     '<input aria-label="Search" style="border: 0; background: #fff"></div>',
    # 1.4.12 Text Spacing
    "spacing_clips": '<div class="gap" style="width: 300px; height: 20px; overflow: hidden; '
                     'line-height: 20px">Fits only at its own line height</div>',
    "spacing_grows": '<div class="gap" style="width: 300px; min-height: 20px; overflow: hidden; '
                     'line-height: 20px">Grows with its text</div>',
    # Fails part-way through the text spacing check (see SETUP).
    "spacing_fails": '<div id="boom" class="gap" style="width: 60px; white-space: nowrap; '
                     'overflow: hidden">Synthetic text that is cut off</div>',
    "already_cut": '<div class="gap" style="width: 60px; white-space: nowrap; overflow: hidden; '
                   'text-overflow: ellipsis">A long title cut off by design</div>',
    # 2.1.2 No Keyboard Trap
    "swallows_tab": '<button onkeydown="if (event.key === \'Tab\') event.preventDefault()">'
                    'Stuck</button><button>Next</button>',
    "pulls_back": '<input aria-label="Grabby" onblur="setTimeout(() => this.focus())">'
                  '<button>Next</button>',
    "moves_on_tab": '<button onkeydown="if (event.key === \'Tab\') { event.preventDefault(); '
                    'this.nextElementSibling.focus(); }">Roving</button><button>Next</button>',
    # 2.5.3 Label in Name
    "name_differs": '<button aria-label="Remove">Delete</button>'
                    '<label for="s">Speaker name</label><input id="s" aria-label="Name">',
    # Part of a word is not the word: "Delete" is not in "Undelete".
    "name_part_word": '<button aria-label="Undelete">Delete</button>'
                      '<label for="t">Time</label><input id="t" aria-label="Timeline start">',
    "name_contains": '<button aria-label="Delete meeting">Delete</button>'
                     '<button aria-label="Close">&times;</button>'
                     '<label for="s">Speaker name</label><input id="s" aria-label="Speaker Name:">',
    # 1.1.1 Non-text Content (in part)
    "image_no_alt": f'<img src="{GIF}" width="20" height="20">',
    "image_alt": f'<img src="{GIF}" alt="" width="20" height="20">',
    # 3.2.1 On Focus
    "opens_on_focus": '<button onfocus="document.getElementById(\'d\').show()">Opens</button>'
                      '<dialog id="d" aria-label="Pop-up">Synthetic</dialog>',
}

# Script run after a fragment is shown, before the audit.
SETUP = {
    # The box's text throws once the text spacing is applied, so the audit
    # fails with the spacing still on the page unless it takes it off anyway.
    "spacing_fails": "Object.defineProperty(document.getElementById('boom'), 'innerText', "
                     "{get() { if (getComputedStyle(this).wordSpacing !== '0px') "
                     "throw new Error('synthetic failure'); return 'Synthetic'; }});",
}

RUNNER = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Fixtures</title>
<script>
const AUDIT = %s, FIXTURES = %s, BASE = %s, SETUP = %s, COLOUR_MODE = %s;
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
window.addEventListener('load', async () => {
  const out = {}, styles = {};
  const colour = new Function(COLOUR_MODE)();
  for (const [name, fragment] of Object.entries(FIXTURES)) {
    document.body.innerHTML = BASE + fragment;
    if (SETUP[name]) new Function(SETUP[name])();
    window.scrollTo(0, 0);
    document.body.getBoundingClientRect();
    await new Promise(r => setTimeout(r, 50));
    try { out[name] = await new AsyncFunction(AUDIT)(); }
    catch (e) { out[name] = {error: String(e)}; }
    // Anything the audit added to the page and left behind (the head holds
    // no style of its own).
    styles[name] = document.head.querySelectorAll('style').length;
  }
  document.body.innerHTML = '';
  const pre = document.createElement('pre');
  pre.id = 'result';
  pre.textContent = JSON.stringify({results: out, styles, colour});
  document.body.append(pre);
});
</script></head><body></body></html>"""


def audit_fixtures(fixtures, dark=False):
    """The audit's result for each fragment, the styles left in the page's head
    after each, and the colour mode the page reported, from headless Edge in
    light (or dark) colour mode."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "fixtures.html"
        page.write_text(RUNNER % (json.dumps(accessibility_check.AUDIT), json.dumps(fixtures),
                                  json.dumps(BASE), json.dumps(SETUP),
                                  json.dumps(accessibility_check.COLOUR_MODE)), encoding="utf-8")
        profile = Path(tmp) / "profile"
        cmd = [str(EDGE), "--headless=new", "--disable-gpu", "--no-first-run",
               "--disable-extensions", f"--user-data-dir={profile}", "--mute-audio",
               "--force-device-scale-factor=1", "--window-size=1100,760",
               # prefers-color-scheme: 0 dark, 1 light (otherwise it follows Windows)
               f"--blink-settings=preferredColorScheme={0 if dark else 1}",
               "--virtual-time-budget=60000", "--dump-dom", page.as_uri()]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=300)
    found = re.search(r'<pre id="result">(.*?)</pre>', out.stdout or "", re.DOTALL)
    if not found:
        raise AssertionError(f"Edge returned no result (exit {out.returncode}): "
                             f"{(out.stderr or '').strip()[-500:]}")
    return json.loads(html.unescape(found.group(1)))


@unittest.skipUnless(EDGE, "Microsoft Edge not installed")
class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.edge = audit_fixtures(FIXTURES)
        cls.results = cls.edge["results"]

    def result(self, name):
        r = self.results[name]
        self.assertNotIn("error", r, r.get("error"))
        return r

    def flagged(self, name, key):
        return self.result(name)[key]

    def test_the_fixtures_run_in_light_mode(self):
        self.assertEqual("light", self.edge["colour"])

    def test_a_clean_fragment_passes_every_check(self):
        r = self.result("clean")
        self.assertGreaterEqual(r["controls"], 5)
        self.assertEqual({k: [] for k in accessibility_check.CHECKS},
                         {k: r[k] for k in accessibility_check.CHECKS})

    def test_every_list_the_audit_returns_fails_a_run(self):
        # A new check that is not in CHECKS would be measured but never fail.
        self.assertEqual(set(self.result("clean")) - {"controls"}, set(accessibility_check.CHECKS))

    def test_target_size(self):
        self.assertEqual(["button 16x16", "button 16x16"],
                         self.flagged("small_adjacent", "small_targets"))
        # Spaced so 24 px circles on their centres meet nothing: the spacing exception.
        self.assertEqual([], self.flagged("small_spaced", "small_targets"))
        # Links in a sentence: the inline exception.
        self.assertEqual([], self.flagged("small_inline_link", "small_targets"))
        # Checkboxes the browser sizes: the user agent exception. Resized by the
        # page, they are targets like any other, unless their label is big enough.
        self.assertEqual([], self.flagged("checkbox_native", "small_targets"))
        self.assertEqual(["input.c 16x16", "input.c 16x16"],
                         self.flagged("checkbox_styled", "small_targets"))
        self.assertEqual([], self.flagged("checkbox_labelled", "small_targets"))

    def test_focus_not_obscured(self):
        self.assertEqual(["button"], self.flagged("obscured", "focus_obscured"))
        # The minimum criterion: partly hidden passes.
        self.assertEqual([], self.flagged("partly_obscured", "focus_obscured"))
        self.assertEqual([], self.flagged("clean", "focus_obscured"))

    def test_focus_ring_contrast(self):
        flagged = self.flagged("pale_ring", "low_contrast_focus")
        self.assertEqual(1, len(flagged))
        self.assertRegex(flagged[0], r"^button\.gap 1\.\d\d$")
        self.assertEqual([], self.flagged("dark_ring", "low_contrast_focus"))

    def test_field_boundary_contrast(self):
        flagged = self.flagged("pale_field", "low_contrast_boundary")
        self.assertEqual(1, len(flagged))
        self.assertRegex(flagged[0], r"^input\.gap 1\.\d\d$")
        self.assertEqual([], self.flagged("dark_field", "low_contrast_boundary"))
        # A borderless field inside a wrapper that draws the boundary.
        self.assertEqual([], self.flagged("wrapped_field", "low_contrast_boundary"))

    def test_text_spacing(self):
        self.assertEqual(["div.gap"], self.flagged("spacing_clips", "text_spacing_cut_off"))
        self.assertEqual([], self.flagged("spacing_grows", "text_spacing_cut_off"))
        # Cut off before the spacing too: a design choice, not what 1.4.12 measures.
        self.assertEqual([], self.flagged("already_cut", "text_spacing_cut_off"))
        # The spacing is taken off again afterwards, and nothing is left in the page.
        self.assertEqual([], self.flagged("clean", "text_spacing_cut_off"))
        self.assertEqual(0, self.edge["styles"]["clean"])

    def test_text_spacing_is_taken_off_when_the_audit_fails(self):
        # The audit fails with the spacing applied; the page must not keep it,
        # or every later view would be audited with it.
        self.assertIn("synthetic failure", self.results["spacing_fails"].get("error", ""))
        self.assertEqual(0, self.edge["styles"]["spacing_fails"])

    def test_keyboard_trap(self):
        self.assertEqual(["button"], self.flagged("swallows_tab", "keyboard_trap"))
        self.assertEqual(["input"], self.flagged("pulls_back", "keyboard_trap"))
        # Handling Tab is fine when focus moves on.
        self.assertEqual([], self.flagged("moves_on_tab", "keyboard_trap"))

    def test_label_in_name(self):
        self.assertEqual(["button", "input#s"], self.flagged("name_differs", "label_not_in_name"))
        # Whole words only: a label that is only part of a word in the name fails.
        self.assertEqual(["button", "input#t"],
                         self.flagged("name_part_word", "label_not_in_name"))
        # Case and punctuation aside; a symbol is not a label.
        self.assertEqual([], self.flagged("name_contains", "label_not_in_name"))

    def test_images_need_a_text_alternative(self):
        self.assertEqual(["img"], self.flagged("image_no_alt", "no_alt"))
        self.assertEqual([], self.flagged("image_alt", "no_alt"))

    def test_focus_opens_nothing(self):
        self.assertEqual(["button"], self.flagged("opens_on_focus", "change_on_focus"))


@unittest.skipUnless(EDGE, "Microsoft Edge not installed")
class DarkModeTests(unittest.TestCase):
    def test_the_audit_runs_the_same_in_dark_mode(self):
        run = audit_fixtures({"clean": FIXTURES["clean"]}, dark=True)
        self.assertEqual("dark", run["colour"])
        r = run["results"]["clean"]
        self.assertNotIn("error", r, r.get("error"))
        self.assertEqual(set(accessibility_check.CHECKS), set(r) - {"controls"})


class ColourModeTests(unittest.TestCase):
    def test_the_modes_each_choice_audits(self):
        modes = accessibility_check.colour_modes
        self.assertEqual([None], modes("system"))
        self.assertEqual(["light"], modes("light"))
        self.assertEqual(["dark"], modes("dark"))
        self.assertEqual(["light", "dark"], modes("both"))
        self.assertEqual("system", accessibility_check.COLOUR_CHOICES[0])  # the default
        with self.assertRaises(KeyError):
            modes("contrast")

    def test_a_mode_the_page_does_not_show_stops_the_run(self):
        shown = accessibility_check.colour_mode_shown
        self.assertEqual("dark", shown("dark", "dark"))
        self.assertEqual("light", shown(None, "light"))     # the PC's own: whatever it is
        self.assertEqual("dark", shown(None, "dark"))
        for asked, page in (("dark", "light"), ("light", "dark"), ("dark", None), (None, None)):
            with self.subTest(asked=asked, page=page), self.assertRaises(RuntimeError):
                shown(asked, page)

    def test_views_are_named_by_mode_only_when_both_are_audited(self):
        key = accessibility_check.view_key
        self.assertEqual("meeting", key("meeting", "dark", [None]))
        self.assertEqual("meeting", key("meeting", "dark", ["dark"]))
        self.assertEqual("meeting (light)", key("meeting", "light", ["light", "dark"]))
        self.assertEqual("meeting (dark)", key("meeting", "dark", ["light", "dark"]))
        # Each mode's view fails the run on its own.
        found = accessibility_check.findings(
            {**ReportTests.ZOOM, "page": ReportTests.PAGE,
             "views": {"meeting (light)": {}, "meeting (dark)": {"low_contrast": ["span 2.10"]}}})
        self.assertEqual(["meeting (dark): low_contrast (1)"], found)


class _Task:
    """A finished .NET Task, as CallDevToolsProtocolMethodAsync returns."""
    IsCompleted, IsFaulted, IsCanceled, Exception, Result = True, False, False, None, "{}"


class _Page:
    """A stand-in for the check's WebView2 window (#328): the page's colour
    mode, which the browser's media emulation changes and, as on a PC in the
    other mode, WebView2's preferred colour scheme for the profile may not."""

    def __init__(self, mode, profile_switches=False, emulation_switches=True,
                 styles_follow=True, emulation_error=None, task=None):
        self.media = self.drawn = mode
        self.profile_switches, self.emulation_switches = profile_switches, emulation_switches
        self.styles_follow, self.emulation_error = styles_follow, emulation_error
        self.task = task if task is not None else _Task()
        self.profile_set, self.devtools = [], []
        page = self

        class Profile:
            def __setattr__(self, name, value):
                page.profile_set.append((name, value))
                if page.profile_switches and value in ("light", "dark"):
                    page.show(value)

        class Core:
            def __init__(self):
                self.Profile = Profile()

            def CallDevToolsProtocolMethodAsync(self, method, params):
                page.devtools.append((method, json.loads(params)))
                if page.emulation_error:
                    raise RuntimeError(page.emulation_error)
                value = json.loads(params)["features"][0]["value"]
                if page.emulation_switches and value:
                    page.show(value)
                return page.task

        class Form:
            def Invoke(self, action):          # the UI thread: runs it there and then
                action()
        self.native = Form()
        self.native.browser = type("Browser", (), {})()
        self.native.browser.webview = type("Control", (), {})()
        self.native.browser.webview.CoreWebView2 = Core()

    def show(self, mode):
        self.media = mode
        if self.styles_follow:
            self.drawn = mode

    def evaluate_js(self, js, callback):
        # The page's answer to the tool's colour-mode scripts.
        if "colorScheme" in js:
            callback({"media": self.media, "drawn": self.drawn})
        else:
            callback(self.media)


def _dotnet():
    """Stand-ins for the .NET names the switch imports (pythonnet is not
    needed): an Action that is the function itself and the scheme values."""
    from types import ModuleType, SimpleNamespace
    system = ModuleType("System")
    system.Action = lambda fn: fn
    core = ModuleType("Microsoft.Web.WebView2.Core")
    core.CoreWebView2PreferredColorScheme = SimpleNamespace(Auto="auto", Light="light",
                                                            Dark="dark")
    return {"System": system, "Microsoft": ModuleType("Microsoft"),
            "Microsoft.Web": ModuleType("Microsoft.Web"),
            "Microsoft.Web.WebView2": ModuleType("Microsoft.Web.WebView2"),
            "Microsoft.Web.WebView2.Core": core}


class ColourSwitchTests(unittest.TestCase):
    """--colour-mode switches the check's own page whatever mode Windows is in
    (#328), and still stops when the page does not show the mode asked for."""

    @classmethod
    def setUpClass(cls):
        # The tool reads the page through the UI check's evaluate. Imported
        # here, outside the patched sys.modules, which would unload numpy.
        import workspace_ui_check  # noqa: F401

    def show(self, page, mode, **kw):
        from unittest import mock
        with mock.patch.dict(sys.modules, _dotnet()):
            return accessibility_check.show_colour_mode(page, mode, **kw)

    def test_light_is_drawn_when_windows_is_dark(self):
        # As on the test laptop: the profile's preferred scheme leaves the page dark.
        page = _Page("dark")
        self.assertEqual("light", self.show(page, "light"))
        self.assertEqual(("light", "light"), (page.media, page.drawn))
        self.assertIn(("Emulation.setEmulatedMedia",
                       {"features": [{"name": "prefers-color-scheme", "value": "light"}]}),
                      page.devtools)

    def test_both_modes_in_turn_on_either_windows_mode(self):
        for windows in ("light", "dark"):
            with self.subTest(windows=windows):
                page = _Page(windows)
                self.assertEqual(["light", "dark"],
                                 [self.show(page, m) for m in ("light", "dark")])

    def test_the_profile_scheme_is_still_used_when_emulation_fails(self):
        page = _Page("dark", profile_switches=True, emulation_error="no DevTools")
        self.assertEqual("light", self.show(page, "light"))
        self.assertIn(("PreferredColorScheme", "light"), page.profile_set)

    def test_a_page_that_does_not_switch_stops_the_run(self):
        page = _Page("dark", emulation_switches=False)
        with self.assertRaisesRegex(RuntimeError, "asked for: light; the page shows: 'dark'") as cm:
            self.show(page, "light", wait=0.3)
        # The error says what each switch did, so a failed run can be diagnosed.
        self.assertIn("(switched by: {'emulation': 'set', 'profile': 'set'})", str(cm.exception))

    def test_a_devtools_call_that_fails_or_never_answers_is_reported(self):
        # _outcome reads the .NET Task off the UI thread: faulted, cancelled
        # and unfinished calls each say so rather than claim the switch was set.
        outcome = accessibility_check._outcome
        faulted = type("Faulted", (_Task,), {"IsFaulted": True,
                                             "Exception": RuntimeError("protocol error")})()
        cancelled = type("Cancelled", (_Task,), {"IsCanceled": True})()
        pending = type("Pending", (_Task,), {"IsCompleted": False})()
        self.assertEqual("set", outcome(_Task(), 0))
        self.assertEqual("failed: protocol error", outcome(faulted, 0))
        self.assertTrue(outcome(cancelled, 0).startswith("failed: "))
        self.assertEqual("no answer", outcome(pending, 0.1))
        # On a page that does not switch, the unanswered call is in the error.
        page = _Page("dark", emulation_switches=False, task=pending)
        with self.assertRaisesRegex(RuntimeError, "'emulation': 'no answer'"):
            self.show(page, "light", wait=0.3)

    def test_styles_that_do_not_follow_the_media_query_stop_the_run(self):
        # The confirmation reads the page's drawn colour scheme too, not only
        # the media query it was told.
        page = _Page("dark", styles_follow=False)
        with self.assertRaisesRegex(RuntimeError, "drawn"):
            self.show(page, "light", wait=0.3)

    def test_the_pc_s_own_mode_changes_nothing(self):
        page = _Page("dark")
        self.assertEqual("dark", self.show(page, None))
        self.assertEqual(([], []), (page.devtools, page.profile_set))

    def test_the_pc_s_own_mode_stops_when_styles_disagree(self):
        # Without --colour-mode the page is read once, with no switch to wait
        # for, and its media query and styles must still agree.
        page = _Page("dark")
        page.drawn = "light"
        with self.assertRaisesRegex(RuntimeError, "asked for: system; the page shows: 'dark' "
                                                  "but its styles are drawn in 'light'"):
            self.show(page, None)
        self.assertEqual(([], []), (page.devtools, page.profile_set))

    def test_windows_own_setting_is_never_written(self):
        source = (ROOT / "tools" / "accessibility_check.py").read_text(encoding="utf-8")
        for name in ("winreg", "AppsUseLightTheme", "SystemParametersInfo"):
            self.assertNotIn(name, source)


class ReportTests(unittest.TestCase):
    ZOOM = {"zoomable": True, "keyboard_zoom": {"before": 1, "zoomed": 1.1, "reset": 1}}
    PAGE = {"lang": "en", "title": "Unminute", "live_status": "polite"}

    def test_a_clean_report_passes(self):
        self.assertEqual([], accessibility_check.findings(
            {**self.ZOOM, "page": self.PAGE, "views": {"meetings": {}}}))

    def test_the_page_language_title_and_status_region_fail(self):
        # Recorded but not failed before #298.
        for page, criterion in (({**self.PAGE, "lang": ""}, "3.1.1"),
                                ({**self.PAGE, "lang": "english please"}, "3.1.1"),
                                ({**self.PAGE, "title": "  "}, "2.4.2"),
                                ({**self.PAGE, "live_status": None}, "4.1.3"),
                                ({**self.PAGE, "live_status": "off"}, "4.1.3"),
                                (None, "3.1.1")):
            with self.subTest(page=page):
                found = accessibility_check.findings({**self.ZOOM, "page": page, "views": {}})
                self.assertTrue(any(f.startswith(criterion + ":") for f in found), found)
        for lang in ("en", "en-NZ", "mi"):
            self.assertEqual([], accessibility_check.page_problems({**self.PAGE, "lang": lang}))

    def test_each_new_finding_fails_its_view(self):
        for key in ("small_targets", "focus_obscured", "low_contrast_focus",
                    "low_contrast_boundary", "text_spacing_cut_off", "keyboard_trap",
                    "label_not_in_name", "no_alt", "change_on_focus"):
            with self.subTest(key=key):
                found = accessibility_check.findings(
                    {**self.ZOOM, "page": self.PAGE, "views": {"meeting": {key: ["button"]}}})
                self.assertEqual([f"meeting: {key} (1)"], found)

    def test_the_locked_screen_fails_unless_audited_with_focus_on_its_heading(self):
        # #111 C4b: the locked screen runs in a process of its own, in the
        # same colour modes; its views fail the run as every other view's do.
        locked = accessibility_check.locked_findings
        views = {"locked screen (light)": {}, "locked screen (dark)": {}}
        self.assertEqual([], locked({"heading_focused": True, "views": views}))
        self.assertEqual(["locked screen: focus did not start on its heading"],
                         locked({"heading_focused": False, "views": views}))
        for report in ({}, {"heading_focused": True, "views": {}},
                       {"heading_focused": True, "views": views, "error": "RuntimeError: x"}):
            with self.subTest(report=report):
                self.assertTrue(locked(report)[0].startswith("locked screen not audited"))
        found = accessibility_check.findings(
            {**self.ZOOM, "page": self.PAGE,
             "views": {**views, "locked screen (dark)": {"low_contrast": ["p 3.10"]}}})
        self.assertEqual(["locked screen (dark): low_contrast (1)"], found)


if __name__ == "__main__":
    unittest.main()
