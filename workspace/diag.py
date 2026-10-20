# workspace/diag.py
# Local, content-free diagnostics (R9, #130; docs/RELEASE_STANDARD.md,
# sections 5 and 6). Each process writes its own small rotating log in the
# workspace's logs\ folder (app.log, worker.log, gpu-worker.log, setup.log).
# There is no telemetry: nothing leaves the computer unless the user copies it.
#
# A record is one JSON line holding only what this module allows. Each field
# has its own rule (FIELDS): fixed words, an incident code, a version, an
# exception type name, a stack of frames, or a number or true/false. Frames
# are reduced to the file within the app, a library or the standard library,
# the line and the function name, so no folder (which can hold the user's
# name), no file name of the user's and no exception message (which can hold
# meeting text) is ever written. A value that does not fit is written as "?";
# a field without a rule takes numbers and true/false only.
# tests/test_diagnostics.py runs the whole lifecycle with marker words in
# titles, transcripts, speaker names, file names and error messages and checks
# that none reach the logs.
#
# Crashes the interpreter cannot report itself (an access violation in a
# native library) are caught by faulthandler, which writes raw text to
# <name>.crash. That file can hold program folders (with the user's name), so
# it is never shared: it is capped (CRASH_MAX), and turned into a record and
# deleted at the next start, when a worker ends, or at a clean close. It counts
# as a crash only when the run that wrote it did not end cleanly: on Windows
# faulthandler also reports native errors that another component recovers
# from, and those are counted, not called crashes.
#
# Console output of the worker and download processes is free text, so it is
# never written either: OutputSummary keeps only the error and warning type
# names it sees, and how often (the user's choice, 17 Oct 2026).

import faulthandler
import io
import json
import math
import os
import re
import secrets
import sys
import sysconfig
import threading
import time
from pathlib import Path

MAX_BYTES = 512 * 1024          # per file
KEEP = 3                        # name.log, name.1.log, name.2.log: at most 1.5 MB a process
CRASH_MAX = 256 * 1024          # a crash file is started afresh past this (limit_crash_file)
CRASH_TAIL = 64 * 1024          # how much of it is read: the last dump is at the end
APP_ROOT = Path(__file__).resolve().parents[1]
WORD = re.compile(r"[A-Za-z0-9_-]{1,40}")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")
FUNC = re.compile(r"<?[A-Za-z_][A-Za-z0-9_]{0,63}>?")
# A frame's file: <frozen ...>, or a Python (or Cython) source file within the
# app, a library or the standard library, at most 8 folders deep.
FRAME_FILE = re.compile(r"(<[A-Za-z0-9_.-]{1,60}>|([A-Za-z0-9_.-]{1,60}/){0,7}"
                        r"[A-Za-z0-9_.-]{1,60}\.pyx?)")
SECRET = re.compile(r"hf_[A-Za-z0-9]{20,}")          # never kept, whatever the field
KINDS = {"start", "stop", "timing", "incident", "crash", "worker_exit", "native_error",
         "output", "integrity",
         # #111 C2b-2: a sealed value that did not open (table, column, count),
         # and a workspace key Windows would not unlock (reason, Windows error)
         "damaged_value", "key_unavailable"}
INTEGRITY_FILE = "integrity.json"   # the last database check (R11): see integrity_state()
# The number and true/false fields the app writes (FIELDS has the text ones).
# Copy diagnostics keeps only these and FIELDS of a record.
NUMBERS = {"line", "col", "count", "exitcode", "hung", "earlier", "truncated",
           "previous_run_unclean", "imports_s", "workspace_s", "page_loaded_s",
           "winerror"}
LOG_NAME = re.compile(r"(app|worker|gpu-worker|setup)(\.[0-9])?")   # a log file's stem
BIG = 10 ** 12                  # no number the app writes is this large
INCIDENT_REPEATS = 3            # the same incident is written this often a run,
INCIDENTS_MAX = 200             # and no more than this many incidents in all
_current = None


