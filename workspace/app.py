# workspace/app.py
# The state-owning workspace controller (#20). One instance per app process.
#
# It owns the Store, the media folder and the worker, and runs ONE background
# processing thread that does, in order of priority:
#   1. cleanup jobs that are due (#17/#18) - before any new processing;
#   2. transcription of the oldest unfinished, unpaused meeting (#14/#15);
#   3. speaker separation for a transcribed meeting (#16).
# Imports (hash + decode) run on a separate intake thread so a long
# transcription never blocks adding the next recording.
#
# The UI (workspace/api.py) only calls the public methods below. They return
# plain data and raise WorkspaceError with a message that is safe to show;
# they never return file paths. Work is found by scanning the database, so a
# restart resumes exactly where the last session stopped, and explicit
# pauses stay paused.

import json
import os
import queue
import re
import threading
import time
import unicodedata
from pathlib import Path

from workspace import asr_engines
from workspace import (audit, cleanup, clipboard, credentials, diag, drafts, engines, gpu,
                       handoff, holds, instance, intake, keystore,
                       media, policy, power, recorder,
                       resources, runner, setup, speaker_names, speakers)
from workspace.chunking import plan_chunks
from workspace import consent as recording_consent
# The workspace's own files and folders (#111 C7): named once, in diag (and
# the lid's restore record in power).
from workspace.diag import (AUDIT_FOLDER, DB_FILE, LOGS_FOLDER, SETTINGS_FILE,
                            SETTINGS_UNREADABLE_FILE)
from workspace.safe_paths import UnsafePathError, validate_input_file
from workspace.store import (DAMAGED_REFUSAL, DamagedContent, KeyMissing, Store, StoreError,
                             default_root, mark_compaction_owed, quick_check_file)
from workspace.worker import GpuWhisperEngine, WorkerEngine, WorkerHandle, safe_message

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".flac",
                    ".ogg", ".aac", ".wma", ".webm", ".avi")
MAX_IMPORT_BYTES = 8 << 30          # 8 GB: several hours of video
MAX_CLIP_S = 30.0
PLAYBACK_GAINS_KEPT = 64                 # meetings whose playback gain is kept in memory
MAX_EDIT_CHARS = 5000
MAX_TITLE_CHARS = 200
FAILED_RETRY_S = 60.0               # a failed cleanup is retried at most this often
RECEIPT_PURGE_S = 3600.0
SR = 16000
# The meeting's audio cannot be played or planned (#111 C3a). Fixed text (P3);
# the processing thread's notes are in runner (damaged_audio_note,
# missing_audio_note) and speakers (AUDIO_UNREADABLE).
AUDIO_UNREADABLE = "The meeting audio could not be read. It may be damaged."
# Background threads wait for work rather than waking every second (R7). A
# wait does not count time asleep on Windows, so long waits are capped: the
# clock is checked again within the cap after the computer wakes.
WATCH_MAX_WAIT_S = 60.0             # watched folders: a scan is at most this late
POLICY_IDLE_WAIT_S = 30.0           # policy thread when not recording
LID_RETRY_S = 60.0                  # a lid restore that failed is tried again this often
# Settings and Setup are polled while open; what they show from disk (model
# and component files) or Credential Manager is worked out at most this often,
# and again at once after a download, removal, token or settings change (R7b).
DISPLAY_CACHE_S = 60.0
PAGE_ERRORS = 20                    # page script errors recorded per run (report_ui_error)
# Copy and open (#109): after one opening of the assistant's page, another is
# refused for this long (a double click, or a page calling it in a loop). The
# same 3 seconds as api.DECLINE_PAUSE_S (a test keeps them equal; api imports
# this module, so it cannot be imported from there).
ASSISTANT_PAUSE_S = 3.0
# Why a click on Copy and open is refused when policy withholds it
# (Workspace._assistant_withheld), worded as Settings words it.
ASSISTANT_WITHHELD = {
    "off": "Your organisation has turned this off. Use Copy prompt and transcript instead.",
    "invalid": ("Your organisation's setting for Copy and open is not valid, so it is not "
                "available. Use Copy prompt and transcript instead, and ask your IT team."),
    "unreadable": ("Your organisation's settings could not be read, so Copy and open is not "
                   "available. Use Copy prompt and transcript instead, and ask your IT team."),
}
SETTINGS_VERSION = 1                # settings.json format (R11): raise if a key's meaning changes
SETTINGS_MAX_BYTES = 64 * 1024      # a larger settings.json is not one the app wrote

# The speech models Settings offers, in the order it lists them.
SPEECH_MODELS = (*asr_engines.PARAKEET_MODELS, *asr_engines.WHISPER_MODELS)

DEFAULT_SETTINGS = {
    "mode": resources.QUIET,                       # quiet | faster
    "model": asr_engines.RECOMMENDED_PARAKEET,
    "speakers": True,                              # separate speakers after transcription
    "battery_override": False,                     # process automatically on battery
    "onboarded": False,                            # guided setup finished or skipped (#19)
    "microphone": False,                           # microphone recording (#30), off by default
    "computer_audio": False,                       # computer audio as a second track (#108)
    "gpu_speakers": False,                         # NVIDIA for speaker identification (Faster)
    "recording_display": "flow",                   # flow | spectrum | wave, while recording
    "lid_override": False,                         # lid "Do nothing" while recording (#172)
    "live_drafts": False,                          # draft text while recording (#104), off
    # What the draft follows with computer audio on (#104, the owner's
    # decision of 22 Oct 2026): microphone | mix (both tracks, mixed live).
    "live_drafts_audio": "microphone",
}
RECORDING_DISPLAYS = ("flow", "spectrum", "wave")


MANAGED_OFF = "{what} is turned off by your organisation. Ask your IT team if you need it."
DOWNLOADS_OFF = ("Downloads are turned off by your organisation. Ask your IT team to install "
                 "the models.")
# A machine install whose policy uses only the models IT installed (#111 B7a).
USER_MODELS_OFF = ("Your organisation uses only the models your IT team installs on this "
                   "computer. Ask your IT team to install or repair it.")
# Every request refused while the workspace is locked (#111 C4a): the bridge
# gate (api.LOCKED_METHODS) and the Workspace's own refusals give this.
LOCKED_TEXT = ("Unminute is locked: Windows could not unlock its key on this computer. "
               "Ask your IT team.")
# Finish-only mode (#111 B5b, the owner's decision B5-7): the version kept for
# an older workspace that still holds meetings opens it for finishing them
# only. New imports, watched-folder intake and recordings are refused, by the
# bridge (api.FINISH_ONLY_REFUSED) and by the Workspace itself; review,
# editing, handoff, cleanup, holds, Setup and settings work. The page shows
# FINISH_ONLY_BANNER.
FINISH_ONLY_TEXT = ("An update is waiting for this workspace, so this version is for "
                    "finishing your meetings only. New meetings open in the updated version "
                    "once this workspace is empty.")
FINISH_ONLY_BANNER = ("An update is waiting for this workspace. Finish your meetings here; new "
                      "meetings open in the updated version once this workspace is empty.")
# The unused graphics card component removal (B5-4) waits this long after
# start-up, so it never slows the window opening.
GPU_UPKEEP_DELAY_S = 60

# Computer audio (#108): a recording opens a computer track only when this is
# True, the user's switch is on and policy allows it, and the confirmation
# named it (wording version 2). The page part of #108 turned it on; setting it
# back to False hides the switch's effect again (set_settings refuses it and
# no recording opens a computer track), as the tests check.
COMPUTER_AUDIO_STARTS = True
SOURCES_CHANGED = ("What would be recorded changed while you were confirming (computer audio "
                   "was turned on or off). Start the recording again.")
COMPUTER_POLICY_STOP = ("Your organisation turned off computer audio. Only the microphone "
                        "is recorded from here.")


class WorkspaceError(ValueError):
    """A refused request. The message is safe to show in the UI."""


def _key_line(locked, code):
    """Copy diagnostics' line for the workspace key (#111 C4a): "Key: ok", or
    the reason, the Windows error number in decimal and hexadecimal, and the
    incident code. Fixed words, numbers and codes only (P3)."""
    if locked is None:
        return "Key: ok"
    parts = [locked.reason if locked.reason in diag.KEY_REASONS else "?"]
    win = diag.windows_error(locked.code)
    if win is not None:
        parts.append(f"Windows error {win} = 0x{win & 0xFFFFFFFF:08X}")
    if code:
        parts.append(f"code {code}")
    return f"Key: locked ({', '.join(parts)})"


def _model_note(name, state, downloads_allowed=True):
    """What Settings says beside the picker about a speech model that is not
    ready, and why Transcribe again refuses it (#244); None when it is ready.
    Manage downloads, which offers every speech model (#246), is named only
    when managed policy allows downloads (K3)."""
    if state == "ready":
        return None
    label = asr_engines.label(name)
    what = "damaged" if state == "invalid" else "not downloaded"
    if not downloads_allowed:
        return (f"{label} is {what}, and downloads are turned off by your organisation. "
                "Choose another speech model, or ask your IT team to install it.")
    if state == "invalid":
        return (f"{label} is damaged. Repair it in Settings > Manage downloads, or choose "
                "another speech model.")
    return (f"{label} is not downloaded. Download it in Settings > Manage downloads, or "
            "choose another speech model.")


_JOINERS = "\u200c\u200d"          # zero-width non-joiner and joiner (_plain_label)


def _plain_label(text):
    """A title or speaker name without invisible format characters (Unicode
    category Cf): a right-to-left override (U+202E) could make the meeting
    list show a disguised name (#111 D2). The zero-width non-joiner and
    joiner (U+200C, U+200D) stay: Persian and Indic scripts need them for
    correct spelling, and they do not reorder text. The handoff file name
    strips format characters too (handoff.title_filename)."""
    return "".join(ch for ch in text
                   if ch in _JOINERS or unicodedata.category(ch) != "Cf")


def _clean_text(text, limit, label=False):
    """Text from the page: control characters other than line breaks and
    tabs go and, for a title or speaker name (label), format characters too."""
    if not isinstance(text, str):
        raise WorkspaceError("Expected text.")
    text = "".join(ch for ch in text if ch in "\n\t" or ch >= " ")
    text = (_plain_label(text) if label else text).strip()
    if len(text) > limit:
        raise WorkspaceError(f"That is too long (limit {limit} characters).")
    return text


class Settings:
    """Configuration only (no content), kept beside the database.

    Values read from settings.json pass the same checks as values set from
    the page: the file is writable by anything running as the user, so an
    invalid value falls back to its default instead of being trusted. Test
    engines ("test:module:function") are accepted only when the tests ask for
    them (allow_test_engines); they would otherwise let a settings value
    import any module in the worker."""

    def __init__(self, path, allow_test_engines=False):
        self.path = Path(path)
        self.allow_test_engines = allow_test_engines
        self.values = dict(DEFAULT_SETTINGS)
        self.unreadable = False            # the file exists but could not be read: all defaults
        self._locked = False               # ...because it could not be opened (in use)
        self._save_lock = threading.Lock()
        self._load(self._read())

    def _load(self, saved):
        """Apply saved settings over the defaults."""
        self.values = dict(DEFAULT_SETTINGS)
        self.repaired = []                 # keys on their default: the saved value was refused
        self.version = SETTINGS_VERSION
        # Saved values this version does not use, written back unchanged (R11):
        # settings only a newer version knows, and a newer version's value for
        # a setting this one knows but cannot use. A rollback then loses none.
        self._kept = {}
        version = saved.pop("settings_version", 0)
        newer = isinstance(version, int) and not isinstance(version, bool) \
            and SETTINGS_VERSION < version < 10 ** 6
        if newer:
            self.version = version
        for key, value in saved.items():
            if key not in DEFAULT_SETTINGS:
                self._kept[key] = value
                continue
            try:
                self._check(key, value)
                self.values[key] = value
            except WorkspaceError:
                self.repaired.append(key)
                if newer:
                    self._kept[key] = value

    def _read(self):
        """The saved settings as a dict ({} if there is no file yet). Sets
        unreadable (and _locked, if the file could not even be opened)."""
        self.unreadable = self._locked = False
        data = None
        for attempt in range(2):                # once more: an antivirus scan may hold it
            try:
                with open(self.path, "rb") as f:
                    data = f.read(SETTINGS_MAX_BYTES + 1)
                break
            except FileNotFoundError:
                return {}
            except OSError:
                time.sleep(0.2 * (1 - attempt))
        if data is None:                        # in use: unknown, not damaged
            self.unreadable = self._locked = True
            return {}
        try:
            saved = json.loads(data.decode("utf-8-sig")) \
                if len(data) <= SETTINGS_MAX_BYTES else None
            json.dumps(saved, indent=2)         # too deeply nested to write back: unreadable
        except (ValueError, RecursionError):
            saved = None
        if not isinstance(saved, dict):
            self.unreadable = True
            return {}
        return saved

    def __getitem__(self, key):
        return self.values[key]

    def _check(self, key, value):
        if key not in DEFAULT_SETTINGS:
            raise WorkspaceError("Unknown setting.")
        if key == "mode" and value not in (resources.QUIET, resources.FASTER):
            raise WorkspaceError("Choose Quiet or Faster.")
        if key == "recording_display" and value not in RECORDING_DISPLAYS:
            raise WorkspaceError("Choose Flowing wave, Spectrum or Sound wave.")
        if key == "live_drafts_audio" and value not in drafts.AUDIO_CHOICES:
            raise WorkspaceError("Choose the microphone, or the microphone and computer audio.")
        if key == "model" and not (isinstance(value, str) and (
                asr_engines.is_parakeet(value) or asr_engines.is_whisper(value)
                or (self.allow_test_engines and value.startswith(engines.TEST_PREFIX)))):
            raise WorkspaceError("Unknown speech model.")
        if (key in ("speakers", "battery_override", "onboarded", "microphone",
                    "computer_audio", "gpu_speakers", "lid_override", "live_drafts")
                and not isinstance(value, bool)):
            raise WorkspaceError("Expected on or off.")

    def update(self, **changes):
        for key, value in changes.items():
            self._check(key, value)
        with self._save_lock:                      # page calls run on their own threads
            if self._locked:
                # It could not be opened at start: read it now, rather than
                # replace settings that may be good with the defaults.
                self._load(self._read())
                if self._locked:
                    raise WorkspaceError("Unminute cannot open its settings file just now. "
                                         "Try again in a moment.")
            values = {**self.values, **changes}
            kept = {k: v for k, v in self._kept.items() if k not in changes}
            # settings_version is the newest version that wrote the file. A key's
            # meaning never changes between versions: a new meaning takes a new key.
            saved = {"settings_version": self.version, **values, **kept}
            text = json.dumps(saved, indent=2)
            if len(text.encode("utf-8")) > SETTINGS_MAX_BYTES:
                text = json.dumps(saved, separators=(",", ":"))
            if len(text.encode("utf-8")) > SETTINGS_MAX_BYTES:
                kept = {}                          # not a file the app wrote: drop the extras
                text = json.dumps({"settings_version": self.version, **values})
            if self.unreadable and self.path.exists():
                # Copied once, aside, before it is replaced: it may hold values
                # a newer version wrote, or the read failed only for a moment.
                # A copy, so a write that fails next still leaves it in place.
                import shutil
                try:
                    shutil.copyfile(self.path, self.path.with_name(SETTINGS_UNREADABLE_FILE))
                except OSError:
                    pass
            # Written in full or not at all: flushed to disk before the swap,
            # so a power cut leaves the old file or the new one, never a part.
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(3):
                try:
                    tmp.replace(self.path)
                    break
                except PermissionError:            # held open for a moment (an antivirus scan)
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
            # Only once the file is saved does the app use the new values.
            self.values, self._kept = values, kept
            self.repaired = [k for k in self.repaired if k not in changes]
            self.unreadable = False


