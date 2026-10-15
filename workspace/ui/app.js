// Unminute UI (#20). All meeting text is rendered with textContent,
// never innerHTML, so markup in a transcript is shown as text, not executed.
// The page holds no file paths: recordings arrive through the native file
// dialog or a native drop, handled in Python.
"use strict";

const $ = (id) => document.getElementById(id);
const DAY = 86400;
const POLL_MS = 2000;
const PAGE = 1000;           // transcript rows per bridge call
const SAVE_DELAY_MS = 800;   // autosave after typing stops

const state = {
  view: "meetings", mid: null, detail: null, total: -1, signature: "",
  hits: [], hit: -1, editing: null, saveTimer: null,
  version: null,                 // null = current version; else an earlier one (read only)
  saving: Promise.resolve(),     // edits are saved strictly in order
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
const OK_MESSAGE = /^(Saved|Copied|Name saved|Template saved|Download started|Folder added|Meeting discarded|Recording saved|Edit undone|Speaker change undone|Token removed|Removed)/;
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
  if (view === "inbox" || view === "active") view = "meetings";   // their earlier names
  if (view !== "meeting") stopAudio();
  if (view !== "meeting" && view !== "handoff") stopEditing();
  if (view !== state.view) { suState.sig = ""; state.downloadsSig = ""; }   // rebuilt on arrival
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
  return linkButton("Discard…", () => attempt(async () => {
    const d = await call("detail", m.id);            // its own deletion limits
    confirmDialog({
      title: "Discard this meeting?",
      body: "Its audio, transcript, edits and speaker names are deleted from this workspace and cannot be recovered. Your original recording file is not deleted. For real meetings, use Transcript reviewed and then the final confirmation instead.",
      check: "Delete this meeting and everything in it.", limitsText: d.limits, danger: true,
      onConfirm: () => attempt(() => call("discard_meeting", m.id).then(() => { say("Meeting discarded."); refresh(); })),
    });
  }), "Delete a meeting you do not need (for example a test recording)");
}

