# tools/draft_scenario.py
# Live drafts in the real-window checks (#370): a recording with draft text on
# the Meetings page, with no microphone, speech model or audio. DraftFeed
# stands in for the recording and its draft session by replacing three methods
# on the check's own Workspace object, recording_status and recording_draft,
# with invented sample sentences (SENTENCES), and microphone_devices, which the
# recording card asks for its choice of microphone (#462). The page reaches them
# only through the app's real bridge methods of the same names (workspace/api.py),
# so the bridge, its argument checks and API_METHODS are unchanged (I3). The
# feed exists only in this tool and the check's process: nothing in
# workspace/ names it, and the shipped app cannot reach it.
#
# The feed answers as the app does (Workspace._draft_status and
# Workspace.recording_draft): a draft session's state with drafts.NOTES, or
# why there is none with drafts.REASONS, in the app's own words, and the same
# generation rules. tests/test_draft_scenario.py keeps the two the same and
# runs this scenario on the real page in headless Edge.
#
# draft_checks runs the scenario for tools/workspace_ui_check.py and
# DRAFT_A11Y and draft_announcements for tools/accessibility_check.py; the
# *_problems functions say what failed. Standard library and workspace.drafts
# only, so a test can import this module without opening a window.

import json
import time

from workspace import drafts

# Invented sample sentences (no real meeting content, P1).
SENTENCES = (
    "Synthetic check: the north bed needs fresh compost before spring.",
    "We could ask the school whether they want the spare seedlings.",
    "The rain tank by the shed has a small leak near the tap.",
    "Let's move the working bee to the second Saturday of the month.",
    "Someone offered to bring a trailer for the green waste.",
    "The bean frames need new string before they go back up.",
    "We have enough mulch for the paths but not for the orchard.",
    "Can we put a notice on the gate about the new opening hours?",
    "The citrus trees look better since the last feed.",
    "Two people asked about a plot for next season.",
    "The tool shed lock sticks when it is cold.",
    "We will check the budget for a second wheelbarrow.",
)
FEED_DEVICE = "Synthetic microphone (draft check)"
FEED_PREFIX = "synthetic-draft-recording-"
LIMIT = 200                          # phrases one recording_draft answer holds (the app's)
# Session states and the reasons there is no session, as the app has them.
SESSION_STATES = (drafts.STARTING, drafts.RUNNING, drafts.BATTERY, drafts.FAILED,
                  drafts.UNAVAILABLE)
REASONS = tuple(drafts.REASONS)


