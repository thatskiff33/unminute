# Security hardening checks (#111 slice A2): the window shell's dispatch guard
# and navigation rule, settings validation, test-only engines, the workspace
# location, SQLite temporary storage, a token left in the environment,
# nvidia-smi, remote debugging (B4), and the installer's and uninstaller's
# folder checks and their wait for a file in use (#239).
#   python -m unittest discover -s tests -v

import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import keyprotect  # noqa: E402

from workspace import credentials, shell  # noqa: E402

# The installer tests start Windows PowerShell, and the junction tests cmd.exe,
# by the full path in the system folder, never by name (#237, #253). The helper
# is loaded from its file so tools/ stays off this module's import path.
_spec = importlib.util.spec_from_file_location("windows_powershell",
                                               ROOT / "tools" / "windows_powershell.py")
windows_powershell = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(windows_powershell)


def mklink_junction(link, target, **kwargs):
    """cmd /c mklink /J link target, with cmd.exe from the system folder and
    the minimal environment, never cmd by name (#253)."""
    return subprocess.run([windows_powershell.system_program("cmd.exe"), "/c", "mklink", "/J",
                           str(link), str(target)],
                          env=windows_powershell.environment(), **kwargs)


HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
HAVE_WEBVIEW = importlib.util.find_spec("webview") is not None

# The uninstaller's decoder profile (#299), and the throwaway name copies of it
# use instead, so no test removes the app's own profile for the user running it.
DECODER_PROFILE_LINE = '$DecoderProfile = "Unminute.Decoder"'
DECODER_TEST_PROFILE = f"Unminute.Test.Uninstall.{os.getpid()}"


def dacl_sddl(path):
    """The DACL of `path` as SDDL ("D:..."), read without any tool whose
    output depends on the system language. ALL APPLICATION PACKAGES is "AC"."""
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    sd = ctypes.c_void_p()
    if adv.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(sd)):
        raise OSError("the permissions could not be read")
    try:
        text = wintypes.LPWSTR()
        if not adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, 1, 4,
                                                                       ctypes.byref(text), None):
            raise OSError("the permissions could not be read")
        try:
            return text.value
        finally:
            k32.LocalFree(text)
    finally:
        k32.LocalFree(sd)


class DomEventTests(unittest.TestCase):
    """Only dragover and drop on the document reach pywebview, rebuilt so a
    page-supplied pywebviewFullPath never survives."""

    def drop(self, files, node="document"):
        return {"event": {"type": "drop", "dataTransfer": {"files": files}}, "nodeId": node}

    def test_page_supplied_full_path_is_removed(self):
        clean = shell._clean_dom_event(self.drop([
            {"name": "a.m4a", "pywebviewFullPath": "C:\\Users\\x\\secret.m4a"},
            {"name": "b.wav"}]))
        files = clean["event"]["dataTransfer"]["files"]
        self.assertEqual(files, [{"name": "a.m4a"}, {"name": "b.wav"}])
        self.assertNotIn("pywebviewFullPath", json.dumps(clean))

    def test_other_nodes_events_and_shapes_are_refused(self):
        for bad in (self.drop([], node="el-1"),
                    {"event": {"type": "click"}, "nodeId": "document"},
                    {"event": "drop", "nodeId": "document"},
                    {"event": {"type": "drop", "dataTransfer": {"files": "x"}},
                     "nodeId": "document"},
                    None, [], "drop"):
            self.assertIsNone(shell._clean_dom_event(bad), repr(bad))

    def test_dragover_carries_nothing_else(self):
        clean = shell._clean_dom_event({"event": {"type": "dragover", "x": 1, "files": [1]},
                                        "nodeId": "document"})
        self.assertEqual(clean, {"event": {"type": "dragover"}, "nodeId": "document"})


