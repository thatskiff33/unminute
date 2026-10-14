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
import queue
import threading
import time
from pathlib import Path

import asr_engines
from workspace import (cleanup, clipboard, credentials, handoff, holds, intake, media,
                       recorder,
                       resources, runner, setup, speaker_names, speakers)
from workspace.chunking import plan_chunks
from workspace.safe_paths import UnsafePathError, validate_input_file
from workspace.store import Store, StoreError, default_root
from workspace.worker import WorkerEngine, WorkerHandle, safe_message

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".flac",
                    ".ogg", ".aac", ".wma", ".webm", ".avi")
MAX_IMPORT_BYTES = 8 << 30          # 8 GB: several hours of video
MAX_CLIP_S = 30.0
MAX_EDIT_CHARS = 5000
MAX_TITLE_CHARS = 200
FAILED_RETRY_S = 60.0               # a failed cleanup is retried at most this often
RECEIPT_PURGE_S = 3600.0
SR = 16000

DEFAULT_SETTINGS = {
    "mode": resources.QUIET,                       # quiet | faster
    "model": asr_engines.RECOMMENDED_PARAKEET,
    "speakers": True,                              # separate speakers after transcription
    "battery_override": False,                     # process automatically on battery
    "onboarded": False,                            # guided setup finished or skipped (#19)
    "microphone": False,                           # microphone recording (#30), off by default
}


class WorkspaceError(ValueError):
    """A refused request. The message is safe to show in the UI."""


def _clean_text(text, limit):
    if not isinstance(text, str):
        raise WorkspaceError("Expected text.")
    text = "".join(ch for ch in text if ch in "\n\t" or ch >= " ").strip()
    if len(text) > limit:
        raise WorkspaceError(f"That is too long (limit {limit} characters).")
    return text


