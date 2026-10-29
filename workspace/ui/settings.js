// Unminute UI: downloads, managed policy and settings.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

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
// Computer audio (#108): its switch's note in Settings and Setup.
const COMPUTER_NOTE = "Records the other side of a Teams or Zoom call as a second track. Any other sound playing on this computer is recorded too, such as notifications and videos. Tell everyone on the call before you start.";
function computerAllowed() {
  return allowed("AllowComputerAudioRecording") && allowed("AllowMicrophoneRecording");
}
function computerNote(s) {
  return !computerAllowed() ? MANAGED_NOTE
    : !s.microphone ? "Turn on Record with a microphone first: computer audio is recorded beside it."
    : COMPUTER_NOTE;
}
// Always possible to turn off; turning on needs policy and the microphone.
const computerLocked = (s) => !s.computer_audio && (!computerAllowed() || !s.microphone);
// Model downloads are off: AllowModelDownloads, or AllowUserModels on a machine
// install (#111 B7a; the window reports it allowed on a per-user install).
function modelDownloadsOff() {
  return !allowed("AllowModelDownloads") || !allowed("AllowUserModels");
}
// Download and Repair are off for this card. The graphics card components stay
// per-user (B7-5, DEC-068): AllowGpuComponent governs them, never AllowUserModels.
function downloadOff(c) {
  return !allowed("AllowModelDownloads")
    || (c.role === "gpu" ? !allowed("AllowGpuComponent") : !allowed("AllowUserModels"));
}
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
  microphone: "Microphone recording", computer_audio: "Computer audio recording",
  gpu_speakers: "NVIDIA graphics card",
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
  $("s-computer").checked = !!s.computer_audio;
  $("s-computer").disabled = computerLocked(s);
  $("s-computer-note").textContent = computerNote(s);
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
      button(f.active ? "Pause" : "Resume", () => attempt(() => call("set_folder_active", f.id, !f.active).then(renderSettings), "s-folders")),
      linkButton("Stop watching…", () => confirmDialog({
        title: "Stop watching this folder?",
        body: "Recordings already imported stay in the workspace. New recordings in this folder will no longer be imported, and its deletion permission is removed.",
        check: "Stop watching the folder.", limits: false,
        onConfirm: () => attempt(() => call("remove_folder", f.id).then(renderSettings), "s-folders") }))),
    el("label", {}, el("input", { type: "checkbox", checked: f.fetch_cloud,
      on: { change: (e) => attempt(() => call("set_folder_authority", f.id, null, e.target.checked).then(renderSettings), "s-folders") } }),
      "Download recordings that are only in cloud storage (OneDrive and similar)"),
    el("label", {}, el("input", { type: "checkbox", checked: f.delete_originals,
      disabled: !f.delete_originals && !allowed("AllowDeleteOriginals"),
      on: { change: (e) => {
        if (!e.target.checked) { attempt(() => call("set_folder_authority", f.id, false, null).then(renderSettings), "s-folders"); return; }
        e.target.checked = false;                     // until the app's own window confirms
        attempt(() => call("set_folder_authority", f.id, true, null).then((r) => {
          if (r && r.cancelled) say(declined(r, "Nothing changed: originals in this folder are kept."));
          return renderSettings();
        }), "s-folders");
      } } }),
      "Delete originals from this folder after review…"),
    f.delete_originals && !allowed("AllowDeleteOriginals")
      ? el("p", { class: "set-text", textContent: "Originals are kept: deleting them is turned off by your organisation." })
      : null);
}

// near: the control changed, so a refusal stays in its card (#416), and the
// controls are drawn again at once from what is saved, not at the next poll.
function setSetting(changes, near) {
  let saved = false;
  attempt(async () => { await call("set_settings", changes); saved = true; await renderSettings(); }, near)
    .then(() => { if (!saved) attempt(renderSettings); });
}