class NavigationTests(unittest.TestCase):
    def test_only_the_windows_own_page(self):
        page = shell.ui_url("index.html")
        self.assertTrue(shell._allowed(page))
        self.assertTrue(shell._allowed(page + "#meeting"))
        self.assertTrue(shell._allowed(page + "?x=1"))
        for other in (shell.ui_url("other.html"), shell.ui_url("app.js"),
                      shell.ui_url("img/logo.svg"), page + "%23/evil.html",
                      page + "%3F/../other.html"):
            self.assertFalse(shell._allowed(other), other)
        self.assertTrue(shell._allowed(shell.ui_url("other.html"), "other.html"))
        self.assertFalse(shell._allowed(page, "other.html"))

    def test_the_window_needs_its_allowed_names_and_has_one_page(self):
        # No default bridge allowlist to fall back on, and no page to choose (#136).
        import inspect
        params = inspect.signature(shell.create_window).parameters
        self.assertIs(params["allowed"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(params["allowed"].default, inspect.Parameter.empty)
        self.assertNotIn("page", params)

    def test_the_lock_keeps_index_html_and_cancels_any_other_page(self):
        from types import SimpleNamespace

        class Event:                       # WebView2's NavigationStarting (+= adds a handler)
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        control = SimpleNamespace(NavigationStarting=Event())
        window = SimpleNamespace(native=SimpleNamespace(browser=SimpleNamespace(webview=control)),
                                 unminute_page="other.html")     # never followed
        with mock.patch.object(shell, "quiet_script_dialogs"):
            shell._lock_once(window)
        [handler] = control.NavigationStarting.handlers
        cancelled = {}
        for name in ("index.html#x", "other.html"):
            args = SimpleNamespace(Uri=shell.ui_url("index.html").rsplit("/", 1)[0] + "/" + name,
                                   Cancel=False)
            handler(None, args)
            cancelled[name] = args.Cancel
        self.assertEqual(cancelled, {"index.html#x": False, "other.html": True})

    def test_the_page_opens_no_link_itself(self):
        # #109: Copy and open asks the app to open the assistant; the page has
        # no link, window.open, location change or form that submits anywhere
        # (its forms are method="dialog"). Only its two stylesheets use href.
        import re
        ui = ROOT / "workspace" / "ui"
        html = (ui / "index.html").read_text(encoding="utf-8")
        script = (ui / "app.js").read_text(encoding="utf-8").replace(
            '"http://www.w3.org/2000/svg"', "SVG_NAMESPACE")            # a name, not an address
        self.assertEqual(re.findall(r"\bhref\s*=\s*\"([^\"]*)\"", html), ["tokens.css", "app.css"])
        for source in (html, script):
            for pattern in (r"window\.open\b", r"\blocation\b", r"\.href\b", r"\bsrcdoc\b",
                            r"\bhttps?://", r"[\"']a[\"']", r"[\"']iframe[\"']"):
                self.assertIsNone(re.search(pattern, source), pattern)
        for pattern in (r"<a\b", r"<iframe\b", r"<form(?![^>]*method=\"dialog\")",
                        r"<[^>]*\s(?:action|formaction|target)\s*="):         # markup only
            self.assertIsNone(re.search(pattern, html), pattern)

    def test_the_page_names_no_assistant_until_the_app_does(self):
        # #109 review: before the preview arrived (or if it failed), the page
        # showed "Copy and open ChatGPT" even where IT had turned it off or
        # named another assistant. Copy and open and its note now start hidden
        # and empty, Copy prompt and transcript is the main button until then,
        # and the preview's name and host fill them (app.js layoutHandoff).
        import re
        html = (ROOT / "workspace" / "ui" / "index.html").read_text(encoding="utf-8")
        for tag_id in ("ho-open", "ho-open-note"):
            tag = re.search(r'<[a-z]+ [^>]*\bid="' + tag_id + r'"[^>]*>', html).group(0)
            self.assertRegex(tag, r"\shidden[\s>]", tag_id)
        for span in ("ho-open-name", "ho-open-host"):
            self.assertIn(f'<span id="{span}"></span>', html)
        copy = re.search(r'<button [^>]*\bid="ho-copy"[^>]*>', html).group(0)
        self.assertIn('class="primary wide-btn"', copy)

    def test_every_hand_off_visit_starts_without_an_assistant(self):
        # #109 review round 2: reopening Hand off kept the last preview's
        # "Copy and open" name and host until the new preview arrived (and
        # for good if it failed). openHandoff now lays the page out as before
        # #109 before it waits for anything; the preview fills it in.
        import re
        script = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
        body = re.search(r"\nasync function openHandoff\(\) \{\n(.*?)\n\}\n", script, re.S)
        self.assertIsNotNone(body, "openHandoff not found")
        self.assertIn("layoutHandoff(null);", body.group(1).split("await", 1)[0])

    def test_the_hand_off_buttons_beneath_copy_and_open_stay_one_row(self):
        # #109 review: the three ways out beneath Copy and open are one flex
        # row (wrapping only where three cannot fit), not a grid with Copy
        # prompt and transcript spanning a row of its own.
        import re
        css = (ROOT / "workspace" / "ui" / "app.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        row = [(s.strip(), d) for s, d in rules
               if re.search(r"\.ho-three|\.ho-pair|#ho-row|#ho-copy\b", s)]
        self.assertTrue(any(s == ".ho-pair" and re.search(r"display\s*:\s*flex", d)
                            for s, d in row), ".ho-pair must stay a flex row")
        for selector, decls in row:
            self.assertNotRegex(decls, r"display\s*:\s*(inline-)?grid", selector)
            self.assertNotRegex(decls, r"grid-(column|row|area|template)", selector)

    def test_a_reminders_failure_still_draws_the_meeting_list(self):
        # #111 C6 review: refresh fetches the reminders before the list, so the
        # rows carry their retention chips. A failed reminders call must not
        # skip renderLists (the list and sidebar count would go stale), and
        # renderReminders must be a plain function, so a throw inside it
        # reaches refresh's catch rather than becoming an unhandled rejection.
        import re
        script = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
        body = re.search(r"\nasync function refresh\(\) \{\n(.*?)\n\}\n", script, re.S)
        self.assertIsNotNone(body, "refresh not found")
        before_list = body.group(1).split("await renderLists(", 1)[0]
        own_catch = (r"try \{\s*reminders = await call\(\"reminders\"\);\s*"
                     r"pollOk\(\"reminders\"\);\s*\}\s*catch \(err\) \{")
        self.assertRegex(before_list, own_catch,
                         "the reminders call must have its own catch before renderLists")
        self.assertNotRegex(script, r"async function renderReminders\(")
        self.assertRegex(script, r"\nfunction renderReminders\(r\) \{")


def page_functions(*names):
    """The source of each named top-level function in app.js (one line or a
    block that ends with a closing brace at the start of a line), for a test
    that runs the page's own code in Node.js."""
    import re
    script = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
    parts = []
    for name in names:
        head = r"^(?:async )?function " + name + r"\("
        found = (re.search(head + r".*\}$", script, re.M)
                 or re.search(head + r".*?^\}$", script, re.M | re.S))
        if found is None:
            raise AssertionError(f"{name} not found at the top level of app.js")
        parts.append(found.group(0))
    return "\n".join(parts) + "\n"


# A failed reminders call, or a reminders banner that cannot be drawn, must
# still let refresh draw the meeting list and report the error once, with no
# unhandled rejection (#111 C6 review round 2: run the page's own refresh and
# renderReminders rather than checking the shape of the source). The same error
# at the next poll is not reported again; a changed one is, and so is the same
# one after a poll that succeeded (#404). Everything else refresh touches is a
# stub. The cases run in order, one poll each.
REFRESH_CASES = r"""
const window = { pywebview: {} };
const pollErrors = {};
const LIST_REFRESH_POLLS = 6;
let refreshing = false, refreshAgain = false, pollTimer = null, pollTimerDelay = 0;
const pollDelay = () => 0;
const schedulePoll = () => {};
const state = { view: "meetings", changes: 0, listsSeen: 0, listsView: "meetings", sinceLists: 0 };
let said = [], listed = [], shown = {}, reply = null;
const unhandled = [];
process.on("unhandledRejection", (err) => unhandled.push(String((err && err.message) || err)));
const say = (text) => { said.push(text); };
const renderStatus = async () => {};
const renderRecorder = async () => {};
const renderLists = async (reminders) => { listed.push(reminders ? "reminders" : null); };
const notice = (kind, text) => ({ kind, text });
const button = () => ({});
const linkButton = () => ({});
const $ = () => ({ replaceChildren: (...lines) => { shown.lines = lines.length; },
                   set hidden(value) { shown.hidden = value; } });
const call = async (name) => {
  if (name !== "reminders") throw new Error("unexpected call " + name);
  return reply();
};
const thresholds = { review_days: 30, action_days: 0, managed: { review: false, action: false } };
const good = { retention: [], recordings_waiting: [], thresholds,
               holds_due: [], awaiting_confirmation: [] };
const cases = {
  call_fails: () => { throw new Error("reminders failed"); },
  call_fails_again: () => { throw new Error("reminders failed"); },
  call_fails_changed: () => { throw new Error("reminders failed differently"); },
  draw_fails: () => ({ ...good, holds_due: null }),       // renderReminders throws a TypeError
  draw_fails_again: () => ({ ...good, holds_due: null }),
  ok: () => good,
  call_fails_after_ok: () => { throw new Error("reminders failed"); },
};
(async () => {
  const out = {};
  for (const [name, answer] of Object.entries(cases)) {
    said = []; listed = []; shown = {}; unhandled.length = 0;
    reply = answer;
    state.changes += 1;                                    // a change: the lists are fetched
    await refresh();
    await new Promise((done) => setTimeout(done, 50));     // let any unhandled rejection surface
    out[name] = { said: said.length, text: said, listed, shown, unhandled: unhandled.length };
  }
  console.log(JSON.stringify(out));
})();
"""


class ReminderFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = page_functions("refresh", "renderReminders", "oneOrMany",
                                    "retentionBanner", "pollError", "pollOk") + REFRESH_CASES
        cls.node = shutil.which("node")

    def run_cases(self):
        if not self.node:
            self.skipTest("Node.js not installed")
        done = subprocess.run([self.node, "-"], input=self.source, capture_output=True,
                              text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_failed_reminders_call_still_draws_the_meeting_list(self):
        out = self.run_cases()["call_fails"]
        self.assertEqual(out["listed"], [None], "renderLists must still run, without reminders")
        self.assertEqual(out["said"], 1, "the failure is reported once")
        self.assertEqual(out["unhandled"], 0)
        self.assertEqual(out["shown"], {}, "no banner is drawn from a failed call")

    def test_a_banner_that_cannot_be_drawn_is_reported_not_lost(self):
        # renderReminders must be a plain function: if it were async and not
        # awaited, its throw would be an unhandled rejection, not a message.
        out = self.run_cases()["draw_fails"]
        self.assertEqual(out["listed"], ["reminders"])
        self.assertEqual(out["said"], 1, "refresh's catch reports the error")
        self.assertEqual(out["unhandled"], 0)

    def test_reminders_with_nothing_due_hide_the_banner(self):
        out = self.run_cases()["ok"]
        self.assertEqual(out, {"said": 0, "text": [], "listed": ["reminders"],
                               "shown": {"lines": 0, "hidden": True}, "unhandled": 0})

    def test_an_error_that_persists_across_polls_is_reported_once(self):
        # #404: a bridge call that keeps failing was toasted, and may be read
        # out, at every poll (2 s while active, 10 s otherwise).
        out = self.run_cases()
        self.assertEqual(out["call_fails"]["text"], ["reminders failed"])
        self.assertEqual(out["call_fails_again"]["text"], [], "the reminders error repeated")
        self.assertEqual(out["draw_fails_again"]["text"], [], "refresh's error repeated")
        self.assertEqual(out["draw_fails_again"]["listed"], ["reminders"])

    def test_a_new_or_changed_error_is_still_reported_at_once(self):
        out = self.run_cases()
        self.assertEqual(out["call_fails_changed"]["text"], ["reminders failed differently"])
        self.assertEqual(out["draw_fails"]["said"], 1)
        self.assertEqual(out["call_fails_after_ok"]["text"], ["reminders failed"],
                         "the same error after a poll that succeeded is new again")


# The toast (#status, a live region): the same message again leaves its text
# alone, as #346's putText does for the other live regions, and starts the
# timer again so the toast still clears that long after the last time it was
# said; a different message, or clearing it, changes it (#404). The page's own
# say and putText run in Node.js against a fake #status that records each
# change to its text.
SAY_CASES = r"""
let writes = 0, timers = 0, cleared = 0;
const node = { _text: "", dataset: {}, classList: { toggle() {} },
               get textContent() { return this._text; },
               set textContent(v) { writes += 1; this._text = String(v); } };
const $ = () => node;
const state = { statusLine: "" };
const OK_MESSAGE = /^(Saved)/;
let toastTimer = null;
const setTimeout = () => { timers += 1; return timers; };
const clearTimeout = (id) => { if (id) cleared += 1; };
const step = (message, opts) => {
  writes = 0; timers = 0; cleared = 0;
  say(message, opts);
  return { writes, timers, cleared, text: node.textContent };
};
console.log(JSON.stringify({
  first: step("The recorder could not be reached."),
  same: step("The recorder could not be reached."),
  changed: step("The reminders could not be read."),
  cleared: step(""),
  cleared_again: step(""),
}));
"""


class ToastRepeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = page_functions("say", "putText") + SAY_CASES
        cls.node = shutil.which("node")

    def run_cases(self):
        if not self.node:
            self.skipTest("Node.js not installed")
        done = subprocess.run([self.node, "-"], input=self.source, capture_output=True,
                              text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_same_message_again_leaves_the_live_region_alone(self):
        out = self.run_cases()
        self.assertEqual(out["first"]["writes"], 1)
        self.assertEqual(out["same"]["writes"], 0, "#status was set again to the same text")
        self.assertEqual((out["same"]["cleared"], out["same"]["timers"]), (1, 1),
                         "the toast's timer must start again, so it still clears")

    def test_a_different_message_or_clearing_changes_it(self):
        out = self.run_cases()
        self.assertEqual((out["changed"]["writes"], out["changed"]["text"]),
                         (1, "The reminders could not be read."))
        self.assertEqual((out["cleared"]["writes"], out["cleared"]["text"]), (1, ""))
        self.assertEqual((out["cleared_again"]["writes"], out["cleared_again"]["timers"]), (0, 0))


# The retention banner's wording (#111 C6). The owner's decision of 22 October
# 2026: "Your organisation asks for each step to be finished within N days"
# appears only for a managed ActionRequiredDays. A managed ReviewReminderDays
# is a nudge, not a deadline, so "Review due" never claims a rule. The page's
# own retentionBanner runs in Node.js (as tests/test_level_meter.py does).
BANNER_CASES = r"""
const notice = (kind, text) => ({ kind, text });
const due = (n) => Array.from({ length: n }, () => ({ state: "due" }));
const act = (n) => Array.from({ length: n }, () => ({ state: "action_required" }));
// banner(meetings, recordings, review days, action days, [IT set review, IT set action])
const banner = (retention, waiting, review, action, [mr, ma]) => retentionBanner({
  retention, recordings_waiting: waiting,
  thresholds: { review_days: review, action_days: action, managed: { review: mr, action: ma } } });
const out = {
  review_managed: banner(due(2), due(1), 7, 0, [true, false]),
  review_managed_both: banner(due(1), [], 7, 14, [true, true]),
  review_default: banner(due(1), [], 30, 0, [false, false]),
  action_managed: banner(act(3), due(1), 7, 14, [true, true]),
  action_only_managed: banner(act(1), act(2), 30, 60, [false, true]),
  action_unmanaged: banner(act(1), [], 30, 60, [true, false]),
  none: banner([], [], 30, 0, [true, true]),
};
console.log(JSON.stringify(out));
"""


class RetentionBannerTests(unittest.TestCase):
    RULE = "Your organisation asks"

    @classmethod
    def setUpClass(cls):
        cls.source = page_functions("oneOrMany", "retentionBanner") + BANNER_CASES
        cls.node = shutil.which("node")

    def banners(self):
        if not self.node:
            self.skipTest("Node.js not installed")
        done = subprocess.run([self.node, "-"], input=self.source, capture_output=True,
                              text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_managed_review_threshold_claims_no_organisation_rule(self):
        out = self.banners()
        for case in ("review_managed", "review_managed_both"):
            with self.subTest(case=case):
                self.assertEqual(out[case]["kind"], "hold")
                self.assertNotIn(self.RULE, out[case]["text"])
                self.assertIn("waited 7 days or more", out[case]["text"])
        self.assertEqual(out["review_managed"]["text"],
                         "2 meetings have waited 7 days or more for their next step. "
                         "1 recording has waited 7 days or more for Keep or Delete.")

    def test_the_app_default_review_threshold_claims_no_rule_either(self):
        text = self.banners()["review_default"]["text"]
        self.assertEqual(text, "1 meeting has waited 30 days or more for its next step.")

    def test_only_a_managed_action_threshold_states_the_organisation_rule(self):
        out = self.banners()
        self.assertEqual(out["action_managed"],
                         {"kind": "alert", "text": "3 meetings need action. Your organisation "
                          "asks for each step to be finished within 14 days."})
        self.assertEqual(out["action_only_managed"]["text"],
                         "1 meeting needs action. 2 recordings need action: keep or delete "
                         "them. Your organisation asks for each step to be finished within "
                         "60 days.")
        unmanaged = out["action_unmanaged"]["text"]
        self.assertNotIn(self.RULE, unmanaged)
        self.assertEqual(unmanaged, "1 meeting needs action. Each has waited 60 days or more.")
        self.assertIsNone(out["none"])


@unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
class DispatchGuardTests(unittest.TestCase):
    def setUp(self):
        import webview.util as util
        self.util = util
        self.seen = []
        self.original = util.js_bridge_call
        util.js_bridge_call = lambda w, name, p, i: self.seen.append((name, p))
        shell.install_dispatch_guard({"rename", "ping"})

    def tearDown(self):
        self.util.js_bridge_call = self.original

    def window(self):
        class Api:
            def rename(self, mid, title):
                return {"ok": True}

            def ping(self):
                return {"ok": True}

        class Window:
            _js_api = Api()
        return Window()

    def test_wrong_argument_counts_are_refused_before_the_call(self):
        w = self.window()
        for args in ([], ["m"], ["m", "t", "extra"], "m", None, {"mid": "m"}):
            self.util.js_bridge_call(w, "rename", args, "1")
        self.util.js_bridge_call(w, "rename", ["m", "t"], "1")
        self.util.js_bridge_call(w, "ping", [], "1")
        self.assertEqual([name for name, _ in self.seen], ["rename", "ping"])
        self.assertIn("rename(arguments)", shell.blocked_calls)

    def test_drop_event_reaches_pywebview_only_cleaned(self):
        shell.install_dispatch_guard({"pywebviewEventHandler"})
        w = self.window()
        self.util.js_bridge_call(w, "pywebviewEventHandler", {
            "event": {"type": "drop", "dataTransfer": {"files": [
                {"name": "a.m4a", "pywebviewFullPath": "C:\\x.m4a"}]}},
            "nodeId": "document"}, "eventHandler")
        self.util.js_bridge_call(w, "pywebviewEventHandler",
                                 {"event": {"type": "drop"}, "nodeId": "el-7"}, "eventHandler")
        self.assertEqual(len(self.seen), 1)
        self.assertNotIn("pywebviewFullPath", json.dumps(self.seen[0][1]))

    def test_guard_is_marked_for_the_start_up_check(self):
        self.assertTrue(getattr(self.util.js_bridge_call, "unminute_guard", False))


class PageAlertTests(unittest.TestCase):
    def test_only_the_alert_message_is_recognised(self):
        self.assertTrue(shell._page_alert(json.dumps(["_pywebviewAlert", json.dumps("hi"),
                                                      "alert"])))
        for other in (json.dumps(["meetings", "[]", "1"]), json.dumps("FilesDropped"),
                      "not json", None, "[]", json.dumps({"a": 1})):
            self.assertFalse(shell._page_alert(other), other)


class AlertGuardTests(unittest.TestCase):
    """The page's window.alert never reaches pywebview's native message box."""

    def fake_backend(self):
        seen = []

        class EdgeChrome:
            def on_script_notify(self, sender, args):
                seen.append(args.get_WebMessageAsJson())

            def on_webview_ready(self, sender, args):
                settings = sender.CoreWebView2.Settings
                seen.append(("settings", settings.AreDefaultScriptDialogsEnabled))
                sender.CoreWebView2.Settings.AreDefaultScriptDialogsEnabled = True  # as pywebview
                self.load_url("file:///index.html")

            def load_url(self, url):
                seen.append(("load", url, self.sender.CoreWebView2.Settings
                             .AreDefaultScriptDialogsEnabled))

            def load_html(self, html, base):
                seen.append(("html",))

        module = type(sys)("webview.platforms.edgechromium")
        module.EdgeChrome = EdgeChrome
        return module, seen

    def test_alert_is_dropped_and_calls_still_reach_pywebview(self):
        module, seen = self.fake_backend()

        class Args:
            def __init__(self, message):
                self.message = message

            def get_WebMessageAsJson(self):
                return self.message
        alert = json.dumps(["_pywebviewAlert", json.dumps("Tick the box, then Allow"), "alert"])
        call = json.dumps(["meetings", "[]", "1"])
        with mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": module}):
            shell.install_alert_guard()
            shell.install_alert_guard()                     # twice: still one wrapper
            view = module.EdgeChrome()
            view.on_script_notify(None, Args(alert))
            view.on_script_notify(None, Args(call))
        self.assertEqual(seen, [call])
        self.assertIn("_pywebviewAlert", shell.blocked_calls)
        self.assertTrue(module.EdgeChrome.on_script_notify.unminute_guard)

    def test_browser_dialogs_are_off_before_the_first_page_loads(self):
        module, seen = self.fake_backend()

        class Settings:
            AreDefaultScriptDialogsEnabled = True

        class Event:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        class Core:
            def __init__(self):
                self.Settings = Settings()
                self.ScriptDialogOpening = Event()
                self.PermissionRequested = Event()
                self.ScreenCaptureStarting = Event()

        class Control:
            def __init__(self):
                self.CoreWebView2 = Core()
        control = Control()
        with mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": module}), \
                mock.patch.object(shell, "_permission_deny", return_value="DENY"):
            shell.install_alert_guard()
            view = module.EdgeChrome()
            view.sender = control
            view.on_webview_ready(control, None)
        # pywebview turned them on, but the page loaded with them off, and
        # any dialog, permission or screen capture asked for is refused.
        self.assertEqual(seen[-1], ("load", "file:///index.html", False))
        core = control.CoreWebView2
        self.assertFalse(core.Settings.AreDefaultScriptDialogsEnabled)
        self.assertEqual(core.ScriptDialogOpening.handlers, [shell._refuse_script_dialog])
        self.assertEqual(core.ScreenCaptureStarting.handlers, [shell._refuse_screen_capture])
        [refuse_permission] = core.PermissionRequested.handlers               # each once

        class Request:                        # the page asks for the microphone
            PermissionKind = "Microphone"
            State = "Default"
        request = Request()
        refuse_permission(None, request)
        self.assertEqual(request.State, "DENY")
        self.assertIn("permission Microphone", shell.blocked_calls)

        class Capture:                        # the page asks to capture the screen
            Cancel = False
        capture = Capture()
        shell._refuse_screen_capture(None, capture)
        self.assertTrue(capture.Cancel)
        self.assertIn("screen capture", shell.blocked_calls)
        self.assertNotIn("load_url", view.__dict__)              # the instance is left as it was
        self.assertTrue(module.EdgeChrome.on_webview_ready.unminute_guard)

    def test_no_page_loads_when_the_guard_cannot_attach(self):
        # An older WebView2 Runtime without the screen-capture event: the
        # page never loads and the window closes, with no error escaping into
        # WebView2's start-up.
        module, seen = self.fake_backend()

        class Settings:
            AreDefaultScriptDialogsEnabled = True

        class Event:
            def __iadd__(self, handler):
                return self

        class Core:
            def __init__(self):
                self.Settings = Settings()
                self.ScriptDialogOpening = Event()
                self.PermissionRequested = Event()

        class Control:
            def __init__(self):
                self.CoreWebView2 = Core()
        control = Control()
        closed = []
        try:
            with mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": module}), \
                    mock.patch.object(shell, "_permission_deny", return_value="DENY"), \
                    mock.patch.object(shell, "_close_now", closed.append):
                shell.install_alert_guard()
                view = module.EdgeChrome()
                view.sender = control
                view.pywebview_window = "the window"
                view.on_webview_ready(control, None)
            self.assertFalse([s for s in seen if s[0] == "load"])
            self.assertEqual(closed, ["the window"])             # once
            self.assertTrue(shell.lock_failed)
        finally:
            shell.lock_failed.clear()


@unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
class BackendCheckTests(unittest.TestCase):
    def test_refuses_the_internet_explorer_fallback(self):
        guilib = importlib.import_module("webview.guilib")

        class Fallback:
            renderer = "mshtml"
        with mock.patch.object(guilib, "initialize", return_value=Fallback()):
            with self.assertRaises(shell.ShellError):
                shell.check_backend()

    def test_a_backend_that_cannot_load_is_refused_with_a_message(self):
        guilib = importlib.import_module("webview.guilib")
        with mock.patch.object(guilib, "initialize", side_effect=OSError("no .NET")):
            with self.assertRaises(shell.ShellError):
                shell.check_backend()

    def test_refuses_an_unguarded_backend(self):
        guilib = importlib.import_module("webview.guilib")

        class Chromium:
            renderer = "edgechromium"
        fake = type(sys)("webview.platforms.edgechromium")
        fake.js_bridge_call = lambda *a: None
        with mock.patch.object(guilib, "initialize", return_value=Chromium()), \
                mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": fake}):
            with self.assertRaises(shell.ShellError):
                shell.check_backend()

    def test_refuses_a_backend_whose_alert_cannot_be_guarded(self):
        guilib = importlib.import_module("webview.guilib")

        class Chromium:
            renderer = "edgechromium"

        def guarded(*a):
            return None
        guarded.unminute_guard = True
        fake = type(sys)("webview.platforms.edgechromium")
        fake.js_bridge_call = guarded                       # calls guarded, alert not
        with mock.patch.object(guilib, "initialize", return_value=Chromium()), \
                mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": fake}):
            with self.assertRaises(shell.ShellError):
                shell.check_backend()


class PreloadTests(unittest.TestCase):
    """#339: the window's backend loads early, on a background thread. The
    window waits for it, then puts every guard in place as before."""

    def setUp(self):
        self.saved = shell._preload["thread"]
        shell._preload["thread"] = None

    def tearDown(self):
        shell.wait_preload()
        shell._preload["thread"] = self.saved

    def test_loads_once_and_wait_returns_only_after_it(self):
        started, release = [], threading.Event()

        def load():
            started.append(threading.current_thread().name)
            release.wait(10)
        with mock.patch.object(shell, "_load_backend", load):
            shell.preload_backend()
            shell.preload_backend()                      # once per process
            waiter = threading.Thread(target=shell.wait_preload)
            waiter.start()
            waiter.join(0.3)
            self.assertTrue(waiter.is_alive())           # still loading
            release.set()
            waiter.join(10)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(started, ["preload-backend"])
        self.assertFalse(shell._preload["thread"].daemon)    # Python's exit waits for it (B1)

    def test_wait_without_a_preload_returns_at_once(self):
        shell.wait_preload()

    @unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
    def test_a_failed_preload_still_refuses_to_start(self):
        # The preload tries, fails quietly on its own thread (nothing reaches
        # the thread's excepthook), and check_backend loads again on the
        # caller's thread and refuses with its message, as before the preload.
        guilib = importlib.import_module("webview.guilib")
        tried, escaped = [], []

        def initialize(gui):
            tried.append(threading.current_thread().name)
            raise OSError("no .NET")
        with mock.patch.object(guilib, "initialize", initialize), \
                mock.patch.dict(sys.modules, {"clr": type(sys)("clr")}), \
                mock.patch.object(threading, "excepthook", escaped.append):
            shell.preload_backend()
            shell.wait_preload()
            self.assertEqual((tried, escaped), (["preload-backend"], []))
            with self.assertRaises(shell.ShellError):
                shell.check_backend()
        self.assertEqual(tried, ["preload-backend", threading.current_thread().name])

    @unittest.skipUnless(HAVE_WEBVIEW and sys.platform == "win32", "pythonnet and .NET")
    def test_a_process_ending_while_the_backend_loads_exits_cleanly(self):
        # #378 review B1: a process that ends while .NET is still loading (a
        # second copy, a workspace that will not open) must not be cut short
        # by pythonnet's unload at exit (exit 127, an unhandled .NET
        # exception). The real backend, no window.
        for delay in (0.05, 0.1, 0.15, 0.2, 0.25, 0.3):
            code = ("import sys, time; from workspace import shell; "
                    f"shell.preload_backend(); time.sleep({delay}); sys.exit(0)")
            r = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, f"after {delay} s: {r.stderr[-800:]}")
            self.assertNotIn("Unhandled Exception", r.stderr)

    def test_pywebview_logs_to_the_stderr_of_the_window(self):
        # #378 review B2: loaded early, pywebview's console handler holds the
        # stream of that moment (None under pythonw). Once the window is made
        # it writes to the current stderr (diag.summarise_output's), with no
        # logging error.
        import io
        import logging
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[pywebview] %(message)s"))
        handler.stream = None                           # webview imported under pythonw
        logger = logging.getLogger("pywebview")
        replaced = io.StringIO()
        with mock.patch.object(logger, "handlers", [handler]), \
                mock.patch.object(sys, "stderr", replaced):
            shell._log_to_current_stderr()
            logger.warning("synthetic warning")
        self.assertIs(handler.stream, replaced)
        self.assertIn("[pywebview] synthetic warning", replaced.getvalue())
        self.assertNotIn("Logging error", replaced.getvalue())

    # #378 review B3: the state a shortcut launch is in. Under pythonw both
    # streams start as None; the preload imports pywebview, whose http module
    # replaces them with NUL (isatty() is True on Windows). The app's output
    # must still be summarised by type, pywebview's records included.
    PYTHONW_OUTPUT = (
        "import json, logging, sys, warnings\n"
        "sys.stdout = sys.stderr = None\n"
        "from workspace import offline_guard\n"
        "offline_guard.install()\n"
        "from workspace import diag, shell\n"
        "shell.preload_backend()\n"
        "shell.wait_preload()\n"
        "streams = [type(sys.stdout).__name__, type(sys.stderr).__name__]\n"
        "class Log:\n"
        "    records = []\n"
        "    def write(self, kind, **fields):\n"
        "        self.records.append((kind, fields.get('type')))\n"
        "log = Log()\n"
        "diag.summarise_output(log)\n"
        "shell._log_to_current_stderr()\n"
        "try:\n"
        "    raise ValueError('synthetic')\n"
        "except ValueError:\n"
        "    logging.getLogger('pywebview').exception('synthetic')\n"
        "warnings.warn('synthetic', UserWarning)\n"
        "out = {'before': streams, 'after': type(sys.stderr).__name__,\n"
        "       'handlers': [type(h.stream).__name__\n"
        "                    for h in logging.getLogger('pywebview').handlers],\n"
        "       'types': sorted({t for k, t in log.records if k == 'output'})}\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(out))\n")

    @unittest.skipUnless(HAVE_WEBVIEW and sys.platform == "win32", "pythonw, pythonnet and .NET")
    def test_output_is_summarised_under_pythonw_after_the_preload(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        self.assertTrue(pythonw.is_file(), "no pythonw.exe beside this Python")
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            r = subprocess.run([str(pythonw), "-B", "-c", self.PYTHONW_OUTPUT, str(result)],
                               cwd=ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0)
            out = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(out["before"], ["TextIOWrapper", "TextIOWrapper"])   # NUL, from pywebview
        self.assertEqual(out["after"], "OutputSummary")
        self.assertEqual(out["handlers"], ["OutputSummary"])
        self.assertEqual(out["types"], ["UserWarning", "ValueError"])

    def test_null_device_streams_count_as_no_console(self):
        from workspace import diag
        null_out, null_err = open(os.devnull, "w"), open(os.devnull, "w")
        try:
            with mock.patch.object(sys, "stdout", null_out), \
                    mock.patch.object(sys, "stderr", null_err):
                diag.summarise_output(object())
                self.assertIsInstance(sys.stdout, diag.OutputSummary)
                self.assertIsInstance(sys.stderr, diag.OutputSummary)
        finally:
            null_out.close()
            null_err.close()

    def test_no_console_leaves_the_handler_alone(self):
        import logging
        handler = logging.StreamHandler()
        before = handler.stream
        with mock.patch.object(logging.getLogger("pywebview"), "handlers", [handler]), \
                mock.patch.object(sys, "stderr", None):
            shell._log_to_current_stderr()
        self.assertIs(handler.stream, before)

    @unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
    def test_the_backend_check_waits_for_the_preload(self):
        guilib = importlib.import_module("webview.guilib")
        order = []

        def initialize(gui):
            order.append("load")
            raise OSError("stop here")
        with mock.patch.object(shell, "wait_preload", lambda: order.append("wait")), \
                mock.patch.object(guilib, "initialize", initialize):
            with self.assertRaises(shell.ShellError):
                shell.check_backend()
        self.assertEqual(order, ["wait", "load"])

    @unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
    def test_the_window_waits_for_the_preload_before_its_guard(self):
        import webview
        order = []
        settings = dict(webview.settings)               # create_window sets three
        try:
            with mock.patch.object(shell, "wait_preload", lambda: order.append("wait")), \
                    mock.patch.object(shell, "_log_to_current_stderr",
                                      lambda: order.append("log")), \
                    mock.patch.object(shell, "install_dispatch_guard",
                                      lambda allowed: order.append("guard")), \
                    mock.patch.object(webview, "create_window", mock.MagicMock()):
                shell.create_window(object(), allowed=frozenset({"status"}), harden=False)
        finally:
            for key, value in settings.items():
                webview.settings[key] = value
        self.assertEqual(order, ["wait", "log", "guard"])

    @unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
    def test_a_backend_loaded_first_still_gets_the_dispatch_guard(self):
        # With the backend module already imported (by the preload), the
        # guard replaces the reference it holds, which check_backend checks.
        import webview.util as util
        original = util.js_bridge_call
        fake = type(sys)("webview.platforms.edgechromium")
        fake.js_bridge_call = original                   # imported by name, unguarded
        try:
            with mock.patch.dict(sys.modules, {"webview.platforms.edgechromium": fake}):
                shell.install_dispatch_guard({"status"})
            self.assertTrue(getattr(fake.js_bridge_call, "unminute_guard", False))
            self.assertIs(fake.js_bridge_call, util.js_bridge_call)
        finally:
            util.js_bridge_call = original


class StorageFolderTests(unittest.TestCase):
    def test_leftovers_are_removed_and_a_fresh_folder_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "webview" / "old"
            (old / "EBWebView").mkdir(parents=True)
            (old / "EBWebView" / "Cache").write_text("x")
            new = shell.storage_folder(tmp)
            self.assertEqual(new.parent, Path(tmp) / "webview")
            self.assertFalse(old.exists())
            self.assertFalse(new.exists())          # WebView2 creates it

    @unittest.skipUnless(os.name == "nt", "NTFS junctions are Windows-only")
    def test_links_are_removed_never_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            precious = Path(tmp) / "Documents"
            (precious / "inner").mkdir(parents=True)
            (precious / "client.docx").write_text("keep me")
            ws = Path(tmp) / "ws"
            ws.mkdir()
            r = mklink_junction(ws / "webview", precious, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            shell.storage_folder(ws)                       # "webview" itself is a junction
            self.assertTrue((precious / "client.docx").exists())
            self.assertTrue((precious / "inner").is_dir())
            self.assertFalse(os.path.isjunction(ws / "webview"))
            inner = ws / "webview" / "old-profile"
            r = mklink_junction(inner, precious, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            shell.storage_folder(ws)                       # a junction inside it
            self.assertFalse(inner.exists())
            self.assertTrue((precious / "client.docx").exists())


class LockFailureTests(unittest.TestCase):
    """If the navigation lock cannot attach, the window is closed, and if even
    that fails the process ends: it never runs without the lock."""

    def setUp(self):
        shell.lock_failed.clear()

    def tearDown(self):
        shell.lock_failed.clear()

    def window(self):
        class Window:
            native = object()                 # no WebView2 control: the lock fails
        return Window()

    def test_window_is_closed_when_the_lock_fails(self):
        with mock.patch.object(shell, "_post_close") as close:
            shell._lock_once(self.window())
        close.assert_called_once()
        self.assertTrue(shell.lock_failed)

    def test_process_ends_if_the_window_cannot_be_closed(self):
        with mock.patch.object(shell, "_post_close", side_effect=RuntimeError), \
                mock.patch.object(shell.os, "_exit", side_effect=SystemExit) as end:
            with self.assertRaises(SystemExit):
                shell._lock_once(self.window())
        end.assert_called_once_with(3)


class StartUpTests(unittest.TestCase):
    """Run in a child process: importing workspace.__main__ installs the
    network guard, which must not affect this test process."""

    def run_main(self, code, env=None):
        import subprocess
        root = Path(__file__).resolve().parents[1]
        return subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_gate_reports_a_bad_location_as_json(self):
        env = {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}
        r = self.run_main("import sys; sys.argv = ['x', '--gate'];"
                          "import workspace.__main__ as m; sys.exit(m.main())", env)
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["kind"], "location")

    RESTORE_LID = ("import json, sys; sys.argv = ['x', '--restore-lid']; "
                   "import workspace.__main__ as m; "
                   "m._app_running = lambda name=None: False; "   # an installed copy may be open
                   "code = m.main(); "
                   "heavy = ('webview', 'numpy', 'torch', 'av', 'workspace.app', "
                   "'workspace.api', 'workspace.shell', 'workspace.worker', "
                   "'workspace.diag'); "
                   "print(json.dumps([n for n in heavy if n in sys.modules])); "
                   "sys.exit(code)")

    def test_restore_lid_prints_a_state_only(self):
        # #191: the uninstaller's switch. No app data folder, then one with no
        # record: a fixed JSON state and exit code, no window or heavy import,
        # and nothing created. The power scheme is never read for these.
        env = {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}
        r = self.run_main(self.RESTORE_LID, env)
        self.assertEqual((r.returncode, r.stdout.splitlines()[0], r.stderr),
                         (1, '{"lid": "location"}', ""))
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_main(self.RESTORE_LID, dict(os.environ, LOCALAPPDATA=tmp))
            self.assertEqual((r.returncode, r.stdout.splitlines(), r.stderr),
                             (0, ['{"lid": "none"}', "[]"], ""))
            self.assertEqual(list(Path(tmp).iterdir()), [])          # nothing created

    def test_restore_lid_leaves_the_lid_alone_for_a_damaged_record(self):
        # A record that cannot say what to put back is removed and nothing is
        # written (a damaged record never reaches the power scheme).
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "Unminute" / "workspace" / "power_restore.json"
            record.parent.mkdir(parents=True)
            record.write_text('{"scheme": "not-a-guid", "ac": 1, "dc": 1}', encoding="utf-8")
            r = self.run_main(self.RESTORE_LID, dict(os.environ, LOCALAPPDATA=tmp))
            self.assertEqual((r.returncode, r.stdout.splitlines(), r.stderr),
                             (2, ['{"lid": "damaged"}', "[]"], ""))
            self.assertFalse(record.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows named mutex")
    def test_restore_lid_stands_aside_while_the_app_is_open(self):
        # The app's own mutex, under a unique name: a record waits for the app.
        name = f"Local\\Unminute.Test.{os.getpid()}"
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "Unminute" / "workspace" / "power_restore.json"
            record.parent.mkdir(parents=True)
            record.write_text("{damaged", encoding="utf-8")
            code = ("import sys; sys.argv = ['x', '--restore-lid']; "
                    "import workspace.__main__ as m; "
                    f"m.MUTEX_NAME = {name!r}; "
                    f"held = m._single_instance({name!r}); "
                    "sys.exit(m.main())")
            r = self.run_main(code, dict(os.environ, LOCALAPPDATA=tmp))
            self.assertEqual((r.returncode, r.stdout.strip(), r.stderr),
                             (3, '{"lid": "running"}', ""))
            self.assertTrue(record.exists())

    # #339: the window's backend starts loading only once the launcher pass has
    # decided this process opens the window. launch.start, the backend load,
    # the mutex and the message are stand-ins: nothing loads, opens or shows.
    PRELOAD_ORDER = ("import sys; sys.argv = ['x']; "
                     "import workspace.__main__ as m; from workspace import launch, shell; "
                     "seen = []; "
                     "shell.preload_backend = lambda: seen.append('preload'); "
                     "m._single_instance = lambda name=None: seen.append('mutex'); "
                     "m._message = lambda text: seen.append('message'); "
                     "launch.start = lambda argv: launch.Outcome(exit={exit}); "
                     "print(m.main(), seen)")

    def test_the_backend_loads_after_the_launcher_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, LOCALAPPDATA=tmp)
            r = self.run_main(self.PRELOAD_ORDER.format(exit=None), env)
            self.assertEqual(r.stdout.strip(), "0 ['preload', 'mutex', 'message']", r.stderr)
            r = self.run_main(self.PRELOAD_ORDER.format(exit=1), env)   # handed over
            self.assertEqual(r.stdout.strip(), "1 []", r.stderr)

    @unittest.skipUnless(sys.platform == "win32", "Windows named mutex")
    def test_only_one_copy_holds_the_workspace(self):
        # A unique name: an installed, running copy of the app holds the real one.
        name = f"Local\\Unminute.Test.{os.getpid()}"
        r = self.run_main("import workspace.__main__ as m; "
                          f"a = m._single_instance({name!r}); "
                          f"b = m._single_instance({name!r}); "
                          "print(a is not None, b is None)")
        self.assertEqual(r.stdout.strip(), "True True", r.stderr)


class WorkspaceLocationTests(unittest.TestCase):
    def check(self, env):
        from workspace import store
        with mock.patch.dict(os.environ, env, clear=True):
            return store.local_appdata()

    def test_missing_relative_or_network_location_is_refused(self):
        from workspace.store import StoreError
        for env in ({}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": "relative\\path"},
                    {"LOCALAPPDATA": "\\\\server\\share\\AppData\\Local"}):
            with self.assertRaises(StoreError, msg=repr(env)):
                self.check(env)

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_location_inside_onedrive_is_refused(self):
        from workspace.store import StoreError
        with self.assertRaises(StoreError):
            self.check({"LOCALAPPDATA": "C:\\Users\\a\\OneDrive - Firm\\AppData\\Local",
                        "OneDriveCommercial": "C:\\Users\\a\\OneDrive - Firm"})
        ok = self.check({"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local",
                         "OneDriveCommercial": "C:\\Users\\a\\OneDrive - Firm"})
        self.assertEqual(ok, Path("C:\\Users\\a\\AppData\\Local"))

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_a_onedrive_variable_without_a_drive_still_counts(self):
        # Python 3.13 and later no longer call "\Users\a\..." absolute (#111 B4).
        from workspace.store import StoreError
        drive = os.path.splitdrive(os.getcwd())[0]          # where realpath puts it
        with self.assertRaises(StoreError):
            self.check({"LOCALAPPDATA": f"{drive}\\Users\\a\\OneDrive - Firm\\AppData\\Local",
                        "OneDriveCommercial": "\\Users\\a\\OneDrive - Firm"})

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_volume_paths_are_local_and_unc_paths_are_not(self):
        from workspace import store
        self.assertFalse(store._network_path("\\\\?\\Volume{1234}\\Users\\a\\AppData"))
        self.assertFalse(store._network_path("C:\\Users\\a\\AppData\\Local"))
        for unc in ("\\\\server\\share\\a", "\\\\?\\UNC\\server\\share\\a", "//server/share"):
            self.assertTrue(store._network_path(unc), unc)

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_a_onedrive_variable_at_the_profile_or_a_drive_root_is_ignored(self):
        local = "C:\\Users\\a\\AppData\\Local"
        for synced in ("C:\\Users\\a", "C:\\Users\\a\\", "C:\\", "C:\\Users"):
            got = self.check({"LOCALAPPDATA": local, "OneDrive": synced,
                              "USERPROFILE": "C:\\Users\\a"})
            self.assertEqual(got, Path(local), synced)

    @unittest.skipUnless(os.name == "nt", "NTFS junctions are Windows-only")
    def test_a_junction_into_onedrive_is_refused(self):
        from workspace.store import StoreError
        with tempfile.TemporaryDirectory() as tmp:
            synced = Path(tmp) / "OneDrive - Firm"
            (synced / "AppData").mkdir(parents=True)
            link = Path(tmp) / "Local"
            r = mklink_junction(link, synced / "AppData", capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            try:
                with self.assertRaises(StoreError):
                    self.check({"LOCALAPPDATA": str(link), "OneDrive": str(synced)})
            finally:
                os.rmdir(link)

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_a_mapped_network_drive_is_refused(self):
        from workspace import store
        from workspace.store import StoreError
        with mock.patch.object(store, "_remote_drive", return_value=True):
            with self.assertRaises(StoreError):
                self.check({"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local"})


@unittest.skipUnless(os.name == "nt", "NTFS junctions are Windows-only")
class RedirectedDataFolderTests(unittest.TestCase):
    """#219 (D3): %LOCALAPPDATA% itself is local, but a junction at Unminute or
    at Unminute\\workspace takes the workspace into OneDrive or onto a network
    drive. Junctions need no privilege, so these run on every Windows machine.
    Only Unminute\\models may be redirected."""

    SYNCED = "OneDrive - Synthetic Firm"

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.synced = self.tmp / self.SYNCED
        self.share = self.tmp / "Share"
        self.cases = 0
        self.fresh_local()

    def fresh_local(self):
        """A new, empty LOCALAPPDATA for the next case."""
        self.cases += 1
        self.local = self.tmp / f"Local {self.cases}"
        self.local.mkdir()

    def junction(self, at, target, how="drive"):
        """LOCALAPPDATA\\<at> made a junction to target (created first)."""
        return self.make_junction(self.local / at, target, how)

    def make_junction(self, link, target, how="drive"):
        """link made a junction to target (created first), the target spelt
        as how says (see spelt)."""
        link.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        r = mklink_junction(link, self.spelt(target, how), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # Removes the junction only, never its target, and before the
        # temporary folder goes (cleanups run last in, first out).
        self.addCleanup(lambda: os.path.isjunction(link) and os.rmdir(link))
        return link

    def spelt(self, folder, how):
        """folder as given to mklink /J: "drive" (C:\\...), "\\\\?\\" (the
        long-path prefix before the drive; see BROKEN_SHAPES for what mklink
        stores) or "volume GUID" (the volume's own name,
        \\\\?\\Volume{...}\\..., which realpath keeps as written)."""
        if how == "drive":
            return str(folder)
        if how == "\\\\?\\":
            return "\\\\?\\" + str(folder)
        import ctypes
        drive, rest = os.path.splitdrive(str(folder))
        buffer = ctypes.create_unicode_buffer(64)
        made = ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive + "\\", buffer, 64)
        self.assertTrue(made and buffer.value.startswith("\\\\?\\Volume{"), how)
        return buffer.value + rest.lstrip("\\")

    # Ways a data folder junction's target can be missing (#266, from the
    # #265 reviews), each made under base by broken(). Of the spellings, only
    # "volume GUID" stores a valid target spelt differently from "drive"
    # (\??\Volume{...}\... against \??\C:\...). What "\\?\ prefix" stores
    # depends on how mklink is called. Through mklink_junction (as here),
    # given \\?\C:\x it stores \??\C:\?\C:\x: os.readlink shows it, and
    # os.stat fails with WinError 123, not 2 as for "drive" (seen on Windows
    # 11 builds 26200 and 26300). Run from PowerShell, mklink /J stored a
    # valid spelling (build 26300). Here it is one more unreachable target.
    BROKEN_SHAPES = ("missing", "link to a broken link", "inside a broken link",
                     "volume GUID", "\\\\?\\ prefix")

    def broken(self, at, shape, base):
        """LOCALAPPDATA\\<at> made a junction whose target, under base, is
        missing in the given shape. Returns what must stay uncreated."""
        n = self.cases
        gone = base / f"Gone {n}"
        hop = self.tmp / f"Hop {n}"
        if shape in ("missing", "volume GUID", "\\\\?\\ prefix"):
            how = {"volume GUID": "volume GUID", "\\\\?\\ prefix": "\\\\?\\"}.get(shape, "drive")
            link = self.junction(at, gone, how)
            os.rmdir(gone)
        elif shape == "link to a broken link":      # Unminute -> Hop -> (Gone, removed)
            self.make_junction(hop, gone)
            link = self.junction(at, hop)
            os.rmdir(gone)
        else:                                       # Unminute -> Hop\Moved; Hop -> (Gone, removed)
            (gone / "Moved").mkdir(parents=True)
            self.make_junction(hop, gone)
            link = self.junction(at, hop / "Moved")
            os.rmdir(gone / "Moved")
            os.rmdir(gone)
        self.assertTrue(os.path.lexists(link), shape)
        self.assertFalse(os.path.exists(link), shape)    # its target is missing
        return gone

    def test_a_data_folder_junction_whose_target_is_missing_is_refused(self):
        # #266: the app closed without its message (Workspace() failed in
        # mkdir). Now refused with a path-free message that names IT.
        from workspace import store
        from workspace.store import StoreError
        for at in ("Unminute", "Unminute\\workspace"):
            for shape in self.BROKEN_SHAPES:
                with self.subTest(at=at, shape=shape):
                    self.fresh_local()
                    gone = self.broken(at, shape, self.tmp / "Elsewhere")
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env())
                    self.assertEqual(str(caught.exception), store.BROKEN_LINK)
                    self.assert_path_free(str(caught.exception))
                    self.assertFalse(os.path.lexists(gone))      # nothing made there

    def test_a_missing_onedrive_target_spelt_another_way_is_refused(self):
        # The #265 round 2 shapes: a target inside a broken junction, or spelt
        # with the volume's GUID name, hid that the missing target was inside
        # OneDrive (before #266 these two at either level were not refused).
        # Every shape is refused, by either message, and nothing is created
        # in OneDrive.
        from workspace import store
        from workspace.store import StoreError
        for at in ("Unminute", "Unminute\\workspace"):
            for shape in self.BROKEN_SHAPES:
                with self.subTest(at=at, shape=shape):
                    self.fresh_local()
                    gone = self.broken(at, shape, self.synced)
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env())
                    self.assertIn(str(caught.exception),
                                  (store.BROKEN_LINK, store.REDIRECTED["onedrive"]))
                    self.assert_path_free(str(caught.exception))
                    self.assertFalse(os.path.lexists(gone))      # nothing made in OneDrive

    def test_a_junction_this_account_may_not_open_is_not_called_broken(self):
        # _broken_link's documented rule: a target refused for access is not
        # missing, so it keeps its own outcome rather than BROKEN_LINK.
        real_stat = os.stat
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                link = self.junction(at, self.tmp / "Elsewhere" / f"Locked {self.cases}")
                key = os.path.normcase(str(link))

                refused = []

                def stat(path, *args, _key=key, _refused=refused, **kwargs):
                    if os.path.normcase(os.fspath(path)) == _key:
                        _refused.append(path)
                        raise PermissionError(13, "Access is denied")
                    return real_stat(path, *args, **kwargs)
                with mock.patch("os.stat", stat):
                    self.assertEqual(self.default_root(self.env()),
                                     self.local / "Unminute" / "workspace")
                self.assertTrue(refused, "the patched stat never saw the junction")

    def test_a_working_data_folder_junction_is_not_called_broken(self):
        # A junction to a local folder that exists is still allowed (#219
        # refuses only OneDrive and network drives), and so is a data folder
        # not created yet.
        self.assertEqual(self.default_root(self.env()), self.local / "Unminute" / "workspace")
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                self.junction(at, self.tmp / "Elsewhere" / f"Here {self.cases}")
                self.assertEqual(self.default_root(self.env()),
                                 self.local / "Unminute" / "workspace")

    def env(self, var="OneDriveCommercial"):
        return {"LOCALAPPDATA": str(self.local), var: str(self.synced)}

    def child_env(self):
        """This process's environment for a child, with the test's folders."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")}
        env.update(self.env())
        return env

    def default_root(self, env):
        from workspace import store
        with mock.patch.dict(os.environ, env, clear=True):
            return store.default_root()

    def on_share(self):
        """_remote_drive as if self.share were a mapped network drive."""
        from workspace import store
        share = store._folder_key(os.path.realpath(self.share))
        return mock.patch.object(store, "_remote_drive",
                                 lambda path: store._folder_key(path).startswith(share))

    def assert_path_free(self, message):
        # P3: no folder name from this computer, only the app's own words.
        for part in (str(self.tmp), self.tmp.name, self.SYNCED, "Share", "Local"):
            self.assertNotIn(part, message)
        self.assertIn("Ask your IT team", message)

    def test_a_data_folder_junction_into_onedrive_is_refused(self):
        from workspace import store
        from workspace.store import StoreError
        for at in ("Unminute", "Unminute\\workspace"):
            for var in ("OneDriveCommercial", "OneDrive"):
                with self.subTest(at=at, var=var):
                    self.fresh_local()
                    target = self.synced / f"Moved {self.cases}"
                    self.junction(at, target)
                    with mock.patch.dict(os.environ, self.env(var), clear=True):
                        self.assertEqual(store.local_appdata(), self.local)  # itself is fine
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env(var))
                    self.assertEqual(str(caught.exception), store.REDIRECTED["onedrive"])
                    self.assert_path_free(str(caught.exception))
                    self.assertEqual(list(target.iterdir()), [])     # nothing written there

    def test_a_junction_into_onedrive_whose_target_is_missing_is_refused(self):
        # Round 1 review: realpath stops at a missing target and returns it as
        # stored, so a junction or 8.3 short name inside that stored target
        # hid OneDrive, and the app then failed in mkdir without its message.
        import ctypes
        from workspace import store
        from workspace.store import StoreError
        alias = self.tmp / "alias"
        self.synced.mkdir(parents=True, exist_ok=True)
        made = mklink_junction(alias, self.synced, capture_output=True, text=True)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.addCleanup(lambda: os.path.isjunction(alias) and os.rmdir(alias))
        spellings = {"junction": alias}
        # The short name, where the volume makes them (CI's may not).
        buffer = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(str(self.synced), buffer, 1024)
        if buffer.value and Path(buffer.value).name.lower() != self.SYNCED.lower():
            spellings["short name"] = Path(buffer.value)
        for at in ("Unminute", "Unminute\\workspace"):
            for how, synced in spellings.items():
                with self.subTest(at=at, how=how):
                    self.fresh_local()
                    moved = f"Moved {self.cases}"
                    link = self.junction(at, synced / moved)
                    os.rmdir(self.synced / moved)                # the target is gone
                    self.assertFalse(os.path.exists(link))
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env())
                    self.assertEqual(str(caught.exception), store.REDIRECTED["onedrive"])
                    self.assert_path_free(str(caught.exception))
                    self.assertFalse((self.synced / moved).exists())   # nothing made there

    def test_a_data_folder_junction_onto_a_network_drive_is_refused(self):
        from workspace import store
        from workspace.store import StoreError
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                self.junction(at, self.share / f"Moved {self.cases}")
                with self.on_share():
                    with self.assertRaises(StoreError) as caught:
                        self.default_root(self.env())
                self.assertEqual(str(caught.exception), store.REDIRECTED["network"])
                self.assert_path_free(str(caught.exception))

    def test_redirecting_only_the_models_folder_is_allowed(self):
        models = self.junction("Unminute\\models", self.synced / "Models")
        expected = self.local / "Unminute" / "workspace"
        self.assertEqual(self.default_root(self.env()), expected)
        # The model cache the app uses is still the redirected folder. In a
        # child process: importing workspace.__main__ installs the network guard.
        r = subprocess.run([sys.executable, "-c",
                            "import workspace.__main__ as m; print(m.model_cache())"],
                           cwd=ROOT, env=self.child_env(), capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), models)
        os.rmdir(models)
        self.junction("Unminute\\models", self.share / "Models")
        with self.on_share():
            self.assertEqual(self.default_root(self.env()), expected)

    @unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
    def test_the_app_does_not_open_a_redirected_workspace(self):
        from workspace import app, store
        from workspace.store import StoreError
        target = self.synced / "Unminute"
        self.junction("Unminute", target)
        with mock.patch.dict(os.environ, self.env(), clear=True):
            with self.assertRaises(StoreError) as caught:
                app.Workspace(cache_dir=self.tmp / "models", start=False, battery=False,
                              protector=keyprotect.TestProtector())
        self.assertEqual(str(caught.exception), store.REDIRECTED["onedrive"])
        self.assertEqual(list(target.iterdir()), [])                 # no workspace.db there

    def run_main(self, flag, before=""):
        """The app's own start in a child process (importing workspace.__main__
        installs the network guard), with this test's folders."""
        return subprocess.run([sys.executable, "-c",
                               f"import json, sys; sys.argv = ['x', {flag!r}]; "
                               f"import workspace.__main__ as m; {before}sys.exit(m.main())"],
                              cwd=ROOT, env=self.child_env(), capture_output=True,
                              text=True, timeout=120)

    def test_status_and_health_report_it_without_a_path(self):
        self.junction("Unminute", self.synced / "Unminute")
        status = self.run_main("--status")
        self.assertEqual(status.returncode, 1, status.stderr)
        self.assert_path_free(status.stdout)
        self.assertIn("redirected into OneDrive", json.loads(status.stdout)["error"])
        health = self.run_main("--health")
        self.assertEqual(json.loads(health.stdout.strip().splitlines()[-1])["workspace"],
                         "location", health.stderr)

    def test_status_health_and_gate_report_a_broken_link_without_a_path(self):
        # #266 (P3): the same message as at start, and no folder in either.
        from workspace import store
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                self.broken(at, "missing", self.tmp / "Elsewhere")
                status = self.run_main("--status")
                self.assertEqual((status.returncode, status.stderr), (1, ""))
                self.assert_path_free(status.stdout)
                self.assertEqual(json.loads(status.stdout)["error"], store.BROKEN_LINK)
                health = self.run_main("--health")
                self.assertEqual((health.returncode, health.stderr), (1, ""))
                for part in (str(self.tmp), self.tmp.name, "Elsewhere", "Gone"):
                    self.assertNotIn(part, health.stdout)        # states only
                result = json.loads(health.stdout.strip().splitlines()[-1])
                self.assertEqual((result["workspace"], result["healthy"]), ("location", False))
                # The installer's workspace check stops with the same message.
                gate = self.run_main("--gate")
                self.assertEqual((gate.returncode, gate.stderr), (6, ""))
                result = json.loads(gate.stdout.strip().splitlines()[-1])
                self.assertEqual((result["kind"], result["message"]),
                                 ("location", store.BROKEN_LINK))

    @unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
    def test_the_app_names_a_broken_link_at_start(self):
        # #266: the message box, not a silent exit with a traceback. The box
        # is replaced by a print, and the single-instance check by a stand-in
        # (an installed copy may be open). Should the workspace ever open,
        # the start stops before any window (diag.install comes first).
        from workspace import store
        stand_ins = ("m._single_instance = lambda name=None: True; "
                     "m._message = lambda text: print(json.dumps(text)); "
                     "import workspace.diag as d; "
                     "d.install = lambda *a, **k: sys.exit(9); ")
        for at in ("Unminute", "Unminute\\workspace"):
            with self.subTest(at=at):
                self.fresh_local()
                gone = self.broken(at, "missing", self.tmp / "Elsewhere")
                r = self.run_main("--start", stand_ins)
                self.assertEqual((r.returncode, r.stderr), (1, ""))
                self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]),
                                 store.BROKEN_LINK)
                self.assertFalse(os.path.lexists(gone))          # nothing made there


class _Tripwire:
    """A stand-in Workspace for the bridge gate (#111 C4a): touching anything
    but what make_api sets up fails the test."""
    _allowed = ("locked", "log", "recording_indicator")

    def __init__(self, locked):
        object.__setattr__(self, "locked", locked)
        object.__setattr__(self, "log", None)
        object.__setattr__(self, "touched", [])

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"the Workspace was called: {name}")


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class LockedBridgeTests(unittest.TestCase):
    """While locked, the page reaches only LOCKED_METHODS; every other bridge
    method refuses with the fixed text without calling the Workspace (I3)."""

    def call_all(self, page, names):
        import inspect
        out = {}
        for name in sorted(names):
            method = getattr(page, name)
            # Arguments of the right count; their values never matter here.
            params = [p for p in inspect.signature(method).parameters.values()
                      if p.default is inspect.Parameter.empty]
            try:
                out[name] = method(*([None] * len(params)))
            except AssertionError as exc:
                out[name] = exc
        return out

    def test_every_content_method_is_refused_while_locked(self):
        from workspace import api
        from workspace.app import LOCKED_TEXT
        self.assertLessEqual(api.LOCKED_METHODS, api.API_METHODS)
        gated = api.API_METHODS - api.LOCKED_METHODS - {"pywebviewEventHandler"}
        self.assertIn("meetings", gated)
        self.assertIn("continue_despite_damage", gated)     # never offered while locked
        ws = _Tripwire(locked=RuntimeError("locked"))
        page = api.make_api(ws)
        results = self.call_all(page, gated)
        self.assertEqual({n: {"ok": False, "error": LOCKED_TEXT} for n in gated}, results)
        self.assertEqual([], ws.touched)
        # Unlocked, the same calls reach the Workspace: the gate refused them.
        ws = _Tripwire(locked=None)
        results = self.call_all(api.make_api(ws), gated)
        reached = {n for n, r in results.items()
                   if isinstance(r, AssertionError) or r.get("error") != LOCKED_TEXT}
        self.assertEqual(gated, reached)

    def test_the_page_keeps_the_signatures_pywebview_reads(self):
        import inspect

        from workspace import api
        ws = _Tripwire(locked=None)
        page = api.make_api(ws)
        for name in api.API_METHODS - {"pywebviewEventHandler"}:
            fn = getattr(type(page), name)
            # pywebview builds the page's stubs from getfullargspec.
            self.assertNotIn("args", inspect.getfullargspec(fn).args, name)
        self.assertEqual(["self", "mid", "start", "count", "version"],
                         inspect.getfullargspec(type(page).transcript).args)

    @unittest.skipUnless(HAVE_WEBVIEW, "pywebview not installed")
    def test_a_drop_imports_nothing_while_locked(self):
        from workspace import api

        class Events:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        window = mock.Mock()
        window._drop_enabled = False
        window.dom.document.events.dragover = Events()
        window.dom.document.events.drop = drops = Events()
        ws = mock.Mock(locked=RuntimeError("locked"))
        api.enable_drop(window, ws)
        event = {"dataTransfer": {"files": [{"pywebviewFullPath": "C:\\x\\a.wav"}]}}
        drops.handlers[0].callback(event)
        ws.import_paths.assert_not_called()
        ws.locked = None
        drops.handlers[0].callback(event)
        ws.import_paths.assert_called_once_with(["C:\\x\\a.wav"])


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class StoreTempTests(unittest.TestCase):
    def test_temporary_data_stays_in_memory(self):
        from workspace.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(Path(tmp) / "workspace.db", protector=keyprotect.TestProtector())
            try:
                self.assertEqual(s.db.execute("PRAGMA temp_store").fetchone()[0], 2)
            finally:
                s.close()


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SettingsAndTestEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_test_engines_need_the_test_flag(self):
        from workspace.app import Settings, WorkspaceError
        with self.assertRaises(WorkspaceError):
            Settings(self.path).update(model="test:os:getcwd")
        Settings(self.path, allow_test_engines=True).update(model="test:os:getcwd")

    def test_saved_values_are_checked_when_loaded(self):
        from workspace.app import DEFAULT_SETTINGS, Settings
        self.path.write_text(json.dumps({"model": "test:antigravity:x", "mode": "turbo",
                                         "speakers": "yes", "microphone": True,
                                         "unknown": 1}), encoding="utf-8")
        s = Settings(self.path)
        self.assertEqual(s["model"], DEFAULT_SETTINGS["model"])
        self.assertEqual(s["mode"], DEFAULT_SETTINGS["mode"])
        self.assertEqual(s["speakers"], DEFAULT_SETTINGS["speakers"])
        self.assertIs(s["microphone"], True)               # a valid value is kept
        self.assertEqual(sorted(s.repaired), ["mode", "model", "speakers"])

    def test_damaged_settings_file_falls_back_to_defaults(self):
        from workspace.app import DEFAULT_SETTINGS, Settings
        for content in ("not json", "[1, 2]", "null"):
            self.path.write_text(content, encoding="utf-8")
            self.assertEqual(Settings(self.path).values, DEFAULT_SETTINGS)

    def test_engine_loader_refuses_test_engines_without_the_flag(self):
        from workspace.asr_engines import ModelLoadError
        from workspace import engines
        with self.assertRaises(ModelLoadError):
            engines.load_engine("test:os:getcwd", self.tmp.name, 1)
        with self.assertRaises(ModelLoadError):
            engines._test_factory("test:os:_exit", False)

    def test_workspace_refuses_a_test_speaker_model_without_the_flag(self):
        from workspace.app import Workspace
        with self.assertRaises(ValueError):
            Workspace(Path(self.tmp.name) / "ws", Path(self.tmp.name) / "m", start=False,
                      speaker_model="test:fake_worker_engines:fake_diarizer",
                      protector=keyprotect.TestProtector())


