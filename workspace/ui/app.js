// Unminute UI (#20). All meeting text is rendered with textContent,
// never innerHTML, so markup in a transcript is shown as text, not executed.
// The page holds no file paths: recordings arrive through the native file
// dialog or a native drop, handled in Python.
"use strict";

const $ = (id) => document.getElementById(id);
const DAY = 86400;
const POLL_MS = 2000;        // while something is happening (docs/RELEASE_STANDARD.md, section 3)
const IDLE_POLL_MS = 10000;  // when nothing is; never while the window is hidden
const LIST_REFRESH_POLLS = 6;  // the lists are fetched on a change, and every 6th poll (a minute idle)
const PAGE = 1000;           // transcript rows per bridge call
const SAVE_DELAY_MS = 800;   // autosave after typing stops

const state = {
  view: "meetings", mid: null, detail: null, total: -1, signature: "",
  hits: [], hit: -1, editing: null, saveTimer: null,
  version: null,                 // null = current version; else an earlier one (read only)
  drawnFor: null,                // "meeting|version" of the transcript rows last drawn in full
  saving: Promise.resolve(),     // edits are saved strictly in order
  listsSig: "",                  // the meeting list last drawn
  changes: null,                 // status().changes at the last poll
  listsSeen: null,               // ...when the lists were last fetched, and where
  listsView: null,
  sinceLists: 0,                 // polls since then
  active: false,                 // something is being processed or imported
  wasActive: false,              // ...at the poll before
  recordingLive: false,          // a recording is open and running
  recActive: false,              // a recording is open or saving: the microphone lists wait (#369)
  locked: null,                  // status().locked: the workspace opened locked (#111 C4b)
};

// -- bridge -----------------------------------------------------------------
// Calls return {ok, value} or {ok: false, error}; they never throw in Python,
// so no stack trace (with file paths) reaches the page.
async function call(name, ...args) {
  const r = await window.pywebview.api[name](...args);
  if (!r || !r.ok) throw new Error((r && r.error) || "The request failed.");
  return r.value;
}

// Short messages appear as a toast. Confirmations ("Saved") clear after a few
// seconds, other messages after longer; a sticky one (an import in progress)
// stays until the app replaces it.
const OK_MESSAGE = /^(Saved|Copied|Name saved|Template saved|Download started|Queued|Folder added|Meeting discarded|Recording saved|Edit undone|Speaker change undone|Token removed|Removed)/;
let toastTimer = null;
function say(message, { sticky = false } = {}) {
  const t = $("status");
  clearTimeout(toastTimer);
  t.textContent = message || "";
  t.classList.toggle("ok", OK_MESSAGE.test(message || ""));
  t.dataset.sticky = sticky && message ? "1" : "";
  if (message && !sticky)
    toastTimer = setTimeout(() => {
      if (state.statusLine) say(state.statusLine, { sticky: true });   // a lasting status returns
      else say("");
    }, OK_MESSAGE.test(message) ? 3000 : 8000);
}

async function attempt(fn) {
  try { return await fn(); } catch (err) { say(String((err && err.message) || err)); }
}

// -- formatting -------------------------------------------------------------
function fmtTime(t) {
  const s = Math.floor(t), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
function fmtDate(epoch) { return new Date(epoch * 1000).toLocaleString(); }
function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
    else if (k in e) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c !== null && c !== undefined) e.append(c);
  return e;
}
const SCOPES = { all: "everything", audio: "the audio", transcript: "the transcript" };
const STAGES = {
  inbox: "Transcribing", review: "In review", handoff: "Reviewed: hand off",
  cleanup: "Cleanup",
};

// -- navigation -------------------------------------------------------------
const VIEWS = ["meetings", "meeting", "handoff", "settings", "downloads", "setup", "help"];
// The sidebar item each view belongs to.
const NAV_OF = { meetings: "meetings", meeting: "meetings", handoff: "meetings", settings: "settings",
                 downloads: "settings", setup: "settings", help: "help" };

function show(view) {
  if (state.locked) return;            // only the locked screen (#111 C4b)
  if (view === "inbox" || view === "active") view = "meetings";   // their earlier names
  if (view !== "meeting") stopAudio();
  if (view !== "meeting" && view !== "handoff") stopEditing();
  if (view !== state.view) { suState.sig = ""; state.downloadsSig = ""; state.listsSig = ""; }   // rebuilt on arrival
  if (view !== state.view) micsStale();                 // returning: the microphones may have changed (#369)
  if (view !== "meetings") stopRecTimer();
  state.view = view;
  document.body.dataset.view = view;
  setMenu(false);
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== view;
  for (const b of document.querySelectorAll(".nav"))
    b.setAttribute("aria-current", b.dataset.view === NAV_OF[view] ? "page" : "false");
  window.scrollTo(0, 0);
  refresh();
}

function setMenu(open) {
  document.body.classList.toggle("menu-open", open);
  $("menu").setAttribute("aria-expanded", String(open));
}

// handoff: open the Hand off as soon as the meeting has loaded (from the list).
async function openMeeting(mid, { handoff = false } = {}) {
  stopEditing();
  state.pendingHandoff = handoff;
  state.mid = mid; state.total = -1; state.signature = ""; state.hits = []; state.hit = -1;
  dropSearches();
  state.version = null; state.items = []; state.speakerList = []; state.playingRow = null; state.speakersSig = "";
  $("m-search").value = ""; $("m-hits").textContent = ""; $("m-hit-nav").hidden = true;
  $("m-search").closest(".search").classList.remove("active");
  $("m-again-form").hidden = true;
  $("m-again").setAttribute("aria-expanded", "false");
  const audio = $("player");
  stopAudio();
  audio.removeAttribute("src"); audio.load();
  setTab("transcript");
  $("transcript").replaceChildren();
  show("meeting");
}

// -- small builders ---------------------------------------------------------------
const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  alert: ["M12 8v5", "M12 16h.01", "circle:12,12,9"],
  clock: ["M12 7v5l3 2", "circle:12,12,9"],
  tick: ["M5 12l5 5 9-10"],
};
function icon(name, size = 20) {
  const s = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({ width: size, height: size, viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": name === "tick" ? 3 : 2, "stroke-linecap": "round",
    "stroke-linejoin": "round", "aria-hidden": "true" })) s.setAttribute(k, v);
  for (const d of ICONS[name]) {
    let p;
    if (d.startsWith("circle:")) {
      const [cx, cy, r] = d.slice(7).split(",");
      p = document.createElementNS(SVG_NS, "circle");
      p.setAttribute("cx", cx); p.setAttribute("cy", cy); p.setAttribute("r", r);
    } else {
      p = document.createElementNS(SVG_NS, "path");
      p.setAttribute("d", d);
    }
    s.append(p);
  }
  return s;
}
const button = (text, onClick, { primary = false, title = "" } = {}) =>
  el("button", { type: "button", class: primary ? "primary" : "", textContent: text, title,
                 on: { click: onClick } });
const linkButton = (text, onClick, title = "") =>
  el("button", { type: "button", class: "link-plain", textContent: text, title, on: { click: onClick } });
function notice(kind, text, ...actions) {
  const lead = kind === "alert" ? icon("alert") : kind === "hold" ? icon("clock")
    : el("span", { class: "tick" }, icon("tick", 12));
  return el("li", { class: `notice ${kind}` }, lead, el("span", { class: "notice-text", textContent: text }),
            ...actions);
}
const joinParts = (...parts) => parts.filter(Boolean).join(" · ");

// -- meetings page -----------------------------------------------------------------
function dayWord(days) { return days === 0 ? "Today" : days === 1 ? "Yesterday" : `${days} days old`; }
function lengthWord(s) { return s == null ? null : s < 60 ? "under 1 min" : `${Math.round(s / 60)} min`; }
function speakersWord(n) { return n ? `${n} speaker${n === 1 ? "" : "s"}` : null; }
const MODE = { quiet: "Quiet mode", faster: "Faster mode" };

function discardLink(m) {
  // The app asks in its own window, which the page cannot answer (#111 A6).
  return linkButton("Discard…", () => attempt(async () => {
    const r = await call("discard_meeting", m.id);
    say(r && r.cancelled ? declined(r, "Nothing was discarded.") : "Meeting discarded.");
    refresh();
  }), "Delete a meeting you do not need (for example a test recording)");
}

// Retention reminders (#111 C6): a meeting whose next step has waited past the
// threshold in force. It blocks nothing; the chip and the banner only say so.
const RETENTION = { action_required: "Action required", due: "Review due" };

function titleLine(m) {
  return el("div", { class: "row-title" },
    el("button", { type: "button", class: "open", textContent: m.title, on: { click: () => openMeeting(m.id) } }),
    m.held ? el("span", { class: "held", textContent: "Held" }) : null,
    m.retention ? el("span", { class: `chip-retention ${m.retention === "due" ? "retention-due" : "retention-action"}`,
                               textContent: RETENTION[m.retention] }) : null);
}

// What a meeting needs next decides its group: needs attention, ready for
// you (review or hand off), transcribing (including speaker identification)
// or cleaning up.
function groupOf(m) {
  if (m.stage === "cleanup") return "cleanup";
  if (m.stage === "inbox") return m.error ? "attention" : "work";
  if (m.busy === "speakers" || m.busy === "rerun" || (m.paused && m.stage === "review")) return "work";
  return "ready";
}

function readyRow(m) {
  const review = m.stage === "review";
  const meta = joinParts(dayWord(m.age_days), lengthWord(m.duration_s), speakersWord(m.speaker_count),
                         !review && m.audio_available === false ? "audio removed" : null);
  return el("li", {},
    el("div", { class: "row-main" }, titleLine(m), el("span", { class: "row-meta", textContent: meta })),
    el("span", { class: `pill ${review ? "review" : "handoff"}`,
                 textContent: review ? "Ready to review" : "Ready to hand off" }),
    el("div", { class: "row-actions" },
      review ? button("Review", () => openMeeting(m.id), { primary: true })
             : button("Hand off…", () => openMeeting(m.id, { handoff: true })),
      review ? discardLink(m) : null));
}

function attentionRow(m) {
  return el("li", {},
    el("div", { class: "row-main" }, titleLine(m),
       el("span", { class: "row-meta bad", textContent: joinParts(dayWord(m.age_days), m.error) })),
    el("span", { class: "pill bad", textContent: "Needs attention" }),
    el("div", { class: "row-actions" },
      button("Try again", () => attempt(() => call("resume", m.id).then(refresh)), { primary: true }),
      m.can_retranscribe ? linkButton("Transcribe with another model…", () => transcribeAgain(m.id, m.model, !!m.error),
        "Uses the model chosen in Settings > Speech model. Finished parts are kept as an earlier version.")
        : null,
      discardLink(m)));
}

function cleanupRow(m) {
  const jobs = m.cleanup || [];
  const failed = jobs.find((j) => j.state === "failed");
  const waiting = jobs.find((j) => j.state === "awaiting_confirmation");
  const open = jobs.find((j) => j.state !== "done");
  const meta = failed ? `Cleanup failed: ${failed.note || "unknown reason"}`
    : waiting ? "A hold was released. Open the meeting to confirm the cleanup."
    : joinParts(dayWord(m.age_days), open ? `removing the ${open.phase === "audio" ? "audio" : "transcript"}` : "cleanup pending");
  return el("li", {},
    el("div", { class: "row-main" }, titleLine(m),
       el("span", { class: `row-meta${failed ? " bad" : ""}`, textContent: meta })),
    el("span", { class: `pill ${failed ? "bad" : "cleanup"}`, textContent: failed ? "Cleanup failed" : "Cleanup pending" }),
    failed ? el("div", { class: "row-actions" },
      button("Try again", () => attempt(() => call("retry_cleanup", failed.id).then(refresh)), { primary: true }))
      : null);
}

// The app's own confirmation cancelled, or asked again too soon after a Cancel (#111 A6).
function declined(r, text) {
  return r.retry_soon ? "Wait a moment, then try again." : text;
}

// Transcribing and speaker identification: a card per meeting with its
// progress under the title. Speaker identification has no safe point inside,
// so Pause stops the pass and Resume restarts it.
function workRow(m, busyTitle) {
  const pause = (title) => button("Pause", () => attempt(() => call("pause", m.id).then(refresh)), { title });
  const resume = () => button("Resume", () => attempt(() => call("resume", m.id).then(refresh)), { primary: true });
  const stopNow = () => button("Stop now", () => attempt(() => call("stop_now", m.id).then(refresh)),
                               { title: "Stops at once; the part in progress is redone later" });
  const skip = () => button("Skip speakers", () => attempt(async () => {
    await call("skip_speakers", m.id);
    say("Speaker identification skipped. The meeting is ready to review.");
    await refresh();
  }), { title: "Go straight to review and hand off without speaker labels. You can still identify speakers later." });
  let meta, pill = null, bar = null, actions;
  if (m.stage === "inbox" && m.paused) {
    meta = `${m.progress}% · paused by you`;
    pill = ["paused", "Paused"];
    bar = ["paused", m.progress];
    actions = [resume(), discardLink(m)];
  } else if (m.unfinished) {                       // #193: kept by a hold, or after Continue anyway
    meta = "the import did not finish";
    pill = ["waiting", "Import did not finish"];
    actions = [discardLink(m)];
  } else if (m.stage === "inbox" && m.busy === "transcribe") {
    meta = joinParts(`${m.progress}%`, MODE[state.mode]);
    bar = ["", m.progress];
    actions = [pause("Stops at the next safe point; nothing is lost"), stopNow(), discardLink(m)];
  } else if (m.stage === "inbox") {
    meta = busyTitle ? `starts after ${busyTitle}` : null;
    pill = ["waiting", "Waiting"];
    actions = [pause("Keeps it from starting until you resume it"), discardLink(m)];
  } else if (m.paused) {
    meta = "text ready · speaker identification paused";
    pill = ["paused", "Paused"];
    actions = [resume(), m.can_skip ? skip() : null, discardLink(m)];
  } else {
    meta = "text ready · working out who spoke";
    pill = ["", "Identifying speakers"];
    bar = ["busy", null];
    actions = [pause("Stops speaker identification now; Resume starts it again from the beginning. The transcript is not affected."),
               m.can_skip ? skip() : null, discardLink(m)];
  }
  const progress = bar ? el("div", { class: `progress ${bar[0]}`, role: "progressbar",
    "aria-label": `${m.title}: ${bar[1] === null ? "working" : `${bar[1]}% transcribed`}`,
    ...(bar[1] === null ? {} : { "aria-valuenow": String(bar[1]), "aria-valuemin": "0", "aria-valuemax": "100" }) },
    el("span", {})) : null;
  if (progress && bar[1] !== null) progress.firstChild.style.width = `${bar[1]}%`;
  return el("li", {},
    el("div", { class: "row-main" },
      el("div", { class: "row-top" }, titleLine(m), el("span", { class: "row-meta", textContent: meta })),
      progress),
    pill ? el("span", { class: `pill ${pill[0]}`, textContent: pill[1] }) : null,
    el("div", { class: "row-actions" }, ...actions));
}

const GROUPS = [
  ["attention", "Needs attention", "rows"],
  ["ready", "Ready for you", "rows"],
  ["work", "Transcribing", "work"],
  ["cleanup", "Cleaning up", "rows"],
];

// Rows that require action first, then those due, otherwise as listed.
const RETENTION_ORDER = { action_required: 0, due: 1 };
const byRetention = (a, b) => (RETENTION_ORDER[a.retention] ?? 2) - (RETENTION_ORDER[b.retention] ?? 2);

// reminders: what renderReminders shows (the Meetings view only), so each row
// carries its retention state without another bridge call.
async function renderLists(reminders) {
  const states = new Map(((reminders && reminders.retention) || []).map((r) => [r.meeting, r.state]));
  const items = (await call("meetings")).map((m) => ({ ...m, retention: states.get(m.id) || null }));
  // The sidebar count: meetings waiting for the user.
  const waiting = items.filter((m) => ["attention", "ready"].includes(groupOf(m))).length;
  $("nav-count").hidden = !waiting;
  $("nav-count").textContent = String(waiting);
  $("nav-count-sr").textContent = waiting ? `, ${waiting} waiting for you` : "";
  if (state.view !== "meetings") return;
  const sig = JSON.stringify([items, state.mode]);     // workRow shows the mode
  if (sig === state.listsSig) return;           // unchanged: keep the rows, buttons and focus
  state.listsSig = sig;
  const busy = items.find((m) => m.busy === "transcribe");
  const sections = [];
  for (const [key, title, kind] of GROUPS) {
    const mine = items.filter((m) => groupOf(m) === key).sort(byRetention);
    if (!mine.length) continue;
    const row = { attention: attentionRow, ready: readyRow, cleanup: cleanupRow,
                  work: (m) => workRow(m, busy && busy.id !== m.id ? busy.title : null) }[key];
    sections.push(el("section", { class: "group", "aria-labelledby": `g-${key}` },
      el("h2", { id: `g-${key}`, textContent: title }),
      el("ul", { class: kind }, ...mine.map(row))));
  }
  $("groups").replaceChildren(...sections);
  $("meetings-empty").hidden = items.length > 0;
}

async function renderStatus() {
  const s = await call("status");
  if (s.locked) {                      // locked after all (a first status() that failed)
    renderLocked(s.locked);
    return;
  }
  state.mode = s.mode;
  state.changes = s.changes;
  state.wasActive = state.active;      // one more fast poll across the gap between jobs
  state.active = !!(s.busy || s.importing);
  const parts = [];
  if (s.importing) parts.push(`Importing ${s.importing} recording${s.importing > 1 ? "s" : ""}`);
  if (s.message) parts.push(s.message);
  const line = parts.join(" · ");
  if (line !== state.statusLine) {
    state.statusLine = line;
    if (line) say(line, { sticky: true });
    else if ($("status").dataset.sticky) say("");
  }
  $("import-errors").replaceChildren(...s.import_errors.map((e) => notice("alert", e,
    linkButton("Dismiss", () => attempt(() => call("clear_import_errors").then(refresh))))));
  renderIntegrity(s.integrity);
  renderFinishOnly(s.finish_only);
}

