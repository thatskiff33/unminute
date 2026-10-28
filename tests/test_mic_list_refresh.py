# tests/test_mic_list_refresh.py
# The page's microphone lists follow microphones plugged in or out while the
# app is open (#369). The app lists the devices afresh on each
# microphone_devices call while no stream is open (recorder.list_devices); the
# page used to ask once, so a headset connected later never appeared until a
# restart. Now it asks again when a list is opened or focused and on returning
# to the page (another view and back, or the window shown again), and never
# while recording or on an idle poll. The microphone selected stays selected
# while it is there; if it has gone, the page says so and selects the Windows
# default, without starting or testing anything.
#
# While recording (#462) the card has its own choice of microphone: the
# microphones Windows has now, asked for once when the card shows, after its
# microphone changes, and when that list is focused (not on idle polls).
# Choosing one asks the app to switch the recording to it, and the card then
# says which microphone records; a refusal is said and the list shows the one
# still recording.
#
# The real page runs in headless Microsoft Edge with the design check's fake
# bridge (tools/design_check/fixtures.js) and synthetic device names, wrapped
# here so the test can change the devices and count the calls: no app window,
# audio device or workspace. A real headset is checked by hand (#189).
#   python tools/run_tests.py

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
import design_check  # noqa: E402

EDGE = next((p for p in design_check.EDGE if p.is_file()), None)

LAPTOP = "Laptop microphone (sample)"
HEADSET = "Headset (sample)"
USB = "USB microphone (sample)"

# Loaded after fixtures.js and before app.js: the fixture bridge, with the
# devices and the recording state under the test's control, every call counted
# and the window's visibility settable. delay: how long the device list takes;
# fail: the list cannot be read (the app's error when PortAudio cannot load);
# changed: Start and Test are refused as the app refuses a device that no
# longer matches the list.
BRIDGE = """
(() => {
  const real = window.pywebview.api;
  const t = window.__mic = {
    mics: [{ index: 1, name: __LAPTOP__, default: true }],
    recording: false, hidden: false, calls: {}, delay: 0, fail: false, changed: false,
    // #462, while recording: what Windows lists (null: the start list), the
    // microphone recording, whether it switched, a switch refused, and the
    // arguments of each switch asked for.
    recMics: null, device: __LAPTOP__, switched: false, paused: false, refuse: false, switches: [],
  };
  Object.defineProperty(document, "hidden", { configurable: true, get: () => t.hidden });
  Object.defineProperty(document, "visibilityState", { configurable: true,
    get: () => (t.hidden ? "hidden" : "visible") });
  window.pywebview = { api: new Proxy({}, { get: (_, method) => async (...args) => {
    t.calls[method] = (t.calls[method] || 0) + 1;
    if (method === "microphone_devices" && t.recording && t.recMics)
      return { ok: true, value: t.recMics.map((d) => ({ ...d, current: d.name === t.device })) };
    if (method === "switch_microphone") {
      t.switches.push(args);
      if (t.refuse) return { ok: false, error: "That microphone could not be opened, so the "
                                               + "recording carries on from the same microphone." };
      t.device = args[1];
      t.switched = true;
      return { ok: true, value: {} };
    }
    if (method === "microphone_devices") {
      const mics = t.mics.map((d) => ({ ...d }));
      if (t.delay) await new Promise((r) => setTimeout(r, t.delay));
      return t.fail ? { ok: false, error: "Microphone support could not start in this "
                        + "installation. Reinstall the app, or contact support." }
        : { ok: true, value: mics };
    }
    if (t.changed && (method === "start_recording" || method === "test_microphone"))
      return { ok: false,
               error: "The list of microphones has changed. Choose the microphone again." };
    const r = await real[method](...args);
    if (method === "recording_status" && t.recording && r.ok)
      return { ok: true, value: { ...r.value, recording: { state: t.paused ? "paused" : "recording",
        seconds: 5, level_db: -30, device: t.device, switched: t.switched, events: [] } } };
    return r;
  } }) };
})();
""".replace("__LAPTOP__", json.dumps(LAPTOP))

