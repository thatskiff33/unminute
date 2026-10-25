# tests/test_draft_scenario.py
# The live-draft scenario of the real-window checks (#370), tools/draft_scenario.py:
#   - DraftFeed, the test-only stand-in for a recording with draft text,
#     answers as the app does (Workspace._draft_status, recording_draft and
#     recorder.Recording.status()), through the app's real bridge, which
#     stays as it is (I3), and puts the Workspace's own methods back;
#   - the verdicts (draft_problems, draft_a11y_problems, announcement_problems)
#     fail on each broken point;
#   - in headless Microsoft Edge, the scenario and the accessibility points
#     run on the real page (workspace/ui) against the real bridge and a
#     throwaway workspace, light and dark, and pass. The window is hidden by
#     the page reporting itself hidden, as a minimised window does; the
#     real-window tools minimise it (tools/workspace_ui_check.py).
# Invented sample sentences only; no microphone, speech model or audio.
#   python tools/run_tests.py

import http.server
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import keyprotect  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
if HAVE_DEPS:
    import draft_scenario  # noqa: E402
    from draft_scenario import DraftFeed  # noqa: E402

    from workspace import api, drafts, recorder  # noqa: E402
    from workspace.app import Workspace, WorkspaceError  # noqa: E402

FAKE = "test:fake_worker_engines:"
EDGE = next((p for p in (Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                         Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"))
             if p.is_file()), None)


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = Workspace(self.root / "ws", self.root / "models", start=False,
                            allow_test_engines=True, battery=False,
                            protector=keyprotect.TestProtector())
        self.ws.settings.update(model=FAKE + "sentences", speakers=False, microphone=True)
        self.addCleanup(self.ws.close)
        self.feed = DraftFeed(self.ws).install()
        self.addCleanup(self.feed.remove)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class FeedTests(WorkspaceCase):
    """DraftFeed answers as the app does, through the app's own bridge."""

    def app_status(self, state=None, why="off", count=0):
        """What Workspace._draft_status says for a session in `state` (or
        none, and why), with the feed's generation."""
        ws = self.ws
        ws._drafts = (types.SimpleNamespace(state=state, count=count,
                                            generation=self.feed.generation, mixing=False)
                      if state else None)
        ws._draft_why, ws._draft_gen = why, self.feed.generation
        try:
            return ws._draft_status(object())
        finally:
            ws._drafts, ws._draft_why = None, "off"

    def test_each_session_state_and_reason_is_answered_in_the_apps_words(self):
        self.feed.begin(drafts.STARTING)
        for state in draft_scenario.SESSION_STATES:
            with self.subTest(state=state):
                self.feed.session(state)
                self.assertEqual(self.feed.draft_status(),
                                 self.app_status(state, count=len(self.feed.phrases)))
                self.feed.say(2)
                self.assertEqual(self.feed.draft_status(),
                                 self.app_status(state, count=len(self.feed.phrases)))
        for reason in draft_scenario.REASONS:
            with self.subTest(reason=reason):
                self.feed.withhold(reason)
                self.assertEqual(self.feed.draft_status(), self.app_status(why=reason))
                self.assertEqual(self.feed.draft_status()["note"], drafts.REASONS[reason])

    def test_the_reasons_and_states_are_all_the_apps(self):
        self.assertEqual(set(draft_scenario.REASONS), set(drafts.REASONS))
        self.assertEqual(set(draft_scenario.SESSION_STATES) | {drafts.STOPPED},
                         {drafts.STARTING, drafts.RUNNING, drafts.BATTERY, drafts.FAILED,
                          drafts.UNAVAILABLE, drafts.STOPPED})

    def test_draft_text_is_answered_as_a_draft_session_snapshot(self):
        self.feed.begin(drafts.RUNNING)
        self.feed.say(3)
        self.feed.skip(4.2)
        self.feed.say(1)
        session = drafts.DraftSession("synthetic", 16000, self.root / "models",
                                      generation=self.feed.generation)
        session.state = drafts.RUNNING
        session._phrases = [dict(p) for p in self.feed.phrases]
        for since in (0, 2, 5, 9):
            with self.subTest(since=since):
                self.assertEqual(self.feed.recording_draft(since), session.snapshot(since))

    def test_no_session_is_answered_as_the_workspace_answers(self):
        self.feed.begin(drafts.RUNNING)
        self.feed.say(2)
        self.feed.withhold("managed")
        ws = self.ws
        ws._recording, ws._draft_why, ws._draft_gen = object(), "managed", self.feed.generation
        try:
            app = type(ws).recording_draft(ws, 2)
        finally:
            ws._recording, ws._draft_why = None, "off"
        self.assertEqual(self.feed.recording_draft(2), app)

    def test_the_recording_has_the_recorders_shape(self):
        class Stream:
            def __init__(self, device, rate, callback, finished):
                self.finished = finished

            def start(self):
                pass

            def stop(self):
                self.finished()

            def close(self):
                pass
        rec = recorder.Recording(self.root / "rec", 3, "Test microphone", 16000,
                                 open_stream=Stream, key=os.urandom(32))
        rec.start()
        try:
            real = rec.status()
        finally:
            rec.stop()
        self.feed.begin()
        fake = self.feed.recording_dict()
        self.assertEqual(set(fake), set(real))
        for key in ("state", "events", "levels", "computer", "viz"):
            self.assertEqual(type(fake[key]), type(real[key]), key)

    def test_through_the_real_bridge(self):
        page = api.make_api(self.ws)
        status = page.recording_status()
        self.assertTrue(status["ok"])
        self.assertIsNone(status["value"]["recording"])
        self.assertIsNone(status["value"]["draft"])
        self.assertEqual(page.recording_draft(0),
                         {"ok": False, "error": "Nothing is being recorded."})
        self.feed.begin(drafts.RUNNING)
        lines = self.feed.say(2)
        status = page.recording_status()["value"]
        real = type(self.ws).recording_status(self.ws)
        # Only the recording and its draft are the feed's; the rest is the app's.
        self.assertEqual({k: v for k, v in status.items() if k not in ("recording", "draft")},
                         {k: v for k, v in real.items() if k not in ("recording", "draft")})
        self.assertEqual(status["draft"]["count"], 2)
        self.assertNotIn(lines[0], json.dumps(status))                 # counts, never text
        got = page.recording_draft(1)["value"]
        self.assertEqual([p["text"] for p in got["phrases"]], lines[1:])
        for bad in (-1, True, 1.5, "0", None, 10 ** 8):               # the bridge's own checks
            with self.subTest(bad=bad):
                self.assertEqual(page.recording_draft(bad),
                                 {"ok": False, "error": "Out of range."})
        self.assertEqual(self.feed.fetches, [1])                       # refused calls not counted
        with self.assertRaises(WorkspaceError):                        # the app's own check
            self.feed.recording_draft(-1)
        self.feed.end()
        self.assertIsNone(page.recording_status()["value"]["recording"])

    def test_a_locked_workspace_shows_no_recording(self):
        self.feed.begin(drafts.RUNNING)
        self.ws.locked = "synthetic"
        try:
            status = self.feed.recording_status()
        finally:
            self.ws.locked = None
        self.assertIsNone(status["recording"])
        self.assertIsNone(status["draft"])

    def test_remove_puts_the_workspaces_own_methods_back(self):
        self.assertIn("recording_status", vars(self.ws))
        self.feed.remove()
        self.assertNotIn("recording_status", vars(self.ws))
        self.assertNotIn("recording_draft", vars(self.ws))
        self.assertEqual(self.ws.recording_status.__func__, Workspace.recording_status)

    def test_a_withheld_draft_restarts_as_a_new_session(self):
        self.feed.begin(drafts.RUNNING)
        self.feed.say(2)
        gen = self.feed.generation
        self.feed.session(drafts.BATTERY)                  # suspended: same session, text kept
        self.assertEqual((self.feed.generation, len(self.feed.phrases)), (gen, 2))
        self.feed.withhold("managed")                      # ended: text forgotten, new number
        self.assertEqual((self.feed.generation, self.feed.phrases), (gen + 1, []))
        self.feed.withhold("unavailable")                  # still none: nothing more ends
        self.assertEqual(self.feed.generation, gen + 1)
        self.feed.session(drafts.STARTING)                 # a new session
        self.assertEqual(self.feed.generation, gen + 2)
        with self.assertRaises(ValueError):
            self.feed.session("managed")
        with self.assertRaises(ValueError):
            self.feed.withhold(drafts.RUNNING)

    def test_the_sentences_are_invented_and_lines_cycle_through_them(self):
        self.feed.begin(drafts.RUNNING)
        said = self.feed.say(len(draft_scenario.SENTENCES) + 1)
        self.assertEqual(said[:-1], list(draft_scenario.SENTENCES))
        self.assertEqual(said[-1], draft_scenario.SENTENCES[0])
        self.assertTrue(draft_scenario.SENTENCES[0].startswith("Synthetic check:"))


if HAVE_DEPS:
    import test_drafts

    @unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
    class SameGenerationsAsTheAppTests(test_drafts.DraftWorkspaceCase):
        """The feed's generations change where a real Workspace's do, with
        a fake microphone and fake draft engine (tests/test_drafts.py)."""

        def generation(self):
            return self.ws.recording_status()["draft"]["generation"]

        def test_withheld_and_started_again(self):
            self.record()
            self.assertTrue(test_drafts.wait_for(self.running))
            self.holder["stream"].play(self.speech(1))
            self.assertTrue(test_drafts.wait_for(lambda: self.draft_phrases()))
            app = [self.generation()]
            self.set_policy({"AllowLiveDrafts": False})       # withheld mid-recording
            self.ws._policy_tick()
            app.append(self.generation())
            self.set_policy()                                  # allowed again: a new session
            self.ws._policy_tick()
            self.assertTrue(test_drafts.wait_for(self.running))
            app.append(self.generation())
            self.ws.stop_recording()
            feed = DraftFeed(self.ws)                          # not installed: its rules only
            feed.begin(drafts.RUNNING)
            feed.say(1)
            ours = [feed.generation]
            feed.withhold("managed")
            ours.append(feed.generation)
            feed.session(drafts.STARTING)
            ours.append(feed.generation)
            self.assertEqual([ours[1] - ours[0], ours[2] - ours[1]],
                             [app[1] - app[0], app[2] - app[1]])


@unittest.skipUnless(HAVE_DEPS and sys.platform == "win32",
                     "the real-window tools run on Windows")
class ToolStartTests(unittest.TestCase):
    """Both tools start as the orchestrator and release evidence run them:
    as scripts from the repository root, so the script's folder (tools/) is
    the first path entry, or with -I, which leaves even that out (and
    PYTHONPATH). --help runs every import the tool makes before its checks
    (draft_scenario imports workspace, so it must come after the tool has put
    the app it checks on the path). Both failed this way on 26 October."""

    def start(self, tool, *flags):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run([sys.executable, *flags, str(Path("tools") / tool), "--help"],
                              cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180, check=False)

    def test_each_tool_starts_from_the_repository_root(self):
        for tool in ("accessibility_check.py", "workspace_ui_check.py"):
            for flags in (("-B",), ("-I", "-B", "-X", "disable-remote-debug")):
                with self.subTest(tool=tool, flags=flags):
                    done = self.start(tool, *flags)
                    self.assertEqual(done.returncode, 0, done.stderr[-1500:])
                    self.assertIn("usage:", done.stdout)


class SeamTests(unittest.TestCase):
    """The feed is the tools' alone: the shipped app cannot reach it (I3)."""

    def test_no_bridge_method_and_nothing_in_the_app_names_it(self):
        text = (ROOT / "workspace" / "api.py").read_text(encoding="utf-8")
        names = re.search(r"API_METHODS = frozenset\(\{(.*?)\}\)", text, re.S).group(1)
        self.assertNotRegex(names, r"feed|scenario|synthetic")
        for path in (ROOT / "workspace").rglob("*"):
            if path.suffix in (".py", ".js", ".html"):
                self.assertNotIn("draft_scenario", path.read_text(encoding="utf-8"), path)
                self.assertNotIn("DraftFeed", path.read_text(encoding="utf-8"), path)

    def test_both_tools_use_the_scenario(self):
        ui = (ROOT / "tools" / "workspace_ui_check.py").read_text(encoding="utf-8")
        a11y = (ROOT / "tools" / "accessibility_check.py").read_text(encoding="utf-8")
        self.assertTrue("draft_scenario.draft_checks(" in ui)
        self.assertTrue("draft_scenario.draft_problems(" in ui)
        self.assertTrue("draft_scenario.DRAFT_A11Y" in a11y)
        self.assertTrue("draft_scenario.draft_announcements(" in a11y)


# A passing report, as the scenario records it; each test breaks one point.
def good_report():
    look = {"area": True, "box": True, "lines": 0, "status": "", "status_shown": True,
            "empty": "", "toggle": "Hide draft", "expanded": "true", "hidden_page": False,
            "said": 0}
    n = draft_scenario.FOLLOW_LINES
    states = []
    for kind, name in draft_scenario.STATE_STEPS:
        note = (drafts.NOTES.get(name) if kind == "session" else drafts.REASONS[name]) \
            or "Draft text is on."
        states.append(dict(look, kind=kind, name=name, expected=note, status=note,
                           status_shown=note != "Draft text is on.", announced=1, fetched=0))
    return {
        "starting": dict(look, status=drafts.NOTES[drafts.STARTING],
                         empty="Getting draft text ready…"),
        "running": dict(look, status="Draft text is on.", status_shown=False,
                        empty="Draft text appears here a few seconds after people speak."),
        "first_lines": dict(look, lines=n, last=True, overflows=True, at_end=True),
        "followed": dict(look, lines=n + 3, last=True, at_end=True),
        "scrolled_up": dict(look, lines=n + 6, scroll_top=0, at_end=False),
        "followed_again": dict(look, lines=n + 8, last=True, at_end=True),
        "said_while_lines_arrived": 0,
        "hidden_by_user": dict(look, box=False, toggle="Show draft", expanded="false",
                               status="Draft text is hidden for this recording."),
        "fetches_while_hidden_by_user": 0,
        "still_hidden": dict(look, box=False),
        "shown_by_user": dict(look, lines=n + 10, status="Draft text is on."),
        "window_hidden": dict(look, lines=n + 10, hidden_page=True),
        "fetches_while_window_hidden": 0,
        "while_window_hidden": dict(look, lines=n + 10, hidden_page=True),
        "window_shown": dict(look, lines=n + 13),
        "fetches_after_shown": 1,
        "states": states,
        "stopped": dict(look, area=False),
    }


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class VerdictTests(unittest.TestCase):
    def test_the_accessibility_check_audits_the_draft_view(self):
        import accessibility_check
        self.assertEqual(accessibility_check.DRAFT_VIEW, draft_scenario.DRAFT_VIEW)
        self.assertIn(draft_scenario.DRAFT_VIEW, accessibility_check.VIEWS)
        # A report with the view: the draft points fail it until they pass.
        view = {"colour_mode": "light"}
        report = {"views": {draft_scenario.DRAFT_VIEW + " (light)": view}}
        self.assertIn("recording draft (light): the draft area was not audited",
                      accessibility_check.draft_findings(report))
        self.assertTrue(any("announced" in f or "spoke" in f
                            for f in accessibility_check.draft_findings(report)))
        self.assertEqual(accessibility_check.draft_findings({"views": {"meetings": view}}), [])

    def test_a_good_report_passes(self):
        self.assertEqual(draft_scenario.draft_problems(good_report()), [])

    def test_each_broken_point_fails(self):
        breaks = {
            "the box did not follow": ("followed", {"at_end": False}),
            "the box moved while scrolled up": ("scrolled_up", {"scroll_top": 400,
                                                                "at_end": True}),
            "Hide draft kept the text": ("hidden_by_user", {"lines": 4}),
            "Hide draft kept the box": ("hidden_by_user", {"box": True}),
            "Show draft fetched nothing": ("shown_by_user", {"lines": 0}),
            "the page did not see itself hidden": ("window_hidden", {"hidden_page": False}),
            "lines drawn while hidden": ("while_window_hidden", {"lines": 99}),
            "nothing fetched once shown": ("window_shown", {"lines": 0}),
            "the area stayed after stop": ("stopped", {"area": True}),
            "starting placeholder": ("starting", {"empty": ""}),
            "running note shown": ("running", {"status_shown": True}),
        }
        for what, (step, change) in breaks.items():
            with self.subTest(what=what):
                report = good_report()
                report[step] = dict(report[step], **change)
                self.assertTrue(draft_scenario.draft_problems(report), what)
        for key, value in (("said_while_lines_arrived", 2), ("fetches_while_hidden_by_user", 1),
                           ("fetches_while_window_hidden", 1), ("fetches_after_shown", 0)):
            with self.subTest(key=key):
                report = good_report()
                report[key] = value
                self.assertTrue(draft_scenario.draft_problems(report), key)

    def test_each_state_must_say_its_note_once(self):
        for i in range(len(draft_scenario.STATE_STEPS)):
            for change in ({"status": "something else"}, {"announced": 2}, {"announced": 0},
                           {"area": False}, {"fetched": 6}):
                with self.subTest(step=i, change=change):
                    report = good_report()
                    report["states"][i] = dict(report["states"][i], **change)
                    self.assertTrue(draft_scenario.draft_problems(report))
        report = good_report()
        report["states"] = report["states"][:-1]
        self.assertIn("not every state was checked", draft_scenario.draft_problems(report))
        report = good_report()                         # withheld: the text must go
        i = [k for k, _ in draft_scenario.STATE_STEPS].index("withhold")
        report["states"][i] = dict(report["states"][i], lines=20)
        self.assertIn("managed: the draft text stayed on the page",
                      [p.split(" ", 1)[1] for p in draft_scenario.draft_problems(report)])
        report = good_report()                         # a note that is set but hidden
        report["states"][0] = dict(report["states"][0], status_shown=False)
        self.assertTrue(draft_scenario.draft_problems(report))

    def test_the_accessibility_points(self):
        good = {"box_shown": True, "lines": 3, "box_live": "off", "box_role": "region",
                "box_name": "Draft text", "box_reachable": True, "status_role": "status",
                "status_live": "polite", "status_outside_box": True, "toggle_button": True,
                "toggle_name": "Hide draft", "toggle_controls": "rec-draft-box",
                "toggle_reachable": True, "line_ratio": 5.1,
                "label_ratio": 5.1, "share_ratio": 5.1}
        self.assertEqual(draft_scenario.draft_a11y_problems(good), [])
        for change in ({"box_live": "polite"}, {"box_live": None}, {"status_live": "off"},
                       {"status_role": None}, {"status_outside_box": False},
                       {"box_reachable": False}, {"toggle_reachable": False},
                       {"box_name": ""}, {"line_ratio": 4.2}, {"label_ratio": None},
                       {"share_ratio": 3.0}, {"lines": 0}):
            with self.subTest(change=change):
                self.assertTrue(draft_scenario.draft_a11y_problems(dict(good, **change)))

    def test_the_announcements(self):
        good = {"lines_added": 3, "while_lines": [],
                "battery": [drafts.NOTES[drafts.BATTERY]], "running": ["Draft text is on."]}
        self.assertEqual(draft_scenario.announcement_problems(good), [])
        for change in ({"while_lines": ["Draft text is on."]}, {"battery": []},
                       {"battery": [drafts.NOTES[drafts.BATTERY]] * 2}, {"running": []}):
            with self.subTest(change=change):
                self.assertTrue(draft_scenario.announcement_problems(dict(good, **change)))


# -- headless Edge: the real page against the real bridge -----------------------------------

UI = ROOT / "workspace" / "ui"
# The page's own policy, with the two additions this harness needs and the
# real window does not: script from the check (unsafe-eval) and the bridge
# reached over this local server (connect-src 'self'). Nothing else changes.
CSP = re.compile(r'(<meta http-equiv="Content-Security-Policy"\s+content=")([^"]*)(")')
SHIM = r"""
"use strict";
(() => {
  const post = (path, body) => fetch(path, {method: "POST", body: JSON.stringify(body)})
    .then((r) => r.json());
  const api = {};
  for (const name of METHODS) api[name] = (...args) => post("/bridge/" + name, args);
  window.pywebview = { api };
  window.addEventListener("load", () => {
    window.dispatchEvent(new Event("pywebviewready"));
    (async () => {                        // the check's evaluate_js, over the server
      for (;;) {
        let job;
        try {
          const r = await fetch("/driver/next");
          if (r.status !== 200) continue;
          job = await r.json();
        } catch (e) { await new Promise((ok) => setTimeout(ok, 200)); continue; }
        let value = null, error = null;
        try { value = await (0, eval)(job.js); } catch (e) { error = String(e && e.stack || e); }
        await post("/driver/done", {id: job.id, value: value === undefined ? null : value, error});
      }
    })();
  });
})();
"""


class Harness:
    """The real page (workspace/ui) in headless Edge, its bridge calls
    answered by the real bridge (api.make_api) in this process, and page
    script run from Python as the tools' evaluate_js does."""

    def __init__(self, bridge, dark=False):
        self.bridge, self.dark = bridge, dark
        self.jobs, self.results, self.seq = queue.Queue(), {}, 0
        self.done = threading.Condition()
        self.calls, self.answered = [], []

    def start(self):
        harness = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = unquote(urlsplit(self.path).path).lstrip("/")
                if path == "driver/next":
                    try:
                        job = harness.jobs.get(timeout=2)
                    except queue.Empty:
                        return self.send_response_only(204) or self.end_headers()
                    return self.send(json.dumps(job).encode(), "application/json")
                if path == "app/shim.js":
                    body = SHIM.replace("METHODS", json.dumps(sorted(
                        api.API_METHODS - {"pywebviewEventHandler"})))
                    return self.send(body.encode(), "text/javascript")
                if not path.startswith("app/"):
                    return self.send_error(404)
                rel = path[4:] or "index.html"
                target = (UI / rel).resolve()
                if not target.is_relative_to(UI.resolve()) or not target.is_file():
                    return self.send_error(404)
                body = target.read_bytes()
                if rel == "index.html":
                    html = body.decode("utf-8")
                    html = CSP.sub(lambda m: m.group(1) + m.group(2).replace(
                        "script-src 'self'", "script-src 'self' 'unsafe-eval'").replace(
                        "connect-src 'none'", "connect-src 'self'") + m.group(3), html)
                    html = html.replace('<script src="app.js"></script>',
                                        '<script src="shim.js"></script>\n'
                                        '<script src="app.js"></script>')
                    body = html.encode("utf-8")
                ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                         ".woff2": "font/woff2", ".svg": "image/svg+xml",
                         ".png": "image/png", ".ico": "image/x-icon"}.get(target.suffix,
                                                                          "application/octet-stream")
                return self.send(body, ctype)

            def do_POST(self):
                path = unquote(urlsplit(self.path).path).lstrip("/")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"null")
                if path == "driver/done":
                    with harness.done:
                        harness.results[body["id"]] = body
                        harness.done.notify_all()
                    return self.send(b"{}", "application/json")
                name = path.removeprefix("bridge/")
                if not path.startswith("bridge/") or name not in api.API_METHODS:
                    return self.send_error(404)
                harness.calls.append(name)
                answer = json.dumps(getattr(harness.bridge, name)(*body)).encode()
                harness.answered.append(name)
                return self.send(answer, "application/json")

            def send(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                if not isinstance(sys.exc_info()[1], ConnectionError):
                    super().handle_error(request, client_address)

        self.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.profile = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        url = f"http://127.0.0.1:{self.server.server_address[1]}/app/index.html"
        self.edge = subprocess.Popen(
            [str(EDGE), "--headless=new", "--disable-gpu", "--no-first-run",
             "--disable-extensions", "--mute-audio", f"--user-data-dir={self.profile.name}",
             "--window-size=1100,760", "--force-device-scale-factor=1",
             f"--blink-settings=preferredColorScheme={0 if self.dark else 1}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Ready once the page's own start-up has had the setup state and
        # shown its first view (Setup here), so it cannot switch views under
        # a step. The real-window tools reach the draft steps long after that.
        end = time.monotonic() + 90
        while "setup_state" not in self.answered and time.monotonic() < end:
            time.sleep(0.05)
        ready = self.evaluate_js("(async () => { await new Promise(r => setTimeout(r, 1000));"
                                 " return !!window.workspace; })()", timeout=60)
        if not ready or "setup_state" not in self.answered:
            raise AssertionError("the page did not start in headless Edge")
        return self

    def stop(self):
        try:
            import windows_powershell        # taskkill by its full path (#237, #253)
            if self.edge.poll() is None:     # never a PID Windows has given out again
                subprocess.run([str(windows_powershell.system_program("taskkill.exe")), "/PID",
                                str(self.edge.pid), "/T", "/F"],
                               capture_output=True, timeout=30, check=False)
            self.edge.wait(30)
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.profile.cleanup()

    def evaluate_js(self, js, callback=None, timeout=90):
        """As pywebview's: run the script, and pass its value to callback (or
        return it). A script error fails the test."""
        self.seq += 1
        job = {"id": self.seq, "js": js}
        self.jobs.put(job)
        end = time.monotonic() + timeout
        with self.done:
            while job["id"] not in self.results:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError(js[:80])
                self.done.wait(left)
            got = self.results.pop(job["id"])
        if got.get("error"):
            raise AssertionError(f"page script failed: {got['error'][:600]}")
        if callback is not None:
            callback(got.get("value"))
        return got.get("value")


def evaluate(window, js, timeout=60):
    """The tools' evaluate (tools/workspace_ui_check.py): an async script."""
    return window.evaluate_js(f"(async () => {{ {js} }})()", timeout=timeout)


HIDE = ("Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});"
        "Object.defineProperty(document, 'visibilityState', {configurable: true, "
        "get: () => 'hidden'}); document.dispatchEvent(new Event('visibilitychange'));")
SHOW = ("delete document.hidden; delete document.visibilityState;"
        "document.dispatchEvent(new Event('visibilitychange'));")


@unittest.skipUnless(HAVE_DEPS and EDGE, "Microsoft Edge not installed")
class HeadlessTests(WorkspaceCase):
    def page(self, dark=False):
        harness = Harness(api.make_api(self.ws), dark=dark).start()
        self.addCleanup(harness.stop)
        return harness

    def test_the_scenario_passes_on_the_real_page(self):
        window = self.page()
        report = draft_scenario.draft_checks(
            window, self.feed, evaluate,
            hide=lambda: evaluate(window, HIDE + "return true;"),
            show=lambda: evaluate(window, SHOW + "return true;"))
        self.assertEqual(draft_scenario.draft_problems(report), [],
                         json.dumps(report, indent=1)[:4000])
        self.assertIn("recording_draft", window.calls)
        self.assertFalse(self.feed.active)

    def test_the_accessibility_points_pass_in_light_and_dark(self):
        import accessibility_check
        for dark in (False, True):
            with self.subTest(dark=dark):
                window = self.page(dark=dark)
                self.feed.begin(drafts.RUNNING)
                self.feed.say(draft_scenario.FOLLOW_LINES)
                evaluate(window, "const wait = (ms) => new Promise(r => setTimeout(r, ms));"
                         + accessibility_check.VIEWS[draft_scenario.DRAFT_VIEW]
                         + "return true;")
                mode = evaluate(window, accessibility_check.COLOUR_MODE)
                self.assertEqual(mode, "dark" if dark else "light")
                audit = evaluate(window, accessibility_check.AUDIT)
                audit["colour_mode"] = mode
                audit["draft"] = evaluate(window, draft_scenario.DRAFT_A11Y)
                self.assertEqual(draft_scenario.draft_a11y_problems(audit["draft"]), [],
                                 audit["draft"])
                report = {"zoomable": True, "keyboard_zoom": {"before": 1, "zoomed": 1.1,
                                                              "reset": 1},
                          "page": {"lang": "en-NZ", "title": "x", "live_status": "polite"},
                          "views": {"recording draft": audit},
                          "draft_announcements": draft_scenario.draft_announcements(
                              window, self.feed, evaluate)}
                self.assertEqual(accessibility_check.findings(report), [], report)
                self.feed.end()


if __name__ == "__main__":
    unittest.main()
