// tests/page_vm.js
// Runs a test's JavaScript against the page's real modules in Node.js (#414
// step 3b, #488). The page is ES modules (workspace/ui): index.html loads
// main.js, which imports the others. This loads the same files as the browser
// does, from main.js through their imports, into one vm context as real
// modules (vm.SourceTextModule, so Node.js needs --experimental-vm-modules),
// evaluates them, then runs the test's code from standard input as a script in
// that context and leaves its output on standard output.
// tests/page_scripts.py run() starts it and reads the output.
//
//   node --experimental-vm-modules tests/page_vm.js [--before CODE] workspace/ui/main.js < test.js
//
// No page code is copied or rewritten: the tests call the functions the page
// ships, linked by their own imports. An import may name only a ./name.js
// file beside main.js, as the page's policy and folder allow; anything else,
// an import of a name the module does not export, or a module that does not
// parse, stops the run. Only main.js runs anything as it loads
// (tests/test_page_scripts.py): it wires the page and listens for pywebview's
// start-up event, which never comes here, so nothing polls. The document and
// window below exist only so that loading can finish; they draw nothing, and
// keep only the listeners the page adds to them, which a test reads as
// loaded.document.listeners (and loaded.window.listeners) by event type, in
// the order added.
// --before runs CODE, as a script in the same context, after those stand-ins
// exist and before the page loads: to give the window a bridge, say.
//
// A module's top-level names are its own, so a test reaches them by name
// through accessors this file puts on the context's global object, one per
// name each module declares at top level (loaded.modules lists them, by
// module, in the order the modules were evaluated). To reach a module's own
// scope, each module is evaluated with one line added after its own text,
// which changes none of its lines: import.meta.pageVm((name) => ..., ...),
// two arrow functions that read and assign a name in that scope. So a test
// replaces what a page function reaches by assigning it, at the test's top
// level or later:
//   * document, window, setTimeout, clearTimeout, setInterval, clearInterval:
//     these are declared here (let), before the page, so document = {...}
//     reaches every page function; declaring one again (const document) stops
//     the test with "already declared" rather than quietly missing the page;
//   * a page function or let: say = (text) => ...; pollTimer = null; this
//     changes the name in the module that declares it, so its own functions
//     and every module that imports it see the replacement, as in the page;
//   * a page const cannot be replaced (assigning it throws, and declaring it
//     again stops the test, as it would stop a page script): change what it
//     reaches instead (document, for $), or the object it holds
//     (state.view = ...).
// These accessors are for the test's code only. In the browser a module that
// uses another module's name without importing it throws "is not defined",
// so here an accessor used from a page module throws the same, rather than
// letting a missing import pass every test (it knows the module from the
// caller's file in the stack).
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
const path = require("path");
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

// -- the arguments ---------------------------------------------------------------
const args = process.argv.slice(2);
let before = null;
if (args[0] === "--before") {
  before = args[1];
  args.splice(0, 2);
}
if (args.length !== 1) {
  console.error("usage: node --experimental-vm-modules tests/page_vm.js [--before CODE] "
                + "workspace/ui/main.js < test.js");
  process.exit(2);
}
if (typeof vm.SourceTextModule !== "function") {
  console.error("page_vm.js needs Node.js's --experimental-vm-modules");
  process.exit(2);
}
const entry = path.resolve(args[0]);
const folder = path.dirname(entry);

// -- the context ---------------------------------------------------------------
// Node's console, events and URL, so a test can print and build events, and
// process, so it can watch for an unhandled rejection or exit. The names a
// test may replace are declared (let) by a first script; the modules read them
// as the browser's globals. loaded keeps the stand-ins the page loaded with,
// whatever a test puts in their place. pageVmTestStarts marks the start of the
// test's code (below).
let declared = null;              // the context's globals when the test's code starts
const modules = {};               // "core.js" -> its top-level names, as evaluated
const context = vm.createContext({
  console, process, queueMicrotask, URL, Event, EventTarget,
  host: { document, window, modules, setTimeout, clearTimeout, setInterval, clearInterval,
          testStarts: () => { declared = new Set(globals()); } },
});
const run = (file, text) => vm.runInContext(text, context, { filename: file });
const globals = () => run("page_vm.js", "Object.getOwnPropertyNames(globalThis)");
const STAND_INS = ["document", "window", "setTimeout", "clearTimeout", "setInterval",
                   "clearInterval", "loaded", "pageVmTestStarts"];
run("page_vm.js", `let { ${STAND_INS.slice(0, 6).join(", ")} } = host;
                   const loaded = Object.freeze({ document, window, modules: host.modules });
                   const pageVmTestStarts = host.testStarts;
                   delete globalThis.host;`);