class Settings:
    """Configuration only (no content), kept beside the database."""

    def __init__(self, path):
        self.path = Path(path)
        self.values = dict(DEFAULT_SETTINGS)
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.values.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.values[key]

    def update(self, **changes):
        for key, value in changes.items():
            if key not in DEFAULT_SETTINGS:
                raise WorkspaceError("Unknown setting.")
            if key == "mode" and value not in (resources.QUIET, resources.FASTER):
                raise WorkspaceError("Choose Quiet or Faster.")
            if key == "model" and not (asr_engines.is_parakeet(value)
                                       or asr_engines.is_whisper(value)
                                       or str(value).startswith("test:")):
                raise WorkspaceError("Unknown speech model.")
            if (key in ("speakers", "battery_override", "onboarded", "microphone")
                    and not isinstance(value, bool)):
                raise WorkspaceError("Expected on or off.")
            self.values[key] = value
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class Workspace:
    def __init__(self, root=None, cache_dir=None, *, speaker_model="pyannote",
                 idle_unload_s=None, battery=None, start=True):
        self.root = Path(root or default_root())
        self.root.mkdir(parents=True, exist_ok=True)
        self.media = self.root / "media"
        self.store = Store(self.root / "workspace.db")
        self.settings = Settings(self.root / "settings.json")
        self.templates = handoff.Templates(self.root / "templates.json")
        self.speaker_model = speaker_model
        self._battery = battery           # tests: fixed value instead of asking Windows
        kw = {} if idle_unload_s is None else {"idle_unload_s": idle_unload_s}
        self.cache_dir = Path(cache_dir or self.root / "models")
        self.handle = WorkerHandle(self.cache_dir,
                                   quiet=self.settings["mode"] == resources.QUIET, **kw)
        # Downloads replace model files: the inference worker is unloaded first.
        self.downloads = setup.Downloads(self.cache_dir, before_install=self.handle.shutdown)
        self.downloads.after = lambda: self._wake.set()    # resume processing afterwards
        self._wake = threading.Event()
        self._closing = threading.Event()
        self._busy = None                 # (meeting id, "transcribing" | "speakers" | "cleanup")
        self._status = ""                 # short, content-free status line
        self._import_errors = []          # recent intake refusals (safe messages)
        self._imports = queue.Queue()
        self._last_purge = 0.0
        self._lock = threading.Lock()
        self._reruns = {}                 # meeting id -> known speaker count (or None)
        self._scan_requested = False
        self.last_scan = None
        self._threads = []
        # Microphone recording (#30): one at a time; heavy processing waits.
        self.recordings = self.root / "recordings"
        self._recording = None
        self._rec_starting = False         # a recording is being opened
        self._yielded = False              # a worker stop caused by recording, not the user
        self._recording_reports = []       # finished or recovered recordings (no content)
        self._hung = {}                    # folder name -> recording whose writer did not finish
        self.open_stream = recorder._sounddevice_stream     # tests use a fake
        self._reconcile_imports()
        intake.recover(self.store)        # imports queued when the app last closed
        if start:
            self.start()

    # -- lifecycle --------------------------------------------------------------------

    def start(self):
        for target, name in ((self._process_loop, "workspace-processing"),
                             (self._intake_loop, "workspace-intake"),
                             (self._watch_loop, "workspace-watch")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self._wake.set()
        # Recordings left by a crash, power loss or a failed import (#30).
        for folder, _ in recorder.unfinished(self.recordings):   # also half-removed ones
            self._imports.put(("__recording__", str(folder)))

    def close(self):
        for _ in range(100):              # a recording still opening (up to 10 s)
            if not self._rec_starting:
                break
            time.sleep(0.1)
        rec = self._recording
        if rec is not None:              # saved now; imported at the next start
            self._recording = None
            try:
                rec.stop()
            except recorder.RecorderError:
                pass                     # its folder is recovered at the next start
        self._closing.set()
        self._wake.set()
        self._imports.put(None)
        self.handle.close()
        for t in self._threads:
            # A folder scan may be hashing a large file; it stops between files.
            t.join(60 if t.name == "workspace-watch" else 15)
        self.store.close()

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
        validated again here. Returns the number accepted."""
        accepted, refused = 0, []
        for p in list(paths)[:50]:
            try:
                real = validate_input_file(p, AUDIO_EXTENSIONS, MAX_IMPORT_BYTES)
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
                if item is None or self._closing.is_set():
                    return
                if item[0] == "__recording__":
                    self._import_recording_folder(Path(item[1]))
                    continue
                path, folder_id, key, content = (*item, None, None)[:4]
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
                self._import_errors = (self._import_errors + [_import_message(exc)])[-5:]
            finally:
                self._imports.task_done()
                self._status = ""
                self._wake.set()

    def _import_one(self, path, folder_id=None, title=None):
        title = title or Path(path).stem[:MAX_TITLE_CHARS] or "Untitled meeting"
        mid, sid = media.import_recording(self.store, self.media, path, title)
        with self.store.tx() as db:
            if folder_id and intake.authority_for(self.store, folder_id):
                # Authority comes only from the folder's explicit setting (#24);
                # the original's location is kept only when it may be deleted.
                db.execute("UPDATE sources SET delete_original = 1, intake_folder = ? "
                           "WHERE id = ?", (folder_id, sid))
            else:
                db.execute("UPDATE sources SET original_path = NULL, intake_folder = ? "
                           "WHERE id = ?", (folder_id, sid))
        try:
            self._plan_version(mid, sid)
        except BaseException:
            self._discard_import(mid)       # nothing half-imported is left behind
            raise
        return mid

    # -- microphone recording (#30) ---------------------------------------------------

    def _need_microphone(self):
        if not self.settings["microphone"]:
            raise WorkspaceError("Microphone recording is turned off in Settings.")

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

    def start_recording(self, device, name=None):
        self._need_microphone()
        # Under the processing lock: the loop re-checks this flag under the
        # same lock before starting heavy work, so nothing can slip in.
        with self._lock:
            if self._capturing():
                raise WorkspaceError("A recording is already running.")
            self._rec_starting = True
            busy = self._busy
        try:
            # Heavy processing waits while recording. Work in progress is
            # stopped; it was never committed and is redone afterwards.
            if busy and busy[1] in ("transcribe", "speakers", "rerun"):
                self._yielded = True
                self.handle.stop_now()
            choice = self._device(device, name)
            rec = recorder.Recording(recorder.new_folder(self.recordings), choice["index"],
                                     choice["name"], choice["rate"],
                                     open_stream=self.open_stream)
            try:
                rec.start()
            except recorder.RecorderError as exc:
                raise WorkspaceError(str(exc)) from exc
            with self._lock:
                self._recording = rec
        finally:
            with self._lock:
                self._rec_starting = False
            self._wake.set()                 # if it failed, processing carries on
        threading.Thread(target=self._watch_recording, args=(rec,), daemon=True,
                         name="recorder-watch").start()
        return rec.status()

    def _watch_recording(self, rec):
        """A recording that stops itself (lost device, full disk, time limit)
        is saved and imported like one the user stopped."""
        while self._recording is rec and not rec.stopped:
            time.sleep(0.5)
        if self._recording is rec and not self._closing.is_set():
            self._finish_recording(rec)

    def _finish_recording(self, rec):
        with self._lock:
            if self._recording is not rec:
                return                   # already finished by the other path
            self._recording = None
        try:
            rec.stop()
        except recorder.RecorderError as exc:
            self._hung[rec.folder.name] = rec
            self._add_report({"title": None, "seconds": rec.status()["seconds"],
                              "events": list(rec.journal["events"]),
                              "note": str(exc), "pending": rec.folder.name})
            return                       # imported at the next start
        self._imports.put(("__recording__", str(rec.folder)))
        self._wake.set()

    def pause_recording(self):
        if self._recording is None:
            raise WorkspaceError("Nothing is being recorded.")
        self._recording.pause()
        return self._recording.status()

    def resume_recording(self):
        if self._recording is None:
            raise WorkspaceError("Nothing is being recorded.")
        self._recording.resume()
        return self._recording.status()

    def stop_recording(self):
        rec = self._recording
        if rec is None:
            raise WorkspaceError("Nothing is being recorded.")
        status = rec.status()
        self._finish_recording(rec)
        return {**status, "state": "saving"}

    def recording_status(self):
        rec = self._recording
        return {"enabled": self.settings["microphone"],
                "recording": rec.status() if rec is not None else None,
                "reports": self._recording_reports[-5:]}

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
        folder = self._pending_folder(report)
        hung = self._hung.get(folder.name)
        if hung is not None and hung.writing:
            raise WorkspaceError("The recording is still being saved. Try again in a moment.")
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            raise WorkspaceError("The recording could not be deleted yet. Close other apps "
                                 "that may be using it, then try again.")
        self._hung.pop(folder.name, None)
        self.discard_recording_report(rid)

    def _import_recording_folder(self, folder):
        """Turn a saved (or crash-recovered) recording into a meeting, then
        remove the recording folder. Failures are reported and the folder is
        kept, to be tried again (Try again, or the next start)."""
        journal = recorder.read_journal(folder)
        has_audio = any(folder.glob("chunk-*.pcm")) or (folder / "recording.wav").exists()
        if journal is None and not (folder / "journal.json").exists() and not has_audio:
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
        if journal.get("state") == "imported" or self._already_imported(folder.name):
            try:                         # already a meeting: finish removing the folder
                recorder.remove_folder(folder)
            except OSError:
                pass                     # tried again at the next start
            return
        events = [e for e in journal.get("events", []) if e["kind"] not in ("paused", "resumed")]
        report = {"title": None, "seconds": 0, "events": events,
                  "device": journal.get("device_name")}
        try:
            wav, seconds = recorder.assemble_wav(folder, journal)
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
                recorder.remove_folder(folder)
                self._add_report(report)
                return
            title = time.strftime("Recording %Y-%m-%d %H:%M",
                                  time.localtime(journal.get("started_at") or time.time()))
            mid = self._import_one(str(wav), None, title)
        except Exception as exc:
            full = isinstance(exc, OSError) and getattr(exc, "errno", None) == 28
            report["note"] = ("It could not be imported yet: " +
                              ("the disk is full. Free some space, then choose Try again."
                               if full else _import_message(exc)) +
                              " The saved audio is kept.")
            report["pending"] = folder.name
            self._add_report(report)
            return
        # Record which recording this meeting came from, then mark the folder
        # as imported before removing it: either one stops a second import.
        report.update(title=title, meeting=mid, recording_id=folder.name)
        try:
            recorder._retrying(lambda: recorder._write_atomic(
                self.media / mid / "recording.json",
                json.dumps(report, indent=1).encode("utf-8")))
        except OSError:
            pass
        journal.update(state="imported", meeting=mid)
        try:
            recorder._retrying(lambda: recorder._write_atomic(
                folder / "journal.json", json.dumps(journal).encode("utf-8")))
        except OSError:
            pass                              # the sidecar above still prevents a duplicate
        try:
            recorder.remove_folder(folder)    # the meeting now holds the audio
        except OSError:
            pass                              # finished at the next start
        self._add_report(report)

    def _already_imported(self, recording_id):
        """True if a meeting was already made from this recording folder."""
        for sidecar in self.media.glob("*/recording.json"):
            try:
                if json.loads(sidecar.read_text(encoding="utf-8")).get(
                        "recording_id") == recording_id:
                    return True
            except (OSError, ValueError):
                continue
        return False

    def _plan_version(self, mid, sid):
        reader = media.AudioReader(media.audio_path(self.media, mid))
        try:
            plan = plan_chunks(reader, len(reader))
        finally:
            reader.close()
        return self._new_version(mid, sid, plan)

    def _discard_import(self, mid):
        """Remove a meeting whose import never completed. Only the app's own
        fresh copy is removed; the original recording is never touched."""
        import shutil
        shutil.rmtree(self.media / mid, ignore_errors=True)
        try:
            with self.store.tx() as db:
                if db.execute("SELECT 1 FROM versions WHERE meeting_id = ?",
                              (mid,)).fetchone() is None:
                    db.execute("DELETE FROM sources WHERE meeting_id = ?", (mid,))
                    db.execute("DELETE FROM meetings WHERE id = ?", (mid,))
        except StoreError:
            pass

    def _reconcile_imports(self):
        """At startup: finish or remove imports cut short by a crash or close
        (a meeting with no transcript version would otherwise wait forever)."""
        for m in self.store._all("SELECT m.id FROM meetings m WHERE NOT EXISTS "
                                 "(SELECT 1 FROM versions v WHERE v.meeting_id = m.id)"):
            src = self.store._one("SELECT id FROM sources WHERE meeting_id = ?", (m["id"],))
            if src is not None and media.audio_path(self.media, m["id"]).is_file():
                try:
                    self._plan_version(m["id"], src["id"])
                    continue
                except Exception:
                    pass
            self._discard_import(m["id"])

    def _new_version(self, mid, sid, plan):
        model = self.settings["model"]
        spec = asr_engines.MANAGED_MODELS.get(model)
        return self.store.ensure_version(
            mid, sid, engine="parakeet" if asr_engines.is_parakeet(model) else "whisper",
            model=model, model_revision=spec.revision if spec else "test",
            precision="int8", config={"chunking": "v1"}, chunk_plan=plan)

    # -- watched folders (#23) -------------------------------------------------------

    def _watch_loop(self):
        """Reconcile watched folders at launch, then every SCAN_INTERVAL_S while
        open. Sleep counts as elapsed time, so waking from sleep scans at once."""
        last = 0.0
        while not self._closing.is_set():
            if time.monotonic() - last >= intake.SCAN_INTERVAL_S or self._scan_requested:
                self._scan_requested = False
                last = time.monotonic()
                try:
                    self.last_scan = intake.scan(
                        self.store, AUDIO_EXTENSIONS, MAX_IMPORT_BYTES,
                        lambda p, fid, key, content: self._imports.put((p, fid, key, content)),
                        stop=self._closing.is_set)
                except Exception as exc:     # a scan problem must not stop watching
                    self.last_scan = {"error": safe_message(exc)}
            self._closing.wait(1.0)

    def watched_folders(self):
        return {"folders": intake.summary(self.store), "last_scan": self.last_scan,
                "interval_minutes": intake.SCAN_INTERVAL_S // 60}

    def add_watched_folder(self, path, include_existing):
        if not isinstance(include_existing, bool):
            raise WorkspaceError("Expected yes or no.")
        try:
            fid = intake.add_folder(self.store, path, include_existing)
        except intake.IntakeError as exc:
            raise WorkspaceError(str(exc)) from exc
        self.scan_now()
        return fid

    def set_folder_active(self, fid, active):
        if not isinstance(active, bool):
            raise WorkspaceError("Expected on or off.")
        intake.set_active(self.store, _hold_id(fid), active)
        if active:
            self.scan_now()

    def set_folder_authority(self, fid, delete_originals=None, fetch_cloud=None):
        fid = _hold_id(fid)
        for v in (delete_originals, fetch_cloud):
            if v is not None and not isinstance(v, bool):
                raise WorkspaceError("Expected on or off.")
        if delete_originals is not None:
            intake.set_delete_originals(self.store, fid, delete_originals)
        if fetch_cloud is not None:
            intake.set_fetch_cloud(self.store, fid, fetch_cloud)
            self.scan_now()

    def remove_folder(self, fid):
        intake.remove_folder(self.store, _hold_id(fid))

    def scan_now(self):
        self._scan_requested = True

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
        if self._capturing():
            return None                      # heavy processing waits for the recording
        with self._lock:
            for mid, count in list(self._reruns.items()):
                m = self.store.meeting(mid)
                v = self.store.active_version(mid)
                if m is None or v is None or m["reviewed_at"] or not self._audio_available(m):
                    del self._reruns[mid]           # no longer possible
                    continue
                return ("rerun", mid, (v["id"], count))
        for m in self.store._all("SELECT * FROM meetings WHERE paused = 0 AND phase != 'error' "
                                 "AND reviewed_at IS NULL AND audio_removed_at IS NULL "
                                 "ORDER BY created_at"):
            v = self.store.active_version(m["id"])
            if v is None:
                continue
            if self.store.chunks(v["id"], status="pending"):
                return ("transcribe", m["id"], v["id"])
            # Speakers once per version; an interrupted pass restarts (it cannot
            # resume), a failed one waits for the user's explicit rerun.
            if self.settings["speakers"] and not any(
                    r["status"] in ("done", "failed", "running")
                    for r in self.store.speaker_runs(v["id"])):
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
                    if kind != "cleanup" and self._capturing():
                        break                # a recording started meanwhile (#30)
                    self._busy = (mid, kind)
                try:
                    self._run(kind, mid, ref)
                except Exception as exc:       # never let one meeting stop the loop
                    try:
                        self.store.set_phase(mid, "error",
                                             f"Unexpected problem: {safe_message(exc)}"[:300])
                    except StoreError:
                        pass
                finally:
                    self._busy = None
                    self._yielded = False      # only the run that recording stopped

    def _maybe_purge(self):
        if time.time() - self._last_purge > RECEIPT_PURGE_S:
            holds.purge_expired_receipts(self.store)
            self._last_purge = time.time()

    def _run(self, kind, mid, ref):
        if kind == "cleanup":
            self._status = "Cleaning up"
            cleanup.run_job(self.store, ref, self.media, quiesce=self.handle.shutdown,
                            original_roots=intake.deletion_roots(self.store, mid))
            return
        if self._closing.is_set():
            return                          # never start a worker during shutdown
        m = self.store.meeting(mid)
        if m is None or m["reviewed_at"]:
            return                          # reviewed meanwhile: nothing more to process
        if not self._audio_available(m):
            # Missing audio (e.g. quarantined or deleted outside the app): stop
            # here with an explanation instead of retrying.
            self.store.set_phase(mid, "error", "The meeting audio is missing from the "
                                               "workspace. Import the recording again.")
            return
        threads = resources.worker_threads(self.settings["mode"])
        self.handle.quiet = self.settings["mode"] == resources.QUIET
        if kind == "rerun":
            vid, count = ref
            with self._lock:
                self._reruns.pop(mid, None)
            self._status = "Identifying speakers"
            speakers.run_speakers(self.store, mid, vid, self.handle,
                                  media.audio_path(self.media, mid), threads=threads,
                                  hint=count, model=self.speaker_model)
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
            reader = media.AudioReader(media.audio_path(self.media, mid))
            try:
                engine = WorkerEngine(self.handle, self.store.version(ref)["model"], threads)
                result = runner.run_version(self.store, mid, ref, reader, engine)
            finally:
                reader.close()               # release the handle before any cleanup
            if result == runner.RunResult.STOPPED and not self._closing.is_set() \
                    and not self._yielded:
                self.store.set_paused(mid, True)    # Stop now = paused until resumed
            return
        self._status = "Identifying speakers"
        speakers.run_speakers(self.store, mid, ref, self.handle,
                              media.audio_path(self.media, mid), threads=threads,
                              model=self.speaker_model)

    # -- controls -------------------------------------------------------------------------

    def _meeting(self, mid):
        if not isinstance(mid, str) or len(mid) != 32:
            raise WorkspaceError("Meeting not found.")
        m = self.store.meeting(mid)
        if m is None:
            raise WorkspaceError("Meeting not found.")
        return m

    def pause(self, mid):
        """Acknowledged at once; takes effect at the next safe point."""
        self._meeting(mid)
        self.store.set_paused(mid, True)

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
        title = _clean_text(title, MAX_TITLE_CHARS) or "Untitled meeting"
        with self.store.tx() as db:
            db.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, mid))

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
        # again before it starts just replaces the request.
        with self._lock:
            self._reruns[mid] = count
        self._wake.set()

    def retranscribe(self, mid):
        """Transcribe again with the current model. The previous version and
        its edits are kept (superseded), never overwritten."""
        m = self._meeting(mid)
        if not self._audio_available(m):
            raise WorkspaceError("The audio has been removed, so the meeting cannot be "
                                 "transcribed again.")
        if m["reviewed_at"]:
            raise WorkspaceError("The transcript review is confirmed; it cannot be replaced.")
        if self._busy and self._busy[0] == mid:
            raise WorkspaceError("Pause this meeting first.")
        v = self.store.active_version(mid)
        source = self.store._one("SELECT id FROM sources WHERE meeting_id = ? "
                                 "ORDER BY created_at DESC LIMIT 1", (mid,))
        reader = media.AudioReader(media.audio_path(self.media, mid))
        try:
            plan = plan_chunks(reader, len(reader))
        finally:
            reader.close()
        new = self._new_version(mid, source["id"], plan)
        if v is not None and new == v["id"]:
            raise WorkspaceError("It is already transcribed with this model and settings. "
                                 "Change the model in Settings first.")
        self.store.set_phase(mid, "transcribing")
        self._wake.set()

    # -- review -------------------------------------------------------------------------

    def _audio_available(self, m):
        return m["audio_removed_at"] is None and media.audio_path(self.media, m["id"]).is_file()

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
        rows = self.store.transcript(v["id"])
        view = speaker_names.resolve(self.store, v["id"])
        page = rows[start:start + count]
        return {"total": len(rows), "start": start, "items": [
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
                     _clean_text(name, speaker_names.MAX_NAME))

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
        return speaker_names.undo(self.store, vid)

    def search(self, mid, query, version=None):
        """Ids of matching passages (ids, not positions, so highlights stay
        right while the page is being updated)."""
        self._meeting(mid)
        query = _clean_text(query, 200)
        v = self._version(mid, version)
        if not query or v is None:
            return []
        rows = self.store.transcript(v["id"])
        return [rows[i]["id"] for i in self.store.search(v["id"], query)]

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
        self.store.save_edit(segment_id, _clean_text(text, MAX_EDIT_CHARS))
        return self._segment(segment_id)

    def undo(self, mid):
        m = self._meeting(mid)
        if m["reviewed_at"]:
            raise WorkspaceError("The transcript review is confirmed, so it can no longer be "
                                 "edited.")
        v = self.store.active_version(mid)
        sid = self.store.undo_edit(v["id"]) if v else None
        return self._segment(sid) if sid else None

    def _segment(self, sid):
        r = self.store._one("SELECT s.id, COALESCE(e.text, s.text) AS text, "
                            "e.text IS NOT NULL AS edited FROM segments s LEFT JOIN edits e "
                            "ON e.segment_id = s.id WHERE s.id = ?", (sid,))
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
        reader = media.AudioReader(media.audio_path(self.media, mid))
        try:
            samples = reader.read(int(start * SR), int(end * SR))
        finally:
            reader.close()               # never keep the audio open (cleanup, #17)
        if not len(samples):
            raise WorkspaceError("That time is after the end of the recording.")
        return samples

    # -- lists and details --------------------------------------------------------------

    def meetings(self):
        out = []
        for row in holds.unfinished_work(self.store):
            m = self.store.meeting(row["id"])
            v = self.store.active_version(row["id"])
            done, total = self.store.progress(v["id"]) if v else (0, 0)
            busy = self._busy and self._busy[0] == row["id"] and self._busy[1]
            stage = self._stage(m, v)
            out.append({**row, "progress": round(100 * done / total) if total else 0,
                        "paused": bool(m["paused"]), "error": m["last_error"]
                        if m["phase"] == "error" else None,
                        "busy": busy or None, "stage": stage,
                        "action": self._action(m, stage)})
        return out

    @staticmethod
    def _action(m, stage):
        if stage == "inbox":
            return ("Needs attention" if m["phase"] == "error" else
                    "Paused" if m["paused"] else "Transcribing")
        return {"review": "Review the transcript",
                "handoff": "Hand off, then confirm the final document is saved",
                "cleanup": "Cleanup pending"}[stage]

    def _stage(self, m, v):
        """inbox: still being processed; review / handoff / cleanup: active meetings."""
        if m["final_saved_at"]:
            return "cleanup"
        if m["reviewed_at"]:
            return "handoff"
        if v is None or self.store.chunks(v["id"], status="pending") or m["phase"] == "error":
            return "inbox"
        return "review"

    def detail(self, mid):
        m = self._meeting(mid)
        v = self.store.active_version(mid)
        runs = self.store.speaker_runs(v["id"]) if v else []
        older = [x for x in self.store.versions(mid) if v is None or x["id"] != v["id"]]
        due = {h["id"] for h in holds.due_for_review(self.store)}
        done, total = self.store.progress(v["id"]) if v else (0, 0)
        return {
            "id": mid, "title": m["title"], "created_at": m["created_at"],
            "stage": self._stage(m, v), "paused": bool(m["paused"]),
            "error": m["last_error"] if m["phase"] == "error" else None,
            "busy": (self._busy[1] if self._busy and self._busy[0] == mid else None),
            "progress": round(100 * done / total) if total else 0,
            "audio_available": self._audio_available(m),
            "audio_removed_at": m["audio_removed_at"],
            "reviewed_at": m["reviewed_at"], "final_saved_at": m["final_saved_at"],
            "model": v["model"] if v else None,
            "edits": self.store.edit_count(v["id"]) if v else 0,
            "can_undo": bool(v) and not m["reviewed_at"] and self.store.can_undo(v["id"]),
            "older_versions": [{"index": i, "model": x["model"],
                                "created_at": x["created_at"],
                                "edits": self.store.edit_count(x["id"])}
                               for i, x in enumerate(older)],
            "speakers": ({"status": runs[-1]["status"], "error": runs[-1]["error"],
                          "count": runs[-1]["hint_count"],
                          "started_at": runs[-1]["started_at"]} if runs else None),
            "holds": [{"id": h["id"], "scope": h["scope"], "reason": h["reason"],
                       "created_at": h["created_at"], "due_review": h["id"] in due}
                      for h in holds.active_holds(self.store, mid)],
            "cleanup": [{"id": j["id"], "phase": j["phase"], "state": j["state"],
                         "note": j["error"]} for j in self.store._all(
                "SELECT * FROM cleanup_jobs WHERE meeting_id = ? ORDER BY intent_at", (mid,))],
            "limits": cleanup.LIMITS,
        }

    def reminders(self):
        """Shown while the app is open: holds due for review and cleanup that
        waits for a confirmation. Nothing is scheduled outside the app."""
        titles = {m["id"]: m["title"] for m in self.store._all("SELECT id, title FROM meetings")}
        return {
            "holds_due": [{"id": h["id"], "meeting": h["meeting_id"],
                           "title": titles.get(h["meeting_id"], ""), "reason": h["reason"]}
                          for h in holds.due_for_review(self.store)],
            "awaiting_confirmation": [{"id": j["id"], "meeting": j["meeting_id"],
                                       "title": titles.get(j["meeting_id"], ""),
                                       "phase": j["phase"]}
                                      for j in holds.awaiting_confirmation(self.store)],
        }

    def status(self):
        return {"busy": self._busy[1] if self._busy else None, "message": self._status,
                "import_errors": list(self._import_errors),
                "importing": self._imports.unfinished_tasks}

    def clear_import_errors(self):
        self._import_errors = []

    def receipts(self):
        return [{"id": r["job_id"], "requested_at": r["created_at"],
                 "completed_at": r["completed_at"], "result": r["result"]}
                for r in holds.receipts(self.store)]

    def get_settings(self):
        choices = [{"name": n, "label": n + (" (recommended)" if n ==
                                               asr_engines.RECOMMENDED_PARAKEET else "")}
                   for n in (*asr_engines.PARAKEET_MODELS, *asr_engines.WHISPER_MODELS)]
        from workspace import update
        return {**self.settings.values, "choices": choices,
                "deployment": update.deployment(),
                "update_blockers": [b["message"] for b in
                                    update.blockers(self.root / "workspace.db")]}

    def set_settings(self, changes):
        if not isinstance(changes, dict):
            raise WorkspaceError("Expected settings.")
        if changes.get("microphone") is False and self._recording is not None:
            raise WorkspaceError("Stop the recording before turning the microphone off.")
        quiet_before = self.handle.quiet
        self.settings.update(**changes)
        self.handle.quiet = self.settings["mode"] == resources.QUIET
        if self.handle.quiet != quiet_before and self._busy is None:
            self.handle.shutdown()        # the next worker starts with the new priority
        self._wake.set()
        return self.get_settings()

    # -- handoff (#22) ------------------------------------------------------------------

    def _handoff(self, fn, *args):
        try:
            return fn(*args)
        except (handoff.HandoffError, clipboard.ClipboardError) as exc:
            raise WorkspaceError(str(exc)) from exc

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
        self.templates.reset(tid)

    def handoff_preview(self, mid, tid):
        """Exactly what Copy and Save will produce, plus the options in use."""
        self._meeting(mid)
        template = self._handoff(self.templates.get, tid)
        out = self._handoff(handoff.build, self.store, mid, template)
        return {**out, "options": handoff.options(self.store, mid)}

    def set_handoff_options(self, mid, omitted, redactions):
        m = self._meeting(mid)
        if not m["reviewed_at"]:
            raise WorkspaceError("Confirm the transcript review first.")
        self._handoff(handoff.set_options, self.store, mid, omitted, redactions)

    def _checked(self, mid, tid, kind, expected):
        """The handoff text, refused unless it is exactly what was previewed."""
        out = self.handoff_preview(mid, tid)
        if not isinstance(expected, str) or out["digests"][kind] != expected:
            raise WorkspaceError("The handoff changed since the preview. Check the preview "
                                 "again, then copy or save.")
        return out

    def copy_handoff(self, mid, tid, kind, expected):
        if kind not in ("combined", "prompt_only"):
            raise WorkspaceError("Choose what to copy.")
        out = self._checked(mid, tid, kind, expected)
        self._handoff(clipboard.copy_text, out[kind])
        return {"chars": len(out[kind]), "long": out["long"] and kind == "combined"}

    def save_handoff_to(self, mid, tid, path, expected):
        """Write the Markdown to a file the user chose in the save dialog. The
        app keeps no record of it; the user files or deletes it. ".md" is
        appended (never substituted) when missing, and a file the dialog did
        not ask about is never overwritten."""
        from workspace.safe_paths import check_raw
        out = self._checked(mid, tid, "markdown", expected)
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
        return {"chars": len(out["markdown"]), "name": target.name}

    # -- setup (#19) ----------------------------------------------------------------------

    def setup_state(self):
        cards = setup.cards(self.cache_dir)
        for c in cards:
            c["download"] = self.downloads.state.get(c["name"])
        speech_ready = any(c["state"] == "ready" for c in cards if c["role"] == "speech")
        import os
        return {"cards": cards, "token_remembered": credentials.has_token(),
                "earlier_token": bool(os.environ.get("HF_TOKEN")),
                "onboarded": self.settings["onboarded"], "speech_ready": speech_ready,
                "speakers": self.settings["speakers"]}

    def download_model(self, name, token=None, remember=True, use_earlier=False):
        """Download or repair a model. For the speaker model the token is the
        one given now (remembered in Credential Manager unless the user
        unticks Remember), the remembered one, or the earlier app's one."""
        import os
        if not isinstance(name, str) or name not in asr_engines.MANAGED_MODELS:
            raise WorkspaceError("Unknown model.")
        if token is not None and not isinstance(token, str):
            raise WorkspaceError("Expected a token.")
        on_success = None
        if asr_engines.MANAGED_MODELS[name].gated:
            try:
                if token or use_earlier:
                    token = (token or "").strip() or os.environ.get("HF_TOKEN") or None
                    if token and remember:
                        # Remembered only once a download has shown it works.
                        on_success = (lambda t=token: credentials.save_token(t))
                else:
                    token = credentials.load_token()
            except credentials.CredentialError as exc:
                raise WorkspaceError(str(exc)) from exc
            if not token:
                raise WorkspaceError("Enter your Hugging Face token to download the speaker "
                                     "model.")
        else:
            token = None                       # public models never receive a token
        with self._lock:                       # atomic with the processing thread's start
            if self._busy and self._busy[1] != "cleanup":
                raise WorkspaceError("Pause processing first: models cannot be replaced "
                                     "while a meeting is being processed.")
            try:
                self.downloads.start(name, token, on_success)
            except setup.SetupError as exc:
                raise WorkspaceError(str(exc)) from exc

    def verify_model(self, name):
        if not isinstance(name, str):
            raise WorkspaceError("Unknown model.")
        with self._lock:
            if self._busy and self._busy[1] != "cleanup":
                raise WorkspaceError("Pause processing first.")
            try:
                self.downloads.verify(name)
            except setup.SetupError as exc:
                raise WorkspaceError(str(exc)) from exc

    def cancel_download(self):
        return self.downloads.cancel()

    def remove_token(self):
        """Forget the remembered token. Downloaded models keep working offline."""
        try:
            return credentials.remove_token()
        except credentials.CredentialError as exc:
            raise WorkspaceError(str(exc)) from exc

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
            return holds.add_hold(self.store, mid, scope, _clean_text(reason, 500))
        except holds.HoldError as exc:
            raise WorkspaceError(str(exc)) from exc

    def release_hold(self, hold_id):
        return holds.release_hold(self.store, _hold_id(hold_id))

    def keep_hold(self, hold_id):
        holds.mark_reviewed(self.store, _hold_id(hold_id))

    def confirm_reviewed(self, mid):
        self._meeting(mid)
        if self._busy and self._busy[0] == mid:
            raise WorkspaceError("Wait until processing of this meeting has finished.")
        try:
            jid = cleanup.confirm_transcript_reviewed(self.store, mid)
        except cleanup.CleanupError as exc:
            raise WorkspaceError(str(exc)) from exc
        self._wake.set()
        return jid

    def confirm_final_saved(self, mid):
        self._meeting(mid)
        try:
            jid = cleanup.confirm_final_saved(self.store, mid)
        except cleanup.CleanupError as exc:
            raise WorkspaceError(str(exc)) from exc
        self._wake.set()
        return jid

    def confirm_cleanup(self, job_id):
        """Explicit confirmation for cleanup that waited on a released hold."""
        holds.confirm_pending_cleanup(self.store, _hold_id(job_id))
        self._wake.set()

    def retry_cleanup(self, job_id):
        """The user's "Try again" for a failed cleanup: run it on the next pass."""
        with self.store.tx() as db:
            db.execute("UPDATE cleanup_jobs SET last_attempt_at = NULL WHERE id = ? AND "
                       "state = 'failed'", (_hold_id(job_id),))
        self._wake.set()


def _hold_id(value):
    if not isinstance(value, str) or len(value) != 32 or not value.isalnum():
        raise WorkspaceError("Not found.")
    return value


def _import_message(exc):
    if isinstance(exc, (media.SourceChanged, UnsafePathError, WorkspaceError)):
        return str(exc)
    if isinstance(exc, ValueError) and "no audio" in str(exc):
        return "That file contains no audio."
    return "That recording could not be imported (unsupported or damaged file)."
