# tests/test_ui_check_clipboard.py
# tools/workspace_ui_check.py retries a copy step while the Windows clipboard
# is briefly busy (#342). The app gives up on a clipboard another program
# holds after about a second and says so in the copy's status
# (workspace/clipboard.py); concurrent test runs on the same desktop made
# every check fail that way on 23 October 2026. The check now waits and clicks
# again, up to COPY_TRIES clicks, only on that message, and reports the
# retries. A copy that stays busy must still fail the step.
#
# The helper is taken from the tool by ast (importing the tool opens windows)
# and run in Node.js with a stand-in for the click, as
# tests/test_ui_check_focus.py does.
#   python tools/run_tests.py

import json
import re
import shutil
import subprocess
import unittest

from test_ui_check_focus import ROOT, TOOL, tool_parts

CLIPBOARD_PY = (ROOT / "workspace" / "clipboard.py").read_text(encoding="utf-8")
DONE = "Copied the prompt and transcript (1,234 characters) and opened ChatGPT in your browser."


class ClipboardRetryTests(unittest.TestCase):
    """The copy steps retry only a busy clipboard, and only for so long."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.parts = tool_parts("CLIPBOARD_BUSY", "COPY_TRIES", "COPY_GAP_MS", "COPY_RETRY_JS",
                               "handoff_passed")

    def retry(self, statuses):
        """Run copyRetrying with a click that answers `statuses` in turn (the
        last repeats). Returns its result, the clicks and the waits between."""
        if not self.node:
            self.skipTest("Node.js not installed")
        script = f"""
const waits = [];
const wait = async (ms) => {{ waits.push(ms); }};   // no real delay
{self.parts["COPY_RETRY_JS"]}
const answers = {json.dumps(statuses)};
let clicks = 0;
const attempt = async () => {{
  clicks++;
  return answers.length > 1 ? answers.shift() : answers[0]; }};
(async () => {{
  const out = await copyRetrying(attempt);
  console.log(JSON.stringify({{ out, clicks, waits }}));
}})();
"""
        done = subprocess.run([self.node, "-"], input=script, capture_output=True, text=True,
                              timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def handoff(self, open_status):
        """The handoff step's evidence from a run that passed, with `open_status`."""
        return {"has_markers": True, "named_before": True, "named_after": False,
                "redacted": True, "left_out": True, "edited_prompt": True,
                "copy_blocked_until_applied": True, "anonymised": True,
                "open_label": "Copy and open ChatGPT", "open_hidden": False,
                "open_status": open_status}

    def test_busy_twice_then_copied_passes_with_two_retries(self):
        busy = self.parts["CLIPBOARD_BUSY"]
        r = self.retry([busy, busy, DONE])
        self.assertEqual({"status": DONE, "retries": 2}, r["out"])
        self.assertEqual(3, r["clicks"])
        self.assertEqual([self.parts["COPY_GAP_MS"]] * 2, r["waits"])
        h = self.handoff(r["out"]["status"])
        self.assertTrue(self.parts["handoff_passed"](h, ["https://chatgpt.com/"]))

    def test_always_busy_fails_after_the_last_try(self):
        busy = self.parts["CLIPBOARD_BUSY"]
        tries = self.parts["COPY_TRIES"]
        r = self.retry([busy])
        self.assertEqual({"status": busy, "retries": tries - 1}, r["out"])
        self.assertEqual(tries, r["clicks"])
        # About 10 clicks over about 30 s: the step waits 1.5 s after each click.
        self.assertEqual(10, tries)
        total_s = (sum(r["waits"]) + 1500 * tries) / 1000
        self.assertLessEqual(total_s, 35)
        # The busy status reaches the judgement unchanged and fails the step.
        h = self.handoff(r["out"]["status"])
        self.assertFalse(self.parts["handoff_passed"](h, ["https://chatgpt.com/"]))

    def test_any_other_status_is_not_retried(self):
        for status in (DONE, "Already opening. Wait a moment and try again.", "", None):
            with self.subTest(status=status):
                r = self.retry([status])
                self.assertEqual({"status": status, "retries": 0}, r["out"])
                self.assertEqual(1, r["clicks"])
                self.assertEqual([], r["waits"])

    def test_the_busy_message_is_the_apps(self):
        # The check matches the app's own text, so the two cannot drift apart.
        self.assertIn(f'raise ClipboardError("{self.parts["CLIPBOARD_BUSY"]}")', CLIPBOARD_PY)
        self.assertNotIn("'", self.parts["CLIPBOARD_BUSY"])   # quoted in JS with '

    def test_the_handoff_step_retries_both_copies_and_reports_them(self):
        step = re.search(r'report\["handoff"\] = evaluate\(window, (.*?)timeout=(\d+)\)',
                         TOOL, re.S)
        self.assertIsNotNone(step, "handoff step not found in the tool")
        body, timeout = step.group(1), int(step.group(2))
        self.assertTrue(body.startswith("WAIT + COPY_RETRY_JS + "))
        for button in ("ho-copy", "ho-open"):
            with self.subTest(button):
                click = f"document.getElementById('{button}').click()"
                self.assertEqual(1, body.count(click))
                before = body[:body.index(click)]
                self.assertGreater(before.rfind("await copyRetrying("),
                                   before.rfind("});"), f"{button} is clicked outside the helper")
        self.assertIn("copy_retries: copied.retries", body)
        self.assertIn("open_retries: opened.retries", body)
        self.assertIn('if not handoff_passed(report.get("handoff") or {}, '
                      'report.get("handoff_opened")):', TOOL)
        # Both copies busy throughout still finish inside the step's time limit,
        # with room for the step's other waits (about 12 s).
        tries, gap = self.parts["COPY_TRIES"], self.parts["COPY_GAP_MS"]
        worst_s = 2 * (tries * 1.5 + (tries - 1) * gap / 1000) + 12
        self.assertLess(worst_s, timeout)


if __name__ == "__main__":
    unittest.main()
