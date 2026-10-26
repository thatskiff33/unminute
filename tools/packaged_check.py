# tools/packaged_check.py
# #28 evidence from an INSTALLED package (not the source tree). Run it with the
# package's own runtime, pointing at a model cache and a non-sensitive
# recording:
#
#   <install>\versions\<v>\runtime\python.exe tools\packaged_check.py
#       <install>\versions\<v> <model cache> <recording.wav> <report.json>
#       [--zip <the zip it was installed from>] [--report-system-dumps]
#
# With --zip the report is release evidence (tools/evidence.py): it names the
# zip and says whether the version folder still holds exactly its files.
#
# It uses a throwaway base folder for LOCALAPPDATA, TEMP and TMP, and for the
# children's APPDATA and USERPROFILE (so a library's cache under ~\.cache or
# %APPDATA% lands there too), so no real workspace is touched. The children
# that open the workspace keep its audit log in its file only: they pass a
# mirror that drops the Windows Event Log copy, so a run adds no entries
# under the app's source. The idle window (step 6), started as the app is,
# still writes its own content-free entries, so it is tested exactly as
# shipped; it runs after the last scan and comparison, so what it writes is
# not compared (the owner's decision, 26 Oct 2026; #418). It checks:
#   - self-test (dependencies, network guard) from the packaged runtime;
#   - offline processing with cached models and no Hugging Face token present;
#   - the full lifecycle (import, transcribe, speakers, review, cleanup);
#   - #111 phase C (C7), since content is sealed and the audio encrypted, so
#     "no plain copy" alone no longer shows much (tools/residue.py):
#       * a positive control: the finder that scans the profile must find
#         every pattern planted in a control file outside it;
#       * at rest, while the meeting exists: the marker (title, edit, speaker
#         name, hold reason) and windows of the decoded audio are in no file
#         under the profile or TEMP, and --verify-audit says "ok";
#       * a microphone recording through the shipped confirmation, from a
#         synthetic stream: its audio is in no file while it records (the
#         scan must include at least one finished chunk), and its key is
#         gone after import and after a discard;
#       * erase evidence through the store: after audio cleanup the audio key
#         is gone while the text key opens and the media folder is gone; after
#         final cleanup no meeting, no update blocker, no free page and an
#         empty write-ahead log (byte absence of a wrapped key stays a unit
#         test: tests/test_cleanup.py);
#       * after final cleanup, only the workspace's own files remain (a list
#         built from the app's constants), logs included and listed; TEMP
#         and the redirected APPDATA and user folder hold nothing (their
#         files are named, and scanned with the rest);
#       * outside the throwaway base, the real LocalLow and the model cache
#         are listed before and after (names, sizes and times only, never
#         opened): every file the run added or changed there must be one
#         residue.EXPLAINED names;
#       * no crash dump, and no crash file with anything in it, under the base;
#       * the report itself carries no Windows user name: error text and
#         the children's results are redacted, and a report that would
#         still carry it anywhere (a file-name list included) fails in
#         place of being written, naming only where (residue.guarded());
#   - not covered yet (the owner's decision, 26 Oct 2026): the window with
#     content. No step shows a meeting in the real window, so the WebView2
#     profile (workspace\webview) and the real start's own files are not
#     shown free of content (docs/SECURITY_MODEL.md T7);
#   - idle CPU and memory of the open app window, and peak memory of the
#     worker during a long transcription.
# Measurements, not promises: compare them on the lowest-supported laptop. The
# report ends with "pass" and the criteria that failed; exit code 1 if any did.
# No dump or crash file is ever opened; the report holds counts only. The
# signed-in user's own dump folders are counted (names and times only) only
# with --report-system-dumps.

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

# evidence.py, residue.py and windows_powershell.py, also under -I
sys.path.append(str(Path(__file__).resolve().parent))
import evidence  # noqa: E402
import residue  # noqa: E402
import windows_powershell  # noqa: E402

MARKER = "zebracrossingquartz"     # distinctive text written into the meeting
WINDOW_SAMPLES = 4096              # each window of decoded audio (float32)
WINDOWS = 8                        # how many, at fixed offsets through the clip
WINDOW_BYTES = WINDOW_SAMPLES * 4
RECORDING_WINDOW_BYTES = 2048      # from the middle of a synthetic block (int16)
CHILD_TIMEOUT_S = 7200


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
    # Windows PowerShell by full path, with a minimal environment (#237).
    out = windows_powershell.run("Get-CimInstance Win32_Process | ForEach-Object "
                                 "{ \"$($_.ProcessId) $($_.ParentProcessId)\" }").stdout
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


