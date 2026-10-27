# tools/perf_check.py
# Release-time performance evidence (docs/RELEASE_STANDARD.md, section 2; #124).
#
# Opens the real Unminute window (WebView2) on a throwaway workspace holding
# synthetic meetings made by fake engines, puts it in each state in turn and
# measures the whole process tree from outside: CPU time (kernel plus user, via
# a Job Object so processes that exit mid-window still count, cross-checked by
# summing the live tree) and private bytes. Working sets are reported for
# reference only and never used for budgets: summing them counts shared pages
# once per process.
#
# The recording states record synthetic audio, never the room: the tool puts a
# paced stream (PacedStream) in place of the microphone through the recorder's
# open_stream seam and answers the app's own recording question itself, so the
# recording is started, confirmed and saved with its consent record by the
# shipped code. Both stand-ins exist only in this tool's child process.
#
# Run "python tools/perf_check.py --help" for the states, the output and the
# exit codes. The window is visible for the whole run; leave the machine idle,
# on mains power, on the Balanced power plan. The worker's peak memory and
# speed need real models and come from tools/packaged_check.py.
#
# A visible state costs what it costs only while it is drawn (#292): the run
# keeps the display on for its own duration, and each window measured with the
# window on screen counts only if the display stayed on, the session unlocked
# and the page drawing (drawing_report()); otherwise the run stops, exit 2.
#
# Before the windowed runs, a release run measures the sealed store once, with
# no window (#111 C7, the C2b-2 design's section 4.8): measure_store().

import argparse
import json
import math
import os
import platform
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# evidence.py, residue.py and windows_powershell.py, also under -I; after the stdlib
sys.path.append(str(ROOT / "tools"))
import evidence  # noqa: E402
import residue  # noqa: E402
import windows_powershell  # noqa: E402

BUDGETS = ROOT / "docs" / "perf-budgets.json"
# The recording states come last, after the leak check too (plan()): every
# other figure is measured before any recording has started. One recording
# runs through both recording states and is discarded before quitting.
STATES = ("meetings", "settings", "minimised", "recording", "recording-minimised")
RECORDING_STATES = ("recording", "recording-minimised")
IDLE_STATES = tuple(s for s in STATES if s not in RECORDING_STATES)
# Through a measuring window the recording must keep recording and its length
# grow by at least this share of the time that passed (the synthetic input is
# paced in real time; a hold-up over PACE_MAX_LATE_S is skipped, not caught up).
RECORDING_HELD_SHARE = 0.5
POLICY_SKIPPED = "not measured (policy)"
MEETINGS = 3                      # synthetic meetings in the measured workspace
# The synthetic microphone: a common Windows input rate, the recorder's block.
SYNTHETIC_RATE = 48000
SYNTHETIC_BLOCK_S = 0.1           # recorder.BLOCK_S, checked by the child at start
SYNTHETIC_DEVICE = {"index": 0, "name": "Synthetic input (performance check)",
                    "label": "Synthetic input (performance check)", "default": True,
                    "rate": SYNTHETIC_RATE}
PACE_MAX_LATE_S = 1.0             # held up longer: carry on from now, never a burst
PREFIX = "PERF "                  # protocol lines between parent and child
WORKER_WAIT_S = 240               # the worker unloads after 120 s idle (worker.py)
READY_TIMEOUT_S = 600             # seeding (up to 300 s) plus the page starting
STEP_TIMEOUT_S = 90               # the window reaching a state
# The child's own limits while starting a recording (record()): the Record
# button ready, the recording card shown, the first second of audio. A state
# that starts a recording gets these on top of STEP_TIMEOUT_S (step_timeout()).
RECORD_WAITS_S = {"button": 30, "started": 30, "audio": 30}
LEAK_BUDGET_MINUTES = 30          # the leak budget is growth over this long
# A release run's shape; release evidence needs at least this (tools/evidence.py).
RELEASE_RUNS = evidence.PERF_RELEASE["runs"]
RELEASE_SETTLE_S = evidence.PERF_RELEASE["settle_s"]
RELEASE_WINDOW_S = evidence.PERF_RELEASE["window_s"]
# The states with the window on screen (#292). Each window measured in one
# counts only if it was drawn (drawing_report()); release evidence checks the
# record of that (tools/evidence.py).
VISIBLE_STATES = evidence.PERF_VISIBLE_STATES
# The level meter draws while recording with the window in view: on every
# display frame up to METER_MAX_HZ, then on every Nth frame for about that rate
# (#294; METER_HZ in workspace/ui/app.js, about 60 a second: the owner's
# decision of 23 Oct 2026, #216). Its draws are counted, not the page's frames,
# and a window counts only if it drew at least half its expected rate
# (min_meter_draws()) and was never undrawn for longer than MAX_UNDRAWN_S at a
# stretch.
DRAWING_STATES = ("recording",)
METER_MAX_HZ = 60
MAX_UNDRAWN_S = 5.0
# GUID_SESSION_DISPLAY_STATUS's values. A dimmed display still draws every frame.
DISPLAY_STATES = {0: "off", 1: "on", 2: "dimmed"}
# The store step (#111 C7): no window, run once, in its own throwaway folder.
STORE_MEETINGS = 500               # meetings in the workspace whose open is timed
STORE_PASSAGES = 5000              # passages in the long meeting (tests/test_app.py's)
STORE_OPENS = 10                   # opens timed, median
STORE_READS = 5                    # listings, pages and searches timed, median
STORE_TIMEOUT_S = 3600
STORE_FIGURES = evidence.PERF_STORE_FIGURES
# Machines whose figures can be compared (--compare): these fields must match.
COMPARABLE = ("cpu", "cores", "logical_processors", "memory_gb", "power_plan",
              "on_mains_power")
NOT_COMPARABLE = "not comparable"
DRAWN_DISPLAY = ("on", "dimmed")
HELP = """
States, in order: meetings (the Meetings view, 3 synthetic meetings), settings
(the Settings view), minimised, recording (the Meetings view while recording,
with the level display running) and recording-minimised (minimised while
recording). The recording states record synthetic audio fed in at real-time
pace in place of the microphone (48 kHz, 0.1 s blocks); no microphone or
audio device is opened. The tool turns microphone recording on in the
throwaway workspace, presses Record and answers the app's recording question
itself. That one recording runs through the app's own code for both recording
states, is checked after each window to be still recording (one that stopped
or stalled means the run could not be measured, exit 2), and is discarded
after them, never saved or imported. Its figures include the stand-in's own
feeding thread (small) and leave out the Windows audio service, which a real
microphone would also use.

Managed policy: where policy turns microphone recording off, the recording
states are "not measured (policy)": the other figures are still measured and
the exit code follows the budgets (a budget set for a recording state then
counts as not measured, exit 1). Where policy turns the recording confirmation
off, the app asks nothing and makes no consent record; the tool accepts that
and the report says so ("recording_consent").

Before the first state it times start-up to interactive (imports, with the
window's backend that the app loads alongside them, then window creation until
the meeting list has rendered; interpreter start and opening the workspace are
left out) and waits for the worker to unload. It also reports "workspace_s" in
"startup": the time to create the run's new workspace, which includes making
its database schema and its root key, protected by Windows Data Protection
(CryptProtectData). It does not time unlocking an existing workspace's key;
the store step's open_0_meetings_s does. Both have budgets (#430): workspace_s
bounds creating a new workspace, not unlocking one. With
--leak-minutes it holds the Meetings view that long, once, in the last run,
after the idle states and before any recording, and reports the growth. The
leak budget is for 30 minutes, so a shorter hold is reported but not compared:
short-window noise cannot be scaled up.

Drawn on screen (#292): a visible state's drawing cost is only paid while the
display is on and the window is drawn, so the run asks Windows to keep the
display on (and the machine awake) for its own duration, without changing the
power plan, and records whether it could ("display": "kept_awake") and the
display's refresh rate once it is kept on ("refresh_hz", which the machine
block then records; with the rate read before, "refresh_hz_before_kept_on",
if the two differ: dynamic refresh can lower it while idle). A run that starts
with the display off or the session locked stops before measuring (exit 2).
Each window measured in a visible state (meetings, settings, recording)
counts only if the display stayed on (Windows' display status for the
session), the session was not locked and showed no screen saver, and the page
was visible and drew: at least one frame in every such window, and while
recording, the level display drawn at least half as often as it should be
(it draws on every display frame up to about 60 a second: so at least 30 a
second on a 60 Hz or faster display, 24 on a 48 Hz one; 60 Hz is assumed if
the rate is not known), with no stretch longer than 5 s undrawn. The page's
frames are counted by a wrapper the tool puts around requestAnimationFrame in
its own window, and the level display's draws by a wrapper around clearRect
that counts the animation frames in which the level display's canvas was
cleared for drawing, so counting adds no frames; the tool asks for one frame
as each window starts, to see one drawn.
Each such window's "drawing" block records what was seen ("page_frames_per_s",
"meter_draws_per_s", "min_meter_draws_per_s", "longest_undrawn_s"). A window
that was not drawn stops the run (exit 2), in every mode, --quick included:
keep the session unlocked and the window in view.

Keeping the display on does not stop a screen saver or an inactivity lock set
by Intune or other policy (a laptop whose policy locks after about 60 minutes
without input locks part-way through a release run). An unattended release
run must account for that, or the run stops with exit 2 when the session
locks or the screen saver starts.

Output: a JSON report with the machine's specification ("machine": processor,
cores and logical processors, memory, Windows build, power plan, mains or
battery, display adapters and drivers, the primary display's refresh rate,
WebView2 version; nothing that names the computer or its user), the app
version, the display block above, every run's figures, the medians
(for the worker's unload, the slowest run: a pass or fail, not a noisy
figure), anything over budget or not measured, and budgets proposed from these
figures (plus 20%). There is no named reference machine: the budgets in
docs/perf-budgets.json apply to whichever machine measures, and release
evidence (--zip) needs the specification in full, so a machine it cannot read
stops the run before it starts (exit 2).

Exit codes: 0 within budget (or no budgets file yet); 1 a figure is over its
budget (a worker that did not unload counts as over), or a budgeted figure was
not measured, or the report would have carried the Windows user name (then
the report written says only where, tools/residue.py guarded()); 2 the window
could not be measured (it did not start, did not respond, lost its recording,
was not drawn in a visible state, or left processes outside the measuring
job).

Store step (#111 C7): before the windowed runs, once and with no window, in
its own throwaway folder with the app's own key protection, the sealed store
is measured ("store"): opening a workspace with 0 and with 500 meetings
(median of 10 opens each), listing the 500 meetings, and, for one meeting of
5,000 passages, a page of 1,000 passages and a search (median of 5 each) and
the database's size. Seeded with synthetic text and audio through the app's
own import. Its figures are compared with the "store" budgets in
docs/perf-budgets.json, set from v0.5.0-preview.4's release run (#430), and
proposed again from each run. --quick leaves it out and does not compare it;
release evidence needs it (tools/evidence.py).

--compare <report>: a local copy of an earlier release's evidence-perf.json
(nothing is fetched). Figures present in both reports are listed side by
side ("compared"), with both machines; if the machines differ in processor,
cores, memory, power plan or mains power, each figure is "not comparable".

Release run (docs/RELEASING.md): 3 runs, 60 s settle, 300 s windows and
--leak-minutes 30, about two and a quarter hours, plus the store step.
--quick is 1 run, 20 s settle and 60 s windows (about 10 minutes), for
before-and-after checks during development.
"""


