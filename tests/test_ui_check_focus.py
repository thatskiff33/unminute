# tests/test_ui_check_focus.py
# tools/workspace_ui_check.py ends a transcript edit whether or not its window
# has focus (#308). Chromium moves document.activeElement on focus() and
# blur() whatever the window, but sends the focus and blur events only while
# the page has focus, and a check started from a terminal often cannot bring
# its window to the front. The page ends an edit on the span's blur event
# (startEditing in workspace/ui/app.js), so a bare blur() in an unfocused
# window left the edit open and every poll skipped the transcript.
#
# The step's script is taken from the tool by ast (importing the tool opens
# windows) and run in Node.js against a small stand-in for those Chromium
# rules, with the page's listener wired as app.js wires it. The CI images
# have Node.js, as tests/test_level_meter.py notes.
#   python tools/run_tests.py

import ast
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = (ROOT / "tools" / "workspace_ui_check.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
# The script the after_edit step ran before #308.
OLD_END_EDIT = "g.span.blur(); await wait(1500);"


def tool_parts(*names):
    """Top-level constants and functions of the tool, by name, without
    importing it."""
    tree = ast.parse(TOOL)
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name in names)
              or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names
                                                    for t in n.targets))]
    space = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "workspace_ui_check", "exec"), space)
    missing = [n for n in names if n not in space]
    assert not missing, f"not in the tool: {missing}"
    return space


# Chromium's rules, as far as the step depends on them (Blink's
# Element::focus and Element::blur, Document::SetFocusedElement): the focused
# element moves either way; the events are sent only while the page has focus.
PAGE = r"""
globalThis.FocusEvent = class FocusEvent extends Event {};
class Element extends EventTarget {
  focus() {
    const old = document.activeElement;
    if (old === this) return;
    document.activeElement = this;
    if (document.hasFocus()) {
      if (old) old.dispatchEvent(new FocusEvent('blur'));
      this.dispatchEvent(new FocusEvent('focus'));
    }
  }
  blur() {
    if (document.activeElement !== this) return;
    document.activeElement = null;
    if (document.hasFocus()) this.dispatchEvent(new FocusEvent('blur'));
  }
}
const wait = () => Promise.resolve();      // the tool's waits do not matter here
async function run(windowFocused, step) {
  globalThis.document = { activeElement: null, hasFocus: () => windowFocused };
  // The page's edit, as startEditing and stopEditing in app.js wire it.
  const page = { editing: null, stops: 0 };
  const stopEditing = () => {
    if (!page.editing) return;
    page.editing = null;
    page.stops++;
  };
  const span = new Element();
  page.editing = span;
  span.focus();
  span.addEventListener('blur', stopEditing, { once: true });
  const editing = page.editing === span && document.activeElement === span;
  const focused = await step({ span });
  return { editing_before: editing, ended: page.editing === null, stops: page.stops,
           focused, active_cleared: document.activeElement === null };
}
"""


def step_function(blur_js, first):
    """The step's script as grow_update runs it (GROW_JS holds BLUR_JS, and
    `focused` is set before `first`), returning the `focused` it reports."""
    return (f"async (g) => {{ {blur_js}\n  let focused = document.hasFocus();\n"
            f"  {first}\n  return focused; }}")


class UnfocusedWindowTests(unittest.TestCase):
    """The after_edit step ends the edit whether or not the window has focus."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.parts = tool_parts("BLUR_JS", "GROW_END_EDIT", "GROW_FIRST", "GROW_MORE",
                               "growing_problems")

    def run_step(self, first):
        if not self.node:
            self.skipTest("Node.js not installed")
        step = step_function(self.parts["BLUR_JS"], first)
        script = PAGE + f"""