function titleLine(m) {
  return el("div", { class: "row-title" },
    el("button", { type: "button", class: "open", textContent: m.title, on: { click: () => openMeeting(m.id) } }),
    m.held ? el("span", { class: "held", textContent: "Held" }) : null);
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

// Transcribing and speaker identification: a card per meeting with its
// progress under the title. Speaker identification has no safe point inside,
// so Pause stops the pass and Resume restarts it.
function workRow(m, busyTitle) {
  const pause = (title) => button("Pause", () => attempt(() => call("pause", m.id).then(refresh)), { title });
  const resume = () => button("Resume", () => attempt(() => call("resume", m.id).then(refresh)), { primary: true });
  const stopNow = () => button("Stop now", () => attempt(() => call("stop_now", m.id).then(refresh)),
                               { title: "Stops at once; the part in progress is redone later" });
  let meta, pill = null, bar = null, actions;
  if (m.stage === "inbox" && m.paused) {
    meta = `${m.progress}% · paused by you`;
    pill = ["paused", "Paused"];
    bar = ["paused", m.progress];
    actions = [resume(), discardLink(m)];
  } else if (m.stage === "inbox" && m.busy === "transcribe") {
    meta = joinParts(`${m.progress}%`, MODE[state.mode]);
    bar = ["", m.progress];
    actions = [pause("Stops at the next safe point; nothing is lost"), stopNow(), discardLink(m)];
  } else if (m.stage === "inbox") {
    meta = busyTitle ? `starts after ${busyTitle}` : "Waiting";
    pill = ["waiting", "Waiting"];
    actions = [pause("Keeps it from starting until you resume it"), discardLink(m)];
  } else if (m.paused) {
    meta = "text ready · speaker identification paused";
    pill = ["paused", "Paused"];
    actions = [resume(), discardLink(m)];
  } else {
    meta = "text ready · working out who spoke";
    pill = ["", "Identifying speakers"];
    bar = ["busy", null];
    actions = [pause("Stops speaker identification now; Resume starts it again from the beginning. The transcript is not affected."),
               discardLink(m)];
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

async function renderLists() {
  const items = await call("meetings");
  // The sidebar count: meetings waiting for the user.
  const waiting = items.filter((m) => ["attention", "ready"].includes(groupOf(m))).length;
  $("nav-count").hidden = !waiting;
  $("nav-count").textContent = String(waiting);
  $("nav-count-sr").textContent = waiting ? `, ${waiting} waiting for you` : "";
  if (state.view !== "meetings") return;
  const busy = items.find((m) => m.busy === "transcribe");
  const sections = [];
  for (const [key, title, kind] of GROUPS) {
    const mine = items.filter((m) => groupOf(m) === key);
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
  state.mode = s.mode;
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
}

async function renderReminders() {
  const r = await call("reminders");
  const lines = [];
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
function signatureOf(d) {
  return [d.progress, d.speakers && d.speakers.status, d.speakers && d.speakers.started_at,
          d.model, d.reviewed_at,
          d.audio_available].join("|");
}

// "Parakeet v2" for "parakeet-tdt-0.6b-v2"; other names as they are.
function modelName(m) {
  if (!m) return null;
  const s = String(m).toLowerCase();
  if (s.includes("parakeet")) return s.includes("v3") ? "Parakeet v3" : s.includes("v2") ? "Parakeet v2" : m;
  if (s.includes("turbo")) return "Whisper large-v3 turbo";
  return m;
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
    return v ? `Earlier version: ${modelName(v.model)} (${dateWord(v.created_at)}) · ${plural(v.edits, "edited passage")}` : "";
  }
  const when = d.reviewed_at ? `Reviewed ${whenWord(d.reviewed_at)}` : `Imported ${whenWord(d.created_at)}`;
  return joinParts(when, lengthWord(d.duration_s), speakersWord(d.speaker_count), modelName(d.model));
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
  $("m-retranscribe").disabled = !d.audio_available || !!d.reviewed_at || transcribing;

  const sig = signatureOf(d) + "|" + state.version + "|" + state.speakerList.map((x) => x.label + x.name).join(",");
  if (sig !== state.signature && !state.editing) {
    state.signature = sig;
    await renderTranscript();
  }
}

// The title box grows with the title, so a long one wraps like a heading.
function fitTitle() {
  const t = $("m-title");
  t.style.height = "auto";
  t.style.height = `${t.scrollHeight}px`;
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
        el("span", { class: "step-dot", "aria-hidden": "true", textContent: done[i] ? "✓" : String(i + 1) }),
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
  const sig = JSON.stringify([s, d.stage, d.busy, d.audio_available, d.reviewed_at, state.version,
                              state.speakerList, state.speakerUndo]);
  if (sig === state.speakersSig) return;
  state.speakersSig = sig;
  const box = $("m-speakers");
  const card = (cls, title, text, ...extra) => el("div", { class: `panel-card ${cls}` },
    title, text ? el("span", { class: "panel-text", textContent: text }) : null, ...extra);
  let content = null;
  if (!s && d.stage === "inbox")
    content = card("soft", el("span", { class: "panel-title", textContent: "Not identified yet" }),
      "Speakers are worked out once the text is ready. You can review the words in the meantime.");
  else if (!s)
    content = card("soft", el("span", { class: "panel-title", textContent: "Not identified" }),
      "Speaker identification runs after transcription when it is downloaded and turned on in Settings.");
  else if (s.status === "running")
    content = card("soft", el("span", { class: "running" }, el("span", { class: "spinner", "aria-hidden": "true" }),
        "Identifying speakers…"),
      "The transcript is ready to review while this runs.",
      d.busy ? button("Pause", () => attempt(() => call("pause", state.mid).then(refresh)),
                      { title: "Stops speaker identification now; Resume starts it again from the beginning." }) : null);
  else if (s.status === "failed")
    content = card("bad", el("span", { class: "panel-title", textContent: "Speaker identification did not work" }),
      `${s.error || "Unknown reason"}. The transcript is still usable.`);
  else if (s.status === "interrupted")
    content = card("soft", el("span", { class: "panel-title", textContent: "Interrupted" }),
      s.error || "Speaker identification was interrupted and will start again from the beginning.");
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

function renderVersions(d) {
  const viewing = state.version !== null;
  const card = (eyebrow, model, edits, open) => {
    const kids = [el("span", { class: "ver-eyebrow", textContent: eyebrow }),
                  el("span", { class: "ver-model", textContent: model }),
                  el("span", { class: "ver-edits", textContent: edits })];
    return open ? el("button", { type: "button", class: "ver-card", on: { click: open } }, ...kids)
      : el("div", { class: "ver-card now" }, ...kids);
  };
  const items = [el("li", {}, card("Current", modelName(d.model) || "No model yet", plural(d.edits, "edited passage"),
    viewing ? () => viewVersion(null) : null))];
  for (const v of d.older_versions) {
    const mine = state.version === v.index;
    items.push(el("li", {}, card(mine ? "Earlier · viewing" : "Earlier · read only",
      `${modelName(v.model)} (${dateWord(v.created_at)})`,
      mine ? plural(v.edits, "edited passage") : `${plural(v.edits, "edited passage")} · Open`,
      mine ? null : () => viewVersion(v.index))));
  }
  $("m-versions").replaceChildren(...items);
}

function viewVersion(index) {
  stopEditing();
  state.version = index;
  state.signature = "";
  $("m-search").value = ""; state.hits = []; $("m-hits").textContent = "";
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
  const name = el("input", { value: sp.name, maxLength: 80, "aria-label": `Name for ${sp.name}` });
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
  const into = el("select", { "aria-label": `Merge ${sp.name} into` },
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
  if (state.editing || state.mid !== mid || state.version !== version) {
    state.signature = "";
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
  state.playingRow = null;
  document.scrollingElement.scrollTop = scroll;
  document.querySelector(".transcript-wrap").scrollTop = paneScroll;
  if ($("m-search").value) runSearch(false);
  updatePlayer();
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
async function runSearch(jump = true) {
  for (const li of document.querySelectorAll("#transcript li.hit")) li.classList.remove("hit", "current");
  const q = $("m-search").value.trim();
  $("m-search").closest(".search").classList.toggle("active", !!q);
  if (!q) { state.hits = []; $("m-hits").textContent = ""; $("m-hit-nav").hidden = true; return; }
  state.hits = await attempt(() => call("search", state.mid, q, state.version)) || [];
  for (const id of state.hits) { const row = rowFor(id); if (row) row.classList.add("hit"); }
  $("m-hits").textContent = state.hits.length ? `${state.hits.length} found` : "No matches";
  $("m-hit-nav").hidden = state.hits.length === 0;
  if (jump) { state.hit = -1; nextHit(1); }
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
    $("p-speed").setAttribute("aria-label", `Playback speed ${state.speed} times`);
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
      "The app does not clear your clipboard or delete Markdown files you saved: copy something else to replace the clipboard (the app's copies are kept out of Windows clipboard history, but check Win+V if you copied the text another way), and delete saved handoff files when you no longer need them.",
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
  for (const id of ["ho-copy", "ho-copy-prompt", "ho-save"]) $(id).disabled = !ready;
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
                      "aria-label": `Leave out ${fmtTime(it.t)}${it.who ? ` ${it.who}` : ""}: ${it.text}` }),
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
  handoffSaid(kind === "combined"
    ? `Copied the prompt and transcript (${r.chars.toLocaleString("en-NZ")} characters). Paste it into your organisation's approved AI assistant.` +
      (r.long ? " It is long: if the assistant cuts it off, use Save Markdown and Copy prompt only instead." : "")
    : "Copied the prompt only. Attach the saved Markdown file in your approved AI assistant, then paste the prompt.");
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
const suState = { step: 1, skipSpeakers: false, mic: null, devices: false, returnTo: null };

// "Parakeet v2" from "Speech to text: Parakeet v2 (recommended)".
function cardName(c) {
  return c.title.replace(/^Speech to text:\s*/i, "").replace(/\s*\((recommended|optional)\)\s*$/i, "");
}
// A download's progress, when its latest message states one.
function downloadPercent(message) {
  const of = /([\d.]+)\s*(?:GB)?\s+of\s+([\d.]+)\s*GB/i.exec(message || "");
  if (of) return Math.min(100, Math.round((100 * parseFloat(of[1])) / parseFloat(of[2])));
  const pct = /(\d{1,3})\s*%/.exec(message || "");
  return pct ? Math.min(100, Number(pct[1])) : null;
}
const isRunning = (c) => !!(c && c.download && c.download.state === "running");

function progressBlock(dl, note) {
  const pct = downloadPercent(dl.message);
  const bar = el("div", { class: `progress${pct === null ? " busy" : ""}`, role: "progressbar", "aria-label": "Download",
    ...(pct === null ? {} : { "aria-valuenow": String(pct), "aria-valuemin": "0", "aria-valuemax": "100" }) }, el("span", {}));
  if (pct !== null) bar.firstChild.style.width = `${pct}%`;
  return el("div", { class: "su-progress" }, bar,
    el("div", { class: "su-progress-text" }, el("span", { textContent: dl.message }), note ? el("span", { textContent: note }) : null));
}

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
  if (n === 4) renderSuMic();
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
      el("span", { class: "su-dot", "aria-hidden": "true", textContent: kind === "done" ? "✓" : String(n) }),
      el("span", { class: "su-name", textContent: name },
         kind === "done" ? el("span", { class: "sr", textContent: ", done" }) : null),
      note ? el("span", { class: "su-opt", textContent: note }) : null);
    if (kind === "now") li.setAttribute("aria-current", "step");
    return li;
  }));
}

function modelCard(c, s) {
  const dl = c.download;
  const busy = s.cards.some(isRunning);
  let action;
  if (isRunning(c)) action = button("Cancel", () => attempt(() => call("cancel_download").then(renderSetup)));
  else if (c.state === "ready")
    action = el("span", { class: "su-ready" }, el("span", { class: "tick" }, icon("tick", 13)), "Ready");
  else {
    action = button(c.state === "missing" ? `Download (${c.size_gb} GB)` : "Repair", () => startDownload(c.name), { primary: true });
    action.disabled = busy;
  }
  return el("div", { class: "su-model" },
    el("div", { class: "su-model-top" },
      el("div", { class: "su-model-text" },
        el("span", { class: "su-model-name" }, cardName(c),
           c.recommended ? el("span", { class: "rec-badge", textContent: "Recommended" }) : null),
        el("span", { class: "su-model-about", textContent: `${c.about} ${c.size_gb} GB, downloaded once.` })),
      action),
    isRunning(c) ? progressBlock(dl, "Each file is checked before use") : null,
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
  const running = isRunning(c);
  const busy = s.cards.some(isRunning);
  const tag = c.role === "speech"
    ? joinParts(c.recommended ? "Recommended" : null, inUse ? "in use" : null) : "Optional";
  const about = c.role === "speech" ? `Speech to text. ${c.about}` : c.about.split(/(?<=\.)\s/)[0];
  const [pillKind, pillText] = running ? ["", "Downloading"]
    : c.state === "ready" ? ["ready", "Ready"]
    : dl && dl.state === "failed" ? ["bad", "Needs attention"]
    : c.state === "invalid" ? ["bad", "Damaged"] : ["", "Not downloaded"];
  let action;
  if (running) action = button("Cancel", () => attempt(() => call("cancel_download").then(refresh)));
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
  else action = button(c.state === "missing" ? `Download (${c.size_gb} GB)` : "Repair", () => startDownload(c.name), { primary: true });
  action.disabled = busy && !running;
  const remove = c.removable && !running ? linkButton("Remove…", () => confirmDialog({
    title: `Remove ${cardName(c)}?`,
    body: `This deletes it from this computer, including any damaged or unfinished copies. Meetings already transcribed keep their text. You can download it again from Downloads.${c.shared_with_earlier_app ? " If the earlier Whisper Transcriber app is installed, it uses the same model files." : ""}`,
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
    running ? progressBlock(dl, null) : null,
    !running && dl && dl.state === "failed" ? el("span", { class: "dl-msg bad", textContent: dl.message }) : null,
    el("details", {}, el("summary", { textContent: "Details" }),
      el("p", { textContent: c.status }),
      el("p", { textContent: `Licence ${c.licence} · ${c.source} · revision ${c.revision}` })));
}

async function renderSetup() {
  const s = await call("setup_state");
  state.setup = s;
  renderSuSteps();
  const speech = s.cards.filter((c) => c.role === "speech");
  const rec = speech.find((c) => c.recommended) || speech[0];
  const others = speech.filter((c) => c !== rec);
  const sig = JSON.stringify(s.cards);
  if (sig !== suState.sig) {                         // unchanged: keep open Details and focus
    suState.sig = sig;
    replaceKeeping($("su-model"), modelCard(rec, s));
    replaceKeeping($("setup-cards"), ...others.map((c) => downloadRow(c, s, false)));
  }
  $("su-other-title").textContent = `Other options: ${others.map(cardName).join(", ")}`;
  $("su-2-note").textContent = isRunning(rec) ? "You can keep going while it downloads" : "";
  const speaker = s.cards.find((c) => c.role === "speakers") || {};
  $("speaker-setup").hidden = speaker.state === "ready";
  $("s-download-speakers").disabled = isRunning(speaker);
  $("s-use-remembered").hidden = !s.token_remembered || speaker.state === "ready";
  $("s-use-earlier").hidden = !s.earlier_token || s.token_remembered || speaker.state === "ready";
  $("s-remove-token").hidden = !s.token_remembered;
  $("s-token-note").textContent = s.token_remembered
    ? "A Hugging Face token is remembered in Windows Credential Manager. Removing it does not affect models already downloaded."
    : "";
  $("su-speaker-status").textContent = speaker.state === "ready" ? "The speaker model is downloaded and ready."
    : speaker.download ? speaker.download.message : "";
  renderSummary(s);
  $("s-finish").disabled = !s.speech_ready;
  $("s-finish-note").textContent = s.speech_ready ? "" : "Download a speech model first (Parakeet v2 is recommended).";
}

function renderSummary(s) {
  const rec = s.cards.find((c) => c.role === "speech" && c.recommended);
  const ready = s.cards.find((c) => c.role === "speech" && c.state === "ready");
  const speaker = s.cards.find((c) => c.role === "speakers") || {};
  const line = (label, value, on) => el("li", {},
    el("span", { class: `s-icon${on ? "" : " off"}` }, on ? icon("tick", 14) : el("span", { "aria-hidden": "true", textContent: "–" })),
    el("span", { class: "s-label", textContent: label }), el("span", { class: "s-value", textContent: value }));
  const speech = ready ? `${cardName(ready)}, ready` : isRunning(rec) ? "Downloading…" : "Not downloaded yet";
  const names = speaker.state === "ready" && !suState.skipSpeakers ? "Ready"
    : suState.skipSpeakers ? "Skipped. Turn on in Settings."
    : isRunning(speaker) ? "Downloading…" : "Not set up. You can add it later in Settings.";
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
  if (suState.mic && !suState.devices) {
    const devices = await attempt(() => call("microphone_devices")) || [];
    $("su-device").replaceChildren(...devices.map((d) => el("option", { value: String(d.index), "data-name": d.name,
      textContent: d.name + (d.default ? " (default)" : "") })));
    const def = devices.find((d) => d.default);
    if (def) $("su-device").value = String(def.index);
    suState.devices = devices.length > 0;
    $("su-test").disabled = !devices.length;
    if (!devices.length) { $("su-test-result").hidden = false; $("su-test-result").textContent = "No microphone found."; }
  }
}

function startDownload(name, extra = {}) {
  return attempt(async () => {
    await call("download_model", name, extra.token || null, extra.remember !== false, !!extra.earlier);
    say("Download started");
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
  $("s-use-earlier").addEventListener("click", () =>
    startDownload(speakerName(), { earlier: true, remember: $("s-remember").checked }));
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
    suState.devices = suState.devices && $("su-mic").checked;
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
    const t = await call("test_microphone", Number($("su-device").value), opt ? opt.dataset.name : "");
    box.classList.toggle("quiet", !t.heard);
    box.textContent = t.heard ? `Sound heard (loudest ${t.peak_db} dB).`
      : `Very quiet (loudest ${t.peak_db} dB). Speak, or check the microphone and its volume.`;
  }));
  $("s-finish").addEventListener("click", () => attempt(async () => {
    const speaker = state.setup.cards.find((c) => c.role === "speakers") || {};
    // First run: speaker names are on when their model is downloaded or downloading, unless
    // skipped. Later visits (a speech model went missing) keep the Settings choice.
    const speakers = state.setup.onboarded ? !!(await call("settings")).speakers
      : !suState.skipSpeakers && (speaker.state === "ready" || isRunning(speaker));
    await call("finish_setup", speakers);
    show("meetings");
  }));
}

// -- downloads -------------------------------------------------------------------------
async function renderDownloads() {
  const s = await call("setup_state");
  state.setup = s;
  const settings = await call("settings");
  const sig = JSON.stringify([s.cards, settings.model]);
  if (sig !== state.downloadsSig) {                  // unchanged: keep open Details and focus
    state.downloadsSig = sig;
    replaceKeeping($("dl-list"), ...s.cards.map((c) => downloadRow(c, s, c.name === settings.model)));
  }
  $("dl-token").hidden = !s.token_remembered;
  $("dl-token-note").textContent = "A Hugging Face token is remembered in Windows Credential Manager. Removing it does not affect models already downloaded.";
}

// -- settings ------------------------------------------------------------------
const MODE_NOTE = {
  quiet: "Quiet leaves most of the computer free while it transcribes. Recommended.",
  faster: "Faster uses more of the computer while transcribing.",
};

async function renderSettings() {
  const s = await call("settings");
  for (const r of document.querySelectorAll('input[name="mode"]')) r.checked = r.value === s.mode;
  $("s-mode-note").textContent = MODE_NOTE[s.mode] || "";
  $("s-battery").checked = s.battery_override;
  $("s-speakers").checked = s.speakers;
  // Graphics card (speaker identification only): shown when an NVIDIA card is present.
  $("s-gpu-box").hidden = !s.gpu.card;
  $("s-gpu").checked = !!s.gpu_speakers;
  // Always possible to turn off; turning on needs the component and driver.
  $("s-gpu").disabled = !s.gpu_speakers && (s.gpu.component !== "ready" || !s.gpu.driver_ok);
  $("s-gpu-note").textContent = !s.gpu.card ? "" : !s.gpu.driver_ok
    ? `${s.gpu.card}: the NVIDIA driver is too old (580 or later is needed). Ask IT to update it.`
    : s.gpu.component !== "ready"
      ? (s.gpu_speakers
        ? "The graphics card component needs repair in Downloads; until then speaker names are worked out on the processor."
        : `${s.gpu.card} found. Download "Graphics card acceleration" in Downloads to use it.`)
      : s.mode !== "faster" && s.gpu_speakers
        ? "Used only in Faster mode (Processing, above)."
        : `${s.gpu.card}. Speeds up speaker names in Faster mode only.`;
  $("s-microphone").checked = !!s.microphone;
  const sel = $("s-model");
  if (document.activeElement !== sel) {
    sel.replaceChildren(...s.choices.map((c) => el("option", { value: c.name,
      textContent: `${modelName(c.name)}${/recommended/.test(c.label) ? " (recommended)" : ""}` })));
    sel.value = s.model;
  }
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
  $("s-deployment").textContent = s.update_blockers.length && dep.mode !== "managed"
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
    f.waiting ? `${f.waiting} waiting until fully copied` : null,
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
      on: { change: (e) => {
        if (!e.target.checked) { attempt(() => call("set_folder_authority", f.id, false, null).then(renderSettings)); return; }
        e.target.checked = false;
        confirmDialog({
          title: "Allow deleting originals from this folder?",
          body: "After you confirm a meeting's transcript is reviewed, the app may delete the original recording from this folder, but only if it is exactly the file that was imported. " +
            "Do not allow this for shared folders, Teams or channel storage, or recordings someone else (such as a meeting organiser) owns. Copies elsewhere, such as on a phone, in the cloud recycle bin or in backups, are not removed.",
          check: "I am responsible for recordings in this folder and allow the app to delete reviewed originals from it.",
          limits: false,
          onConfirm: () => attempt(() => call("set_folder_authority", f.id, true, null).then(renderSettings)),
        });
      } } }),
      "Delete originals from this folder after review…"));
}

