# tools/perf_check.py
# Release-time performance evidence (docs/RELEASE_STANDARD.md, section 2; #124).
#
# Opens the real Unminute window (WebView2) on a throwaway workspace holding
# synthetic meetings made by fake engines, puts it in each state in turn and
# measures the whole process tree from outside: CPU time (kernel plus user, via
# a Job Object so processes that exit mid-window still count, cross-checked by
# summing the live tree) and private bytes. Working sets are reported for
# reference only and never used for budgets: summing them counts shared pages
# once per process.
#
# Run "python tools/perf_check.py --help" for the states, the output and the
# exit codes. The window is visible for the whole run; leave the machine idle,
# on mains power, on the Balanced power plan. The worker's peak memory and
# speed need real models and come from tools/packaged_check.py.

import argparse
import hashlib
import json
import math
import os
import platform
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = ROOT / "docs" / "perf-budgets.json"
STATES = ("meetings", "settings", "minimised")
MEETINGS = 3                      # synthetic meetings in the measured workspace
PREFIX = "PERF "                  # protocol lines between parent and child
WORKER_WAIT_S = 240               # the worker unloads after 120 s idle (worker.py)
READY_TIMEOUT_S = 600             # seeding (up to 300 s) plus the page starting
STEP_TIMEOUT_S = 90               # the window reaching a state
LEAK_BUDGET_MINUTES = 30          # the leak budget is growth over this long
HELP = """
States, in order: meetings (the Meetings view, 3 synthetic meetings), settings
(the Settings view) and minimised. Before them it times start-up to
interactive (imports, then window creation until the meeting list has
rendered; interpreter start and opening the workspace are left out) and waits
for the worker to unload. With --leak-minutes it then holds the Meetings view
that long, once, in the last run, and reports the growth. The leak budget is
for 30 minutes, so a shorter hold is reported but not compared: short-window
noise cannot be scaled up.

Output: a JSON report with the machine, the app and WebView2 versions, every
run's figures, the medians, anything over budget or not measured, and budgets
proposed from these figures (plus 20%).

Exit codes: 0 within budget (or no budgets file yet); 1 a figure is over its
budget (a worker that did not unload counts as over), or a budgeted figure was
not measured; 2 the window could not be measured (it did not start, did not
respond, or left processes outside the measuring job).

Release run (docs/RELEASING.md): 3 runs, 60 s settle, 300 s windows and
--leak-minutes 30, about 90 minutes. --quick is 1 run, 20 s settle and 60 s
windows, for before-and-after checks during development.
"""


# -- comparing with budgets (pure; tested in tests/test_perf_check.py) ---------------

def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def median_report(runs):
    """Median of each numeric figure across runs, per state. A figure missing
    from some runs (for example a worker that did not exit) uses the others."""
    out = {}
    for state in dict.fromkeys(s for r in runs for s in r):
        keys = dict.fromkeys(k for r in runs for k in (r.get(state) or {}))
        figures = {}
        for key in keys:
            values = [r[state][key] for r in runs if _number((r.get(state) or {}).get(key))]
            if values:
                figures[key] = round(statistics.median(values), 3)
        out[state] = figures
    return out


def compare(figures, budgets, skip=()):
    """(over, not_measured): [(state, metric, value, limit)] for every figure
    over its budget, and [(state, metric)] for every set budget with no figure.
    A budget of null is not set yet; states in skip were not asked for."""
    over, missing = [], []
    for state, limits in budgets.items():
        if state in skip:
            continue
        for metric, limit in limits.items():
            if not _number(limit):
                continue
            value = figures.get(state, {}).get(metric)
            if not _number(value):
                missing.append((state, metric))
            elif value > limit:
                over.append((state, metric, value, limit))
    return over, missing


BUDGETED = ("cpu_percent_of_one_core", "private_mb", "startup_s")


