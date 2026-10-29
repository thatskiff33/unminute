// Unminute UI: handoff.
// One of the page's ordered scripts (index.html, #414): it only declares.
// main.js, the last, runs the page.
"use strict";

// -- handoff (#22) ------------------------------------------------------------------
// A page in three steps: template, details to remove, copy or save. The
// preview is exactly what Copy or Save produces. Nothing is sent anywhere by
// the app: the user pastes or attaches it in their approved assistant.
// "Replace names" applies to this handoff only and is not stored.
// Copy and open (#109) copies, then the app opens the assistant's address
// (policy, or ChatGPT): the page only shows the name and host it is given.
const handoffState = { template: "minutes", templates: [], preview: null, generation: 0,
                       dirty: false, anonymise: false, total: 0 };

async function openHandoff() {
  // Nothing from the previous meeting stays on the page while this one loads.
  if (handoffState.mid !== state.mid) handoffState.anonymise = false;   // this hand off only
  handoffState.mid = state.mid;
  handoffState.preview = null;
  handoffState.dirty = false;
  handoffState.generation++;
  for (const id of ["ho-preview", "ho-info", "ho-redactions"]) {
    if ($(id).tagName === "TEXTAREA") $(id).value = ""; else $(id).textContent = "";
  }
  $("ho-passages").replaceChildren();
  clearErrorsIn("view-handoff");                  // errors belong to the visit they came from
  $("ho-left-out").textContent = "";
  $("ho-long").hidden = true;
  $("ho-apply").disabled = true;                  // until this meeting's options have loaded
  layoutHandoff(null);                            // no assistant until this visit's preview says
  handoffState.loads = (handoffState.loads || 0) + 1;
  setHandoffButtons();
  handoffState.templates = await attempt(() => call("handoff_templates")) || [];
  if (!handoffState.templates.some((t) => t.id === handoffState.template))
    handoffState.template = "minutes";
  renderTemplateSelect();
  closeTemplateEditor();
  $("ho-meeting").textContent = state.detail ? state.detail.title : "Meeting";
  $("ho-status").textContent = "";
  $("ho-anon").checked = handoffState.anonymise;
  show("handoff");
  $("ho-close").focus();
  await renderHandoffOptions();
  await renderHandoffPreview();
}

function closeHandoff() {
  closeTemplateEditor();
  show("meeting");
  $("m-primary").focus();
}

function renderTemplateSelect() {
  const sel = $("ho-template");
  sel.replaceChildren(...handoffState.templates.map((t) =>
    el("option", { value: t.id, textContent: `${t.title} (v${t.version}${t.edited ? ", edited" : ""})` })));
  sel.value = handoffState.template;
}

function setHandoffButtons() {
  const ready = !!handoffState.preview && !handoffState.dirty;
  for (const id of ["ho-open", "ho-copy", "ho-copy-prompt", "ho-save"]) $(id).disabled = !ready;
  $("ho-dirty").hidden = !handoffState.dirty;
}

async function renderHandoffPreview() {
  const generation = ++handoffState.generation;
  handoffState.preview = null;
  setHandoffButtons();
  let p;
  try { p = await call("handoff_preview", state.mid, handoffState.template, handoffState.anonymise); }
  catch (err) {
    if (generation !== handoffState.generation) return;
    $("ho-preview").textContent = "";
    handoffFailed(err, "ho-preview-pane");
    return;
  }
  if (generation !== handoffState.generation) return;
  clearError($("ho-preview-pane"));
  handoffState.preview = p;
  layoutHandoff(p.assistant);
  setHandoffButtons();
  $("ho-preview").textContent = p.combined;
  const total = handoffState.total;
  const d = state.detail;
  const parts = [d ? lengthWord(d.duration_s) : null,
                 total ? `${total - p.omitted} of ${plural(total, "passage")}` : null,
                 p.anonymised ? "names replaced" : null,
                 p.redactions ? plural(p.redactions, "redaction") : null];
  $("ho-info").textContent = joinParts(...parts);
  $("ho-long").hidden = !p.long;
}

