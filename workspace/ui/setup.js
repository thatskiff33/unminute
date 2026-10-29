// Unminute UI: setup.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

// -- setup (#19) ----------------------------------------------------------------------
// First run in five steps. The token is typed into a password field and
// passed straight to Python; the page never receives it back (only whether
// one is remembered).
const SU_STEPS = [["Welcome", ""], ["Speech model", ""], ["Speaker names", "Optional"],
                  ["Microphone", "Optional"], ["Ready", ""]];
// returnTo: opened from Downloads for one step (the speaker token); leaving that step goes back.
const suState = { step: 1, skipSpeakers: false, mic: null, computer: false, returnTo: null };

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
  return button("Cancel", (e) => attempt(async () => {
    const cancelled = await call("cancel_download", c.name);
    if (cancelled) sayDone("Removed from the queue."); else say("It is no longer in the queue.");
    then();
  }, e.currentTarget));
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
  if (isRunning(c)) action = button("Cancel", (e) => attempt(() => call("cancel_download", c.name).then(renderSetup), e.currentTarget));
  else if (isQueued(c)) action = cancelQueued(c, renderSetup);
  else if (c.state === "ready")
    action = el("span", { class: "su-ready" }, el("span", { class: "tick" }, icon("tick", 13)), "Ready");
  else if (downloadOff(c))
    action = itManaged(c);
  else {
    // Another download running: this one queues behind it (#366).
    action = button(c.state === "missing" ? `Download (${c.size_gb} GB)` : "Repair",
                    (e) => startDownload(c.name, { near: e.currentTarget }), { primary: true });
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
  if (running) action = button("Cancel", (e) => attempt(() => call("cancel_download", c.name).then(refresh), e.currentTarget));
  else if (queued) action = cancelQueued(c, refresh);
  else if (c.state === "ready")
    action = button("Check every file", (e) => attempt(() => call("verify_model", c.name).then(refresh), e.currentTarget),
                    { title: "Compare every file with its published checksum" });
  else if (c.needs_token && c.state === "missing")
    action = button("Set up…", () => {
      suState.step = 3;
      suState.returnTo = state.view === "downloads" ? "downloads" : null;
      show("setup");
      setStep(3);
    }, { primary: true });
  else if (downloadOff(c))
    action = itManaged(c);
  else action = button(c.state === "missing" ? `Download (${c.size_gb} GB)` : "Repair",
                       (e) => startDownload(c.name, { near: e.currentTarget }), { primary: true });
  // Download, Repair and Set up stay available: another download queues (#366).
  if (c.state === "ready" && !running && !queued && action.tagName === "BUTTON") action.disabled = busy;
  const remove = c.removable && !running && !queued ? linkButton("Remove…", (e) => confirmDialog({
    title: `Remove ${cardName(c)}?`,
    body: `This deletes it from this computer, including any damaged or unfinished copies. Meetings already transcribed keep their text. You can download it again from Downloads.`,
    check: "Remove it from this computer.", limits: false, danger: true,
    onConfirm: ((near) => () => attempt(async () => {
      const gb = await call("remove_model", c.name);
      sayDone(`Removed. About ${gb} GB freed.`);
      refresh();
    }, near))(e.currentTarget) })) : null;
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
  $("s-download-speakers").disabled = isRunning(speaker) || isQueued(speaker) || modelDownloadsOff();
  $("s-token").disabled = modelDownloadsOff();
  $("s-use-remembered").hidden = !s.token_remembered || speaker.state === "ready"
    || modelDownloadsOff();
  $("s-use-remembered").disabled = isRunning(speaker) || isQueued(speaker);
  $("s-remove-token").hidden = !s.token_remembered;
  $("s-token-note").textContent = s.token_remembered
    ? "A Hugging Face token is remembered in Windows Credential Manager. Removing it does not affect models already downloaded."
    : "";
  putText($("su-speaker-status"), speaker.state === "ready" ? "The speaker model is downloaded and ready."   // live: only on change
    : isQueued(speaker) ? queueNote(speaker.download)
    : speaker.download ? speaker.download.message : "");
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
  const mic = !suState.mic ? "Skipped. Turn on in Settings." : suState.computer ? "On, with computer audio" : "On";
  $("su-summary").replaceChildren(line("Speech model", speech, !!ready),
    line("Speaker names", names, names === "Ready"), line("Microphone", mic, !!suState.mic));
}

async function renderSuMic() {
  const s = await attempt(() => call("settings"));
  if (!s) return;
  state.managed = s.managed;
  suState.mic = !!s.microphone;
  suState.computer = !!s.computer_audio;
  $("su-mic").checked = suState.mic;
  $("su-mic-row").hidden = $("su-computer-row").hidden = !suState.mic;
  $("su-computer").checked = suState.computer;
  $("su-computer").disabled = computerLocked(s);
  $("su-computer-note").textContent = computerNote(s);
  if (!suState.mic) $("su-test-result").hidden = true;
  if (suState.mic && (micLists["su-device"].stale || !$("su-device").options.length))
    await attempt(() => relistMics("su-device", showSuMics));
}

// extra.near: the control that asked, so an error stays beside it (#416).
function startDownload(name, extra = {}) {
  return attempt(async () => {
    const how = await call("download_model", name, extra.token || null, extra.remember !== false);
    sayDone(how === "queued" ? "Queued. It starts when the download before it finishes." : "Download started");
    refresh();
  }, extra.near || null);
}

function wireSetup() {
  for (const b of document.querySelectorAll("[data-help]"))
    b.addEventListener("click", () => attempt(() => call("open_help", b.dataset.help)));
  for (const b of document.querySelectorAll("#view-setup [data-go]"))
    b.addEventListener("click", () => setStep(Number(b.dataset.go)));
  const speakerName = () => (state.setup.cards.find((c) => c.role === "speakers") || {}).name;
  $("s-download-speakers").addEventListener("click", () => {
    const token = $("s-token").value.trim();
    if (!token) { showError(errorHome("s-token"), "Paste your Hugging Face token first.", $("s-token")); return; }
    $("s-token").value = "";                         // never kept in the page
    suState.skipSpeakers = false;
    startDownload(speakerName(), { token, remember: $("s-remember").checked, near: $("s-download-speakers") });
  });
  $("s-use-remembered").addEventListener("click", () => startDownload(speakerName(), { near: $("s-use-remembered") }));
  const removeToken = (e) => attempt(async () => {
    await call("remove_token");
    sayDone("Token removed. Downloaded models keep working.");
    refresh();
  }, e.currentTarget);
  $("s-remove-token").addEventListener("click", removeToken);
  $("dl-remove-token").addEventListener("click", removeToken);
  $("s-skip-speakers").addEventListener("click", () => { suState.skipSpeakers = true; setStep(4); });
  $("su-next-3").addEventListener("click", () => { suState.skipSpeakers = false; setStep(4); });
  $("su-mic").addEventListener("change", () => attempt(async () => {
    await call("set_settings", { microphone: $("su-mic").checked });
    micLists["su-device"].stale = true;
    renderSuMic();
  }));
  $("su-computer").addEventListener("change", () => attempt(async () => {
    await call("set_settings", { computer_audio: $("su-computer").checked });
    renderSuMic();
  }));
  $("su-skip-4").addEventListener("click", () => {
    // First run only: skipping undoes a switch turned on here. Later visits leave Settings alone.
    if (suState.mic && !state.setup.onboarded)
      attempt(() => call("set_settings", { microphone: false }).then(() => { suState.mic = suState.computer = false; renderSummary(state.setup); }));
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