function setSetting(changes) { attempt(() => call("set_settings", changes).then(renderSettings)); }

// -- refresh loop ----------------------------------------------------------------
let refreshing = false;
// -- microphone recording (#30) ---------------------------------------------------

const REC_TEXT = { recording: "Recording", paused: "Paused", stopped: "Saving",
                   saving: "Saving", device_lost: "Microphone lost", failed: "Could not save" };
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

// The level meter shows the last few seconds as bars, newest on the right.
const LEVEL_BARS = 36;
const levels = [];
const REC_KIND = { recording: "", paused: "paused", device_lost: "lost", failed: "lost",
                   stopped: "saving", saving: "saving" };

function deviceName() {
  const opt = $("rec-device").selectedOptions[0];
  return opt ? opt.dataset.name : "";
}

function renderLevel(rec, tick) {
  const box = $("rec-level");
  if (box.children.length !== LEVEL_BARS)
    box.replaceChildren(...Array.from({ length: LEVEL_BARS }, () => el("span", {})));
  if (rec.state === "recording" && tick) {
    levels.push(Math.max(-60, Math.min(0, rec.level_db)));
    if (levels.length > LEVEL_BARS) levels.shift();
  }
  [...box.children].forEach((bar, i) => {
    const db = levels[i];
    bar.classList.toggle("on", db !== undefined);
    bar.style.height = db === undefined ? "" : `${Math.round(8 + ((db + 60) / 60) * 56)}px`;
  });
  box.setAttribute("aria-valuenow", String(Math.round(levels.length ? levels[levels.length - 1] : -60)));
}

