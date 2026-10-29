"""Message kinds from the bridge, and errors that stay beside their action (#416).

The bridge says what a refusal is ("error" or "info", app.ERROR_KINDS) and the
page styles a message by that kind, never by its wording: the OK_MESSAGE
regular expression that matched the start of a message is gone. On Import,
Hand off and Settings (with Downloads and Setup's download steps) an error
stays next to the control that caused it until it is dismissed or that part
of the page next succeeds. The page's own functions run in Node.js with the
page's real scripts loaded and what they reach replaced (tests/page_scripts.py
run), as tests/test_hardening.py's toast and refresh tests do."""

import json
import pickle
import re
import types
import unittest
from html.parser import HTMLParser
from pathlib import Path

from workspace import app
import page_scripts

try:
    from workspace import api
    HAVE_NUMPY = True
except ImportError:                       # api imports numpy for the clip WAV
    HAVE_NUMPY = False

ROOT = Path(__file__).resolve().parents[1]
APP_JS = page_scripts.source()
INDEX = (ROOT / "workspace" / "ui" / "index.html").read_text(encoding="utf-8")


def page_functions(*names):
    """The named page functions' own text, as the page ships them."""
    return "\n".join(page_scripts.function_text(name) for name in names) + "\n"


class WorkspaceErrorKindTests(unittest.TestCase):
    def test_an_error_by_default_and_info_when_said(self):
        self.assertEqual(app.WorkspaceError("Refused.").kind, "error")
        self.assertEqual(app.WorkspaceError("Wait.", kind="info").kind, "info")
        self.assertEqual(str(app.WorkspaceError("Wait.", kind="info")), "Wait.")

    def test_an_unknown_kind_is_refused(self):
        for bad in ("success", "ok", "", None, "Error"):
            with self.subTest(kind=bad), self.assertRaises(ValueError):
                app.WorkspaceError("x", kind=bad)

    def test_it_still_pickles_with_its_message_and_kind(self):
        for kind in app.ERROR_KINDS:
            with self.subTest(kind=kind):
                again = pickle.loads(pickle.dumps(app.WorkspaceError("Refused.", kind=kind)))
                self.assertEqual((type(again), str(again), again.kind),
                                 (app.WorkspaceError, "Refused.", kind))

    def test_the_kinds_are_the_pages(self):
        page = page_scripts.run("console.log(JSON.stringify(MESSAGE_KINDS));")
        self.assertIsInstance(page, list, "MESSAGE_KINDS is not a list in the page scripts")
        self.assertEqual({"success"} | set(app.ERROR_KINDS), set(page))


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class BridgeKindTests(unittest.TestCase):
    """Every refusal the bridge sends carries a kind (I3: the return shape)."""

    def page(self, **workspace):
        ws = types.SimpleNamespace(log=None, locked=None, finish_only=False, **workspace)
        return api.make_api(ws)

    def test_a_refusal_says_its_kind(self):
        def refused(mid):
            raise app.WorkspaceError("Meeting not found.")

        def waiting(mid):
            raise app.WorkspaceError("Already opening.", kind="info")
        self.assertEqual(self.page(detail=refused).detail("m"),
                         {"ok": False, "error": "Meeting not found.", "kind": "error"})
        self.assertEqual(self.page(detail=waiting).detail("m"),
                         {"ok": False, "error": "Already opening.", "kind": "info"})

    def test_an_unexpected_failure_is_an_error_with_its_code_only(self):
        def broken(mid):
            raise RuntimeError("C:\\Users\\someone\\secret.wav")
        r = self.page(detail=broken).detail("m")
        self.assertEqual((r["ok"], r["kind"]), (False, "error"))
        self.assertRegex(r["error"], r"^Something went wrong \(code [^)]+\)\. Please try again\.$")
        self.assertNotIn("secret", json.dumps(r))

    def test_a_kind_the_page_does_not_know_is_sent_as_an_error(self):
        error = app.WorkspaceError("Refused.")
        error.kind = "success"                # set after the check: never passed on

        def refused(mid):
            raise error
        self.assertEqual(self.page(detail=refused).detail("m")["kind"], "error")

    def test_success_is_unchanged(self):
        self.assertEqual(self.page(detail=lambda mid: {"id": mid}).detail("m"),
                         {"ok": True, "value": {"id": "m"}})

    def test_the_gates_say_their_kind(self):
        ws = types.SimpleNamespace(log=None, locked=RuntimeError("locked"), finish_only=False)
        self.assertEqual(api.make_api(ws).detail("m"),
                         {"ok": False, "error": app.LOCKED_TEXT, "kind": "error"})
        ws = types.SimpleNamespace(log=None, locked=None, finish_only=True)
        self.assertEqual(api.make_api(ws).scan_now(),
                         {"ok": False, "error": app.FINISH_ONLY_TEXT, "kind": "info"})

    def test_no_method_is_added(self):
        self.assertNotIn("kind", api.API_METHODS)
        self.assertNotIn("message_kind", api.API_METHODS)


