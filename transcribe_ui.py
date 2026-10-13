# transcribe_ui.py
# Local UI for faster-whisper. Audio never leaves the machine.
#
# What it does:
#   - Queue audio/video files (Add files... or drag-and-drop), click Start
#     queue; files are processed one by one, one transcript per input file
#   - Options (model, .txt/.md, timestamps, speakers) are read when Start
#     queue is clicked and apply to the whole run
#   - Optional speaker identification (Speaker 1, Speaker 2, ...) via pyannote,
#     with a Rename speakers... step afterwards to put real names in
#   - Transcripts save to the 'extracted' folder next to this script; name
#     clashes get a " (2)" suffix rather than overwriting
#   - Progress bar, elapsed timer, time remaining, Pause/Resume and Stop
#     (stopping saves a "(partial)" transcript)
#   - Settings (options, window size, theme) persist between sessions
#
# Nothing heavy loads until Start queue is clicked: the app idles at a few
# hundred MB, and only ONE Whisper model is ever kept in memory at a time
# (see transcriber_core). The model stays cached between runs, so a second
# queue with the same model starts instantly.
#
# Run with the venv python (no activation needed):
#   %LOCALAPPDATA%\whisper\.venv\Scripts\pythonw.exe transcribe_ui.py
# or double-click whisper-ui.cmd, or the Start Menu shortcut.

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

# pythonw.exe has no console: give libraries that write to stdout/stderr
# (tqdm download bars etc.) a sink so they do not crash on None streams.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import transcriber_core as core  # sets up caches/token; import before models load

import sv_ttk

try:
    import darkdetect
except ImportError:
    darkdetect = None

# Drag-and-drop is optional: without tkinterdnd2 the Add files... button
# still covers everything.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

# App icon ships next to this script (whisper.ico). Used for the window/title
# bar and the taskbar; an explicit AppUserModelID makes a pinned shortcut group
# with the running window instead of showing the default Python feather icon.
ICON_FILE = Path(__file__).resolve().parent / "whisper.ico"
APP_ID = "Unlimit.WhisperTranscriber"

STATUS_COLOURS = {
    "queued": "",
    "working": "#3b8ed0",
    "done": "#2e9e4f",
    "failed": "#d64545",
    "stopped": "#cc8400",
}