# -- comparing with budgets (pure; tested in tests/test_perf_check.py) ---------------

def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def median_report(runs):
    """Median of each numeric figure across runs, per state. A figure missing
    from some runs (for example a worker that did not exit) uses the others."""
    out = {}
    for state in dict.fromkeys(s for r in runs for s in r):
        keys = dict.fromkeys(k for r in runs for k in (r.get(state) or {}))
        figures = {}
        for key in keys:
            values = [r[state][key] for r in runs if _number((r.get(state) or {}).get(key))]
            if values:
                figures[key] = round(statistics.median(values), 3)
        out[state] = figures
    return out


def compare(figures, budgets, skip=()):
    """(over, not_measured): [(state, metric, value, limit)] for every figure
    over its budget, and [(state, metric)] for every set budget with no figure.
    A budget of null is not set yet; states in skip were not asked for."""
    over, missing = [], []
    for state, limits in budgets.items():
        if state in skip:
            continue
        for metric, limit in limits.items():
            if not _number(limit):
                continue
            value = figures.get(state, {}).get(metric)
            if not _number(value):
                missing.append((state, metric))
            elif value > limit:
                over.append((state, metric, value, limit))
    return over, missing


# The figures a budget can bound, and propose() proposes: the per-state
# figures, startup's workspace_s and the store step's figures (#111 C7, #430).
BUDGETED = ("cpu_percent_of_one_core", "private_mb", "startup_s", "workspace_s") + STORE_FIGURES


def worst_worker(runs):
    """The slowest worker unload across runs (a run whose worker never unloaded
    is recorded at the wait limit), or None when no run had a worker. Unloading
    is pass or fail, so one failed run must fail the whole check."""
    values = [r["worker"]["exit_after_last_use_s"] for r in runs
              if _number((r.get("worker") or {}).get("exit_after_last_use_s"))]
    return max(values) if values else None


def propose(figures, margin=0.2):
    """Budgets from a baseline: each budgeted figure plus the margin."""
    out = {}
    for state, metrics in figures.items():
        limits = {}
        for metric in BUDGETED:
            value = metrics.get(metric)
            if not _number(value):
                continue
            if metric == "private_mb":
                limits[metric] = math.ceil(value * (1 + margin))
            else:          # rounded up, never closer than 0.05 to the baseline
                limit = max(value * (1 + margin), value + 0.05)
                limits[metric] = math.ceil(round(limit * 100, 6)) / 100
        if limits:
            out[state] = limits
    return out


def machines_differ(now, earlier):
    """The COMPARABLE fields in which two machines differ (empty when the
    figures can be compared). A field missing from either counts as different."""
    now = now if isinstance(now, dict) else {}
    earlier = earlier if isinstance(earlier, dict) else {}
    return [f for f in COMPARABLE
            if now.get(f) is None or earlier.get(f) is None or now.get(f) != earlier.get(f)]


def _figures(report):
    """(state, metric) -> value for every numeric median and store figure."""
    out = {}
    for state, metrics in (report.get("median") or {}).items():
        for metric, value in (metrics or {}).items():
            if _number(value):
                out[(state, metric)] = value
    for metric, value in (report.get("store") or {}).items():
        if _number(value):
            out[("store", metric)] = value
    return out


def compare_reports(now, earlier):
    """The figures present in both reports, side by side, and both machines.
    Each figure is "not comparable" when the machines differ (machines_differ)."""
    differ = machines_differ(now.get("machine"), earlier.get("machine"))
    a, b = _figures(now), _figures(earlier)
    figures = []
    for key in sorted(set(a) & set(b)):
        row = {"state": key[0], "metric": key[1], "now": a[key], "earlier": b[key]}
        if differ:
            row["change_percent"] = NOT_COMPARABLE
        elif b[key]:
            row["change_percent"] = round((a[key] - b[key]) / b[key] * 100, 1)
        figures.append(row)
    return {"machines_differ_in": differ,
            "machine_now": now.get("machine"), "machine_earlier": earlier.get("machine"),
            "earlier_app": (earlier.get("environment") or {}).get("app"),
            "only_now": len(set(a) - set(b)), "compared": figures}


def store_problems(store):
    """What the store step left out (empty when it measured everything)."""
    if not isinstance(store, dict):
        return ["the store step did not run"]
    return [f for f in STORE_FIGURES if not _number(store.get(f))]


# -- the recording states (pure apart from PacedStream's thread; tested) --------------

def synthetic_blocks(rate=SYNTHETIC_RATE, block_s=SYNTHETIC_BLOCK_S, seconds=6, seed=7):
    """Speech-like synthetic audio, as 16-bit mono blocks shaped (frames, 1)
    like sounddevice's: noise under a syllable-rate envelope with short
    pauses, so the level display moves as it would for a voice. Made once and
    looped, so feeding it costs almost nothing per block."""
    import numpy as np
    frames = int(rate * block_s)
    count = max(1, int(round(seconds / block_s)))
    t = np.arange(frames * count) / rate
    envelope = np.abs(np.sin(2 * np.pi * 2.5 * t)) * (np.sin(2 * np.pi * 0.4 * t) > -0.6)
    noise = np.random.default_rng(seed).standard_normal(t.size)
    audio = np.clip(noise * envelope * 3000, -32768, 32767).astype(np.int16)
    return [audio[i * frames:(i + 1) * frames].reshape(-1, 1) for i in range(count)]


class PacedStream:
    """A stand-in for sounddevice's InputStream at the recorder's open_stream
    seam (workspace.recorder.Recording): from its own thread, as PortAudio
    does, it calls callback(block, frames, None, None) with the next synthetic
    block each time one block of real time has passed. The schedule is fixed
    from the start, so pacing does not drift; after a hold-up longer than
    max_late_s it carries on from now instead of sending a burst (the missed
    blocks are counted in `skipped`). finished() runs once when the stream
    stops, as sounddevice's finished_callback does. Opens no device."""

    def __init__(self, rate, callback, finished, block_s=SYNTHETIC_BLOCK_S, blocks=None,
                 clock=time.monotonic, wait=None, max_late_s=PACE_MAX_LATE_S):
        self.rate, self.block_s = int(rate), block_s
        self.frames = int(self.rate * block_s)
        self._callback, self._finished = callback, finished
        self._blocks = blocks if blocks is not None else synthetic_blocks(self.rate, block_s)
        self._clock, self._max_late = clock, max_late_s
        self._halt = threading.Event()
        self._wait = wait or self._halt.wait     # True once stopped
        self._ended = threading.Lock()
        self._finished_called = False
        self._thread = None
        self.delivered = self.skipped = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="perf-synthetic-input",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        start, n = self._clock(), 0
        try:
            while not self._halt.is_set():
                due = start + (n + 1) * self.block_s    # a block arrives once it is captured
                delay = due - self._clock()
                if delay > 0 and self._wait(delay):
                    break
                if self._halt.is_set():
                    break
                late = self._clock() - due
                if late > self._max_late:            # held up: no burst of old blocks
                    self.skipped += math.floor(round(late / self.block_s, 6))
                    start = self._clock() - (n + 1) * self.block_s
                self._callback(self._blocks[n % len(self._blocks)], self.frames, None, None)
                n += 1
                self.delivered += 1
        except Exception:
            pass                       # as PortAudio: a failing callback ends the stream
        finally:
            if not self._halt.is_set():
                self._end()

    def _end(self):
        with self._ended:
            if self._finished_called:
                return
            self._finished_called = True
        self._finished()

    def stop(self):
        self._halt.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(5)
        self._end()

    def close(self):
        if not self._halt.is_set():
            self.stop()


def consent_answerer(recording_title):
    """The tool's answer to the app's own questions (workspace.consent), as a
    function for make_api's confirm: yes to the recording question only, so
    the recording is started, and its consent record made, by the shipped
    bridge; no to anything else (a deletion grant or a discard is never
    approved). Returns (confirm, asked), asked listing the titles shown."""
    asked = []

    def confirm(question):
        asked.append(question.get("title"))
        return question.get("title") == recording_title
    return confirm, asked


def consent_outcome(required, record, asked, recording_title, valid):
    """How the recording was confirmed, checked against managed policy:
    "asked and recorded" when the confirmation is required (only the
    recording question was asked and the record is valid), "off by policy"
    when policy turns it off (nothing asked, no record). Raises otherwise."""
    if required:
        if not valid(record):
            raise RuntimeError("the recording has no valid consent record")
        if asked != [recording_title]:
            raise RuntimeError(f"unexpected questions: {asked}")
        return "asked and recorded"
    if record is not None or asked:
        raise RuntimeError(f"the confirmation is off by policy, yet asked: {asked}")
    return "off by policy"


