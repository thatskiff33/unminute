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
- **`time.monotonic()` counts from boot.** Never start a "last run" timer at 0.0, or a check
  is skipped for the first minutes after boot. Start at `float("-inf")`.

## Windows, WebView2 and pythonnet

- **WebView2 objects are usable only on the UI thread.** Marshal calls with `form.Invoke`.
- **Do not combine `TrySuspendAsync` with `MemoryUsageTargetLevel`** (Microsoft's WebView2
  guidance).
- **pythonnet returns a new wrapper on every .NET property read**, so `form.Icon is x` is always
  false. Compare handles or values instead.
- **Clicking WinForms buttons in UI automation.** Post `WM_LBUTTONDOWN` and `WM_LBUTTONUP` at the
  control's centre. `BM_CLICK` uses the current pointer position, so a button that checks where
  the mouse was released ignores it, and the check hangs with a dialog open on the user's screen.
- **Power requests do not survive a closed lid.** `PowerSetRequest` keeps a computer awake only
  against the idle timer: Windows ends it when the user closes the lid (action Sleep), presses
  the power button or chooses Sleep, and on Modern Standby on battery five minutes after the
  sleep timeout. `powercfg /requests` shows the request while recording. On Intune-managed
  laptops the lid action can be set by policy (`SelectLidCloseAction*`); on one such laptop
  `powercfg /q` did not list the lid setting, though `PowerReadACValueIndex` still read it.

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
  With `--app` they check a package instead of the checkout: run them with that package's
  `runtime\python.exe -I -B -X disable-remote-debug`, or they stop (`tools/evidence.py`).
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