function renderRecCard(rec, tick) {
  const kind = REC_KIND[rec.state] ?? "saving";
  $("rec-card").className = `rec-card ${kind}`.trim();
  $("rec-state").textContent = REC_TEXT[rec.state] || rec.state;
  state.recSeconds = rec.seconds;
  $("rec-clock").textContent = fmtSeconds(rec.seconds);
  $("rec-icon").hidden = kind !== "lost";
  $("rec-spinner").hidden = kind !== "saving";
  $("rec-level").hidden = kind !== "";
  if (kind === "") renderLevel(rec, tick);
  $("rec-middle").hidden = kind !== "paused";
  $("rec-middle").textContent = "Nothing is being recorded. Resume to carry on in the same recording.";
  $("rec-sub").hidden = kind !== "lost" && kind !== "saving";
  $("rec-sub").textContent = kind === "saving"
    ? "The recording is then imported and transcribed like any other."
    : rec.state === "failed" ? "The recording stopped. Saving what was recorded."
    : `${fmtSeconds(rec.seconds)} recorded before it disconnected. Saving what was recorded.`;
  $("rec-pause").hidden = kind !== "";
  $("rec-resume").hidden = kind !== "paused";
  $("rec-stop").hidden = kind === "saving" || kind === "lost";   // a lost microphone saves by itself
  $("rec-stop").className = kind === "paused" ? "" : "primary";
  $("rec-note").hidden = kind !== "" && kind !== "paused";
  $("rec-note").textContent = `${rec.device || "Microphone"}. Only the microphone is recorded, not computer sound. ` +
    "Tell everyone present the meeting is being recorded.";
}

