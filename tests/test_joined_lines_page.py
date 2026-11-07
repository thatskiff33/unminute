# Joined lines on the page (#561, the owner's decision DEC-100): a passage the
# bridge marks `joined` is drawn inside the line before it, with one time and
# one speaker (the first passage's), and each passage keeps its own text span
# and id:
#   - drawing: one line per joined line, every passage once and in order;
#   - editing one passage of a line saves that passage only, and an edit that
#     ends the sentence splits the line (1a); Undo joins it again; an edit
#     that does not change a sentence end asks the app for nothing more;
#   - search marks the line holding the matching passage;
#   - moving one passage of a line to another speaker splits it (2a), with
#     the passage chosen in the Move box;
#   - while transcribing, a passage that joins the last line drawn goes into
#     it, appended, not redrawn;
#   - Hand off's Leave out list has one choice per line, which leaves out
#     every passage in it (3a), and is ticked if any of them was left out.
#
# The real page runs in headless Microsoft Edge with the design check's fake
# bridge (tools/design_check/fixtures.js), wrapped here so the test owns the
# transcript (synthetic sentences) and sees each call: no app window,
# workspace or audio. The bridge here marks `joined` by its own copy of the
# rule, and the test checks that copy against workspace/joins.py on the same
# passages, so the lines expected are the app's.
#   python tools/run_tests.py --only test_joined_lines_page

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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import design_check  # noqa: E402

from workspace import joins  # noqa: E402

EDGE = next((p for p in design_check.EDGE if p.is_file()), None)

# Passages as [id, start, end, label, text]: lines [1, 2], [3], [4, 5], [6, 7].
ROWS = [[1, 0, 2, "SPEAKER_00", "Let's go through the plan"],
        [2, 2.4, 4, "SPEAKER_00", "for the garden project."],
        [3, 4.2, 6, "SPEAKER_00", "Then agree who does what."],
        [4, 8, 9, "SPEAKER_01", "Sounds good"],
        [5, 9.2, 10, "SPEAKER_01", "to me."],
        [6, 10.5, 12, "SPEAKER_01", "The budget"],
        [7, 12.3, 13, "SPEAKER_01", "came in under"]]
# Added while transcribing: 8 joins the last line, 9 starts its own.
MORE = [[8, 13.2, 14, "SPEAKER_01", "than planned."],
        [9, 15, 16, "SPEAKER_00", "Good."]]

# Loaded after fixtures.js and before the page's scripts.
BRIDGE = """
(() => {
  const real = window.pywebview.api;
  const NAMES = { SPEAKER_00: "Alex", SPEAKER_01: "Sam", NEW_1: "Speaker 3" };
  const row = ([id, s, e, label, text]) => ({ id, t: s, end: e, label, text, edited: false });
  const t = window.__join = { rows: ROWS_JSON.map(row), more: MORE_JSON.map(row), calls: [],
                              edits: [], undo: [], assigned: [], options: null,
                              progress: 100, reviewed: false };
  // The rule, as workspace/joins.py has it (checked against it by the test).
  const joinsPrevious = (p, c) => !/[.?!][\\s"')\\]}]*$/.test(p.text) && c.t - p.end <= 1.0 &&
    (p.label || null) === (c.label || null);
  const page = (start, count) => {
    const items = t.rows.map((r, i) => ({ ...r, who: NAMES[r.label] || null, overlap: false,
                                          joined: i > 0 && joinsPrevious(t.rows[i - 1], r) }));
    return { total: items.length, start, items: items.slice(start, start + count) };
  };
  const ok = (value) => ({ ok: true, value });
  window.pywebview = { api: new Proxy({}, { get: (_, method) => async (...args) => {
    t.calls.push(method);
    if (method === "transcript") return ok(page(args[1] || 0, args[2] || 500));
    if (method === "search") {
      const q = String(args[1]).toLowerCase();
      return ok(t.rows.filter((r) => r.text.toLowerCase().includes(q)).map((r) => r.id));
    }
    if (method === "edit") {
      const row = t.rows.find((r) => r.id === args[1]);
      t.edits.push([args[1], args[2]]);
      t.undo.push([row.id, row.text, row.edited]);
      row.text = args[2]; row.edited = true;
      return ok({ edited: true });
    }
    if (method === "undo") {
      const last = t.undo.pop();
      if (!last) return ok(null);
      const row = t.rows.find((r) => r.id === last[0]);
      row.text = last[1]; row.edited = last[2];
      return ok({ id: row.id, text: row.text, edited: row.edited });
    }
    if (method === "assign_speaker") {
      t.assigned.push([args[1], args[2]]);
      t.rows.find((r) => r.id === args[1]).label = args[2] === "new" ? "NEW_1" : args[2];
      return ok(null);
    }
    if (method === "set_handoff_options") { t.options = args[1]; return ok(null); }
    const r = await real[method](...args);
    if (method === "detail" && r.ok) {
      const d = { ...r.value, progress: t.progress, can_undo: true };
      return ok(t.reviewed ? { ...d, stage: "handoff", reviewed_at: d.created_at + 3600,
                                audio_available: false, audio_removed_at: d.created_at + 3600,
                                can_undo: false } : d);
    }
    if (method === "handoff_preview" && r.ok)
      return ok({ ...r.value, omitted: 1, options: { omitted: [2], redactions: [] } });
    return r;
  } }) };
})();
""".replace("ROWS_JSON", json.dumps(ROWS)).replace("MORE_JSON", json.dumps(MORE))