class DraftFeed:
    """A recording with live drafts, for the check's Workspace only.

    install() replaces ws.recording_status, ws.recording_draft and
    ws.microphone_devices on that object; remove() puts the Workspace's own
    back. While no recording has begun, or after end(), each answers exactly
    as the Workspace does. Every
    recording_draft call is counted in `fetches` (the `since` it asked for)."""

    def __init__(self, ws, clock=time.monotonic):
        self.ws, self.clock = ws, clock
        self.fetches = []
        self.recording = None        # the synthetic recording's id while one is open
        self.generation = 0
        self.state = None            # a session's state, or None when there is no session
        self.why = "off"             # why there is no session (drafts.REASONS), as the app's
        self.phrases = []
        self.said = 0                # sentences said so far, for the next line's text
        self.started = 0.0
        self._recordings = 0

    # -- installing ------------------------------------------------------------------

    def install(self):
        self.ws.recording_status = self.recording_status
        self.ws.recording_draft = self.recording_draft
        self.ws.microphone_devices = self.microphone_devices
        return self

    def remove(self):
        for name in ("recording_status", "recording_draft", "microphone_devices"):
            self.ws.__dict__.pop(name, None)

    @property
    def active(self):
        return self.recording is not None

    # -- the script ----------------------------------------------------------------

    def begin(self, state=drafts.STARTING):
        """A recording starts, with a new draft session in `state`."""
        self._recordings += 1
        self.recording = f"{FEED_PREFIX}{self._recordings}"
        self.started = self.clock()
        self.why = "off"
        self.state = None
        self.session(state)

    def session(self, state):
        """The draft session's state. With no session (withheld), a new one
        starts, as Workspace._start_drafts does: a new generation, no text."""
        if state not in SESSION_STATES:
            raise ValueError(f"not a draft session state: {state!r}")
        self._need_recording()
        if self.state is None:
            self.generation += 1
            self.phrases = []
        self.state = state

    def withhold(self, reason):
        """No draft session, and why. A session that was running ends and its
        text is forgotten, with a new generation (Workspace._end_drafts)."""
        if reason not in REASONS:
            raise ValueError(f"not a reason drafts are withheld: {reason!r}")
        self._need_recording()
        if self.state is not None:
            self.generation += 1
        self.state, self.why, self.phrases = None, reason, []

    def say(self, n=1):
        """n more invented sentences, as the draft worker's phrases. Returns
        their text."""
        if self.state is None:
            raise ValueError("no draft session")
        out = []
        for _ in range(n):
            text = SENTENCES[self.said % len(SENTENCES)]
            start = 3.0 * len(self.phrases)
            self.phrases.append({"kind": "phrase", "text": text, "start": start,
                                 "end": start + 2.5})
            self.said += 1
            out.append(text)
        return out

    def skip(self, seconds):
        """A skipped note, as when the draft worker falls behind."""
        if self.state is None:
            raise ValueError("no draft session")
        self.phrases.append({"kind": "skipped", "seconds": float(seconds)})

    def end(self):
        """The recording stops: nothing is kept."""
        self.recording, self.state, self.why, self.phrases = None, None, "off", []

    def _need_recording(self):
        if self.recording is None:
            raise ValueError("no recording has begun")

    # -- what the app answers --------------------------------------------------------

    def note(self):
        return (drafts.NOTES.get(self.state) if self.state is not None
                else drafts.REASONS.get(self.why))

    def draft_status(self):
        """recording_status's "draft", as Workspace._draft_status."""
        if self.recording is None:
            return None
        if self.state is not None:
            return {"state": self.state, "note": self.note(), "count": len(self.phrases),
                    "generation": self.generation, "mix": False}
        if self.why == "off":
            return None
        return {"state": self.why, "note": self.note(), "count": 0,
                "generation": self.generation, "mix": False}

    def recording_dict(self):
        """recording_status's "recording", shaped as recorder.Recording.status()."""
        return {"state": "recording", "seconds": round(self.clock() - self.started, 1),
                "level_db": -30.0, "id": self.recording, "levels": [], "viz": None,
                "device": FEED_DEVICE, "switched": False, "following": True,
                "computer": None, "events": []}

    def microphone_devices(self):
        """While its recording is open, the recording card's list (#462): the
        feed's microphone only, as the one recording. Otherwise, and while
        locked, the Workspace's own answer."""
        if self.recording is None or self.ws.locked is not None:
            return type(self.ws).microphone_devices(self.ws)
        return [{"index": 0, "name": FEED_DEVICE, "label": FEED_DEVICE, "default": True,
                 "current": True}]

    def recording_status(self):
        """The Workspace's own answer, with this recording and its draft in
        place of none (never while the workspace is locked: nothing records)."""
        status = type(self.ws).recording_status(self.ws)
        if self.recording is not None and self.ws.locked is None:
            status = dict(status, recording=self.recording_dict(), draft=self.draft_status())
        return status

    def recording_draft(self, since=0):
        """As Workspace.recording_draft: the same argument check and errors."""
        if self.recording is None:
            return type(self.ws).recording_draft(self.ws, since)
        if not isinstance(since, int) or isinstance(since, bool) or not 0 <= since <= 10 ** 7:
            return type(self.ws).recording_draft(self.ws, since)      # its own refusal
        self.fetches.append(since)
        if self.state is None:
            return {"state": self.why, "note": self.note(), "generation": self.generation,
                    "count": 0, "since": since, "phrases": []}
        return {"state": self.state, "note": self.note(), "generation": self.generation,
                "count": len(self.phrases), "since": since,
                "phrases": [dict(p) for p in self.phrases[since:since + LIMIT]]}


# -- the scenario on the page (tools/workspace_ui_check.py) --------------------------------