# Run after app.js: the steps of one run (?mic=meetings|recording|setup), then
# the observations as JSON in <pre id="mic-result">.
STEPS = """
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const $ = (id) => document.getElementById(id);
  const t = window.__mic, ws = window.workspace;
  const LAPTOP = { index: 1, name: __LAPTOP__, default: true };
  const HEADSET = { index: 3, name: __HEADSET__, default: false };
  const USB = { index: 4, name: __USB__, default: false };
  const n = () => t.calls.microphone_devices || 0;
  const names = (sel) => [...sel.options].map((o) => o.dataset.name);
  const chosen = (sel) => (sel.selectedOptions[0] ? sel.selectedOptions[0].dataset.name : null);
  const look = (sel) => ({ options: names(sel), chosen: chosen(sel) });
  const visibility = async (hidden) => {
    t.hidden = hidden;
    document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
    await wait(1500);
  };
  const choose = (sel, index) => {
    sel.value = String(index);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const errors = [];
  window.addEventListener("error", (e) => errors.push(String(e.message || e)));
  window.addEventListener("unhandledrejection", (e) => errors.push(String(e.reason)));
  while (document.documentElement.dataset.ready !== "1") await wait(100);
  await wait(500);
  const run = new URLSearchParams(location.search).get("mic");
  const out = { has_focus: document.hasFocus() };
  try {
    if (run === "meetings") {
      const sel = $("rec-device");
      out.first = { calls: n(), ...look(sel) };
      // Idle: three 10 s polls ask nothing of the microphones.
      let c = n();
      await wait(35000);
      out.idle_calls = n() - c;
      // A headset is connected; the list is opened.
      t.mics = [LAPTOP, HEADSET];
      c = n();
      $("mic-change").click();
      await wait(1500);
      out.opened = { calls: n() - c, ...look(sel) };
      // The headset is chosen, then disconnected; back from Settings.
      choose(sel, 3);
      out.chosen_label = $("mic-name").textContent;
      $("mic-change").click();                    // the chooser closes
      t.mics = [LAPTOP];
      ws.show("settings");
      await wait(1500);
      ws.show("meetings");
      await wait(1500);
      out.returned = { ...look(sel), mic_name: $("mic-name").textContent,
                       toast: $("status").textContent,
                       starts: t.calls.start_recording || 0, tests: t.calls.test_microphone || 0 };
      // Connected again while the window was minimised.
      t.mics = [LAPTOP, HEADSET];
      await visibility(true);
      await visibility(false);
      out.shown_again = look(sel);
      // Another is connected while the chooser is open: focusing the list asks again.
      $("mic-change").click();
      await wait(4000);
      t.mics = [LAPTOP, HEADSET, USB];
      sel.blur();
      c = n();
      sel.focus();
      await wait(1500);
      out.focused = { calls: n() - c, ...look(sel) };
      // A microphone other than the default is chosen; another one leaves. It
      // stays chosen, and nothing is said.
      choose(sel, 3);
      await wait(4000);
      t.mics = [LAPTOP, HEADSET];
      $("status").textContent = "";
      sel.blur();
      sel.focus();
      await wait(1500);
      out.kept = { ...look(sel), toast: $("status").textContent };
      // Its number changes (PortAudio renumbers after a change), its name does
      // not: still the one chosen, at its new number. Back from Settings.
      t.mics = [LAPTOP, { ...USB, index: 2 }, { ...HEADSET, index: 5 }];
      ws.show("settings");
      await wait(1500);
      ws.show("meetings");
      await wait(1500);
      out.shifted = { ...look(sel), value: sel.value, toast: $("status").textContent };
      // Focused again within 3 s of the last ask: not asked again; after: asked.
      t.mics = [LAPTOP, { ...HEADSET, index: 5 }];
      c = n();
      sel.blur();
      sel.focus();
      await wait(1000);
      out.throttled = { calls: n() - c, ...look(sel) };
      await wait(2500);
      sel.blur();
      sel.focus();
      await wait(1500);
      out.after_throttle = { calls: n() - c, ...look(sel) };
      // Test refused as "changed": the list is reloaded, keeping the choice
      // at its new number.
      t.mics = [LAPTOP, { ...HEADSET, index: 7 }];
      t.changed = true;
      c = n();
      $("rec-test").click();
      await wait(1500);
      out.changed_test = { calls: n() - c, ...look(sel), value: sel.value,
                           toast: $("status").textContent };
      // Start refused as "changed", the chosen one gone: the default is selected.
      t.mics = [LAPTOP];
      c = n();
      $("rec-start-chosen").click();
      await wait(1500);
      out.changed_start = { calls: n() - c, ...look(sel), toast: $("status").textContent };
      t.changed = false;
    } else if (run === "recording") {
      const sel = $("rec-device");
      t.recording = true;
      await ws.refresh();
      await wait(1500);
      out.recording_shown = !$("rec-card").hidden;
      const c = n();
      t.mics = [LAPTOP, HEADSET];
      ws.show("settings");
      await wait(1500);
      ws.show("meetings");
      await wait(1500);
      await visibility(true);
      await visibility(false);
      $("mic-change").click();
      sel.focus();
      await wait(20000);                           // ten 2 s polls while recording
      out.recording_calls = n() - c;
      out.during = look(sel);
      // Once the recording has ended, the card goes at once, without waiting
      // for the list, and the list catches up when it arrives.
      t.delay = 3000;
      t.recording = false;
      ws.refresh();
      await wait(500);
      out.drawn = { card_hidden: $("rec-card").hidden, start_shown: !$("start").hidden,
                    ...look(sel) };
      await wait(4000);
      out.after = look(sel);
    } else if (run === "choice") {
      // #462: the card's choice of microphone while recording.
      const sel = $("rec-mic");
      t.recMics = [{ index: 0, name: LAPTOP.name, label: LAPTOP.name, default: true },
                   { index: 1, name: HEADSET.name, label: HEADSET.name, default: false }];
      const before = n();                          // the start list's ask when the page opened
      t.recording = true;
      await ws.refresh();
      await wait(1500);
      out.first = { calls: n() - before, shown: !$("rec-mic-row").hidden, ...look(sel),
                    note: $("rec-note").textContent };
      // Polls while recording ask nothing more.
      let c = n();
      await wait(10000);
      out.idle_calls = n() - c;
      // The headset is chosen: the app is asked to switch, by its place and name.
      c = n();
      choose(sel, 1);
      await wait(2500);
      out.switched = { switches: [...t.switches], calls: n() - c, ...look(sel),
                       note: $("rec-note").textContent };
      // Paused: still offered. A refused switch is said, and the list shows
      // the one still recording.
      t.paused = true;
      await wait(2500);
      out.paused_shown = !$("rec-mic-row").hidden;
      t.refuse = true;
      $("status").textContent = "";
      choose(sel, 0);
      await wait(2500);
      out.refused = { ...look(sel), toast: $("status").textContent };
      t.refuse = false;
      t.paused = false;
      // Once the recording has ended, the choice goes with the card.
      t.recording = false;
      await ws.refresh();
      await wait(1500);
      out.ended_hidden = $("rec-mic-row").hidden && $("rec-card").hidden;
    } else if (run === "setup") {
      const sel = $("su-device");
      out.first = { calls: n(), ...look(sel) };
      // Connected while on step 4: focusing the list asks again.
      t.mics = [LAPTOP, HEADSET];
      await wait(4000);
      sel.blur();
      let c = n();
      sel.focus();
      await wait(1500);
      out.focused = { calls: n() - c, ...look(sel) };
      // Chosen, then disconnected while the window was minimised.
      choose(sel, 3);
      t.mics = [LAPTOP];
      await visibility(true);
      await visibility(false);
      out.shown_again = { ...look(sel), toast: $("status").textContent,
                          test_disabled: $("su-test").disabled };
      // Idle on step 4: the 2 s polls ask nothing of the microphones.
      c = n();
      await wait(10000);
      out.idle_calls = n() - c;
      // Back to step 4 from step 3, with the headset connected again.
      t.mics = [LAPTOP, HEADSET];
      ws.setStep(3);
      await wait(500);
      ws.setStep(4);
      await wait(1500);
      out.step_again = look(sel);
      // Test refused as "changed": reloaded, keeping the choice at its new number.
      choose(sel, 3);
      t.mics = [LAPTOP, { ...HEADSET, index: 8 }];
      t.changed = true;
      c = n();
      $("su-test").click();
      await wait(1500);
      out.changed_test = { calls: n() - c, ...look(sel), value: sel.value,
                           toast: $("status").textContent };
      t.changed = false;
      // The list cannot be read: asked once when the step is shown, not on
      // every 2 s poll after.
      t.fail = true;
      ws.setStep(3);
      await wait(500);
      c = n();
      ws.setStep(4);
      await wait(1000);
      const said = $("status").textContent;   // a toast that is not a confirmation clears after 8 s
      await wait(9000);
      out.failed = { calls: n() - c, toast: said };
      t.fail = false;
    }
  } catch (e) {
    out.error = String((e && e.stack) || e);
  }
  out.errors = errors;
  out.ui_error_reports = t.calls.report_ui_error || 0;
  const pre = document.createElement("pre");
  pre.id = "mic-result";
  pre.textContent = JSON.stringify(out);
  document.body.append(pre);
})();
""".replace("__LAPTOP__", json.dumps(LAPTOP)).replace("__HEADSET__", json.dumps(HEADSET)).replace(
    "__USB__", json.dumps(USB))

