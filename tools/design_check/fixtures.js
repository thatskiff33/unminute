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
    [42, 52, "SPEAKER_01", "Agreed. The beds can go in during the first week of next month, within budget."],
    [52, 56, "SPEAKER_00", "Who is organising the volunteers for the build day?"],
    [72, 79, "SPEAKER_01", "We should check the insurance cover for volunteers before the build day."],
    [79, 84, "SPEAKER_00", "I'll ask about that this week and report back at our next catch-up."],
  ];
  const NAMES = { SPEAKER_00: "Alex", SPEAKER_01: "Sam" };
  const SPEAKERS = [{ label: "SPEAKER_00", name: "Alex", passages: 15, seconds: 520 },
                    { label: "SPEAKER_01", name: "Sam", passages: 17, seconds: 480 }];
  // Silence of a given length, as the bridge's audio clips are data URLs.
  function silence(seconds) {
    const rate = 8000, n = Math.max(1, Math.round(seconds * rate));
    const buf = new Uint8Array(44 + n), v = new DataView(buf.buffer);
    const tag = (o, s) => { for (let i = 0; i < s.length; i++) buf[o + i] = s.charCodeAt(i); };
    tag(0, "RIFF"); v.setUint32(4, 36 + n, true); tag(8, "WAVE"); tag(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate, true); v.setUint16(32, 1, true);
    v.setUint16(34, 8, true); tag(36, "data"); v.setUint32(40, n, true); buf.fill(128, 44);
    let s = "";
    for (let i = 0; i < buf.length; i += 8192) s += String.fromCharCode(...buf.subarray(i, i + 8192));
    return `data:audio/wav;base64,${btoa(s)}`;
  }

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
        duration_s: 1085, speaker_count: 2, model: "parakeet-tdt-0.6b-v2", audio_available: true, cleanup: [] },
      { id: "m-quarterly", title: "Quarterly planning (sample)", age_days: 1,
        action: "Hand off, then confirm the final document is saved", held: false, progress: 100,
        paused: false, error: null, busy: null, stage: "handoff",
        duration_s: 2520, speaker_count: 4, model: "parakeet-tdt-0.6b-v2", audio_available: true, cleanup: [] },
      { id: "m-catchup", title: "Team catch-up (sample)", age_days: 0, action: "Transcribing",
        held: false, progress: 46, paused: false, error: null, busy: "transcribe", stage: "inbox",
        duration_s: 1500, speaker_count: null, model: "parakeet-tdt-0.6b-v2", audio_available: true, cleanup: [] },
    ],
    detail: { "m-garden": meeting({}) },
    transcript: { "m-garden": GARDEN },
    recording: { enabled: true, recording: null, reports: [] },
    status: { busy: null, message: "", import_errors: [], importing: 0, mode: "quiet" },
    reminders: { holds_due: [], awaiting_confirmation: [] },
  };

  // -- scenarios -----------------------------------------------------------------------
  // Each changes the data and lists steps to run once the page is ready.
  const row = (over) => ({ held: false, progress: 100, paused: false, error: null, busy: null,
    duration_s: 1500, speaker_count: null, model: "parakeet-tdt-0.6b-v2", audio_available: true,
    cleanup: [], age_days: 0, action: "", ...over });
  // Board heights of the level meter's bars, as microphone levels in dB.
  const LEVELS = [18, 30, 44, 26, 52, 60, 38, 22, 48, 64, 56, 34, 20, 40, 58, 46, 28, 16, 36, 54,
                  62, 44, 30, 24, 42, 50, 38, 26, 18, 14].map((h) => ((h - 8) / 56) * 60 - 60);
  let levelAt = 0;
  const show = (view) => () => window.workspace.show(view);
  const SCENARIOS = {
    home: { steps: [] },
    recording: {
      change: (d) => {
        d.recording.recording = { state: "recording", seconds: 767, level_db: -60,
                                  device: "Laptop microphone", events: [] };
        d.levels = true;
      },
      steps: [],
    },
    "home-empty": { change: (d) => { d.meetings = []; }, steps: [] },
    "home-drop": {
      steps: [() => {
        const drag = () => {
          const dt = new DataTransfer();
          dt.items.add(new File(["x"], "meeting.wav", { type: "audio/wav" }));
          document.dispatchEvent(new DragEvent("dragover", { dataTransfer: dt, bubbles: true }));
        };
        drag();
        setInterval(drag, 100);
      }],
    },
    "home-states": {
      change: (d) => {
        d.reminders.holds_due = [{ id: "h1", meeting: "m-quarterly", title: "Quarterly planning (sample)",
                                   reason: "Waiting for the board's approval" }];
        d.status.import_errors = ["notes.docx could not be imported: it is not an audio or video file."];
        d.meetings = [
          row({ id: "m-site", title: "Site visit notes (sample)", stage: "inbox", progress: 0,
                error: "Could not be transcribed: the recording could not be read." }),
          row({ id: "m-catchup", title: "Team catch-up (sample)", stage: "inbox", progress: 46, busy: "transcribe" }),
          row({ id: "m-client", title: "Client call (sample)", stage: "inbox", progress: 20, paused: true }),
          row({ id: "m-workshop", title: "Workshop (sample)", stage: "inbox", progress: 0 }),
          row({ id: "m-planning", title: "Planning session (sample)", stage: "review", busy: "speakers" }),
          row({ id: "m-garden", title: "Community garden planning (sample)", stage: "review",
                duration_s: 1085, speaker_count: 2 }),
          row({ id: "m-quarterly", title: "Quarterly planning (sample)", stage: "handoff", age_days: 1,
                duration_s: 2520, speaker_count: 4, held: true, audio_available: false }),
          row({ id: "m-board", title: "Board meeting (sample)", stage: "cleanup", age_days: 3,
                cleanup: [{ id: "j1", phase: "transcript", state: "pending", note: null }] }),
          row({ id: "m-strategy", title: "Strategy day (sample)", stage: "cleanup", age_days: 3,
                cleanup: [{ id: "j2", phase: "transcript", state: "failed",
                            note: "a file is in use. Close it and try again." }] }),
        ];
      },
      steps: [],
    },
    help: { steps: [show("help")] },
    // Meeting review: playing from 0:24, so the passage is highlighted. The
    // slowest speed keeps the clock at 0:24 while the page is captured.
    meeting: { steps: openAndPlay() },
    "meeting-transcribing": {
      change: (d) => {
        d.detail["m-garden"] = meeting({ stage: "inbox", progress: 46, busy: "transcribe", speakers: null,
                                         speaker_count: null, edits: 0, can_undo: false });
        d.transcript["m-garden"] = GARDEN.slice(0, 6).map(([a, b, , text]) => [a, b, null, text]);
      },
      steps: [openGarden],
    },
    "meeting-edit": {
      steps: [...openAndPlay(), () => {
        const box = document.getElementById("m-search");
        box.value = "budget";
        box.dispatchEvent(new Event("input"));
      }, wait(400), () => document.getElementById("m-next").click(),
      () => document.querySelector('#transcript li[data-id="8"] .who:not(.again)').click(), wait(300),
      () => document.querySelector('#transcript li[data-id="10"] .text').click(),
      () => window.workspace.say("Saved")],
    },
    "meeting-reviewed": {
      change: (d) => {
        d.detail["m-garden"] = meeting({ stage: "handoff", reviewed_at: at(15, 12), audio_available: false,
                                         audio_removed_at: at(15, 12), can_undo: false });
      },
      steps: [openGarden],
    },
    "meeting-earlier": {
      change: (d) => {
        d.detail["m-garden"] = meeting({ older_versions: [{ index: 0, model: "parakeet-tdt-0.6b-v3",
                                                            created_at: at(1, 20), edits: 2 }] });
      },
      steps: [...openAndPlay(), () => document.querySelector('[role=tab][data-tab="versions"]').click(),
              () => document.querySelector("#m-versions button.ver-card").click(), wait(400),
              () => document.querySelector('.wide-tabs [role=tab][data-tab="speakers"]').click()],
    },
  };
  function wait(ms) { return () => new Promise((r) => setTimeout(r, ms)); }
  function openGarden() { return window.workspace.openMeeting("m-garden").then(wait(800)); }
  function openAndPlay() {
    return [openGarden, () => { window.workspace.play(24, 26); }, wait(500),
            () => { document.getElementById("player").playbackRate = 0.0625; }];
  }
  const name = location.hash.slice(1) || "home";
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
    recording_status: () => {
      // While "recording", each poll reports the next of the board's levels.
      if (data.levels && data.recording.recording)
        data.recording.recording.level_db = LEVELS[Math.min(levelAt++, LEVELS.length - 1)];
      return data.recording;
    },
    microphone_devices: () => [{ index: 1, name: "Laptop microphone", default: true }],
    detail: (mid) => data.detail[mid],
    transcript: (mid, start = 0, count = 500) => transcriptPage(mid, start, count),
    search: (mid, q) => transcriptPage(mid, 0, 1000).items
      .filter((it) => it.text.toLowerCase().includes(String(q).toLowerCase())).map((it) => it.id),
    speakers: () => ({ speakers: SPEAKERS, can_undo: false }),
    speaker_examples: (mid, label) => ({ total: 3, items: transcriptPage(mid, 0, 1000).items
      .filter((it) => it.label === label).slice(0, 3).map((it) => ({ id: it.id, start: it.t, end: it.end, text: it.text })) }),
    clip: (mid, start, end) => silence(end - start),
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
    let chain = new Promise((r) => setTimeout(r, 600));
    for (const step of scenario.steps) chain = chain.then(step).then(() => new Promise((r) => setTimeout(r, 300)));
    chain.then(() => { document.documentElement.dataset.ready = "1"; });
  });
})();