def last_json(text):
    """The child's last JSON line (its result)."""
    return json.loads(text.strip().splitlines()[-1])


def scan(base, roots, patterns):
    """residue.find over roots, with the names relative to base."""
    return residue.find(roots, patterns, base)


def chunks_scanned(scanned):
    """How many of the scanned files are finished microphone chunks (named by
    the package's recorder: chunk-NNNNNN.uma). "Not found while it records"
    means something only if at least one was scanned (review of #335)."""
    from workspace import recorder
    return sum(1 for rel in scanned if Path(rel).name.startswith(recorder.MIC_PREFIX)
               and Path(rel).suffix == recorder.CHUNK_SUFFIX)


def check(version_dir, cache, wav, report, system_dumps=False):
    runtime, app = Path(version_dir) / "runtime", Path(version_dir) / "app"
    started = time.time()
    env = dict(os.environ)
    env.pop("HF_TOKEN", None)                         # cached processing needs no token
    base = Path(tempfile.mkdtemp(prefix="umpkg-"))
    # The child's TEMP and TMP, APPDATA and user folder, all in the base:
    # whatever the app or a library leaves there is named and scanned.
    redirected = {"TEMP": base / "temp", "APPDATA": base / "roaming",
                  "USERPROFILE": base / "home"}
    for folder in redirected.values():
        folder.mkdir()
    temp = redirected["TEMP"]
    env["LOCALAPPDATA"] = str(base)
    env["TEMP"] = env["TMP"] = str(temp)
    env["APPDATA"], env["USERPROFILE"] = str(redirected["APPDATA"]), str(redirected["USERPROFILE"])
    profile = base / "Unminute"
    roots = [profile, *redirected.values()]           # every scan covers all of these
    py = str(runtime / "python.exe")
    flags = ["-I", "-B", "-X", "disable-remote-debug"]    # as the Start menu shortcut starts it
    tools = str(Path(__file__).resolve().parent)
    # The workspace's own file names, from the package's constants.
    evidence.add_app(app)
    names = residue.names()
    workspace = profile / "workspace"
    ws_prefix = Path(os.path.relpath(workspace, base)).as_posix()
    # The real LocalLow and model cache, by name, size and time only (P2).
    places = residue.real_places(cache)
    before = residue.snapshot(places)

    r = subprocess.run([py, *flags, "-m", "workspace", "--self-test"], capture_output=True,
                       text=True,
                       env=env, cwd=app)
    report["self_test"] = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "no output"

    script = base / "lifecycle.py"                    # in the base, outside the profile
    script.write_text(LIFECYCLE, encoding="utf-8")
    command = [py, *flags, str(script)]
    rest = [str(cache), str(wav), MARKER, str(base), tools]

    def child(mode, timeout=CHILD_TIMEOUT_S):
        try:
            r = subprocess.run([*command, mode, *rest], capture_output=True, text=True,
                               env=env, cwd=app, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"the {mode} step exceeded {timeout:,} s"}
        # What the child says goes into the report without the base path,
        # the home folder or the user name (P3), redacted once decoded:
        # JSON escapes a name that is not ASCII (review of #335).
        try:
            result = last_json(r.stdout)
        except (ValueError, IndexError):
            return {"error": residue.redact((r.stderr or r.stdout)[-2000:], base)}
        return residue.redact_json(result, base)

    # 1. Import, transcribe and identify speakers; the marker goes into the
    #    title, an edit, a speaker's name and a hold reason; the child writes
    #    windows of the decoded audio to the base (outside the profile).
    report["lifecycle"] = child("process")

    # 2. At rest, while the meeting exists. The positive control first: the
    #    same finder must find every pattern in a planted file.
    windows_file = base / "windows.bin"
    windows = (residue.split_windows(windows_file.read_bytes(), WINDOW_BYTES)
               if windows_file.exists() else {})
    patterns = {**residue.text_patterns(MARKER), **windows}
    report["control"] = {"patterns": len(patterns), "missed": residue.control(base, patterns)}
    at_rest = scan(base, roots, patterns)
    outside = residue.compare(before, places, names)
    r = subprocess.run([py, *flags, "-m", "workspace", "--verify-audit"], capture_output=True,
                       text=True, env=env, cwd=app)
    try:
        audit_result = last_json(r.stdout).get("result")
    except (ValueError, IndexError, AttributeError):
        audit_result = "no output"
    report["at_rest"] = {"windows": len(windows), "found": at_rest["hits"],
                         "unreadable": at_rest["unreadable"],
                         "files_scanned": len(at_rest["scanned"]),
                         "audit_files": sum(1 for s in at_rest["scanned"]
                                            if s.startswith(f"{ws_prefix}/{names['audit']}/")),
                         "verify_audit": audit_result}

    # 3. A microphone recording through the shipped confirmation (#111 C3b).
    #    The child pauses while recording, so the files are scanned then.
    report["recording"] = record(command, rest, env, app, base, roots)

    # 4. Audio cleanup, then final cleanup, with erase evidence from the store.
    report["cleanup"] = child("cleanup", timeout=1800)

    # 5. After final cleanup: nothing of the content, only the workspace's own
    #    files, and the logs among what was scanned.
    rec_file = base / "recording_windows.bin"
    if rec_file.exists():
        patterns.update({f"recording_{k}": v for k, v in residue.split_windows(
            rec_file.read_bytes(), RECORDING_WINDOW_BYTES).items()})
    report["control"]["after_cleanup_missed"] = residue.control(base, patterns)
    after = scan(base, roots, patterns)
    # Listed through residue, which names a folder it cannot list rather
    # than skipping it (review of #335).
    in_base = Path(os.path.relpath(profile, base)).as_posix()
    files = sorted(f"{in_base}/{name}"
                   for name in residue.listing(profile, after["unreadable"], base))
    left = residue.left_in(redirected, base)
    outside = residue.compare(before, places, names, earlier=outside)
    report["residual"] = {"files_after_cleanup": files, "found": after["hits"],
                          "unreadable": sorted(set(after["unreadable"]) | set(left["unreadable"])),
                          "unexpected_files": residue.unexpected(profile, workspace, names),
                          "logs_scanned": residue.logs_scanned(after["scanned"], names,
                                                               ws_prefix),
                          # TEMP, APPDATA and the user folder: every file, by name.
                          "temp_files_left": left["files"]["TEMP"],
                          "redirected_files_left": left["files"],
                          "left_unexplained": left["unexplained"],
                          "left_explained": left["explained"],
                          # LocalLow and the model cache: new or changed files, by name.
                          "outside_new": outside["new"],
                          "outside_unexplained": outside["unexplained"],
                          # Each residue.EXPLAINED entry relied on, by name.
                          "outside_explained": outside["explained"],
                          "outside_unreadable": outside["unreadable"],
                          "content_backups": [n for n in files if n.endswith(
                              (".bak", ".backup", ".old", ".copy")) or "backup" in n.lower()]}

    # 6. Idle window: CPU and memory of the whole process tree for 60 s. taskkill
    # is found first, so a missing one stops the check before the app starts.
    taskkill = windows_powershell.system_program("taskkill.exe")
    p = subprocess.Popen([str(runtime / "pythonw.exe"), *flags, "-m", "workspace"], env=env,
                         cwd=app)
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
    kill(taskkill, pids)

    # 7. Crash dumps: none under the base (the window was ended by taskkill,
    #    which leaves its crash file empty). Counts only, nothing opened.
    report["dumps"] = residue.dumps(base)
    if system_dumps:
        report["system_dumps"] = residue.system_dumps(started)
    # The throwaway base goes (files can stay locked for a moment after the
    # kill). Its path names the Windows user, so it is not reported.
    for _ in range(20):
        shutil.rmtree(base, ignore_errors=True)
        if not base.exists():
            break
        time.sleep(0.5)
    report["profile_removed"] = not base.exists()


