// Unminute UI: navigation and small builders.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

// -- navigation -------------------------------------------------------------
const VIEWS = ["meetings", "meeting", "handoff", "settings", "downloads", "setup", "help"];
// The sidebar item each view belongs to.
const NAV_OF = { meetings: "meetings", meeting: "meetings", handoff: "meetings", settings: "settings",
                 downloads: "settings", setup: "settings", help: "help" };

function show(view) {
  if (state.locked) return;            // only the locked screen (#111 C4b)
  if (view === "inbox" || view === "active") view = "meetings";   // their earlier names
  if (view !== "meeting") stopAudio();
  if (view !== "meeting" && view !== "handoff") stopEditing();
  if (view !== state.view) { suState.sig = ""; state.downloadsSig = ""; state.listsSig = ""; }   // rebuilt on arrival
  if (view !== state.view) micsStale();                 // returning: the microphones may have changed (#369)
  if (view !== "meetings") stopRecTimer();
  state.view = view;
  document.body.dataset.view = view;
  setMenu(false);
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== view;
  for (const b of document.querySelectorAll(".nav"))
    b.setAttribute("aria-current", b.dataset.view === NAV_OF[view] ? "page" : "false");
  window.scrollTo(0, 0);
  refresh();
}

function setMenu(open) {
  document.body.classList.toggle("menu-open", open);
  $("menu").setAttribute("aria-expanded", String(open));
}

// handoff: open the Hand off as soon as the meeting has loaded (from the list).
async function openMeeting(mid, { handoff = false } = {}) {
  stopEditing();
  state.pendingHandoff = handoff;
  state.mid = mid; state.total = -1; state.signature = ""; state.hits = []; state.hit = -1;
  dropSearches();
  state.version = null; state.items = []; state.speakerList = []; state.playingRow = null; state.speakersSig = "";
  $("m-search").value = ""; $("m-hits").textContent = ""; $("m-hit-nav").hidden = true;
  $("m-search").closest(".search").classList.remove("active");
  $("m-again-form").hidden = true;
  $("m-again").setAttribute("aria-expanded", "false");
  const audio = $("player");
  stopAudio();
  audio.removeAttribute("src"); audio.load();
  setTab("transcript");
  $("transcript").replaceChildren();
  show("meeting");
}

// -- small builders ---------------------------------------------------------------
const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  alert: ["M12 8v5", "M12 16h.01", "circle:12,12,9"],
  clock: ["M12 7v5l3 2", "circle:12,12,9"],
  tick: ["M5 12l5 5 9-10"],
};
function icon(name, size = 20) {
  const s = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({ width: size, height: size, viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": name === "tick" ? 3 : 2, "stroke-linecap": "round",
    "stroke-linejoin": "round", "aria-hidden": "true" })) s.setAttribute(k, v);
  for (const d of ICONS[name]) {
    let p;
    if (d.startsWith("circle:")) {
      const [cx, cy, r] = d.slice(7).split(",");
      p = document.createElementNS(SVG_NS, "circle");
      p.setAttribute("cx", cx); p.setAttribute("cy", cy); p.setAttribute("r", r);
    } else {
      p = document.createElementNS(SVG_NS, "path");
      p.setAttribute("d", d);
    }
    s.append(p);
  }
  return s;
}
const button = (text, onClick, { primary = false, title = "" } = {}) =>
  el("button", { type: "button", class: primary ? "primary" : "", textContent: text, title,
                 on: { click: onClick } });
const linkButton = (text, onClick, title = "") =>
  el("button", { type: "button", class: "link-plain", textContent: text, title, on: { click: onClick } });
function notice(kind, text, ...actions) {
  const lead = kind === "alert" ? icon("alert") : kind === "hold" ? icon("clock")
    : el("span", { class: "tick" }, icon("tick", 12));
  return el("li", { class: `notice ${kind}` }, lead, el("span", { class: "notice-text", textContent: text }),
            ...actions);
}
const joinParts = (...parts) => parts.filter(Boolean).join(" · ");
