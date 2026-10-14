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
// The meeting view's two scrolling panes fill the window below the toolbar.
function fitMeeting() {
  if (state.view !== "meeting") return;
  const top = document.querySelector(".meeting-body").getBoundingClientRect().top + window.scrollY;
  document.documentElement.style.setProperty("--meeting-top", `${Math.round(top)}px`);
}
window.addEventListener("resize", fitMeeting);
// Anything above the panes changing height (a banner, a wrapped toolbar or
// search count) moves them: refit at once rather than at the next refresh.
const meetingTopObserver = new ResizeObserver(() => fitMeeting());
for (const el of document.querySelectorAll("body > header, body > .banner")) meetingTopObserver.observe(el);
for (let el = document.querySelector(".meeting-body").previousElementSibling; el; el = el.previousElementSibling) {
  meetingTopObserver.observe(el);
}

function show(view) {
  state.view = view;
  for (const v of ["inbox", "active", "meeting", "settings", "setup"]) $(`view-${v}`).hidden = v !== view;
  for (const b of document.querySelectorAll(".nav"))
    b.setAttribute("aria-current", b.dataset.view === view ? "page" : "false");
  refresh();
}

async function openMeeting(mid) {
  stopEditing();
  state.mid = mid; state.total = -1; state.signature = ""; state.hits = []; state.hit = -1;
  state.version = null;
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
  const discard = el("button", { type: "button", class: "link", textContent: "Discard…",
    title: "Delete a meeting you do not need (for example a test recording)",
    on: { click: () => attempt(async () => {
      const d = await call("detail", m.id);            // its own deletion limits
      confirmDialog({
        title: "Discard this meeting?",
        body: "Its audio, transcript, edits and speaker names are deleted from this workspace and cannot be recovered. Your original recording file is not deleted. For real meetings, use Transcript reviewed and then the final confirmation instead.",
        check: "Delete this meeting and everything in it.", limitsText: d.limits,
        onConfirm: () => attempt(() => call("discard_meeting", m.id).then(() => { say("Meeting discarded."); refresh(); })),
      });
    }) } });
  if (m.stage === "inbox") {
    row.append(el("progress", { max: 100, value: m.progress, title: `${m.progress}% transcribed` }));
    const label = m.error ? "Needs attention" : m.paused ? "Paused"
      : m.busy === "transcribing" ? "Transcribing" : "Waiting";
    row.append(el("span", { class: m.error ? "error" : "muted", textContent: label }));
    row.append(m.paused || m.error
      ? el("button", { type: "button", textContent: m.error ? "Try again" : "Resume",
                       on: { click: () => attempt(() => call("resume", m.id).then(refresh)) } })
      : el("button", { type: "button", textContent: "Pause",
                       title: "Stops at the next safe point; nothing is lost",
                       on: { click: () => attempt(() => call("pause", m.id).then(refresh)) } }));
    if (!m.paused && !m.error)
      row.append(el("button", { type: "button", textContent: "Stop now",
                                title: "Stops at once; the part in progress is redone later",
                                on: { click: () => attempt(() => call("stop_now", m.id).then(refresh)) } }));
    row.append(discard);
  } else {
    row.append(el("span", { class: "badge", textContent: STAGES[m.stage] }));
    // Speaker identification runs after transcription; it can be paused too.
    // It has no safe point inside, so Pause stops the pass and Resume restarts it.
    if (m.busy === "speakers" || m.busy === "rerun") {
      row.append(el("span", { class: "muted", textContent: "Identifying speakers" }));
      row.append(el("button", { type: "button", textContent: "Pause",
                                title: "Stops speaker identification now; Resume starts it again from the beginning (with the same number of people, if you gave one). The transcript is not affected.",
                                on: { click: () => attempt(() => call("pause", m.id).then(refresh)) } }));
    } else if (m.paused && m.stage === "review") {
      row.append(el("span", { class: "muted", textContent: "Paused" }));
      row.append(el("button", { type: "button", textContent: "Resume",
                                on: { click: () => attempt(() => call("resume", m.id).then(refresh)) } }));
    }
    if (m.stage === "review") row.append(discard);       // before review is confirmed only
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
  $("import-errors").replaceChildren(...s.import_errors.map((e) => el("li", {}, e, " ",
    el("button", { type: "button", class: "link", textContent: "Dismiss",
                   on: { click: () => attempt(() => call("clear_import_errors").then(refresh)) } }))));
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
  return [d.progress, d.speakers && d.speakers.status, d.speakers && d.speakers.started_at,
          d.model, d.reviewed_at,
          d.audio_available].join("|");
}

async function renderMeeting() {
  if (!state.mid) return;
  let d;
  try { d = await call("detail", state.mid); }
  catch (err) { say(String(err.message || err)); show("active"); return; }
  state.detail = d;
  requestAnimationFrame(fitMeeting);     // banners or errors above may have changed
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
  renderVersions(d);
  $("m-retranscribe").disabled = !d.audio_available || !!d.reviewed_at || d.stage === "inbox";

  const sig = signatureOf(d) + "|" + state.version;
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
        el("button", { type: "button", textContent: "Confirm cleanup…",
                       on: { click: () => confirmDialog({
                         title: `Confirm the ${phase} cleanup`,
                         body: "The hold on this material was released. Nothing was removed when it was released; this confirmation lets the cleanup go ahead.",
                         check: `Remove the ${phase === "audio" ? "audio" : "transcript working content"} now.`,
                         onConfirm: () => attempt(() => call("confirm_cleanup", j.id).then(refresh)),
                       }) } }));
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
      ? el("div", {}, el("button", { type: "button", class: "primary", textContent: "Hand off…",
                                     on: { click: openHandoff } }))
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
  if (s && s.status === "done" && s.device) {
    // Where it actually ran, including an honest fallback note.
    if (s.device.startsWith("cuda")) text += " Ran on the graphics card.";
    else if (s.device.includes("graphics card not used")) text += " Ran on the processor: the graphics card could not be used this time.";
  }
  $("m-speakers").textContent = text;
  $("m-names").disabled = !canName();
  $("m-undo-speakers").disabled = !canName();
  $("m-rerun").disabled = !d.audio_available || d.stage === "inbox" || !!d.reviewed_at
    || !!d.busy || state.version !== null;
}

