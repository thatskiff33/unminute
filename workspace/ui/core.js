// Unminute UI (#20). All meeting text is rendered with textContent,
// never innerHTML, so markup in a transcript is shown as text, not executed.
// The page holds no file paths: recordings arrive through the native file
// dialog or a native drop, handled in Python.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
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
  lastRec: null,                 // the open recording at the latest poll (#462's choice)
  locked: null,                  // status().locked: the workspace opened locked (#111 C4b)
};

// -- bridge -----------------------------------------------------------------
// Calls return {ok, value} or {ok: false, error, kind}; they never throw in
// Python, so no stack trace (with file paths) reaches the page. kind (#416)
// says what a refusal is ("error" or "info"); an answer without a known kind
// counts as an error.
async function call(name, ...args) {
  const r = await window.pywebview.api[name](...args);
  if (!r || !r.ok) {
    const err = new Error((r && r.error) || "The request failed.");
    err.kind = r && r.kind === "info" ? "info" : "error";
    throw err;
  }
  return r.value;
}

// Short messages appear as a toast, styled by their kind, never by their
// wording (#416): "success" (a tick; clears after 3 s), "info" (the default)
// and "error" (an alert mark), which clear after 8 s; a sticky one (an import
// in progress) stays until the app replaces it. Errors from Import, Hand off
// and Settings stay beside their action instead (showError, below).
const MESSAGE_KINDS = ["success", "info", "error"];
// #status is a live region: a screen reader may read it again at each change,
// even to the same text, so the same message again leaves it alone (#404, as
// #346's putText) and only starts the timer again.
let toastTimer = null;
function say(message, { sticky = false, kind = "info" } = {}) {
  const t = $("status");
  clearTimeout(toastTimer);
  putText(t, message || "");
  if (!MESSAGE_KINDS.includes(kind)) kind = "info";
  t.classList.toggle("ok", !!message && kind === "success");
  t.classList.toggle("error", !!message && kind === "error");
  t.dataset.sticky = sticky && message ? "1" : "";
  if (message && !sticky)
    toastTimer = setTimeout(() => {
      if (state.statusLine) say(state.statusLine, { sticky: true });   // a lasting status returns
      else say("");
    }, kind === "success" ? 3000 : 8000);
}
const sayDone = (message) => say(message, { kind: "success" });
const sayError = (err) => say(String((err && err.message) || err), { kind: (err && err.kind) || "error" });

// -- errors beside their action (#416) ------------------------------------------
// On Import, Hand off and Settings (with Downloads and Setup), an error stays
// next to the control that caused it until it is dismissed or that part of the
// page next succeeds, instead of a toast that clears after 8 s. Its place is
// the nearest [data-errors] container: in its .error-slot if it has one, else
// at its end. One error per place, the latest. It is a role="alert", filled
// after it is placed, so it is announced once; the same text again is left
// alone (putText).
function errorHome(near) {
  const node = typeof near === "string" ? $(near) : near;
  return node && node.closest ? node.closest("[data-errors]") : null;
}
function showError(home, message, back = null) {
  if (!home) { say(message, { kind: "error" }); return; }
  const slot = home.querySelector(":scope .error-slot") || home;
  let box = slot.querySelector(":scope > .inline-error");
  if (!box) {
    const dismiss = el("button", { type: "button", class: "link-plain", textContent: "Dismiss",
      on: { click: () => {
        box.remove();
        const to = box.errorBack;                    // the control of the latest error here
        if (to && to.isConnected && !to.disabled) to.focus();   // not lost to the page
      } } });
    box = el("div", { class: "inline-error", role: "alert" }, icon("alert", 18),
             el("span", { class: "inline-error-text" }), dismiss);
    slot.append(box);
  }
  box.errorBack = back;
  putText(box.querySelector(".inline-error-text"), message);
}
function clearError(home) {
  const box = home && (home.querySelector(":scope .error-slot") || home).querySelector(":scope > .inline-error");
  if (box) box.remove();
}
function clearErrorsIn(view) { for (const box of $(view).querySelectorAll(".inline-error")) box.remove(); }

// Runs fn and shows what went wrong. near (an element or its id) puts an
// error beside its action; its place is found when the action starts, before
// a redraw can remove near, and a success there clears it. An "info" refusal
// is toasted wherever it came from.
async function attempt(fn, near = null) {
  const home = near ? errorHome(near) : null;
  const back = typeof near === "string" ? $(near) : near;
  try {
    const value = await fn();
    clearError(home);
    return value;
  } catch (err) {
    const message = String((err && err.message) || err);
    const kind = (err && err.kind) || "error";
    if (home && kind === "error") showError(home, message, back);
    else say(message, { kind });
  }
}

// An error from the poll is shown once, not at every poll (#404): a bridge call
// that keeps failing would otherwise be toasted, and may be read out, every 2
// or 10 s. Kept for each place in the poll, and forgotten when that place next
// succeeds, so a new or changed error, or the same one after a recovery, is
// shown at once.
const pollErrors = {};
function pollError(where, err) {
  const message = String((err && err.message) || err);
  if (pollErrors[where] === message) return;
  pollErrors[where] = message;
  say(message, { kind: (err && err.kind) || "error" });
}
function pollOk(where) { delete pollErrors[where]; }

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

