// Unminute UI: microphone recording, the level meter and the microphone lists.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

// -- refresh loop ----------------------------------------------------------------
let refreshing = false;
// -- microphone recording (#30) ---------------------------------------------------

const REC_TEXT = { recording: "Recording", paused: "Paused", stopped: "Saving",
                   saving: "Saving", device_lost: "Microphone lost", slept: "Computer went to sleep",
                   failed: "Could not save" };
let recTimer = null;

function fmtSeconds(s) {
  const t = Math.floor(s), h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60);
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(t % 60).padStart(2, "0")}`;
}

// An event as the page says it: its note without the note's own full stop,
// then when and how long, so a line reads "...switched to it at 8:32 (about
// 0.4 s)", never "...switched to it. at 8:32" (the owner's look, #462).
// In a note of several sentences the time follows the first, which says what
// happened ("...went to sleep... at 8:32. Recording stopped; ..."), not the
// last. The mix's notes are about the whole recording: no time (their at_s
// is 0). eventLine adds the final full stop (the card's list); reports join
// eventText with "; ".
function eventText(e) {
  const note = (e.note || e.kind).replace(/\.\s*$/, "");
  const split = /^(.+?)\.\s+(?=[A-Z])([\s\S]+)$/.exec(note);
  const first = split ? split[1] : note, rest = split ? `. ${split[2]}` : "";
  const at = e.at_s != null && !/^mix_/.test(e.kind || "") ? ` at ${fmtSeconds(e.at_s)}` : "";
  const len = e.seconds ? ` (about ${e.seconds < 1 ? e.seconds.toFixed(1) : Math.round(e.seconds)} s)` : "";
  return `${first}${at}${len}${rest}`;
}
function eventLine(e) { return `${eventText(e)}.`; }

// The recording display: one canvas across the card, drawn about 60 times a
// second (#216) from the latest block the recorder measured (ten a second),
// easing between them.
//   flow: Siri-style curves; height follows the level, ripple follows the pitch.
//   spectrum: pitch from low (left) to high, mirrored about the centre line.
//   wave: the shape of the sound itself.
const viz = { mode: "flow", target: null, db: -96, amp: 0, k: 2.5, phase: 0, bands: [], wave: [],
              gain: 4, raf: 0, last: 0, said: 0 };
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const vizColours = { accent: "", quiet: "" };
function readVizColours() {
  const css = getComputedStyle(document.documentElement);
  vizColours.accent = css.getPropertyValue("--record").trim() || "#E56600";
  vizColours.quiet = css.getPropertyValue("--line").trim() || "rgba(20,41,102,0.14)";
}

// Speech sits around -50 to -10 dBFS: spread -60..-10 over 0..1, so loud speech
// still has room to move rather than sitting at the top.
const levelAmount = (db) => Math.pow(Math.max(0, Math.min(1, (db + 60) / 50)), 0.9);

function feedViz(rec, display) {
  if (display) viz.mode = display;
  viz.target = rec.state === "recording" ? rec.viz : null;
  viz.db = rec.state === "recording" ? rec.level_db : -96;
  const box = $("rec-level");
  const now = Date.now();
  if (now - viz.said > 1000) {                       // the meter's value, once a second
    viz.said = now;
    box.setAttribute("aria-valuenow", String(Math.round(Math.max(-60, Math.min(0, viz.db)))));
  }
  if (!viz.raf) viz.raf = requestAnimationFrame(drawViz);
}

function stopViz() {
  $("rec-level").setAttribute("aria-valuenow", "-60");
  if (viz.raf) cancelAnimationFrame(viz.raf);
  viz.raf = 0; viz.target = null; viz.amp = 0; viz.bands = []; viz.wave = [];
  restartMeter(viz, meterClock);
}

// -- the meter's frame rate (#216) --
// Drawing on every display frame (120 a second on a 120 Hz screen) cost most of
// the processor time while recording with the window visible. The meter draws
// on every Nth frame instead, N = round(display rate / 60), at least 1: every
// frame at 60 Hz, every 2nd at 120 or 144 Hz. N is whole, so draws stay evenly
// spaced. requestAnimationFrame still drives it, so it stops when hidden.
// About 60 a second is the owner's choice (23 Oct 2026): 30 a second saved too
// little processor time for the drop in quality. tools/perf_check.py expects
// the same rate, and tools/ui_idle_check.py reads this constant to check it.
const METER_HZ = 60;
// The easing rates below (0.3, 0.08, 0.25, 0.35, 0.1) are shares per frame. The
// motion they give was seen and approved drawing on every frame of a 120 Hz
// display (the development laptop's, and the #216 demo), so 120 is the
// reference. easeShare turns each rate into the share for the time that passed,
// so the motion matches that at any draw rate.
const EASE_REF_HZ = 120;

// How many display frames apart to draw. Near the half-way point between two
// steps (90 Hz is 1.5), the current step is kept, so it does not flip between them.
function meterStep(displayHz, current) {
  const ratio = displayHz / METER_HZ;
  const want = Math.max(1, Math.round(ratio));
  if (!current || want === current) return want;
  return Math.abs(ratio - (Math.min(want, current) + 0.5)) < 0.1 ? current : want;
}

// Called on every animation frame with its time; true on the frames to draw.
// Tracks the display's frame interval, ignoring the first frame and long gaps
// (a stall, or the window hidden and shown again).
function meterFrame(clock, t) {
  const gap = clock.prev ? t - clock.prev : 0;
  clock.prev = t;
  if (gap > 2 && gap < 50) {
    clock.frameMs = clock.frameMs ? clock.frameMs + (gap - clock.frameMs) * 0.1 : gap;
    clock.step = meterStep(1000 / clock.frameMs, clock.step);
  }
  if (++clock.count < (clock.step || 1)) return false;
  clock.count = 0;
  return true;
}
const meterClock = { prev: 0, frameMs: 0, step: 0, count: 0 };

// The time since the last draw, in seconds (at most 0.1), and the draw's time kept.
function meterDt(v, t) {
  const dt = Math.min(0.1, v.last ? (t - v.last) / 1000 : 0.016);
  v.last = t;
  return dt;
}

// After the meter stops or the window is hidden, the next draw starts afresh:
// one frame's easing rather than a jump most of the way, and a new frame count.
// The display rate measured so far is kept.
function restartMeter(v, clock) { v.last = 0; clock.prev = 0; clock.count = 0; }

// The share of the way to move over dt seconds, for a rate per EASE_REF_HZ frame.
function easeShare(rate, dt) { return 1 - Math.pow(1 - rate, dt * EASE_REF_HZ); }
function ease(cur, to, rate, dt) { return cur + (to - cur) * easeShare(rate, dt); }

function drawViz(t) {
  const c = $("rec-canvas");
  if ($("rec-level").hidden || $("rec-card").hidden || state.view !== "meetings") { viz.raf = 0; return; }
  viz.raf = requestAnimationFrame(drawViz);
  if (!meterFrame(meterClock, t)) return;
  const dt = meterDt(viz, t);
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
  }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  if (!vizColours.accent) readVizColours();
  const { accent, quiet } = vizColours;
  const mid = h / 2;
  viz.amp = ease(viz.amp, levelAmount(viz.db), 0.3, dt);
  const tg = viz.target;
  if (viz.mode === "spectrum") drawSpectrum(g, w, h, mid, tg, dt, accent, quiet);
  else if (viz.mode === "wave") drawWave(g, w, h, mid, tg, dt, accent, quiet);
  else drawFlow(g, w, h, mid, tg, dt, accent, quiet);
}

function line(g, colour, width) { g.strokeStyle = colour; g.lineWidth = width; g.lineJoin = "round"; g.lineCap = "round"; }

function drawFlow(g, w, h, mid, tg, dt, accent, quiet) {
  // Ripple count follows the voice's pitch (80 to 400 Hz: 1.5 to 4.5 waves).
  const pitch = tg && tg.pitch_hz ? tg.pitch_hz : 0;
  if (pitch) viz.k = ease(viz.k, 1.5 + Math.max(0, Math.min(1, (pitch - 80) / 320)) * 3, 0.08, dt);
  if (!reducedMotion.matches) viz.phase += dt * (1.2 + viz.amp * 7);
  const amp = Math.max(0.04, viz.amp) * (mid - 3);
  const curves = [[1, 1, 1, 0.95], [0.75, 1.35, 0.7, 0.55], [1.3, 0.7, 0.5, 0.35]];
  for (const [speed, freq, size, alpha] of curves) {
    g.beginPath();
    for (let x = 0; x <= w; x += 2) {
      const u = (x / w) * 4 - 2;                       // -2..2 across the card
      const taper = Math.pow(2 / (2 + Math.pow(u, 4)), 2);   // the ends rest on the line
      const y = mid + amp * size * taper * Math.sin(u * viz.k * freq * Math.PI + viz.phase * speed);
      if (x === 0) g.moveTo(x, y); else g.lineTo(x, y);
    }
    g.globalAlpha = alpha;
    line(g, accent, size === 1 ? 2.5 : 2);
    g.stroke();
  }
  g.globalAlpha = 1;
  if (viz.amp < 0.02) { g.beginPath(); g.moveTo(0, mid); g.lineTo(w, mid); line(g, quiet, 1); g.stroke(); }
}

function drawSpectrum(g, w, h, mid, tg, dt, accent, quiet) {
  const target = tg ? tg.bands.map((db) => Math.max(0, Math.min(1, (db + 85) / 70))) : [];
  const n = target.length || viz.bands.length || 40;
  if (viz.bands.length !== n) viz.bands = new Array(n).fill(0);
  for (let i = 0; i < n; i++) viz.bands[i] = ease(viz.bands[i], target[i] || 0, 0.25, dt);
  const pts = viz.bands.map((v, i) => [(i / (n - 1)) * w, v * (mid - 3)]);
  const shape = (sign) => {
    g.moveTo(0, mid);
    for (let i = 0; i < pts.length - 1; i++) {
      const [x0, y0] = pts[i], [x1, y1] = pts[i + 1];
      g.quadraticCurveTo(x0, mid - sign * y0, (x0 + x1) / 2, mid - sign * (y0 + y1) / 2);
    }
    g.lineTo(w, mid);
  };
  g.beginPath(); shape(1); shape(-1);
  g.globalAlpha = 0.35; g.fillStyle = accent; g.fill();
  g.globalAlpha = 1;
  g.beginPath(); shape(1); line(g, accent, 2); g.stroke();
  g.beginPath(); shape(-1); line(g, accent, 2); g.stroke();
  g.beginPath(); g.moveTo(0, mid); g.lineTo(w, mid); line(g, quiet, 1); g.stroke();
}

function drawWave(g, w, h, mid, tg, dt, accent, quiet) {
  const target = tg ? tg.wave : [];
  const n = target.length || viz.wave.length || 128;
  if (viz.wave.length !== n) viz.wave = new Array(n).fill(0);
  // Scale so speech fills most of the height, without blowing up silence.
  const peak = target.reduce((m, v) => Math.max(m, Math.abs(v)), 0);
  if (peak > 0.002) viz.gain = ease(viz.gain, Math.min(40, 0.85 / peak), 0.1, dt);
  for (let i = 0; i < n; i++) viz.wave[i] = ease(viz.wave[i], target[i] || 0, 0.35, dt);
  g.beginPath();
  for (let i = 0; i < n; i++) {
    const x = (i / (n - 1)) * w;
    const y = mid - Math.max(-1, Math.min(1, viz.wave[i] * viz.gain)) * (mid - 3);
    if (i === 0) g.moveTo(x, y);
    else {
      const px = ((i - 1) / (n - 1)) * w;
      const py = mid - Math.max(-1, Math.min(1, viz.wave[i - 1] * viz.gain)) * (mid - 3);
      g.quadraticCurveTo(px, py, (px + x) / 2, (py + y) / 2);
    }
  }
  line(g, viz.amp < 0.02 ? quiet : accent, 2);
  g.stroke();
}

const REC_KIND = { recording: "", paused: "paused", device_lost: "lost", slept: "lost", failed: "lost",
                   stopped: "saving", saving: "saving" };

function deviceName() {
  const opt = $("rec-device").selectedOptions[0];
  return opt ? opt.dataset.name : "";
}
// The full name, for showing (Windows' own list cuts names at 31 characters).
function deviceLabel() {
  const opt = $("rec-device").selectedOptions[0];
  return opt ? opt.dataset.label || opt.dataset.name : "";
}
const deviceOption = (d) => el("option", { value: String(d.index), "data-name": d.name,
  "data-label": d.label || d.name, textContent: (d.label || d.name) + (d.default ? " (default)" : "") });

// -- microphone lists (#369) -----------------------------------------------------------
// The app lists the microphones afresh on each microphone_devices call while no
// stream is open, so one plugged in or out while the app is open is seen. The
// page asks again when a list is opened or focused and on returning to it
// (another view and back, or the window shown again); never while recording,
// and not on idle polls. sig: what the list shows, so an unchanged answer
// leaves the list (and an open drop-down) alone. at: when it was last asked, so
// a focus just after opening the chooser does not ask twice.
const MIC_ASK_AGAIN_MS = 3000;
const micLists = { "rec-device": { stale: true, busy: null, at: -Infinity, sig: null },
                   "su-device": { stale: true, busy: null, at: -Infinity, sig: null } };

function micsStale() {
  for (const m of Object.values(micLists)) m.stale = true;
}

// Fill a list, keeping the microphone selected if it is still there (matched by
// the name the app opens it by), otherwise the Windows default. False if the
// selected microphone has gone.
function fillMics(sel, devices) {
  const m = micLists[sel.id];
  const sig = JSON.stringify(devices.map((d) => [d.index, d.name, d.label || "", !!d.default]));
  if (sig === m.sig && sel.options.length === devices.length) return true;
  const prev = sel.selectedOptions[0];
  const name = prev && prev.dataset.name, index = prev && prev.value;
  sel.replaceChildren(...devices.map(deviceOption));
  m.sig = sig;
  const kept = prev && (devices.find((d) => d.name === name && String(d.index) === index)
                        || devices.find((d) => d.name === name));
  const pick = kept || devices.find((d) => d.default);
  if (pick) sel.value = String(pick.index);
  return !prev || !!kept;
}

// Said on the page only (P3): no device name, which the list already shows.
function micGone(devices) {
  say("The chosen microphone is no longer connected. " + (devices.some((d) => d.default)
    ? "The Windows default is selected instead." : devices.length ? "Choose another." : "No microphone found."));
}

// Ask for a list again and show it with render(devices). One request at a time
// per list; nothing while a recording is going (state.recActive). A failed ask
// counts as asked too: it is tried again on the next trigger, not on every
// poll (an empty list is the exception, as before: see renderRecorder).
function relistMics(id, render) {
  const m = micLists[id];
  if (state.recActive) return Promise.resolve();
  if (m.busy) return m.busy;
  m.busy = (async () => {
    try {
      const devices = await call("microphone_devices");
      if (!fillMics($(id), devices)) micGone(devices);
      render(devices);
    } finally {
      m.stale = false;
      m.at = performance.now();
      m.busy = null;
    }
  })();
  return m.busy;
}

// Focusing a list asks again, unless it was asked just now.
function relistOnFocus(id, render) {
  $(id).addEventListener("focus", () => {
    if (performance.now() - micLists[id].at >= MIC_ASK_AGAIN_MS) attempt(() => relistMics(id, render));
  });
}

function showRecMics(devices) {
  const sel = $("rec-device");
  $("mic-name").textContent = $("rec-start-device").textContent = deviceLabel() || "No microphone";
  $("rec-start").disabled = $("rec-start-chosen").disabled = !sel.options.length;
  const result = $("rec-test-result");
  if (!devices.length) result.textContent = "No microphone found.";
  else if (result.textContent === "No microphone found.") result.textContent = "";
}

function showSuMics(devices) {
  $("su-test").disabled = !devices.length;
  const box = $("su-test-result");
  if (!devices.length) { box.hidden = false; box.textContent = "No microphone found."; }
  else if (box.textContent === "No microphone found.") { box.hidden = true; box.textContent = ""; }
}

// "under a minute", "1 minute", "23 minutes", "1 hour 5 minutes".
function fmtDuration(s) {
  const m = Math.round(s / 60);
  if (m < 1) return "under a minute";
  const h = Math.floor(m / 60), mm = m % 60;
  const part = (n, unit) => `${n} ${unit}${n === 1 ? "" : "s"}`;
  return h ? part(h, "hour") + (mm ? ` ${part(mm, "minute")}` : "") : part(m, "minute");
}

function renderRecCard(rec, display, lidOverride, lidRefused) {
  const kind = REC_KIND[rec.state] ?? "saving";
  $("rec-card").className = `rec-card ${kind}`.trim();
  // A live region (polite): set only when the state changes. Set at every
  // poll, even to the same text, Narrator read it again ten times a second
  // ("Rec... Rec...", the owner's check of 26 Oct 2026, #346).
  putText($("rec-state"), REC_TEXT[rec.state] || rec.state);
  state.recSeconds = rec.seconds;
  $("rec-clock").textContent = fmtSeconds(rec.seconds);
  $("rec-icon").hidden = kind !== "lost";
  $("rec-spinner").hidden = kind !== "saving";
  $("rec-level").hidden = kind !== "";
  if (kind === "") feedViz(rec, display);
  else stopViz();
  $("rec-middle").hidden = kind !== "paused";
  $("rec-middle").textContent = "Nothing is being recorded. Resume to carry on in the same recording.";
  $("rec-sub").hidden = kind !== "lost" && kind !== "saving";
  $("rec-sub").textContent = kind === "saving"
    ? "The recording is then imported and transcribed like any other."
    : rec.state === "failed" ? "The recording stopped. Saving what was recorded."
    : rec.state === "slept" ? `${fmtSeconds(rec.seconds)} recorded before the computer went to sleep. Saving what was recorded.`
    : `${fmtSeconds(rec.seconds)} recorded before it disconnected. Saving what was recorded.`;
  $("rec-pause").hidden = kind !== "";
  $("rec-resume").hidden = kind !== "paused";
  $("rec-stop").hidden = kind === "saving" || kind === "lost";   // a lost microphone saves by itself
  $("rec-discard").hidden = kind !== "" && kind !== "paused";
  $("rec-stop").className = kind === "paused" ? "" : "primary";
  $("rec-note").hidden = kind !== "" && kind !== "paused";
  // The lid (#172): how long the recording carried on with the lid closed (its
  // marks could not be seen meanwhile), and that closing it will not stop it.
  const lid = [...rec.events].reverse().find((e) => e.kind === "lid");
  const lidNote = (lid ? `The recording carried on while the lid was closed (about ${fmtDuration(lid.seconds)}). ` : "")
    + (lidOverride ? "Closing the lid does not stop this recording: stop it here when the meeting ends. "
      : lidRefused === "managed" ? "Closing the lid may stop this recording: your organisation sets what closing the lid does. "
      : lidRefused ? "Closing the lid may stop this recording: Windows did not allow Unminute to change the lid setting. " : "");
  const comp = rec.computer;
  const what = !comp ? "Only the microphone is recorded. Tell everyone present the meeting is being recorded."
    : comp.on ? "Microphone and computer audio are recorded. Tell everyone in the meeting, including people on the call."
    : "Only the microphone is recorded now. Tell everyone in the meeting, including people on the call.";
  // #462: after a switch of microphone, which one records now (fixed text
  // and the device's name, on the page only).
  $("rec-note").textContent = lidNote + (rec.switched ? `Now recording from ${rec.device || "the microphone"}. `
    : `${rec.device || "Microphone"}. `) + what;
  renderRecMic(rec, kind);
  renderComputer(rec, kind);
}

// The microphone choice while recording (#462). The list is what Windows has
// now (microphone_devices while recording), asked for when a recording's card
// first shows, after its microphone changes, and when the list is focused
// (not again within MIC_ASK_AGAIN_MS); never on idle polls. Choosing one
// switches the recording to it, and it then stops following the default.
const recMics = { id: null, device: null, sig: null, busy: null, at: -Infinity };

function fillRecMics(devices, rec) {
  const sel = $("rec-mic");
  const sig = JSON.stringify([rec.device, devices.map((d) => [d.index, d.name, !!d.default, !!d.current])]);
  if (sig === recMics.sig) return;
  recMics.sig = sig;
  const options = devices.map((d) => el("option", { value: String(d.index), "data-name": d.name,
    textContent: d.name + (d.default ? " (Windows default)" : "") }));
  const current = devices.find((d) => d.current);
  // The microphone recording now, even when Windows no longer lists it.
  if (!current) options.unshift(el("option", { value: "", disabled: true, textContent: rec.device || "Microphone" }));
  sel.replaceChildren(...options);
  sel.value = current ? String(current.index) : "";
}

function relistRecMics(rec) {
  if (recMics.busy) return recMics.busy;
  recMics.busy = (async () => {
    try { fillRecMics(await call("microphone_devices"), rec); }
    finally { recMics.at = performance.now(); recMics.busy = null; }
  })();
  return recMics.busy;
}

function renderRecMic(rec, kind) {
  const shown = kind === "" || kind === "paused";
  $("rec-mic-row").hidden = !shown;
  if (!shown) return;
  if (recMics.id !== rec.id || recMics.device !== rec.device) {
    Object.assign(recMics, { id: rec.id, device: rec.device, sig: null });
    relistRecMics(rec).then(() => pollOk("rec-mics"), (err) => pollError("rec-mics", err));
  }
}

function wireRecMic() {
  const sel = $("rec-mic");
  sel.addEventListener("focus", () => {
    if (performance.now() - recMics.at >= MIC_ASK_AGAIN_MS && state.lastRec)
      attempt(() => relistRecMics(state.lastRec));
  });
  sel.addEventListener("change", () => attempt(async () => {
    const opt = sel.selectedOptions[0];
    if (!opt || !opt.value) return;
    sel.disabled = true;
    try {
      const rec = await call("switch_microphone", Number(opt.value), opt.dataset.name);
      state.lastRec = rec;
      recMics.sig = null;
      await relistRecMics(rec).catch(() => {});
    } catch (err) {
      recMics.sig = null;                 // show what is recording again
      if (state.lastRec) relistRecMics(state.lastRec).catch(() => {});
      throw err;
    } finally {
      sel.disabled = false;
    }
    renderRecorder();
  }));
}

// Computer audio (#108): a thin second bar under the display, set at each
// poll (ten a second) with no drawing loop of its own, so it adds next to
// nothing to the recording's processor time (#216).
const compSeen = { id: null, mic: false, comp: false, said: 0 };
function renderComputer(rec, kind) {
  const comp = rec.computer;
  const shown = !!comp && (kind === "" || kind === "paused");
  $("rec-computer").hidden = !shown;
  if (compSeen.id !== rec.id) Object.assign(compSeen, { id: rec.id, mic: false, comp: false, said: 0 });
  if (!shown) { $("rec-computer-hint").hidden = true; $("rec-computer-hint").textContent = ""; return; }
  const db = comp.on && kind === "" ? comp.level_db : -96;
  if (kind === "" && rec.level_db > -50) compSeen.mic = true;
  if (db > -60) compSeen.comp = true;
  $("rec-computer").classList.toggle("off", !comp.on);
  $("rec-computer-label").textContent = comp.on ? "Computer audio" : "Computer audio stopped";
  $("rec-computer-fill").style.transform = `scaleX(${levelAmount(db).toFixed(3)})`;
  const now = Date.now();
  if (now - compSeen.said > 1000) {                  // the meter's value, once a second
    compSeen.said = now;
    $("rec-computer-level").setAttribute("aria-valuenow", String(Math.round(Math.max(-60, Math.min(0, db)))));
  }
  // Every active output is recorded, so silence here means nothing has
  // played yet (a call not joined, or its sound off). A check at the start
  // of each recording: from 60 s of recorded time, if the microphone has
  // heard anything and the computer nothing at all, say so.
  // Once the computer has played anything the hint stays off for this
  // recording: later silence is normal (a quiet call, a muted pause), and a
  // change of output has its own note under the card. The hint is a polite
  // live region: a screen reader reads a change made while it is shown, not
  // text that arrives with it, so it is shown empty first and filled at the
  // next poll, and its text is set only when it changes (never re-read at
  // each poll).
  const quiet = kind === "" && comp.on && rec.seconds >= 60 && compSeen.mic && !compSeen.comp;
  const hint = $("rec-computer-hint");
  if (!quiet) { hint.hidden = true; hint.textContent = ""; return; }
  const text = "No sound from the computer yet. If you are on a call, check that you can hear it on this computer.";
  if (hint.hidden) hint.hidden = false;
  else if (hint.textContent !== text) hint.textContent = text;
}

function reportItem(rep) {
  const dismiss = linkButton("Dismiss",
    () => attempt(() => call("discard_recording_report", rep.id).then(renderRecorder)));
  const events = rep.events.length ? ` ${rep.events.map(eventText).join("; ")}.` : "";
  if (rep.pending && rep.recovered) {                // left when the app closed: the user decides
    const when = rep.started_at ? ` from ${new Date(rep.started_at * 1000).toLocaleString()}` : "";
    // A two-track recording says how much of each track was saved (#108).
    const saved = rep.computer_seconds == null ? `about ${fmtSeconds(rep.seconds)}`
      : `about ${fmtSeconds(rep.seconds)} of microphone and ${fmtSeconds(rep.computer_seconds)} of computer audio`;
    return notice("alert", `Recovered recording${when} (${saved}). ${rep.note}`,
      button("Keep and transcribe", () => attempt(() => call("retry_recording_import", rep.id).then(renderRecorder)),
             { primary: true }),
      linkButton("Delete…", () => confirmDialog({ title: "Delete this recording?",
        body: "The recovered audio is deleted from this computer. It is not transcribed and cannot be recovered.",
        check: "Delete the recording.", limits: false, danger: true,
        onConfirm: () => attempt(() => call("delete_pending_recording", rep.id).then(renderRecorder)) })));
  }
  if (rep.pending && rep.discarded)                 // discarded: it can only be deleted
    return notice("alert", `Discarded recording (${fmtSeconds(rep.seconds)}) could not be deleted yet. ${rep.note}`,
      linkButton("Delete now", () => attempt(() => call("delete_pending_recording", rep.id).then(renderRecorder))));
  if (rep.pending)
    return notice("alert", `Recording: ${fmtSeconds(rep.seconds)}. ${rep.note}${events}`,
      button("Try again", () => attempt(() => call("retry_recording_import", rep.id).then(renderRecorder)),
             { primary: true }),
      linkButton("Delete recording…", () => confirmDialog({ title: "Delete this recording?",
        body: "The saved audio of this recording is deleted from this computer. It cannot be recovered.",
        check: "Delete the recording.", limits: false, danger: true,
        onConfirm: () => attempt(() => call("delete_pending_recording", rep.id).then(renderRecorder)) })),
      dismiss);
  const done = rep.note ? ` ${rep.note}` : rep.meeting ? " It is in Meetings now." : " It is being imported.";
  if (rep.lid)                                      // a lid setting that could not be put back
    return notice("alert", rep.note, dismiss);
  if (rep.events.some((e) => e.kind === "sleep"))
    return notice("alert", `The computer went to sleep, so the recording stopped. ${fmtSeconds(rep.seconds)} recorded before that was saved. If you closed the lid: to keep recording with it closed, set Windows to do nothing when the lid is closed; the recording then carries on until you stop it here.${done}`, dismiss);
  if (rep.events.some((e) => e.kind === "device_lost"))
    return notice("alert", `Microphone lost. ${fmtSeconds(rep.seconds)} recorded before it disconnected was saved.${done}`, dismiss);
  return notice("done", `Recording: ${fmtSeconds(rep.seconds)}${rep.computer ? " of microphone and computer audio" : ""} saved.${done}${events}`, dismiss);
}