function renderVersions(d) {
  const viewing = state.version !== null;
  const current = `Current: ${d.model || "no model yet"} · ${d.edits} edited passage${d.edits === 1 ? "" : "s"}`;
  const items = [el("li", {}, viewing
    ? el("button", { type: "button", class: "link", textContent: current,
                     on: { click: () => viewVersion(null) } })
    : el("strong", { textContent: current }))];
  for (const v of d.older_versions) {
    const label = `Earlier: ${v.model} (${fmtDate(v.created_at)}) · ${v.edits} edited passage${v.edits === 1 ? "" : "s"}`;
    items.push(el("li", {}, state.version === v.index ? el("strong", { textContent: label })
      : el("button", { type: "button", class: "link", textContent: label,
                       on: { click: () => viewVersion(v.index) } })));
  }
  $("m-versions").replaceChildren(...items);
  $("m-viewing").hidden = !viewing;
}

function viewVersion(index) {
  stopEditing();
  state.version = index;
  state.signature = "";
  $("m-search").value = ""; state.hits = []; $("m-hits").textContent = "";
  renderMeeting();
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
    item.who || canName() ? whoButton(item) : null,
    text,
    item.overlap ? el("span", { class: "overlap", textContent: "(overlapping speech)" }) : null);
  li.dataset.id = item.id;
  return li;
}

// -- speaker names (#21) ---------------------------------------------------------
function canName() {
  const d = state.detail;
  return d && !d.reviewed_at && state.version === null && d.speakers && d.speakers.status === "done";
}

function whoButton(item) {
  const b = el("button", { type: "button", class: "who" + (item.who ? "" : " muted"),
                           textContent: (item.who || "No speaker") + ":",
                           title: "Change the speaker of this passage" });
  b.addEventListener("click", () => reassign(b, item));
  return b;
}

// Reassigning needs an explicit Move: arrow keys on a select fire "change"
// on every step, which must not move the passage.
async function reassign(button, item) {
  if (!canName()) { say("Speakers can be changed only before the review is confirmed."); return; }
  const list = await attempt(() => call("speakers", state.mid));
  if (!list) return;
  const sel = el("select", { "aria-label": "Speaker for this passage" },
    ...list.speakers.map((s) => el("option", { value: s.label, textContent: s.name,
                                                 selected: s.label === item.label })),
    el("option", { value: "new", textContent: "Someone else (new speaker)" }));
  const box = el("span", { class: "reassign" }, sel);
  const restore = () => { if (box.isConnected) box.replaceWith(button); };
  const move = el("button", { type: "button", textContent: "Move", on: { click: () => attempt(async () => {
    await call("assign_speaker", state.mid, item.id, sel.value);
    restore();
    state.signature = "";
    say("Speaker changed. Undo speaker change reverses it.");
    renderMeeting();
  }) } });
  const cancel = el("button", { type: "button", textContent: "Cancel", on: { click: restore } });
  box.append(" ", move, " ", cancel);
  box.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); restore(); button.focus(); }
    if (e.key === "Enter" && e.target === sel) { e.preventDefault(); move.click(); }
  });
  button.replaceWith(box);
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
  $("n-list").replaceChildren(...data.speakers.map((sp) => speakerBlock(sp, data.speakers)));
}