def plan(leak_minutes, recording_allowed):
    """What the parent measures, in order, in one run: the idle states, the
    leak check (if asked for) before any recording, then the recording states
    unless managed policy turns microphone recording off."""
    out = list(IDLE_STATES)
    if leak_minutes:
        out.append("leak")
    if recording_allowed:
        out += RECORDING_STATES
    return out


def step_timeout(state):
    """How long the parent waits for the child to reach `state`: a state that
    may start a recording also gets the child's own waits for that."""
    if state in RECORDING_STATES:
        return STEP_TIMEOUT_S + sum(RECORD_WAITS_S.values())
    return STEP_TIMEOUT_S


def check_held(state, before, after, elapsed):
    """Raise Unmeasurable unless the recording open when `state` was reached
    (before: the child's {"state", "seconds"}) was still recording after its
    window, with its length grown by at least RECORDING_HELD_SHARE of the
    elapsed seconds. Returns the seconds recorded during the window."""
    if not before:
        raise Unmeasurable(f"no recording was open in {state}")
    if not after:
        raise Unmeasurable(f"the recording ended during {state}")
    if after.get("state") != "recording":
        raise Unmeasurable(f"the recording was {after.get('state')!r} after {state}")
    grown = after["seconds"] - before["seconds"]
    if grown < elapsed * RECORDING_HELD_SHARE:
        raise Unmeasurable(f"the recording grew {grown:.1f} s in {elapsed:.0f} s of {state}")
    return round(grown, 1)


class LostRecording(RuntimeError):
    """A recording state reached with no recording open: starting a second one
    would put its start (and the first one's saving) in the figures."""


def steps(state, recording):
    """What the child does, in order, to put the window in `state` ("quit" to
    end), given whether a recording is open. A recording is started only for
    the first recording state, before minimising (the window then keeps the
    page running instead of suspending it), and is discarded before any other
    state and before quitting, so no other figure includes it and nothing is
    imported. A later recording state with nothing recording is refused."""
    if state == "quit":
        return ["discard"] if recording else []
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    wanted = state in RECORDING_STATES
    if wanted and not recording and state != RECORDING_STATES[0]:
        raise LostRecording(f"nothing was recording when {state} began")
    out = ["restore"]
    if recording and not wanted:
        out.append("discard")
    out.append("show:settings" if state == "settings" else "show:meetings")
    if wanted and not recording:
        out.append("record")
    if state in ("minimised", "recording-minimised"):
        out.append("minimise")
    return out


# -- was the window drawn? (#292; pure, tested in tests/test_perf_check.py) -------------
#
# An overnight release run measured recording with the window visible at about
# 19% of one core; with the screen on it was about 44% (#216), most of it the
# level display drawn on every frame. The display had most likely turned itself
# off, so the browser drew few or no frames. So each window measured in a
# visible state needs the display on, the session unlocked and the page drawing.

# Put into the tool's own window once the page has started (the child, never
# the app). It counts two things, and adds no frame of its own while counting:
#   - the page's frames: the distinct animation frames in which the page's own
#     requestAnimationFrame callbacks ran (wrapped; one count per frame);
#   - the level display's draws: the animation frames in which the level
#     display's canvas (#rec-canvas) was cleared for drawing (clearRect wrapped;
#     one count per frame). The meter clears its canvas each time it draws and
#     only then, so this counts frames it drew, not callbacks it skipped (#294),
#     and the longest stretch between two draws.
# read() returns the counts and the longest undrawn stretch since the last read
# (to the read itself), then starts the next stretch, and asks for one frame of
# its own ("ping") so the next read shows whether any frame was drawn in
# between, even in a view that draws nothing. Only counts leave the page.
PAGE_COUNTER_JS = """(() => {
  if (window.__perfCheck) return true;
  const raf = window.requestAnimationFrame.bind(window);
  const ctx = CanvasRenderingContext2D.prototype, clear = ctx.clearRect;
  const p = { frames: 0, frame: -1, draws: 0, drawn: -1, last: performance.now(), gap: 0,
              ping: 0 };
  window.requestAnimationFrame = (cb) => raf((t) => {
    if (t !== p.frame) { p.frame = t; p.frames += 1; }
    return cb(t);
  });
  ctx.clearRect = function (...args) {
    if (this.canvas && this.canvas.id === "rec-canvas" && p.drawn !== p.frame) {
      const now = performance.now();
      p.drawn = p.frame; p.draws += 1;
      p.gap = Math.max(p.gap, now - p.last); p.last = now;
    }
    return clear.apply(this, args);
  };
  p.read = () => {
    const now = performance.now(), undrawn = Math.max(p.gap, now - p.last);
    p.gap = 0; p.last = now;
    raf(() => { p.ping = performance.now(); });
    return JSON.stringify({ visibility: document.visibilityState, frames: p.frames,
                            draws: p.draws, undrawn_ms: undrawn, now: now, ping: p.ping });
  };
  Object.defineProperty(window, "__perfCheck", { value: p });
  return true;
})()"""
PAGE_READ_JS = "window.__perfCheck ? window.__perfCheck.read() : null"
PAGE_COUNTS = ("frames", "draws", "undrawn_ms", "now", "ping")


def page_reading(raw):
    """PAGE_READ_JS's answer as {"visibility", "frames", "draws", "undrawn_ms",
    "now", "ping"}, or None when it is not that (no counter, or the page did
    not answer). The answer is JSON text; one evaluate_js has already parsed is
    taken as well."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("visibility"), str):
        return None
    if not all(_number(data.get(k)) and data[k] >= 0 for k in PAGE_COUNTS):
        return None
    return {"visibility": data["visibility"][:20], **{k: data[k] for k in PAGE_COUNTS}}


def display_states(events, t0, t1):
    """The display's power states during [t0, t1], in order, from (time, state)
    events in time order: the state it was in at t0, then any it changed to
    before t1. None when no state was known at t0."""
    at_start = [state for t, state in events if t <= t0]
    if not at_start:
        return None
    return list(dict.fromkeys([at_start[-1]] + [s for t, s in events if t0 < t <= t1]))


def refresh_rate(hz):
    """A display refresh rate as Windows reports it, or None: 0 and 1 mean
    "the hardware's default", not a rate (DEVMODE)."""
    return hz if evidence._count(hz) and hz > 1 else None


def min_meter_draws(refresh_hz):
    """The fewest level-display draws a second that show it was drawn: half
    of what it draws on this display (every frame, at most about METER_MAX_HZ;
    #294). A display whose rate is not known is taken as METER_MAX_HZ."""
    hz = refresh_rate(refresh_hz) or METER_MAX_HZ
    return round(min(METER_MAX_HZ, hz) / 2, 1)


def session_problems(session):
    """Why the session could not show the window ({"locked", "screensaver"},
    or None when unread); [] when it could."""
    if not isinstance(session, dict):
        return ["the session's state could not be read"]
    return ([] if session.get("locked") is False else ["the session was locked"]) + (
        [] if session.get("screensaver") is False else ["the screen saver was running"])


def drawing_report(state, display, sessions, page0, page1, refresh_hz=None):
    """Whether a window measured in a visible state was drawn as a person
    would see it, and why not. display: display_states() for the window;
    sessions: the session's state ({"locked", "screensaver"}, or None when
    unread) at its start and end; page0, page1: page_reading() then;
    refresh_hz: the display's refresh rate during the run (None if not known).
    Returns {"drawn", "problems", "display", "page_visibility",
    "page_frames_per_s", "meter_draws_per_s", "min_meter_draws_per_s",
    "longest_undrawn_s"}; the last two only for a state the level display
    draws in (DRAWING_STATES), else None."""
    problems = []
    if display is None:
        problems.append("the display's power state was not known")
    else:
        problems += [f"the display was {s}" for s in display if s not in DRAWN_DISPLAY]
    for session in sessions:
        problems += session_problems(session)
    frames = draws = least = longest = None
    meter = state in DRAWING_STATES
    if meter:
        least = min_meter_draws(refresh_hz)
    if not page0 or not page1:
        problems.append("the page did not report its frames")
    else:
        if page0["visibility"] != "visible" or page1["visibility"] != "visible":
            problems.append(f"the page was {page0['visibility']}, then {page1['visibility']}")
        if page1["ping"] <= page0["now"]:
            problems.append("the page drew no frame")
        seconds = (page1["now"] - page0["now"]) / 1000
        if seconds > 0:
            frames = round((page1["frames"] - page0["frames"]) / seconds, 1)
            draws = round((page1["draws"] - page0["draws"]) / seconds, 1)
        if meter:
            longest = round(page1["undrawn_ms"] / 1000, 1)
            if draws is None or draws < least:
                problems.append(f"the level display drew {draws} times a second "
                                f"(at least {least} expected)")
            if longest > MAX_UNDRAWN_S:
                problems.append(f"the level display was not drawn for {longest} s at a "
                                f"stretch (at most {MAX_UNDRAWN_S} s allowed)")
    return {"drawn": not problems, "problems": list(dict.fromkeys(problems)),
            "display": display,
            "page_visibility": [p["visibility"] if p else None for p in (page0, page1)],
            "page_frames_per_s": frames, "meter_draws_per_s": draws,
            "min_meter_draws_per_s": least, "longest_undrawn_s": longest}


# -- measuring from outside (Windows only)--------------------------------------------

def _win():
    import ctypes
    from ctypes import wintypes
    return ctypes, wintypes


def processes():
    """{pid: (parent pid, exe name)} for every process, from a Toolhelp snapshot."""
    ctypes, wintypes = _win()

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)                 # TH32CS_SNAPPROCESS
    out = {}
    try:
        e = Entry()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def tree(root_pid):
    """root_pid and all its descendants: [(pid, exe name)]."""
    table = processes()
    kids = {}
    for pid, (ppid, _) in table.items():
        kids.setdefault(ppid, []).append(pid)
    seen, todo = [], [root_pid]
    while todo:
        pid = todo.pop()
        if pid in table and pid not in seen:
            seen.append(pid)
            todo += kids.get(pid, [])
    return [(pid, table[pid][1]) for pid in seen]