def record(command, rest, env, app, base, roots, timeout=1800):
    """The recording step: the child records synthetic audio, says when, and
    waits; the profile and the redirected folders (roots) are scanned for
    that audio; then the child
    imports it, records and discards a second one, and reports. A child that
    takes longer than timeout is killed (a watchdog: reading its output
    would otherwise wait for ever)."""
    proc = subprocess.Popen([*command, "record", *rest], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, cwd=app)
    err = []
    threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True).start()
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    out, during = None, None
    try:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("recording"):
                rec_file = base / "recording_windows.bin"
                patterns = (residue.split_windows(rec_file.read_bytes(),
                                                  RECORDING_WINDOW_BYTES)
                            if rec_file.exists() else {})
                missed = residue.control(base, patterns) if patterns else ["no patterns"]
                found = scan(base, roots, patterns)
                during = {"seconds": msg.get("seconds"), "windows": len(patterns),
                          "control_missed": missed, "found": found["hits"],
                          "unreadable": found["unreadable"],
                          "chunks_scanned": chunks_scanned(found["scanned"])}
                proc.stdin.write("go\n")
                proc.stdin.flush()
            elif "result" in msg:
                out = residue.redact_json(msg["result"], base)     # decoded (P3)
        proc.wait(60)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
    if out is None:
        out = {"error": "the recording step ended without a result: "
                        + residue.redact("".join(err)[-2000:], base)}
    out["during"] = during
    return out


