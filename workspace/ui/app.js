// Meeting workspace UI (#20). All meeting text is rendered with textContent,
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
  view: "inbox", mid: null, detail: null, total: -1, signature: "",
  hits: [], hit: -1, editing: null, saveTimer: null,
};

// -- bridge -----------------------------------------------------------------
// Calls return {ok, value} or {ok: false, error}; they never throw in Python,
// so no stack trace (with file paths) reaches the page.
async function call(name, ...args) {
  const r = await window.pywebview.api[name](...args);
  if (!r || !r.ok) throw new Error((r && r.error) || "The request failed.");
  return r.value;
}

function say(message) { $("status").textContent = message || ""; }

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
function age(days) { return days === 0 ? "today" : days === 1 ? "1 day old" : `${days} days old`; }
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
function show(view) {
  state.view = view;
  for (const v of ["inbox", "active", "meeting", "settings"]) $(`view-${v}`).hidden = v !== view;
  for (const b of document.querySelectorAll(".nav"))
    b.setAttribute("aria-current", b.dataset.view === view ? "page" : "false");
  refresh();
}

async function openMeeting(mid) {
  stopEditing();
  state.mid = mid; state.total = -1; state.signature = ""; state.hits = []; state.hit = -1;
  $("m-search").value = ""; $("m-hits").textContent = "";
  $("transcript").replaceChildren();
  show("meeting");
}

// -- lists --------------------------------------------------------------------
function meetingRow(m) {
  const name = el("div", { class: "name" },
    el("button", { type: "button", class: "link", textContent: m.title,
                   on: { click: () => openMeeting(m.id) } }),
    m.held ? el("span", { class: "badge hold", textContent: "Held" }) : null,
    el("div", { class: "muted", textContent: `${age(m.age_days)} · Next: ${m.action}` }));
  const row = el("li", {}, name);
  if (m.stage === "inbox") {
    row.append(el("progress", { max: 100, value: m.progress, title: `${m.progress}% transcribed` }));
    const label = m.error ? "Needs attention" : m.paused ? "Paused"
      : m.busy === "transcribing" ? "Transcribing" : "Waiting";
    row.append(el("span", { class: m.error ? "error" : "muted", textContent: label }));
    row.append(m.paused || m.error
      ? el("button", { type: "button", textContent: m.error ? "Try again" : "Resume",
                       on: { click: () => attempt(() => call("resume", m.id).then(refresh)) } })
      : el("button", { type: "button", textContent: "Pause",
                       on: { click: () => attempt(() => call("pause", m.id).then(refresh)) } }));
  } else {
    row.append(el("span", { class: "badge", textContent: STAGES[m.stage] }));
  }
  return row;
}

async function renderLists() {
  const items = await call("meetings");
  const inbox = items.filter((m) => m.stage === "inbox");
  const active = items.filter((m) => m.stage !== "inbox");
  $("inbox-list").replaceChildren(...inbox.map(meetingRow));
  $("active-list").replaceChildren(...active.map(meetingRow));
  $("inbox-empty").hidden = inbox.length > 0;
  $("active-empty").hidden = active.length > 0;
}

async function renderStatus() {
  const s = await call("status");
  const parts = [];
  if (s.importing) parts.push(`Importing ${s.importing} recording${s.importing > 1 ? "s" : ""}`);
  if (s.message) parts.push(s.message);
  say(parts.join(" · "));
  $("import-errors").replaceChildren(...s.import_errors.map((e) => el("li", { textContent: e })));
}

async function renderReminders() {
  const r = await call("reminders");
  const box = $("reminders");
  const lines = [];
  for (const h of r.holds_due)
    lines.push(el("p", {},
      `Hold due for review: "${h.title}" (kept because: ${h.reason}). `,
      el("button", { type: "button", textContent: "Keep",
                     on: { click: () => attempt(() => call("keep_hold", h.id).then(refresh)) } }),
      " ",
      el("button", { type: "button", class: "link", textContent: "Open meeting",
                     on: { click: () => openMeeting(h.meeting) } })));
  for (const j of r.awaiting_confirmation)
    lines.push(el("p", {},
      `"${j.title}": a hold was released. The ${j.phase} cleanup is waiting for your confirmation. `,
      el("button", { type: "button", class: "link", textContent: "Open meeting",
                     on: { click: () => openMeeting(j.meeting) } })));
  box.replaceChildren(...lines);
  box.hidden = lines.length === 0;
}

// -- one meeting ---------------------------------------------------------------
function signatureOf(d) {
  return [d.progress, d.speakers && d.speakers.status, d.model, d.reviewed_at,
          d.audio_available].join("|");
}

