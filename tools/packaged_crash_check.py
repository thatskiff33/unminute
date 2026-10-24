# tools/packaged_crash_check.py
# #28 crash evidence from an INSTALLED package: hard-kill the real app process
# (and its worker) around chunk commits, speaker identification, cleanup and a
# held cleanup, restart, and check that nothing is lost, duplicated or brought
# back. Also checks token removal (test credential only) and that the
# clipboard sequence number is unchanged (do not copy anything while it runs).
# Run it with the package's own runtime:
#
#   <install>\versions\<v>\runtime\python.exe tools\packaged_crash_check.py
#       <install>\versions\<v> <model cache> <recording.wav> <report.json> [seconds]
#       [--zip <the zip it was installed from>]
#
# With --zip the report is release evidence (tools/evidence.py): it names the
# zip and says whether the version folder still holds exactly its files. The app
# runs with the Start menu shortcut's flags (-I -B -X disable-remote-debug).
#
# #111 phase C (C7) adds: kills placed by what the parent can see from outside
# (no test seam ships in the package): during an encrypted import (once the
# meeting's audio.partial holds its first 12 blocks, about 12 s, so the scan
# windows from its first 8 s were all decoded and written), at two landings
# around audio cleanup (each landing's state classified: the audio key and
# the audio both there, or the key gone and the audio recorded as removed,
# never the key gone while the audio shows as available) and while recording
# synthetic audio (once enough is saved). After final cleanup the profile
# holds only the workspace's own files and none of the content
# (tools/residue.py, with its positive control). Crash dumps under the base
# fail the report; the app's own crash files are counted, not failed, because
# this check kills the app on purpose.
#
# Uses throwaway LOCALAPPDATA profiles ("crash", "reference", "landing",
# "import" and "recording"), each with its own TEMP, in one base folder deleted
# at the end; no real workspace or saved token is touched. The model cache is
# used as given: pass a copy if it must stay untouched. Synthetic audio only.
# The report ends with "pass" and the list of criteria that failed; exit code 1
# if any did.

import argparse
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

# evidence.py, residue.py and windows_powershell.py, also under -I
sys.path.append(str(Path(__file__).resolve().parent))
import evidence  # noqa: E402
import residue  # noqa: E402
import windows_powershell  # noqa: E402

MARKER = "quokkalanternmeridian"    # distinctive text written into the transcript
FLAGS = ["-I", "-B", "-X", "disable-remote-debug"]   # as the Start menu shortcut starts it
WINDOW_BYTES = 4096 * 4             # a window of decoded audio (float32)
RECORDING_WINDOW_BYTES = 2048       # from the middle of a synthetic block (int16)
IMPORT_MINUTES = 60                 # the synthetic import that is killed part-way
# Its scan windows come from its first IMPORT_WINDOWS_S seconds, and the kill
# waits until audio.partial holds at least IMPORT_KILL_BLOCKS sealed blocks
# (workspace/audiocrypt.py: a 64-byte header and its 16-byte tag, then blocks
# of 16,384 float32 frames, about 1 s, each with a 16-byte tag). So every
# window has been decoded and written before the kill, and "none of them
# found" means something (review of #335).
IMPORT_WINDOWS_S = 8
IMPORT_KILL_BLOCKS = 12
AUDIO_DATA_START = 64 + 16
AUDIO_BLOCK_BYTES = 16384 * 4 + 16
IMPORT_KILL_BYTES = AUDIO_DATA_START + IMPORT_KILL_BLOCKS * AUDIO_BLOCK_BYTES
RECORDING_SAVED_S = 12              # recorded before the kill: at least two 5 s chunks
LANDINGS = (0, 0.05)                # seconds after the review confirmation


def clip(wav, seconds, out):
    """The first `seconds` of a WAV file (keeps the check short)."""
    with wave.open(str(wav), "rb") as r, wave.open(str(out), "wb") as w:
        w.setparams(r.getparams())
        w.writeframes(r.readframes(int(seconds * r.getframerate())))


def synthetic_wav(out, minutes, rate=16000):
    """A long synthetic recording (random 16-bit noise), for the import that
    is killed part-way. Written a minute at a time."""
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for _ in range(minutes):
            w.writeframes(os.urandom(rate * 60 * 2))