function speakerBlock(sp, all) {
  const name = el("input", { value: sp.name, maxLength: 80, "aria-label": `Name for ${sp.name}` });
  // Renaming does not rebuild the panel, so focus and typing elsewhere are kept.
  name.addEventListener("change", () => attempt(async () => {
    await call("rename_speaker", state.mid, sp.label, name.value);
    say("Name saved");
    afterNamesChange(false);
  }));
  const ex = sp.examples;
  const examples = el("ul", { class: "examples" }, ...ex.items.map((it) =>
    el("li", {}, el("button", { type: "button", textContent: `▶ ${fmtTime(it.start)}`,
                                title: "Play exactly this passage",
                                on: { click: () => play(it.start, it.end, true) } }),
       el("span", { textContent: it.text }))));
  const page = (offset) => () => { namesState.offsets[sp.label] = offset; renderNames(); };
  const more = ex.total > sp.offset + 3
    ? el("button", { type: "button", textContent: "More examples", on: { click: page(sp.offset + 3) } })
    : (sp.offset ? el("button", { type: "button", textContent: "First examples", on: { click: page(0) } }) : null);
  const others = all.filter((o) => o.label !== sp.label);
  const into = el("select", { "aria-label": `Merge ${sp.name} into` },
    ...others.map((o) => el("option", { value: o.label, textContent: o.name })));
  const merge = others.length ? el("div", { class: "row" }, "Same person as ", into,
    el("button", { type: "button", textContent: "Merge",
                   on: { click: () => attempt(async () => {
                     await call("merge_speakers", state.mid, sp.label, into.value);
                     afterNamesChange(true);
                   }) } })) : null;
  return el("section", { class: "speaker" },
    el("div", { class: "row" }, name,
       el("span", { class: "muted", textContent: `${sp.passages} passage${sp.passages === 1 ? "" : "s"}` })),
    examples, more, merge);
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
  const rows = [];
  const mid = state.mid, version = state.version;
  let start = 0, total = 1;
  while (start < total) {
    const page = await call("transcript", mid, start, PAGE, version);
    total = page.total;
    for (const item of page.items) rows.push(transcriptRow(item, audio));
    start += page.items.length;
    if (!page.items.length) break;
  }
  // The user may have started editing, or switched meeting or version, while
  // this was loading: never replace a passage being edited; render later.
  if (state.editing || state.mid !== mid || state.version !== version) {
    state.signature = "";
    return;
  }
  list.replaceChildren(...rows);
  state.total = total;
  document.scrollingElement.scrollTop = scroll;
  document.querySelector(".transcript-wrap").scrollTop = paneScroll;
  if ($("m-search").value) runSearch(false);
}

function startEditing(span) {
  const d = state.detail;
  if (!d || d.reviewed_at) { say("The transcript review is confirmed, so it can no longer be edited."); return; }
  if (state.version !== null) { say("Earlier versions are read only. Switch to the current version to edit."); return; }
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
  if (!q) { state.hits = []; $("m-hits").textContent = ""; return; }
  state.hits = await attempt(() => call("search", state.mid, q, state.version)) || [];
  for (const id of state.hits) { const row = rowFor(id); if (row) row.classList.add("hit"); }
  $("m-hits").textContent = state.hits.length ? `${state.hits.length} found` : "No matches";
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
// exact: play precisely [start, end] (speaker examples must stop at their
// bounds); otherwise at least a second, at most 30 s, from a passage start.
async function play(start, end, exact = false) {
  const player = $("player");
  player.pause();
  const stop = exact ? Math.max(end, start + 0.05) : Math.min(Math.max(end, start + 1), start + 30);
  const url = await attempt(() => call("clip", state.mid, start, stop));
  if (!url) return;
  player.src = url;
  await attempt(() => player.play());
}

// -- confirmations ---------------------------------------------------------------
function confirmDialog({ title, body, check, onConfirm, limits = true, limitsText = null }) {
  const dlg = $("confirm");
  $("c-title").textContent = title;
  $("c-body").textContent = body;
  $("c-limits").textContent = limitsText || (limits && state.detail ? state.detail.limits : "");
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
    body: "All of this meeting's transcript working content will be removed from the workspace: text, edits, speakers, handoff options and versions. Copying or exporting alone never does this. " +
      "The app does not clear your clipboard or delete Markdown files you saved: copy something else to replace the clipboard (the app's copies are kept out of Windows clipboard history, but check Win+V if you copied the text another way), and delete saved handoff files when you no longer need them.",
    check: "The final document is reviewed and saved outside this app.",
    onConfirm: () => attempt(() => call("confirm_final_saved", d.id).then(() => show("active"))),
  });
}