def worst_worker(runs):
    """The slowest worker unload across runs (a run whose worker never unloaded
    is recorded at the wait limit), or None when no run had a worker. Unloading
    is pass or fail, so one failed run must fail the whole check."""
    values = [r["worker"]["exit_after_last_use_s"] for r in runs
              if _number((r.get("worker") or {}).get("exit_after_last_use_s"))]
    return max(values) if values else None


def propose(figures, margin=0.2):
    """Budgets from a baseline: each budgeted figure plus the margin."""
    out = {}
    for state, metrics in figures.items():
        limits = {}
        for metric in BUDGETED:
            value = metrics.get(metric)
            if not _number(value):
                continue
            if metric == "private_mb":
                limits[metric] = math.ceil(value * (1 + margin))
            else:          # rounded up, never closer than 0.05 to the baseline
                limit = max(value * (1 + margin), value + 0.05)
                limits[metric] = math.ceil(round(limit * 100, 6)) / 100
        if limits:
            out[state] = limits
    return out


# -- measuring from outside (Windows only) --------------------------------------------

def _win():
    import ctypes
    from ctypes import wintypes
    return ctypes, wintypes


def processes():
    """{pid: (parent pid, exe name)} for every process, from a Toolhelp snapshot."""
    ctypes, wintypes = _win()

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)                 # TH32CS_SNAPPROCESS
    out = {}
    try:
        e = Entry()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def tree(root_pid):
    """root_pid and all its descendants: [(pid, exe name)]."""
    table = processes()
    kids = {}
    for pid, (ppid, _) in table.items():
        kids.setdefault(ppid, []).append(pid)
    seen, todo = [], [root_pid]
    while todo:
        pid = todo.pop()
        if pid in table and pid not in seen:
            seen.append(pid)
            todo += kids.get(pid, [])
    return [(pid, table[pid][1]) for pid in seen]


def sample(pid):
    """CPU seconds and memory of one process, or None if it is gone."""
    ctypes, wintypes = _win()

    class Counters(ctypes.Structure):                          # PROCESS_MEMORY_COUNTERS_EX2
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage", "PrivateWorkingSetSize")
        ] + [("SharedCommitUsage", ctypes.c_uint64)]
    k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000 | 0x0010, False, pid)           # QUERY_LIMITED | VM_READ
    if not h:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
            return None
        secs = sum(((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7 for t in times[2:])
        c = Counters()
        for size in (ctypes.sizeof(Counters), ctypes.sizeof(Counters) - 16):  # EX2, then EX
            c.cb = size
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(c), size):
                break
        else:
            return None              # no memory figures: retried, then unmeasurable
        return {"cpu_s": secs, "private": c.PrivateUsage, "working_set": c.WorkingSetSize,
                "private_ws": c.PrivateWorkingSetSize if c.cb == ctypes.sizeof(Counters)
                else None}
    finally:
        k32.CloseHandle(h)


class Job:
    """A Job Object holding the child, for CPU accounting and kill-on-close."""

    def __init__(self, process):
        ctypes, wintypes = _win()
        self.ctypes, self.k32 = ctypes, ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.handle = self.k32.CreateJobObjectW(None, None)

        class Limits(ctypes.Structure):          # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
            _fields_ = [("a", ctypes.c_int64 * 2), ("LimitFlags", wintypes.DWORD),
                        ("b", ctypes.c_size_t * 2), ("c", wintypes.DWORD),
                        ("d", ctypes.c_size_t), ("e", wintypes.DWORD), ("f", wintypes.DWORD),
                        ("io", ctypes.c_uint64 * 6), ("g", ctypes.c_size_t * 4)]
        limits = Limits()
        limits.LimitFlags = 0x2000                              # KILL_ON_JOB_CLOSE
        if not self.k32.SetInformationJobObject(self.handle, 9, ctypes.byref(limits),
                                                ctypes.sizeof(limits)):
            raise OSError(ctypes.get_last_error(), "could not set kill-on-close on the job")
        if not self.k32.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError(ctypes.get_last_error(), "could not put the window in a job")

    def cpu_seconds(self):
        ctypes = self.ctypes

        class Info(ctypes.Structure):            # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
            _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                        ("ThisPeriodTotalUserTime", ctypes.c_int64),
                        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                        ("TotalPageFaultCount", ctypes.c_uint32),
                        ("TotalProcesses", ctypes.c_uint32), ("ActiveProcesses", ctypes.c_uint32),
                        ("TotalTerminatedProcesses", ctypes.c_uint32)]
        info = Info()
        self.k32.QueryInformationJobObject(self.handle, 1, ctypes.byref(info),
                                           ctypes.sizeof(info), None)
        return (info.TotalUserTime + info.TotalKernelTime) / 1e7, info.ActiveProcesses

    def close(self):
        self.k32.CloseHandle(self.handle)                       # kills anything left