CALL_CASES = r"""
window = { pywebview: { api: {} } };
const out = {};
(async () => {
  for (const [name, reply] of [["info", { ok: false, error: "Wait.", kind: "info" }],
                               ["error", { ok: false, error: "Refused.", kind: "error" }],
                               ["none", { ok: false, error: "Refused." }],
                               ["unknown", { ok: false, error: "Refused.", kind: "success" }],
                               ["empty", null]]) {
    window.pywebview.api.m = async () => reply;
    try { await call("m"); out[name] = "no error"; }
    catch (err) { out[name] = [err.message, err.kind]; }
  }
  window.pywebview.api.m = async () => ({ ok: true, value: 7 });
  out.ok = await call("m");
  console.log(JSON.stringify(out));
})();
"""

SAY_CASES = r"""
const toggles = {};
const node = { _text: "", dataset: {}, classList: { toggle(c, on) { toggles[c] = on; } },
               get textContent() { return this._text; },
               set textContent(v) { this._text = String(v); } };
document = { getElementById: () => node };
state.statusLine = "";
let delay = null;
setTimeout = (fn, ms) => { delay = ms; return 1; };
clearTimeout = () => {};
const step = (message, opts) => { delay = null; say(message, opts); return { ...toggles, delay }; };
console.log(JSON.stringify({
  success: step("Saved", { kind: "success" }),
  info: step("Checking watched folders"),
  error: step("Saved nothing: the disk is full.", { kind: "error" }),
  unknown: step("Saved", { kind: "ok" }),
  cleared: step("", { kind: "error" }),
}));
"""

ATTEMPT_CASES = r"""
const said = [], shown = [], cleared = [];
const homes = { "s-scan": "card", "ho-out": "step" };
document = { getElementById: (id) => ({ id }) };
errorHome = (near) => homes[typeof near === "string" ? near : near.id] || null;
showError = (home, message) => shown.push([home, message]);
clearError = (home) => cleared.push(home);
say = (message, opts = {}) => said.push([message, opts.kind || "info"]);
const fail = (message, kind) => async () => {
  const e = new Error(message);
  if (kind) e.kind = kind;
  throw e;
};
(async () => {
  await attempt(fail("Refused.", "error"), "s-scan");
  await attempt(fail("Wait.", "info"), "s-scan");
  await attempt(fail("Refused."), null);
  await attempt(async () => { throw new TypeError("x is undefined"); }, "s-scan");
  const value = await attempt(async () => 5, "s-scan");
  await attempt(async () => 6);
  console.log(JSON.stringify({ said, shown, cleared, value }));
})();
"""

HANDOFF_CASES = r"""
const nodes = {};
document = { activeElement: { id: "ho-copy" },
             getElementById: (id) => (nodes[id] = nodes[id] || { id, textContent: "" }) };
const said = [], shown = [], cleared = [];
say = (message, opts) => said.push([message, opts.kind]);
showError = (home, message, back) => shown.push([home.id, message, back.id]);
clearError = (home) => cleared.push(home.id);
const err = (message, kind) => Object.assign(new Error(message), kind ? { kind } : {});
handoffFailed(err("The handoff changed since the preview."));
const afterError = $("ho-status").textContent;
handoffFailed(err("Already opening.", "info"));
const afterInfo = $("ho-status").textContent;
handoffFailed(err("Not found."), "ho-preview-pane");
handoffSaid("Copied the prompt only.");
console.log(JSON.stringify({ said, shown, cleared, afterError, afterInfo,
                             status: $("ho-status").textContent }));
"""

