// Trial UI. All transcript and AI text is rendered with textContent, never
// innerHTML, so markup in a transcript is shown as text and never executed.
"use strict";

const statusEl = document.getElementById("status");
const list = document.getElementById("transcript");
const player = document.getElementById("player");

function fmt(t) {
  const s = Math.floor(t), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

function row(item) {
  const li = document.createElement("li");
  const t = document.createElement("button");
  t.className = "t";
  t.type = "button";
  t.textContent = fmt(item.t);
  t.title = "Play this passage";
  t.addEventListener("click", () => playFrom(item.t));
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = item.who ? item.who + ":" : "";
  const text = document.createElement("span");
  text.textContent = item.text;
  li.append(t, who, text);
  return li;
}

// Bridge calls return {ok: true, value} or {ok: false, error}; they never
// throw, so no Python stack trace (with file paths) reaches the page.
async function call(name, ...args) {
  const r = await window.pywebview.api[name](...args);
  if (!r || !r.ok) throw new Error((r && r.error) || "The request failed.");
  return r.value;
}

// Render every paragraph in pages from the bridge; returns timing for the trial.
async function renderAll() {
  const t0 = performance.now();
  let start = 0, total = 1;
  while (start < total) {
    const page = await call("transcript", start, 2000);
    total = page.total;
    const frag = document.createDocumentFragment();
    for (const item of page.items) frag.append(row(item));
    list.append(frag);
    start += page.items.length;
    if (!page.items.length) break;
  }
  await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  return { rendered: list.children.length, ms: performance.now() - t0 };
}

async function playFrom(t) {
  try {
    player.src = await call("clip", t, t + 10);
    await player.play();
  } catch (err) {
    statusEl.textContent = String(err && err.message || err);
  }
}

window.trial = { renderAll, playFrom, call };

window.addEventListener("pywebviewready", async () => {
  const r = await renderAll();
  window.trial.lastRender = r;
  statusEl.textContent = `${r.rendered} paragraphs ready`;
});
