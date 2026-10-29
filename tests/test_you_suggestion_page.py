# tests/test_you_suggestion_page.py
# The "You" suggestion in the Name speakers panel (#108 slice 5), follow-ups
# from the review of #458 (#461):
#   - the suggestion row follows what is typed: a name typed for the
#     suggested speaker, or "You" typed for anyone, hides it at once (as the
#     panel would after a rebuild), so Use 'You' can never replace a name
#     being typed; putting the box back as it was shows it again;
#   - after Use 'You', the panel is rebuilt without the button, and focus
#     goes to that speaker's name box (now "You"), not to the page;
#   - after More examples, the rebuilt panel no longer has the button that
#     was pressed, and focus goes to that speaker's new paging button, not to
#     the page (#475, from the review of #476);
#   - if the rebuild after Use 'You' fails, the focus it was to set is
#     dropped, so a later rebuild (More examples) does not move focus to a
#     name box (#471 review nit);
#   - the accessibility check's own focus steps (tools/accessibility_check.py,
#     YOU_FOCUS, #475) pass on the page.
#
# The real page runs in headless Microsoft Edge with the design check's fake
# bridge (tools/design_check/fixtures.js, the "names-you" scenario) and
# synthetic speaker names, wrapped here so the test controls the panel and
# sees each rename asked for: no app window, workspace or audio. The
# real-window accessibility run (tools/accessibility_check.py) is separate.
#   python tools/run_tests.py --only test_you_suggestion_page

import html
import http.server
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import accessibility_check  # noqa: E402
import design_check  # noqa: E402

EDGE = next((p for p in design_check.EDGE if p.is_file()), None)

# Loaded after fixtures.js and before the page's scripts: speaker_panel answers from the
# test's names, suggesting SPEAKER_00 as the app would (a default name, and
# no one called "You"), and rename_speaker changes them and is recorded.
BRIDGE = """
(() => {
  const real = window.pywebview.api;
  const t = window.__you = { names: { SPEAKER_00: "Speaker 1", SPEAKER_01: "Speaker 2" },
                             renames: [], failPanel: 0, failed: 0 };
  const suggested = () => {
    const names = Object.values(t.names);
    return /^Speaker [0-9]+$/.test(t.names.SPEAKER_00)
      && !names.some((n) => n.trim().toLowerCase() === "you") ? "SPEAKER_00" : null;
  };
  window.pywebview = { api: new Proxy({}, { get: (_, method) => async (...args) => {
    if (method === "rename_speaker") {
      t.renames.push([args[1], args[2]]);
      t.names[args[1]] = args[2];
      return { ok: true, value: null };
    }
    if (method === "speaker_panel" && t.failPanel > 0) {    // the next panels fail
      t.failPanel -= 1;
      t.failed += 1;
      return { ok: false, error: "The request failed." };
    }
    const r = await real[method](...args);
    if (method === "speaker_panel" && r.ok) {
      const speakers = r.value.speakers.map((sp) => ({ ...sp, name: t.names[sp.label] }));
      return { ok: true, value: { ...r.value, you: suggested(), speakers } };
    }
    return r;
  } }) };
})();
"""