def sample(pid):
    """CPU seconds and memory of one process, or None if it is gone."""
    ctypes, wintypes = _win()

    class Counters(ctypes.Structure):                          # PROCESS_MEMORY_COUNTERS_EX2
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage", "PrivateWorkingSetSize")
        ] + [("SharedCommitUsage", ctypes.c_uint64)]
    k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000 | 0x0010, False, pid)           # QUERY_LIMITED | VM_READ
    if not h:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
            return None
        secs = sum(((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7 for t in times[2:])
        c = Counters()
        for size in (ctypes.sizeof(Counters), ctypes.sizeof(Counters) - 16):  # EX2, then EX
            c.cb = size
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(c), size):
                break
        else:
            return None              # no memory figures: retried, then unmeasurable
        return {"cpu_s": secs, "private": c.PrivateUsage, "working_set": c.WorkingSetSize,
                "private_ws": c.PrivateWorkingSetSize if c.cb == ctypes.sizeof(Counters)
                else None}
    finally:
        k32.CloseHandle(h)


class Job:
    """A Job Object holding the child, for CPU accounting and kill-on-close."""

    def __init__(self, process):
        ctypes, wintypes = _win()
        self.ctypes, self.k32 = ctypes, ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.handle = self.k32.CreateJobObjectW(None, None)

        class Limits(ctypes.Structure):          # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
            _fields_ = [("a", ctypes.c_int64 * 2), ("LimitFlags", wintypes.DWORD),
                        ("b", ctypes.c_size_t * 2), ("c", wintypes.DWORD),
                        ("d", ctypes.c_size_t), ("e", wintypes.DWORD), ("f", wintypes.DWORD),
                        ("io", ctypes.c_uint64 * 6), ("g", ctypes.c_size_t * 4)]
        limits = Limits()
        limits.LimitFlags = 0x2000                              # KILL_ON_JOB_CLOSE
        if not self.k32.SetInformationJobObject(self.handle, 9, ctypes.byref(limits),
                                                ctypes.sizeof(limits)):
            raise OSError(ctypes.get_last_error(), "could not set kill-on-close on the job")
        if not self.k32.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError(ctypes.get_last_error(), "could not put the window in a job")

    def cpu_seconds(self):
        ctypes = self.ctypes

        class Info(ctypes.Structure):            # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
            _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                        ("ThisPeriodTotalUserTime", ctypes.c_int64),
                        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                        ("TotalPageFaultCount", ctypes.c_uint32),
                        ("TotalProcesses", ctypes.c_uint32), ("ActiveProcesses", ctypes.c_uint32),
                        ("TotalTerminatedProcesses", ctypes.c_uint32)]
        info = Info()
        self.k32.QueryInformationJobObject(self.handle, 1, ctypes.byref(info),
                                           ctypes.sizeof(info), None)
        return (info.TotalUserTime + info.TotalKernelTime) / 1e7, info.ActiveProcesses

    def close(self):
        self.k32.CloseHandle(self.handle)                       # kills anything left


class Unmeasurable(Exception):
    """The window cannot be measured as the standard requires (exit code 2)."""


def snapshot(pid, tries=3):
    """The tree and a sample of every process in it. A process that exits
    between the two is retried; one that cannot be opened is an error, because
    leaving it out would under-report memory."""
    for _ in range(tries):
        procs = tree(pid)
        samples = {p: sample(p) for p, _ in procs}
        if all(samples.values()):
            return procs, samples
        time.sleep(0.2)
    missing = [name for p, name in procs if not samples[p]]
    raise Unmeasurable(f"could not read {len(missing)} process(es): {', '.join(missing)}")


def measure(job, pid, settle, window):
    """Wait settle seconds, then measure CPU over window seconds and memory at its end."""
    time.sleep(settle)
    job0, _ = job.cpu_seconds()
    _, before = snapshot(pid)
    t0 = time.monotonic()
    time.sleep(window)
    job1, active = job.cpu_seconds()
    procs, after = snapshot(pid)
    elapsed = time.monotonic() - t0
    if active < len(procs):
        raise Unmeasurable(f"{len(procs) - active} process(es) are outside the measuring job")
    tree_cpu = sum(a["cpu_s"] - (before.get(p) or {}).get("cpu_s", 0) for p, a in after.items())
    live = list(after.values())
    names = sorted(name for _, name in procs)
    mb = lambda key: round(sum(a.get(key) or 0 for a in live) / 2**20)  # noqa: E731
    return {
        "cpu_percent_of_one_core": round((job1 - job0) / elapsed * 100, 3),
        "cpu_percent_tree": round(tree_cpu / elapsed * 100, 3),
        "private_mb": mb("private"),
        "private_working_set_mb": mb("private_ws") if all(a.get("private_ws") is not None
                                                          for a in live) else None,
        "working_set_mb_reference_only": mb("working_set"),
        "processes": len(procs), "processes_in_job": active,
        "python_processes": sum(1 for n in names if n.startswith("python")),
        "process_names": names,
    }


class DisplayEvents:
    """The display's power state over time, as (time.monotonic(), state)
    events; record() may be called from any thread."""

    def __init__(self, clock=time.monotonic):
        self.events, self._clock = [], clock
        self._lock, self.known = threading.Lock(), threading.Event()

    def record(self, value):
        with self._lock:
            self.events.append((self._clock(), DISPLAY_STATES.get(value, "unknown")))
        self.known.set()

    def states(self, t0, t1):
        with self._lock:
            events = list(self.events)
        return display_states(events, t0, t1)

    def current(self):
        """The display's state now (the last one recorded), or None."""
        with self._lock:
            return self.events[-1][1] if self.events else None


GUID_SESSION_DISPLAY_STATUS = "2b84c20e-ad23-4ddf-93db-05ffbd7efca5"
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
PBT_POWERSETTINGCHANGE, DEVICE_NOTIFY_CALLBACK = 0x8013, 2


class WindowsDisplay(DisplayEvents):
    """The run's display (#292). start() asks Windows to keep the display on
    and the machine awake while this thread runs (SetThreadExecutionState with
    ES_CONTINUOUS; stop() releases it, and Windows does when the process ends;
    no power setting or plan is changed), and records the display's state for
    the user's session from Windows' notifications (GUID_SESSION_DISPLAY_STATUS:
    the state when asked, then each change; no window is opened). session()
    reads whether the session is locked or a screen saver is running. Reads
    give None when they cannot be made, never an exception."""

    def __init__(self):
        super().__init__()
        self._handle = self._keep = None
        self.kept_awake = False

    def start(self):
        """{"kept_awake", "power_state_known"} once Windows has answered (5 s at most)."""
        ctypes, wintypes = _win()
        k32 = ctypes.WinDLL("kernel32")
        k32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        k32.SetThreadExecutionState.restype = ctypes.c_uint32
        self.kept_awake = bool(k32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED))
        try:
            self._register(ctypes, wintypes)
        except (OSError, AttributeError):
            self._handle = None
        return {"kept_awake": self.kept_awake,
                "power_state_known": self._handle is not None and self.known.wait(5)}

    def _register(self, ctypes, wintypes):
        import uuid

        class Guid(ctypes.Structure):
            _fields_ = [("bytes", ctypes.c_ubyte * 16)]
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                           ctypes.c_void_p)

        class Subscribe(ctypes.Structure):       # DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS
            _fields_ = [("Callback", callback_type), ("Context", ctypes.c_void_p)]

        def changed(context, kind, setting):
            # POWERBROADCAST_SETTING: the setting's GUID, then a DWORD data
            # length (offset 16), then the data (offset 20): a DWORD here.
            try:
                if kind == PBT_POWERSETTINGCHANGE and setting:
                    if ctypes.c_uint32.from_address(setting + 16).value >= 4:
                        self.record(ctypes.c_uint32.from_address(setting + 20).value)
            except Exception:
                pass
            return 0
        guid = Guid((ctypes.c_ubyte * 16)(*uuid.UUID(GUID_SESSION_DISPLAY_STATUS).bytes_le))
        function = callback_type(changed)
        params = Subscribe(function, None)
        powrprof = ctypes.WinDLL("powrprof")
        powrprof.PowerSettingRegisterNotification.argtypes = [
            ctypes.POINTER(Guid), wintypes.DWORD, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        powrprof.PowerSettingRegisterNotification.restype = wintypes.DWORD
        handle = ctypes.c_void_p()
        self._keep = (guid, function, params, powrprof)     # alive while registered
        if powrprof.PowerSettingRegisterNotification(ctypes.byref(guid), DEVICE_NOTIFY_CALLBACK,
                                                     ctypes.byref(params),
                                                     ctypes.byref(handle)) == 0 and handle.value:
            self._handle = handle

    def stop(self):
        """Lets the display go first, then stops watching it, each on its own:
        a failure to stop watching cannot keep the display on. Windows
        releases both when the process ends in any case."""
        try:
            ctypes, _ = _win()
            k32 = ctypes.WinDLL("kernel32")
            k32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
            k32.SetThreadExecutionState(ES_CONTINUOUS)         # back to normal
        except (OSError, AttributeError):
            pass
        try:
            if self._handle is not None:
                ctypes, _ = _win()
                powrprof = self._keep[3]       # _keep stays: the callback may still be held
                powrprof.PowerSettingUnregisterNotification.argtypes = [ctypes.c_void_p]
                powrprof.PowerSettingUnregisterNotification(self._handle)
                self._handle = None
        except (OSError, AttributeError):
            pass

    def session(self):
        """{"locked": the input desktop is not the user's, "screensaver": running}."""
        try:
            ctypes, wintypes = _win()
            user32 = ctypes.WinDLL("user32")
            user32.OpenInputDesktop.restype = wintypes.HANDLE
            user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            user32.CloseDesktop.argtypes = [wintypes.HANDLE]
            user32.GetUserObjectInformationW.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD)]
            locked = True              # the lock screen's desktop cannot be opened
            desktop = user32.OpenInputDesktop(0, False, 0x0001)      # DESKTOP_READOBJECTS
            if desktop:
                try:
                    name = ctypes.create_unicode_buffer(64)
                    if user32.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name),
                                                        ctypes.byref(wintypes.DWORD())):
                        locked = name.value.lower() != "default"      # UOI_NAME
                finally:
                    user32.CloseDesktop(desktop)
            running = wintypes.BOOL()
            if not user32.SystemParametersInfoW(0x0072, 0, ctypes.byref(running), 0):
                return None                                  # SPI_GETSCREENSAVERRUNNING
            return {"locked": locked, "screensaver": bool(running.value)}
        except (OSError, AttributeError):
            return None