def kill(taskkill, pids):
    """Hard-kill each process in `pids`, the last first. `taskkill` is
    windows_powershell.system_program("taskkill.exe"): started by its full
    path in the system folder with the minimal environment, so a taskkill.exe
    in the current folder or on PATH never runs instead (#253)."""
    env = windows_powershell.environment()
    for x in reversed(pids):
        subprocess.run([taskkill, "/F", "/PID", str(x)], capture_output=True,
                       stdin=subprocess.DEVNULL, env=env)


LIFECYCLE = r'''
# The worker is started with multiprocessing "spawn", which re-imports this
# script in the child: everything must sit behind the __main__ guard.
# Modes: process (import, transcribe, speakers, marker, audio windows),
# record (a synthetic recording, kept then discarded), cleanup (both
# confirmations, with erase evidence). Key bytes are never printed, written
# or returned: only whether each key opened.
import json, sys, time
sys.path.insert(0, ".")
from workspace import offline_guard
offline_guard.disable_telemetry(); offline_guard.install()

WINDOW_SAMPLES, WINDOWS, RECORDING_WINDOW_BYTES = 4096, 8, 2048


def say(**kw):
    print(json.dumps(kw), flush=True)


class FileOnlyMirror:
    """The audit log's Windows Event Log copy, dropped: this throwaway
    workspace sits where the app's own would (LOCALAPPDATA is redirected),
    so without it every run would add entries under the app's source, with
    the user's SID, for chains nobody keeps (review of #335). The records
    still go to the workspace's audit folder, which the check verifies."""

    def report(self, event_id, level, text):
        pass


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


def key_state(get, *args):
    """"opens", "missing" (KeyMissing) or "damaged": never the key itself."""
    from workspace.store import KeyMissing
    try:
        key = get(*args)
    except KeyMissing:
        return "missing"
    except Exception:
        return "damaged"
    del key
    return "opens"


def varied(window, least):
    import numpy as np
    return np.unique(window).size >= least


def audio_windows(wav, base):
    """Windows of the clip's decoded audio (float32), as the app decodes it,
    at fixed offsets: each moved on until it is not near-silence, so the
    bytes cannot turn up by chance. Decoded in memory, written to the base."""
    import io
    import numpy as np
    from pathlib import Path
    from workspace import media
    buf = io.BytesIO()
    media.decode(wav, buf)
    audio = np.frombuffer(buf.getvalue(), dtype=np.float32)
    chosen = []
    for k in range(WINDOWS):
        at = int(len(audio) * (k + 1) / (WINDOWS + 1))
        while at + WINDOW_SAMPLES <= len(audio):
            w = audio[at:at + WINDOW_SAMPLES]
            if varied(w, WINDOW_SAMPLES // 2):
                chosen.append(w.tobytes())
                break
            at += WINDOW_SAMPLES
    Path(base, "windows.bin").write_bytes(b"".join(chosen))
    return len(chosen)


def process(ws, wav, marker, base):
    out = {}
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
    mid = m["id"]
    out["meetings_imported"] = len(ws.meetings())
    out["transcribed_s"] = round(time.time() - t)
    out["worker_peak_mb"] = round(peak / 2**20)
    if m["error"] or out.get("error"):
        out["error"] = m["error"] or out["error"]
        out["speakers"] = (ws.detail(mid)["speakers"] or {}).get("status")
        return out
    d = ws.detail(mid)
    out["speakers"] = d["speakers"]["status"] if d["speakers"] else None
    items = ws.transcript(mid)["items"]
    out["passages"] = len(items)
    # The marker, wherever the user writes text: an edit, the title, a
    # speaker's name and a hold's reason (the hold is released: releasing
    # deletes nothing, and cleanup then goes ahead).
    ws.edit(mid, items[0]["id"], "Marker " + marker + " for the residual check")
    ws.rename(mid, "Marker " + marker)
    label = next((i["label"] for i in items if i.get("label")), None)
    if label:
        ws.rename_speaker(mid, label, "Marker " + marker)
    out["speaker_named"] = bool(label)
    ws.release_hold(ws.add_hold(mid, "audio", "Marker " + marker + " hold reason"))
    out["windows"] = audio_windows(wav, base)
    out["audio_key"] = key_state(ws.store.meeting_key, mid, "audio")
    out["blocked_network_attempts"] = len(offline_guard.blocked)
    return out


def recording_windows(blocks, base):
    """The middle of every fifth synthetic block that is not near-silence
    (the stream's pauses are zeros), written to the base."""
    from pathlib import Path
    chosen = []
    for n, b in enumerate(blocks):
        raw = b.tobytes()
        mid = raw[(len(raw) - RECORDING_WINDOW_BYTES) // 2:][:RECORDING_WINDOW_BYTES]
        if n % 5 == 0 and len(mid) == RECORDING_WINDOW_BYTES and len(set(mid)) > 200:
            chosen.append(mid)
        if len(chosen) == WINDOWS:
            break
    Path(base, "recording_windows.bin").write_bytes(b"".join(chosen))
    return len(chosen)


def wait_seconds(ws, seconds, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        rec = ws.recording_status().get("recording")
        if rec and rec["seconds"] >= seconds:
            return rec["seconds"]
        time.sleep(0.2)
    raise TimeoutError("the synthetic audio did not reach the recording")


def record(ws, pc, base):
    """Through the bridge, as the page does: the app asks its own recording
    question and the answerer says yes to it only (tools/perf_check.py)."""
    from workspace import api, consent, update
    from workspace.diag import DB_FILE
    out = {}
    ws.settings.update(onboarded=True, speakers=True, microphone=True)
    ws.start()
    ws.wait_idle(600)
    confirm, asked = pc.consent_answerer(consent.RECORDING_TITLE)
    bridge = api.make_api(ws, lambda: None, confirm=confirm)
    before = len(ws.meetings())
    required = ws.consent_required()
    r = bridge.start_recording(0, pc.SYNTHETIC_DEVICE["name"])
    if not r.get("ok"):
        return {"error": "the recording did not start: " + str(r.get("error"))[:200]}
    rec = ws._recording
    rid = rec.folder.name
    out["consent"] = pc.consent_outcome(required, rec.journal.get("consent"), list(asked),
                                        consent.RECORDING_TITLE,
                                        consent.valid_recording_record)
    asked.clear()
    seconds = wait_seconds(ws, 8)
    out["recording_windows"] = recording_windows(pc.synthetic_blocks(), base)
    out["key_while_recording"] = key_state(ws.store.recording_key, rid)
    say(recording=True, seconds=seconds)
    sys.stdin.readline()                       # the parent has scanned the files
    out["stopped"] = bool(bridge.stop_recording().get("ok"))
    t = time.time()
    while time.time() - t < 1500:
        if len(ws.meetings()) > before and ws.wait_idle(5):
            break
        time.sleep(1)
    out["imported"] = len(ws.meetings()) - before
    out["key_after_import"] = key_state(ws.store.recording_key, rid)
    # A second recording, discarded.
    r = bridge.start_recording(0, pc.SYNTHETIC_DEVICE["name"])
    if not r.get("ok"):
        out["error"] = "the second recording did not start: " + str(r.get("error"))[:200]
        return out
    rid2 = ws._recording.folder.name
    wait_seconds(ws, 2)
    # Its key opens first, so "missing" after the discard is about this id.
    out["key_before_discard"] = key_state(ws.store.recording_key, rid2)
    out["discarded"] = bool(bridge.discard_recording().get("ok"))
    out["key_after_discard"] = key_state(ws.store.recording_key, rid2)
    ws.wait_idle(120)
    out["recordings_waiting"] = sum(b["count"] for b in update.blockers(ws.root / DB_FILE)
                                    if b["kind"] == "recordings")
    # Only the recording question was ever asked (the answerer says no to any other).
    out["other_questions"] = [q for q in asked if q != consent.RECORDING_TITLE]
    return out


def cleanup(ws):
    """Both confirmations for every meeting, with erase evidence: whether each
    key opens, never the key."""
    from workspace import media
    ws.settings.update(onboarded=True, speakers=True)
    ws.start()
    ws.wait_idle(1200)
    out = {"meetings": len(ws.meetings()), "after_audio_cleanup": []}
    for m in ws.meetings():
        mid = m["id"]
        ws.confirm_reviewed(mid)
        folder = media.meeting_folder(ws.media, mid)
        t = time.time()
        while time.time() - t < 120 and (folder.exists() or not ws.wait_idle(1)):
            time.sleep(0.25)
        out["after_audio_cleanup"].append({
            "audio_key": key_state(ws.store.meeting_key, mid, "audio"),
            "text_key": key_state(ws.store.meeting_key, mid, "text"),
            "media_folder_gone": not folder.exists()})
    for m in ws.meetings():
        ws.confirm_final_saved(m["id"])
        ws.wait_idle(120)
    out["meetings_left"] = len(ws.meetings())
    out["receipts"] = sorted(r["result"] for r in ws.receipts())
    out["blocked_network_attempts"] = len(offline_guard.blocked)
    return out


def at_end(root):
    """After the app has closed: what would block an update, the database's
    free pages and its write-ahead log (read before opening: closing may
    checkpoint), through the store."""
    from workspace import update
    from workspace.diag import DB_FILE
    from workspace.store import Store
    db = root / DB_FILE
    wal = db.with_name(db.name + "-wal")
    out = {"wal_bytes": wal.stat().st_size if wal.exists() else 0,
           "blockers": [b["kind"] for b in update.blockers(db)]}
    store = Store(db, housekeeping=False)
    try:
        out["freelist_pages"] = store._one("PRAGMA freelist_count")[0]
        out["meetings_in_store"] = len(store.meetings(content=False))
    finally:
        store.close()
    return out


def main():
    mode, cache, wav, marker, base, tools = sys.argv[1:7]
    from workspace.app import Workspace
    pc = None
    if mode == "record":
        # The synthetic microphone, in this process only, as tools/perf_check.py
        # does: no audio device opens, and the power scheme is never changed.
        sys.path.append(tools)
        import perf_check as pc
        from workspace import power, recorder
        recorder.list_devices = lambda: [dict(pc.SYNTHETIC_DEVICE)]

        def never(*a, **k):
            raise RuntimeError("the packaged check never changes the power scheme")
        power._PowrProf.write = power._PowrProf.apply = never
    ws = Workspace(cache_dir=cache, start=False, battery=False, audit_mirror=FileOnlyMirror())
    if pc is not None:
        blocks = pc.synthetic_blocks()
        ws.open_stream = lambda device, rate, callback, finished: pc.PacedStream(
            rate, callback, finished, blocks=blocks)
    root = ws.root
    try:
        if mode == "process":
            out = process(ws, wav, marker, base)
        elif mode == "record":
            out = record(ws, pc, base)
        else:
            out = cleanup(ws)
    finally:
        ws.close()
    if mode == "cleanup":
        out.update(at_end(root))
    if mode == "record":
        say(result=out)                         # the parent reads lines as they come
    else:
        say(**out)


if __name__ == "__main__":
    main()
'''


