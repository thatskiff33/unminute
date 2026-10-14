# tools/packaged_check.py
# #28 evidence from an INSTALLED package (not the source tree). Run it with the
# package's own runtime, pointing at a model cache and a non-sensitive
# recording:
#
#   <install>\versions\<v>\runtime\python.exe tools\packaged_check.py
#       <install>\versions\<v> <model cache> <recording.wav> <report.json>
#
# It uses a throwaway LOCALAPPDATA, so no real workspace is touched, and checks:
#   - self-test (dependencies, network guard) from the packaged runtime;
#   - offline processing with cached models and no Hugging Face token present;
#   - the full lifecycle (import, transcribe, speakers, review, cleanup) and
#     that no transcript text remains readable in any workspace file and that
#     no backup or copy of content exists;
#   - idle CPU and memory of the open app window, and peak memory of the
#     worker during a long transcription.
# Measurements, not promises: compare them on the lowest-supported laptop.

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

MARKER = "zebracrossingquartz"     # distinctive text written into the transcript


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def memory(pid):
    """(working set MB, peak working set MB) of a process."""
    k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000 | 0x0010, False, pid)   # QUERY_LIMITED | VM_READ
    if not h:
        return None
    try:
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return None
        return round(pmc.WorkingSetSize / 2**20), round(pmc.PeakWorkingSetSize / 2**20)
    finally:
        k32.CloseHandle(h)


def cpu_seconds(pid):
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt),
                            ctypes.byref(ut))
        as_s = (lambda ft: ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7)
        return as_s(kt) + as_s(ut)
    finally:
        k32.CloseHandle(h)


def tree(root_pid):
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | ForEach-Object "
                          "{ \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
                         capture_output=True, text=True).stdout
    kids = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            kids.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, todo = [], [root_pid]
    while todo:
        p = todo.pop()
        if p not in seen:
            seen.append(p)
            todo += kids.get(p, [])
    return seen


def check(version_dir, cache, wav, report):
    runtime = Path(version_dir) / "runtime"
    env = dict(os.environ)
    env.pop("HF_TOKEN", None)                         # cached processing needs no token
    tmp = Path(tempfile.mkdtemp(prefix="ww28-"))
    env["LOCALAPPDATA"] = str(tmp)
    py = str(runtime / "python.exe")

    r = subprocess.run([py, "-m", "workspace", "--self-test"], capture_output=True, text=True,
                       env=env, cwd=Path(version_dir) / "app")
    report["self_test"] = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "no output"

    # Lifecycle in a child process using the packaged runtime and app code.
    script = tmp / "lifecycle.py"
    script.write_text(LIFECYCLE, encoding="utf-8")
    r = subprocess.run([py, str(script), str(cache), str(wav), MARKER], capture_output=True,
                       text=True, env=env, cwd=Path(version_dir) / "app", timeout=7200)
    try:
        report["lifecycle"] = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        report["lifecycle"] = {"error": (r.stderr or r.stdout)[-2000:]}

    # Residual content: every file the app wrote under the throwaway profile.
    hits, files = [], 0
    for p in (tmp / "whisper").rglob("*"):
        if p.is_file():
            files += 1
            try:
                if MARKER.encode() in p.read_bytes():
                    hits.append(str(p.relative_to(tmp)))
            except OSError:
                pass
    names = sorted(str(p.relative_to(tmp)) for p in (tmp / "whisper").rglob("*") if p.is_file())
    report["residual"] = {"files_after_cleanup": names, "marker_found_in": hits,
                          "content_backups": [n for n in names if n.endswith(
                              (".bak", ".backup", ".old", ".copy")) or "backup" in n.lower()]}

    # Idle window: CPU and memory of the whole process tree for 60 s.
    p = subprocess.Popen([str(runtime / "pythonw.exe"), "-m", "workspace"], env=env,
                         cwd=Path(version_dir) / "app")
    time.sleep(20)
    pids = tree(p.pid)
    cpu0 = sum(cpu_seconds(x) or 0 for x in pids)
    time.sleep(60)
    pids = tree(p.pid)
    cpu1 = sum(cpu_seconds(x) or 0 for x in pids)
    mems = [memory(x) for x in pids]
    report["idle_window"] = {"processes": len(pids),
                             "cpu_percent_of_one_core": round((cpu1 - cpu0) / 60 * 100, 1),
                             "working_set_mb": sum(m[0] for m in mems if m)}
    for x in reversed(pids):
        subprocess.run(["taskkill", "/F", "/PID", str(x)], capture_output=True)
    report["profile"] = str(tmp)


