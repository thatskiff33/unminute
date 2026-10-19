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
- **Stale bytecode.** An edit and restore of the same size within one second can leave a stale
  `.pyc` that Python keeps using. Use `python -B`, or delete `__pycache__` with Python.
  `tools/run_tests.py` writes no bytecode, passes that on to every process a test starts
  (`PYTHONDONTWRITEBYTECODE`; a child started with `-I` ignores it, so it needs `-B`), and
  fails when a run left a new or changed `.pyc` in the checkout, naming the files without
  deleting them (#206). `tools/evidence.py --zip` refuses a checkout whose `tools/`, `tests/`
  or `docs/` hold them.
- **Dead-code report.** `python -m vulture workspace asr_engines.py offline_guard.py
  tools/vulture_whitelist.py --min-confidence 60` runs what CI's report runs (install vulture
  from `requirements/ci-vulture.txt`). Most hits are .NET or ctypes attributes set by name;
  add one to the whitelist only with its reason. Remove anything that is really unused.
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
- **Each confirmation rule has a native script only it can pass.** In
  `tools/workspace_ui_check.py`, keys (Shift posted to the focused Cancel) and clicks on the
  text before the action is available must restart the wait ("keys wait again", "clicks wait
  again"); Enter on the ticked statement, long after the action was enabled, must cancel
  (`AcceptButton` is Cancel); and the keyboard script's quick Enter comes 2.5 s after the tick
  but just after the action took the focus, so only the focus hold can ignore it. A script
  that several rules could pass (for example an Enter posted while Cancel has the focus, which
  WinForms gives to Cancel whatever `AcceptButton` is) proves none of them.
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
  purpose). Other system programs are started the same way, through
  `windows_powershell.system_program(...)` with `windows_powershell.environment()`:
  `netstat` in `process_listeners.listeners()`, `taskkill` in `tools/packaged_check.py` and
  `tools/packaged_crash_check.py`, and `cmd` for `mklink /J` in the tests that make
  junctions (#253). `tests/test_windows_powershell.py` scans every tool and test for a start
  of any of these by name. A child that
  is not `-ErrorAction Stop` can also exit 0 after such an error, so read its stderr, not
  only its exit code. To reproduce: from `pwsh` 7, start Python, and
  from Python start `powershell`.

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
- **`tools/accessibility_check.py`** can report `no_focus_ring` when its window loses focus.
  Rerun before treating it as real.
- **Clipboard tests** skip as "clipboard busy" while another app holds the clipboard.
- **Real-window checks** (`tools/workspace_ui_check.py`, `tools/accessibility_check.py`) open
  windows on the user's screen and answer their own dialogs. Tell the user before running one.
  With `--app` they check a package instead of the checkout. Run them with that package's
  `runtime\python.exe` and the app's flags (`-I -B -X disable-remote-debug`); with any other
  Python, without those flags, or with app modules from elsewhere, they stop (`tools/evidence.py`).
  They need an unlocked screen: on a locked one the app's confirmation windows cannot be
  answered, so `workspace_ui_check.py` closes them after 20 s and fails ("is the screen
  locked?").
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