def display_watch():
    """The run's display (tests put a fake in its place)."""
    return WindowsDisplay()


def refresh_now():
    """The primary display's refresh rate now (refresh_rate()), or None when
    it cannot be read (tests put a fake in its place)."""
    try:
        return refresh_rate(WindowsProbe().display_refresh_hz())
    except Exception:
        return None


# -- the parent: runs the child once per run and measures it ---------------------------

# -- the machine it ran on (pure apart from WindowsProbe; tested with a fake) ------------
#
# No named reference machine (owner decision, 18 Oct 2026, #121): a release is
# measured on whichever Windows machine prepares it, and the report records
# that machine's specification (tools/evidence.py, PERF_MACHINE, which a
# release's evidence must hold in full). Specification only: never the
# computer's or the user's name, an account, a serial number or a path
# (AGENTS.md P3). Every registry value and CIM property read is named here.

CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
WINDOWS_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
WEBVIEW2_KEYS = tuple(               # the WebView2 Runtime's version ("pv"), per install
    (hive, rf"{prefix}\Microsoft\EdgeUpdate\Clients\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}")
    for hive, prefix in (("HKLM", r"SOFTWARE\WOW6432Node"), ("HKLM", "SOFTWARE"),
                         ("HKCU", "Software")))
REGISTRY_READS = {("HKLM", CPU_KEY): ("ProcessorNameString",),
                  ("HKLM", WINDOWS_KEY): ("CurrentMajorVersionNumber",
                                          "CurrentMinorVersionNumber", "CurrentBuild", "UBR",
                                          "DisplayVersion", "InstallationType"),
                  **{place: ("pv",) for place in WEBVIEW2_KEYS}}
CIM_READS = {"Win32_Processor": ("NumberOfCores",),
             "Win32_VideoController": ("Name", "DriverVersion")}
NAME_LIMIT = 80                       # a GPU or processor name, as workspace/gpu.py keeps
BATTERY_NONE, BATTERY_UNKNOWN = 128, 255     # SYSTEM_POWER_STATUS.BatteryFlag


def _short(value):
    """A trimmed name or version, or None for anything that is not text."""
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:NAME_LIMIT]


def _power_plan(output):
    """The active plan's name from "powercfg /getactivescheme", whose last
    bracketed words name it (in the system's language), or None."""
    line = (output or "").strip()
    if not line.endswith(")") or "(" not in line:
        return None
    return _short(line[line.rindex("(") + 1:-1])


def machine_spec(probe):
    """The specification of the machine, from a probe that reads the
    registry, CIM and a few system calls (WindowsProbe; tests pass a fake).
    A field that could not be read is None (gpus too, when CIM could not be
    asked), so the report says what is missing instead of guessing."""
    def reg(hive, key, value):
        if value not in REGISTRY_READS.get((hive, key), ()):
            raise ValueError(f"{hive} {key} {value} is not a listed read")
        return probe.registry(hive, key, value)

    cim = probe.cim()
    cim = cim if isinstance(cim, dict) else {}
    processors = [p for p in cim.get("Win32_Processor") or [] if isinstance(p, dict)]
    cores = [p.get("NumberOfCores") for p in processors]
    adapters = cim.get("Win32_VideoController")
    gpus = None
    if isinstance(adapters, list):
        gpus = sorted(({"name": _short(a.get("Name")), "driver": _short(a.get("DriverVersion"))}
                       for a in adapters if isinstance(a, dict) and _short(a.get("Name"))),
                      key=lambda g: (g["name"], g["driver"] or ""))

    build = [reg("HKLM", WINDOWS_KEY, v) for v in (
        "CurrentMajorVersionNumber", "CurrentMinorVersionNumber", "CurrentBuild", "UBR")]
    os_build = windows = None
    if all((isinstance(part, int) and not isinstance(part, bool) and part >= 0)
           or (isinstance(part, str) and part.isascii() and part.isdigit()) for part in build):
        os_build = ".".join(str(int(part)) for part in build)
        # The build tells Windows 11 from 10 (ProductName still says "Windows 10"
        # on 11); InstallationType ("Client", "Server", "Server Core") tells a
        # server, whose builds overlap the client's.
        server = _short(reg("HKLM", WINDOWS_KEY, "InstallationType")) or ""
        family = ("Windows Server" if server.startswith("Server")
                  else "Windows 11" if int(build[2]) >= 22000 else "Windows 10")
        release = _short(reg("HKLM", WINDOWS_KEY, "DisplayVersion"))
        windows = f"{family} {release}" if release else family

    memory = probe.memory_bytes()
    status = probe.power_status()           # (ACLineStatus, BatteryFlag) or None
    line, flag = status if isinstance(status, tuple) and len(status) == 2 else (None, None)
    webview2 = None
    for hive, key in WEBVIEW2_KEYS:
        webview2 = _short(reg(hive, key, "pv"))
        if webview2 and webview2 != "0.0.0.0":   # "0.0.0.0" is left by an uninstall
            break
        webview2 = None
    logical = probe.logical_processors()
    hz = probe.display_refresh_hz()          # drawing cost scales with it (#292)
    return {
        "cpu": _short(reg("HKLM", CPU_KEY, "ProcessorNameString")),
        "cores": sum(cores) if cores and all(evidence._count(c) for c in cores) else None,
        "logical_processors": logical if evidence._count(logical) else None,
        "memory_gb": round(memory / 2**30, 1) if evidence._count(memory) else None,
        "windows": windows,
        "os_build": os_build,
        "power_plan": _power_plan(probe.power_plan()),
        "on_mains_power": {0: False, 1: True}.get(line),
        "battery": None if flag in (None, BATTERY_UNKNOWN) else flag != BATTERY_NONE,
        "gpus": gpus,
        "display_refresh_hz": refresh_rate(hz),
        "webview2": webview2,
    }


def _system_program(*parts):
    """A program in the Windows system folder, by absolute path only (never
    found through PATH or the current folder), or None."""
    ctypes, _ = _win()
    buf = ctypes.create_unicode_buffer(260)
    n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)
    path = os.path.join(buf.value, *parts) if 0 < n < 260 else None
    return path if path and os.path.isfile(path) else None


def cim_script():
    """The fixed PowerShell command WindowsProbe.cim runs. It must work in
    ConstrainedLanguage mode (WDAC or AppLocker script enforcement on a managed
    laptop), which allows Get-CimInstance, @{} and ConvertTo-Json but refuses
    setting a .NET property: so the console's UTF-8 output encoding is only
    tried. Without it a non-ASCII adapter name may lose a character, since the
    output is decoded with errors="replace"; the query itself still answers."""
    query = "; ".join(f"$o['{cls}'] = @(Get-CimInstance {cls} | Select-Object "
                      f"{','.join(props)})" for cls, props in CIM_READS.items())
    return ("$ErrorActionPreference = 'Stop'; "
            "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}; $o = @{}; "
            f"{query}; $o | ConvertTo-Json -Depth 3 -Compress")


class WindowsProbe:
    """What machine_spec reads, from this Windows machine. Each read gives
    None when it cannot be made, never an exception."""

    def registry(self, hive, key, value):
        import winreg
        root = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}[hive]
        try:
            with winreg.OpenKey(root, key) as k:
                return winreg.QueryValueEx(k, value)[0]
        except OSError:
            return None

    def cim(self):
        """{class: [{property: value}]} for CIM_READS, from Windows PowerShell's
        Get-CimInstance (local WMI; no network), or None. Started by full path
        with a minimal environment (#237); a missing powershell.exe gives None."""
        try:
            r = windows_powershell.run(cim_script(), text=False, timeout=120,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                return None
            data = json.loads(r.stdout.decode("utf-8-sig", errors="replace"))
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        # Only the listed properties, whatever else came back.
        return {cls: [{p: row.get(p) for p in CIM_READS[cls]} for row in data.get(cls) or []
                      if isinstance(row, dict)] if isinstance(data.get(cls), list) else None
                for cls in CIM_READS}

    def memory_bytes(self):
        ctypes, wintypes = _win()

        class Mem(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_uint64)] + [(f"x{i}", ctypes.c_uint64)
                                                              for i in range(6)]
        m = Mem()
        m.dwLength = ctypes.sizeof(m)
        return m.ullTotalPhys if ctypes.windll.kernel32.GlobalMemoryStatusEx(
            ctypes.byref(m)) else None

    def power_status(self):
        ctypes, wintypes = _win()

        class Power(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte),
                        ("a", wintypes.DWORD), ("b", wintypes.DWORD)]
        p = Power()
        return (p.ACLineStatus, p.BatteryFlag) if ctypes.windll.kernel32.GetSystemPowerStatus(
            ctypes.byref(p)) else None

    def power_plan(self):
        exe = _system_program("powercfg.exe")
        if exe is None:
            return None
        try:
            r = subprocess.run([exe, "/getactivescheme"], capture_output=True, text=True,
                               timeout=60, creationflags=getattr(subprocess,
                                                                 "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    def logical_processors(self):
        return os.cpu_count()

    def display_refresh_hz(self):
        """The primary display's current refresh rate (EnumDisplaySettingsW), or None."""
        ctypes, wintypes = _win()

        class DevMode(ctypes.Structure):                       # DEVMODEW, display fields
            _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", wintypes.WORD),
                        ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                        ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                        ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
                        ("dmDisplayOrientation", wintypes.DWORD),
                        ("dmDisplayFixedOutput", wintypes.DWORD), ("dmColor", ctypes.c_short),
                        ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                        ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                        ("dmFormName", ctypes.c_wchar * 32), ("dmLogPixels", wintypes.WORD),
                        ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                        ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                        ("dmDisplayFrequency", wintypes.DWORD)] + [
                (f"x{i}", wintypes.DWORD) for i in range(8)]
        try:
            user32 = ctypes.WinDLL("user32")
            user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                    ctypes.c_void_p]
            mode = DevMode()
            mode.dmSize = ctypes.sizeof(mode)
            if not user32.EnumDisplaySettingsW(None, 0xFFFFFFFF, ctypes.byref(mode)):
                return None                                  # ENUM_CURRENT_SETTINGS
            return int(mode.dmDisplayFrequency)
        except (OSError, AttributeError):
            return None