class Workspace:
    def __init__(self, root=None, cache_dir=None, *, speaker_model="pyannote",
                 idle_unload_s=None, battery=None, start=True, allow_test_engines=False,
                 policy_reader=None, check_integrity=False, lid_switch=None, protector=None,
                 on_unavailable="raise", opener=None, audit_mirror=None,
                 instance_lock=False, finish_only=False):
        """allow_test_engines: tests and the UI check tools only. Lets the
        settings, the speaker model and the worker use "test:module:function"
        engines; never set by the app itself. policy_reader: tests only; the
        app reads HKLM\\SOFTWARE\\Policies\\Unminute (workspace.policy).
        check_integrity: the app's own start (R11): check the database when
        the app last ended without closing, or the last check found damage.
        lid_switch: tests only; a power.LidSwitch with a fake power scheme.
        protector, on_unavailable: passed to the Store (#111 C2b-2): the key
        protector (Windows Data Protection unless given; tests give their
        own, which the store refuses for the real workspace) and what
        happens when the key cannot be used: "raise", or "lock" (the app's
        own start, #111 C4a), when the workspace opens locked (self.locked):
        nothing is read but counts, nothing is written, and every job,
        import, scan, recording and the start-up upkeep waits.
        opener: tests and the UI check tools only; called with the assistant's
        address instead of asking Windows to open it (copy_and_open_assistant).
        audit_mirror: tests and the packaged checks only; receives the audit
        log's Event Log copies instead of Windows (#111 C5). The packaged
        checks pass one that drops them (review of #335).
        instance_lock: the app's own start (#111 B5): hold the workspace lock
        (workspace/instance.py) for this Workspace's life, so another Windows
        session of the same user cannot open the workspace at the same time.
        Raises instance.Held when another process holds it.
        finish_only: the app's own start as compat (#111 B5b): no new imports,
        watched-folder intake or recordings (FINISH_ONLY_TEXT)."""
        if str(speaker_model).startswith(engines.TEST_PREFIX) and not allow_test_engines:
            raise ValueError("Test engines are for tests only.")
        self.root = Path(root or default_root())
        self.root.mkdir(parents=True, exist_ok=True)
        self.finish_only = finish_only is True
        # The workspace lock (#111 B5): before the lid, the database check
        # and the store, so nothing is read, written or migrated while
        # another session of this user has the workspace open.
        self._instance = self._take_instance() if instance_lock else None
        # The lid (#172): a "Do nothing" left by a crash or power cut is put
        # back first of all, before anything that could fail (the database
        # may be damaged by the same power cut). It never stops the start.
        self.lid = lid_switch or power.LidSwitch(self.root / power.RESTORE_FILE)
        try:
            self.lid.restore()
        except Exception:
            pass
        self.media = self.root / "media"
        # Content-free diagnostics (R9): workspace\logs. The app's start
        # (__main__) installs it as the process's log; tests read it directly.
        self.log = diag.Log(self.root / LOGS_FOLDER, "app")
        self._integrity = {"state": None, "code": None}
        self._check_database(check_integrity)
        held = self._integrity["state"] == "failed"
        self._integrity_ok = threading.Event()      # set: work may start
        # A damaged database opens without marking interrupted work, and the
        # start-up upkeep below waits too: on damaged pages it could delete.
        self.store = Store(self.root / DB_FILE, housekeeping=not held,
                           protector=protector, on_unavailable=on_unavailable)
        self.store.log = self.log         # damaged values are recorded here (content-free)
        # Locked (#111 C4a): the key cannot be used, so nothing can be opened
        # or written. Work is held as for damage, but nothing releases it in
        # this run (no Continue anyway): the next start tries the key again.
        self.locked = self.store.locked
        self._lock_code = None            # the incident code of a locked start
        self._record_keys()
        if not held and self.locked is None:
            self._integrity_ok.set()
        self.audit = self._open_audit(audit_mirror)
        self.settings = Settings(self.root / SETTINGS_FILE,
                                 allow_test_engines=allow_test_engines)
        # Handoff templates are built at their first use, not here (#111 C2c):
        # nothing is read, converted or logged while the key is unavailable.
        self._templates = None
        self._templates_lock = threading.Lock()
        self.policy = policy.PolicySource(reader=policy_reader or policy.read_registry)
        self._clipboard_clears = []       # (due, sequence) for ClipboardClearMinutes
        self._clipboard_lock = threading.Lock()
        self._policy_wake = threading.Event()   # a clear was scheduled, or closing
        self._opener = opener
        self._assistant_lock = threading.Lock()
        self._assistant_opened_at = None  # when copy_and_open last opened (self._clock)
        self._clock = time.monotonic      # counts time asleep; tests use a fake
        self._display = {}                # Settings and Setup values (_display_value)
        self._display_gen = 0
        self._display_lock = threading.Lock()
        self.speaker_model = speaker_model
        self._battery = battery           # tests: fixed value instead of asking Windows
        kw = {} if idle_unload_s is None else {"idle_unload_s": idle_unload_s}
        self.cache_dir = Path(cache_dir or self.root / "models")
        self.handle = WorkerHandle(self.cache_dir,
                                   quiet=self.settings["mode"] == resources.QUIET,
                                   allow_test_engines=allow_test_engines, log=self.log, **kw)
        # Live drafts while recording (#104): off by default, memory only.
        self._drafts = None               # drafts.DraftSession for the open recording
        self._draft_gen = 0               # each session's number (late results are dropped)
        self._drafts_ended = None         # the recording whose drafts ended for good
        self._drafts_ended_why = None     # and why, the note it keeps (#384)
        self._draft_why = "off"           # why drafts are not running, for the page
        self.draft_model = drafts.DRAFT_MODEL     # tests: a "test:" engine
        # Downloads replace model files: the draft worker and the inference
        # worker let go of them first.
        # Downloads the user queued are kept (model names only) and carry on
        # at the next start (#366); the current policy and the token are
        # checked again as each one starts (_prepare_queued_download).
        self.downloads = setup.Downloads(self.cache_dir, before_install=self._before_model_change,
                                         before_gpu_install=self._release_gpu_worker,
                                         queue_path=self.root / setup.QUEUE_FILE)
        self.downloads.after = self._after_download
        self.downloads.prepare = self._prepare_queued_download
        self.downloads.log_dir = str(self.log.folder)
        self._wake = threading.Event()
        self._closing = threading.Event()
        self._busy = None                 # (meeting id, "transcribing" | "speakers" | "cleanup")
        self._busy_ref = None             # the job's ref (a speaker pass: its skip count)
        self._status = ""                 # short, content-free status line
        self._import_errors = []          # recent intake refusals (safe messages)
        self._imports = queue.Queue()
        self._last_purge = 0.0
        self._lock = threading.Lock()
        self._reruns = {}                 # meeting id -> known speaker count (or None)
        self._rerun_count = None          # the count of the rerun now running
        self._gpu_pass = False            # a speaker pass on the graphics card is running
        self._gpu_whisper_run = False     # Whisper is transcribing on the graphics card
        self._removing = None             # a model being removed: no job starts meanwhile
        self._scan_wake = threading.Event()     # Check now (scan_now), or closing
        self.last_scan = None
        self._threads = []
        # Microphone recording (#30): one at a time; heavy processing waits.
        self.recordings = self.root / "recordings"
        self._recording = None
        self._rec_starting = False         # a recording is being opened
        # Called with True/False whenever a recording starts or ends, so the
        # window can show it (title and taskbar, #111 A6). Set by the bridge.
        self.recording_indicator = None
        self._indicator_lock = threading.Lock()
        self._indicated = False
        self.keep_awake = power.KeepAwake()  # held while a recording is open; tests use a fake
        # The lid (#172): with the opt-in setting, "Do nothing" while recording.
        # A change left by a crash or power cut is put back first of all.
        self.lid_watch = power.LidWatch(self._lid_changed)
        self._lid_retry_at = float("-inf")  # a waiting restore is retried while idle
        self._lid_result = None            # the last override's outcome, for the page
        self._power_on = False             # keep-awake and lid are set for a recording
        self._yielded = False             # a worker stop caused by recording, not the user
        self._recording_reports = []       # finished or recovered recordings (no content)
        self._hung = {}                    # folder name -> recording whose writer did not finish
        self.open_stream = recorder._sounddevice_stream     # tests use a fake
        self.open_loopback = None          # computer audio: loopback.open_loopback; tests a fake
        # Playback level (#108): (meeting, audio file id) -> gain, in memory
        # only, never logged or shown (P3); a new audio file gets a new id.
        self._playback_gains = {}
        self._playback_gains_lock = threading.Lock()
        if not held and self.locked is None:
            self._upkeep()
        if start:
            self.start()

    def _record_keys(self):
        """The key's state at this start, for --status and --health
        (logs\\keys.json; they never unlock the key themselves), and a locked
        start in the log with an incident code. Copy diagnostics uses this
        run's own state (_key_line). Reason, Windows error number and code
        only (P3)."""
        if self.locked is None:
            diag.record_keys(self.log.folder, "ok", log=self.log)
            return
        self._lock_code = diag.new_code()
        self.log.write("key_unavailable", code=self._lock_code, reason=self.locked.reason,
                       winerror=self.locked.code)
        diag.record_keys(self.log.folder, "locked", self.locked.reason, self.locked.code,
                         self._lock_code, log=self.log)

    def _open_audit(self, mirror):
        """The audit log (#111 C5): content-free records of how meetings are
        handled, each written after its action has committed, and never
        blocking it. Only the workspace in its own place with the app's key
        protector copies records to the Windows Event Log: a workspace opened
        elsewhere (tests, and check tools that name their own folder) keeps
        its file only, so test runs add nothing under the app's source. The
        packaged checks (tools/packaged_check.py, tools/packaged_crash_check.py)
        redirect LOCALAPPDATA, so their throwaway workspace is in its own
        place: they pass an audit_mirror that drops the copies instead. The
        idle window of tools/packaged_check.py starts as the app does and
        still writes its entries (the owner's decision, 26 Oct 2026, #418).
        A record that breaks its
        rules is refused, counted and logged by type; it never raises, in
        tests too (their fakes need not use the app's ids), so the app tests
        check that nothing was refused. Opened once Workspace.locked is
        known; while locked it writes and reads only its own folder, never
        the database or a key (plain metadata, P3)."""
        from workspace.store import SCHEMA_VERSION, under_workspace_root
        real = self.store.protector.name == keystore.APP_PROTECTOR
        if mirror is None and real and os.name == "nt" \
                and under_workspace_root(self.store.path):
            mirror = audit.EventLogMirror()
        log = audit.AuditLog(self.root / AUDIT_FOLDER, mirror=mirror, log=self.log,
                             new_workspace=self.store.new_root)
        if self.locked is not None:
            # A locked start (#111 C4a): Workspace.locked's reason and Windows
            # error number only, the number to diag's rule as keys.json has
            # it. The log is its own plain file: no key is asked for.
            log.record("locked", reason=self.locked.reason,
                       winerror=diag.windows_error(self.locked.code))
        else:
            from workspace import update
            try:
                version = str(update.package_version())
            except Exception:
                version = None
            log.record("app_start", schema=SCHEMA_VERSION,
                       version=version if audit.valid_version(version) else None)
        return log

    def _check_database(self, check_integrity):
        """The database check (R11), before the store opens: after the app
        last ended without closing (its run marker is still there: __main__
        builds the Workspace before diag.install replaces it), or while the
        last check found damage. On damage, every job (cleanup too), import,
        folder scan and receipt purge waits until the user chooses Continue
        anyway (the user's choices, 17 Oct 2026, #132). The hold is in memory
        only, so it adds no write to the damaged database."""
        db = self.root / DB_FILE
        last = diag.integrity_state(self.log.folder)
        failed_before = bool(last and last["result"] == "failed")
        skipped_before = bool(last and last["result"] == "skipped")
        if not (check_integrity and db.exists()
                and (self.log.running_path.exists() or failed_before or skipped_before)):
            return
        sound = quick_check_file(db)
        if sound is None:                       # could not be checked (busy, memory)
            self.log.write("integrity", result="skipped")
            if failed_before:                   # the damage found before still stands
                self._integrity = {"state": "failed", "code": last["code"]}
            else:                               # kept, so the next start checks again
                diag.record_integrity(self.log.folder, "skipped", log=self.log)
            return
        code = None if sound else diag.new_code()
        result = "ok" if sound else "failed"
        self.log.write("integrity", result=result, code=code)
        diag.record_integrity(self.log.folder, result, code, log=self.log)
        if not sound:
            self._integrity = {"state": "failed", "code": code}

    def _upkeep(self, discard=True):
        """Start-up upkeep that writes: finish (or, with discard, remove)
        imports cut short by a crash, and line up imports queued when the app
        last closed. Run at open, or once a damaged database's hold is
        released: then nothing is removed, because damaged pages can make a
        finished meeting look unfinished; a start whose check passes does it."""
        self._reconcile_imports(discard)
        intake.recover(self.store)
        if discard:                       # a sound database
            self._sweep_recording_keys()
            cleanup.compact_deferred(self.store)       # owed compaction (#198)

    def _sweep_recording_keys(self):
        """Delete a recording key that is no longer needed (#111 C3b): its
        folder is gone (a crash between making the key and the folder, or
        after a folder was removed), or a meeting was already made from it
        (a crash between the import and the meeting's first version, whose
        set-up the start-up upkeep could not finish; its folder is removed,
        not imported, so its chunks are erased first). A folder whose key is
        missing is kept and reported when it is imported, never deleted here.
        Runs only at a sound, unlocked start, after _reconcile_imports (never
        while locked: no key row is read or changed then, C4a design 7.2),
        before any recording or import can start."""
        if self.locked is not None:
            return
        try:
            ids = self.store.recording_key_ids()
            gone = [rid for rid in ids
                    if not (audit.HEX32.fullmatch(rid) and (self.recordings / rid).is_dir())
                    or self.store.recording_imported(rid)]
        except StoreError:
            return
        for rid in gone:
            self._forget_recording_key(rid)

    def _forget_recording_key(self, recording_id):
        """Crypto-erase a recording (#111 C3b): delete its key row, then empty
        the write-ahead log, before any of its files is deleted, so the
        chunks left behind (on disk, or in freed clusters) can never be
        opened. A log that stays busy is compacted at the next sound start
        (compact.pending). Never while locked. Returns whether a key went."""
        if self.locked is not None:
            return False
        try:
            if recording_id not in self.store.recording_key_ids():
                return False
            emptied = self.store.destroy_recording_key(recording_id)
        except StoreError as exc:
            self.log.incident("recording_key", exc)
            return False
        if not emptied:
            mark_compaction_owed(self.store.path)
        return True

    # -- lifecycle --------------------------------------------------------------------

    def start(self):
        # Downloads queued before the app last closed carry on (#366), before
        # the processing thread starts, so no job slips in ahead of them. Not
        # while locked (#111 C4a): the page cannot show them; they wait.
        if self.locked is None:
            self.downloads.resume()
        for target, name in ((self._process_loop, "workspace-processing"),
                             (self._intake_loop, "workspace-intake"),
                             (self._watch_loop, "workspace-watch"),
                             (self._policy_loop, "workspace-policy")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self._wake.set()
        # Ask nvidia-smi once in the background, so Setup and Settings do not
        # wait for it the first time they open.
        threading.Thread(target=gpu.detected, daemon=True, name="gpu-detect").start()
        # Recordings left by a crash, power loss or a failed import (#30). One the
        # app never stopped waits for the user (Keep and transcribe, or Delete).
        # Not while locked (#111 C4a): they wait, untouched, for a start that
        # can import them.
        if self.locked is not None:
            return
        for folder, _ in recorder.unfinished(self.recordings):   # also half-removed ones
            self._imports.put(("__recovered__", str(folder)))

    def close(self):
        for _ in range(100):              # a recording still opening (up to 10 s)
            if not self._rec_starting:
                break
            time.sleep(0.1)
        with self._lock:                 # claimed once: the policy tick may race
            rec, self._recording = self._recording, None
        self._end_drafts(rec, final=True)
        if rec is not None:              # saved now; imported at the next start
            self._indicate()
            try:
                rec.stop()
                self._recording_ended(rec, "saved")
            except recorder.RecorderError:
                pass                     # its folder is recovered at the next start
        self._flush_clipboard_clears()
        self._closing.set()
        self.downloads.close()            # the queue is kept for the next start (#366)
        self._wake.set()
        self._scan_wake.set()
        self._policy_wake.set()
        self._integrity_ok.set()          # an import waiting on the check ends with the app
        self._imports.put(None)
        self.handle.close()
        for t in self._threads:
            # A folder scan may be hashing a large file; it stops between files.
            t.join(60 if t.name == "workspace-watch" else 15)
        self.store.close()
        self.audit.close()
        held = getattr(self, "_instance", None)
        if held is not None:
            held.release()                # last: the workspace is closed

    def _take_instance(self):
        """The workspace lock. instance.Held when another process holds it;
        a lock file that cannot be opened or locked for any other reason
        stops the start too, rather than open the workspace unguarded (no
        path in the message, P3)."""
        try:
            return instance.acquire(self.root)
        except OSError:
            raise StoreError("Unminute could not use its workspace's lock file, so it "
                             "did not open the workspace. Ask your IT team.") from None

    # -- Settings and Setup data (R7b) -------------------------------------------------

    def _after_download(self):
        """A download or check ended: what Setup shows is read again, and
        processing that waited carries on."""
        self._forget_display()
        self._wake.set()

    def _display_value(self, key, compute, version=None):
        """A value Settings or Setup shows, worked out again only after
        DISPLAY_CACHE_S, when `version` changes, or after _forget_display()."""
        now = self._clock()
        with self._display_lock:
            hit, gen = self._display.get(key), self._display_gen
        if hit and hit[0] > now and hit[1] == version:
            return hit[2]
        value = compute()
        with self._display_lock:
            if self._display_gen == gen:     # not forgotten meanwhile: still current
                self._display[key] = (now + DISPLAY_CACHE_S, version, value)
        return value

    def _forget_display(self):
        with self._display_lock:
            self._display.clear()
            self._display_gen += 1

    # -- managed policy (#111 A5) ---------------------------------------------------

    def _allowed(self, name):
        return bool(self.policy.current().get(name))

    def _user_models_allowed(self):
        """Whether models in this user's own cache are used (#111 B7a): always,
        except on a machine install whose policy sets AllowUserModels to 0 or
        cannot be read (K3). A download there would never be used."""
        return asr_engines.machine_root() is None or self._allowed("AllowUserModels")

    def _model_downloads_allowed(self):
        """Model downloads (not the graphics card components) are allowed and
        of use: AllowModelDownloads, and _user_models_allowed."""
        return self._allowed("AllowModelDownloads") and self._user_models_allowed()

    def _not_locked(self):
        """Refuse a request that would read or write content while the
        workspace is locked (#111 C4a). The bridge gate refuses first; this
        covers the drop handler and any other caller."""
        if self.locked is not None:
            raise WorkspaceError(LOCKED_TEXT)

    def _not_finish_only(self):
        """Refuse new work in finish-only mode (#111 B5b). The bridge refuses
        first; this covers the drop handler and any other caller."""
        if self.finish_only:
            raise WorkspaceError(FINISH_ONLY_TEXT)

    def check_can_record(self, device, name=None):
        """Refuse now what start_recording would refuse, before anyone is asked
        (the microphone included)."""
        self._not_locked()
        self._not_finish_only()
        self._need_microphone()
        if self._capturing():
            raise WorkspaceError("A recording is already running.")
        self._device(device, name)

    def deletion_grant_folder(self, fid):
        """The path of the watched folder a deletion grant would apply to, read
        from the app's own records for the Windows question; None if policy
        refuses the grant anyway (set_folder_authority says why)."""
        fid = _hold_id(fid)
        if not self._allowed("AllowDeleteOriginals"):
            return None
        rows = self.store.folders("id = ?", (fid,))
        if not rows:
            raise WorkspaceError("Not found.")
        return rows[0]["path"]

    def meeting_for_discard(self, mid):
        """Title and import time, for the Windows question."""
        m = self._meeting(mid, content=True)
        return m["title"], m["created_at"]

    def consent_required(self):
        """Each recording needs the confirmation (in the app's own window) that everyone has been
        told (#111 A6), unless the organisation's policy turns it off."""
        return self._allowed("RequireRecordingConsent")

    def _indicate(self):
        """Show the current state (a recording is open, paused or not), so
        racing starts and ends always settle on the truth. The computer is
        kept awake for the same span (workspace.power)."""
        with self._indicator_lock:
            on = self._recording is not None
            if on != self._power_on:          # once per start and per end
                self._power_on = on
                # Nothing here may stop a recording starting or ending.
                try:
                    if on:
                        self.keep_awake.hold()    # refused: recording carries on regardless
                        self.lid_watch.start()
                        if self.settings["lid_override"] and self._allowed("AllowLidOverride"):
                            self._lid_result = self.lid.override()
                            if not self.settings["lid_override"]:   # turned off meanwhile
                                self.lid.restore()
                                self._lid_result = None
                    else:
                        self.keep_awake.release()
                        self.lid_watch.stop()
                        self.lid.restore()
                        self._lid_result = None
                except Exception:
                    pass
                self._lid_problem_report()
            hook = self.recording_indicator
            if hook is None or on == self._indicated:
                return
            try:
                hook(on)
                self._indicated = on
            except Exception:            # the indicator never stops a recording;
                self._indicated = None   # the next change is always applied

    LID_NOTES = {
        # The user's decision on #188: a record that cannot say what to put back
        # leaves the lid action as it is, and the user is told once.
        "lost": "Unminute could not put your lid setting back, because its saved record "
                "was damaged. Check what closing the lid does in Windows Settings > System "
                "> Power & battery > Lid & power button controls.",
        "failed": "Unminute could not put your lid setting back yet. It tries again while "
                  "the app is open. If this stays, check what closing the lid does in "
                  "Windows Settings > System > Power & battery > Lid & power button "
                  "controls.",
    }

    def _lid_problem_report(self):
        """A lid restore that failed or found a damaged record is shown once,
        under the recording card (no content)."""
        problem = getattr(self.lid, "problem", None)
        if problem in self.LID_NOTES and problem != getattr(self, "_lid_reported", None):
            self._lid_reported = problem
            self._add_report({"title": None, "seconds": 0, "events": [], "lid": True,
                              "note": self.LID_NOTES[problem]})
        elif problem is None:
            self._lid_reported = None

    def _lid_changed(self, open_):
        """Windows reports the lid opening or closing (power.LidWatch)."""
        rec = self._recording
        if rec is not None:
            rec.lid_changed(open_)

    def managed(self):
        """What the organisation's policy sets, for Settings and Setup: the
        permissions in force and which values are managed (no paths)."""
        p = self.policy.current()
        allow = {name: bool(p.get(name)) for name in policy.SPEC
                 if policy.SPEC[name][0] == "bool"}
        # AllowUserModels as it applies here (#111 B7a): only a machine install
        # reads it, so a per-user install reports it allowed. Off there, a
        # model download would not be used (USER_MODELS_OFF), so the page shows
        # "Ask your IT team" for the models. AllowModelDownloads stays raw: the
        # graphics card components stay per-user (B7-5, DEC-068) and follow
        # only it and AllowGpuComponent.
        allow["AllowUserModels"] = self._user_models_allowed()
        return {**p.summary(), "allow": allow,
                "clipboard_clear_minutes": p.get("ClipboardClearMinutes"),
                "handoff_folder_set": bool(p.get("HandoffDefaultFolder")),
                "assistant": self._assistant_setting(p)}

    def wait_idle(self, timeout=60.0):
        """Tests and tools: wait until no work is queued or running."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if (self._busy is None and self._imports.unfinished_tasks == 0
                    and self._next_work() is None):
                return True
            time.sleep(0.05)
        return False

    # -- intake -------------------------------------------------------------------------

    def import_paths(self, paths):
        """Queue recordings chosen in the file dialog or dropped on the window.
        Paths come from the operating system, never from page script, and are
        validated again here. Returns the number accepted. Refused while
        locked (#111 C4a) and in finish-only mode (#111 B5b)."""
        self._not_locked()
        self._not_finish_only()
        accepted, refused = 0, []
        network = self._allowed("AllowNetworkSources")
        for p in list(paths)[:50]:
            try:
                real = validate_input_file(p, AUDIO_EXTENSIONS, MAX_IMPORT_BYTES,
                                           allow_network=network)
            except UnsafePathError as exc:
                refused.append(str(exc))
                continue
            self._imports.put((real, None))         # by hand: never deduplicated
            accepted += 1
        if refused:
            self._import_errors = (self._import_errors + refused)[-5:]
        return {"accepted": accepted, "refused": refused}

    def _intake_loop(self):
        while True:
            item = self._imports.get()
            try:
                if item is not None and not self._only_reports(item):
                    self._integrity_ok.wait()   # held while damage holds work (R11)
                if item is None or self._closing.is_set():
                    return
                if item[0] in ("__recording__", "__recovered__"):
                    self._import_recording_folder(Path(item[1]), ask=item[0] == "__recovered__")
                    continue
                path, folder_id, key, content = (*item, None, None)[:4]
                if key and not (self._allowed("AllowWatchedFolders")
                                and intake.watching(self.store, folder_id)):
                    # Lined up before its folder was paused, removed or turned
                    # off by policy: it waits until the folder is on again (#152).
                    intake.import_held(self.store, folder_id, key)
                    continue
                self._status = "Importing a recording"
                try:
                    self._import_one(path, folder_id)
                except Exception:
                    if key:
                        intake.import_finished(self.store, folder_id, key, content, False)
                    raise
                if key:
                    intake.import_finished(self.store, folder_id, key, content, True)
            except Exception as exc:          # a bad file must not stop the intake thread
                code = (None if _expected_import_error(exc)
                        else _import_incident(self.log, "import", exc))
                self._import_errors = (self._import_errors + [_import_message(exc, code)])[-5:]
            finally:
                self._imports.task_done()
                self._status = ""
                self._wake.set()

    def _only_reports(self, item):
        """Does this queued item only ask the user, writing nothing to the
        database? A recording recovered after a crash waits for Keep or
        Delete; the database is not touched until the user chooses. Shown
        even while damage holds work (R11), so it is never hidden."""
        if item[0] != "__recovered__":
            return False
        folder = Path(item[1])
        journal = recorder.read_journal(folder)
        return journal is None or journal.get("state") == "recording"

    def _import_one(self, path, folder_id=None, title=None, recording=None, stream=None):
        """recording: the recording's id when this imports a microphone
        recording (the audit log ties its confirmation to the meeting).
        stream: for a microphone recording (#111 C3b), (audio, (recording
        folder id, info JSON)) in place of path: the audio is streamed to the
        decoder (media.import_stream); the recording's key is deleted when the
        meeting's first version is set up (_plan_version)."""
        if stream is not None:
            audio, row = stream
            mid, sid = media.import_stream(self.store, self.media, audio, title, row)
        else:
            title = (title or _plain_label(Path(path).stem).strip()[:MAX_TITLE_CHARS]
                     or "Untitled meeting")
            mid, sid = media.import_recording(self.store, self.media, path, title)
        # Authority comes only from the folder's explicit setting (#24); the
        # original's location is kept only when it may be deleted.
        may_delete = bool(folder_id and intake.authority_for(self.store, folder_id)
                          and self._allowed("AllowDeleteOriginals"))
        self.store.set_source_intake(sid, folder_id, may_delete=may_delete)
        try:
            self._plan_version(mid, sid)
        except BaseException:
            self._discard_import(mid)       # nothing half-imported is left behind
            raise
        length = self.store._one("SELECT samples, sample_rate FROM sources WHERE id = ?", (sid,))
        self.audit.record("meeting_imported", meeting=mid,
                          source="recording" if recording else "folder" if folder_id else "file",
                          folder=folder_id, recording=recording,
                          seconds=(length["samples"] // length["sample_rate"]
                                   if length and length["sample_rate"] else 0),
                          delete_original=may_delete)
        return mid

    # -- microphone recording (#30) ---------------------------------------------------

    def _need_microphone(self):
        if not self._allowed("AllowMicrophoneRecording"):
            raise WorkspaceError(MANAGED_OFF.format(what="Microphone recording"))
        if not self.settings["microphone"]:
            raise WorkspaceError("Microphone recording is turned off in Settings.")

    def _computer_audio_allowed(self):
        """Policy allows computer audio: both keys, since it is only ever
        recorded beside the microphone. An unreadable key grants nothing."""
        return (self._allowed("AllowMicrophoneRecording")
                and self._allowed("AllowComputerAudioRecording"))

    def _computer_wanted(self):
        """A recording started now would record computer audio: the app opens
        computer tracks at all (COMPUTER_AUDIO_STARTS), the user's switch is
        on (off by default) and policy allows it."""
        return bool(COMPUTER_AUDIO_STARTS and self.settings["computer_audio"]
                    and self._computer_audio_allowed())

    def recording_sources(self):
        """What a recording started now would record, for the confirmation
        the bridge shows (wording version 2): from the app's own settings and
        policy, never from the page."""
        return (recording_consent.WITH_COMPUTER if self._computer_wanted()
                else recording_consent.MICROPHONE_ONLY)

    def _computer_track(self):
        """The computer track for a recording about to start, or None. The
        source is every active output, mixed (the owner's decision 1, 25 Oct
        2026, after real-sound trials: the default output alone missed a call
        on a headset's hands-free output, and a meeting app's own process gave
        silence)."""
        if not self._computer_wanted():
            return None
        from workspace import loopback       # only when a recording needs it
        return {"open": self.open_loopback or loopback.open_loopback,
                "source": {"kind": loopback.ALL}, "rate": loopback.LOOPBACK_RATE,
                "block_s": loopback.POLL_S}

    def microphone_devices(self):
        self._need_microphone()
        try:
            return recorder.list_devices()
        except recorder.RecorderError as exc:
            raise WorkspaceError(str(exc)) from exc

    def _device(self, device, name=None):
        """The device at this index, if it is still the microphone the page
        listed (PortAudio numbers can shift when devices are plugged in or
        out; a different microphone must never be opened silently)."""
        choice = next((d for d in self.microphone_devices() if d["index"] == device), None)
        if choice is None or (name is not None and choice["name"] != name):
            raise WorkspaceError("The list of microphones has changed. Choose the "
                                 "microphone again.")
        return choice

    def test_microphone(self, device, name=None):
        self._need_microphone()
        if self._capturing():
            raise WorkspaceError("Stop the recording before testing a microphone.")
        choice = self._device(device, name)
        try:
            return recorder.measure_level(choice["index"], choice["rate"],
                                          open_stream=self.open_stream)
        except recorder.RecorderError as exc:
            raise WorkspaceError(str(exc)) from exc
        except Exception as exc:
            raise WorkspaceError("The microphone could not be opened. Check that Windows "
                                 "allows apps to use it (Settings > Privacy > "
                                 "Microphone).") from exc

    def _capturing(self):
        return self._recording is not None or self._rec_starting

    def start_recording(self, device, name=None, consent=None):
        """consent: the record of the confirmation (workspace.consent),
        kept with the recording. The bridge asks for it; it is required unless
        policy turns the confirmation off. Refused while locked (#111 C4a)
        and in finish-only mode (#111 B5b)."""
        self._not_locked()
        self._not_finish_only()
        self._need_microphone()
        # Worked out once: the sources the confirmation must name are the
        # ones this recording opens (#108). A switch or policy changed while
        # the question was open means asking again, never a different start.
        computer = self._computer_track()
        sources = (recording_consent.WITH_COMPUTER if computer is not None
                   else recording_consent.MICROPHONE_ONLY)
        if consent is not None and not recording_consent.valid_recording_record(consent,
                                                                                 sources):
            if any(recording_consent.valid_recording_record(consent, other) for other in
                   (recording_consent.MICROPHONE_ONLY, recording_consent.WITH_COMPUTER)):
                raise WorkspaceError(SOURCES_CHANGED)
            raise WorkspaceError("The recording confirmation is not valid.")
        if consent is None and self.consent_required():
            raise WorkspaceError("Recording needs your confirmation that everyone in the "
                                 "meeting has been told.")
        # Under the processing lock: the loop re-checks this flag under the
        # same lock before starting heavy work, so nothing can slip in.
        with self._lock:
            if self._capturing():
                raise WorkspaceError("A recording is already running.")
            self._rec_starting = True
            busy = self._busy
        self._policy_wake.set()              # policy is checked every second while recording
        try:
            # Heavy processing waits while recording. Work in progress is
            # stopped; it was never committed and is redone afterwards.
            if busy and busy[1] in ("transcribe", "speakers", "rerun"):
                self._yielded = True
                self.handle.stop_now()
            choice = self._device(device, name)
            # The recording's key (#111 C3b) is made and stored, wrapped, before
            # its folder or stream: a crash in between leaves a key with no
            # folder, which the next start deletes (_sweep_recording_keys).
            folder = recorder.new_folder(self.recordings)
            key = self.store.create_recording_key(folder.name)
            try:
                rec = recorder.Recording(folder, choice["index"],
                                         choice.get("label") or choice["name"], choice["rate"],
                                         open_stream=self.open_stream,
                                         computer=computer, key=key)
                if consent is not None:
                    rec.journal["consent"] = dict(consent)     # saved with the journal
                rec.start()
            except BaseException as exc:
                self._forget_recording_key(folder.name)    # the start removed the folder
                if isinstance(exc, recorder.RecorderError):
                    raise WorkspaceError(str(exc)) from exc
                raise
            finally:
                key = None
            with self._lock:
                self._recording = rec
                self._draft_why = "off"      # until the tick decides for this recording
            self._indicate()
            self._policy_wake.set()          # live drafts, if on, start without waiting
        finally:
            with self._lock:
                self._rec_starting = False
            self._wake.set()                 # if it failed, processing carries on
        self.audit.record("recording_started", recording=rec.folder.name,
                          consent="confirmed" if consent is not None else "not_required",
                          wording=consent.get("wording") if consent is not None else None,
                          method=("app-window" if consent is not None
                                  and consent.get("method") == "app-window" else None),
                          sources=("mic_and_computer" if computer is not None
                                   else "mic_only"))
        threading.Thread(target=self._watch_recording, args=(rec,), daemon=True,
                         name="recorder-watch").start()
        return rec.status()

    def _recording_ended(self, rec, result):
        """The audit record for a recording that stopped and was saved, or
        was discarded (#111 C5)."""
        try:
            seconds = int(rec.status()["seconds"] or 0)
        except Exception:
            seconds = 0
        self.audit.record("recording_ended", recording=rec.folder.name, result=result,
                          seconds=max(0, seconds))

    def _watch_recording(self, rec):
        """A recording that stops itself (lost device, full disk, time limit)
        is saved and imported like one the user stopped."""
        while self._recording is rec and not rec.stopped:
            time.sleep(0.5)
        if self._recording is rec and not self._closing.is_set():
            self._finish_recording(rec)

    def _finish_recording(self, rec, event=None):
        with self._lock:
            if self._recording is not rec:
                return False             # already finished by the other path
            self._recording = None
        self._indicate()
        if event:                        # noted only by the path that finishes it
            rec._event(event[0], note=event[1])
        try:
            rec.stop()
        except recorder.RecorderError as exc:
            self._end_drafts(rec)
            self._hung[rec.folder.name] = rec
            self._add_report({"title": None, "seconds": rec.status()["seconds"],
                              "events": list(rec.journal["events"]),
                              "note": str(exc), "pending": rec.folder.name})
            return                       # imported at the next start
        self._recording_ended(rec, "saved")
        # Capture has stopped; drafts end before the import is queued. Until
        # the draft worker has exited, processing waits for it (D4).
        self._end_drafts(rec)
        self._imports.put(("__recording__", str(rec.folder)))
        self._wake.set()

    def pause_recording(self):
        if self._recording is None:
            raise WorkspaceError("Nothing is being recorded.")
        self._recording.pause()          # live drafts: the phrase in progress ends here
        return self._recording.status()

    def resume_recording(self):
        if self._recording is None:
            raise WorkspaceError("Nothing is being recorded.")
        if not self._allowed("AllowMicrophoneRecording"):
            raise WorkspaceError(MANAGED_OFF.format(what="Microphone recording"))
        if self._recording.computer_on and not self._computer_audio_allowed():
            self._recording.stop_computer(COMPUTER_POLICY_STOP)   # the microphone resumes
        self._recording.resume()
        return self._recording.status()

    def stop_recording(self):
        with self._lock:
            rec = self._recording
        if rec is None:
            raise WorkspaceError("Nothing is being recorded.")
        status = rec.status()
        if self._finish_recording(rec) is False and (
                not rec.folder.exists() or recorder.is_discarded(rec.folder)):
            raise WorkspaceError("The recording was discarded.")   # Discard got there first
        return {**status, "state": "saving"}

    def recording_active(self):
        """A recording is open (closing the window asks what to do with it)."""
        return self._recording is not None

    def discard_recording(self):
        """The user's explicit choice: stop the recording and delete its audio
        without importing it. Nothing of it is kept."""
        with self._lock:
            rec = self._recording
            if rec is None:
                raise WorkspaceError("Nothing is being recorded.")
            # Marked first: whatever happens next, this folder is deleted, never
            # imported (at the next start, or by Try again). Unmarked, it carries on.
            try:
                recorder.mark_discarded(rec.folder)
            except OSError as exc:
                raise WorkspaceError("The recording could not be discarded. It carries on; "
                                     "try again, or stop and save it.") from exc
            self._recording = None                # the watcher will not import it now
        self._indicate()
        self._recording_ended(rec, "discarded")
        self._end_drafts(rec)                     # its draft text goes with it
        # Its key goes first (#111 C3b): whatever is left on disk can never be
        # opened, even if the files cannot all be deleted yet.
        self._forget_recording_key(rec.folder.name)
        report = {"title": None, "seconds": rec.status()["seconds"], "events": [],
                  "note": "Delete it now, or it is deleted when the app next starts.",
                  "pending": rec.folder.name, "discarded": True}
        try:
            rec.stop()
        except recorder.RecorderError as exc:
            self._hung[rec.folder.name] = rec     # still writing: delete it once it is free
            self._add_report(report)
            self._wake.set()
            raise WorkspaceError("The recording is still being saved, so it could not be "
                                 "deleted yet. It is listed on the Meetings page.") from exc
        try:
            recorder.remove_folder(rec.folder)
        except OSError as exc:
            self._add_report(report)
            self._wake.set()
            raise WorkspaceError("The recording could not be deleted yet (another program may "
                                 "be using it). It is listed on the Meetings page.") from exc
        self._wake.set()                          # processing that waited carries on

    def recording_status(self):
        rec = self._recording
        if self.locked is not None:              # nothing records, and no report is shown
            return {"enabled": False, "computer_audio": False,
                    "display": self.settings["recording_display"],
                    "lid_override": False, "lid_refused": None, "recording": None,
                    "reports": [], "draft": None}
        enabled = bool(self.settings["microphone"] and self._allowed("AllowMicrophoneRecording"))
        return {"enabled": enabled,
                # A recording started now records computer audio too (#108).
                "computer_audio": enabled and self._computer_wanted(),
                "display": self.settings["recording_display"],
                # The lid action is "Do nothing" for this recording (set, or already so).
                "lid_override": bool(self._lid_result and self._lid_result[0]),
                # The setting is on but the lid action could not be set: why.
                "lid_refused": (self._lid_result[1] if self._lid_result
                                and not self._lid_result[0] else None),
                "recording": rec.status() if rec is not None else None,
                "reports": self._recording_reports[-5:],
                # Live drafts (#104): state and counts only, never text. The
                # page fetches the text (recording_draft) only when count or
                # generation changes, so this 100 ms poll stays small.
                "draft": self._draft_status(rec)}

    # -- live drafts while recording (#104) -------------------------------------------

    def _draft_status(self, rec):
        """Whether the recording card shows a draft area, and when the page
        should fetch draft text: the state, a note in the page's words, the
        phrase count and the generation. None when drafts are off (the
        switch) or nothing is recorded. Fixed words and numbers only (P3)."""
        if rec is None:
            return None
        session = self._drafts
        if session is not None:
            return {"state": session.state, "note": drafts.NOTES.get(session.state),
                    "count": session.count, "generation": session.generation,
                    "mix": session.mixing}
        if self._draft_why == "off":
            return None
        return {"state": self._draft_why, "note": drafts.REASONS.get(self._draft_why),
                "count": 0, "generation": self._draft_gen, "mix": False}

    def _draft_mix(self, rec, session):
        """The draft follows a live mix of both tracks when the user chose it
        (live_drafts_audio) and this recording's computer track is on and
        allowed (#108, K3); otherwise the microphone. Checked at the start
        and on each tick, so a change of setting, or a computer track that
        stops, applies within a second."""
        want = (self.settings["live_drafts_audio"] == drafts.MIX and rec.computer_on
                and self._computer_audio_allowed())
        if want and rec.tap_computer is None and session.state in (drafts.STARTING,
                                                                     drafts.RUNNING):
            if session.mix(rec.computer_rate):
                rec.tap_computer = session.tap_computer
        elif not want and session.mixing:
            rec.tap_computer = None       # the recorder first, then the worker is told
            session.unmix()

    def _draft_reason(self, p):
        """Why live drafts may not run now, or None. Managed policy withholds
        them when it says no, holds a bad value or cannot be read (K3). Until
        the page shows drafts, they stay off whatever the settings file says."""
        if not (self.settings["live_drafts"] and drafts.ON_PAGE):
            return "off"
        if not p.get("AllowLiveDrafts"):
            return "managed"
        if not resources.may_start_automatic_work(self.settings["battery_override"],
                                                  battery=self._battery):
            return "battery"
        return None

    def _draft_model_ready(self):
        if self.draft_model.startswith(engines.TEST_PREFIX):
            return self.handle.allow_test_engines
        return asr_engines.parakeet_status(self.draft_model, self.cache_dir)[0] == "ready"

    def _draft_tick(self, p):
        """On the policy tick, every second while recording: start, suspend,
        resume or end live drafts as the setting, policy and battery say.
        Off, managed or unreadable: no draft worker, no model."""
        rec = self._recording
        if rec is None or rec.stopped:
            return
        reason = self._draft_reason(p)
        session = self._drafts
        if session is not None:
            if reason == "battery":
                session.suspend()            # the worker ends; the text so far is kept
            elif reason:
                self._draft_why = reason
                self._end_drafts(rec)
            elif session.state == drafts.BATTERY:
                session.resume()
            if not reason:
                self._draft_mix(rec, session)
            return
        with self._lock:                  # the mark and its note, set together
            ended, ended_why = self._drafts_ended == rec.journal["id"], self._drafts_ended_why
        if ended:
            # Ended for this recording: its note stays (#384). A later battery
            # or policy reason does not replace it, as drafts will not come
            # back for this recording; the switch turned off hides it.
            self._draft_why = "off" if reason == "off" else ended_why
            return
        if reason:
            self._draft_why = reason
            return
        if self.downloads.busy() or self._removing:
            self._draft_why = "busy"          # model files may be changing: not yet
            return
        if not self._draft_model_ready():
            self._draft_why = "unavailable"   # Parakeet v2 is not downloaded or is damaged
            with self._lock:
                self._drafts_ended = rec.journal["id"]
                self._drafts_ended_why = "unavailable"
            return
        self._start_drafts(rec)

    def _start_drafts(self, rec):
        with self._lock:
            # Checked again under the lock that a model download or removal
            # takes before it ends drafts (_end_drafts), so one that began
            # since the tick is never missed.
            if (self._recording is not rec or self._drafts is not None
                    or self._drafts_ended == rec.journal["id"]
                    or self.downloads.busy() or self._removing):
                return
            self._draft_gen += 1
            session = drafts.DraftSession(rec.journal["id"], rec.rate, self.cache_dir,
                                          generation=self._draft_gen, model=self.draft_model,
                                          log=self.log,
                                          allow_test_engines=self.handle.allow_test_engines)
            self._drafts = session        # processing waits from here (D4)
        # The tap holds audio from now, so the first words are drafted too.
        if session.open():
            rec.tap = session.tap
            self._draft_mix(rec, session)
        try:
            # D4: one heavy model at a time. The main worker lets go of a model
            # it may still hold (idle) before the draft worker loads one.
            self.handle.shutdown()
            session.start()
        except Exception as exc:
            self.log.incident("drafts", exc)      # type name and code frames, never its text (P3)
            self._end_drafts(rec, final=True, why="failed")   # not "ended" (#381)

    def _end_drafts(self, rec=None, final=False, why="ended"):
        """End live drafts: the draft worker is ended (and waited for) and its
        text forgotten. final: not again for this recording (its model files
        are about to change, the app is closing, or the worker could not
        start), and why is the reason the page shows for it."""
        with self._lock:                  # _start_drafts checks the mark under it
            session = self._drafts
            if final:
                current = self._recording
                target = session.recording_id if session is not None else (
                    rec.journal["id"] if rec is not None else
                    current.journal["id"] if current is not None else None)
                if target is not None:
                    self._drafts_ended = target
                    self._draft_why = why
                    self._drafts_ended_why = self._draft_why
        if session is None:
            return
        for r in (rec, self._recording):
            if r is not None and r.tap == session.tap:
                r.tap = None
            if r is not None and r.tap_computer == session.tap_computer:
                r.tap_computer = None
        session.stop()
        with self._lock:
            # Its text is forgotten, so the page must forget it too: it clears
            # what it shows when the generation changes. With the same one it
            # kept the text and, its count above this 0, asked for it again
            # on every clock tick (#370). Raised first, so a reader without
            # the lock sees the session at its own generation, or none at the
            # new one, never none at the old one.
            self._draft_gen += 1
            if self._drafts is session:
                self._drafts = None
        self._wake.set()                      # processing that waited for it carries on

    def _before_model_change(self):
        """A model is about to be downloaded, repaired or replaced: the draft
        worker and the inference worker let go of its files first."""
        self._end_drafts(final=True)
        self.handle.shutdown()

    def recording_draft(self, since=0):
        """The live draft of the recording in progress: state, a note, and the
        phrases after index since (text and times only, at most 200), for the
        recording card (bridge method recording_draft, #104). The page asks
        only when recording_status's draft count or generation changes."""
        if not isinstance(since, int) or isinstance(since, bool) or not 0 <= since <= 10 ** 7:
            raise WorkspaceError("Expected a position.")
        if self._recording is None:
            raise WorkspaceError("Nothing is being recorded.")
        session = self._drafts
        if session is not None:
            return session.snapshot(since)
        return {"state": self._draft_why, "note": drafts.REASONS.get(self._draft_why),
                "generation": self._draft_gen, "count": 0, "since": since, "phrases": []}

    def _add_report(self, report):
        import uuid
        report["id"] = uuid.uuid4().hex
        self._recording_reports = (self._recording_reports + [report])[-20:]

    def discard_recording_report(self, rid):
        self._recording_reports = [r for r in self._recording_reports if r["id"] != rid]

    def retry_recording_import(self, rid):
        """The user's "Try again" for a recording that could not be imported."""
        report = next((r for r in self._recording_reports if r["id"] == rid), None)
        folder = self._pending_folder(report)
        hung = self._hung.get(folder.name)
        if hung is not None and hung.writing:
            raise WorkspaceError("The recording is still being saved. Try again in a moment, "
                                 "or restart the app.")
        if recorder.is_discarded(folder):
            raise WorkspaceError("This recording was discarded. Delete it instead.")
        self._hung.pop(folder.name, None)
        self.discard_recording_report(rid)
        self._imports.put(("__recording__", str(folder)))

    def _pending_folder(self, report):
        folder = (self.recordings / report["pending"]
                  if report and report.get("pending") else None)
        if folder is None or not folder.is_dir():
            raise WorkspaceError("That recording is no longer waiting.")
        return folder

    def delete_pending_recording(self, rid):
        """The user's explicit choice to delete a recording that could not be
        imported (for example its journal is damaged)."""
        report = next((r for r in self._recording_reports if r["id"] == rid), None)
        if (report and report.get("discarded")
                and not (self.recordings / report["pending"]).is_dir()):
            self.discard_recording_report(rid)    # already gone: nothing left to delete
            return
        folder = self._pending_folder(report)
        hung = self._hung.get(folder.name)
        if hung is not None and hung.writing:
            raise WorkspaceError("The recording is still being saved. Try again in a moment.")
        try:                                     # if any file is locked, the rest
            recorder.mark_discarded(folder)      # is deleted at the next start, never imported
        except OSError:
            pass
        self._forget_recording_key(folder.name)  # first: what is left can never be opened
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            raise WorkspaceError("The recording could not be deleted yet. Close other apps "
                                 "that may be using it, then try again.")
        self._hung.pop(folder.name, None)
        self.discard_recording_report(rid)
        if audit.HEX32.fullmatch(folder.name):     # the recording's id (recorder.new_folder)
            self.audit.record("recording_deleted", recording=folder.name)

    def _import_recording_folder(self, folder, ask=False):
        """Turn a saved (or crash-recovered) recording into a meeting, then
        remove the recording folder. Failures are reported and the folder is
        kept, to be tried again (Try again, or the next start).

        Encrypted (#111 C3b): the chunks are opened under the recording's key
        and checked in full (recorder.open_recording), and the recording is
        streamed to the sandboxed decoder over its stdin; no plain copy is
        written. The import's commit stores the sealed recording_sources row
        (no second import); the key is kept until the meeting's first version
        is set up, in whose transaction it is deleted before the write-ahead
        log is emptied (_plan_version), so a failure before then keeps the
        folder and its key for Try again (the owner's decision, 22 Oct 2026).
        Before any folder of a recording is deleted (discarded, nothing
        recorded, or already a meeting) its key goes first, so whatever is
        left can never be opened.

        A recovered folder that only reports (_only_reports) runs while
        damage holds work (R11). Deleting a folder deletes its key first, a
        write, so while held a discarded, empty or already imported folder is
        left as it is, for a start whose check passes."""
        held = not self._integrity_ok.is_set()
        if recorder.is_discarded(folder):        # the user discarded it: delete, never import
            if held:
                return                           # R11: deleted at a sound start
            self._forget_recording_key(folder.name)
            try:
                recorder.remove_folder(folder)
            except OSError:
                pass                             # tried again at the next start
            return
        journal = recorder.read_journal(folder)
        if journal is None and not (folder / "journal.json").exists() \
                and not recorder.has_audio(folder):
            if held:
                return                           # R11: deleted at a sound start
            self._forget_recording_key(folder.name)
            try:                         # an empty leftover (removal stopped at the end)
                recorder.remove_folder(folder)
            except OSError:
                pass
            return
        if journal is None:
            # Unreadable (locked, or damaged by a power cut): never delete
            # audio that has not been imported. Keep it and say so.
            self._add_report({"title": None, "seconds": 0, "events": [],
                              "note": "A recording could not be read (its record file is "
                                      "locked or damaged). The audio is kept. Choose Try "
                                      "again, or Delete it if you do not need it.",
                              "pending": folder.name})
            return
        if journal.get("state") == "imported" or self.store.recording_imported(folder.name):
            if held:
                return                           # R11: removed at a sound start
            self._forget_recording_key(folder.name)    # set-up unfinished: kept until now
            try:                         # already a meeting: finish removing the folder
                recorder.remove_folder(folder)
            except OSError:
                pass                     # tried again at the next start
            return
        if recorder.journal_format(journal) not in (1, recorder.JOURNAL_FORMAT):
            # Saved by a newer version (or damaged): importing what this version
            # understands could leave a track out. Keep it all and say so.
            self._add_report({"title": None, "seconds": 0, "events": [],
                              "note": "This recording was saved in a form this version of "
                                      "Unminute cannot read, perhaps by a newer version. The "
                                      "audio is kept. Update Unminute, then choose Try again, "
                                      "or Delete it if you do not need it.",
                              "pending": folder.name})
            return
        if ask and journal.get("state") == "recording":
            # The app closed or crashed while recording: nothing is transcribed
            # until the user chooses to keep it.
            rate = journal.get("rate") or 0
            self._add_report({"title": None, "events": [], "pending": folder.name,
                              "recovered": True, "started_at": journal.get("started_at"),
                              "seconds": round(journal.get("frames", 0) / rate, 1) if rate else 0,
                              "note": "The app closed while this was recording. Keep it to "
                                      "transcribe it, or delete it."})
            return
        events = [e for e in journal.get("events", []) if e["kind"] not in ("paused", "resumed")]
        report = {"title": None, "seconds": 0, "events": events,
                  "device": journal.get("device_name")}
        unreadable = {"title": None, "seconds": 0, "events": [], "pending": folder.name,
                      "note": "The recording could not be read. The saved files are kept; "
                              "choose Delete if you do not need them."}
        try:
            key = self.store.recording_key(folder.name)
        except DamagedContent:                   # KeyMissing too: nothing can be opened
            self._add_report(unreadable)
            return
        mid = audio = None
        try:
            audio = recorder.open_recording(folder, journal, key, events)
            key = None
            if not audio.readable:               # no chunk opened: kept, never deleted
                self._add_report(unreadable)
                return
            seconds = audio.seconds
            report["seconds"] = round(seconds, 1)
            interrupted = journal.get("state") == "recording"   # the app did not stop it
            missing = (recorder.missing_tail_seconds(folder, journal, seconds)
                       if interrupted else 0)
            if interrupted and missing >= 0.5:
                events.append({"kind": "interrupted", "at_s": round(seconds, 1),
                               "seconds": round(missing, 1),
                               "note": "The app closed unexpectedly while recording. About "
                                       f"{missing:.0f} s at the end could not be saved."})
            if seconds < 0.5:
                report["note"] = "Nothing was recorded."
                audio.close()
                self._forget_recording_key(folder.name)
                recorder.remove_folder(folder)
                self._add_report(report)
                return
            title = time.strftime("Recording %Y-%m-%d %H:%M",
                                  time.localtime(journal.get("started_at") or time.time()))
            recording_id = folder.name if audit.HEX32.fullmatch(folder.name) else None
            # Which recording the meeting came from and its details, sealed in
            # the database with the import (recording_sources): the consent
            # confirmation stays with the meeting (#111 A6) until audio
            # cleanup, as recording.json did.
            info = {**report, "title": title, "recording_id": folder.name,
                    "sources": [recorder.MICROPHONE]
                    + ([recorder.COMPUTER] if audio.computer is not None else []),
                    **({"consent": journal["consent"]} if "consent" in journal else {})}
            mid = self._import_one(None, None, title, recording=recording_id,
                                   stream=(audio, (folder.name, json.dumps(info))))
            two = audio.computer is not None       # the report says both were saved
        except Exception as exc:
            full = isinstance(exc, OSError) and getattr(exc, "errno", None) == 28
            changed = media.damaged_audio(exc)  # a chunk changed after it was checked
            code = (None if full or changed or _expected_import_error(exc)
                    else _import_incident(self.log, "recording_import", exc))
            report["note"] = ("It could not be imported yet: " +
                              ("the disk is full. Free some space, then choose Try again."
                               if full else
                               "part of the saved audio changed while it was read. Choose "
                               "Try again." if changed else _import_message(exc, code)) +
                              " The saved audio is kept.")
            report["pending"] = folder.name
            self._add_report(report)
            return
        finally:
            if audio is not None:
                audio.close()
        # Mark the folder as imported before removing it; the recording_sources
        # row, committed with the import, also stops a second import.
        report.update(title=title, meeting=mid, recording_id=folder.name, computer=two)
        if interrupted and recording_id:          # the app did not stop it: recovered
            self.audit.record("recording_ended", recording=recording_id, result="recovered",
                              seconds=max(0, int(seconds)))
        journal.update(state="imported", meeting=mid)
        try:
            recorder._retrying(lambda: recorder._write_atomic(
                folder / "journal.json", json.dumps(journal).encode("utf-8")))
        except OSError:
            pass                              # the recording_sources row still prevents a duplicate
        try:
            recorder.remove_folder(folder)    # the meeting now holds the audio
        except OSError:
            pass                              # finished at the next start
        self._add_report(report)

    def _erase_if_key_gone(self, recording_id):
        """After a meeting's first version is set up: if its transaction
        deleted the recording's key, empty the write-ahead log
        (erase_checkpoint), or leave the compaction owed when it stays busy."""
        try:
            if recording_id in self.store.recording_key_ids():
                return
            if not self.store.erase_checkpoint():
                mark_compaction_owed(self.store.path)
        except StoreError:
            pass

    def _plan_version(self, mid, sid):
        """Plan and set up a meeting's version. For a meeting made from a
        microphone recording whose key is still kept (#111 C3b), the version's
        transaction deletes that key (store.ensure_version), and the
        write-ahead log is emptied here."""
        reader = media.open_audio(self.store, self.media, mid)
        try:
            plan = plan_chunks(reader, len(reader))
        finally:
            reader.close()
        recording_id = self.store.meeting_recording_key(mid)
        vid = self._new_version(mid, sid, plan)
        if recording_id is not None:
            self._erase_if_key_gone(recording_id)
        return vid

    def _discard_import(self, mid):
        """Remove a meeting whose import never completed. Only the app's own
        fresh copy is removed; the original recording is never touched.

        A meeting that has ever had a hold, active or released, is kept
        (#193, invariant C2: releasing a hold deletes nothing): it stays listed
        as an import that did not finish, and only the user's Discard removes
        it. The check, the row deletion and the folder removal happen in one
        transaction under the store's lock (store.discard_unfinished_import),
        so no hold can be added in between, and a folder that cannot be
        removed rolls the rows back. Returns whether it was removed."""
        import shutil
        try:
            folder = media.meeting_folder(self.media, mid)
        except media.BadMeetingId:
            return False                      # not the app's own id: never touched

        def remove_folder():
            if folder.exists():
                shutil.rmtree(folder)
        try:
            return self.store.discard_unfinished_import(mid, remove_folder)
        except (StoreError, OSError):
            return False                      # unreadable or locked: kept for next time

    def _reconcile_imports(self, discard=True):
        """At startup: finish or remove imports cut short by a crash or close
        (a meeting with no transcript version would otherwise wait forever).
        discard False: finish only, never remove (a damaged database, R11).
        A meeting already confirmed for removal (Discard marks it reviewed) is
        left to its cleanup jobs: nothing is planned for it again (#181).
        A removal is said on the Meetings page (import_errors), so a meeting
        the update check counted before this start does not just vanish
        (#425): fixed words and a count only (P3)."""
        removed = 0
        for m in self.store._all("SELECT m.id FROM meetings m WHERE m.reviewed_at IS NULL "
                                 "AND NOT EXISTS (SELECT 1 FROM versions v "
                                 "WHERE v.meeting_id = m.id)"):
            src = self.store._one("SELECT id FROM sources WHERE meeting_id = ?", (m["id"],))
            try:
                has_audio = media.audio_path(self.media, m["id"]).is_file()
            except media.BadMeetingId:
                continue                    # not the app's own id (#194): never touched
            if src is not None and has_audio:
                try:
                    self._plan_version(m["id"], src["id"])
                    continue
                except StoreError:
                    continue                # the database, not the import: kept for next time
                except Exception:
                    pass
            if discard and self._discard_import(m["id"]):
                removed += 1
        if removed:
            self._import_errors = (self._import_errors + [_imports_removed(removed)])[-5:]

    def _new_version(self, mid, sid, plan, model=None):
        """The version for this source with the given model (by default the
        one chosen in Settings)."""
        model = model or self.settings["model"]
        spec = asr_engines.MANAGED_MODELS.get(model)
        return self.store.ensure_version(
            mid, sid, engine="parakeet" if asr_engines.is_parakeet(model) else "whisper",
            model=model, model_revision=spec.revision if spec else "test",
            precision="int8", config={"chunking": "v1"}, chunk_plan=plan)

    # -- watched folders (#23) -------------------------------------------------------

    def _watch_loop(self):
        """Reconcile watched folders LAUNCH_SCAN_DELAY_S after launch (so
        start-up is not slowed), then every SCAN_INTERVAL_S while open, and at
        once on Check now (scan_now) or when a folder is added or turned on.
        Between scans the thread sleeps until the next one is due; the clock
        counts time asleep, so after the computer wakes a scan that fell due
        runs within WATCH_MAX_WAIT_S."""
        due = self._clock() + intake.LAUNCH_SCAN_DELAY_S
        while not self._closing.is_set():
            now = self._clock()
            if not self._scan_wake.is_set() and now < due:
                self._scan_wake.wait(min(due - now, WATCH_MAX_WAIT_S))
                continue
            self.log.limit_crash_file()     # keep the raw crash file small (R9)
            self._scan_wake.clear()          # a request made during the scan scans again
            due = self._clock() + intake.SCAN_INTERVAL_S
            if self._integrity_ok.is_set():  # held while damage holds work (R11)
                self._scan_folders()

    def _scan_folders(self):
        if self.finish_only:
            # Finish-only (#111 B5b): watched folders stay configured, but
            # nothing is scanned, so no new meeting comes in.
            self.last_scan = {"finish_only": True}
            return
        self._scan_watched()

    def _scan_watched(self):
        try:
            if not self._allowed("AllowWatchedFolders"):
                self.last_scan = {"managed_off": True}      # nothing is read
            else:
                self.last_scan = intake.scan(
                    self.store, AUDIO_EXTENSIONS, MAX_IMPORT_BYTES,
                    lambda p, fid, key, content: self._imports.put((p, fid, key, content)),
                    # Closing, or policy turning watched folders off meanwhile.
                    stop=lambda: (self._closing.is_set()
                                  or not self._allowed("AllowWatchedFolders")))
                if not self._allowed("AllowWatchedFolders"):
                    self.last_scan = {"managed_off": True}  # turned off part-way
        except Exception as exc:             # a scan problem must not stop watching
            self.last_scan = {"error": safe_message(exc)}

    def watched_folders(self):
        return {"folders": intake.summary(self.store), "last_scan": self.last_scan,
                "interval_minutes": intake.SCAN_INTERVAL_S // 60}

    def add_watched_folder(self, path, include_existing):
        if not self._allowed("AllowWatchedFolders"):
            raise WorkspaceError(MANAGED_OFF.format(what="Watched folders"))
        if not isinstance(include_existing, bool):
            raise WorkspaceError("Expected yes or no.")
        try:
            fid = intake.add_folder(self.store, path, include_existing)
        except intake.IntakeError as exc:
            raise WorkspaceError(str(exc)) from exc
        self.audit.record("folder_added", folder=fid, existing=include_existing)
        self._scan_wake.set()   # not scan_now: finish-only still saves it (B5b)
        return fid

    def set_folder_active(self, fid, active):
        if not isinstance(active, bool):
            raise WorkspaceError("Expected on or off.")
        intake.set_active(self.store, _hold_id(fid), active)
        if active:
            self._scan_wake.set()

    def set_folder_authority(self, fid, delete_originals=None, fetch_cloud=None,
                             confirmed=False):
        """confirmed: the person said Yes in the Windows question (the bridge
        asks); granting deletion is refused without it (#111 A6)."""
        fid = _hold_id(fid)
        for v in (delete_originals, fetch_cloud):
            if v is not None and not isinstance(v, bool):
                raise WorkspaceError("Expected on or off.")
        if delete_originals and not self._allowed("AllowDeleteOriginals"):
            raise WorkspaceError(MANAGED_OFF.format(what="Deleting original recordings"))
        if delete_originals and confirmed is not True:
            raise WorkspaceError("Allowing the app to delete originals needs your confirmation "
                                 "in Windows.")
        if delete_originals is not None:
            before = intake.authority_for(self.store, fid)
            intake.set_delete_originals(self.store, fid, delete_originals)
            after = intake.authority_for(self.store, fid)
            if after != before:                   # changed: granted (confirmed) or withdrawn
                self.audit.record("delete_authority", folder=fid, granted=after)
        if fetch_cloud is not None:
            intake.set_fetch_cloud(self.store, fid, fetch_cloud)
            self._scan_wake.set()

    def remove_folder(self, fid):
        fid = _hold_id(fid)
        row = self.store._one("SELECT delete_originals FROM watched_folders WHERE id = ?",
                              (fid,))
        intake.remove_folder(self.store, fid)
        if row is not None and audit.HEX32.fullmatch(fid):
            self.audit.record("folder_removed", folder=fid,
                              had_authority=bool(row["delete_originals"]))

    def scan_now(self):
        self._not_locked()
        self._not_finish_only()
        self._scan_wake.set()

    # -- processing thread ------------------------------------------------------------

    def _next_work(self):
        """The next job, in priority order, or None. Pure database scan, plus
        explicit speaker reruns the user asked for (#16 known count)."""
        now = time.time()
        # Every due cleanup job is considered: one held or waiting job must not
        # block cleanup of other meetings.
        for job in self.store._all(
                "SELECT id, meeting_id, phase FROM cleanup_jobs WHERE state = 'pending' OR "
                "(state = 'failed' AND COALESCE(last_attempt_at, 0) <= ?) ORDER BY intent_at",
                (now - FAILED_RETRY_S,)):
            if (not cleanup.is_held(self.store, job["meeting_id"], job["phase"])
                    and not self._waiting_on_audio(job)):
                return ("cleanup", job["meeting_id"], job["id"])
        if self._capturing() or self._drafts is not None:
            return None                      # heavy processing waits for the recording
                                             # and its draft worker (D4)
        with self._lock:
            for mid, count in list(self._reruns.items()):
                m = self.store.meeting_state(mid)
                v = self.store.active_version(mid)
                if m is None or v is None or m["reviewed_at"] or not self._audio_available(m):
                    del self._reruns[mid]           # no longer possible
                    continue
                if m["paused"]:
                    continue                        # kept for Resume
                return ("rerun", mid, (v["id"], count, self.store.skip_count(v["id"])))
        for m in self.store.meetings("paused = 0 AND phase != ? AND reviewed_at IS NULL "
                                     "AND audio_removed_at IS NULL", ("error",), content=False):
            v = self.store.active_version(m["id"])
            if v is None:
                continue
            if self.store.chunks(v["id"], status="pending"):
                return ("transcribe", m["id"], v["id"])
            if self._speakers_due(self.store.speaker_runs(v["id"])):
                return ("speakers", m["id"], v["id"])
        return None

    def _waiting_on_audio(self, job):
        return job["phase"] == cleanup.TRANSCRIPT and self.store._one(
            "SELECT 1 FROM cleanup_jobs WHERE meeting_id = ? AND phase = ? AND state != 'done'",
            (job["meeting_id"], cleanup.AUDIO)) is not None

    def _process_loop(self):
        while not self._closing.is_set():
            self._wake.wait(timeout=30)
            self._wake.clear()
            last, repeats = None, 0
            while not self._closing.is_set():
                if not self._integrity_ok.is_set():
                    break                    # damage holds every job and the purge (R11)
                try:
                    self._maybe_purge()
                    work = self._next_work()
                except StoreError:
                    work = None
                if work is None:
                    self._status = ""
                    break
                # Safety net against tight loops: the same work coming straight
                # back (e.g. a full disk stopping progress from being recorded)
                # waits for the next wake-up instead of spinning.
                repeats = repeats + 1 if work == last else 0
                last = work
                if repeats >= 3:
                    self._status = "Waiting before trying again"
                    break
                kind, mid, ref = work
                if kind in ("transcribe", "speakers") and not resources.may_start_automatic_work(
                        self.settings["battery_override"], battery=self._battery):
                    self._status = "Waiting: the computer is on battery power"
                    break
                with self._lock:
                    # Never start model work while a model is being replaced (#19).
                    if kind != "cleanup" and self.downloads.busy():
                        self._status = "Waiting for the model download to finish"
                        break
                    if kind != "cleanup" and (self._capturing() or self._drafts is not None):
                        break                # a recording started meanwhile (#30)
                    if kind != "cleanup" and self._removing:
                        self._status = "Waiting while a model is removed"
                        break
                    self._busy, self._busy_ref = (mid, kind), ref
                try:
                    self._run(kind, mid, ref)
                except Exception as exc:       # never let one meeting stop the loop
                    code = self.log.incident("processing", exc, job=kind)
                    try:
                        self.store.set_phase(mid, "error", f"Unexpected problem (code {code}): "
                                                           f"{safe_message(exc)}"[:300])
                    except StoreError:
                        pass
                finally:
                    with self._lock:           # cleared together, as they are set
                        self._busy = self._busy_ref = None
                    self._yielded = False      # only the run that recording stopped
                    if kind != "cleanup":      # a model may have failed its check at load
                        self._forget_display()

    def _maybe_purge(self):
        if time.time() - self._last_purge > RECEIPT_PURGE_S:
            holds.purge_expired_receipts(self.store)
            self._last_purge = time.time()

    def _run(self, kind, mid, ref):
        if kind == "cleanup":
            self._status = "Cleaning up"
            # With deletion authority withheld by policy, originals are kept,
            # exactly as when the user revokes a folder's authority. Re-read
            # now: a deletion never relies on a policy cached for 15 minutes.
            self.policy.refresh()
            roots = (intake.deletion_roots(self.store, mid)
                     if self._allowed("AllowDeleteOriginals") else [])
            cleanup.run_job(self.store, ref, self.media, quiesce=self.handle.shutdown,
                            original_roots=roots, on_event=self.audit.record)
            return
        if self._closing.is_set():
            return                          # never start a worker during shutdown
        m = self.store.meeting_state(mid)
        if m is None or m["reviewed_at"]:
            return                          # reviewed meanwhile: nothing more to process
        if not self._audio_available(m):
            # Missing audio (e.g. quarantined or deleted outside the app): stop
            # here with an explanation instead of retrying.
            v = self.store.active_version(mid)
            self.store.set_phase(mid, "error", runner.missing_audio_note(
                self._version_source(v["id"] if v else None)))
            return
        threads = resources.worker_threads(self.settings["mode"])
        self.handle.quiet = self.settings["mode"] == resources.QUIET
        # A model that is not ready names IT, not Manage downloads, when
        # managed policy turns downloads off (K3; unreadable: off). The worker
        # never reads the policy, so its error is reworded here (#257).
        downloads = self._model_downloads_allowed()
        if kind == "rerun":
            vid, count, skips = ref
            with self._lock:
                self._reruns.pop(mid, None)
                self._rerun_count = count
            self._status = "Identifying speakers"
            audio = self._processing_audio(mid, media.audio_ref, vid,
                                           speaker_pass=(count, skips))
            if audio is None:
                return
            device, site = self._choose_speaker_device()
            try:
                result = speakers.run_speakers(self.store, mid, vid, self.handle, audio,
                                               threads=threads, hint=count,
                                               model=self.speaker_model, device=device,
                                               gpu_site=site, skips_seen=skips,
                                               downloads_allowed=downloads)
                m = self.store.meeting_state(mid)
                if result == speakers.SpeakerResult.STOPPED and m and m["paused"]:
                    with self._lock:          # paused mid-pass: kept, with its count,
                        if self.store.skip_count(vid) == skips:    # unless skipped since
                            self._reruns.setdefault(mid, count)
            finally:
                self._gpu_pass = False
                with self._lock:
                    self._rerun_count = None
            return
        if kind == "transcribe":
            version = self.store.version(ref)
            spec = asr_engines.MANAGED_MODELS.get(version["model"])
            if spec is not None and spec.revision != version["model_revision"]:
                # Never continue a transcript with a different model revision.
                self.store.set_phase(mid, "error", "The speech model was updated since this "
                                     "meeting started. Use Transcribe again to continue.")
                return
            self._status = "Transcribing"
            reader = self._processing_audio(mid, media.open_audio, ref)
            if reader is None:
                return
            model = version["model"]
            device, site = self._choose_whisper_device(model)
            try:
                if device == "cuda":
                    engine = GpuWhisperEngine(self.handle, model, threads, site,
                                              gpu.WHISPER_COMPONENT)
                else:
                    engine = WorkerEngine(self.handle, model, threads,
                                          note=device if device != "cpu" else None)
                result = runner.run_version(self.store, mid, ref, reader, engine,
                                            downloads_allowed=downloads)
            finally:
                reader.close()               # release the handle before any cleanup
                self._gpu_whisper_run = False
            if result == runner.RunResult.STOPPED and not self._closing.is_set() \
                    and not self._yielded:
                self.store.set_paused(mid, True)    # Stop now = paused until resumed
            return
        self._status = "Identifying speakers"
        # Chosen only with no skip recorded (_speakers_due): one since stops it.
        audio = self._processing_audio(mid, media.audio_ref, ref, speaker_pass=(None, 0))
        if audio is None:
            return
        device, site = self._choose_speaker_device()
        try:
            speakers.run_speakers(self.store, mid, ref, self.handle, audio, threads=threads,
                                  model=self.speaker_model, device=device, gpu_site=site,
                                  skips_seen=0, downloads_allowed=downloads)
        finally:
            self._gpu_pass = False

    def _processing_audio(self, mid, opener, vid, speaker_pass=None):
        """The meeting's audio for the processing thread (opener:
        media.open_audio for transcription, media.audio_ref for speakers), or
        None once the reason is recorded (#111 C3a). A missing key or file is
        missing audio: the meeting's note. A key that will not open (also an
        incident, by type only) or audio that is damaged or not this
        meeting's: for transcription the meeting's note; for a speaker pass
        (speaker_pass: its hint and skips seen) a failed speaker run only, so
        a finished transcript stays usable (D4, code review of #281). Notes
        are chosen by the version's source, in fixed texts (P3)."""
        try:
            return opener(self.store, self.media, mid)
        except (KeyMissing, FileNotFoundError):
            note = runner.missing_audio_note(self._version_source(vid))
        except DamagedContent as exc:
            code = self.log.incident("audio_key", exc)
            if speaker_pass is not None:
                self._speaker_audio_failed(vid, speaker_pass, f" (code {code})")
                return None
            note = runner.missing_audio_note(self._version_source(vid))
        except Exception as exc:
            if not media.damaged_audio(exc):
                raise
            if speaker_pass is not None:
                self._speaker_audio_failed(vid, speaker_pass)
                return None
            note = runner.damaged_audio_note(self._version_source(vid))
        self.store.set_phase(mid, "error", note)
        return None

    def _speaker_audio_failed(self, vid, speaker_pass, code=""):
        hint, skips = speaker_pass
        speakers.record_failure(self.store, vid, speakers.AUDIO_UNREADABLE + code,
                                hint=hint, skips_seen=skips)

    def _version_source(self, vid):
        """The source a version was made from, or None."""
        version = self.store.version(vid) if vid is not None else None
        return self.store.source(version["source_id"]) if version else None

    def _user_audio(self, mid):
        """The meeting's audio for something the user asked for (playback,
        transcribe again), or WorkspaceError with fixed wording."""
        try:
            return media.open_audio(self.store, self.media, mid)
        except (DamagedContent, OSError):         # KeyMissing included
            raise WorkspaceError(AUDIO_UNREADABLE) from None
        except Exception as exc:
            if media.damaged_audio(exc):
                raise WorkspaceError(AUDIO_UNREADABLE) from None
            raise

    def _choose_speaker_device(self):
        """Choose the device and mark a GPU pass in one step, under the lock
        that a GPU component download or removal also takes."""
        with self._lock:
            device, site = self._speaker_device()
            self._gpu_pass = device == "cuda"
        return device, site

    def _speaker_device(self):
        """The graphics card for speaker identification only when the user
        turned it on, in Faster mode, with the component verified. Otherwise
        the processor; when the card was wanted but its component is missing or
        failed its check, the pass records why (#153), and the meeting says so."""
        if not (self.settings["gpu_speakers"] and self.settings["mode"] == resources.FASTER
                and self._allowed("AllowGpuComponent")
                and not self.downloads.involves(gpu.COMPONENT)):   # not while replaced or queued
            return "cpu", None
        if gpu.verified_for_use(self.cache_dir):
            return "cuda", str(gpu.site_dir(self.cache_dir))
        missing = gpu.status(self.cache_dir)[0] == "missing"
        if missing and gpu.WHISPER.status(self.cache_dir)[0] != "missing":
            return "cpu", None          # the card is on for Whisper only (#248)
        why = ("the graphics card component is not downloaded" if missing else
               "the graphics card component failed its check; repair it in Downloads")
        return f"cpu (graphics card not used: {why})", None

    def _choose_whisper_device(self, model):
        """Choose the device for a transcription run and mark a GPU run in one
        step, under the lock a GPU component download or removal also takes."""
        with self._lock:
            device, site = self._whisper_device(model)
            self._gpu_whisper_run = device == "cuda"
        return device, site

    def _whisper_device(self, model):
        """Whisper on the graphics card (#248) only when the user turned the
        card on, in Faster mode, for a model offered there
        (gpu.WHISPER_PRECISION), with its own component verified. Otherwise the
        processor. A component that is there but failed its check is recorded
        as the reason (#153), and the meeting says so; one never downloaded is
        not: the setting may be on for speaker identification only."""
        if not (model in gpu.WHISPER_PRECISION and self.settings["gpu_speakers"]
                and self.settings["mode"] == resources.FASTER
                and self._allowed("AllowGpuComponent")
                and not self.downloads.involves(gpu.WHISPER_COMPONENT)):   # or queued (#366)
            return "cpu", None
        if gpu.WHISPER.status(self.cache_dir)[0] == "missing":
            return "cpu", None
        if gpu.WHISPER.verified_for_use(self.cache_dir):
            return "cuda", str(gpu.WHISPER.site_dir(self.cache_dir))
        return ("cpu (graphics card not used: the graphics card component for Whisper failed "
                "its check; repair it in Downloads)"), None

    def _release_gpu_worker(self, name):
        """Before a graphics card component is replaced: stop the one worker
        that loads it, and leave the other running."""
        self.handle.release_gpu("whisper" if name == gpu.WHISPER_COMPONENT else "speakers")

    # -- controls -------------------------------------------------------------------------

    def _meeting(self, mid, content=False):
        """The meeting's plain state (store.meeting_state: nothing opened), or
        with content=True also its title and note (store.meeting). Raises
        WorkspaceError for an id that is not the app's own or not found."""
        try:
            media.meeting_folder(self.media, mid)   # the app's own id format only (#194)
        except media.BadMeetingId:
            raise WorkspaceError("Meeting not found.") from None
        m = self.store.meeting(mid) if content else self.store.meeting_state(mid)
        if m is None:
            raise WorkspaceError("Meeting not found.")
        return m

    def _error_note(self, m, damaged=None):
        """The note shown for a meeting, from its phase and stored note. A
        meeting whose text key will not open has no stored note: it could
        only be sealed under that key (#111 C2b-2), so the runner's fixed
        wording is shown instead, worked out here and never stored.
        damaged: the meeting's columns that did not open (store.meeting),
        read here when not given and only when needed."""
        if m["phase"] != "error":
            return None
        if m["last_error"] is not None:
            return m["last_error"]
        if damaged is None:
            full = self.store.meeting(m["id"])
            damaged = full.get("damaged", ()) if full else ()
        return runner.DAMAGED_NOTE if "title" in damaged else None

    def pause(self, mid):
        """Acknowledged at once; takes effect at the next safe point. Speaker
        identification works on the whole meeting at once and has no safe point
        inside, so pausing it stops the pass; Resume starts it again."""
        self._meeting(mid)
        with self._lock:
            self.store.set_paused(mid, True)
            busy = self._busy
            if busy and busy[0] == mid and busy[1] in ("speakers", "rerun"):
                self.handle.stop_now()       # a stopped rerun is kept for Resume (_run)

    def stop_now(self, mid):
        """Stop immediately: the part in progress is discarded (never committed)."""
        self._meeting(mid)
        self.store.set_paused(mid, True)
        if self._busy and self._busy[0] == mid:
            self.handle.stop_now()

    def resume(self, mid):
        m = self._meeting(mid)
        v = self.store.active_version(mid)
        if m["phase"] == "error" and v is not None:
            self.store.reset_failures(v["id"])          # the user's explicit "Try again"
            self.store.set_phase(mid, "transcribing")
        self.store.set_paused(mid, False)
        self._wake.set()

    def rename(self, mid, title):
        self._meeting(mid)
        title = _clean_text(title, MAX_TITLE_CHARS, label=True) or "Untitled meeting"
        self.store.rename_meeting(mid, title)

    def rerun_speakers(self, mid, count=None):
        m = self._meeting(mid)
        if m["reviewed_at"]:
            raise WorkspaceError("The transcript review is confirmed, so speakers can no "
                                 "longer be changed.")
        if not self._audio_available(m):
            raise WorkspaceError("The audio has been removed, so speakers cannot be "
                                 "identified again.")
        v = self.store.active_version(mid)
        if v is None or self.store.chunks(v["id"], status="pending"):
            raise WorkspaceError("Finish transcription first.")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int)
                                  or not 1 <= count <= speakers.MAX_HINT):
            raise WorkspaceError(f"Enter a number of people from 1 to {speakers.MAX_HINT}.")
        # Queued for the processing thread (one heavy job at a time); asking
        # again before it starts just replaces the request, and asking again
        # for the pass that is already running (a double click) is ignored.
        with self._lock:
            if (self._busy == (mid, "rerun") and self._rerun_count == count
                    and not self.handle._stop_requested.is_set()):
                self._reruns.pop(mid, None)     # this exact pass is already running
                return
            self._reruns[mid] = count
        self._wake.set()

    def skip_speakers(self, mid):
        """Go to review and hand off without speaker labels: no speaker pass
        for this meeting's current version, now or after transcription. Useful
        for one or two people, where an AI assistant does not need labels. A
        pass in progress stops at once. The transcript is not touched, and
        Identify speakers again still works later while the audio is kept."""
        m = self._meeting(mid)
        if m["reviewed_at"]:
            raise WorkspaceError("The transcript review is confirmed, so speakers can no "
                                 "longer be changed.")
        v = self.store.active_version(mid)
        if v is None:
            raise WorkspaceError("The recording is still being imported.")
        with self._lock:
            queued = mid in self._reruns          # its value (a count) may be None
            self._reruns.pop(mid, None)           # a queued Identify again is dropped
            chosen = self._chosen_skips(mid)
            running = chosen is not None
            # A pass already chosen but not yet started must see the skip
            # count change, so a new skip is recorded even after an earlier
            # one. A pass that saw a skip already (it is stopping) needs no
            # second one: a double click records nothing.
            again = queued or (running and chosen == self.store.skip_count(v["id"]))
            # Recorded before the pass is stopped, so nothing can restart it.
            # The check for labels is in the same transaction: a pass that
            # finished a moment ago wins, and nothing is recorded.
            result = self.store.skip_speaker_run(v["id"], again=again)
            if result == "labelled":
                raise WorkspaceError("Speakers are already identified for this meeting.")
            if running:
                self.handle.stop_now()
            if m["paused"] and not self.store.chunks(v["id"], status="pending"):
                # Paused speaker identification was the only work left.
                self.store.set_paused(mid, False)
        self._wake.set()

    def _chosen_skips(self, mid):
        """The skip count the speaker pass chosen for this meeting was picked
        with (an automatic pass only ever with none), or None if no speaker
        pass for it is chosen or running. Called under self._lock."""
        busy, ref = self._busy, self._busy_ref
        if not busy or busy[0] != mid:
            return None
        if busy[1] == "speakers":
            return 0
        if busy[1] == "rerun" and ref:
            return ref[2]
        return None

    def _speakers_due(self, runs):
        """An automatic speaker pass is due: once per version; an interrupted
        pass restarts (it cannot resume), a failed one waits for the user's
        explicit rerun, and a skipped one never runs unless the user asks."""
        return self.settings["speakers"] and not any(
            r["status"] in ("done", "failed", "running", "skipped") for r in runs)

    def _run_note(self, m, runs):
        """The latest run's note. An interrupted pass restarts by itself only
        if it is still due, or an Identify again is queued; otherwise the
        stored "will start again" would mislead."""
        last = runs[-1]
        if (last["status"] == "interrupted" and not self._speakers_due(runs)
                and m["id"] not in self._reruns):
            if m["reviewed_at"]:
                return "Speaker identification was interrupted; the transcript is unaffected."
            return ("Speaker identification was interrupted. Use Identify again to run it; "
                    "the transcript is unaffected.")
        return last["error"]

    def _can_skip(self, m, runs):
        return (not m["reviewed_at"] and not any(r["status"] == "done" for r in runs)
                and not (runs and runs[-1]["status"] == "skipped"
                         and m["id"] not in self._reruns))

    def retranscribe(self, mid):
        """Transcribe again with the current model. The previous version and
        its edits are kept (superseded), never overwritten. Also how a meeting
        that failed (phase "error") moves to the model now chosen in Settings
        (#244): its finished parts stay with the earlier version. A model that
        is not ready is refused before anything changes. Its state is the
        Downloads page's (cached up to DISPLAY_CACHE_S), so files removed
        outside the app in that window can still fail at load; the earlier
        version and its edits stay as they were."""
        m = self._meeting(mid)
        if not self._audio_available(m):
            raise WorkspaceError("The audio has been removed, so the meeting cannot be "
                                 "transcribed again.")
        if m["reviewed_at"]:
            raise WorkspaceError("The transcript review is confirmed; it cannot be replaced.")
        if self._busy and self._busy[0] == mid:
            raise WorkspaceError("Pause this meeting first.")
        # Read once: the version made below uses this model, even if Settings
        # changes while it is checked.
        model = self.settings["model"]
        if model in SPEECH_MODELS:            # test engines have no download state
            note = _model_note(model, self._speech_states().get(model, "missing"),
                               self._model_downloads_allowed())
            if note:
                raise WorkspaceError(note)
        v = self.store.active_version(mid)
        source = self.store._one("SELECT id FROM sources WHERE meeting_id = ? "
                                 "ORDER BY created_at DESC LIMIT 1", (mid,))
        reader = self._user_audio(mid)
        try:
            plan = plan_chunks(reader, len(reader))
        except Exception as exc:
            if media.damaged_audio(exc):
                raise WorkspaceError(AUDIO_UNREADABLE) from None
            raise
        finally:
            reader.close()
        new = self._new_version(mid, source["id"], plan, model)
        if v is not None and new == v["id"]:
            if m["phase"] == "error":
                raise WorkspaceError(f"This meeting already uses {asr_engines.label(model)}. "
                                     "Use Try again, or choose another speech model in "
                                     "Settings first.")
            raise WorkspaceError("It is already transcribed with this model and settings. "
                                 "Change the model in Settings first.")
        self._carry_skip(v, new)
        # Clears a failed meeting's error with its phase: the processing
        # thread skips only meetings in the error phase, so the new version
        # starts at the next wake.
        self.store.set_phase(mid, "transcribing")
        self._wake.set()

    def _carry_skip(self, old, new_vid):
        """A meeting whose speakers were skipped keeps that choice when it is
        transcribed again (a new version would otherwise queue a pass)."""
        if old is None:
            return
        runs = self.store.speaker_runs(old["id"])
        if runs and runs[-1]["status"] == "skipped":
            self.store.skip_speaker_run(new_vid)      # never over labels (store checks)

    # -- review -------------------------------------------------------------------------

    def _audio_available(self, m):
        try:
            return (m["audio_removed_at"] is None
                    and media.audio_path(self.media, m["id"]).is_file())
        except media.BadMeetingId:
            return False                      # a damaged row never breaks the list (#194)

    def _version(self, mid, version):
        """The active version, or (read-only) an earlier one by its position in
        detail()["older_versions"]."""
        if version is None:
            return self.store.active_version(mid)
        older = [x for x in self.store.versions(mid) if x["state"] != "active"]
        if (isinstance(version, bool) or not isinstance(version, int)
                or not 0 <= version < len(older)):
            raise WorkspaceError("That earlier version is not available.")
        return older[version]

    def transcript(self, mid, start=0, count=500, version=None):
        self._meeting(mid)
        v = self._version(mid, version)
        if v is None:
            return {"total": 0, "start": 0, "items": []}
        # Paged in SQL: only this page's passages are read and opened (#111).
        # Under the store's lock, so the total and the page agree.
        with self.store._lock:
            total = self.store.passage_count(v["id"])
            page = self.store.transcript_page(v["id"], start, count, words=False)
        view = speaker_names.resolve(self.store, v["id"])
        return {"total": total, "start": start, "items": [
            {"id": r["id"], "t": r["start_s"], "end": r["end_s"], "text": r["text"],
             "edited": bool(r["edited"]), "label": view["labels"].get(r["id"]),
             "who": view["names"].get(view["labels"].get(r["id"])),
             "overlap": bool(view["overlap"].get(r["id"]))} for r in page]}

    # -- speaker names (#21) ------------------------------------------------------------

    def _naming_version(self, mid):
        m = self._meeting(mid)
        if m["reviewed_at"]:
            raise WorkspaceError("The transcript review is confirmed, so speakers can no "
                                 "longer be changed.")
        v = self.store.active_version(mid)
        if v is None:
            raise WorkspaceError("Speakers have not been identified for this meeting.")
        return v["id"]

    def _naming(self, fn, *args):
        try:
            return fn(*args)
        except speaker_names.NamingError as exc:
            raise WorkspaceError(str(exc)) from exc
        except DamagedContent:
            raise WorkspaceError(DAMAGED_REFUSAL) from None

    def speakers(self, mid):
        self._meeting(mid)
        v = self.store.active_version(mid)
        if v is None:
            return {"speakers": [], "can_undo": False}
        return speaker_names.summary(self.store, v["id"])

    def speaker_panel(self, mid, offsets=None):
        """Speakers, names and examples for the Name speakers panel (one pass)."""
        self._meeting(mid)
        v = self.store.active_version(mid)
        if v is None:
            return {"speakers": [], "can_undo": False}
        offsets = offsets or {}
        if not isinstance(offsets, dict) or len(offsets) > 100 or not all(
                isinstance(k, str) and isinstance(o, int) and not isinstance(o, bool)
                and 0 <= o <= 10**6 for k, o in offsets.items()):
            raise WorkspaceError("Out of range.")
        return speaker_names.panel(self.store, v["id"], offsets)

    def speaker_examples(self, mid, label, offset=0):
        self._meeting(mid)
        v = self.store.active_version(mid)
        if v is None or not isinstance(label, str):
            raise WorkspaceError("Speaker not found.")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10**6:
            raise WorkspaceError("Out of range.")
        return speaker_names.examples(self.store, v["id"], label, offset)

    def rename_speaker(self, mid, label, name):
        vid = self._naming_version(mid)
        self._naming(speaker_names.rename, self.store, vid, label,
                     _clean_text(name, speaker_names.MAX_NAME, label=True))

    def merge_speakers(self, mid, label, into):
        vid = self._naming_version(mid)
        self._naming(speaker_names.merge, self.store, vid, label, into)

    def assign_speaker(self, mid, segment_id, label):
        vid = self._naming_version(mid)
        if isinstance(segment_id, bool) or not isinstance(segment_id, int):
            raise WorkspaceError("That passage is not in the current transcript.")
        return self._naming(speaker_names.assign, self.store, vid, segment_id, label)

    def undo_speakers(self, mid):
        vid = self._naming_version(mid)
        return self._naming(speaker_names.undo, self.store, vid)

    def search(self, mid, query, version=None):
        """Ids of matching passages (ids, not positions, so highlights stay
        right while the page is being updated)."""
        self._meeting(mid)
        query = _clean_text(query, 200)
        v = self._version(mid, version)
        if not query or v is None:
            return []
        return self.store.search(v["id"], query)

    def edit(self, mid, segment_id, text):
        """Autosaved edit of one passage, kept separate from the generated text."""
        m = self._meeting(mid)
        if m["reviewed_at"]:
            raise WorkspaceError("The transcript review is confirmed, so it can no longer be "
                                 "edited.")
        v = self.store.active_version(mid)
        if (v is None or isinstance(segment_id, bool) or not isinstance(segment_id, int)
                or self.store._one("SELECT 1 FROM segments WHERE id = ? AND version_id = ?",
                                   (segment_id, v["id"])) is None):
            raise WorkspaceError("That passage is not in the current transcript.")
        try:
            self.store.save_edit(segment_id, _clean_text(text, MAX_EDIT_CHARS))
        except DamagedContent:
            raise WorkspaceError(DAMAGED_REFUSAL) from None   # D1: an unreadable edit stays
        return self._segment(segment_id)

    def undo(self, mid):
        m = self._meeting(mid)
        if m["reviewed_at"]:
            raise WorkspaceError("The transcript review is confirmed, so it can no longer be "
                                 "edited.")
        v = self.store.active_version(mid)
        try:
            sid = self.store.undo_edit(v["id"]) if v else None
        except DamagedContent:
            raise WorkspaceError(DAMAGED_REFUSAL) from None
        return self._segment(sid) if sid else None

    def _segment(self, sid):
        r = self.store.passage(sid)
        return {"id": r["id"], "text": r["text"], "edited": bool(r["edited"])}

    def clip(self, mid, start, end):
        """A bounded piece of the meeting audio as 16-bit WAV bytes."""
        m = self._meeting(mid)
        if not self._audio_available(m):
            raise WorkspaceError("The audio has been removed after review, so playback is no "
                                 "longer available.")
        for value in (start, end):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 \
                    or value != value:
                raise WorkspaceError("Expected a time in seconds.")
        if end <= start or end - start > MAX_CLIP_S:
            raise WorkspaceError(f"Clips are limited to {MAX_CLIP_S:.0f} seconds.")
        reader = self._user_audio(mid)
        try:
            gain = self._playback_gain(mid, reader)
            samples = reader.read(int(start * SR), int(end * SR))
        except Exception as exc:
            if media.damaged_audio(exc):
                raise WorkspaceError(AUDIO_UNREADABLE) from None
            raise
        finally:
            reader.close()               # never keep the audio open (cleanup, #17)
        if not len(samples):
            raise WorkspaceError("That time is after the end of the recording.")
        return media.apply_playback_gain(samples, gain)

    def _playback_gain(self, mid, reader):
        """The gain that plays this meeting at a standard level (the owner's
        decision 2(b), 25 Oct 2026; media.playback_gain), worked out on the
        meeting's first clip and kept in memory for this audio file only:
        the file's random id changes whenever the meeting's audio is written
        again, so a gain is never stale. Damaged audio (a block where the
        sample is read that does not open) plays as it is, and that is
        remembered; a file error that may pass (another program holding the
        file) plays this clip as it is and is tried again on the next. Never
        logged, shown or kept on disk (P3); the saved audio never changes."""
        key = (mid, reader.file_id)
        with self._playback_gains_lock:
            gain = self._playback_gains.get(key)
        if gain is not None:
            return gain
        try:
            gain = media.playback_gain(reader)
        except OSError:
            return 1.0                     # may pass: not remembered (review nit 3)
        except Exception as exc:
            if not media.damaged_audio(exc):
                raise
            gain = 1.0
        with self._playback_gains_lock:
            for old in [k for k in self._playback_gains if k[0] == mid]:
                del self._playback_gains[old]          # an earlier audio file of this meeting
            while len(self._playback_gains) >= PLAYBACK_GAINS_KEPT:
                del self._playback_gains[next(iter(self._playback_gains))]
            self._playback_gains[key] = gain
        return gain

    # -- lists and details --------------------------------------------------------------

    def meetings(self):
        """The Meetings list. Each table is read once for the whole list, not
        once per meeting, so the number of queries does not grow with the
        number of meetings (R7b). Read under the store's lock: one consistent
        view, with no write from another thread in between."""
        q, active = self.store._all, "SELECT id FROM versions WHERE state = 'active'"
        with self.store._lock:
            with self._imports.mutex:     # an import running or queued (#181, #193)
                importing = self._imports.unfinished_tasks > 0
            rows = holds.unfinished_work(self.store)
            if not rows:
                return []
            # Plain state and the error note: the titles come once, from
            # unfinished_work above.
            meetings = {m["id"]: m for m in self.store.meetings(content=("last_error",))}
            versions = {v["meeting_id"]: v for v in q(
                "SELECT * FROM versions WHERE state = 'active'")}
            chunks = {c["version_id"]: c for c in q(
                "SELECT version_id, COALESCE(SUM(CASE WHEN status = 'done' THEN end_sample - "
                "start_sample END), 0) AS done, COALESCE(SUM(end_sample - start_sample), 0) "
                "AS total, SUM(status = 'pending') AS pending FROM chunks "
                f"WHERE version_id IN ({active}) GROUP BY version_id")}
            runs = _group(self.store.active_speaker_runs(), "version_id")
            sources = {s["id"]: s for s in q(
                "SELECT id, samples, sample_rate FROM sources WHERE id IN "
                "(SELECT source_id FROM versions WHERE state = 'active')")}
            jobs = _group(q("SELECT id, meeting_id, phase, state, error FROM cleanup_jobs "
                            "ORDER BY intent_at"), "meeting_id")
            # The current identification: the latest finished run (runs are oldest first).
            current = {vid: done[-1] for vid, rs in runs.items()
                       if (done := [r for r in rs if r["status"] == "done"])}
            # Corrections of finished runs of active versions: a version has at
            # most one (finishing a run supersedes the one before), the current.
            logs = _group(self.store.active_corrections(), "run_id") if current else {}
            # Known counts cost nothing; a new run or correction is worked out
            # here, still under the lock, from the same view of the data.
            counts = {vid: self._speaker_count(vid, run, _log_key(logs.get(run["id"], [])))
                      for vid, run in current.items()}
        working = self._busy                 # read once: a job may end meanwhile
        out = []
        for row in rows:
            m = meetings.get(row["id"])
            if m is None:
                continue
            # Columns of the meeting that did not open, from the titles read
            # above: never a read per meeting (#111 C4b). Not for the page.
            row = dict(row)
            damaged = row.pop("damaged", ())
            v = versions.get(row["id"])
            c = chunks.get(v["id"]) if v else None
            done, total = (c["done"], c["total"]) if c else (0, 0)
            busy = working and working[0] == row["id"] and working[1]
            stage = self._stage(m, v, pending=bool(c and c["pending"]))
            known = (sources.get(v["source_id"]), counts.get(v["id"])) if v else None
            out.append({**row, **self._facts(v, known),
                        "audio_available": self._audio_available(m),
                        "cleanup": [{"id": j["id"], "phase": j["phase"], "state": j["state"],
                                     "note": j["error"]} for j in jobs.get(row["id"], [])],
                        "progress": round(100 * done / total) if total else 0,
                        "paused": bool(m["paused"]), "error": self._error_note(m, damaged),
                        "busy": busy or None, "stage": stage,
                        # No version and no import running: an import that did
                        # not finish (kept by a hold, or after Continue anyway).
                        "unfinished": v is None and not importing
                        and not m["reviewed_at"],
                        "can_skip": bool(v) and self._can_skip(m, runs.get(v["id"], [])),
                        "can_retranscribe": bool(v) and self._can_retranscribe(m, stage),
                        "action": self._action(m, stage)})
        return out

    def _facts(self, v, known=None):
        """Recording length, number of speakers and model of a working version.
        known: (source row, speaker count) when the caller has worked them out
        already (meetings()); otherwise read here."""
        if v is None:
            return {"duration_s": None, "speaker_count": None, "model": None,
                    "model_label": None}
        if known is None:
            with self.store._lock:
                run = speaker_names.current_run(self.store, v["id"])
                log = _log_key(self.store.corrections(run["id"])) if run else ()
                known = (self.store.source(v["source_id"]),
                         self._speaker_count(v["id"], run, log))
        src, count = known
        return {"duration_s": round(src["samples"] / src["sample_rate"], 1) if src else None,
                "speaker_count": count, "model": v["model"],
                "model_label": asr_engines.label(v["model"])}

    def _ran_on(self, vid):
        """Where a version was transcribed, for its details (#248): the
        processor or the graphics card, the precision, the number of parts,
        and whether the card was given up for the processor. Only those
        facts: the recorded reason stays in the database."""
        return [{"where": "gpu" if (r["backend"] or "").startswith("cuda") else "cpu",
                 "precision": r["precision"], "parts": r["parts"],
                 "fallback": "graphics card not used" in (r["backend"] or "")}
                for r in self.store.ran_on(vid)]

    def _speaker_count(self, vid, run, log):
        """Speakers in the current identification (run), kept per version
        until the run or its corrections change. log is the whole (small)
        correction log: an undone correction's id can be reused by the next
        one, so the last id alone could repeat a key. Call under the store's
        lock, so the count comes from the same data as its key."""
        if run is None:
            return None
        key = (run["id"], log)
        cache = self.__dict__.setdefault("_speaker_counts", {})
        hit = cache.get(vid)
        if hit is None or hit[0] != key:
            if len(cache) > 1000:             # versions no longer listed
                cache.clear()
            hit = cache[vid] = (key, len(speaker_names.resolve(self.store, vid)["order"]))
        return hit[1]

    def _can_retranscribe(self, m, stage):
        """Transcribe again is offered while the audio is kept and the review
        is not confirmed, except while the meeting is still being transcribed.
        A meeting that failed is still in the inbox, but can switch model
        (#244)."""
        return (self._audio_available(m) and not m["reviewed_at"]
                and (stage != "inbox" or m["phase"] == "error"))

    @staticmethod
    def _action(m, stage):
        if stage == "inbox":
            return ("Needs attention" if m["phase"] == "error" else
                    "Paused" if m["paused"] else "Transcribing")
        return {"review": "Review the transcript",
                "handoff": "Hand off, then confirm the final document is saved",
                "cleanup": "Cleanup pending"}[stage]

    def _stage(self, m, v, pending=None):
        """inbox: still being processed; review / handoff / cleanup: active
        meetings. pending: whether the version has chunks still to do, when
        the caller has read it already."""
        if m["final_saved_at"]:
            return "cleanup"
        if m["reviewed_at"]:
            return "handoff"
        if v is None:
            return "inbox"
        if pending is None:
            pending = bool(self.store.chunks(v["id"], status="pending"))
        if pending or m["phase"] == "error":
            return "inbox"
        return "review"

    def detail(self, mid):
        m = self._meeting(mid, content=True)
        v = self.store.active_version(mid)
        runs = self.store.speaker_runs(v["id"]) if v else []
        older = [x for x in self.store.versions(mid) if v is None or x["id"] != v["id"]]
        due = {h["id"] for h in holds.due_for_review(self.store)}
        done, total = self.store.progress(v["id"]) if v else (0, 0)
        stage = self._stage(m, v)
        return {
            "id": mid, "title": m["title"], "created_at": m["created_at"],
            "stage": stage, "paused": bool(m["paused"]),
            "error": self._error_note(m, m.get("damaged", ())),
            "busy": (self._busy[1] if self._busy and self._busy[0] == mid else None),
            "progress": round(100 * done / total) if total else 0,
            "audio_available": self._audio_available(m),
            "audio_removed_at": m["audio_removed_at"],
            "reviewed_at": m["reviewed_at"], "final_saved_at": m["final_saved_at"],
            **self._facts(v),
            "ran_on": self._ran_on(v["id"]) if v else [],
            "edits": self.store.edit_count(v["id"]) if v else 0,
            "can_undo": bool(v) and not m["reviewed_at"] and self.store.can_undo(v["id"]),
            "older_versions": [{"index": i, "model": x["model"],
                                "model_label": asr_engines.label(x["model"]),
                                "created_at": x["created_at"],
                                "edits": self.store.edit_count(x["id"]),
                                "ran_on": self._ran_on(x["id"])}
                               for i, x in enumerate(older)],
            "can_skip": bool(v) and self._can_skip(m, runs),
            "can_retranscribe": bool(v) and self._can_retranscribe(m, stage),
            "speakers": ({"status": runs[-1]["status"], "error": self._run_note(m, runs),
                          "device": runs[-1]["backend"],
                          "count": runs[-1]["hint_count"],
                          "started_at": runs[-1]["started_at"]} if runs else None),
            "holds": [{"id": h["id"], "scope": h["scope"], "reason": h["reason"],
                       "created_at": h["created_at"], "due_review": h["id"] in due,
                       "review_due_at": (h["reviewed_at"] or h["created_at"])
                       + holds.HOLD_REVIEW_DAYS * 86400}
                      for h in holds.active_holds(self.store, mid)],
            "cleanup": [{"id": j["id"], "phase": j["phase"], "state": j["state"],
                         "note": j["error"]} for j in self.store._all(
                "SELECT * FROM cleanup_jobs WHERE meeting_id = ? ORDER BY intent_at", (mid,))],
            "limits": cleanup.LIMITS,
        }

    def reminders(self):
        """Shown while the app is open: holds due for review, cleanup that
        waits for a confirmation, and the retention reminders (#111 C6):
        meetings whose next step is "due" or "action_required" under the
        thresholds in force, and recordings recovered after a crash that
        still wait for Keep or Delete (their start time from the in-memory
        report; no journal is read). Nothing is scheduled outside the app."""
        titles = {m["id"]: m["title"] for m in self.store.meetings(content=("title",))}
        p, now = self.policy.current(), time.time()
        limits = holds.thresholds(p)
        try:
            retention = holds.retention_state(self.store, p, now)
        except StoreError:
            retention = []        # a damaged database (R11): its own notice says so
        waiting = []
        for r in list(self._recording_reports):
            if r.get("recovered") and r.get("pending") and not r.get("discarded"):
                started = r.get("started_at")
                days = (max(0, int((now - started) // holds.DAY))
                        if isinstance(started, (int, float)) else 0)
                waiting.append({"id": r["id"], "days": days, "state": holds.reminder_level(
                    days, limits["review_days"], limits["action_days"])})
        return {
            "holds_due": [{"id": h["id"], "meeting": h["meeting_id"],
                           "title": titles.get(h["meeting_id"], ""), "reason": h["reason"]}
                          for h in holds.due_for_review(self.store)],
            "awaiting_confirmation": [{"id": j["id"], "meeting": j["meeting_id"],
                                       "title": titles.get(j["meeting_id"], ""),
                                       "phase": j["phase"]}
                                      for j in holds.awaiting_confirmation(self.store)],
            "retention": [{"meeting": r["meeting"], "title": titles.get(r["meeting"], ""),
                           "step": r["step"], "days": r["days"], "state": r["state"]}
                          for r in retention if r["state"]],
            "recordings_waiting": waiting,
            "thresholds": limits,
        }

    # -- diagnostics (R9b, #130) ---------------------------------------------------------

    def report_ui_error(self, event, line, col):
        """A page script error (the page's error and unhandledrejection
        events): fixed values and numbers only, at most PAGE_ERRORS a run
        and one a second, so a page cannot flood the log."""
        now = self._clock()
        with self._lock:
            sent = self.__dict__.setdefault("_page_errors", [0, float("-inf")])
            if sent[0] >= PAGE_ERRORS or now - sent[1] < 1.0:
                return None
            sent[0], sent[1] = sent[0] + 1, now
        code = diag.new_code()
        self.log.write("incident", code=code, area="page", event=event, line=line, col=col)
        return code

    def _retention_line(self, p):
        """Copy diagnostics: the reminder thresholds in force, who set them,
        and counts only (#111 C6)."""
        limits = holds.thresholds(p)
        try:
            counts = holds.retention_counts(holds.retention_state(self.store, p))
        except StoreError:
            # A damaged database (R11): the rest of the diagnostics still count.
            counts = {"counts": "unreadable"}

        def shown(key):
            days = limits[f"{key}_days"]
            return f"{key}_days={days or 'off'}" + (
                " (policy)" if limits["managed"][key] else " (app default)")
        line = ("Retention reminders: " + shown("review") + ", " + shown("action") + "; "
                + ", ".join(f"{k}={v}" for k, v in counts.items()))
        if limits["action_days"] and limits["action_days"] <= limits["review_days"]:
            line += "; the review-due threshold has no effect (action required comes first)"
        return line

    def diagnostics(self):
        """What Copy diagnostics copies: versions, settings and policy values,
        counts, model states and the newest log records. Built only from
        values the app controls (each a known value or a token without
        spaces), and the records pass the log's field rules again (see
        diag.shareable_records): no meeting content, names, folders or tokens."""
        import platform

        from workspace import update
        token = re.compile(r"[A-Za-z0-9_.+-]{1,60}")      # no spaces: no free text
        word = lambda v: v if isinstance(v, str) and token.fullmatch(v) else "?"  # noqa: E731
        lines = ["Unminute diagnostics (no meeting content)"]
        try:
            version = update.package_version()
        except Exception:
            version = "?"
        deployment = update.deployment()
        lines.append(f"Version: {word(str(version))}; install: "
                     f"{word(deployment.get('mode'))}; Windows {word(platform.version())}; "
                     f"Python {word(platform.python_version())}")
        settings = []
        for key in DEFAULT_SETTINGS:
            value = self.settings.values.get(key)
            try:
                self.settings._check(key, value)    # one of its known values only
                shown = ("on" if value else "off") if isinstance(value, bool) else word(value)
            except WorkspaceError:
                shown = "?"
            settings.append(f"{key}={shown}")
        lines.append("Settings: " + ", ".join(settings))
        p = self.policy.current()
        # A folder, name or address shows only as "set": its shape was
        # checked, but it is still text IT typed, not one of a set of values.
        managed = [f"{name}=" + ("invalid" if name in p.invalid else "set"
                                 if policy.SPEC[name][0] in ("path", "name", "url")
                                 else str(p.get(name)))
                   for name in p.summary()["managed"] if name in policy.SPEC]
        # How many value names Unminute does not recognise (#111 D2): a count
        # only, since a name is text IT typed (policy summary() lists them).
        unknown = len(p.unrecognised)
        lines.append("Managed policy: " + (", ".join(managed) if managed else "none")
                     + ("; unreadable" if p.unreadable else "")
                     + (f"; names not recognised: {unknown}" if unknown else ""))
        # The key's state (#111 C4a), with the codes IT needs while locked.
        lines.append(_key_line(self.locked, self._lock_code))
        try:
            counts = self.store._one(
                "SELECT (SELECT COUNT(*) FROM meetings) AS meetings, "
                "(SELECT COUNT(*) FROM holds WHERE released_at IS NULL) AS holds, "
                "(SELECT COUNT(*) FROM cleanup_jobs) AS cleanup, "
                "(SELECT COUNT(*) FROM watched_folders) AS folders, "
                # Meetings, not jobs: a confirmation adds one job per phase.
                # Confirmed means pending or failed: a job a released hold
                # left awaiting_confirmation needs the user to confirm again.
                "(SELECT COUNT(DISTINCT meeting_id) FROM cleanup_jobs "
                "WHERE state IN ('pending', 'failed')) AS unfinished")
        except StoreError:                    # a locked database that is not this version's
            counts = None
        if counts is None:
            lines.append("Workspace: ?")
        else:
            lines.append("Workspace: " + ", ".join(f"{k}={counts[k]}" for k in
                                                   ("meetings", "holds", "cleanup", "folders")))
            # Plain counts IT needs before renaming a locked workspace (#111 C4a):
            # meetings already confirmed for removal stay in the renamed folder.
            lines.append(f"Active holds: {counts['holds']}")
            lines.append(f"Cleanup confirmed and not finished: {counts['unfinished']}")
        lines.append(self._retention_line(p))
        try:
            cards = self._display_value("cards", lambda: setup.cards(self.cache_dir))
            lines.append("Models: " + ", ".join(f"{word(c['name'])}={word(c['state'])}"
                                                 for c in cards))
        except Exception:
            lines.append("Models: ?")
        info = gpu.detected()
        lines.append("Graphics card: " + (f"NVIDIA, driver {word(info.get('driver'))}, "
                                          f"{'supported' if info.get('driver_ok') else 'too old'}"
                                          if info else "none found"))
        lines.append(f"Crashes in the last 7 days: {diag.recent_crashes(self.log.folder)}")
        lines.append(self._audit_line())
        checked = diag.integrity_state(self.log.folder)       # values it has checked
        if checked is None:
            lines.append("Database check: not run")
        else:
            code = f", code {checked['code']}" if checked["code"] else ""
            lines.append(f"Database check: {checked['result']} ({checked['t'] or '?'}{code})")
        lines.append("Recent log records (newest last):")
        lines += [json.dumps(r, separators=(",", ":"))
                  for r in diag.shareable_records(self.log.folder, limit=40)]
        return "\n".join(lines) + "\n"

    def _audit_line(self):
        """Copy diagnostics' audit log line: numbers and states only (#111 C5)."""
        try:
            a = self.audit.summary()
        except Exception:
            return "Audit log: ?"

        def number(v):
            return v if isinstance(v, int) and not isinstance(v, bool) else "?"
        if a["mirror"] == "ok":
            mirror = "Event Log copy ok"
        elif a["mirror"] == "off":
            mirror = "no Event Log copy"
        else:
            since = a["failing_since"]
            when = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
                    if isinstance(since, int) else "?")
            mirror = f"Event Log copy failing since {when}"
        seq = "none" if a["seq"] is None else number(a["seq"])
        return (f"Audit log: newest seq {seq}; {number(a['written'])} written this run; "
                f"{mirror} ({number(a['unmirrored'])} without a copy); failed writes "
                f"{number(a['failed_writes'])}; segments dropped "
                f"{number(a['dropped_segments'])}; suppressed {number(a['suppressed'])}; "
                f"refused {number(a['refused'])}")

    def copy_diagnostics(self):
        """Copy diagnostics to the clipboard (kept out of clipboard history and
        cloud sync, like every copy the app makes)."""
        text = self.diagnostics()
        try:
            clipboard.copy_text(text)
        except clipboard.ClipboardError as exc:
            raise WorkspaceError(str(exc)) from exc
        return {"chars": len(text)}

    # -- database check (R11, #132) ------------------------------------------------------

    def continue_despite_damage(self):
        """Continue anyway: the user lets work start on a damaged database,
        and the start-up upkeep that waited runs now. The next start checks
        again (and holds again while the damage stays). Does nothing while
        locked (#111 C4a): continuing could neither read nor write content."""
        if self.locked is not None:
            return
        with self._lock:
            if self._integrity["state"] != "failed":
                return
            self._integrity = {"state": "continued", "code": self._integrity["code"]}
        self.log.write("integrity", result="continued", code=self._integrity["code"])
        self.audit.record("damage_continued")
        try:
            self.store.mark_interrupted()
            self._upkeep(discard=False)       # nothing removed on a damaged database
        except StoreError:
            pass                              # damaged pages: tried again at the next start
        finally:
            self._integrity_ok.set()
            self._wake.set()
            self._scan_wake.set()

    def _integrity_notice(self):
        """For the page: the damage found and its code, while work is held."""
        with self._lock:
            info = dict(self._integrity)
        return info if info["state"] == "failed" else None

    def status(self):
        held = not self._integrity_ok.is_set()
        waiting = self._imports.unfinished_tasks
        message = self._status
        if held:                              # nothing runs: say why, once (R11)
            message = "On hold: the workspace database is damaged" + (
                f" · {waiting} import{'s' if waiting > 1 else ''} waiting" if waiting else "")
        locked = self._locked_notice()
        if locked is not None:                # the locked screen states the damage too
            message = "Locked"
        return {"busy": self._busy[1] if self._busy else None, "message": message,
                "mode": self.settings["mode"],
                "import_errors": list(self._import_errors),
                "importing": 0 if held else waiting,
                "changes": self._changes(),
                "integrity": None if locked is not None else self._integrity_notice(),
                "locked": locked,
                # The page's banner and hidden record and import actions (#111 B5b).
                "finish_only": self.finish_only}

    def _locked_notice(self):
        """For the page (#111 C4a): why the workspace is locked, the incident
        code, the Windows error number and the code of any damage the
        database check found; None when it is not locked. Fixed words,
        numbers and codes only."""
        if self.locked is None:
            return None
        damage = self._integrity_notice()
        return {"reason": self.locked.reason if self.locked.reason in diag.KEY_REASONS else None,
                "code": self._lock_code, "win_error": diag.windows_error(self.locked.code),
                "damage": damage["code"] if damage else None}

    def _changes(self):
        """A number that differs whenever the Meetings list or the reminders
        could: a database commit, the job running, speaker passes asked for,
        or the mode. The page asks for the lists only then (and every minute
        anyway, for ages and hold reviews). Opaque: it carries no ids."""
        busy = self._busy
        return hash((self.store.revision, busy[:2] if busy else None,
                     frozenset(self._reruns), self.settings["mode"])) & 0xFFFFFFFF

    def clear_import_errors(self):
        self._import_errors = []

    def receipts(self):
        return [{"id": r["job_id"], "requested_at": r["created_at"],
                 "completed_at": r["completed_at"], "result": r["result"]}
                for r in holds.receipts(self.store)]

    def _speech_states(self):
        """Each speech model's state ("ready", "missing" or "invalid"), worked
        out as the Downloads page does (setup.status) and kept as long as its
        cards (DISPLAY_CACHE_S, or until a download, removal or check ends).
        Every speech model has a card there (#246; a test keeps it so)."""
        cards = self._display_value("cards", lambda: setup.cards(self.cache_dir))
        return {c["name"]: c["state"] for c in cards if c["role"] == "speech"}

    def get_settings(self):
        self.policy.refresh()             # re-read on each poll while Settings or Downloads is open
        states = self._speech_states()
        downloads = self._model_downloads_allowed()
        choices = [{"name": n, "label": asr_engines.label(n),
                    "recommended": n == asr_engines.RECOMMENDED_PARAKEET,
                    # The page labels a model that is not ready and shows the
                    # note beside the picker, with Manage downloads only if
                    # policy allows downloads (#244, K3). The choice is still saved.
                    "state": states.get(n, "missing"),
                    "offered": downloads,
                    "note": _model_note(n, states.get(n, "missing"), downloads)}
                   for n in SPEECH_MODELS]
        from workspace import update
        info = gpu.detected()
        effective = dict(self.settings.values)
        if not self._allowed("AllowMicrophoneRecording"):
            effective["microphone"] = False
        if not (COMPUTER_AUDIO_STARTS and self._computer_audio_allowed()
                and effective["microphone"]):
            effective["computer_audio"] = False
        if not self._allowed("AllowGpuComponent"):
            effective["gpu_speakers"] = False
        lid_managed = self._display_value("lid_managed", self.lid.managed)
        if not self._allowed("AllowLidOverride") or lid_managed:
            effective["lid_override"] = False
        if not self._allowed("AllowLiveDrafts"):
            effective["live_drafts"] = False
        return {**effective, "choices": choices, "managed": self.managed(),
                # Live drafts (#104) need Parakeet v2: its state, for the
                # switch's note, and whether Manage downloads can fetch it.
                "live_drafts_model": {"state": states.get(drafts.DRAFT_MODEL, "missing"),
                                      "label": asr_engines.label(drafts.DRAFT_MODEL),
                                      "offered": downloads},
                # Settings on their default because the saved value could not
                # be used (R11): names of settings only, never their values.
                "repaired": {"keys": list(self.settings.repaired),
                             "all": self.settings.unreadable},
                "lid_managed": lid_managed,
                "gpu": {"card": info["name"] if info else None,
                        "driver_ok": bool(info and info["driver_ok"]),
                        "min_driver": gpu.MIN_DRIVER_TEXT,
                        "component": self._display_value(
                            "gpu", lambda: gpu.status(self.cache_dir))[0] if info
                        else "missing",
                        # Whisper on the graphics card (#248): its own component.
                        "whisper_component": self._display_value(
                            "gpu-whisper", lambda: gpu.WHISPER.status(self.cache_dir))[0]
                        if info else "missing"},
                "deployment": update.deployment(),
                # Counts from the database and the recordings folder: read
                # again whenever either changes.
                "update_blockers": self._display_value(
                    "blockers", lambda: [b["message"] for b in
                                         update.blockers(self.root / DB_FILE)],
                    version=(self.store.revision, _folders(self.recordings)))}

    def set_settings(self, changes):
        if not isinstance(changes, dict):
            raise WorkspaceError("Expected settings.")
        if changes.get("microphone") is True and not self._allowed("AllowMicrophoneRecording"):
            raise WorkspaceError(MANAGED_OFF.format(what="Microphone recording"))
        if changes.get("computer_audio") is True:
            # The page part of #108 offers the switch (COMPUTER_AUDIO_STARTS
            # is True). If the feature is ever turned off again, this keeps the
            # switch refused and nothing in the same change is saved.
            if not COMPUTER_AUDIO_STARTS:
                raise WorkspaceError("Computer audio is not available in this version.")
            if not self._computer_audio_allowed():
                raise WorkspaceError(MANAGED_OFF.format(what="Computer audio recording"))
            if not changes.get("microphone", self.settings["microphone"]):
                raise WorkspaceError("Turn on microphone recording first: computer audio is "
                                     "recorded beside the microphone.")
        if changes.get("microphone") is False:
            changes = {**changes, "computer_audio": False}    # never without the microphone
        if changes.get("gpu_speakers") is True and not self._allowed("AllowGpuComponent"):
            raise WorkspaceError(MANAGED_OFF.format(what="The graphics card component"))
        if changes.get("live_drafts") is True and not self._allowed("AllowLiveDrafts"):
            raise WorkspaceError(MANAGED_OFF.format(what="Draft text while recording"))
        if changes.get("live_drafts") is True and not drafts.ON_PAGE:
            raise WorkspaceError("Draft text while recording is not available in this "
                                 "version.")
        if changes.get("lid_override") is True:
            if not self._allowed("AllowLidOverride"):
                raise WorkspaceError(MANAGED_OFF.format(what="Changing the lid setting"))
            if self.lid.managed():
                raise WorkspaceError("Your organisation sets what closing the lid does, so "
                                     "Unminute leaves it alone. Ask your IT team.")
        if changes.get("gpu_speakers") is True:
            info = gpu.detected()
            if info is None or not info["driver_ok"]:
                raise WorkspaceError("This computer has no NVIDIA graphics card with a recent "
                                     f"enough driver ({gpu.MIN_DRIVER_TEXT} or later).")
            if "ready" not in (gpu.status(self.cache_dir)[0],
                               gpu.WHISPER.status(self.cache_dir)[0]):
                raise WorkspaceError("Download a graphics card component in Settings > "
                                     "Manage downloads first.")
        if changes.get("microphone") is False and self._recording is not None:
            raise WorkspaceError("Stop the recording before turning the microphone off.")
        quiet_before = self.handle.quiet
        self.settings.update(**changes)
        if changes.get("lid_override") is False and self.lid.active:
            try:
                self.lid.restore()            # turned off mid-recording: put it back now
            except Exception:
                pass                          # never fails the setting; retried while idle
            self._lid_result = None
        self._forget_display()
        self.handle.quiet = self.settings["mode"] == resources.QUIET
        if self.handle.quiet != quiet_before and self._busy is None:
            self.handle.shutdown()        # the next worker starts with the new priority
        self._wake.set()
        self._policy_wake.set()           # live drafts follow the switch at once (#104)
        return self.get_settings()

    # -- handoff (#22) ------------------------------------------------------------------

    def _handoff(self, fn, *args):
        try:
            return fn(*args)
        except (handoff.HandoffError, clipboard.ClipboardError) as exc:
            raise WorkspaceError(str(exc)) from exc
        except DamagedContent:
            # Part of the meeting cannot be read (#111 C2b-2): no preview, so
            # nothing is copied or saved either (_checked builds it first).
            raise WorkspaceError(DAMAGED_REFUSAL) from None

    @property
    def templates(self):
        """The handoff templates (templates.sealed, sealed under the workspace
        key; #111 C2c), built at their first use in a session, when a plain
        templates.json from an earlier version is converted. While the store
        is locked nothing is built, read, converted or logged: its
        KeyUnavailable is raised (C4a's bridge gate refuses first)."""
        with self._templates_lock:
            if self._templates is None:
                if self.store.locked is not None:
                    raise self.store.locked
                sealer = self.store.file_sealer(handoff.TEMPLATES_FILE)
                templates = handoff.open_templates(self.root, sealer)
                if templates.unreadable:
                    self.log.write("templates_unreadable")      # content-free (P3)
                self._templates = templates
            return self._templates

    def handoff_templates(self):
        return self.templates.all()

    def save_template(self, tid, title, prompt):
        if tid is not None and (not isinstance(tid, str) or len(tid) > 40):
            raise WorkspaceError("That template no longer exists.")
        if not isinstance(title, str) or not isinstance(prompt, str):
            raise WorkspaceError("Expected text.")
        return self._handoff(self.templates.save, tid, title, prompt)

    def reset_template(self, tid):
        if not isinstance(tid, str):
            raise WorkspaceError("That template no longer exists.")
        self._handoff(self.templates.reset, tid)

    def handoff_preview(self, mid, tid, anonymise=False):
        """Exactly what Copy and Save will produce, plus the options in use.
        anonymise (this handoff only, not stored) replaces speaker names with
        Speaker 1, Speaker 2."""
        self._meeting(mid)
        if not isinstance(anonymise, bool):
            raise WorkspaceError("Expected yes or no.")
        template = self._handoff(self.templates.get, tid)
        out = self._handoff(handoff.build, self.store, mid, template, anonymise)
        if self._allowed("HandoffFilenameIncludesTitle") and not anonymise:
            out = {**out, "filename": handoff.title_filename(self.store.meeting(mid)["title"],
                                                             out["filename"])}
        return {**out, "options": handoff.options(self.store, mid),
                "assistant": self._assistant_shown(self._assistant())}

    def set_handoff_options(self, mid, omitted, redactions):
        m = self._meeting(mid)
        if not m["reviewed_at"]:
            raise WorkspaceError("Confirm the transcript review first.")
        self._handoff(handoff.set_options, self.store, mid, omitted, redactions)

    def _checked(self, mid, tid, kind, expected, anonymise=False):
        """The handoff text, refused unless it is exactly what was previewed."""
        out = self.handoff_preview(mid, tid, anonymise)
        if not isinstance(expected, str) or out["digests"][kind] != expected:
            raise WorkspaceError("The handoff changed since the preview. Check the preview "
                                 "again, then copy or save.")
        return out

    def copy_handoff(self, mid, tid, kind, expected, anonymise=False):
        if kind not in ("combined", "prompt_only"):
            raise WorkspaceError("Choose what to copy.")
        out = self._checked(mid, tid, kind, expected, anonymise)
        sequence = self._handoff(clipboard.copy_text, out[kind])
        self._record_handoff("handoff_copied", mid, out, what=kind)
        minutes = self.policy.current().get("ClipboardClearMinutes")
        if minutes:
            self._schedule_clipboard_clear(sequence, minutes * 60)
        return {"chars": len(out[kind]), "long": out["long"] and kind == "combined",
                "clears_in_minutes": minutes or None}

    def _record_handoff(self, kind, mid, out, **extra):
        """The audit record of a handoff that left the app: the meeting,
        whether the template is a built-in one (possibly edited) or the
        user's own, and counts. Never the template's text or anything copied
        (#111 C5). A handoff whose details cannot be read for the record is
        logged as an incident and still goes ahead."""
        try:
            fields = {"meeting": mid,
                      "template": ("built_in" if out["template"]["id"] in handoff.BUILTIN
                                   else "custom"),
                      "names_replaced": bool(out["anonymised"]), "omitted": out["omitted"],
                      "redactions": out["redactions"], **extra}
        except (KeyError, TypeError) as exc:
            self.log.incident("audit", exc)
            return
        self.audit.record(kind, **fields)

    # -- copy and open the assistant (#109) ---------------------------------------------

    def _assistant(self, p=None):
        """(name, address) of the assistant Copy and open uses, or None when
        policy turns it off or sets it badly. IT sets the name and address
        together: one without the other, or either one invalid, withholds the
        button rather than falling back to the default, so the app never opens
        ChatGPT where IT meant something else. An unreadable policy withholds
        it too (AllowHandoffAssistant is a permission)."""
        p = p or self.policy.current()
        if not p.get("AllowHandoffAssistant"):
            return None
        keys = ("HandoffAssistantName", "HandoffAssistantUrl")
        if any(k in p.invalid for k in keys):
            return None
        name, url = (p.get(k) for k in keys)
        if name is None and url is None:
            return handoff.DEFAULT_ASSISTANT
        if name is None or url is None:
            return None
        return name, url

    @staticmethod
    def _assistant_shown(assistant):
        """What the page may show: the name and the host, never a path or query."""
        if assistant is None:
            return None
        from urllib.parse import urlsplit
        return {"name": assistant[0], "host": urlsplit(assistant[1]).hostname}

    def _assistant_setting(self, p):
        """For Settings: whether IT set the assistant, and what is in force."""
        keys = ("AllowHandoffAssistant", "HandoffAssistantName", "HandoffAssistantUrl")
        if not any(p.managed(k) for k in keys):
            return None
        assistant = self._assistant(p)
        if assistant is not None:
            return {"state": "set", **self._assistant_shown(assistant)}
        return {"state": self._assistant_withheld(p)}

    @staticmethod
    def _assistant_withheld(p):
        """Why policy withholds Copy and open: "unreadable" (the key exists
        but could not be read), "off" (IT turned it off) or "invalid" (a value
        is not valid, or only one of the name and address is set)."""
        if p.unreadable:
            return "unreadable"
        return "off" if p.values.get("AllowHandoffAssistant") is False else "invalid"

    def _open_address(self, url):
        """Ask Windows to open a validated https address in the user's default
        browser. The app process makes no connection itself (N2)."""
        if self._opener is not None:
            self._opener(url)
        else:
            os.startfile(url)              # Windows only: checked before anything is copied

    def copy_and_open_assistant(self, mid, tid, expected, anonymise=False):
        """Copy the prompt and transcript exactly as previewed, then open the
        assistant's fixed address (policy, or the ChatGPT default) in the
        user's browser. The page supplies no address or name, and nothing is
        added to the address: the user pastes, checks and sends. The policy is
        read again first, so a change IT made applies at this click."""
        if self._opener is None and not hasattr(os, "startfile"):
            raise WorkspaceError("Opening the browser works only on Windows.")
        p = self.policy.refresh()
        assistant = self._assistant(p)
        if assistant is None:
            raise WorkspaceError(ASSISTANT_WITHHELD[self._assistant_withheld(p)])
        name, url = assistant
        with self._assistant_lock:
            last, now = self._assistant_opened_at, self._clock()
            if last is not None and now - last < ASSISTANT_PAUSE_S:
                raise WorkspaceError("Already opening. Wait a moment and try again.")
            self._assistant_opened_at = now
        try:
            copied = self.copy_handoff(mid, tid, "combined", expected, anonymise)
        except BaseException:
            with self._assistant_lock:     # nothing opened: the next click may try again
                if self._assistant_opened_at == now:
                    self._assistant_opened_at = last
            raise
        try:
            self._open_address(url)
            opened = True
        except OSError:
            opened = False                 # the copy stays; the page says to open it by hand
        if opened:
            self.audit.record("assistant_opened", meeting=mid,
                              assistant=("default" if assistant == handoff.DEFAULT_ASSISTANT
                                         else "policy"))
        return {**copied, "opened": opened, **self._assistant_shown(assistant)}

    def _schedule_clipboard_clear(self, sequence, delay_s):
        """Clear the app's own copy later (policy), unless something else was
        copied since. The policy thread waits until it is due, checking at
        least every POLICY_IDLE_WAIT_S against the monotonic clock, which
        counts time asleep (a plain timer's wait does not, so it would clear
        late after sleep). Pending clears also run when the app closes."""
        with self._clipboard_lock:
            self._clipboard_clears.append((self._clock() + delay_s, sequence))
        self._policy_wake.set()

    def _run_clipboard_clears(self, everything=False):
        now = self._clock()
        with self._clipboard_lock:
            due = [s for t, s in self._clipboard_clears if everything or t <= now]
            self._clipboard_clears = [] if everything else [
                (t, s) for t, s in self._clipboard_clears if t > now]
        for sequence in due:
            try:
                clipboard.clear_if_unchanged(sequence)
            except clipboard.ClipboardError:
                pass                    # busy: the copy stays, as without the policy

    def _flush_clipboard_clears(self):
        self._run_clipboard_clears(everything=True)

    def _policy_loop(self):
        """Its own thread, so a long folder scan never delays a clear or a stop."""
        while True:
            # Cleared before the closing check: a clear scheduled, or close(),
            # after this point ends the wait below at once.
            self._policy_wake.clear()
            if self._closing.is_set():
                return
            try:
                self._policy_tick()
            except Exception:            # tried again at the next tick
                pass
            self._policy_wake.wait(self._policy_wait_s())

    def _policy_wait_s(self):
        """Every second while recording or opening a recording (start_recording
        wakes the thread), so a policy change read meanwhile stops it at once.
        Otherwise until the next clipboard clear is due, capped at
        POLICY_IDLE_WAIT_S: on time while awake, and at most that late after
        the computer wakes (a wait does not count time asleep)."""
        if self._capturing():
            return 1.0
        with self._clipboard_lock:
            due = min((t for t, _ in self._clipboard_clears), default=None)
        if due is None:
            return POLICY_IDLE_WAIT_S
        return max(0.0, min(due - self._clock(), POLICY_IDLE_WAIT_S))

    def _policy_tick(self):
        """Due clipboard clears, and a recording that policy no
        longer allows is stopped and saved (the policy is re-read every 15
        minutes, or on each refresh while Settings or Downloads is open). A key
        that cannot be read refuses new recordings but does not cut one short:
        it is read again within 30 seconds."""
        self._run_clipboard_clears()
        p = self.policy.current()
        try:
            self._queue_policy()              # #366: waiting downloads policy now refuses
        except Exception:
            pass
        try:
            if not p.get("AllowLidOverride") and self.lid.active:
                self.lid.restore()            # IT turned it off: put the lid action back
                self._lid_result = None
            elif self.lid.active and self._clock() - self._lid_retry_at >= LID_RETRY_S:
                # Under the lock that sets the lid for a recording, so a retry
                # can never undo the override of one that has just started.
                with self._indicator_lock:
                    if not self._power_on and not self._capturing():
                        self._lid_retry_at = self._clock()
                        self.lid.restore()    # a restore that failed earlier: try again
            self._lid_problem_report()
        except Exception:
            pass
        stop = not p.get("AllowMicrophoneRecording") and not p.unreadable
        rec = self._recording
        if stop and rec is not None:
            self._finish_recording(rec, event=(
                "policy_stop", "Stopped because your organisation turned off microphone "
                               "recording"))
        elif (rec is not None and rec.computer_on and not p.unreadable
              and not p.get("AllowComputerAudioRecording")):
            # Only the computer track stops; the microphone carries on. Like
            # the microphone, an unreadable key does not cut a track short.
            rec.stop_computer(COMPUTER_POLICY_STOP)
        try:
            self._draft_tick(p)
        except Exception as exc:          # drafts never affect the recording
            self.log.incident("drafts", exc)

    def handoff_folder(self):
        """Where the handoff Save dialog starts: the organisation's folder if
        policy sets one that exists, else the dialog's own default."""
        folder = self.policy.current().get("HandoffDefaultFolder")
        return folder if folder and Path(folder).is_dir() else ""

    def save_handoff_to(self, mid, tid, path, expected, anonymise=False):
        """Write the Markdown to a file the user chose in the save dialog. The
        app keeps no record of it; the user files or deletes it. ".md" is
        appended (never substituted) when missing, and a file the dialog did
        not ask about is never overwritten."""
        from workspace.safe_paths import check_raw
        out = self._checked(mid, tid, "markdown", expected, anonymise)
        try:
            check_raw(path)
        except UnsafePathError as exc:
            raise WorkspaceError(str(exc)) from exc
        target = Path(path)
        if target.suffix.lower() != ".md":
            target = target.with_name(target.name + ".md")
            if target.exists():
                raise WorkspaceError(f"{target.name} already exists. Choose another name.")
        try:
            with open(target, "w", encoding="utf-8", newline="\n") as f:
                f.write(out["markdown"])
        except OSError as exc:
            reason = exc.strerror or "file error"
            raise WorkspaceError(f"Could not save the file: {reason}.") from exc
        self._record_handoff("handoff_saved", mid, out)
        return {"chars": len(out["markdown"]), "name": target.name}

    # -- setup (#19) ----------------------------------------------------------------------

    def setup_state(self):
        # The cards check every model's files and the component's file list;
        # the token answers come from Credential Manager. Both are kept for
        # DISPLAY_CACHE_S. Download progress is live.
        cards = [dict(c) for c in self._display_value("cards",
                                                      lambda: setup.cards(self.cache_dir))]
        self._queue_policy()                   # the current policy, while Downloads is open
        waiting = self.downloads.queued()
        for c in cards:
            dl = self.downloads.state.get(c["name"])
            # How far it is, as a number (R12): the page never reads it out of a message.
            c["download"] = dict(dl, percent=self.downloads.percent(c["name"])) if dl else None
            if dl and dl.get("state") == "queued" and c["name"] in waiting:
                c["download"]["position"] = waiting.index(c["name"]) + 1   # 1: next (#366)
        speech_ready = any(c["state"] == "ready" for c in cards if c["role"] == "speech")
        remembered = self._display_value("tokens", credentials.has_token)
        return {"cards": cards, "token_remembered": remembered,
                "onboarded": self.settings["onboarded"], "speech_ready": speech_ready,
                "speakers": self.settings["speakers"], "managed": self.managed()}

    def download_model(self, name, token=None, remember=True):
        """Download or repair a model. For the speaker model the token is the
        one given now (remembered in Credential Manager unless the user
        unticks Remember), or the remembered one."""
        if not isinstance(name, str) or not setup.known(name):
            raise WorkspaceError("Unknown model.")
        if not self._allowed("AllowModelDownloads"):
            raise WorkspaceError(DOWNLOADS_OFF)
        gpu_only = gpu.component(name) is not None   # used by no work unless a GPU run
        if gpu_only and not self._allowed("AllowGpuComponent"):
            raise WorkspaceError(MANAGED_OFF.format(what="The graphics card component"))
        if not gpu_only and not self._user_models_allowed():
            raise WorkspaceError(USER_MODELS_OFF)
        if token is not None and not isinstance(token, str):
            raise WorkspaceError("Expected a token.")
        on_success = None
        if name in asr_engines.MANAGED_MODELS and asr_engines.MANAGED_MODELS[name].gated:
            try:
                if token:
                    token = token.strip() or None
                    if token and remember:
                        # Remembered only once a download has shown it works.
                        on_success = (lambda t=token: credentials.save_token(t))
                else:
                    token = credentials.load_token()
            except credentials.CredentialError as exc:
                raise WorkspaceError(str(exc)) from exc
            if not token:
                self._forget_display()         # Setup may still offer a token removed elsewhere
                raise WorkspaceError("Enter your Hugging Face token to download the speaker "
                                     "model.")
        else:
            token = None                       # public models never receive a token
        with self._lock:                       # atomic with the processing thread's start
            if self._busy and self._busy[1] != "cleanup" and not (
                    gpu_only and not self._gpu_in_use(name)):
                raise WorkspaceError("Pause processing first: models cannot be replaced "
                                     "while a meeting is being processed.")
            try:
                # "started", or "queued" behind the download or check running (#366).
                return self.downloads.start(name, token, on_success)
            except setup.SetupError as exc:
                raise WorkspaceError(str(exc)) from exc

    def _prepare_queued_download(self, name, token):
        """A queued download is about to start (#366): the checks of
        download_model again, as things may have changed while it waited.
        Policy that turns downloads off (K3; unreadable: off) ends the whole
        queue; anything else ends this one only. Returns the token to send:
        the one typed with the click (memory only), else the remembered one."""
        if not self._allowed("AllowModelDownloads"):
            raise setup.QueueStopped(DOWNLOADS_OFF)
        gpu_only = gpu.component(name) is not None
        if gpu_only and not self._allowed("AllowGpuComponent"):
            raise setup.SetupError(MANAGED_OFF.format(what="The graphics card component"))
        if not gpu_only and not self._user_models_allowed():
            raise setup.SetupError(USER_MODELS_OFF)
        with self._lock:
            # No job starts while a download waits; one that was running at the
            # click (only a graphics card component may be queued then) may
            # since have put that component to use.
            if self._busy and self._busy[1] != "cleanup" and not (
                    gpu_only and not self._gpu_in_use(name)):
                raise setup.SetupError("Pause processing first: models cannot be replaced "
                                       "while a meeting is being processed.")
        if not (name in asr_engines.MANAGED_MODELS and asr_engines.MANAGED_MODELS[name].gated):
            return None                        # public models never receive a token
        if not token:
            try:
                token = credentials.load_token()
            except credentials.CredentialError as exc:
                raise setup.SetupError(str(exc)) from exc
        if not token:
            self._forget_display()
            raise setup.SetupError("Enter your Hugging Face token to download the speaker "
                                   "model.")
        return token

    def _queue_policy(self):
        """Waiting downloads that policy no longer allows end now, and say
        why (#366, K3): all of them when downloads are off, or the graphics
        card components when those are. A running download carries on."""
        if not self.downloads.queued():
            return
        if not self._allowed("AllowModelDownloads"):
            self.downloads.drop_queued(DOWNLOADS_OFF)
            return
        if not self._allowed("AllowGpuComponent"):
            self.downloads.drop_queued(MANAGED_OFF.format(what="The graphics card component"),
                                       only=lambda n: gpu.component(n) is not None)
        if not self._user_models_allowed():
            self.downloads.drop_queued(USER_MODELS_OFF, only=lambda n: gpu.component(n) is None)

    def remove_model(self, name):
        """Remove a downloaded model, or the graphics card component, from this
        computer. Refused while the model is still needed. Returns GB freed."""
        if not isinstance(name, str) or not setup.known(name):
            raise WorkspaceError("Unknown model.")
        if self.downloads.busy():
            raise WorkspaceError("Wait for the download or check to finish first.")
        with self._lock:      # the processing thread takes this lock to start any job
            if self._removing:
                raise WorkspaceError("Another model is being removed. Wait a moment.")
            self._check_removable(name)
            self._removing = name             # no job starts until removal ends
        try:
            if gpu.component(name) is not None:
                self._release_gpu_worker(name)
                # The setting goes off with the last graphics card component.
                others = [c for c in gpu.COMPONENTS.values() if c.key != name]
                if self.settings["gpu_speakers"] and not any(
                        c.status(self.cache_dir)[0] == "ready" for c in others):
                    self.settings.update(gpu_speakers=False)
            else:
                self._before_model_change()    # the workers may still hold it loaded
            freed = setup.remove(name, self.cache_dir)
        except setup.RemovalRefused as exc:
            raise WorkspaceError(str(exc)) from exc
        except OSError as exc:
            raise WorkspaceError("Some files could not be removed. Close other apps that may "
                                 "be using them, then try again.") from exc
        finally:
            with self._lock:
                self._removing = None
            self._forget_display()
            self._wake.set()
        self.downloads.state.pop(name, None)
        return round(freed / 1e9, 2)

    def start_gpu_upkeep(self, delay_s=GPU_UPKEEP_DELAY_S, **kwargs):
        """Remove unused graphics card components in the background, delay_s
        after start-up (#111 B5b, the owner's decision B5-4). Called by the
        app's own start for the current version only (never compat or
        fallback). Not while locked."""
        if self.locked is not None:
            return

        def run():
            if self._closing.wait(delay_s):
                return
            try:
                self.remove_unused_gpu(**kwargs)
            except Exception as exc:              # upkeep must never stop the app
                self.log.incident("setup", exc)
        threading.Thread(target=run, daemon=True, name="gpu-upkeep").start()

    def remove_unused_gpu(self, install_root=None, mode=None, other=None):
        """Remove the graphics card component copies no installed version of
        the app names (update.gpu_keep, from each version's version.json),
        and only when this user has one install: a per-user and a machine
        install share the model cache. Nothing is removed when either cannot
        be known, while a download, check, removal or graphics card run is
        going on, or from a source checkout. install_root, mode and other:
        tests only (the install folder, the install mode and whether the
        other mode is registered). Returns [(version, bytes)] removed. Logs
        the versions and sizes only."""
        from workspace import update
        if install_root is None:
            mode = update.deployment()["mode"]
            if mode == "source":
                return []
            install_root = Path(update.__file__).resolve().parents[4]
        keep = update.gpu_keep(install_root)
        if keep is None:
            self.log.write("gpu_kept", why="unknown")
            return []
        other = update.other_install(mode) if other is None else other
        if other is not False:
            self.log.write("gpu_kept", why="other_install" if other else "unknown")
            return []
        with self._lock:
            # Checked once, at the start: what is removed is never a version
            # this app downloads, checks, removes or runs (those are kept).
            if (self.downloads.busy() or self._removing or self._gpu_pass
                    or self._gpu_whisper_run):
                self.log.write("gpu_kept", why="busy")      # tried again next start
                return []
        # The running version's own components are always kept.
        removed = gpu.remove_superseded(
            self.cache_dir, keep | {c.version for c in gpu.COMPONENTS.values()}, log=self.log)
        if removed:
            self._forget_display()
        return removed

    def _check_removable(self, name):
        """Refuse removal while the model is still needed (under the lock)."""
        if gpu.component(name) is not None:
            if self._gpu_in_use(name):
                what = ("Transcription" if name == gpu.WHISPER_COMPONENT
                        else "Speaker identification")
                raise WorkspaceError(f"{what} is using the graphics card. Pause it first.")
        else:
            spec = asr_engines.MANAGED_MODELS[name]
            if name == self.settings["model"]:
                raise WorkspaceError("This is the speech model selected in Settings. Choose "
                                     "another speech model first.")
            if spec.family == "pyannote" and self.settings["speakers"]:
                raise WorkspaceError("Turn off speaker identification in Settings first.")
            if spec.family == "pyannote" and self._reruns:
                raise WorkspaceError("A speaker pass you asked for is still waiting (for a paused "
                                     "meeting, it runs on Resume). Let it finish first.")
            waiting = [m for m in self.store._all(
                "SELECT m.id FROM meetings m JOIN versions v ON v.meeting_id = m.id "
                "AND v.state = 'active' WHERE v.model = ? AND EXISTS (SELECT 1 FROM chunks c "
                "WHERE c.version_id = v.id AND c.status = 'pending')", (name,))]
            if waiting:
                are = "meetings are" if len(waiting) != 1 else "meeting is"
                raise WorkspaceError(f"{len(waiting)} {are} still being transcribed with this "
                                     "model. Finish or remove them first.")
            if self._busy and self._busy[1] != "cleanup":
                raise WorkspaceError("Pause processing first.")

    def _gpu_in_use(self, name=gpu.COMPONENT):
        """The worker that loads this graphics card component is running now: a
        speaker pass, or Whisper transcribing, on the card (each set before its
        worker starts, so there is no gap)."""
        return self._gpu_whisper_run if name == gpu.WHISPER_COMPONENT else self._gpu_pass

    def verify_model(self, name):
        if not isinstance(name, str):
            raise WorkspaceError("Unknown model.")
        with self._lock:
            if self._busy and self._busy[1] != "cleanup" and not (
                    gpu.component(name) is not None and not self._gpu_in_use(name)):
                raise WorkspaceError("Pause processing first.")
            try:
                self.downloads.verify(name)
            except setup.SetupError as exc:
                raise WorkspaceError(str(exc)) from exc

    def cancel_download(self, name=None):
        """Cancel the running download, or (name) the one named: running,
        or waiting in the queue, which then leaves it (#366)."""
        if name is not None and (not isinstance(name, str) or not setup.known(name)):
            raise WorkspaceError("Unknown model.")
        return self.downloads.cancel(name)

    def remove_token(self):
        """Forget the remembered token. Downloaded models keep working offline."""
        try:
            return credentials.remove_token()
        except credentials.CredentialError as exc:
            raise WorkspaceError(str(exc)) from exc
        finally:
            self._forget_display()

    def finish_setup(self, speakers):
        """speakers: the user's choice. Turning them on before the speaker model
        has finished downloading is fine: separation waits until it is ready
        (a missing model fails with an explanation and can be rerun)."""
        if not isinstance(speakers, bool):
            raise WorkspaceError("Expected on or off.")
        self.settings.update(onboarded=True, speakers=speakers)
        self._wake.set()

    def open_help(self, key):
        """Open one of a fixed set of help pages in the user's own browser."""
        url = setup.HELP_LINKS.get(key) if isinstance(key, str) else None
        if url is None:
            raise WorkspaceError("Unknown page.")
        import webbrowser
        webbrowser.open(url)

    # -- holds and cleanup --------------------------------------------------------------

    def add_hold(self, mid, scope, reason):
        self._meeting(mid)
        try:
            hid = holds.add_hold(self.store, mid, scope, _clean_text(reason, 500))
        except holds.HoldError as exc:
            raise WorkspaceError(str(exc)) from exc
        self.audit.record("hold_placed", meeting=mid, hold=hid, scope=scope)   # never the reason
        return hid

    def _hold_row(self, hid):
        """A hold's plain columns (never its reason), or None."""
        return self.store._one("SELECT meeting_id, scope, released_at FROM holds WHERE id = ?",
                               (hid,))

    def release_hold(self, hold_id):
        hid = _hold_id(hold_id)
        before = self._hold_row(hid)
        jobs = holds.release_hold(self.store, hid)
        after = self._hold_row(hid)
        if before is not None and before["released_at"] is None \
                and after is not None and after["released_at"] is not None:
            self.audit.record("hold_released", meeting=before["meeting_id"], hold=hid,
                              scope=before["scope"])
        return jobs

    def keep_hold(self, hold_id):
        hid = _hold_id(hold_id)
        holds.mark_reviewed(self.store, hid)
        row = self._hold_row(hid)
        if row is not None and row["released_at"] is None:
            self.audit.record("hold_kept", meeting=row["meeting_id"], hold=hid,
                              scope=row["scope"])

    def confirm_reviewed(self, mid):
        self._meeting(mid)
        if self._busy and self._busy[0] == mid:
            raise WorkspaceError("Wait until processing of this meeting has finished.")
        try:
            jid = cleanup.confirm_transcript_reviewed(self.store, mid)
        except cleanup.CleanupError as exc:
            raise WorkspaceError(str(exc)) from exc
        self.audit.record("transcript_reviewed", meeting=mid, job=jid)
        self._wake.set()
        return jid

    def discard_meeting(self, mid):
        """Delete a meeting that is not needed, with one explicit confirmation
        (cleanup.discard). Work in progress on it is stopped first. A meeting
        whose import never finished can be discarded too, but only while no
        import is running or queued (#181). Only the intake thread (fed by
        self._imports) creates meetings, adds sources or grants delete_original,
        so no import can still give the meeting a source or deletion authority.
        (Start-up upkeep plans no version for a meeting Discard has marked.)"""
        self._meeting(mid)
        with self._lock:
            self._reruns.pop(mid, None)
            busy = self._busy
            if busy and busy[0] == mid and busy[1] != "cleanup":
                self.handle.stop_now()
            with self._imports.mutex:     # queued or in progress (task_done comes last)
                importing = self._imports.unfinished_tasks > 0
            try:
                cleanup.discard(self.store, mid, unfinished=not importing)
            except cleanup.CleanupError as exc:
                raise WorkspaceError(str(exc)) from exc
        self.audit.record("meeting_discarded", meeting=mid)
        self._wake.set()

    def confirm_final_saved(self, mid):
        self._meeting(mid)
        try:
            jid = cleanup.confirm_final_saved(self.store, mid)
        except cleanup.CleanupError as exc:
            raise WorkspaceError(str(exc)) from exc
        self.audit.record("final_saved", meeting=mid, job=jid)
        self._wake.set()
        return jid

    def _job_row(self, jid):
        return self.store._one("SELECT meeting_id, phase, state FROM cleanup_jobs WHERE id = ?",
                               (jid,))

    def confirm_cleanup(self, job_id):
        """Explicit confirmation for cleanup that waited on a released hold."""
        jid = _hold_id(job_id)
        before = self._job_row(jid)
        holds.confirm_pending_cleanup(self.store, jid)
        after = self._job_row(jid)
        if before is not None and before["state"] == "awaiting_confirmation" \
                and after is not None and after["state"] == "pending":
            self.audit.record("cleanup_confirmed", meeting=before["meeting_id"],
                              phase=before["phase"], job=jid)
        self._wake.set()

    def retry_cleanup(self, job_id):
        """The user's "Try again" for a failed cleanup: run it on the next pass."""
        jid = _hold_id(job_id)
        with self.store.tx() as db:
            changed = db.execute("UPDATE cleanup_jobs SET last_attempt_at = NULL WHERE id = ? "
                                 "AND state = 'failed'", (jid,)).rowcount
        row = self._job_row(jid) if changed else None
        if row is not None:
            self.audit.record("cleanup_retry", meeting=row["meeting_id"], phase=row["phase"],
                              job=jid)
        self._wake.set()


def _hold_id(value):
    if not isinstance(value, str) or len(value) != 32 or not value.isalnum():
        raise WorkspaceError("Not found.")
    return value


def _group(rows, key):
    """{row[key]: [rows in their order]}."""
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def _log_key(corrections):
    """A speaker correction log as a hashable key (_speaker_count)."""
    return tuple((c["id"], c["kind"], c["target"], c["value"]) for c in corrections)


def _folders(path):
    """How many folders are in path (0 if it is missing): cheap to compare."""
    try:
        return sum(1 for e in os.scandir(path) if e.is_dir())
    except OSError:
        return 0


def _expected_import_error(exc):
    """A refusal or a bad file, explained to the user; anything else is a bug.
    The decoder's container not starting (#299) is not the file's fault: it
    is logged with a code for IT (_import_incident)."""
    if isinstance(exc, media.SandboxUnavailable):
        return False
    return (isinstance(exc, (media.SourceChanged, media.DecodeError, UnsafePathError,
                             WorkspaceError))
            or (isinstance(exc, ValueError) and "no audio" in str(exc)))


def _imports_removed(count):
    """What the Meetings page says when start-up removed imports cut short
    (_reconcile_imports, #425)."""
    one = count == 1
    return (f"{'An import' if one else f'{count} imports'} that did not finish before "
            f"Unminute last stopped {'was' if one else 'were'} removed; the original "
            f"{'recording was' if one else 'recordings were'} not changed. A microphone "
            "recording, or one from a watched folder, comes back by itself; import any "
            "other recording again if you still need it.")


def _import_incident(log, area, exc):
    """Log an unexpected import failure; returns its code. For the decoder's
    container (#299) the record also says which step failed and its Windows
    error number: no path, name or text (P3)."""
    fields = {}
    if isinstance(exc, media.SandboxUnavailable):
        fields = {"step": exc.step, "winerror": exc.winerror}
    return log.incident(area, exc, **fields)


def _import_message(exc, code=None):
    if isinstance(exc, media.SandboxUnavailable):
        return f"{exc} (code {code})" if code else str(exc)
    if isinstance(exc, (media.SourceChanged, media.DecodeError, UnsafePathError,
                        WorkspaceError)):
        return str(exc)
    if isinstance(exc, ValueError) and "no audio" in str(exc):
        return "That file contains no audio."
    if code:                              # unexpected: the code finds it in the log
        return f"That recording could not be imported (code {code})."
    return "That recording could not be imported (unsupported or damaged file)."