# Run after the page's scripts, once the scenario has opened the panel: the steps, then
# the observations as JSON in <pre id="you-result">.
STEPS = """
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const t = window.__you;
  const boxes = () => [...document.querySelectorAll("#n-list .n-label input")];
  const row = () => document.querySelector("#n-list .n-you");
  const shown = () => !!row() && row().getClientRects().length > 0;
  const type = (box, value) => {
    box.value = value;
    box.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const errors = [];
  window.addEventListener("error", (e) => errors.push(String(e.message || e)));
  window.addEventListener("unhandledrejection", (e) => errors.push(String(e.reason)));
  while (document.documentElement.dataset.ready !== "1") await wait(100);
  await wait(500);
  const out = {};
  try {
    out.open = document.getElementById("names").open;
    out.first = shown();
    // A name typed for the suggested speaker, then put back.
    type(boxes()[0], "Aroha");
    out.typed_name = shown();
    type(boxes()[0], "Speaker 1");
    out.put_back = shown();
    // "You" typed for the other speaker, then put back.
    type(boxes()[1], " you ");
    out.other_you = shown();
    type(boxes()[1], "Speaker 2");
    out.other_back = shown();
    // Use 'You', from the keyboard's point of view: the button has focus.
    const use = row().querySelector("button");
    use.focus();
    use.click();
    await wait(1500);
    const now = document.activeElement;
    out.after = { shown: shown(), renames: t.renames.slice(),
                  focus_box: boxes().indexOf(now),
                  focus_value: now && typeof now.value === "string" ? now.value : null,
                  in_dialog: !!(now && now.closest("#names")) };
    // More examples, from the keyboard: the rebuild replaces the button, and
    // focus goes to that speaker's new one, not to the page (#475).
    t.names.SPEAKER_00 = "Speaker 1";
    const first = document.querySelector("#n-list .n-more");
    first.focus();
    first.click();
    await wait(1500);
    const was = document.activeElement;
    out.more = { replaced: !first.isConnected,
                 paging: was.classList.contains("n-more"),
                 speaker: boxes().indexOf(was.closest(".speaker")?.querySelector(".n-label input")),
                 in_dialog: !!was.closest("#names") };
    const again = shown();
    t.failPanel = 1;
    row().querySelector("button").click();
    await wait(1500);
    const more = document.querySelector("#n-list .n-more");
    more.focus();
    more.click();
    await wait(1500);
    const now2 = document.activeElement;
    out.failed = { again, failed: t.failed, renames: t.renames.length, rebuilt: !shown(),
                   focus_box: boxes().indexOf(now2),
                   focus_more: now2.classList.contains("n-more")
                     && now2.closest(".speaker") === boxes()[0].closest(".speaker") };
  } catch (e) {
    out.error = String((e && e.stack) || e);
  }
  out.errors = errors;
  const pre = document.createElement("pre");
  pre.id = "you-result";
  pre.textContent = JSON.stringify(out);
  document.body.append(pre);
})();
"""


# The accessibility check's own focus steps (tools/accessibility_check.py,
# YOU_FOCUS, #475), run as that check runs them once the panel is open; its
# answer as JSON in <pre id="you-result">.
FOCUS_STEPS = """
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const errors = [];
  window.addEventListener("error", (e) => errors.push(String(e.message || e)));
  window.addEventListener("unhandledrejection", (e) => errors.push(String(e.reason)));
  while (document.documentElement.dataset.ready !== "1") await wait(100);
  await wait(500);
  let out;
  try {
    const open = document.getElementById("names").open;
    out = { open, focus: await (async () => { YOU_FOCUS })() };
  } catch (e) {
    out = { error: String((e && e.stack) || e) };
  }
  out.errors = errors;
  const pre = document.createElement("pre");
  pre.id = "you-result";
  pre.textContent = JSON.stringify(out);
  document.body.append(pre);
})();
""".replace("YOU_FOCUS", accessibility_check.YOU_FOCUS)


class Handler(design_check.Handler):
    """The design check's server, with the wrapper before the page's scripts and
    the steps after them (the page's content security policy allows scripts from the
    page's own origin only)."""
    steps = STEPS

    def do_GET(self):
        path = urlsplit(self.path).path.lstrip("/")
        if path == "app/you-bridge.js":
            return self._send(BRIDGE.encode("utf-8"), "text/javascript; charset=utf-8")
        if path == "app/you-steps.js":
            return self._send(self.steps.encode("utf-8"), "text/javascript; charset=utf-8")
        if path in ("app/", "app/index.html"):
            page = (design_check.UI / "index.html").read_text(encoding="utf-8")
            page = page.replace(design_check.APP_TAG,
                                '<script src="/tools/design_check/fixtures.js"></script>\n'
                                '<script src="you-bridge.js"></script>\n'
                                + design_check.APP_TAG)
            page = page.replace(design_check.MAIN_TAG,
                                design_check.MAIN_TAG + '\n<script src="you-steps.js"></script>')
            return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        return super().do_GET()


class Server(http.server.ThreadingHTTPServer):
    # Edge asks for the page's files at once: a longer queue than the default
    # five, so no connection is refused (as tests/test_ui_page_audit.py).
    request_queue_size = 64
    daemon_threads = True