# Page helpers. The status line's text is recorded on every change
# (window.__draftSaid), so the check can count what a screen reader would hear.
DRAFT_JS = """
    const wait = (ms) => new Promise(r => setTimeout(r, ms));
    const until = async (ok, ms = 15000) => {
        const end = performance.now() + ms;
        while (!ok() && performance.now() < end) await wait(25);
        return ok(); };
    const frames = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const $ = (id) => document.getElementById(id);
    const box = () => $('rec-draft-box');
    const lines = () => box().querySelectorAll('.rec-draft-line, .rec-draft-skip').length;
    const texts = () => [...box().querySelectorAll('.rec-draft-line')].map(p => p.textContent);
    const atEnd = () => box().scrollHeight - box().scrollTop - box().clientHeight < 8;
    const shown = (el) => !el.hidden && !el.closest('[hidden]')
        && getComputedStyle(el).display !== 'none';
    const status = () => $('rec-draft-status');
    const said = () => (window.__draftSaid || []).slice();
    const look = () => ({area: shown($('rec-draft')), box: shown(box()), lines: lines(),
        status: status().textContent, status_shown: shown(status())
            && !status().classList.contains('sr'),
        empty: box().querySelector('.rec-draft-empty')?.textContent || '',
        toggle: $('rec-draft-toggle').textContent.trim(),
        expanded: $('rec-draft-toggle').getAttribute('aria-expanded'),
        hidden_page: document.hidden, said: said().length});"""
# Waits are in page time; a step that times out returns what it saw, and the
# rule that needed it fails.
WATCH_JS = """
    if (!window.__draftWatch) {
        window.__draftSaid = [];
        window.__draftWatch = new MutationObserver(() =>
            window.__draftSaid.push(document.getElementById('rec-draft-status').textContent));
        window.__draftWatch.observe(document.getElementById('rec-draft-status'),
            {childList: true, characterData: true, subtree: true});
    }"""
# The states each note is checked in, in order: (session or withheld, state).
STATE_STEPS = (
    ("session", drafts.BATTERY),
    ("session", drafts.RUNNING),
    ("session", drafts.FAILED),
    ("withhold", "managed"),
    ("withhold", "unavailable"),
    ("session", drafts.UNAVAILABLE),
    ("session", drafts.STARTING),
)
HIDDEN_WAIT_S = 3.0          # how long the window stays hidden while new text arrives
FOLLOW_LINES = 12            # enough to overflow the box (about eight lines high)