function reportItem(rep) {
  const dismiss = linkButton("Dismiss",
    () => attempt(() => call("discard_recording_report", rep.id).then(renderRecorder)));
  const events = rep.events.length ? ` ${rep.events.map(eventText).join("; ")}.` : "";
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
  if (rep.events.some((e) => e.kind === "device_lost"))
    return notice("alert", `Microphone lost. ${fmtSeconds(rep.seconds)} recorded before it disconnected was saved.${done}`, dismiss);
  return notice("done", `Recording: ${fmtSeconds(rep.seconds)} saved.${done}${events}`, dismiss);
}

async function renderRecorder(tick = false) {
  const r = await call("recording_status");
  const sel = $("rec-device");
  if (r.enabled && !sel.options.length) {
    const devices = await call("microphone_devices");
    sel.replaceChildren(...devices.map((d) => el("option", { value: String(d.index), "data-name": d.name,
      textContent: d.name + (d.default ? " (default)" : "") })));
    const def = devices.find((d) => d.default);
    if (def) sel.value = String(def.index);
    if (!devices.length) $("rec-test-result").textContent = "No microphone found.";
  }
  const now = r.recording || {};
  const rec = state.recSaving ? { ...now, state: "saving", seconds: now.seconds || state.recSeconds || 0, events: now.events || [] }
    : r.recording;
  const active = !!rec;
  $("start").hidden = active;
  $("rec-card").hidden = !active;
  $("rec-start").hidden = !r.enabled;
  $("mic-line").hidden = !r.enabled;
  if (!r.enabled) $("mic-chooser").hidden = true;
  $("mic-name").textContent = $("rec-start-device").textContent = deviceName() || "No microphone";
  $("rec-start").disabled = $("rec-start-chosen").disabled = !sel.options.length;
  if (active) renderRecCard(rec, tick);
  else levels.length = 0;
  $("rec-events").replaceChildren(...(rec ? rec.events.filter((e) => !["paused", "resumed"].includes(e.kind))
    .map((e) => el("li", { textContent: eventText(e) })) : []));
  $("rec-reports").replaceChildren(...r.reports.map(reportItem));
  // Faster updates for the clock and level meter while recording.
  const live = active && rec.state !== "stopped";
  if (live && !recTimer)
    recTimer = setInterval(() => { if (state.view === "meetings") renderRecorder(true).catch(() => {}); }, 300);
  if (!live && recTimer) { clearInterval(recTimer); recTimer = null; }
}