// Finish-only mode (#111 B5b): this version was opened for a workspace an
// update is waiting to take over. Record, import and drop are not offered
// (the app refuses them anyway); everything else works. Announced once.
const FINISH_ONLY_TEXT = "An update is waiting for this workspace. Finish your meetings here; " +
  "new meetings open in the updated version once this workspace is empty.";
function renderFinishOnly(on) {
  on = on === true;
  if (on === state.finishOnly) return;
  state.finishOnly = on;
  $("finish-only").hidden = !on;
  $("finish-only").replaceChildren(...(on ? [notice("hold", FINISH_ONLY_TEXT)] : []));
  $("start").classList.toggle("finish-only", on);
  $("s-scan").hidden = on;                   // Settings' "Check now": no watched-folder intake
  if (on) say(FINISH_ONLY_TEXT);
}

// The database check after an unexpected close (R11) found damage: work is
// held until the user chooses Continue anyway. Rebuilt only on a change, so
// the buttons keep their focus between polls; announced once when it appears.
function renderIntegrity(info) {
  const sig = info ? `${info.state}:${info.code || ""}` : "";
  if (sig === state.integritySig) return;
  const appeared = !!info && !state.integritySig;
  state.integritySig = sig;
  const box = $("integrity");
  box.hidden = !info;
  const text = `Unminute found damage in its workspace database (code ${info && info.code}). ` +
    "Processing, imports and cleanup are on hold, so as little as possible is written to it. " +
    "You can still read, copy and hand off meetings; changes you make are still saved. " +
    "Ask your IT team before you continue.";
  box.replaceChildren(...(info ? [notice("alert", text,
    linkButton("How to recover", () => attempt(() => call("open_help", "recovery"))),
    button("Continue anyway", () => attempt(async () => {
      await call("continue_despite_damage");
      await refresh();
      $("import").focus();              // the button has gone: keep the focus on the page
    })),
  )] : []));
  if (appeared) say(text);              // announced once; the notice itself stays
}

// The banner says what the organisation asks only for "Action required", and
// only when IT set ActionRequiredDays (C6-5). "Review due" is a nudge, not a
// deadline, so it never claims a rule, even when IT set ReviewReminderDays
// (the owner's decision, 22 October 2026).
function oneOrMany(n, one, many) { return n === 1 ? one : many; }
function retentionBanner(r) {
  const count = (list, level) => (list || []).filter((x) => x.state === level).length;
  const t = r.thresholds;
  const action = [count(r.retention, "action_required"), count(r.recordings_waiting, "action_required")];
  const due = [count(r.retention, "due"), count(r.recordings_waiting, "due")];
  const parts = [];
  if (action[0] || action[1]) {
    const days = t.action_days;
    if (action[0]) parts.push(`${action[0]} ${oneOrMany(action[0], "meeting needs", "meetings need")} action.`);
    if (action[1]) parts.push(`${action[1]} ${oneOrMany(action[1], "recording needs", "recordings need")} action: keep or delete ${oneOrMany(action[1], "it", "them")}.`);
    parts.push(t.managed.action ? `Your organisation asks for each step to be finished within ${days} days.`
                                : `Each has waited ${days} days or more.`);
    return notice("alert", parts.join(" "));
  }
  if (!(due[0] || due[1])) return null;
  const days = t.review_days;
  if (due[0]) parts.push(`${due[0]} ${oneOrMany(due[0], "meeting has", "meetings have")} waited ${days} days or more for ${oneOrMany(due[0], "its", "their")} next step.`);
  if (due[1]) parts.push(`${due[1]} ${oneOrMany(due[1], "recording has", "recordings have")} waited ${days} days or more for Keep or Delete.`);
  return notice("hold", parts.join(" "));
}

// -- locked (#111 C4b) ---------------------------------------------------------------
// Windows would not unlock the workspace key, so the app opened locked (C4a):
// the bridge refuses everything but api.LOCKED_METHODS. The page then shows
// only this screen, asks for nothing else (no list, meeting or setting, so no
// title or name can appear) and never polls: only closing and opening
// Unminute can change the state. Fixed words and codes, shown as text.
// The paragraph is two short sentences, what happened and that nothing is
// lost (the owner, 22 Oct 2026); what to do is in IT's steps and the guide.
const LOCKED_MISMATCH = "Unminute's key does not match its workspace, so it cannot open your " +
  "meetings.";
const LOCKED_REASONS = {
  windows: "Windows could not unlock Unminute's encryption key for your account on this computer.",
  key: LOCKED_MISMATCH,
  protector: LOCKED_MISMATCH,
  missing: "Unminute's key record is missing from its workspace, so it cannot open your meetings.",
};
const LOCKED_OTHER = "Unminute cannot use its encryption key, so it cannot open your meetings.";
const LOCKED_KEPT = "Your meetings are still on this computer, and nothing has been deleted.";

// A Windows error number as Windows tools show it: 0x8009000B for -2146893813.
const hex32 = (n) => "0x" + (n >>> 0).toString(16).toUpperCase().padStart(8, "0");

function renderLocked(info) {
  state.locked = info;
  clearTimeout(pollTimer);              // nothing polls while locked
  pollTimer = null;
  stopRecTimer();
  setMenu(false);
  $("sidebar").hidden = true;
  document.querySelector(".topbar").hidden = true;
  $("drop-overlay").hidden = true;
  for (const v of VIEWS) $(`view-${v}`).hidden = true;
  document.body.dataset.view = "locked";
  $("view-locked").hidden = false;
  say("");                              // nothing left from a usual start
  // The damage code itself is in the codes below.
  const damage = info.damage ? " Unminute also found damage in its workspace database." : "";
  // The alert is announced once, as it is filled in.
  $("locked-reason").textContent =
    `${LOCKED_REASONS[info.reason] || LOCKED_OTHER}${damage} ${LOCKED_KEPT}`;
  const codes = [["Code", info.code]];
  if (Number.isInteger(info.win_error))
    codes.push(["Windows error", `${info.win_error} (${hex32(info.win_error)})`]);
  codes.push(["Database damage", info.damage]);
  const shown = codes.filter(([, value]) => value);
  $("locked-codes").replaceChildren(...shown.flatMap(([name, value]) =>
    [el("dt", { textContent: name }), el("dd", { textContent: String(value) })]));
  $("locked-codes").hidden = !shown.length;
  // Focus moves to the heading, which is focusable only until it loses focus:
  // it is not a control, so the tab order stays the buttons'.
  const heading = $("h-locked");
  heading.tabIndex = -1;
  heading.addEventListener("blur", () => heading.removeAttribute("tabindex"), { once: true });
  heading.focus();
}

function wireLocked() {
  $("locked-copy").addEventListener("click", () => attempt(async () => {
    await call("copy_diagnostics");
    say("Copied the details for IT. They hold no meeting content.");
  }));
  $("locked-help").addEventListener("click", () => attempt(() => call("open_help", "recovery")));
  $("locked-close").addEventListener("click", () => attempt(() => call("close_app")));
}

function renderReminders(r) {
  const lines = [];
  const banner = retentionBanner(r);
  if (banner) lines.push(banner);
  for (const h of r.holds_due)
    lines.push(notice("hold", `Hold due for review: ${h.title}.`,
      button("Keep", () => attempt(() => call("keep_hold", h.id).then(refresh)),
             { title: `Kept because: ${h.reason}` }),
      linkButton("Open meeting", () => openMeeting(h.meeting))));
  for (const j of r.awaiting_confirmation)
    lines.push(notice("hold", `${j.title}: a hold was released. The ${j.phase} cleanup is waiting for your confirmation.`,
      linkButton("Open meeting", () => openMeeting(j.meeting))));
  $("reminders").replaceChildren(...lines);
  $("reminders").hidden = lines.length === 0;
}

// -- one meeting ---------------------------------------------------------------
// What the drawn transcript depends on, apart from how far transcription has
// got (the progress, which renderMeeting adds in front).
function signatureOf(d) {
  return [d.speakers && d.speakers.status, d.speakers && d.speakers.started_at,
          d.model, d.reviewed_at,
          d.audio_available].join("|");
}

function whenWord(epoch) {
  const d = new Date(epoch * 1000), now = new Date();
  const time = d.toLocaleTimeString("en-NZ", { hour: "numeric", minute: "2-digit" });
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (epoch >= day) return `today, ${time}`;
  if (epoch >= day - DAY) return `yesterday, ${time}`;
  return `${d.toLocaleDateString("en-NZ")}, ${time}`;
}
const dateWord = (epoch) => new Date(epoch * 1000).toLocaleDateString("en-NZ");
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function metaLine(d) {
  if (state.version !== null) {
    const v = d.older_versions.find((x) => x.index === state.version);
    return v ? `Earlier version: ${v.model_label} (${dateWord(v.created_at)}) · ${plural(v.edits, "edited passage")}` : "";
  }
  const when = d.reviewed_at ? `Reviewed ${whenWord(d.reviewed_at)}` : `Imported ${whenWord(d.created_at)}`;
  return joinParts(when, lengthWord(d.duration_s), speakersWord(d.speaker_count), d.model_label);
}

async function renderMeeting() {
  if (!state.mid) return;
  let d;
  try { d = await call("detail", state.mid); }
  catch (err) { say(String(err.message || err)); show("meetings"); return; }
  state.detail = d;
  if (state.pendingHandoff) {
    state.pendingHandoff = false;
    if (d.reviewed_at && !d.final_saved_at) openHandoff();
  }
  const s = d.speakers;
  const mid = state.mid;
  const list = s && s.status === "done" ? await attempt(() => call("speakers", mid)) : null;
  if (mid !== state.mid) return;
  state.speakerList = list ? list.speakers : [];
  state.speakerUndo = !!(list && list.can_undo);
  if (document.activeElement !== $("m-title")) { $("m-title").value = d.title; fitTitle(); }
  $("m-badges").textContent = d.holds.length ? "Held" : "";
  $("m-meta").textContent = metaLine(d);
  $("m-meta-short").textContent = state.version !== null ? metaLine(d)
    : joinParts(dayWord(Math.floor((Date.now() / 1000 - d.created_at) / DAY)), lengthWord(d.duration_s), speakersWord(d.speaker_count));
  renderHead(d);
  renderSteps(d);
  renderBanner(d);
  $("m-error").hidden = !d.error;
  $("m-error").textContent = d.error || "";
  const transcribing = d.stage === "inbox";
  $("m-progress").hidden = !transcribing;
  if (transcribing) {
    $("m-progress").querySelector(".progress > span").style.width = `${d.progress}%`;
    $("m-progress").querySelector(".progress").setAttribute("aria-valuenow", String(d.progress));
    $("m-progress-text").textContent = joinParts(`${d.progress}% transcribed`, d.paused ? "paused" : MODE[state.mode]);
  }
  const readOnly = !!d.reviewed_at || state.version !== null;
  $("m-hint").textContent = readOnly ? ""
    : transcribing ? "Transcribing. You can play and correct the finished parts now."
    : "Play passages and correct any words. Select text to edit it. Changes save as you go.";
  $("m-undo").hidden = readOnly;
  $("m-undo").disabled = !d.can_undo;
  $("m-more").hidden = !transcribing;
  $("m-player").hidden = !d.audio_available;
  if (!d.audio_available) $("player").pause();
  renderSpeakers(d);
  renderVersions(d);
  renderHolds(d);
  $("m-retranscribe").disabled = !d.can_retranscribe;     // also for a failed meeting (#244)

  const shape = signatureOf(d) + "|" + state.version + "|" + state.speakerList.map((x) => x.label + x.name).join(",");
  const sig = `${d.progress}|${shape}`;
  if (sig !== state.signature && !state.editing) {
    // Only the progress moved (transcription went on): new passages normally
    // just extend the list, so append them (#127). Anything else, or an
    // append that cannot be proved safe, redraws the whole list. An append
    // given up because an edit, another meeting or a newer draw started while
    // it loaded draws nothing: a redraw then would be thrown away too. The
    // signature goes back, unless another meeting is open or something else
    // has changed it since, so the next refresh tries the append again.
    const before = state.signature, mid = state.mid;
    const onlyProgress = before !== "" && before.slice(before.indexOf("|") + 1) === shape;
    state.signature = sig;
    const appended = onlyProgress ? await appendTranscript() : REDRAW;
    if (appended === REDRAW) await renderTranscript();
    else if (appended === ABANDONED && state.mid === mid && state.signature === sig) state.signature = before;
  }
}

// The title box grows with the title, so a long one wraps like a heading. Its
// height includes its border, which scrollHeight leaves out.
function fitTitle() {
  const t = $("m-title");
  t.style.height = "auto";
  t.style.height = `${t.scrollHeight + t.offsetHeight - t.clientHeight}px`;
}

// The header's action: Pause and Stop now while transcribing, Resume or Try
// again when stopped, otherwise the step's main action.
function renderHead(d) {
  const transcribing = d.stage === "inbox";
  $("m-pause").hidden = !transcribing || d.paused || !!d.error;
  $("m-stop").hidden = $("m-pause").hidden;
  $("m-resume").hidden = !(d.paused || d.error) || !!d.reviewed_at;
  $("m-resume").textContent = d.error ? "Try again" : "Resume";
  const action = state.version !== null || transcribing ? null
    : d.stage === "review" ? "review" : d.stage === "handoff" ? "handoff" : null;
  $("m-primary").hidden = !action;
  $("m-primary").dataset.action = action || "";
  $("m-primary-label").textContent = action === "review" ? "Transcript reviewed…" : "Hand off…";
  $("m-primary-tick").toggleAttribute("hidden", action !== "review");   // SVG: the attribute, not .hidden
  $("m-primary-arrow").toggleAttribute("hidden", action !== "handoff");
}

const STEP_NAMES = ["Transcribe", "Review", "Hand off", "Clean up"];
function renderSteps(d) {
  const done = [d.stage !== "inbox", !!d.reviewed_at, !!d.final_saved_at, false];
  const now = done.indexOf(false);
  $("steps").replaceChildren(...STEP_NAMES.map((name, i) => {
    const kind = done[i] ? "done" : i === now ? "now" : "";
    const li = el("li", { class: kind },
      el("span", { class: "step-bar" }),
      el("span", { class: "step-label" },
        el("span", { class: done[i] ? "step-dot done-tick" : "step-dot", "aria-hidden": "true",
                      textContent: done[i] ? "" : String(i + 1) }),
        el("span", { textContent: name }, done[i] ? el("span", { class: "sr", textContent: ", done" }) : null)));
    if (i === now) li.setAttribute("aria-current", "step");
    return li;
  }));
  $("m-step-short").textContent = `Step ${now + 1} of 4: ${STEP_NAMES[now]}`;
}

// One banner under the steps: an earlier version, cleanup, or the review lock.
function renderBanner(d) {
  const jobs = d.cleanup || [];
  const failed = jobs.find((j) => j.state === "failed");
  const waiting = jobs.find((j) => j.state === "awaiting_confirmation");
  let kind = "", text = "", icon = "lock", action = null;
  if (state.version !== null) {
    kind = "hold"; icon = "clock";
    text = "You are viewing an earlier version (read only). Its edits are kept as they were.";
    action = ["Back to the current version", () => viewVersion(null)];
  } else if (failed) {
    kind = "bad"; icon = "clock";
    text = `Cleanup failed: ${failed.note || "unknown reason"}`;
    action = ["Try again", () => attempt(() => call("retry_cleanup", failed.id).then(refresh))];
  } else if (waiting) {
    kind = "hold"; icon = "clock";
    text = `A hold was released. The ${waiting.phase} cleanup is waiting for your confirmation.`;
    action = ["Confirm cleanup…", () => confirmCleanup(waiting)];
  } else if (d.final_saved_at) {
    text = "Final document saved. Unminute is removing this meeting's working copies.";
  } else if (d.reviewed_at) {
    action = ["Final document saved…", () => confirmFinal(d)];
    text = d.audio_available
      ? "Reviewed and locked. The text can no longer be edited. The audio is kept while a hold covers it."
      : "Reviewed and locked. The audio was removed after the transcript review was confirmed, so playback and transcribing again are no longer available.";
  }
  $("m-banner").hidden = !text;
  $("m-banner").className = `m-banner ${kind}`.trim();
  $("m-playback").textContent = text;
  $("m-banner-lock").toggleAttribute("hidden", icon !== "lock");
  $("m-banner-clock").toggleAttribute("hidden", icon !== "clock");
  $("m-banner-action").hidden = !action;
  if (action) $("m-banner-action").textContent = action[0];
  state.bannerAction = action ? action[1] : null;
}

function confirmCleanup(j) {
  confirmDialog({
    title: `Confirm the ${j.phase} cleanup`,
    body: "The hold on this material was released. Nothing was removed when it was released; this confirmation lets the cleanup go ahead.",
    check: `Remove the ${j.phase === "audio" ? "audio" : "transcript working content"} now.`,
    onConfirm: () => attempt(() => call("confirm_cleanup", j.id).then(refresh)),
  });
}

// -- side panel -------------------------------------------------------------------
function setTab(tab) {
  state.tab = tab;
  $("view-meeting").dataset.tab = tab;
  const side = tab === "transcript" ? (state.sideTab || "speakers") : tab;
  if (tab !== "transcript") state.sideTab = tab;
  for (const b of document.querySelectorAll("#view-meeting [role=tab]")) {
    const on = b.closest(".wide-tabs") ? b.dataset.tab === side : b.dataset.tab === tab;
    b.setAttribute("aria-selected", String(on));
  }
  for (const name of ["speakers", "versions", "holds"]) $(`tab-${name}`).hidden = name !== side;
}