class EnvironmentTokenTests(unittest.TestCase):
    def test_token_leaves_the_environment_and_is_not_kept(self):
        with mock.patch.dict(os.environ, {"HF_TOKEN": " hf_example ",
                                          "HUGGING_FACE_HUB_TOKEN": "hf_example"}):
            credentials.drop_environment_token()
            self.assertNotIn("HF_TOKEN", os.environ)
            self.assertNotIn("HUGGING_FACE_HUB_TOKEN", os.environ)
        self.assertFalse(hasattr(credentials, "earlier_token"))
        self.assertNotIn("hf_example", repr(vars(credentials)))

    def test_no_token_is_fine(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            credentials.drop_environment_token()
            self.assertNotIn("HF_TOKEN", os.environ)

    def test_the_window_process_and_its_children_never_see_it(self):
        # Importing workspace.__main__ (as the app starts) drops it before any
        # child process starts; run in a child so this process keeps its guard off.
        import subprocess
        root = Path(__file__).resolve().parents[1]
        code = ("import os, subprocess, sys\n"
                "import workspace.__main__\n"
                "names = ('HF_TOKEN', 'HUGGING_FACE_HUB_TOKEN')\n"
                "print(any(n in os.environ for n in names))\n"
                "r = subprocess.run([sys.executable, '-c', "
                "'import os; print(any(n in os.environ for n in "
                "(\"HF_TOKEN\", \"HUGGING_FACE_HUB_TOKEN\")))'], "
                "capture_output=True, text=True)\n"
                "print(r.stdout.strip())\n")
        env = dict(os.environ, HF_TOKEN="hf_example", HUGGING_FACE_HUB_TOKEN="hf_example")
        r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.stdout.split(), ["False", "False"], r.stderr)
        self.assertNotIn("hf_example", r.stdout + r.stderr)


# -- keeping the installer tests off the real install (#111 B6a, B6b) -----------------
# On 23 October 2026 a local test run uninstalled the owner's real per-user install.
# So every test here that runs install.ps1 or uninstall.ps1 runs a scratch copy
# (scratch_text: its Start menu folder and uninstall key swapped for a scratch folder
# and a throwaway HKCU key, and machine mode's Program Files, all-users Start menu
# folder, HKLM uninstall entry and Event Log source for scratch folders and throwaway
# HKCU keys) through run_installer, which refuses, before anything runs, unless the
# copy, the install folder (or, for a -Machine run, every machine place), LOCALAPPDATA
# and APPDATA are all scratch, and fails the test if the real install, per user or
# machine (its folder, Start menu shortcut, uninstall entry, pins or Event Log
# source), changed while the copy ran. The real places come from the shell's known
# folders, which do not read LOCALAPPDATA or APPDATA, so a test's scratch variables
# cannot hide them. InstallerIsolationTests proves all of this.

PROGRAMS_CALL = '[Environment]::GetFolderPath("Programs")'
REAL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
REAL_KEY_LINE = '$RegKey = "HKCU:\\' + REAL_KEY + '"'
# The copies' uninstall key: absent unless a test creates it, and removed after.
TEST_KEY = rf"Software\Unminute-UnitTest-uninstall-{os.getpid()}"
TEST_KEY_LINE = '$RegKey = "HKCU:\\' + TEST_KEY + '"'
# How uninstall.ps1 reads the roaming profile folder, where pins live (#111 B8b):
# uninstaller_copy names a scratch folder there instead.
ROAMING_READ = "$env:APPDATA"
# Where this account's TaskBar and StartMenu pins live, inside its roaming folder.
REAL_PINNED = ("Microsoft", "Internet Explorer", "Quick Launch", "User Pinned")
# Machine mode's places (#111 B6b), each set once at the top of both scripts, and
# the throwaway HKCU keys the copies use instead of the HKLM ones.
PROGRAM_FILES_LINE = '$ProgramFilesFolder = [Environment]::GetFolderPath("ProgramFiles")'
COMMON_PROGRAMS_LINE = '$CommonProgramsFolder = [Environment]::GetFolderPath("CommonPrograms")'
REAL_MACHINE_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Unminute"
REAL_EVENT_KEY = r"SYSTEM\CurrentControlSet\Services\EventLog\Application\Unminute"
MACHINE_KEY_LINE = '$MachineRegKey = "HKLM:\\' + REAL_MACHINE_KEY + '"'
EVENT_KEY_LINE = '$EventSourceKey = "HKLM:\\' + REAL_EVENT_KEY + '"'
TEST_MACHINE_KEY = TEST_KEY + r"-machine\Uninstall\Unminute"
TEST_EVENT_KEY = TEST_KEY + r"-machine\EventLog\Unminute"
# The runtime firewall rule (#111 B7b): the rules' group, set once in both scripts'
# shared machine section, and the policy key install.ps1 reads to warn. A copy names
# a throwaway group and HKCU key instead, and every Windows Firewall command that
# changes something ((New|Set|Remove|Enable|Disable|Copy|Rename)-NetFirewall*, in any
# letter case, Set-NetFirewallProfile included; #455) is renamed to one that does not
# exist (FIREWALL_STAND_IN), so no test here writes this computer's firewall, even on
# CI's elevated runner. Reading it (Get-NetFirewallRule, Get-NetFirewallProfile) is
# left as it is. A copy that still holds such a command, names a firewall command
# through its module (NetSecurity\New-NetFirewallRule), or reaches the firewall by
# netsh or its COM interface (HNetCfg) is refused (check_scratch_run).
FIREWALL_GROUP_LINE = '$FirewallGroup = "Unminute runtime"'
TEST_FIREWALL_GROUP = f"Unminute-UnitTest-firewall-{os.getpid()}"
POLICY_KEY_LINE = '$PolicyKey = "HKLM:\\SOFTWARE\\Policies\\Unminute"'
TEST_POLICY_KEY = TEST_KEY + "-policy"
FIREWALL_STAND_IN = "Stop-UnminuteTestFirewall"
FIREWALL_WRITE = re.compile(r"(?i)\b(?:New|Set|Remove|Enable|Disable|Copy|Rename)-NetFirewall\w*")
# A firewall command, or its stand-in, named through a module (module\command).
FIREWALL_QUALIFIED = re.compile(r"(?i)\\\s*(?:[a-z]+-NetFirewall|" + FIREWALL_STAND_IN + ")")
# What a copy may not hold: the ways a script reaches this account's own Start menu
# or uninstall entries, or this computer's Program Files, all-users Start menu, HKLM
# uninstall entries, Event Log sources, policy key or firewall rules.
REAL_REACH = ("GetFolderPath", "SpecialFolder", "Start Menu\\", "CurrentVersion\\Uninstall",
              "Registry::", "Services\\EventLog", "HKLM:\\SYSTEM",
              "HKLM:\\SOFTWARE\\Microsoft\\Windows", "HKLM:\\SOFTWARE\\Policies", "HKEY_",
              "$env:Program", "$env:ALLUSERSPROFILE", "$env:PUBLIC", "CreateSubKey",
              "DeleteSubKey", "DeleteValue", "EventSource(", "New-EventLog", "Remove-EventLog",
              "Unminute runtime", "netsh", "HNetCfg")
# How a copy's machine places read (check_scratch_run).
MACHINE_PLACES = (("Program Files", r'(?m)^\$ProgramFilesFolder = "([^"]*)"\r?$'),
                  ("the all-users Start menu folder",
                   r'(?m)^\$CommonProgramsFolder = "([^"]*)"\r?$'))
MACHINE_KEYS = (("the machine uninstall key", r'(?m)^\$MachineRegKey = "([^"]*)"\r?$',
                 "HKCU:\\" + TEST_MACHINE_KEY),
                ("the Event Log source key", r'(?m)^\$EventSourceKey = "([^"]*)"\r?$',
                 "HKCU:\\" + TEST_EVENT_KEY))
# The firewall group and the policy key (#111 B7b): wherever a copy sets one, it is
# the throwaway one; a -Machine run needs the group (both scripts set it).
FIREWALL_PLACES = (("firewall group", r'(?m)^\$FirewallGroup = "([^"]*)"\r?$',
                    TEST_FIREWALL_GROUP, True),
                   ("policy key", r'(?m)^\$PolicyKey = "([^"]*)"\r?$',
                    "HKCU:\\" + TEST_POLICY_KEY, False))


def real_places():
    """This account's real install folder, data folder, Start menu Programs folder
    and roaming folder, and this computer's Program Files (both views) and all-users
    Start menu Programs folder, from the shell (SHGetFolderPathW), never from
    LOCALAPPDATA or APPDATA."""
    import ctypes

    def known(csidl):
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) or not buf.value:
            raise OSError("the shell did not give one of this account's folders")
        return Path(buf.value)

    local = known(0x1C)                         # CSIDL_LOCAL_APPDATA
    program_files = known(0x26)                 # CSIDL_PROGRAM_FILES
    places = {"install": local / "Programs" / "Unminute", "data": local / "Unminute",
              "programs": known(0x02), "roaming": known(0x1A),   # CSIDL_PROGRAMS, CSIDL_APPDATA
              "program_files": program_files,
              "common_programs": known(0x17)}           # CSIDL_COMMON_PROGRAMS
    try:
        places["program_files_x86"] = known(0x2A)                 # CSIDL_PROGRAM_FILESX86
    except OSError:
        pass                                    # none on a 32-bit Windows
    return places


def _registry_state(hive, subkey):
    """A registry key's values, subkeys and time in the 64-bit view, to compare in
    memory (never shown): None when absent, "unreadable" when it cannot be read."""
    import winreg
    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            info = winreg.QueryInfoKey(key)
            return (info[2], sorted(winreg.EnumValue(key, i) for i in range(info[1])),
                    sorted(winreg.EnumKey(key, i) for i in range(info[0])))
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"


def _names_unminute(entry):
    """A pin's name, or its first megabyte read as ASCII or UTF-16, names Unminute in
    any case (a pin of the real install names its folder): read only, never shown."""
    if "unminute" in entry.name.lower():
        return True
    try:
        with open(entry.path, "rb") as f:
            data = f.read(1 << 20).lower()
    except OSError:
        return False
    return b"unminute" in data or "unminute".encode("utf-16-le") in data


def real_pin_state(roaming):
    """The TaskBar and StartMenu pins of Unminute under this roaming folder, which
    uninstall.ps1 -Offboard removes (#111 B8b; #332 review): each one's name, size,
    time and attributes. Other apps' pins are left out, so the user pinning something
    meanwhile is no change to the real install."""
    state = []
    for kind in ("TaskBar", "StartMenu"):
        folder = Path(roaming).joinpath(*REAL_PINNED, kind)
        try:
            with os.scandir(folder) as entries:
                pins = [(e.name, e.stat(follow_symlinks=False)) for e in entries
                        if e.name.lower().endswith(".lnk") and _names_unminute(e)]
        except FileNotFoundError:
            state.append((kind, None))
            continue
        except OSError:
            state.append((kind, "unreadable"))
            continue
        state.append((kind, sorted((name, st.st_size, st.st_mtime_ns, st.st_file_attributes)
                                   for name, st in pins)))
    return state


def real_install_state():
    """The real install folders' and Start menu shortcuts' times and attributes, per
    user and machine, the real uninstall entries' and Event Log source's values and
    times, and the real pins of Unminute (real_pin_state), to compare in memory
    (never shown)."""
    import winreg
    places = real_places()
    state = []
    for path in (places["install"], places["programs"] / "Unminute.lnk",
                 places["program_files"] / "Unminute", places["common_programs"] / "Unminute.lnk"):
        try:
            st = os.lstat(path)
            state.append((st.st_mtime_ns, st.st_file_attributes))
        except FileNotFoundError:
            state.append(None)
        except OSError:
            state.append("unreadable")
    state.append(_registry_state(winreg.HKEY_CURRENT_USER, REAL_KEY))
    state.append(_registry_state(winreg.HKEY_LOCAL_MACHINE, REAL_MACHINE_KEY))
    state.append(_registry_state(winreg.HKEY_LOCAL_MACHINE, REAL_EVENT_KEY))
    state.extend(real_pin_state(places["roaming"]))
    return state


def _folder(path):
    """path in one spelling: links, junctions and 8.3 short names resolved, so a
    junction in the scratch folder that leads to a real place is seen as that place."""
    return os.path.normcase(os.path.realpath(path)).rstrip("\\") + "\\"


def is_scratch(path, scratch):
    """path is inside the scratch folder and is not, holds no and sits in no real place."""
    if not path or not os.path.isabs(path) or str(path)[:2] in ("\\\\", "//"):
        return False
    full = _folder(path)
    if not full.startswith(_folder(scratch)):
        return False
    return not any(full.startswith(_folder(p)) or _folder(p).startswith(full)
                   for p in real_places().values())


def local_form(folder):
    r"""The local path a network or device form (\\localhost\C$\..., \\?\C:\...)
    names, or None for any other server."""
    import re
    m = re.match(r"^[\\/]{2}(?:\?[\\/](.*)|localhost[\\/]([A-Za-z])\$(?:[\\/](.*))?)$",
                 folder, re.IGNORECASE)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else f"{m.group(2)}:\\{m.group(3) or ''}"


def scratch_text(name, programs, program_files=None, common_programs=None):
    """packaging/<name>'s text with its Start menu folder replaced by programs and its
    uninstall key by TEST_KEY; machine mode's Program Files and all-users Start menu
    folder by program_files and common_programs (by default "Program Files" and
    "CommonPrograms" beside programs), and its HKLM uninstall entry and Event Log
    source by throwaway HKCU keys (TEST_MACHINE_KEY, TEST_EVENT_KEY). Write it into
    the scratch folder and run it with run_installer."""
    programs = Path(programs)
    program_files = Path(program_files or programs.parent / "Program Files")
    common_programs = Path(common_programs or programs.parent / "CommonPrograms")
    text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
    if (text.count(PROGRAMS_CALL), text.count(REAL_KEY_LINE)) != (1, 1):
        raise AssertionError(f"{name} no longer sets its Start menu folder and uninstall "
                             "key once each, so the tests cannot redirect them")
    machine = (PROGRAM_FILES_LINE, COMMON_PROGRAMS_LINE, MACHINE_KEY_LINE, EVENT_KEY_LINE)
    if [text.count(line) for line in machine] != [1, 1, 1, 1]:
        raise AssertionError(f"{name} no longer sets each machine place once, so the tests "
                             "cannot redirect them")
    for folder in (program_files, common_programs):
        folder.mkdir(parents=True, exist_ok=True)
    if name == "uninstall.ps1":
        # The decoder's AppContainer profile (#299): a throwaway name, never the app's.
        if text.count(DECODER_PROFILE_LINE) != 1:
            raise AssertionError("uninstall.ps1 no longer names the decoder's profile once, so "
                                 "the tests cannot redirect it")
        text = text.replace(DECODER_PROFILE_LINE, f'$DecoderProfile = "{DECODER_TEST_PROFILE}"')
    # The runtime firewall rule (#111 B7b): a throwaway group and policy key, and no
    # command that adds or removes a rule.
    policy_lines = 1 if name == "install.ps1" else 0
    if text.count(FIREWALL_GROUP_LINE) != 1 or text.count(POLICY_KEY_LINE) != policy_lines:
        raise AssertionError(f"{name} no longer sets its firewall group (and install.ps1 its "
                             "policy key) once, so the tests cannot redirect them")
    text = (text.replace(FIREWALL_GROUP_LINE, f'$FirewallGroup = "{TEST_FIREWALL_GROUP}"')
            .replace(POLICY_KEY_LINE, f'$PolicyKey = "HKCU:\\{TEST_POLICY_KEY}"'))
    text = FIREWALL_WRITE.sub(FIREWALL_STAND_IN, text)
    return (text.replace(PROGRAMS_CALL, f'"{programs}"').replace(REAL_KEY_LINE, TEST_KEY_LINE)
            .replace(PROGRAM_FILES_LINE, f'$ProgramFilesFolder = "{program_files}"')
            .replace(COMMON_PROGRAMS_LINE, f'$CommonProgramsFolder = "{common_programs}"')
            .replace(MACHINE_KEY_LINE, f'$MachineRegKey = "HKCU:\\{TEST_MACHINE_KEY}"')
            .replace(EVENT_KEY_LINE, f'$EventSourceKey = "HKCU:\\{TEST_EVENT_KEY}"'))