def load_settings():
    try:
        return json.loads(core.SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        core.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        core.SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # settings are a convenience, never fatal


def usable_geometry(saved, default="920x640"):
    """Return the saved geometry only if its title bar lands on a live monitor.

    Positions are saved per-session, but monitors get unplugged or rearranged
    between sessions (dock/undock); restoring a position from a departed
    monitor puts the window somewhere it cannot be seen or grabbed. In that
    case keep the saved size but drop the position so Windows places it.
    """
    import ctypes
    import re

    m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", saved or "")
    if not m:
        return default
    w, h, x, y = (int(g) for g in m.groups())

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    # Test the strip where the title bar sits; MONITOR_DEFAULTTONULL (0)
    # returns NULL when no monitor shows any part of that strip.
    rect = RECT(x, y, x + w, y + 40)
    if ctypes.windll.user32.MonitorFromRect(ctypes.byref(rect), 0):
        return saved
    return f"{w}x{h}"


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        root.title("Whisper transcriber (local, offline)")
        root.geometry(usable_geometry(self.settings.get("geometry", "")))
        root.minsize(760, 480)

        # Light/dark: explicit choice from last session, else follow Windows.
        theme = self.settings.get("theme")
        if theme not in ("light", "dark"):
            theme = (darkdetect.theme() or "light").lower() if darkdetect else "light"
        sv_ttk.set_theme(theme)

        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)
        self.frm = frm

        # --- Settings card ----------------------------------------------------
        card = ttk.LabelFrame(frm, text="Settings", padding=(10, 6))
        card.grid(row=0, column=0, sticky="ew")

        row = ttk.Frame(card)
        row.pack(fill="x")
        ttk.Label(row, text="Model:").pack(side="left")
        self.model_var = tk.StringVar(value=self.settings.get("model", core.MODELS[0]))
        if self.model_var.get() not in core.MODELS:
            self.model_var.set(core.MODELS[0])
        self.model_buttons = {}
        grid = ttk.Frame(row)
        grid.pack(side="left")
        for i, (name, _, hint) in enumerate(core.MODEL_INFO):
            btn = ttk.Radiobutton(grid, text="", variable=self.model_var, value=name)
            btn.grid(row=i // 2, column=i % 2, sticky="w", padx=(10, 0))
            self.model_buttons[name] = (btn, hint)
        self.refresh_model_labels()

        row2 = ttk.Frame(card)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Save as:").pack(side="left")
        self.fmt_var = tk.StringVar(value=self.settings.get("fmt", ".txt"))
        ttk.Radiobutton(row2, text=".txt", variable=self.fmt_var,
                        value=".txt").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(row2, text=".md", variable=self.fmt_var,
                        value=".md").pack(side="left", padx=(10, 0))
        self.ts_var = tk.BooleanVar(value=self.settings.get("timestamps", False))
        ttk.Checkbutton(row2, text="Include timestamps",
                        variable=self.ts_var).pack(side="left", padx=(24, 0))

        row3 = ttk.Frame(card)
        row3.pack(fill="x", pady=(6, 0))
        self.diar_var = tk.BooleanVar(value=self.settings.get("speakers", False))
        diar_ok, diar_reason = core.diarizer_available()
        diar_text = "Identify speakers (labels lines as Speaker 1, Speaker 2, ...)"
        if not diar_ok:
            diar_text += f"  - unavailable: {diar_reason}"
            self.diar_var.set(False)
        self.diar_btn = ttk.Checkbutton(row3, text=diar_text, variable=self.diar_var,
                                        state=("normal" if diar_ok else "disabled"))
        self.diar_btn.pack(side="left")

        # --- Toolbar ----------------------------------------------------------
        bar = ttk.Frame(frm)
        bar.grid(row=1, column=0, sticky="ew", pady=10)
        ttk.Button(bar, text="Add files...",
                   command=self.add_files).pack(side="left")
        ttk.Button(bar, text="Remove selected",
                   command=self.remove_selected).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Clear finished",
                   command=self.clear_finished).pack(side="left", padx=(8, 0))
        # Accent = the main action. Also restarts leftover files after a Stop.
        ttk.Button(bar, text="Start queue", style="Accent.TButton",
                   command=self.start_queue).pack(side="left", padx=(20, 0))
        self.pause_btn = ttk.Button(bar, text="Pause", command=self.toggle_pause,
                                    state="disabled")
        self.pause_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop_job,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Open transcripts folder",
                   command=self.open_folder).pack(side="left", padx=(20, 0))
        self.theme_btn = ttk.Button(bar, width=3, command=self.toggle_theme)
        self.theme_btn.pack(side="right")
        # The only action that goes online: re-runs the installer to repair
        # packages and fetch any missing models.
        ttk.Button(bar, text="Update / repair",
                   command=self.update_app).pack(side="right", padx=(0, 8))
        self.refresh_theme_button()

        # --- Queue list (the centrepiece, grows with the window) ---------------
        qframe = ttk.Frame(frm)
        qframe.grid(row=2, column=0, sticky="nsew")
        frm.rowconfigure(2, weight=3)
        self.tree = ttk.Treeview(qframe, columns=("file", "options", "status"),
                                 show="headings")
        self.tree.heading("file", text="File")
        self.tree.heading("options", text="Options")
        self.tree.heading("status", text="Status")
        self.tree.column("file", width=440)
        self.tree.column("options", width=190, stretch=False)
        self.tree.column("status", width=90, stretch=False)
        for status, colour in STATUS_COLOURS.items():
            if colour:
                self.tree.tag_configure(status, foreground=colour)
        qscroll = ttk.Scrollbar(qframe, command=self.tree.yview)
        self.tree.configure(yscrollcommand=qscroll.set)
        qscroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)

        hint = "Drop audio or video files anywhere in this window to queue them." \
            if DND_AVAILABLE else \
            "Use Add files... to queue audio or video files."
        hint += "  Double-click a finished row to open its transcript."
        ttk.Label(frm, text=hint, foreground="#888888").grid(
            row=3, column=0, sticky="w", pady=(4, 0))

        # --- Collapsible details log -------------------------------------------
        self.log_visible = bool(self.settings.get("log_visible", False))
        self.log_toggle = ttk.Checkbutton(
            frm, text="Show details", style="Switch.TCheckbutton",
            command=self.toggle_log)
        self.log_toggle.state(["selected"] if self.log_visible else ["!selected"])
        self.log_toggle.grid(row=4, column=0, sticky="w", pady=(8, 0))

        self.log_frame = ttk.Frame(frm)
        self.log_frame.grid(row=5, column=0, sticky="nsew", pady=(4, 0))
        self.log_box = tk.Text(self.log_frame, wrap="word", state="disabled",
                               height=8, borderwidth=0)
        lscroll = ttk.Scrollbar(self.log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=lscroll.set)
        lscroll.pack(side="right", fill="y")
        self.log_box.pack(fill="both", expand=True)
        if not self.log_visible:
            self.log_frame.grid_remove()
        frm.rowconfigure(5, weight=2 if self.log_visible else 0)

        # --- Progress + status line (pinned at the bottom) ----------------------
        foot = ttk.Frame(frm)
        foot.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        foot.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(foot, mode="determinate", maximum=100)
        self.bar.grid(row=0, column=0, sticky="ew")
        self.status = ttk.Label(foot, text="Idle", anchor="e")
        self.status.grid(row=0, column=1, padx=(10, 0))

        # --- State (same worker/queue machinery as before) ----------------------
        # Shared queue: the UI appends, the worker pops (both under the lock).
        self.pending = []
        self.pending_lock = threading.Lock()
        self.next_id = 1
        self.worker_running = False
        self.run_opts = ""        # options text for the current run
        self.results = {}         # tree iid -> worker result dict (for open/rename)
        self.errors = {}          # tree iid -> error message for failed rows

        # Progress state, owned by the UI thread and fed by worker events.
        self.job_start = None      # monotonic time the queue run started
        self.file_start = None     # monotonic time the current file started
        self.file_label = ""       # current file name
        self.phase = ""            # e.g. "identifying speakers", "transcribing"
        self.audio_total = None    # current file's audio length in seconds
        self.audio_done = 0.0      # seconds of audio transcribed so far
        # Pause/stop coordination with the worker thread.
        self.cancel_event = None
        self.pause_event = None
        self.paused_accum = 0.0    # total seconds spent paused this run
        self.pause_started = None  # monotonic time the current pause began
        self.file_paused_base = 0.0  # paused total when the current file started

        # Worker threads talk to the UI through this queue (tkinter is not thread-safe).
        self.queue = queue.Queue()
        self.root.after(100, self.tick)

        if DND_AVAILABLE:
            root.drop_target_register(DND_FILES)
            root.dnd_bind("<<Drop>>", self.on_drop)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.log(f"Transcripts save to: {core.EXTRACT_DIR}")
        self.log(f"Model cache: {core.CACHE_DIR}")
        self.log("Network: offline. Audio and transcripts stay on this machine; "
                 "only Update / repair (and a one-off model download) goes online.")
        self.log("Models load into memory when you start the queue "
                 "(then stay loaded for follow-up runs).\n")

    # -- helpers ---------------------------------------------------------------

    def emit(self, kind, payload=None):
        """Thread-safe event: queue it for the UI thread to handle."""
        self.queue.put((kind, payload))

    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def refresh_model_labels(self):
        """Radio button labels reflect whether weights are on disk yet."""
        for name, (btn, hint) in self.model_buttons.items():
            label = f"{name} ({hint})"
            state, _ = core.model_status(name)
            if state == "invalid":
                label += " - needs repair"
            elif state != "ready":
                size = core.MODEL_DOWNLOAD_SIZES.get(name, "large")
                label += f" - {size} download required"
            btn.configure(text=label)

    def refresh_theme_button(self):
        self.theme_btn.configure(
            text="☀" if sv_ttk.get_theme() == "dark" else "\U0001f319")

    def toggle_theme(self):
        sv_ttk.set_theme("light" if sv_ttk.get_theme() == "dark" else "dark")
        self.refresh_theme_button()

    def toggle_log(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_frame.grid()
        else:
            self.log_frame.grid_remove()
        self.frm.rowconfigure(5, weight=2 if self.log_visible else 0)
        self.log_toggle.state(["selected"] if self.log_visible else ["!selected"])

    def paused_total(self, now):
        """Total seconds spent paused this run, including a pause in progress."""
        current = (now - self.pause_started) if self.pause_started is not None else 0.0
        return self.paused_accum + current

    def queued_count(self):
        with self.pending_lock:
            return len(self.pending)

    def set_status_tag(self, iid, status):
        self.tree.set(iid, "status", status)
        self.tree.item(iid, tags=(status,))

    def tick(self):
        """UI-thread heartbeat: drain worker events, then refresh the status line."""
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "item_status":
                    iid, status = str(payload[0]), payload[1]
                    if self.tree.exists(iid):
                        self.set_status_tag(iid, status)
                        if status == "working":
                            # Show the options actually being used for this run.
                            self.tree.set(iid, "options", self.run_opts)
                elif kind == "item_result":
                    self.results[str(payload[0])] = payload[1]
                elif kind == "item_error":
                    self.errors[str(payload[0])] = payload[1]
                elif kind == "file_start":
                    self.file_label = payload
                    self.file_start = time.monotonic()
                    self.file_paused_base = self.paused_total(self.file_start)
                    self.phase = "preparing"
                    self.audio_total = None
                    self.audio_done = 0.0
                    self.set_bar_indeterminate(True)  # warming up / scanning audio
                elif kind == "phase":
                    self.phase = payload
                elif kind == "duration":
                    self.audio_total = payload or None
                    self.set_bar_indeterminate(False)
                elif kind == "progress":
                    self.audio_done = payload
                    if self.audio_total:
                        self.bar.configure(
                            value=min(100.0, 100.0 * self.audio_done / self.audio_total))
                elif kind == "file_done":
                    self.bar.configure(value=100)
                elif kind == "done":
                    now = time.monotonic()
                    elapsed = (now - self.job_start - self.paused_total(now)) \
                        if self.job_start else 0
                    self.job_start = None
                    self.pause_started = None
                    self.phase = ""
                    self.worker_running = False
                    if str(self.bar.cget("mode")) == "indeterminate":
                        self.set_bar_indeterminate(False)
                    result = payload or "ok"
                    if result == "ok":
                        self.bar.configure(value=100)
                        self.status.configure(text=f"Done in {core.format_ts(elapsed)}")
                    elif result == "stopped":
                        self.status.configure(text=f"Stopped after {core.format_ts(elapsed)}")
                    else:
                        self.status.configure(text=f"Failed after {core.format_ts(elapsed)}")
                    self.stop_btn.configure(state="disabled")
                    self.pause_btn.configure(state="disabled", text="Pause")
                    self.refresh_model_labels()  # an on-demand download may have finished
                    # No auto-restart: leftover files wait for Start queue.
                    leftover = self.queued_count()
                    if leftover:
                        self.status.configure(
                            text=self.status.cget("text")
                            + f"  |  {leftover} queued (click Start queue)")
        except queue.Empty:
            pass

        # Refresh the timer/percent/ETA readout while a run is in progress.
        if self.job_start is not None:
            now = time.monotonic()
            paused = self.paused_total(now)
            working = now - self.job_start - paused
            is_paused = self.pause_event is not None and self.pause_event.is_set()
            parts = [self.file_label or "Starting..."]
            queued = self.queued_count()
            if queued:
                parts.append(f"{queued} queued")
            if self.cancel_event is not None and self.cancel_event.is_set():
                parts.append("stopping...")
            elif is_paused:
                parts.append("PAUSED")
            elif self.phase:
                parts.append(self.phase)
            parts.append(f"elapsed {core.format_ts(working)}")
            if self.audio_total:
                pct = min(100.0, 100.0 * self.audio_done / self.audio_total)
                parts.append(f"{pct:.0f}%")
                # Estimate time left for this file from its own working pace
                # (pause time excluded so the estimate stays honest).
                if pct > 3 and not is_paused:
                    file_paused = paused - self.file_paused_base
                    file_elapsed = now - (self.file_start or self.job_start) - file_paused
                    parts.append(f"~{core.format_ts(file_elapsed * (100 - pct) / pct)} left")
            self.status.configure(text="  |  ".join(parts))

        self.root.after(100, self.tick)

    def set_bar_indeterminate(self, on):
        """Marquee animation while we cannot compute a percentage yet."""
        if on:
            self.bar.configure(mode="indeterminate")
            self.bar.start(12)
        else:
            self.bar.stop()
            self.bar.configure(mode="determinate", value=0)

    def open_folder(self):
        core.EXTRACT_DIR.mkdir(exist_ok=True)
        os.startfile(core.EXTRACT_DIR)

    def on_close(self):
        self.settings.update({
            "model": self.model_var.get(),
            "fmt": self.fmt_var.get(),
            "timestamps": self.ts_var.get(),
            "speakers": self.diar_var.get(),
            "theme": sv_ttk.get_theme(),
            "geometry": self.root.geometry(),
            "log_visible": self.log_visible,
            "last_dir": self.settings.get("last_dir", ""),
        })
        save_settings(self.settings)
        self.root.destroy()

    # -- queue actions -----------------------------------------------------------

    def add_files(self):
        """Add files to the queue. Nothing starts until Start queue is clicked;
        options are applied at that point, not now."""
        paths = filedialog.askopenfilenames(
            title="Choose audio or video files", filetypes=core.FILETYPES,
            initialdir=self.settings.get("last_dir") or None)
        if paths:
            self.settings["last_dir"] = str(Path(paths[0]).parent)
            self.add_paths(paths)

    def on_drop(self, event):
        paths = self.root.tk.splitlist(event.data)
        good = [p for p in paths
                if Path(p).is_file() and Path(p).suffix.lower() in core.AUDIO_EXTENSIONS]
        skipped = len(paths) - len(good)
        if skipped:
            self.log(f"Ignored {skipped} dropped item(s) that are not audio/video files.")
        self.add_paths(good)

    def add_paths(self, paths):
        for p in paths:
            item = {"id": self.next_id, "path": p}
            self.next_id += 1
            with self.pending_lock:
                self.pending.append(item)
            self.tree.insert("", "end", iid=str(item["id"]),
                             values=(Path(p).name, "", "queued"))
        if paths and not self.worker_running:
            self.log(f"Added {len(paths)} file(s). Set your options, then click Start queue.")

    def remove_selected(self):
        """Remove selected files that have not started yet (and finished rows)."""
        for iid in self.tree.selection():
            removed = False
            with self.pending_lock:
                for i, item in enumerate(self.pending):
                    if str(item["id"]) == iid:
                        del self.pending[i]
                        removed = True
                        break
            status = self.tree.set(iid, "status") if self.tree.exists(iid) else ""
            if removed or status in ("done", "failed", "stopped"):
                self.forget_row(iid)

    def clear_finished(self):
        for iid in list(self.tree.get_children()):
            if self.tree.set(iid, "status") in ("done", "failed", "stopped"):
                self.forget_row(iid)

    def forget_row(self, iid):
        self.tree.delete(iid)
        self.results.pop(iid, None)
        self.errors.pop(iid, None)

    # -- row interactions ----------------------------------------------------------

    def on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.open_row_transcript(iid)

    def open_row_transcript(self, iid):
        result = self.results.get(iid)
        if result and Path(result["out"]).exists():
            os.startfile(result["out"])
        elif iid in self.errors:
            messagebox.showerror("Transcription failed", self.errors[iid])

    def on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        menu = tk.Menu(self.root, tearoff=0)
        result = self.results.get(iid)
        if result:
            menu.add_command(label="Open transcript",
                             command=lambda: self.open_row_transcript(iid))
            menu.add_command(label="Show in folder",
                             command=lambda: subprocess.Popen(
                                 ["explorer", "/select,", result["out"]]))
            if result["speakers"]:
                menu.add_command(label="Rename speakers...",
                                 command=lambda: self.rename_speakers_dialog(iid))
            menu.add_separator()
        if iid in self.errors:
            menu.add_command(label="Show error",
                             command=lambda: messagebox.showerror(
                                 "Transcription failed", self.errors[iid]))
            menu.add_separator()
        menu.add_command(label="Remove from list",
                         command=lambda: self.remove_row(iid))
        menu.tk_popup(event.x_root, event.y_root)

    def remove_row(self, iid):
        self.tree.selection_set(iid)
        self.remove_selected()

    def rename_speakers_dialog(self, iid):
        """Rename Speaker 1/2/... to real names; the saved file is rewritten."""
        result = self.results.get(iid)
        if not result:
            return
        # Speakers in order of first appearance, each with their first line
        # as a hint for working out who is who.
        speakers, first_line = [], {}
        for who, text, _ in result["entries"]:
            if who and who not in first_line:
                speakers.append(who)
                first_line[who] = text
        if not speakers:
            messagebox.showinfo("Rename speakers", "No speakers in this transcript.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Rename speakers")
        dlg.transient(self.root)
        dlg.grab_set()
        body = ttk.Frame(dlg, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="First line spoken is shown to help identify each person."
                  ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        entries = {}
        for i, who in enumerate(speakers):
            ttk.Label(body, text=who + ":").grid(row=1 + 2 * i, column=0,
                                                 sticky="w", padx=(0, 8))
            var = tk.StringVar(value=who)
            ttk.Entry(body, textvariable=var, width=32).grid(
                row=1 + 2 * i, column=1, sticky="ew")
            hint = first_line[who]
            if len(hint) > 70:
                hint = hint[:70] + "..."
            ttk.Label(body, text=f'"{hint}"', foreground="#888888").grid(
                row=2 + 2 * i, column=1, sticky="w", pady=(0, 6))
            entries[who] = var
        body.columnconfigure(1, weight=1)

        def apply():
            mapping = {who: var.get().strip() for who, var in entries.items()
                       if var.get().strip() and var.get().strip() != who}
            if mapping:
                try:
                    core.rename_speakers(result, mapping)
                except OSError as exc:
                    messagebox.showerror("Rename speakers",
                                         f"Could not rewrite the transcript:\n{exc}")
                    return
                self.log("Renamed " + ", ".join(f"{a} -> {b}" for a, b in mapping.items())
                         + f" in {result['out']}")
            dlg.destroy()

        btns = ttk.Frame(body)
        btns.grid(row=1 + 2 * len(speakers), column=0, columnspan=2,
                  sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="Rename", style="Accent.TButton",
                   command=apply).pack(side="right")

    # -- maintenance -----------------------------------------------------------------

    def update_app(self):
        """Re-run the installer to repair Python packages and fetch any missing
        models. This is the ONLY thing that uses the internet. It does NOT pull
        new code from anywhere - it refreshes this local install in place."""
        if self.worker_running:
            messagebox.showinfo("Update / repair",
                                "Finish or stop the current queue first.")
            return
        repo = Path(__file__).resolve().parent
        installer = repo / "install.ps1"
        if not installer.exists():
            messagebox.showerror("Update / repair",
                                 f"Installer not found:\n{installer}")
            return
        # Keep whatever model set is already installed; don't pull large-v3 onto
        # a machine that only has turbo, or Whisper onto a Parakeet-only one.
        # The installer always verifies (and repairs) Parakeet models on disk.
        if core.model_downloaded("large-v3"):
            models = "full"
        elif core.model_downloaded("large-v3-turbo"):
            models = "turbo"
        else:
            models = "parakeet"
        if not messagebox.askyesno(
                "Update / repair",
                "This re-runs setup in a separate window to:\n"
                "  - reinstall the Python packages at their tested versions\n"
                "  - download any speech models that are missing\n"
                "  - check Parakeet model files and replace damaged ones\n\n"
                "It needs the internet for this step only. Your audio and "
                "transcripts never leave the machine, and normal transcription "
                "stays fully offline.\n\n"
                "It may take a few minutes. Restart the transcriber afterwards "
                "if anything was updated.\n\nContinue?"):
            return
        # The installer (and its download steps) must be allowed online, so drop
        # the offline flags for the child. PowerShell does not import
        # transcriber_core, so it won't re-add them.
        env = os.environ.copy()
        for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            env.pop(k, None)
        # If a token exists, let the installer verify it (confirms speaker ID
        # still works, no prompt appears). Only skip the token step when there
        # is genuinely no token, so an unattended repair never stops to ask.
        args = ["-Models", models]
        if not os.environ.get("HF_TOKEN"):
            args.append("-SkipToken")
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NoExit", "-ExecutionPolicy", "Bypass",
                 "-File", str(installer)] + args,
                cwd=str(repo), env=env,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self.log("Launched Update / repair in a separate window. "
                     "Restart the transcriber when it finishes.")
        except Exception as exc:
            messagebox.showerror("Update / repair",
                                 f"Could not start the updater:\n{exc}")

    # -- run control -----------------------------------------------------------------

    def start_queue(self):
        """Start the worker if it is not already running and work is waiting.
        The options are read NOW and apply to this whole run."""
        if self.worker_running or not self.queued_count():
            return
        model = self.model_var.get()
        state, detail = core.model_status(model)
        size = core.MODEL_DOWNLOAD_SIZES.get(model, "several GB")
        if state == "invalid":
            if not messagebox.askyesno(
                    "Repair model?",
                    f"The installed {model} files are not usable:\n{detail}\n\n"
                    f"Download a fresh, verified copy ({size}, needs internet)? "
                    f"The damaged copy is kept aside, not deleted."):
                return
        elif state != "ready":
            if not messagebox.askyesno(
                    "Download model?",
                    f"{model} is not installed yet. It is a one-off {size} download "
                    f"(needs internet; saved locally for offline use after that).\n\n"
                    f"Download it now?"):
                return
        settings = {
            "model": model,
            "fmt": self.fmt_var.get(),
            "speakers": self.diar_var.get(),
            "timestamps": self.ts_var.get(),
        }
        self.run_opts = core.MODEL_SHORT.get(model, model) \
            + ", " + settings["fmt"].lstrip(".") \
            + (", speakers" if settings["speakers"] else "") \
            + (", ts" if settings["timestamps"] else "")
        self.log(f"Starting queue: {model}, {settings['fmt']}, "
                 f"speakers {'on' if settings['speakers'] else 'off'}, "
                 f"timestamps {'on' if settings['timestamps'] else 'off'}")
        self.worker_running = True
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.job_start = time.monotonic()
        self.file_start = None
        self.file_label = ""
        self.phase = ""
        self.audio_total = None
        self.audio_done = 0.0
        self.paused_accum = 0.0
        self.pause_started = None
        self.file_paused_base = 0.0
        self.pause_btn.configure(state="normal", text="Pause")
        self.stop_btn.configure(state="normal")
        self.set_bar_indeterminate(True)
        threading.Thread(
            target=core.queue_worker,
            args=(self.pending, self.pending_lock, settings, self.emit,
                  self.cancel_event, self.pause_event),
            daemon=True,
        ).start()

    def toggle_pause(self):
        if self.job_start is None or self.pause_event is None:
            return
        if self.pause_event.is_set():
            # Resume: bank the paused time so timers and ETA exclude it.
            self.pause_event.clear()
            if self.pause_started is not None:
                self.paused_accum += time.monotonic() - self.pause_started
                self.pause_started = None
            self.pause_btn.configure(text="Pause")
            if str(self.bar.cget("mode")) == "indeterminate":
                self.bar.start(12)
        else:
            self.pause_event.set()
            self.pause_started = time.monotonic()
            self.pause_btn.configure(text="Resume")
            if str(self.bar.cget("mode")) == "indeterminate":
                self.bar.stop()

    def stop_job(self):
        """Stop the current file and the rest of the queue. Files not yet
        started stay listed as queued; click Start queue to resume them."""
        if self.cancel_event is None:
            return
        self.cancel_event.set()
        # Unpause so the worker can wake up and exit promptly.
        if self.pause_started is not None:
            self.paused_accum += time.monotonic() - self.pause_started
            self.pause_started = None
        if self.pause_event is not None:
            self.pause_event.clear()
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled", text="Pause")


def set_app_user_model_id():
    # Must run before any window is shown so Windows ties the taskbar button
    # to our pinned shortcut. Harmless if it fails (non-Windows, old shell).
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def set_window_icon(root):
    try:
        if ICON_FILE.exists():
            # default=... applies to the root and every Toplevel (e.g. dialogs)
            root.iconbitmap(default=str(ICON_FILE))
    except Exception:
        pass  # icon is cosmetic, never fatal


def main():
    set_app_user_model_id()
    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    set_window_icon(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # pythonw.exe has no console, so surface fatal errors in a dialog instead.
        messagebox.showerror("Whisper transcriber", str(exc))
        raise