function speakerClass(label) {
  const i = state.speakerList.findIndex((s) => s.label === label);
  return i < 0 ? "none" : `sp${(i % 3) + 1}`;
}
function initials(name) {
  const n = String(name || "").trim();
  const numbered = /^Speaker (\d+)$/i.exec(n);
  if (numbered) return `S${numbered[1]}`;
  const words = n.split(/\s+/).filter(Boolean);
  if (!words.length) return "?";
  return (words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2)).toUpperCase();
}

function renderSpeakers(d) {
  const s = d.speakers;
  const sig = JSON.stringify([s, d.stage, d.busy, d.audio_available, d.reviewed_at, d.can_skip,
                              state.version, state.speakerList, state.speakerUndo]);
  if (sig === state.speakersSig) return;
  state.speakersSig = sig;
  const box = $("m-speakers");
  const card = (cls, title, text, ...extra) => el("div", { class: `panel-card ${cls}` },
    title, text ? el("span", { class: "panel-text", textContent: text }) : null, ...extra);
  const canSkip = d.can_skip && state.version === null;    // the app says when Skip applies
  const skip = () => canSkip ? button("Skip speaker identification", () => attempt(async () => {
    await call("skip_speakers", state.mid);
    say("Speaker identification skipped. The transcript is ready to review and hand off.");
    await refresh();
  }), { title: "Go straight to review and hand off without speaker labels. You can still identify speakers later." }) : null;
  let content = null;
  if (!s && d.stage === "inbox")
    content = card("soft", el("span", { class: "panel-title", textContent: "Not identified yet" }),
      "Speakers are worked out once the text is ready. You can review the words in the meantime. " +
      "For one or two people you can skip this.", skip());
  else if (!s)
    content = card("soft", el("span", { class: "panel-title", textContent: "Not identified" }),
      "Speaker identification runs after transcription when it is downloaded and turned on in Settings.",
      skip());
  else if (s.status === "skipped" && d.can_skip)     // Identify again is queued after a skip
    content = card("soft", el("span", { class: "panel-title", textContent: "Waiting to identify speakers" }),
      "Identify again starts when the computer is free. Skip to keep the transcript without speaker labels.",
      skip());
  else if (s.status === "skipped")
    content = card("soft", el("span", { class: "panel-title", textContent: "Skipped" }),
      "No speaker labels. The transcript is ready to review and hand off as it is." +
      (canRerun(d) ? " To label speakers later, use Identify again." : ""));
  else if (s.status === "running")
    content = card("soft", el("span", { class: "running" }, el("span", { class: "spinner", "aria-hidden": "true" }),
        "Identifying speakers…"),
      "The transcript is ready to review while this runs.",
      d.busy ? button("Pause", () => attempt(() => call("pause", state.mid).then(refresh)),
                      { title: "Stops speaker identification now; Resume starts it again from the beginning." }) : null,
      skip());
  else if (s.status === "failed")
    content = card("bad", el("span", { class: "panel-title", textContent: "Speaker identification did not work" }),
      `${s.error || "Unknown reason"}. The transcript is still usable.`);
  else if (s.status === "interrupted")
    content = card("soft", el("span", { class: "panel-title", textContent: "Interrupted" }),
      s.error || "Speaker identification was interrupted and will start again from the beginning.",
      skip());
  box.replaceChildren(...(content ? [content] : []));
  box.hidden = !content;

  const total = state.speakerList.reduce((a, x) => a + (x.seconds || 0), 0);
  $("m-speaker-list").replaceChildren(...state.speakerList.map((sp) => {
    const share = total ? Math.round((100 * (sp.seconds || 0)) / total) : null;
    return el("li", { class: speakerClass(sp.label) },
      el("span", { class: "sp-avatar", "aria-hidden": "true", textContent: initials(sp.name) }),
      el("span", { class: "sp-text" }, el("span", { class: "sp-name", textContent: sp.name }),
        el("span", { class: "sp-count", textContent: joinParts(plural(sp.passages, "passage"), share === null ? null : `${share}%`) })),
      d.audio_available ? el("button", { type: "button", class: "round-btn", "aria-label": `Play an example of ${sp.name}`,
        on: { click: () => playExample(sp.label) } }, playIcon()) : null);
  }));
  // Where it actually ran, including an honest fallback note.
  let device = "";
  if (s && s.status === "done" && s.device) {
    if (s.device.startsWith("cuda")) device = "Ran on the graphics card.";
    else if (s.device.includes("graphics card not used")) device = "Ran on the processor: the graphics card could not be used this time.";
  }
  if (device) $("m-speaker-list").append(el("li", { class: "device-note", textContent: device }));
  const naming = canName();
  $("m-name-tools").hidden = !naming && !canRerun(d);
  $("m-names").hidden = !naming;
  $("m-names").disabled = !naming;
  $("m-undo-speakers").hidden = !(naming && state.speakerUndo);
  $("m-again").closest(".again-row").hidden = !canRerun(d);
  if (!canRerun(d)) { $("m-again-form").hidden = true; $("m-again").setAttribute("aria-expanded", "false"); }
}

function canRerun(d) {
  return d.audio_available && d.stage !== "inbox" && !d.reviewed_at && !d.busy && state.version === null;
}

function mergeIcon() {
  const s = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({ width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) s.setAttribute(k, v);
  for (const d of ["M6 4v6a6 6 0 0 0 6 6h6", "M15 13l3 3-3 3", "M6 20v-4"]) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", d);
    s.append(p);
  }
  return s;
}

function playIcon(size = 14) {
  const s = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({ width: size, height: size, viewBox: "0 0 24 24", "aria-hidden": "true" })) s.setAttribute(k, v);
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute("d", "M7 4l13 8-13 8z");
  p.setAttribute("fill", "currentColor");
  s.append(p);
  return s;
}

async function playExample(label) {
  const ex = await attempt(() => call("speaker_examples", state.mid, label, 0));
  const it = ex && ex.items[0];
  if (it) play(it.start, it.end, true);
}

// Where a version was transcribed and at what precision (#248), from what
// each committed part recorded: "Processor, int8", "Graphics card, float16", or
// both in the order used, with a note when the graphics card was given up.
function ranOnWord(ranOn) {
  if (!ranOn || !ranOn.length) return "";
  const where = (r) => (r.where === "gpu" ? "graphics card" : "processor");
  // Rows that read the same are said once; the format follows the rows left.
  const seen = [];
  for (const r of ranOn) {
    if (!seen.some((s) => where(s) === where(r) && s.precision === r.precision)) seen.push(r);
  }
  const parts = seen.map((r) => (seen.length === 1 ? `${where(r)}, ${r.precision}`
                                                   : `${where(r)} (${r.precision})`));
  const text = parts.join(", then ");
  const said = text.charAt(0).toUpperCase() + text.slice(1);
  return ranOn.some((r) => r.fallback) ? `${said} · the graphics card could not be used` : said;
}

function renderVersions(d) {
  const viewing = state.version !== null;
  const card = (eyebrow, model, edits, open, ranOn) => {
    const kids = [el("span", { class: "ver-eyebrow", textContent: eyebrow }),
                  el("span", { class: "ver-model", textContent: model }),
                  el("span", { class: "ver-edits", textContent: edits })];
    const ran = ranOnWord(ranOn);
    if (ran) kids.push(el("span", { class: "ver-edits", textContent: ran }));
    return open ? el("button", { type: "button", class: "ver-card", on: { click: open } }, ...kids)
      : el("div", { class: "ver-card now" }, ...kids);
  };
  const items = [el("li", {}, card("Current", d.model_label || "No model yet", plural(d.edits, "edited passage"),
    viewing ? () => viewVersion(null) : null, d.ran_on))];
  for (const v of d.older_versions) {
    const mine = state.version === v.index;
    items.push(el("li", {}, card(mine ? "Earlier · viewing" : "Earlier · read only",
      `${v.model_label} (${dateWord(v.created_at)})`,
      mine ? plural(v.edits, "edited passage") : `${plural(v.edits, "edited passage")} · Open`,
      mine ? null : () => viewVersion(v.index), v.ran_on)));
  }
  $("m-versions").replaceChildren(...items);
}

function viewVersion(index) {
  stopEditing();
  state.version = index;
  state.signature = "";
  $("m-search").value = ""; state.hits = []; state.hit = -1; $("m-hits").textContent = "";
  dropSearches();
  $("m-hit-nav").hidden = true;
  $("m-search").closest(".search").classList.remove("active");
  renderMeeting();
}

function renderHolds(d) {
  $("m-holds").replaceChildren(...d.holds.map((h) => el("li", {},
    el("span", { class: "hold-title", textContent: `Keeping ${SCOPES[h.scope]}` }),
    el("span", { class: "hold-text", textContent: `Reason: ${h.reason}. ` +
      (h.due_review ? "Due for review now." : `Review due ${dateWord(h.review_due_at)}.`) }),
    el("div", { class: "row" },
      button("Keep", () => attempt(() => call("keep_hold", h.id).then(refresh)),
             { title: "You looked at it and still need it" }),
      linkButton("Release…", () => confirmDialog({
        title: "Release this hold?",
        body: "Releasing a hold deletes nothing by itself. Any cleanup it was holding back waits for your confirmation.",
        check: "Release the hold.", limits: false,
        onConfirm: () => attempt(() => call("release_hold", h.id).then(refresh)),
      }))))));
  const waiting = (d.cleanup || []).filter((j) => j.state === "awaiting_confirmation");
  $("m-cleanup-notes").replaceChildren(...waiting.map((j) => el("div", { class: "panel-card soft" },
    el("span", { class: "panel-text", textContent: `A hold was released. The ${j.phase} cleanup is waiting for your confirmation.` }),
    button("Confirm cleanup…", () => confirmCleanup(j), { primary: true }))));
}

// -- transcript ----------------------------------------------------------------
// A passage shows its speaker's label when the speaker changes; the others
// show theirs on hover or focus, so any passage can be moved.
function transcriptRow(item, audio, turn) {
  const t = el("button", { type: "button", class: "t", textContent: fmtTime(item.t),
    disabled: !audio, title: audio ? "Play from here" : "Audio removed after review",
    on: { click: () => playFrom(item.t) } });
  const text = el("span", { class: "text" + (item.edited ? " edited" : ""), textContent: item.text,
    title: "Select to edit", tabIndex: 0 });
  text.dataset.id = item.id;
  text.addEventListener("click", () => startEditing(text));
  text.addEventListener("keydown", (e) => { if (e.key === "Enter" && !text.isContentEditable) { e.preventDefault(); startEditing(text); } });
  const chip = item.who || canName();
  const li = el("li", { class: turn ? "turn" : "" }, t,
    el("div", { class: "row-body" }, chip && turn ? whoButton(item, false) : null, text,
       item.overlap ? el("span", { class: "overlap", textContent: "(overlapping speech)" }) : null),
    chip && !turn ? whoButton(item, true) : null);
  li.dataset.id = item.id;
  return li;
}

// -- speaker names (#21) ---------------------------------------------------------
function canName() {
  const d = state.detail;
  return d && !d.reviewed_at && state.version === null && d.speakers && d.speakers.status === "done";
}

function whoButton(item, again) {
  const name = item.who || "No speaker";
  const b = el("button", { type: "button", class: `who ${item.label ? speakerClass(item.label) : "none"}${again ? " again" : ""}`,
                           title: "Change the speaker of this passage" },
    el("span", { class: "avatar", "aria-hidden": "true", textContent: item.who ? initials(item.who) : "?" }),
    el("span", { class: "who-name", textContent: name }));
  b.addEventListener("click", () => reassign(b, item));
  return b;
}

// Reassigning needs an explicit Move: arrow keys on a select fire "change"
// on every step, which must not move the passage.
async function reassign(button, item) {
  if (!canName()) { say("Speakers can be changed only before the review is confirmed."); return; }
  const open = button.closest("li").querySelector(".reassign");
  if (open) { open.querySelector("select").focus(); return; }
  const list = await attempt(() => call("speakers", state.mid));
  if (!list) return;
  const sel = el("select", { "aria-label": "Speaker for this passage" },
    ...list.speakers.map((s) => el("option", { value: s.label, textContent: s.name,
                                                 selected: s.label === item.label })),
    el("option", { value: "new", textContent: "Someone else (new speaker)" }));
  const body = button.closest("li").querySelector(".row-body");
  const box = el("div", { class: "reassign" }, el("label", {}, "Move this passage to ", sel));
  const restore = () => { if (box.isConnected) { box.remove(); button.hidden = false; } };
  const move = el("button", { type: "button", class: "primary", textContent: "Move", on: { click: () => attempt(async () => {
    await call("assign_speaker", state.mid, item.id, sel.value);
    restore();
    state.signature = "";
    say("Speaker changed. Undo speaker change reverses it.");
    renderMeeting();
  }) } });
  const cancel = el("button", { type: "button", class: "cancel", textContent: "Cancel", on: { click: restore } });
  box.append(move, cancel);
  box.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); restore(); button.focus(); }
    if (e.key === "Enter" && e.target === sel) { e.preventDefault(); move.click(); }
  });
  // A label shown on hover makes way for the box; a speaker's first label stays above it.
  if (button.classList.contains("again")) { button.hidden = true; body.prepend(box); }
  else button.after(box);
  sel.focus();
}

async function undoSpeakers() {
  const kind = await attempt(() => call("undo_speakers", state.mid));
  say(kind ? "Speaker change undone" : "No speaker change to undo");
  afterNamesChange(true);
}

const namesState = { offsets: {}, generation: 0 };

async function openNames() {
  if (!canName()) { say("Speakers can be named once they are identified, and before the review is confirmed."); return; }
  namesState.offsets = {};
  await renderNames();
  $("names").showModal();
}

// One call returns every speaker with examples. A generation counter drops
// a render that finishes after a newer one started.
async function renderNames() {
  const generation = ++namesState.generation;
  const data = await attempt(() => call("speaker_panel", state.mid, namesState.offsets));
  if (!data || generation !== namesState.generation) return;
  $("n-undo").disabled = !data.can_undo;
  $("n-list").replaceChildren(...data.speakers.map((sp, i) => speakerBlock(sp, data.speakers, i)));
}

function speakerBlock(sp, all, index) {
  const name = el("input", { value: sp.name, maxLength: 80,
                           "aria-label": `Name for ${sp.name}, ${plural(sp.passages, "passage")}` });
  // Renaming does not rebuild the panel, so focus and typing elsewhere are kept.
  name.addEventListener("change", () => attempt(async () => {
    await call("rename_speaker", state.mid, sp.label, name.value);
    say("Name saved");
    afterNamesChange(false);
  }));
  const ex = sp.examples;
  const examples = el("ul", { class: "examples" }, ...ex.items.map((it) => {
    const b = el("button", { type: "button", class: "ex-play", title: "Play exactly this passage",
                             "aria-label": `Play ${fmtTime(it.start)}`,
                             on: { click: () => play(it.start, it.end, true) } }, playIcon(10), fmtTime(it.start));
    return el("li", {}, b, el("span", { textContent: it.text }));
  }));
  const page = (offset) => () => { namesState.offsets[sp.label] = offset; renderNames(); };
  const more = ex.total > sp.offset + 3
    ? el("button", { type: "button", class: "n-more", textContent: "More examples", on: { click: page(sp.offset + 3) } })
    : (sp.offset ? el("button", { type: "button", class: "n-more", textContent: "First examples", on: { click: page(0) } }) : null);
  const others = all.filter((o) => o.label !== sp.label);
  const into = el("select", { "aria-label": `${sp.name}: same person as` },
    ...others.map((o) => el("option", { value: o.label, textContent: o.name })));
  const merge = others.length ? el("label", { class: "n-merge" }, "Same person as", into,
    el("button", { type: "button", class: "n-merge-btn", "aria-label": `Merge ${sp.name} into the chosen speaker`,
                   title: "Merge into the chosen speaker", on: { click: () => attempt(async () => {
                     await call("merge_speakers", state.mid, sp.label, into.value);
                     afterNamesChange(true);
                   }) } }, mergeIcon())) : null;
  return el("section", { class: `speaker sp${(index % 3) + 1}` },
    el("div", { class: "n-head" },
      el("span", { class: "n-avatar", "aria-hidden": "true", textContent: initials(sp.name) }),
      el("label", { class: "n-label" }, plural(sp.passages, "passage"), name)),
    examples,
    more || merge ? el("div", { class: "n-foot" }, more || el("span", {}), merge) : null);
}

function afterNamesChange(rebuildPanel) {
  state.signature = "";
  if (rebuildPanel && $("names").open) renderNames();
  renderMeeting();
}

async function renderTranscript() {
  const list = $("transcript");
  const audio = state.detail && state.detail.audio_available;
  const scroll = document.scrollingElement.scrollTop;
  const paneScroll = document.querySelector(".transcript-wrap").scrollTop;   // wide windows
  const items = [];
  const mid = state.mid, version = state.version;
  let start = 0, total = 1;
  while (start < total) {
    const page = await call("transcript", mid, start, PAGE, version);
    total = page.total;
    items.push(...page.items);
    start += page.items.length;
    if (!page.items.length) break;
  }
  // The user may have started editing, or switched meeting or version, while
  // this was loading: never replace a passage being edited; render later.
  const stopped = () => state.editing || state.mid !== mid || state.version !== version;
  if (stopped()) {
    state.signature = "";
    return;
  }
  // A search open: its matches are found before the list is redrawn and shown
  // with it, so the new rows never appear with the old list's count and
  // highlights. Search opens every passage (#111), which on a long meeting
  // takes longer than drawing the rows.
  const query = $("m-search").value.trim();
  const seq = query ? newSearch() : 0;
  const hits = query ? (await attempt(() => call("search", mid, query, version)) || []) : [];
  if (stopped()) {
    state.signature = "";
    if (query && seq === searchSeq) showHits(query, hits, takeJump());   // on the rows shown
    return;
  }
  let prev;
  list.replaceChildren(...items.map((item, i) => {
    const turn = !!item.label && (i === 0 || item.label !== prev);
    prev = item.label;
    return transcriptRow(item, audio, turn);
  }));
  state.items = items;
  state.total = total;
  state.drawnFor = `${mid}|${version}`;
  state.playingRow = null;
  document.scrollingElement.scrollTop = scroll;
  document.querySelector(".transcript-wrap").scrollTop = paneScroll;
  // The latest search shows its matches; otherwise the matches shown so far
  // go on the new rows, and the newer search (typed meanwhile) shows its own.
  if (query && seq === searchSeq) showHits(query, hits, takeJump());
  else markHits();
  updatePlayer();
}

