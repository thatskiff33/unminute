// tools/design_check/fixtures.js
// A fake pywebview bridge for tools/design_check.py: the real page (workspace/ui)
// runs against fixed sample data, so each screen can be compared with its
// design board. Loaded before app.js only by the check's local server; never
// shipped. The scenario is chosen by the URL hash, e.g. index.html#meeting.
// All names and text are the fictional sample meeting used on the boards.
"use strict";
(() => {
  // What the app's bridge answers while the workspace is locked (#111 C4b):
  // workspace/api.py LOCKED_METHODS, and workspace/app.py LOCKED_TEXT for the
  // rest. tests/test_consistency.py keeps both equal to the app's.
  const LOCKED_METHODS = new Set(["status", "recording_status", "copy_diagnostics", "open_help",
                                  "close_app", "report_ui_error", "zoom"]);
  const LOCKED_TEXT = "Unminute is locked: Windows could not unlock its key on this computer. Ask your IT team.";
  // -- perf mode (tools/ui_idle_check.py) ------------------------------------------
  // index.html?perf=visible|hidden&ms=60000 counts what the idle page does over
  // ms of page time: bridge calls per method, timers, animation frames,
  // meeting-list rebuilds and page errors. Timers are wrapped here, before
  // app.js loads. "hidden" starts visible and then becomes hidden, with the same
  // bubbling visibilitychange a minimised window gets.
  // &display=120 (the recording run, #340) adds a simulated display: see below.
  const query = new URLSearchParams(location.search);
  const perfMode = query.get("perf");
  // pendingFrames: animation frames requested and not yet run. Headless Edge
  // runs almost no frames while virtual time skips ahead, so frames run are no
  // measure; an animation loop always leaves one frame pending.
  // draws: with the simulated display below, the frames in which the level
  // meter's canvas (#rec-canvas) was cleared for drawing, one count per frame,
  // as tools/perf_check.py counts them in the real window.
  // allCalls: every bridge call since the page loaded, never reset (locked mode).
  // live: changes to the text of a live region (polite or assertive, or role
  // status, alert or log) while measured. A screen reader may read a live
  // region again at each change, even to the same text, so an idle page or a
  // steady recording must make none (Narrator, 26 Oct 2026, #346). Counted
  // when the region holds text afterwards (what a screen reader would read):
  // a message cleared (a toast going) is not. Text only: a region shown,
  // hidden or relabelled (attributes) is not counted.
  const perf = { calls: {}, allCalls: {}, timeouts: 0, rebuilds: 0, errors: [], live: 0, liveIds: new Set(),
                 intervals: new Map(), pendingFrames: new Set(), draws: 0, drawnFrame: -1 };
  const LIVE = '[aria-live="polite"], [aria-live="assertive"], [role="status"], [role="alert"], [role="log"]';
  function liveRegion(node) {
    const element = node.nodeType === 1 ? node : node.parentElement;
    const region = element && element.closest(LIVE);
    return region && region.getAttribute("aria-live") !== "off" ? region : null;
  }
  // The simulated display (perf mode with &display=Hz). Headless Edge runs
  // almost no animation frames while virtual time skips ahead, so the meter's
  // frame rate cannot be seen there. Instead, frames come from a timer here,
  // display times a second of page time (8 and 9 ms apart for 120 Hz, keeping
  // to the rate), each with its exact display time, as a real display's do:
  // every callback asked for before a frame runs in it. While the page reports
  // itself hidden no frame runs, as with a minimised window; the ticks go on.
  // ticks: display refreshes so far; frames: those the page was drawn in.
  const sim = { hz: perfMode ? Number(query.get("display")) || 0 : 0, ticks: 0, frames: 0,
                queue: new Map(), seq: 0, stopped: false };
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
      if (sim.hz) {
        const id = ++sim.seq;
        sim.queue.set(id, fn);
        perf.pendingFrames.add(id);
        return id;
      }
      const id = realFrame((t) => { perf.pendingFrames.delete(id); fn(t); });
      perf.pendingFrames.add(id);
      return id;
    };
    window.cancelAnimationFrame = (id) => {
      perf.pendingFrames.delete(id);
      if (sim.hz) sim.queue.delete(id);
      else realCancelFrame(id);
    };
    if (sim.hz) {
      const period = 1000 / sim.hz;
      const tick = () => {
        if (sim.stopped) return;
        sim.ticks++;
        if (!pageHidden && sim.queue.size) {
          sim.frames++;
          const t = 1000 + sim.ticks * period;
          const due = [...sim.queue];
          sim.queue.clear();
          for (const [id, fn] of due) {
            perf.pendingFrames.delete(id);
            try { fn(t); } catch (e) { window.reportError(e); }   // to the error listeners
          }
        }
        realSetTimeout(tick, Math.round((sim.ticks + 1) * period) - Math.round(sim.ticks * period));
      };
      realSetTimeout(tick, Math.round(period));
      const realClearRect = CanvasRenderingContext2D.prototype.clearRect;
      CanvasRenderingContext2D.prototype.clearRect = function (...args) {
        if (this.canvas && this.canvas.id === "rec-canvas" && perf.drawnFrame !== sim.frames) {
          perf.drawnFrame = sim.frames;
          perf.draws++;
        }
        return realClearRect.apply(this, args);
      };
    }
    Object.defineProperty(document, "hidden", { configurable: true, get: () => pageHidden });
    Object.defineProperty(document, "visibilityState", { configurable: true,
                                                          get: () => (pageHidden ? "hidden" : "visible") });
    // A page that throws may stop polling and look idle: errors fail the check.
    window.addEventListener("error", (e) => { perf.errors.push(String(e.message || e)); });
    window.addEventListener("unhandledrejection", (e) => { perf.errors.push(String(e.reason)); });
  }
  function perfMeasure() {
    if (perfMode === "transcribing") { growMeasure(); return; }
    if (perfMode === "locked") { lockedMeasure(); return; }
    const ms = Number(query.get("ms")) || 60000;
    const groups = document.getElementById("groups");
    if (groups) {
      new MutationObserver((records) => { perf.rebuilds += records.length; })
        .observe(groups, { childList: true });
    }
    new MutationObserver((records) => {
      for (const r of records) {
        const region = liveRegion(r.target);
        if (region && region.textContent.trim()) {
          perf.live++;
          perf.liveIds.add(region.id || region.className || "?");
        }
      }
    }).observe(document.body, { subtree: true, childList: true, characterData: true });
    // Count from here, before the page becomes hidden, so work it does on the
    // visibilitychange itself counts too.
    perf.calls = {}; perf.timeouts = 0; perf.rebuilds = 0; perf.draws = 0;
    perf.live = 0; perf.liveIds.clear();
    perf.ticksFrom = sim.ticks; perf.framesFrom = sim.frames;
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
      const before = { ...perf.calls }, drawsBefore = perf.draws;
      pageHidden = false;
      document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
      realSetTimeout(() => finish(ms, seen, Object.keys(perf.calls).filter(
        (k) => perf.calls[k] > (before[k] || 0)), null, perf.draws - drawsBefore), 1000);
    }, ms);
  }
  function total(calls) { return Object.values(calls).reduce((a, b) => a + b, 0); }
  function snapshot() {
    return { calls: { ...perf.calls }, timeouts: perf.timeouts, rebuilds: perf.rebuilds,
             intervals: [...perf.intervals.values()], frames: perf.pendingFrames.size,
             rows: document.querySelectorAll("#groups li").length, draws: perf.draws,
             live: perf.live, liveIds: [...perf.liveIds],
             ticks: sim.ticks - perf.ticksFrom, simFrames: sim.frames - perf.framesFrom };
  }
  function finish(ms, seen, catchUp, redrawn = null, catchUpDraws = null) {
    sim.stopped = true;                  // no more simulated frames: the figures are in
    const perMinute = (n) => Math.round(n * 60000 / ms * 10) / 10;
    // Per second of the simulated display's time (ticks / Hz), which keeps to
    // page time; null without a simulated display.
    const perSecond = (n) => (sim.hz && seen.ticks
      ? Math.round(n * sim.hz / seen.ticks * 10) / 10 : null);
    const calls = Object.fromEntries(Object.entries(seen.calls).map(([k, v]) => [k, perMinute(v)]));
    const result = {
      mode: perfMode, page_ms: ms, calls_per_minute: calls,
      bridge_calls_per_minute: perMinute(total(seen.calls)),
      timeouts_per_minute: perMinute(seen.timeouts),
      pending_animation_frames: seen.frames,
      list_rebuilds_per_minute: perMinute(seen.rebuilds),
      live_region_changes_per_minute: perMinute(seen.live),
      live_regions_changed: seen.liveIds,
      live_intervals: seen.intervals.length,
      fastest_interval_ms: seen.intervals.length ? Math.min(...seen.intervals) : null,
      meeting_rows: seen.rows,
      catch_up_calls: catchUp,           // hidden: the methods called once shown again
      redrawn_after_change: redrawn,     // visible: the list redrawn within a poll of a change
      display_hz: sim.hz || null,
      page_frames_per_second: perSecond(seen.simFrames),
      meter_draws_per_second: perSecond(seen.draws),
      catch_up_meter_draws: catchUpDraws,   // hidden: meter draws in the 1 s once shown again
      page_errors: perf.errors,
    };
    const out = document.createElement("pre");
    out.id = "perf-result";
    out.textContent = JSON.stringify(result);
    document.body.append(out);
  }

  // -- locked mode (tools/ui_idle_check.py, #111 C4b) -------------------------------
  // index.html?perf=locked&ms=60000#locked: the locked screen left alone for ms
  // of page time. It must make no bridge call and keep no timer then, and from
  // the start call only what the app allows while locked (allCalls), with no
  // sample meeting's title anywhere in the page.
  function lockedMeasure() {
    const ms = Number(query.get("ms")) || 60000;
    perf.calls = {}; perf.timeouts = 0;
    realSetTimeout(() => {
      const seen = snapshot();
      const perMinute = (n) => Math.round(n * 60000 / ms * 10) / 10;
      const view = document.getElementById("view-locked");
      const others = [...document.querySelectorAll(".view")].filter((v) => v !== view && !v.hidden);
      const text = document.body.textContent;
      const result = {
        mode: "locked", page_ms: ms,
        calls_per_minute: Object.fromEntries(Object.entries(seen.calls).map(([k, v]) => [k, perMinute(v)])),
        bridge_calls_per_minute: perMinute(total(seen.calls)),
        timeouts_per_minute: perMinute(seen.timeouts),
        live_intervals: seen.intervals.length,
        pending_animation_frames: seen.frames,
        all_calls: { ...perf.allCalls },
        locked_shown: !!view && !view.hidden && !others.length
          && document.getElementById("sidebar").hidden,
        sample_titles_shown: SAMPLE_TITLES.filter((t) => text.includes(t)),
        page_errors: perf.errors,
      };
      const out = document.createElement("pre");
      out.id = "perf-result";
      out.textContent = JSON.stringify(result);
      document.body.append(out);
    }, ms);
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
  const GROW_HELD = 10;            // passages held back while the edit runs (steps 5 and 6)
  const grow = { added: 0, redraws: 0, rowsSent: 0, redrawSteps: [], editId: 5,
                 editText: "Edited while transcribing (sample).", editHeld: null, heldDrawn: null, editOnLoad: false };
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
      growMore(GROW_HELD);
    },
    () => {                          // nothing was drawn under the edit; it ends
      grow.editHeld = grow.added === grow.addedAtEdit;
      document.activeElement.blur();
      // No new passages and no progress: the rows held back must still be
      // appended once the edit has ended, because the given-up append put its
      // signature back. Nothing else changes before step 7 to draw them.
    },
    () => {                          // speakers identified: every passage labelled
      grow.heldDrawn = grow.added - grow.addedAtEdit;
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
      held_rows_drawn: grow.heldDrawn,
      held_rows_expected: GROW_HELD,
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
           source: "NVIDIA parakeet-tdt-0.6b-v2, ONNX export by istupakov",
           label: "Parakeet v2", title: "Speech to text: Parakeet v2 (recommended)",
           about: "Fast and accurate for English. Runs on this computer's processor." }),
    card({ name: "parakeet-tdt-0.6b-v3", role: "speech", size_gb: 0.67, source: "NVIDIA parakeet-tdt-0.6b-v3, ONNX export by istupakov",
           revision: "8f23f0c", label: "Parakeet v3", title: "Speech to text: Parakeet v3",
           about: "Newer multilingual version. In our tests it was less accurate for English." }),
    card({ name: "large-v3-turbo", role: "speech", size_gb: 1.62, state: "missing", status: "Not downloaded.",
           removable: false, licence: "MIT", source: "OpenAI whisper-large-v3-turbo, CTranslate2 conversion",
           revision: "0a363e9", label: "Whisper large-v3 turbo", title: "Speech to text: Whisper large-v3 turbo", about: "Alternative engine; slower on a processor." }),
    card({ name: "large-v3", role: "speech", size_gb: 3.09, state: "missing", status: "Not downloaded.",
           removable: false, licence: "MIT", source: "OpenAI whisper-large-v3, CTranslate2 conversion by Systran",
           revision: "edaa852", label: "Whisper large-v3", title: "Speech to text: Whisper large-v3",
           about: "OpenAI's most accurate Whisper model. In our tests on Intel Arc graphics it was no more accurate than turbo. It is the largest model, so expect it to be the slowest on a processor." }),
    card({ name: "pyannote/speaker-diarization-community-1", role: "speakers", recommended: true, size_gb: 0.03,
           needs_token: true, label: "Speaker identification", title: "Speaker identification (optional)",
           about: "Works out who spoke when. Downloading it needs a free Hugging Face account and token." }),
    // The two graphics card components (#248), as workspace/setup.py describes
    // them on a computer with an NVIDIA RTX 500 (tests/test_single_source.py).
    card({ name: "gpu-speakers", role: "gpu", size_gb: 2.0, state: "missing", status: "Not downloaded yet.",
           removable: false, licence: "PyTorch BSD-3-Clause; NVIDIA CUDA runtime and cuDNN under NVIDIA's licences",
           source: "PyTorch CUDA 13.0 build from download-r2.pytorch.org", revision: "torch-2.14.1-cu130",
           label: "Graphics card acceleration", title: "Graphics card acceleration (optional)",
           about: "Speeds up speaker identification on this computer's NVIDIA RTX 500. Used only in Faster mode, once turned on in Settings." }),
    card({ name: "gpu-whisper", role: "gpu", size_gb: 0.57, state: "missing", status: "Not downloaded yet.",
           removable: false, licence: "NVIDIA CUDA Toolkit licence (cuBLAS), published by NVIDIA on PyPI",
           source: "NVIDIA cuBLAS 12.8.4.1 for CUDA 12 from files.pythonhosted.org", revision: "cublas-12.8.4.1-cu12",
           label: "Graphics card for Whisper", title: "Graphics card for Whisper (optional)",
           about: "Runs Whisper large-v3 turbo and Whisper large-v3 on this computer's NVIDIA RTX 500, several times faster than on the processor. Used only in Faster mode, once the graphics card is turned on in Settings. Parakeet stays on the processor." }),
  ];
  // Live drafts (#104): sample words only, matching the sample garden meeting.
  const DRAFT = [
    "Okay, thanks for coming, everyone. First up is the garden roster for next month.",
    "I can take the first two Saturdays, but I'm away the weekend after that.",
    "Great. Can someone look at the tap by the shed? It was dripping again on Tuesday.",
    "I'll check it on Thursday and bring a spare washer.",
    "And has the seed order gone in yet, or are we still waiting on prices",
  ].map((text, i) => ({ kind: "phrase", text, start: 700 + i * 9, end: 706 + i * 9 }));
  const SETTINGS = {
    mode: "quiet", battery_override: false, speakers: true, gpu_speakers: false, microphone: true,
    model: "parakeet-tdt-0.6b-v2", onboarded: true, recording_display: "flow",
    live_drafts: false, live_drafts_audio: "microphone", computer_audio: false,
    live_drafts_model: { state: "ready", label: "Parakeet v2", offered: true },
    // Each with its state as the Downloads page shows it (#244); CARDS above.
    choices: [{ name: "parakeet-tdt-0.6b-v2", label: "Parakeet v2", recommended: true,
                state: "ready", offered: true, note: null },
              { name: "parakeet-tdt-0.6b-v3", label: "Parakeet v3", recommended: false,
                state: "ready", offered: true, note: null },
              { name: "large-v3-turbo", label: "Whisper large-v3 turbo", recommended: false,
                state: "missing", offered: true,
                note: "Whisper large-v3 turbo is not downloaded. Download it in Settings > Manage downloads, or choose another speech model." },
              { name: "large-v3", label: "Whisper large-v3", recommended: false,
                state: "missing", offered: true,
                note: "Whisper large-v3 is not downloaded. Download it in Settings > Manage downloads, or choose another speech model." }],
    // Both components ready: Settings' note names both uses (#248).
    gpu: { card: "NVIDIA RTX 500", driver_ok: true, min_driver: "580", component: "ready", whisper_component: "ready" },
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
             template: { id: "minutes", title: TEMPLATE.title, version: 1 }, filename: "x.md", options: opts,
             // The default when IT sets none (#109); null when policy withholds it.
             assistant: "assistant" in data ? data.assistant : { name: "ChatGPT", host: "chatgpt.com" } };
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
      // Where the version was transcribed (#248): Parakeet always on the processor.
      ran_on: [{ where: "cpu", precision: "int8", parts: 25, fallback: false }],
      speakers: { status: "done", error: null, device: "cpu", count: null, started_at: at(2, 5) },
      holds: [], cleanup: [], limits: LIMITS, duration_s: 1085, speaker_count: 2, mode: "quiet",
      ...over,
    };
    // As the app reports it: Skip applies until speakers are labelled or skipped.
    if (!("can_skip" in m))
      m.can_skip = !m.reviewed_at && !(m.speakers && ["done", "skipped"].includes(m.speakers.status));
    if (!("can_retranscribe" in m)) m.can_retranscribe = canRetranscribe(m);
    return m;
  }
  // As the app reports it (#244): with the audio kept and the review not
  // confirmed, unless still transcribing; a failed meeting can switch model.
  function canRetranscribe(m) {
    return m.audio_available !== false && !m.reviewed_at && (m.stage !== "inbox" || !!m.error);
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
    recording: { enabled: true, computer_audio: false, display: "flow", recording: null, reports: [] },
    status: { busy: null, message: "", import_errors: [], importing: 0, mode: "quiet", integrity: null },
    reminders: { holds_due: [], awaiting_confirmation: [], retention: [], recordings_waiting: [],
                 thresholds: { review_days: 30, action_days: 0, managed: { review: false, action: false } } },
  };

  // -- scenarios -----------------------------------------------------------------------
  // Each changes the data and lists steps to run once the page is ready.
  const row = (over) => {
    const m = { held: false, progress: 100, paused: false, error: null, busy: null,
      duration_s: 1500, speaker_count: null, model: "parakeet-tdt-0.6b-v2", model_label: "Parakeet v2", audio_available: true,
      cleanup: [], age_days: 0, action: "", can_skip: true, ...over };
    if (!("can_retranscribe" in m)) m.can_retranscribe = canRetranscribe(m);
    return m;
  };
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
    // Live drafts (#104): the recording card with draft text (sample words).
    "recording-drafts": {
      change: (d) => {
        SCENARIOS.recording.change(d);
        d.recording.recording.id = "r1";
        d.recording.draft = { state: "running", note: null, count: DRAFT.length, generation: 1, mix: false };
      },
      steps: [],
    },
    "recording-drafts-starting": {
      change: (d) => {
        SCENARIOS.recording.change(d);
        d.recording.recording.id = "r1";
        d.recording.draft = { state: "starting", note: "Getting draft text ready.", count: 0, generation: 1, mix: false };
      },
      steps: [],
    },
    "recording-drafts-battery": {
      change: (d) => {
        SCENARIOS.recording.change(d);
        d.recording.recording.id = "r1";
        d.recording.draft = { state: "battery", count: DRAFT.length, generation: 1, mix: false,
                              note: "Draft text is paused while on battery. The recording carries on." };
      },
      steps: [],
    },
    "recording-drafts-hidden": {
      change: (d) => { SCENARIOS["recording-drafts"].change(d); },
      steps: [() => document.getElementById("rec-draft-toggle").click()],
    },
    // #462: the microphone switched mid-recording (a headset connected and
    // Windows made it the default): the card names it, with the choice of
    // microphone, and the switch and its short gap under the card.
    "recording-switched": {
      change: (d) => {
        SCENARIOS.recording.change(d);
        Object.assign(d.recording.recording, { id: "r1", device: "Headset (sample)", switched: true,
          following: true, events: [
            { kind: "mic_switched", at_s: 512, seconds: null,
              note: "Windows changed its default microphone, so the recording switched to it." },
            { kind: "gap", at_s: 512, seconds: 0.4,
              note: "Silence was recorded here while the microphone switched." }] });
        d.recMics = [{ index: 0, name: "Laptop microphone", label: "Laptop microphone", default: false, current: false },
                     { index: 1, name: "Headset (sample)", label: "Headset (sample)", default: true, current: true }];
      },
      steps: [],
    },
    // Computer audio (#108): on in Settings, so the line under Record says so.
    "home-computer": { change: (d) => { d.recording.computer_audio = true; }, steps: [] },
    // A two-track recording recovered after the app closed: each track's length (#108 slice 5).
    "home-recovered-computer": {
      change: (d) => {
        d.recording.computer_audio = true;
        d.recording.reports = [{ id: "rep-recovered", title: null, events: [], pending: "sample", recovered: true,
          started_at: at(9, 30), seconds: 1512.4, computer_seconds: 1497.9,
          note: "The app closed while this was recording. Keep it to transcribe it, or delete it." }];
      },
      steps: [],
    },
    // Recording with computer audio: the thin second bar under the display.
    "recording-computer": {
      change: (d) => {
        SCENARIOS.recording.change(d);
        d.recording.computer_audio = true;
        d.recording.recording.computer = { on: true, level_db: -30, device: "Speakers (sample)" };
        d.compLevels = true;
      },
      steps: [],
    },
    // A minute in, the microphone hears the room but the computer plays nothing.
    "recording-computer-quiet": {
      change: (d) => {
        SCENARIOS["recording-computer"].change(d);
        d.recording.recording.seconds = 75;
        d.recording.recording.computer.level_db = -96;
        d.compLevels = false;
      },
      steps: [wait(1200)],
    },
    // Policy turned computer audio off mid-recording: the bar says it stopped.
    "recording-computer-stopped": {
      change: (d) => {
        SCENARIOS["recording-computer"].change(d);
        d.recording.recording.computer = { on: false, level_db: -96, device: "Speakers (sample)" };
        d.recording.recording.events = [{ kind: "computer_policy_stop", at_s: 512,
          note: "Your organisation turned off computer audio. Only the microphone is recorded from here." }];
        d.compLevels = false;
      },
      steps: [],
    },
    // #404 (the live-region gate in tools/ui_idle_check.py, no boards): a
    // recording going while two bridge calls fail at every poll, with sample
    // errors: the reminders from the start, and recording_status once the card
    // is drawn (the clock's own reads drop their errors). The step waits for a
    // poll, so each error has been shown once before the count starts.
    "recording-bridge-error": {
      change: (d) => {
        SCENARIOS.recording.change(d);
        d.fail = { reminders: "The reminders could not be read (sample)." };
      },
      steps: [() => { data.fail.recording_status = "The recorder could not be reached (sample)."; },
              wait(2500)],
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
    // Retention reminders (#111 C6), as Workspace.reminders reports them.
    // IT's thresholds (Review due at 7 days, Action required at 14), and the
    // app's own default (Review due at 30, Action required off).
    "home-retention": { change: (d) => retention(d, true), steps: [] },
    "home-retention-default": { change: (d) => retention(d, false), steps: [] },
    help: { steps: [show("help")] },
    // #111 C4b: Windows would not unlock the workspace key. The bridge then
    // answers only the methods the app allows while locked (api.LOCKED_METHODS).
    locked: {
      change: (d) => {
        d.status = { ...d.status, message: "Locked", integrity: null,
                     locked: { reason: "windows", code: "U-3F9A2C", win_error: -2146893813, damage: null } };
        d.locked = true;
      },
      steps: [],
    },
    // The same, but the page's first status() fails, so it starts as usual and
    // its next status (the first refresh) must still end on the locked screen.
    "locked-after-error": {
      change: (d) => { SCENARIOS.locked.change(d); d.failStatus = 1; },
      steps: [],
    },
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
    // #108 slice 5: a call recorded with computer audio, speakers not yet
    // named; the one mostly on the user's microphone is suggested as "You".
    "names-you": {
      change: (d) => { d.panelYou = "SPEAKER_00"; d.panelNames = { SPEAKER_00: "Speaker 1", SPEAKER_01: "Speaker 2" }; },
      steps: [...openAndPlay(), () => document.getElementById("m-names").click(), wait(600)],
    },
    confirm: { steps: [...openAndPlay(), () => document.getElementById("m-primary").click()] },
    handoff: {
      change: reviewed,
      steps: [openGarden, () => document.getElementById("m-primary").click(), wait(800),
              () => document.getElementById("ho-anon").click(),
              () => document.getElementById("ho-apply").click(), wait(500)],
    },
    // #109: policy turns Copy and open off; the page is laid out as before it.
    "handoff-off": {
      change: (d) => { reviewed(d); d.assistant = null; },
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
    "setup-4-computer": { change: (d) => { firstRun(d); d.settings.computer_audio = true; d.micHeard = true; },
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
    // Computer audio (#108): turned on, and turned off by the organisation.
    "settings-computer": {
      change: (d) => { SCENARIOS.settings.change(d); d.settings = { ...d.settings, computer_audio: true }; },
      steps: [show("settings")],
    },
    "settings-computer-managed": {
      change: (d) => {
        SCENARIOS.settings.change(d);
        d.settings = { ...d.settings, managed: { managed: ["AllowComputerAudioRecording"], invalid: [],
          allow: { AllowComputerAudioRecording: false }, assistant: null } };
      },
      steps: [show("settings")],
    },
    // #109: IT set the Hand off assistant, so Settings says which is in force.
    "settings-assistant": {
      change: (d) => {
        SCENARIOS.settings.change(d);
        d.settings = { ...d.settings, managed: {
          managed: ["HandoffAssistantName", "HandoffAssistantUrl"], invalid: [],
          allow: {}, assistant: { state: "set", name: "Copilot", host: "copilot.example.org" } } };
      },
      steps: [show("settings")],
    },
    // #300: a policy value name Unminute does not recognise (a trailing
    // space), so Settings names it for IT; nothing else is managed.
    "settings-unrecognised": {
      change: (d) => {
        SCENARIOS.settings.change(d);
        d.settings = { ...d.settings, managed: {
          managed: [], invalid: [], allow: {}, unrecognised: ["AllowModelDownloads\\x20"] } };
      },
      steps: [show("settings")],
    },
    // Live drafts (#104): the switch on; and with computer audio on (#108,
    // a later version), the choice of what the draft follows.
    "settings-drafts": {
      change: (d) => { SCENARIOS.settings.change(d); d.settings = { ...d.settings, live_drafts: true }; },
      steps: [show("settings")],
    },
    "settings-drafts-mix": {
      change: (d) => {
        SCENARIOS.settings.change(d);
        d.settings = { ...d.settings, live_drafts: true, computer_audio: true, live_drafts_audio: "mix" };
      },
      steps: [show("settings")],
    },
    downloads: { steps: [show("downloads")] },
    // #366: one download running and two queued behind it (Models-Queued,
    // and Models-Queued-Dark in the dark theme); and the same queue carried
    // on after a restart (Models-Resumed).
    "downloads-queued": { change: (d) => queueDownloads(d, false), steps: [show("downloads")] },
    // #346 (the live-region gate in tools/ui_idle_check.py, no boards): a meeting
    // whose transcription stopped (the assertive #m-error), and Setup's speaker
    // step while its model downloads (#su-speaker-status), each left open.
    "meeting-error": {
      change: (d) => {
        d.detail["m-garden"] = meeting({ stage: "inbox", progress: 40,
                                         error: "Could not be transcribed: the recording could not be read." });
      },
      steps: [openGarden],
    },
    "setup-3-downloading": {
      change: (d) => {
        firstRun(d);
        d.cards = d.cards.map((c) => (c.role === "speakers" ? { ...c, download: { state: "running", percent: 30,
          log: [], message: "Downloading the speaker model (sample)..." } } : c));
      },
      steps: [() => window.workspace.setStep(3)],
    },
    "downloads-resumed": { change: (d) => queueDownloads(d, true), steps: [show("downloads")] },
    // #416: an error stays beside the action that caused it, until dismissed
    // (no boards yet: compared with the nearest one, for the owner's look).
    // The refusals are the app's own wording; the kind comes from the bridge.
    "home-import-error": {
      change: (d) => { d.fail = { import_dialog: "The file dialog is not available." }; },
      steps: [show("meetings"), wait(300), () => document.getElementById("import").click(), wait(300),
              () => document.activeElement.blur()],
    },
    "handoff-error": {
      change: (d) => {
        reviewed(d);
        d.fail = { copy_handoff: "The handoff changed since the preview. Check the preview again, then copy or save." };
      },
      steps: [openGarden, () => document.getElementById("m-primary").click(), wait(800),
              () => document.getElementById("ho-copy").click(), wait(300),
              () => document.activeElement.blur()],
    },
    "settings-error": {
      change: (d) => {
        SCENARIOS.settings.change(d);
        d.fail = { set_settings: "Stop the recording before turning the microphone off." };
      },
      steps: [show("settings"), wait(400), () => document.getElementById("s-microphone").click(), wait(300),
              () => document.activeElement.blur()],
    },
    "downloads-error": {
      change: (d) => { d.fail = { download_model: "Downloads are turned off by your organisation. Ask your IT team to install the models." }; },
      steps: [show("downloads"), wait(400),
              () => [...document.querySelectorAll("#dl-list button")]
                .find((b) => b.textContent.startsWith("Download (")).click(), wait(300),
              () => document.activeElement.blur()],
    },
    "setup-3-token-error": {
      change: firstRun,
      steps: [() => window.workspace.setStep(3), wait(300),
              () => document.getElementById("s-download-speakers").click(),
              () => document.activeElement.blur()],
    },
    // An error from elsewhere still reaches the toast, now with an alert mark:
    // here Copy diagnostics fails unexpectedly (the bridge's wording, a sample code).
    "help-toast-error": {
      change: (d) => { d.fail = { copy_diagnostics: "Something went wrong (code U-3F2A9C). Please try again." }; },
      // The toast clears after 8 s, and Edge's virtual time runs past that before
      // the screenshot, so the same message is held (sticky) once it has shown.
      steps: [show("help"), wait(300), () => document.getElementById("help-diagnostics").click(), wait(300),
              () => document.activeElement.blur(),
              () => window.workspace.say(document.getElementById("status").textContent,
                                         { sticky: true, kind: "error" })],
    },
    // #244: a chosen model that is not downloaded (no board yet): the note
    // beside the picker; and the failed meeting's Transcribe with another model.
    "settings-model-not-ready": {
      change: (d) => { d.settings = { ...d.settings, model: "large-v3-turbo" }; },
      steps: [show("settings")],
    },
    "failed-model-not-ready": {
      change: (d) => {
        d.settings = { ...d.settings, model: "large-v3-turbo" };
        d.meetings = [row({ id: "m-site", title: "Site visit notes (sample)", stage: "inbox", progress: 0,
                            model: "large-v3-turbo", model_label: "Whisper large-v3 turbo",
                            error: "The speech model is not ready: Whisper large-v3 turbo is not installed or failed verification." })];
      },
      steps: [show("meetings"), wait(400),
              () => [...document.querySelectorAll("#view-meetings button")]
                .find((b) => b.textContent.startsWith("Transcribe with another model")).click()],
    },
    "meeting-earlier": {
      change: (d) => {
        d.detail["m-garden"] = meeting({ older_versions: [{ index: 0, model: "parakeet-tdt-0.6b-v3",
                                                            model_label: "Parakeet v3",
                                                            created_at: at(1, 20), edits: 2,
                                                            ran_on: [{ where: "cpu", precision: "int8", parts: 25, fallback: false }] }] });
      },
      steps: [...openAndPlay(), () => document.querySelector('[role=tab][data-tab="versions"]').click(),
              () => document.querySelector("#m-versions button.ver-card").click(), wait(400),
              () => document.querySelector('.wide-tabs [role=tab][data-tab="speakers"]').click()],
    },
    // The Versions tab (#248): the current version, Whisper large-v3 turbo,
    // began on the graphics card and finished on the processor after the card
    // could not be used; the earlier one is Parakeet v2 on the processor.
    "meeting-versions": {
      change: (d) => {
        d.detail["m-garden"] = meeting({
          model: "large-v3-turbo", model_label: "Whisper large-v3 turbo",
          ran_on: [{ where: "gpu", precision: "float16", parts: 9, fallback: false },
                   { where: "cpu", precision: "int8", parts: 16, fallback: true }],
          older_versions: [{ index: 0, model: "parakeet-tdt-0.6b-v2", model_label: "Parakeet v2",
                             created_at: at(1, 20), edits: 1,
                             ran_on: [{ where: "cpu", precision: "int8", parts: 25, fallback: false }] }] });
      },
      steps: [...openAndPlay(), () => document.querySelector('.wide-tabs [role=tab][data-tab="versions"]').click()],
    },
  };
  function firstRun(d) {
    d.setup = { onboarded: false, speech_ready: true };
    d.cards = d.cards.map((c) => (c.role === "speakers" ? { ...c, state: "missing", status: "Not downloaded." } : c));
  }
  function retention(d, managed) {
    // Oldest first, as the app lists them: the page puts action required
    // first, then review due, in each group.
    d.meetings = [
      row({ id: "m-quarterly", title: "Quarterly planning (sample)", stage: "handoff", age_days: 41,
            duration_s: 2520, speaker_count: 4, audio_available: false }),
      row({ id: "m-strategy", title: "Strategy day (sample)", stage: "cleanup", age_days: 35,
            cleanup: [{ id: "j2", phase: "transcript", state: "failed",
                        note: "a file is in use. Close it and try again." }] }),
      row({ id: "m-budget", title: "Budget review (sample)", stage: "review", age_days: 33, speaker_count: 3 }),
      row({ id: "m-site", title: "Site visit notes (sample)", stage: "inbox", progress: 0, age_days: 31,
            can_retranscribe: false, error: "Could not be transcribed: the recording could not be read." }),
      row({ id: "m-team", title: "Team catch-up (sample)", stage: "review", age_days: 9 }),
      row({ id: "m-garden", title: "Community garden planning (sample)", stage: "review",
            duration_s: 1085, speaker_count: 2 }),
    ];
    // Each step has its own clock: the quarterly meeting was reviewed 10 days ago.
    const steps = [["m-quarterly", "hand_off", 10], ["m-strategy", "cleanup_unfinished", 35],
                   ["m-budget", "review", 33], ["m-site", "review", 31], ["m-team", "review", 9]];
    const [review, action] = managed ? [7, 14] : [30, 0];
    const level = (days) => action && days >= action ? "action_required" : review && days >= review ? "due" : null;
    d.reminders.thresholds = { review_days: review, action_days: action,
                               managed: { review: managed, action: managed } };
    d.reminders.retention = steps.filter(([, , days]) => level(days)).map(([meeting, step, days]) => (
      { meeting, title: d.meetings.find((m) => m.id === meeting).title, step, days, state: level(days) }));
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
  function queueDownloads(d, resumed) {
    const again = resumed ? { resumed: true } : {};
    const set = (n, download) => {
      d.cards = d.cards.map((c) => (c.name === n ? { ...c, download } : c));
    };
    set("large-v3-turbo", { state: "running", percent: 41, log: [], ...again,
                            message: "Downloading Whisper large-v3 turbo (1.62 GB, revision 0a363e9)..." });
    set("large-v3", { state: "queued", position: 1, log: [], ...again,
                      message: "Queued. It starts when the download before it finishes." });
    set("gpu-speakers", { state: "queued", position: 2, log: [], ...again,
                          message: "Queued. It starts when the download before it finishes." });
  }
  const SAMPLE_TITLES = [...new Set([...data.meetings.map((m) => m.title),
                                       ...Object.values(data.detail).map((m) => m.title)])];
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
    clear_import_errors: () => null,
    copy_diagnostics: () => ({ chars: 1200 }),
    open_help: () => null,
    close_app: () => null,
    recording_status: () => {
      // While "recording", each poll reports the next of the board's levels.
      if (data.levels && data.recording.recording) {
        const db = LEVELS[levelAt % LEVELS.length];
        data.recording.recording.level_db = db;
        data.recording.recording.viz = sampleViz(db, levelAt++);
        if (data.compLevels)                 // the other side of the call, out of step
          data.recording.recording.computer.level_db = LEVELS[(levelAt + 11) % LEVELS.length];
      }
      return data.recording;
    },
    // While recording (#462), what Windows has now, with the one recording marked.
    microphone_devices: () => (data.recording.recording
      ? data.recMics || [{ index: 0, name: data.recording.recording.device, label: data.recording.recording.device,
                           default: true, current: true }]
      : [{ index: 1, name: "Laptop microphone", default: true }]),
    switch_microphone: (index, name) => {
      const rec = data.recording.recording;
      Object.assign(rec, { device: name, switched: true, following: false });
      if (data.recMics) for (const m of data.recMics) m.current = m.name === name;
      return rec;
    },
    recording_draft: (since) => {
      const d = data.recording.draft || { state: "off", generation: 0 };
      const phrases = DRAFT.slice(since, since + 200);
      return { state: d.state, note: d.note || null, generation: d.generation, count: DRAFT.length,
               since, phrases };
    },
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
    speaker_panel: (mid) => ({ can_undo: false, you: data.panelYou || null,
      speakers: SPEAKERS.map((sp) => ({ ...sp, name: (data.panelNames || {})[sp.label] || sp.name, offset: 0,
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
      perf.allCalls[method] = (perf.allCalls[method] || 0) + 1;
      // A refusal carries its kind, as the app's bridge sends it (#416).
      if (data.fail && data.fail[method]) return { ok: false, error: data.fail[method], kind: "error" };   // #404
      if (method === "status" && data.failStatus > 0) {    // locked-after-error
        data.failStatus -= 1;
        return { ok: false, error: "The request failed.", kind: "error" };
      }
      // As the app's bridge gate (api.make_api): while locked, only these.
      if (data.locked && !LOCKED_METHODS.has(method)) return { ok: false, error: LOCKED_TEXT, kind: "error" };
      const fn = handlers[method];
      if (!fn) return { ok: false, error: `The design check has no sample data for ${String(method)}.`, kind: "error" };
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
