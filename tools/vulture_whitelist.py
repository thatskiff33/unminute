# tools/vulture_whitelist.py
# Names vulture reports as unused that are used in ways it cannot see (#136).
# The dead-code report (.github/workflows/quality.yml) reads this file with the
# shipped code; anything it still lists is a candidate to remove. Add a name
# here only with the reason, in the same pull request. Not shipped.
# ruff: noqa
_ = None

# .NET objects (pythonnet: WebView2, WinForms) and ctypes structures: setting
# an attribute or adding an event handler is the use.
_.AcceptButton
_.AccessibleDescription
_.Activated
_.AreDefaultScriptDialogsEnabled
_.AutoScroll
_.AutoSize
_.BackColor
_.Bounds
_.CancelButton
_.CheckedChanged
_.Click
_.EndCap
_.ForeColor
_.FormClosing
_.GotFocus
_.HandleCreated
_.Interval
_.KeyDown
_.KeyPreview
_.LostFocus
_.MaximumSize
_.MemoryUsageTargetLevel
_.MouseDown
_.MouseEnter
_.MouseLeave
_.MouseUp
_.NavigationStarting
_.Opacity
_.Paint
_.PermissionRequested
_.Resize
_.ScreenCaptureStarting
_.ScriptDialogOpening
_.ShowInTaskbar
_.StartCap
_.StartPosition
_.State
_.Tick
_.UseMnemonic
_.Visible
_.LimitFlags            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (workspace/jobs.py)
_.ActiveProcessLimit
_.ProcessMemoryLimit
_.SimpleReasonString    # REASON_CONTEXT (workspace/power.py)

# Library settings and protocols: the library reads them.
_.before_load           # pywebview window event (workspace/shell.py)
_.dragover              # pywebview DOM events (workspace/api.py)
_.drop
_.dont_write_bytecode   # sys.dont_write_bytecode (workspace/gpu.py)
_.intra_op_num_threads  # onnxruntime SessionOptions (asr_engines.py)
_.inter_op_num_threads
_.row_factory           # sqlite3 connection (workspace/store.py)
_.encoding              # a text stream's protocol (workspace/diag.py)
_.errors
_.writable
time_info               # sounddevice callback signature (workspace/recorder.py)

# Test helpers, named as such where they are defined: tests and tools call them.
_.current_speakers      # workspace/store.py
_.wait_idle             # workspace/app.py
_.idle_wakeups          # workspace/worker.py
get_priority            # workspace/resources.py
resume_pending          # workspace/cleanup.py
uninstall               # offline_guard.py

# Written but not yet read: surfacing it in diagnostics is a follow-up (#196).
minimise_failed
_.minimise_failed
