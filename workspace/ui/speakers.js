// Unminute UI: speaker names.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

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
    sayDone("Speaker changed. Undo speaker change reverses it.");
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
  if (kind) sayDone("Speaker change undone"); else say("No speaker change to undo");
  afterNamesChange(true);
}

// focus: where focus goes once the panel is rebuilt, as {label, part}: that
// speaker's name box after Use 'You', or their paging button after More
// examples or First examples. The rebuild replaces the button that was
// pressed, so without this focus would fall to the page (#461, #475).
const namesState = { offsets: {}, generation: 0, focus: null };

async function openNames() {
  if (!canName()) { say("Speakers can be named once they are identified, and before the review is confirmed."); return; }
  namesState.offsets = {};
  namesState.focus = null;
  await renderNames();
  $("names").showModal();
}

// One call returns every speaker with examples. A generation counter drops
// a render that finishes after a newer one started (the newer one takes any
// pending focus). A failed render drops the pending focus, so a later
// rebuild (More examples) does not move focus to a name box (#471 review).
async function renderNames() {
  const generation = ++namesState.generation;
  const data = await attempt(() => call("speaker_panel", state.mid, namesState.offsets));
  if (generation !== namesState.generation) return;
  if (!data) { namesState.focus = null; return; }
  $("n-undo").disabled = !data.can_undo;
  const blocks = data.speakers.map((sp, i) => speakerBlock(sp, data.speakers, i, data.you));
  $("n-list").replaceChildren(...blocks);
  youFollowsTyping(blocks, data.speakers, data.you);
  const focus = namesState.focus;
  namesState.focus = null;
  if (focus && blocks.length) {
    const i = data.speakers.findIndex((sp) => sp.label === focus.label);
    const block = blocks[Math.max(i, 0)];
    (block.querySelector(focus.part) || block.querySelector(".n-label input")).focus();
  }
}

// The suggestion follows what is typed (#461): a name typed for the
// suggested speaker, or 'You' typed for anyone, hides it at once, as the
// rebuilt panel would, so Use 'You' never replaces a name being typed.
// Putting the boxes back as they were shows it again.
function youFollowsTyping(blocks, speakers, you) {
  const i = speakers.findIndex((sp) => sp.label === you);
  const row = i < 0 ? null : blocks[i].querySelector(".n-you");
  if (!row) return;
  const boxes = blocks.map((b) => b.querySelector(".n-label input"));
  const update = () => {
    row.hidden = boxes[i].value !== speakers[i].name
      || boxes.some((b) => b.value.trim().toLowerCase() === "you");
  };
  for (const box of boxes) box.addEventListener("input", update);
}

// A two-track recording (#108): the speaker who was mostly on the user's
// microphone is suggested as "You". Only offered: the user chooses Use
// 'You', and nothing is named without them (the owner's decision 5).
function youSuggestion(sp, name) {
  const use = el("button", { type: "button", class: "n-you-use", textContent: "Use 'You'",
    "aria-label": `Use 'You' as the name for ${sp.name}`, on: { click: () => attempt(async () => {
      await call("rename_speaker", state.mid, sp.label, "You");
      name.value = "You";
      sayDone("Name saved");
      namesState.focus = { label: sp.label, part: ".n-label input" };   // the button goes (#461)
      afterNamesChange(true);
    }) } });
  return el("p", { class: "n-you" }, el("span", { textContent: "Mostly on your microphone: probably you." }), use);
}

function speakerBlock(sp, all, index, you) {
  const name = el("input", { value: sp.name, maxLength: 80,
                           "aria-label": `Name for ${sp.name}, ${plural(sp.passages, "passage")}` });
  // Renaming does not rebuild the panel, so focus and typing elsewhere are kept.
  name.addEventListener("change", () => attempt(async () => {
    await call("rename_speaker", state.mid, sp.label, name.value);
    sayDone("Name saved");
    afterNamesChange(false);
  }));
  const ex = sp.examples;
  const examples = el("ul", { class: "examples" }, ...ex.items.map((it) => {
    const b = el("button", { type: "button", class: "ex-play", title: "Play exactly this passage",
                             "aria-label": `Play ${fmtTime(it.start)}`,
                             on: { click: () => play(it.start, it.end, true) } }, playIcon(10), fmtTime(it.start));
    return el("li", {}, b, el("span", { textContent: it.text }));
  }));
  const page = (offset) => () => {
    namesState.offsets[sp.label] = offset;
    namesState.focus = { label: sp.label, part: ".n-more" };     // the button goes too (#475)
    renderNames();
  };
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
    you === sp.label ? youSuggestion(sp, name) : null,
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
      sayDone("Saved");
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
    sayDone("Edit undone");
  } else if (r === null) say("Nothing to undo");
  renderMeeting();
}