def _set(*names):
    return lambda v: v if v in names else "?"


def _pattern(regex):
    return lambda v: v if isinstance(v, str) and regex.fullmatch(v) else "?"


def _frames(value):
    def text(v, regex):
        return v if isinstance(v, str) and regex.fullmatch(v) and not SECRET.search(v) else "?"
    out = []
    for item in list(value)[:40] if isinstance(value, (list, tuple)) else []:
        try:
            file, line, func = item
        except (TypeError, ValueError):
            continue
        out.append([text(file, FRAME_FILE),
                    line if isinstance(line, int) and not isinstance(line, bool)
                    and 0 <= line < BIG else 0,
                    text(func, FUNC)])
    return out


# What each text field may hold. Fields not listed take numbers and true/false.
FIELDS = {
    "code": _pattern(re.compile(r"U-[0-9A-F]{6}")),
    "area": _set("bridge", "processing", "import", "recording_import", "worker",
                 "worker_start", "setup", "uncaught", "thread", "page", "open"),
    "process": _set("app", "worker", "gpu-worker", "setup"),
    "job": _set("transcribe", "speakers", "rerun", "cleanup"),
    "op": _set("load", "transcribe", "diarize", "exit"),
    "stream": _set("stdout", "stderr"),
    "event": _set("error", "rejection"),  # a page error (report_ui_error)
    "result": _set("ok", "failed", "skipped", "continued"),   # a database check (R11)
    # #111 C2b-2: why the workspace key could not be used (store.KeyUnavailable),
    # and where a sealed value did not open (store.CONTENT_COLUMNS, logical names)
    "reason": _set("windows", "key", "protector", "missing"),
    "table": _set("meetings", "reviewed_snapshots", "sources", "segments", "edits",
                  "edit_history", "speaker_runs", "speaker_corrections", "handoff_options",
                  "holds", "recording_sources", "watched_folders", "intake_meta"),
    "column": _set("title", "last_error", "text", "original_path", "words_json", "before",
                   "after", "error", "value", "omitted", "redactions", "reason", "info",
                   "path", "v"),
    "version": _pattern(re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-preview\.[0-9]{1,4})?"
                                   r"(\+source)?|source")),
    "python": _pattern(re.compile(r"[0-9]{1,2}\.[0-9]{1,3}\.[0-9]{1,3}")),
    "error": _pattern(IDENT),            # an exception or warning type name
    "type": _pattern(IDENT),             # a console type name; an incident's cause
    "method": _pattern(IDENT),           # a bridge method's name
    "thread": _pattern(WORD),            # the app's own thread names
    "fatal": _pattern(re.compile(r"[A-Za-z0-9_]{1,64}")),
    "frames": _frames,
}


