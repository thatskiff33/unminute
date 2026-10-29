// Unminute UI: live drafts, and the recording card's controls.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

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
  if (state.locked) { stopRecTimer(); return; }     // only the locked screen (#111 C4b, #346)
  if (tick && recReading) return;                   // the previous read is still on its way
  recReading = true;
  let r;
  try { r = await call("recording_status"); } finally { recReading = false; }
  if (state.locked) { stopRecTimer(); return; }     // locked while the read was on its way
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
    relistMics("rec-device", showRecMics).then(() => pollOk("mics"), (err) => pollError("mics", err));
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
  $("mic-computer").hidden = !r.computer_audio;
  $("rec-start").disabled = $("rec-start-chosen").disabled = !sel.options.length;
  state.lastRec = active ? rec : null;
  if (active) renderRecCard(rec, r.display, r.lid_override, r.lid_refused);
  else { stopViz(); $("rec-mic-row").hidden = true; }   // the choice goes with the card (#462)
  renderDraft(rec, r.draft).catch(() => {});
  $("rec-events").replaceChildren(...(rec ? rec.events.filter((e) => !["paused", "resumed"].includes(e.kind))
    .map((e) => el("li", { textContent: eventLine(e) })) : []));
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
  wireRecMic();
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
      try { await call("discard_recording"); sayDone("Recording discarded."); }
      finally { for (const id of busy) $(id).disabled = false; renderRecorder(); }
    }),
  }));
  $("rec-stop").addEventListener("click", () => attempt(async () => {
    state.recSaving = true;               // stopping can take a while: show Saving meanwhile
    $("rec-stop").disabled = true;
    renderRecorder().catch(() => {});
    try { await call("stop_recording"); }
    finally { state.recSaving = false; $("rec-stop").disabled = false; }
    sayDone("Recording saved. It is being imported.");
    renderRecorder();
  }));
}

// While files are dragged over the window, show where to let go. The drop
// itself is handled natively; dragover repeats while over the window, so the
// overlay hides shortly after it stops (dragged away, dropped or cancelled).