LIFECYCLE = r'''
# The worker is started with multiprocessing "spawn", which re-imports this
# script in the child: everything must sit behind the __main__ guard.
import json, sys, time
sys.path.insert(0, ".")
import offline_guard
offline_guard.disable_telemetry(); offline_guard.install()


def worker_peak(pid):
    import ctypes
    from ctypes import wintypes
    class PMC(ctypes.Structure):
        _fields_ = ([("cb", wintypes.DWORD), ("a", wintypes.DWORD),
                     ("peak", ctypes.c_size_t)]
                    + [("x%d" % i, ctypes.c_size_t) for i in range(7)])
    k = ctypes.WinDLL("kernel32"); k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1010, False, pid)
    if not h:
        return 0
    try:
        pm = PMC(); pm.cb = ctypes.sizeof(pm)
        ok = ctypes.WinDLL("psapi").GetProcessMemoryInfo(h, ctypes.byref(pm), pm.cb)
        return pm.peak if ok else 0
    finally:
        k.CloseHandle(h)


def main():
    from workspace.app import Workspace
    cache, wav, marker = sys.argv[1], sys.argv[2], sys.argv[3]
    ws = Workspace(cache_dir=cache, start=False, battery=False)
    out = {}
    try:
        ws.settings.update(onboarded=True, speakers=True)
        ws.start()
        t = time.time()
        ws.import_paths([wav])
        while not ws.meetings() and time.time() - t < 600:
            time.sleep(1)
        peak = 0
        while time.time() - t < 7000:
            if ws.handle.pid:
                peak = max(peak, worker_peak(ws.handle.pid))
            if ws.wait_idle(5) and ws.meetings():
                m = ws.meetings()[0]
                if m["error"]:
                    break                          # failed: report it, do not wait on
                if m["stage"] != "inbox":
                    d = ws.detail(m["id"])
                    if d["speakers"] and d["speakers"]["status"] in ("done", "failed"):
                        break
        m = ws.meetings()[0]
        out["meetings_imported"] = len(ws.meetings())
        out["transcribed_s"] = round(time.time() - t)
        out["worker_peak_mb"] = round(peak / 2**20)
        if m["error"]:
            out["error"] = m["error"]
            return out
        d = ws.detail(m["id"])
        out["speakers"] = d["speakers"]["status"] if d["speakers"] else None
        items = ws.transcript(m["id"])["items"]
        out["passages"] = len(items)
        ws.edit(m["id"], items[0]["id"], "Marker " + marker + " for the residual check")
        ws.confirm_reviewed(m["id"]); ws.wait_idle(120)
        ws.confirm_final_saved(m["id"]); ws.wait_idle(120)
        out["meetings_left"] = len(ws.meetings())
        out["receipts"] = [r["result"] for r in ws.receipts()]
        out["blocked_network_attempts"] = len(offline_guard.blocked)
        return out
    finally:
        ws.close()


if __name__ == "__main__":
    print(json.dumps(main()))
'''


def main():
    version_dir, cache, wav, out = sys.argv[1:5]
    report = {"when": time.strftime("%Y-%m-%d %H:%M"), "cpu_count": os.cpu_count()}
    try:
        check(version_dir, cache, wav, report)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    Path(out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
