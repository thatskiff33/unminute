// Unminute UI: search and playback.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

// -- search --------------------------------------------------------------------
// Every search request takes the next number, and only the latest request's
// answer is shown, so an answer that comes late never replaces a newer one.
// Typing and opening another meeting or version take a number too, dropping
// the answers to every request made before. A search the user typed jumps to
// its first match when its matches are shown, whichever request (typed, or a
// redraw's for the same text) answers last.
let searchSeq = 0, searchJump = false;
function newSearch(jump = false) { if (jump) searchJump = true; return ++searchSeq; }
function dropSearches() { searchJump = false; return ++searchSeq; }
function takeJump() { const jump = searchJump; searchJump = false; return jump; }

async function runSearch(jump = true) {
  const q = $("m-search").value.trim(), mid = state.mid, version = state.version;
  if (!q) { dropSearches(); showHits("", [], false); return; }
  const seq = newSearch(jump);
  const hits = await attempt(() => call("search", mid, q, version)) || [];
  if (seq !== searchSeq) return;        // a newer search, typing or another meeting since
  showHits(q, hits, takeJump());
}

// A search's matches shown on the rows drawn: highlighted and counted. Without
// a jump, the current match stays current if it still matches.
function showHits(q, hits, jump) {
  const current = state.hit >= 0 ? state.hits[state.hit] : undefined;
  for (const li of document.querySelectorAll("#transcript li.hit")) li.classList.remove("hit", "current");
  $("m-search").closest(".search").classList.toggle("active", !!q);
  if (!q) { state.hits = []; state.hit = -1; $("m-hits").textContent = ""; $("m-hit-nav").hidden = true; return; }
  state.hits = hits;
  state.hit = jump ? -1 : hits.indexOf(current);
  $("m-hits").textContent = hits.length ? `${hits.length} found` : "No matches";
  $("m-hit-nav").hidden = hits.length === 0;
  markHits();
  if (jump) nextHit(1);
  else if (state.hit >= 0) $("m-hits").textContent = `${state.hit + 1} of ${hits.length}`;
}

