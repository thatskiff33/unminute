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
#   python tools/perf_check.py <report.json>              release run: 3 runs,
#                                                          60 s settle, 300 s windows
#   python tools/perf_check.py <report.json> --quick      1 run, 20 s settle, 60 s
#                                                          windows (before/after checks)
#   --app <install>\versions\<v>\app   measure an installed package's code; run
#                                      with that package's runtime\python.exe
#   --zip <Unminute-X.Y.Z.zip>         record the SHA-256 of the package measured
#   --leak-minutes 30                  also hold the Meetings view and report growth
#
# Exit code 1 when a figure exceeds its budget in docs/perf-budgets.json. The
# window is visible for the whole run; leave the machine idle, on mains power.
# The worker's peak and speed come from tools/packaged_check.py (real models).

import argparse
import hashlib
import json
import math
import os
import platform
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


def compare(figures, budgets):
    """[(state, metric, value, limit)] for every figure over its budget. A budget
    of null (not set yet) or a figure not measured is skipped."""
    over = []
    for state, limits in budgets.items():
        for metric, limit in limits.items():
            value = figures.get(state, {}).get(metric)
            if _number(limit) and _number(value) and value > limit:
                over.append((state, metric, value, limit))
    return over


BUDGETED = ("cpu_percent_of_one_core", "private_mb", "startup_s")


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
            else:                           # never closer than 0.05 to the baseline
                limits[metric] = round(max(value * (1 + margin), value + 0.05), 2)
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
            return {"cpu_s": secs}
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
        self.k32.SetInformationJobObject(self.handle, 9, ctypes.byref(limits),
                                         ctypes.sizeof(limits))
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


def measure(job, pid, settle, window):
    """Wait settle seconds, then measure CPU over window seconds and memory at its end."""
    time.sleep(settle)
    job0, _ = job.cpu_seconds()
    before = {p: sample(p) for p, _ in tree(pid)}
    time.sleep(window)
    job1, active = job.cpu_seconds()
    procs = tree(pid)
    after = {p: sample(p) for p, _ in procs}
    tree_cpu = sum(a["cpu_s"] - (before.get(p) or {}).get("cpu_s", 0)
                   for p, a in after.items() if a)
    live = [a for a in after.values() if a]
    names = sorted(name for _, name in procs)
    mb = lambda key: round(sum(a.get(key) or 0 for a in live) / 2**20)  # noqa: E731
    return {
        "cpu_percent_of_one_core": round((job1 - job0) / window * 100, 3),
        "cpu_percent_tree": round(tree_cpu / window * 100, 3),
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


def one_run(args, settle, window):
    # -B: never write bytecode into the package being measured.
    child = [sys.executable, "-B", str(Path(__file__).resolve()), "--child"]
    if args.app:
        child += ["--app", args.app]
    proc = subprocess.Popen(child, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, encoding="utf-8", bufsize=1)
    job = Job(proc)
    result = {}

    def read():
        for line in proc.stdout:
            if line.startswith(PREFIX):
                return json.loads(line[len(PREFIX):])
        raise RuntimeError(f"the window process ended (exit {proc.wait()})")

    def say(command):
        proc.stdin.write(command + "\n")
        proc.stdin.flush()

    try:
        ready = read()
        if ready.get("error"):
            raise RuntimeError(ready["error"])
        result["startup"] = {k: ready[k] for k in ("imports_s", "window_to_interactive_s",
                                                   "startup_s")}
        # Idle means after the worker has unloaded. Waiting for it also measures
        # the standard's "gone within its idle time plus 5 s" (section 3).
        worker = ready.get("worker_pid")
        if worker:
            print("  waiting for the worker to unload", flush=True)
            deadline = time.time() + WORKER_WAIT_S
            while time.time() < deadline and any(p == worker for p, _ in tree(proc.pid)):
                time.sleep(0.5)
            gone = not any(p == worker for p, _ in tree(proc.pid))
            result["worker"] = {"exit_after_last_use_s": round(time.time()
                                                               - ready["worker_last_used"], 1)
                                if gone else None, "exited": gone}
        for state in STATES:
            say(state)
            if read().get("state") != state:
                raise RuntimeError(f"the window did not reach {state}")
            result[state] = measure(job, proc.pid, settle, window)
            print(f"  {state}: {result[state]['cpu_percent_of_one_core']}% of one core, "
                  f"{result[state]['private_mb']} MB private", flush=True)
        if args.leak_minutes:
            say("meetings")
            read()
            time.sleep(settle)
            start = sum((sample(p) or {}).get("private") or 0 for p, _ in tree(proc.pid))
            time.sleep(args.leak_minutes * 60)
            end = sum((sample(p) or {}).get("private") or 0 for p, _ in tree(proc.pid))
            result["leak"] = {"minutes": args.leak_minutes,
                              "private_growth_mb": round((end - start) / 2**20, 1)}
        say("quit")
        proc.wait(60)
    finally:
        if proc.poll() is None:
            proc.kill()
        job.close()
    return result


def parent(args):
    settle, window, runs = (20, 60, 1) if args.quick else (args.settle, args.window, args.runs)
    report = {"tool": "tools/perf_check.py", "mode": "quick" if args.quick else "release",
              "settle_s": settle, "window_s": window, "runs": runs,
              "environment": environment(args)}
    results = []
    for n in range(runs):
        print(f"run {n + 1} of {runs}", flush=True)
        results.append(one_run(args, settle, window))
    report["each_run"] = results
    figures = median_report(results)
    worker_runs = [{"worker": r["worker"]} for r in results if r.get("worker", {}).get("exited")]
    if worker_runs:
        figures["worker"] = median_report(worker_runs)["worker"]
    report["median"] = figures
    budgets = json.loads(BUDGETS.read_text(encoding="utf-8")) if BUDGETS.exists() else {}
    over = compare(figures, budgets.get("budgets", {}))
    report["over_budget"] = [{"state": s, "metric": m, "value": v, "budget": b}
                             for s, m, v, b in over]
    report["proposed_budgets_plus_20_percent"] = propose(figures)
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["median"], indent=2))
    print("OVER BUDGET: " + "; ".join(f"{s} {m} {v} > {b}" for s, m, v, b in over)
          if over else "WITHIN BUDGET" if budgets else "NO BUDGETS YET")
    return 1 if over else 0


# -- the child: the real window on synthetic meetings ----------------------------------

def child(args):
    t0 = time.perf_counter()
    app = Path(args.app) if args.app else ROOT
    sys.path[:0] = [str(app), str(ROOT / "tests")]
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
    with tempfile.TemporaryDirectory(prefix="unminute-perf-") as tmp:
        root = Path(tmp)
        ws = Workspace(root / "ws", root / "models", start=False, allow_test_engines=True,
                       battery=False)
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
            ws.close()
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
        ws.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Release-time performance evidence.")
    parser.add_argument("report", nargs="?", help="where to write the JSON report")
    parser.add_argument("--quick", action="store_true",
                        help="1 run, 20 s settle, 60 s windows, for before/after checks")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--settle", type=int, default=60)
    parser.add_argument("--window", type=int, default=300)
    parser.add_argument("--leak-minutes", type=int, default=0)
    parser.add_argument("--app", help="an installed package's app folder")
    parser.add_argument("--zip", help="the package zip measured, to record its SHA-256")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(args)
    if not args.report:
        parser.error("give a report path")
    return parent(args)


if __name__ == "__main__":
    sys.exit(main())