class Unmeasurable(Exception):
    """The window cannot be measured as the standard requires (exit code 2)."""


def snapshot(pid, tries=3):
    """The tree and a sample of every process in it. A process that exits
    between the two is retried; one that cannot be opened is an error, because
    leaving it out would under-report memory."""
    for _ in range(tries):
        procs = tree(pid)
        samples = {p: sample(p) for p, _ in procs}
        if all(samples.values()):
            return procs, samples
        time.sleep(0.2)
    missing = [name for p, name in procs if not samples[p]]
    raise Unmeasurable(f"could not read {len(missing)} process(es): {', '.join(missing)}")


def measure(job, pid, settle, window):
    """Wait settle seconds, then measure CPU over window seconds and memory at its end."""
    time.sleep(settle)
    job0, _ = job.cpu_seconds()
    _, before = snapshot(pid)
    t0 = time.monotonic()
    time.sleep(window)
    job1, active = job.cpu_seconds()
    procs, after = snapshot(pid)
    elapsed = time.monotonic() - t0
    if active < len(procs):
        raise Unmeasurable(f"{len(procs) - active} process(es) are outside the measuring job")
    tree_cpu = sum(a["cpu_s"] - (before.get(p) or {}).get("cpu_s", 0) for p, a in after.items())
    live = list(after.values())
    names = sorted(name for _, name in procs)
    mb = lambda key: round(sum(a.get(key) or 0 for a in live) / 2**20)  # noqa: E731
    return {
        "cpu_percent_of_one_core": round((job1 - job0) / elapsed * 100, 3),
        "cpu_percent_tree": round(tree_cpu / elapsed * 100, 3),
        "private_mb": mb("private"),
        "private_working_set_mb": mb("private_ws") if all(a.get("private_ws") is not None
                                                          for a in live) else None,
        "working_set_mb_reference_only": mb("working_set"),
        "processes": len(procs), "processes_in_job": active,
        "python_processes": sum(1 for n in names if n.startswith("python")),
        "process_names": names,
    }


# -- the parent: runs the child once per run and measures it ---------------------------

def environment(args):
    import winreg
    env = {"when": time.strftime("%Y-%m-%d %H:%M"), "python": platform.python_version(),
           "windows": platform.version(), "logical_processors": os.cpu_count()}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            env["cpu"] = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except OSError:
        pass
    ctypes, wintypes = _win()

    class Mem(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64)] + [(f"x{i}", ctypes.c_uint64)
                                                          for i in range(6)]
    m = Mem()
    m.dwLength = ctypes.sizeof(m)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        env["memory_gb"] = round(m.ullTotalPhys / 2**30, 1)

    class Power(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte),
                    ("a", wintypes.DWORD), ("b", wintypes.DWORD)]
    p = Power()
    if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p)):
        env["on_mains_power"] = p.ACLineStatus == 1
    r = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True)
    env["power_plan"] = r.stdout.strip().split("(")[-1].rstrip(")") if r.stdout else None
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate"
                       r"\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
                      (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate"
                       r"\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")):
        try:
            with winreg.OpenKey(hive, key) as k:
                env["webview2"] = winreg.QueryValueEx(k, "pv")[0]
                break
        except OSError:
            continue
    app = Path(args.app) if args.app else ROOT
    version = app.parent / "version.json"
    if version.exists():
        env["app"] = json.loads(version.read_text(encoding="utf-8"))
    else:
        r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True)
        env["app"] = {"version": "source", "commit": r.stdout.strip() or None}
    if args.zip:
        env["package_sha256"] = hashlib.sha256(Path(args.zip).read_bytes()).hexdigest()
    return env