def _clean(key, value):
    """A field's value as it may be written, or "?" if it is not allowed."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) < BIG else "?"
    if isinstance(value, float):
        return round(value, 3) if math.isfinite(value) and abs(value) < BIG else "?"
    if isinstance(value, str) and SECRET.search(value):
        return "?"
    rule = FIELDS.get(key)
    return rule(value) if rule else "?"


def _library_roots(prefix=None, stdlib=None):
    """(folder, label) for code outside the app. The package runs the
    embeddable Python: the standard library is python3XX.zip beside it, and
    the libraries are in its lib folder (os.__file__ is no guide there: os is
    built in, and its made-up path points at lib). A development install has
    the standard library folder that sysconfig names, and site-packages
    (found by name in where(), as is the GPU component's site folder)."""
    prefix = Path(prefix or sys.prefix).resolve()
    zipped = prefix / f"python{sys.version_info[0]}{sys.version_info[1]}.zip"
    if zipped.is_file():
        roots = [(zipped, "stdlib/")]
        if (prefix / "lib").is_dir():
            roots.append((prefix / "lib", ""))
        return roots
    return [(Path(stdlib or sysconfig.get_paths()["stdlib"]).resolve(), "stdlib/")]


_ROOTS = _library_roots()


def where(filename):
    """A source file as written in a record: within the app (workspace/app.py),
    within a library (torch/nn/modules/module.py), within the standard library
    (stdlib/threading.py), or <other>. Never a folder outside those."""
    if not filename:
        return "<other>"
    if filename.startswith("<"):
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename.strip("<>"))[:60]
        return f"<{name}>" if name else "<other>"         # <frozen_importlib._bootstrap>
    if not os.path.isabs(filename):
        # The embeddable Python's zipped standard library names its frames
        # relatively (json\\decoder.py); elsewhere a relative name says nothing.
        rel = "stdlib/" + filename.replace("\\", "/")
        zipped = bool(_ROOTS) and _ROOTS[0][0].suffix.lower() == ".zip"
        return rel if zipped and FRAME_FILE.fullmatch(rel) else "<other>"
    try:
        path = Path(filename).resolve()
    except (OSError, ValueError):
        return "<other>"
    parts = list(path.parts)
    lower = [p.lower() for p in parts]
    rel = None
    for i in range(len(parts) - 1, -1, -1):
        if lower[i] == "site-packages" or (lower[i] == "site" and i >= 2
                                            and lower[i - 2] == "gpu"):
            rel = "/".join(parts[i + 1:])              # a library (or the GPU component)
            break
    if rel is None:
        for base, prefix in ((APP_ROOT, ""), *_ROOTS):
            try:
                rel = prefix + path.relative_to(base).as_posix()
                break
            except ValueError:
                continue
    if rel is None or not FRAME_FILE.fullmatch(rel):
        return "<other>"
    return rel


def frames(tb, limit=12):
    """[[file, line, function], ...] for a traceback, innermost last."""
    entries = []
    while tb is not None:
        entries.append(tb)
        tb = tb.tb_next
    out = []
    for entry in entries[-limit:]:                 # only these are reduced
        code = entry.tb_frame.f_code
        out.append([where(code.co_filename), entry.tb_lineno,
                    code.co_name if FUNC.fullmatch(code.co_name or "") else "?"])
    return out


def _type_name(exc):
    name = type(exc).__qualname__
    return name if IDENT.fullmatch(name) else "?"


def new_code():
    """A short incident code to show the user and find in the log."""
    return "U-" + secrets.token_hex(3).upper()


