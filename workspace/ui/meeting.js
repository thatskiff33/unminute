// Unminute UI: one meeting, with its side panel and transcript rows.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

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
  catch (err) { sayError(err); show("meetings"); return; }
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
  // Live regions (this one assertive) are set only when their text changes:
  // set at every poll, even to the same text, a screen reader reads them
  // again each time (#346, the owner's Narrator check of 26 Oct 2026).
  putText($("m-error"), d.error || "");
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
  putText($("m-playback"), text);                  // in #m-banner, a live region: only on change
  $("m-banner-lock").toggleAttribute("hidden", icon !== "lock");
  $("m-banner-clock").toggleAttribute("hidden", icon !== "clock");
  $("m-banner-action").hidden = !action;
  if (action) putText($("m-banner-action"), action[0]);
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