// While transcribing, append the passages that came after the last one drawn
// instead of drawing every row again (#127). Fetched from the last row drawn
// on, so it can be compared. Passages are only ever added to a version, never
// removed or moved, so if that row is still at its place, unchanged, every row
// drawn is still there in the same order and the rest are new. Returns
// APPENDED, or, having changed nothing, REDRAW when that cannot be shown
// (nothing drawn yet, an earlier version shown, a search open, as its matches
// would go stale, an earlier passage added by a chunk done again, the speakers
// or totals not lining up, an error), so the caller redraws the whole list; or
// ABANDONED when an edit, another meeting or version, or a newer draw started
// while this was loading, so anything drawn now would be stale or unwanted.
const APPENDED = "appended", REDRAW = "redraw", ABANDONED = "abandoned";
async function appendTranscript() {
  const mid = state.mid, base = state.items || [], n = base.length;
  if (!n || state.version !== null || state.drawnFor !== `${mid}|null` || $("m-search").value.trim())
    return REDRAW;
  const fetched = [];
  let start = n - 1, total = null;
  try {
    do {
      const page = await call("transcript", mid, start, PAGE, null);
      if (total !== null && page.total !== total) return REDRAW;  // it changed while paging
      total = page.total;
      fetched.push(...page.items);
      start += page.items.length;
      if (!page.items.length) break;
    } while (start < total);
  } catch {
    return REDRAW;                     // the full redraw reports the error
  }
  if (state.editing || state.mid !== mid || state.version !== null || state.items !== base)
    return ABANDONED;
  const last = base[n - 1], first = fetched[0];
  if (total < n || fetched.length !== total - (n - 1) || !first || first.id !== last.id ||
      first.label !== last.label || first.who !== last.who || first.overlap !== last.overlap ||
      $("m-search").value.trim())
    return REDRAW;
  const added = fetched.slice(1);
  if (added.length) {
    const audio = state.detail && state.detail.audio_available;
    let prev = last.label;
    $("transcript").append(...added.map((item) => {
      const turn = !!item.label && item.label !== prev;
      prev = item.label;
      return transcriptRow(item, audio, turn);
    }));
    state.items = base.concat(added);
  }
  state.total = total;
  updatePlayer();
  return APPENDED;
}

const EDIT_HINT = "Enter or Esc to finish · Shift+Enter for a new line · saves as you type";
function startEditing(span) {
  const d = state.detail;
  if (!d || d.reviewed_at) { say("The transcript review is confirmed, so it can no longer be edited."); return; }
  if (state.version !== null) { say("Earlier versions are read only. Switch to the current version to edit."); return; }
  if (state.editing === span) return;
  stopEditing();
  state.editing = span;
  span.contentEditable = "plaintext-only";
  span.after(el("span", { class: "edit-hint", textContent: EDIT_HINT }));
  span.focus();
  span.addEventListener("input", scheduleSave);
  span.addEventListener("blur", stopEditing, { once: true });
  span.addEventListener("keydown", editKeys);
}

function editKeys(e) {
  if (e.key === "Escape" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); e.target.blur(); }
}

function scheduleSave() {
  clearTimeout(state.saveTimer);
  const span = state.editing;
  state.saveTimer = setTimeout(() => save(span), SAVE_DELAY_MS);
}

// Saves run one after another, in order, so a later keystroke can never be
// overwritten by an earlier save finishing last; undo waits for them too.
function save(span) {
  if (!span) return state.saving;
  const mid = state.mid, id = Number(span.dataset.id), text = span.textContent;
  state.saving = state.saving.then(async () => {
    const r = await attempt(() => call("edit", mid, id, text));
    if (r) {
      span.classList.toggle("edited", r.edited);
      say("Saved");
      $("m-undo").disabled = false;
    }
  });
  return state.saving;
}

function stopEditing() {
  const span = state.editing;
  if (!span) return;
  state.editing = null;
  clearTimeout(state.saveTimer);
  span.removeEventListener("input", scheduleSave);
  span.removeEventListener("keydown", editKeys);
  span.contentEditable = "false";
  const hint = span.nextElementSibling;
  if (hint && hint.classList.contains("edit-hint")) hint.remove();
  save(span);
}

async function undo() {
  if (state.version !== null) return;
  stopEditing();
  await state.saving;                      // the edit being closed is saved first
  const r = await attempt(() => call("undo", state.mid));
  if (r) {
    const span = document.querySelector(`#transcript .text[data-id="${r.id}"]`);
    if (span) { span.textContent = r.text; span.classList.toggle("edited", r.edited); span.scrollIntoView({ block: "center" }); }
    say("Edit undone");
  } else if (r === null) say("Nothing to undo");
  renderMeeting();
}

// -- search --------------------------------------------------------------------
// Every search request takes the next number, and only the latest request's
// answer is shown, so an answer that comes late never replaces a newer one.
// Typing and opening another meeting or version take a number too, dropping
// the answers to every request made before. A search the user typed jumps to
// its first match when its matches are shown, whichever request (typed, or a
// redraw's for the same text) answers last.
let searchSeq = 0, searchJump = false;
function newSearch(jump = false) { if (jump) searchJump = true; return ++searchSeq; }
function dropSearches() { searchJump = false; return ++searchSeq; }
function takeJump() { const jump = searchJump; searchJump = false; return jump; }

async function runSearch(jump = true) {
  const q = $("m-search").value.trim(), mid = state.mid, version = state.version;
  if (!q) { dropSearches(); showHits("", [], false); return; }
  const seq = newSearch(jump);
  const hits = await attempt(() => call("search", mid, q, version)) || [];
  if (seq !== searchSeq) return;        // a newer search, typing or another meeting since
  showHits(q, hits, takeJump());
}

// A search's matches shown on the rows drawn: highlighted and counted. Without
// a jump, the current match stays current if it still matches.
function showHits(q, hits, jump) {
  const current = state.hit >= 0 ? state.hits[state.hit] : undefined;
  for (const li of document.querySelectorAll("#transcript li.hit")) li.classList.remove("hit", "current");
  $("m-search").closest(".search").classList.toggle("active", !!q);
  if (!q) { state.hits = []; state.hit = -1; $("m-hits").textContent = ""; $("m-hit-nav").hidden = true; return; }
  state.hits = hits;
  state.hit = jump ? -1 : hits.indexOf(current);
  $("m-hits").textContent = hits.length ? `${hits.length} found` : "No matches";
  $("m-hit-nav").hidden = hits.length === 0;
  markHits();
  if (jump) nextHit(1);
  else if (state.hit >= 0) $("m-hits").textContent = `${state.hit + 1} of ${hits.length}`;
}

// The matches shown so far marked on the rows drawn (after a redraw).
function markHits() {
  if (!$("m-search").value.trim()) return;
  for (const id of state.hits) { const row = rowFor(id); if (row) row.classList.add("hit"); }
  const row = state.hit >= 0 ? rowFor(state.hits[state.hit]) : null;
  if (row) row.classList.add("current");
}

function rowFor(id) { return document.querySelector(`#transcript li[data-id="${Number(id)}"]`); }

function nextHit(dir) {
  if (!state.hits.length) return;
  const prev = state.hit >= 0 ? rowFor(state.hits[state.hit]) : null;
  if (prev) prev.classList.remove("current");
  state.hit = (state.hit + dir + state.hits.length) % state.hits.length;
  const row = rowFor(state.hits[state.hit]);
  if (row) { row.classList.add("current"); row.scrollIntoView({ block: "center" }); }
  $("m-hits").textContent = `${state.hit + 1} of ${state.hits.length}`;
}

// -- playback ------------------------------------------------------------------
// The bridge serves at most 30 s of audio at a time, so the player plays
// clips back to back, fetching the next one while the current one plays.
// Speaker examples play exactly their passage and stop.
const SPEEDS = [1, 1.25, 1.5, 2, 0.75];
const clips = { start: 0, end: 0, exact: false, generation: 0, next: null };

function meetingLength() {
  const d = state.detail;
  const last = state.items && state.items.length ? state.items[state.items.length - 1].end : 0;
  return Math.max((d && d.duration_s) || 0, last || 0);
}

function fetchClip(start, end) {
  return call("clip", state.mid, start, end).catch((err) => { say(String(err.message || err)); return null; });
}

async function playFrom(t, { exact = false, end = null } = {}) {
  const audio = $("player");
  const length = meetingLength();
  const start = Math.max(0, Math.min(t, Math.max(0, length - 0.05)));
  const stop = exact ? Math.max(end, start + 0.05) : Math.min(start + 30, length);
  if (stop - start < 0.05) return;
  const generation = ++clips.generation;
  clips.pending = start;             // where playback is going, until the clip arrives
  audio.pause();
  const pre = clips.next && clips.next.start === start && clips.next.end === stop ? clips.next.url : null;
  clips.next = null;
  const url = await (pre || fetchClip(start, stop));
  if (generation !== clips.generation) return;
  clips.pending = null;
  if (!url) return;
  Object.assign(clips, { start, end: stop, exact });
  audio.src = url;
  audio.playbackRate = exact ? 1 : state.speed || 1;
  try { await audio.play(); }
  catch (err) { if (err && err.name !== "AbortError") say(String(err.message || err)); }
  if (generation !== clips.generation) return;
  if (!exact && stop < length - 0.05) {
    const nextEnd = Math.min(stop + 30, length);
    clips.next = { start: stop, end: nextEnd, url: fetchClip(stop, nextEnd) };
  }
}

// play(start, end, exact): exact plays just [start, end]; otherwise it plays on.
function play(start, end, exact = false) { return playFrom(start, { exact, end }); }

function position() {
  const audio = $("player");
  if (clips.pending != null) return clips.pending;
  return audio.src ? clips.start + (audio.currentTime || 0) : 0;
}

function stopAudio() {
  clips.generation++;
  clips.next = null;
  clips.pending = null;
  $("player").pause();
}

function itemAt(t) {
  const items = state.items || [];
  let found = null;
  for (const it of items) { if (it.t <= t + 0.01) found = it; else break; }
  return found;
}

function updatePlayer() {
  const audio = $("player");
  const length = meetingLength();
  const pos = Math.min(position(), length);
  const playing = !audio.paused && !audio.ended;
  $("p-icon-play").style.display = playing ? "none" : "";
  $("p-icon-pause").style.display = playing ? "" : "none";
  $("p-play").setAttribute("aria-label", playing ? "Pause (Space)" : "Play (Space)");
  $("p-time").textContent = `${fmtTime(pos)} / ${fmtTime(length)}`;
  $("p-fill").style.width = length ? `${(100 * pos) / length}%` : "0";
  $("p-seek").setAttribute("aria-valuemax", String(Math.round(length)));
  $("p-seek").setAttribute("aria-valuenow", String(Math.round(pos)));
  $("p-seek").setAttribute("aria-valuetext", `${fmtTime(pos)} of ${fmtTime(length)}`);
  const it = audio.src ? itemAt(pos) : null;
  $("p-who").textContent = it ? joinParts(it.who, fmtTime(it.t)) : "";
  const row = it ? rowFor(it.id) : null;
  if (row !== state.playingRow) {
    if (state.playingRow) state.playingRow.classList.remove("playing");
    if (row) row.classList.add("playing");
    state.playingRow = row;
  }
}

function togglePlay() {
  const audio = $("player");
  if (!audio.paused) { audio.pause(); return; }
  if (audio.src && !audio.ended && !clips.exact) attempt(() => audio.play());
  else playFrom(position());
}

function nextPassage(dir) {
  const items = state.items || [];
  if (!items.length) return;
  const cur = itemAt(position());
  const i = cur ? items.indexOf(cur) : -1;
  const target = items[Math.max(0, Math.min(items.length - 1, i + dir))];
  if (target) playFrom(target.t);
}

function wirePlayer() {
  const audio = $("player");
  for (const ev of ["play", "pause", "timeupdate", "emptied"]) audio.addEventListener(ev, updatePlayer);
  audio.addEventListener("ended", () => {
    if (!clips.exact && clips.end < meetingLength() - 0.05) playFrom(clips.end);
    else updatePlayer();
  });
  $("p-play").addEventListener("click", togglePlay);
  $("p-back").addEventListener("click", () => playFrom(position() - 5));
  $("p-speed").addEventListener("click", () => {
    const i = SPEEDS.indexOf(state.speed || 1);
    state.speed = SPEEDS[(i + 1) % SPEEDS.length];
    $("p-speed").textContent = `${state.speed}×`;
    $("p-speed").setAttribute("aria-label", `Playback speed ${state.speed}×`);
    if (!clips.exact) audio.playbackRate = state.speed;
  });
  const seek = $("p-seek");
  seek.addEventListener("click", (e) => {
    const r = seek.getBoundingClientRect();
    playFrom(((e.clientX - r.left) / r.width) * meetingLength());
  });
  seek.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -5, ArrowRight: 5, PageDown: -30, PageUp: 30 }[e.key];
    if (step) { e.preventDefault(); playFrom(position() + step); }
    if (e.key === "Home") { e.preventDefault(); playFrom(0); }
    if (e.key === "End") { e.preventDefault(); playFrom(Math.max(0, meetingLength() - 5)); }
  });
}

// -- confirmations ---------------------------------------------------------------
// danger: the confirm button is red, for deleting (discard, delete, remove).
function confirmDialog({ title, body, check, onConfirm, limits = true, limitsText = null, danger = false }) {
  const dlg = $("confirm");
  $("c-title").textContent = title;
  $("c-body").textContent = body;
  $("c-limits").textContent = limitsText || (limits && state.detail ? state.detail.limits : "");
  $("c-check-text").textContent = check;
  $("c-check").checked = false;
  $("c-ok").disabled = true;
  $("c-ok").className = danger ? "danger" : "primary";
  $("c-icon").classList.toggle("danger", danger);
  $("c-icon-ok").toggleAttribute("hidden", danger);
  $("c-icon-danger").toggleAttribute("hidden", !danger);
  dlg.returnValue = "";
  dlg.onclose = () => { if (dlg.returnValue === "ok") onConfirm(); };
  dlg.showModal();
}

// Transcribe again with the model chosen in Settings > Speech model, from the
// Versions tab or a failed meeting's row (#244). A chosen model that is not
// ready is not started: the dialog says so and offers Manage downloads.
// meetingModel: the model the meeting's current version uses; failed: the
// meeting stopped with an error (only then does "still not ready" apply).
function transcribeAgain(mid, meetingModel, failed) {
  attempt(async () => {
    const s = await call("settings");
    const chosen = s.choices.find((c) => c.name === s.model);
    if (chosen && chosen.note) { modelNotReady(chosen, failed && chosen.name === meetingModel); return; }
    const named = chosen ? `${chosen.label}, the model chosen in Settings > Speech model`
      : "the model chosen in Settings > Speech model";
    confirmDialog({
      title: "Transcribe again with the current model?",
      body: `It uses ${named}. To use another model, change it there first. ` +
        "A new transcript is made from the audio. Your edits stay with the current version, " +
        "which is kept as an earlier version you can still open (read only); they are not copied " +
        "into the new transcript.",
      check: "I understand my edits stay with the earlier version.",
      limits: false,
      onConfirm: () => attempt(async () => {
        await call("retranscribe", mid);
        if (state.view === "meeting" && state.mid === mid) viewVersion(null);
        say("Transcribing again. The previous version and its edits are kept.");
        pollSoon();
      }),
    });
  });
}

// The chosen speech model is not downloaded or is damaged: say so, with the
// way to fix it (Manage downloads, unless policy turns downloads off) or to choose another.
function modelNotReady(chosen, same) {
  const dlg = $("model-dlg");
  $("md-title").textContent = same ? `${chosen.label} is still not ready` : `${chosen.label} is not ready`;
  $("md-body").textContent = (same ? "This meeting stopped because its speech model is not ready, and it is " +
    "still the model chosen in Settings > Speech model. " : "") + chosen.note;
  $("md-downloads").hidden = !chosen.offered;
  dlg.returnValue = "";
  dlg.onclose = () => {
    if (dlg.returnValue === "downloads") show("downloads");
    else if (dlg.returnValue === "settings") {
      show("settings");
      $("h-model").scrollIntoView({ block: "center" });   // the picker fills once Settings has loaded
    }
  };
  dlg.showModal();
}

function confirmReviewed(d) {
  confirmDialog({
    title: "Confirm the transcript is reviewed",
    body: "The reviewed text is saved in the workspace first. Then the app's copy of the audio is removed" +
      (d.holds.length ? " (once no hold covers it)" : "") +
      ". After this, playback and transcribing again are not possible and the text can no longer be edited.",
    check: "I have reviewed this transcript.",
    onConfirm: () => attempt(() => call("confirm_reviewed", d.id).then(refresh)),
  });
}

function confirmFinal(d) {
  confirmDialog({
    title: "Confirm the final document is saved",
    body: "All of this meeting's transcript working content will be removed from the workspace: text, edits, speakers, handoff options and versions. Copying or exporting alone never does this. " +
      (state.managed && state.managed.clipboard_clear_minutes
        ? "The app does not delete Markdown files you saved. Your organisation has the app clear its own copies from the clipboard after a set time, but copy something else now to be sure (check Win+V if you copied the text another way), and delete saved handoff files when you no longer need them."
        : "The app does not clear your clipboard or delete Markdown files you saved: copy something else to replace the clipboard (the app's copies are kept out of Windows clipboard history, but check Win+V if you copied the text another way), and delete saved handoff files when you no longer need them."),
    check: "The final document is reviewed and saved outside this app.",
    onConfirm: () => attempt(() => call("confirm_final_saved", d.id).then(() => {
      $("ho-preview").textContent = "";
      $("ho-passages").replaceChildren();
      handoffState.preview = null;
      show("meetings");
    })),
  });
}