def run_page(base):
    url = f"{base}/app/index.html#names-you"
    # Headless Edge now and then never finishes a page, or loses one of its
    # files: one more try, with a fresh profile (as tests/test_mic_list_refresh.py).
    problem = None
    for _attempt in (1, 2):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            cmd = [str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                   "--no-first-run", "--disable-extensions", f"--user-data-dir={profile}",
                   "--mute-audio", "--force-device-scale-factor=1", "--window-size=1100,760",
                   "--virtual-time-budget=30000", "--dump-dom", url]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=120)
            except subprocess.TimeoutExpired:
                problem = "Edge did not finish in 120 seconds"
                continue
        found = re.search(r'<pre id="you-result">(.*?)</pre>', out.stdout or "", re.DOTALL)
        if found:
            return json.loads(html.unescape(found.group(1)))
        problem = (f"Edge returned no result (exit {out.returncode}): "
                   f"{(out.stderr or '').strip()[-300:]}")
    return {"error": f"twice: {problem}"}


def serve_and_run(handler):
    server = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return run_page(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()


@unittest.skipUnless(EDGE, "Microsoft Edge not installed")
class YouSuggestionPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = serve_and_run(Handler)

    def setUp(self):
        self.assertNotIn("error", self.r, self.r.get("error"))
        self.assertEqual([], self.r["errors"], "the page threw")
        self.assertTrue(self.r["open"], "the panel did not open: nothing was tested")
        self.assertTrue(self.r["first"], "no suggestion was shown: nothing was tested")

    def test_a_name_typed_for_the_suggested_speaker_hides_the_suggestion(self):
        self.assertFalse(self.r["typed_name"])
        self.assertTrue(self.r["put_back"])

    def test_you_typed_for_another_speaker_hides_the_suggestion(self):
        self.assertFalse(self.r["other_you"])
        self.assertTrue(self.r["other_back"])

    def test_use_you_names_the_speaker_and_keeps_focus_on_their_name(self):
        after = self.r["after"]
        self.assertEqual([["SPEAKER_00", "You"]], after["renames"])
        self.assertFalse(after["shown"], "the suggestion stayed after Use 'You'")
        self.assertEqual((0, "You", True),
                         (after["focus_box"], after["focus_value"], after["in_dialog"]),
                         "focus was lost when the button went")

    def test_a_failed_rebuild_after_use_you_drops_the_pending_focus(self):
        failed = self.r["failed"]
        self.assertTrue(failed["again"], "no suggestion the second time: nothing was tested")
        self.assertEqual((1, 2, True), (failed["failed"], failed["renames"], failed["rebuilt"]),
                         "the rebuild did not fail, or the later one did not happen")
        self.assertEqual(-1, failed["focus_box"],
                         "a later rebuild moved focus to a name box: the failed rebuild's "
                         "pending focus was kept")
        self.assertTrue(failed["focus_more"],
                        "after More examples focus is not on that speaker's button")

    def test_more_examples_keeps_focus_on_that_speakers_paging_button(self):
        more = self.r["more"]
        self.assertTrue(more["replaced"], "the button was not rebuilt: nothing was tested")
        self.assertEqual((True, 0, True), (more["paging"], more["speaker"], more["in_dialog"]),
                         "focus fell out of the panel when the rebuild replaced the button")


class FocusHandler(Handler):
    steps = FOCUS_STEPS


@unittest.skipUnless(EDGE, "Microsoft Edge not installed")
class AccessibilityFocusStepsTests(unittest.TestCase):
    """tools/accessibility_check.py's YOU_FOCUS on the real page (#475): its
    answer passes you_focus_problems, as the real-window run must. Headless
    Edge treats its page as focused; the real window is checked by the
    accessibility check itself, with release evidence."""

    @classmethod
    def setUpClass(cls):
        cls.r = serve_and_run(FocusHandler)

    def test_the_checks_focus_steps_pass_on_the_page(self):
        self.assertNotIn("error", self.r, self.r.get("error"))
        self.assertEqual([], self.r["errors"], "the page threw")
        self.assertTrue(self.r["open"], "the panel did not open: nothing was tested")
        focus = self.r["focus"]
        self.assertEqual("More examples", (focus.get("paging") or [{}])[0].get("pressed"), focus)
        # The paging ends on First examples, so focus after it is checked too
        # (#479): the fixture keeps the offsets, as the app does.
        self.assertEqual("First examples", (focus.get("paging") or [{}])[-1].get("pressed"),
                         focus)
        self.assertEqual([], accessibility_check.you_focus_problems(focus), focus)


if __name__ == "__main__":
    unittest.main()