# Each run: the fixtures.js scenario it starts from.
RUNS = {"meetings": "home", "recording": "home", "choice": "home", "setup": "setup-4"}


class Handler(design_check.Handler):
    """The design check's server, with the wrapper before app.js and the steps
    after it (the page's content security policy allows scripts from the
    page's own origin only)."""

    def do_GET(self):
        path = urlsplit(self.path).path.lstrip("/")
        if path == "app/mic-bridge.js":
            return self._send(BRIDGE.encode("utf-8"), "text/javascript; charset=utf-8")
        if path == "app/mic-steps.js":
            return self._send(STEPS.encode("utf-8"), "text/javascript; charset=utf-8")
        if path in ("app/", "app/index.html"):
            page = (design_check.UI / "index.html").read_text(encoding="utf-8")
            page = page.replace(design_check.APP_TAG,
                                '<script src="/tools/design_check/fixtures.js"></script>\n'
                                '<script src="mic-bridge.js"></script>\n'
                                + design_check.APP_TAG + '\n<script src="mic-steps.js"></script>')
            return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        return super().do_GET()


class Server(http.server.ThreadingHTTPServer):
    # Edge asks for the page's files at once: a longer queue than the default
    # five, so no connection is refused (as tests/test_ui_page_audit.py).
    request_queue_size = 64
    daemon_threads = True