async function renderMeeting() {
  if (!state.mid) return;
  let d;
  try { d = await call("detail", state.mid); }
  catch (err) { say(String(err.message || err)); show("active"); return; }
  state.detail = d;
  if (document.activeElement !== $("m-title")) $("m-title").value = d.title;
  const badges = [el("span", { class: "badge", textContent: STAGES[d.stage] })];
  if (d.holds.length) badges.push(el("span", { class: "badge hold", textContent: "Held" }));
  $("m-badges").replaceChildren(...badges);
  const bits = [`Imported ${fmtDate(d.created_at)}`];
  if (d.stage === "inbox") bits.push(`${d.progress}% transcribed${d.paused ? " (paused)" : ""}`);
  if (d.busy) bits.push(d.busy === "speakers" ? "identifying speakers…" : `${d.busy}…`);
  $("m-state").textContent = bits.join(" · ");
  $("m-error").hidden = !d.error; $("m-error").textContent = d.error || "";
  $("m-pause").hidden = d.stage !== "inbox" || d.paused || !!d.error;
  $("m-stop").hidden = $("m-pause").hidden;
  $("m-resume").hidden = !(d.paused || d.error) || !!d.reviewed_at;
  $("m-resume").textContent = d.error ? "Try again" : "Resume";
  $("m-undo").disabled = !d.can_undo;
  $("m-playback").textContent = d.audio_available ? "Select a time to play that passage."
    : "The audio was removed after the transcript review was confirmed, so playback and transcribing again are no longer available.";
  renderSteps(d);
  renderSpeakers(d);
  renderHolds(d);
  $("m-versions").textContent = `${d.model || "No model yet"} · ${d.edits} edited passage${d.edits === 1 ? "" : "s"}` +
    (d.older_versions.length ? ` · ${d.older_versions.length} earlier version(s) kept with their own edits` : "");
  $("m-retranscribe").disabled = !d.audio_available || !!d.reviewed_at || d.stage === "inbox";

  const sig = signatureOf(d);
  if (sig !== state.signature && !state.editing) {
    state.signature = sig;
    await renderTranscript();
  }
}

function step(text, done, ...extra) {
  return el("li", { class: done ? "done" : "" }, done ? `✓ ${text}` : text, ...extra);
}

function renderSteps(d) {
  const steps = [];
  const jobs = Object.fromEntries(d.cleanup.map((j) => [j.phase, j]));
  const jobNote = (phase) => {
    const j = jobs[phase];
    if (!j) return null;
    if (j.state === "awaiting_confirmation")
      return el("div", {}, el("span", { class: "muted", textContent: "A hold was released. " }),
        el("button", { type: "button", textContent: "Confirm cleanup",
                       on: { click: () => attempt(() => call("confirm_cleanup", j.id).then(refresh)) } }));
    if (j.state === "failed")
      return el("div", {}, el("span", { class: "error", textContent: `Cleanup failed: ${j.note} ` }),
        el("button", { type: "button", textContent: "Try again",
                       on: { click: () => attempt(() => call("retry_cleanup", j.id).then(refresh)) } }));
    return el("div", { class: "muted", textContent: j.note || "Cleanup pending…" });
  };
  steps.push(step("Transcribe", d.stage !== "inbox"));
  steps.push(step("Review the transcript against the audio, then confirm", !!d.reviewed_at,
    d.reviewed_at ? jobNote("audio")
      : el("div", {}, el("button", { type: "button", textContent: "Transcript reviewed…",
          disabled: d.stage === "inbox", on: { click: () => confirmReviewed(d) } }))));
  steps.push(step("Hand off the reviewed text and create the final document outside the app",
    !!d.final_saved_at, d.reviewed_at && !d.final_saved_at
      ? el("div", { class: "muted", textContent: "Copy and prompt tools arrive with the handoff feature (#22)." })
      : null));
  steps.push(step("Confirm the final document is reviewed and saved", !!d.final_saved_at,
    d.final_saved_at ? jobNote("transcript")
      : el("div", {}, el("button", { type: "button", textContent: "Final document saved…",
          disabled: !d.reviewed_at, on: { click: () => confirmFinal(d) } }))));
  $("steps").replaceChildren(...steps);
}

function renderSpeakers(d) {
  const s = d.speakers;
  let text = "Not identified yet.";
  if (s && s.status === "done") text = "Speakers identified. Labels are machine estimates; check them while reviewing.";
  else if (s && s.status === "failed") text = `Speaker identification did not work: ${s.error || "unknown reason"}. The transcript is still usable.`;
  else if (s && s.status === "interrupted") text = s.error || "Speaker identification was interrupted and will start again from the beginning.";
  else if (s && s.status === "running") text = "Identifying speakers…";
  $("m-speakers").textContent = text;
  $("m-rerun").disabled = !d.audio_available || d.stage === "inbox";
}

