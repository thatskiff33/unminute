# tests/test_ui_page_audit.py
# The page itself against tools/accessibility_check.py's audit (#298), in
# headless Microsoft Edge with the design check's fake bridge and synthetic
# sample data (tools/design_check/fixtures.js): no app window, workspace or
# real content. The real-window tool remains the release check (section 12 of
# docs/RELEASE_STANDARD.md); this keeps the views it found wanting from
# drifting back between its runs:
#   - every focusable control shows a focus indicator (2.4.7) at 3:1 (1.4.11),
#     including on the pink of a hold and of a passage left out, on Meetings
#     and Settings as well;
#   - text reaches 4.5:1 (1.4.3): "Optional" on the current setup step, and
#     no text finding in the setup steps, the meeting or the hand off page;
#   - the tick on a done step is a graphic, not text (the owner's decision of
#     25 October 2026 on #298): drawn by the style, with no text of its own,
#     aria-hidden, at 3:1 on the brand green (1.4.11);
#   - pointer targets are 24 by 24 CSS px or spaced (2.5.8): the back links,
#     the transcript's times and passages, the template links and the
#     "Replace names" check box; and nothing covers part of a back link's
#     target (the meeting title's border box and the hand-off page's heading
#     reach into it), which the audit's box measurement cannot see;
#   - accessible names contain the visible label (2.5.3): playback speed,
#     the speaker names dialog and the passages to leave out;
#   - field boundaries reach 3:1 (1.4.11) in every view, except where a named
#     owner exception in tools/accessibility_check.py (OWNER_EXCEPTIONS; the
#     owner's decision of 25 October 2026 on #298) covers the finding: a
#     border in the brand --control-line colour, and the meeting title at
#     rest, which shows no border until hovered (at 1280, 640 and 320 px); the
#     title's box is tall enough for its text with the border;
#   - nothing that only a pointer can use (2.1.1): the seek bar's fill.
# A finding an owner exception covers is not a failure here, as in the tool;
# any other finding of the same check is.

import contextlib
import html
import io
import http.server
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import accessibility_check  # noqa: E402
import design_check  # noqa: E402

EDGE = next((p for p in design_check.EDGE if p.is_file()), None)

# Each view: the fixtures.js scenario, script run once it is ready, the window
# width, and the audit lists that must be empty.
FOCUS = ["focus_not_measured", "no_focus_ring", "low_contrast_focus", "low_contrast_boundary"]
TITLE = ["pointer_only"]
# The views whose sample shows a done step's tick.
TICKS = ("setup speaker names", "setup microphone", "meeting", "handoff page")
VIEWS = {
    # The real-window run of 23 October 2026 reported no_focus_ring on these
    # two (and on the meeting and hand off views); none is found headless.
    "meetings": ("home", "", 1100, FOCUS),
    "settings": ("settings", "", 1100, FOCUS),
    "setup speaker names": ("setup-3", "", 1100, ["low_contrast"] + FOCUS),
    "setup microphone": ("setup-4", "", 1100, ["low_contrast"] + FOCUS),
    "meeting": ("meeting", "", 1280,
                ["low_contrast", "small_targets", "label_not_in_name"] + TITLE + FOCUS),
    "meeting 640": ("meeting", "", 640, TITLE + FOCUS),
    "meeting 320": ("meeting", "", 320, TITLE + FOCUS),
    "names dialog": ("names", "", 1100, ["low_contrast", "label_not_in_name"] + TITLE + FOCUS),
    "handoff page": ("handoff", "const i = document.querySelector('#ho-passages input');"
                     "i.checked = true; i.dispatchEvent(new Event('change', {bubbles: true}));",
                     1100, ["low_contrast", "small_targets", "label_not_in_name"] + FOCUS),
}

# Narrow widths load the page in a frame of exactly that width (design_check's
# frame.html): Edge will not make a window narrower than about 500 px, and its
# page is a little narrower than the window.
FRAMED = (640, 320)

