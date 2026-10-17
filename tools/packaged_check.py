# tools/packaged_check.py
# #28 evidence from an INSTALLED package (not the source tree). Run it with the
# package's own runtime, pointing at a model cache and a non-sensitive
# recording:
#
#   <install>\versions\<v>\runtime\python.exe tools\packaged_check.py
#       <install>\versions\<v> <model cache> <recording.wav> <report.json>
#       [--zip <the zip it was installed from>]
#
# With --zip the report is release evidence (tools/evidence.py): it names the
# zip and says whether the version folder still holds exactly its files.
#
# It uses a throwaway LOCALAPPDATA, so no real workspace is touched, and checks:
#   - self-test (dependencies, network guard) from the packaged runtime;
#   - offline processing with cached models and no Hugging Face token present;
#   - the full lifecycle (import, transcribe, speakers, review, cleanup) and
#     that no transcript text remains readable in any workspace file and that
#     no backup or copy of content exists;
#   - idle CPU and memory of the open app window, and peak memory of the
#     worker during a long transcription.
# Measurements, not promises: compare them on the lowest-supported laptop. The
# report ends with "pass" and the criteria that failed; exit code 1 if any did.

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))    # evidence.py, also under -I
import evidence  # noqa: E402

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
    flags = ["-I", "-B", "-X", "disable-remote-debug"]    # as the Start menu shortcut starts it

    r = subprocess.run([py, *flags, "-m", "workspace", "--self-test"], capture_output=True,
                       text=True,
                       env=env, cwd=Path(version_dir) / "app")
    report["self_test"] = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "no output"

    # Lifecycle in a child process using the packaged runtime and app code.
    script = tmp / "lifecycle.py"
    script.write_text(LIFECYCLE, encoding="utf-8")
    try:
        r = subprocess.run([py, *flags, str(script), str(cache), str(wav), MARKER],
                           capture_output=True,
                           text=True, env=env, cwd=Path(version_dir) / "app", timeout=7200)
        report["lifecycle"] = json.loads(r.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        report["lifecycle"] = {"error": "lifecycle script exceeded 7,200 s"}
    except (ValueError, IndexError):
        report["lifecycle"] = {"error": (r.stderr or r.stdout)[-2000:]}

    # Residual content: every file the app wrote under the throwaway profile.
    # A file that cannot be read cannot be shown free of the marker: it fails.
    hits, unreadable = [], []
    for p in (tmp / "whisper").rglob("*"):
        if p.is_file():
            try:
                if MARKER.encode() in p.read_bytes():
                    hits.append(str(p.relative_to(tmp)))
            except OSError:
                unreadable.append(str(p.relative_to(tmp)))
    names = sorted(str(p.relative_to(tmp)) for p in (tmp / "whisper").rglob("*") if p.is_file())
    report["residual"] = {"files_after_cleanup": names, "marker_found_in": hits,
                          "unreadable": unreadable,
                          "content_backups": [n for n in names if n.endswith(
                              (".bak", ".backup", ".old", ".copy")) or "backup" in n.lower()]}

    # Idle window: CPU and memory of the whole process tree for 60 s.
    p = subprocess.Popen([str(runtime / "pythonw.exe"), *flags, "-m", "workspace"], env=env,
                         cwd=Path(version_dir) / "app")
    time.sleep(20)
    pids = tree(p.pid)
    cpu0 = sum(cpu_seconds(x) or 0 for x in pids)
    time.sleep(60)
    pids = tree(p.pid)
    cpu1 = sum(cpu_seconds(x) or 0 for x in pids)
    mems = [memory(x) for x in pids]
    # Still running after the minute: a window that crashed or stopped at an
    # error message would leave only its own process, or none.
    report["idle_window"] = {"window_running": p.poll() is None, "processes": len(pids),
                             "cpu_percent_of_one_core": round((cpu1 - cpu0) / 60 * 100, 1),
                             "working_set_mb": sum(m[0] for m in mems if m)}
    for x in reversed(pids):
        subprocess.run(["taskkill", "/F", "/PID", str(x)], capture_output=True)
    # The throwaway profile goes (files can stay locked for a moment after the
    # kill). Its path names the Windows user, so it is not reported.
    for _ in range(20):
        shutil.rmtree(tmp, ignore_errors=True)
        if not tmp.exists():
            break
        time.sleep(0.5)
    report["profile_removed"] = not tmp.exists()


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
        imported = ws.import_paths([wav])
        while not ws.meetings() and time.time() - t < 600:
            time.sleep(1)
        if not ws.meetings():
            return {"error": "no meeting was created", "import_result": repr(imported)[:500]}
        peak = 0
        # Leave the parent's 7,200 s limit room for model load and close().
        while time.time() - t < 6600:
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
        else:
            out["error"] = "timed out before transcription and speakers finished"
        m = ws.meetings()[0]
        out["meetings_imported"] = len(ws.meetings())
        out["transcribed_s"] = round(time.time() - t)
        out["worker_peak_mb"] = round(peak / 2**20)
        if m["error"] or out.get("error"):
            out["error"] = m["error"] or out["error"]
            out["speakers"] = (ws.detail(m["id"])["speakers"] or {}).get("status")
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


def verdict(r):
    """Names of the criteria that failed (empty when everything passed). The
    idle window and the worker's peak have no budget yet (docs/RELEASE_STANDARD.md),
    so they only have to be measured."""
    life, res, idle = r.get("lifecycle") or {}, r.get("residual") or {}, r.get("idle_window") or {}
    checks = {
        "no error": not r.get("error") and not life.get("error"),
        "self-test passed": r.get("self_test") == "PASS",
        "transcribed with speakers": life.get("meetings_imported") == 1
            and life.get("speakers") == "done" and (life.get("passages") or 0) > 0,
        "cleaned up, with both receipts": life.get("meetings_left") == 0
            and sorted(life.get("receipts") or []) == ["audio removed", "transcript removed"],
        # The app process's own network guard; the worker's guard is covered by
        # the unit tests (tests/test_workers.py, tests/test_offline_guard.py).
        "no network attempts by the app process": life.get("blocked_network_attempts") == 0,
        "no content left behind": res.get("marker_found_in") == []
            and res.get("unreadable") == [] and res.get("content_backups") == [],
        "idle window measured": idle.get("window_running") is True
            and (idle.get("processes") or 0) > 1 and (idle.get("working_set_mb") or 0) > 0,
    }
    return [name for name, ok in checks.items() if not ok]


def main():
    ap = argparse.ArgumentParser(description="Lifecycle evidence from an installed package (#28).",
                                 allow_abbrev=False)
    ap.add_argument("version_dir", help=r"the installed version folder (<install>\versions\<v>)")
    ap.add_argument("cache", help="a model cache with the models already downloaded")
    ap.add_argument("wav", help="a non-sensitive recording")
    ap.add_argument("out", help="where to write the JSON report")
    ap.add_argument("--zip", help="the zip installed in version_dir: records it as release "
                    "evidence (tools/evidence.py); exit 1 unless that evidence passes")
    args = ap.parse_args()
    # A wrong --zip, or a folder that is not that zip's, stops now, not after the run.
    before = evidence.prepare(ap, args, version_dir=Path(args.version_dir))
    report = {"when": time.strftime("%Y-%m-%d %H:%M"), "cpu_count": os.cpu_count()}
    try:
        check(args.version_dir, args.cache, args.wav, report)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["failed"] = verdict(report)
    report["pass"] = not report["failed"]
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if args.zip:
        ev = evidence.stamp_file(args.out, "packaged", args.zip, args.version_dir,
                                 report["pass"], before=before)
        return 0 if ev["pass"] else 1
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