function renderHolds(d) {
  $("m-holds").replaceChildren(...d.holds.map((h) => el("li", {},
    el("div", { textContent: `Keeping ${SCOPES[h.scope]}: ${h.reason}` }),
    el("div", { class: "muted", textContent: `Since ${fmtDate(h.created_at)}${h.due_review ? " · due for review" : ""}` }),
    h.due_review ? el("button", { type: "button", textContent: "Keep",
      on: { click: () => attempt(() => call("keep_hold", h.id).then(refresh)) } }) : null,
    " ",
    el("button", { type: "button", textContent: "Release",
      title: "Releasing a hold deletes nothing by itself; cleanup still needs your confirmation.",
      on: { click: () => attempt(() => call("release_hold", h.id).then(refresh)) } }))));
}

// -- transcript ----------------------------------------------------------------
function transcriptRow(item, audio) {
  const t = el("button", { type: "button", class: "t", textContent: fmtTime(item.t),
    disabled: !audio, title: audio ? "Play this passage" : "Audio removed after review",
    on: { click: () => play(item.t, item.end) } });
  const text = el("span", { class: "text" + (item.edited ? " edited" : ""), textContent: item.text,
    title: "Select to edit", tabIndex: 0 });
  text.dataset.id = item.id;
  text.addEventListener("click", () => startEditing(text));
  text.addEventListener("keydown", (e) => { if (e.key === "Enter" && !text.isContentEditable) { e.preventDefault(); startEditing(text); } });
  const li = el("li", {}, t,
    item.who ? el("span", { class: "who", textContent: item.who + ":" }) : null,
    text,
    item.overlap ? el("span", { class: "overlap", textContent: "(overlapping speech)" }) : null);
  li.dataset.id = item.id;
  return li;
}

async function renderTranscript() {
  const list = $("transcript");
  const audio = state.detail && state.detail.audio_available;
  const scroll = document.scrollingElement.scrollTop;
  const rows = [];
  let start = 0, total = 1;
  while (start < total) {
    const page = await call("transcript", state.mid, start, PAGE);
    total = page.total;
    for (const item of page.items) rows.push(transcriptRow(item, audio));
    start += page.items.length;
    if (!page.items.length) break;
  }
  list.replaceChildren(...rows);
  state.total = total;
  document.scrollingElement.scrollTop = scroll;
  if ($("m-search").value) runSearch(false);
}

function startEditing(span) {
  const d = state.detail;
  if (!d || d.reviewed_at) { say("The transcript review is confirmed, so it can no longer be edited."); return; }
  if (state.editing === span) return;
  stopEditing();
  state.editing = span;
  span.contentEditable = "plaintext-only";
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

async function save(span) {
  if (!span) return;
  const id = Number(span.dataset.id);
  const r = await attempt(() => call("edit", state.mid, id, span.textContent));
  if (r) {
    span.classList.toggle("edited", r.edited);
    say("Saved");
    $("m-undo").disabled = false;
  }
}

function stopEditing() {
  const span = state.editing;
  if (!span) return;
  state.editing = null;
  clearTimeout(state.saveTimer);
  span.removeEventListener("input", scheduleSave);
  span.removeEventListener("keydown", editKeys);
  span.contentEditable = "false";
  save(span);
}

async function undo() {
  stopEditing();
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
  if (!q) { state.hits = []; $("m-hits").textContent = ""; return; }
  state.hits = await attempt(() => call("search", state.mid, q)) || [];
  const rows = $("transcript").children;
  for (const i of state.hits) if (rows[i]) rows[i].classList.add("hit");
  $("m-hits").textContent = state.hits.length ? `${state.hits.length} found` : "No matches";
  if (jump) { state.hit = -1; nextHit(1); }
}

function nextHit(dir) {
  if (!state.hits.length) return;
  const rows = $("transcript").children;
  if (state.hit >= 0 && rows[state.hits[state.hit]]) rows[state.hits[state.hit]].classList.remove("current");
  state.hit = (state.hit + dir + state.hits.length) % state.hits.length;
  const row = rows[state.hits[state.hit]];
  if (row) { row.classList.add("current"); row.scrollIntoView({ block: "center" }); }
  $("m-hits").textContent = `${state.hit + 1} of ${state.hits.length}`;
}

// -- playback ------------------------------------------------------------------
async function play(start, end) {
  const player = $("player");
  player.pause();
  const stop = Math.min(Math.max(end, start + 1), start + 30);
  const url = await attempt(() => call("clip", state.mid, start, stop));
  if (!url) return;
  player.src = url;
  await attempt(() => player.play());
}

// -- confirmations ---------------------------------------------------------------
function confirmDialog({ title, body, check, onConfirm }) {
  const dlg = $("confirm");
  $("c-title").textContent = title;
  $("c-body").textContent = body;
  $("c-limits").textContent = state.detail ? state.detail.limits : "";
  $("c-check-text").textContent = check;
  $("c-check").checked = false;
  $("c-ok").disabled = true;
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
    body: "All of this meeting's transcript working content will be removed from the workspace: text, edits, speakers and versions. Copying or exporting alone never does this.",
    check: "The final document is reviewed and saved outside this app.",
    onConfirm: () => attempt(() => call("confirm_final_saved", d.id).then(() => show("active"))),
  });
}