// -- handoff (#22) ------------------------------------------------------------------
// The preview is exactly what Copy or Save produces. Nothing is sent anywhere
// by the app: the user pastes or attaches it in their approved assistant.
const handoffState = { template: "minutes", templates: [], preview: null, generation: 0,
                       dirty: false, show: "combined" };

async function openHandoff() {
  handoffState.templates = await attempt(() => call("handoff_templates")) || [];
  if (!handoffState.templates.some((t) => t.id === handoffState.template))
    handoffState.template = "minutes";
  renderTemplateSelect();
  $("ho-editor").hidden = true;
  await renderHandoffOptions();
  await renderHandoffPreview();
  $("handoff").showModal();
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
  try { p = await call("handoff_preview", state.mid, handoffState.template); }
  catch (err) {
    if (generation !== handoffState.generation) return;
    $("ho-preview").textContent = "";
    $("ho-status").textContent = String(err.message || err);
    return;
  }
  if (generation !== handoffState.generation) return;
  handoffState.preview = p;
  setHandoffButtons();
  $("ho-preview").textContent = p[handoffState.show];
  const size = `${p.chars.toLocaleString()} characters`;
  const parts = [`Template: ${p.template.title}, version ${p.template.version}`, size];
  if (p.omitted) parts.push(`${p.omitted} passage${p.omitted === 1 ? "" : "s"} left out`);
  if (p.redactions) parts.push(`${p.redactions} redaction${p.redactions === 1 ? "" : "s"}`);
  $("ho-info").textContent = parts.join(" · ");
  $("ho-long").hidden = !p.long;
}

async function renderHandoffOptions() {
  const p = await attempt(() => call("handoff_preview", state.mid, handoffState.template));
  if (!p) return;
  handoffState.dirty = false;
  $("ho-redactions").value = p.options.redactions.join("\n");
  const omitted = new Set(p.options.omitted);
  const page = await attempt(() => call("transcript", state.mid, 0, 2000));
  const rows = [];
  let start = 0, total = page ? page.total : 0, items = page ? page.items : [];
  while (true) {
    for (const it of items)
      rows.push(el("li", {}, el("label", {},
        el("input", { type: "checkbox", value: it.id, checked: omitted.has(it.id) }),
        ` [${fmtTime(it.t)}] ${it.who ? it.who + ": " : ""}${it.text}`)));
    start += items.length;
    if (start >= total || !items.length) break;
    const next = await attempt(() => call("transcript", state.mid, start, 2000));
    items = next ? next.items : [];
  }
  $("ho-passages").replaceChildren(...rows);
}

async function applyHandoffOptions() {
  const omitted = [...document.querySelectorAll("#ho-passages input:checked")].map((i) => Number(i.value));
  const redactions = $("ho-redactions").value.split("\n").map((s) => s.trim()).filter(Boolean);
  await attempt(async () => {
    await call("set_handoff_options", state.mid, omitted, redactions);
    handoffState.dirty = false;
    say("Handoff options saved. The transcript itself is unchanged.");
    await renderHandoffPreview();
  });
}

async function copyHandoff(kind) {
  let r;
  const p = handoffState.preview;
  if (!p || handoffState.dirty) return;
  try { r = await call("copy_handoff", state.mid, handoffState.template, kind, p.digests[kind]); }
  catch (err) { $("ho-status").textContent = String(err.message || err); return; }
  $("ho-status").textContent = kind === "combined"
    ? `Copied the prompt and transcript (${r.chars.toLocaleString()} characters). Paste it into your organisation's approved AI assistant.` +
      (r.long ? " It is long: if the assistant cuts it off, use Save Markdown and Copy prompt only instead." : "")
    : "Copied the prompt only. Attach the saved Markdown file in your approved AI assistant, then paste the prompt.";
}

async function saveHandoff() {
  let r;
  const p = handoffState.preview;
  if (!p || handoffState.dirty) return;
  try { r = await call("save_handoff", state.mid, handoffState.template, p.digests.markdown); }
  catch (err) { $("ho-status").textContent = String(err.message || err); return; }
  if (r) $("ho-status").textContent = `Saved ${r.name}. The app keeps no record of this file; file or delete it yourself when you no longer need it.`;
}