def draft_checks(window, feed, evaluate, hide, show):
    """The draft scenario on the real page: a recording with draft text
    (feed), driven as a person would. `evaluate(window, js, timeout)` runs
    page script; hide() and show() minimise and restore the window (or, in
    headless Edge, make the page report itself hidden). Returns the evidence;
    draft_problems says what failed."""
    out = {}

    def page(js, timeout=60):
        return evaluate(window, DRAFT_JS + js, timeout)

    feed.begin(drafts.STARTING)
    try:
        out["starting"] = page(WATCH_JS + f"""
            window.workspace.show('meetings');
            await until(() => shown($('rec-draft'))
                        && status().textContent === {json.dumps(feed.note())});
            return look();""")
        feed.session(drafts.RUNNING)
        out["running"] = page("""
            await until(() => status().textContent === 'Draft text is on.'
                        && !!box().querySelector('.rec-draft-empty'));
            return look();""")
        # The box follows the newest line while it is at the end.
        mark = len(feed.fetches)
        last = feed.say(FOLLOW_LINES)[-1]
        out["first_lines"] = page(f"""
            await until(() => lines() === {FOLLOW_LINES}); await frames();
            return Object.assign(look(), {{at_end: atEnd(),
                overflows: box().scrollHeight > box().clientHeight + 8,
                last: texts().pop() === {json.dumps(last)}}});""")
        for _ in range(3):                     # one line at a time, each fetched
            last = feed.say(1)[-1]
            time.sleep(0.4)
        out["followed"] = page(f"""
            await until(() => lines() === {FOLLOW_LINES + 3}); await frames();
            return Object.assign(look(), {{at_end: atEnd(),
                last: texts().pop() === {json.dumps(last)}}});""")
        # Scrolled up: new lines are added below, the box stays where it is.
        page("box().scrollTop = 0; await wait(300); return true;")
        feed.say(3)
        out["scrolled_up"] = page(f"""
            await until(() => lines() === {FOLLOW_LINES + 6}); await frames();
            return Object.assign(look(), {{scroll_top: box().scrollTop, at_end: atEnd()}});""")
        # Back at the end: it follows again.
        page("box().scrollTop = box().scrollHeight; await wait(300); return true;")
        last = feed.say(2)[-1]
        out["followed_again"] = page(f"""
            await until(() => lines() === {FOLLOW_LINES + 8}); await frames();
            return Object.assign(look(), {{at_end: atEnd(),
                last: texts().pop() === {json.dumps(last)}}});""")
        out["fetches_for_lines"] = len(feed.fetches) - mark
        # The status line said nothing from "Draft text is on." to here.
        out["said_while_lines_arrived"] = (out["followed_again"].get("said", -1)
                                           - out["running"].get("said", 0))
        # Hide draft: the text goes from the page and nothing is fetched.
        out["hidden_by_user"] = page("""
            $('rec-draft-toggle').click();
            await until(() => !shown(box())); await wait(300);
            return look();""")
        mark = len(feed.fetches)
        feed.say(2)
        time.sleep(1.5)
        out["fetches_while_hidden_by_user"] = len(feed.fetches) - mark
        out["still_hidden"] = page("return look();")
        # Show draft: the latest text is fetched again.
        total = len(feed.phrases)
        out["shown_by_user"] = page(f"""
            $('rec-draft-toggle').click();
            await until(() => lines() === {total}); await frames();
            return Object.assign(look(), {{at_end: atEnd()}});""")
        # The window hidden (minimised): nothing is fetched until it is shown.
        hide()
        out["window_hidden"] = page("await until(() => document.hidden); return look();")
        mark = len(feed.fetches)
        feed.say(3)
        time.sleep(HIDDEN_WAIT_S)
        out["fetches_while_window_hidden"] = len(feed.fetches) - mark
        out["while_window_hidden"] = page("return look();")
        show()
        total = len(feed.phrases)
        out["window_shown"] = page(f"""
            await until(() => !document.hidden && lines() === {total}); await frames();
            return look();""")
        out["fetches_after_shown"] = len(feed.fetches) - mark
        # Each state's note, announced once per change.
        out["states"] = []
        for kind, name in STATE_STEPS:
            before = page("return said().length;")
            mark = len(feed.fetches)
            (feed.session if kind == "session" else feed.withhold)(name)
            expect = feed.note() or "Draft text is on."
            got = page(f"""
                await until(() => status().textContent === {json.dumps(expect)});
                await wait(600);
                return look();""")
            got.update(kind=kind, name=name, expected=expect,
                       announced=got.get("said", 0) - before,
                       fetched=len(feed.fetches) - mark)
            out["states"].append(got)
        # Stop: the area goes and keeps nothing.
        feed.end()
        out["stopped"] = page("""
            await until(() => !shown($('rec-draft'))); return look();""")
    finally:
        feed.end()
    return out


