// Unminute UI: diagnostics, wiring and start-up. The last of the page's
// ordered scripts (index.html, #414), and the only one that runs anything as
// the page loads: the other scripts only declare, so it does not matter which
// of them a function is in, as long as each loads before this one.
"use strict";

// -- diagnostics (R9b, #130) ---------------------------------------------------
// The page's own script errors reach the app's log as a fixed kind and a line
// and column: never a message, which could quote meeting text. Python
// rate-limits them; a failed report is not reported again. The first error
// before the bridge is ready (in wire(), say) is kept until it is.
let reporting = false, bridgeReady = false, earlyError = null;
// Since the split into ordered scripts (#414), a line alone does not say
// which file: an error in one of PAGE_SCRIPTS is sent as the script's place
// in the list times 100000 plus its line (file 3, line 120: 300120), within
// the bridge's limit of 10,000,000; any other line is sent as it is. No file
// name is sent (docs/DEVELOPER_NOTES.md, "Page scripts").
const PAGE_SCRIPTS = ["core.js", "navigation.js", "meetings.js", "meeting.js", "speakers.js",
                      "playback.js", "confirm.js", "handoff.js", "setup.js", "settings.js",
                      "recording.js", "drafts.js", "polling.js", "main.js"];
const SCRIPT_LINES = 100000;
function scriptLine(url, line) {
  const at = PAGE_SCRIPTS.findIndex((name) => String(new URL(name, document.baseURI)) === url);
  return at < 0 ? line : (at + 1) * SCRIPT_LINES + Math.max(0, line | 0);
}

// The window opens the page from file://, where WebView2 hides an uncaught
// error in the page's own scripts from the error event: "Script error.", no
// file, line 0 (real window, 30 Oct 2026, #491). An error the page catches
// itself still has its stack. So every callback the browser calls later (an
// event listener, an on... handler, a timer, an animation frame) runs inside
// guarded(): if it throws, the guard notes where, from the stack, and throws
// the same error on, so nothing else changes; the error event that follows at
// once sends that place. From file:// the browser sends no unhandledrejection
// event at all for an error in the page's own scripts (same probe), so when an
// async callback's promise rejects, the guard sends the rejection itself and
// rejects on with the same reason (the event, where there is one, then skips
// it). Only numbers leave stackPlace: the stack's first frame in one of
// PAGE_SCRIPTS, as scriptLine numbers it, and the column. The error's name and
// message are cut off unread (they could quote meeting text) and no file name
// or path is sent (docs/DEVELOPER_NOTES.md, "Page scripts").
let caughtAt = null;                  // [line, col] of the error now being reported
const sentRejections = new WeakSet();  // the reasons (objects) of rejections a guard has sent
const guards = new WeakMap();         // a callback -> its guard, to remove it again
const FRAME = /^ +at (?:.* \()?(.+):(\d+):(\d+)\)?$/;
function stackPlace(err) {
  let stack, head;
  try {
    stack = String(err.stack);
    head = String(err);
  } catch (e) { return null; }        // not an Error, or one that will not say
  if (!stack.startsWith(head)) return null;
  for (const frame of stack.slice(head.length).split("\n")) {
    const m = FRAME.exec(frame);
    if (!m) continue;
    const at = PAGE_SCRIPTS.findIndex((name) => String(new URL(name, document.baseURI)) === m[1]);
    if (at >= 0) return [scriptLine(m[1], Number(m[2])), Math.max(0, Number(m[3]) | 0)];
  }
  return null;
}
function guarded(fn) {
  if (typeof fn !== "function") return fn;
  let guard = guards.get(fn);
  if (!guard) {
    guard = function (...args) {
      let value;
      try {
        value = fn.apply(this, args);
      } catch (err) {
        caughtAt = stackPlace(err);
        throw err;
      }
      // The browser does not wait for a callback's promise, so its rejection
      // is one nobody handles.
      if (value instanceof Promise) {
        value.catch((reason) => {
          if (Object(reason) === reason) sentRejections.add(reason);
          reportUiError("rejection", ...(stackPlace(reason) || [0, 0]));
          throw reason;
        });
      }
      return value;
    };
    guards.set(fn, guard);
  }
  return guard;
}
// Run once, as the page loads, before any listener is added: the browser's
// own ways to call the page later go through guarded(). A listener object
// (handleEvent) and a timer given as text (which the page's policy refuses
// anyway) pass unchanged.
function guardCallbacks(win) {
  const proto = win.EventTarget && win.EventTarget.prototype;
  if (proto) {
    const add = proto.addEventListener, remove = proto.removeEventListener;
    proto.addEventListener = function (type, fn, options) {
      return add.call(this, type, guarded(fn), options);
    };
    proto.removeEventListener = function (type, fn, options) {
      return remove.call(this, type, (typeof fn === "function" && guards.get(fn)) || fn, options);
    };
  }
  for (const name of ["setTimeout", "setInterval", "requestAnimationFrame"]) {
    const real = win[name];
    if (typeof real === "function") win[name] = (fn, ...rest) => real.call(win, guarded(fn), ...rest);
  }
}
function reportUiError(event, line, col) {
  if (!bridgeReady) { earlyError = earlyError || [event, line, col]; return; }
  if (reporting) return;
  reporting = true;
  call("report_ui_error", event, Math.max(0, line | 0), Math.max(0, col | 0))
    .catch(() => {}).finally(() => { reporting = false; });
}