def environment(args):
    """When and with what the run was made. The machine's specification is
    returned under "machine", which parent() moves to the report's top level."""
    env = {"when": time.strftime("%Y-%m-%d %H:%M"), "python": platform.python_version(),
           "machine": machine_spec(WindowsProbe())}
    app = Path(args.app) if args.app else ROOT
    version = app.parent / "version.json"
    if version.exists():
        env["app"] = json.loads(version.read_text(encoding="utf-8"))
    else:
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True)
        version = ROOT / "VERSION"            # as workspace/__main__.py names a source run
        name = version.read_text(encoding="utf-8-sig").strip() + "+source" \
            if version.exists() else "source"
        env["app"] = {"version": name, "commit": r.stdout.strip() or None}
    return env


def child_command(args, workdir):
    """The base interpreter, not a venv's launcher: the launcher starts the real
    interpreter as a second process, which could escape the job if it started
    before the job is assigned. __PYVENV_LAUNCHER__ keeps the venv's packages,
    as multiprocessing does."""
    env = dict(os.environ)
    exe = sys.executable
    base = getattr(sys, "_base_executable", exe)
    if os.path.normcase(base) != os.path.normcase(exe):
        env["__PYVENV_LAUNCHER__"] = exe
        exe = base
    # -B: never write bytecode into the package being measured. Release
    # evidence (--zip) starts the window as the Start menu shortcut does.
    flags = ["-I", "-B", "-X", "disable-remote-debug"] if args.zip else ["-B"]
    command = [exe, *flags, str(Path(__file__).resolve()), "--child", "--workdir", str(workdir)]
    if args.app:
        command += ["--app", args.app]
    return command, env


def one_run(args, settle, window, leak_minutes=0, display=None, refresh_hz=None):
    """One run of the window through every state. display: the run's
    WindowsDisplay (parent()); without one, no visible state counts as drawn.
    refresh_hz: the display's refresh rate (parent()), for the level display's
    expected draws (min_meter_draws())."""
    workdir = Path(tempfile.mkdtemp(prefix="unminute-perf-"))
    command, env = child_command(args, workdir)
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                            text=True, encoding="utf-8", bufsize=1)
    job = None
    lines = queue.Queue()

    def pump():                      # read in a thread, so every wait has a deadline
        for line in proc.stdout:
            if line.startswith(PREFIX):
                lines.put(json.loads(line[len(PREFIX):]))
        lines.put(None)

    def read(timeout):
        try:
            msg = lines.get(timeout=timeout)
        except queue.Empty:
            raise Unmeasurable(f"the window did not answer within {timeout} s") from None
        if msg is None:
            raise Unmeasurable(f"the window process ended (exit {proc.wait()})")
        if msg.get("error"):
            raise Unmeasurable(f"the window reported: {msg['error']}")
        return msg

    def say(command):
        proc.stdin.write(command + "\n")
        proc.stdin.flush()

    def leak():                      # in the Meetings view, before any recording (plan())
        say("meetings")
        read(STEP_TIMEOUT_S)
        print(f"  holding the Meetings view for {leak_minutes} minutes (leak check)",
              flush=True)
        time.sleep(settle)
        _, first = snapshot(proc.pid)
        time.sleep(leak_minutes * 60)
        _, last = snapshot(proc.pid)
        growth = (sum(s["private"] or 0 for s in last.values())
                  - sum(s["private"] or 0 for s in first.values())) / 2**20
        out = {"minutes": leak_minutes, "private_growth_mb_raw": round(growth, 1)}
        if leak_minutes >= LEAK_BUDGET_MINUTES:          # the budget is per 30 minutes
            out["private_growth_mb"] = round(growth, 1)
        return out

    def look():                      # the session and the page's frames, now (#292)
        say("probe")
        page = read(STEP_TIMEOUT_S).get("page")
        return {"session": display.session() if display else None, "page": page}

    result = {}
    try:
        try:
            job = Job(proc)
        except OSError as exc:
            raise Unmeasurable(f"could not set up the measuring job: {exc}") from exc
        threading.Thread(target=pump, daemon=True).start()
        ready = read(READY_TIMEOUT_S)
        # Every start-up figure is required except workspace_s, which a child
        # of an older package does not send (review of #335).
        result["startup"] = {k: ready[k] for k in ("imports_s", "window_to_interactive_s",
                                                   "startup_s")}
        if "workspace_s" in ready:
            result["startup"]["workspace_s"] = ready["workspace_s"]
        # Idle means after the worker has unloaded. Waiting for it also measures
        # the standard's "gone within its idle time plus 5 s" (section 3). A
        # worker that does not unload is recorded at the wait limit, so it fails
        # its budget rather than dropping out of the figures.
        worker = ready.get("worker_pid")
        if worker:
            print("  waiting for the worker to unload", flush=True)
            deadline = time.time() + WORKER_WAIT_S
            while time.time() < deadline and any(p == worker for p, _ in tree(proc.pid)):
                time.sleep(0.5)
            gone = not any(p == worker for p, _ in tree(proc.pid))
            result["worker"] = {"exited": gone, "exit_after_last_use_s": round(
                time.time() - ready["worker_last_used"], 1) if gone else WORKER_WAIT_S}
        allowed = ready.get("recording_allowed") is True
        if not allowed:                  # managed policy: microphone recording off
            result["not_measured_by_policy"] = list(RECORDING_STATES)
            print(f"  {', '.join(RECORDING_STATES)}: {POLICY_SKIPPED}", flush=True)
        for state in plan(leak_minutes, allowed):
            if state == "leak":
                result["leak"] = leak()
                continue
            say(state)
            reached = read(step_timeout(state))
            if reached.get("state") != state:
                raise Unmeasurable(f"the window did not reach {state}")
            t0 = time.monotonic()
            visible = state in VISIBLE_STATES
            time.sleep(settle)
            if visible:                      # read just before the window, outside it
                start, at0 = look(), time.monotonic()
            result[state] = measure(job, proc.pid, 0, window)
            if visible:                      # and just after it
                at1, end = time.monotonic(), look()
                drawing = drawing_report(
                    state, display.states(at0, at1) if display else None,
                    [start["session"], end["session"]], start["page"], end["page"],
                    refresh_hz)
                result[state]["drawing"] = drawing
                if not drawing["drawn"]:
                    raise Unmeasurable(
                        f"{state} was not drawn on screen ({'; '.join(drawing['problems'])}): "
                        "keep the session unlocked and the window in view")
            if state in RECORDING_STATES:    # still the same recording, still recording
                say("check")
                held = read(STEP_TIMEOUT_S)
                result[state]["recorded_s"] = check_held(
                    state, reached.get("recording"), held.get("recording"),
                    time.monotonic() - t0)
                if reached.get("consent"):   # sent by the state that started it
                    result["recording_consent"] = reached["consent"]
            print(f"  {state}: {result[state]['cpu_percent_of_one_core']}% of one core, "
                  f"{result[state]['private_mb']} MB private", flush=True)
        say("quit")
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
        if job is not None:
            job.close()                  # kills anything left, WebView2 included
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            pass
        for _ in range(10):              # files can stay locked for a moment
            shutil.rmtree(workdir, ignore_errors=True)
            if not workdir.exists():
                break
            time.sleep(0.5)
    return result