function wireRecorder() {
  const device = () => Number($("rec-device").value);
  // A changed device list means choosing again: reload it.
  const relist = (err) => { if (/changed/.test(String(err && err.message))) $("rec-device").replaceChildren(); throw err; };
  const start = (btn) => attempt(async () => {
    btn.disabled = true;           // one recording per click
    try { await call("start_recording", device(), deviceName()).catch(relist); }
    finally { btn.disabled = false; }
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
    if (open) $("rec-device").focus();
  });
  $("rec-device").addEventListener("change", () => {
    $("mic-name").textContent = $("rec-start-device").textContent = deviceName();
  });
  $("rec-test").addEventListener("click", () => attempt(async () => {
    $("rec-test-result").textContent = "Listening for 3 seconds…";
    const t = await call("test_microphone", device(), deviceName()).catch(relist);
    $("rec-test-result").textContent = t.heard
      ? `Sound heard (loudest ${t.peak_db} dB).`
      : `Very quiet (loudest ${t.peak_db} dB). Speak, or check the microphone and its volume.`;
  }));
  $("rec-pause").addEventListener("click", () => attempt(async () => { await call("pause_recording"); renderRecorder(); }));
  $("rec-resume").addEventListener("click", () => attempt(async () => { await call("resume_recording"); renderRecorder(); }));
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
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
    overlay.hidden = false;
    clearTimeout(dragTimer);
    dragTimer = setTimeout(hide, 400);
  });
  document.addEventListener("drop", () => { clearTimeout(dragTimer); hide(); });
}

