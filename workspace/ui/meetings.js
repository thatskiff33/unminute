// Unminute UI: the meetings page and the locked screen.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

// -- meetings page -----------------------------------------------------------------
function dayWord(days) { return days === 0 ? "Today" : days === 1 ? "Yesterday" : `${days} days old`; }
function lengthWord(s) { return s == null ? null : s < 60 ? "under 1 min" : `${Math.round(s / 60)} min`; }
function speakersWord(n) { return n ? `${n} speaker${n === 1 ? "" : "s"}` : null; }
const MODE = { quiet: "Quiet mode", faster: "Faster mode" };

function discardLink(m) {
  // The app asks in its own window, which the page cannot answer (#111 A6).
  return linkButton("Discard…", () => attempt(async () => {
    const r = await call("discard_meeting", m.id);
    if (r && r.cancelled) say(declined(r, "Nothing was discarded."));
    else sayDone("Meeting discarded.");
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
    sayDone("Copied the details for IT. They hold no meeting content.");
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