const step = {step};
(async () => {{
  const out = {{ unfocused: await run(false, step), focused: await run(true, step) }};
  console.log(JSON.stringify(out));
}})();
"""
        done = subprocess.run([self.node, "-"], input=script, capture_output=True, text=True,
                              timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_stand_in_shows_the_flake_with_a_bare_blur(self):
        # The cause in #308, so the stand-in does not pass the step by itself:
        # unfocused, a bare blur() clears the focus but the edit stays open.
        out = self.run_step(OLD_END_EDIT)
        self.assertTrue(out["unfocused"]["editing_before"])
        self.assertTrue(out["unfocused"]["active_cleared"])
        self.assertEqual(0, out["unfocused"]["stops"])
        self.assertFalse(out["unfocused"]["ended"])
        self.assertEqual(1, out["focused"]["stops"])

    def test_the_edit_ends_once_without_window_focus(self):
        out = self.run_step(self.parts["GROW_END_EDIT"])["unfocused"]
        self.assertTrue(out["editing_before"])
        self.assertTrue(out["ended"], "the page's stopEditing did not run")
        self.assertEqual(1, out["stops"])
        self.assertTrue(out["active_cleared"])
        self.assertIs(False, out["focused"], "the step must report the window had no focus")

    def test_the_edit_ends_once_with_window_focus(self):
        out = self.run_step(self.parts["GROW_END_EDIT"])["focused"]
        self.assertTrue(out["ended"])
        self.assertEqual(1, out["stops"], "the page's stopEditing ran more than once")
        self.assertIs(True, out["focused"])

    def blur_events(self, focus_sequence, calls):
        """Blur events the span receives when blurLikeFocused runs `calls`
        times while document.hasFocus() answers `focus_sequence` in turn
        (the last answer repeats)."""
        if not self.node:
            self.skipTest("Node.js not installed")
        script = PAGE + f"""
{self.parts["BLUR_JS"]}
const answers = {json.dumps(focus_sequence)};
globalThis.document = {{ activeElement: null,
  hasFocus: () => answers.length > 1 ? answers.shift() : answers[0] }};
const span = new Element();
document.activeElement = span;
let blurs = 0;
span.addEventListener('blur', () => blurs++);
for (let i = 0; i < {calls}; i++) blurLikeFocused(span);
console.log(JSON.stringify(blurs));
"""
        done = subprocess.run([self.node, "-"], input=script, capture_output=True, text=True,
                              timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_helper_sends_exactly_one_blur(self):
        # Like a focused window: one blur for the focused element, whatever
        # the window's focus, and none once nothing is focused. The window
        # gaining focus between the helper's check and blur() (the native
        # event then fires) must not add a second, nor losing it leave none.
        cases = {"unfocused": ([False], 1), "focused": ([True], 1),
                 "called again once nothing is focused": ([False], 3),
                 "the window gains focus mid-call": ([False, True], 1),
                 "the window loses focus mid-call": ([True, False], 1)}
        for name, (sequence, calls) in cases.items():
            with self.subTest(name):
                self.assertEqual(1, self.blur_events(sequence, calls))

    def test_the_page_listener_runs_once(self):
        # A second guard behind the helper's one-blur rule: a repeated blur
        # cannot end the edit twice.
        start = re.search(r"^function startEditing\(span\) \{.*?^\}$", APP_JS, re.M | re.S)
        self.assertIsNotNone(start, "startEditing not found in app.js")
        self.assertIn('span.addEventListener("blur", stopEditing, { once: true });',
                      start.group(0))
        stop = re.search(r"^function stopEditing\(\) \{.*?^\}$", APP_JS, re.M | re.S)
        self.assertIsNotNone(stop, "stopEditing not found in app.js")
        self.assertIn("if (!span) return;", stop.group(0))

    def test_the_check_uses_the_helper_and_reports_focus(self):
        self.assertIn("first=GROW_END_EDIT", TOOL)
        self.assertIn("return {{ok, focused,", TOOL)
        # Every blur() the check relies on goes through the helper. The
        # settings step's sel.blur() only moves document.activeElement, which
        # works without focus (the page reads activeElement there, not events).
        blur_js = self.parts["BLUR_JS"]
        rest = TOOL.replace(blur_js, "")
        self.assertEqual(["sel.blur()"], re.findall(r"[\w.]+\.blur\(\)", rest))
        self.assertIn("blurLikeFocused(span)", rest)               # edit_autosave
        self.assertIn("out.focused = document.hasFocus();", rest)  # settings

    def test_a_failure_says_the_window_had_no_focus(self):
        problems = self.parts["growing_problems"]
        more = self.parts["GROW_MORE"]
        good = {"ok": True, "redrawn": False, "added": more, "focused": True}
        steps = {n: dict(good) for n in ("append_1", "append_2", "after_undo", "later_chunk")}
        stuck = dict(good, ok=False, added=0, focused=False)
        found = problems(dict(steps, after_edit=stuck))
        self.assertIn("after_edit: new passages were not appended (the window had no focus)",
                      found)
        found = problems(dict(steps, after_edit=dict(stuck, focused=True)))
        self.assertIn("after_edit: new passages were not appended", found)
        self.assertNotIn("after_edit: new passages were not appended (the window had no focus)",
                         found)


if __name__ == "__main__":
    unittest.main()
