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

RUNNER = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Fixtures</title>
<script>
const AUDIT = %s, FIXTURES = %s, BASE = %s;
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
window.addEventListener('load', async () => {
  const out = {};
  for (const [name, fragment] of Object.entries(FIXTURES)) {
    document.body.innerHTML = BASE + fragment;
    window.scrollTo(0, 0);
    document.body.getBoundingClientRect();
    await new Promise(r => setTimeout(r, 50));
    try { out[name] = await new AsyncFunction(AUDIT)(); }
    catch (e) { out[name] = {error: String(e)}; }
  }
  document.body.innerHTML = '';
  const pre = document.createElement('pre');
  pre.id = 'result';
  pre.textContent = JSON.stringify(out);
  document.body.append(pre);
});
</script></head><body></body></html>"""


def audit_fixtures(fixtures):
    """The audit's result for each fragment, from headless Edge."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "fixtures.html"
        page.write_text(RUNNER % (json.dumps(accessibility_check.AUDIT), json.dumps(fixtures),
                                  json.dumps(BASE)), encoding="utf-8")
        profile = Path(tmp) / "profile"
        cmd = [str(EDGE), "--headless=new", "--disable-gpu", "--no-first-run",
               "--disable-extensions", f"--user-data-dir={profile}", "--mute-audio",
               "--force-device-scale-factor=1", "--window-size=1100,760",
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
        cls.results = audit_fixtures(FIXTURES)

    def result(self, name):
        r = self.results[name]
        self.assertNotIn("error", r, r.get("error"))
        return r

    def flagged(self, name, key):
        return self.result(name)[key]

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
        # The spacing is taken off again afterwards.
        self.assertEqual([], self.flagged("clean", "text_spacing_cut_off"))

    def test_keyboard_trap(self):
        self.assertEqual(["button"], self.flagged("swallows_tab", "keyboard_trap"))
        self.assertEqual(["input"], self.flagged("pulls_back", "keyboard_trap"))
        # Handling Tab is fine when focus moves on.
        self.assertEqual([], self.flagged("moves_on_tab", "keyboard_trap"))

    def test_label_in_name(self):
        self.assertEqual(["button", "input#s"], self.flagged("name_differs", "label_not_in_name"))
        # Case and punctuation aside; a symbol is not a label.
        self.assertEqual([], self.flagged("name_contains", "label_not_in_name"))

    def test_images_need_a_text_alternative(self):
        self.assertEqual(["img"], self.flagged("image_no_alt", "no_alt"))
        self.assertEqual([], self.flagged("image_alt", "no_alt"))

    def test_focus_opens_nothing(self):
        self.assertEqual(["button"], self.flagged("opens_on_focus", "change_on_focus"))


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


if __name__ == "__main__":
    unittest.main()