def child_command(args, workdir):
    """The base interpreter, not a venv's launcher: the launcher starts the real
    interpreter as a second process, which could escape the job if it started
    before the job is assigned. __PYVENV_LAUNCHER__ keeps the venv's packages,
    as multiprocessing does."""
    env = dict(os.environ)
    exe = sys.executable
    base = getattr(sys, "_base_executable", exe)
    if os.path.normcase(base) != os.path.normcase(exe):
        env["__PYVENV_LAUNCHER__"] = exe
        exe = base
    # -B: never write bytecode into the package being measured.
    command = [exe, "-B", str(Path(__file__).resolve()), "--child", "--workdir", str(workdir)]
    if args.app:
        command += ["--app", args.app]
    return command, env


def one_run(args, settle, window, leak_minutes=0):
    workdir = Path(tempfile.mkdtemp(prefix="unminute-perf-"))
    command, env = child_command(args, workdir)
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                            text=True, encoding="utf-8", bufsize=1)
    job = None
    lines = queue.Queue()

    def pump():                      # read in a thread, so every wait has a deadline
        for line in proc.stdout:
            if line.startswith(PREFIX):
                lines.put(json.loads(line[len(PREFIX):]))
        lines.put(None)

    def read(timeout):
        try:
            msg = lines.get(timeout=timeout)
        except queue.Empty:
            raise Unmeasurable(f"the window did not answer within {timeout} s") from None
        if msg is None:
            raise Unmeasurable(f"the window process ended (exit {proc.wait()})")
        if msg.get("error"):
            raise Unmeasurable(f"the window reported: {msg['error']}")
        return msg

    def say(command):
        proc.stdin.write(command + "\n")
        proc.stdin.flush()

    result = {}
    try:
        try:
            job = Job(proc)
        except OSError as exc:
            raise Unmeasurable(f"could not set up the measuring job: {exc}") from exc
        threading.Thread(target=pump, daemon=True).start()
        ready = read(READY_TIMEOUT_S)
        result["startup"] = {k: ready[k] for k in ("imports_s", "window_to_interactive_s",
                                                   "startup_s")}
        # Idle means after the worker has unloaded. Waiting for it also measures
        # the standard's "gone within its idle time plus 5 s" (section 3). A
        # worker that does not unload is recorded at the wait limit, so it fails
        # its budget rather than dropping out of the figures.
        worker = ready.get("worker_pid")
        if worker:
            print("  waiting for the worker to unload", flush=True)
            deadline = time.time() + WORKER_WAIT_S
            while time.time() < deadline and any(p == worker for p, _ in tree(proc.pid)):
                time.sleep(0.5)
            gone = not any(p == worker for p, _ in tree(proc.pid))
            result["worker"] = {"exited": gone, "exit_after_last_use_s": round(
                time.time() - ready["worker_last_used"], 1) if gone else WORKER_WAIT_S}
        for state in STATES:
            say(state)
            if read(STEP_TIMEOUT_S).get("state") != state:
                raise Unmeasurable(f"the window did not reach {state}")
            result[state] = measure(job, proc.pid, settle, window)
            print(f"  {state}: {result[state]['cpu_percent_of_one_core']}% of one core, "
                  f"{result[state]['private_mb']} MB private", flush=True)
        if leak_minutes:
            say("meetings")
            read(STEP_TIMEOUT_S)
            print(f"  holding the Meetings view for {leak_minutes} minutes (leak check)",
                  flush=True)
            time.sleep(settle)
            _, first = snapshot(proc.pid)
            time.sleep(leak_minutes * 60)
            _, last = snapshot(proc.pid)
            growth = (sum(s["private"] or 0 for s in last.values())
                      - sum(s["private"] or 0 for s in first.values())) / 2**20
            result["leak"] = {"minutes": leak_minutes, "private_growth_mb_raw": round(growth, 1)}
            if leak_minutes >= LEAK_BUDGET_MINUTES:      # the budget is per 30 minutes
                result["leak"]["private_growth_mb"] = round(growth, 1)
        say("quit")
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
        if job is not None:
            job.close()                  # kills anything left, WebView2 included
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            pass
        for _ in range(10):              # files can stay locked for a moment
            shutil.rmtree(workdir, ignore_errors=True)
            if not workdir.exists():
                break
            time.sleep(0.5)
    return result