function wire() {
  $("help-diagnostics").addEventListener("click", () => attempt(async () => {
    await call("copy_diagnostics");
    sayDone("Copied diagnostics: no meeting content. Paste them into your support request.");
  }));
  $("s-add-folder").addEventListener("click", () => attempt(async () => {
    const added = await call("add_folder_dialog", $("s-include-existing").checked);
    if (added) sayDone("Folder added. It will be checked now.");
    pollSoon();
  }, "s-add-folder"));
  $("s-scan").addEventListener("click", () => attempt(async () => {
    await call("scan_now");
    say("Checking watched folders");
    pollSoon();
  }, "s-scan"));
  wireSetup();
  wireHandoff();
  wireLocked();
  for (const b of document.querySelectorAll(".nav")) b.addEventListener("click", () => show(b.dataset.view));
  $("import").addEventListener("click", () => attempt(async () => {
    await call("clear_import_errors");
    const r = await call("import_dialog");
    if (r.accepted) say(`Importing ${r.accepted} recording${r.accepted > 1 ? "s" : ""}`);
    refresh();
  }, "import"));
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
  $("m-retranscribe").addEventListener("click", () =>
    transcribeAgain(state.mid, state.detail ? state.detail.model : null, !!(state.detail && state.detail.error)));
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
        pollSoon();
      }),
    });
  });
  let searchTimer;
  $("m-search").addEventListener("input", () => {
    dropSearches();                      // answers to what was typed before are stale
    clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250);
  });
  $("m-search").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); nextHit(e.shiftKey ? -1 : 1); } });
  $("m-next").addEventListener("click", () => nextHit(1));
  $("m-prev").addEventListener("click", () => nextHit(-1));
  $("h-add").addEventListener("click", () => attempt(async () => {
    await call("add_hold", state.mid, $("h-scope").value, $("h-reason").value);
    $("h-reason").value = "";
    refresh();
  }));
  $("c-check").addEventListener("change", () => { $("c-ok").disabled = !$("c-check").checked; });
  for (const r of document.querySelectorAll('input[name="mode"]')) r.addEventListener("change", () => setSetting({ mode: r.value }, r));
  for (const r of document.querySelectorAll('input[name="recdisplay"]'))
    r.addEventListener("change", () => setSetting({ recording_display: r.value }, r));
  $("s-battery").addEventListener("change", () => setSetting({ battery_override: $("s-battery").checked }, "s-battery"));
  $("s-speakers").addEventListener("change", () => setSetting({ speakers: $("s-speakers").checked }, "s-speakers"));
  $("s-gpu").addEventListener("change", () => setSetting({ gpu_speakers: $("s-gpu").checked }, "s-gpu"));
  $("s-microphone").addEventListener("change", () => setSetting({ microphone: $("s-microphone").checked }, "s-microphone"));
  $("s-computer").addEventListener("change", () => setSetting({ computer_audio: $("s-computer").checked }, "s-computer"));
  $("s-lid").addEventListener("change", () => setSetting({ lid_override: $("s-lid").checked }, "s-lid"));
  $("s-drafts").addEventListener("change", () => setSetting({ live_drafts: $("s-drafts").checked }, "s-drafts"));
  for (const r of document.querySelectorAll('input[name="draftsaudio"]'))
    r.addEventListener("change", () => setSetting({ live_drafts_audio: r.value }, r));
  wireRecorder();
  $("s-model").addEventListener("change", () => setSetting({ model: $("s-model").value }, "s-model"));
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