RUNNER = """
(async () => {
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  const EXTRA = {%s};
  const AUDIT = async () => {%s};
  while (document.documentElement.dataset.ready !== '1') await wait(100);
  await wait(500);
  const view = new URLSearchParams(location.search).get('view');
  if (EXTRA[view]) EXTRA[view]();
  await wait(300);
  let out;
  try { out = await AUDIT(); } catch (e) { out = {error: String(e)}; }
  out.has_focus = document.hasFocus();
  out.width = innerWidth;
  // The meeting title's box holds its text, border included (no line cut off).
  const title = document.getElementById('m-title');
  out.title_fits = title.scrollHeight <= title.clientHeight;
  // Each shown done step's tick: text of its own (there should be none), its
  // generated content, whether it is hidden from assistive technology, and
  // the tick's contrast on the marker's fill (WCAG 2.2 luminance).
  const lum = (c) => { const [r, g, b] = c.match(/[0-9.]+/g).slice(0, 3).map((v) => {
    v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  out.done_ticks = [...document.querySelectorAll('.done-tick')].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !el.closest('[hidden]');
  }).map((el) => {
    const fg = lum(getComputedStyle(el, '::before').color);
    const bg = lum(getComputedStyle(el).backgroundColor);
    return {cls: el.className, text: el.textContent, hidden: !!el.closest('[aria-hidden="true"]'),
            content: getComputedStyle(el, '::before').content,
            ratio: (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05)};
  });
  // Each shown back link answers a pointer over its whole 24 px target: no
  // later element (such as the meeting title's border box) covers part of it.
  // The audit measures the link's box, which an overlap leaves unchanged.
  // Every row is tried at a quarter, half and three quarters across, clear of
  // the rounded corners; a link behind an open modal dialog is not a target.
  out.back_links = 0;
  out.back_link_covered = [];
  const modal = document.querySelector('dialog:modal');
  for (const link of document.querySelectorAll('.back-link')) {
    let r = link.getBoundingClientRect();
    if (!r.width || !r.height || (modal && !modal.contains(link))) continue;
    // The audit may have scrolled it out of view, where nothing is hit.
    link.scrollIntoView({block: 'center'});
    r = link.getBoundingClientRect();
    out.back_links++;
    for (let y = r.top + 0.5; y < r.bottom; y += 1) {
      for (const x of [0.25, 0.5, 0.75].map(f => r.left + f * r.width)) {
        const hit = document.elementFromPoint(x, y);
        if (!link.contains(hit)) {
          const by = hit ? (hit.id ? '#' + hit.id : hit.tagName) : 'nothing';
          out.back_link_covered.push('#' + link.id + ' row ' + Math.floor(y - r.top)
            + ' of ' + Math.round(r.height) + ': ' + by);
        }
      }
    }
  }
  // In a narrow view the page is in a frame: the result goes to the top page,
  // which is the document Edge prints.
  const top = window.top.document;
  const pre = top.createElement('pre');
  pre.id = 'audit-result';
  pre.textContent = JSON.stringify(out);
  top.body.append(pre);
})();
"""


def runner():
    extra = ",\n".join(f"{json.dumps(k)}: () => {{ {v[1]} }}" for k, v in VIEWS.items())
    return (RUNNER % (extra, accessibility_check.AUDIT)).encode("utf-8")


class Handler(design_check.Handler):
    """The design check's server, with the audit run after app.js (the page's
    content security policy allows scripts from the page's own origin only)."""

    def do_GET(self):
        path = urlsplit(self.path).path.lstrip("/")
        if path == "app/page-audit.js":
            return self._send(runner(), "text/javascript; charset=utf-8")
        if path in ("app/", "app/index.html"):
            page = (design_check.UI / "index.html").read_text(encoding="utf-8")
            page = page.replace(design_check.APP_TAG,
                                '<script src="/tools/design_check/fixtures.js"></script>\n'
                                + design_check.APP_TAG + '\n<script src="page-audit.js"></script>')
            return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        return super().do_GET()


class Server(http.server.ThreadingHTTPServer):
    # Four Edge processes load the page at once: a longer queue than the
    # default five, so no connection is refused.
    request_queue_size = 64
    daemon_threads = True