def parent(args):
    settle, window, runs = (20, 60, 1) if args.quick else (args.settle, args.window, args.runs)
    report = {"tool": "tools/perf_check.py", "mode": "quick" if args.quick else "release",
              "settle_s": settle, "window_s": window, "runs": runs,
              "leak_minutes": args.leak_minutes,
              "recording_input": {"synthetic": True, "rate": SYNTHETIC_RATE,
                                  "block_s": SYNTHETIC_BLOCK_S, "paced": "real time"}}
    results = []
    display = None
    try:
        # Inside the guard: a probe that fails (a damaged version.json, a
        # missing system tool) is "not measured", not a traceback.
        try:
            env = environment(args)
        except Exception as exc:
            raise RuntimeError(
                f"the environment probe failed ({type(exc).__name__}: {exc})") from exc
        report["machine"] = env.pop("machine", None)
        report["environment"] = env
        # Release evidence needs the machine in full (tools/evidence.py): say
        # so now, not after two hours of measuring.
        missing = evidence.machine_problems(report["machine"])
        if getattr(args, "zip", None) and missing:
            raise RuntimeError(f"the machine's specification could not be read in full "
                               f"({', '.join(missing)}), and release evidence records it")
        # The display stays on for the run, and its state is watched, so each
        # visible state is measured while drawn (#292).
        display = display_watch()
        report["display"] = display.start()
        if not report["display"]["power_state_known"]:
            raise RuntimeError("the display's power state could not be read, so the visible "
                               "states could not be shown to be drawn")
        # Not drawn from the start: stop now, not after the first window.
        at_start = report["display"]["at_start"] = display.current()
        if at_start not in DRAWN_DISPLAY:
            raise RuntimeError(f"the display was {at_start or 'in an unknown state'} as the run "
                               "started, so the visible states could not be drawn: turn the "
                               "display on and start again")
        problems = session_problems(display.session())
        if problems:
            raise RuntimeError(f"{'; '.join(problems)} as the run started, so the visible "
                               "states could not be drawn: unlock the session and start again")
        # The refresh rate once the display is kept on (dynamic refresh can
        # lower it while idle): the machine block takes it, and the display
        # block keeps the rate read before if the two differ.
        machine = report["machine"] if isinstance(report["machine"], dict) else {}
        before, hz = machine.get("display_refresh_hz"), refresh_now()
        report["display"]["refresh_hz"] = hz
        if hz != before:
            report["display"]["refresh_hz_before_kept_on"] = before
            if hz is not None and "display_refresh_hz" in machine:
                machine["display_refresh_hz"] = hz
        if not args.quick:            # once, no window, before the windowed runs (#111 C7)
            print("store step (no window)", flush=True)
            report["store"] = store_step(args)
            missing = store_problems(report["store"])
            if missing:
                raise RuntimeError(f"the store step did not measure {', '.join(missing)}")
        for n in range(runs):
            print(f"run {n + 1} of {runs}", flush=True)
            last = n == runs - 1
            results.append(one_run(args, settle, window, args.leak_minutes if last else 0,
                                   display=display, refresh_hz=hz or before))
    except Exception as exc:          # anything that stops a run means "not measured"
        # Without the home folder or the user name; then the name guard (P3).
        report["each_run"], report["error"] = results, residue.redact(str(exc))
        residue.write_guarded(args.report, report)
        print(f"COULD NOT MEASURE: {exc}")
        return 2
    finally:
        if display is not None:
            display.stop()            # the display may turn off again as the plan says
    report["each_run"] = results
    # Managed policy: recording states it turned off (in any run), and how the
    # recordings were confirmed. Neither is a figure, so neither has a median.
    policy_off = list(dict.fromkeys(s for r in results for s in r.pop(
        "not_measured_by_policy", [])))
    consents = list(dict.fromkeys(r.pop("recording_consent") for r in results
                                  if "recording_consent" in r))
    report["not_measured_by_policy"] = policy_off
    report["recording_consent"] = consents
    figures = median_report(results)
    worst = worst_worker(results)       # pass or fail, not a noisy figure: the worst run counts
    if worst is not None:
        figures["worker"] = {"exit_after_last_use_s": worst}
    report["median"] = figures
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8")) if BUDGETS.exists() else {}
    skip = () if args.leak_minutes >= LEAK_BUDGET_MINUTES else ("leak",)
    if args.quick:                    # --quick has no store step to compare
        skip += ("store",)
    # The store step's figures are measured once, not per run, so they are
    # compared and proposed as they are, beside the medians (#430).
    gated = dict(figures, store=report["store"]) if report.get("store") else figures
    over, missing = compare(gated, budgets.get("budgets", {}), skip)
    report["over_budget"] = [{"state": s, "metric": m, "value": v, "budget": b}
                             for s, m, v, b in over]
    report["not_measured"] = [{"state": s, "metric": m} for s, m in missing]
    report["not_asked_for"] = list(skip)
    report["proposed_budgets_plus_20_percent"] = propose(gated)
    if getattr(args, "compare", None):
        report["comparison"] = compare_reports(report, earlier_report(args.compare))
    # Last: a report that would carry the Windows user name is written as a
    # failing report naming only where (P3; review of 78f17ee, #419).
    named = residue.write_guarded(args.report, report) is not report
    if named:
        print(f"FAILED: {residue.CARRIES_USER_NAME}")
    print(json.dumps(report["median"], indent=2))
    for s, m, v, b in over:
        print(f"OVER BUDGET: {s} {m} {v} > {b}")
    for s, m in missing:
        print(f"NOT MEASURED: {s} {m}")
    for s in policy_off:
        print(f"{s}: {POLICY_SKIPPED}; managed policy turns microphone recording off")
    if "off by policy" in consents:
        print("The recording confirmation is off by policy: no consent record was made.")
    if not report["display"]["kept_awake"]:
        print("Windows did not keep the display on for the run; each visible window was "
              "still checked as drawn.")
    if "leak" in skip:
        print(f"Not compared: the leak check needs --leak-minutes {LEAK_BUDGET_MINUTES} or more.")
    if "store" in skip:
        print("Not compared: the store step does not run with --quick.")
    if report.get("store"):
        print("store step: " + json.dumps(report["store"]))
    comparison = report.get("comparison")
    if comparison and comparison["machines_differ_in"]:
        print("Compared with the earlier report: not comparable, the machines differ in "
              + ", ".join(comparison["machines_differ_in"]))
    if not budgets:
        print("NO BUDGETS YET")
    elif not over and not missing and not named:
        print("WITHIN BUDGET")
    return 1 if over or missing or named else 0