function editTemplate(isNew) {
  const t = handoffState.templates.find((x) => x.id === handoffState.template);
  if (!t && !isNew) return;
  $("ho-editor").hidden = false;
  $("ho-editor").dataset.id = isNew ? "" : t.id;
  $("ho-t-title").value = isNew ? "" : t.title;
  $("ho-t-prompt").value = isNew ? (t ? t.prompt : "") : t.prompt;
  $("ho-t-reset").textContent = !isNew && t.builtin ? "Restore original wording" : "Delete template";
  $("ho-t-reset").hidden = isNew || (t.builtin && !t.edited);
}

async function saveTemplate() {
  const id = $("ho-editor").dataset.id || null;
  const saved = await attempt(() => call("save_template", id, $("ho-t-title").value, $("ho-t-prompt").value));
  if (!saved) return;
  handoffState.template = saved;
  handoffState.templates = await attempt(() => call("handoff_templates")) || handoffState.templates;
  renderTemplateSelect();
  $("ho-editor").hidden = true;
  say("Template saved as a new version");
  renderHandoffPreview();
}

async function resetTemplate() {
  await attempt(() => call("reset_template", handoffState.template));
  handoffState.templates = await attempt(() => call("handoff_templates")) || handoffState.templates;
  if (!handoffState.templates.some((t) => t.id === handoffState.template)) handoffState.template = "minutes";
  renderTemplateSelect();
  $("ho-editor").hidden = true;
  renderHandoffPreview();
}

function wireHandoff() {
  $("ho-template").addEventListener("change", () => { handoffState.template = $("ho-template").value; $("ho-editor").hidden = true; renderHandoffPreview(); });
  $("ho-edit").addEventListener("click", () => editTemplate(false));
  $("ho-new").addEventListener("click", () => editTemplate(true));
  $("ho-t-save").addEventListener("click", saveTemplate);
  $("ho-t-cancel").addEventListener("click", () => { $("ho-editor").hidden = true; });
  $("ho-t-reset").addEventListener("click", resetTemplate);
  $("ho-apply").addEventListener("click", applyHandoffOptions);
  const markDirty = () => { handoffState.dirty = true; setHandoffButtons(); };
  $("ho-redactions").addEventListener("input", markDirty);
  $("ho-passages").addEventListener("change", markDirty);
  $("ho-show").addEventListener("change", () => {
    handoffState.show = $("ho-show").value;
    if (handoffState.preview) $("ho-preview").textContent = handoffState.preview[handoffState.show];
  });
  $("ho-copy").addEventListener("click", () => copyHandoff("combined"));
  $("ho-copy-prompt").addEventListener("click", () => copyHandoff("prompt_only"));
  $("ho-save").addEventListener("click", saveHandoff);
  $("ho-close").addEventListener("click", () => $("handoff").close());
}

// -- setup (#19) ----------------------------------------------------------------------
// The token is typed into a password field and passed straight to Python;
// the page never receives it back (only whether one is remembered).
async function renderSetup() {
  const s = await call("setup_state");
  state.setup = s;
  $("setup-cards").replaceChildren(...s.cards.map((c) => {
    const dl = c.download;
    const running = dl && dl.state === "running";
    const busy = s.cards.some((x) => x.download && x.download.state === "running");
    const action = running
      ? el("button", { type: "button", textContent: "Cancel",
                       on: { click: () => attempt(() => call("cancel_download").then(renderSetup)) } })
      : c.state === "ready"
        ? el("button", { type: "button", textContent: "Check every file", disabled: busy,
                         title: "Compare every file with its published checksum",
                         on: { click: () => attempt(() => call("verify_model", c.name).then(renderSetup)) } })
        : c.needs_token && c.state === "missing"
          ? el("span", { class: "muted", textContent: "Download it with the steps below." })
          : el("button", { type: "button", class: c.recommended ? "primary" : "",
                           textContent: c.state === "missing" ? `Download (${c.size_gb} GB)` : "Repair",
                           disabled: busy, on: { click: () => startDownload(c.name) } });
    // Remove a downloaded model from this computer (it can be downloaded again).
    const remove = c.removable && !running
      ? el("button", { type: "button", class: "link", textContent: "Remove", disabled: busy,
                       on: { click: () => confirmDialog({
                         title: `Remove ${c.title}?`,
                         body: `This deletes it from this computer, including any damaged or unfinished copies. Meetings already transcribed keep their text. You can download it again from Setup.${c.shared_with_earlier_app ? " If the earlier Whisper Transcriber app is installed, it uses the same model files." : ""}`,
                         check: "Remove it from this computer.", limits: false,
                         onConfirm: () => attempt(async () => {
                           const gb = await call("remove_model", c.name);
                           say(`Removed. About ${gb} GB freed.`);
                           renderSetup();
                         }) }) } })
      : null;
    return el("li", { class: "card" },
      el("h2", { textContent: c.title }),
      el("p", { textContent: c.about }),
      el("p", { class: c.state === "ready" ? "ok" : c.state === "invalid" ? "error" : "muted",
                textContent: running ? dl.message : (dl && dl.state === "failed" ? dl.message : c.status) }),
      el("p", { class: "muted", textContent: `${c.size_gb} GB · licence ${c.licence} · ${c.source} · revision ${c.revision}` }),
      el("div", { class: "row" }, action, remove));
  }));
  const speaker = s.cards.find((c) => c.role === "speakers");
  const speakerRunning = speaker.download && speaker.download.state === "running";
  $("speaker-setup").hidden = speaker.state === "ready";
  $("s-download-speakers").disabled = speakerRunning;
  $("s-use-remembered").hidden = !s.token_remembered || speaker.state === "ready";
  $("s-use-earlier").hidden = !s.earlier_token || s.token_remembered || speaker.state === "ready";
  $("s-remove-token").hidden = !s.token_remembered;
  $("s-token-note").textContent = s.token_remembered
    ? "A Hugging Face token is remembered in Windows Credential Manager. Removing it does not affect models already downloaded."
    : "";
  if (!state.setupChoiceTouched) $("s-speakers-on").checked = s.onboarded ? s.speakers : true;
  $("s-finish").disabled = !s.speech_ready;
  $("s-finish-note").textContent = s.speech_ready
    ? (speaker.state === "ready" ? "Everything is ready." : "You can add speaker identification later from Settings.")
    : "Download a speech model first (Parakeet v2 is recommended).";
}