// -- handoff (#22) ------------------------------------------------------------------
// A page in three steps: template, details to remove, copy or save. The
// preview is exactly what Copy or Save produces. Nothing is sent anywhere by
// the app: the user pastes or attaches it in their approved assistant.
// "Replace names" applies to this handoff only and is not stored.
// Copy and open (#109) copies, then the app opens the assistant's address
// (policy, or ChatGPT): the page only shows the name and host it is given.
const handoffState = { template: "minutes", templates: [], preview: null, generation: 0,
                       dirty: false, anonymise: false, total: 0 };

async function openHandoff() {
  // Nothing from the previous meeting stays on the page while this one loads.
  if (handoffState.mid !== state.mid) handoffState.anonymise = false;   // this hand off only
  handoffState.mid = state.mid;
  handoffState.preview = null;
  handoffState.dirty = false;
  handoffState.generation++;
  for (const id of ["ho-preview", "ho-info", "ho-redactions"]) {
    if ($(id).tagName === "TEXTAREA") $(id).value = ""; else $(id).textContent = "";
  }
  $("ho-passages").replaceChildren();
  $("ho-left-out").textContent = "";
  $("ho-long").hidden = true;
  $("ho-apply").disabled = true;                  // until this meeting's options have loaded
  layoutHandoff(null);                            // no assistant until this visit's preview says
  handoffState.loads = (handoffState.loads || 0) + 1;
  setHandoffButtons();
  handoffState.templates = await attempt(() => call("handoff_templates")) || [];
  if (!handoffState.templates.some((t) => t.id === handoffState.template))
    handoffState.template = "minutes";
  renderTemplateSelect();
  closeTemplateEditor();
  $("ho-meeting").textContent = state.detail ? state.detail.title : "Meeting";
  $("ho-status").textContent = "";
  $("ho-anon").checked = handoffState.anonymise;
  show("handoff");
  $("ho-close").focus();
  await renderHandoffOptions();
  await renderHandoffPreview();
}

function closeHandoff() {
  closeTemplateEditor();
  show("meeting");
  $("m-primary").focus();
}

function renderTemplateSelect() {
  const sel = $("ho-template");
  sel.replaceChildren(...handoffState.templates.map((t) =>
    el("option", { value: t.id, textContent: `${t.title} (v${t.version}${t.edited ? ", edited" : ""})` })));
  sel.value = handoffState.template;
}

function setHandoffButtons() {
  const ready = !!handoffState.preview && !handoffState.dirty;
  for (const id of ["ho-open", "ho-copy", "ho-copy-prompt", "ho-save"]) $(id).disabled = !ready;
  $("ho-dirty").hidden = !handoffState.dirty;
}

async function renderHandoffPreview() {
  const generation = ++handoffState.generation;
  handoffState.preview = null;
  setHandoffButtons();
  let p;
  try { p = await call("handoff_preview", state.mid, handoffState.template, handoffState.anonymise); }
  catch (err) {
    if (generation !== handoffState.generation) return;
    $("ho-preview").textContent = "";
    say(String(err.message || err));
    return;
  }
  if (generation !== handoffState.generation) return;
  handoffState.preview = p;
  layoutHandoff(p.assistant);
  setHandoffButtons();
  $("ho-preview").textContent = p.combined;
  const total = handoffState.total;
  const d = state.detail;
  const parts = [d ? lengthWord(d.duration_s) : null,
                 total ? `${total - p.omitted} of ${plural(total, "passage")}` : null,
                 p.anonymised ? "names replaced" : null,
                 p.redactions ? plural(p.redactions, "redaction") : null];
  $("ho-info").textContent = joinParts(...parts);
  $("ho-long").hidden = !p.long;
}

async function renderHandoffOptions() {
  // A load for a meeting the user has since left must not fill this page.
  const load = handoffState.loads, mid = state.mid;
  const stale = () => load !== handoffState.loads || mid !== state.mid;
  const p = await attempt(() => call("handoff_preview", mid, handoffState.template, handoffState.anonymise));
  if (!p || stale()) return;
  handoffState.dirty = false;
  $("ho-anon").checked = handoffState.anonymise;    // a tick made while loading is not applied
  setHandoffButtons();
  $("ho-redactions").value = p.options.redactions.join("\n");
  const omitted = new Set(p.options.omitted);
  const rows = [];
  let start = 0, total = 0;
  do {
    const page = await attempt(() => call("transcript", mid, start, 2000));
    if (stale()) return;
    if (!page || !page.items.length) break;
    total = page.total;
    for (const it of page.items)
      rows.push(el("li", {}, el("label", { title: `${it.who ? it.who + ": " : ""}${it.text}` },
        el("input", { type: "checkbox", value: it.id, checked: omitted.has(it.id),
                      "aria-label": `Leave out ${fmtTime(it.t)} ${it.text}${it.who ? ` (${it.who})` : ""}` }),
        el("span", { class: "t-mark", textContent: fmtTime(it.t) }),
        el("span", { class: "p-text", textContent: it.text }))));
    start += page.items.length;
  } while (start < total);
  handoffState.total = total;
  $("ho-passages").replaceChildren(...rows);
  $("ho-apply").disabled = false;
  countLeftOut();
}

function countLeftOut() {
  const n = document.querySelectorAll("#ho-passages input:checked").length;
  $("ho-left-out").textContent = n ? `· ${n} left out` : "";
}

async function applyHandoffOptions() {
  const omitted = [...document.querySelectorAll("#ho-passages input:checked")].map((i) => Number(i.value));
  const redactions = $("ho-redactions").value.split("\n").map((s) => s.trim()).filter(Boolean);
  await attempt(async () => {
    await call("set_handoff_options", state.mid, omitted, redactions);
    handoffState.anonymise = $("ho-anon").checked;
    handoffState.dirty = false;
    say("Saved. The transcript itself is unchanged.");
    await renderHandoffPreview();
  });
}

// With an assistant (the default), Copy and open is the main button and Copy
// prompt and transcript joins the other two in a row beneath it. Turned off
// by policy, and until the first preview says, the page is laid out as before
// #109: Copy prompt and transcript is the main button (index.html starts so).
function layoutHandoff(a) {
  const copy = $("ho-copy"), row = $("ho-row");
  $("ho-open").hidden = $("ho-open-note").hidden = !a;
  if (a) {
    $("ho-open-name").textContent = a.name;
    $("ho-open-host").textContent = a.host;
    if (copy.parentNode !== row) row.prepend(copy);
  } else if (copy.parentNode === row) {
    row.before(copy);
  }
  copy.classList.toggle("primary", !a);
  copy.classList.toggle("wide-btn", !a);
  row.classList.toggle("ho-three", !!a);
}

function clearsSentence(r) {
  return r.clears_in_minutes
    ? ` Your organisation clears it from the clipboard after ${r.clears_in_minutes} minute${r.clears_in_minutes === 1 ? "" : "s"} or when you close the app.`
    : "";
}

function handoffSaid(message) {
  $("ho-status").textContent = message;   // the last result, kept on the page
  say(message);                           // announced once, by the toast
}

async function copyHandoff(kind) {
  let r;
  const p = handoffState.preview;
  if (!p || handoffState.dirty) return;
  try { r = await call("copy_handoff", state.mid, handoffState.template, kind, p.digests[kind], handoffState.anonymise); }
  catch (err) { handoffSaid(String(err.message || err)); return; }
  const clears = clearsSentence(r);
  handoffSaid((kind === "combined"
    ? `Copied the prompt and transcript (${r.chars.toLocaleString("en-NZ")} characters). Paste it into your organisation's approved AI assistant.` +
      (r.long ? " It is long: if the assistant cuts it off, use Save Markdown and Copy prompt only instead." : "")
    : "Copied the prompt only. Attach the saved Markdown file in your approved AI assistant, then paste the prompt.") + clears);
}

// The app copies first and opens the browser only if the copy worked. It
// cannot tell whether a browser appeared, so the message says what to do if not.
async function copyAndOpen() {
  let r;
  const p = handoffState.preview;
  if (!p || handoffState.dirty) return;
  try { r = await call("copy_and_open_assistant", state.mid, handoffState.template, p.digests.combined, handoffState.anonymise); }
  catch (err) { handoffSaid(String(err.message || err)); return; }
  // The app read the policy again at the click: show the name and host it used.
  if (handoffState.preview === p) {
    p.assistant = { name: r.name, host: r.host };
    layoutHandoff(p.assistant);
  }
  const chars = r.chars.toLocaleString("en-NZ");
  const long = r.long ? ` It is long: if ${r.name} cuts it off, use Save Markdown and Copy prompt only instead.` : "";
  handoffSaid((r.opened
    ? `Copied the prompt and transcript (${chars} characters) and opened ${r.name} in your browser. Sign in with your organisation's business account, paste with Ctrl+V, check it, then send. If no browser appears, open ${r.host} yourself.`
    : `Copied the prompt and transcript (${chars} characters), but Unminute could not open your browser. Open ${r.host} yourself, sign in with your organisation's business account and paste.`)
    + long + clearsSentence(r));
}

async function saveHandoff() {
  let r;
  const p = handoffState.preview;
  if (!p || handoffState.dirty) return;
  try { r = await call("save_handoff", state.mid, handoffState.template, p.digests.markdown, handoffState.anonymise); }
  catch (err) { handoffSaid(String(err.message || err)); return; }
  if (r) handoffSaid(`Saved ${r.name}. The app keeps no record of this file; file or delete it yourself when you no longer need it.`);
}

// Editing a template replaces the preview with the editor.
function editTemplate(isNew) {
  const t = handoffState.templates.find((x) => x.id === handoffState.template);
  if (!t && !isNew) return;
  $("ho-editor").hidden = false;
  $("ho-preview-pane").hidden = true;
  $("ho-editor").dataset.id = isNew ? "" : t.id;
  $("ho-editor-title").textContent = isNew ? "New template" : "Edit template";
  $("ho-t-title").value = isNew ? "" : t.title;
  $("ho-t-prompt").value = isNew ? (t ? t.prompt : "") : t.prompt;
  $("ho-t-reset").textContent = !isNew && t.builtin ? "Restore original wording" : "Delete template";
  $("ho-t-reset").hidden = isNew || (t.builtin && !t.edited);
  $("ho-t-title").focus();
}

function closeTemplateEditor() {
  $("ho-editor").hidden = true;
  $("ho-preview-pane").hidden = false;
}

async function saveTemplate() {
  const id = $("ho-editor").dataset.id || null;
  const saved = await attempt(() => call("save_template", id, $("ho-t-title").value, $("ho-t-prompt").value));
  if (!saved) return;
  handoffState.template = saved;
  handoffState.templates = await attempt(() => call("handoff_templates")) || handoffState.templates;
  renderTemplateSelect();
  closeTemplateEditor();
  say("Template saved as a new version");
  renderHandoffPreview();
}

async function resetTemplate() {
  await attempt(() => call("reset_template", handoffState.template));
  handoffState.templates = await attempt(() => call("handoff_templates")) || handoffState.templates;
  if (!handoffState.templates.some((t) => t.id === handoffState.template)) handoffState.template = "minutes";
  renderTemplateSelect();
  closeTemplateEditor();
  renderHandoffPreview();
}

function wireHandoff() {
  $("ho-template").addEventListener("change", () => {
    handoffState.template = $("ho-template").value;
    closeTemplateEditor();
    renderHandoffPreview();
  });
  $("ho-edit").addEventListener("click", () => editTemplate(false));
  $("ho-new").addEventListener("click", () => editTemplate(true));
  $("ho-t-save").addEventListener("click", saveTemplate);
  $("ho-t-cancel").addEventListener("click", closeTemplateEditor);
  $("ho-t-reset").addEventListener("click", resetTemplate);
  $("ho-apply").addEventListener("click", applyHandoffOptions);
  const markDirty = () => { handoffState.dirty = true; setHandoffButtons(); };
  $("ho-redactions").addEventListener("input", markDirty);
  $("ho-anon").addEventListener("change", markDirty);
  $("ho-passages").addEventListener("change", () => { countLeftOut(); markDirty(); });
  $("ho-open").addEventListener("click", copyAndOpen);
  $("ho-copy").addEventListener("click", () => copyHandoff("combined"));
  $("ho-copy-prompt").addEventListener("click", () => copyHandoff("prompt_only"));
  $("ho-save").addEventListener("click", saveHandoff);
  $("ho-close").addEventListener("click", closeHandoff);
  $("ho-final").addEventListener("click", () => { if (state.detail) confirmFinal(state.detail); });
}

// -- setup (#19) ----------------------------------------------------------------------
// First run in five steps. The token is typed into a password field and
// passed straight to Python; the page never receives it back (only whether
// one is remembered).
const SU_STEPS = [["Welcome", ""], ["Speech model", ""], ["Speaker names", "Optional"],
                  ["Microphone", "Optional"], ["Ready", ""]];
// returnTo: opened from Downloads for one step (the speaker token); leaving that step goes back.
const suState = { step: 1, skipSpeakers: false, mic: null, returnTo: null };

// A card's short name and a download's progress (a number, or
// null when it cannot be told) come from Python as they are: the page never
// reads them out of a title or a message (R12, #133).
const cardName = (c) => c.label;
const isRunning = (c) => !!(c && c.download && c.download.state === "running");
// Waiting behind the download (or check) running (#366): position 1 is next.
const isQueued = (c) => !!(c && c.download && c.download.state === "queued");
const RESUMED = "Carried on from the last time Unminute was open.";
function queueNote(dl) {
  const where = dl.position === 1 ? "Next in the queue." : `Number ${dl.position} in the queue.`;
  return dl.resumed ? `${where} ${RESUMED}` : where;
}
// Python says whether anything was cancelled: the entry may have finished or
// failed since the page was drawn, and then nothing was removed (#368 review).
function cancelQueued(c, then) {
  return button("Cancel", () => attempt(async () => {
    const cancelled = await call("cancel_download", c.name);
    say(cancelled ? "Removed from the queue." : "It is no longer in the queue.");
    then();
  }));
}

function progressBlock(c, note) {
  const dl = c.download;
  const bar = el("div", { class: "progress", role: "progressbar", "aria-label": "Download", "data-card": c.name },
    el("span", {}));
  setProgress(bar, dl.percent);
  return el("div", { class: "su-progress" }, bar,
    el("div", { class: "su-progress-text" }, el("span", { textContent: dl.message }), note ? el("span", { textContent: note }) : null));
}
// A bar's value, set again in place on every poll: the list around it is
// rebuilt only when something else changes, so focus and open Details stay.
function setProgress(bar, percent) {
  const pct = typeof percent === "number" ? percent : null;
  bar.classList.toggle("busy", pct === null);
  for (const [k, v] of [["aria-valuenow", pct], ["aria-valuemin", 0], ["aria-valuemax", 100]])
    if (pct === null) bar.removeAttribute(k); else bar.setAttribute(k, String(v));
  bar.firstChild.style.width = pct === null ? "" : `${pct}%`;
}
function updateProgress(box, cards) {
  for (const c of cards) {
    const bar = c.download && box.querySelector(`.progress[data-card="${CSS.escape(c.name)}"]`);
    if (bar) setProgress(bar, c.download.percent);
  }
}
// The cards as the lists are drawn, without the progress (it updates in place).
const cardsSig = (cards) => JSON.stringify(cards.map((c) =>
  (c.download ? { ...c, download: { ...c.download, percent: null } } : c)));

function setStep(n) {
  $("s-token").value = "";                           // a typed token is never kept in the page
  if (suState.returnTo && n !== 3) {
    const back = suState.returnTo;
    suState.returnTo = null;
    show(back);
    return;
  }
  suState.step = n;
  for (let i = 1; i <= 5; i++) $(`su-${i}`).hidden = i !== n;
  renderSuSteps();
  window.scrollTo(0, 0);
  if (n === 4) { micLists["su-device"].stale = true; renderSuMic(); }   // the step shown again (#369)
  if (state.setup) renderSummary(state.setup);
  // Focus moves into the new step (its first control) and the step is announced.
  const first = [...$(`su-${n}`).querySelectorAll("button, input, select, summary")]
    .find((c) => !c.disabled && c.offsetParent !== null);
  if (first) first.focus();
  say(`Step ${n} of 5: ${$(`su-${n}-title`).textContent}`);
}

function renderSuSteps() {
  $("su-steps").replaceChildren(...SU_STEPS.map(([name, note], i) => {
    const n = i + 1, kind = n < suState.step ? "done" : n === suState.step ? "now" : "";
    const li = el("li", { class: kind },
      el("span", { class: kind === "done" ? "su-dot done-tick" : "su-dot", "aria-hidden": "true",
                    textContent: kind === "done" ? "" : String(n) }),
      el("span", { class: "su-name", textContent: name },
         kind === "done" ? el("span", { class: "sr", textContent: ", done" }) : null),
      note ? el("span", { class: "su-opt", textContent: note }) : null);
    if (kind === "now") li.setAttribute("aria-current", "step");
    return li;
  }));
}

function modelCard(c, s) {
  const dl = c.download;
  let action;
  if (isRunning(c)) action = button("Cancel", () => attempt(() => call("cancel_download", c.name).then(renderSetup)));
  else if (isQueued(c)) action = cancelQueued(c, renderSetup);
  else if (c.state === "ready")
    action = el("span", { class: "su-ready" }, el("span", { class: "tick" }, icon("tick", 13)), "Ready");
  else if (!allowed("AllowModelDownloads") || (c.role === "gpu" && !allowed("AllowGpuComponent")))
    action = itManaged(c);
  else {
    // Another download running: this one queues behind it (#366).
    action = button(c.state === "missing" ? `Download (${c.size_gb} GB)` : "Repair", () => startDownload(c.name), { primary: true });
  }
  return el("div", { class: "su-model" },
    el("div", { class: "su-model-top" },
      el("div", { class: "su-model-text" },
        el("span", { class: "su-model-name" }, cardName(c),
           c.recommended ? el("span", { class: "rec-badge", textContent: "Recommended" }) : null),
        el("span", { class: "su-model-about", textContent: `${c.about} ${c.size_gb} GB, downloaded once.` })),
      action),
    isRunning(c) ? progressBlock(c, "Each file is checked before use") : null,
    isQueued(c) ? el("span", { class: "dl-msg", textContent: queueNote(dl) }) : null,
    isRunning(c) && dl.resumed ? el("span", { class: "dl-msg", textContent: RESUMED }) : null,
    dl && dl.state === "failed" ? el("span", { class: "su-failed", textContent: dl.message }) : null);
}