def parent(args):
    settle, window, runs = (20, 60, 1) if args.quick else (args.settle, args.window, args.runs)
    report = {"tool": "tools/perf_check.py", "mode": "quick" if args.quick else "release",
              "settle_s": settle, "window_s": window, "runs": runs,
              "leak_minutes": args.leak_minutes, "environment": environment(args)}
    results = []
    try:
        for n in range(runs):
            print(f"run {n + 1} of {runs}", flush=True)
            last = n == runs - 1
            results.append(one_run(args, settle, window, args.leak_minutes if last else 0))
    except Exception as exc:          # anything that stops a run means "not measured"
        report["each_run"], report["error"] = results, str(exc)
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"COULD NOT MEASURE: {exc}")
        return 2
    report["each_run"] = results
    figures = median_report(results)
    worst = worst_worker(results)       # pass or fail, not a noisy figure: the worst run counts
    if worst is not None:
        figures["worker"] = {"exit_after_last_use_s": worst}
    report["median"] = figures
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8")) if BUDGETS.exists() else {}
    skip = () if args.leak_minutes >= LEAK_BUDGET_MINUTES else ("leak",)
    over, missing = compare(figures, budgets.get("budgets", {}), skip)
    report["over_budget"] = [{"state": s, "metric": m, "value": v, "budget": b}
                             for s, m, v, b in over]
    report["not_measured"] = [{"state": s, "metric": m} for s, m in missing]
    report["not_asked_for"] = list(skip)
    report["proposed_budgets_plus_20_percent"] = propose(figures)
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["median"], indent=2))
    for s, m, v, b in over:
        print(f"OVER BUDGET: {s} {m} {v} > {b}")
    for s, m in missing:
        print(f"NOT MEASURED: {s} {m}")
    if skip:
        print(f"Not compared: the leak check needs --leak-minutes {LEAK_BUDGET_MINUTES} or more.")
    if not budgets:
        print("NO BUDGETS YET")
    elif not over and not missing:
        print("WITHIN BUDGET")
    return 1 if over or missing else 0


# -- the child: the real window on synthetic meetings ----------------------------------