def run_page(base, run):
    url = f"{base}/app/index.html?mic={run}#{RUNS[run]}"
    # Headless Edge now and then never finishes a page, or loses one of its
    # files: one more try, with a fresh profile (a timed-out Edge may still
    # hold the first).
    problem = None
    for _attempt in (1, 2):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            cmd = [str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                   "--no-first-run", "--disable-extensions", f"--user-data-dir={profile}",
                   "--mute-audio", "--force-device-scale-factor=1", "--window-size=1100,760",
                   "--virtual-time-budget=120000", "--dump-dom", url]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=180)
            except subprocess.TimeoutExpired:
                problem = f"Edge did not finish {run} in 180 seconds"
                continue
        found = re.search(r'<pre id="mic-result">(.*?)</pre>', out.stdout or "", re.DOTALL)
        if found:
            return json.loads(html.unescape(found.group(1)))
        problem = (f"Edge returned no result (exit {out.returncode}): "
                   f"{(out.stderr or '').strip()[-300:]}")
    return {"error": f"twice: {problem}"}


@unittest.skipUnless(EDGE, "Microsoft Edge not installed")
class MicrophoneListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            # One Edge at a time: each run takes seconds, and none competes
            # with another for the server or the machine.
            cls.results = {run: run_page(base, run) for run in RUNS}
        finally:
            server.shutdown()
            server.server_close()

    def result(self, run):
        r = self.results[run]
        self.assertNotIn("error", r, r.get("error"))
        self.assertEqual([], r["errors"], "the page threw")
        self.assertEqual(0, r["ui_error_reports"], "the page reported a script error")
        return r

    def test_a_microphone_connected_later_appears_when_the_list_is_opened(self):
        r = self.result("meetings")
        self.assertEqual({"calls": 1, "options": [LAPTOP], "chosen": LAPTOP}, r["first"])
        self.assertEqual(1, r["opened"]["calls"])
        self.assertEqual([LAPTOP, HEADSET], r["opened"]["options"])
        self.assertEqual(LAPTOP, r["opened"]["chosen"], "the selected microphone changed")
        self.assertEqual(HEADSET, r["chosen_label"])

    def test_a_chosen_microphone_that_has_gone_falls_back_to_the_default_and_says_so(self):
        r = self.result("meetings")["returned"]
        self.assertEqual([LAPTOP], r["options"])
        self.assertEqual(LAPTOP, r["chosen"])
        self.assertEqual(LAPTOP, r["mic_name"])
        self.assertIn("no longer connected", r["toast"])
        self.assertIn("Windows default", r["toast"])
        self.assertNotIn(HEADSET, r["toast"], "device names stay in the list")
        self.assertEqual((0, 0), (r["starts"], r["tests"]), "nothing is started or tested")

    def test_the_list_is_asked_again_when_the_window_is_shown_or_the_list_focused(self):
        r = self.result("meetings")
        self.assertTrue(r["has_focus"], "the page had no focus: focus was not tried")
        self.assertEqual({"options": [LAPTOP, HEADSET], "chosen": LAPTOP}, r["shown_again"])
        self.assertEqual(1, r["focused"]["calls"])
        self.assertEqual([LAPTOP, HEADSET, USB], r["focused"]["options"])
        self.assertEqual(LAPTOP, r["focused"]["chosen"])

    def test_a_chosen_microphone_stays_chosen_while_it_is_connected(self):
        r = self.result("meetings")
        self.assertEqual({"options": [LAPTOP, HEADSET], "chosen": HEADSET, "toast": ""},
                         r["kept"])
        shifted = r["shifted"]
        self.assertEqual(([LAPTOP, USB, HEADSET], HEADSET, "5", ""),
                         (shifted["options"], shifted["chosen"], shifted["value"],
                          shifted["toast"]))

    def test_focusing_again_within_3_seconds_does_not_ask(self):
        r = self.result("meetings")
        self.assertEqual(0, r["throttled"]["calls"])
        self.assertEqual([LAPTOP, USB, HEADSET], r["throttled"]["options"])
        self.assertEqual(1, r["after_throttle"]["calls"])
        self.assertEqual([LAPTOP, HEADSET], r["after_throttle"]["options"])

    def test_a_changed_list_reloads_it_keeping_the_choice_where_it_can(self):
        changed = "The list of microphones has changed"
        test = self.result("meetings")["changed_test"]
        self.assertEqual((1, HEADSET, "7"), (test["calls"], test["chosen"], test["value"]))
        self.assertIn(changed, test["toast"])
        start = self.result("meetings")["changed_start"]
        self.assertEqual((1, [LAPTOP], LAPTOP),
                         (start["calls"], start["options"], start["chosen"]))
        self.assertIn(changed, start["toast"])
        setup = self.result("setup")["changed_test"]
        self.assertEqual((1, HEADSET, "8"), (setup["calls"], setup["chosen"], setup["value"]))
        self.assertIn(changed, setup["toast"])

    def test_setup_does_not_ask_again_on_every_poll_after_a_failure(self):
        r = self.result("setup")["failed"]
        self.assertEqual(1, r["calls"])
        self.assertIn("could not start", r["toast"])

    def test_an_idle_page_does_not_ask(self):
        self.assertEqual(0, self.result("meetings")["idle_calls"])
        self.assertEqual(0, self.result("setup")["idle_calls"])

    def test_nothing_is_asked_while_recording(self):
        # The start list is not asked for while recording; the card's own
        # choice (#462) asks once when the card first shows, before this count.
        r = self.result("recording")
        self.assertTrue(r["recording_shown"], "the recording did not show: nothing was tested")
        self.assertEqual(0, r["recording_calls"])
        self.assertEqual([LAPTOP], r["during"]["options"])
        self.assertEqual({"card_hidden": True, "start_shown": True, "options": [LAPTOP],
                          "chosen": LAPTOP}, r["drawn"], "the card waited for the list")
        self.assertEqual({"options": [LAPTOP, HEADSET], "chosen": LAPTOP}, r["after"])

    def test_the_card_offers_the_microphones_windows_has_while_recording(self):
        # #462: asked for once when the card shows, the one recording selected,
        # and not again on the polls that follow.
        r = self.result("choice")
        self.assertEqual({"calls": 1, "shown": True, "options": [LAPTOP, HEADSET],
                          "chosen": LAPTOP}, {k: r["first"][k] for k in
                                              ("calls", "shown", "options", "chosen")})
        self.assertTrue(r["first"]["note"].startswith(LAPTOP + ". "), r["first"]["note"])
        self.assertEqual(0, r["idle_calls"])

    def test_choosing_a_microphone_switches_the_recording_and_says_which(self):
        r = self.result("choice")
        switched = r["switched"]
        self.assertEqual([[1, HEADSET]], switched["switches"])
        self.assertEqual(HEADSET, switched["chosen"])
        self.assertTrue(switched["note"].startswith(f"Now recording from {HEADSET}. "),
                        switched["note"])
        self.assertTrue(r["paused_shown"], "the choice went while paused")
        refused = r["refused"]
        self.assertEqual(HEADSET, refused["chosen"], "a refused switch left the list wrong")
        self.assertIn("could not be opened", refused["toast"])
        self.assertTrue(r["ended_hidden"])

    def test_setup_step_4_follows_the_microphones_too(self):
        r = self.result("setup")
        self.assertEqual([LAPTOP], r["first"]["options"])
        self.assertEqual(1, r["focused"]["calls"])
        self.assertEqual({"options": [LAPTOP, HEADSET], "chosen": LAPTOP},
                         {k: r["focused"][k] for k in ("options", "chosen")})
        gone = r["shown_again"]
        self.assertEqual(([LAPTOP], LAPTOP), (gone["options"], gone["chosen"]))
        self.assertIn("no longer connected", gone["toast"])
        self.assertFalse(gone["test_disabled"])
        self.assertEqual({"options": [LAPTOP, HEADSET], "chosen": LAPTOP}, r["step_again"])


if __name__ == "__main__":
    unittest.main()