def draft_problems(dr):
    """What the draft scenario found wrong (an empty list: nothing)."""
    bad = []
    s = dr.get("starting") or {}
    if not (s.get("area") and s.get("status") == drafts.NOTES[drafts.STARTING]
            and s.get("status_shown") and s.get("empty") == "Getting draft text ready…"):
        bad.append("starting: the area, its note or its placeholder")
    if not (s.get("toggle") == "Hide draft" and s.get("expanded") == "true"):
        bad.append("starting: the Hide draft button")
    s = dr.get("running") or {}
    if not (s.get("status") == "Draft text is on." and s.get("status_shown") is False
            and s.get("empty", "").startswith("Draft text appears here")):
        bad.append("running: the status line or the placeholder")
    s = dr.get("first_lines") or {}
    if not (s.get("lines") == FOLLOW_LINES and s.get("last") and s.get("overflows")
            and s.get("at_end") and not s.get("empty")):
        bad.append("first lines: not shown, or the box did not follow the newest")
    s = dr.get("followed") or {}
    if not (s.get("lines") == FOLLOW_LINES + 3 and s.get("last") and s.get("at_end")):
        bad.append("new lines: the box did not follow the newest line")
    s = dr.get("scrolled_up") or {}
    if not (s.get("lines") == FOLLOW_LINES + 6 and s.get("scroll_top", -1) < 8
            and s.get("at_end") is False):
        bad.append("scrolled up: the box moved to the newest line")
    s = dr.get("followed_again") or {}
    if not (s.get("lines") == FOLLOW_LINES + 8 and s.get("last") and s.get("at_end")):
        bad.append("back at the end: the box did not follow again")
    if dr.get("said_while_lines_arrived") != 0:
        bad.append("the status line changed as lines arrived (it must announce state only)")
    s = dr.get("hidden_by_user") or {}
    if not (s.get("box") is False and s.get("lines") == 0 and s.get("toggle") == "Show draft"
            and s.get("expanded") == "false"
            and s.get("status") == "Draft text is hidden for this recording."
            and s.get("status_shown")):
        bad.append("Hide draft: the text, the button or the status line")
    if dr.get("fetches_while_hidden_by_user") != 0 \
            or (dr.get("still_hidden") or {}).get("lines") != 0:
        bad.append("Hide draft: draft text was fetched or shown while hidden")
    s = dr.get("shown_by_user") or {}
    if not (s.get("box") and s.get("lines", 0) > 0 and s.get("toggle") == "Hide draft"
            and s.get("expanded") == "true" and s.get("status") == "Draft text is on."):
        bad.append("Show draft: the latest text, the button or the status line")
    if not (dr.get("window_hidden") or {}).get("hidden_page"):
        bad.append("the window did not report itself hidden")
    if dr.get("fetches_while_window_hidden") != 0:
        bad.append("draft text was fetched while the window was hidden")
    w, ws = dr.get("while_window_hidden") or {}, dr.get("window_shown") or {}
    if w.get("lines") != (dr.get("window_hidden") or {}).get("lines"):
        bad.append("lines were added while the window was hidden")
    if not (ws.get("hidden_page") is False and ws.get("lines") == w.get("lines", -3) + 3
            and dr.get("fetches_after_shown", 0) > 0):
        bad.append("the latest text was not fetched once the window was shown")
    states = dr.get("states") or []
    if [(s.get("kind"), s.get("name")) for s in states] != list(STATE_STEPS):
        bad.append("not every state was checked")
    for s in states:
        name = f"{s.get('kind')} {s.get('name')}"
        if s.get("status") != s.get("expected"):
            bad.append(f"{name}: the status line did not say its note")
        elif s.get("expected") != "Draft text is on." and not s.get("status_shown"):
            bad.append(f"{name}: its note was not shown")
        if s.get("announced") != 1:
            bad.append(f"{name}: announced {s.get('announced')} times, not once")
        if not s.get("area"):
            bad.append(f"{name}: the draft area was not shown")
        # Withheld or ended, the app forgets the text (docs/WORKSPACE_DESIGN.md,
        # live drafts): so does the page, and it has nothing new to fetch.
        if s.get("kind") == "withhold" and s.get("lines") != 0:
            bad.append(f"{name}: the draft text stayed on the page")
        if s.get("fetched", 99) > 1:
            bad.append(f"{name}: draft text was fetched {s.get('fetched')} times with "
                       "nothing new")
    s = dr.get("stopped") or {}
    if not (s.get("area") is False and s.get("lines") == 0):
        bad.append("stopped: the draft area stayed, or kept its text")
    return bad


# -- the accessibility check (tools/accessibility_check.py) --------------------------------