def verdict(r):
    """Names of the criteria that failed (empty when everything passed). The
    idle window and the worker's peak have no budget yet (docs/RELEASE_STANDARD.md),
    so they only have to be measured."""
    life, res, idle = r.get("lifecycle") or {}, r.get("residual") or {}, r.get("idle_window") or {}
    rest, ctl = r.get("at_rest") or {}, r.get("control") or {}
    rec, clean = r.get("recording") or {}, r.get("cleanup") or {}
    during, dumps = rec.get("during") or {}, r.get("dumps") or {}
    meetings = clean.get("meetings") or 0
    erased = clean.get("after_audio_cleanup") or []
    checks = {
        "no error": not r.get("error") and not life.get("error"),
        "self-test passed": r.get("self_test") == "PASS",
        "transcribed with speakers": life.get("meetings_imported") == 1
            and life.get("speakers") == "done" and (life.get("passages") or 0) > 0,
        "cleaned up, with both receipts": not clean.get("error") and meetings >= 1
            and clean.get("meetings_left") == 0
            and clean.get("receipts") == sorted(["audio removed", "transcript removed"]
                                                * meetings),
        # The app process's own network guard; the worker's guard is covered by
        # the unit tests (tests/test_workers.py, tests/test_offline_guard.py).
        "no network attempts by the app process": life.get("blocked_network_attempts") == 0
            and clean.get("blocked_network_attempts") == 0,
        # #111 C7: the finder finds what was planted, or "not found" means nothing.
        "scanner control found every pattern": ctl.get("missed") == []
            and ctl.get("after_cleanup_missed") == [] and (ctl.get("patterns") or 0) > 2,
        "nothing readable at rest": life.get("speaker_named") is True
            and rest.get("windows") == 8 and rest.get("found") == {}
            and rest.get("unreadable") == [] and life.get("audio_key") == "opens",
        "audit log verified, no content in it": rest.get("verify_audit") == "ok"
            and (rest.get("audit_files") or 0) > 0,
        "recording encrypted while it records": during.get("windows") == 8
            and (during.get("chunks_scanned") or 0) >= 1
            and during.get("control_missed") == [] and during.get("found") == {}
            and during.get("unreadable") == [],
        "recording confirmed, then its key erased": not rec.get("error")
            and rec.get("consent") in ("asked and recorded", "off by policy")
            and rec.get("key_while_recording") == "opens" and rec.get("stopped") is True
            and rec.get("imported") == 1 and rec.get("key_after_import") == "missing"
            and rec.get("key_before_discard") == "opens"
            and rec.get("discarded") is True and rec.get("key_after_discard") == "missing"
            and rec.get("recordings_waiting") == 0 and rec.get("other_questions") == [],
        "audio key erased, text key kept": len(erased) == meetings >= 1 and all(
            e.get("audio_key") == "missing" and e.get("text_key") == "opens"
            and e.get("media_folder_gone") is True for e in erased),
        "nothing to block an update, database compacted": clean.get("blockers") == []
            and clean.get("freelist_pages") == 0 and clean.get("wal_bytes") == 0
            and clean.get("meetings_in_store") == 0,
        "no content left behind": res.get("found") == {}
            and res.get("unreadable") == [] and res.get("content_backups") == [],
        "only the workspace's own files left": res.get("unexpected_files") == [],
        # Every file left in the child's TEMP, APPDATA or user folder, all in
        # the base, is named; none is expected (residue.EXPLAINED).
        "nothing left in TEMP, APPDATA or the user folder":
            isinstance(res.get("temp_files_left"), list)
            and isinstance(res.get("redirected_files_left"), dict)
            and res.get("left_unexplained") == [],
        # The real LocalLow and model cache, by name only (P2).
        "nothing new outside the base unexplained":
            isinstance(res.get("outside_new"), dict) and res.get("outside_unexplained") == []
            and res.get("outside_unreadable") == [],
        "logs scanned": bool(res.get("logs_scanned")),
        "no crash dumps": dumps.get("dumps") == 0 and dumps.get("crash_files") == 0,
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
    ap.add_argument("--report-system-dumps", action="store_true",
                    help="also count new crash dumps of the runtime or the window in your own "
                    "CrashDumps and Windows Error Reporting folders (names and times only; "
                    "reported, never failed)")
    args = ap.parse_args()
    # A wrong --zip, or a folder that is not that zip's, stops now, not after the run.
    before = evidence.prepare(ap, args, version_dir=Path(args.version_dir))
    report = {"when": time.strftime("%Y-%m-%d %H:%M"), "cpu_count": os.cpu_count()}
    try:
        check(args.version_dir, args.cache, args.wav, report,
              system_dumps=args.report_system_dumps)
    except Exception as exc:
        report["error"] = residue.redact(f"{type(exc).__name__}: {exc}")
    report["failed"] = verdict(report)
    report["pass"] = not report["failed"]
    # Last: a report that would carry the Windows user name anywhere, the
    # file-name lists included, fails in place of being written (P3).
    report = residue.guarded(report)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if args.zip:
        ev = evidence.stamp_file(args.out, "packaged", args.zip, args.version_dir,
                                 report["pass"], before=before)
        return 0 if ev["pass"] else 1
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
