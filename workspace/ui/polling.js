// Unminute UI: drops, the refresh and polling.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

let dragTimer = null;
function wireDrop() {
  const overlay = $("drop-overlay");
  const hide = () => { overlay.hidden = true; };
  document.addEventListener("dragover", (e) => {
    if (state.locked) return;            // nothing is imported while locked (#111 C4a)
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
    if (state.finishOnly) return;            // nothing is imported in finish-only mode
    overlay.hidden = false;
    clearTimeout(dragTimer);
    dragTimer = setTimeout(hide, 400);
  });
  document.addEventListener("drop", () => { clearTimeout(dragTimer); hide(); pollSoon(); });
}

let refreshAgain = false;

async function refresh() {
  if (!window.pywebview || state.locked) return;   // locked: nothing to refresh (#111 C4b)
  if (refreshing) {                    // one is running: run again when it ends, so an
    refreshAgain = true;               // action's refresh is never lost
    return;
  }
  refreshing = true;
  try {
    await renderStatus();
    if (state.locked) return;          // only the locked screen from here (#111 C4b)
    // The meeting list and reminders only when Python reports a change, the
    // view changed, or every LIST_REFRESH_POLLS polls anyway (ages and hold
    // reviews depend on the time). Counted in polls, not by the clock: the
    // design fixtures fix the clock for stable screenshots, and the idle
    // check must see this refresh. renderLists also keeps the sidebar count
    // current.
    state.sinceLists += 1;
    const lists = state.changes === undefined || state.changes !== state.listsSeen ||
                  state.view !== state.listsView || state.sinceLists >= LIST_REFRESH_POLLS;
    if (lists) {
      // The reminders first, so the rows can carry their retention chips:
      // the same two calls as before, in the other order. A failed reminders
      // call is reported but still lets the list and sidebar count draw.
      let reminders = null;
      if (state.view === "meetings") {
        try {
          reminders = await call("reminders");
          pollOk("reminders");
        } catch (err) {
          pollError("reminders", err);
        }
      }
      await renderLists(reminders);
      if (reminders) renderReminders(reminders);
      state.listsSeen = state.changes;
      state.listsView = state.view;
      state.sinceLists = 0;
    }
    if (state.view === "meetings") {
      await renderRecorder();
    } else if (state.view === "meeting") await renderMeeting();
    else if (state.view === "settings") await renderSettings();
    else if (state.view === "setup") await renderSetup();
    else if (state.view === "downloads") await renderDownloads();
    pollOk("refresh");
  } catch (err) {
    pollError("refresh", err);
  } finally {
    refreshing = false;
  }
  if (refreshAgain) {
    refreshAgain = false;
    await refresh();
    return;
  }
  if (pollTimer && pollDelay() < pollTimerDelay) schedulePoll();
}

// -- polling (docs/RELEASE_STANDARD.md, section 3) -----------------------------------
// Every 2 s while something is happening (a meeting being processed, an import,
// a recording) or Setup or Downloads is open; every 10 s otherwise; never while
// the window is hidden or minimised. Showing it again refreshes at once.
let pollTimer = null;
let pollTimerDelay = 0;

function pollDelay() {
  const active = state.active || state.wasActive ||
                 (state.recordingLive && state.view === "meetings") ||
                 state.view === "setup" || state.view === "downloads";
  return active ? POLL_MS : IDLE_POLL_MS;
}

// Soon, not in up to 10 s: after something the page did not start itself (a
// recording dropped on the window is imported by Python directly).
function pollSoon() {
  if (state.locked) return;
  state.active = true;                 // so an in-flight poll re-arms fast, not at 10 s;
  if (document.hidden) return;         // wasActive then keeps one more 2 s poll
  clearTimeout(pollTimer);
  pollTimerDelay = 500;
  pollTimer = setTimeout(pollNow, pollTimerDelay);
}

function schedulePoll() {
  if (state.locked) return;            // nothing changes until Unminute is opened again
  clearTimeout(pollTimer);
  pollTimer = null;
  if (document.hidden) return;
  pollTimerDelay = pollDelay();
  pollTimer = setTimeout(pollNow, pollTimerDelay);
}

async function pollNow() {
  await refresh();
  schedulePoll();
}