class Log:
    """One process's log: logs\\<name>.log, rotated at MAX_BYTES, KEEP files.
    The logs folder is made inside an existing workspace only."""

    def __init__(self, folder, name):
        if not WORD.fullmatch(name):
            raise ValueError("A log name is a short code.")
        self.folder, self.name = Path(folder), name
        self._lock = threading.Lock()
        self.path = self.folder / f"{name}.log"
        self.crash_path = self.folder / f"{name}.crash"
        self.running_path = self.folder / f"{name}.running"
        self._crash_file = None
        self._incidents = {}            # incident -> (its last code, times written)
        self._incidents_written = 0

    # -- writing ----------------------------------------------------------------

    def write(self, kind, **fields):
        """Append one record. Never raises: diagnostics must not break the app."""
        record = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "kind": kind if kind in KINDS else "?"}
        for key, value in fields.items():
            if WORD.fullmatch(key):
                record[key] = _clean(key, value)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n"
        with self._lock:
            try:
                self.folder.mkdir(exist_ok=True)       # never recreates a removed workspace
                size = self.path.stat().st_size if self.path.exists() else 0
                if size + len(line) > MAX_BYTES and not self._rotate() \
                        and size + len(line) > 2 * MAX_BYTES:
                    return                             # cannot rotate: stop growing
                with open(self.path, "a", encoding="ascii") as f:
                    f.write(line)
            except OSError:
                pass

    def _rotate(self):
        """name.log is moved aside first; only then do the older files move
        down (the oldest is replaced) and it becomes name.1.log. If the first
        move fails (a viewer holding the file open), nothing changes, and the
        current file grows until it can rotate; if a later one fails, it is
        put back."""
        aside = self.folder / f"{self.name}.rotating"
        try:
            self.path.replace(aside)
        except OSError:
            return False
        try:
            for i in range(KEEP - 1, 1, -1):
                newer = self.folder / f"{self.name}.{i - 1}.log"
                if newer.exists():
                    newer.replace(self.folder / f"{self.name}.{i}.log")
            aside.replace(self.folder / f"{self.name}.1.log")
        except OSError:
            try:
                aside.replace(self.path)
            except OSError:
                pass
            return False
        return True

    def incident(self, area, exc, **fields):
        """Record an unexpected error (type and frames only) and return its
        code, for the message the user sees. The same incident (area, type,
        frames and fields) is written INCIDENT_REPEATS times a run and then
        keeps its last code, and at most INCIDENTS_MAX are written a run: a
        failure that repeats (a page calling a broken method again and again)
        cannot push older records, such as a crash, out of the log. An error
        raised from another (raise ... from cause) also records the cause's
        type name as "type", never its text: a fixed-wording error such as the
        speaker model's ModelLoadError still says what failed underneath."""
        error, found = _type_name(exc), frames(exc.__traceback__)
        if exc.__cause__ is not None and "type" not in fields:
            fields["type"] = _type_name(exc.__cause__)
        key = (area, error, json.dumps(found), json.dumps(fields, sort_keys=True, default=str))
        with self._lock:
            code, times = self._incidents.get(key, (None, 0))
            if times >= INCIDENT_REPEATS or self._incidents_written >= INCIDENTS_MAX:
                return code or new_code()
            code = new_code()
            self._incidents[key] = (code, times + 1)
            self._incidents_written += 1
        self.write("incident", code=code, area=area, error=error, frames=found, **fields)
        return code

    # -- this run --------------------------------------------------------------

    def open_run(self, others=()):
        """Start of a run: a crash left by an unclean previous run becomes a
        record, crash files other processes left (others: their log names) are
        counted, and this run is marked running. Returns whether the previous
        run ended uncleanly."""
        unclean = self.running_path.exists()
        if self.collect_crash(unclean) is None and unclean:
            # Ended without closing and without a native crash (ended in Task
            # Manager's Details, power lost): still a crash, counted once.
            self.write("crash", code=new_code(), process=self.name, fatal="did_not_close")
        for name in others:
            self.collect_crash(False, crash_path=self.folder / f"{name}.crash", process=name)
        try:
            self.folder.mkdir(exist_ok=True)
            # Which process is running (--health asks Windows whether it still
            # is, from any session): its id and start time, no names.
            self.running_path.write_text(json.dumps(
                {"pid": os.getpid(), "created": process_created()}), encoding="ascii")
        except OSError:
            pass
        return unclean

    def close_run(self):
        """A clean end of the run: a crash file from this run held only native
        errors that something recovered from."""
        self.stop_crash_capture()
        try:
            self.running_path.unlink()
        except OSError:
            pass
        self.collect_crash(False)

    def collect_crash(self, crashed, crash_path=None, process=None):
        """Read (its last CRASH_TAIL) and delete a crash file: this log's own,
        or crash_path, written by another process such as a worker. If the run
        that wrote it crashed, its last dump becomes a "crash" record here;
        otherwise its dumps are counted as native errors that were recovered
        from. Returns the crash's code, or None."""
        path = Path(crash_path) if crash_path else self.crash_path
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - CRASH_TAIL))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        try:
            path.unlink()
        except OSError:
            pass
        if not text.strip():
            return None
        fatal, found, dumps = parse_faulthandler(text)
        process = process or self.name
        if crashed:
            code = new_code()
            self.write("crash", code=code, process=process, fatal=fatal, frames=found,
                       earlier=dumps - 1, truncated=size > CRASH_TAIL)
            return code
        self.write("native_error", process=process, fatal=fatal, count=dumps,
                   truncated=size > CRASH_TAIL)
        return None

    def capture_crashes(self):
        """faulthandler writes fatal errors of this process to <name>.crash."""
        self.stop_crash_capture()
        try:
            self.folder.mkdir(exist_ok=True)
            self._crash_file = open(self.crash_path, "w", encoding="utf-8")
            faulthandler.enable(file=self._crash_file, all_threads=True)
        except (OSError, RuntimeError, ValueError):
            self._crash_file = None

    def limit_crash_file(self):
        """Called now and then (the app's watch thread): a crash file grown past
        CRASH_MAX with native errors that were recovered from is counted and
        started afresh, so it stays small."""
        try:
            if self._crash_file is None or self.crash_path.stat().st_size <= CRASH_MAX:
                return
        except OSError:
            return
        self.stop_crash_capture()
        self.collect_crash(False)
        self.capture_crashes()

    def stop_crash_capture(self):
        if self._crash_file is not None:
            try:
                faulthandler.disable()
                self._crash_file.close()
            except (OSError, ValueError):
                pass
            self._crash_file = None


