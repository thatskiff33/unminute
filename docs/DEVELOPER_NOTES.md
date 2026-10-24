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
  workers need for `tests/fake_worker_engines.py`.
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
- **Tools drive their own version of the app.** Since R13c (#136) the tools import
  `workspace.asr_engines` and `workspace.offline_guard`, so they fail with an `ImportError`
  against a checkout or package from before that move (v0.5.0-preview.3 and earlier). To
  measure an older package, run that version's own tools; `tools/evidence.py` already refuses
  evidence whose tools come from a different commit than the package.
- **`time.monotonic()` counts from boot.** Never start a "last run" timer at 0.0, or a check
  is skipped for the first minutes after boot. Start at `float("-inf")`.
- **PyAV 17 and 8-channel planar audio.** An 8-channel planar frame reports 9 planes, and
  `to_ndarray()` on one read past them and crashed the process (found in #220; 7 and 16
  channels were fine). Convert to an interleaved format (`flt`, not `fltp`) before reading
  samples, as `workspace/decode_child.py` does.

## Windows, WebView2 and pythonnet

- **WebView2 objects are usable only on the UI thread.** Marshal calls with `form.Invoke`.
- **Do not combine `TrySuspendAsync` with `MemoryUsageTargetLevel`** (Microsoft's WebView2
  guidance).
- **pythonnet returns a new wrapper on every .NET property read**, so `form.Icon is x` is always
  false. Compare handles or values instead.
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

## Claude Code hooks on Windows

- **Hook commands run in Git Bash unless the hook sets `"shell": "powershell"`.** On a machine
  without Git Bash, a bash-style command fails and Claude Code blocks every matching tool call.
  The repo's guard therefore uses PowerShell and `$env:CLAUDE_PROJECT_DIR`.
- **PowerShell turns a hook's exit code 2 (block) into 1 (warn only)** unless the command ends
  with `exit $LASTEXITCODE`. `tests/test_agent_guard.py` runs the configured command through
  PowerShell to catch both mistakes.
- **A hook that exits 2 because it cannot run blocks everything**, including the `git switch`
  needed to recover. The guard's command therefore allows when the hook file is missing (a
  branch from before it existed) and warns with exit 1 when `python` is not on `PATH`.
- **Simulating a hook with a PowerShell pipeline misleads.** Piping into `powershell -Command`
  from PowerShell does not reach the native program, and `$input` adds a byte order mark. Test
  with a real process pipe, as the test does.

## Checks that can fail for reasons other than the code

- **`tools/design_check.py`** runs headless Edge with a 120 s timeout, and times out when another
  session runs headless Edge at the same time. Rerun the missed cases one at a time. The "names"
  case varies slightly between runs on `main` itself.
- **`tools/accessibility_check.py`** fails a view with `focus_not_measured` when its window loses
  keyboard focus during the focus pass (another window came to the front): Edge then draws no
  focus style at all. Keep the window in front and rerun; it is not a page fault. It does the
  same when Edge does not treat the focus as keyboard focus (`:focus-visible` does not match,
  as can happen after a mouse click in the window): press Tab once in the window and rerun.
- **Clipboard tests** skip as "clipboard busy" while another app holds the clipboard.
- **A busy clipboard in `tools/workspace_ui_check.py` (#342).** The app waits about a second
  for a clipboard another program holds, then reports "The clipboard is in use by another
  program. Try again." On 23 October 2026 that failed the check's handoff step on every PR
  (and the locked screen's "Copy details for IT" step on #318), most likely because
  concurrent test runs on the same desktop held the clipboard. The check now clicks a copy
  again only on that message, up to 10 clicks over about 30 s, and reports the retries
  (`copy_retries` and `open_retries` in the handoff step). A copy that stays busy still fails
  the step. A new copy step in the check goes through `COPY_RETRY_JS`
  (`tests/test_ui_check_clipboard.py`). Many retries in a report mean something kept the
  clipboard busy: find it rather than raising `COPY_TRIES` (AGENTS.md E2).
- **Real-window checks** (`tools/workspace_ui_check.py`, `tools/accessibility_check.py`) open
  windows on the user's screen and answer their own dialogs. Tell the user before running one.
  With `--app` they check a package instead of the checkout. Run them with that package's
  `runtime\python.exe` and the app's flags (`-I -B -X disable-remote-debug`); with any other
  Python, without those flags, or with app modules from elsewhere, they stop (`tools/evidence.py`).
  They need an unlocked screen: on a locked one the app's confirmation windows cannot be
  answered, so `workspace_ui_check.py` closes them after 20 s and fails ("is the screen
  locked?").
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