def check_scratch_run(script, install_dir, scratch, env, where, machine=False):
    """Refuses (AssertionError) unless a run of script would write only scratch places."""
    import re
    script = Path(script)
    if not is_scratch(script, scratch) or not script.is_file():
        raise AssertionError("an installer test runs only a copy in its scratch folder")
    text = script.read_text(encoding="utf-8")
    if (TEST_KEY_LINE not in text or any(r.lower() in text.lower() for r in REAL_REACH)
            or FIREWALL_WRITE.search(text) or FIREWALL_QUALIFIED.search(text)):
        raise AssertionError("an installer test's copy still reaches the real Start menu, "
                             "Program Files, uninstall keys or Event Log (use scratch_text)")
    if DECODER_PROFILE_LINE in text:
        raise AssertionError("an installer test's copy still names the decoder's real "
                             "AppContainer profile (use scratch_text)")
    # Machine mode's places (#111 B6b): wherever a copy sets one, it is scratch, and a
    # -Machine run needs every one set.
    for what, pattern in MACHINE_PLACES:
        found = re.findall(pattern, text)
        if (len(found) > 1 or (machine and not found)
                or (found and not is_scratch(found[0], scratch))):
            raise AssertionError(f"an installer test's {what} must be scratch")
    for what, pattern, key in MACHINE_KEYS:
        found = re.findall(pattern, text)
        if len(found) > 1 or (machine and not found) or (found and found != [key]):
            raise AssertionError(f"an installer test's {what} must be scratch")
    for what, pattern, value, needed in FIREWALL_PLACES:
        found = re.findall(pattern, text)
        if len(found) > 1 or (machine and needed and not found) or (found and found != [value]):
            raise AssertionError(f"an installer test's {what} must be scratch")
    for name in ("LOCALAPPDATA", "APPDATA"):
        if not is_scratch(env.get(name), scratch):
            raise AssertionError(f"an installer test runs only with a scratch {name}")
    if machine:
        if install_dir is not None:
            raise AssertionError("a machine run takes no install folder")
        return
    if install_dir is None:
        raise AssertionError("a run without an install folder is a machine run (machine=True)")
    folder = str(install_dir)
    if folder == "":
        return                                  # refused by the script before anything
    if folder[:2] in ("\\\\", "//"):            # refused by the script; still never real
        local = local_form(folder)
        if local is None or any(_folder(local).startswith(_folder(p)) or
                                _folder(p).startswith(_folder(local))
                                for p in real_places().values()):
            raise AssertionError("an installer test's install folder may be a real place")
        return
    if not os.path.isabs(folder):
        if where is None:
            raise AssertionError("a relative install folder needs the folder it is read from")
        folder = os.path.join(where, folder)
    if not is_scratch(folder, scratch):
        raise AssertionError("an installer test's install folder must be scratch")


# -- Windows PowerShell's module analysis cache (#332, CI time) -------------------------
# A command a script has not loaded yet (ConvertFrom-Json, Get-Item, New-Object) makes
# Windows PowerShell 5.1 search every module folder for it, Program Files' first, and note
# what each module exports in its module analysis cache. The minimal environment names no
# cache file, and PowerShell writes the cache only some seconds after a search, so a short
# run never keeps it and the next run searches again. On CI's runner image, whose Program
# Files module folder holds 54 modules (AWS, Microsoft Graph), that search took 12 to 17 s
# in every run (measured on CI, 26 October 2026; loading the modules by path first did not
# avoid it): about 20 minutes each for this module's and tests/test_uninstall_offboard.py's
# installer runs. So the installer runs, and the shortcuts the offboarding tests make, name
# one cache file (PSModuleAnalysisCachePath) in a temporary folder made for this test
# process, filled once by a run that waits until PowerShell has written it. The cache holds
# only module paths and the command names they export; the scripts, their arguments, every
# check before and after a run, and every assertion are unchanged.
# CI (#389): the filling run itself took 41 to 50 s in each CI chunk with installer tests.
# The chunk job restores the file from an earlier run on the same runner image and Windows
# PowerShell version (actions/cache) and names it in UNMINUTE_PSMODULE_CACHE: a file found
# there is used as it is. A missing or empty one is filled there as before, for the job to
# save. A stale one is still correct: PowerShell analyses again any module whose file
# changed. Speed only, as above.

_MODULE_CACHE = {}
CACHE_VARIABLE = "UNMINUTE_PSMODULE_CACHE"


def module_cache():
    """The PSModuleAnalysisCachePath for this process's installer runs: the absolute path
    in UNMINUTE_PSMODULE_CACHE (CI), used as it is when a file is there, or else a file in
    a new temporary folder (removed at exit); filled on first use when there is none. If
    it is not written within a minute the runs still work, only as slowly as before."""
    if "path" not in _MODULE_CACHE:
        given = os.environ.get(CACHE_VARIABLE, "")
        if os.path.isabs(given):
            path = given
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                _MODULE_CACHE["path"] = path
                print(f"Module analysis cache: restored, {os.path.getsize(path)} bytes",
                      file=sys.stderr, flush=True)
                return path
            os.makedirs(os.path.dirname(path), exist_ok=True)
        else:
            import atexit
            folder = tempfile.mkdtemp(prefix="unminute-psmodules-")
            atexit.register(shutil.rmtree, folder, True)
            path = os.path.join(folder, "ModuleAnalysisCache")
        _MODULE_CACHE["path"] = path
        started = time.monotonic()
        # A name no module exports, so every module on the path is searched and noted (a
        # cache filled by one found command alone left runs at about 12 s on CI), then
        # a wait until PowerShell has written the cache after that search.
        warm = ("$null = Get-Command -Name Get-UnminuteNoSuchCommand -ErrorAction Ignore; "
                "$searched = [DateTime]::UtcNow; "
                "$clock = [Diagnostics.Stopwatch]::StartNew(); "
                "while ($clock.Elapsed.TotalSeconds -lt 120 -and -not ("
                "[IO.File]::Exists($env:PSModuleAnalysisCachePath) -and "
                "[IO.File]::GetLastWriteTimeUtc($env:PSModuleAnalysisCachePath) -ge $searched)"
                ") { Start-Sleep -Milliseconds 200 }")
        subprocess.run([windows_powershell.executable(), "-NoProfile", "-Command", warm],
                       capture_output=True, text=True, timeout=300,
                       env=windows_powershell.environment({"PSModuleAnalysisCachePath": path}))
        print(f"Module analysis cache: filled in {time.monotonic() - started:.0f} s",
              file=sys.stderr, flush=True)
    return _MODULE_CACHE["path"]


def run_installer(script, install_dir, scratch, extra="", env=None, location=None, cwd=None,
                  silent=True, timeout=180, stdin=None, machine=False):
    """The only way these tests run install.ps1 or uninstall.ps1: a scratch copy (from
    scratch_text), in Windows PowerShell 5.1 with its minimal environment, LOCALAPPDATA
    and APPDATA at scratch\\Local and scratch\\Roaming unless env gives other scratch
    folders, and -InstallDir install_dir, read from location (Set-Location) or cwd
    when relative; or, with machine=True and install_dir None, -Machine (#111 B6b);
    stdin is text for its standard input (an answer to a question).
    Refused before anything runs unless every place is scratch (check_scratch_run);
    fails if the real install, per user or machine, changed during the run."""
    import re
    if re.search(r"(?i)(^|\s)-Machine\b", extra):
        raise AssertionError("a machine run goes through run_installer(..., machine=True)")
    scratch = Path(scratch)
    base = {"LOCALAPPDATA": str(scratch / "Local"), "APPDATA": str(scratch / "Roaming")}
    full = windows_powershell.environment({**base, **(env or {})})
    check_scratch_run(script, install_dir, scratch, full, location or cwd, machine)
    full["PSModuleAnalysisCachePath"] = module_cache()      # speed only (above)
    for name in ("LOCALAPPDATA", "APPDATA"):
        Path(full[name]).mkdir(parents=True, exist_ok=True)
    where = "-Machine" if machine else f"-InstallDir '{install_dir}'"
    call = f"& '{script}' {where} {'-Silent' if silent else ''} {extra}"
    if location:
        # A relative -InstallDir is read from PowerShell's location, which
        # Set-Location moves away from the process's own folder.
        call = f"Set-Location -LiteralPath '{location}'; {call}"
    before = real_install_state()
    _INSTALLER_RUN.active = True                # checked above, so the guard lets it start
    try:
        # errors="replace": Windows PowerShell writes in the console's code page, so an
        # unlisted name such as one with a ligature can be a byte the locale codec lacks.
        return subprocess.run([windows_powershell.executable(), "-NoProfile",
                               "-ExecutionPolicy", "Bypass",
                               "-Command", f"{call}; exit $LASTEXITCODE"],
                              input=stdin, capture_output=True, text=True, errors="replace",
                              timeout=timeout, cwd=cwd, env=full)
    finally:
        _INSTALLER_RUN.active = False
        if real_install_state() != before:
            raise AssertionError("a test run changed this computer's real install (its "
                                 "folder, Start menu shortcut, uninstall entry, pins or "
                                 "Event Log source): check it")


# -- every other process these tests start (#332 review) -------------------------------
# The 23 October incident was a run of an installer script with its default -InstallDir.
# run_installer checks each run it makes. Every other process a test in this module
# starts goes through the guard below (setUpModule), which refuses, before the process
# starts, a command line or input that names a packaging script, or names a .ps1 file
# that is a copy of one (renamed or scratch) or that names one in turn, however it is
# spelt: -File, -Command, a dot-source, one string, or a path relative to cwd.

_INSTALLER_RUN = threading.local()
# A .ps1 name in a command line or a script: quoted (spaces allowed) or bare.
_PS1_NAME = re.compile(r"""'([^'\r\n]+?\.ps1)'|"([^"\r\n]+?\.ps1)"|([^\s'"`;&|(){}]+\.ps1)""",
                       re.IGNORECASE)
# Text that only an installer script, or a copy of one, holds.
INSTALLER_MARKS = REAL_REACH + ("$RegKey", "$OwnEntries",
                                'if ($MyInvocation.InvocationName -eq ".")')
_UNGUARDED_POPEN = subprocess.Popen
_UNGUARDED_RUN = subprocess.run


def installer_reach(args, cwd=None, stdin_text=None):
    """What installer script a process with this command line (and input) would run,
    or None: a packaging script, or a .ps1 file that copies one or names one."""
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    texts = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args]
    if isinstance(stdin_text, (str, bytes)):
        texts.append(os.fsdecode(stdin_text) if isinstance(stdin_text, bytes) else stdin_text)
    packaging = _folder(ROOT / "packaging")
    seen = set()
    queue = [(text, 0) for text in texts]
    while queue:
        text, depth = queue.pop()
        if any(mark.lower() in text.lower() for mark in INSTALLER_MARKS):
            return "an installer script's copy"
        names = [next(group for group in match.groups() if group)
                 for match in _PS1_NAME.finditer(text)]
        if text.strip().lower().endswith(".ps1") and "\n" not in text.strip():
            names.append(text.strip())          # one argument that is a path with spaces
        for name in names:
            path = Path(name)
            if not path.is_absolute():
                path = Path(cwd or os.getcwd()) / path
            try:
                full = _folder(path)
                found = path.is_file()
            except (OSError, ValueError):       # a name no file can have
                continue
            if full.startswith(packaging):
                return "a packaging script"
            if full in seen or depth >= 3 or not found:
                continue
            seen.add(full)
            queue.append((path.read_text(encoding="utf-8", errors="replace"), depth + 1))
    return None


def _refuse_installer_reach(args, cwd, stdin_text=None):
    if getattr(_INSTALLER_RUN, "active", False):
        return
    reach = installer_reach(args, cwd, stdin_text)
    if reach:
        raise AssertionError(f"a test started {reach} other than through run_installer, "
                             "which alone runs them, with scratch places only")


class _GuardedPopen(_UNGUARDED_POPEN):
    """subprocess.Popen while this module's tests run: refuses an installer script."""

    def __init__(self, args, *rest, **kwargs):
        _refuse_installer_reach(args, kwargs.get("cwd"))
        super().__init__(args, *rest, **kwargs)


def _guarded_run(*args, **kwargs):
    """subprocess.run while this module's tests run: also checks the input it sends."""
    _refuse_installer_reach(args[0] if args else kwargs.get("args"), kwargs.get("cwd"),
                            kwargs.get("input"))
    return _UNGUARDED_RUN(*args, **kwargs)


def setUpModule():
    for patch in (mock.patch.object(subprocess, "Popen", _GuardedPopen),
                  mock.patch.object(subprocess, "run", _guarded_run)):
        patch.start()
        unittest.addModuleCleanup(patch.stop)


def installer_starts(tree, exempt=()):
    """The lines of process starts (subprocess, os, asyncio) in tree, outside the
    nodes in exempt, whose arguments name an installer script or -InstallDir, either
    in the call itself or through a name or attribute set from such text first (#332
    review: B8b's first test module built its command in a variable). A name counts
    within its own function, class or module body; an attribute (self.copy) anywhere
    in tree. A value carries it through str(), Path(), scratch_text() and any method
    call (.read_text(), os.path.join), but not through another function called by its
    bare name (such as a helper that reads a pattern out of a script). Text passed in
    through a function's parameters is not followed: the runtime guard (setUpModule)
    sees those starts in the two modules that run them."""
    import ast
    starters = {"run", "Popen", "call", "check_call", "check_output", "system",
                "startfile", "create_subprocess_exec", "create_subprocess_shell"}
    scopes = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

    def own(scope):
        """scope's nodes, without those of the functions and classes inside it."""
        todo = list(ast.iter_child_nodes(scope))
        while todo:
            node = todo.pop()
            yield node
            if not isinstance(node, scopes):
                todo.extend(ast.iter_child_nodes(node))

    def carried(node):
        """node's parts whose text a value built from node can hold."""
        todo = [node]
        while todo:
            part = todo.pop()
            yield part
            if not (isinstance(part, ast.Call) and isinstance(part.func, ast.Name)
                    and part.func.id not in ("str", "repr", "format", "Path", "scratch_text")):
                todo.extend(ast.iter_child_nodes(part))

    def reaches(node, names, attributes, parts=ast.walk):
        return any(
            (isinstance(inner, ast.Constant) and isinstance(inner.value, str)
             and re.search(r"(?i)install\.ps1|-InstallDir", inner.value)) or
            (isinstance(inner, ast.Name) and inner.id in names) or
            (attributes and isinstance(inner, ast.Attribute)
             and ast.unparse(inner) in attributes)
            for inner in parts(node))

    def assignments(nodes):
        for node in nodes:
            if isinstance(node, ast.Assign):
                yield node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                yield [node.target], node.value
            elif isinstance(node, ast.NamedExpr):
                yield [node.target], node.value

    every = [n for n in ast.walk(tree) if isinstance(n, scopes)]
    attributes = set()
    names = {id(scope): set() for scope in every}
    grew = True
    while grew:                                 # until nothing more is reached
        grew = False
        for scope in every:
            for targets, value in assignments(own(scope)):
                if not reaches(value, names[id(scope)], attributes, carried):
                    continue
                for target in targets:
                    for part in ast.walk(target):
                        if isinstance(part, ast.Name) and part.id not in names[id(scope)]:
                            names[id(scope)].add(part.id)
                            grew = True
                        elif (isinstance(part, ast.Attribute)
                              and ast.unparse(part) not in attributes):
                            attributes.add(ast.unparse(part))
                            grew = True
    found = set()
    for scope in every:
        for node in own(scope):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("subprocess", "os", "asyncio")
                    and (node.func.attr in starters
                         or node.func.attr.startswith(("spawn", "exec")))
                    and id(node) not in exempt
                    and reaches(node, names[id(scope)], attributes)):
                found.add(node.lineno)
    return sorted(found)