// Rebuilds a list in place, keeping open Details and keyboard focus on the same
// control (by position), so a download's progress updates do not disturb either.
function replaceKeeping(box, ...children) {
  const controls = () => [...box.querySelectorAll("button, summary, a[href], input, select")];
  const at = controls().indexOf(document.activeElement);
  const open = [...box.querySelectorAll("details")].map((d) => d.open);
  box.replaceChildren(...children);
  box.querySelectorAll("details").forEach((d, i) => { if (open[i]) d.open = true; });
  if (at >= 0) {
    const target = controls()[at];
    if (target && !target.disabled) target.focus();
  }
}

// One row per model or component, for Other options and the Downloads page.
function downloadRow(c, s, inUse) {
  const dl = c.download;
  const running = isRunning(c), queued = isQueued(c);
  // Check every file and Remove wait for every download, queued ones too.
  const busy = s.cards.some((x) => isRunning(x) || isQueued(x));
  const tag = c.role === "speech"
    ? joinParts(c.recommended ? "Recommended" : null, inUse ? "in use" : null) : "Optional";
  const about = c.role === "speech" ? `Speech to text. ${c.about}` : c.about.split(/(?<=\.)\s/)[0];
  const [pillKind, pillText] = running ? ["", "Downloading"]
    : queued ? ["queued", "Queued"]
    : c.state === "ready" ? ["ready", "Ready"]
    : dl && dl.state === "failed" ? ["bad", "Needs attention"]
    : c.state === "invalid" ? ["bad", "Damaged"] : ["", "Not downloaded"];
  let action;
  if (running) action = button("Cancel", () => attempt(() => call("cancel_download", c.name).then(refresh)));
  else if (queued) action = cancelQueued(c, refresh);
  else if (c.state === "ready")
    action = button("Check every file", () => attempt(() => call("verify_model", c.name).then(refresh)),
                    { title: "Compare every file with its published checksum" });
  else if (c.needs_token && c.state === "missing")
    action = button("Set up…", () => {
      suState.step = 3;
      suState.returnTo = state.view === "downloads" ? "downloads" : null;
      show("setup");
      setStep(3);
    }, { primary: true });
  else if (!allowed("AllowModelDownloads") || (c.role === "gpu" && !allowed("AllowGpuComponent")))
    action = itManaged(c);
  else action = button(c.state === "missing" ? `Download (${c.size_gb} GB)` : "Repair", () => startDownload(c.name), { primary: true });
  // Download, Repair and Set up stay available: another download queues (#366).
  if (c.state === "ready" && !running && !queued && action.tagName === "BUTTON") action.disabled = busy;
  const remove = c.removable && !running && !queued ? linkButton("Remove…", () => confirmDialog({
    title: `Remove ${cardName(c)}?`,
    body: `This deletes it from this computer, including any damaged or unfinished copies. Meetings already transcribed keep their text. You can download it again from Downloads.`,
    check: "Remove it from this computer.", limits: false, danger: true,
    onConfirm: () => attempt(async () => {
      const gb = await call("remove_model", c.name);
      say(`Removed. About ${gb} GB freed.`);
      refresh();
    }) })) : null;
  if (remove) remove.disabled = busy;
  return el("li", { class: "dl-item" },
    el("div", { class: "dl-row" },
      el("div", { class: "dl-text" },
        el("span", { class: "dl-name" }, cardName(c), tag ? el("span", { class: "dl-tag", textContent: ` ${tag}` }) : null),
        el("span", { class: "dl-desc", textContent: `${about} ${c.size_gb} GB.` })),
      el("span", { class: `dl-pill ${pillKind}`.trim(), textContent: pillText }),
      action, remove),
    running ? progressBlock(c, null) : null,
    running && dl.resumed ? el("span", { class: "dl-msg", textContent: RESUMED }) : null,
    queued ? el("span", { class: "dl-msg", textContent: queueNote(dl) }) : null,
    !running && dl && dl.state === "failed" ? el("span", { class: "dl-msg bad", textContent: dl.message }) : null,
    el("details", {}, el("summary", { textContent: "Details" }),
      el("p", { textContent: c.status }),
      el("p", { textContent: `Licence ${c.licence} · ${c.source} · revision ${c.revision}` })));
}

async function renderSetup() {
  const s = await call("setup_state");
  state.setup = s;
  state.managed = s.managed;
  renderSuSteps();
  // Back on step 4 (from another view, or the window shown again): ask again (#369).
  if (suState.step === 4 && suState.mic && micLists["su-device"].stale)
    await attempt(() => relistMics("su-device", showSuMics));
  const speech = s.cards.filter((c) => c.role === "speech");
  const rec = speech.find((c) => c.recommended) || speech[0];
  const others = speech.filter((c) => c !== rec);
  const sig = cardsSig(s.cards);
  if (sig !== suState.sig) {                         // unchanged: keep open Details and focus
    suState.sig = sig;
    replaceKeeping($("su-model"), modelCard(rec, s));
    replaceKeeping($("setup-cards"), ...others.map((c) => downloadRow(c, s, false)));
  }
  updateProgress($("su-model"), s.cards);
  updateProgress($("setup-cards"), s.cards);
  $("su-other-title").textContent = `Other options: ${others.map(cardName).join(", ")}`;
  $("su-2-note").textContent = isRunning(rec) || isQueued(rec) ? "You can keep going while it downloads" : "";
  const speaker = s.cards.find((c) => c.role === "speakers") || {};
  $("speaker-setup").hidden = speaker.state === "ready";
  $("s-download-speakers").disabled = isRunning(speaker) || isQueued(speaker) || !allowed("AllowModelDownloads");
  $("s-token").disabled = !allowed("AllowModelDownloads");
  $("s-use-remembered").hidden = !s.token_remembered || speaker.state === "ready"
    || !allowed("AllowModelDownloads");
  $("s-use-remembered").disabled = isRunning(speaker) || isQueued(speaker);
  $("s-remove-token").hidden = !s.token_remembered;
  $("s-token-note").textContent = s.token_remembered
    ? "A Hugging Face token is remembered in Windows Credential Manager. Removing it does not affect models already downloaded."
    : "";
  $("su-speaker-status").textContent = speaker.state === "ready" ? "The speaker model is downloaded and ready."
    : isQueued(speaker) ? queueNote(speaker.download)
    : speaker.download ? speaker.download.message : "";
  renderSummary(s);
  $("s-finish").disabled = !s.speech_ready;
  $("s-finish-note").textContent = s.speech_ready ? "" : `Download a speech model first (${cardName(rec)} is recommended).`;
}

function renderSummary(s) {
  const rec = s.cards.find((c) => c.role === "speech" && c.recommended);
  const ready = s.cards.find((c) => c.role === "speech" && c.state === "ready");
  const speaker = s.cards.find((c) => c.role === "speakers") || {};
  const line = (label, value, on) => el("li", {},
    el("span", { class: `s-icon${on ? "" : " off"}` }, on ? icon("tick", 14) : el("span", { "aria-hidden": "true", textContent: "–" })),
    el("span", { class: "s-label", textContent: label }), el("span", { class: "s-value", textContent: value }));
  const speech = ready ? `${cardName(ready)}, ready` : isRunning(rec) ? "Downloading…"
    : isQueued(rec) ? "Queued" : "Not downloaded yet";
  const names = speaker.state === "ready" && !suState.skipSpeakers ? "Ready"
    : suState.skipSpeakers ? "Skipped. Turn on in Settings."
    : isRunning(speaker) ? "Downloading…" : isQueued(speaker) ? "Queued"
    : "Not set up. You can add it later in Settings.";
  const mic = suState.mic ? "On" : "Skipped. Turn on in Settings.";
  $("su-summary").replaceChildren(line("Speech model", speech, !!ready),
    line("Speaker names", names, names === "Ready"), line("Microphone", mic, !!suState.mic));
}

async function renderSuMic() {
  const s = await attempt(() => call("settings"));
  if (!s) return;
  suState.mic = !!s.microphone;
  $("su-mic").checked = suState.mic;
  $("su-mic-row").hidden = !suState.mic;
  if (!suState.mic) $("su-test-result").hidden = true;
  if (suState.mic && (micLists["su-device"].stale || !$("su-device").options.length))
    await attempt(() => relistMics("su-device", showSuMics));
}

function startDownload(name, extra = {}) {
  return attempt(async () => {
    const how = await call("download_model", name, extra.token || null, extra.remember !== false);
    say(how === "queued" ? "Queued. It starts when the download before it finishes." : "Download started");
    refresh();
  });
}

function wireSetup() {
  for (const b of document.querySelectorAll("[data-help]"))
    b.addEventListener("click", () => attempt(() => call("open_help", b.dataset.help)));
  for (const b of document.querySelectorAll("#view-setup [data-go]"))
    b.addEventListener("click", () => setStep(Number(b.dataset.go)));
  const speakerName = () => (state.setup.cards.find((c) => c.role === "speakers") || {}).name;
  $("s-download-speakers").addEventListener("click", () => {
    const token = $("s-token").value.trim();
    if (!token) { say("Paste your Hugging Face token first."); return; }
    $("s-token").value = "";                         // never kept in the page
    suState.skipSpeakers = false;
    startDownload(speakerName(), { token, remember: $("s-remember").checked });
  });
  $("s-use-remembered").addEventListener("click", () => startDownload(speakerName()));
  const removeToken = () => attempt(async () => {
    await call("remove_token");
    say("Token removed. Downloaded models keep working.");
    refresh();
  });
  $("s-remove-token").addEventListener("click", removeToken);
  $("dl-remove-token").addEventListener("click", removeToken);
  $("s-skip-speakers").addEventListener("click", () => { suState.skipSpeakers = true; setStep(4); });
  $("su-next-3").addEventListener("click", () => { suState.skipSpeakers = false; setStep(4); });
  $("su-mic").addEventListener("change", () => attempt(async () => {
    await call("set_settings", { microphone: $("su-mic").checked });
    micLists["su-device"].stale = true;
    renderSuMic();
  }));
  $("su-skip-4").addEventListener("click", () => {
    // First run only: skipping undoes a switch turned on here. Later visits leave Settings alone.
    if (suState.mic && !state.setup.onboarded)
      attempt(() => call("set_settings", { microphone: false }).then(() => { suState.mic = false; renderSummary(state.setup); }));
  });
  $("su-test").addEventListener("click", () => attempt(async () => {
    const box = $("su-test-result");
    box.hidden = false; box.classList.remove("quiet");
    box.textContent = "Listening for 3 seconds…";
    const opt = $("su-device").selectedOptions[0];
    const t = await call("test_microphone", Number($("su-device").value), opt ? opt.dataset.name : "")
      .catch(async (err) => {             // a changed device list: reload it (#369)
        if (/changed/.test(String(err && err.message)))
          await relistMics("su-device", showSuMics).catch(() => {});
        throw err;
      });
    box.classList.toggle("quiet", !t.heard);
    box.textContent = t.heard ? `Sound heard (loudest ${t.peak_db} dB).`
      : `Very quiet (loudest ${t.peak_db} dB). Speak, or check the microphone and its volume.`;
  }));
  relistOnFocus("su-device", showSuMics);
  $("s-finish").addEventListener("click", () => attempt(async () => {
    const speaker = state.setup.cards.find((c) => c.role === "speakers") || {};
    // First run: speaker names are on when their model is downloaded or downloading, unless
    // skipped. Later visits (a speech model went missing) keep the Settings choice.
    const speakers = state.setup.onboarded ? !!(await call("settings")).speakers
      : !suState.skipSpeakers && (speaker.state === "ready" || isRunning(speaker) || isQueued(speaker));
    await call("finish_setup", speakers);
    show("meetings");
  }));
}

// -- downloads -------------------------------------------------------------------------
async function renderDownloads() {
  const s = await call("setup_state");
  state.setup = s;
  state.managed = s.managed;
  const settings = await call("settings");
  const sig = JSON.stringify([cardsSig(s.cards), settings.model, s.managed]);
  if (sig !== state.downloadsSig) {                  // unchanged: keep open Details and focus
    state.downloadsSig = sig;
    replaceKeeping($("dl-list"), ...s.cards.map((c) => downloadRow(c, s, c.name === settings.model)));
  }
  updateProgress($("dl-list"), s.cards);
  $("dl-token").hidden = !s.token_remembered;
  $("dl-token-note").textContent = "A Hugging Face token is remembered in Windows Credential Manager. Removing it does not affect models already downloaded.";
}

// -- managed policy (#111 A5) ------------------------------------------------------
// What the organisation's policy allows (from settings and setup_state). Until
// the first answer arrives everything is treated as allowed; the app itself
// enforces the policy whatever the page shows.
function allowed(name) {
  const m = state.managed;
  return !m || !m.allow || m.allow[name] !== false;
}
const MANAGED_NOTE = "Turned off by your organisation.";
// A component the organisation switched off, or models IT installs instead of downloads.
function itManaged(c) {
  return el("span", { class: "set-text", textContent: c.role === "gpu" && !allowed("AllowGpuComponent")
    ? "Turned off by your organisation" : "Ask your IT team" });
}

// -- settings ------------------------------------------------------------------
const MODE_NOTE = {
  quiet: "Quiet leaves most of the computer free while it transcribes. Recommended.",
  faster: "Faster uses more of the computer while transcribing.",
};

// Settings on their default because the saved value could not be used (R11).
const SETTING_NAMES = {
  mode: "Processing speed", model: "Speech model", speakers: "Identify speakers",
  battery_override: "Process on battery power", onboarded: "Guided setup",
  microphone: "Microphone recording", gpu_speakers: "NVIDIA graphics card",
  recording_display: "Recording display", live_drafts: "Draft text while recording",
  live_drafts_audio: "What draft text follows",
};
function renderRepaired(r) {
  const names = ((r && r.keys) || []).map((k) => SETTING_NAMES[k] || k);
  const text = r && r.all
    ? "Your saved settings could not be read, so all of them are on their defaults. Check them below."
    : names.length
      ? `Some saved settings could not be used, so they are on their defaults: ${names.join(", ")}. Check them below.`
      : "";
  $("s-repaired").textContent = text;
  $("s-repaired").hidden = !text;
}

// Policy value names Unminute does not recognise (#300), such as a misspelling
// or a trailing space: they grant and withhold nothing, so Settings names them
// for IT to fix. The bridge sends them as printable ASCII with escapes for
// anything else (policy.shown_name); they are shown as text only.
function unrecognisedNote(m) {
  const names = (m && Array.isArray(m.unrecognised) ? m.unrecognised : [])
    .filter((n) => typeof n === "string" && n);
  if (!names.length) return "";
  return names.length === 1
    ? `Your organisation's policy has a value Unminute does not recognise: ${names[0]}. Ask your IT team to check its name.`
    : `Your organisation's policy has values Unminute does not recognise: ${names.join(", ")}. Ask your IT team to check their names.`;
}
function renderUnrecognised(m) {
  const text = unrecognisedNote(m);
  $("s-unrecognised").textContent = text;
  $("s-unrecognised").hidden = !text;
}

// Hand off's Copy and open (#109), when the organisation's policy sets it:
// the name and host in force, or why there is no button.
function renderAssistantSetting(a) {
  const text = !a ? ""
    : a.state === "set" ? `Hand off assistant: ${a.name}, ${a.host} (set by your organisation).`
    : a.state === "off" ? "Hand off assistant: turned off by your organisation."
    : a.state === "unreadable" ? "Hand off assistant: your organisation's settings could not be read, so Copy and open is not shown. Ask your IT team."
    : "Hand off assistant: set by your organisation but not valid, so Copy and open is not shown. Ask your IT team.";
  $("s-assistant").textContent = text;
  $("s-assistant").hidden = !text;
}

// A speech model in the Settings picker, marked as the Downloads page shows
// its state (#244): Not downloaded, Damaged.
const MODEL_STATE = { missing: " (not downloaded)", invalid: " (damaged)" };
function modelChoice(c) {
  return `${c.label}${c.recommended ? " (recommended)" : ""}${MODEL_STATE[c.state] || ""}`;
}

// Live drafts (#104): off by default, Parakeet v2 only, and IT can turn it
// off. With computer audio on, the user chooses what the draft follows.
function renderDraftSettings(s) {
  const permitted = allowed("AllowLiveDrafts");
  const model = s.live_drafts_model || {};
  const ready = model.state === "ready";
  $("s-drafts").checked = !!s.live_drafts;
  $("s-drafts").disabled = !permitted;               // always possible to turn off otherwise
  $("s-drafts-note").textContent = !permitted ? MANAGED_NOTE
    : !ready ? (model.offered
      ? `Needs ${model.label}: download it in Manage downloads (Speech model, above).`
      : `Needs ${model.label}. Ask your IT team.`)
    : "Rough text a few seconds after people speak. The full transcript replaces it after you stop. Uses more memory and processor time while recording.";
  // The choice matters only while computer audio is recorded as well (#108).
  $("s-drafts-audio-box").hidden = !s.computer_audio;
  for (const r of document.querySelectorAll('input[name="draftsaudio"]')) {
    r.checked = r.value === (s.live_drafts_audio || "microphone");
    r.disabled = !permitted || !s.live_drafts;
  }
  $("s-drafts-audio-note").textContent = s.live_drafts_audio === "mix"
    ? "Draft text also includes what others say on the call. The full transcript always uses both."
    : "Draft text follows your microphone only. The full transcript always uses both.";
}