FATAL = re.compile(r"(Fatal Python error: [A-Za-z ]{1,60}|Windows fatal exception: "
                   r"(?:code 0x[0-9a-fA-F]{8}|[a-z ]{1,40}))")
FAULT_FRAME = re.compile(r'File "(.*)", line (\d+) in (\S+)')


def parse_faulthandler(text):
    """(fatal error code, frames innermost last, number of dumps) from
    faulthandler's text: the last dump only (earlier ones were native errors
    the process went on from), and of it the current thread's frames."""
    starts = [m.start() for m in FATAL.finditer(text)]
    last = text[starts[-1]:] if starts else text
    m = FATAL.search(last)
    fatal = re.sub(r"[^A-Za-z0-9_]+", "_", m.group(1)).strip("_")[:64] if m else "fatal"
    block = last
    at = last.find("Current thread")
    if at >= 0:
        block = last[at:]
        end = re.search(r"\n\s*\n|\nThread 0x", block)
        block = block[:end.start()] if end else block
    found = [[where(path), int(line), func if FUNC.fullmatch(func) else "?"]
             for path, line, func in FAULT_FRAME.findall(block)[:40]]
    found.reverse()                                # faulthandler lists the newest first
    return fatal, found, max(1, len(starts))


class OutputSummary(io.TextIOBase):
    """A stand-in for a console stream that keeps no text: it counts the
    error and warning type names in the two places Python writes them, an
    exception's last line ("ImportError: ...", "torch.cuda.OutOfMemoryError:
    ...") and a warning ("file.py:12: UserWarning: ..."), and records each
    name's count when it first appears and at each power of two. A name
    anywhere else on a line (text that happens to end in "Error") is not
    counted."""

    EXCEPTION = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]{0,60}\.){0,8}"
                           r"((?:[A-Za-z_][A-Za-z0-9_]{0,60})?(?:Error|Exception|Warning))(?::|$)")
    WARNING = re.compile(r":\d{1,7}: ((?:[A-Za-z_][A-Za-z0-9_]{0,60})?Warning): ")

    def __init__(self, log, stream):
        super().__init__()
        self.log, self.stream = log, stream
        self.counts = {}
        self._partial = ""
        self._lock = threading.Lock()

    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"

    def writable(self):
        return True

    def _name(self, line):
        m = self.EXCEPTION.match(line.strip()) or self.WARNING.search(line)
        # Only the name of an exception or warning class loaded in this
        # process: a word in the text (a folder, a file name) never is one.
        return m.group(1) if m and _loaded_exception(m.group(1)) else None

    @property
    def buffer(self):
        return _DISCARD                              # bytes written to .buffer are dropped

    def write(self, text):
        if not isinstance(text, str):
            return 0
        with self._lock:
            lines = (self._partial + text).split("\n")
            self._partial = lines.pop()[-2000:]
            seen = [name for line in lines if (name := self._name(line))]
        for name in seen:
            n = self.counts[name] = self.counts.get(name, 0) + 1
            if n & (n - 1) == 0:                        # 1, 2, 4, 8, ...
                self.log.write("output", stream=self.stream, type=name, count=n)
        return len(text)

    def isatty(self):
        return False

    def fileno(self):
        raise io.UnsupportedOperation("no console")