async function refresh() {
  if (refreshing || !window.pywebview) return;
  refreshing = true;
  try {
    await renderStatus();
    await renderLists();                 // also keeps the sidebar count current
    if (state.view === "meetings") {
      await renderReminders();
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
}

function wire() {
  $("s-add-folder").addEventListener("click", () => attempt(async () => {
    const added = await call("add_folder_dialog", $("s-include-existing").checked);
    if (added) say("Folder added. It will be checked now.");
    renderSettings();
  }));
  $("s-scan").addEventListener("click", () => attempt(async () => { await call("scan_now"); say("Checking watched folders"); }));
  wireSetup();
  wireHandoff();
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
  $("m-retranscribe").addEventListener("click", () => confirmDialog({
    title: "Transcribe again with the current model?",
    body: "A new transcript is made from the audio. Your edits stay with the current version, " +
      "which is kept as an earlier version you can still open (read only); they are not copied " +
      "into the new transcript.",
    check: "I understand my edits stay with the earlier version.",
    limits: false,
    onConfirm: () => attempt(async () => {
      await call("retranscribe", state.mid);
      viewVersion(null);
      say("Transcribing again. The previous version and its edits are kept.");
    }),
  }));
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
      }),
    });
  });
  let searchTimer;
  $("m-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250); });
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
  $("s-battery").addEventListener("change", () => setSetting({ battery_override: $("s-battery").checked }));
  $("s-speakers").addEventListener("change", () => setSetting({ speakers: $("s-speakers").checked }));
  $("s-gpu").addEventListener("change", () => setSetting({ gpu_speakers: $("s-gpu").checked }));
  $("s-microphone").addEventListener("change", () => setSetting({ microphone: $("s-microphone").checked }));
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

window.workspace = { call, show, openMeeting, refresh, state, play, say, setStep };   // for the check tools

wire();
window.addEventListener("pywebviewready", async () => {
  // First run, or no usable speech model: start with Setup.
  let s = null;
  try { s = await call("setup_state"); } catch (err) { /* shown by refresh */ }
  if (s && (!s.onboarded || !s.speech_ready)) {
    suState.step = s.onboarded ? 2 : 1;
    show("setup");
    setStep(suState.step);
  } else show("meetings");
  setInterval(refresh, POLL_MS);
});