def audit(base, name, dark):
    scenario, _, width, _ = VIEWS[name]
    url = f"{base}/app/index.html?view={quote(name)}#{scenario}"
    window = width
    if width in FRAMED:
        url = f"{base}/tools/design_check/frame.html?w={width}&h=760&src={quote(url, safe='')}"
        window = max(width + 40, 500)
    # Headless Edge now and then never finishes a page, or exits 0 with no
    # result for one view (#377): one more try, with a fresh profile (a
    # timed-out Edge may still hold the first), reported on stderr. A page
    # that gave a result is never tried again, whatever it found.
    view = f"{name} ({'dark' if dark else 'light'})"
    problem = None
    for _attempt in (1, 2):
        if problem:
            print(f"test_ui_page_audit: {view}: {problem}; retried once",
                  file=sys.stderr, flush=True)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            cmd = [str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                   "--no-first-run", "--disable-extensions", f"--user-data-dir={profile}",
                   "--mute-audio", "--force-device-scale-factor=1", f"--window-size={window},760",
                   "--autoplay-policy=no-user-gesture-required",
                   # prefers-color-scheme: 0 dark, 1 light (otherwise it follows Windows)
                   f"--blink-settings=preferredColorScheme={0 if dark else 1}",
                   "--virtual-time-budget=60000", "--dump-dom", url]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=120)
            except subprocess.TimeoutExpired:
                problem = "Edge did not finish in 120 seconds"
                continue
        found = re.search(r'<pre id="audit-result">(.*?)</pre>', out.stdout or "", re.DOTALL)
        if found:
            return json.loads(html.unescape(found.group(1)))
        problem = (f"Edge returned no result (exit {out.returncode}): "
                   f"{(out.stderr or '').strip()[-300:]}")
    return {"error": f"{view}, twice: {problem}"}


@unittest.skipUnless(EDGE, "Microsoft Edge not installed")
class PageAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        runs = [(name, dark) for name in VIEWS for dark in (False, True)]
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = pool.map(lambda r: audit(base, *r), runs)
                cls.results = {(n, "dark" if d else "light"): r
                               for (n, d), r in zip(runs, results, strict=True)}
        finally:
            server.shutdown()
            server.server_close()

    def test_each_view_is_audited_with_keyboard_focus(self):
        for (name, mode), r in self.results.items():
            with self.subTest(view=name, mode=mode):
                self.assertNotIn("error", r, r.get("error"))
                self.assertGreater(r["controls"], 0)
                self.assertTrue(r["has_focus"], "the page did not have focus: nothing measured")
                width = VIEWS[name][2]
                if width in FRAMED:
                    self.assertEqual(width, r["width"], "the page is not at the view's width")
                else:
                    self.assertGreater(r["width"], 900, "the page is not in its wide layout")

    def test_the_meeting_title_holds_its_text(self):
        for (name, mode), r in self.results.items():
            if VIEWS[name][0] == "meeting":
                with self.subTest(view=name, mode=mode):
                    self.assertIs(r.get("title_fits"), True, "the title's box cuts off its text")

    def test_back_links_answer_over_their_whole_target(self):
        for (name, mode), r in self.results.items():
            with self.subTest(view=name, mode=mode):
                scenario, _, width, _ = VIEWS[name]
                if scenario == "meeting" and width > 480:
                    # Below 481 px the meeting hides its back link.
                    self.assertGreater(r.get("back_links", 0), 0, "no back link was checked")
                self.assertEqual([], r.get("back_link_covered", ["(no result)"]))

    def test_no_findings_in_the_views_fixed_for_298(self):
        # Only a finding a named owner exception covers is left out, as in the tool.
        for (name, mode), r in self.results.items():
            for key in VIEWS[name][3]:
                with self.subTest(view=name, mode=mode, check=key):
                    found = r.get(key, ["(no result)"])
                    self.assertEqual(
                        [], [f for f in found
                             if not accessibility_check.owner_exception(key, f, mode)])

    def test_the_meeting_title_at_rest_is_the_owner_s_exception(self):
        # Measured at rest: no boundary, and that finding is the named exception.
        for (name, mode), r in self.results.items():
            if VIEWS[name][0] != "meeting":
                continue
            with self.subTest(view=name, mode=mode):
                title = [f for f in r.get("low_contrast_boundary", [])
                         if f.startswith("textarea#m-title ")]
                self.assertEqual(1, len(title), "the title was not measured at rest")
                self.assertEqual("meeting title at rest", accessibility_check.owner_exception(
                    "low_contrast_boundary", title[0], mode))

    def test_the_done_tick_is_a_graphic_at_3_to_1(self):
        for (name, mode), r in self.results.items():
            if name not in TICKS:
                continue
            with self.subTest(view=name, mode=mode):
                ticks = r.get("done_ticks", [])
                self.assertGreater(len(ticks), 0, "no done step's tick was shown")
                for tick in ticks:
                    self.assertEqual("", tick["text"], "the tick is written as text")
                    self.assertTrue(tick["hidden"], "the tick is not aria-hidden")
                    self.assertEqual('"✓" / ""', tick["content"])
                    self.assertGreaterEqual(tick["ratio"], 3.0)


