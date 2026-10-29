// Unminute UI: confirmations.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

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

// Transcribe again with the model chosen in Settings > Speech model, from the
// Versions tab or a failed meeting's row (#244). A chosen model that is not
// ready is not started: the dialog says so and offers Manage downloads.
// meetingModel: the model the meeting's current version uses; failed: the
// meeting stopped with an error (only then does "still not ready" apply).
function transcribeAgain(mid, meetingModel, failed) {
  attempt(async () => {
    const s = await call("settings");
    const chosen = s.choices.find((c) => c.name === s.model);
    if (chosen && chosen.note) { modelNotReady(chosen, failed && chosen.name === meetingModel); return; }
    const named = chosen ? `${chosen.label}, the model chosen in Settings > Speech model`
      : "the model chosen in Settings > Speech model";
    confirmDialog({
      title: "Transcribe again with the current model?",
      body: `It uses ${named}. To use another model, change it there first. ` +
        "A new transcript is made from the audio. Your edits stay with the current version, " +
        "which is kept as an earlier version you can still open (read only); they are not copied " +
        "into the new transcript.",
      check: "I understand my edits stay with the earlier version.",
      limits: false,
      onConfirm: () => attempt(async () => {
        await call("retranscribe", mid);
        if (state.view === "meeting" && state.mid === mid) viewVersion(null);
        say("Transcribing again. The previous version and its edits are kept.");
        pollSoon();
      }),
    });
  });
}

// The chosen speech model is not downloaded or is damaged: say so, with the
// way to fix it (Manage downloads, unless policy turns downloads off) or to choose another.
function modelNotReady(chosen, same) {
  const dlg = $("model-dlg");
  $("md-title").textContent = same ? `${chosen.label} is still not ready` : `${chosen.label} is not ready`;
  $("md-body").textContent = (same ? "This meeting stopped because its speech model is not ready, and it is " +
    "still the model chosen in Settings > Speech model. " : "") + chosen.note;
  $("md-downloads").hidden = !chosen.offered;
  dlg.returnValue = "";
  dlg.onclose = () => {
    if (dlg.returnValue === "downloads") show("downloads");
    else if (dlg.returnValue === "settings") {
      show("settings");
      $("h-model").scrollIntoView({ block: "center" });   // the picker fills once Settings has loaded
    }
  };
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
      (state.managed && state.managed.clipboard_clear_minutes
        ? "The app does not delete Markdown files you saved. Your organisation has the app clear its own copies from the clipboard after a set time, but copy something else now to be sure (check Win+V if you copied the text another way), and delete saved handoff files when you no longer need them."
        : "The app does not clear your clipboard or delete Markdown files you saved: copy something else to replace the clipboard (the app's copies are kept out of Windows clipboard history, but check Win+V if you copied the text another way), and delete saved handoff files when you no longer need them."),
    check: "The final document is reviewed and saved outside this app.",
    onConfirm: () => attempt(() => call("confirm_final_saved", d.id).then(() => {
      $("ho-preview").textContent = "";
      $("ho-passages").replaceChildren();
      handoffState.preview = null;
      show("meetings");
    })),
  });
}