class DataFolderOverlapTests(unittest.TestCase):
    """#136: uninstalling deletes the install folder, so the install folder must
    never be, hold or sit inside %LOCALAPPDATA%\\Unminute (workspace and models),
    and the uninstaller removes only what the installer put there. Scratch copies of
    both scripts run for real (run_installer), with a temporary LOCALAPPDATA."""

    MESSAGE = "must not be in, or hold"
    TEST_KEY = TEST_KEY

    def run_script(self, script, install_dir, local, extra="", location=None, cwd=None):
        """Runs a scratch copy with -Silent; cwd is the test's scratch folder."""
        return run_installer(script, install_dir, cwd, extra, env={"LOCALAPPDATA": str(local)},
                             location=location, cwd=cwd)

    def installer_copy(self, tmp):
        """install.ps1 as scratch_text makes it, in tmp."""
        programs = Path(tmp) / "programs"
        programs.mkdir(exist_ok=True)
        copy = Path(tmp) / "install-copy.ps1"
        copy.write_text(scratch_text("install.ps1", programs), encoding="utf-8")
        return copy

    @unittest.skipUnless(sys.platform == "win32", "the installer is Windows PowerShell")
    def test_the_installer_refuses_an_install_folder_that_overlaps_the_data(self):
        # A temporary LOCALAPPDATA and a package that does not exist: the checks
        # come first, so nothing real is read or written.
        with tempfile.TemporaryDirectory() as tmp:
            install = self.installer_copy(tmp)
            local = Path(tmp) / "Local"
            local.mkdir()
            absent = f"-Package '{Path(tmp) / 'absent.zip'}'"
            for where in (local / "Unminute", local / "Unminute" / "app", local):
                with self.subTest(where=where.name):
                    r = self.run_script(install, where, local, absent, cwd=tmp)
                    self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                    self.assertIn(self.MESSAGE, r.stderr)
            r = self.run_script(install, "Unminute", local, absent, location=local, cwd=tmp)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn(self.MESSAGE, r.stderr)
            for where in (r"\\localhost\C$\Unminute", "//localhost/C$/Unminute"):
                with self.subTest(where=where):
                    r = self.run_script(install, where, local, absent, cwd=tmp)
                    self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                    self.assertIn("own drive", r.stderr)
            r = self.run_script(install, "", local, absent, cwd=tmp)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("No install folder", r.stderr)
            # The default shape, and a sibling of the data folder: allowed, so the
            # run stops later, at a prerequisite or the missing package.
            for where in (local / "Programs" / "Unminute", local / "Unminute2"):
                with self.subTest(where=where.name):
                    r = self.run_script(install, where, local, absent, cwd=tmp)
                    self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertNotIn(self.MESSAGE, r.stdout + r.stderr)
            self.assertEqual(list(local.iterdir()), [])          # nothing was written

    @unittest.skipUnless(sys.platform == "win32", "the installer is Windows PowerShell")
    def test_the_installer_drops_a_trailing_backslash(self):
        # The registered uninstall command quotes -InstallDir: a trailing \ there
        # reads as \", a literal quote, and the uninstall fails (R13b round 2
        # review). A copy that stops after the folder checks and prints the folder.
        stop = "    if ($Options.Rollback) { Invoke-Rollback }"
        with tempfile.TemporaryDirectory() as tmp:
            text = self.installer_copy(tmp).read_text(encoding="utf-8")
            self.assertEqual(text.count(stop), 1)
            local = Path(tmp) / "Local"
            local.mkdir()
            copy = Path(tmp) / "install-copy.ps1"
            report = '    Write-Output "DIR=$($Options.InstallDir)"; exit 0'
            copy.write_text(text.replace(stop, report), encoding="utf-8")
            folder = Path(tmp) / "Apps" / "Unminute"
            for where in (f"{folder}\\", f"{folder}\\\\", f"{folder}/", str(folder)):
                with self.subTest(where=where):
                    r = self.run_script(copy, where, local, cwd=tmp)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn(f"DIR={folder}\n", r.stdout)
            self.assertFalse(folder.exists())

    def uninstaller_copy(self, tmp):
        """uninstall.ps1 with this computer's Start menu folder, roaming profile
        folder (where pins live, #111 B8b), uninstall key and the decoder's
        AppContainer profile (#299) swapped for temporary folders, a key that does
        not exist and a throwaway profile name, so a mistake removes nothing real
        (scratch_text)."""
        programs = Path(tmp) / "programs"
        programs.mkdir(exist_ok=True)
        roaming = Path(tmp) / "roaming"
        roaming.mkdir(exist_ok=True)
        text = scratch_text("uninstall.ps1", programs)
        self.assertEqual(text.count(ROAMING_READ), 1)
        text = text.replace(ROAMING_READ, f'"{roaming}"')
        # Nothing else in it may reach this computer's own shortcuts or registry.
        for reach in ("GetFolderPath", "Start Menu\\", "$env:APPDATA", "HKCU:\\Software\\Microsoft",
                      "HKLM", "Registry::", "ComObject"):
            self.assertNotIn(reach.lower(), text.lower(), reach)
        copy = Path(tmp) / "uninstall-copy.ps1"
        copy.write_text(text, encoding="utf-8")
        return copy, programs / "Unminute.lnk"

    @unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
    def test_the_uninstaller_removes_only_its_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            data = local / "Unminute" / "workspace"
            data.mkdir(parents=True)
            kept = data / "workspace.db"
            kept.write_text("synthetic")
            copy, link = self.uninstaller_copy(tmp)
            link.write_text("shortcut")
            for where, location in ((local / "Unminute", None), (data, None), (local, None),
                                    ("Unminute", local)):           # relative to the location
                with self.subTest(where=str(where)):
                    r = self.run_script(copy, where, local, location=location, cwd=tmp)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn("data folder", r.stderr)      # this check, not a later one
                    self.assertIn("so nothing was removed", r.stderr)
                    self.assertTrue(kept.is_file() and link.is_file())
            r = self.run_script(copy, "", local, cwd=tmp)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("No install folder", r.stderr)
            # An install folder holding something the installer did not put there
            # is left as it is.
            install = local / "Programs" / "Unminute"
            (install / "versions" / "0.5.0").mkdir(parents=True)
            (install / "current.json").write_text("{}")
            shutil.copy(copy, install / "uninstall.ps1")
            (install / "notes.txt").write_text("not the installer's")
            r = self.run_script(copy, install, local, cwd=tmp)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("did not put there", r.stderr)
            self.assertTrue((install / "versions" / "0.5.0").is_dir() and link.is_file())
            # Only its own: all removed, with the shortcut, and the data kept.
            (install / "notes.txt").unlink()
            r = self.run_script(copy, install, local, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(install.exists())
            self.assertFalse(link.exists())
            self.assertTrue(kept.is_file())

    @unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
    def test_the_uninstaller_needs_an_install_or_the_registered_folder(self):
        # A wrong -InstallDir (missing, empty, or a network or device form) must not
        # remove the real install's shortcut and uninstall entry and report success
        # (R13b review). The registered folder, with its files already gone, may.
        import winreg
        self.addCleanup(self.delete_test_key)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            local.mkdir()
            copy, link = self.uninstaller_copy(tmp)
            link.write_text("shortcut")
            empty = Path(tmp) / "empty"
            empty.mkdir()
            typo = Path(tmp) / "typo"
            for where, says in ((typo, "No Unminute install"),
                                (empty, "No Unminute install"),
                                (r"\\localhost\C$\Programs\Unminute", "own drive"),
                                ("//localhost/C$/Programs/Unminute", "own drive"),
                                ("\\\\?\\" + str(typo), "own drive")):
                with self.subTest(where=str(where)):
                    r = self.run_script(copy, where, local, cwd=tmp)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn(says, r.stderr)
                    self.assertIn("so nothing was removed", r.stderr)
                    self.assertTrue(link.is_file() and empty.is_dir())
            # An uninstall entry for another folder does not make this one an install.
            registered = Path(tmp) / "Programs" / "Unminute"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY) as key:
                winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(registered))
            r = self.run_script(copy, typo, local, cwd=tmp)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertTrue(link.is_file() and self.key_exists())
            # An older or second install, while the entry names another folder that
            # still holds an install: its own files go, but the shortcut and the
            # entry are the other install's (R13b round 2 review).
            (registered / "versions" / "0.5.0").mkdir(parents=True)
            older = Path(tmp) / "Programs" / "OldInstall"
            (older / "versions" / "0.4.3").mkdir(parents=True)
            (older / "current.json").write_text("{}")
            r = self.run_script(copy, older, local, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(older.exists())
            self.assertTrue(link.is_file() and self.key_exists())
            self.assertTrue(registered.is_dir())
            # The registered folder, already deleted: the shortcut and entry go.
            shutil.rmtree(registered)
            r = self.run_script(copy, registered, local, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(link.exists())
            self.assertFalse(self.key_exists())

    @unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
    def test_the_registered_folder_named_another_way_removes_its_entries(self):
        # The registered folder given through a junction, or by its 8.3 short name,
        # is the same install, not another one: the shortcut and the entry go with
        # it (R13b round 3 review).
        import ctypes
        import winreg
        self.addCleanup(self.delete_test_key)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            local.mkdir()
            copy, link = self.uninstaller_copy(tmp)
            parent = Path(tmp) / "Programs With A Long Name"
            registered = parent / "Unminute"
            junction = Path(tmp) / "alias"
            made = mklink_junction(junction, parent, capture_output=True, text=True,
                                   timeout=30)
            self.assertEqual(0, made.returncode, made.stdout + made.stderr)
            aliases = [str(junction / "Unminute")]
            # The short name, where the volume makes them (CI's may not).
            registered.mkdir(parents=True)
            buffer = ctypes.create_unicode_buffer(1024)
            ctypes.windll.kernel32.GetShortPathNameW(str(registered), buffer, 1024)
            if buffer.value and buffer.value.lower() != str(registered).lower():
                aliases.append(buffer.value)
            for alias in aliases:
                with self.subTest(alias=alias):
                    (registered / "versions" / "0.5.0").mkdir(parents=True, exist_ok=True)
                    (registered / "current.json").write_text("{}")
                    link.write_text("shortcut")
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY) as key:
                        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ,
                                          str(registered))
                    r = self.run_script(copy, alias, local, cwd=tmp)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertFalse((registered / "current.json").exists())
                    self.assertFalse(link.exists())
                    self.assertFalse(self.key_exists())
                    registered.mkdir(parents=True, exist_ok=True)
            os.rmdir(junction)

    @unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
    def test_the_uninstaller_removes_the_decoders_profile_unless_another_install_uses_it(self):
        # #299: the decoder's AppContainer profile (a throwaway one here) goes with
        # the program; when the uninstall entry names another install that is still
        # there, that install still uses it, so it stays.
        import winreg

        from workspace import appcontainer
        self.addCleanup(self.delete_test_key)
        self.addCleanup(appcontainer.delete_profile, DECODER_TEST_PROFILE)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            local.mkdir()
            copy, link = self.uninstaller_copy(tmp)
            install = local / "Programs" / "Unminute"
            other = Path(tmp) / "Other" / "Unminute"
            (other / "versions" / "0.5.0").mkdir(parents=True)
            (other / "current.json").write_text("{}")
            for elsewhere in (True, False):
                with self.subTest(elsewhere=elsewhere):
                    appcontainer.ensure_profile(DECODER_TEST_PROFILE).close()
                    (install / "versions" / "0.5.0").mkdir(parents=True, exist_ok=True)
                    (install / "current.json").write_text("{}")
                    if elsewhere:
                        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY) as key:
                            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ,
                                              str(other))
                    else:
                        self.delete_test_key()
                    r = self.run_script(copy, install, local, cwd=tmp)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertFalse(install.exists())
                    self.assertEqual(appcontainer.profile_exists(DECODER_TEST_PROFILE), elsewhere)
                    self.assertNotIn("decoder's Windows profile", r.stderr)

    def key_exists(self):
        import winreg
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY))
            return True
        except FileNotFoundError:
            return False

    def delete_test_key(self):
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.TEST_KEY)
        except FileNotFoundError:
            pass

    def test_both_scripts_have_the_same_overlap_check(self):
        # The installer's copy is the one run for every case above.
        def check(name):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            start = text.index("# The data (workspace and models) is in")
            return text[start:text.index("\n}\n", start) + 3]
        self.assertEqual(check("install.ps1"), check("uninstall.ps1"))
        self.assertIn("function Get-DataOverlap", check("uninstall.ps1"))

    @unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
    def test_the_uninstaller_never_removes_through_a_link(self):
        # #297 review Q2: a junction inside a version is removed as a link and what
        # it points to stays; a junction at versions or current.json stops the
        # uninstall before anything is removed.
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            local.mkdir()
            copy, link = self.uninstaller_copy(tmp)
            target = Path(tmp) / "someone"
            (target / "versions" / "0.5.0").mkdir(parents=True)
            (target / "sentinel.txt").write_text("keep")
            kept = snapshot(target)

            def junction(at, to):
                made = mklink_junction(at, to, capture_output=True, text=True, timeout=30)
                self.assertEqual(0, made.returncode, made.stdout + made.stderr)
                self.addCleanup(lambda: os.path.lexists(at) and os.rmdir(at))

            def install(name):
                folder = Path(tmp) / name
                folder.mkdir()
                (folder / "current.json").write_text('{"version": "0.5.0"}')
                shutil.copy(copy, folder / "uninstall.ps1")
                return folder

            inside = install("inside")
            (inside / "versions" / "0.5.0" / "app").mkdir(parents=True)
            (inside / "versions" / "0.5.0" / "app" / "held.txt").write_text("ours")
            junction(inside / "versions" / "0.5.0" / "app" / "link", target)
            link.write_text("shortcut")
            r = self.run_script(copy, inside, local, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(os.path.lexists(inside))
            self.assertEqual(snapshot(target), kept)
            for at in ("versions", "current.json"):
                with self.subTest(at=at):
                    folder = install(f"as-{at}")
                    if at == "current.json":
                        (folder / "current.json").unlink()
                        (folder / "versions" / "0.5.0").mkdir(parents=True)
                    junction(folder / at, target if at == "current.json" else target / "versions")
                    link.write_text("shortcut")
                    before = snapshot(folder)
                    r = self.run_script(copy, folder, local, cwd=tmp)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn("link or junction", r.stderr)
                    self.assertIn("so nothing was removed", r.stderr)
                    self.assertEqual(snapshot(folder), before)
                    self.assertEqual(snapshot(target), kept)
                    self.assertTrue(link.is_file())

    def test_both_scripts_remove_folders_the_same_way(self):
        # #297: the uninstaller removes with the installer's walk, which never
        # follows a link, not with Remove-Item -Recurse.
        def walk(name):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            start = text.index("# Removes a folder the installer made without following links")
            end = text.index("\n}\n", text.index("function Remove-FolderTree")) + 3
            return text[start:end]
        self.assertEqual(walk("install.ps1"), walk("uninstall.ps1"))
        for function in ("Test-Link", "Remove-OwnFolder", "Remove-FolderTree"):
            self.assertIn(f"function {function}(", walk("uninstall.ps1"))
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn("Invoke-LockRetry { Remove-OwnFolder $path }", uninstall)
        self.assertNotRegex(uninstall,
                            r"Remove-Item[^\n]*\$(path|Options\.InstallDir)[^\n]*-Recurse")


# A hook for copies of install.ps1 and uninstall.ps1 (FolderInUseTests): at the named
# step, the copy says it is ready, then waits until the test says go.
WAIT_FOR_TEST = '''\
function Wait-ForTest([string]$Stage) {
    if ($env:UNMINUTE_TEST_WAIT -ne $Stage) { return }
    New-Item -ItemType File -Path "$env:UNMINUTE_TEST_SYNC.ready" | Out-Null
    while (-not (Test-Path -LiteralPath "$env:UNMINUTE_TEST_SYNC.go")) {
        Start-Sleep -Milliseconds 20
    }
}
'''
WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32", "the installer is Windows PowerShell")


def write_manifest(folder):
    """package-manifest.json for every file under folder, as the build writes it:
    {relative path: [size, SHA-256]}."""
    import hashlib
    manifest = {}
    for path in sorted(Path(folder).rglob("*")):
        if path.is_file() and path.name != "package-manifest.json":
            data = path.read_bytes()
            manifest[path.relative_to(folder).as_posix()] = [len(data),
                                                             hashlib.sha256(data).hexdigest()]
    (Path(folder) / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def lock_retry_block(name):
    """The wait for a file in use (#239), as packaging/<name> has it: the time limit,
    Test-TransientLock and Invoke-LockRetry."""
    text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
    start = text.index("# A folder cannot be renamed or removed")
    return text[start:text.index("\n}\n", text.index("function Invoke-LockRetry")) + 3]


class FolderInUseTests(unittest.TestCase):
    """#239: a folder cannot be renamed or removed while a file in it is open, and
    antivirus software opens newly written files for a moment to scan them. The
    installer's two renames and the uninstaller's removal are tried again for up to
    30 seconds from the first refusal, for each step, when refused as access denied
    or in use, then fail with the message
    they always gave; any other error fails at once. Copies of the scripts run for
    real in temporary folders with a temporary LOCALAPPDATA (DataFolderOverlapTests);
    the installer's copy runs only Install-Version, on a small synthetic package. A
    hook pauses each copy just before the step while the test opens a file in the
    folder, as a scanner might. Since #111 B6a the copy runs New-StagedCopy, which
    verifies the copy against the package's manifest, then Install-Version."""

    uninstaller_copy = DataFolderOverlapTests.uninstaller_copy
    BOUND = "$LockRetrySeconds = 30\n"
    START = '$ErrorActionPreference = "Stop"\n'
    MAIN = "    if ($Options.Rollback) { Invoke-Rollback }\n"
    STAGING = "Rename-Item -LiteralPath $staging -NewName (Split-Path $target -Leaf)"
    OLD = "Rename-Item -LiteralPath $target -NewName (Split-Path $old -Leaf)"
    REMOVE = 'foreach ($name in "versions", "current.json", "current.json.tmp")'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "Local"
        self.local.mkdir()
        self.sync = self.root / "sync"
        self.package = self.root / "package"
        self.write_package(self.package, "new")
        self.install = self.root / "Apps"
        self.versions = self.install / "versions"

    @staticmethod
    def write_package(folder, label, version="1.0.0"):
        (folder / "app").mkdir(parents=True)
        (folder / "app" / "held.txt").write_text(label)
        (folder / "runtime").mkdir()
        (folder / "runtime" / "python.exe").write_bytes(b"synthetic")
        (folder / "uninstall.ps1").write_text("# synthetic\n")
        # As every package has: a live version names itself (the installer's folder
        # check, #297, takes a version folder without it for someone else's).
        (folder / "version.json").write_text(json.dumps({"version": version}))
        write_manifest(folder)

    def with_hooks(self, text, steps, seconds=None):
        """text with the hook, called just before the line holding each step, and
        with the time limit set to seconds."""
        self.assertEqual(text.count(self.START), 1)
        text = text.replace(self.START, self.START + WAIT_FOR_TEST)
        if seconds is not None:
            self.assertEqual(text.count(self.BOUND), 1)
            text = text.replace(self.BOUND, f"$LockRetrySeconds = {seconds}\n")
        lines = text.split("\n")
        for stage, step in steps.items():
            at = [i for i, line in enumerate(lines) if step in line]
            self.assertEqual(len(at), 1, step)
            indent = lines[at[0]][:len(lines[at[0]]) - len(lines[at[0]].lstrip())]
            lines.insert(at[0], f"{indent}Wait-ForTest '{stage}'")
        return "\n".join(lines)

    def installer(self, seconds=None):
        """install.ps1 (scratch_text), after its folder checks, running only
        New-StagedCopy and Install-Version."""
        programs = self.root / "programs"
        programs.mkdir(exist_ok=True)
        text = scratch_text("install.ps1", programs)
        self.assertEqual(text.count(self.MAIN), 1)
        text = text.replace(self.MAIN,
                            '    $staging = New-StagedCopy $env:UNMINUTE_TEST_PACKAGE "1.0.0"\n'
                            '    $target = Install-Version $staging "1.0.0"\n'
                            '    Write-Output "TARGET=$target"; exit 0\n')
        copy = self.root / "install-copy.ps1"
        copy.write_text(self.with_hooks(text, {"old": self.OLD, "staging": self.STAGING}, seconds),
                        encoding="utf-8")
        return copy

    def uninstaller(self, seconds=None):
        """The uninstaller's copy (DataFolderOverlapTests), and an install for it."""
        copy, link = self.uninstaller_copy(self.root)
        copy.write_text(self.with_hooks(copy.read_text(encoding="utf-8"),
                                        {"remove": self.REMOVE}, seconds), encoding="utf-8")
        link.write_text("shortcut")
        self.write_package(self.versions / "0.5.0", "old", "0.5.0")
        (self.install / "current.json").write_text("{}")
        shutil.copy(copy, self.install / "uninstall.ps1")
        return copy, link

    def hold(self, path, release_after=None):
        """Opens path without letting it be deleted, as a scanner might, until
        release_after seconds have passed or the test ends."""
        held = open(path, "rb")
        self.addCleanup(held.close)
        if release_after is not None:
            timer = threading.Timer(release_after, held.close)
            timer.daemon = True
            timer.start()
            self.addCleanup(timer.cancel)

    def run_copy(self, copy, stage, action, silent=True):
        """Runs the copy; when it reaches stage, action() runs while the copy waits.
        Returns the result and the seconds from then until the copy finished."""
        ready, go = Path(f"{self.sync}.ready"), Path(f"{self.sync}.go")
        ready.unlink(missing_ok=True)
        go.unlink(missing_ok=True)
        went, failed, stop = [], [], threading.Event()

        def watch():
            while not ready.exists():
                if stop.wait(0.02):
                    return
            try:
                action()
            except Exception as e:                  # reported below; the copy goes on
                failed.append(e)
            went.append(time.monotonic())
            go.write_text("")

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        # run_installer's minimal environment: from pwsh 7 (as in CI), an inherited
        # PSModulePath hides 5.1's own Get-FileHash, which New-StagedCopy's check uses.
        r = run_installer(copy, self.install, self.root, silent=silent, cwd=self.root, env={
            "LOCALAPPDATA": str(self.local), "UNMINUTE_TEST_WAIT": stage,
            "UNMINUTE_TEST_SYNC": str(self.sync), "UNMINUTE_TEST_PACKAGE": str(self.package)})
        done = time.monotonic()
        stop.set()
        watcher.join(10)
        if failed:
            raise failed[0]
        self.assertTrue(went, "the copy never reached the step: " + r.stdout + r.stderr)
        return r, done - went[0]

    @WINDOWS_ONLY
    def test_a_staging_folder_in_use_for_a_moment_is_renamed_once_it_is_free(self):
        staging = self.versions / "1.0.0.partial"
        r, waited = self.run_copy(self.installer(), "staging",
                                  lambda: self.hold(staging / "app" / "held.txt", 3), silent=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"TARGET={self.versions / '1.0.0'}\n", r.stdout)
        self.assertEqual((self.versions / "1.0.0" / "app" / "held.txt").read_text(), "new")
        self.assertFalse(staging.exists())
        self.assertGreater(waited, 2.5)              # refused until the file was free
        self.assertEqual(r.stdout.count("Trying again for up to 30 seconds"), 1, r.stdout)

    @WINDOWS_ONLY
    def test_a_reinstall_waits_for_the_live_version_in_use_for_a_moment(self):
        live = self.versions / "1.0.0"
        self.write_package(live, "old")
        r, waited = self.run_copy(self.installer(), "old",
                                  lambda: self.hold(live / "app" / "held.txt", 3))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual((live / "app" / "held.txt").read_text(), "new")
        self.assertEqual(sorted(p.name for p in self.versions.iterdir()), ["1.0.0"])
        self.assertGreater(waited, 2.5)

    @WINDOWS_ONLY
    def test_a_staging_folder_that_stays_in_use_fails_as_before(self):
        # A 3-second limit for speed; the scripts' own is 30 seconds
        # (test_both_scripts_wait_the_same_way).
        staging = self.versions / "1.0.0.partial"
        r, waited = self.run_copy(self.installer(seconds=3), "staging",
                                  lambda: self.hold(staging / "app" / "held.txt"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(r.stderr.startswith("Installation failed: "), r.stderr)
        self.assertIn("1.0.0.partial", r.stderr)
        self.assertTrue(staging.is_dir())            # the next run removes it
        self.assertFalse((self.versions / "1.0.0").exists())
        self.assertTrue(2.5 < waited < 20, waited)

    @WINDOWS_ONLY
    def test_a_live_version_that_stays_in_use_is_left_in_place(self):
        live = self.versions / "1.0.0"
        self.write_package(live, "old")
        r, waited = self.run_copy(self.installer(seconds=3), "old",
                                  lambda: self.hold(live / "app" / "held.txt"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(r.stderr.startswith("Installation failed: "), r.stderr)
        self.assertEqual((live / "app" / "held.txt").read_text(), "old")
        self.assertFalse((self.versions / "1.0.0.old").exists())
        self.assertTrue(2.5 < waited < 20, waited)

    @WINDOWS_ONLY
    def test_any_other_error_fails_at_once(self):
        # With the scripts' own 30-second limit: no waiting for an error a retry
        # cannot fix, such as a folder that is already there or one that is gone.
        staging, target = self.versions / "1.0.0.partial", self.versions / "1.0.0"
        for name, action in (("already there", lambda: (target / "app").mkdir(parents=True)),
                             ("gone", lambda: shutil.rmtree(staging))):
            with self.subTest(name):
                shutil.rmtree(self.install, ignore_errors=True)
                r, waited = self.run_copy(self.installer(), "staging", action)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertTrue(r.stderr.startswith("Installation failed: "), r.stderr)
                self.assertLess(waited, 10)

    @WINDOWS_ONLY
    def test_the_uninstaller_waits_for_a_file_in_use_for_a_moment(self):
        copy, link = self.uninstaller()
        held = self.versions / "0.5.0" / "app" / "held.txt"
        r, waited = self.run_copy(copy, "remove", lambda: self.hold(held, 3))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.install.exists())
        self.assertFalse(link.exists())
        self.assertGreater(waited, 2.5)

    @WINDOWS_ONLY
    def test_the_uninstaller_fails_as_before_when_the_file_stays_in_use(self):
        copy, link = self.uninstaller(seconds=3)
        held = self.versions / "0.5.0" / "app" / "held.txt"
        r, waited = self.run_copy(copy, "remove", lambda: self.hold(held))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(r.stderr.startswith("Removal failed: "), r.stderr)
        # Still shows as installed: current.json, the shortcut and the held file stay.
        self.assertTrue((self.install / "current.json").is_file())
        self.assertTrue(held.is_file() and link.is_file())
        self.assertTrue(2.5 < waited < 20, waited)

    @WINDOWS_ONLY
    def test_the_time_limit_counts_from_the_first_refusal(self):
        # The uninstaller's removal of a large versions folder can run for tens of
        # seconds before it reaches a file a scanner holds; it still gets the whole
        # wait, which starts at the first refusal, not at the start of the step. A
        # 2-second limit for speed, with a first attempt that takes 3 seconds.
        script = self.root / "retry.ps1"

        def run(attempt, place=None):
            # The item refused is in this test's own folder, where the run can write,
            # so access denied there is taken for a file in use (#451).
            place = place or self.root / "item"
            script.write_text(
                '$ErrorActionPreference = "Stop"\n'
                '$Options = @{ Silent = $false }\n'
                'function Write-Step([string]$Message) { Write-Output $Message }\n'
                + lock_retry_block("uninstall.ps1").replace(self.BOUND, "$LockRetrySeconds = 2\n")
                + '$script:tries = 0\n'
                'try {\n'
                '    Invoke-LockRetry {\n'
                '        $script:tries++\n'
                f'        {attempt}\n'
                f"    }} '{place}'\n"
                '    Write-Output "DONE tries=$script:tries"\n'
                '} catch { Write-Output "FAILED tries=$script:tries $($_.Exception.Message)" }\n',
                encoding="utf-8")
            self.addCleanup(lambda: [p.unlink() for p in self.root.glob(".probe-*.tmp")])
            r = subprocess.run([windows_powershell.executable(), "-NoProfile",
                                "-ExecutionPolicy", "Bypass",
                                "-File", str(script)], capture_output=True, text=True,
                               timeout=60, cwd=self.root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return r.stdout

        slow_then_refused = ('if ($script:tries -eq 1) { Start-Sleep -Seconds 3; '
                             'throw [UnauthorizedAccessException]::new("held") }')
        out = run(slow_then_refused)
        self.assertIn("DONE tries=2", out)
        self.assertEqual(out.count("Trying again for up to 2 seconds"), 1, out)
        # Still bounded: a step refused every time fails once the wait has passed.
        start = time.monotonic()
        out = run('Start-Sleep -Milliseconds 500; throw [UnauthorizedAccessException]::new("held")')
        self.assertIn("FAILED", out)
        self.assertIn("held", out)
        self.assertGreater(int(out.split("FAILED tries=")[1].split()[0]), 2)
        self.assertLess(time.monotonic() - start, 20)
        # #451: a sharing violation is in use wherever it is; access denied where
        # the run cannot write is a permission fault, refused at once, not retried.
        out = run('if ($script:tries -eq 1) { throw [IO.IOException]::new("in use", '
                  '-2147024864) }', place="")
        self.assertIn("DONE tries=2", out)
        out = run('throw [UnauthorizedAccessException]::new("denied")',
                  place=self.root / "no-such-folder" / "item")
        self.assertIn("FAILED tries=1 denied", out)
        self.assertNotIn("Trying again", out)
        self.assertEqual(list(self.root.glob(".probe-*")), [])     # the probe never stays

    def test_both_scripts_wait_the_same_way(self):
        block = lock_retry_block
        self.assertEqual(block("install.ps1"), block("uninstall.ps1"))
        self.assertIn(self.BOUND, block("install.ps1"))     # about 30 seconds a step (#239)
        install = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        for step in (self.STAGING, self.OLD):
            self.assertIn("Invoke-LockRetry { " + step, install)
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn("Invoke-LockRetry { Remove-OwnFolder $path }", uninstall)


# Stand-ins that a copy of install.ps1 defines just before its main part
# (InstallFolderScopeTests): the prerequisites, the workspace check, the app-running
# check, the shortcut and the uninstall entry do nothing, and the package is a
# synthetic folder. The gate's stand-in says that it ran, and from which folder in the
# install folder ("outside" for any other).
INSTALL_STUBS = '''\
function Test-Prerequisite { }
function Test-AppRunning { }
function Test-WorkspaceGate([string]$Root, [string]$Checker = "", [int]$Schema = 0) {
    $inside = $Root.StartsWith($Options.InstallDir + "\\", [StringComparison]::OrdinalIgnoreCase)
    $where = if ($inside) { $Root.Substring($Options.InstallDir.Length + 1) } else { "outside" }
    Write-Output "GATE RAN $where"
}
function Get-PackageRoot { return $env:UNMINUTE_TEST_PACKAGE }
function New-Shortcut { }
function Register-Uninstall { }
'''


def snapshot(folder):
    """Every path under folder, with each file's text, without following links."""
    found, todo = {}, [Path(folder)]
    while todo:
        for path in sorted(todo.pop().iterdir()):
            rel = path.relative_to(folder).as_posix()
            if path.is_junction() or path.is_symlink():
                found[rel] = "<link>"
            elif path.is_file():
                found[rel] = path.read_text(encoding="utf-8", errors="replace")
            else:
                found[rel] = "<dir>"
                todo.append(path)
    return found


@WINDOWS_ONLY
class InstallFolderScopeTests(unittest.TestCase):
    """#297 (D2 red-team): the installer writes and removes only inside a folder
    that is an install (current.json) or holds only what an install that stopped
    early leaves; it removes only folders it made, never through a link; and the
    versions it reads from version.json and current.json must be plain folder names
    before they become paths. A copy of install.ps1 runs for real in temporary
    folders, with a temporary LOCALAPPDATA and APPDATA, its Start menu folder and
    uninstall key swapped for temporary ones, and the steps that would reach this
    computer (prerequisites, the workspace check, the shortcut and the uninstall
    entry) replaced by stand-ins (INSTALL_STUBS). The package is synthetic."""

    MAIN = "# -- main -----"
    NOT_OURS = "already holds files that Unminute did not put there"
    LINK = "holds a link or junction"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # The long form of the path: the installer's manifest check compares paths
        # as Get-ChildItem spells them, which expands an 8.3 short name.
        self.root = Path(os.path.realpath(tmp.name))
        self.local = self.root / "Local"
        self.local.mkdir()
        self.appdata = self.root / "Roaming"
        self.appdata.mkdir()
        self.install = self.root / "Apps"
        self.versions = self.install / "versions"
        self.package = self.root / "package"
        self.copy = self.installer_copy()

    def installer_copy(self):
        programs = self.root / "programs"
        programs.mkdir()
        text = scratch_text("install.ps1", programs)
        self.assertEqual(text.count(self.MAIN), 1)
        text = text.replace(self.MAIN, INSTALL_STUBS + self.MAIN)
        copy = self.root / "install-copy.ps1"
        copy.write_text(text, encoding="utf-8")
        return copy

    def write_package(self, version="1.0.0", folder=None, schema=None):
        """A synthetic package: a few files, version.json (stating a storage
        format if schema is given) and the manifest."""
        import hashlib
        folder = folder or self.package
        shutil.rmtree(folder, ignore_errors=True)
        (folder / "app").mkdir(parents=True)
        (folder / "app" / "held.txt").write_text("new")
        (folder / "runtime").mkdir()
        (folder / "runtime" / "python.exe").write_bytes(b"synthetic")
        (folder / "uninstall.ps1").write_text("# synthetic\n")
        info = {"name": "Unminute", "version": version, "features": ["gate", "restore-lid"]}
        if schema is not None:
            info["schema"] = schema
        (folder / "version.json").write_text(json.dumps(info))
        manifest = {}
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                data = path.read_bytes()
                manifest[path.relative_to(folder).as_posix()] = [len(data),
                                                                 hashlib.sha256(data).hexdigest()]
        (folder / "package-manifest.json").write_text(json.dumps(manifest))

    @staticmethod
    def version_folder(folder, version=None, schema=None):
        """An installed version as the installer leaves it: version.json names it."""
        (folder / "runtime").mkdir(parents=True)
        (folder / "runtime" / "python.exe").write_bytes(b"synthetic")
        (folder / "uninstall.ps1").write_text("# synthetic\n")
        info = {"name": "Unminute", "version": version or folder.name, "features": ["gate"]}
        if schema is not None:
            info["schema"] = schema
        (folder / "version.json").write_text(json.dumps(info))

    def write_current(self, **record):
        self.install.mkdir(parents=True, exist_ok=True)
        (self.install / "current.json").write_text(json.dumps(record))

    def current(self):
        return json.loads((self.install / "current.json").read_text(encoding="utf-8-sig"))

    def run_installer(self, extra="", install=None):
        # The module's run_installer, with its minimal environment: from pwsh 7 (as in
        # CI), an inherited PSModulePath hides 5.1's own Get-FileHash, which the
        # manifest check uses.
        return run_installer(self.copy, install or self.install, self.root, extra, cwd=self.root,
                             env={"LOCALAPPDATA": str(self.local), "APPDATA": str(self.appdata),
                                  "UNMINUTE_TEST_PACKAGE": str(self.package)})

    def junction(self, link, target):
        made = mklink_junction(link, target, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, made.returncode, made.stdout + made.stderr)
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))

    # ALL APPLICATION PACKAGES ("AC") may read and execute (0x1200a9), inherited by
    # folders and files (OICI): what the decoder's AppContainer needs (#299).
    DECODER_GRANT = "(A;OICI;0x1200a9;;;AC)"

    def test_the_versions_folder_lets_the_decoders_container_read_it(self):
        self.write_package("1.0.0")
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(self.DECODER_GRANT, dacl_sddl(self.versions))
        for inside in ("1.0.0", "1.0.0/runtime", "1.0.0/runtime/python.exe",
                       "1.0.0/app/held.txt"):
            self.assertRegex(dacl_sddl(self.versions / inside), r"\(A;(OICI)?ID;0x1200a9;;;AC\)",
                             inside)                        # inherited, copied in after the grant
        for beside in ("current.json", "uninstall.ps1"):    # not the record or the uninstaller
            self.assertNotIn(";;;AC)", dacl_sddl(self.install / beside), beside)
        self.write_package("1.1.0")
        r = self.run_installer()                                  # an update: still one entry
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(dacl_sddl(self.versions).count(";;;AC)"), 1)
        self.assertRegex(dacl_sddl(self.versions / "1.1.0" / "runtime" / "python.exe"),
                         r"\(A;ID;0x1200a9;;;AC\)")

    def test_a_rollback_puts_the_decoders_read_access_back(self):
        self.write_package("1.0.0")
        self.assertEqual(self.run_installer().returncode, 0)
        self.write_package("1.1.0")
        self.assertEqual(self.run_installer().returncode, 0)
        # Taken away since (a scratch folder only): the rollback grants it again.
        removed = subprocess.run([windows_powershell.system_program("icacls.exe"),
                                  str(self.versions), "/remove:g", "*S-1-15-2-1"],
                                 capture_output=True, text=True,
                                 env=windows_powershell.environment())
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertNotIn(";;;AC)", dacl_sddl(self.versions / "1.0.0" / "runtime" / "python.exe"))
        r = self.run_installer("-Rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.current()["version"], "1.0.0")
        self.assertIn(self.DECODER_GRANT, dacl_sddl(self.versions))
        self.assertRegex(dacl_sddl(self.versions / "1.0.0" / "runtime" / "python.exe"),
                         r"\(A;ID;0x1200a9;;;AC\)")

    def test_an_update_removes_only_the_versions_unminute_made(self):
        # The current and previous versions stay, an older Unminute version goes,
        # and anything else in versions is left in place and counted, without names.
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0", managed=True)
        for name in ("0.9.0", "0.8.0", "0.7.0"):
            self.version_folder(self.versions / name)
        (self.versions / "project-a").mkdir()
        (self.versions / "project-a" / "data.txt").write_text("someone's work")
        (self.versions / "0.6.0").mkdir()                       # no version.json
        (self.versions / "0.6.0" / "data.txt").write_text("someone's work")
        self.version_folder(self.versions / "0.5.0", "0.5.1")   # names another version
        self.version_folder(self.versions / "Tool", "tool")     # case differs
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(p.name for p in self.versions.iterdir()),
                         ["0.5.0", "0.6.0", "0.9.0", "1.0.0", "Tool", "project-a"])
        self.assertEqual((self.versions / "project-a" / "data.txt").read_text(), "someone's work")
        self.assertEqual(self.current()["version"], "1.0.0")
        self.assertEqual(self.current()["previous"], "0.9.0")
        self.assertTrue(self.current()["managed"])
        self.assertIn("4 folder(s) in the versions folder were not made by Unminute", r.stderr)
        for name in ("project-a", "0.6.0", "Tool", str(self.root)):
            self.assertNotIn(name, r.stdout + r.stderr)

    def test_a_folder_that_is_not_an_install_is_refused_and_nothing_changes(self):
        def red_team(folder):                 # the D2 reproduction, with no current.json
            (folder / "versions" / "project-a").mkdir(parents=True)
            (folder / "versions" / "project-a" / "data.txt").write_text("someone's work")
            (folder / "versions" / "project-b").mkdir()
            (folder / "versions" / "1.0.0").mkdir()          # named like it, but empty

        def other_file(folder):               # a version of ours beside someone's file
            self.version_folder(folder / "versions" / "0.9.0")
            (folder / "notes.txt").write_text("someone's notes")

        def only_a_script(folder):            # someone's uninstall.ps1, no version
            folder.mkdir(parents=True)
            (folder / "uninstall.ps1").write_text("someone's script")

        def a_file_in_versions(folder):
            (folder / "versions").mkdir(parents=True)
            (folder / "versions" / "list.txt").write_text("someone's list")

        self.write_package("1.0.0")
        for make in (red_team, other_file, only_a_script, a_file_in_versions):
            with self.subTest(make.__name__):
                folder = self.root / make.__name__
                make(folder)
                before = snapshot(folder)
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(self.NOT_OURS, r.stderr)
                self.assertIn("-InstallDir", r.stderr)
                self.assertNotIn(str(folder), r.stdout + r.stderr)
                self.assertEqual(snapshot(folder), before)
        # A file where the folder should be.
        afile = self.root / "a-file"
        afile.write_text("someone's file")
        r = self.run_installer(install=afile)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(afile.read_text(), "someone's file")

    def test_an_empty_or_new_folder_and_an_existing_install_are_accepted(self):
        self.write_package("1.0.0")
        for name in ("new", "empty"):
            with self.subTest(name):
                folder = self.root / name
                if name == "empty":
                    folder.mkdir()
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertTrue((folder / "versions" / "1.0.0" / "version.json").is_file())
                self.assertTrue((folder / "uninstall.ps1").is_file())
        # An install (current.json) updates; what else is in it is left alone.
        self.write_current(version="0.9.0", previous=None, managed=False)
        self.version_folder(self.versions / "0.9.0")
        (self.install / "uninstall.ps1").write_text("# synthetic\n")
        (self.install / "readme.txt").write_text("someone's note")
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("1.0.0", "0.9.0"))
        self.assertEqual((self.install / "readme.txt").read_text(), "someone's note")
        # The same version again.
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("1.0.0", "0.9.0"))
        self.assertEqual(sorted(p.name for p in self.versions.iterdir()), ["0.9.0", "1.0.0"])

    def test_an_install_that_stopped_before_current_json_finishes(self):
        # What a first install leaves when it stops after copying (#245, #239): a
        # complete version and the uninstaller, or a copy still in progress, or one
        # of each from an earlier try. A rerun finishes it.
        def copied(folder):
            self.version_folder(folder / "versions" / "1.0.0")
            (folder / "uninstall.ps1").write_text("# synthetic\n")

        def copying(folder):
            (folder / "versions" / "1.0.0.partial" / "app").mkdir(parents=True)
            (folder / "versions" / "1.0.0.partial" / "app" / "held.txt").write_text("half")

        def replaced(folder):
            self.version_folder(folder / "versions" / "1.0.0.old", "1.0.0")
            self.version_folder(folder / "versions" / "0.9.0")
            (folder / "versions" / "0.9.0.partial").mkdir()
            (folder / "uninstall.ps1").write_text("# synthetic\n")

        self.write_package("1.0.0")
        for make in (copied, copying, replaced):
            with self.subTest(make.__name__):
                folder = self.root / make.__name__
                make(folder)
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                record = json.loads((folder / "current.json").read_text(encoding="utf-8-sig"))
                self.assertEqual(record["version"], "1.0.0")
                self.assertEqual((folder / "versions" / "1.0.0" / "app" / "held.txt").read_text(),
                                 "new")
                self.assertFalse((folder / "versions" / "1.0.0.partial").exists())
                self.assertFalse((folder / "versions" / "1.0.0.old").exists())

    def test_links_in_the_install_folder_are_refused(self):
        # A junction where the installer would look or remove: refused before
        # anything is written, the link and what it points to are left as they were.
        target = self.root / "someone"
        target.mkdir()
        (target / "sentinel.txt").write_text("keep")
        self.write_package("1.0.0")

        def in_versions(folder):              # an install, with a junction in versions
            self.write_current(version="0.9.0", previous=None, managed=False)
            self.version_folder(self.versions / "0.9.0")
            self.junction(self.versions / "0.8.0", target)

        def as_versions(folder):              # versions itself is a junction
            folder.mkdir(parents=True)
            self.junction(folder / "versions", target)

        def as_staging(folder):               # the copy's working folder is a junction
            (folder / "versions").mkdir(parents=True)
            self.junction(folder / "versions" / "1.0.0.partial", target)

        def as_record(folder):                # current.json is a link to a folder
            folder.mkdir(parents=True)
            self.junction(folder / "current.json", target)

        for make in (in_versions, as_versions, as_staging, as_record):
            with self.subTest(make.__name__):
                folder = self.install if make is in_versions else self.root / make.__name__
                make(folder)
                before = snapshot(folder)
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(self.LINK, r.stderr)
                self.assertEqual(snapshot(folder), before)
                self.assertEqual(sorted(p.name for p in target.iterdir()), ["sentinel.txt"])

    def test_a_link_inside_an_old_version_is_removed_as_a_link(self):
        # Removing an older version never follows a junction inside it.
        target = self.root / "someone"
        target.mkdir()
        (target / "sentinel.txt").write_text("keep")
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0", managed=False)
        for name in ("0.9.0", "0.8.0", "0.7.0"):
            self.version_folder(self.versions / name)
        (self.versions / "0.7.0" / "app").mkdir()
        self.junction(self.versions / "0.7.0" / "app" / "link", target)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.versions / "0.7.0").exists())
        self.assertEqual((target / "sentinel.txt").read_text(), "keep")

    def test_a_package_version_that_is_not_a_plain_name_is_refused(self):
        # "..\x" would install beside versions; "0.6.0." names the folder 0.6.0,
        # because Windows drops a final dot, so the old retention step then removed
        # the version it had just installed.
        # "1.0.0\n": a regular expression's $ also matches before a final newline
        # (#297 review N1).
        for version in ("..\\x", "0.6.0.", "", "a/b", ".hidden", "1.0.0.partial", "1.0.0\n"):
            with self.subTest(version=version):
                shutil.rmtree(self.install, ignore_errors=True)
                self.write_package(version)
                r = self.run_installer()
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn("version", r.stderr)
                self.assertFalse(self.install.exists())
                self.assertFalse((self.root / "x").exists())

    def test_a_previous_value_that_is_not_a_plain_name_is_ignored(self):
        # Reinstalling the same version keeps current.json's previous version; one
        # that is not a plain name is treated as absent.
        self.write_package("1.0.0")
        for bad in ("..\\x", "0.8.0.", "C:\\Elsewhere", "0.8.0\n"):
            with self.subTest(bad=bad):
                shutil.rmtree(self.install, ignore_errors=True)
                self.write_current(version="1.0.0", previous=bad, managed=False)
                self.version_folder(self.versions / "1.0.0")
                r = self.run_installer()
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.current()["version"], "1.0.0")
                self.assertIsNone(self.current()["previous"])

    def test_a_current_json_that_is_not_an_install_record_is_refused(self):
        # #297 review N2: only a current.json that reads as JSON and names a plain
        # version makes the folder an install. Another program's current.json, or
        # one that names no valid version, leaves the folder check as for any other
        # folder, so its versions\1.0.0.partial and the file itself are kept.
        self.write_package("1.0.0")
        records = {"another program's": json.dumps({"name": "Other", "settings": 1}),
                   "not JSON": "someone's notes", "empty": "",
                   "a path": json.dumps({"version": "..\\x"}),
                   "a final dot": json.dumps({"version": "0.8.0."}),
                   "a final newline": json.dumps({"version": "1.0.0\n"}),
                   "a list": json.dumps([{"version": "1.0.0"}]),
                   # #297 round 2 (R2-1): the version must be a JSON string, and a
                   # path, when there is one, must be this folder's versions\<version>.
                   "a number": json.dumps({"version": 100}),
                   "a version list": json.dumps({"version": ["1.0.0"]}),
                   "a path elsewhere": json.dumps({"version": "1.0.0",
                                                   "path": str(self.root / "versions" / "1.0.0")}),
                   "a path that is no string": json.dumps({"version": "1.0.0", "path": 5})}
        for number, (label, text) in enumerate(records.items()):
            with self.subTest(label):
                folder = self.root / f"record-{number}"
                (folder / "versions" / "1.0.0.partial").mkdir(parents=True)
                (folder / "versions" / "1.0.0.partial" / "data.txt").write_text("someone's work")
                (folder / "current.json").write_text(text)
                before = snapshot(folder)
                r = self.run_installer(install=folder)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(self.NOT_OURS, r.stderr)
                self.assertEqual(snapshot(folder), before)

    def test_an_install_the_installer_made_is_still_an_install(self):
        # The record the installer writes (version and path) makes the folder an
        # install, so an update over it carries on (#297 round 2).
        self.write_package("0.9.0")
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(Path(self.current()["path"]), self.versions / "0.9.0")
        self.write_package("1.0.0")
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("1.0.0", "0.9.0"))

    # PowerShell 7's ConvertFrom-Json, unlike 5.1's, sends the items of a top-level
    # list down the pipeline one by one, so a one-item list reads as its object. This
    # stand-in gives Windows PowerShell 5.1 that behaviour.
    PS7_CONVERT = r"""
function ConvertFrom-Json {
    param([Parameter(ValueFromPipeline = $true, Position = 0)]$InputObject)
    process {
        foreach ($item in (Microsoft.PowerShell.Utility\ConvertFrom-Json $InputObject)) { $item }
    }
}
"""

    def record_check(self, folder, convert=""):
        """A script that runs install.ps1's own Test-InstallRecord on each
        folder/case-*.json and prints case-NN=1 for an install's record, 0 otherwise."""
        text = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        start = text.index("$VersionPattern = '")
        end = text.index("\n}\n", text.index("function Test-InstallRecord")) + 3
        script = folder / "record-check.ps1"
        script.write_text('$ErrorActionPreference = "Stop"\n' + convert + text[start:end] + """
foreach ($file in Get-ChildItem -LiteralPath $args[0] -Filter 'case-*.json' | Sort-Object Name) {
    Write-Output ("{0}={1}" -f $file.BaseName, [int](Test-InstallRecord $file))
}
""", encoding="utf-8")
        return script

    def test_the_install_record_is_an_object_with_a_string_version_and_its_own_path(self):
        # #297 round 2 (R2-1): only a JSON object whose version is a JSON string with
        # a plain name, and whose path, if there is one, is <InstallDir>\versions\
        # <version> (letter case aside), is an install's record. On PowerShell 7 a
        # one-item list must not pass for its object.
        folder = self.root / "records"
        folder.mkdir()
        own = str(folder / "versions" / "1.0.0")
        cases = {
            "an object": ({"version": "1.0.0"}, 1),
            "with its path": ({"version": "1.0.0", "path": own, "previous": None}, 1),
            "with its path in other letter case": ({"version": "1.0.0", "path": own.upper()}, 1),
            "a list": ([{"version": "1.0.0"}], 0),
            "a list of its own record": ([{"version": "1.0.0", "path": own}], 0),
            "a string": ("1.0.0", 0),
            "an empty object": ({}, 0),
            "a number": ({"version": 100}, 0),
            "a version list": ({"version": ["1.0.0"]}, 0),
            "a version object": ({"version": {"name": "1.0.0"}}, 0),
            "a path elsewhere": ({"version": "1.0.0",
                                  "path": str(self.root / "versions" / "1.0.0")}, 0),
            "a path to another version": ({"version": "1.0.0",
                                           "path": str(folder / "versions" / "0.9.0")}, 0),
            "a path inside the version": ({"version": "1.0.0", "path": own + "\\app"}, 0),
            "a path that is no string": ({"version": "1.0.0", "path": 5}, 0),
            "a null path": ({"version": "1.0.0", "path": None}, 0),
        }
        expected = []
        for number, (record, verdict) in enumerate(cases.values()):
            (folder / f"case-{number:02}.json").write_text(json.dumps(record))
            expected.append(f"case-{number:02}={verdict}")
        # Whitespace before a list still leaves a list.
        (folder / "case-99.json").write_text(" \r\n\t" + json.dumps([{"version": "1.0.0"}]))
        expected.append("case-99=0")
        shells = [("Windows PowerShell 5.1", windows_powershell.executable(), ""),
                  ("5.1 with PowerShell 7's ConvertFrom-Json", windows_powershell.executable(),
                   self.PS7_CONVERT)]
        pwsh = shutil.which("pwsh")
        if os.environ.get("GITHUB_ACTIONS"):          # CI's Windows runner has PowerShell 7
            self.assertIsNotNone(pwsh, "PowerShell 7 (pwsh) is missing from the CI runner")
        if pwsh:
            shells.append(("PowerShell 7", pwsh, ""))
        for label, exe, convert in shells:
            with self.subTest(label):
                script = self.record_check(folder, convert)
                env = windows_powershell.environment()
                if convert == "" and exe == pwsh:
                    env.pop("PSModulePath")       # PowerShell 7 finds its own modules
                r = subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                    str(script), str(folder)], capture_output=True, text=True,
                                   timeout=120, env=env, cwd=self.root)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(r.stdout.split(), expected, r.stderr)

    def test_rollback_refuses_a_previous_version_with_a_final_newline(self):
        # #297 review N1: "0.8.0\n" is not a plain name, so there is no previous version.
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0\n", managed=False)
        for name in ("0.9.0", "0.8.0"):
            self.version_folder(self.versions / name)
        before = (self.install / "current.json").read_text()
        r = self.run_installer("-Rollback")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("There is no previous version to go back to.", r.stderr)
        self.assertNotIn("GATE RAN", r.stdout)
        self.assertEqual((self.install / "current.json").read_text(), before)

    def test_rollback_refuses_a_previous_version_that_is_not_a_plain_name(self):
        # -Rollback never looks outside versions or runs anything from there. Each
        # case has a folder that would run where the value leads.
        self.write_package("1.0.0")
        escape = self.install / "x"
        cases = (("previous", "..\\x"), ("previous", "0.8.0."), ("version", "..\\x"),
                 ("previous", "0.8.0-link"))
        for field, bad in cases:
            with self.subTest(field=field, bad=bad):
                shutil.rmtree(self.install, ignore_errors=True)
                record = {"version": "0.9.0", "previous": "0.8.0", "managed": False}
                record[field] = bad
                self.write_current(**record)
                for name in ("0.9.0", "0.8.0"):
                    self.version_folder(self.versions / name)
                self.version_folder(escape, "x")
                if bad == "0.8.0-link":
                    self.junction(self.versions / bad, self.versions / "0.8.0")
                before = (self.install / "current.json").read_text()
                r = self.run_installer("-Rollback")
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertNotIn("GATE RAN", r.stdout)
                self.assertEqual((self.install / "current.json").read_text(), before)

    def test_rollback_still_goes_back_to_a_plain_previous_version(self):
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0", managed=True)
        for name in ("0.9.0", "0.8.0"):
            self.version_folder(self.versions / name)
        r = self.run_installer("-Rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("GATE RAN", r.stdout)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("0.8.0", "0.9.0"))

    # -- current.json format 2 (#111 B5a) ------------------------------------------

    def slots(self):
        """current.json's slots without the install times."""
        return {name: {k: v for k, v in entry.items() if k != "installed"}
                for name, entry in self.current()["slots"].items()}

    def test_an_install_records_each_versions_format_and_install_order(self):
        from workspace import update
        self.write_package("1.0.0", schema=2)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        record = self.current()
        self.assertEqual((record["format"], record["version"], record["previous"]),
                         (2, "1.0.0", None))
        self.assertEqual(self.slots(), {"1.0.0": {"schema": 2, "seq": 1}})
        self.assertRegex(record["slots"]["1.0.0"]["installed"],
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        state, read = update.read_slots(self.install)      # what the launcher reads
        self.assertEqual((state, read["slots"]), ("ok", {"1.0.0": {
            "schema": 2, "seq": 1, "installed": record["slots"]["1.0.0"]["installed"]}}))

    def test_an_update_from_format_1_numbers_the_kept_versions_oldest_first(self):
        # A version from before B5 states no format: recorded without one.
        self.write_package("1.0.0", schema=2)
        self.write_current(version="0.9.0", previous="0.8.0", managed=True)
        for name in ("0.9.0", "0.8.0"):
            self.version_folder(self.versions / name)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["version"], self.current()["previous"]),
                         ("1.0.0", "0.9.0"))
        self.assertEqual(self.slots(), {"0.9.0": {"seq": 1}, "1.0.0": {"schema": 2, "seq": 2}})
        self.assertTrue(self.current()["managed"])

    def format2(self, current, previous, slots, managed=False):
        self.write_current(format=2, version=current, previous=previous, managed=managed,
                           path=str(self.versions / current), slots=slots)

    def test_an_update_gives_the_new_version_the_next_number_and_drops_removed_ones(self):
        self.write_package("1.1.0", schema=3)
        self.format2("1.0.0", "0.9.0", {
            "1.0.0": {"schema": 2, "seq": 7, "installed": "2027-01-02T00:00:00Z"},
            "0.9.0": {"schema": 2, "seq": 5, "installed": "2027-01-01T00:00:00Z"}})
        for name in ("1.0.0", "0.9.0"):
            self.version_folder(self.versions / name, schema=2)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.slots(), {"1.0.0": {"schema": 2, "seq": 7},
                                        "1.1.0": {"schema": 3, "seq": 8}})
        self.assertEqual(self.current()["slots"]["1.0.0"]["installed"], "2027-01-02T00:00:00Z")
        self.assertEqual(sorted(p.name for p in self.versions.iterdir()), ["1.0.0", "1.1.0"])

    def test_a_reinstall_keeps_the_number_and_records_a_new_install_time(self):
        self.write_package("1.0.0", schema=2)
        self.format2("1.0.0", "0.9.0", {
            "1.0.0": {"schema": 2, "seq": 7, "installed": "2027-01-02T00:00:00Z"},
            "0.9.0": {"schema": 2, "seq": 5, "installed": "2027-01-01T00:00:00Z"}})
        for name in ("1.0.0", "0.9.0"):
            self.version_folder(self.versions / name, schema=2)
        r = self.run_installer()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.current()["previous"], "0.9.0")
        self.assertEqual(self.slots(), {"0.9.0": {"schema": 2, "seq": 5},
                                        "1.0.0": {"schema": 2, "seq": 7}})
        installed = {k: v["installed"] for k, v in self.current()["slots"].items()}
        self.assertEqual(installed["0.9.0"], "2027-01-01T00:00:00Z")
        self.assertNotEqual(installed["1.0.0"], "2027-01-02T00:00:00Z")

    def test_rollback_writes_format_2_and_keeps_each_versions_number(self):
        self.write_package("1.0.0")
        self.format2("1.0.0", "0.9.0", {
            "1.0.0": {"schema": 2, "seq": 7, "installed": "2027-01-02T00:00:00Z"},
            "0.9.0": {"schema": 2, "seq": 5, "installed": "2027-01-01T00:00:00Z"}},
                     managed=True)
        for name in ("1.0.0", "0.9.0"):
            self.version_folder(self.versions / name, schema=2)
        r = self.run_installer("-Rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        record = self.current()
        self.assertEqual((record["format"], record["version"], record["previous"],
                          record["managed"]), (2, "0.9.0", "1.0.0", True))
        self.assertEqual(record["slots"], {
            "0.9.0": {"schema": 2, "seq": 5, "installed": "2027-01-01T00:00:00Z"},
            "1.0.0": {"schema": 2, "seq": 7, "installed": "2027-01-02T00:00:00Z"}})
        self.assertEqual(record["path"].lower(), str(self.versions / "0.9.0").lower())

    def test_rollback_from_a_format_1_record_writes_format_2(self):
        self.write_package("1.0.0")
        self.write_current(version="0.9.0", previous="0.8.0", managed=False)
        for name in ("0.9.0", "0.8.0"):
            self.version_folder(self.versions / name)
        r = self.run_installer("-Rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.current()["format"], self.current()["version"]), (2, "0.8.0"))
        self.assertEqual(self.slots(), {"0.8.0": {"seq": 1}, "0.9.0": {"seq": 2}})

    def test_both_scripts_use_the_same_version_name_pattern(self):
        import re

        def pattern(name, where):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            found = re.findall(where, text)
            self.assertEqual(len(found), 1, name)
            return found[0]

        found = pattern("install.ps1", r"(?m)^\$VersionPattern = '([^']+)'$")
        self.assertEqual(found, pattern("uninstall.ps1", r"-cnotmatch '([^']+)'"))
        # As .NET reads it (#297 review N1): a final newline is not part of a name.
        check = (f"$p = '{found}'; foreach ($n in '1.0.0', \"1.0.0`n\", \"1.0.0`r`n\") "
                 "{ Write-Output ([int]($n -cmatch $p)) }")
        r = subprocess.run([windows_powershell.executable(), "-NoProfile", "-Command", check],
                           capture_output=True, text=True, timeout=60,
                           env=windows_powershell.environment())
        self.assertEqual(r.stdout.split(), ["1", "0", "0"], r.stdout + r.stderr)


# One installer log line (#111 B6a): fixed words and numbers only.
LOG_LINE = (r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ mode=user version=(?:-|[0-9A-Za-z.+_-]+) "
            r"step=(?:start|rollback|copy|gate|switch|register|done) "
            r"result=(?:begun|ok|stopped)(?: code=\d)? elapsed_ms=\d+(?: error=\w+)?")


@WINDOWS_ONLY
class StagedCopyTests(unittest.TestCase):
    """#111 B6a, the installer's behaviour in both modes: the package folder is
    checked for links before anything is copied; the copy in
    versions\\<version>.partial is verified once, by exact name, size and SHA-256
    (the owner's decision B6-8), before its own runtime runs the workspace check and
    before the swap; current.json is replaced whole or not at all; one run at a time
    (install.lock); a log without names or paths. A copy of install.ps1 runs for real
    in temporary folders, as in InstallFolderScopeTests (whose helpers these borrow),
    with a synthetic package."""

    MAIN = InstallFolderScopeTests.MAIN
    setUp = InstallFolderScopeTests.setUp
    installer_copy = InstallFolderScopeTests.installer_copy
    write_package = InstallFolderScopeTests.write_package
    version_folder = staticmethod(InstallFolderScopeTests.version_folder)
    write_current = InstallFolderScopeTests.write_current
    current = InstallFolderScopeTests.current
    junction = InstallFolderScopeTests.junction
    NO_LINK = "The package contains a link or junction, so nothing was installed."
    BUSY = "Another install or update of Unminute is still running"

    def run_copy(self, copy=None, install=None, extra="", env=None):
        """Runs the installer's copy (or another copy) with -Silent (run_installer)."""
        base = {"LOCALAPPDATA": str(self.local), "APPDATA": str(self.appdata),
                "UNMINUTE_TEST_PACKAGE": str(self.package)}
        return run_installer(copy or self.copy, install or self.install, self.root, extra,
                             env={**base, **(env or {})}, cwd=self.root)

    def variant(self, name, find, insert):
        """A copy of the installer copy with insert placed before the one line holding find."""
        text = self.copy.read_text(encoding="utf-8")
        lines = text.split("\n")
        at = [i for i, line in enumerate(lines) if find in line]
        self.assertEqual(len(at), 1, find)
        lines.insert(at[0], insert)
        copy = self.root / name
        copy.write_text("\n".join(lines), encoding="utf-8")
        return copy

    def existing_install(self):
        """An install of 0.9.0, as the installer leaves it."""
        self.version_folder(self.versions / "0.9.0")
        self.write_current(version="0.9.0", path=str(self.versions / "0.9.0"), managed=False)
        (self.install / "uninstall.ps1").write_text("# synthetic\n")

    def log(self, install=None):
        path = (install or self.install) / "logs" / "install.log"
        return path.read_bytes().decode("utf-8") if path.exists() else ""

    def test_listed_names_must_match_exactly(self):
        # A letter-case variant, a Kelvin sign (U+212A) for K or a capital sharp s
        # (U+1E9E) of a listed name is not that name. Before B6a the check lower-cased
        # both sides and so took each of these for the listed file.
        def case(folder):                    # listed held.txt, the copy has Held.txt
            (folder / "app" / "held.txt").rename(folder / "app" / "Held.txt")

        def kelvin(folder):                  # an unlisted file beside the listed kelvin.txt
            (folder / "app" / "\u212aelvin.txt").write_text("planted")

        def sharp_s(folder):                 # an unlisted file beside the listed strasse
            (folder / "app" / "STRA\u1e9eE.txt").write_text("planted")

        def ligature(folder):                # U+FB01 for "fi"
            (folder / "app" / "\ufb01le.txt").write_text("planted")

        def extra(folder):
            (folder / "app" / "planted.pth").write_text("import os")

        def missing(folder):
            (folder / "app" / "file.txt").unlink()

        def changed(folder):
            (folder / "runtime" / "python.exe").write_bytes(b"synthetiC")

        for label, change in (("case", case), ("kelvin", kelvin), ("sharp_s", sharp_s),
                              ("ligature", ligature), ("extra", extra), ("missing", missing),
                              ("changed", changed)):
            with self.subTest(label):
                self.write_package("1.0.0")
                for name in ("kelvin.txt", "stra\u00dfe.txt", "file.txt"):
                    (self.package / "app" / name).write_text("listed")
                write_manifest(self.package)
                change(self.package)
                install = self.root / f"Apps-{label}"
                r = self.run_copy(install=install)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertNotIn("GATE RAN", r.stdout)
                self.assertFalse((install / "versions" / "1.0.0").exists())
                self.assertFalse((install / "versions" / "1.0.0.partial").exists())
                self.assertFalse((install / "current.json").exists())

    def test_a_link_in_the_package_stops_the_install_before_anything_is_copied(self):
        outside = self.root / "outside"
        (outside / "empty").mkdir(parents=True)
        (outside / "full").mkdir()
        (outside / "full" / "sentinel.txt").write_text("someone's file")
        for label, target in (("empty", outside / "empty"), ("full", outside / "full")):
            with self.subTest(label):
                self.write_package("1.0.0")
                self.junction(self.package / "app" / "linked", target)
                install = self.root / f"Apps-{label}"
                r = self.run_copy(install=install)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn(self.NO_LINK, r.stderr)
                self.assertNotIn("GATE RAN", r.stdout)
                self.assertFalse((install / "versions").exists())
                os.rmdir(self.package / "app" / "linked")
        self.assertEqual(snapshot(outside), {"empty": "<dir>", "full": "<dir>",
                                             "full/sentinel.txt": "someone's file"})
        # The package folder itself a junction.
        self.write_package("1.0.0", folder=self.root / "real-package")
        shutil.rmtree(self.package, ignore_errors=True)
        self.junction(self.package, self.root / "real-package")
        r = self.run_copy(install=self.root / "Apps-root")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn(self.NO_LINK, r.stderr)

    def test_the_workspace_check_runs_from_the_verified_copy(self):
        # Never from %TEMP% or the package folder (T22): from versions\<v>.partial,
        # after its verification, before the swap.
        self.write_package("1.0.0")
        r = self.run_copy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("GATE RAN versions\\1.0.0.partial\n", r.stdout)
        self.assertTrue((self.versions / "1.0.0" / "package-manifest.json").is_file())
        self.assertFalse((self.versions / "1.0.0.partial").exists())

    def test_a_failed_check_of_the_copy_changes_nothing_else(self):
        self.existing_install()
        before = {k: v for k, v in snapshot(self.install).items() if not k.startswith("logs")}
        self.write_package("1.0.0")
        (self.package / "app" / "planted.pth").write_text("import os")
        r = self.run_copy()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        after = {k: v for k, v in snapshot(self.install).items() if not k.startswith("logs")}
        self.assertEqual(after, {**before, "versions": "<dir>"})

    def test_the_package_is_unpacked_into_a_new_folder_named_by_a_whole_guid(self):
        # The real Get-PackageRoot, with -Package: a zip beside its .sha256.
        import hashlib
        import zipfile
        text = self.copy.read_text(encoding="utf-8")
        stub = "function Get-PackageRoot { return $env:UNMINUTE_TEST_PACKAGE }\n"
        self.assertEqual(text.count(stub), 1)
        listing = ("function Test-WorkspaceGate { Write-Output (\"TEMP=\" + "
                   "((Get-ChildItem -LiteralPath $env:TEMP -Name) -join \",\")) }\n")
        text = text.replace(stub, "").replace(
            INSTALL_STUBS[INSTALL_STUBS.index("function Test-WorkspaceGate"):
                          INSTALL_STUBS.index("function Get-PackageRoot")], listing)
        copy = self.root / "install-package.ps1"
        copy.write_text(text, encoding="utf-8")
        self.write_package("1.0.0", folder=self.root / "build" / "Unminute-1.0.0")
        zpath = self.root / "Unminute-1.0.0.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            for path in sorted((self.root / "build").rglob("*")):
                if path.is_file():
                    z.write(path, path.relative_to(self.root / "build").as_posix())
        digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
        (self.root / "Unminute-1.0.0.zip.sha256").write_text(f"{digest}  {zpath.name}\n")
        temp = self.root / "temp"
        temp.mkdir()
        r = self.run_copy(copy=copy, extra=f"-Package '{zpath}'",
                          env={"TEMP": str(temp), "TMP": str(temp)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"(?m)^TEMP=um[0-9a-f]{32}$")
        self.assertEqual(list(temp.iterdir()), [])                  # removed afterwards

    def test_current_json_is_replaced_whole_or_not_at_all(self):
        self.existing_install()
        before = (self.install / "current.json").read_bytes()
        self.write_package("1.0.0")
        failing = self.variant("install-fail.ps1", "    Move-RecordFile $temp $File",
                               '    if ($env:UNMINUTE_TEST_FAIL) { throw "injected detail" }')
        r = self.run_copy(copy=failing, env={"UNMINUTE_TEST_FAIL": "1"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual((self.install / "current.json").read_bytes(), before)
        self.assertTrue((self.install / "current.json.tmp").is_file())
        # The log keeps the error's type, never its message.
        self.assertRegex(self.log(), r"step=switch result=stopped code=1 elapsed_ms=\d+ "
                                     r"error=RuntimeException\r\n$")
        self.assertNotIn("injected", self.log())
        # The next run removes the leftover and replaces the record.
        r = self.run_copy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.install / "current.json.tmp").exists())
        raw = (self.install / "current.json").read_bytes()
        # As Set-Content -Encoding UTF8 wrote it: a byte-order mark and a final line break.
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf") and raw.endswith(b"\r\n"), raw[:8])
        self.assertEqual(self.current()["version"], "1.0.0")
        self.assertEqual(self.current()["previous"], "0.9.0")

    def test_the_log_has_one_line_per_step_without_names_or_paths(self):
        self.write_package("1.0.0")
        r = self.run_copy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.write_package("1.0.1")
        (self.package / "app" / "planted.pth").write_text("import os")
        r = self.run_copy()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        import re
        lines = self.log().split("\r\n")
        self.assertEqual(lines[-1], "")
        for line in lines[:-1]:
            self.assertRegex(line, f"^{LOG_LINE}$")
        steps = [re.search(r"step=(\w+) result=(\w+)", line).groups() for line in lines[:-1]]
        self.assertEqual(steps, [("start", "begun"), ("copy", "begun"), ("gate", "begun"),
                                 ("switch", "begun"), ("register", "begun"), ("done", "ok"),
                                 ("start", "begun"), ("copy", "begun"), ("copy", "stopped")])
        self.assertIn("version=1.0.0 step=done result=ok code=0", lines[5])
        self.assertIn("version=1.0.1 step=copy result=stopped code=4", lines[8])
        text = self.log()
        for secret in (str(self.root), os.environ.get("USERNAME", "") or "\0", "\\", "planted"):
            self.assertNotIn(secret, text)

    def test_the_log_keeps_one_earlier_file_at_its_limit(self):
        logs = self.install / "logs"
        logs.mkdir(parents=True)
        (logs / "install.log").write_bytes(b"x" * (256 * 1024 - 40))
        (logs / "install.log.1").write_bytes(b"older")
        self.write_package("1.0.0")
        r = self.run_copy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((logs / "install.log.1").read_bytes(), b"x" * (256 * 1024 - 40))
        self.assertLess((logs / "install.log").stat().st_size, 4096)
        self.assertEqual(sorted(p.name for p in logs.iterdir()), ["install.log", "install.log.1"])

    def test_one_run_at_a_time(self):
        self.existing_install()
        before = snapshot(self.install)
        text = self.copy.read_text(encoding="utf-8")
        self.assertEqual(text.count("$LockWaitSeconds = 600\n"), 1)
        quick = self.root / "install-quick.ps1"
        quick.write_text(text.replace("$LockWaitSeconds = 600\n", "$LockWaitSeconds = 2\n"),
                         encoding="utf-8")
        self.write_package("1.0.0")
        held = open(self.install / "install.lock", "a+b")       # another run holds it
        try:
            r = self.run_copy(copy=quick)
        finally:
            held.close()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(self.BUSY, r.stderr)
        after = snapshot(self.install)
        self.assertEqual({k: v for k, v in after.items() if not k.startswith("logs")},
                         {**before, "install.lock": ""})
        # Once it is free, the install goes ahead, and the lock goes with the run.
        r = self.run_copy(copy=quick)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.install / "install.lock").exists())
        self.assertEqual(self.current()["version"], "1.0.0")

    def test_the_installers_own_entries_belong_to_it(self):
        # A run that stopped early leaves the log folder, the lock or a temporary
        # record; the folder is still the installer's own, and the leftover goes.
        self.write_package("1.0.0")
        (self.install / "logs").mkdir(parents=True)
        (self.install / "logs" / "install.log").write_text("earlier\r\n")
        (self.install / "current.json.tmp").write_text("{")
        (self.install / "install.lock").write_text("")
        r = self.run_copy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.install / "current.json.tmp").exists())
        # Anything else in logs is someone else's: refused, and nothing changes.
        other = self.root / "Other"
        (other / "logs").mkdir(parents=True)
        (other / "logs" / "notes.txt").write_text("someone's notes")
        r = self.run_copy(install=other)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(snapshot(other), {"logs": "<dir>", "logs/notes.txt": "someone's notes"})
        # A link at the log folder, or in it, is refused in every case.
        for label in ("at", "in"):
            with self.subTest(link=label):
                folder = self.root / f"Linked-{label}"
                folder.mkdir()
                target = self.root / f"target-{label}"
                target.mkdir()
                if label == "at":
                    self.junction(folder / "logs", target)
                else:
                    (folder / "logs").mkdir()
                    self.junction(folder / "logs" / "install.log", target)
                r = self.run_copy(install=folder)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("holds a link or junction", r.stderr)
                self.assertEqual(list(target.iterdir()), [])