# A small stand-in for the DOM, enough for showError and clearError: elements
# with children, classes, attributes, closest("[data-errors]") and the three
# selectors the page uses.
FAKE_DOM = r"""
class Node {
  constructor(tag, props = {}) {
    this.tag = tag; this.children = []; this.parent = null; this.attrs = {};
    this._text = ""; this.className = ""; this.disabled = false; this.focused = 0;
    this.listeners = {}; this.writes = 0;
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") this.className = v;
      else if (k === "on") this.listeners = v;
      else if (k === "textContent") this._text = v;
      else this.attrs[k] = v;
    }
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.writes += 1; this._text = String(v); this.children = []; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === root; }
  has(cls) { return this.className.split(" ").includes(cls); }
  append(...kids) { for (const k of kids) { k.parent = this; this.children.push(k); } }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }
  closest(sel) {
    for (let n = this; n; n = n.parent)
      if (sel === "[data-errors]" && "data-errors" in n.attrs) return n;
    return null;
  }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelector(sel) {
    const has = (cls) => (n) => n.has(cls);
    if (sel === ":scope .error-slot") return this.all().find(has("error-slot")) || null;
    if (sel === ":scope > .inline-error") return this.children.find(has("inline-error")) || null;
    if (sel === ".inline-error-text") return this.all().find(has("inline-error-text")) || null;
    throw new Error("selector not faked: " + sel);
  }
  focus() { this.focused += 1; }
  click() { this.listeners.click(); }
}
const root = new Node("body");
el = (tag, props = {}, ...kids) => {
  const n = new Node(tag, props);
  n.append(...kids.filter(Boolean));
  return n;
};
icon = () => new Node("svg");
const said = [];
say = (message, opts) => said.push([message, opts.kind]);
"""

SHOW_CASES = FAKE_DOM + r"""
const card = el("section", { "data-errors": "" });
const first = el("button"), other = el("button");
card.append(first, other);
const step = el("li", { "data-errors": "" });
const slot = el("div", { class: "error-slot" });
step.append(el("pre"), slot, el("div"));
root.append(card, step);
const boxes = (n) => n.all().filter((c) => c.has("inline-error"));
const text = (n) => boxes(n).map((b) => b.querySelector(".inline-error-text").textContent);
const out = {};
showError(card, "Refused.", first);
out.first = { texts: text(card), role: boxes(card)[0].attrs.role,
              last: card.children.at(-1).has("inline-error") };
const span = boxes(card)[0].querySelector(".inline-error-text");
const writes = span.writes;
showError(card, "Refused.", first);
out.same = { count: boxes(card).length, writes: span.writes - writes };
showError(card, "Refused again.", first);
out.changed = { count: boxes(card).length, texts: text(card) };
showError(card, "Refused again.", other);     // the same error, now from another control
showError(step, "Not found.");
out.slot = { inSlot: text(slot),
             stepDirect: step.children.filter((c) => c.has("inline-error")).length };
const dismiss = boxes(card)[0].children.find((c) => c.tag === "button");
dismiss.click();
out.dismissed = { count: boxes(card).length, focused: [first.focused, other.focused],
                  label: dismiss._text };
clearError(step);
out.cleared = boxes(step).length;
clearError(null);
showError(null, "Nowhere to put it.");
out.toasted = said;
console.log(JSON.stringify(out));
"""