class FakeEdge:
    """Stands in for the subprocess module in audit(): each run gives the next
    answer, a dumped page (a dict, printed as the runner prints it), None for
    a page with no result, or "timeout"."""
    TimeoutExpired = subprocess.TimeoutExpired
    CompletedProcess = subprocess.CompletedProcess    # the real ones, kept here

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def run(self, cmd, **kw):
        self.calls += 1
        answer = self.answers.pop(0)
        if answer == "timeout":
            raise self.TimeoutExpired(cmd, kw.get("timeout"))
        page = "<html><body></body></html>" if answer is None else (
            '<html><body><pre id="audit-result">' + html.escape(json.dumps(answer))
            + "</pre></body></html>")
        stderr = "" if answer is not None else "fallback_task_provider.cc:126] no task"
        return self.CompletedProcess(cmd, 0, stdout=page, stderr=stderr)


class NoResultRetryTests(unittest.TestCase):
    """#377: headless Edge now and then exits 0 with no result for one view.
    That view is tried once more, and the retry is reported; a result is
    never retried, whatever it finds, and no assertion above is changed."""
    CLEAN = {"controls": 3, "low_contrast": []}
    FINDINGS = {"controls": 3, "low_contrast": ["span#x 2.1:1"]}

    def audit(self, *answers):
        edge = FakeEdge(*answers)
        said = io.StringIO()
        with (mock.patch.object(sys.modules[__name__], "subprocess", edge),
              contextlib.redirect_stderr(said)):
            result = audit("http://127.0.0.1:9", "names dialog", True)
        return result, edge.calls, said.getvalue()

    def test_no_result_then_a_result_passes_and_the_retry_is_reported(self):
        result, calls, said = self.audit(None, self.CLEAN)
        self.assertEqual(self.CLEAN, result)
        self.assertEqual(2, calls)
        self.assertIn("names dialog (dark)", said)
        self.assertIn("no result", said)
        self.assertIn("retried once", said)

    def test_no_result_twice_fails(self):
        result, calls, _ = self.audit(None, None)
        self.assertEqual(2, calls)
        self.assertIn("no result", result.get("error", ""))

    def test_a_result_with_findings_is_never_retried(self):
        result, calls, said = self.audit(self.FINDINGS, self.CLEAN)
        self.assertEqual(self.FINDINGS, result)
        self.assertEqual(1, calls)
        self.assertEqual("", said)

    def test_a_result_holding_the_audit_s_own_error_is_never_retried(self):
        failed = {"error": "TypeError: audit failed", "has_focus": True}
        result, calls, _ = self.audit(failed, self.CLEAN)
        self.assertEqual(failed, result)
        self.assertEqual(1, calls)

    def test_a_timeout_then_a_result_passes(self):
        result, calls, said = self.audit("timeout", self.CLEAN)
        self.assertEqual(self.CLEAN, result)
        self.assertEqual(2, calls)
        self.assertIn("retried once", said)

    def test_a_timeout_and_no_result_fail(self):
        result, calls, _ = self.audit("timeout", None)
        self.assertEqual(2, calls)
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
