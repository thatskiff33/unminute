// tests/page_vm.js
// Runs a test's JavaScript against the page's real scripts in Node.js (#414
// step 3b). The page is several ordered classic scripts (workspace/ui,
// index.html); this loads each one, in the order given, into one vm context,
// so their top-level names are shared as they are in the browser, then runs
// the test's code from standard input as one more script in that context and
// leaves its output on standard output. tests/page_scripts.py run() passes
// the scripts in index.html's order and reads the output.
//
//   node tests/page_vm.js workspace/ui/core.js ... workspace/ui/main.js < test.js
//
// No page code is copied or rewritten: the tests call the functions the page
// ships. Only main.js runs anything as it loads (tests/test_page_scripts.py):
// it wires the page and listens for pywebview's start-up event, which never
// comes here, so nothing polls. The document and window below exist only so
// that loading can finish; they draw nothing, and keep only the listeners the
// page adds to them, which a test reads as loaded.document.listeners (and
// loaded.window.listeners) by event type, in the order added.
//
// A test replaces what a page function reaches through a global name by
// assigning it, at the test's top level or later:
//   * document, window, setTimeout, clearTimeout, setInterval, clearInterval:
//     these are declared here (let), before the page, so document = {...}
//     reaches every page function; declaring one again (const document) stops
//     the test with "already declared" rather than quietly missing the page;
//   * a page function or let: say = (text) => ...; pollTimer = null;
//   * a page const cannot be replaced (declaring it again stops the test, as
//     it would stop a page script): change what it reaches instead (document,
//     for $), or the object it holds (state.view = ...).
// Assigning a name that neither the page, this file nor the test declares (a
// stub of a function the page has since renamed or removed) fails the test
// with "is not defined", rather than creating a new global the page never
// calls while the test passes on the real function (#495). The test's code
// runs in strict mode, which stops most such assignments where they happen;
// Node.js's vm lets one that assigns a function through even then (it cannot
// tell it from a function declaration: Node.js 24, 30 Oct 2026), so a global
// the test's code added also fails the run, once that code has run and again
// as Node.js exits (for a callback or a timer).
"use strict";

const fs = require("fs");
const vm = require("vm");

// -- what loading the page needs -----------------------------------------------
// A stand-in element: every property and method main.js's wiring touches.
class StandIn {
  constructor(tag = "div", id = "") {
    this.tagName = String(tag).toUpperCase();
    this.id = id;
    this.dataset = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.textContent = "";
    this.className = "";
    this.children = [];
    this.classList = { add() {}, remove() {}, toggle() { return false; }, contains: () => false };
  }
  addEventListener() {}
  removeEventListener() {}
  setAttribute() {}
  getAttribute() { return null; }
  removeAttribute() {}
  hasAttribute() { return false; }
  append() {}
  replaceChildren() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() {}
  blur() {}
}

const elements = new Map();
const document = {
  baseURI: "file:///C:/stand-in/workspace/ui/index.html",
  hidden: false,
  body: new StandIn("body"),
  activeElement: null,
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new StandIn("div", id));
    return elements.get(id);
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement: (tag) => new StandIn(tag),
  createElementNS: (ns, tag) => new StandIn(tag),
  listeners: {},
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  removeEventListener() {},
  hasFocus: () => true,
};
const media = { matches: false, addEventListener() {}, removeEventListener() {} };
const window = {
  matchMedia: () => media,
  listeners: {},
  addEventListener: document.addEventListener,
  removeEventListener() {},
  scrollTo() {},
};

// -- the context ---------------------------------------------------------------
// Node's console, events and URL, so a test can print and build events, and
// process, so it can watch for an unhandled rejection or exit. The names a
// test may replace are declared (let) by a first script, so a replacement is
// seen by the page's functions (a property of the context's global object is
// read before a later script's own declaration of the same name). loaded
// keeps the stand-ins the page loaded with, whatever a test puts in their place.
// pageVmTestStarts marks the start of the test's code (below).
let declared = null;              // the context's globals when the test's code starts
const context = vm.createContext({
  console, process, queueMicrotask, URL, Event, EventTarget,
  host: { document, window, setTimeout, clearTimeout, setInterval, clearInterval,
          testStarts: () => { declared = new Set(globals()); } },
});
const run = (file, text) => vm.runInContext(text, context, { filename: file });
const globals = () => run("page_vm.js", "Object.getOwnPropertyNames(globalThis)");
run("page_vm.js", `let { document, window, setTimeout, clearTimeout, setInterval,
                         clearInterval } = host;
                   const loaded = Object.freeze({ document, window });
                   const pageVmTestStarts = host.testStarts;
                   delete globalThis.host;`);
for (const file of process.argv.slice(2)) run(file, fs.readFileSync(file, "utf8"));

// -- the test ------------------------------------------------------------------
// A script's own function and var declarations are made before its first
// statement runs, so the globals at pageVmTestStarts() are the page's, this
// file's and the test's own: a global added after that was made by assigning
// an undeclared name (or by globalThis.name = ..., which a test has no need of).
const failOnUndeclared = () => {
  if (declared === null) return;                // the test's code did not start
  const added = globals().filter((name) => !declared.has(name));
  for (const name of added) {
    console.error(`${name} is not defined: the test assigned a name that neither the `
                  + "page nor the test declares, so the page never sees it");
    declared.add(name);                         // reported once
  }
  if (added.length > 0) process.exitCode = 1;
};
process.on("exit", failOnUndeclared);           // an assignment in a callback or a timer
// Strict mode, and the mark, on the test's first line, so its line numbers stay its own.
run("test.js", '"use strict"; pageVmTestStarts();' + fs.readFileSync(0, "utf8"));
failOnUndeclared();
