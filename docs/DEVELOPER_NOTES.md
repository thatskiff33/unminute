# Developer notes: tooling traps and flaky checks

Practical lessons from working on this repo, for people and agents. The rules themselves are
in [AGENTS.md](../AGENTS.md); this page covers how tools behave. Add a note when a trap costs
real time, and remove it when the cause is fixed.

## Writing files

- **Escapes in agent file tools.** An agent's file-writing tool can turn a backslash-u escape
  sequence into the real character. That has put a right-to-left override, byte order marks and
  line separators into source files. Build such strings with `chr()`, or check the written file.
  `tests/test_source_text.py` fails on any invisible or bidirectional character in tracked files.
- **Shell heredocs.** They collapse backslashes and once inserted a NUL byte. Write content that
  contains backslashes (Windows paths, regular expressions, registry keys) with a file tool or a
  Python script, then check `git diff` for NUL bytes.
- **Patch scripts.** Write them to a file and run them, rather than passing them inline.

## Python and processes

- **Spawned workers re-run your script.** Workers use multiprocessing spawn, so an ad-hoc script
  that builds a `Workspace` must put everything under `if __name__ == "__main__":`. Otherwise
  each worker re-runs it: one demo opened duplicate windows and leaked temporary folders.
- **Run tests through `tools/run_tests.py`.** It puts `tests/` on the import path, which spawned
  workers need for `tests/fake_worker_engines.py`. CI runs it in chunks (`--chunk K/N`, #386;
  on a pull request, `--chunk K/N --only <the selected tests>`, #500),
  each a separate process on its own machine, so a test that only passes after another class
  has run in the same process can fail there: reproduce with the same `--chunk`. When the
  chunks drift apart in time, refresh `tools/test_durations.json` with `--update-durations`
  and the chunks' saved CI logs.
- **Tests never download a model (#382).** The runner replaces `setup._download_main`, the code
  the download process runs, with `download_guard.REFUSED` (`tests/download_guard.py`) for the
  whole run. A `Downloads` or `Workspace` built without a fake target is refused when its
  download process starts, before the process is sent anything; the test it happened in
  fails with an error naming the cause, even when the error came from a thread or was
  swallowed. A test that patches `setup._download_main` and does not put it back fails too.
  Fake the download process with a `Downloads` target or a patch from
  `tests/fake_worker_engines.py`. A test of the real download process itself, with no network,
  opts in with `with download_guard.real_download_process():`. The refused download process
  prints an `EOFError` of its own: the refusal comes after Windows has started it.
- **Join workers before removing a test's folder.** A worker that is still ending holds its
  `logs\worker.crash` open, and Windows will not delete an open file, so a tear-down that removes
  the folder first fails with `WinError 32` (#278). After closing a `Workspace` or `WorkerHandle`,
  call `join_workers(self)` from `tests/worker_exit.py`, then remove the folder.
- **Tests pass the test key protector.** Since schema 2 (#111 C2b-2) a `Store` or `Workspace`
  built without `protector=keyprotect.TestProtector()` uses Windows Data Protection: it works
  on Windows but raises `KeyUnavailable` on Linux CI, and the two cannot open each other's
  workspaces. The test protector is refused under the real workspace folder. Never use it in
  `workspace/` or `tools/` (`tests/test_content_access.py` checks). That test also fails on a
  `Store(...)` or `Workspace(...)` in `tests/` without `protector=` (or `**kw` after
  `kw.setdefault("protector", ...)`), apart from the tests in `REAL_PROTECTOR_TESTS`: a test
  that leaves it out passes on Windows through real DPAPI, which hides the gap. A raw SQL
  write to a content column in a test needs the sealed name and a value from `store._seal`.
- **Stale bytecode.** An edit and restore of the same size within one second can leave a stale
  `.pyc` that Python keeps using. Use `python -B`, or delete `__pycache__` with Python.
  `tools/run_tests.py` writes no bytecode, passes that on to every process a test starts
  (`PYTHONDONTWRITEBYTECODE`; a child started with `-I` ignores it, so it needs `-B`), and
  fails when a run left a new or changed `.pyc` in the checkout, naming the files without
  deleting them (#206). `tools/evidence.py --zip` refuses a checkout whose `tools/`, `tests/`
  or `docs/` hold them.
- **Dead-code report.** `python -m vulture workspace tools/vulture_whitelist.py
  --min-confidence 60` runs what CI's report runs (install vulture
  from `requirements/ci-vulture.txt`). Most hits are .NET or ctypes attributes set by name;
  add one to the whitelist only with its reason. Remove anything that is really unused.
- **Type check (report only, #413).** `python tools/type_check.py` runs what CI's "Type check
  (report only)" job runs: mypy over `workspace/` with `mypy.ini` (install mypy from
  `requirements/ci-mypy.txt`), compared with the committed baseline
  `tools/mypy_baseline.txt`. It lists errors new since the baseline and ones fixed since; CI
  shows the new ones as warnings on the pull request and never fails on them. Fix a new
  error rather than add it. When a change fixes errors, run `python tools/type_check.py
  --update` and commit the shorter baseline; adding an error to it needs its reason in the
  pull request. The baseline has no line numbers, so moving code leaves it alone. mypy reads
  no installed package (`no_site_packages`: third-party imports are typed `Any`) and checks
  the Windows branches (`platform = win32`), so every machine gets the same result, and it
  writes no cache. Making it a gate needs the owner. The page's scripts have their own
  check, below.
- **The page's type check (report only, #489; the owner's decision DEC-089).**
  `python tools/ts_check.py` runs what the same CI job runs next: TypeScript's checker over
  `workspace/ui/*.js` with `tsconfig.json`, compared with the committed baseline
  `tools/ts_baseline.txt`, reported and updated as the mypy check is. Install TypeScript
  first with `npm ci --ignore-scripts --no-audit --no-fund` (Node.js on the PATH): it is
  pinned by exact version and integrity hash in `package.json` and `package-lock.json`,
  goes into `node_modules/` (ignored by git) and is a development tool only, under the
  Apache License 2.0, never in the package. `.npmrc` turns off install scripts, the audit
  and funding calls and the update check. Only scripts with a `// @ts-check` line before
  `"use strict";` are checked (`checkJs` is off); the rest are still read, as the checked
  ones import from them, so a name a checked module uses without importing it is reported
  too. Type notes are JSDoc comments only, so the shipped code does not
  change; `tools/page_globals.d.ts` declares what the page uses but does not declare (the
  pywebview bridge). To opt a script in, add the line, run `python tools/ts_check.py
  --update` and commit the baseline; fix an error rather than add it where that is cheap.
  A new major TypeScript version is a pull request of its own (Dependabot proposes minor
  and patch versions only).
- **Quality report (report only, #365).** `python tools/quality_report.py` runs what CI's
  "Quality report (report only)" job runs: the line count of each tracked code file and its
  growth since the committed baseline `tools/quality_baseline.json`, the largest Python
  modules, and ruff's C901 complexity (threshold 10, `--exit-zero`; ruff from
  `requirements/ci.txt`). CI shows a function new over the threshold, or more complex than in
  the baseline, as a warning on the pull request and never fails on it. Split or simplify
  rather than accept growth. Run `python tools/quality_report.py --update` and commit the
  baseline in a pull request that shrinks a module or simplifies a function, or at a release;
  a refresh that takes in growth or new complexity says why in the pull request. Making it a
  gate needs the owner.
- **Security lint (report only, #415).** `python tools/security_lint.py` runs ruff's S rules
  (flake8-bandit, `--exit-zero`; ruff from `requirements/ci.txt`) and compares them with
  the triaged baseline `tools/security_baseline.json`: for each file and rule, how many
  findings are accepted and why. CI runs it on every pull request in the "Quality report
  (report only)" job and shows a file and rule over the baseline as a warning, never a
  failure; the required "Ruff" check does not select S. Run it by hand too when a change
  starts processes, builds SQL, parses XML, opens URLs or swallows exceptions. Fix a new
  finding, or accept it with its reason: run `python tools/security_lint.py --update` (it
  keeps every reason and leaves a new entry's `why` empty, which the report lists as
  untriaged), write the reason in, and say why in the pull request. Making it a gate needs
  the owner.
- **Coverage (report only, #413).** `.coveragerc` measures lines and branches in `workspace/`,
  and the Python processes the tests start (`patch = subprocess`, so each process writes its
  own data file). Set `COVERAGE_FILE` to a path outside the checkout first, as CI does, then
  `python -m coverage run tools/run_tests.py --only <modules>`, `python -m coverage combine`
  and `python -m coverage report`. A child started with an environment of its own, or the
  decoder after it lowers itself to Low integrity, is not measured. There is no floor:
  that would be a gate, which needs the owner.
- **Tools drive their own version of the app.** Since R13c (#136) the tools import
  `workspace.asr_engines` and `workspace.offline_guard`, so they fail with an `ImportError`
  against a checkout or package from before that move (v0.5.0-preview.3 and earlier). To
  measure an older package, run that version's own tools; `tools/evidence.py` already refuses
  evidence whose tools come from a different commit than the package.
- **`time.monotonic()` counts from boot.** Never start a "last run" timer at 0.0, or a check
  is skipped for the first minutes after boot. Start at `float("-inf")`.
- **The decoder cannot read a development Python (#299).** The app decodes in a Windows
  AppContainer, which reads only what ALL APPLICATION PACKAGES may read: an installed app's
  `versions` folder (the installer grants it), not a development or CI Python, a virtual
  environment or a checkout. It never falls back, so a decode from a checkout is refused
  (`media.SandboxUnavailable`, step `start` or `ready`). Tests that decode call
  `decoder_runtime.use()` (`tools/decoder_runtime.py`) in `setUpModule`, and the tools that
  decode from a checkout call it too: it copies the interpreter, the standard library, numpy,
  PyAV and the imported `workspace` package into a temporary folder (about 3 s and 2,500
  files), grants ALL APPLICATION PACKAGES read on that folder only, and uses a throwaway
  AppContainer profile, removing both at exit; a run that was killed leaves its profile, which
  the next run removes (`sweep_profiles`, only once the process that made it has ended). A new
  test module or tool that decodes needs the same call. Never grant the container read on a whole checkout: `extracted/` can hold real
  content (P2). In a package's own runtime it does nothing, so release evidence uses the real
  container.
- **In the AppContainer, the decoder never opens the null device by name.** Windows Server 2025
  (the hosted CI runner) refuses `NUL` to a process in an AppContainer, though Windows 11 build
  26300 allows it, so `open(os.devnull)` there failed every decode at `ready` on CI only. The
  decoder sends stray output to the null device the app hands it as stderr (descriptor 2).
  Code that runs in the decoder opens nothing by name; it uses what it was given.
- **PyAV 17 and 8-channel planar audio.** An 8-channel planar frame reports 9 planes, and
  `to_ndarray()` on one read past them and crashed the process (found in #220; 7 and 16
  channels were fine). Convert to an interleaved format (`flt`, not `fltp`) before reading
  samples, as `workspace/decode_child.py` does.

## Page scripts

- **The page is several ES modules, not one app.js** (#414 step 3, the owner's decision
  DEC-082; modules since #488, the user's decision DEC-090). `workspace/ui/index.html` loads
  one script, `main.js`, as a module (`type="module"`), and it imports the others. Each
  module imports by name what it uses from another (`import { $, call } from "./core.js";`)
  and lists what it gives the others at its end (`export { ... };`), so every declaration
  reads as it did. Until #488 part A the window loaded the page from `file://`, where
  Chromium refuses ES modules (origin `null`); the WebView2 virtual host
  (`https://unminute.invalid/`, "WebView2 virtual host" below) allows them under the same
  `script-src 'self'`. Only `main.js` runs anything as the page loads (wiring, start-up, the
  error listeners); the others only declare. The modules import each other in cycles, which
  is safe because of that: the browser evaluates every module `main.js` reaches before
  `main.js` itself, but one module may be evaluated before another it imports from, whose
  `const` and `let` are not set until then (a function is). So no module but `main.js` reads
  an imported name outside a function body. A module's top-level `let` can be assigned only
  by that module: another calls a function of its owner (`stopPolling()` in `polling.js`).
- **Nothing is global.** The page's names are its modules' own. The window's close
  (`askClose`) and the check tools reach the page only through `window.workspace`
  (`main.js`), which lists what they call (`groupOf`, `showError` and `errorHome` are there
  for the checks only). A check's own script added after the page's tag must be a module
  too (`tools/design_check.py` `after_page`): a classic script there runs before the page's
  module, which waits until the document is parsed. A classic script before it (the design
  check's `fixtures.js` bridge) still runs first.
- **Start-up (#488).** A module script runs once the document is parsed, before
  `DOMContentLoaded`; pywebview 6.2.1 injects its bridge and sends `pywebviewready` only
  after WebView2's `NavigationCompleted`, which comes with the page's `load`. So `main.js`'s
  listeners are in place before the event, as they were for the classic scripts, and the
  page starts on it as before (`tests/test_page_scripts.py`, `StartUpTests`). The checks'
  stand-in bridges send the event on `load` too. The page does not look for a bridge that is
  already ready: that would read `window.pywebview.api` outside `call()`, which the I3 guard
  forbids (`tests/test_consistency.py`). The real window shows whether the order holds.
- **Adding a module.** Import it where it is used, list it in `PAGE_SCRIPTS` in `main.js`
  and in `packaging/app-allowlist.json`, start it with `"use strict";` and declare only,
  with plain `import { ... } from "./x.js";` and `export { ... };` lists.
  `tests/test_page_scripts.py` fails if the modules `main.js` reaches, `PAGE_SCRIPTS`, the
  `.js` files in the folder and the allowlist differ; if `index.html` loads anything but
  that one module tag; if an import or export is not a plain list, or names a file outside
  the folder or a name the other module does not export, or a module imports at run time
  (`import()`) or uses `import.meta`; if a module other than `main.js` runs code at load or
  reads an imported name as it loads (`const probe = wire();` in `polling.js`); or if two
  modules declare the same top-level name, which the tests reach in one namespace.
- **CI's "JavaScript syntax" job does not check the modules.** It runs `node --check` on
  each `.js` file, and Node.js passes a file with `import` or `export` without checking it:
  on Node.js 24 (6 Nov 2026) it exits 0 even for one that does not parse. Every page test
  loads every module through `tests/page_vm.js`, which stops on one that does not parse
  (`HarnessModuleTests`), so the tests check them instead.
- **Tests that read the page's code** use `tests/page_scripts.py`: `source()` joins the
  modules in `PAGE_SCRIPTS` order, so a function is found by name whichever file it is in.
  It reads `index.html` with the standard library's HTML parser, never a pattern, so a
  script tag is found however it is spelt (`<SCRIPT>`, `</script >`), as the browser finds
  it (CodeQL `py/bad-tag-filter`); `tools/design_check.py` `page_tags_found` does the same.
  The I3 bridge-name guard and the T1 "opens no link" guard read `every_source()`, every
  `.js` file under `workspace/ui`, whether `main.js` imports it or not.
- **Tests that run the page's code** call the real functions, never a copy cut out of the
  source with a pattern (#414 step 3b). `page_scripts.run(code)` starts Node.js with
  `--experimental-vm-modules` on `tests/page_vm.js`, which loads the real modules from
  `main.js` through their own imports into one `vm` context (`vm.SourceTextModule`; an
  import of anything but a `./name.js` beside `main.js`, or of a name not exported, stops
  the run), evaluates them (`main.js` wires stand-in elements and waits for a
  `pywebviewready` that never comes), runs the test's code after them and returns what it
  printed as JSON. `run(code, before=...)` runs code before the page loads (a bridge already
  in place, say). Each module is evaluated with one line added after its own text, which
  moves none of its lines (`import.meta.pageVm(...)`: two arrow functions that read and
  assign a name in the module's own scope), and `page_vm.js` puts an accessor on the
  context's global object for each top-level name of each module. So the test replaces what
  the page reaches by assigning it: `document = {...}`, `window = {...}`,
  `setTimeout = ...`, a page function (`say = ...`) or a `let`. That changes the name in the
  module that declares it, so its own functions and every module that imports it see the
  replacement, as in the page. A page `const` (`$`, `button`, `state`) cannot be replaced:
  replace the `document` that `$` reads, or change the object (`state.view = ...`).
  Declaring a page or stand-in name again (`const document`) fails with "already declared",
  so a test cannot quietly use its own copy while the page uses the real one. The accessors
  are for the test's code only: a module that uses another module's name without importing
  it throws "is not defined" in the browser, and an accessor reached from a page module
  throws the same (it knows the caller's file from the stack), so a missing import does not
  pass the tests. Assignments are covered too (#495): a stub of a name that neither the page
  nor the test declares (a function since renamed or removed, `renamedAway = () => 0`)
  fails with "is not defined" instead of creating a new global the page never calls. The
  test's code runs in strict mode, which stops most such assignments where they happen;
  Node.js's `vm` lets the assignment of a function through even in strict mode (it cannot
  tell it from a function declaration), so `page_vm.js` also fails the run if the test's
  code added a global, both once that code has run and as Node.js exits (for a callback or
  a timer). For a check on how a function is written, `page_scripts.function_text(name)` and
  `listener_texts()` give the loaded function's own text. Without Node.js these tests skip;
  CI has it, and `--strict-skips` does not allow that skip.
- **A page error's line names its script by number.** `report_ui_error` still takes a fixed
  kind, a line and a column, and no file name. For an error in one of the page's modules, the
  page sends the script's place in `PAGE_SCRIPTS` (`core.js` is 1) times
  100000 plus the line: 300120 is line 120 of the third script, `meetings.js`. A line from
  any other script (none in the app; `fixtures.js` in the design check) is sent as it is.
  The largest value fits the bridge's limit of 10,000,000 while there are fewer than 100
  scripts of fewer than 100,000 lines each.
- **From `file://` the browser hides the page's own errors; the page reads its own stack.**
  This describes the window before #488 part A, which serves the page from a virtual host
  instead; the guards below stay, and whether WebView2 now gives the file and line itself is
  to confirm in that change's real-window run. The window opened the page from `file://`,
  and WebView2 then gave an uncaught error in one
  of the page's scripts to the `error` event as "Script error.", with no file, line 0 and
  no error object, and sends no `unhandledrejection` event at all for one. Measured in the
  real window on 30 Oct 2026 (#491; a throwaway empty workspace, `groupOf(null)` raised from
  a timer): before the fix `report_ui_error` received `("error", 0, 0)` for each error and
  nothing for a rejection. Headless Edge from `file://` does the same (#414 probe); served
  over HTTP, as in the design check, the file and line arrive. An error the page catches
  itself still has its full stack, so `main.js` (`guardCallbacks`, at load before any
  listener) wraps each callback the browser calls later in `guarded()`:
  `EventTarget.prototype.addEventListener` (and `removeEventListener`, which finds the same
  wrapper), `setTimeout`, `setInterval`, `requestAnimationFrame`, the page's `on...`
  handlers and `wire()`. A guard that catches an error notes its place from the stack and
  throws the same error on, so the browser handles it as before and the `error` event that
  follows sends that place. When a promise a callback returns rejects and nothing else
  handles it by the next task (the browser decides at the end of the task), the guard sends
  the rejection itself and rejects on with the same reason, in a promise of its own; the
  `unhandledrejection` event, where the browser sends one, skips that promise. It is known
  by the promise, not the reason, so a string or number reason is not sent twice (#501). A
  promise the page also handles is not sent (#501): `guardCallbacks` makes
  `Promise.prototype.then` (which `catch` and `finally` call) note each promise it is called
  on, and the guard watches with the browser's own `then`, so its watch is not counted. A
  `then` with no rejection handler, and a `finally`, pass a watched promise's rejection on
  to the promise they return, so the guard watches that one too, and the rejection is sent
  once, from wherever it ends up unhandled (#505 review). A promise is watched once, however
  many callbacks return it. Over HTTP the guard's watch is a native handler on each promise
  it watches, so the browser sends no `unhandledrejection` event for a promise `then` or
  `finally` returns either; the guard's own reject-on takes its place, and the rejection is
  still sent once. Known limits from `file://` (Node probe of the real page functions, 2 Nov
  2026, #506): an `await` does not call `then`, so a returned promise handled only by an
  `await` elsewhere is still sent; and the guard follows only `then` without a rejection
  handler and `finally`, so a rejection passed on any other way is not sent at all: through
  `Promise.all`, `race` or `any`, by an async function that returns the promise or a
  `new Promise` resolved with it, or by a rejection handler that throws it again
  (`catch((e) => { throw e; })`). Served over HTTP the browser's event still sends these.
  `Promise.allSettled` handles the rejection, so nothing is sent, as in the browser. No page
  script passes a promise a callback returns on in these ways (#506): the page's rethrowing
  handlers are on promises made inside a callback, whose rejection reaches the callback's
  own promise by `await`. Following them would mean patching `Promise.all`, `race` and
  `any`, and an async function's return still could not be reached; #488 makes the guard
  unnecessary. A `then` called after the guard has sent a rejection sends it again, as the
  browser does for a new unhandled promise; the bridge records at most one a second.
  `stackPlace` cuts off the
  error's name and message unread (they could quote meeting text), takes the first frame
  whose URL is one of `PAGE_SCRIPTS` and returns two numbers: no message, file name or path
  reaches the bridge. Not covered: an error while a script first loads (before `main.js`
  installs the guards) and a promise chain the page builds itself (`.then` callbacks) still
  send 0, or nothing for a rejection. `tools/workspace_ui_check.py` raises a timer's, a
  listener's and an async callback's error in the real window and needs each line;
  `tests/test_page_scripts.py` (`PageErrorPlaceTests`) runs the functions themselves.
  Served over HTTP the browser's own file and line are used. Loading the page another way
  (a virtual host instead of `file://`) should make the browser give the details itself;
  #488 part A does that (the user's decision DEC-090). The real-window run (4 Nov 2026) shows
  the browser gives the file and line, except for an error a guard rethrows, where the line is
  the guard's `throw err`; the guard's noted place is sent for that error (`e.error` is the
  error the guard threw).

## Windows, WebView2 and pythonnet

- **WebView2 objects are usable only on the UI thread.** Marshal calls with `form.Invoke`.
- **Do not combine `TrySuspendAsync` with `MemoryUsageTargetLevel`** (Microsoft's WebView2
  guidance).
- **pythonnet returns a new wrapper on every .NET property read**, so `form.Icon is x` is always
  false. Compare handles or values instead.
- **WebView2 virtual host (#488).** On an https origin WebView2 remembers a permission
  decision for the origin unless the handler sets `SavesInProfile` false: the real-window run
  (4 Nov 2026) saw a frame's microphone request refused by the remembered decision, never
  reaching the app's handler. `shell._permission_refuser` sets it false, so every request is
  the app's own refusal.
  The window maps `https://unminute.invalid/` to
  `workspace/ui` only (`shell._map_ui_folder`, `CoreWebView2HostResourceAccessKind.Deny`),
  before pywebview's first navigation; a mapping made later applies only after a reload
  (Microsoft's reference). WebView2 serves those requests itself, with no DNS lookup, so the
  host-resolver switch (`MAP * ~NOTFOUND`) does not stop them. Deny blocks only other
  origins: the page's own scripts, styles, fonts and logo load. Checked 3 Nov 2026 with a
  WebView2 control in a form never shown (the app's switches, the shell's own mapping call,
  the bundled page): origin `https://unminute.invalid`, a secure context, all scripts, both
  stylesheets, the Archivo font and the logo loaded, `fetch` of the page's own file refused
  (`connect-src 'none'`), a page of another origin could not load the logo (with DenyCors,
  the control, it could), and no listening socket in the process tree. The same kind of probe
  (review of #537, WebView2 Runtime 156.0.4314.58, 3 Nov 2026) found the page a secure context from `file://` too, with the same browser
  APIs (pickers, device choosers, WebAuthn, media, clipboard, location) from both addresses,
  although Microsoft's page on local content says `file://` lacks them; but only the virtual
  host let page script start a `Worker` or `SharedWorker` and register a service worker (Microsoft's reference
  says the mapping does not apply to service worker scripts, yet the registration of a mapped
  script succeeded), so the policy has `worker-src 'none'`. Headless Edge has no
  such mapping: `tools/ui_idle_check.py` and the design check serve the page from their own
  local HTTP server, as before. pythonnet needs an enum member, not its number
  (`DockStyle.Fill`, not 5), and `CoreWebView2CreationProperties` is in
  `Microsoft.Web.WebView2.WinForms`, not `.Core`.
- **Clicking WinForms buttons in UI automation.** Post `WM_LBUTTONDOWN` and `WM_LBUTTONUP` at the
  control's centre, as a real click sends. WinForms turns `BM_CLICK` into the control's Click
  with no mouse or key event (a posted `BM_CLICK` on an off-screen `Button` raised Click only;
  19 Oct 2026). On the confirmation's action that skips the app's mouse rule, which is on
  MouseUp (`mouse_up_on_action` in `workspace/shell.py`), and takes the path of an assistive
  tool's Invoke instead (`act_clicked`: it answers once the action has been enabled for the
  wait), so a check meant to test a person's click would test the wrong rule.
- **A posted click cannot tick a covered WinForms `CheckBox`.** On mouse-up, WinForms'
  `CheckBox` acts only if `WindowFromPoint` at that point on screen is the check box itself. A
  check started from a terminal usually cannot bring its window to the foreground (Windows'
  foreground lock), so the window the user has in front covers the confirmation and the posted
  click is lost. That made `tools/workspace_ui_check.py` fail for days on an unchanged `main`
  ("a confirmation was left open (is the screen locked?)"; 19 Oct 2026). It now ticks the
  statement in two ways that work whatever is in front. The "keyboard" script posts Space to
  the check box; that ticks it whatever has the focus, so the script's Tab count to the action
  (Enter answers only on a focused action) is what shows the statement is in the tab order.
  The "tick first" script posts `BM_CLICK` to the check box, which WinForms turns into the box's
  Click with no mouse or key event (as on any button, above), so only the tick itself
  (`CheckedChanged`) can restart the wait, and the check shows that it does. The
  confirmation's action button is not affected by the cover: the app answers its mouse-up from
  the button's own client rectangle (`mouse_up_on_action` in `workspace/shell.py`). The
  real-window check does not try a mouse tick when the confirmation happens to be uncovered:
  the result would depend on what is on the user's screen, and forcing the foreground or
  moving the real pointer would take over the user's desktop. Instead `tests/test_evidence.py`
  shows, on an off-screen stand-in form, that a posted click acts on the check box when nothing
  covers it and is lost when a window does, and that Space and `BM_CLICK` tick it while it is
  covered, `BM_CLICK` with no mouse or key event. That test holds a named mutex
  (`Local\UnminuteTickTest`), because concurrent suites on one machine would put their
  stand-ins at the same place on screen and cover each other.
- **A page without window focus gets no `blur` event.** Chromium's `focus()` and `blur()` move
  `document.activeElement` whatever the window, but send the focus and blur events only while
  the page has focus (`document.hasFocus()`). The page ends a transcript edit on the span's
  `blur`, so a bare `blur()` from `tools/workspace_ui_check.py` in a window left behind the
  user's left the edit open, and the growing-transcript step `after_edit` failed with no rows
  appended (21 Oct 2026, #308). The check now ends edits with `blurLikeFocused` (`BLUR_JS`):
  when the element held the focus and no native `blur` arrived during `blur()`, it dispatches
  the one a focused window would deliver, so the element gets exactly one blur either way. The
  steps that depend on focus (the first edit, the growing transcript's steps and the settings
  step) record `focused` in the report. Headless Edge shows the same rule: a page has no
  focus while it loads, then gains it. `tests/test_ui_check_focus.py` runs the step against a
  stand-in for the rule.
- **The scripted confirmation rules each have a native script only they can pass.** In
  `tools/workspace_ui_check.py`, keys (Shift posted to the focused Cancel) and clicks on the
  text before the action is available must restart the wait ("keys wait again", "clicks wait
  again"); Enter on the ticked statement, long after the action was enabled, must cancel
  (`AcceptButton` is Cancel); Esc on the ticked statement, long after the action was enabled,
  must close the window with no answer (`CancelButton` is Cancel), and the script checks that
  the window was already closed about 0.7 s later, so the stuck-window clean-up cannot pass
  it; the keyboard script's quick Enter comes 2.5 s after the tick but just after the action
  took the focus, so only the focus hold can ignore it; and an Invoke without the focus
  (`BM_CLICK` while Cancel keeps it, the path of an assistive tool's Invoke, above) must be
  ignored about 0.3 s after the action is enabled and answer about 1.8 s after (`act_clicked`),
  so an Invoke that skipped the wait fails. A script that several rules could pass (for example
  an Enter posted while Cancel has the focus, which WinForms gives to Cancel whatever
  `AcceptButton` is) proves none of them. Not scripted by this check: Space, or an
  Invoke, on the focused action; the wait restarting when the window returns to the front;
  buttons having no keyboard shortcut; high contrast; and restoring a minimised app first.
  One question at a time is a unit test (`tests/test_app.py`). `tests/test_evidence.py` checks
  the Esc, Invoke and "until enabled" steps on an off-screen stand-in form, and
  `tests/test_accessors.py` the verdict (`native_problems`), which names each rule that failed.
- **WebView2 and the user's account (#509).** On an Entra ID joined laptop with the user signed
  in, the WebView2 Runtime's browser process (`msedgewebview2.exe`, the direct child of the app)
  sometimes loaded `oneauth.dll`, `wininet.dll`, `Windows.Security.Authentication.Web.Core.dll`
  and an in-process account extension (`AadWamExtension.dll` or
  `MicrosoftAccountWAMExtension.dll`) within half a second of the window starting to close,
  and wrote `AppData\Local\Microsoft\IdentityCache\...\Accounts`, `AT` and `ID` files and
  WinINet's `INetCache\IE\container.dat` and `INetCookies\ESE\container.dat` (with an
  unlistable `INetCache\Content.IE5`) under the profile its known folders resolve to. A
  blank window from the packaged runtime with the app's switches reproduced it (runtime
  156.0.4314.50 and .58, pywebview 6.2.1, 2 Nov 2026): 6 of 29 closes after a UI
  Automation client had read the window (all in one 20-minute spell), none of 12 without one,
  and never while a window was open; Windows Forms alone, and an app ended with `taskkill`,
  never did. The files are dated at the close, and no process held them open a moment later.
  What was tried, by the runtime's behaviour: `--force-renderer-accessibility` (Microsoft's
  list of WebView2 browser flags) did not bring it on; disabling the runtime's internal feature
  `msLazyLoadOneAuthInWebView` loads OneAuth at start-up instead, every time (so the runtime
  defers it, and the close is what we saw set it off); disabling `msOneAuthWAM` makes the runtime
  load the Web Account Manager client at every start instead. Microsoft documents no setting
  that turns the lookup off (single sign-on with the OS account,
  `AllowSingleSignOnUsingOSPrimaryAccount`, is already off), so the app ends the browser
  process itself once a close goes ahead (`shell.end_browser_on_close`), and the shutdown
  never runs. Ending it makes pywebview's own private-mode clean-up fail at its first step
  (it asks the ended process for its id), so `shell._clear_after_end` disposes the control and
  removes the same folder without that step. That replaces pywebview 6.2.1's
  `EdgeChrome.clear_user_data` at class level: on a pywebview upgrade, re-check it
  (`tests/test_webview_close.py` `PinnedPywebviewTests` fails if its shape changes; if it no
  longer runs, the profile stays until the next start's `storage_folder` clean-up). A Windows
  shutdown or sign-out is left alone (`CloseReason.WindowsShutDown`): Windows Forms raises
  FormClosing at `WM_QUERYENDSESSION` but closes the form only at `WM_ENDSESSION`, so a
  cancelled shutdown would otherwise leave the window open without its browser. For the same
  reason `shell.never_block_shutdown`, which lets a shutdown's close through while recording,
  never marks the window closable (`unminute_may_close`): a cancelled shutdown (`WM_ENDSESSION`
  with wParam 0) raises no Windows Forms event, so a mark set at the query would outlive it and
  the user's next close would skip the recording question (#512). Windows Forms also keeps the
  form's close reason at `WindowsShutDown` after a cancelled shutdown (`Form.cs` resets it only
  in `ShowDialog` and for a cancelled MDI child, and `WM_CLOSE` sets `TaskManagerClosing` only
  when no reason is set), so a later `WM_CLOSE` from another program reaches FormClosing as
  `WindowsShutDown` too, and would keep the browser (#520). From the first shutdown query the
  window therefore watches its own messages through a window subclass (`SetWindowSubclass`,
  `shell._watch_messages`), and `shell.EndSession` treats a `WindowsShutDown` close after a
  cancelled end of session as an ordinary close; the next `WM_QUERYENDSESSION`, seen before
  Windows Forms raises its FormClosing, makes it a query again. Every message to the window then
  runs a little Python, so the watch starts only at a shutdown query; if it cannot be installed,
  or the window handle is recreated, the browser is kept for such a close, as before, and the
  next shutdown query tries again; if handing a message on fails, the window still answers a
  shutdown query TRUE (#522). With the #509 fix the browser and its children were gone within
  0.1 s of the close, the profile folder was removed, no crash report was written, and the real
  start closed with exit code 0. The internal feature names come from the runtime's `msedge.dll`
  and are not documented: do not ship them as switches. To look again, the probe scripts watched
  the tree's loaded modules (Toolhelp snapshots every 50 ms, names only) and the files by name,
  in a throwaway `USERPROFILE`; never list or read the real profile's `IdentityCache` (it holds
  the user's tokens).
- **Power requests do not survive a closed lid.** `PowerSetRequest` keeps a computer awake only
  against the idle timer: Windows ends it when the user closes the lid (action Sleep), presses
  the power button or chooses Sleep, and on Modern Standby on battery five minutes after the
  sleep timeout. `powercfg /requests` shows the request while recording. On Intune-managed
  laptops the lid action can be set by policy (`SelectLidCloseAction*`); on one such laptop
  `powercfg /q` did not list the lid setting, though `PowerReadACValueIndex` still read it.
- **Windows PowerShell 5.1 started under PowerShell 7 needs its own module path.** A `pwsh`
  step (the default shell on GitHub's Windows runners) sets `PSModulePath` to PowerShell 7's
  module folders. A `powershell.exe` that inherits it through another process, such as the
  build's Python, cannot load its own `Microsoft.PowerShell.Security` ("the module could not
  be loaded", `CouldNotAutoloadMatchingModule`), so `Get-AuthenticodeSignature` is missing.
  That failed the first hosted release build (v0.5.0-preview.1, 19 Oct 2026) while the same
  build passed from Windows PowerShell or Git Bash. `pwsh` repairs the variable only for a
  `powershell.exe` it starts itself, so the error does not show when you try the command at a
  `pwsh` prompt. Removing the variable is not enough either: 5.1 then rebuilds a default
  that puts the user's own `Documents\WindowsPowerShell\Modules` first, which any program
  running as the user can write to. The tools therefore start Windows PowerShell through
  `tools/windows_powershell.py` (#237): by its absolute path in the system folder
  (`GetSystemDirectoryW`), since a bare `powershell` is searched for beside Python and in
  the current folder first (if that file is missing, the tools stop rather than look
  anywhere else); with `PSModulePath` set to the system folder's
  `WindowsPowerShell\v1.0\Modules` (5.1 adds
  `Program Files\WindowsPowerShell\Modules`, from the known folder rather than the
  `ProgramFiles` variable); and with an allowlisted environment instead of the parent's, so
  .NET settings such as `COR_ENABLE_PROFILING`, `COR_PROFILER` or `COMPlus_*`, which can
  load a DLL into `powershell.exe`, never reach it. A script that needs another variable
  passes it to `windows_powershell.run` explicitly. `tools/build_package.py` (the signature
  checks), `tools/packaged_check.py`, `tools/process_listeners.py` and `tools/perf_check.py`
  use it, as do the tests that start Windows PowerShell themselves (`tests/test_evidence.py`,
  `tests/test_hardening.py`; `tests/test_agent_guard.py` finds it as Claude Code does, on
  purpose). `tools/build_package.py` loads its own copy by path, compiled from the source
  (#268), so a test that patches the helper for the build patches
  `build_package.windows_powershell`, not the module `import windows_powershell` gives.
  Other system programs are started the same way, through
  `windows_powershell.system_program(...)` with `windows_powershell.environment()`:
  `netstat` in `process_listeners.listeners()`, `taskkill` in `tools/packaged_check.py` and
  `tools/packaged_crash_check.py`, and `cmd` for `mklink /J` in the tests that make
  junctions (#253). `tools/import_dialog_check.ps1` starts `taskkill` by its full path too,
  and checks it exists before the app starts. `tests/test_windows_powershell.py` scans every
  Python tool and test, and every PowerShell tool, for the usual ways of starting any of these
  by name: in Python, the name first in a list, or at the start of a string, passed to `run`,
  `Popen`, `call`, `check_call`, `check_output`, `system` or `popen`; in PowerShell (comments
  left out), `Start-Process` or `&` with the name, or the name as a command of its own. It is
  a text scan, not a proof: a name in a tuple or a constant, `executable=` or `shutil.which`
  is not found, so review a new start too. A child that
  is not `-ErrorAction Stop` can also exit 0 after such an error, so read its stderr, not
  only its exit code. To reproduce: from `pwsh` 7, start Python, and
  from Python start `powershell`.
- **Removing a folder that may hold a junction (#297).** On Windows PowerShell 5.1.26100,
  `Remove-Item -Recurse -Force` removed a junction inside a folder as a link and left its
  target alone (checked 21 October 2026 in a scratch folder); older 5.1 builds are not known.
  .NET Framework's `[IO.Directory]::Delete(path, $true)` failed on the same folder with
  "Access to the path ... is denied". So `packaging/install.ps1` and
  `packaging/uninstall.ps1` remove folders with the same walk (`Remove-OwnFolder`, kept equal
  by `tests/test_hardening.py`), which deletes a link as a link and never descends into it;
  on 20,000 small files it took about as long as `Remove-Item` (6.5 against 6.2 seconds on a
  development laptop). The uninstaller's walk was also checked with a junction under
  PowerShell 7.6 (22 October 2026); symbolic links need administrator rights or Developer
  Mode to create, so they were not.
- **A regular expression's `$` matches before a final newline (#297).** In .NET, as in
  Python, a pattern `^...$` also accepts a name with a final newline, such as `"1.0.0\n"`.
  A check on a name that becomes a path ends in `\z` instead.
- **Pester tests dot-source the installer scripts (#111 B6a).** `install.ps1` and
  `uninstall.ps1` end their function definitions with a guard,
  `if ($MyInvocation.InvocationName -eq ".") { return }`, so dot-sourcing defines the
  functions and runs nothing. A script without that guard runs its main part when
  dot-sourced, with its default `-InstallDir`: on 23 Oct 2026 a failing-first run of
  the new Pester tests against the scripts from before B6a uninstalled the per-user copy
  on the development computer.
- **Installer tests never reach the real install (#111 B6a, after that incident).**
  Pester: `tests/powershell/Isolation.ps1` points `LOCALAPPDATA` and `APPDATA` into
  TestDrive, and a test loads a script only as
  `. (Confirm-Loadable $path); Set-ScratchTarget`. `Confirm-Loadable` refuses, before
  anything in the script runs, a script without the guard, one that reaches the Start
  menu or the uninstall key other than through `$ProgramsFolder` and `$RegKey` (as every
  script before B6a did), and a run whose variables are not scratch; `Set-ScratchTarget`
  points `$RegKey` at a throwaway `HKCU\Software\Unminute-PesterTest-<guid>` key and
  `$ProgramsFolder` at a scratch folder, and checks every place the script would write.
  Above the guard a script may only define functions and assign values that run no
  command and call no .NET method except two known reads, and its parameters' default
  values may run only `Join-Path`; `Confirm-Loadable` refuses anything else. The
  scratch checks resolve junctions, links and short names first. A test in the suite
  checks that every load has that form and that no test file starts a script any
  other way (`&`, `-File`, `Start-Process`, `Invoke-Expression`, jobs and the like:
  `Find-UncheckedRun`). `tools/run_pester.ps1` also
  runs the suite with scratch variables and fails if the real install's folder, Start
  menu shortcut, uninstall entry or pins of Unminute (TaskBar and StartMenu, which
  `-Offboard` removes; `Get-RealInstallState` in `Isolation.ps1`) changed. Python: every test runs a scratch copy
  (`scratch_text`) through `run_installer` in `tests/test_hardening.py`, which refuses
  unless the copy, `-InstallDir`, `LOCALAPPDATA` and `APPDATA` are scratch and fails if
  the real install, pins included (`real_install_state`), changed
  (`tests/test_uninstall_offboard.py` runs its copies through it too). Every other
  process a test in either module starts is checked before it starts and refused if it
  names a packaging script, or a `.ps1` that copies or runs one (each module has a test
  that its guard is on); and no process start in any test module may name an installer
  script, in the call or through a variable or attribute set first. `InstallerIsolationTests` proves it. The real places come
  from the shell's known folders, which ignore `LOCALAPPDATA` and `APPDATA`. Never
  dot-source an older revision of either script, and never run the scripts or Pester any
  other way; show failing-first evidence for installer behaviour with the Python-driven
  tests (a scratch copy of the older script through `run_installer`).
- **In the minimal environment, each Windows PowerShell 5.1 start pays for module search
  on CI.** The first command a run takes from a module (`ConvertFrom-Json`, `Get-Item`,
  `New-Object`) makes 5.1 search every module folder, Program Files' first, and record the
  exports in its module analysis cache. With no `PSModuleAnalysisCachePath` named, and the
  cache written only some seconds after a search, a short run never keeps it. GitHub's
  Windows image has 54 modules there (AWS, Microsoft Graph), so each such start took 12 to
  17 s on CI (measured 26 October 2026), against well under a second on a laptop; loading
  the modules by path first did not help. It made the installer tests take about 98 minutes
  instead of 42. `run_installer` and the offboarding tests' `shortcut` therefore name one
  cache file for the test process (`module_cache` in `tests/test_hardening.py`), filled
  once by a run that looks up a name no module exports (so every module is noted; a cache
  filled by one found command still left about 12 s a run) and waits until PowerShell
  writes it. Other one-off starts through
  `tools/windows_powershell.py` still pay the search once each.
  Filling it took 41 to 50 s in each CI chunk with installer tests, so the chunk job keeps the
  file between runs (#389): `actions/cache`, keyed by the runner image and the Windows
  PowerShell version, restores it to the path named in `UNMINUTE_PSMODULE_CACHE`, and
  `module_cache` uses a file found there as it is. A miss, or an empty file, is filled there
  and saved by the first chunk to finish; a stale file is still correct, as PowerShell
  analyses again any module whose file changed. Locally the variable is unset and nothing
  changes. To clear a bad entry, find its key with `gh cache list --key psmodule-analysis-`
  and remove it with `gh cache delete <key>`; the next run on that image fills and saves it
  again.
- **Windows PowerShell 5.1 reads a `.ps1` without a byte-order mark in the system code
  page.** Keep the installer scripts and their Pester tests ASCII; build other characters
  from their code points (`[char]0x212A`).
- **Machine mode is tested without elevation, and only in scratch (#111 B6b).** Both
  scripts set every place outside the install folder that machine mode writes once, at
  their top: `$ProgramFilesFolder`, `$CommonProgramsFolder`, `$MachineRegKey` and
  `$EventSourceKey`. `tests/powershell/Isolation.ps1` refuses a script that reaches Program
  Files, the all-users Start menu, HKLM or the Event Log any other way (comments aside;
  HKLM otherwise only in the WebView2 check's two reads, and a key opened only to read), and
  `Set-ScratchTarget` points all four at scratch folders and throwaway
  `HKCU\Software\Unminute-PesterTest-<guid>` keys; `tests/powershell/Machine.Tests.ps1` gives
  each test its own with `Set-ScratchMachineTarget`, and its "machine places" tests prove it.
  The Pester mocks stand in only elevation, bitness, the folder's permissions as read and its
  creation with them; everything else, junctions and permissions on folders and files
  beneath the install folder included, runs for real in TestDrive. Python: `scratch_text`
  swaps the same four for scratch folders and `HKCU\Software\Unminute-UnitTest-*` keys, and
  `run_installer(..., machine=True)` is the only way a test runs `-Machine`, refused unless
  every machine place is scratch (`InstallerIsolationTests`). `tools/run_pester.ps1` and
  `run_installer` also fail if the real machine install (Program Files\Unminute, the
  all-users shortcut, the HKLM uninstall entry or the Event Log source) changed. Never run
  `install.ps1 -Machine` or `uninstall.ps1 -Machine` any other way: elevated (as GitHub's
  runners are), they would write this computer's Program Files, HKLM and Start menu. A real
  System-context install is a manual check (#189).
- **A file written over in place can come out with permissions of its own (#111 B6b).** On
  GitHub's elevated runner, `current.json` (replaced with `File.Replace`) or `uninstall.ps1`
  (`Copy-Item -Force` over the old one) in a machine install came out with permissions of
  its own, and the next run's permission check stopped with exit code 4 (Quality run of
  23 October 2026 on 01627bf). `Set-InheritedOnly` resets just those two files after the run
  writes them. The cause is not confirmed, and a run without elevation does not show it: the
  Pester test sets the permissions itself to check the reset, and only the elevated CI job
  exercises the real case (the Pester job in Quality).
- **A 32-bit Windows PowerShell sees another Program Files and registry (#111 B6b).** In a
  32-bit process `[Environment]::GetFolderPath("ProgramFiles")` and `%ProgramFiles%` name
  `Program Files (x86)`, and `HKLM:\SOFTWARE` is the `WOW6432Node` view. `install.ps1
  -Machine` refuses a 32-bit run; from a 32-bit host, `%SystemRoot%\Sysnative\...` reaches the
  64-bit `powershell.exe`, and `%ProgramW6432%` names the 64-bit Program Files. The per-user
  installer reads the machine entry with `RegistryKey.OpenBaseKey(LocalMachine, Registry64)`
  so a 32-bit host sees it.
- **Remove a junction as a link (#111 B6b).** `[IO.Directory]::Delete(path, $false)` removes
  the junction itself and leaves its target; `[IO.Directory]::Move` renames the junction, not
  its target. `Remove-Item -Recurse` on a junction is never used. In Windows PowerShell 5.1,
  `(Get-Item -Force).Target` gives a junction's target as a one-item `string[]`, and
  `LinkType` is `Junction`.
- **Pester's package has deep paths.** Unpacked under a long folder it passes 260
  characters and `ExtractToDirectory` fails; `tools/run_pester.ps1` unpacks into a short
  folder name under `-ModuleRoot`, which should itself be short (CI uses `RUNNER_TEMP`).
- **The PowerShell Gallery fails now and then (#427).** `Install-Module` reported "Source
  Location ... is not valid" and "Package ... failed to download" on 27 October 2026, and a
  re-run passed. CI no longer uses `Install-Module`: PSScriptAnalyzer (its job in `ci.yml`)
  and Pester (`tools/run_pester.ps1`) are fetched as `.nupkg` files from
  `https://www.powershellgallery.com/api/v2/package/<name>/<version>`, which redirects to
  `cdn.powershellgallery.com`, with up to 3 attempts, and cached by version and SHA-256. To
  move a pin, download the package, take its SHA-256 (`Get-FileHash`), and check that the
  file's SHA-512, in base64, equals the `PackageHash` the Gallery publishes for it
  (`https://www.powershellgallery.com/api/v2/FindPackagesById()?id='<name>'&$filter=Version eq '<version>'`;
  the `Packages(Id=...,Version=...)` form works too; the Gallery sometimes returns a
  transient 404 for either, so try again before concluding the package is missing).
  Then change the version and hash together: in the job's `env` for PSScriptAnalyzer; in
  `tools/run_pester.ps1` and the `pester` job's `env` in `quality.yml` for Pester
  (`tests/test_consistency.py` holds those two equal). A new key starts a new cache entry; the old one expires unused.

## Claude Code hooks on Windows

- **Hook commands run in Git Bash unless the hook sets `"shell": "powershell"`.** On a machine
  without Git Bash, a bash-style command fails and Claude Code blocks every matching tool call.
  The repo's guard therefore uses PowerShell and `$env:CLAUDE_PROJECT_DIR`.
- **PowerShell turns a hook's exit code 2 (block) into 1 (warn only)** unless the command
  passes the code on itself. Claude Code lets a call through on exit 1, so the guard's command
  turns any exit but 0 into 2 (`if ($LASTEXITCODE -eq 0) { exit 0 }; exit 2`).
  `tests/test_agent_guard.py` runs the configured command through PowerShell to catch these
  mistakes.
- **The guard fails closed** (the owner's decision DEC-066, 25 Oct 2026). It blocks when `CLAUDE_PROJECT_DIR` is not
  an existing folder named by a drive or network path (unset, empty, relative, drive-only,
  device form or missing) or `python` is not on `PATH`, and the hook sets
  `"onFailure": "block"` with a 60 s timeout, so a hook that cannot start, fails or times out
  blocks too (Claude Code 2.1.295 and later). If Python breaks, every tool call the hook's
  matcher names (file tools, Glob, Grep and the shells) is blocked: fix `PATH` from a
  terminal. The command still allows when the hook file is missing (a branch from before it
  existed) and the hook's path is under 260 characters, which Windows PowerShell can test, so
  the `git switch` needed to recover is not blocked in that case.
- **Simulating a hook with a PowerShell pipeline misleads.** Piping into `powershell -Command`
  from PowerShell does not reach the native program, and `$input` adds a byte order mark. Test
  with a real process pipe, as the test does.

## Checks that can fail for reasons other than the code

- **Headless Edge times out when another headless Edge run competes with it.**
  `tools/design_check.py` (120 s per screenshot) and `tools/ui_idle_check.py` (300 s per run)
  then print one paragraph starting `NOT MEASURED: Headless Edge timed out on`, naming the page,
  mode and scenario and the tool's own limit, and exit 2. Edge returned nothing, so nothing was
  measured: it is not a page result, and never a pass (the "UI idle (headless Edge)" CI job
  fails on it). Rerun when no other headless Edge run is active; for `design_check.py`, rerun
  the missed cases one at a time. Until #361 both tools died here with a raw
  `subprocess.TimeoutExpired` traceback, which read like a page failure (seen on #336's lane,
  25 October 2026). The "names" case of `design_check.py` varies slightly between runs on
  `main` itself.
- **`tools/accessibility_check.py`** fails a view with `focus_not_measured` when its window loses
  keyboard focus during the focus pass (another window came to the front): Edge then draws no
  focus style at all. Keep the window in front and rerun; it is not a page fault. It does the
  same when Edge does not treat the focus as keyboard focus (`:focus-visible` does not match,
  as can happen after a mouse click in the window): press Tab once in the window and rerun.
- **`tools/accessibility_check.py` fails a view as "the page did not settle"** when finite
  animations or transitions are still running 3 s after the view opened (#370). A hidden,
  minimised or locked window may not advance transitions at all, so this can come from the
  window rather than the page: keep the window in front and the screen unlocked, and rerun.
  "Could not check settling" means the settle script did not return a count; that fails too.
  Don't raise `SETTLE_MS` to clear either (AGENTS.md E2).
- **Wait for the state a check audits, not a fixed time (#393).** The "recording draft" view
  in `tools/accessibility_check.py` waited a fixed 1.5 s, but its draft text arrives only
  after several bridge calls in turn. On a loaded CI runner that took longer, and
  `test_draft_scenario`'s accessibility test audited a hidden, empty box at random ("the draft
  box with text was not shown", 26 October 2026). The view now also waits, up to
  `DRAFT_WAIT_MS`, for the box to show a line; a box that never comes still fails. The test
  answers one of the page's first calls 2 s late, so a fixed wait fails it every time: in light,
  `recording_status` (the area still hidden, as on CI); in dark, `recording_draft` (the box
  shown with no lines yet) (#400).
- **`test_draft_scenario`'s headless page timed out on a script that does not wait (#405).**
  The test drives the page over a local server: the page asks for a script, runs it and posts
  the answer back. One lost request stopped that for good, and every later script timed out
  (main CI, 26 October 2026, on `return said().length;`). A script sent but never received is
  now sent again, and an answer is posted again until Python has it, so each runs once. The
  scenario test loses one of each on purpose. On a timeout the error now says whether Edge
  still runs, when the page last asked for a script and how many bridge calls it made, which
  tells a dead page from a stopped driver.
- **A scratch environment without every requirement fails `tools/accessibility_check.py`
  with `focus_obscured` (#416).** On 6 November 2026 a virtual environment without
  `sounddevice` made the page toast "Microphone support could not start in this
  installation" (`recorder.list_devices`, correctly). The toast sits at the bottom right
  for 8 s and covered a focused control on the "recording draft" and "meeting" views. Install
  the hash-locked requirements, not a subset, or check the toast's text before treating the
  finding as a page fault.
- **Reflow widths are reached by resizing again (#416).** One resize in proportion from the
  full window can land a few CSS px wide (643 for 640, 6 November 2026), as the frame does
  not scale and a vertical scrollbar can come or go. `reflow` resizes up to `REFLOW_RESIZES`
  times while the page is wider than the target, and keeps a narrower result, which is a
  stricter test. A view still wider after that is a finding.
- **`tools/accessibility_check.py` lists "OWNER EXCEPTIONS"** before its findings: field
  boundaries in the brand `--control-line` colour and the meeting title at rest (#298, the owner's decision DEC-047 of 25 October 2026). They are expected and do not fail the run. Each is
  matched by its colour for the mode the view was audited in (the light token in light, the
  dark token in dark; for the title, its id and transparent border), so a field drawn with
  any other border colour below 3:1 is still a finding. Don't widen `OWNER_EXCEPTIONS`
  to clear one; that needs the owner (AGENTS.md E2).
- **Clipboard tests** skip as "clipboard busy" while another app holds the clipboard.
- **A busy clipboard in `tools/workspace_ui_check.py` (#342).** The app waits about a second
  for a clipboard another program holds, then reports "The clipboard is in use by another
  program. Try again." On 23 October 2026 that failed the check's handoff step on every PR
  (and the locked screen's "Copy details for IT" step on #318), most likely because
  concurrent test runs on the same desktop held the clipboard. The check now clicks a copy
  again only on that message, up to 10 clicks over about 30 s, and reports the retries
  (`copy_retries` and `open_retries` in the handoff step, `copy_retries` in the locked
  screen's report). The busy message takes about a second to arrive, so a handoff click's
  status is read 1.5 s after it, and the locked screen's as soon as it appears, waiting up
  to 3 s (`TEXT_AFTER_JS`). A copy that stays busy still fails
  the step. A new copy step in the check goes through `COPY_RETRY_JS`
  (`tests/test_ui_check_clipboard.py`). Many retries in a report mean something kept the
  clipboard busy: find it rather than raising `COPY_TRIES` (AGENTS.md E2).
- **Real-window checks** (`tools/workspace_ui_check.py`, `tools/accessibility_check.py`) open
  windows on the user's screen and answer their own dialogs. Tell the user before running one.
  With `--app` they check a package instead of the checkout. Run them with that package's
  `runtime\python.exe` and the app's flags (`-I -B -X disable-remote-debug`); with any other
  Python, without those flags, or with app modules from elsewhere, they stop (`tools/evidence.py`).
  They need an unlocked session: on a locked one the app's confirmation windows cannot be
  answered and the clipboard cannot be opened (`OpenClipboard`: access denied). They read the
  session's lock flag (`tools/session_state.py`) before and after each window step, as
  `tools/packaged_check.py` and `tools/perf_check.py` do, and stop with exit 2, "the session
  locked", never a pass or a failure (#507). Do not test for a lock by the input desktop's name
  alone: on Windows 11 26H2 `OpenInputDesktop` still returned "Default" while the session was
  locked. They use that name only when the flag cannot be read, and then print a `NOTE` on
  standard error; `tools/evidence.py check` refuses such a report as release evidence (#514).
  A confirmation left open for 20 s on an unlocked session is still closed, and fails
  ("is the screen locked?").
- **`PrivacyDraftTests`** in `tests/test_consistency.py` fails on any Markdown file (all but
  `CHANGELOG.md` and `changelog.d/`; `docs/history/` included) that uses a word in
  `LEGAL_WORDS`: words that take a position on the law or name an outside review step, which
  the docs leave to each deploying organisation (the owner's rule). A factual phrase can trip
  it, such as the name of a Windows exception in a crash note: reword it (give the exception
  code instead). Never loosen the pattern or exempt a doc to make a change pass (AGENTS.md,
  E2); ask the user.
- **Speed tests measure processor time, not wall time (#293).** The private data guard's
  speed tests in `tests/test_agent_guard.py` failed at 2.0 s wall clock when the laptop was
  busy. They now time the test's own thread (`time.thread_time()`) against the same 2.0 s, and
  the too-complex test also counts the guard's work (`counted_work`) against a fixed limit
  per command shape (`TOO_COMPLEX`), 5% above what the bounded guard measured, so raising
  any of its work bounds by a tenth fails the test. A negative control lifts the guard's
  work bounds and checks that both measures then fail. Load that slows the processor itself
  (clock speed, heat) still counts, so a failure there is worth a rerun before treating it as
  real. Do not raise the 2.0 s or the work limits to make a run pass (AGENTS.md E2); a
  deliberate change to the guard's work needs the shapes measured again, with the reason in
  the PR.
- **A cancelled CI run on a pull request** usually means a newer push superseded it (#395):
  each workflow cancels a pull request's older run when a new one starts. Its checks,
  including a failed "Unit tests (Windows)", belong to the old head; look at the newest
  head's run instead. A run on `main` is never cancelled this way. Do not re-run a superseded
  run: it joins the same group and cancels the current head's run, whose "Unit tests
  (Windows)" then fails (it blocks, never passes); re-run the head's run instead.
- **`tests/test_recorder.py`** `test_a_recovered_recording_can_be_deleted_instead` failed once on
  the Windows runner and passed on rerun (#148).

## Git and worktrees

- **Undoing a mutation test.** Never use `git checkout -- <file>` when the file has uncommitted
  work; it throws the work away. Restore from a copy.
- **Rebasing onto a moved `main`.** Squash to one commit, rebase, resolve, and confirm no conflict
  markers remain. Then compare the rebased diff with the reviewed diff before pushing. Do not
  chain commands after a rebase that can stop on a conflict.
- **Removing a worktree.** Leave it first (`cd` out). Git's safety check and Windows ("device
  busy") both refuse otherwise.
- **Squash merges** are not reported by `git branch --merged`. Check a branch against its PR
  (`gh pr list --state all --head <branch>`) before deleting it.