// -- settings ------------------------------------------------------------------
async function renderSettings() {
  const s = await call("settings");
  for (const r of document.querySelectorAll('input[name="mode"]')) r.checked = r.value === s.mode;
  $("s-battery").checked = s.battery_override;
  $("s-speakers").checked = s.speakers;
  const sel = $("s-model");
  if (document.activeElement !== sel) {
    sel.replaceChildren(...s.choices.map((c) => el("option", { value: c.name, textContent: c.label })));
    sel.value = s.model;
  }
  const receipts = await call("receipts");
  $("receipts").replaceChildren(...(receipts.length ? receipts.map((r) =>
    el("li", { textContent: `${fmtDate(r.completed_at)}: ${r.result}` }))
    : [el("li", { class: "muted", textContent: "None." })]));
}

function setSetting(changes) { attempt(() => call("set_settings", changes).then(renderSettings)); }

// -- refresh loop ----------------------------------------------------------------
let refreshing = false;
async function refresh() {
  if (refreshing || !window.pywebview) return;
  refreshing = true;
  try {
    await renderStatus();
    await renderReminders();
    if (state.view === "inbox" || state.view === "active") await renderLists();
    else if (state.view === "meeting") await renderMeeting();
    else if (state.view === "settings") await renderSettings();
  } catch (err) {
    say(String((err && err.message) || err));
  } finally {
    refreshing = false;
  }
}

function wire() {
  for (const b of document.querySelectorAll(".nav")) b.addEventListener("click", () => show(b.dataset.view));
  $("import").addEventListener("click", () => attempt(async () => {
    await call("clear_import_errors");
    const r = await call("import_dialog");
    if (r.accepted) say(`Importing ${r.accepted} recording${r.accepted > 1 ? "s" : ""}`);
    refresh();
  }));
  $("back").addEventListener("click", () => { stopEditing(); show(state.detail && state.detail.stage === "inbox" ? "inbox" : "active"); });
  $("m-title").addEventListener("change", () => attempt(() => call("rename", state.mid, $("m-title").value).then(refresh)));
  $("m-pause").addEventListener("click", () => attempt(() => call("pause", state.mid).then(refresh)));
  $("m-stop").addEventListener("click", () => attempt(() => call("stop_now", state.mid).then(refresh)));
  $("m-resume").addEventListener("click", () => attempt(() => call("resume", state.mid).then(refresh)));
  $("m-undo").addEventListener("click", undo);
  $("m-retranscribe").addEventListener("click", () => attempt(async () => {
    await call("retranscribe", state.mid);
    state.signature = "";
    say("Transcribing again. The previous version and its edits are kept.");
    refresh();
  }));
  $("m-rerun").addEventListener("click", () => attempt(async () => {
    const v = $("m-count").value.trim();
    await call("rerun_speakers", state.mid, v ? Number(v) : null);
    say("Identifying speakers again");
  }));
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
  $("s-model").addEventListener("change", () => setSetting({ model: $("s-model").value }));
  document.addEventListener("keydown", (e) => {
    if (state.view === "meeting" && (e.ctrlKey || e.metaKey) && e.key === "z" && !state.editing
        && !(e.target instanceof HTMLInputElement)) { e.preventDefault(); undo(); }
    if (state.view === "meeting" && (e.ctrlKey || e.metaKey) && e.key === "f") { e.preventDefault(); $("m-search").focus(); }
  });
}

window.workspace = { call, show, openMeeting, refresh, state };   // for the UI check tool

wire();
window.addEventListener("pywebviewready", () => { show("inbox"); setInterval(refresh, POLL_MS); });