// -- the page's modules ------------------------------------------------------------
// Each module's top-level names, as the page writes them: a declaration at the
// start of a line (function, async function, class, const, let or var, with
// each name of a const a = 1, b = 2), outside the import and export lists.
// tests/test_page_scripts.py checks this finds exactly what its own reading
// of the scripts finds.
function topLevelNames(text) {
  const names = [];
  const word = /^[A-Za-z_$][\w$]*/;
  for (const line of text.split(/\r?\n/)) {
    let m = /^(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)/.exec(line)
      || /^class\s+([A-Za-z_$][\w$]*)/.exec(line);
    if (m) { names.push(m[1]); continue; }
    m = /^(?:const|let|var)\s+/.exec(line);
    if (!m) continue;
    // const a = 1, b = [2, 3];  let x, y;  a name after each comma outside brackets
    let rest = line.slice(m[0].length), depth = 0, quote = null, expect = true;
    for (let i = 0; i < rest.length; i++) {
      const c = rest[i];
      if (quote) { if (c === "\\") i++; else if (c === quote) quote = null; continue; }
      if (expect && depth === 0) {
        const name = word.exec(rest.slice(i));
        if (!name) break;                          // a destructured declaration: not used here
        names.push(name[0]);
        i += name[0].length - 1;
        expect = false;
      } else if (c === "/" && rest[i + 1] === "/") break;
      else if ("\"'`".includes(c)) quote = c;
      else if ("([{".includes(c)) depth++;
      else if (")]}".includes(c)) depth--;
      else if (c === "," && depth === 0) {
        expect = true;
        while (rest[i + 1] === " ") i++;
      }
    }
  }
  return names;
}

// The line added after each module's own text (see the header): its two arrow
// functions read and assign a name in the module's own scope.
const HOOK = "\n;import.meta.pageVm((pageVmName) => eval(pageVmName),"
             + " (pageVmName, pageVmValue) => eval(pageVmName + \" = pageVmValue\"));\n";
const PAGE_FILE = /^\.\/([A-Za-z0-9_-]+\.js)$/;
const loading = new Map();        // "core.js" -> its module
const scopes = {};                // "core.js" -> { get, set }, once it has been evaluated
const owner = {};                 // a top-level name -> the module that declares it
function load(name) {
  if (loading.has(name)) return loading.get(name);
  const file = path.join(folder, name);
  const text = fs.readFileSync(file, "utf8");     // a missing file stops the run
  const module = new vm.SourceTextModule(text + HOOK, {
    context, identifier: file,
    initializeImportMeta(meta) {
      meta.pageVm = (get, set) => {
        scopes[name] = { get, set };
        modules[name] = Object.freeze(topLevelNames(text));
      };
    },
  });
  loading.set(name, module);
  for (const word of topLevelNames(text)) {
    if (owner[word]) throw new Error(`${word} is declared in ${owner[word]} and ${name}`);
    if (STAND_INS.includes(word)) throw new Error(`${name} declares ${word}, which page_vm.js stands in for`);
    owner[word] = name;
  }
  return module;
}

// The test's way to each top-level name: read and assigned in its own module.
// From a page module, the name is not defined (it did not import it).
const pageFiles = () => new Set([...loading.keys()].map((name) => path.join(folder, name)));
function callerFile(below) {
  const limit = Error.stackTraceLimit, prepare = Error.prepareStackTrace;
  const holder = {};
  try {
    Error.stackTraceLimit = 1;
    Error.prepareStackTrace = (_, sites) => sites;
    Error.captureStackTrace(holder, below);
    const [site] = holder.stack;
    return site ? site.getFileName() : null;
  } finally {
    Error.stackTraceLimit = limit;
    Error.prepareStackTrace = prepare;
  }
}
function reach(word) {
  const home = owner[word];
  const refuse = (from) => {
    if (pageFiles().has(from)) {
      throw new ReferenceError(`${word} is not defined: ${path.basename(from)} uses it `
                               + `without importing it from ${home}`);
    }
    if (!scopes[home]) throw new ReferenceError(`${word} is not loaded yet (${home})`);
  };
  const get = function () { refuse(callerFile(get)); return scopes[home].get(word); };
  const set = function (value) { refuse(callerFile(set)); scopes[home].set(word, value); };
  return { get, set, enumerable: true, configurable: false };
}

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

(async () => {
  const main = load(path.basename(entry));
  await main.link((specifier, from) => {
    const m = PAGE_FILE.exec(specifier);
    if (!m) {
      throw new Error(`${path.basename(from.identifier)} imports ${specifier}: a page module `
                      + "imports only ./name.js from its own folder");
    }
    return load(m[1]);
  });
  const global = run("page_vm.js", "globalThis");
  for (const word of Object.keys(owner)) Object.defineProperty(global, word, reach(word));
  if (before !== null) run("before.js", '"use strict";' + before);
  await main.evaluate();
  Object.freeze(modules);
  for (const [name, words] of Object.entries(modules)) {
    for (const word of words) scopes[name].get(word);   // a name the reading made up throws
  }
  process.on("exit", failOnUndeclared);         // an assignment in a callback or a timer
  // Strict mode, and the mark, on the test's first line, so its line numbers stay its own.
  run("test.js", '"use strict"; pageVmTestStarts();' + fs.readFileSync(0, "utf8"));
  failOnUndeclared();
})().catch((err) => {
  setImmediate(() => { throw err; });           // reported, and exit code 1, as an uncaught error
});