def earlier_report(path):
    """An earlier release's evidence-perf.json, a local file only (--compare)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a performance report")
    return data


def store_step(args):
    """Run measure_store() once in a child (the package's runtime, as the
    window child runs), in a throwaway folder removed afterwards."""
    workdir = Path(tempfile.mkdtemp(prefix="unminute-store-"))
    command, env = child_command(args, workdir)
    command[command.index("--child")] = "--store-child"
    try:
        r = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                           env=env, timeout=STORE_TIMEOUT_S)
        found = [json.loads(line[len(PREFIX):]) for line in r.stdout.splitlines()
                 if line.startswith(PREFIX)]
        if r.returncode or not found:
            # The report is published: no throwaway path, home folder or user name (P3).
            raise RuntimeError(f"the store step failed (exit {r.returncode}): "
                               f"{residue.redact(r.stderr[-800:], workdir)}")
        return found[-1]
    finally:
        for _ in range(10):
            shutil.rmtree(workdir, ignore_errors=True)
            if not workdir.exists():
                break
            time.sleep(0.5)


def _median(fn, times):
    out = []
    for _ in range(times):
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return round(statistics.median(out), 4)


def measure_store(root, protector=None, meetings=STORE_MEETINGS, passages=STORE_PASSAGES,
                  opens=STORE_OPENS, reads=STORE_READS):
    """The C2b-2 design's section 4.8 figures (#111 C7), with no window:
    Workspace open time with 0 and with `meetings` meetings (median of
    `opens`), listing them, and for one meeting of `passages` passages a page
    of 1,000 and a search (median of `reads`), and that database's size.
    Seeded through the app's import (synthetic noise) and the store (synthetic
    text). protector: tests only; the app's own (Windows Data Protection)
    otherwise. Returns figures only, with the number of meetings the
    workspace timed for the `meetings` figures actually listed; a count that
    is not `meetings` (or an empty one that is not empty) stops the step, so
    the 500-meeting figure cannot come from a smaller workspace (review of
    #335)."""
    import types
    import wave

    import numpy as np

    from workspace.app import Workspace
    from workspace.diag import DB_FILE
    root = Path(root)
    models = root / "models"
    rng = np.random.default_rng(11)

    def synthetic(path, seconds):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((rng.standard_normal(int(16000 * seconds)) * 300)
                          .astype("<i2").tobytes())

    def workspace(folder):
        return Workspace(folder, models, start=False, battery=False, protector=protector)

    def open_time(folder):
        """The median open time, and how many meetings the folder's
        workspace lists (counted after the timing, so it costs nothing)."""
        def once():
            workspace(folder).close()
        median = _median(once, opens)
        ws = workspace(folder)
        try:
            return median, len(ws.meetings())
        finally:
            ws.close()

    def counted(what, found, wanted):
        if found != wanted:
            raise RuntimeError(f"the store step's {what} lists {found} meetings, "
                               f"not {wanted}")

    out = {}
    empty = root / "empty"
    workspace(empty).close()                  # made (and its key) before timing
    out["open_0_meetings_s"], found = open_time(empty)
    counted("empty workspace", found, 0)

    many = root / "many"
    ws = workspace(many)
    try:
        for i in range(meetings):
            path = root / f"synthetic-{i}.wav"
            synthetic(path, 1)
            ws._import_one(path)
            path.unlink()
        counted("listing", len(ws.meetings()), meetings)
        out["listing_500_meetings_s"] = _median(ws.meetings, reads)
    finally:
        ws.close()
    out["open_500_meetings_s"], found = open_time(many)
    counted("opened workspace", found, meetings)

    long = root / "long"
    ws = workspace(long)
    try:
        path = root / "synthetic-long.wav"
        synthetic(path, 60)
        ws._import_one(path)
        path.unlink()
        mid = ws.meetings()[0]["id"]
        vid = ws.store.active_version(mid)["id"]
        idx = ws.store.chunks(vid, status="pending")[0]["idx"]
        ws.store.commit_chunk(vid, idx, [
            types.SimpleNamespace(start=i * 3.0, end=i * 3.0 + 2.5,
                                  text=f"passage {i} with some words", words=())
            for i in range(passages)])
        start = max(0, passages - 1000)
        out["page_5000_passages_s"] = _median(lambda: ws.transcript(mid, start, 1000), reads)
        out["search_5000_passages_s"] = _median(
            lambda: ws.search(mid, f"passage {passages - 1}"), reads)
    finally:
        ws.close()
    size = sum(p.stat().st_size for p in long.glob(DB_FILE + "*") if p.is_file())
    out["database_5000_passages_mb"] = round(size / 2**20, 2)
    out["meetings"], out["passages"] = found, passages      # the count measured above
    return out


def store_child(args):
    """--store-child: measure_store() in this process, figures on stdout."""
    app = Path(args.app) if args.app else ROOT
    evidence.add_app(app)
    from workspace import offline_guard     # as the app itself starts
    offline_guard.disable_telemetry()
    offline_guard.install()
    if args.app:
        evidence.require_app_modules(app)
    figures = measure_store(Path(args.workdir))
    sys.stdout.write(PREFIX + json.dumps(figures) + "\n")
    sys.stdout.flush()
    return 0


# -- the child: the real window on synthetic meetings ----------------------------------

def child(args):
    t0 = time.perf_counter()
    app = Path(args.app) if args.app else ROOT
    evidence.add_app(app)                     # as the package's own runtime has it, if so
    sys.path.append(str(ROOT / "tests"))      # fake engines, after the standard library
    from workspace import offline_guard     # as the app itself starts (workspace/__main__.py)
    offline_guard.disable_telemetry()
    offline_guard.install()
    # The window's backend loads alongside the imports, as workspace/__main__.py
    # starts it (#339). Its whole wait is counted here, with the imports: the
    # app also overlaps it with opening the workspace, which is not counted.
    from workspace import shell
    preload = getattr(shell, "preload_backend", None)    # absent in older packages
    if preload is not None:
        preload()
    import wave

    import numpy as np

    from workspace import api, consent, credentials, power, recorder
    from workspace.app import Workspace
    if preload is not None:
        shell.wait_preload()
    imports_s = time.perf_counter() - t0
    if args.app:
        evidence.require_app_modules(app)       # measuring the package, not this checkout
    # The synthetic microphone, in this process only: the device list names
    # one synthetic input and opening it starts a PacedStream, so no audio
    # device (and no sounddevice) is ever loaded. Never the real power scheme.
    if recorder.BLOCK_S != SYNTHETIC_BLOCK_S:
        raise SystemExit("perf_check: recorder.BLOCK_S changed; update SYNTHETIC_BLOCK_S")
    blocks = synthetic_blocks()
    recorder.list_devices = lambda: [dict(SYNTHETIC_DEVICE)]

    def open_synthetic(device, rate, callback, finished):
        return PacedStream(rate, callback, finished, blocks=blocks)

    def never(*a, **k):
        raise RuntimeError("perf_check never changes the power scheme")
    power._PowrProf.write = power._PowrProf.apply = never

    def send(**msg):
        sys.stdout.write(PREFIX + json.dumps(msg) + "\n")
        sys.stdout.flush()

    def log(text):                     # progress for whoever runs the tool
        print(f"  [{time.perf_counter() - t0:6.1f} s] {text}", file=sys.stderr, flush=True)

    fake = "test:fake_worker_engines:"
    root = Path(args.workdir)          # made and removed by the parent
    opening = time.perf_counter()
    ws = Workspace(root / "ws", root / "models", start=False, allow_test_engines=True,
                   battery=False)
    workspace_s = time.perf_counter() - opening      # a new workspace: keys made too
    ws.open_stream = open_synthetic
    try:
        ws.settings.update(model=fake + "sentences", speakers=False, onboarded=True)
        ws.start()
        rng = np.random.default_rng(7)
        for i in range(MEETINGS):
            path = root / f"synthetic-meeting-{i + 1}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((rng.standard_normal(16000 * 60) * 300).astype("<i2").tobytes())
            ws.import_paths([str(path)])
        log("transcribing the synthetic meetings with fake engines")
        if not ws.wait_idle(300) or len(ws.meetings()) < MEETINGS:
            send(error="the synthetic meetings were not ready in 300 s")
            return 1
        worker = {"worker_pid": ws.handle.pid, "worker_last_used": time.time()}
        vault = {}                         # never touch the real Credential Manager
        credentials.load_token = lambda target=None: vault.get("token")
        credentials.has_token = lambda target=None: "token" in vault
        holder = {}

        def wait_for(window, js, ok, what, timeout=90):
            value, end = None, time.monotonic() + timeout
            while time.monotonic() < end:
                value = window.evaluate_js(js)
                if ok(value):
                    return value
                time.sleep(0.05)
            raise TimeoutError(f"{what} (last value {value!r})")

        def drive(window):
            try:
                if not window.events.loaded.wait(90):
                    raise TimeoutError("the page did not load")
                log("page loaded")
                # show() sets data-view; the page's start-up handler calls it once
                # it has chosen its first view, so wait for that before switching.
                wait_for(window, "document.body.dataset.view || ''", bool,
                         "the page did not start")
                log("page started")
                window.evaluate_js("window.workspace.show('meetings')")
                wait_for(window, "document.querySelectorAll('#groups li').length",
                         lambda n: (n or 0) >= MEETINGS, "the meetings list did not fill")
                interactive = time.perf_counter() - holder["t1"]
                log("meetings shown")
                send(imports_s=round(imports_s, 2), window_to_interactive_s=round(interactive, 2),
                     startup_s=round(imports_s + interactive, 2),
                     workspace_s=round(workspace_s, 3),
                     recording_allowed=ws.managed()["allow"]["AllowMicrophoneRecording"],
                     **worker)
                # The frame counter (#292), after start-up is timed, before any state.
                if window.evaluate_js(PAGE_COUNTER_JS) is not True:
                    raise RuntimeError("the page's frame counter could not be set up")
                for line in sys.stdin:
                    state = line.strip()
                    if state == "check":                 # after a recording state's window
                        send(recording=recording_now())
                        continue
                    if state == "probe":                 # around a visible state's window
                        send(page=page_reading(window.evaluate_js(PAGE_READ_JS)))
                        continue
                    started = {}
                    for step in steps(state, ws.recording_active()):
                        if step == "restore":
                            window.restore()
                        elif step == "discard":          # never imported or kept
                            ws.discard_recording()
                        elif step.startswith("show:"):
                            window.evaluate_js(f"window.workspace.show('{step[5:]}')")
                        elif step == "record":
                            started["consent"] = record(window)
                        elif step == "minimise":
                            window.minimize()
                    if state == "quit":
                        break
                    if state in RECORDING_STATES:
                        send(state=state, recording=recording_now(), **started)
                    else:
                        send(state=state)
            except Exception as exc:          # report, then let the window close
                send(error=f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    if ws.recording_active():
                        ws.discard_recording()
                except Exception:
                    pass
                window.destroy()

        def record(window):
            """Start a recording as a person does: microphone recording on in
            Settings, then Record. The bridge asks the app's own recording
            question; consent_answerer says yes, and the bridge makes the
            consent record that start_recording requires and keeps. Where
            policy turns the confirmation off, nothing is asked and no record
            is made (consent_outcome). Returns that outcome for the report."""
            waits = RECORD_WAITS_S                # the parent allows for these (step_timeout)
            if not ws.settings["microphone"]:
                ws.set_settings({"microphone": True})
                window.evaluate_js("window.workspace.show('meetings')")
            wait_for(window, "(() => { const b = document.getElementById('rec-start');"
                             " return !!b && !b.hidden && !b.disabled; })()", bool,
                     "the Record button was not ready", waits["button"])
            required = ws.consent_required()      # as the bridge reads it on the click
            window.evaluate_js("document.getElementById('rec-start').click()")
            wait_for(window, "!document.getElementById('rec-card').hidden", bool,
                     "the recording did not start", waits["started"])
            rec = getattr(ws, "_recording", None)
            if rec is None:
                raise RuntimeError("the recording did not open")
            outcome = consent_outcome(required, rec.journal.get("consent"), list(asked),
                                      consent.RECORDING_TITLE, consent.valid_recording_record)
            asked.clear()
            end = time.monotonic() + waits["audio"]
            while (recording_now() or {}).get("seconds", 0) < 1:
                if time.monotonic() > end:
                    raise TimeoutError("no synthetic audio reached the recording")
                time.sleep(0.1)
            if "sounddevice" in sys.modules:
                raise RuntimeError("an audio device library was loaded")
            log(f"recording synthetic audio (confirmation {outcome})")
            return outcome

        def recording_now():
            """The open recording's state and length for the parent, or None."""
            rec = ws.recording_status().get("recording")
            return {"state": rec["state"], "seconds": rec["seconds"]} if rec else None

        # make_api's confirm seam (for tests and this tool; the app passes none):
        # the recording question is answered here, never shown on screen.
        confirm, asked = consent_answerer(consent.RECORDING_TITLE)
        holder["t1"] = time.perf_counter()          # window creation to interactive
        window = shell.create_window(api.make_api(ws, lambda: holder.get("window"),
                                                  confirm=confirm),
                                     title="Unminute performance check (synthetic data)",
                                     allowed=api.API_METHODS)
        holder["window"] = window
        if hasattr(shell, "watch_minimise"):     # as workspace/__main__.py does (#129)
            capturing = getattr(ws, "_capturing", ws.recording_active)
            window.events.shown += lambda: shell.watch_minimise(window, capturing)
        shell.run(lambda w: threading.Thread(target=drive, args=(w,), daemon=True).start(),
                  (window,), storage_path=shell.storage_folder(ws.root))
        return 0
    finally:
        ws.close()


def main():
    parser = argparse.ArgumentParser(
        description="Release-time performance evidence (docs/RELEASE_STANDARD.md, section 2).",
        epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("report", nargs="?", help="where to write the JSON report")
    parser.add_argument("--quick", action="store_true",
                        help="1 run, 20 s settle, 60 s windows, for before/after checks")
    parser.add_argument("--runs", type=int, default=RELEASE_RUNS)
    parser.add_argument("--settle", type=int, default=RELEASE_SETTLE_S)
    parser.add_argument("--window", type=int, default=RELEASE_WINDOW_S)
    parser.add_argument("--leak-minutes", type=int, default=0,
                        help="hold the Meetings view this long, once, and report growth")
    parser.add_argument("--app", help="an installed package's app folder (run with that "
                        "package's Python: runtime\\python.exe -I -B -X disable-remote-debug)")
    parser.add_argument("--zip", help="the package zip installed at --app: records it as release "
                        "evidence (tools/evidence.py); exit 1 unless that evidence passes")
    parser.add_argument("--compare", help="a local copy of an earlier release's "
                        "evidence-perf.json: list the figures both have, side by side")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--store-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--workdir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(args)
    if args.store_child:
        return store_child(args)
    if not args.report:
        parser.error("give a report path")
    if args.compare:                    # a bad file stops now, not after the run
        try:
            earlier_report(args.compare)
        except (OSError, ValueError) as exc:
            parser.error(f"--compare: {exc}")
    before = evidence.prepare(parser, args, Path(args.app) if args.app else None)
    rc = parent(args)
    if args.zip:
        run = run_shape(args)
        ev = evidence.stamp_file(args.report, "perf", args.zip, Path(args.app).resolve().parent,
                                 passed=rc == 0 and evidence.perf_release_run(run)
                                 and BUDGETS.exists(), before=before, run=run)
        rc = rc or (0 if ev["pass"] else 1)
    return rc


def run_shape(args):
    """What the evidence records of this run (a full release run counts)."""
    return {"quick": args.quick, "runs": args.runs, "settle_s": args.settle,
            "window_s": args.window, "leak_minutes": args.leak_minutes}


if __name__ == "__main__":
    sys.exit(main())
