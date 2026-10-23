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
#   - text reaches 4.5:1 (1.4.3): the green tick of a done step, and
#     "Optional" on the current setup step;
#   - pointer targets are 24 by 24 CSS px or spaced (2.5.8): the back links,
#     the transcript's times and passages, the template links and the
#     "Replace names" check box;
#   - accessible names contain the visible label (2.5.3): playback speed,
#     the speaker names dialog and the passages to leave out.

import html
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
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import accessibility_check  # noqa: E402
import design_check  # noqa: E402

EDGE = next((p for p in design_check.EDGE if p.is_file()), None)

# Each view: the fixtures.js scenario, script run once it is ready, the window
# width, and the audit lists that must be empty.
FOCUS = ["focus_not_measured", "no_focus_ring", "low_contrast_focus"]
VIEWS = {
    # The real-window run of 23 October 2026 reported no_focus_ring on these
    # two (and on the meeting and hand off views); none is found headless.
    "meetings": ("home", "", 1100, FOCUS),
    "settings": ("settings", "", 1100, FOCUS),
    "setup speaker names": ("setup-3", "", 1100, ["low_contrast"] + FOCUS),
    "setup microphone": ("setup-4", "", 1100, ["low_contrast"] + FOCUS),
    "meeting": ("meeting", "", 1280,
                ["low_contrast", "small_targets", "label_not_in_name"] + FOCUS),
    "names dialog": ("names", "", 1100, ["low_contrast", "label_not_in_name"] + FOCUS),
    "handoff page": ("handoff", "const i = document.querySelector('#ho-passages input');"
                     "i.checked = true; i.dispatchEvent(new Event('change', {bubbles: true}));",
                     1100, ["low_contrast", "small_targets", "label_not_in_name"] + FOCUS),
}

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
  const pre = document.createElement('pre');
  pre.id = 'audit-result';
  pre.textContent = JSON.stringify(out);
  document.body.append(pre);
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
    # Headless Edge now and then never finishes a page: one more try, with a
    # fresh profile (the timed-out Edge may still hold the first).
    for attempt in (1, 2):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            cmd = [str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                   "--no-first-run", "--disable-extensions", f"--user-data-dir={profile}",
                   "--mute-audio", "--force-device-scale-factor=1", f"--window-size={width},760",
                   "--autoplay-policy=no-user-gesture-required",
                   # prefers-color-scheme: 0 dark, 1 light (otherwise it follows Windows)
                   f"--blink-settings=preferredColorScheme={0 if dark else 1}",
                   "--virtual-time-budget=60000", "--dump-dom", url]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=120)
                break
            except subprocess.TimeoutExpired:
                if attempt == 2:
                    return {"error": f"Edge did not finish {name} twice in 120 seconds"}
    found = re.search(r'<pre id="audit-result">(.*?)</pre>', out.stdout or "", re.DOTALL)
    if not found:
        return {"error": f"Edge returned no result (exit {out.returncode}): "
                         f"{(out.stderr or '').strip()[-300:]}"}
    return json.loads(html.unescape(found.group(1)))


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

    def test_no_findings_in_the_views_fixed_for_298(self):
        for (name, mode), r in self.results.items():
            for key in VIEWS[name][3]:
                with self.subTest(view=name, mode=mode, check=key):
                    self.assertEqual([], r.get(key, ["(no result)"]))


if __name__ == "__main__":
    unittest.main()