async function renderHandoffOptions() {
  // A load for a meeting the user has since left must not fill this page.
  const load = handoffState.loads, mid = state.mid;
  const stale = () => load !== handoffState.loads || mid !== state.mid;
  const p = await attempt(() => call("handoff_preview", mid, handoffState.template, handoffState.anonymise));
  if (!p || stale()) return;
  handoffState.dirty = false;
  $("ho-anon").checked = handoffState.anonymise;    // a tick made while loading is not applied
  setHandoffButtons();
  $("ho-redactions").value = p.options.redactions.join("\n");
  const omitted = new Set(p.options.omitted);
  const rows = [];
  let start = 0, total = 0;
  do {
    const page = await attempt(() => call("transcript", mid, start, 2000));
    if (stale()) return;
    if (!page || !page.items.length) break;
    total = page.total;
    for (const it of page.items)
      rows.push(el("li", {}, el("label", { title: `${it.who ? it.who + ": " : ""}${it.text}` },
        el("input", { type: "checkbox", value: it.id, checked: omitted.has(it.id),
                      "aria-label": `Leave out ${fmtTime(it.t)} ${it.text}${it.who ? ` (${it.who})` : ""}` }),
        el("span", { class: "t-mark", textContent: fmtTime(it.t) }),
        el("span", { class: "p-text", textContent: it.text }))));
    start += page.items.length;
  } while (start < total);
  handoffState.total = total;
  $("ho-passages").replaceChildren(...rows);
  $("ho-apply").disabled = false;
  countLeftOut();
}

function countLeftOut() {
  const n = document.querySelectorAll("#ho-passages input:checked").length;
  $("ho-left-out").textContent = n ? `· ${n} left out` : "";
}

async function applyHandoffOptions() {
  const omitted = [...document.querySelectorAll("#ho-passages input:checked")].map((i) => Number(i.value));
  const redactions = $("ho-redactions").value.split("\n").map((s) => s.trim()).filter(Boolean);
  await attempt(async () => {
    await call("set_handoff_options", state.mid, omitted, redactions);
    handoffState.anonymise = $("ho-anon").checked;
    handoffState.dirty = false;
    sayDone("Saved. The transcript itself is unchanged.");
    await renderHandoffPreview();
  }, "ho-apply");
}

// With an assistant (the default), Copy and open is the main button and Copy
// prompt and transcript joins the other two in a row beneath it. Turned off
// by policy, and until the first preview says, the page is laid out as before
// #109: Copy prompt and transcript is the main button (index.html starts so).
function layoutHandoff(a) {
  const copy = $("ho-copy"), row = $("ho-row");
  $("ho-open").hidden = $("ho-open-note").hidden = !a;
  if (a) {
    $("ho-open-name").textContent = a.name;
    $("ho-open-host").textContent = a.host;
    if (copy.parentNode !== row) row.prepend(copy);
  } else if (copy.parentNode === row) {
    row.before(copy);
  }
  copy.classList.toggle("primary", !a);
  copy.classList.toggle("wide-btn", !a);
  row.classList.toggle("ho-three", !!a);
}

function clearsSentence(r) {
  return r.clears_in_minutes
    ? ` Your organisation clears it from the clipboard after ${r.clears_in_minutes} minute${r.clears_in_minutes === 1 ? "" : "s"} or when you close the app.`
    : "";
}

// A result: kept on the page for a screen reader (#ho-status) and announced
// once, by the toast; it clears an earlier error beside Copy or save.
function handoffSaid(message, kind = "success") {
  $("ho-status").textContent = message;
  clearError($("ho-out"));
  say(message, { kind });
}
// An error stays beside its action (#416): Copy or save, or the preview.
function handoffFailed(err, where = "ho-out") {
  const message = String((err && err.message) || err);
  if (err && err.kind === "info") { handoffSaid(message, "info"); return; }
  $("ho-status").textContent = "";
  showError($(where), message, document.activeElement);   // Dismiss returns to the button
}

async function copyHandoff(kind) {
  let r;
  const p = handoffState.preview;
  if (!p || handoffState.dirty) return;
  try { r = await call("copy_handoff", state.mid, handoffState.template, kind, p.digests[kind], handoffState.anonymise); }
  catch (err) { handoffFailed(err); return; }
  const clears = clearsSentence(r);
  handoffSaid((kind === "combined"
    ? `Copied the prompt and transcript (${r.chars.toLocaleString("en-NZ")} characters). Paste it into your organisation's approved AI assistant.` +
      (r.long ? " It is long: if the assistant cuts it off, use Save Markdown and Copy prompt only instead." : "")
    : "Copied the prompt only. Attach the saved Markdown file in your approved AI assistant, then paste the prompt.") + clears);
}