async function renderSettings() {
  const s = await call("settings");
  state.managed = s.managed;
  $("s-managed").hidden = !(s.managed && s.managed.managed.length);
  renderUnrecognised(s.managed);
  renderAssistantSetting(s.managed && s.managed.assistant);
  renderRepaired(s.repaired);
  for (const r of document.querySelectorAll('input[name="mode"]')) r.checked = r.value === s.mode;
  for (const r of document.querySelectorAll('input[name="recdisplay"]')) r.checked = r.value === s.recording_display;
  $("s-mode-note").textContent = MODE_NOTE[s.mode] || "";
  $("s-battery").checked = s.battery_override;
  $("s-speakers").checked = s.speakers;
  // Graphics card: shown when an NVIDIA card is present. One switch for both
  // components: speaker names, and Whisper transcription (#248).
  $("s-gpu-box").hidden = !s.gpu.card;
  $("s-gpu").checked = !!s.gpu_speakers;
  const gpuUses = [s.gpu.component === "ready" ? "speaker names" : null,
    s.gpu.whisper_component === "ready" ? "Whisper transcription" : null]
    .filter(Boolean);
  // Always possible to turn off; turning on needs a component and the driver.
  $("s-gpu").disabled = !allowed("AllowGpuComponent")
    || (!s.gpu_speakers && (!gpuUses.length || !s.gpu.driver_ok));
  $("s-gpu-note").textContent = !s.gpu.card ? "" : !allowed("AllowGpuComponent") ? MANAGED_NOTE : !s.gpu.driver_ok
    ? `${s.gpu.card}: the NVIDIA driver is too old (${s.gpu.min_driver} or later is needed). Ask IT to update it.`
    : !gpuUses.length
      ? (s.gpu_speakers
        ? "The graphics card component needs repair in Downloads; until then the processor is used."
        : `${s.gpu.card} found. Download "Graphics card acceleration" or "Graphics card for Whisper" in Downloads to use it.`)
      : s.mode !== "faster" && s.gpu_speakers
        ? "Used only in Faster mode (Processing, above)."
        : `${s.gpu.card}. Speeds up ${gpuUses.join(" and ")} in Faster mode only.`;
  $("s-microphone").checked = !!s.microphone;
  $("s-microphone").disabled = !allowed("AllowMicrophoneRecording");
  renderDraftSettings(s);
  // The lid (#172): "Do nothing" while recording, put back afterwards.
  $("s-lid").checked = !!s.lid_override;
  $("s-lid").disabled = !s.lid_override && (!allowed("AllowLidOverride") || s.lid_managed);
  $("s-lid-note").textContent = !allowed("AllowLidOverride") ? MANAGED_NOTE
    : s.lid_managed ? "Your organisation sets what closing the lid does, so Unminute leaves it alone."
    : "While recording, Windows is set to do nothing when the lid is closed, and put back afterwards. Stop the recording before you put the laptop away.";
  $("s-add-folder").disabled = $("s-include-existing").disabled = !allowed("AllowWatchedFolders");
  const sel = $("s-model");
  if (document.activeElement !== sel) {
    sel.replaceChildren(...s.choices.map((c) => el("option", { value: c.name, textContent: modelChoice(c) })));
    sel.value = s.model;
  }
  // A chosen model that is not ready says so beside the picker, and Manage
  // downloads stands out when it can fix it (#244). The choice is still saved.
  const chosen = s.choices.find((c) => c.name === s.model);
  const note = (chosen && chosen.note) || "";
  $("s-model-hint").hidden = !note;
  $("s-model-hint").textContent = note;
  $("s-downloads").classList.toggle("primary", !!note && chosen.offered);
  const setup = await call("setup_state");
  const speechReady = setup.cards.filter((c) => c.role === "speech" && c.state === "ready").length;
  const speakerReady = setup.cards.some((c) => c.role === "speakers" && c.state === "ready");
  $("s-models-line").replaceChildren(
    el("span", { class: "tick" }, icon("tick", 11)),
    `${plural(speechReady, "speech model")}${speakerReady ? " and the speaker model" : ""} downloaded`);

  const wf = await call("watched_folders");
  $("s-folders").replaceChildren(...(wf.folders.length ? wf.folders.map(folderItem)
    : [el("li", { class: "empty-folder", textContent: "No folders are watched yet." })]));
  const dep = s.deployment || {};
  const version = dep.version ? `Unminute ${dep.version}` : "Unminute";
  $("s-version").textContent = version;
  $("s-about-version").textContent = version;
  $("s-deployment").textContent = dep.mode === "machine"
    ? "Installed for everyone on this computer by your IT team."
    : s.update_blockers.length && dep.mode !== "managed"
    ? "An update needs an empty workspace. Holding one up now:"
    : dep.mode === "managed"
    ? "Updates are managed by your IT team."
    : dep.mode === "self-service"
      ? "To update, close the app, then run install.cmd from the new package. An update that changes how the workspace is stored waits until the workspace is empty."
      : "Running from source (development).";
  $("s-update-blockers").replaceChildren(...(s.update_blockers.length
    ? s.update_blockers.map((b) => el("li", { textContent: b }))
    : [el("li", { class: "none", textContent: "Nothing would hold up an update." })]));
  const receipts = await call("receipts");
  $("receipts").replaceChildren(...(receipts.length ? receipts.map((r) =>
    el("li", {}, el("span", { textContent: r.result }), el("span", { class: "when", textContent: dateWord(r.completed_at) })))
    : [el("li", { class: "none", textContent: "None." })]));
}

function folderItem(f) {
  const status = f.error || joinParts(f.active ? "Watching" : "Paused",
    f.last_scan_at ? `checked ${whenWord(f.last_scan_at)}` : "not checked yet",
    f.imported ? `${f.imported} imported` : null,
    f.waiting ? `${f.waiting} waiting ${f.active ? "until fully copied" : "until you resume"}` : null,
    f.cloud ? `${f.cloud} in cloud storage, not on this computer yet` : null,
    f.duplicates ? `${f.duplicates} already imported` : null,
    f.unreadable ? `${f.unreadable} could not be read (no permission)` : null,
    f.failed ? `${f.failed} could not be imported (damaged or unsupported)` : null);
  const folderIcon = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({ width: 22, height: 22, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) folderIcon.setAttribute(k, v);
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z");
  folderIcon.append(path);
  return el("li", {},
    el("div", { class: "folder-top" }, folderIcon,
      el("div", { class: "folder-text" }, el("span", { class: "folder-path", textContent: f.path }),
        el("span", { class: `folder-status${f.error ? " bad" : ""}`, textContent: status })),
      button(f.active ? "Pause" : "Resume", () => attempt(() => call("set_folder_active", f.id, !f.active).then(renderSettings))),
      linkButton("Stop watching…", () => confirmDialog({
        title: "Stop watching this folder?",
        body: "Recordings already imported stay in the workspace. New recordings in this folder will no longer be imported, and its deletion permission is removed.",
        check: "Stop watching the folder.", limits: false,
        onConfirm: () => attempt(() => call("remove_folder", f.id).then(renderSettings)) }))),
    el("label", {}, el("input", { type: "checkbox", checked: f.fetch_cloud,
      on: { change: (e) => attempt(() => call("set_folder_authority", f.id, null, e.target.checked).then(renderSettings)) } }),
      "Download recordings that are only in cloud storage (OneDrive and similar)"),
    el("label", {}, el("input", { type: "checkbox", checked: f.delete_originals,
      disabled: !f.delete_originals && !allowed("AllowDeleteOriginals"),
      on: { change: (e) => {
        if (!e.target.checked) { attempt(() => call("set_folder_authority", f.id, false, null).then(renderSettings)); return; }
        e.target.checked = false;                     // until the app's own window confirms
        attempt(() => call("set_folder_authority", f.id, true, null).then((r) => {
          if (r && r.cancelled) say(declined(r, "Nothing changed: originals in this folder are kept."));
          return renderSettings();
        }));
      } } }),
      "Delete originals from this folder after review…"),
    f.delete_originals && !allowed("AllowDeleteOriginals")
      ? el("p", { class: "set-text", textContent: "Originals are kept: deleting them is turned off by your organisation." })
      : null);
}

function setSetting(changes) { attempt(() => call("set_settings", changes).then(renderSettings)); }

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

function eventText(e) {
  const at = e.at_s != null ? ` at ${fmtSeconds(e.at_s)}` : "";
  const len = e.seconds ? ` (about ${e.seconds < 1 ? e.seconds.toFixed(1) : Math.round(e.seconds)} s)` : "";
  return `${e.note || e.kind}${at}${len}`;
}

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
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", readVizColours);

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
  $("rec-state").textContent = REC_TEXT[rec.state] || rec.state;
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
  $("rec-note").textContent = lidNote + `${rec.device || "Microphone"}. Only the microphone is recorded, not computer sound. ` +
    "Tell everyone present the meeting is being recorded.";
}

function reportItem(rep) {
  const dismiss = linkButton("Dismiss",
    () => attempt(() => call("discard_recording_report", rep.id).then(renderRecorder)));
  const events = rep.events.length ? ` ${rep.events.map(eventText).join("; ")}.` : "";
  if (rep.pending && rep.recovered) {                // left when the app closed: the user decides
    const when = rep.started_at ? ` from ${new Date(rep.started_at * 1000).toLocaleString()}` : "";
    return notice("alert", `Recovered recording${when} (about ${fmtSeconds(rep.seconds)}). ${rep.note}`,
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
  return notice("done", `Recording: ${fmtSeconds(rep.seconds)} saved.${done}${events}`, dismiss);
}

// -- live drafts while recording (#104) -------------------------------------------------
// Rough text a few seconds behind speech. The app keeps it in memory only and
// replaces it with the full transcript after Stop and save. Shown as text only
// (textContent), not selectable, and not announced as it grows: the status
// line below it announces changes of state only. recording_status carries
// the draft's state and counts, never text; the text is fetched only when
// the count or generation changes, never while the window is hidden or the
// user has hidden the draft, and at most DRAFT_KEEP phrases stay on the page.
// renderDraft runs with every clock tick (10 a second), so it changes the
// page only when something has changed (#216's budget; #340's ratchets).
const DRAFT_KEEP = 300;
const DRAFT_EMPTY = {
  starting: "Getting draft text ready…",
  running: "Draft text appears here a few seconds after people speak.",
};
// shown: how many times Hide or Show was selected, so a fetch that either
// overtook is dropped rather than added to the box.
const draft = { rec: null, gen: null, count: 0, hidden: false, reading: false, said: null, shown: 0 };

function putText(node, text) { if (node.textContent !== text) node.textContent = text; }
function putHidden(node, hidden) { if (node.hidden !== hidden) node.hidden = hidden; }

function draftLine(p) {
  return p.kind === "skipped"
    ? el("p", { class: "rec-draft-skip",
                textContent: `(Draft skipped about ${Math.max(1, Math.round(p.seconds))} s to keep up. The recording itself is complete.)` })
    : el("p", { class: "rec-draft-line", textContent: p.text });
}

function draftEmpty(stateName) {
  const box = $("rec-draft-box");
  const lines = box.querySelector(".rec-draft-line, .rec-draft-skip");
  const text = lines ? "" : DRAFT_EMPTY[stateName] || "";
  const empty = box.querySelector(".rec-draft-empty");
  if (!text) { if (empty) empty.remove(); return; }
  if (empty) putText(empty, text);
  else box.append(el("p", { class: "rec-draft-empty", textContent: text }));
}

function clearDraft() {
  $("rec-draft-box").replaceChildren();
  draft.count = 0;
}

async function renderDraft(rec, d) {
  const area = $("rec-draft");
  const live = !!(rec && d && REC_KIND[rec.state] !== "saving" && REC_KIND[rec.state] !== "lost");
  putHidden(area, !live);
  if (!live) {                                      // stopped, saving or off: nothing kept
    if (draft.rec !== null) { clearDraft(); draft.rec = draft.gen = draft.said = null; }
    return;
  }
  if (draft.rec !== rec.id) {                       // Hide draft is for this recording only
    clearDraft();
    Object.assign(draft, { rec: rec.id, gen: d.generation, hidden: false, said: null });
  }
  if (draft.gen !== d.generation) { clearDraft(); draft.gen = d.generation; }
  // The status line: a change of state is announced once (polite).
  const said = draft.hidden ? "Draft text is hidden for this recording."
    : d.note || (d.state === "running" ? "Draft text is on." : "");
  if (said !== draft.said) {
    draft.said = said;
    $("rec-draft-status").textContent = said;
  }
  $("rec-draft-status").classList.toggle("sr", !draft.hidden && !d.note);
  putHidden($("rec-draft-box"), draft.hidden);
  const toggle = $("rec-draft-toggle");
  putText(toggle, draft.hidden ? "Show draft" : "Hide draft");
  if (toggle.getAttribute("aria-expanded") !== String(!draft.hidden))
    toggle.setAttribute("aria-expanded", String(!draft.hidden));
  if (draft.hidden) return;
  draftEmpty(d.state);
  if (d.count === draft.count || draft.reading || document.hidden) return;
  draft.reading = true;
  const shown = draft.shown;
  try {
    const got = await call("recording_draft", Math.max(draft.count, d.count - DRAFT_KEEP));
    if (got.generation !== draft.gen || draft.hidden || draft.rec !== rec.id
        || draft.shown !== shown) return;
    const box = $("rec-draft-box");
    const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 8;
    box.append(...got.phrases.map(draftLine));
    const lines = box.querySelectorAll(".rec-draft-line, .rec-draft-skip");
    for (let i = 0; i < lines.length - DRAFT_KEEP; i++) lines[i].remove();
    draft.count = got.since + got.phrases.length;
    draftEmpty(got.state);
    if (atEnd) box.scrollTop = box.scrollHeight;    // follow the newest, unless scrolled up
  } finally {
    draft.reading = false;
  }
}

function toggleDraft() {
  draft.hidden = !draft.hidden;
  draft.shown++;                                     // a fetch already on its way is dropped
  clearDraft();                                      // hidden: the page keeps no draft text
  draft.said = null;
  renderRecorder().catch(() => {});
}

let recReading = false;
async function renderRecorder(tick = false) {
  if (tick && recReading) return;                   // the previous read is still on its way
  recReading = true;
  let r;
  try { r = await call("recording_status"); } finally { recReading = false; }
  const sel = $("rec-device");
  // While recording (or saving) the list is not asked for (#369): it can wait,
  // and is asked for once the recording has ended (its microphone may be gone).
  const wasActive = state.recActive;
  state.recActive = !!r.recording || !!state.recSaving;
  if (wasActive && !state.recActive) micsStale();
  // Not awaited: the card is drawn now, and the list (with the microphone line
  // and Start) when it arrives. Safe, as Start and Test send the microphone's
  // index and name, and the app refuses any that no longer match. While no
  // microphone is listed, every poll asks, as before #369.
  if (r.enabled && (!sel.options.length || micLists["rec-device"].stale))
    relistMics("rec-device", showRecMics).catch((err) => say(String((err && err.message) || err)));
  const now = r.recording || {};
  const rec = state.recSaving ? { ...now, state: "saving", seconds: now.seconds || state.recSeconds || 0, events: now.events || [] }
    : r.recording;
  const active = !!rec;
  $("start").hidden = active;
  $("rec-card").hidden = !active;
  $("rec-start").hidden = !r.enabled;
  $("mic-line").hidden = !r.enabled;
  if (!r.enabled) $("mic-chooser").hidden = true;
  $("mic-name").textContent = $("rec-start-device").textContent = deviceLabel() || "No microphone";
  $("rec-start").disabled = $("rec-start-chosen").disabled = !sel.options.length;
  if (active) renderRecCard(rec, r.display, r.lid_override, r.lid_refused);
  else stopViz();
  renderDraft(rec, r.draft).catch(() => {});
  $("rec-events").replaceChildren(...(rec ? rec.events.filter((e) => !["paused", "resumed"].includes(e.kind))
    .map((e) => el("li", { textContent: eventText(e) })) : []));
  const reportsSig = JSON.stringify(r.reports);
  if (reportsSig !== state.reportsSig) {             // unchanged: keep buttons and focus
    state.reportsSig = reportsSig;
    $("rec-reports").replaceChildren(...r.reports.map(reportItem));
  }
  // Faster updates for the clock and level meter while recording.
  const live = active && rec.state !== "stopped";
  state.recordingLive = live;
  if (live && !recTimer && !document.hidden && state.view === "meetings")
    recTimer = setInterval(() => { if (state.view === "meetings") renderRecorder(true).catch(() => {}); }, 100);
  if (!live) stopRecTimer();
}

function stopRecTimer() {
  if (recTimer) { clearInterval(recTimer); recTimer = null; }
}

function wireRecorder() {
  const device = () => Number($("rec-device").value);
  // A changed device list means choosing again: reload it, keeping the choice if
  // that microphone is still there.
  const relist = async (err) => {
    if (/changed/.test(String(err && err.message)))
      await relistMics("rec-device", showRecMics).catch(() => {});
    throw err;
  };
  const start = (btn) => attempt(async () => {
    btn.disabled = true;           // one recording per click
    let r;
    try { r = await call("start_recording", device(), deviceName()).catch(relist); }
    finally { btn.disabled = false; }
    if (r && r.cancelled) { say(declined(r, "Recording not started.")); return; }
    $("mic-chooser").hidden = true;
    $("mic-change").setAttribute("aria-expanded", "false");
    renderRecorder();
  });
  $("rec-start").addEventListener("click", () => start($("rec-start")));
  $("rec-start-chosen").addEventListener("click", () => start($("rec-start-chosen")));
  $("mic-change").addEventListener("click", () => {
    const open = $("mic-chooser").hidden;
    $("mic-chooser").hidden = !open;
    $("mic-change").setAttribute("aria-expanded", String(open));
    if (open) {
      attempt(() => relistMics("rec-device", showRecMics));   // opened: ask again (#369)
      $("rec-device").focus();
    }
  });
  relistOnFocus("rec-device", showRecMics);
  $("rec-device").addEventListener("change", () => {
    $("mic-name").textContent = $("rec-start-device").textContent = deviceLabel();
  });
  $("rec-test").addEventListener("click", () => attempt(async () => {
    $("rec-test-result").textContent = "Listening for 3 seconds…";
    const t = await call("test_microphone", device(), deviceName()).catch(relist);
    $("rec-test-result").textContent = t.heard
      ? `Sound heard (loudest ${t.peak_db} dB).`
      : `Very quiet (loudest ${t.peak_db} dB). Speak, or check the microphone and its volume.`;
  }));
  $("rec-draft-toggle").addEventListener("click", toggleDraft);
  $("rec-pause").addEventListener("click", () => attempt(async () => { await call("pause_recording"); renderRecorder(); }));
  $("rec-resume").addEventListener("click", () => attempt(async () => { await call("resume_recording"); renderRecorder(); }));
  $("rec-discard").addEventListener("click", () => confirmDialog({
    title: "Discard this recording?",
    body: "Recording stops and the audio recorded so far is deleted from this computer. It is not transcribed and cannot be recovered.",
    check: "Delete this recording.", limits: false, danger: true,
    onConfirm: () => attempt(async () => {
      const busy = ["rec-discard", "rec-stop", "rec-pause", "rec-resume"];
      for (const id of busy) $(id).disabled = true;
      say("Discarding the recording…", { sticky: true });
      try { await call("discard_recording"); say("Recording discarded."); }
      finally { for (const id of busy) $(id).disabled = false; renderRecorder(); }
    }),
  }));
  $("rec-stop").addEventListener("click", () => attempt(async () => {
    state.recSaving = true;               // stopping can take a while: show Saving meanwhile
    $("rec-stop").disabled = true;
    renderRecorder().catch(() => {});
    try { await call("stop_recording"); }
    finally { state.recSaving = false; $("rec-stop").disabled = false; }
    say("Recording saved. It is being imported.");
    renderRecorder();
  }));
}

// While files are dragged over the window, show where to let go. The drop
// itself is handled natively; dragover repeats while over the window, so the
// overlay hides shortly after it stops (dragged away, dropped or cancelled).
let dragTimer = null;
function wireDrop() {
  const overlay = $("drop-overlay");
  const hide = () => { overlay.hidden = true; };
  document.addEventListener("dragover", (e) => {
    if (state.locked) return;            // nothing is imported while locked (#111 C4a)
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
    if (state.finishOnly) return;            // nothing is imported in finish-only mode
    overlay.hidden = false;
    clearTimeout(dragTimer);
    dragTimer = setTimeout(hide, 400);
  });
  document.addEventListener("drop", () => { clearTimeout(dragTimer); hide(); pollSoon(); });
}

let refreshAgain = false;

async function refresh() {
  if (!window.pywebview || state.locked) return;   // locked: nothing to refresh (#111 C4b)
  if (refreshing) {                    // one is running: run again when it ends, so an
    refreshAgain = true;               // action's refresh is never lost
    return;
  }
  refreshing = true;
  try {
    await renderStatus();
    if (state.locked) return;          // only the locked screen from here (#111 C4b)
    // The meeting list and reminders only when Python reports a change, the
    // view changed, or every LIST_REFRESH_POLLS polls anyway (ages and hold
    // reviews depend on the time). Counted in polls, not by the clock: the
    // design fixtures fix the clock for stable screenshots, and the idle
    // check must see this refresh. renderLists also keeps the sidebar count
    // current.
    state.sinceLists += 1;
    const lists = state.changes === undefined || state.changes !== state.listsSeen ||
                  state.view !== state.listsView || state.sinceLists >= LIST_REFRESH_POLLS;
    if (lists) {
      // The reminders first, so the rows can carry their retention chips:
      // the same two calls as before, in the other order. A failed reminders
      // call is reported but still lets the list and sidebar count draw.
      let reminders = null;
      if (state.view === "meetings") {
        try {
          reminders = await call("reminders");
        } catch (err) {
          say(String((err && err.message) || err));
        }
      }
      await renderLists(reminders);
      if (reminders) renderReminders(reminders);
      state.listsSeen = state.changes;
      state.listsView = state.view;
      state.sinceLists = 0;
    }
    if (state.view === "meetings") {
      await renderRecorder();
    } else if (state.view === "meeting") await renderMeeting();
    else if (state.view === "settings") await renderSettings();
    else if (state.view === "setup") await renderSetup();
    else if (state.view === "downloads") await renderDownloads();
  } catch (err) {
    say(String((err && err.message) || err));
  } finally {
    refreshing = false;
  }
  if (refreshAgain) {
    refreshAgain = false;
    await refresh();
    return;
  }
  if (pollTimer && pollDelay() < pollTimerDelay) schedulePoll();
}

// -- polling (docs/RELEASE_STANDARD.md, section 3) -----------------------------------
// Every 2 s while something is happening (a meeting being processed, an import,
// a recording) or Setup or Downloads is open; every 10 s otherwise; never while
// the window is hidden or minimised. Showing it again refreshes at once.
let pollTimer = null;
let pollTimerDelay = 0;

function pollDelay() {
  const active = state.active || state.wasActive ||
                 (state.recordingLive && state.view === "meetings") ||
                 state.view === "setup" || state.view === "downloads";
  return active ? POLL_MS : IDLE_POLL_MS;
}

// Soon, not in up to 10 s: after something the page did not start itself (a
// recording dropped on the window is imported by Python directly).
function pollSoon() {
  if (state.locked) return;
  state.active = true;                 // so an in-flight poll re-arms fast, not at 10 s;
  if (document.hidden) return;         // wasActive then keeps one more 2 s poll
  clearTimeout(pollTimer);
  pollTimerDelay = 500;
  pollTimer = setTimeout(pollNow, pollTimerDelay);
}

function schedulePoll() {
  if (state.locked) return;            // nothing changes until Unminute is opened again
  clearTimeout(pollTimer);
  pollTimer = null;
  if (document.hidden) return;
  pollTimerDelay = pollDelay();
  pollTimer = setTimeout(pollNow, pollTimerDelay);
}

async function pollNow() {
  await refresh();
  schedulePoll();
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(pollTimer);
    pollTimer = null;
    stopRecTimer();
    restartMeter(viz, meterClock);           // minimised: the meter's frames pause (#216)
  } else {
    state.sinceLists = LIST_REFRESH_POLLS;   // hidden for a while: ages and reviews moved on
    micsStale();                             // and microphones may have come or gone (#369)
    pollNow();
  }
});