class _Discard:
    def write(self, data):
        return len(data)

    def flush(self):
        pass


_DISCARD = _Discard()
_EXCEPTIONS = {"names": set(), "at": 0.0}


def _loaded_exception(name):
    """Is name the name of an exception or warning class loaded in this
    process? The set is worked out again (at most once a second) when a name
    is not in it yet."""
    if name in _EXCEPTIONS["names"]:
        return True
    now = time.monotonic()
    if now - _EXCEPTIONS["at"] < 1.0:
        return False
    _EXCEPTIONS["at"] = now
    names, seen, stack = set(), set(), [BaseException]
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        names.add(cls.__name__)
        try:
            stack.extend(cls.__subclasses__())
        except TypeError:
            pass
    _EXCEPTIONS["names"] = names
    return name in names


def quiet_crashes():
    """A crash ends this process at once: no Windows Error Reporting dialog
    holding it open (and, to confirm on a managed device, no WER dump)."""
    if os.name == "nt":
        try:
            import ctypes
            # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
        except (AttributeError, OSError):
            pass


def summarise_output(log):
    """Console output of this process (no console: pythonw, a worker) is
    summarised into log, never kept as text."""
    if sys.stdout is None or sys.stderr is None or not sys.stderr.isatty():
        sys.stdout = OutputSummary(log, "stdout")
        sys.stderr = OutputSummary(log, "stderr")


# -- the process's log and hooks -----------------------------------------------------

def current():
    return _current


def use(log):
    """Make log this process's log: unexpected exceptions on any thread are
    recorded (then handled as before), and fatal native errors are written to
    its crash file."""
    global _current
    _current = log
    log.capture_crashes()
    previous, previous_thread = sys.excepthook, threading.excepthook

    def excepthook(kind, exc, tb):
        if exc is not None:
            log.incident("uncaught", exc.with_traceback(tb))
        previous(kind, exc, tb)

    def thread_excepthook(args):
        if args.exc_value is not None:
            thread = args.thread.name if args.thread is not None else None
            log.incident("thread", args.exc_value, thread=thread)
        previous_thread(args)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


def install(log, others=()):
    """The app's start: what the previous run left is collected, this run is
    marked running, and log becomes the process's log (use). Returns whether
    the previous run ended uncleanly (R11 can check the database then)."""
    unclean = log.open_run(others)
    use(log)
    return unclean


def set_current(log):
    """The log that incident() writes to, without installing any hook (the
    bridge in tests and tools; the app uses install())."""
    global _current
    _current = log


def incident(area, exc, **fields):
    """Record exc in this process's log, if it has one; returns a code to show."""
    log = _current
    if log is None:
        return new_code()
    return log.incident(area, exc, **fields)