# Run after the page's scripts: the steps, then what was seen as JSON in
# <pre id="join-result">.
STEPS = """
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const t = window.__join;
  const errors = [];
  window.addEventListener("error", (e) => errors.push(String(e.message || e)));
  window.addEventListener("unhandledrejection", (e) => errors.push(String(e.reason)));
  const rows = () => [...document.querySelectorAll("#transcript > li")];
  const lines = () => rows().map((li) => [...li.querySelectorAll(".text")]
    .map((s) => Number(s.dataset.id)));
  const span = (id) => document.querySelector(`#transcript .text[data-id="${id}"]`);
  const finishEdit = async (s) => {
    s.blur();
    await wait(100);
    if (window.workspace.state.editing === s) s.dispatchEvent(new FocusEvent("blur"));
    await wait(1500);
  };
  const edit = async (id, text) => {
    const s = span(id);
    s.click();
    s.textContent = text;
    s.dispatchEvent(new Event("input"));
    await finishEdit(s);
  };
  const transcriptCalls = () => t.calls.filter((c) => c === "transcript").length;
  while (document.documentElement.dataset.ready !== "1") await wait(100);
  const out = {};
  try {
    await window.workspace.openMeeting("m-garden");
    await wait(1200);
    out.first = { lines: lines(),
                  times: rows().map((li) => li.querySelectorAll(".t").length),
                  ids: rows().map((li) => Number(li.dataset.id)),
                  chips: rows().map((li) => li.querySelectorAll(".who").length),
                  turns: rows().map((li) => li.classList.contains("turn")),
                  wrapped: rows().map((li) => !!li.querySelector(".row-body > .line")),
                  text: rows()[0].querySelector(".row-body > .line")?.textContent || null };
    // An edit that ends the sentence splits the line; Undo joins it again.
    await edit(1, "Let's go through the plan.");
    out.split = { edits: t.edits.slice(), lines: lines() };
    document.getElementById("m-undo").click();
    await wait(1500);
    out.undone = { lines: lines(), text: span(1).textContent };
    // An edit that keeps the sentence end asks the app for nothing more.
    const before = transcriptCalls();
    await edit(3, "Then agree who does what, today.");
    out.plain = { calls: transcriptCalls() - before, lines: lines(), edit: t.edits.at(-1) };
    // Search marks the line that holds the match.
    const box = document.getElementById("m-search");
    box.value = "garden"; box.dispatchEvent(new Event("input"));
    await wait(1200);
    out.search = { hit: rows().map((li) => li.classList.contains("hit")),
                   current: rows().map((li) => li.classList.contains("current")),
                   count: document.getElementById("m-hits").textContent };
    box.value = ""; box.dispatchEvent(new Event("input"));
    await wait(800);
    // Move the second passage of the line [4, 5] to the other speaker.
    rows()[2].querySelector(".who:not(.again)").click();
    await wait(800);
    const selects = [...rows()[2].querySelectorAll(".reassign select")];
    out.move_box = selects.map((s) => [...s.options].map((o) => o.value));
    selects[1].value = "5";
    selects[0].value = "SPEAKER_00";
    [...rows()[2].querySelectorAll(".reassign button")]
      .find((b) => b.textContent === "Move").click();
    await wait(1500);
    out.moved = { assigned: t.assigned.slice(), lines: lines() };
    // Transcription goes on: 8 joins the last line, 9 starts a new one.
    let removed = 0, added = 0;
    new MutationObserver((records) => {
      for (const r of records) { removed += r.removedNodes.length; added += r.addedNodes.length; }
    }).observe(document.getElementById("transcript"), { childList: true });
    t.rows.push(...t.more);
    t.progress = 101;
    await window.workspace.refresh();
    await wait(1500);
    out.appended = { lines: lines(), removed, added };
    // Hand off: one choice per line.
    t.reviewed = true;
    await window.workspace.openMeeting("m-garden");
    await wait(1200);
    document.getElementById("m-primary").click();
    await wait(2000);
    const choices = [...document.querySelectorAll("#ho-passages input")];
    out.leave_out = { ids: choices.map((c) => c.dataset.ids),
                      checked: choices.map((c) => c.checked),
                      texts: [...document.querySelectorAll("#ho-passages .p-text")]
                        .map((s) => s.textContent) };
    choices.find((c) => c.dataset.ids === "6,7,8").click();
    document.getElementById("ho-apply").click();
    await wait(1500);
    out.applied = t.options;
  } catch (e) {
    out.error = String((e && e.stack) || e);
  }
  out.errors = errors;
  const pre = document.createElement("pre");
  pre.id = "join-result";
  pre.textContent = JSON.stringify(out);
  document.body.append(pre);
})();
"""