// The matches shown so far marked on the rows drawn (after a redraw).
function markHits() {
  if (!$("m-search").value.trim()) return;
  for (const id of state.hits) { const row = rowFor(id); if (row) row.classList.add("hit"); }
  const row = state.hit >= 0 ? rowFor(state.hits[state.hit]) : null;
  if (row) row.classList.add("current");
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
// The bridge serves at most 30 s of audio at a time, so the player plays
// clips back to back, fetching the next one while the current one plays.
// Speaker examples play exactly their passage and stop.
const SPEEDS = [1, 1.25, 1.5, 2, 0.75];
const clips = { start: 0, end: 0, exact: false, generation: 0, next: null };

function meetingLength() {
  const d = state.detail;
  const last = state.items && state.items.length ? state.items[state.items.length - 1].end : 0;
  return Math.max((d && d.duration_s) || 0, last || 0);
}

function fetchClip(start, end) {
  return call("clip", state.mid, start, end).catch((err) => { sayError(err); return null; });
}

async function playFrom(t, { exact = false, end = null } = {}) {
  const audio = $("player");
  const length = meetingLength();
  const start = Math.max(0, Math.min(t, Math.max(0, length - 0.05)));
  const stop = exact ? Math.max(end, start + 0.05) : Math.min(start + 30, length);
  if (stop - start < 0.05) return;
  const generation = ++clips.generation;
  clips.pending = start;             // where playback is going, until the clip arrives
  audio.pause();
  const pre = clips.next && clips.next.start === start && clips.next.end === stop ? clips.next.url : null;
  clips.next = null;
  const url = await (pre || fetchClip(start, stop));
  if (generation !== clips.generation) return;
  clips.pending = null;
  if (!url) return;
  Object.assign(clips, { start, end: stop, exact });
  audio.src = url;
  audio.playbackRate = exact ? 1 : state.speed || 1;
  try { await audio.play(); }
  catch (err) { if (err && err.name !== "AbortError") sayError(err); }
  if (generation !== clips.generation) return;
  if (!exact && stop < length - 0.05) {
    const nextEnd = Math.min(stop + 30, length);
    clips.next = { start: stop, end: nextEnd, url: fetchClip(stop, nextEnd) };
  }
}

// play(start, end, exact): exact plays just [start, end]; otherwise it plays on.
function play(start, end, exact = false) { return playFrom(start, { exact, end }); }

function position() {
  const audio = $("player");
  if (clips.pending != null) return clips.pending;
  return audio.src ? clips.start + (audio.currentTime || 0) : 0;
}

function stopAudio() {
  clips.generation++;
  clips.next = null;
  clips.pending = null;
  $("player").pause();
}

function itemAt(t) {
  const items = state.items || [];
  let found = null;
  for (const it of items) { if (it.t <= t + 0.01) found = it; else break; }
  return found;
}

function updatePlayer() {
  const audio = $("player");
  const length = meetingLength();
  const pos = Math.min(position(), length);
  const playing = !audio.paused && !audio.ended;
  $("p-icon-play").style.display = playing ? "none" : "";
  $("p-icon-pause").style.display = playing ? "" : "none";
  $("p-play").setAttribute("aria-label", playing ? "Pause (Space)" : "Play (Space)");
  $("p-time").textContent = `${fmtTime(pos)} / ${fmtTime(length)}`;
  $("p-fill").style.width = length ? `${(100 * pos) / length}%` : "0";
  $("p-seek").setAttribute("aria-valuemax", String(Math.round(length)));
  $("p-seek").setAttribute("aria-valuenow", String(Math.round(pos)));
  $("p-seek").setAttribute("aria-valuetext", `${fmtTime(pos)} of ${fmtTime(length)}`);
  const it = audio.src ? itemAt(pos) : null;
  $("p-who").textContent = it ? joinParts(it.who, fmtTime(it.t)) : "";
  const row = it ? rowFor(it.id) : null;
  if (row !== state.playingRow) {
    if (state.playingRow) state.playingRow.classList.remove("playing");
    if (row) row.classList.add("playing");
    state.playingRow = row;
  }
}

function togglePlay() {
  const audio = $("player");
  if (!audio.paused) { audio.pause(); return; }
  if (audio.src && !audio.ended && !clips.exact) attempt(() => audio.play());
  else playFrom(position());
}

function nextPassage(dir) {
  const items = state.items || [];
  if (!items.length) return;
  const cur = itemAt(position());
  const i = cur ? items.indexOf(cur) : -1;
  const target = items[Math.max(0, Math.min(items.length - 1, i + dir))];
  if (target) playFrom(target.t);
}

function wirePlayer() {
  const audio = $("player");
  for (const ev of ["play", "pause", "timeupdate", "emptied"]) audio.addEventListener(ev, updatePlayer);
  audio.addEventListener("ended", () => {
    if (!clips.exact && clips.end < meetingLength() - 0.05) playFrom(clips.end);
    else updatePlayer();
  });
  $("p-play").addEventListener("click", togglePlay);
  $("p-back").addEventListener("click", () => playFrom(position() - 5));
  $("p-speed").addEventListener("click", () => {
    const i = SPEEDS.indexOf(state.speed || 1);
    state.speed = SPEEDS[(i + 1) % SPEEDS.length];
    $("p-speed").textContent = `${state.speed}×`;
    $("p-speed").setAttribute("aria-label", `Playback speed ${state.speed}×`);
    if (!clips.exact) audio.playbackRate = state.speed;
  });
  const seek = $("p-seek");
  seek.addEventListener("click", (e) => {
    const r = seek.getBoundingClientRect();
    playFrom(((e.clientX - r.left) / r.width) * meetingLength());
  });
  seek.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -5, ArrowRight: 5, PageDown: -30, PageUp: 30 }[e.key];
    if (step) { e.preventDefault(); playFrom(position() + step); }
    if (e.key === "Home") { e.preventDefault(); playFrom(0); }
    if (e.key === "End") { e.preventDefault(); playFrom(Math.max(0, meetingLength() - 5)); }
  });
}