// Closing the window while recording: the app keeps the window open and asks.
function askClose() {
  const dlg = $("close-rec");
  if (dlg.open) return true;
  dlg.returnValue = "";
  dlg.onclose = () => attempt(async () => {
    const choice = dlg.returnValue;
    if (choice === "save") {
      // If it already stopped by itself it was saved: close either way.
      try { await call("stop_recording"); } finally { await call("close_app"); }
    } else if (choice === "discard") {
      await call("discard_recording");             // on an error the window stays, and says why
      await call("close_app");
    } else renderRecorder();                         // keep recording
  });
  dlg.showModal();
  return true;
}


// -- start-up --------------------------------------------------------------------
// Everything the page runs as it loads, in the order app.js ran it before the
// split: the meter's colours follow the theme, the polling follows the window's
// visibility, page errors are reported, then the page is wired and started.
// First, the browser's ways to call the page later are guarded (#491).
guardCallbacks(window);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", readVizColours);

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearTimeout(pollTimer);
    pollTimer = null;
    stopRecTimer();
    restartMeter(viz, meterClock);           // minimised: the meter's frames pause (#216)
  } else {
    state.sinceLists = LIST_REFRESH_POLLS;   // hidden for a while: ages and reviews moved on
    micsStale();                             // and microphones may have come or gone (#369)
    pollNow();
  }
});

// Where the browser gives the file and line (served over HTTP, as in the
// design check), they are used; from file:// it gives neither, and the place
// a guard noted is used instead (#491). A rejection a guard has already sent
// is not sent again; any other is sent with its reason's place, when it is an
// Error with a stack.
window.addEventListener("error", (e) => {
  const caught = caughtAt;
  caughtAt = null;
  if (e.filename) reportUiError("error", scriptLine(e.filename, e.lineno), e.colno);
  else reportUiError("error", ...(caught || [0, 0]));
});
window.addEventListener("unhandledrejection", (e) => {
  if (Object(e.reason) === e.reason && sentRejections.has(e.reason)) return;
  reportUiError("rejection", ...(stackPlace(e.reason) || [0, 0]));
});
window.addEventListener("pywebviewready", () => {
  bridgeReady = true;
  const early = earlyError;
  earlyError = null;
  if (early) reportUiError(...early);
});

window.workspace = { call, show, openMeeting, refresh, state, play, say, setStep, askClose };   // check tools, close

guarded(wire)();                      // an error in wiring has its place too (#491)
window.addEventListener("pywebviewready", async () => {
  // A locked workspace (#111 C4b): the status first, and if it is locked only
  // the locked screen, with no other request and no polling.
  let st = null;
  try { st = await call("status"); } catch (err) { /* shown by refresh */ }
  if (st && st.locked) {
    renderLocked(st.locked);
    return;
  }
  // First run, or no usable speech model: start with Setup.
  let s = null;
  try { s = await call("setup_state"); } catch (err) { /* shown by refresh */ }
  if (s) state.managed = s.managed;                 // what policy allows, from the start
  if (s && (!s.onboarded || !s.speech_ready)) {
    suState.step = s.onboarded ? 2 : 1;
    show("setup");
    setStep(suState.step);
  } else show("meetings");
  schedulePoll();
});