@WINDOWS_ONLY
class UninstallOwnEntriesTests(unittest.TestCase):
    """#111 B6a: the uninstaller removes the installer's new entries (the log folder
    with its two files, current.json.tmp, install.lock), refuses anything else in
    the log folder, and never runs beside an install (exit 3)."""

    uninstaller_copy = DataFolderOverlapTests.uninstaller_copy
    run_script = DataFolderOverlapTests.run_script

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.local = self.root / "Local"
        self.local.mkdir()
        self.copy, self.link = self.uninstaller_copy(self.root)
        self.install = self.root / "Apps"
        version = self.install / "versions" / "0.9.0"
        (version / "runtime").mkdir(parents=True)
        (version / "version.json").write_text(json.dumps({"version": "0.9.0"}))
        (self.install / "current.json").write_text(json.dumps({"version": "0.9.0"}))
        shutil.copy(self.copy, self.install / "uninstall.ps1")
        (self.install / "logs").mkdir()
        (self.install / "logs" / "install.log").write_text("line\r\n")
        (self.install / "logs" / "install.log.1").write_text("older\r\n")
        (self.install / "current.json.tmp").write_text("{")

    def test_the_installers_own_entries_are_removed(self):
        r = self.run_script(self.copy, self.install, self.local, cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.install.exists())

    def test_anything_else_in_the_log_folder_stops_it(self):
        (self.install / "logs" / "notes.txt").write_text("someone's notes")
        before = snapshot(self.install)
        r = self.run_script(self.copy, self.install, self.local, cwd=self.root)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("did not put there", r.stderr)
        self.assertEqual(snapshot(self.install), before)

    def test_an_entry_of_the_wrong_kind_stops_it(self):
        # #332 review: only versions and logs are folders. A folder named like one of
        # the installer's files (current.json, uninstall.ps1, current.json.tmp,
        # install.lock), or a file named versions, is not the installer's, so nothing
        # is removed; before, a folder named current.json was removed with everything
        # in it.
        def as_folder(name):
            path = self.install / name
            path.unlink(missing_ok=True)
            (path / "notes").mkdir(parents=True)
            (path / "notes" / "keep.txt").write_text("someone's notes")

        def versions_as_file(_):
            shutil.rmtree(self.install / "versions")
            (self.install / "versions").write_text("someone's notes")

        cases = {name: as_folder for name in ("current.json", "uninstall.ps1",
                                              "current.json.tmp", "install.lock")}
        cases["versions"] = versions_as_file
        for name, make in cases.items():
            with self.subTest(name):
                self.setUp()
                make(name)
                before = snapshot(self.install)
                r = self.run_script(self.copy, self.install, self.local, cwd=self.root)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn("did not put there", r.stderr)
                self.assertEqual(snapshot(self.install), before)

    def test_it_never_runs_beside_an_install(self):
        before = snapshot(self.install)
        with open(self.install / "install.lock", "a+b"):        # an install holds it
            r = self.run_script(self.copy, self.install, self.local, cwd=self.root)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("An install or update of Unminute is running", r.stderr)
        self.assertEqual(snapshot(self.install), {**before, "install.lock": ""})