def child(args):
    t0 = time.perf_counter()
    app = Path(args.app) if args.app else ROOT
    sys.path[:0] = [str(app), str(ROOT / "tests")]
    import offline_guard                   # as the app itself starts (workspace/__main__.py)
    offline_guard.disable_telemetry()
    offline_guard.install()
    import wave

    import numpy as np

    from workspace import api, credentials, shell
    from workspace.app import Workspace
    imports_s = time.perf_counter() - t0

    def send(**msg):
        sys.stdout.write(PREFIX + json.dumps(msg) + "\n")
        sys.stdout.flush()

    def log(text):                     # progress for whoever runs the tool
        print(f"  [{time.perf_counter() - t0:6.1f} s] {text}", file=sys.stderr, flush=True)

    fake = "test:fake_worker_engines:"
    root = Path(args.workdir)          # made and removed by the parent
    ws = Workspace(root / "ws", root / "models", start=False, allow_test_engines=True,
                   battery=False)
    try:
        ws.settings.update(model=fake + "sentences", speakers=False, onboarded=True)
        ws.start()
        rng = np.random.default_rng(7)
        for i in range(MEETINGS):
            path = root / f"synthetic-meeting-{i + 1}.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((rng.standard_normal(16000 * 60) * 300).astype("<i2").tobytes())
            ws.import_paths([str(path)])
        log("transcribing the synthetic meetings with fake engines")
        if not ws.wait_idle(300) or len(ws.meetings()) < MEETINGS:
            send(error="the synthetic meetings were not ready in 300 s")
            return 1
        worker = {"worker_pid": ws.handle.pid, "worker_last_used": time.time()}
        vault = {}                         # never touch the real Credential Manager
        credentials.load_token = lambda target=None: vault.get("token")
        credentials.has_token = lambda target=None: "token" in vault
        holder = {}

        def wait_for(window, js, ok, what, timeout=90):
            value, end = None, time.monotonic() + timeout
            while time.monotonic() < end:
                value = window.evaluate_js(js)
                if ok(value):
                    return value
                time.sleep(0.05)
            raise TimeoutError(f"{what} (last value {value!r})")

        def drive(window):
            try:
                if not window.events.loaded.wait(90):
                    raise TimeoutError("the page did not load")
                log("page loaded")
                # show() sets data-view; the page's start-up handler calls it once
                # it has chosen its first view, so wait for that before switching.
                wait_for(window, "document.body.dataset.view || ''", bool,
                         "the page did not start")
                log("page started")
                window.evaluate_js("window.workspace.show('meetings')")
                wait_for(window, "document.querySelectorAll('#groups li').length",
                         lambda n: (n or 0) >= MEETINGS, "the meetings list did not fill")
                interactive = time.perf_counter() - holder["t1"]
                log("meetings shown")
                send(imports_s=round(imports_s, 2), window_to_interactive_s=round(interactive, 2),
                     startup_s=round(imports_s + interactive, 2), **worker)
                for line in sys.stdin:
                    state = line.strip()
                    if state == "quit":
                        break
                    window.restore()
                    if state in ("meetings", "minimised"):
                        window.evaluate_js("window.workspace.show('meetings')")
                    elif state == "settings":
                        window.evaluate_js("window.workspace.show('settings')")
                    if state == "minimised":
                        window.minimize()
                    send(state=state)
            except Exception as exc:          # report, then let the window close
                send(error=f"{type(exc).__name__}: {exc}")
            finally:
                window.destroy()

        holder["t1"] = time.perf_counter()          # window creation to interactive
        window = shell.create_window(api.make_api(ws, lambda: holder.get("window")),
                                     title="Unminute performance check (synthetic data)",
                                     allowed=api.API_METHODS)
        holder["window"] = window
        shell.run(lambda w: threading.Thread(target=drive, args=(w,), daemon=True).start(),
                  (window,), storage_path=shell.storage_folder(ws.root))
        return 0
    finally:
        ws.close()


def main():
    parser = argparse.ArgumentParser(
        description="Release-time performance evidence (docs/RELEASE_STANDARD.md, section 2).",
        epilog=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", nargs="?", help="where to write the JSON report")
    parser.add_argument("--quick", action="store_true",
                        help="1 run, 20 s settle, 60 s windows, for before/after checks")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--settle", type=int, default=60)
    parser.add_argument("--window", type=int, default=300)
    parser.add_argument("--leak-minutes", type=int, default=0,
                        help="hold the Meetings view this long, once, and report growth")
    parser.add_argument("--app", help="an installed package's app folder")
    parser.add_argument("--zip", help="the package zip measured, to record its SHA-256")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--workdir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(args)
    if not args.report:
        parser.error("give a report path")
    return parent(args)


if __name__ == "__main__":
    sys.exit(main())