// The app copies first and opens the browser only if the copy worked. It
// cannot tell whether a browser appeared, so the message says what to do if not.
async function copyAndOpen() {
  let r;
  const p = handoffState.preview;
  if (!p || handoffState.dirty) return;
  try { r = await call("copy_and_open_assistant", state.mid, handoffState.template, p.digests.combined, handoffState.anonymise); }
  catch (err) { handoffFailed(err); return; }
  // The app read the policy again at the click: show the name and host it used.
  if (handoffState.preview === p) {
    p.assistant = { name: r.name, host: r.host };
    layoutHandoff(p.assistant);
  }
  const chars = r.chars.toLocaleString("en-NZ");
  const long = r.long ? ` It is long: if ${r.name} cuts it off, use Save Markdown and Copy prompt only instead.` : "";
  handoffSaid((r.opened
    ? `Copied the prompt and transcript (${chars} characters) and opened ${r.name} in your browser. Sign in with your organisation's business account, paste with Ctrl+V, check it, then send. If no browser appears, open ${r.host} yourself.`
    : `Copied the prompt and transcript (${chars} characters), but Unminute could not open your browser. Open ${r.host} yourself, sign in with your organisation's business account and paste.`)
    + long + clearsSentence(r), r.opened ? "success" : "info");   // half done is not a success
}

async function saveHandoff() {
  let r;
  const p = handoffState.preview;
  if (!p || handoffState.dirty) return;
  try { r = await call("save_handoff", state.mid, handoffState.template, p.digests.markdown, handoffState.anonymise); }
  catch (err) { handoffFailed(err); return; }
  if (r) handoffSaid(`Saved ${r.name}. The app keeps no record of this file; file or delete it yourself when you no longer need it.`);
}

// Editing a template replaces the preview with the editor.
function editTemplate(isNew) {
  const t = handoffState.templates.find((x) => x.id === handoffState.template);
  if (!t && !isNew) return;
  $("ho-editor").hidden = false;
  $("ho-preview-pane").hidden = true;
  $("ho-editor").dataset.id = isNew ? "" : t.id;
  $("ho-editor-title").textContent = isNew ? "New template" : "Edit template";
  $("ho-t-title").value = isNew ? "" : t.title;
  $("ho-t-prompt").value = isNew ? (t ? t.prompt : "") : t.prompt;
  $("ho-t-reset").textContent = !isNew && t.builtin ? "Restore original wording" : "Delete template";
  $("ho-t-reset").hidden = isNew || (t.builtin && !t.edited);
  $("ho-t-title").focus();
}

function closeTemplateEditor() {
  clearError($("ho-editor"));
  $("ho-editor").hidden = true;
  $("ho-preview-pane").hidden = false;
}

async function saveTemplate() {
  const id = $("ho-editor").dataset.id || null;
  const saved = await attempt(() => call("save_template", id, $("ho-t-title").value, $("ho-t-prompt").value),
                              "ho-t-save");
  if (!saved) return;
  handoffState.template = saved;
  handoffState.templates = await attempt(() => call("handoff_templates")) || handoffState.templates;
  renderTemplateSelect();
  closeTemplateEditor();
  sayDone("Template saved as a new version");
  renderHandoffPreview();
}

async function resetTemplate() {
  let reset = false;                               // a refusal stays in the editor, beside it
  await attempt(async () => { await call("reset_template", handoffState.template); reset = true; }, "ho-t-reset");
  if (!reset) return;
  handoffState.templates = await attempt(() => call("handoff_templates")) || handoffState.templates;
  if (!handoffState.templates.some((t) => t.id === handoffState.template)) handoffState.template = "minutes";
  renderTemplateSelect();
  closeTemplateEditor();
  renderHandoffPreview();
}

function wireHandoff() {
  $("ho-template").addEventListener("change", () => {
    handoffState.template = $("ho-template").value;
    closeTemplateEditor();
    renderHandoffPreview();
  });
  $("ho-edit").addEventListener("click", () => editTemplate(false));
  $("ho-new").addEventListener("click", () => editTemplate(true));
  $("ho-t-save").addEventListener("click", saveTemplate);
  $("ho-t-cancel").addEventListener("click", closeTemplateEditor);
  $("ho-t-reset").addEventListener("click", resetTemplate);
  $("ho-apply").addEventListener("click", applyHandoffOptions);
  const markDirty = () => { handoffState.dirty = true; setHandoffButtons(); };
  $("ho-redactions").addEventListener("input", markDirty);
  $("ho-anon").addEventListener("change", markDirty);
  $("ho-passages").addEventListener("change", () => { countLeftOut(); markDirty(); });
  $("ho-open").addEventListener("click", copyAndOpen);
  $("ho-copy").addEventListener("click", () => copyHandoff("combined"));
  $("ho-copy-prompt").addEventListener("click", () => copyHandoff("prompt_only"));
  $("ho-save").addEventListener("click", saveHandoff);
  $("ho-close").addEventListener("click", closeHandoff);
  $("ho-final").addEventListener("click", () => { if (state.detail) confirmFinal(state.detail); });
}