class PageKindTests(unittest.TestCase):
    def test_no_wording_decides_how_a_message_looks(self):
        self.assertNotIn("OK_MESSAGE", APP_JS)
        say = page_functions("say")
        self.assertNotRegex(say, r"\.test\(", "say() matches the wording of a message")
        self.assertIn('kind = "info"', say)

    def test_call_keeps_the_bridges_kind_and_treats_anything_else_as_an_error(self):
        out = page_scripts.run(CALL_CASES)
        self.assertEqual(out["info"], ["Wait.", "info"])
        self.assertEqual(out["error"], ["Refused.", "error"])
        self.assertEqual(out["none"], ["Refused.", "error"])
        self.assertEqual(out["unknown"], ["Refused.", "error"])
        self.assertEqual(out["empty"], ["The request failed.", "error"])
        self.assertEqual(out["ok"], 7)

    def test_the_toast_is_styled_by_kind(self):
        out = page_scripts.run(SAY_CASES)
        self.assertEqual((out["success"]["ok"], out["success"]["error"], out["success"]["delay"]),
                         (True, False, 3000))
        self.assertEqual((out["info"]["ok"], out["info"]["error"], out["info"]["delay"]),
                         (False, False, 8000))
        # The same wording as a success, as an error: the kind decides.
        self.assertEqual((out["error"]["ok"], out["error"]["error"], out["error"]["delay"]),
                         (False, True, 8000))
        self.assertEqual((out["unknown"]["ok"], out["unknown"]["error"]), (False, False))
        self.assertEqual((out["cleared"]["ok"], out["cleared"]["error"], out["cleared"]["delay"]),
                         (False, False, None))

    def test_attempt_puts_an_error_beside_its_action(self):
        out = page_scripts.run(ATTEMPT_CASES)
        self.assertEqual(out["shown"], [["card", "Refused."], ["card", "x is undefined"]])
        # Info is toasted wherever it came from; without a place, an error is too.
        self.assertEqual(out["said"], [["Wait.", "info"], ["Refused.", "error"]])
        self.assertEqual(out["value"], 5)
        self.assertEqual(out["cleared"], ["card", None], "a success clears the error in its place")

    def test_hand_off_keeps_an_error_beside_copy_or_save(self):
        out = page_scripts.run(HANDOFF_CASES)
        self.assertEqual(out["shown"],
                         [["ho-out", "The handoff changed since the preview.", "ho-copy"],
                          ["ho-preview-pane", "Not found.", "ho-copy"]])
        self.assertEqual(out["afterError"], "", "an error is not left as the last result")
        self.assertEqual(out["afterInfo"], "Already opening.")
        self.assertEqual(out["said"], [["Already opening.", "info"],
                                       ["Copied the prompt only.", "success"]])
        self.assertEqual(out["cleared"], ["ho-out", "ho-out"])
        self.assertEqual(out["status"], "Copied the prompt only.")

    def test_an_error_stays_until_dismissed_and_is_set_only_when_it_changes(self):
        out = page_scripts.run(SHOW_CASES)
        self.assertEqual(out["first"], {"texts": ["Refused."], "role": "alert", "last": True})
        self.assertEqual(out["same"], {"count": 1, "writes": 0},
                         "the same error again must leave the alert alone (#404, #346)")
        self.assertEqual(out["changed"], {"count": 1, "texts": ["Refused again."]})
        self.assertEqual(out["slot"], {"inSlot": ["Not found."], "stepDirect": 0})
        # Dismiss returns focus to the control of the latest error in that place.
        self.assertEqual(out["dismissed"], {"count": 0, "focused": [0, 1], "label": "Dismiss"})
        self.assertEqual(out["cleared"], 0)
        self.assertEqual(out["toasted"], [["Nowhere to put it.", "error"]])