DRAFT_VIEW = "recording draft"
# The draft area's own points, audited in each colour mode on top of the
# general audit: the box is not read out as it grows; its status line is
# polite and outside it; the box and its button are named and reachable by
# keyboard; and the grey text's contrast against the box (1.4.3).
DRAFT_A11Y = r"""
const $ = (id) => document.getElementById(id);
const box = $('rec-draft-box'), status = $('rec-draft-status'), toggle = $('rec-draft-toggle');
const rgb = (c) => { const m = c.match(/[\d.]+/g); return m ? m.map(Number) : [0, 0, 0, 1]; };
const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 :
  Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
const bgOf = (el) => {
  for (let e = el; e; e = e.parentElement) {
    const c = rgb(getComputedStyle(e).backgroundColor);
    if ((c[3] ?? 1) > 0.5) return c;
  }
  return rgb(getComputedStyle(document.body).backgroundColor);
};
const ratio = (el) => {
  const fg = rgb(getComputedStyle(el).color), bg = bgOf(el), a = fg[3] ?? 1;
  const mix = fg.slice(0, 3).map((v, i) => v * a + bg[i] * (1 - a));
  const L1 = lum(mix), L2 = lum(bg);
  return Math.round((Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05) * 100) / 100;
};
const reach = (el) => { el.focus({focusVisible: true});
  const ok = document.activeElement === el && el.tabIndex >= 0; el.blur(); return ok; };
const named = (el) => (el.getAttribute('aria-labelledby') || '').split(/\s+/)
  .map(i => $(i)?.textContent || '').join(' ').trim();
const line = box.querySelector('.rec-draft-line');
const shown = (el) => !!el && !el.closest('[hidden]') && getComputedStyle(el).display !== 'none';
return {
  box_shown: shown(box), lines: box.querySelectorAll('.rec-draft-line').length,
  box_live: box.getAttribute('aria-live'), box_role: box.getAttribute('role'),
  box_name: named(box), box_reachable: reach(box),
  status_role: status.getAttribute('role'), status_live: status.getAttribute('aria-live'),
  status_outside_box: !box.contains(status),
  toggle_button: toggle.tagName === 'BUTTON', toggle_name: toggle.textContent.trim(),
  toggle_controls: toggle.getAttribute('aria-controls'),
  toggle_reachable: shown(toggle) && reach(toggle),
  line_ratio: line ? ratio(line) : null,
  label_ratio: ratio(document.querySelector('.rec-draft-label')),
  share_ratio: ratio(document.querySelector('.rec-draft-share')),
};"""


def draft_a11y_problems(a):
    """What DRAFT_A11Y found wrong in one colour mode (an empty list: nothing)."""
    a = a or {}
    bad = []
    if not (a.get("box_shown") and a.get("lines", 0) > 0):
        bad.append("the draft box with text was not shown")
    if a.get("box_live") != "off":
        bad.append(f"4.1.3: the draft box is announced as it grows (aria-live "
                   f"{a.get('box_live')!r}, not 'off')")
    if not (a.get("status_role") == "status" and a.get("status_live") == "polite"
            and a.get("status_outside_box")):
        bad.append("4.1.3: the draft status line is not a polite status outside the box")
    if not (a.get("box_role") == "region" and a.get("box_name") == "Draft text"):
        bad.append("4.1.2: the draft box is not a region named Draft text")
    if not a.get("box_reachable"):
        bad.append("2.1.1: the draft box cannot be reached by keyboard (to scroll it)")
    if not (a.get("toggle_button") and a.get("toggle_name") in ("Hide draft", "Show draft")
            and a.get("toggle_controls") == "rec-draft-box" and a.get("toggle_reachable")):
        bad.append("2.1.1, 4.1.2: the Hide draft button")
    for key in ("line_ratio", "label_ratio", "share_ratio"):
        if not (isinstance(a.get(key), (int, float)) and a[key] >= 4.5):
            bad.append(f"1.4.3: draft text contrast {key} {a.get(key)} is below 4.5:1")
    return bad


def draft_announcements(window, feed, evaluate):
    """With draft text showing: what the status line said while lines
    arrived (nothing), and at a change of state and back (once each)."""
    def page(js):
        return evaluate(window, DRAFT_JS + js, 60)
    page(WATCH_JS + "return true;")
    before = page("return said();")
    feed.say(3)
    total = len(feed.phrases)
    lines = page(f"await until(() => lines() === {total}); await wait(600); return said();")
    feed.session(drafts.BATTERY)
    battery = page(f"""await until(() => status().textContent === {json.dumps(feed.note())});
        await wait(600); return said();""")
    feed.session(drafts.RUNNING)
    running = page("""await until(() => status().textContent === 'Draft text is on.');
        await wait(600); return said();""")
    final = page("return look();")
    return {"lines_added": 3, "while_lines": lines[len(before):],
            "battery": battery[len(lines):], "running": running[len(battery):],
            "final": final}


def announcement_problems(an):
    """What draft_announcements found wrong (an empty list: nothing)."""
    an = an or {}
    bad = []
    if an.get("while_lines") != []:
        bad.append("4.1.3: the draft status line spoke as lines arrived "
                   f"({len(an.get('while_lines') or [None])} times)")
    if an.get("battery") != [drafts.NOTES[drafts.BATTERY]]:
        bad.append("4.1.3: a change of state was not announced once")
    if an.get("running") != ["Draft text is on."]:
        bad.append("4.1.3: the return to running was not announced once")
    return bad