@WINDOWS_ONLY
class InstallerIsolationTests(unittest.TestCase):
    """#111 B6a, after a local test run uninstalled the owner's real install on 23
    October 2026: no test here can reach this account's real install. run_installer
    refuses, before anything runs, every way a run could write a real place, and
    fails a run during which the real install changed; every test runs the scripts
    through it. tests/powershell proves the same for the Pester tests. #111 B6b: the
    same for machine mode's places (Program Files, the all-users Start menu, the HKLM
    uninstall entry and the Event Log source), in a -Machine run and every other."""

    DEFAULT = object()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.copy, _ = DataFolderOverlapTests.uninstaller_copy(self, self.root)
        self.install = self.root / "Apps"

    def refused(self, script=None, install=DEFAULT, env=None, location=None, cwd=None,
                extra="", machine=False):
        """run_installer's refusal, checked to come before anything runs."""
        if install is self.DEFAULT:
            install = None if machine else self.install
        with mock.patch.object(subprocess, "run", side_effect=RuntimeError("it ran")) as run:
            with self.assertRaises(AssertionError) as caught:
                run_installer(script or self.copy, install, self.root, extra, env=env,
                              location=location, cwd=cwd, machine=machine)
        run.assert_not_called()
        return str(caught.exception)

    def test_every_machine_place_in_a_copy_is_scratch(self):
        # #111 B6b: scratch_text points Program Files, the all-users Start menu folder,
        # the HKLM uninstall entry and the Event Log source at scratch, in both scripts.
        import re
        for name in ("install.ps1", "uninstall.ps1"):
            with self.subTest(name):
                text = scratch_text(name, self.root / "programs")
                for what, pattern in MACHINE_PLACES:
                    found = re.findall(pattern, text)
                    self.assertEqual(len(found), 1, what)
                    self.assertTrue(is_scratch(found[0], self.root), what)
                for what, pattern, key in MACHINE_KEYS:
                    self.assertEqual(re.findall(pattern, text), [key], what)
                    self.assertTrue(key.startswith("HKCU:\\Software\\Unminute-UnitTest-"), what)
                # #111 B7b: a throwaway firewall group (and, in install.ps1, policy
                # key), and no command that adds or removes a firewall rule.
                for what, pattern, value, needed in FIREWALL_PLACES:
                    expected = [value] if needed or name == "install.ps1" else []
                    self.assertEqual(re.findall(pattern, text), expected, what)
                    self.assertIn("Unminute-UnitTest-", value, what)
                self.assertIn(FIREWALL_STAND_IN, text)
                for reach in REAL_REACH:
                    self.assertNotIn(reach.lower(), text.lower(), reach)
                self.assertIsNone(FIREWALL_WRITE.search(text))

    def test_a_copy_renames_every_firewall_write(self):
        # #455: scratch_text renames every Windows Firewall command that changes
        # something ((New|Set|Remove|Enable|Disable|Copy|Rename)-NetFirewall*, in any
        # letter case, Set-NetFirewallProfile included), not only the three the
        # scripts use today; reads are left as they are.
        writes = ("New-NetFirewallRule", "Set-NetFirewallRule", "Remove-NetFirewallRule",
                  "Enable-NetFirewallRule", "Disable-NetFirewallRule", "Copy-NetFirewallRule",
                  "Rename-NetFirewallRule", "Set-NetFirewallProfile", "Set-NetFirewallSetting",
                  "New-NetFirewallHyperVRule", "remove-netfirewallrule")
        fake = self.root / "fake-repo"
        (fake / "packaging").mkdir(parents=True)
        for name in ("install.ps1", "uninstall.ps1"):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            extra = "".join(f"# {w}\n" for w in writes) + "# Get-NetFirewallProfile\n"
            (fake / "packaging" / name).write_text(extra + text, encoding="utf-8")
        with mock.patch.object(sys.modules[__name__], "ROOT", fake):
            for name in ("install.ps1", "uninstall.ps1"):
                with self.subTest(name):
                    text = scratch_text(name, self.root / "programs")
                    for write in writes:
                        self.assertFalse(write.lower() in text.lower(), write)
                    self.assertIsNone(FIREWALL_WRITE.search(text))
                    self.assertIn("# Get-NetFirewallProfile", text)
                    self.assertGreaterEqual(text.count(FIREWALL_STAND_IN), len(writes))

    def test_a_machine_run_cannot_reach_the_real_places(self):
        # #111 B6b: each machine place of a copy pointed back at a real one, or at
        # anything but the copy's own throwaway key, is refused before anything runs,
        # in a -Machine run and in a per-user one.
        real = real_places()
        good = scratch_text("install.ps1", self.root / "programs")
        machine_key = '$MachineRegKey = "HKCU:\\' + TEST_MACHINE_KEY + '"'
        event_key = '$EventSourceKey = "HKCU:\\' + TEST_EVENT_KEY + '"'
        cases = (
            ("Program Files", f'$ProgramFilesFolder = "{self.root / "Program Files"}"',
             f'$ProgramFilesFolder = "{real["program_files"]}"', "Program Files must be scratch"),
            ("Program Files inside", f'$ProgramFilesFolder = "{self.root / "Program Files"}"',
             f'$ProgramFilesFolder = "{real["program_files"] / "Unminute"}"',
             "Program Files must be scratch"),
            ("Program Files (x86)", f'$ProgramFilesFolder = "{self.root / "Program Files"}"',
             f'$ProgramFilesFolder = "{real.get("program_files_x86", real["program_files"])}"',
             "Program Files must be scratch"),
            ("the all-users Start menu",
             f'$CommonProgramsFolder = "{self.root / "CommonPrograms"}"',
             f'$CommonProgramsFolder = "{real["common_programs"]}"',
             "still reaches"),              # its path names the Start Menu folder
            ("a real profile folder",
             f'$CommonProgramsFolder = "{self.root / "CommonPrograms"}"',
             f'$CommonProgramsFolder = "{real["roaming"] / "Microsoft"}"',
             "all-users Start menu folder must be scratch"),
            ("Program Files by the shell", f'$ProgramFilesFolder = "{self.root / "Program Files"}"',
             PROGRAM_FILES_LINE, "still reaches"),
            ("the all-users Start menu by the shell",
             f'$CommonProgramsFolder = "{self.root / "CommonPrograms"}"', COMMON_PROGRAMS_LINE,
             "still reaches"),
            ("the HKLM uninstall entry", machine_key, MACHINE_KEY_LINE, "still reaches"),
            ("the Event Log source", event_key, EVENT_KEY_LINE, "still reaches"),
            ("another HKCU key", machine_key, '$MachineRegKey = "HKCU:\\Software\\Unminute"',
             "machine uninstall key must be scratch"),
            ("another HKLM key", event_key, '$EventSourceKey = "HKLM:\\SOFTWARE\\Unminute"',
             "Event Log source key must be scratch"),
            # #111 B7b: the firewall group and the policy key.
            ("the real firewall group", f'$FirewallGroup = "{TEST_FIREWALL_GROUP}"',
             FIREWALL_GROUP_LINE, "still reaches"),
            ("another firewall group", f'$FirewallGroup = "{TEST_FIREWALL_GROUP}"',
             '$FirewallGroup = "Core Networking"', "firewall group must be scratch"),
            ("the real policy key", f'$PolicyKey = "HKCU:\\{TEST_POLICY_KEY}"',
             POLICY_KEY_LINE, "still reaches"),
            ("another policy key", f'$PolicyKey = "HKCU:\\{TEST_POLICY_KEY}"',
             '$PolicyKey = "HKCU:\\Software\\Unminute"', "policy key must be scratch"),
            ("a firewall write", FIREWALL_STAND_IN + " -Name $Name -DisplayName",
             "New-NetFirewallRule -Name $Name -DisplayName", "still reaches"),
        )
        for label, find, put, says in cases:
            with self.subTest(label):
                self.assertEqual(good.count(find), 1, find)
                copy = self.root / "machine-copy.ps1"
                copy.write_text(good.replace(find, put), encoding="utf-8")
                self.assertIn(says, self.refused(copy, machine=True))
                self.assertIn(says, self.refused(copy))
        # #455: every Windows Firewall command that changes something, by any verb and
        # in any letter case, and any firewall command named through its module (which
        # a stand-in cannot catch), is refused wherever it is.
        anchor = FIREWALL_STAND_IN + " -Name $Name -DisplayName"
        for put in ("Enable-NetFirewallRule", "Disable-NetFirewallRule", "Copy-NetFirewallRule",
                    "Rename-NetFirewallRule", "Set-NetFirewallProfile", "set-netfirewallsetting",
                    "New-NetFirewallAddressFilter", "NetSecurity\\Get-NetFirewallRule",
                    "NetSecurity\\" + FIREWALL_STAND_IN, "netsh advfirewall",
                    "New-Object -ComObject HNetCfg.FwPolicy2"):
            with self.subTest(put):
                self.assertEqual(good.count(anchor), 1, anchor)
                copy = self.root / "machine-copy.ps1"
                copy.write_text(good.replace(anchor, put + "; " + anchor), encoding="utf-8")
                self.assertIn("still reaches", self.refused(copy, machine=True))
                self.assertIn("still reaches", self.refused(copy))
        # The copy as scratch_text makes it is not refused (it would run). The shared
        # module cache is filled first: its first use runs PowerShell, which the mock
        # below would stop, leaving the cache unwritten for the rest of this process.
        module_cache()
        copy = self.root / "machine-copy.ps1"
        copy.write_text(good, encoding="utf-8")
        with mock.patch.object(subprocess, "run", side_effect=RuntimeError("it ran")):
            with self.assertRaises(RuntimeError):
                run_installer(copy, None, self.root, machine=True)
        # A copy without its machine places runs only per user.
        per_user = self.root / "per-user-copy.ps1"
        per_user.write_text("\n".join(line for line in good.splitlines()
                                      if not line.startswith(("$ProgramFilesFolder =",
                                                              "$MachineRegKey ="))),
                            encoding="utf-8")
        self.assertIn("Program Files must be scratch", self.refused(per_user, machine=True))
        # -Machine only through machine=True, which takes no install folder.
        self.assertIn("machine=True", self.refused(extra="-Machine"))
        self.assertIn("machine=True", self.refused(extra="-Silent -machine"))
        self.assertIn("takes no install folder", self.refused(copy, self.install, machine=True))
        self.assertIn("is a machine run", self.refused(copy, None))

    def test_the_real_state_covers_the_machine_install(self):
        # Program Files\Unminute, the all-users shortcut, the HKLM uninstall entry and
        # the Event Log source (read only, in the 64-bit view) are compared too: four
        # places, three keys, then the two pin folders (#332 review).
        import winreg
        state = real_install_state()
        self.assertEqual(len(state), 9)
        calls = []
        with mock.patch.object(sys.modules[__name__], "_registry_state",
                               side_effect=lambda hive, key: calls.append((hive, key))):
            real_install_state()
        self.assertEqual(calls, [(winreg.HKEY_CURRENT_USER, REAL_KEY),
                                 (winreg.HKEY_LOCAL_MACHINE, REAL_MACHINE_KEY),
                                 (winreg.HKEY_LOCAL_MACHINE, REAL_EVENT_KEY)])
        real = real_places()
        for name in ("program_files", "common_programs"):
            self.assertTrue(real[name].is_absolute(), name)
        self.assertNotEqual(_folder(real["program_files"]), _folder(real["common_programs"]))

    def test_only_a_scratch_copy_runs(self):
        for name in ("install.ps1", "uninstall.ps1"):
            with self.subTest(name):
                self.assertIn("only a copy", self.refused(ROOT / "packaging" / name))
                plain = self.root / f"plain-{name}"
                shutil.copy(ROOT / "packaging" / name, plain)
                self.assertIn("still reaches", self.refused(plain))
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        outside = Path(elsewhere.name) / "uninstall-copy.ps1"
        shutil.copy(self.copy, outside)
        self.assertIn("only a copy", self.refused(outside))

    def test_no_real_place_is_an_install_folder(self):
        places = real_places()
        for name, place in places.items():
            with self.subTest(name):
                self.assertIn("must be scratch", self.refused(install=place))
                self.assertIn("must be scratch", self.refused(install=place / "inner"))
        # The network and device forms of the real install folder, which the scripts
        # refuse; and any other server's.
        drive, rest = os.path.splitdrive(str(places["install"]))
        for form in (f"\\\\localhost\\{drive[0]}$" + rest,
                     f"//localhost/{drive[0]}$" + rest.replace("\\", "/"),
                     "\\\\?\\" + str(places["install"]), r"\\server\share\Unminute"):
            with self.subTest(form):
                self.assertIn("may be a real place", self.refused(install=form))
        self.assertIn("needs the folder", self.refused(install="Apps"))
        self.assertIn("must be scratch", self.refused(install="..\\..", cwd=self.root))

    def test_the_variables_must_be_scratch(self):
        places = real_places()
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        for name, value in (("LOCALAPPDATA", places["install"].parent.parent),
                            ("LOCALAPPDATA", places["data"]),
                            ("APPDATA", places["roaming"]),
                            ("APPDATA", elsewhere.name),
                            ("LOCALAPPDATA", "")):
            with self.subTest(name=name, value=value):
                self.assertIn(f"scratch {name}", self.refused(env={name: str(value)}))

    def test_the_real_places_come_from_the_shell(self):
        # A test's own LOCALAPPDATA and APPDATA cannot move them, nor (#111 B6b)
        # variables naming Program Files or the all-users folders.
        real = real_places()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "Local"),
                                          "APPDATA": str(self.root / "Roaming"),
                                          "ProgramFiles": str(self.root / "PF"),
                                          "ProgramW6432": str(self.root / "PF"),
                                          "ProgramData": str(self.root / "PD"),
                                          "ALLUSERSPROFILE": str(self.root / "PD")}):
            self.assertEqual(real_places(), real)
        self.assertNotIn(_folder(self.root), [_folder(p) for p in real.values()])

    def test_every_run_shares_one_written_module_cache_named_after_the_checks(self):
        # #332, CI time: each installer run names the one module analysis cache, which
        # the first use has filled, in a temporary folder that is neither the test's
        # scratch folder nor any real place; a refused run never fills it (refused()
        # above checks nothing runs at all).
        cache = module_cache()
        self.assertTrue(os.path.isfile(cache), "PowerShell did not write the module cache")
        self.assertFalse(is_scratch(cache, self.root))
        self.assertFalse(any(_folder(cache).startswith(_folder(p))
                             for p in real_places().values()))
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            run_installer(self.copy, "", self.root, cwd=self.root)
            run_installer(self.copy, self.install, self.root, cwd=self.root)
        self.assertEqual([c.kwargs["env"]["PSModuleAnalysisCachePath"] for c in run.call_args_list],
                         [cache, cache])
        self.assertEqual(module_cache(), cache)

    def test_a_run_that_changes_the_real_install_fails(self):
        # The copy stops at once (no install folder given), so only the stand-in for
        # the real install's state changes.
        r = run_installer(self.copy, "", self.root, cwd=self.root)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        module = sys.modules[__name__]
        with mock.patch.object(module, "real_install_state", side_effect=[["before"], ["after"]]):
            with self.assertRaises(AssertionError) as caught:
                run_installer(self.copy, "", self.root, cwd=self.root)
        self.assertIn("changed this computer's real install", str(caught.exception))

    def test_the_real_install_state_covers_its_pins(self):
        # #332 review: uninstall.ps1 -Offboard removes pins, so a change to a pin of
        # Unminute in this account's TaskBar or StartMenu pin folder is a change to the
        # real install. Here the real places are stand-ins in the scratch folder; other
        # apps' pins never count, so a pin the user changes meanwhile fails nothing.
        real = self.root / "real"
        places = {"install": real / "Local" / "Programs" / "Unminute",
                  "data": real / "Local" / "Unminute", "programs": real / "Programs",
                  "roaming": real / "Roaming", "program_files": real / "Program Files",
                  "common_programs": real / "CommonPrograms"}
        pinned = places["roaming"].joinpath(*REAL_PINNED)
        target = str(places["install"] / "versions" / "1.0.0" / "runtime" / "pythonw.exe")
        pins = {
            "by its name": pinned / "TaskBar" / "Unminute.lnk",
            "by its target": pinned / "StartMenu" / "Renamed.lnk",
            "by its target, in UTF-16": pinned / "TaskBar" / "Other name.lnk",
        }
        for label, pin in pins.items():
            pin.parent.mkdir(parents=True, exist_ok=True)
            body = (target if "target" in label else "synthetic").encode(
                "utf-16-le" if "UTF-16" in label else "ascii")
            pin.write_bytes(b"L\0\0\0" + body)
        editor = pinned / "StartMenu" / "Editor.lnk"
        editor.write_bytes(b"L\0\0\0C:\\Program Files\\Editor\\editor.exe")
        module = sys.modules[__name__]
        with mock.patch.object(module, "real_places", return_value=places):
            before = real_install_state()
            # Another app's pin changed, added or removed: no change to the real install.
            editor.write_bytes(b"L\0\0\0C:\\Program Files\\Editor\\editor2.exe")
            (pinned / "TaskBar" / "Browser.lnk").write_bytes(b"L\0\0\0C:\\Browser\\b.exe")
            self.assertEqual(real_install_state(), before)
            editor.unlink()
            self.assertEqual(real_install_state(), before)
            for label, pin in pins.items():
                with self.subTest(label):
                    base = real_install_state()
                    kept = pin.read_bytes()
                    pin.unlink()
                    self.assertNotEqual(real_install_state(), base)
                    pin.write_bytes(kept)
                    base = real_install_state()
                    os.utime(pin, ns=(os.stat(pin).st_atime_ns, os.stat(pin).st_mtime_ns + 10**9))
                    self.assertNotEqual(real_install_state(), base)

    def test_every_run_of_the_scripts_goes_through_run_installer(self):
        # No other code in this file builds a command line with -InstallDir or, since
        # #111 B6b, -Machine, or dot-sources a script.
        import ast
        import re
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        allowed = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "run_installer"
                   and node.col_offset == 0]
        self.assertEqual(len(allowed), 1)
        inside = {id(n) for n in ast.walk(allowed[0])}
        # This class's own refusals name -Machine to show run_installer refuses it.
        inside |= {id(n) for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef) and node.name == type(self).__name__
                   for n in ast.walk(node)}
        found = [node.lineno for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)
                 and (re.search(r"-InstallDir\s+(['\"]|$)", node.value)
                      or re.search(r"(^|\s)-Machine(\s|$)", node.value)
                      or re.search(r"(^|[;{(]\s*)\.\s+['\"&(]", node.value))
                 and id(node) not in docstrings
                 and id(node) not in inside]
        self.assertEqual(found, [], "an installer run outside run_installer")

    def test_the_guard_is_on_for_every_test_here(self):
        self.assertIs(subprocess.Popen, _GuardedPopen)
        self.assertIs(subprocess.run, _guarded_run)

    def test_no_other_run_reaches_an_installer_script(self):
        # #332 review: a run with the script's default -InstallDir, the 23 October
        # incident, is refused before the process starts, however it is spelt. The
        # process start itself is replaced too, so nothing can run here even if the
        # guard let it through.
        real = {name: ROOT / "packaging" / name for name in ("install.ps1", "uninstall.ps1")}
        renamed = self.root / "renamed.ps1"
        shutil.copy(real["uninstall.ps1"], renamed)
        spaced = self.root / "with space" / "plain copy.ps1"
        spaced.parent.mkdir()
        shutil.copy(real["install.ps1"], spaced)
        wrapper = self.root / "wrapper.ps1"
        wrapper.write_text(f"& '{real['uninstall.ps1']}' -Silent\n", encoding="utf-8")
        nested = self.root / "nested.ps1"
        nested.write_text(f". \"{wrapper}\"\n", encoding="utf-8")
        ps = windows_powershell.executable()
        forms = {
            "-File, the script": ([ps, "-File", real["uninstall.ps1"], "-Silent"], None),
            "-File, relative to cwd": ([ps, "-File", "packaging\\install.ps1"], ROOT),
            "-File, in its folder": ([ps, "-File", "uninstall.ps1"], ROOT / "packaging"),
            "-Command": ([ps, "-Command", f"& '{real['uninstall.ps1']}' -Silent"], None),
            "a dot-source": ([ps, "-Command", f". \"{real['install.ps1']}\""], None),
            "one string": (f'"{ps}" -File "{real["uninstall.ps1"]}" -Silent', None),
            "a renamed copy": ([ps, "-File", renamed], None),
            "a copy in a folder with a space": ([ps, "-File", str(spaced)], None),
            "a scratch copy, default -InstallDir": ([ps, "-File", self.copy, "-Silent"], None),
            "a script that runs it": ([ps, "-File", wrapper], None),
            "a script that runs one that runs it": ([ps, "-File", nested], None),
        }
        with mock.patch.object(_UNGUARDED_POPEN, "_execute_child",
                               side_effect=RuntimeError("it ran")):
            for label, (args, cwd) in forms.items():
                for start in (subprocess.run, subprocess.Popen, subprocess.check_output):
                    with self.subTest(label, start=start.__name__):
                        with self.assertRaises(AssertionError) as caught:
                            start(args, cwd=cwd)
                        self.assertIn("other than through run_installer", str(caught.exception))
            text = scratch_text("uninstall.ps1", self.root)
            with self.subTest("the script as input"):
                with self.assertRaises(AssertionError):
                    subprocess.run([ps, "-Command", "-"], input=text, text=True)
            # Any other script still starts (here, as far as the replaced start).
            other = self.root / "other.ps1"
            other.write_text("exit 0\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "it ran"):
                subprocess.run([ps, "-File", other], cwd=self.root)

    def test_no_test_module_starts_an_installer_script_itself(self):
        # Static: in every test module, no call that starts a process names an
        # installer script or -InstallDir, outside run_installer (the guard above also
        # sees names built at run time, in this module).
        import ast
        found = []
        for module in sorted((ROOT / "tests").glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            exempt = set()
            if module.name == Path(__file__).name:
                # run_installer, which runs the copies; and the test above that proves
                # the guard refuses a copy's text as input, with the start replaced
                # (named since the scan follows variables; #332 review).
                exempt = {id(n) for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                          and (node.name == "run_installer" and node.col_offset == 0
                               or node.name == "test_no_other_run_reaches_an_installer_script")
                          for n in ast.walk(node)}
                self.assertEqual(
                    sum(isinstance(n, ast.FunctionDef) for n in ast.walk(tree) if id(n) in exempt),
                    2)
            found += [f"{module.name}:{line}" for line in installer_starts(tree, exempt)]
        self.assertEqual(found, [], "a process start that names an installer script")

    def test_the_static_scan_sees_a_command_built_first(self):
        # #332 review: B8b's first test module built the command in a variable and
        # then started it, which a scan of the start call alone does not see. (Here
        # -InstallDir takes no quotes, so the run_installer check above does not take
        # these shapes for runs.)
        import ast
        import textwrap
        shapes = {
            "in the call": """
                subprocess.run([ps, "-Command", f"& '{s}' -InstallDir {d}"])
                """,
            "in a variable": """
                call = f"& '{s}' -InstallDir {d}" + (" -Silent" if silent else "")
                subprocess.run([ps, "-Command", f"{call}; exit $LASTEXITCODE"])
                """,
            "through two variables": """
                call = f"& '{s}' -InstallDir {d}"
                args = [ps, "-Command", call]
                subprocess.Popen(args)
                """,
            "added to later": """
                call = "exit 0"
                call += " -InstallDir x"
                os.system(call)
                """,
            "in an attribute": """
                self.script = root / "uninstall.ps1"
                os.system(f"{ps} -File {self.script}")
                """,
            "inside a function": """
                def go():
                    call = "& 'x.ps1' -InstallDir y"
                    return subprocess.run([ps, "-Command", call])
                """,
        }
        for label, source in shapes.items():
            with self.subTest(label):
                self.assertNotEqual(installer_starts(ast.parse(textwrap.dedent(source))), [])
        # A name that holds something else, or the same name in another function,
        # does not count.
        other = """
            def one():
                call = "& 'x.ps1' -InstallDir y"
                return call

            def two():
                call = "exit 0"
                return subprocess.run([ps, "-Command", call])
            """
        self.assertEqual(installer_starts(ast.parse(textwrap.dedent(other))), [])


def delete_test_key(subkey):
    """Removes a throwaway HKCU key these tests made, with everything under it."""
    import winreg
    if not subkey.startswith("Software\\Unminute-UnitTest-"):
        raise AssertionError("only a test's own key is removed")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            children = [winreg.EnumKey(key, i) for i in range(winreg.QueryInfoKey(key)[0])]
    except FileNotFoundError:
        return
    for child in children:
        delete_test_key(subkey + "\\" + child)
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)


@WINDOWS_ONLY
class MachineModeScriptTests(unittest.TestCase):
    """#111 B6b: what of machine mode runs for real without administrator rights.
    The machine install and uninstall themselves are tested in tests/powershell
    (Machine.Tests.ps1) with elevation and the folder's permissions stood in and every
    machine place a scratch one; a real System-context install is a manual check
    (#189, E1). Here: the per-user installer's refusal beside a machine install, the
    switches that need -Machine, a run without elevation, and the machine section
    both scripts share. Every run is a scratch copy through run_installer, so Program
    Files, the all-users Start menu, the HKLM uninstall entry and the Event Log source
    are scratch folders and throwaway HKCU keys (InstallerIsolationTests)."""

    MAIN = StagedCopyTests.MAIN
    setUp = InstallFolderScopeTests.setUp
    installer_copy = InstallFolderScopeTests.installer_copy
    write_package = InstallFolderScopeTests.write_package
    run_copy = StagedCopyTests.run_copy
    variant = StagedCopyTests.variant
    EVERYONE = "already installed for everyone on this computer"

    def machine_entry(self):
        """The copies' stand-in for the HKLM uninstall entry (TEST_MACHINE_KEY), made
        for this test and removed after it."""
        import winreg
        self.addCleanup(delete_test_key, TEST_KEY + "-machine")
        winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TEST_MACHINE_KEY, 0,
                           winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY).Close()

    def test_a_per_user_install_is_refused_beside_a_machine_install(self):
        # The real check (Test-MachineInstalled, through the 64-bit view) reads the
        # copy's stand-in for the HKLM entry.
        self.write_package("1.0.0")
        r = self.run_copy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        shutil.rmtree(self.install)
        self.machine_entry()
        r = self.run_copy()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(self.EVERYONE, r.stderr)
        self.assertFalse(self.install.exists())
        # Also a rollback: nothing of the per-user install changes.
        r = self.run_copy(extra="-Rollback")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertFalse(self.install.exists())

    def test_a_registry_that_cannot_be_read_does_not_stop_a_per_user_install(self):
        self.write_package("1.0.0")
        copy = self.variant("install-unreadable.ps1", self.MAIN,
                            'function Test-MachineInstalled { throw "denied" }')
        r = self.run_copy(copy=copy)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.install / "versions" / "1.0.0").is_dir())

    def test_remove_slot_needs_machine(self):
        self.write_package("1.0.0")
        r = self.run_copy(extra="-RemoveSlot 0.9.0")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-RemoveSlot is for a machine install", r.stderr)
        self.assertFalse(self.install.exists())

    def test_block_runtime_network_needs_machine(self):
        # #111 B7b: the runtime firewall rule is machine mode's only; a per-user run
        # refuses the switch before it writes anything.
        self.write_package("1.0.0")
        r = self.run_copy(extra="-BlockRuntimeNetwork")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("-BlockRuntimeNetwork is for a machine install", r.stderr)
        self.assertFalse(self.install.exists())

    def machine_copy(self, name, stand_ins=""):
        """A scratch copy of install.ps1 for a -Machine run: scratch_text's places, and
        none of INSTALL_STUBS, so its own Get-PackageRoot reads the -Package zip
        (self.copy's stand-in returns UNMINUTE_TEST_PACKAGE: unset in a machine run, it
        handed an empty path on and the run failed with exit 1; CI's elevated run of
        c6bd723). stand_ins go just before the main part."""
        programs = self.root / "programs"
        programs.mkdir(exist_ok=True)
        text = scratch_text("install.ps1", programs)
        self.assertNotIn("UNMINUTE_TEST_PACKAGE", text)
        if stand_ins:
            self.assertEqual(text.count(self.MAIN), 1)
            text = text.replace(self.MAIN, stand_ins + "\n" + self.MAIN)
        copy = self.root / name
        copy.write_text(text, encoding="utf-8")
        return copy

    def test_a_machine_run_writes_only_scratch_places(self):
        # The real elevation check, without stand-ins, through run_installer: every
        # machine place is scratch (refused otherwise), and the test fails if the real
        # install, per user or machine, changed.
        import ctypes
        program_files = self.root / "Program Files"      # scratch_text's, beside programs
        install = self.machine_copy("install-machine.ps1")
        uninstall = self.root / "uninstall-machine.ps1"
        uninstall.write_text(scratch_text("uninstall.ps1", self.root / "programs"),
                             encoding="utf-8")
        zpath = self.root / "Unminute-1.0.0.zip"
        zpath.write_bytes(b"not read")
        self.addCleanup(delete_test_key, TEST_KEY + "-machine")
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        env = {"LOCALAPPDATA": str(self.local), "APPDATA": str(self.appdata)}
        real = real_install_state()
        for name, script, extra in (("install", install, f"-Package '{zpath}'"),
                                    ("uninstall", uninstall, "")):
            with self.subTest(name):
                r = run_installer(script, None, self.root, extra, env=env, cwd=self.root,
                                  machine=True)
                out = r.stdout + r.stderr
                if not elevated:
                    self.assertEqual(r.returncode, 2, out)
                    self.assertIn("needs administrator rights", r.stderr)
                    self.assertEqual(list(program_files.iterdir()), [])
                    continue
                # As CI runs (GitHub's runners are elevated): past the elevation check.
                self.assertNotIn("needs administrator rights", r.stderr)
                if name == "install":
                    # It makes the machine folder in the scratch Program Files
                    # (New-MachineRoot, before the package is read), checks it, takes
                    # the lock, then stops at the package's missing checksum file
                    # (exit 4), not on anything unexpected on the way (review of
                    # a3ab563, N1).
                    self.assertEqual(r.returncode, 4, out)
                    self.assertIn("The checksum file", r.stderr)
                    self.assertIn("is missing; download it with the package.", r.stderr)
                    self.assertEqual([p.name for p in program_files.iterdir()], ["Unminute"])
                else:
                    # It removes what the stopped install left there (its own folder,
                    # with the installer's permissions and log), and succeeds.
                    self.assertEqual(r.returncode, 0, out)
                    self.assertEqual(list(program_files.iterdir()), [])
        # run_installer also checks this around each run.
        self.assertEqual(real_install_state(), real)

    def test_a_machine_run_past_the_elevation_check_stops_at_the_package(self):
        # The elevated install of test_a_machine_run_writes_only_scratch_places, run
        # here without administrator rights: elevation, the folder's creation and its
        # permissions as read are stood in (as in Machine.Tests.ps1); everything else is
        # the real script in run_installer's minimal environment, as on CI: the real
        # prerequisites, the folder checks, install.lock, the permission walk, the run's
        # temporary folder, and its own Get-PackageRoot, which stops at the missing
        # checksum file (exit 4).
        zpath = self.root / "Unminute-1.0.0.zip"
        zpath.write_bytes(b"not read")
        self.addCleanup(delete_test_key, TEST_KEY + "-machine")
        copy = self.machine_copy("install-elevated.ps1", "\n".join((
            "function Test-Elevated { $true }",
            "function New-MachineRoot([string]$Root) {",
            "    [void][IO.Directory]::CreateDirectory($Root)",
            "}",
            "function Get-FolderSecurity([string]$Path) { Get-MachineRootSecurity }")))
        env = {"LOCALAPPDATA": str(self.local), "APPDATA": str(self.appdata)}
        r = run_installer(copy, None, self.root, f"-Package '{zpath}'", env=env,
                          cwd=self.root, machine=True)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 4, out)
        self.assertIn("The checksum file", r.stderr)
        self.assertIn("is missing; download it with the package.", r.stderr)
        program_files = self.root / "Program Files"
        self.assertEqual([p.name for p in program_files.iterdir()], ["Unminute"])
        root = program_files / "Unminute"
        # The lock, the log and the run's temporary folder: the last two went with
        # the run, the log stays.
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["logs"])
        self.assertIn("result=stopped code=4",
                      (root / "logs" / "install.log").read_text(encoding="utf-8"))

    def test_both_scripts_have_the_same_machine_section(self):
        def section(name):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            start = text.index("# -- machine mode, shared (#111 B6b) --")
            end = text.index("# -- end of the shared machine section --")
            return text[start:end]
        self.assertEqual(section("install.ps1"), section("uninstall.ps1"))
        shared = section("uninstall.ps1")
        for function in ("Get-MachineRoot", "Test-MachineRootSecurity", "Get-AclChangeCount",
                         "Test-OwnJunction", "Remove-Junction"):
            self.assertIn(f"function {function}", shared)
        # Every machine place outside the install folder is set once, in this section,
        # where the tests redirect it (scratch_text, tests/powershell/Isolation.ps1).
        for line in (PROGRAM_FILES_LINE, COMMON_PROGRAMS_LINE, MACHINE_KEY_LINE, EVENT_KEY_LINE,
                     FIREWALL_GROUP_LINE):
            self.assertIn(line, shared)
        # #111 B7b: the firewall is reached only through these, here and in install.ps1's
        # own section (tests/powershell/Isolation.ps1 checks where each command is).
        for function in ("Get-RuntimeRule", "Remove-RuntimeRule"):
            self.assertIn(f"function {function}", shared)
        # Junctions are removed as links only, never with Remove-Item -Recurse.
        self.assertIn("[IO.Directory]::Delete($Path, $false)", shared)
        for name in ("install.ps1", "uninstall.ps1"):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"Remove-Item[^\n]*(current|Junction)[^\n]*-Recurse")