function startDownload(name, extra = {}) {
  return attempt(async () => {
    await call("download_model", name, extra.token || null, extra.remember !== false, !!extra.earlier);
    say("Download started");
    renderSetup();
  });
}

function wireSetup() {
  for (const b of document.querySelectorAll("[data-help]"))
    b.addEventListener("click", () => attempt(() => call("open_help", b.dataset.help)));
  const speakerName = () => state.setup.cards.find((c) => c.role === "speakers").name;
  $("s-download-speakers").addEventListener("click", () => {
    const token = $("s-token").value.trim();
    if (!token) { say("Paste your Hugging Face token first."); return; }
    $("s-token").value = "";                         // never kept in the page
    startDownload(speakerName(), { token, remember: $("s-remember").checked });
  });
  $("s-use-remembered").addEventListener("click", () => startDownload(speakerName()));
  $("s-use-earlier").addEventListener("click", () =>
    startDownload(speakerName(), { earlier: true, remember: $("s-remember").checked }));
  $("s-remove-token").addEventListener("click", () => attempt(async () => {
    await call("remove_token");
    say("Token removed. Downloaded models keep working.");
    renderSetup();
  }));
  $("s-speakers-on").addEventListener("change", () => { state.setupChoiceTouched = true; });
  $("s-finish").addEventListener("click", () => attempt(async () => {
    await call("finish_setup", $("s-speakers-on").checked);
    show("inbox");
  }));
  $("s-skip-speakers").addEventListener("click", () => attempt(async () => {
    if (!state.setup.speech_ready) { say("Download a speech model first."); return; }
    await call("finish_setup", false);
    show("inbox");
  }));
}