// -- diagnostics (R9b, #130) ---------------------------------------------------
// The page's own script errors reach the app's log as a fixed kind and a line
// and column: never a message, which could quote meeting text. Python
// rate-limits them; a failed report is not reported again. The first error
// before the bridge is ready (in wire(), say) is kept until it is.
let reporting = false, bridgeReady = false, earlyError = null;
function reportUiError(event, line, col) {
  if (!bridgeReady) { earlyError = earlyError || [event, line, col]; return; }
  if (reporting) return;
  reporting = true;
  call("report_ui_error", event, Math.max(0, line | 0), Math.max(0, col | 0))
    .catch(() => {}).finally(() => { reporting = false; });
}
window.addEventListener("error", (e) => reportUiError("error", e.lineno, e.colno));
window.addEventListener("unhandledrejection", () => reportUiError("rejection", 0, 0));
window.addEventListener("pywebviewready", () => {
  bridgeReady = true;
  const early = earlyError;
  earlyError = null;
  if (early) reportUiError(...early);
});

function wire() {
  $("help-diagnostics").addEventListener("click", () => attempt(async () => {
    await call("copy_diagnostics");
    say("Copied diagnostics: no meeting content. Paste them into your support request.");
  }));
  $("s-add-folder").addEventListener("click", () => attempt(async () => {
    const added = await call("add_folder_dialog", $("s-include-existing").checked);
    if (added) say("Folder added. It will be checked now.");
    pollSoon();
  }));
  $("s-scan").addEventListener("click", () => attempt(async () => {
    await call("scan_now");
    say("Checking watched folders");
    pollSoon();
  }));
  wireSetup();
  wireHandoff();
  wireLocked();
  for (const b of document.querySelectorAll(".nav")) b.addEventListener("click", () => show(b.dataset.view));
  $("import").addEventListener("click", () => attempt(async () => {
    await call("clear_import_errors");
    const r = await call("import_dialog");
    if (r.accepted) say(`Importing ${r.accepted} recording${r.accepted > 1 ? "s" : ""}`);
    refresh();
  }));
  $("back").addEventListener("click", () => { stopEditing(); show("meetings"); });
  $("top-back").addEventListener("click", () => {
    if (state.view === "handoff") { closeHandoff(); return; }
    stopEditing(); show("meetings");
  });
  $("menu").addEventListener("click", () => setMenu(!document.body.classList.contains("menu-open")));
  $("empty-watch").addEventListener("click", () => show("settings"));
  $("s-downloads").addEventListener("click", () => show("downloads"));
  $("dl-back").addEventListener("click", () => show("settings"));
  wireDrop();
  // A title is one line: Enter saves it, and pasted line breaks become spaces.
  $("m-title").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("m-title").blur(); } });
  $("m-title").addEventListener("input", () => {
    const t = $("m-title");
    if (/[\r\n]/.test(t.value)) t.value = t.value.replace(/[\r\n]+/g, " ");
    fitTitle();
  });
  window.addEventListener("resize", fitTitle);
  $("m-title").addEventListener("change", () => attempt(() => call("rename", state.mid, $("m-title").value).then(refresh)));
  $("m-pause").addEventListener("click", () => attempt(() => call("pause", state.mid).then(refresh)));
  $("m-stop").addEventListener("click", () => attempt(() => call("stop_now", state.mid).then(refresh)));
  $("m-resume").addEventListener("click", () => attempt(() => call("resume", state.mid).then(refresh)));
  $("m-undo").addEventListener("click", undo);
  $("m-names").addEventListener("click", openNames);
  $("m-primary").addEventListener("click", () => {
    const d = state.detail;
    if (!d) return;
    if ($("m-primary").dataset.action === "review") confirmReviewed(d);
    else if ($("m-primary").dataset.action === "handoff") openHandoff();
  });
  $("m-banner-action").addEventListener("click", () => { if (state.bannerAction) state.bannerAction(); });
  for (const t of document.querySelectorAll("#view-meeting [role=tab]")) {
    t.addEventListener("click", () => setTab(t.dataset.tab));
    // Arrow keys move between tabs in the same tab list.
    t.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
      const tabs = [...t.parentElement.querySelectorAll("[role=tab]")];
      const next = tabs[(tabs.indexOf(t) + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      next.focus(); next.click();
    });
  }
  setTab("transcript");
  $("m-again").addEventListener("click", () => {
    const open = $("m-again-form").hidden;
    $("m-again-form").hidden = !open;
    $("m-again").setAttribute("aria-expanded", String(open));
    if (open) $("m-count").focus();
  });
  $("m-again-cancel").addEventListener("click", () => {
    $("m-again-form").hidden = true;
    $("m-again").setAttribute("aria-expanded", "false");
    $("m-again").focus();
  });
  wirePlayer();
  $("n-close").addEventListener("click", () => { stopAudio(); $("names").close(); });
  $("names").addEventListener("close", stopAudio);
  $("n-undo").addEventListener("click", undoSpeakers);
  $("m-undo-speakers").addEventListener("click", undoSpeakers);
  $("m-retranscribe").addEventListener("click", () =>
    transcribeAgain(state.mid, state.detail ? state.detail.model : null, !!(state.detail && state.detail.error)));
  $("m-rerun").addEventListener("click", () => {
    const v = $("m-count").value.trim();
    confirmDialog({
      title: "Identify speakers again?",
      body: "The speaker labels start afresh" + (v ? ` for ${v} people` : "") + ". Names and " +
        "corrections you made stay with the previous identification and are no longer shown. " +
        "The transcript text is not changed.",
      check: "Start speaker identification again.",
      limits: false,
      onConfirm: () => attempt(async () => {
        await call("rerun_speakers", state.mid, v ? Number(v) : null);
        $("m-again-form").hidden = true;
        $("m-again").setAttribute("aria-expanded", "false");
        say("Identifying speakers again");
        pollSoon();
      }),
    });
  });
  let searchTimer;
  $("m-search").addEventListener("input", () => {
    dropSearches();                      // answers to what was typed before are stale
    clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250);
  });
  $("m-search").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); nextHit(e.shiftKey ? -1 : 1); } });
  $("m-next").addEventListener("click", () => nextHit(1));
  $("m-prev").addEventListener("click", () => nextHit(-1));
  $("h-add").addEventListener("click", () => attempt(async () => {
    await call("add_hold", state.mid, $("h-scope").value, $("h-reason").value);
    $("h-reason").value = "";
    refresh();
  }));
  $("c-check").addEventListener("change", () => { $("c-ok").disabled = !$("c-check").checked; });
  for (const r of document.querySelectorAll('input[name="mode"]')) r.addEventListener("change", () => setSetting({ mode: r.value }));
  for (const r of document.querySelectorAll('input[name="recdisplay"]'))
    r.addEventListener("change", () => setSetting({ recording_display: r.value }));
  $("s-battery").addEventListener("change", () => setSetting({ battery_override: $("s-battery").checked }));
  $("s-speakers").addEventListener("change", () => setSetting({ speakers: $("s-speakers").checked }));
  $("s-gpu").addEventListener("change", () => setSetting({ gpu_speakers: $("s-gpu").checked }));
  $("s-microphone").addEventListener("change", () => setSetting({ microphone: $("s-microphone").checked }));
  $("s-lid").addEventListener("change", () => setSetting({ lid_override: $("s-lid").checked }));
  $("s-drafts").addEventListener("change", () => setSetting({ live_drafts: $("s-drafts").checked }));
  for (const r of document.querySelectorAll('input[name="draftsaudio"]'))
    r.addEventListener("change", () => setSetting({ live_drafts_audio: r.value }));
  wireRecorder();
  $("s-model").addEventListener("change", () => setSetting({ model: $("s-model").value }));
  document.addEventListener("keydown", (e) => {
    const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement
      || e.target instanceof HTMLSelectElement || (e.target instanceof HTMLElement && e.target.isContentEditable);
    if (e.key === "?" && !typing && !e.ctrlKey && !e.metaKey && !document.querySelector("dialog[open]")) {
      e.preventDefault(); show("help"); return;
    }
    if (e.key === "Escape" && document.body.classList.contains("menu-open")) { setMenu(false); $("menu").focus(); }
    else if (e.key === "Escape" && state.view === "handoff" && !typing && !document.querySelector("dialog[open]")) {
      // The template editor closes first; changes not yet applied keep the page open.
      if (!$("ho-editor").hidden) closeTemplateEditor();
      else if (handoffState.dirty) say("Apply your changes first, or go back with the link at the top.");
      else closeHandoff();
    }
    // The player: Space plays or pauses, J and K move to the next or previous passage.
    if (state.view === "meeting" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey
        && !document.querySelector("dialog[open]") && state.detail && state.detail.audio_available) {
      const onControl = e.target instanceof HTMLButtonElement || (e.target instanceof HTMLElement && e.target.getAttribute("role") === "slider");
      if (e.key === " " && !onControl) { e.preventDefault(); togglePlay(); return; }
      if (e.key === "j" || e.key === "J") { e.preventDefault(); nextPassage(1); return; }
      if (e.key === "k" || e.key === "K") { e.preventDefault(); nextPassage(-1); return; }
    }
    if (state.view === "meeting" && (e.ctrlKey || e.metaKey) && e.key === "z" && !state.editing
        && !typing) { e.preventDefault(); undo(); }
    if (state.view === "meeting" && (e.ctrlKey || e.metaKey) && e.key === "f") { e.preventDefault(); $("m-search").focus(); }
    // Keyboard zoom (#27): the browser's own Ctrl+plus/minus/0 keys are off in this window.
    if ((e.ctrlKey || e.metaKey) && !e.altKey) {
      const dir = ["+", "="].includes(e.key) ? "in" : ["-", "_"].includes(e.key) ? "out" : e.key === "0" ? "reset" : null;
      if (dir) { e.preventDefault(); attempt(() => call("zoom", dir)); }
    }
  });
}

// Closing the window while recording: the app keeps the window open and asks.
function askClose() {
  const dlg = $("close-rec");
  if (dlg.open) return true;
  dlg.returnValue = "";
  dlg.onclose = () => attempt(async () => {
    const choice = dlg.returnValue;
    if (choice === "save") {
      // If it already stopped by itself it was saved: close either way.
      try { await call("stop_recording"); } finally { await call("close_app"); }
    } else if (choice === "discard") {
      await call("discard_recording");             // on an error the window stays, and says why
      await call("close_app");
    } else renderRecorder();                         // keep recording
  });
  dlg.showModal();
  return true;
}

window.workspace = { call, show, openMeeting, refresh, state, play, say, setStep, askClose };   // check tools, close

wire();
window.addEventListener("pywebviewready", async () => {
  // A locked workspace (#111 C4b): the status first, and if it is locked only
  // the locked screen, with no other request and no polling.
  let st = null;
  try { st = await call("status"); } catch (err) { /* shown by refresh */ }
  if (st && st.locked) {
    renderLocked(st.locked);
    return;
  }
  // First run, or no usable speech model: start with Setup.
  let s = null;
  try { s = await call("setup_state"); } catch (err) { /* shown by refresh */ }
  if (s) state.managed = s.managed;                 // what policy allows, from the start
  if (s && (!s.onboarded || !s.speech_ready)) {
    suState.step = s.onboarded ? 2 : 1;
    show("setup");
    setStep(suState.step);
  } else show("meetings");
  schedulePoll();
});