STUB_APP = '''\
# A stand-in for the app: records how the uninstaller started it, then acts as told.
import json, os, sys, time
from pathlib import Path
install = Path(__file__).resolve().parents[4]
record = Path(os.environ["LOCALAPPDATA"], "Unminute", "workspace", "power_restore.json")
Path(os.environ["UNMINUTE_TEST_LOG"]).write_text(json.dumps({
    "argv": sys.argv[1:], "cwd": os.getcwd(), "isolated": sys.flags.isolated,
    "no_bytecode": sys.dont_write_bytecode, "x": sorted(sys._xoptions),
    "installed": (install / "current.json").is_file()
    and (install / "versions" / "9.9.0" / "runtime" / "python.exe").is_file(),
    "record": record.is_file()}))
mode = os.environ.get("UNMINUTE_TEST_MODE", "0")
if mode == "hang":
    time.sleep(600)
if mode == "crash":
    raise RuntimeError("synthetic failure")
if mode == "noise":
    print("x" * 200000)
    sys.stderr.write("y" * 200000)
sys.exit(int(mode) if mode.isdigit() else 0)
'''


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell's cache")
class ModuleCacheTests(unittest.TestCase):
    """#389, CI time: CI restores the module analysis cache from an earlier run and names
    it in UNMINUTE_PSMODULE_CACHE. A restored file is used as it is, with no warm-up; a
    missing or empty one is filled as before, at that path, so the job can save it. With
    no name, or a relative one, the cache is a temporary file as before (#332). No
    PowerShell runs here: subprocess.run is a stand-in."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for patch in (mock.patch.dict(_MODULE_CACHE, clear=True),
                      mock.patch.dict(os.environ)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("UNMINUTE_PSMODULE_CACHE", None)

    def cache(self, name=None):
        """module_cache() with name in UNMINUTE_PSMODULE_CACHE: (its path, the warm-up
        runs' cache paths)."""
        if name is not None:
            os.environ["UNMINUTE_PSMODULE_CACHE"] = str(name)
        done = subprocess.CompletedProcess([], 0, "", "")
        with (mock.patch.object(subprocess, "run", return_value=done) as run,
              mock.patch("sys.stderr")):                      # its notes for the CI log
            path = module_cache()
            self.assertEqual(path, module_cache())          # once per process
        return path, [c.kwargs["env"]["PSModuleAnalysisCachePath"] for c in run.call_args_list]

    def test_a_restored_cache_is_used_without_a_warm_up(self):
        given = self.root / "restored" / "ModuleAnalysisCache"
        given.parent.mkdir()
        given.write_bytes(b"cached")
        self.assertEqual((str(given), []), self.cache(given))
        self.assertEqual(b"cached", given.read_bytes())       # left as it was

    def test_a_missing_cache_is_warmed_at_the_named_path(self):
        given = self.root / "not-restored" / "ModuleAnalysisCache"
        self.assertEqual((str(given), [str(given)]), self.cache(given))
        self.assertTrue(given.parent.is_dir())               # the job saves it from there

    def test_an_empty_cache_is_warmed_again(self):
        given = self.root / "ModuleAnalysisCache"
        given.write_bytes(b"")
        self.assertEqual((str(given), [str(given)]), self.cache(given))

    def test_with_no_name_or_a_relative_one_the_cache_is_a_temporary_file(self):
        for name in (None, "", "relative/ModuleAnalysisCache"):
            with self.subTest(name=name):
                _MODULE_CACHE.clear()
                path, warmed = self.cache(name)
                self.assertEqual([path], warmed)
                self.assertTrue(os.path.isabs(path))
                self.assertTrue(Path(path).parent.name.startswith("unminute-psmodules-"))


@unittest.skipUnless(sys.platform == "win32", "the uninstaller is Windows PowerShell")
class UninstallLidRestoreTests(unittest.TestCase):
    """#191: before removing anything, the uninstaller runs the installed app
    once with --restore-lid, with a hard time limit, and carries on whatever
    happens. Throwaway installs in temporary folders, with a temporary
    LOCALAPPDATA, Start menu folder and uninstall key (DataFolderOverlapTests).
    The runtime is a copy of this Python's interpreter whose ._pth file gives
    it the standard library and the install's app folder, as the package's
    runtime has its own. A stand-in app records how it was started; the real
    app's switch is run only with records that never reach the power scheme."""

    uninstaller_copy = DataFolderOverlapTests.uninstaller_copy
    WARNING = "Warning: Unminute could not put back what closing the lid does."
    TIMEOUT = "$LidRestoreSeconds = 60"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local = self.root / "Local"
        self.record = self.local / "Unminute" / "workspace" / "power_restore.json"
        self.record.parent.mkdir(parents=True)
        self.log = self.root / "started.json"
        self.copy, _ = self.uninstaller_copy(self.tmp.name)

    def install(self, name, app=None, features=("gate", "restore-lid"), version="9.9.0"):
        """A throwaway install: current.json, version.json, the runtime and
        the stand-in app (or app, a folder for the runtime's import path)."""
        install = self.root / name
        target = install / "versions" / "9.9.0"
        (target / "app" / "workspace").mkdir(parents=True)
        (target / "app" / "workspace" / "__init__.py").write_text("")
        (target / "app" / "workspace" / "__main__.py").write_text(STUB_APP)
        (target / "version.json").write_text(json.dumps({"version": "9.9.0",
                                                          "features": list(features)}))
        (install / "current.json").write_text(json.dumps({"version": version,
                                                           "path": str(target)}))
        shutil.copy(self.copy, install / "uninstall.ps1")
        base = Path(sys.base_prefix)
        runtime = target / "runtime"
        runtime.mkdir()
        shutil.copy(base / "python.exe", runtime)
        for dll in list(base.glob("python3*.dll")) + list(base.glob("vcruntime*.dll")):
            shutil.copy(dll, runtime)
        versioned = [d for d in base.glob("python3*.dll") if d.stem != "python3"]
        self.assertEqual(len(versioned), 1, versioned)
        (runtime / f"{versioned[0].stem}._pth").write_text("\n".join(
            [str(base / "Lib"), str(base / "DLLs"), str(app or target / "app")]) + "\n")
        return install

    def uninstall(self, install, mode="0", script=None):
        return run_installer(script or self.copy, install, self.root, cwd=self.tmp.name,
                             env={"LOCALAPPDATA": str(self.local),
                                  "UNMINUTE_TEST_LOG": str(self.log), "UNMINUTE_TEST_MODE": mode})

    def started(self):
        return json.loads(self.log.read_text()) if self.log.exists() else None

    def test_the_lid_is_put_back_before_anything_is_removed(self):
        self.record.write_text("{}")                     # its content is the app's business
        install = self.install("Apps")
        r = self.uninstall(install)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Warning", r.stderr)
        started = self.started()
        self.assertEqual(started["argv"], ["--restore-lid"])
        self.assertEqual((started["isolated"], started["no_bytecode"]), (1, True))
        self.assertIn("disable-remote-debug", started["x"])
        self.assertEqual(os.path.normcase(started["cwd"]),
                         os.path.normcase(str(install / "versions" / "9.9.0" / "app")))
        self.assertTrue(started["installed"] and started["record"])   # before removal
        self.assertFalse(install.exists())
        self.assertTrue(self.record.exists())            # the uninstaller never touches it

    def test_nothing_runs_without_a_record(self):
        install = self.install("Apps")
        r = self.uninstall(install)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(self.started())
        self.assertEqual(r.stderr, "")
        self.assertFalse(install.exists())

    def test_any_other_result_still_uninstalls_and_says_to_check(self):
        self.record.write_text("{}")
        for mode in ("1", "2", "3", "crash"):
            with self.subTest(mode=mode):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps{mode}")
                r = self.uninstall(install, mode)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIsNotNone(self.started())
                self.assertIn(self.WARNING, r.stderr)
                self.assertNotIn("synthetic", r.stdout + r.stderr)   # its output is not shown
                self.assertFalse(install.exists())

    def test_a_noisy_restore_cannot_block_it(self):
        # More output than a pipe holds: read, never shown, and no hang.
        self.record.write_text("{}")
        install = self.install("Apps")
        r = self.uninstall(install, "noise")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("xxxx", r.stdout + r.stderr)
        self.assertNotIn("Warning", r.stderr)
        self.assertFalse(install.exists())

    def test_a_hung_restore_is_stopped_and_the_uninstall_carries_on(self):
        import time
        self.record.write_text("{}")
        text = self.copy.read_text(encoding="utf-8")
        self.assertEqual(text.count(self.TIMEOUT), 1)
        quick = self.root / "uninstall-quick.ps1"
        quick.write_text(text.replace(self.TIMEOUT, "$LidRestoreSeconds = 3"), encoding="utf-8")
        install = self.install("Apps")
        began = time.monotonic()
        r = self.uninstall(install, "hang", script=quick)
        self.assertLess(time.monotonic() - began, 120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNotNone(self.started())
        self.assertIn(self.WARNING, r.stderr)
        self.assertFalse(install.exists())               # stopped, so nothing stayed open

    def test_a_version_that_cannot_restore_is_never_started(self):
        # Without the feature, an older app would take the switch for an ordinary
        # start and open its window; a version that is not a plain folder name is
        # never followed, even where it leads to a version that would run. Either
        # way the uninstall carries on and says to check.
        self.record.write_text("{}")
        for name, kwargs in (("old", {"features": ("gate",)}),
                             ("path", {"version": "..\\..\\Elsewhere"}),
                             ("absolute", {"version": str(self.root / "Elsewhere")})):
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                install = self.install(f"Apps-{name}", **kwargs)
                # A version folder that would run, where the path leads.
                shutil.rmtree(self.root / "Elsewhere", ignore_errors=True)
                shutil.copytree(install / "versions" / "9.9.0", self.root / "Elsewhere")
                r = self.uninstall(install)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIsNone(self.started())
                self.assertIn(self.WARNING, r.stderr)
                self.assertFalse(install.exists())

    def test_the_real_switch_runs_from_the_installed_layout(self):
        # The app's own code (this checkout) through the same command line, with a
        # record that cannot say what to put back: it is removed, the power scheme
        # is never read or written, and the uninstall says to check the setting.
        # If an installed copy of the app is open on this computer, the switch
        # stands aside instead and the record stays.
        self.record.write_text('{"scheme": "not-a-guid", "ac": 1, "dc": 1}', encoding="utf-8")
        install = self.install("Apps", app=ROOT)
        open_ = app_is_open()
        r = self.uninstall(install)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(install.exists())
        self.assertIn(self.WARNING, r.stderr)
        self.assertEqual(self.record.exists(), open_)


def app_is_open():
    """Whether an installed copy of the app holds its mutex in this session."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(0x00100000, False, "Local\\Unminute.Workspace")
    error = ctypes.get_last_error()
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    return bool(handle) or error == 5


class RestoreLidFeatureTests(unittest.TestCase):
    def test_the_package_declares_the_switch_the_uninstaller_looks_for(self):
        build = (ROOT / "tools" / "build_package.py").read_text(encoding="utf-8")
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn('"features": ["gate", "restore-lid", "launch-gate", "offboard", '
                      '"launch-check"]', build)
        self.assertIn('-notcontains "restore-lid"', uninstall)
        main = (ROOT / "workspace" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn('if "--restore-lid" in sys.argv:', main)
        # Before anything is removed: the restore comes after the last refusal and
        # before the first removal.
        body = uninstall[uninstall.index("try {\n    # An empty -InstallDir"):]
        self.assertLess(body.index("did not put there"), body.index("    Restore-LidSetting\n"))
        self.assertLess(body.index("    Restore-LidSetting\n"), body.index("Remove-Item"))


class RemoteDebugTests(unittest.TestCase):
    """#111 B4: Python 3.14 lets another process run code in a running Python
    process (PEP 768). Every app process starts with -X disable-remote-debug."""

    def test_the_app_and_its_processes_start_with_remote_debugging_off(self):
        install = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        args = '$LaunchArgs = "-I -B -X disable-remote-debug -m workspace"'
        self.assertEqual(install.count(args), 1)
        self.assertEqual(install.count("Arguments = $LaunchArgs"), 2)   # new and pinned
        self.assertNotIn('Arguments = "-m workspace"', install)
        # The gate starts with the same options (#111 B6a: one list for every start).
        self.assertIn('$gateArgs = @($LaunchArgs -split " ") + "--gate"', install)
        # No other list of options: "-m workspace" ends a quoted string only there.
        self.assertEqual(install.count('-m workspace"'), 1)
        self.assertNotIn('"-m", "workspace"', install)
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertEqual(uninstall.count(args), 1)
        self.assertIn('$start.Arguments = "$LaunchArgs --restore-lid"', uninstall)
        # -Offboard's runs too (#111 B8b), and no other list of the options.
        self.assertIn('$start.Arguments = "$LaunchArgs $Switch"', uninstall)
        self.assertEqual(uninstall.count("-I -B "), 1)
        self.assertEqual(uninstall.count('-m workspace"'), 1)
        self.assertNotIn('"-m", "workspace"', uninstall)
        media = (ROOT / "workspace" / "media.py").read_text(encoding="utf-8")
        self.assertIn('"-I", "-B", "-X", "disable-remote-debug", "-c"', media)

    def test_the_option_turns_it_off_and_workers_inherit_it(self):
        # What multiprocessing passes to the workers it starts, from a parent
        # started as the shortcut starts the app.
        # (Python 3.13 and earlier have no such interface, and so nothing to pass on.)
        code = ("import subprocess, sys; "
                "print(hasattr(sys, 'is_remote_debug_enabled')); "
                "print(getattr(sys, 'is_remote_debug_enabled', lambda: False)()); "
                "print(subprocess._args_from_interpreter_flags())")
        r = subprocess.run([sys.executable, "-X", "disable-remote-debug", "-c", code],
                           capture_output=True, text=True, timeout=60)
        has, enabled, flags = r.stdout.splitlines()
        self.assertEqual(enabled, "False", r.stderr)
        if has == "True":
            self.assertIn("'-X', 'disable-remote-debug'", flags)


class NvidiaSmiTests(unittest.TestCase):
    def test_never_found_through_path_or_the_current_folder(self):
        from workspace import gpu
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "nvidia-smi.exe"
            planted.write_text("x")
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch.dict(os.environ, {"PATH": tmp}):
                    found = gpu._nvidia_smi()
            finally:
                os.chdir(cwd)
        self.assertNotEqual(found and os.path.normcase(os.path.dirname(found)),
                            os.path.normcase(tmp))


if __name__ == "__main__":
    unittest.main()