def watch_for(folder, name, proc, timeout, min_size=0):
    """Wait until a file called `name` of at least min_size bytes is anywhere
    under folder (the parent sees only what is on disk), or the process ends.
    Returns its size then (stat only), or None."""
    end = time.time() + timeout
    while time.time() < end and proc.poll() is None:
        for p in (folder.rglob(name) if folder.exists() else ()):
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size >= min_size:
                return size
        time.sleep(0.05)
    return None


def windows_of(ctx, wav, out):
    """Windows of a clip's decoded audio, decoded in memory by the child in
    the packaged runtime, written to out (in the base, outside any profile)."""
    r = subprocess.run([ctx["py"], *FLAGS, str(ctx["script"]), "windows", ctx["cache"],
                        str(wav), str(out)],
                       capture_output=True, text=True, env=ctx["env"], cwd=ctx["app"])
    if r.returncode or not Path(out).exists():
        raise RuntimeError("the audio windows could not be made: " + r.stderr[-1500:])


class Child:
    """One run of CHILD in the packaged runtime; status lines arrive on a queue."""

    def __init__(self, ctx, *args):
        # taskkill by its full path in the system folder, never by name (#253);
        # found first, so a missing one stops the check before the child starts.
        self.taskkill = windows_powershell.system_program("taskkill.exe")
        self.p = subprocess.Popen([ctx["py"], *FLAGS, str(ctx["script"]), *map(str, args)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env=ctx["env"], cwd=ctx["app"])
        self.lines, self.err = queue.Queue(), []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.err.extend(self.p.stderr), daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                self.lines.put(json.loads(line))
            except ValueError:
                pass
        self.lines.put(None)

    def until(self, test, timeout):
        """Wait for a status line satisfying `test`; returns it, or None at exit."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                s = self.lines.get(timeout=1)
            except queue.Empty:
                continue
            if s is None:
                if self.p.wait(60):
                    raise RuntimeError("child failed: " + "".join(self.err)[-1500:])
                return None
            if test(s):
                return s
        raise TimeoutError("child did not reach the expected state")

    def kill(self):
        """Hard kill of the app process and its worker, like a crash or power cut."""
        alive = self.p.poll() is None
        subprocess.run([self.taskkill, "/F", "/T", "/PID", str(self.p.pid)], capture_output=True,
                       stdin=subprocess.DEVNULL, env=windows_powershell.environment())
        self.p.wait(30)
        return alive

    def finish(self, timeout):
        last = None
        while True:
            s = self.until(lambda s: True, timeout)
            if s is None:
                break
            last = s
        self.p.wait(60)
        if self.p.returncode:
            raise RuntimeError("child failed: " + "".join(self.err)[-1500:])
        return last


def run_to_idle(ctx, wav=None, timeout=3000):
    last = Child(ctx, "run", ctx["cache"], wav or "-").finish(timeout)
    if not (last and last.get("idle")):
        raise RuntimeError(f"the app did not become idle: {last}")
    return last


def act(ctx, action, kill_after=None):
    """Perform an action; optionally hard-kill `kill_after` seconds after it."""
    c = Child(ctx, "act", ctx["cache"], action, MARKER)
    s = c.until(lambda s: "acted" in s, 600)
    if s is None:
        raise RuntimeError("action failed: " + "".join(c.err)[-1500:])
    if kill_after is None:
        c.finish(600)
        return {"result": s["acted"], "killed": False}
    time.sleep(kill_after)
    return {"result": s["acted"], "killed": c.kill()}


def inspect(ctx):
    r = subprocess.run([ctx["py"], *FLAGS, str(ctx["script"]), "inspect", ctx["cache"], "-",
                        MARKER],
                       capture_output=True, text=True, env=ctx["env"], cwd=ctx["app"])
    return json.loads(r.stdout.strip().splitlines()[-1])


def landing(state):
    """Where a kill around audio cleanup left the meeting (C3c's order): the
    audio key and the audio both there ("before"), or the key gone and the
    audio recorded as removed ("after"). Anything else is "inconsistent",
    the key gone while the audio still shows as available above all."""
    key, removed = state.get("audio_key"), state.get("audio_removed")
    if key == "opens" and removed is False and state.get("audio_files") == 1:
        return "before"
    if key == "missing" and removed is True:
        return "after"
    return "inconsistent"


def leftovers(base, ctx, names, patterns):
    """After final cleanup: files in the profile, any holding the marker or
    the audio windows (with the scanner's positive control), and any that
    are not the workspace's own."""
    profile = ctx["profile"] / "Unminute"
    files = sorted(p.relative_to(ctx["profile"]).as_posix()
                   for p in profile.rglob("*") if p.is_file()) if profile.exists() else []
    found = residue.find([profile, ctx["temp"]], patterns, base)
    prefix = Path(os.path.relpath(profile / "workspace", base)).as_posix()
    return {"files": files, "found": found["hits"], "unreadable": found["unreadable"],
            "control_missed": residue.control(base, patterns),
            "unexpected_files": residue.unexpected(profile, profile / "workspace", names),
            "logs_scanned": len(residue.logs_scanned(found["scanned"], names, prefix))}


def clipboard_sequence():
    return ctypes.WinDLL("user32").GetClipboardSequenceNumber()


def check(version_dir, cache, wav, report, seconds):
    runtime, app = Path(version_dir) / "runtime", Path(version_dir) / "app"
    base = Path(tempfile.mkdtemp(prefix="umcrash-"))
    script = base / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    short = base / "clip.wav"
    clip(wav, seconds, short)
    tools = str(Path(__file__).resolve().parent)
    evidence.add_app(app)                 # the workspace's own file names, from the package
    names = residue.names()

    def ctx(name):
        env = dict(os.environ)
        env.pop("HF_TOKEN", None)
        env["LOCALAPPDATA"] = str(base / name)
        temp = base / name / "temp"       # TEMP and TMP in the throwaway profile
        temp.mkdir(parents=True, exist_ok=True)
        env["TEMP"] = env["TMP"] = str(temp)
        return {"py": str(runtime / "python.exe"), "script": script, "env": env,
                "app": app, "cache": str(cache), "profile": base / name, "temp": temp,
                "tools": tools}

    crash, ref = ctx("crash"), ctx("reference")
    clip0 = clipboard_sequence()
    # Windows of the clip's decoded audio, for the scans.
    windows_of(ref, short, base / "windows.bin")
    patterns = {**residue.text_patterns(MARKER),
                **residue.split_windows((base / "windows.bin").read_bytes(), WINDOW_BYTES)}

    # 1. Reference: one uninterrupted run.
    run_to_idle(ref, short)
    report["reference"] = inspect(ref)

    # 2. Crashes during transcription (after the first commit and near the
    #    end) and during speaker identification, each followed by a restart.
    kills = []
    c = Child(crash, "run", crash["cache"], short)
    first = c.until(lambda s: s.get("committed", 0) >= 1, 1200)
    if first is None:
        raise RuntimeError("the run finished before the first kill point")
    total = first["chunks"]
    kills.append({"at": f"after {first['committed']} of {total} chunks committed",
                  "killed": c.kill()})
    c = Child(crash, "run", crash["cache"], "-")
    s = c.until(lambda s: s.get("committed", 0) >= max(2, total - 2), 1200)
    time.sleep(0.3)                       # land inside the next chunk's work
    kills.append({"at": f"after {s['committed']} of {total} chunks committed",
                  "killed": c.kill()})
    c = Child(crash, "run", crash["cache"], "-")
    c.until(lambda s: s.get("busy") == "speakers", 1200)
    time.sleep(3)
    kills.append({"at": "during speaker identification", "killed": c.kill()})
    run_to_idle(crash)
    after = inspect(crash)
    report["processing_crashes"] = {
        "kills": kills, "state": after,
        "same_text_as_reference": after["text_sha256"] == report["reference"]["text_sha256"],
        "same_segment_count": after["segments"] == report["reference"]["segments"]}

    # 3. Crash straight after the review confirmation (audio cleanup), restart.
    #    A second meeting ("landing") is killed 50 ms later. Each landing's
    #    state is read before the restart, then the restart must finish it.
    landings = []
    land = ctx("landing")
    run_to_idle(land, short)
    for profile, after in ((crash, LANDINGS[0]), (land, LANDINGS[1])):
        k = act(profile, "edit_review", kill_after=after)
        killed_state = inspect(profile)
        run_to_idle(profile)
        landings.append({"after_s": after, "kill": k, "landed": landing(killed_state),
                         "state": inspect(profile)})
    report["review_cleanup_crash"] = {"kill": landings[0]["kill"],
                                      "state": landings[0]["state"], "landings": landings}

    # 4. Crash just after the final confirmation (transcript cleanup), restart.
    k = act(crash, "final", kill_after=0.05)
    run_to_idle(crash)
    report["final_cleanup_crash"] = {"kill": k, "state": inspect(crash),
                                     "residue": leftovers(base, crash, names, patterns)}

    # 5. Hold race on the reference meeting: hold, review, crash; restart;
    #    release (nothing may happen); confirm; then final cleanup.
    hold = {"crash": act(ref, "hold_review", kill_after=0)}
    run_to_idle(ref)
    hold["after_restart"] = inspect(ref)
    hold["release"] = act(ref, "release")
    run_to_idle(ref)
    hold["after_release"] = inspect(ref)
    hold["confirm"] = act(ref, "confirm")
    run_to_idle(ref)
    hold["after_confirm"] = inspect(ref)
    act(ref, "final")
    run_to_idle(ref)
    hold["after_final"] = inspect(ref)
    hold["residue"] = leftovers(base, ref, names, patterns)
    report["hold_race"] = hold

    # 6. Kill during the encrypted import of a long synthetic recording, once
    #    its audio.partial holds the first IMPORT_KILL_BLOCKS blocks; at the
    #    next start the import is discarded and nothing of it is left: no
    #    window of its first IMPORT_WINDOWS_S seconds, all decoded before the
    #    kill, is anywhere in the profile or its TEMP.
    imp = ctx("import")
    long_wav = base / "long.wav"
    synthetic_wav(long_wav, IMPORT_MINUTES)
    start = base / "long-start.wav"
    clip(long_wav, IMPORT_WINDOWS_S, start)   # its first seconds decode the same way
    windows_of(imp, start, base / "import-windows.bin")
    c = Child(imp, "run", imp["cache"], long_wav)
    size = watch_for(imp["profile"], "audio.partial", c.p, 600, IMPORT_KILL_BYTES)
    killed = c.kill()
    run_to_idle(imp)
    long_wav.unlink()
    import_patterns = residue.split_windows((base / "import-windows.bin").read_bytes(),
                                            WINDOW_BYTES)
    found = residue.find([imp["profile"]], import_patterns, base)
    report["import_crash"] = {"partial_seen": size is not None, "partial_bytes": size,
                              "killed": killed,
                              "state": inspect(imp), "windows": len(import_patterns),
                              "control_missed": residue.control(base, import_patterns),
                              "found": found["hits"], "unreadable": found["unreadable"]}

    # 7. Kill while recording synthetic audio once enough is saved; at the
    #    next start the recording waits for Keep, and keeping it makes one
    #    meeting from the saved chunks, with no partial chunk left behind.
    rec = ctx("recording")
    c = Child(rec, "record", rec["cache"], rec["tools"], MARKER, str(base))
    s = c.until(lambda s: (s.get("recording_seconds") or 0) >= RECORDING_SAVED_S, 300)
    killed = c.kill()
    kept = Child(rec, "keep", rec["cache"], rec["tools"], MARKER, str(base)).finish(1800)
    rec_file = base / "recording-windows.bin"
    rec_patterns = (residue.split_windows(rec_file.read_bytes(), RECORDING_WINDOW_BYTES)
                    if rec_file.exists() else {})
    found = residue.find([rec["profile"]], rec_patterns, base)
    report["recording_crash"] = {
        "seconds_at_kill": (s or {}).get("recording_seconds"), "killed": killed,
        "kept": kept, "state": inspect(rec), "windows": len(rec_patterns),
        "control_missed": residue.control(base, rec_patterns) if rec_patterns else ["none"],
        "found": found["hits"], "unreadable": found["unreadable"],
        "partial_chunks_left": len(list(rec["profile"].rglob("*.tmp")))}

    # 6. Token removal with a test-only credential name.
    r = subprocess.run([crash["py"], *FLAGS, str(script), "token", "-", "-", MARKER],
                       capture_output=True, text=True, env=crash["env"], cwd=app)
    report["token_removal"] = json.loads(r.stdout.strip().splitlines()[-1])

    report["clipboard_sequence_unchanged"] = clipboard_sequence() == clip0
    # Crash files, by name and size only: dumps fail; the app's own crash
    # files are expected here (the check kills the app) and only counted.
    report["dumps"] = residue.dumps(base)
    report["failed"] = verdict(report)
    report["pass"] = not report["failed"]
    shutil.rmtree(base, ignore_errors=True)
    report["profiles_removed"] = not base.exists()


def verdict(r):
    """Names of the criteria that failed (empty when everything passed)."""
    ref, pc = r["reference"], r["processing_crashes"]
    rv, fin, h = r["review_cleanup_crash"], r["final_cleanup_crash"], r["hold_race"]
    imp, rec = r["import_crash"], r["recording_crash"]
    clean = (lambda st, res: st["meetings"] == 0 and st["segments"] == 0
             and st["edits"] == 0 and st["audio_files"] == 0 and not res["found"]
             and not res["unreadable"] and st["freelist_pages"] == 0
             and st["wal_bytes"] == 0 and st["blockers"] == [])
    own_files = (lambda res: res["unexpected_files"] == [] and res["control_missed"] == []
                 and res["logs_scanned"] > 0)
    landings = rv.get("landings") or []
    kept = rec.get("kept") or {}
    checks = {
        "reference transcribed with speakers": ref["segments"] > 0
            and ref["speaker_runs"] == ["done"] and not ref["errors"],
        "every kill landed on a live process": all(k["killed"] for k in pc["kills"])
            and len(landings) == len(LANDINGS) and all(x["kill"]["killed"] for x in landings)
            and fin["kill"]["killed"] and h["crash"]["killed"] and imp["killed"]
            and rec["killed"],
        "same transcript as reference after crashes": pc["same_text_as_reference"]
            and pc["same_segment_count"] and pc["state"]["duplicate_segments"] == 0
            and not pc["state"]["errors"],
        "interrupted work recorded": pc["state"]["attempts"].get("interrupted", 0) >= 1
            and pc["state"]["speaker_runs"][-1] == "done",
        "audio removed, transcript kept after review crash": rv["state"]["audio_files"] == 0
            and rv["state"]["marker_in_edits"] and rv["state"]["segments"] == ref["segments"],
        # #111 C3c's order, wherever the kill landed; the restart finishes it.
        "every cleanup landing consistent, then finished": len(landings) == len(LANDINGS)
            and all(x["landed"] in ("before", "after") for x in landings)
            and all(x["state"]["audio_key"] == "missing" and x["state"]["audio_removed"]
                    and x["state"]["audio_files"] == 0
                    and x["state"]["receipts"].count("audio removed") == 1
                    for x in landings),
        "nothing left after final cleanup crash": clean(fin["state"], fin["residue"]),
        "hold kept audio through a crash": h["after_restart"]["audio_files"] == 1
            and h["after_restart"]["active_holds"] == 1,
        "release alone removed nothing": h["after_release"]["audio_files"] == 1
            and h["after_release"]["cleanup_jobs"][0]["state"] == "awaiting_confirmation",
        "confirmation then removed audio": h["after_confirm"]["audio_files"] == 0,
        "nothing left after held meeting's final cleanup": clean(h["after_final"],
                                                                 h["residue"]),
        "only the workspace's own files after final cleanup": own_files(fin["residue"])
            and own_files(h["residue"]),
        "interrupted import discarded, nothing of it left": imp["partial_seen"]
            and (imp.get("partial_bytes") or 0) >= IMPORT_KILL_BYTES
            and imp["state"]["meetings"] == 0 and imp["state"]["audio_files"] == 0
            and imp["state"]["blockers"] == [] and imp["windows"] == 8
            and imp["control_missed"] == [] and not imp["found"] and not imp["unreadable"],
        "recording kept after a crash, nothing plain left": kept.get("recovered") is True
            and (kept.get("seconds") or 0) >= 5 and kept.get("imported") == 1
            and kept.get("key_after_import") == "missing"
            and rec["state"]["meetings"] == 1 and "recordings" not in rec["state"]["blockers"]
            and rec["windows"] == 8 and rec["control_missed"] == []
            and not rec["found"] and not rec["unreadable"] and rec["partial_chunks_left"] == 0,
        "no crash dumps": (r.get("dumps") or {}).get("dumps") == 0,
        "test token removed": r["token_removal"]["saved"]
            and not r["token_removal"]["present_after_removal"]
            and not r["token_removal"]["loaded_after_removal"],
        "clipboard sequence unchanged": r["clipboard_sequence_unchanged"],
    }
    return [name for name, ok in checks.items() if not ok]


CHILD = r'''
# Runs inside the packaged runtime. Everything is behind the __main__ guard:
# the worker is started with multiprocessing "spawn", which re-imports this file.
import hashlib, json, sys, time
sys.path.insert(0, ".")
from workspace import offline_guard
offline_guard.disable_telemetry(); offline_guard.install()


def say(**kw):
    print(json.dumps(kw), flush=True)


def status(ws):
    st = {"busy": ws._busy[1] if ws._busy else None, "meetings": len(ws.meetings())}
    rows = ws.store._all("SELECT status FROM chunks")
    st["chunks"] = len(rows)
    st["committed"] = sum(r["status"] == "done" for r in rows)     # committed chunks
    return st


def run(ws, wav):
    if wav != "-" and not ws.meetings():
        ws.import_paths([wav])
    t, idle = time.time(), False
    while time.time() - t < 3000:
        say(**status(ws))
        if ws.wait_idle(0.25) and (ws.meetings() or wav == "-") and ws.wait_idle(1):
            idle = True
            break
    say(done=True, idle=idle, **status(ws))


def act(ws, action, marker):
    m = ws.meetings()[0]
    mid = m["id"]
    if action in ("edit_review", "hold_review"):
        if action == "hold_review":
            ws.add_hold(mid, "audio", "Crash check hold")
        items = ws.transcript(mid)["items"]
        ws.edit(mid, items[0]["id"], "Marker " + marker + " for the crash check")
        say(acted=ws.confirm_reviewed(mid))
    elif action == "final":
        say(acted=ws.confirm_final_saved(mid))
    elif action == "release":
        hid = ws.store._one("SELECT id FROM holds WHERE released_at IS NULL")["id"]
        say(acted=ws.release_hold(hid))
    elif action == "confirm":
        jobs = ws.store._all("SELECT id FROM cleanup_jobs WHERE state = "
                             "'awaiting_confirmation'")
        for j in jobs:
            ws.confirm_cleanup(j["id"])
        say(acted=len(jobs))
    t = time.time()
    while time.time() - t < 300 and not ws.wait_idle(0.25):
        say(**status(ws))


def inspect(root, marker):
    from workspace import media
    from workspace.diag import DB_FILE
    from workspace.store import Store
    wal = root / (DB_FILE + "-wal")          # before opening: closing may checkpoint
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    # Through the store, which opens content (#111). housekeeping=False: opening
    # must not mark running attempts interrupted, which this check measures.
    store = Store(root / DB_FILE, housekeeping=False)
    try:
        one = lambda q: store._one(q)[0]
        rows = store._all
        out = {"meetings": one("SELECT count(*) FROM meetings"),
               "phases": [r[0] for r in rows("SELECT phase FROM meetings")],
               "errors": [m["last_error"] for m in store.meetings()
                          if m["last_error"] is not None],
               "chunks": {r[0]: r[1] for r in rows(
                   "SELECT status, count(*) FROM chunks GROUP BY status")},
               "segments": one("SELECT count(*) FROM segments"),
               # The same chunk committed twice would show up as duplicates here.
               "duplicate_segments": one("SELECT count(*) FROM (SELECT version_id, chunk_idx, "
                                         "start_s, count(*) c FROM segments GROUP BY 1, 2, 3 "
                                         "HAVING c > 1)"),
               "attempts": {r[0] or "none": r[1] for r in rows(
                   "SELECT result, count(*) FROM attempts GROUP BY result")},
               "speaker_runs": [r[0] for r in rows(
                   "SELECT status FROM speaker_runs ORDER BY started_at")],
               "speaker_turns": one("SELECT count(*) FROM speaker_turns"),
               "edits": one("SELECT count(*) FROM edits"),
               "cleanup_jobs": [dict(r) for r in rows(
                   "SELECT phase, state, error FROM cleanup_jobs")],
               "active_holds": one("SELECT count(*) FROM holds WHERE released_at IS NULL"),
               "receipts": [r[0] for r in rows("SELECT result FROM receipts")],
               "audio_files": len(list((root / "media").rglob(media.AUDIO_FILE))),
               "freelist_pages": one("PRAGMA freelist_count")}
        # The first meeting's audio key (whether it opens, never the key) and
        # whether its audio is recorded as removed (#111 C7: cleanup landings).
        first = store.meetings(content=False)[:1]
        out["audio_key"] = out["audio_removed"] = None
        if first:
            from workspace.store import KeyMissing
            try:
                store.meeting_key(first[0]["id"], "audio")
                out["audio_key"] = "opens"
            except KeyMissing:
                out["audio_key"] = "missing"
            except Exception:
                out["audio_key"] = "damaged"
            out["audio_removed"] = first[0]["audio_removed_at"] is not None
        # The generated text of every version, in version then time order, and
        # whether any edit holds the marker (case ignored, as SQL LIKE did).
        texts, marked = [], False
        for (vid,) in rows("SELECT DISTINCT version_id FROM segments ORDER BY version_id"):
            for r in store.transcript(vid, words=False):
                texts.append(r["generated"])
                marked = marked or (bool(r["edited"])
                                    and marker.casefold() in r["text"].casefold())
        out["text_sha256"] = hashlib.sha256(" ".join(texts).encode()).hexdigest()
        out["marker_in_edits"] = marked
    finally:
        store.close()
    out["wal_bytes"] = wal_bytes
    from workspace import update
    out["blockers"] = [b["kind"] for b in update.blockers(root / DB_FILE)]
    return out


WINDOW_SAMPLES, WINDOWS, RECORDING_WINDOW_BYTES = 4096, 8, 2048


def windows(wav, out):
    """Windows of a clip's decoded audio (float32), as the app decodes it, at
    fixed offsets, each moved on until it is not near-silence; decoded in
    memory, written to out."""
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
            if np.unique(w).size >= WINDOW_SAMPLES // 2:
                chosen.append(w.tobytes())
                break
            at += WINDOW_SAMPLES
    Path(out).write_bytes(b"".join(chosen))


def synthetic_input(tools):
    """The synthetic microphone of tools/perf_check.py, in this process only:
    no audio device opens and the power scheme is never changed."""
    sys.path.append(tools)
    import perf_check as pc
    from workspace import power, recorder
    recorder.list_devices = lambda: [dict(pc.SYNTHETIC_DEVICE)]

    def never(*a, **k):
        raise RuntimeError("the crash check never changes the power scheme")
    power._PowrProf.write = power._PowrProf.apply = never
    return pc


def recording_windows(pc, base):
    """The middle of every fifth synthetic block that is not near-silence."""
    from pathlib import Path
    chosen = []
    for n, b in enumerate(pc.synthetic_blocks()):
        raw = b.tobytes()
        mid = raw[(len(raw) - RECORDING_WINDOW_BYTES) // 2:][:RECORDING_WINDOW_BYTES]
        if n % 5 == 0 and len(mid) == RECORDING_WINDOW_BYTES and len(set(mid)) > 200:
            chosen.append(mid)
        if len(chosen) == WINDOWS:
            break
    Path(base, "recording-windows.bin").write_bytes(b"".join(chosen))


def record(ws, pc, base):
    """Record synthetic audio through the bridge (the app's own recording
    question, answered yes) and report its length until killed."""
    from workspace import api, consent
    confirm, _asked = pc.consent_answerer(consent.RECORDING_TITLE)
    bridge = api.make_api(ws, lambda: None, confirm=confirm)
    recording_windows(pc, base)
    r = bridge.start_recording(0, pc.SYNTHETIC_DEVICE["name"])
    if not r.get("ok"):
        say(error="the recording did not start")
        return
    t = time.time()
    while time.time() - t < 600:              # killed long before this
        rec = ws.recording_status().get("recording")
        say(recording_seconds=rec["seconds"] if rec else 0)
        time.sleep(0.5)


def keep(ws):
    """After the crash: the recording waits for Keep; keep it (as the page's
    Keep does) and wait for its meeting. Key bytes are never read out."""
    from workspace.store import KeyMissing
    t = time.time()
    report = None
    while time.time() - t < 120 and report is None:
        report = next((x for x in ws.recording_status()["reports"]
                       if x.get("recovered") and x.get("pending")), None)
        time.sleep(0.25)
    if report is None:
        say(recovered=False)
        return
    rid = report["pending"]
    ws.retry_recording_import(report["id"])
    t = time.time()
    while time.time() - t < 1500 and not (ws.meetings() and ws.wait_idle(5)):
        time.sleep(1)
    try:
        ws.store.recording_key(rid)
        key = "opens"
    except KeyMissing:
        key = "missing"
    except Exception:
        key = "damaged"
    say(recovered=True, seconds=report.get("seconds"), imported=len(ws.meetings()),
        key_after_import=key)


def token(marker):
    from workspace import credentials
    target = "UnminuteCrashCheck/" + marker   # never the real token's name
    credentials.save_token("hf_testonly" + marker, target=target)
    saved = credentials.has_token(target=target)
    credentials.remove_token(target=target)
    return {"saved": saved, "present_after_removal": credentials.has_token(target=target),
            "loaded_after_removal": credentials.load_token(target=target) is not None}


def main():
    mode, cache, arg, *rest = sys.argv[1:]
    from workspace.app import Workspace, default_root
    if mode == "inspect":
        say(**inspect(default_root(), rest[0]))
        return
    if mode == "token":
        say(**token(rest[0]))
        return
    if mode == "windows":
        windows(arg, rest[0])
        return
    pc = synthetic_input(arg) if mode in ("record", "keep") else None
    ws = Workspace(cache_dir=cache, start=False, battery=False)
    if pc is not None:
        blocks = pc.synthetic_blocks()
        ws.open_stream = lambda device, rate, callback, finished: pc.PacedStream(
            rate, callback, finished, blocks=blocks)
    try:
        ws.settings.update(onboarded=True, speakers=True,
                           **({"microphone": True} if pc is not None else {}))
        ws.start()
        if mode == "run":
            run(ws, arg)
        elif mode == "record":
            record(ws, pc, rest[1])
        elif mode == "keep":
            keep(ws)
        else:
            act(ws, arg, rest[0])
    finally:
        ws.close()


if __name__ == "__main__":
    main()
'''


def main():
    ap = argparse.ArgumentParser(description="Crash evidence from an installed package (#28).",
                                 allow_abbrev=False)
    ap.add_argument("version_dir", help=r"the installed version folder (<install>\versions\<v>)")
    ap.add_argument("cache", help="a model cache with the models already downloaded")
    ap.add_argument("wav", help="a non-sensitive recording")
    ap.add_argument("out", help="where to write the JSON report")
    ap.add_argument("seconds", nargs="?", type=float, default=240,
                    help="how much of the recording to use (default 240)")
    ap.add_argument("--zip", help="the zip installed in version_dir: records it as release "
                    "evidence (tools/evidence.py); exit 1 unless that evidence passes")
    args = ap.parse_args()
    # A wrong --zip, or a folder that is not that zip's, stops now, not after the run.
    before = evidence.prepare(ap, args, version_dir=Path(args.version_dir))
    report = {"when": time.strftime("%Y-%m-%d %H:%M"), "audio_seconds": args.seconds}
    try:
        check(args.version_dir, args.cache, args.wav, report, args.seconds)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["failed"] = [f"the check stopped ({type(exc).__name__})"]
        report["pass"] = False
    passed = report.get("pass") is True and not report.get("error")
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if args.zip:
        ev = evidence.stamp_file(args.out, "crash", args.zip, args.version_dir, passed,
                                 before=before)
        return 0 if ev["pass"] else 1
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