def shareable_records(folder, limit=60):
    """The newest records of each log the app writes in folder, oldest first
    (undated ones before all others), passed through the field rules again
    for Copy diagnostics: only FIELDS and NUMBERS are kept, each to its rule.
    That keeps a record to the shapes the app writes; it cannot tell a word
    the app wrote from the same-shaped word written by another program that
    can already write to the user's logs folder. Never the raw crash files,
    and no more of a file than a log can grow to."""
    out = []
    for path in files(folder):
        if not LOG_NAME.fullmatch(path.stem):
            continue                            # not a log the app writes
        try:
            with open(path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - 2 * MAX_BYTES))   # write() never grows a log past this
                lines = f.read().decode("ascii", errors="replace").splitlines()
        except OSError:
            continue
        if size > 2 * MAX_BYTES:
            lines = lines[1:]                   # started part way through a line
        for line in lines[-limit:]:
            try:
                raw = json.loads(line)
            except (ValueError, RecursionError):  # RecursionError: nested too deeply
                continue
            if not isinstance(raw, dict):
                continue
            t = raw.get("t")
            record = {"t": t if isinstance(t, str) and re.fullmatch(
                          r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z", t) else "?",
                      "kind": raw.get("kind") if raw.get("kind") in KINDS else "?",
                      "log": path.stem}
            for key, value in raw.items():
                if key in FIELDS or key in NUMBERS:
                    record[key] = _clean(key, value)
            out.append(record)
    out.sort(key=lambda r: (r["t"] != "?", r["t"]))
    return out[-limit:]


def integrity_state(folder):
    """The last database check (R11), from <logs>\\integrity.json:
    {"result": "ok" | "failed" | "skipped", "code": "U-..." or None, "t":
    time}, or None if none was recorded (or the file cannot be read)."""
    try:
        with open(Path(folder) / INTEGRITY_FILE, "rb") as f:
            raw = json.loads(f.read(500).decode("ascii"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or raw.get("result") not in ("ok", "failed", "skipped"):
        return None
    code, t = raw.get("code"), raw.get("t")
    return {"result": raw["result"],
            "code": code if FIELDS["code"](code) == code else None,
            "t": t if isinstance(t, str) and re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z", t) else None}


def record_integrity(folder, result, code=None):
    """Keep the result of a database check for the next start, --status and
    --health: written in full or not at all. Never raises."""
    folder = Path(folder)
    state = {"result": result, "code": code,
             "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = folder / (INTEGRITY_FILE + ".tmp")
    try:
        folder.mkdir(exist_ok=True)
        with open(tmp, "w", encoding="ascii") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(folder / INTEGRITY_FILE)
    except OSError:
        pass


def recent_crashes(folder, days=7, now=None):
    """Crashes (including runs that ended without closing) and worker or
    download processes that ended by themselves, in the last days. A crash
    and its process's exit share a code and count once; a time more than a
    day ahead (the clock was wrong) does not count."""
    now = now or time.time()
    cutoff, ahead = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + d))
                     for d in (-days * 86400, 86400))
    seen = set()
    for i, r in enumerate(shareable_records(folder, limit=100000)):
        if r["t"] == "?" or not cutoff <= r["t"] <= ahead:
            continue
        if r["kind"] == "crash" or (r["kind"] == "worker_exit" and not r.get("hung")):
            code = r.get("code")
            seen.add(code if isinstance(code, str) and code != "?" else i)
    return len(seen)


def files(folder):
    """The log files in folder (never the raw .crash files, which can hold
    program folders): what Copy diagnostics may read, and what tests check."""
    try:
        return sorted(p for p in Path(folder).iterdir() if p.is_file() and p.suffix == ".log")
    except OSError:                             # missing, or not readable
        return []


def process_created(pid=None):
    """When a running process started (a Windows FILETIME), or None if it has
    ended or cannot be asked. pid None: this process."""
    import ctypes
    from ctypes import wintypes
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    if pid is None:
        handle = kernel32.GetCurrentProcess()
    else:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
    try:
        status = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(status)) \
                or status.value != 259:                    # STILL_ACTIVE
            return None
        times = [wintypes.FILETIME() for _ in range(4)]    # created, exited, kernel, user
        if not kernel32.GetProcessTimes(wintypes.HANDLE(handle),
                                        *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        if pid is not None:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


def run_owner_alive(marker):
    """Is the process a run marker names (open_run) still running? None if
    the marker names none (written before R9b, or unreadable)."""
    try:
        with open(marker, "rb") as f:
            owner = json.loads(f.read(200).decode("ascii"))
        pid, created = owner["pid"], owner["created"]
    except (OSError, ValueError, RecursionError, TypeError, KeyError):
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(created, int):
        return None
    return process_created(pid) == created
