// tools/design_check/fixtures.js
// A fake pywebview bridge for tools/design_check.py: the real page (workspace/ui)
// runs against fixed sample data, so each screen can be compared with its
// design board. Loaded before app.js only by the check's local server; never
// shipped. The scenario is chosen by the URL hash, e.g. index.html#meeting.
// All names and text are the fictional sample meeting used on the boards.
"use strict";
(() => {
  // A fixed clock: 15 Oct 2026, 2:30 pm local time.
  const FIXED = new Date(2026, 9, 15, 14, 30, 0).getTime();
  const RealDate = Date;
  class FixedDate extends RealDate {
    constructor(...args) { if (args.length) super(...args); else super(FIXED); }
    static now() { return FIXED; }
  }
  window.Date = FixedDate;
  const now = FIXED / 1000;
  const at = (h, m) => new RealDate(2026, 9, 15, h, m, 0).getTime() / 1000;
  const DAY = 86400;

  // -- sample meetings -------------------------------------------------------------
  const LIMITS = "This removes the app's own working copies on this computer. It cannot remove copies elsewhere: synced or backed-up folders (for example OneDrive), the original on a phone or recorder, or anything you shared with an AI assistant or other service. Ordinary deletion is not a forensic erasure of the disk.";
  const GARDEN = [
    [0, 2, "SPEAKER_00", "Thanks for making time this morning."],
    [2, 6, "SPEAKER_00", "Let's go through the plan for the community garden project."],
    [6, 8, "SPEAKER_00", "Then agree who does what."],
    [8, 10, "SPEAKER_01", "Sounds good."],
    [10, 13, "SPEAKER_01", "The main thing from last week is the budget."],
    [13, 19, "SPEAKER_01", "We have quotes for the raised beds and the water tank, and both came in under budget."],
    [19, 24, "SPEAKER_00", "That's great news. How far under are we?"],
    [24, 26, "SPEAKER_01", "About 12% overall."],
    [26, 33, "SPEAKER_01", "The water tank was the bigger saving, because the supplier included delivery and installation."],
    [33, 39, "SPEAKER_00", "Then I suggest we keep that margin as a contingency rather than spending it straight away.", true],
    [39, 42, "SPEAKER_00", "Weather delays could push the timeline out."],
    [42, 44, "SPEAKER_01", "Agreed."],
    [44, 45, "SPEAKER_01", "On the timeline."],
    [45, 52, "SPEAKER_01", "The beds can go in during the first week of next month."],
    [52, 56, "SPEAKER_00", "Who is organising the volunteers for the build day?"],
    [72, 79, "SPEAKER_01", "We should check the insurance cover for volunteers before the build day."],
    [79, 84, "SPEAKER_00", "I'll ask about that this week and report back at our next catch-up."],
  ];
  const NAMES = { SPEAKER_00: "Alex", SPEAKER_01: "Sam" };

  function meeting(over) {
    return {
      id: "m-garden", title: "Community garden planning (sample)", created_at: at(1, 57),
      stage: "review", paused: false, error: null, busy: null, progress: 100,
      audio_available: true, audio_removed_at: null, reviewed_at: null, final_saved_at: null,
      model: "parakeet-tdt-0.6b-v2", edits: 1, can_undo: true, older_versions: [],
      speakers: { status: "done", error: null, device: "cpu", count: null, started_at: at(2, 5) },
      holds: [], cleanup: [], limits: LIMITS, duration_s: 1085, speaker_count: 2, mode: "quiet",
      ...over,
    };
  }

  const data = {
    setup: { onboarded: true, speech_ready: true },
    meetings: [
      { id: "m-garden", title: "Community garden planning (sample)", age_days: 0, action: "Review the transcript",
        held: false, progress: 100, paused: false, error: null, busy: null, stage: "review",
        duration_s: 1085, speaker_count: 2, model: "parakeet-tdt-0.6b-v2", mode: "quiet" },
      { id: "m-quarterly", title: "Quarterly planning (sample)", age_days: 1,
        action: "Hand off, then confirm the final document is saved", held: false, progress: 100,
        paused: false, error: null, busy: null, stage: "handoff",
        duration_s: 2520, speaker_count: 4, model: "parakeet-tdt-0.6b-v2", mode: "quiet" },
      { id: "m-catchup", title: "Team catch-up (sample)", age_days: 0, action: "Transcribing",
        held: false, progress: 46, paused: false, error: null, busy: "transcribing", stage: "inbox",
        duration_s: 1500, speaker_count: null, model: "parakeet-tdt-0.6b-v2", mode: "quiet" },
    ],
    detail: { "m-garden": meeting({}) },
    transcript: { "m-garden": GARDEN },
    recording: { enabled: true, recording: null, reports: [] },
    status: { busy: null, message: "", import_errors: [], importing: 0 },
    reminders: { holds_due: [], awaiting_confirmation: [] },
  };

  // -- scenarios -----------------------------------------------------------------------
  // Each changes the data and lists steps to run once the page is ready.
  const SCENARIOS = {
    inbox: { steps: [] },
  };
  const name = location.hash.slice(1) || "inbox";
  const scenario = SCENARIOS[name] || { steps: [] };
  if (scenario.change) scenario.change(data);

  // -- the bridge ----------------------------------------------------------------------
  function transcriptPage(mid, start, count) {
    const rows = (data.transcript[mid] || []).map(([t, end, label, text, edited], i) => ({
      id: i + 1, t, end, text, edited: !!edited, label, who: NAMES[label] || null, overlap: false }));
    return { total: rows.length, start, items: rows.slice(start, start + count) };
  }
  const handlers = {
    setup_state: () => ({ cards: [], token_remembered: false, earlier_token: false,
                          speakers: true, ...data.setup }),
    status: () => data.status,
    reminders: () => data.reminders,
    meetings: () => data.meetings,
    recording_status: () => data.recording,
    microphone_devices: () => [{ index: 1, name: "Laptop microphone", default: true }],
    detail: (mid) => data.detail[mid],
    transcript: (mid, start = 0, count = 500) => transcriptPage(mid, start, count),
    search: () => [],
    receipts: () => [],
    zoom: () => null,
  };
  const api = new Proxy({}, {
    get: (_, method) => async (...args) => {
      const fn = handlers[method];
      if (!fn) return { ok: false, error: `The design check has no sample data for ${String(method)}.` };
      return { ok: true, value: fn(...args) };
    },
  });
  window.pywebview = { api };

  // Tell app.js the bridge is ready, then run the scenario's steps in order.
  window.addEventListener("load", () => {
    window.dispatchEvent(new Event("pywebviewready"));
    let chain = new Promise((r) => setTimeout(r, 300));
    for (const step of scenario.steps) chain = chain.then(step).then(() => new Promise((r) => setTimeout(r, 300)));
    chain.then(() => { document.documentElement.dataset.ready = "1"; });
  });
})();