// -- settings ------------------------------------------------------------------
async function renderSettings() {
  const s = await call("settings");
  for (const r of document.querySelectorAll('input[name="mode"]')) r.checked = r.value === s.mode;
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
        ? "The graphics card component needs repair in Setup; until then speaker identification runs on the processor."
        : `${s.gpu.card} found. Download "Graphics card acceleration" in Setup to use it.`)
      : s.mode !== "faster" && s.gpu_speakers
        ? "Used only in Faster mode (Processing, above)."
        : `${s.gpu.card}. Transcription always runs on the processor.`;
  $("s-microphone").checked = !!s.microphone;
  const sel = $("s-model");
  if (document.activeElement !== sel) {
    sel.replaceChildren(...s.choices.map((c) => el("option", { value: c.name, textContent: c.label })));
    sel.value = s.model;
  }
  const wf = await call("watched_folders");
  $("s-folders").replaceChildren(...(wf.folders.length ? wf.folders.map((f) => el("li", {},
    el("div", { textContent: f.path }),
    el("div", { class: f.error ? "error" : "muted", textContent: f.error ||
      [f.active ? "Watching" : "Paused",
       f.last_scan_at ? `checked ${fmtDate(f.last_scan_at)}` : "not checked yet",
       f.imported ? `${f.imported} imported` : null,
       f.waiting ? `${f.waiting} waiting until fully copied` : null,
       f.cloud ? `${f.cloud} in cloud storage, not on this computer yet` : null,
       f.duplicates ? `${f.duplicates} already imported` : null,
       f.unreadable ? `${f.unreadable} could not be read (no permission)` : null,
       f.failed ? `${f.failed} could not be imported (damaged or unsupported)` : null].filter(Boolean).join(" · ") }),
    el("label", {}, el("input", { type: "checkbox", checked: f.fetch_cloud,
      on: { change: (e) => attempt(() => call("set_folder_authority", f.id, null, e.target.checked).then(renderSettings)) } }),
      " Download recordings that are only in cloud storage (OneDrive and similar)"),
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
      " Delete originals from this folder after review (needs confirmation)"),
    el("button", { type: "button", textContent: f.active ? "Pause" : "Resume",
                   on: { click: () => attempt(() => call("set_folder_active", f.id, !f.active).then(renderSettings)) } }),
    " ",
    el("button", { type: "button", textContent: "Stop watching",
                   on: { click: () => confirmDialog({
                     title: "Stop watching this folder?",
                     body: "Recordings already imported stay in the workspace. New recordings in this folder will no longer be imported, and its deletion permission is removed.",
                     check: "Stop watching the folder.", limits: false,
                     onConfirm: () => attempt(() => call("remove_folder", f.id).then(renderSettings)) }) } })))
    : [el("li", { class: "muted", textContent: "No folders are watched." })]));
  const dep = s.deployment || {};
  $("s-deployment").textContent = dep.mode === "managed"
    ? `Version ${dep.version}. Updates are managed by your IT team.`
    : dep.mode === "self-service"
      ? `Version ${dep.version}. To update, close the app, then run install.cmd from the new package. Updates that change how the workspace is stored wait until the workspace is empty; the list below shows what would make one wait.`
      : "Running from source (development).";
  $("s-update-blockers").replaceChildren(...(s.update_blockers.length
    ? s.update_blockers.map((b) => el("li", { textContent: b }))
    : [el("li", { class: "muted", textContent: "Nothing would hold up an update." })]));
  const receipts = await call("receipts");
  $("receipts").replaceChildren(...(receipts.length ? receipts.map((r) =>
    el("li", { textContent: `${fmtDate(r.completed_at)}: ${r.result}` }))
    : [el("li", { class: "muted", textContent: "None." })]));
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

async function renderRecorder() {
  const r = await call("recording_status");
  $("rec-panel").hidden = !r.enabled;
  if (!r.enabled) return;
  const sel = $("rec-device");
  if (!sel.options.length) {
    const devices = await call("microphone_devices");
    sel.replaceChildren(...devices.map((d) => el("option", { value: String(d.index), "data-name": d.name,
      textContent: d.name + (d.default ? " (default)" : "") })));
    const def = devices.find((d) => d.default);
    if (def) sel.value = String(def.index);
    if (!devices.length) $("rec-state").textContent = "No microphone found.";
  }
  const rec = r.recording;
  const active = !!rec && (rec.state === "recording" || rec.state === "paused");
  $("rec-start").hidden = active;
  $("rec-pause").hidden = !active || rec.state === "paused";
  $("rec-resume").hidden = !active || rec.state !== "paused";
  $("rec-stop").hidden = !active;
  sel.disabled = $("rec-test").disabled = active;
  $("rec-level").value = active ? Math.max(-60, rec.level_db) : -60;
  $("rec-state").textContent = rec ? `${REC_TEXT[rec.state] || rec.state} ${fmtSeconds(rec.seconds)}` : "";
  $("rec-events").replaceChildren(...(rec ? rec.events.filter((e) => !["paused", "resumed"].includes(e.kind))
    .map((e) => el("li", { textContent: eventText(e) })) : []));
  $("rec-reports").replaceChildren(...r.reports.map((rep) => el("li", {},
    el("span", { textContent: `${rep.title || "Recording"}: ${fmtSeconds(rep.seconds)} saved` +
      (rep.note ? `. ${rep.note}` : "") + (rep.events.length ? ". " + rep.events.map(eventText).join("; ") : "") }),
    rep.pending ? el("button", { type: "button", class: "link", textContent: "Try again",
                    on: { click: () => attempt(() => call("retry_recording_import", rep.id).then(renderRecorder)) } }) : null,
    rep.pending ? " " : null,
    rep.pending ? el("button", { type: "button", class: "link", textContent: "Delete recording",
                    on: { click: () => confirmDialog({ title: "Delete this recording?",
                      body: "The saved audio of this recording is deleted from this computer. It cannot be recovered.",
                      check: "Delete the recording.", limits: false,
                      onConfirm: () => attempt(() => call("delete_pending_recording", rep.id).then(renderRecorder)) }) } }) : null,
    " ", el("button", { type: "button", class: "link", textContent: "Dismiss",
                        on: { click: () => attempt(() => call("discard_recording_report", rep.id).then(renderRecorder)) } }))));
  // Faster updates for the level meter while recording.
  if (active && !recTimer) recTimer = setInterval(() => { if (state.view === "inbox") renderRecorder().catch(() => {}); }, 300);
  if (!active && recTimer) { clearInterval(recTimer); recTimer = null; }
}

