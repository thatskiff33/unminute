# tools/packaged_crash_check.py
# #28 crash evidence from an INSTALLED package: hard-kill the real app process
# (and its worker) around chunk commits, speaker identification, cleanup and a
# held cleanup, restart, and check that nothing is lost, duplicated or brought
# back. Also checks token removal (test credential only) and that the
# clipboard is untouched. Run it with the package's own runtime:
#
#   <install>\versions\<v>\runtime\python.exe tools\packaged_crash_check.py
#       <install>\versions\<v> <model cache> <recording.wav> <report.json> [seconds]
#
# Uses two throwaway LOCALAPPDATA profiles ("crash" and "reference"); no real
# workspace, model cache or saved token is touched. Synthetic audio only.

import ctypes
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

MARKER = "quokkalanternmeridian"    # distinctive text written into the transcript


def clip(wav, seconds, out):
    """The first `seconds` of a WAV file (keeps the check short)."""
    with wave.open(str(wav), "rb") as r, wave.open(str(out), "wb") as w:
        w.setparams(r.getparams())
        w.writeframes(r.readframes(int(seconds * r.getframerate())))


class Child:
    """One run of CHILD in the packaged runtime; status lines arrive on a queue."""

    def __init__(self, ctx, *args):
        self.p = subprocess.Popen([ctx["py"], str(ctx["script"]), *map(str, args)],
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
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.p.pid)], capture_output=True)
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
    return Child(ctx, "run", ctx["cache"], wav or "-").finish(timeout)


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
    r = subprocess.run([ctx["py"], str(ctx["script"]), "inspect", ctx["cache"], "-", MARKER],
                       capture_output=True, text=True, env=ctx["env"], cwd=ctx["app"])
    return json.loads(r.stdout.strip().splitlines()[-1])


def residue(profile):
    """Every file left in the profile, and any that still contain the marker."""
    names, hits = [], []
    for p in (profile / "whisper").rglob("*"):
        if p.is_file():
            names.append(p.relative_to(profile).as_posix())
            if MARKER.encode() in p.read_bytes():
                hits.append(names[-1])
    return {"files": sorted(names), "marker_found_in": hits}


def clipboard_sequence():
    return ctypes.WinDLL("user32").GetClipboardSequenceNumber()


def check(version_dir, cache, wav, report, seconds):
    runtime, app = Path(version_dir) / "runtime", Path(version_dir) / "app"
    base = Path(tempfile.mkdtemp(prefix="ww28crash-"))
    script = base / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    short = base / "clip.wav"
    clip(wav, seconds, short)

    def ctx(name):
        env = dict(os.environ)
        env.pop("HF_TOKEN", None)
        env["LOCALAPPDATA"] = str(base / name)
        return {"py": str(runtime / "python.exe"), "script": script, "env": env,
                "app": app, "cache": str(cache), "profile": base / name}

    crash, ref = ctx("crash"), ctx("reference")
    clip0 = clipboard_sequence()

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
    kills.append({"at": "after first chunk commit", "killed": c.kill()})
    c = Child(crash, "run", crash["cache"], "-")
    c.until(lambda s: s.get("committed", 0) >= max(2, total - 2), 1200)
    time.sleep(0.3)                       # land inside the next chunk's work
    kills.append({"at": f"chunk {max(2, total - 2)} of {total}", "killed": c.kill()})
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
    k = act(crash, "edit_review", kill_after=0)
    run_to_idle(crash)
    report["review_cleanup_crash"] = {"kill": k, "state": inspect(crash)}

    # 4. Crash just after the final confirmation (transcript cleanup), restart.
    k = act(crash, "final", kill_after=0.05)
    run_to_idle(crash)
    report["final_cleanup_crash"] = {"kill": k, "state": inspect(crash),
                                     "residue": residue(crash["profile"])}

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
    hold["residue"] = residue(ref["profile"])
    report["hold_race"] = hold

    # 6. Token removal with a test-only credential name.
    r = subprocess.run([crash["py"], str(script), "token", "-", "-", MARKER],
                       capture_output=True, text=True, env=crash["env"], cwd=app)
    report["token_removal"] = json.loads(r.stdout.strip().splitlines()[-1])

    report["clipboard_unchanged"] = clipboard_sequence() == clip0
    report["profiles"] = str(base)