class Handler(design_check.Handler):
    """The design check's server, with the wrapper before the page's scripts and
    the steps after them (the page's content security policy allows scripts
    from the page's own origin only)."""

    def do_GET(self):
        path = urlsplit(self.path).path.lstrip("/")
        if path == "app/join-bridge.js":
            return self._send(BRIDGE.encode("utf-8"), "text/javascript; charset=utf-8")
        if path == "app/join-steps.js":
            return self._send(STEPS.encode("utf-8"), "text/javascript; charset=utf-8")
        if path in ("app/", "app/index.html"):
            page = (design_check.UI / "index.html").read_text(encoding="utf-8")
            page = page.replace(design_check.APP_TAG,
                                '<script src="/tools/design_check/fixtures.js"></script>\n'
                                '<script src="join-bridge.js"></script>\n'
                                + design_check.APP_TAG)
            page = page.replace(design_check.MAIN_TAG,
                                design_check.MAIN_TAG + "\n"
                                + design_check.after_page("join-steps.js"))
            return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        return super().do_GET()


class Server(http.server.ThreadingHTTPServer):
    # Edge asks for the page's files at once: a longer queue than the default
    # five, so no connection is refused (as tests/test_ui_page_audit.py).
    request_queue_size = 64
    daemon_threads = True


def run_page(base):
    # "#home": the scenario only shows the Meetings page; the steps open the meeting.
    url = f"{base}/app/index.html#home"
    # Headless Edge now and then never finishes a page, or loses one of its
    # files: one more try, with a fresh profile (as tests/test_mic_list_refresh.py).
    problem = None
    for _attempt in (1, 2):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            cmd = [str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                   "--no-first-run", "--disable-extensions", f"--user-data-dir={profile}",
                   "--mute-audio", "--force-device-scale-factor=1", "--window-size=1100,760",
                   "--virtual-time-budget=60000", "--dump-dom", url]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=120)
            except subprocess.TimeoutExpired:
                problem = "Edge did not finish in 120 seconds"
                continue
        found = re.search(r'<pre id="join-result">(.*?)</pre>', out.stdout or "", re.DOTALL)
        if found:
            return json.loads(html.unescape(found.group(1)))
        problem = (f"Edge returned no result (exit {out.returncode}): "
                   f"{(out.stderr or '').strip()[-300:]}")
    return {"error": f"twice: {problem}"}


def app_lines(rows):
    """The lines workspace/joins.py makes of rows ([id, start, end, label, text]), as ids."""
    return [[p["id"] for p in line] for line in joins.lines(
        [{"id": i, "text": text, "start": s, "end": e, "label": label, "overlap": False}
         for i, s, e, label, text in rows])]


