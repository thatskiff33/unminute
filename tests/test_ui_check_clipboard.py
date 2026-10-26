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
                               "COPY_READ_MS", "COPY_POLL_MS", "TEXT_AFTER_JS",
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
        # Each copy's status is read after the app's busy answer can arrive,
        # and both copies busy throughout still finish inside the step's time
        # limit, with room for the step's other waits (about 12 s). The waits
        # are read from the tool, as the locked screen's are (#318 review).
        tries, gap = self.parts["COPY_TRIES"], self.parts["COPY_GAP_MS"]
        worst_s = 12
        for button in ("ho-copy", "ho-open"):
            with self.subTest(button):
                read = re.search(rf"{button}'\)\.click\(\); await wait\((\d+)\);", body)
                self.assertIsNotNone(read, f"no wait after the {button} click")
                read_ms = int(read.group(1))
                self.assertGreaterEqual(read_ms, busy_ms() + 400)
                worst_s += (tries * read_ms + (tries - 1) * gap) / 1000
        self.assertLess(worst_s, timeout)

    def test_the_locked_screen_copy_retries_and_reports_it(self):
        # The locked screen's "Copy details for IT" puts the diagnostics on
        # the same clipboard, so it is retried the same way (#318 review).
        step = re.search(r'copied = evaluate\(window, (.*?)timeout=(\d+)\)', TOOL, re.S)
        self.assertIsNotNone(step, "locked screen copy step not found in the tool")
        body, timeout = step.group(1), int(step.group(2))
        self.assertTrue(body.startswith("WAIT + COPY_RETRY_JS + TEXT_AFTER_JS + "))
        click = "document.getElementById('locked-copy').click();"
        self.assertEqual(1, TOOL.count(click))
        self.assertEqual(1, body.count(click))
        before, after = body[:body.index(click)], body[body.index(click) + len(click):]
        helper = before.rfind("await copyRetrying(")
        self.assertGreater(helper, -1, "locked-copy is clicked outside the helper")
        # Each click starts from an empty status, so a slow answer is not read
        # as the last click's.
        self.assertIn("document.getElementById('status').textContent = '';", before[helper:])
        self.assertIn('report["copied"] = copied.get("status")', TOOL)
        self.assertIn('report["copy_retries"] = copied.get("retries")', TOOL)
        # The status is read as soon as the click's answer appears, not after
        # a fixed wait (#318 review, round 6): the click is followed at once
        # by textAfter on the status.
        self.assertTrue(after.lstrip().startswith(
            "return await textAfter(() => document.getElementById('status').textContent);"),
            "the locked-copy status is not read with textAfter")
        # textAfter waits long enough for the app's busy answer: the app tries
        # to open the clipboard 20 times, 50 ms apart (about 1 s), before it
        # says the clipboard is busy. Read sooner and the cleared status is
        # empty, so the retry never fires (#318 review, round 5). The 400 ms
        # leaves room for building the diagnostics and the page's round trip.
        read_ms = self.parts["COPY_READ_MS"]
        self.assertGreaterEqual(read_ms, busy_ms() + 400)
        # A copy busy throughout still finishes inside the step's time limit.
        tries, gap = self.parts["COPY_TRIES"], self.parts["COPY_GAP_MS"]
        self.assertLess(tries * read_ms / 1000 + (tries - 1) * gap / 1000 + 5, timeout)

    def text_after(self, appears_after):
        """Run textAfter on a status that gets its text after `appears_after`
        polls (None: never). Returns its result and the waits it made."""
        if not self.node:
            self.skipTest("Node.js not installed")
        script = f"""
const waits = [];
const wait = async (ms) => {{ waits.push(ms); }};   // no real delay
{self.parts["TEXT_AFTER_JS"]}
const appearsAfter = {json.dumps(appears_after)};
const shown = () => appearsAfter !== null && waits.length >= appearsAfter;
const read = () => (shown() ? {json.dumps(DONE)} : '');
(async () => {{
  const out = await textAfter(read);
  console.log(JSON.stringify({{ out, waits }}));
}})();
"""
        done = subprocess.run([self.node, "-"], input=script, capture_output=True, text=True,
                              timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_status_is_read_as_soon_as_it_appears(self):
        poll = self.parts["COPY_POLL_MS"]
        for polls in (0, 1, 12):
            with self.subTest(polls=polls):
                r = self.text_after(polls)
                self.assertEqual(DONE, r["out"])
                self.assertEqual([poll] * polls, r["waits"])

    def test_a_status_that_never_appears_reads_empty_after_the_limit(self):
        # Empty, so the step fails as "locked screen Copy details for IT",
        # after no more than COPY_READ_MS.
        r = self.text_after(None)
        self.assertEqual("", r["out"])
        self.assertEqual(self.parts["COPY_READ_MS"], sum(r["waits"]))
        self.assertTrue(set(r["waits"]) <= {self.parts["COPY_POLL_MS"]})


def busy_ms():
    """How long the app tries to open a busy clipboard before saying so, in
    ms, from workspace/clipboard.py's _open (20 tries 50 ms apart today)."""
    opens = re.search(r"def _open\(user32\):\n    for _ in range\((\d+)\):.*?"
                      r"time\.sleep\(([\d.]+)\)", CLIPBOARD_PY, re.S)
    assert opens, "the app's clipboard wait not found in workspace/clipboard.py"
    return int(opens.group(1)) * float(opens.group(2)) * 1000


if __name__ == "__main__":
    unittest.main()