CHILD = r'''
# Runs inside the packaged runtime. Everything is behind the __main__ guard:
# the worker is started with multiprocessing "spawn", which re-imports this file.
import hashlib, json, sqlite3, sys, time
sys.path.insert(0, ".")
import offline_guard
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
    t = time.time()
    while time.time() - t < 3000:
        say(**status(ws))
        if ws.wait_idle(0.25) and (ws.meetings() or wav == "-") and ws.wait_idle(1):
            break
    say(done=True, **status(ws))


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
    wal = root / "workspace.db-wal"          # before opening: closing may checkpoint
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    db = sqlite3.connect(str(root / "workspace.db"))
    db.row_factory = sqlite3.Row
    one = lambda q: db.execute(q).fetchone()[0]
    out = {"meetings": one("SELECT count(*) FROM meetings"),
           "phases": [r[0] for r in db.execute("SELECT phase FROM meetings")],
           "errors": [r[0] for r in db.execute("SELECT last_error FROM meetings "
                                               "WHERE last_error IS NOT NULL")],
           "chunks": {r[0]: r[1] for r in db.execute(
               "SELECT status, count(*) FROM chunks GROUP BY status")},
           "segments": one("SELECT count(*) FROM segments"),
           # The same chunk committed twice would show up as duplicates here.
           "duplicate_segments": one("SELECT count(*) FROM (SELECT version_id, chunk_idx, "
                                     "start_s, count(*) c FROM segments GROUP BY 1, 2, 3 "
                                     "HAVING c > 1)"),
           "attempts": {r[0] or "none": r[1] for r in db.execute(
               "SELECT result, count(*) FROM attempts GROUP BY result")},
           "speaker_runs": [r[0] for r in db.execute(
               "SELECT status FROM speaker_runs ORDER BY started_at")],
           "speaker_turns": one("SELECT count(*) FROM speaker_turns"),
           "edits": one("SELECT count(*) FROM edits"),
           "cleanup_jobs": [dict(r) for r in db.execute(
               "SELECT phase, state, error FROM cleanup_jobs")],
           "active_holds": one("SELECT count(*) FROM holds WHERE released_at IS NULL"),
           "receipts": [r[0] for r in db.execute("SELECT result FROM receipts")],
           "audio_files": len(list((root / "media").rglob("audio.f32"))),
           "freelist_pages": one("PRAGMA freelist_count")}
    text = " ".join(r[0] for r in db.execute(
        "SELECT text FROM segments ORDER BY version_id, start_s, id"))
    out["text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    out["marker_in_edits"] = bool(one("SELECT count(*) FROM edits WHERE text LIKE '%"
                                      + marker + "%'"))
    db.close()
    out["wal_bytes"] = wal_bytes
    return out


def token(marker):
    from workspace import credentials
    target = "WhisperTranscriberCrashCheck/" + marker   # never the real token's name
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
    ws = Workspace(cache_dir=cache, start=False, battery=False)
    try:
        ws.settings.update(onboarded=True, speakers=True)
        ws.start()
        if mode == "run":
            run(ws, arg)
        else:
            act(ws, arg, rest[0])
    finally:
        ws.close()


if __name__ == "__main__":
    main()
'''


def main():
    version_dir, cache, wav, out = sys.argv[1:5]
    seconds = float(sys.argv[5]) if len(sys.argv) > 5 else 240
    report = {"when": time.strftime("%Y-%m-%d %H:%M"), "audio_seconds": seconds}
    try:
        check(version_dir, cache, wav, report, seconds)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    Path(out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