class _Ancestry(HTMLParser):
    """Each id in index.html, and whether it or an ancestor has data-errors."""
    VOID = {"input", "img", "br", "meta", "link", "hr", "source", "col", "wbr", "area", "base"}

    def __init__(self):
        super().__init__()
        self.stack, self.homed = [], {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        homed = "data-errors" in attrs or bool(self.stack and self.stack[-1][1])
        if "id" in attrs:
            self.homed[attrs["id"]] = homed
        if tag not in self.VOID:
            self.stack.append((tag, homed))

    def handle_startendtag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.homed[attrs["id"]] = "data-errors" in attrs or bool(
                self.stack and self.stack[-1][1])

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


class ErrorPlaceTests(unittest.TestCase):
    """Every action the page names as an error's place sits in a [data-errors]
    container of index.html, so its error cannot fall back to the toast."""

    @classmethod
    def setUpClass(cls):
        parser = _Ancestry()
        parser.feed(INDEX)
        cls.homed = parser.homed

    def test_import_hand_off_and_settings_have_places(self):
        for name in ("import", "ho-apply", "ho-copy", "ho-save", "ho-open", "ho-preview",
                     "ho-t-save", "ho-t-reset", "s-scan", "s-add-folder", "s-folders",
                     "s-model", "s-microphone", "dl-list", "dl-remove-token", "s-token",
                     "s-download-speakers", "s-use-remembered", "setup-cards", "su-model"):
            with self.subTest(element=name):
                self.assertTrue(self.homed.get(name), f"#{name} has no [data-errors] place")

    def test_every_settings_card_is_a_place(self):
        cards = re.findall(r'<section class="set-card"[^>]*>', INDEX)
        self.assertGreater(len(cards), 4)
        for card in cards:
            self.assertIn("data-errors", card)

    def test_every_place_named_in_the_page_exists(self):
        named = set(re.findall(r'\}, "([\w-]+)"\)', APP_JS))
        named |= set(re.findall(r'setSetting\(\{[^}]*\}, "([\w-]+)"\)', APP_JS))
        named |= set(re.findall(r'handoffFailed\(err, "([\w-]+)"\)', APP_JS))
        named |= set(re.findall(r'attempt\(\(\) => call\([^\n]*?\), "([\w-]+)"\)', APP_JS))
        self.assertGreater(len(named), 12, "the patterns no longer find the page's places")
        for name in sorted(named):
            with self.subTest(element=name):
                self.assertTrue(self.homed.get(name), f"#{name} has no [data-errors] place")


def call_args(source, name):
    """The arguments of each call of name( in source (not its definition), as
    source text split at the call's own commas. Strings, template literals,
    line comments and nested brackets are skipped over; a call that does not
    close fails."""
    found = []
    for m in re.finditer(r"(?<![\w.])" + re.escape(name) + r"\(", source):
        if source[max(0, m.start() - 9):m.start()] == "function ":
            continue
        stack, args, start, i = ["("], [], m.end(), m.end()
        while stack:
            if i >= len(source):
                raise AssertionError(f"a call of {name}( at {m.start()} does not close")
            ch, top = source[i], stack[-1]
            if top == "`":                                 # inside a template literal
                if ch == "\\":
                    i += 1
                elif ch == "`":
                    stack.pop()
                elif source.startswith("${", i):
                    stack.append("{")
                    i += 1
            elif source.startswith("//", i):              # a comment: to the end of its line
                i = source.index("\n", i)
                continue
            elif ch in "'\"":                               # a string: to its closing quote
                i += 1
                while source[i] != ch:
                    i += 2 if source[i] == "\\" else 1
            elif ch in "`([{":
                stack.append(ch)
            elif ch in ")]}":
                stack.pop()
                if not stack:
                    args.append(source[start:i].strip())
            elif ch == "," and len(stack) == 1:
                args.append(source[start:i].strip())
                start = i + 1
            i += 1
        found.append([a for a in args if a])
    return found


# The bridge methods of the download steps: Setup's steps 2 and 3 and the
# Downloads page. An error from any of them stays beside the control used.
DOWNLOAD_METHODS = ("download_model", "cancel_download", "remove_model", "verify_model",
                    "remove_token")


class DownloadPlaceTests(unittest.TestCase):
    """Every download action on Setup and Downloads passes the control that
    asked, so a refusal stays beside it rather than in the toast (#473 review:
    Setup step 2's own Download, Repair and Cancel passed none)."""

    def test_every_start_download_passes_its_control(self):
        calls = call_args(APP_JS, "startDownload")
        self.assertGreaterEqual(len(calls), 4, "the page's startDownload calls were not found")
        for args in calls:
            with self.subTest(call=", ".join(args)):
                self.assertEqual(len(args), 2, "startDownload without a place")
                self.assertRegex(args[1], r"\bnear:", "startDownload without a place")

    def test_every_attempt_on_the_download_cards_passes_a_place(self):
        cards = page_functions("cancelQueued", "modelCard", "downloadRow", "renderDownloads",
                               "startDownload")
        calls = call_args(cards, "attempt")
        self.assertGreaterEqual(len(calls), 6, "the download cards' attempt calls were not found")
        for args in calls:
            with self.subTest(call=args[0][:60]):
                self.assertEqual(len(args), 2, "attempt without a place")
                self.assertNotEqual(args[1], "null", "attempt without a place")

    def test_every_download_method_is_attempted_with_a_place(self):
        named = 0
        for args in call_args(APP_JS, "attempt"):
            if not any(f'"{method}"' in args[0] for method in DOWNLOAD_METHODS):
                continue
            named += 1
            with self.subTest(call=args[0][:60]):
                self.assertEqual(len(args), 2, "a download action without a place")
                self.assertNotEqual(args[1], "null", "a download action without a place")
        self.assertGreaterEqual(named, 6, "the page's download actions were not found")


if __name__ == "__main__":
    unittest.main()