function wireRecorder() {
  const device = () => Number($("rec-device").value);
  const deviceName = () => $("rec-device").selectedOptions[0]?.dataset.name || "";
  // A changed device list means choosing again: reload it.
  const relist = (err) => { if (/changed/.test(String(err && err.message))) $("rec-device").replaceChildren(); throw err; };
  $("rec-test").addEventListener("click", () => attempt(async () => {
    $("rec-test-result").textContent = "Listening for 3 seconds…";
    const t = await call("test_microphone", device(), deviceName()).catch(relist);
    $("rec-test-result").textContent = t.heard
      ? `Sound heard (loudest ${t.peak_db} dB).`
      : `Very quiet (loudest ${t.peak_db} dB). Speak, or check the microphone and its volume.`;
  }));
  $("rec-start").addEventListener("click", () => attempt(async () => {
    $("rec-start").disabled = true;           // one recording per click
    try { await call("start_recording", device(), deviceName()).catch(relist); }
    finally { $("rec-start").disabled = false; }
    renderRecorder();
  }));
  $("rec-pause").addEventListener("click", () => attempt(async () => { await call("pause_recording"); renderRecorder(); }));
  $("rec-resume").addEventListener("click", () => attempt(async () => { await call("resume_recording"); renderRecorder(); }));
  $("rec-stop").addEventListener("click", () => attempt(async () => {
    await call("stop_recording"); say("Recording saved. It is being imported."); renderRecorder();
  }));
}

async function refresh() {
  if (refreshing || !window.pywebview) return;
  refreshing = true;
  try {
    await renderStatus();
    await renderReminders();
    if (state.view === "inbox" || state.view === "active") await renderLists();
    if (state.view === "inbox") await renderRecorder();
    else if (state.view === "meeting") await renderMeeting();
    else if (state.view === "settings") await renderSettings();
    else if (state.view === "setup") await renderSetup();
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
  $("back").addEventListener("click", () => { stopEditing(); show(state.detail && state.detail.stage === "inbox" ? "inbox" : "active"); });
  $("m-title").addEventListener("change", () => attempt(() => call("rename", state.mid, $("m-title").value).then(refresh)));
  $("m-pause").addEventListener("click", () => attempt(() => call("pause", state.mid).then(refresh)));
  $("m-stop").addEventListener("click", () => attempt(() => call("stop_now", state.mid).then(refresh)));
  $("m-resume").addEventListener("click", () => attempt(() => call("resume", state.mid).then(refresh)));
  $("m-undo").addEventListener("click", undo);
  $("m-names").addEventListener("click", openNames);
  $("n-close").addEventListener("click", () => { $("player").pause(); $("names").close(); });
  $("names").addEventListener("close", () => $("player").pause());
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
    if (state.view === "meeting" && (e.ctrlKey || e.metaKey) && e.key === "z" && !state.editing
        && !(e.target instanceof HTMLInputElement)) { e.preventDefault(); undo(); }
    if (state.view === "meeting" && (e.ctrlKey || e.metaKey) && e.key === "f") { e.preventDefault(); $("m-search").focus(); }
    // Keyboard zoom (#27): the browser's own Ctrl+plus/minus/0 keys are off in this window.
    if ((e.ctrlKey || e.metaKey) && !e.altKey) {
      const dir = ["+", "="].includes(e.key) ? "in" : ["-", "_"].includes(e.key) ? "out" : e.key === "0" ? "reset" : null;
      if (dir) { e.preventDefault(); attempt(() => call("zoom", dir)); }
    }
  });
}

window.workspace = { call, show, openMeeting, refresh, state };   // for the UI check tool

wire();
window.addEventListener("pywebviewready", async () => {
  // First run, or no usable speech model: start with Setup.
  let s = null;
  try { s = await call("setup_state"); } catch (err) { /* shown by refresh */ }
  show(s && (!s.onboarded || !s.speech_ready) ? "setup" : "inbox");
  setInterval(refresh, POLL_MS);
});