class RuleCopyTests(unittest.TestCase):
    """The test bridge's copy of the rule gives the app's lines (so the lines
    the page tests expect are the app's)."""

    def test_the_lines_expected_are_the_apps(self):
        self.assertEqual([[1, 2], [3], [4, 5], [6, 7]], app_lines(ROWS))
        self.assertEqual([[1, 2], [3], [4, 5], [6, 7, 8], [9]], app_lines(ROWS + MORE))
        split = [r if r[0] != 1 else [1, 0, 2, "SPEAKER_00", "Let's go through the plan."]
                 for r in ROWS]
        self.assertEqual([[1], [2], [3], [4, 5], [6, 7]], app_lines(split))
        moved = [r if r[0] != 5 else [5, 9.2, 10, "SPEAKER_00", "to me."] for r in ROWS]
        self.assertEqual([[1, 2], [3], [4], [5], [6, 7]], app_lines(moved))
        self.assertEqual([[1, 2], [3], [4], [5], [6, 7, 8], [9]],
                         app_lines(moved + MORE))


@unittest.skipUnless(EDGE, "Microsoft Edge not installed")
class JoinedLinesPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            cls.r = run_page(f"http://127.0.0.1:{server.server_address[1]}")
        finally:
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.assertNotIn("error", self.r, self.r.get("error"))
        self.assertEqual([], self.r["errors"], "the page threw")

    def test_a_joined_passage_is_drawn_in_the_line_before_it(self):
        first = self.r["first"]
        self.assertEqual(app_lines(ROWS), first["lines"])
        self.assertEqual([1, 1, 1, 1], first["times"], "one time per line")
        self.assertEqual([1, 3, 4, 6], first["ids"], "a line is its first passage's")
        self.assertEqual([True, False, True, False], first["turns"])
        self.assertEqual([1, 1, 1, 1], first["chips"], "one speaker label per line")
        self.assertEqual([True, False, True, True], first["wrapped"])
        self.assertEqual("Let's go through the plan for the garden project.", first["text"])

    def test_an_edit_saves_its_passage_and_a_sentence_end_splits_the_line(self):
        split = self.r["split"]
        self.assertEqual([[1, "Let's go through the plan."]], split["edits"])
        self.assertEqual([[1], [2], [3], [4, 5], [6, 7]], split["lines"])
        undone = self.r["undone"]
        self.assertEqual("Let's go through the plan", undone["text"])
        self.assertEqual(app_lines(ROWS), undone["lines"])

    def test_an_edit_that_keeps_the_sentence_end_asks_for_nothing_more(self):
        plain = self.r["plain"]
        self.assertEqual([3, "Then agree who does what, today."], plain["edit"])
        self.assertEqual(0, plain["calls"])
        self.assertEqual(app_lines(ROWS), plain["lines"])

    def test_search_marks_the_line_holding_the_match(self):
        search = self.r["search"]
        self.assertEqual([True, False, False, False], search["hit"])
        self.assertEqual([True, False, False, False], search["current"])
        self.assertIn("1", search["count"])

    def test_moving_one_passage_of_a_line_splits_it(self):
        self.assertEqual(["4", "5"], self.r["move_box"][1], "no choice of passage")
        moved = self.r["moved"]
        self.assertEqual([[5, "SPEAKER_00"]], moved["assigned"])
        self.assertEqual([[1, 2], [3], [4], [5], [6, 7]], moved["lines"])

    def test_a_passage_joining_the_last_line_is_appended_into_it(self):
        appended = self.r["appended"]
        moved = [r if r[0] != 5 else [5, 9.2, 10, "SPEAKER_00", "to me."] for r in ROWS]
        self.assertEqual(app_lines(moved + MORE), appended["lines"])
        self.assertEqual((0, 1), (appended["removed"], appended["added"]),
                         "the list was redrawn, or the joined passage got a line of its own")

    def test_the_leave_out_list_has_one_choice_per_line(self):
        leave = self.r["leave_out"]
        self.assertEqual(["1,2", "3", "4", "5", "6,7,8", "9"], leave["ids"])
        # A line with any passage left out shows as left out.
        self.assertEqual([True, False, False, False, False, False], leave["checked"])
        self.assertEqual("The budget came in under than planned.", leave["texts"][4])
        # Applied: every passage of each line ticked, as the handoff leaves them out.
        self.assertEqual([1, 2, 6, 7, 8], sorted(self.r["applied"]))


if __name__ == "__main__":
    unittest.main()
