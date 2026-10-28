# workspace/recording_ws.py
# The Workspace's microphone recording (#30, #108, #462) and live drafts while
# recording (#104), moved unchanged out of workspace/app.py (#414). Workspace
# inherits RecordingMixin, so the bridge (workspace/api.py) calls the same
# methods on the Workspace as before. The mixin keeps no state of its own:
# every attribute, thread and lock it uses is the Workspace's, set in
# Workspace.__init__, so the locking is unchanged. What stays in app.py:
# COMPUTER_AUDIO_STARTS and _computer_wanted (tests patch
# workspace.app.COMPUTER_AUDIO_STARTS), the indicator, the lid and the policy
# tick that calls _draft_tick.

import json
import threading
import time
from typing import TYPE_CHECKING, Any

from workspace import asr_engines, audit, drafts, engines, media, recorder, resources
from workspace import consent as recording_consent
from workspace.app_common import (MANAGED_OFF, WorkspaceError, _expected_import_error,
                                  _import_incident, _import_message)
from workspace.store import DamagedContent

if TYPE_CHECKING:                      # the type check only (tools/type_check.py)
    import queue
    from pathlib import Path

    from workspace import diag, setup
    from workspace.app import Settings
    from workspace.store import Store
    from workspace.worker import WorkerHandle

SOURCES_CHANGED = ("What would be recorded changed while you were confirming (computer audio "
                   "was turned on or off). Start the recording again.")
COMPUTER_POLICY_STOP = ("Your organisation turned off computer audio. Only the microphone "
                        "is recorded from here.")


def _computer_seconds(journal):
    """{"computer_seconds": seconds} for a two-track recording's journal (how
    much computer audio was saved: the crash-recovery report gives each
    track's, #108), or {} for a microphone-only one. A count only (P3)."""
    if recorder.journal_format(journal) != recorder.JOURNAL_FORMAT:
        return {}
    track = (journal.get("tracks") or {}).get(recorder.COMPUTER)
    if not isinstance(track, dict):
        return {}
    rate, frames = track.get("rate"), track.get("frames", 0)
    if (isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0
            or isinstance(frames, bool) or not isinstance(frames, int) or frames < 0):
        return {"computer_seconds": 0}
    return {"computer_seconds": round(frames / rate, 1)}


