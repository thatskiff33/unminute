// tools/design_check/fixtures.js
// A fake pywebview bridge for tools/design_check.py: the real page (workspace/ui)
// runs against fixed sample data, so each screen can be compared with its
// design board. Loaded before app.js only by the check's local server; never
// shipped. The scenario is chosen by the URL hash, e.g. index.html#meeting.
// All names and text are the fictional sample meeting used on the boards.
"use strict";
(() => {
  // -- perf mode (tools/ui_idle_check.py) ------------------------------------------
  // index.html?perf=visible|hidden&ms=60000 counts what the idle page does over
  // ms of page time: bridge calls per method, timers, animation frames,
  // meeting-list rebuilds and page errors. Timers are wrapped here, before
  // app.js loads. "hidden" starts visible and then becomes hidden, with the same
  // bubbling visibilitychange a minimised window gets.
  const query = new URLSearchParams(location.search);
  const perfMode = query.get("perf");
  // pendingFrames: animation frames requested and not yet run. Headless Edge
  // runs almost no frames while virtual time skips ahead, so frames run are no
  // measure; an animation loop always leaves one frame pending.
  const perf = { calls: {}, timeouts: 0, rebuilds: 0, errors: [],
                 intervals: new Map(), pendingFrames: new Set() };
  const realSetTimeout = window.setTimeout.bind(window);
  let pageHidden = false;
  let afterNextList = null;      // perf: run once the page next fetches the meeting list
  // -- early-error mode (tools/ui_idle_check.py, #171) ------------------------------
  // index.html?early=1 throws twice before the bridge is ready, then makes it
  // ready. The page must keep the first error and report it once the bridge is
  // ready, as its kind, line and column only. This listener is added before
  // app.js's, so it sees each error first.
  const early = query.get("early") === "1";
  const earlyLog = { thrown: [], thrown_after_ready: 0, reports: [], ready: false };
  if (early) {
    window.addEventListener("error", (e) => {
      if (earlyLog.ready) earlyLog.thrown_after_ready++;
      else earlyLog.thrown.push([e.lineno, e.colno]);
    });
  }
  if (perfMode) {
    const realSetInterval = window.setInterval.bind(window);
    const realClearInterval = window.clearInterval.bind(window);
    const realClearTimeout = window.clearTimeout.bind(window);
    const realFrame = window.requestAnimationFrame.bind(window);
    window.setInterval = (fn, ms, ...rest) => {
      const id = realSetInterval(fn, ms, ...rest);
      perf.intervals.set(id, Number(ms) || 0);
      return id;
    };
    // Either clear function stops either kind of timer.
    window.clearInterval = (id) => { perf.intervals.delete(id); realClearInterval(id); };
    window.clearTimeout = (id) => { perf.intervals.delete(id); realClearTimeout(id); };
    window.setTimeout = (fn, ms, ...rest) => { perf.timeouts++; return realSetTimeout(fn, ms, ...rest); };
    const realCancelFrame = window.cancelAnimationFrame.bind(window);
    window.requestAnimationFrame = (fn) => {
      const id = realFrame((t) => { perf.pendingFrames.delete(id); fn(t); });
      perf.pendingFrames.add(id);
      return id;
    };
    window.cancelAnimationFrame = (id) => { perf.pendingFrames.delete(id); realCancelFrame(id); };
    Object.defineProperty(document, "hidden", { configurable: true, get: () => pageHidden });
    Object.defineProperty(document, "visibilityState", { configurable: true,
                                                          get: () => (pageHidden ? "hidden" : "visible") });
    // A page that throws may stop polling and look idle: errors fail the check.
    window.addEventListener("error", (e) => { perf.errors.push(String(e.message || e)); });
    window.addEventListener("unhandledrejection", (e) => { perf.errors.push(String(e.reason)); });
  }
  function perfMeasure() {
    if (perfMode === "transcribing") { growMeasure(); return; }
    const ms = Number(query.get("ms")) || 60000;
    const groups = document.getElementById("groups");
    if (groups) {
      new MutationObserver((records) => { perf.rebuilds += records.length; })
        .observe(groups, { childList: true });
    }
    // Count from here, before the page becomes hidden, so work it does on the
    // visibilitychange itself counts too.
    perf.calls = {}; perf.timeouts = 0; perf.rebuilds = 0;
    if (perfMode === "hidden") {
      pageHidden = true;
      document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
    }
    realSetTimeout(() => {
      const seen = snapshot();
      if (perfMode !== "hidden") {
        // Then a meeting changes (as when an import finishes), just after the
        // page has fetched the list: only the reported change can make it
        // fetch again, and redraw, within the next poll (11 s), not the
        // every-6th-poll refresh. Not part of the idle figures.
        let done = false;
        const end = (redrawn) => { if (!done) { done = true; finish(ms, seen, null, redrawn); } };
        afterNextList = () => {
          const rebuilt = perf.rebuilds;
          data.meetings = data.meetings.map((m, i) => (i ? m : { ...m, title: `${m.title} (2)` }));
          realSetTimeout(() => end(perf.rebuilds > rebuilt), 11000);
        };
        return realSetTimeout(() => end(false), 75000);   // the page never fetched it
      }
      // Shown again: the page must catch up by itself, promptly. Counted
      // separately, so the hidden figures above stay what the hidden page did.
      const before = { ...perf.calls };
      pageHidden = false;
      document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
      realSetTimeout(() => finish(ms, seen, Object.keys(perf.calls).filter(
        (k) => perf.calls[k] > (before[k] || 0))), 1000);
    }, ms);
  }
  function total(calls) { return Object.values(calls).reduce((a, b) => a + b, 0); }
  function snapshot() {
    return { calls: { ...perf.calls }, timeouts: perf.timeouts, rebuilds: perf.rebuilds,
             intervals: [...perf.intervals.values()], frames: perf.pendingFrames.size,
             rows: document.querySelectorAll("#groups li").length };
  }
  function finish(ms, seen, catchUp, redrawn = null) {
    const perMinute = (n) => Math.round(n * 60000 / ms * 10) / 10;
    const calls = Object.fromEntries(Object.entries(seen.calls).map(([k, v]) => [k, perMinute(v)]));
    const result = {
      mode: perfMode, page_ms: ms, calls_per_minute: calls,
      bridge_calls_per_minute: perMinute(total(seen.calls)),
      timeouts_per_minute: perMinute(seen.timeouts),
      pending_animation_frames: seen.frames,
      list_rebuilds_per_minute: perMinute(seen.rebuilds),
      live_intervals: seen.intervals.length,
      fastest_interval_ms: seen.intervals.length ? Math.min(...seen.intervals) : null,
      meeting_rows: seen.rows,
      catch_up_calls: catchUp,           // hidden: the methods called once shown again
      redrawn_after_change: redrawn,     // visible: the list redrawn within a poll of a change
      page_errors: perf.errors,
    };
    const out = document.createElement("pre");
    out.id = "perf-result";
    out.textContent = JSON.stringify(result);
    document.body.append(out);
  }

  // -- transcribing mode (tools/ui_idle_check.py, #127) -----------------------------
  // index.html?perf=transcribing#meeting-transcribing-long: a long meeting open
  // while it is transcribed. Every GROW_STEP_MS of page time a step changes the
  // data as transcription would (more passages, an earlier chunk done again,
  // speakers identified) or as a person would (an edit, a search). The page
  // polls every 2 s, so it sees each step at least once before the next. What
  // the page fetched and drew is counted; at the end the rows shown are checked
  // against the data: each passage once, in order, speaker turns right.
  const GROW_STEP_MS = 4000;
  const GROW_LONG = 3000;          // passages before the measured window
  const grow = { added: 0, redraws: 0, rowsSent: 0, redrawSteps: [], editId: 5,
                 editText: "Edited while transcribing (sample).", editHeld: null, editOnLoad: false };
  // Start editing a passage from inside a transcript call, so the page's
  // append is still waiting on that call when the edit begins: it must give up
  // without drawing anything or fetching the whole list (a redraw it would
  // throw away), and append the rows once the edit ends.
  function growEdit() {
    grow.editOnLoad = false;
    const span = document.querySelector(`#transcript .text[data-id="${grow.editId}"]`);
    span.click();
    span.textContent = grow.editText;
    span.dispatchEvent(new Event("input"));
    grow.addedAtEdit = grow.added;
  }
  const LABELS = ["SPEAKER_00", "SPEAKER_01"];
  function growRow(i, t, label = null) {
    return [t, t + 2.5, label, `Passage ${i} about the plan (sample).`, false, i];
  }
  function growMore(n, labels = null) {
    const rows = data.transcript["m-garden"];
    const next = Math.max(...rows.map((r) => r[5])) + 1;
    const t = Math.max(...rows.map((r) => r[0])) + 3;
    for (let k = 0; k < n; k++) rows.push(growRow(next + k, t + 3 * k, labels ? labels[k] : null));
    growProgress();
  }
  function growProgress() {
    const d = data.detail["m-garden"];
    data.detail["m-garden"] = { ...d, progress: d.progress + 1 };
  }
  function growSort() {
    data.transcript["m-garden"].sort((a, b) => a[0] - b[0] || a[5] - b[5]);
  }
  const otherLabel = (label) => (label === LABELS[0] ? LABELS[1] : LABELS[0]);
  const lastLabel = () => data.transcript["m-garden"].at(-1)[2];
  const searchFor = (text) => {
    const box = document.getElementById("m-search");
    box.value = text;
    box.dispatchEvent(new Event("input"));
  };
  // Step n (from 1) runs GROW_STEPS[n - 1]; a redraw the page made between
  // step n and step n + 1 is put down to step n.
  const GROW_STEPS = [
    () => growMore(10),
    () => growMore(10),
    () => {                          // an earlier chunk done again: passages land mid-list
      const rows = data.transcript["m-garden"];
      const next = Math.max(...rows.map((r) => r[5])) + 1;
      for (let k = 0; k < 5; k++) rows.push(growRow(next + k, 3 * (200 + k) + 1.5));
      growSort();
      growProgress();
    },
    () => growMore(10),
    () => {                          // more passages; an edit starts while the page loads them
      grow.editOnLoad = true;
      growMore(10);
    },
    () => {                          // nothing was drawn under the edit; it ends
      grow.editHeld = grow.added === grow.addedAtEdit;
      document.activeElement.blur();
      growMore(10);
    },
    () => {                          // speakers identified: every passage labelled
      data.transcript["m-garden"].forEach((r, i) => { r[2] = LABELS[Math.floor(i / 4) % 2]; });
      const d = data.detail["m-garden"];
      data.detail["m-garden"] = { ...d, speakers: { status: "done", error: null, device: "cpu",
                                                    count: null, started_at: at(14, 20) },
                                  speaker_count: 2 };
    },
    () => {                          // the next passages carry on the last speaker's turn
      const l = lastLabel(), o = otherLabel(l);
      growMore(10, [l, l, o, o, o, l, l, o, l, o]);
    },
    () => {                          // ...or start a new turn
      const o = otherLabel(lastLabel());
      growMore(10, [o, o, otherLabel(o), o, o, o, otherLabel(o), otherLabel(o), o, o]);
    },
    () => { searchFor("plan"); growMore(10, Array(10).fill(lastLabel())); },
    () => { searchFor(""); growMore(10, Array(10).fill(otherLabel(lastLabel()))); },
    () => growMore(10, Array(10).fill(lastLabel())),
  ];
  // The steps after which the page must redraw the whole list: an earlier
  // chunk, speakers identified, and more passages while a search is open.
  const GROW_REDRAW_STEPS = [3, 7, 10];

  function growMeasure() {
    const list = document.getElementById("transcript");
    new MutationObserver((records) => {
      for (const r of records) {
        grow.added += r.addedNodes.length;
        if (r.removedNodes.length) grow.redraws++;
      }
    }).observe(list, { childList: true });
    perf.calls = {}; perf.timeouts = 0; grow.rowsSent = 0;
    let step = 0, redraws = 0;
    const tick = () => {
      if (step && grow.redraws > redraws) grow.redrawSteps.push(step);
      redraws = grow.redraws;
      if (step === GROW_STEPS.length) { growFinish(); return; }
      GROW_STEPS[step++]();
      realSetTimeout(tick, GROW_STEP_MS);
    };
    realSetTimeout(tick, GROW_STEP_MS);
  }

  function growFinish() {
    const rows = data.transcript["m-garden"];
    const shown = [...document.querySelectorAll("#transcript > li")];
    const ids = shown.map((li) => Number(li.dataset.id));
    // A passage starts a turn when its speaker differs from the one before.
    const turns = rows.map((r, i) => !!r[2] && (i === 0 || r[2] !== rows[i - 1][2]));
    const edited = document.querySelector(`#transcript .text[data-id="${grow.editId}"]`);
    const result = {
      mode: perfMode, steps: GROW_STEPS.length, step_ms: GROW_STEP_MS,
      calls: { ...perf.calls },
      transcript_calls: perf.calls.transcript || 0,
      rows_fetched: grow.rowsSent,
      rows_drawn: grow.added,
      full_redraws: grow.redraws,
      redraw_steps: grow.redrawSteps,
      expected_redraw_steps: GROW_REDRAW_STEPS,
      rows_shown: ids.length,
      rows_expected: rows.length,
      each_once_in_order: ids.length === rows.length && ids.every((id, i) => id === rows[i][5]),
      turns_right: shown.length === rows.length &&
                   shown.every((li, i) => li.classList.contains("turn") === turns[i]),
      edit_held_back: grow.editHeld,
      edit_kept: !!edited && edited.textContent === grow.editText,
      page_errors: perf.errors,
    };
    const out = document.createElement("pre");
    out.id = "perf-result";
    out.textContent = JSON.stringify(result);
    document.body.append(out);
  }

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
  const EXAMPLES = {
    SPEAKER_00: [{ start: 0, end: 2, text: "Thanks for making time this morning." },
                 { start: 52, end: 56, text: "Who is organising the volunteers for the build day?" },
                 { start: 79, end: 84, text: "I'll ask about that this week and report back at our next catch-up." }],
    SPEAKER_01: [{ start: 10, end: 13, text: "The main thing from last week is the budget." },
                 { start: 26, end: 28, text: "The water tank was the bigger saving." },
                 { start: 72, end: 79, text: "We should check the insurance cover for volunteers before the build day." }],
  };
  const card = (over) => ({ state: "ready", status: "Downloaded and verified.", licence: "CC BY 4.0",
    source: "NVIDIA, ONNX export", revision: "0bbb45a", removable: true, needs_token: false, recommended: false,
    download: null, ...over });
  const CARDS = [
    card({ name: "parakeet-tdt-0.6b-v2", role: "speech", recommended: true, size_gb: 0.66,
           label: "Parakeet v2", title: "Speech to text: Parakeet v2 (recommended)",
           about: "Fast and accurate for English. Runs on this computer's processor." }),
    card({ name: "parakeet-tdt-0.6b-v3", role: "speech", size_gb: 0.67, label: "Parakeet v3", title: "Speech to text: Parakeet v3",
           about: "Newer multilingual version. In our tests it was less accurate for English." }),
    card({ name: "large-v3-turbo", role: "speech", size_gb: 1.62, state: "missing", status: "Not downloaded.",
           removable: false, label: "Whisper large-v3 turbo", title: "Speech to text: Whisper large-v3 turbo", about: "Alternative engine; slower on a processor." }),
    card({ name: "pyannote/speaker-diarization-community-1", role: "speakers", recommended: true, size_gb: 0.03,
           needs_token: true, label: "Speaker identification", title: "Speaker identification (optional)",
           about: "Works out who spoke when. Downloading it needs a free Hugging Face account and token." }),
    card({ name: "gpu-component", role: "gpu", size_gb: 1.92, state: "missing", status: "Not downloaded.",
           removable: false, label: "Graphics card acceleration", title: "Graphics card acceleration (optional)",
           about: "Speeds up speaker identification on this computer's NVIDIA RTX 500. Used only in Faster mode." }),
  ];
  const SETTINGS = {
    mode: "quiet", battery_override: false, speakers: true, gpu_speakers: false, microphone: true,
    model: "parakeet-tdt-0.6b-v2", onboarded: true,
    choices: [{ name: "parakeet-tdt-0.6b-v2", label: "Parakeet v2", recommended: true },
              { name: "parakeet-tdt-0.6b-v3", label: "Parakeet v3", recommended: false },
              { name: "large-v3-turbo", label: "Whisper large-v3 turbo", recommended: false }],
    gpu: { card: "NVIDIA RTX 500", driver_ok: true, min_driver: "580", component: "ready" },
    deployment: { mode: "self-service", version: "0.3.0" },
    update_blockers: ["Community garden planning (sample): in review", "Team catch-up (sample): transcribing"],
    repaired: { keys: [], all: false },
  };
  const TEMPLATE = { id: "minutes", title: "Meeting minutes (sample)", version: 1, edited: false, builtin: true,
                     prompt: "[Your prompt wording. Edits are saved as a new version; the original wording can always be restored.]" };
  function handoffPreview(anonymise) {
    const opts = data.options || { omitted: [13], redactions: ["water tank"] };
    const who = (label) => (anonymise ? { SPEAKER_00: "Speaker 1", SPEAKER_01: "Speaker 2" } : NAMES)[label];
    const lines = GARDEN.slice(0, 7).map(([t, , label, text]) =>
      `[${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}] ${who(label)}: ${text.replace("water tank", "[redacted]")}`);
    const combined = "[Template prompt appears here: the wording saved in the selected template]\n\n" +
      "## Transcript\nCommunity garden planning (sample)\n\n" + lines.join("\n");
    return { combined, prompt_only: "[prompt]", markdown: "[markdown]", chars: combined.length, long: false,
             omitted: opts.omitted.length, redactions: opts.redactions.length, anonymised: anonymise,
             digests: { combined: "a", prompt_only: "b", markdown: "c" },
             template: { id: "minutes", title: TEMPLATE.title, version: 1 }, filename: "x.md", options: opts };
  }
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
    const m = {
      id: "m-garden", title: "Community garden planning (sample)", created_at: at(1, 57),
      stage: "review", paused: false, error: null, busy: null, progress: 100,
      audio_available: true, audio_removed_at: null, reviewed_at: null, final_saved_at: null,
      model: "parakeet-tdt-0.6b-v2", model_label: "Parakeet v2", edits: 1, can_undo: true, older_versions: [],
      speakers: { status: "done", error: null, device: "cpu", count: null, started_at: at(2, 5) },
      holds: [], cleanup: [], limits: LIMITS, duration_s: 1085, speaker_count: 2, mode: "quiet",
      ...over,
    };
    // As the app reports it: Skip applies until speakers are labelled or skipped.
    if (!("can_skip" in m))
      m.can_skip = !m.reviewed_at && !(m.speakers && ["done", "skipped"].includes(m.speakers.status));
    return m;
  }

  const data = {
    setup: { onboarded: true, speech_ready: true },
    cards: CARDS,
    settings: SETTINGS,
    meetings: [
      { id: "m-garden", title: "Community garden planning (sample)", age_days: 0, action: "Review the transcript",
        held: false, progress: 100, paused: false, error: null, busy: null, stage: "review",
        duration_s: 1085, speaker_count: 2, model: "parakeet-tdt-0.6b-v2", model_label: "Parakeet v2", audio_available: true, cleanup: [] },
      { id: "m-quarterly", title: "Quarterly planning (sample)", age_days: 1,
        action: "Hand off, then confirm the final document is saved", held: false, progress: 100,
        paused: false, error: null, busy: null, stage: "handoff",
        duration_s: 2520, speaker_count: 4, model: "parakeet-tdt-0.6b-v2", model_label: "Parakeet v2", audio_available: true, cleanup: [] },
      { id: "m-catchup", title: "Team catch-up (sample)", age_days: 0, action: "Transcribing",
        held: false, progress: 46, paused: false, error: null, busy: "transcribe", stage: "inbox",
        duration_s: 1500, speaker_count: null, model: "parakeet-tdt-0.6b-v2", model_label: "Parakeet v2", audio_available: true, cleanup: [] },
    ],
    detail: { "m-garden": meeting({}) },
    transcript: { "m-garden": GARDEN },
    recording: { enabled: true, display: "flow", recording: null, reports: [] },
    status: { busy: null, message: "", import_errors: [], importing: 0, mode: "quiet", integrity: null },
    reminders: { holds_due: [], awaiting_confirmation: [] },
  };

  // -- scenarios -----------------------------------------------------------------------
  // Each changes the data and lists steps to run once the page is ready.
  const row = (over) => ({ held: false, progress: 100, paused: false, error: null, busy: null,
    duration_s: 1500, speaker_count: null, model: "parakeet-tdt-0.6b-v2", model_label: "Parakeet v2", audio_available: true,
    cleanup: [], age_days: 0, action: "", can_skip: true, ...over });
  // Board heights of the level meter's bars, as microphone levels in dB.
  const LEVELS = [18, 30, 44, 26, 52, 60, 38, 22, 48, 64, 56, 34, 20, 40, 58, 46, 28, 16, 36, 54,
                  62, 44, 30, 24, 42, 50, 38, 26, 18, 14]
    // sample speech levels (dBFS), from the board's bar heights
    .map((h) => Math.pow(Math.max(0, h - 6) / 58, 1 / 0.8) * 47 - 55);
  // Display data like the recorder's for one block of a voice at `db`: a few
  // harmonics of 160 Hz across the bands, and a short slice of that wave.
  function sampleViz(db, n) {
    const bands = Array.from({ length: 40 }, (_, i) => {
      const hz = 80 * Math.pow(100, i / 39);
      const harmonic = Math.abs(Math.sin(Math.PI * hz / 160));
      return Math.round((db - 6 - 18 * Math.log2(hz / 160 + 1) - 14 * harmonic) * 10) / 10;
    });
    const a = Math.pow(10, db / 20) * 1.4;
    const wave = Array.from({ length: 128 }, (_, i) => {
      const t = (i + n * 37) / 48;
      return Math.round(a * (Math.sin(t) + 0.5 * Math.sin(2.7 * t) + 0.25 * Math.sin(5.3 * t)) * 1000) / 1000;
    });
    return { bands, pitch_hz: 160, wave };
  }
  let levelAt = 0;
  const show = (view) => () => window.workspace.show(view);
  const SCENARIOS = {
    home: { steps: [] },
    "recording-spectrum": {
      change: (d) => { SCENARIOS.recording.change(d); d.recording.display = "spectrum"; },
      steps: [],
    },
    "recording-wave": {
      change: (d) => { SCENARIOS.recording.change(d); d.recording.display = "wave"; },
      steps: [],
    },
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
    // Transcribing mode (tools/ui_idle_check.py): a long meeting, still transcribing.
    "meeting-transcribing-long": {
      change: (d) => {
        d.detail["m-garden"] = meeting({ stage: "inbox", progress: 40, busy: "transcribe", speakers: null,
                                         speaker_count: null, edits: 0, can_undo: false });
        d.transcript["m-garden"] = Array.from({ length: GROW_LONG }, (_, i) => growRow(i + 1, 3 * i));
        d.status.busy = "transcribe";
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
    names: { steps: [...openAndPlay(), () => document.getElementById("m-names").click(), wait(600),
                     () => document.querySelector("#n-list input").focus()] },
    confirm: { steps: [...openAndPlay(), () => document.getElementById("m-primary").click()] },
    handoff: {
      change: reviewed,
      steps: [openGarden, () => document.getElementById("m-primary").click(), wait(800),
              () => document.getElementById("ho-anon").click(),
              () => document.getElementById("ho-apply").click(), wait(500)],
    },
    "handoff-template": {
      change: reviewed,
      steps: [openGarden, () => document.getElementById("m-primary").click(), wait(800),
              () => document.getElementById("ho-anon").click(),
              () => document.getElementById("ho-apply").click(), wait(500),
              () => document.getElementById("ho-edit").click(),
              () => document.activeElement.blur()],
    },
    "setup-1": { change: firstRun, steps: [] },
    "setup-2": {
      change: (d) => {
        firstRun(d);
        d.cards[0] = { ...d.cards[0], state: "missing", download: { state: "running", message: "Downloading 0.41 of 0.66 GB", percent: 62 } };
      },
      steps: [() => window.workspace.setStep(2)],
    },
    "setup-3": { change: firstRun, steps: [() => window.workspace.setStep(3)] },
    "setup-4": { change: (d) => { firstRun(d); d.micHeard = true; },
                 steps: [() => window.workspace.setStep(4), wait(500), () => document.getElementById("su-test").click()] },
    "setup-5": {
      change: (d) => { firstRun(d); d.settings.microphone = false; },
      steps: [() => window.workspace.setStep(4), wait(400), () => window.workspace.setStep(5)],
    },
    settings: {
      change: (d) => {
        d.folders = [{ id: "f1", path: "Documents\\Meeting recordings (sample)", active: true, last_scan_at: at(14, 28),
                       imported: 4, fetch_cloud: true, delete_originals: false }];
        d.receipts = [{ completed_at: at(9, 0) - DAY, result: "Audio and transcript removed (meeting 1)" },
                      { completed_at: at(9, 0) - 2 * DAY, result: "Audio removed after review (meeting 2)" }];
      },
      steps: [show("settings")],
    },
    downloads: { steps: [show("downloads")] },
    "meeting-earlier": {
      change: (d) => {
        d.detail["m-garden"] = meeting({ older_versions: [{ index: 0, model: "parakeet-tdt-0.6b-v3",
                                                            model_label: "Parakeet v3",
                                                            created_at: at(1, 20), edits: 2 }] });
      },
      steps: [...openAndPlay(), () => document.querySelector('[role=tab][data-tab="versions"]').click(),
              () => document.querySelector("#m-versions button.ver-card").click(), wait(400),
              () => document.querySelector('.wide-tabs [role=tab][data-tab="speakers"]').click()],
    },
  };
  function firstRun(d) {
    d.setup = { onboarded: false, speech_ready: true };
    d.cards = d.cards.map((c) => (c.role === "speakers" ? { ...c, state: "missing", status: "Not downloaded." } : c));
  }
  function reviewed(d) {
    d.detail["m-garden"] = meeting({ stage: "handoff", reviewed_at: at(15, 12), audio_available: false,
                                     audio_removed_at: at(15, 12), can_undo: false });
  }
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
  // A row's id is its sixth field where it has one (the transcribing mode's
  // passages keep theirs when others land before them), else its place.
  function transcriptPage(mid, start, count) {
    const rows = (data.transcript[mid] || []).map(([t, end, label, text, edited, id], i) => ({
      id: id || i + 1, t, end, text, edited: !!edited, label, who: NAMES[label] || null, overlap: false }));
    return { total: rows.length, start, items: rows.slice(start, start + count) };
  }
  const handlers = {
    setup_state: () => ({ cards: data.cards, token_remembered: false,
                          speakers: true, ...data.setup }),
    settings: () => data.settings,
    watched_folders: () => ({ folders: data.folders || [], last_scan: null, interval_minutes: 5 }),
    test_microphone: () => ({ heard: !!data.micHeard, peak_db: -18 }),
    // changes: as the app reports it, different whenever the lists are.
    status: () => ({ ...data.status, changes: JSON.stringify([data.meetings, data.reminders]) }),
    reminders: () => data.reminders,
    meetings: () => {
      const items = data.meetings;
      if (afterNextList) {                 // perf: change it once this answer is out
        realSetTimeout(afterNextList, 0);
        afterNextList = null;
      }
      return items;
    },
    report_ui_error: (...args) => {
      if (early) earlyLog.reports.push({ args, before_ready: !earlyLog.ready });
      return null;
    },
    continue_despite_damage: () => null,
    copy_diagnostics: () => ({ chars: 1200 }),
    recording_status: () => {
      // While "recording", each poll reports the next of the board's levels.
      if (data.levels && data.recording.recording) {
        const db = LEVELS[levelAt % LEVELS.length];
        data.recording.recording.level_db = db;
        data.recording.recording.viz = sampleViz(db, levelAt++);
      }
      return data.recording;
    },
    microphone_devices: () => [{ index: 1, name: "Laptop microphone", default: true }],
    detail: (mid) => data.detail[mid],
    transcript: (mid, start = 0, count = 500) => {
      const page = transcriptPage(mid, start, count);
      grow.rowsSent += page.items.length;
      if (grow.editOnLoad) growEdit();
      return page;
    },
    search: (mid, q) => transcriptPage(mid, 0, 1000).items
      .filter((it) => it.text.toLowerCase().includes(String(q).toLowerCase())).map((it) => it.id),
    speakers: () => ({ speakers: SPEAKERS, can_undo: false }),
    speaker_examples: (mid, label) => ({ total: 3, items: transcriptPage(mid, 0, 1000).items
      .filter((it) => it.label === label).slice(0, 3).map((it) => ({ id: it.id, start: it.t, end: it.end, text: it.text })) }),
    clip: (mid, start, end) => silence(end - start),
    speaker_panel: (mid) => ({ can_undo: false, speakers: SPEAKERS.map((sp) => ({ ...sp, offset: 0,
      examples: { total: 6, items: EXAMPLES[sp.label] } })) }),
    handoff_templates: () => [TEMPLATE],
    handoff_preview: (mid, tid, anonymise) => handoffPreview(!!anonymise),
    set_handoff_options: (mid, omitted, redactions) => { data.options = { omitted, redactions }; return null; },
    receipts: () => data.receipts || [],
    zoom: () => null,
  };
  if (perfMode === "transcribing") {
    handlers.edit = (mid, id, text) => {
      const row = data.transcript[mid].find((r) => r[5] === id);
      row[3] = text;
      row[4] = true;
      return { edited: true };
    };
  }
  const api = new Proxy({}, {
    get: (_, method) => async (...args) => {
      perf.calls[method] = (perf.calls[method] || 0) + 1;
      const fn = handlers[method];
      if (!fn) return { ok: false, error: `The design check has no sample data for ${String(method)}.` };
      return { ok: true, value: fn(...args) };
    },
  });
  window.pywebview = { api };

  // Tell app.js the bridge is ready, then run the scenario's steps in order.
  window.addEventListener("load", () => {
    if (early) {
      // Each thrown in its own task, so each reaches the page's error listener,
      // and from its own line: the check tells the first from the second by place.
      realSetTimeout(() => { throw new Error("first early error"); }, 0);
      realSetTimeout(() => { throw new Error("second early error"); }, 20);
      realSetTimeout(() => {
        earlyLog.ready = true;
        window.dispatchEvent(new Event("pywebviewready"));
      }, 40);
      realSetTimeout(() => {
        const out = document.createElement("pre");
        out.id = "early-result";
        out.textContent = JSON.stringify(earlyLog);
        document.body.append(out);
      }, 1500);
      return;
    }
    window.dispatchEvent(new Event("pywebviewready"));
    let chain = new Promise((r) => setTimeout(r, 600));
    for (const step of scenario.steps) chain = chain.then(step).then(() => new Promise((r) => setTimeout(r, 300)));
    chain.then(() => {
      document.documentElement.dataset.ready = "1";
      if (perfMode) perfMeasure();
    });
  });
})();
