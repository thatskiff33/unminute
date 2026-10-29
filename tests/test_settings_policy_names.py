# Settings shows the policy value names Unminute does not recognise (#300,
# from #111 D2 finding 8): a mistyped name under the policy key grants and
# withholds nothing, so the page names it for IT to fix. The names come from
# Policy.summary() (printable ASCII, escapes for everything else) and reach the
# page as text only. The page's own unrecognisedNote and renderUnrecognised run
# in Node.js with the page's real scripts loaded and a stand-in for the
# element (tests/page_scripts.py run), as tests/test_level_meter.py does.
#   python tools/run_tests.py

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import keyprotect  # noqa: E402
import page_scripts  # noqa: E402

UI = ROOT / "workspace" / "ui"
APP_JS = page_scripts.source()
INDEX = (UI / "index.html").read_text(encoding="utf-8")
HELPERS = ("unrecognisedNote", "renderUnrecognised")
MANAGED_LINE = ('<p id="s-managed" class="set-text" hidden>Some settings are managed by your '
                'organisation. Ask your IT team if you need one changed.</p>')


# Each case renders into a stand-in element and reports what a reader sees.
SIMULATION = r"""
const els = { "s-unrecognised": { textContent: "stale", hidden: false, innerHTML: "" } };
document = { getElementById: (id) => els[id] };
const cases = {
  one: { managed: [], invalid: [], unrecognised: ["AllowModelDownloads\\x20"] },
  two: { managed: ["AllowGpuComponent"], invalid: [],
         unrecognised: ["AllowModelDownl\\u043eads", "<b>Allow</b>"] },
  empty: { managed: ["AllowGpuComponent"], invalid: [], unrecognised: [] },
  missing: { managed: [], invalid: [] },
  none: null,
  not_a_list: { unrecognised: "AllowModelDownloads" },
  odd_items: { unrecognised: [3, null, "", { a: 1 }, "Real"] },
};
const result = {};
for (const [k, m] of Object.entries(cases)) {
  renderUnrecognised(m);
  const e = els["s-unrecognised"];
  result[k] = { text: e.textContent, hidden: e.hidden, html: e.innerHTML };
  e.textContent = "stale"; e.hidden = false;
}
console.log(JSON.stringify(result));
"""


class UnrecognisedLineTests(unittest.TestCase):
    out = None

    def simulate(self):
        if UnrecognisedLineTests.out is None:             # one run serves every test here
            UnrecognisedLineTests.out = page_scripts.run(SIMULATION)
        return json.loads(json.dumps(UnrecognisedLineTests.out))

    def test_names_are_shown_as_text_with_what_to_do(self):
        r = self.simulate()
        self.assertEqual(r["one"], {
            "text": "Your organisation's policy has a value Unminute does not recognise: "
                    "AllowModelDownloads\\x20. Ask your IT team to check its name.",
            "hidden": False, "html": ""})
        self.assertEqual(r["two"]["text"],
                         "Your organisation's policy has values Unminute does not recognise: "
                         "AllowModelDownl\\u043eads, <b>Allow</b>. "
                         "Ask your IT team to check their names.")
        self.assertFalse(r["two"]["hidden"])
        self.assertEqual(r["two"]["html"], "")        # textContent only, never markup

    def test_hidden_and_empty_when_there_are_none(self):
        r = self.simulate()
        for case in ("empty", "missing", "none", "not_a_list"):
            with self.subTest(case=case):
                self.assertEqual(r[case], {"text": "", "hidden": True, "html": ""})

    def test_only_text_names_are_listed(self):
        r = self.simulate()
        self.assertEqual(r["odd_items"]["text"],
                         "Your organisation's policy has a value Unminute does not recognise: "
                         "Real. Ask your IT team to check its name.")

    def test_page_has_the_line_and_keeps_the_managed_line(self):
        # The managed line is unchanged and still shown only for managed values.
        self.assertTrue(MANAGED_LINE in INDEX, "the managed line changed")
        self.assertTrue('$("s-managed").hidden = !(s.managed && s.managed.managed.length);'
                        in APP_JS, "the managed line is no longer shown as before")
        # The new line is empty and hidden in the markup, straight after it.
        self.assertTrue(MANAGED_LINE + '\n    <p id="s-unrecognised" class="set-text" hidden></p>'
                        in INDEX, "no empty, hidden s-unrecognised line after the managed line")
        render = page_scripts.function_text("renderSettings")
        self.assertTrue(render.startswith("async function renderSettings() {"), render[:40])
        self.assertIn("renderUnrecognised(s.managed);", render)
        for name in HELPERS:
            self.assertNotIn("innerHTML", page_scripts.function_text(name))
            self.assertNotIn("insertAdjacentHTML", page_scripts.function_text(name))
        # A name has no spaces (they show as \x20), so the line may break
        # inside one rather than run off a narrow window.
        css = (UI / "app.css").read_text(encoding="utf-8")
        self.assertTrue(re.search(r"#s-unrecognised \{[^}]*overflow-wrap: anywhere;", css),
                        "the line does not wrap inside a long name")


class SettingsCarriesTheNamesTests(unittest.TestCase):
    """The bridge's settings answer carries the shown names, never the data."""

    def open(self, reader):
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                       allow_test_engines=True, policy_reader=reader,
                       protector=keyprotect.TestProtector())
        self.addCleanup(ws.close)
        return ws

    def test_settings_lists_unrecognised_names_escaped(self):
        ws = self.open(lambda: ({}, (), ("AllowModelDownloads ", "AllowModelDownl\u043eads")))
        managed = ws.get_settings()["managed"]
        self.assertEqual(managed["unrecognised"],
                         ["AllowModelDownl\\u043eads", "AllowModelDownloads\\x20"])
        self.assertEqual(managed["managed"], [])            # nothing granted or withheld
        self.assertTrue(managed["allow"]["AllowModelDownloads"])

    def test_settings_without_unrecognised_names(self):
        ws = self.open(lambda: ({"AllowGpuComponent": False}, ()))
        managed = ws.get_settings()["managed"]
        self.assertEqual(managed["unrecognised"], [])
        self.assertEqual(managed["managed"], ["AllowGpuComponent"])


if __name__ == "__main__":
    unittest.main()