class RecordingMixin:
    """Recording and live drafts, for Workspace (workspace/app.py) only."""

    if TYPE_CHECKING:
        # For the type check only: what the methods below use of the
        # Workspace, typed as Workspace.__init__ sets it. Nothing here exists
        # when the app runs.
        _battery: Any
        _busy: Any | None
        _closing: threading.Event
        _draft_gen: int
        _draft_why: str
        _drafts: Any | None
        _drafts_ended: Any | None
        _drafts_ended_why: Any | None
        _hung: dict[Any, Any]
        _imports: queue.Queue[Any]
        _integrity_ok: threading.Event
        _lid_result: Any | None
        _lock: threading.Lock
        _mic_choices: list[Any]
        _policy_wake: threading.Event
        _rec_starting: bool
        _recording: Any | None
        _recording_reports: list[Any]
        _removing: Any | None
        _wake: threading.Event
        _yielded: bool
        audit: audit.AuditLog
        cache_dir: Path
        downloads: setup.Downloads
        draft_model: str
        handle: WorkerHandle
        list_microphones: Any | None
        locked: Any | None
        log: diag.Log
        open_loopback: Any | None
        open_stream: Any
        recordings: Path
        settings: Settings
        store: Store
        watch_microphones: Any | None

        def _allowed(self, name): ...
        def _computer_wanted(self): ...
        def _forget_recording_key(self, recording_id): ...
        def _import_one(self, path, folder_id=None, title=None, recording=None, stream=None): ...
        def _indicate(self): ...
        def _not_finish_only(self): ...
        def _not_locked(self): ...
        def consent_required(self): ...

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
        """The microphones to choose from. While a recording is open (#462),
        the ones Windows has now, for the card's choice (PortAudio cannot
        list them afresh while its stream is open): each with its place in
        the list as its index, which only switch_microphone takes, and
        whether it is the one recording."""
        self._need_microphone()
        rec = self._recording
        if rec is not None:
            return self._recording_microphones(rec)
        try:
            return recorder.list_devices()
        except recorder.RecorderError as exc:
            raise WorkspaceError(str(exc)) from exc

    def _windows_microphones(self):
        """[{"id", "name", "default"}] from Windows (loopback.list_inputs), or
        the test's fake. Names go to the page only, never to a log (P3)."""
        lister = self.list_microphones
        if lister is None:
            from workspace import loopback       # only while recording
            lister = loopback.list_inputs
        try:
            return [d for d in lister() if isinstance(d.get("name"), str) and d["name"]]
        except Exception as exc:
            raise WorkspaceError("The list of microphones could not be read. The recording "
                                 "carries on.") from exc

    def _recording_microphones(self, rec):
        listing = self._windows_microphones()
        self._mic_choices = [(d["id"], d["name"][:120]) for d in listing]
        return [{"index": i, "name": name, "label": name, "default": bool(d.get("default")),
                 "current": rec.is_current(name)}
                for i, ((_, name), d) in enumerate(zip(self._mic_choices, listing, strict=True))]

    def _mic_allowed(self):
        """Managed policy and the setting still allow the microphone: read
        before every switch of microphone while recording (#462, K3). An
        unreadable policy grants nothing."""
        return bool(self.settings["microphone"] and self._allowed("AllowMicrophoneRecording"))

    def _mic_follow(self, on_default):
        """How a recording follows the Windows default microphone (#462), or
        None. on_default: the microphone it starts on is the one marked as
        the Windows default; only then are changes of default followed from
        the start (DEC-080: a microphone the person chose stays). A test's
        fake stream has no Windows devices to follow unless the test gives a
        fake watch too."""
        watch = self.watch_microphones
        if watch is None:
            if self.open_stream is not recorder._sounddevice_stream:
                return None
            from workspace import loopback       # only when a recording starts
            watch = loopback.CaptureWatch
        return {"watch": watch, "resolve": recorder.find_input, "allowed": self._mic_allowed,
                "on_default": bool(on_default)}

    def switch_microphone(self, index, name):
        """The person chose a microphone on the recording card (#462): the
        recording switches to it, in the same recording, and stops following
        the Windows default. index and name: an entry of the list
        microphone_devices gave while recording, still connected now.
        Managed policy is read first (K3)."""
        self._not_locked()
        rec = self._recording
        if rec is None:
            raise WorkspaceError("Nothing is being recorded.")
        if not self._allowed("AllowMicrophoneRecording"):
            raise WorkspaceError(MANAGED_OFF.format(what="Microphone recording"))
        if not self.settings["microphone"]:
            raise WorkspaceError("Microphone recording is turned off in Settings.")
        choices = self._mic_choices
        if not 0 <= index < len(choices) or choices[index][1] != name:
            raise WorkspaceError("The list of microphones has changed. Choose the "
                                 "microphone again.")
        ident = choices[index][0]
        if ident not in {d["id"] for d in self._windows_microphones()}:
            raise WorkspaceError("That microphone is no longer connected. Choose another.")
        try:
            rec.choose(name)
        except recorder.RecorderError as exc:
            raise WorkspaceError(str(exc)) from exc
        return rec.status()

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
                                         computer=computer, key=key,
                                         follow=self._mic_follow(choice.get("default")))
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
            return {"state": session.state, "note": session.note,
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
            # Parakeet v2 is not downloaded or is damaged; IT's copy names IT (#464).
            why = drafts.unavailable_reason(self.draft_model)
            self._draft_why = why
            with self._lock:
                self._drafts_ended = rec.journal["id"]
                self._drafts_ended_why = why
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
                              **_computer_seconds(journal),
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
            if audio.echo_skipped:               # the plain mix, with its event (#376)
                self.log.write("echo_skipped", cause=audio.echo_skipped)
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

