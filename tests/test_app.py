# The workspace controller and its page API (#20), end to end with a real
# worker process, fake engines and synthetic audio (no models, no UI).
#   python -m unittest discover -s tests -v

import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
SR = 16000

if HAVE_DEPS:
    import numpy as np

    from workspace import asr_engines
    from workspace import api, engines, media, policy
    from workspace.app import Workspace, WorkspaceError
    from workspace.store import new_id


def fake(name):
    return f"test:fake_worker_engines:{name}"


def write_wav(path, seconds):
    rng = np.random.default_rng(1)
    pcm = (rng.standard_normal(int(seconds * SR)) * 300).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rec = write_wav(self.root / "synthetic-standup.wav", 100)
        self.ws = None

    def tearDown(self):
        if self.ws:
            self.ws.close()
        self.tmp.cleanup()

    def open(self, start=True, model="sentences", speakers=True):
        ws = Workspace(self.root / "ws", self.root / "models", start=False, allow_test_engines=True,
                       speaker_model=fake("fake_diarizer"), battery=False,
                       protector=keyprotect.TestProtector())
        ws.settings.update(model=fake(model), speakers=speakers)
        if start:
            ws.start()
        self.ws = ws
        return ws

    def reopen(self, **kw):
        self.ws.close()
        self.ws = None
        return self.open(**kw)

    def imported(self, ws):
        self.assertEqual(ws.import_paths([str(self.rec)])["accepted"], 1)
        self.assertTrue(ws.wait_idle(120))
        [m] = ws.meetings()
        return m["id"]

    # -- the main path ------------------------------------------------------------

    def test_import_transcribe_speakers_then_review(self):
        ws = self.open()
        mid = self.imported(ws)
        [m] = ws.meetings()
        self.assertEqual((m["stage"], m["progress"], m["title"]),
                         ("review", 100, "synthetic-standup"))
        page = ws.transcript(mid)
        self.assertGreater(page["total"], 20)
        self.assertTrue(all(i["who"] for i in page["items"]))     # speakers attached
        self.assertEqual(ws.detail(mid)["speakers"]["status"], "done")
        self.assertEqual(len(ws.clip(mid, 1.0, 3.0)), 2 * SR)

    def test_edits_and_undo_survive_restart(self):
        ws = self.open()
        mid = self.imported(ws)
        first, second = ws.transcript(mid)["items"][:2]
        ws.edit(mid, first["id"], "Corrected first passage")
        ws.edit(mid, second["id"], "Corrected second passage")
        ws.edit(mid, second["id"], "Corrected second passage again")
        ws = self.reopen()
        items = {i["id"]: i for i in ws.transcript(mid)["items"]}
        self.assertEqual(items[second["id"]]["text"], "Corrected second passage again")
        self.assertTrue(ws.detail(mid)["can_undo"])
        self.assertEqual(ws.undo(mid)["text"], "Corrected second passage")
        self.assertEqual(ws.undo(mid)["text"], second["text"])            # generated text back
        self.assertFalse(ws.undo(mid)["edited"])                          # first passage
        self.assertIsNone(ws.undo(mid))
        self.assertEqual(ws.detail(mid)["edits"], 0)

    def test_edits_are_refused_for_other_versions_and_bad_input(self):
        ws = self.open()
        mid = self.imported(ws)
        for bad in (True, "1", 10**9, None):
            with self.assertRaises(WorkspaceError):
                ws.edit(mid, bad, "x")
        with self.assertRaises(WorkspaceError):
            ws.edit(mid, ws.transcript(mid)["items"][0]["id"], "x" * 6000)

    def test_retranscribe_keeps_previous_version_and_its_edits(self):
        ws = self.open()
        mid = self.imported(ws)
        seg = ws.transcript(mid)["items"][0]
        ws.edit(mid, seg["id"], "Human correction")
        with self.assertRaises(WorkspaceError):
            ws.retranscribe(mid)                     # same model: nothing would change
        ws.set_settings({"model": fake("sentences_v2")})
        ws.retranscribe(mid)
        self.assertTrue(ws.wait_idle(120))
        d = ws.detail(mid)
        [older] = d["older_versions"]
        self.assertEqual((older["index"], older["model"], older["edits"]),
                         (0, fake("sentences"), 1))
        # The earlier version, with its edit, can still be read (not edited).
        self.assertEqual(ws.transcript(mid, version=0)["items"][0]["text"], "Human correction")
        self.assertEqual(ws.search(mid, "human", version=0), [seg["id"]])
        with self.assertRaises(WorkspaceError):
            ws.edit(mid, seg["id"], "edit an old version")
        with self.assertRaises(WorkspaceError):
            ws.transcript(mid, version=1)
        self.assertTrue(ws.transcript(mid)["items"][0]["text"].startswith("second"))
        old = ws.store.passage(seg["id"])
        self.assertEqual(old["text"], "Human correction")    # machine history untouched

    def test_a_failed_meeting_is_transcribed_again_with_another_model(self):
        # #244: the model the meeting started with cannot load, so Try again
        # would fail again. Transcribe again moves it to the model now chosen.
        ws = self.open(model="load_fails")
        mid = self.imported(ws)
        [m] = ws.meetings()
        self.assertEqual(m["stage"], "inbox")
        self.assertIn("speech model is not ready", m["error"])
        self.assertTrue(m["can_retranscribe"])
        with self.assertRaisesRegex(WorkspaceError, "already uses .*Use Try again"):
            ws.retranscribe(mid)                     # the same model: Try again is the way
        ws.set_settings({"model": fake("sentences")})
        ws.retranscribe(mid)
        self.assertTrue(ws.wait_idle(120))
        d = ws.detail(mid)
        self.assertEqual((d["stage"], d["error"], d["model"]), ("review", None, fake("sentences")))
        [older] = d["older_versions"]
        self.assertEqual(older["model"], fake("load_fails"))
        self.assertGreater(ws.transcript(mid)["total"], 20)

    def test_transcribe_again_uses_the_model_it_checked(self):
        # #244 review: the model is read once, so a change to Settings while
        # it is checked cannot slip an unchecked model into the new version.
        ws = self.open(model="load_fails")
        mid = self.imported(ws)
        ws.set_settings({"model": fake("sentences")})
        active = ws.store.active_version

        def settings_change_meanwhile(m):
            ws.settings.update(model=fake("sentences_v2"))
            return active(m)
        with mock.patch.object(ws.store, "active_version", side_effect=settings_change_meanwhile):
            ws.retranscribe(mid)
        self.assertTrue(ws.wait_idle(120))
        self.assertEqual(ws.detail(mid)["model"], fake("sentences"))

    def test_a_failed_meeting_keeps_its_finished_parts_and_edits(self):
        # D1: what was finished (and corrected) before the failure stays as the
        # earlier version; the new model's text never overwrites it.
        ws = self.open()
        mid = self.imported(ws)
        seg = ws.transcript(mid)["items"][0]
        ws.edit(mid, seg["id"], "Human correction")
        ws.store.set_phase(mid, "error", "Synthetic failure")     # as a run that stopped
        ws.set_settings({"model": fake("sentences_v2")})
        ws.retranscribe(mid)
        self.assertTrue(ws.wait_idle(120))
        d = ws.detail(mid)
        self.assertIsNone(d["error"])
        [older] = d["older_versions"]
        self.assertEqual((older["model"], older["edits"]), (fake("sentences"), 1))
        self.assertEqual(ws.transcript(mid, version=0)["items"][0]["text"], "Human correction")
        self.assertTrue(ws.transcript(mid)["items"][0]["text"].startswith("second"))

    def test_transcribe_again_is_offered_unless_transcribing_reviewed_or_without_audio(self):
        ws = self.open(start=False)
        ws._import_one(self.rec)
        [m] = ws.meetings()
        mid = m["id"]
        self.assertFalse(m["can_retranscribe"])               # still being transcribed
        self.assertFalse(ws.detail(mid)["can_retranscribe"])
        ws.store.set_phase(mid, "error", "Synthetic failure")
        [m] = ws.meetings()
        self.assertEqual(m["stage"], "inbox")
        self.assertTrue(m["can_retranscribe"])                # failed: can switch model
        self.assertTrue(ws.detail(mid)["can_retranscribe"])
        row = ws.store.meeting_state(mid)
        self.assertFalse(ws._can_retranscribe({**dict(row), "reviewed_at": 1.0}, "inbox"))
        self.assertFalse(ws._can_retranscribe({**dict(row), "reviewed_at": 1.0}, "handoff"))
        self.assertTrue(ws._can_retranscribe({**dict(row), "phase": "transcribing"}, "review"))
        media.audio_path(ws.media, mid).unlink()              # audio removed
        [m] = ws.meetings()
        self.assertFalse(m["can_retranscribe"])
        self.assertFalse(ws.detail(mid)["can_retranscribe"])

    def test_transcribe_again_refuses_a_model_that_is_not_ready(self):
        # Nothing is downloaded in this test's model folder: a meeting is
        # never moved to a model that cannot run, and nothing changes.
        ws = self.open(start=False)
        ws._import_one(self.rec)
        [m] = ws.meetings()
        mid = m["id"]
        ws.store.set_phase(mid, "error", "Synthetic failure")
        before = [v["id"] for v in ws.store.versions(mid)]
        ws.set_settings({"model": asr_engines.RECOMMENDED_PARAKEET})
        with self.assertRaisesRegex(WorkspaceError, "^Parakeet v2 is not downloaded. Download "
                                                    "it in Settings > Manage downloads"):
            ws.retranscribe(mid)
        self.assertEqual([v["id"] for v in ws.store.versions(mid)], before)
        self.assertEqual(ws.meetings()[0]["error"], "Synthetic failure")
        # K3: with downloads turned off by policy, it names IT, not Manage downloads.
        ws.policy = policy.PolicySource(reader=lambda: ({"AllowModelDownloads": False}, ()))
        with self.assertRaisesRegex(WorkspaceError, "^Parakeet v2 is not downloaded, and "
                                                    "downloads are turned off by your "
                                                    "organisation. .*ask your IT team"):
            ws.retranscribe(mid)
        self.assertEqual([v["id"] for v in ws.store.versions(mid)], before)

    # Managed policy as read from the registry: downloads allowed (no policy),
    # turned off, and a policy key that cannot be read (K3: grants nothing).
    @staticmethod
    def download_policies():
        return {"allowed": ({}, ()), "off": ({"AllowModelDownloads": False}, ()),
                "unreadable": ({}, policy.UNREADABLE)}

    def open_under_policy(self, case, read, model="sentences", speaker_model="fake_diarizer"):
        """A workspace of its own for one policy case, started after the policy is set."""
        if self.ws:
            self.ws.close()
        ws = Workspace(self.root / case / "ws", self.root / case / "models", start=False,
                       allow_test_engines=True, speaker_model=fake(speaker_model),
                       battery=False, protector=keyprotect.TestProtector())
        ws.settings.update(model=fake(model), speakers=True)
        ws.policy = policy.PolicySource(reader=lambda: read)
        self.ws = ws
        ws.start()
        return ws

    def test_a_speech_model_that_is_not_ready_names_it_when_downloads_are_off(self):
        # #257: the worker's own "not ready" error (Parakeet v2 from an empty
        # model folder) sends the user to Manage downloads. With downloads
        # turned off by policy, or a policy that cannot be read, that page
        # cannot help: the stored error names the organisation and IT instead.
        for case, read in self.download_policies().items():
            with self.subTest(case):
                ws = self.open_under_policy(case, read, model="speech_not_ready")
                self.imported(ws)
                [m] = ws.meetings()
                error = m["error"]
                self.assertTrue(error.startswith("The speech model is not ready: "
                                                 "ModelLoadError: Parakeet v2 is not ready. "),
                                error)
                if case == "allowed":
                    self.assertIn(engines.fix_speech(asr_engines.PARAKEET_V2), error)
                    self.assertNotIn("organisation", error)
                else:
                    self.assertNotIn("Manage downloads", error)
                    self.assertIn("Parakeet v2 is not ready. Downloads are turned off by your "
                                  "organisation. Choose another speech model in Settings, or "
                                  "ask your IT team to install it.", error)
                self.assertNotIn(str(self.root), error)            # P3: no paths
                self.assertTrue(m["can_retranscribe"])

    def test_parakeets_own_load_error_names_it_when_downloads_are_off(self):
        # #260 review: Parakeet's own load errors (a copy that fails its full
        # hash check, or one the runtime cannot load) carry their own advice.
        # Through the real worker, with downloads off it names IT too.
        for case, read in self.download_policies().items():
            with self.subTest(case):
                ws = self.open_under_policy(case, read, model="speech_cannot_be_used")
                self.imported(ws)
                [m] = ws.meetings()
                error = m["error"]
                self.assertTrue(error.startswith("The speech model is not ready: "
                                                 "ModelLoadError: Parakeet v2 cannot be used. "),
                                error)
                if case == "allowed":
                    self.assertIn(asr_engines.PARAKEET_FIX, error)
                    self.assertNotIn("organisation", error)
                else:
                    self.assertNotIn("Manage downloads", error)
                    self.assertIn("Parakeet v2 cannot be used. Downloads are turned off by your "
                                  "organisation. Choose another speech model in Settings, or "
                                  "ask your IT team to install it. (", error)
                self.assertNotIn(str(self.root), error)            # P3: no paths
                self.assertTrue(m["can_retranscribe"])

    def test_a_speaker_model_that_is_not_ready_names_it_when_downloads_are_off(self):
        # #257: the same for speaker identification (the real load from an
        # empty model folder). The transcript stays usable either way (D4).
        for case, read in self.download_policies().items():
            with self.subTest(case):
                ws = self.open_under_policy(case, read, speaker_model="speaker_not_ready")
                mid = self.imported(ws)
                d = ws.detail(mid)
                self.assertEqual((d["stage"], d["speakers"]["status"]), ("review", "failed"))
                error = d["speakers"]["error"]
                self.assertTrue(error.startswith(
                    "WorkerError: ModelLoadError: The speaker identification model is not "
                    "installed or failed verification. "), error)
                if case == "allowed":
                    self.assertIn("Repair it in Settings > Manage downloads.", error)
                    self.assertNotIn("organisation", error)
                else:
                    self.assertNotIn("Manage downloads", error)
                    self.assertIn("failed verification. Downloads are turned off by your "
                                  "organisation. Ask your IT team to install or repair it.",
                                  error)
                self.assertNotIn(str(self.root), error)            # P3: no paths
                self.assertGreater(ws.transcript(mid)["total"], 20)

    def test_search_finds_edited_text(self):
        ws = self.open()
        mid = self.imported(ws)
        items = ws.transcript(mid)["items"]
        ws.edit(mid, items[5]["id"], "The budget for Kawaroa park")
        self.assertEqual(ws.search(mid, "kawaroa"), [items[5]["id"]])
        self.assertEqual(len(ws.search(mid, "sentence")), len(items) - 1)
        self.assertEqual(ws.search(mid, "   "), [])

    # -- pause, stop, restart --------------------------------------------------------

    def test_explicit_pause_stays_paused_after_restart(self):
        ws = self.open(start=False)
        ws._import_one(self.rec)
        mid = ws.meetings()[0]["id"]
        ws.pause(mid)
        ws = self.reopen()
        self.assertTrue(ws.wait_idle(30))
        [m] = ws.meetings()
        self.assertEqual((m["paused"], m["progress"], m["stage"]), (True, 0, "inbox"))
        ws.resume(mid)
        self.assertTrue(ws.wait_idle(120))
        self.assertEqual(ws.meetings()[0]["stage"], "review")

    def test_battery_waits_for_automatic_work(self):
        ws = Workspace(self.root / "ws", self.root / "models", start=False,
                       allow_test_engines=True, battery=True, protector=keyprotect.TestProtector())
        self.ws = ws
        ws.settings.update(model=fake("sentences"))
        ws._import_one(self.rec)
        ws.start()
        time.sleep(1.5)
        self.assertEqual(ws.meetings()[0]["progress"], 0)
        self.assertIn("battery", ws.status()["message"])
        ws.set_settings({"battery_override": True})
        self.assertTrue(ws.wait_idle(120))
        self.assertEqual(ws.meetings()[0]["progress"], 100)

    # -- review, holds and cleanup -------------------------------------------------------

    def test_review_confirmation_removes_audio_and_locks_text(self):
        ws = self.open()
        mid = self.imported(ws)
        ws.confirm_reviewed(mid)
        self.assertTrue(ws.wait_idle(60))
        d = ws.detail(mid)
        self.assertFalse(d["audio_available"])
        self.assertEqual(d["stage"], "handoff")
        with self.assertRaisesRegex(WorkspaceError, "playback is no longer available"):
            ws.clip(mid, 1.0, 2.0)
        with self.assertRaises(WorkspaceError):
            ws.edit(mid, ws.transcript(mid)["items"][0]["id"], "late change")
        with self.assertRaises(WorkspaceError):
            ws.retranscribe(mid)
        self.assertGreater(ws.transcript(mid)["total"], 0)     # still readable for handoff

    def test_hold_release_needs_confirmation_and_reminders_show_it(self):
        ws = self.open()
        mid = self.imported(ws)
        hid = ws.add_hold(mid, "audio", "Client asked us to keep the recording")
        ws.confirm_reviewed(mid)
        self.assertTrue(ws.wait_idle(30))
        self.assertTrue(ws.detail(mid)["audio_available"])
        self.assertTrue(ws.meetings()[0]["held"])
        [h] = ws.detail(mid)["holds"]                           # review due 30 days after adding
        self.assertAlmostEqual(h["review_due_at"] - h["created_at"], 30 * 86400, delta=1)
        self.assertEqual(len(ws.release_hold(hid)), 1)
        self.assertTrue(ws.wait_idle(30))
        self.assertTrue(ws.detail(mid)["audio_available"])       # nothing removed silently
        [waiting] = ws.reminders()["awaiting_confirmation"]
        ws.confirm_cleanup(waiting["id"])
        self.assertTrue(ws.wait_idle(30))
        self.assertFalse(ws.detail(mid)["audio_available"])

    def test_final_confirmation_leaves_only_a_receipt(self):
        ws = self.open()
        mid = self.imported(ws)
        ws.confirm_reviewed(mid)
        ws.confirm_final_saved(mid)
        self.assertTrue(ws.wait_idle(60))
        self.assertEqual(ws.meetings(), [])
        receipts = ws.receipts()
        self.assertEqual(len(receipts), 2)
        self.assertFalse(any("synthetic" in r["result"] for r in receipts))
        with self.assertRaises(WorkspaceError):
            ws.detail(mid)

    def test_overdue_hold_is_listed_for_review(self):
        ws = self.open(start=False)
        ws._import_one(self.rec)
        mid = ws.meetings()[0]["id"]
        hid = ws.add_hold(mid, "all", "Dispute")
        with ws.store.tx() as db:
            db.execute("UPDATE holds SET reviewed_at = ?", (time.time() - 40 * 86400,))
        self.assertEqual([h["id"] for h in ws.reminders()["holds_due"]], [hid])
        ws.keep_hold(hid)
        self.assertEqual(ws.reminders()["holds_due"], [])

    def test_held_cleanup_does_not_block_other_meetings(self):
        ws = self.open()
        first = self.imported(ws)
        ws.add_hold(first, "audio", "dispute")
        ws.confirm_reviewed(first)
        ws.import_paths([str(write_wav(self.root / "second.wav", 30))])
        self.assertTrue(ws.wait_idle(120))
        second = next(m["id"] for m in ws.meetings() if m["id"] != first)
        ws.confirm_reviewed(second)
        self.assertTrue(ws.wait_idle(60))
        self.assertTrue(ws.detail(first)["audio_available"])
        self.assertFalse(ws.detail(second)["audio_available"])

    def test_missing_audio_stops_with_an_explanation(self):
        ws = self.open(start=False, speakers=True)
        ws._import_one(self.rec)
        mid = ws.meetings()[0]["id"]
        from workspace import media
        media.audio_path(ws.media, mid).unlink()           # e.g. quarantined
        ws.start()
        self.assertTrue(ws.wait_idle(30))
        [m] = ws.meetings()
        self.assertIn("missing", m["error"])

    def test_a_missing_audio_key_is_missing_audio_and_no_key_is_made(self):
        # #111 C3a: the audio file is encrypted under the meeting's audio key;
        # without the key it cannot be read, and a new key is never made.
        from workspace import app as app_module
        ws = self.open(start=False, speakers=True)
        ws._import_one(self.rec)
        mid = ws.meetings()[0]["id"]
        with ws.store.tx() as db:
            ws.store.destroy_keys(db, mid, ("audio",))
        self.assertTrue(media.audio_path(ws.media, mid).is_file())
        ws.start()
        self.assertTrue(ws.wait_idle(30))
        self.assertEqual(ws.store.meeting(mid)["last_error"], app_module.AUDIO_MISSING)
        self.assertEqual(ws.store.passage_count(ws.store.active_version(mid)["id"]), 0)
        self.assertEqual(ws.store._all("SELECT purpose FROM content_keys WHERE meeting_id = ? "
                                       "AND purpose = 'audio'", (mid,)), [])
        with self.assertRaises(WorkspaceError) as ctx:          # playback says so too
            ws.clip(mid, 0, 1)
        self.assertEqual(str(ctx.exception), app_module.AUDIO_UNREADABLE)

    def test_damaged_audio_stops_playback_and_transcription_with_fixed_wording(self):
        from workspace import app as app_module
        from workspace import runner
        ws = self.open(start=False, speakers=False)
        ws._import_one(self.rec)
        mid = ws.meetings()[0]["id"]
        path = media.audio_path(ws.media, mid)
        data = bytearray(path.read_bytes())
        data[100] ^= 1                                       # inside the first block
        path.write_bytes(bytes(data))
        with self.assertRaises(WorkspaceError) as ctx:
            ws.clip(mid, 0, 1)
        self.assertEqual(str(ctx.exception), app_module.AUDIO_UNREADABLE)
        self.assertNotIn(str(self.root), str(ctx.exception))
        ws.start()
        self.assertTrue(ws.wait_idle(30))
        m = ws.store.meeting(mid)
        self.assertEqual(m["phase"], "error")
        # The original's location is kept only with deletion authority (#24).
        self.assertEqual(m["last_error"], runner.AUDIO_DAMAGED_KEPT)
        vid = ws.store.active_version(mid)["id"]
        self.assertEqual([c["failures"] for c in ws.store.chunks(vid)],
                         [0] * len(ws.store.chunks(vid)))

    def test_discard_stops_work_and_removes_the_meeting_securely(self):
        import time as _time
        ws = self.open(start=False, model="sleepy")         # transcription in progress
        ws.start()
        self.assertEqual(ws.import_paths([str(self.rec)])["accepted"], 1)
        deadline = _time.time() + 60
        while _time.time() < deadline and not (ws._busy and ws._busy[1] == "transcribe"):
            _time.sleep(0.1)
        self.assertEqual(ws._busy[1], "transcribe")          # really mid-transcription
        [m] = ws.meetings()
        started = _time.time()
        ws.discard_meeting(m["id"])
        self.assertTrue(ws.wait_idle(120))
        self.assertEqual(ws.meetings(), [])
        self.assertFalse((ws.media / m["id"]).exists())
        self.assertEqual(sorted(r["result"] for r in ws.receipts()),
                         ["audio removed", "transcript removed"])
        self.assertLess(_time.time() - started, 25)        # stopped, not waited out (30 s)

    def test_discard_is_refused_while_held_after_review_or_while_importing(self):
        ws = self.open(start=False)
        ws._import_one(str(self.rec))
        [m] = ws.meetings()
        ws.add_hold(m["id"], "audio", "keep for review")
        with self.assertRaises(WorkspaceError):
            ws.discard_meeting(m["id"])
        hid = ws.store._one("SELECT id FROM holds")["id"]
        ws.release_hold(hid)
        with ws.store.tx() as db:                            # as if a watched folder allowed it
            db.execute("UPDATE sources SET delete_original = 1")
        ws.discard_meeting(m["id"])
        # The original's deletion authority is withdrawn before any job runs.
        self.assertEqual(ws.store._one("SELECT delete_original FROM sources")[0], 0)
        from workspace import cleanup
        reviewed = ws.store.create_meeting("reviewed")
        with ws.store.tx() as db:
            db.execute("UPDATE meetings SET reviewed_at = 1 WHERE id = ?", (reviewed,))
        with self.assertRaises(cleanup.CleanupError):
            cleanup.discard(ws.store, reviewed)               # use the final confirmation
        importing = ws.store.create_meeting("still importing")
        with self.assertRaises(cleanup.CleanupError):
            cleanup.discard(ws.store, importing)              # no version yet

    def test_an_import_that_never_finished_can_be_discarded_when_none_is_running(self):
        # #181: after Continue anyway, a meeting with no version stays listed;
        # Discard removes it once no import is running or queued.
        ws = self.open(start=False)
        unfinished = ws.store.create_meeting("import did not finish")
        ws._imports.put(("__recording__", str(self.root / "elsewhere")))   # one queued
        with self.assertRaisesRegex(WorkspaceError, "still being imported"):
            ws.discard_meeting(unfinished)
        ws._imports.get()
        ws._imports.task_done()                          # the queue is idle again
        ws.discard_meeting(unfinished)
        self.assertIsNone(ws.store._one("SELECT 1 FROM meetings WHERE id = ? AND "
                                        "final_saved_at IS NULL", (unfinished,)))
        ws.start()
        self.assertTrue(ws.wait_idle(60))
        self.assertIsNone(ws.store.meeting(unfinished))
        self.assertEqual(sorted(r["result"] for r in ws.receipts()),
                         ["audio removed", "transcript removed"])

    def test_models_can_be_removed_unless_still_needed(self):
        from workspace import asr_engines
        from workspace import gpu
        ws = self.open(start=False)
        cache = ws.cache_dir

        def put(path):
            path.mkdir(parents=True, exist_ok=True)
            (path / "model.bin").write_bytes(b"x" * 1000)
            return path
        v2 = put(asr_engines.model_dir(asr_engines.PARAKEET_MODELS["parakeet-tdt-0.6b-v2"], cache))
        v3 = put(asr_engines.model_dir(asr_engines.PARAKEET_MODELS["parakeet-tdt-0.6b-v3"], cache))
        v3_bad = put(v3.with_name(v3.name + ".invalid-20261015"))
        vad = put(asr_engines.model_dir(asr_engines.SILERO_VAD, cache))
        spk = asr_engines.SPEAKER_MODEL
        spk_dir = put(asr_engines.model_dir(spk, cache))
        spk_hub = put(Path(cache) / ("models--" + spk.hub_repos[0].replace("/", "--")))
        comp = put(gpu.component_dir(cache))
        ws.settings.update(model="parakeet-tdt-0.6b-v2", speakers=True)
        with self.assertRaises(WorkspaceError):
            ws.remove_model("parakeet-tdt-0.6b-v2")        # the selected speech model
        self.assertEqual(ws.remove_model("parakeet-tdt-0.6b-v3"), 0.0)   # GB freed (tiny files)
        self.assertFalse(v3.exists() or v3_bad.exists())    # damaged copies go too
        self.assertTrue(vad.exists())                       # still needed by v2
        ws.settings.update(model="large-v3-turbo")
        ws.remove_model("parakeet-tdt-0.6b-v2")
        self.assertFalse(v2.exists() or vad.exists())       # the last Parakeet takes VAD
        with self.assertRaises(WorkspaceError):
            ws.remove_model(spk.name)                       # speakers still switched on
        ws.settings.update(speakers=False)
        ws.remove_model(spk.name)
        self.assertFalse(spk_dir.exists() or spk_hub.exists())
        ws.settings.update(gpu_speakers=True)
        ws.remove_model(gpu.COMPONENT)
        self.assertFalse(comp.exists())
        self.assertFalse(ws.settings["gpu_speakers"])       # switched off with it
        with self.assertRaises(WorkspaceError):
            ws.remove_model("../../Windows")                # only known names

    def test_removal_never_follows_links_or_matches_other_models(self):
        import _winapi

        from workspace import asr_engines
        ws = self.open(start=False)
        cache = Path(ws.cache_dir)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep")
        v3 = asr_engines.model_dir(asr_engines.PARAKEET_MODELS["parakeet-tdt-0.6b-v3"], cache)
        v3.parent.mkdir(parents=True, exist_ok=True)
        _winapi.CreateJunction(str(outside), str(v3))       # the model folder is a junction
        with self.assertRaises(WorkspaceError) as e:
            ws.remove_model("parakeet-tdt-0.6b-v3")
        self.assertIn("link", str(e.exception))
        self.assertTrue((outside / "precious.txt").exists())
        os.rmdir(v3)                                         # removes the junction only
        # large-v3's check records must not match large-v3-turbo's.
        marks = cache / "whisper" / ".verified"
        marks.mkdir(parents=True)
        turbo_mark = marks / ("faster-whisper-large-v3-turbo-" + "a" * 16 + ".json")
        large_mark = marks / ("faster-whisper-large-v3-" + "b" * 16 + ".json")
        turbo_mark.write_text("{}")
        large_mark.write_text("{}")
        ws.remove_model("large-v3")
        self.assertTrue(turbo_mark.exists())
        self.assertFalse(large_mark.exists())

    def test_a_model_still_transcribing_a_meeting_is_not_removed(self):
        from workspace import asr_engines
        ws = self.open(start=False)
        ws.settings.update(model="parakeet-tdt-0.6b-v3")
        ws._import_one(str(self.rec))                       # pending chunks, v3 version
        ws.settings.update(model="parakeet-tdt-0.6b-v2")
        folder = asr_engines.model_dir(asr_engines.PARAKEET_MODELS["parakeet-tdt-0.6b-v3"],
                                       ws.cache_dir)
        folder.mkdir(parents=True)
        with self.assertRaises(WorkspaceError) as e:
            ws.remove_model("parakeet-tdt-0.6b-v3")
        self.assertIn("still being transcribed", str(e.exception))
        self.assertTrue(folder.exists())

    def test_pausing_a_speaker_rerun_keeps_it_and_its_count(self):
        import time as _time
        ws = self.open()
        mid = self.imported(ws)
        ws.speaker_model = fake("diarizer_sleepy")
        ws.rerun_speakers(mid, 3)
        deadline = _time.time() + 60
        while _time.time() < deadline and not (ws._busy and ws._busy[1] == "rerun"):
            _time.sleep(0.1)
        ws.pause(mid)
        self.assertTrue(ws.wait_idle(30))
        self.assertEqual(ws._reruns.get(mid), 3)            # kept, with its count
        _time.sleep(1)
        self.assertIsNone(ws._busy)                          # waits while paused
        ws.speaker_model = fake("fake_diarizer")
        ws.resume(mid)
        self.assertTrue(ws.wait_idle(60))
        runs = ws.store.speaker_runs(ws.store.active_version(mid)["id"])
        self.assertEqual(runs[-1]["status"], "done")
        self.assertEqual(runs[-1]["hint_count"], 3)

    def test_speaker_identification_can_be_paused_and_resumed(self):
        import time as _time
        ws = self.open(start=False)
        ws.speaker_model = fake("diarizer_sleepy")          # a long speaker pass
        ws.start()
        self.assertEqual(ws.import_paths([str(self.rec)])["accepted"], 1)
        deadline = _time.time() + 120
        while _time.time() < deadline and not (ws._busy and ws._busy[1] == "speakers"):
            _time.sleep(0.1)
        [m] = ws.meetings()
        self.assertEqual(m["busy"], "speakers")             # shown in the list
        ws.pause(m["id"])                                   # stops the pass at once
        self.assertTrue(ws.wait_idle(30))
        vid = ws.store.active_version(m["id"])["id"]
        self.assertEqual([r["status"] for r in ws.store.speaker_runs(vid)], ["interrupted"])
        _time.sleep(1)
        self.assertIsNone(ws._busy)                         # paused: it does not restart
        self.assertTrue(ws.meetings()[0]["paused"])
        ws.speaker_model = fake("fake_diarizer")
        ws.resume(m["id"])                                  # starts it again
        self.assertTrue(ws.wait_idle(120))
        self.assertEqual([r["status"] for r in ws.store.speaker_runs(vid)][-1], "done")
        self.assertTrue(ws.transcript(m["id"])["items"])    # the transcript is untouched

    # -- skipping speaker identification ------------------------------------------------

    def wait_for_speakers(self, ws):
        """Until a speaker pass has really started: its run is recorded (_busy
        is set a moment earlier, before the pass is begun)."""
        import time as _time
        deadline = _time.time() + 120
        while _time.time() < deadline:
            for m in ws.meetings():
                v = ws.store.active_version(m["id"])
                if v and any(r["status"] == "running" for r in ws.store.speaker_runs(v["id"])):
                    return
            _time.sleep(0.1)
        self.fail("No speaker pass started.")

    def transcribed(self, ws):
        """Import and transcribe on this thread; returns (meeting id, next job)."""
        ws._import_one(self.rec)
        [m] = ws.meetings()
        job = ws._next_work()
        while job and job[0] == "transcribe":
            ws._run(*job)
            job = ws._next_work()
        return m["id"], job

    def test_a_skip_after_the_pass_was_chosen_stops_it_before_it_starts(self):
        ws = self.open(start=False)
        mid, job = self.transcribed(ws)
        self.assertEqual(job[:2], ("speakers", mid))
        self.assertTrue(ws.meetings()[0]["can_skip"])
        ws.skip_speakers(mid)                     # after the choice, before the pass starts
        ws._run(*job)
        vid = ws.store.active_version(mid)["id"]
        self.assertEqual([r["status"] for r in ws.store.speaker_runs(vid)], ["skipped"])
        self.assertFalse(any(i["who"] for i in ws.transcript(mid)["items"]))
        self.assertFalse(ws.meetings()[0]["can_skip"])      # skipped already
        self.assertIsNone(ws._next_work())

    def test_a_skip_cancels_an_identify_again_already_chosen(self):
        ws = self.open(start=False)
        mid, job = self.transcribed(ws)
        ws.skip_speakers(mid)
        ws.rerun_speakers(mid)
        self.assertTrue(ws.detail(mid)["can_skip"])         # queued: Skip cancels it
        job = ws._next_work()
        self.assertEqual(job[:2], ("rerun", mid))
        ws.skip_speakers(mid)                     # after the choice, before the pass starts
        ws._run(*job)
        vid = ws.store.active_version(mid)["id"]
        self.assertEqual([r["status"] for r in ws.store.speaker_runs(vid)],
                         ["skipped", "skipped"])
        self.assertIsNone(ws._next_work())

    def test_an_interrupted_pass_that_will_not_restart_says_so(self):
        from workspace.store import SPEAKER_RESTART_NOTE
        ws = self.open(start=False)
        mid, _ = self.transcribed(ws)
        ws.skip_speakers(mid)
        vid = ws.store.active_version(mid)["id"]
        rid = ws.store.start_speaker_run(vid)     # Identify again, then the app closed mid-pass
        ws.store.end_speaker_run(rid, "interrupted", SPEAKER_RESTART_NOTE)
        s = ws.detail(mid)["speakers"]
        self.assertEqual(s["status"], "interrupted")
        self.assertIn("Use Identify again", s["error"])
        ws._reruns[mid] = None                    # queued again: it will start by itself
        self.assertEqual(ws.detail(mid)["speakers"]["error"], SPEAKER_RESTART_NOTE)
        del ws._reruns[mid]
        ws.confirm_reviewed(mid)                  # reviewed: Identify again is not offered
        self.assertNotIn("Identify again", ws.detail(mid)["speakers"]["error"])

    def test_a_paused_identify_again_is_not_queued_again_after_a_skip(self):
        from unittest import mock

        from workspace import speakers
        ws = self.open(start=False)
        mid, _ = self.transcribed(ws)
        ws.skip_speakers(mid)
        ws.rerun_speakers(mid)
        job = ws._next_work()
        vid = ws.store.active_version(mid)["id"]

        def paused_then_skipped(*a, **kw):
            # The user pauses the pass, then skips before it is put back in the queue.
            ws.store.set_paused(mid, True)
            ws.store.skip_speaker_run(vid, again=True)
            return speakers.SpeakerResult.STOPPED
        with mock.patch.object(speakers, "run_speakers", side_effect=paused_then_skipped):
            ws._run(*job)
        self.assertNotIn(mid, ws._reruns)

    def test_skip_stops_a_running_pass_and_it_never_restarts(self):
        import time as _time

        from workspace import handoff
        ws = self.open(start=False)
        ws.speaker_model = fake("diarizer_sleepy")          # a long speaker pass
        ws.start()
        self.assertEqual(ws.import_paths([str(self.rec)])["accepted"], 1)
        self.wait_for_speakers(ws)
        [m] = ws.meetings()
        ws.skip_speakers(m["id"])
        ws.skip_speakers(m["id"])                           # a double click is harmless
        self.assertTrue(ws.wait_idle(30))
        _time.sleep(1)
        self.assertIsNone(ws._busy)                         # nothing restarts it
        vid = ws.store.active_version(m["id"])["id"]
        self.assertEqual([r["status"] for r in ws.store.speaker_runs(vid)],
                         ["superseded", "skipped"])
        [m] = ws.meetings()
        self.assertEqual((m["stage"], m["busy"], m["paused"]), ("review", None, False))
        self.assertEqual(ws.detail(m["id"])["speakers"]["status"], "skipped")
        page = ws.transcript(m["id"])
        self.assertTrue(page["items"])
        self.assertFalse(any(i["who"] for i in page["items"]))     # no labels
        # Straight on to review and hand off, with no speaker labels.
        ws.confirm_reviewed(m["id"])
        tmpl = ws.templates.get("summary")
        out = handoff.build(ws.store, m["id"], tmpl)
        self.assertIn("Speakers (as labelled): not identified", out["markdown"])

    def test_skip_before_the_text_is_ready_means_no_pass_at_all(self):
        ws = self.open(start=False)
        ws._import_one(self.rec)            # imported and planned; nothing processed yet
        [m] = ws.meetings()
        self.assertEqual(m["stage"], "inbox")
        ws.skip_speakers(m["id"])
        ws.start()
        self.assertTrue(ws.wait_idle(120))
        vid = ws.store.active_version(m["id"])["id"]
        self.assertEqual([r["status"] for r in ws.store.speaker_runs(vid)], ["skipped"])
        self.assertEqual(ws.meetings()[0]["stage"], "review")

    def test_skip_resumes_a_paused_pass_into_review(self):
        ws = self.open(start=False)
        ws.speaker_model = fake("diarizer_sleepy")
        ws.start()
        self.assertEqual(ws.import_paths([str(self.rec)])["accepted"], 1)
        self.wait_for_speakers(ws)
        [m] = ws.meetings()
        ws.pause(m["id"])
        self.assertTrue(ws.wait_idle(30))
        self.assertTrue(ws.meetings()[0]["paused"])
        ws.skip_speakers(m["id"])                           # Paused was only for speakers
        self.assertFalse(ws.meetings()[0]["paused"])
        self.assertTrue(ws.wait_idle(30))
        self.assertIsNone(ws._busy)

    def test_speakers_can_be_identified_after_a_skip(self):
        ws = self.open(start=False)
        ws.speaker_model = fake("diarizer_sleepy")
        ws.start()
        self.assertEqual(ws.import_paths([str(self.rec)])["accepted"], 1)
        self.wait_for_speakers(ws)
        [m] = ws.meetings()
        ws.skip_speakers(m["id"])
        self.assertTrue(ws.wait_idle(30))
        ws.speaker_model = fake("fake_diarizer")
        ws.rerun_speakers(m["id"])
        self.assertTrue(ws.wait_idle(120))
        self.assertEqual(ws.detail(m["id"])["speakers"]["status"], "done")
        self.assertTrue(all(i["who"] for i in ws.transcript(m["id"])["items"]))

    def test_skip_is_refused_once_labelled_or_reviewed(self):
        ws = self.open()
        mid = self.imported(ws)                             # speakers done
        self.assertFalse(ws.detail(mid)["can_skip"])        # the page offers no Skip
        self.assertFalse(ws.meetings()[0]["can_skip"])
        with self.assertRaises(WorkspaceError):
            ws.skip_speakers(mid)
        ws.confirm_reviewed(mid)
        with self.assertRaises(WorkspaceError):
            ws.skip_speakers(mid)
        with self.assertRaises(WorkspaceError):
            ws.skip_speakers("0" * 32)

    def test_transcribing_again_keeps_the_skip(self):
        ws = self.open(start=False)
        ws.speaker_model = fake("diarizer_sleepy")
        ws.start()
        self.assertEqual(ws.import_paths([str(self.rec)])["accepted"], 1)
        self.wait_for_speakers(ws)
        [m] = ws.meetings()
        ws.skip_speakers(m["id"])
        self.assertTrue(ws.wait_idle(30))
        ws.settings.update(model=fake("sentences_v2"))
        ws.retranscribe(m["id"])
        self.assertTrue(ws.wait_idle(120))
        vid = ws.store.active_version(m["id"])["id"]
        self.assertEqual([r["status"] for r in ws.store.speaker_runs(vid)], ["skipped"])

    def test_gpu_component_downloads_while_a_cpu_speaker_pass_runs(self):
        from unittest import mock

        from workspace import gpu
        ws = self.open(start=False)
        started = []
        ws.downloads.start = lambda name, token=None, on_success=None: started.append(name)
        ws._busy = ("x" * 32, "speakers")                    # a CPU speaker pass
        ws.download_model(gpu.COMPONENT)
        self.assertEqual(started, [gpu.COMPONENT])
        with self.assertRaises(WorkspaceError):
            ws.download_model("parakeet-tdt-0.6b-v2")       # speech models still wait
        with mock.patch.object(ws, "_gpu_in_use", return_value=True):
            with self.assertRaises(WorkspaceError):
                ws.download_model(gpu.COMPONENT)            # not while the GPU is in use
        ws._busy = None

    def test_speaker_rerun_runs_on_the_processing_thread_and_not_after_review(self):
        ws = self.open()
        mid = self.imported(ws)
        ws.rerun_speakers(mid, 3)
        ws.rerun_speakers(mid, 3)                           # double click: one pass
        self.assertTrue(ws.wait_idle(60))
        runs = ws.store.speaker_runs(ws.store.active_version(mid)["id"])
        self.assertEqual([r["status"] for r in runs], ["superseded", "done"])
        self.assertEqual(runs[-1]["hint_count"], 3)
        ws.add_hold(mid, "audio", "keep")
        ws.confirm_reviewed(mid)
        with self.assertRaises(WorkspaceError):
            ws.rerun_speakers(mid, 2)

    def test_interrupted_import_is_finished_or_removed_at_startup(self):
        ws = self.open(start=False)
        from workspace import media
        mid, _ = media.import_recording(ws.store, ws.media, self.rec, "cut short")
        orphan = ws.store.create_meeting("never got a source")
        ws = self.reopen(start=False)
        ids = [m["id"] for m in ws.meetings()]
        self.assertEqual(ids, [mid])                        # finished, orphan removed
        self.assertNotIn(orphan, ids)
        self.assertIsNotNone(ws.store.active_version(mid))

    def test_speaker_names_flow_into_transcript_and_snapshot(self):
        ws = self.open()
        mid = self.imported(ws)
        [a, b] = ws.speakers(mid)["speakers"]
        ws.rename_speaker(mid, a["label"], "Aroha")
        items = ws.transcript(mid)["items"]
        self.assertEqual(items[0]["who"], "Aroha")
        target = next(i for i in items if i["label"] == b["label"])
        ws.assign_speaker(mid, target["id"], a["label"])
        self.assertEqual(next(i for i in ws.transcript(mid)["items"]
                              if i["id"] == target["id"])["who"], "Aroha")
        ex = ws.speaker_examples(mid, a["label"])
        self.assertEqual(len(ex["items"]), 3)
        ws.confirm_reviewed(mid)
        self.assertTrue(ws.wait_idle(60))
        self.assertIn("Aroha: ", ws.store.snapshot(mid))
        with self.assertRaises(WorkspaceError):
            ws.rename_speaker(mid, a["label"], "Late change")
        ws.confirm_final_saved(mid)
        self.assertTrue(ws.wait_idle(60))
        self.assertEqual(ws.store._one("SELECT COUNT(*) AS n FROM speaker_corrections")["n"], 0)

    def test_meeting_facts_and_handoff_name_replacement(self):
        from workspace import clipboard
        ws = self.open()
        mid = self.imported(ws)
        row = next(m for m in ws.meetings() if m["id"] == mid)
        self.assertGreater(row["duration_s"], 0)
        self.assertEqual((row["speaker_count"], bool(row["model"])), (2, True))
        d = ws.detail(mid)
        self.assertEqual((d["duration_s"], d["speaker_count"]), (row["duration_s"], 2))
        self.assertEqual((row["audio_available"], row["cleanup"]), (True, []))
        self.assertIn(ws.status()["mode"], ("quiet", "faster"))
        [a, b] = ws.speakers(mid)["speakers"]
        self.assertTrue(a["seconds"] > 0 and b["seconds"] > 0)
        ws.rename_speaker(mid, a["label"], "Aroha")
        ws.rename_speaker(mid, b["label"], "Benny")
        ws.rename_speaker(mid, b["label"], "Ben")            # an earlier name is replaced too
        tid = ws.save_template(None, "Ours", "Write minutes for Aroha's team. Benny chairs.")
        first = ws.transcript(mid)["items"][0]
        self.assertEqual(first["who"], "Aroha")               # Aroha speaks first
        ws.edit(mid, first["id"], "Thanks aroha and BEN, welcome. Benefits next, says Benny.")
        ws.rename(mid, "Catch-up with Aroha")
        ws.confirm_reviewed(mid)
        self.assertTrue(ws.wait_idle(60))
        plain = ws.handoff_preview(mid, "minutes")
        anon = ws.handoff_preview(mid, "minutes", True)
        self.assertIn("**Aroha:**", plain["markdown"])
        self.assertFalse(plain["anonymised"])
        self.assertTrue(anon["anonymised"])
        self.assertNotIn("aroha", anon["combined"].casefold())
        self.assertIn("**Speaker 1:**", anon["markdown"])
        # Whole words only: "Benefits" is not a name.
        self.assertIn("Thanks Speaker 1 and Speaker 2, welcome. Benefits next, says Speaker 2.",
                      anon["markdown"])
        self.assertIn("# Catch-up with Speaker 1", anon["markdown"])
        self.assertNotIn("Aroha", anon["filename"])
        self.assertIn("full names were replaced", anon["markdown"])
        ours = ws.handoff_preview(mid, tid, True)
        self.assertIn("Write minutes for Speaker 1's team. Speaker 2 chairs.", ours["prompt_only"])
        copied = []
        real = clipboard.copy_text
        clipboard.copy_text = copied.append
        try:
            with self.assertRaisesRegex(WorkspaceError, "changed since the preview"):
                ws.copy_handoff(mid, "minutes", "combined", anon["digests"]["combined"])
            ws.copy_handoff(mid, "minutes", "combined", anon["digests"]["combined"], True)
        finally:
            clipboard.copy_text = real
        self.assertEqual(copied, [anon["combined"]])
        with self.assertRaises(WorkspaceError):
            ws.handoff_preview(mid, "minutes", "yes")
        ws.confirm_final_saved(mid)
        self.assertTrue(ws.wait_idle(60))

    def test_handoff_copy_and_save_after_review(self):
        from workspace import clipboard
        ws = self.open()
        mid = self.imported(ws)
        with self.assertRaises(WorkspaceError):
            ws.handoff_preview(mid, "minutes")               # review first
        ws.confirm_reviewed(mid)
        self.assertTrue(ws.wait_idle(60))
        copied = []
        real = clipboard.copy_text
        clipboard.copy_text = copied.append
        preview = ws.handoff_preview(mid, "minutes")
        summary = ws.handoff_preview(mid, "summary")
        try:
            r = ws.copy_handoff(mid, "minutes", "combined", preview["digests"]["combined"])
            ws.copy_handoff(mid, "summary", "prompt_only", summary["digests"]["prompt_only"])
            with self.assertRaisesRegex(WorkspaceError, "changed since the preview"):
                ws.copy_handoff(mid, "minutes", "combined", "0" * 64)
        finally:
            clipboard.copy_text = real
        self.assertEqual(copied[0], preview["combined"])      # exactly what was previewed
        self.assertEqual(r["chars"], len(preview["combined"]))
        self.assertNotIn("sentence", copied[1])
        md_digest = preview["digests"]["markdown"]
        out = ws.save_handoff_to(mid, "minutes", str(self.root / "handoff"), md_digest)
        saved = (self.root / "handoff.md").read_bytes().decode("utf-8")
        self.assertEqual((out["name"], saved), ("handoff.md", preview["markdown"]))  # LF kept
        (self.root / "plan v1.2.md").write_text("theirs")
        with self.assertRaisesRegex(WorkspaceError, "already exists"):
            ws.save_handoff_to(mid, "minutes", str(self.root / "plan v1.2"), md_digest)
        self.assertEqual((self.root / "plan v1.2.md").read_text(), "theirs")
        with self.assertRaises(WorkspaceError):
            ws.copy_handoff(mid, "minutes", "everything", "x")
        ws.confirm_final_saved(mid)
        self.assertTrue(ws.wait_idle(60))
        self.assertTrue((self.root / "handoff.md").exists())  # the user's file is theirs

    def test_copy_and_open_copies_the_preview_and_opens_only_the_fixed_address(self):
        # #109: the exact previewed text goes to the clipboard, then the fixed
        # address opens; no transcript word ever reaches the address.
        from workspace import clipboard, handoff
        ws = self.open()
        mid = self.imported(ws)
        ws.rename(mid, "MARKERTITLE meeting")
        ws.confirm_reviewed(mid)
        self.assertTrue(ws.wait_idle(60))
        opened, copied = [], []
        ws._opener = opened.append
        preview = ws.handoff_preview(mid, "minutes", True)
        self.assertEqual(preview["assistant"], {"name": "ChatGPT", "host": "chatgpt.com"})
        with mock.patch.object(clipboard, "copy_text", side_effect=copied.append):
            with self.assertRaisesRegex(WorkspaceError, "changed since the preview"):
                ws.copy_and_open_assistant(mid, "minutes", "0" * 64, True)
            self.assertEqual(opened, [])
            r = ws.copy_and_open_assistant(mid, "minutes", preview["digests"]["combined"], True)
        self.assertEqual(copied, [preview["combined"]])
        self.assertEqual(opened, [handoff.DEFAULT_ASSISTANT[1]])
        self.assertTrue(r["opened"])
        self.assertNotIn("MARKERTITLE", opened[0])            # the address as is, nothing added
        ws.confirm_final_saved(mid)
        self.assertTrue(ws.wait_idle(60))

    # -- intake ---------------------------------------------------------------------

    def test_refused_imports_explain_without_paths(self):
        ws = self.open(start=False)
        text = self.root / "notes.txt"
        text.write_text("not audio")
        r = ws.import_paths([str(text), r"\\server\share\a.wav", "relative.wav",
                             str(self.root / "missing.wav")])
        self.assertEqual(r["accepted"], 0)
        self.assertEqual(len(r["refused"]), 4)
        for message in r["refused"]:
            self.assertNotIn(str(self.root), message)

    def test_damaged_recording_is_reported_and_leaves_nothing(self):
        ws = self.open()
        bad = self.root / "broken.wav"
        bad.write_bytes(b"RIFF" + b"\0" * 400)
        ws.import_paths([str(bad)])
        self.assertTrue(ws.wait_idle(30))
        self.assertEqual(ws.meetings(), [])
        [message] = ws.status()["import_errors"]
        self.assertNotIn(str(self.root), message)

    # -- long transcripts -----------------------------------------------------------

    def test_long_transcript_pages_quickly(self):
        ws = self.open(start=False)
        ws._import_one(self.rec)
        mid = ws.meetings()[0]["id"]
        vid = ws.store.active_version(mid)["id"]
        idx = ws.store.chunks(vid, status="pending")[0]["idx"]
        # About 4 hours of short passages, written through the store (#111).
        self.assertTrue(ws.store.commit_chunk(vid, idx, [
            types.SimpleNamespace(start=i * 3.0, end=i * 3.0 + 2.5,
                                  text=f"passage {i} with some words", words=())
            for i in range(5000)]))
        t = time.perf_counter()
        page = ws.transcript(mid, 4000, 1000)
        hits = ws.search(mid, "passage 4999")
        elapsed = time.perf_counter() - t
        self.assertEqual((page["total"], len(page["items"])), (5000, 1000))
        self.assertEqual(hits, [ws.transcript(mid, 4999, 1)["items"][0]["id"]])
        self.assertLess(elapsed, 2.0)

    def test_search_in_a_growing_transcript_finds_the_new_passages(self):
        # tools/workspace_ui_check.py's growing transcript, without the page:
        # a long first chunk, a search with no match yet, then the chunk that
        # matches committed while the meeting is open (every value sealed,
        # #111). Search finds the new passages, by id in transcript order, in
        # the edited text where there is an edit.
        ws = self.open(start=False)
        ws._import_one(write_wav(self.root / "synthetic-growing.wav", 360))
        mid = ws.meetings()[0]["id"]
        vid = ws.store.active_version(mid)["id"]
        pending = [c["idx"] for c in ws.store.chunks(vid, status="pending")]
        self.assertGreaterEqual(len(pending), 3)

        def commit(idx, n):
            chunk = next(c for c in ws.store.chunks(vid) if c["idx"] == idx)
            start, end = chunk["start_sample"] / SR, chunk["end_sample"] / SR
            gap = (end - start) / n
            self.assertTrue(ws.store.commit_chunk(vid, idx, [
                types.SimpleNamespace(start=start + i * gap, end=start + (i + 0.8) * gap,
                                      text=f"growing passage {idx}.{i} about the plan", words=())
                for i in range(n)]))

        first, target, later = pending[0], pending[1], pending[-1]
        commit(first, 3000)
        query = f"growing passage {target}."
        self.assertEqual(ws.search(mid, query), [])
        commit(later, 20)                               # a later chunk first: still none
        self.assertEqual(ws.search(mid, query), [])
        commit(target, 20)
        rows = ws.transcript(mid, 0, 4000)["items"]
        new = [r["id"] for r in rows if r["text"].startswith(query)]
        self.assertEqual(len(new), 20)
        self.assertEqual(ws.search(mid, query), new)
        self.assertEqual(ws.search(mid, query.upper()), new)
        # An edit is what is searched: one new passage edited away, one old one in.
        old = rows[0]["id"]
        ws.edit(mid, new[0], "Edited while transcribing")
        ws.edit(mid, old, f"Now about {query} as well")
        self.assertEqual(ws.search(mid, query), [old] + new[1:])
        self.assertEqual(ws.search(mid, "edited while"), [new[0]])
        self.assertEqual(len(ws.search(mid, "growing passage")), 500)    # store's limit


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False,
                            allow_test_engines=True, battery=False,
                            protector=keyprotect.TestProtector())
        self.ws.settings.update(model=fake("sentences"), speakers=False)
        self.ws._import_one(write_wav(root / "a.wav", 20))
        self.mid = self.ws.meetings()[0]["id"]
        self.api = api.make_api(self.ws)

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def test_facade_exposes_only_the_allowed_names(self):
        public = {n for n in dir(type(self.api)) if not n.startswith("_")}
        self.assertEqual(public | {"pywebviewEventHandler"}, api.API_METHODS)
        self.assertFalse(hasattr(self.api, "__dict__"))

    def test_copy_and_open_takes_no_address_from_the_page(self):
        # #109: the page names the meeting, template, digest and anonymise flag
        # only; the shell's guard refuses any other argument before the call.
        import inspect
        params = list(inspect.signature(type(self.api).copy_and_open_assistant).parameters)
        self.assertEqual(params, ["self", "mid", "tid", "expected", "anonymise"])
        opened = []
        self.ws._opener = opened.append
        r = self.api.copy_and_open_assistant(self.mid, "minutes", "x")   # not reviewed yet
        self.assertFalse(r["ok"])
        self.assertNotIn("Traceback", str(r))
        self.assertEqual(opened, [])

    def test_errors_are_plain_results(self):
        r = self.api.detail("x" * 32)
        self.assertEqual(r, {"ok": False, "error": "Meeting not found."})
        r = self.api.transcript(self.mid, -1, 10)
        self.assertFalse(r["ok"])
        r = self.api.edit(self.mid, {"__class__": 1}, "x")
        self.assertFalse(r["ok"])
        self.assertNotIn("Traceback", str(r))

    def test_zoom_accepts_only_known_steps_and_needs_a_window(self):
        for bad in ("bigger", 2, None, {"__class__": 1}):
            self.assertEqual(self.api.zoom(bad), {"ok": False, "error": "Unknown zoom."})
        self.assertEqual(self.api.zoom("in"), {"ok": False, "error": "Zoom is not available."})

    def test_clip_is_a_bounded_data_url(self):
        r = self.api.clip(self.mid, 0, 2)
        self.assertTrue(r["value"].startswith("data:audio/wav;base64,"))
        import base64
        import io
        import wave
        with wave.open(io.BytesIO(base64.b64decode(r["value"].split(",", 1)[1]))) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(),
                              w.getnframes()), (16000, 1, 2, 2 * 16000))
        self.assertFalse(self.api.clip(self.mid, 0, 60)["ok"])
        self.assertFalse(self.api.clip(self.mid, "0", 2)["ok"])

    def test_import_dialog_without_window_is_refused(self):
        self.assertFalse(self.api.import_dialog()["ok"])

    def test_hold_ids_are_validated(self):
        for bad in ("../x", 5, None, "a" * 33):
            self.assertFalse(self.api.release_hold(bad)["ok"])
        self.assertTrue(self.api.add_hold(self.mid, "all", "reason")["ok"])
        self.assertEqual(len(new_id()), 32)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class NativeConfirmationTests(unittest.TestCase):
    """Starting a recording, allowing deletion of originals and discarding a
    meeting need a Yes in a question the page cannot answer (#111 A6)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False,
                            allow_test_engines=True, battery=False,
                            protector=keyprotect.TestProtector())
        self.ws.settings.update(model=fake("sentences"), speakers=False)
        self.ws._import_one(write_wav(root / "a.wav", 20))
        self.mid = self.ws.meetings()[0]["id"]
        self.asked, self.answer = [], False

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def bridge(self, **kw):
        def confirm(question):
            self.asked.append(question)
            return self.answer
        return api.make_api(self.ws, confirm=confirm, **{"decline_pause_s": 0, **kw})

    def test_recording_asks_first_and_passes_the_record_on(self):
        from unittest import mock

        from workspace import consent
        b = self.bridge()
        with mock.patch.object(self.ws, "check_can_record"), \
                mock.patch.object(self.ws, "start_recording", return_value={"ok": 1}) as start:
            self.assertEqual(b.start_recording(3, "Mic")["value"], api.CANCELLED)
            start.assert_not_called()                       # No: nothing starts
            self.answer = True
            self.assertEqual(b.start_recording(3, "Mic")["value"], {"ok": 1})
        self.assertEqual(self.asked[0], consent.recording_question())
        self.assertTrue(self.asked[0]["check"])            # a statement to tick first
        record = start.call_args.kwargs["consent"]
        self.assertEqual((record["confirmed"], record["wording"]),
                         (True, consent.RECORDING_WORDING_VERSION))

    def test_a_refused_recording_never_asks(self):
        b = self.bridge()
        r = b.start_recording(3, "Mic")                     # microphone is off in Settings
        self.assertFalse(r["ok"])
        r = b.start_recording(True, "Mic")                  # not a device number
        self.assertFalse(r["ok"])
        self.assertEqual(self.asked, [])

    def test_policy_without_confirmation_does_not_ask(self):
        from unittest import mock
        self.ws.policy = policy.PolicySource(reader=lambda: ({"RequireRecordingConsent": False},
                                                             ()))
        b = self.bridge()
        with mock.patch.object(self.ws, "check_can_record"), \
                mock.patch.object(self.ws, "start_recording", return_value={}) as start:
            b.start_recording(3, "Mic")
        self.assertEqual(self.asked, [])
        self.assertIsNone(start.call_args.kwargs["consent"])

    def test_discarding_asks_with_the_title(self):
        b = self.bridge()
        self.assertEqual(b.discard_meeting(self.mid)["value"], api.CANCELLED)
        self.assertIsNotNone(self.ws.store.meeting(self.mid))          # still there
        title = self.ws.meeting_for_discard(self.mid)[0]
        self.assertIn(title, self.asked[0]["detail"])       # named from the app's records
        self.assertEqual((self.asked[0]["style"], self.asked[0]["action"]), ("danger", "Discard"))
        self.assertIn("cannot remove copies", self.asked[0]["limits"])
        self.answer = True
        self.assertTrue(b.discard_meeting(self.mid)["ok"])
        self.assertFalse(b.discard_meeting("0" * 32)["ok"])            # unknown: not asked
        self.assertEqual(len(self.asked), 2)

    def test_allowing_deletion_names_the_folder_and_turning_it_off_does_not_ask(self):
        import os
        b = self.bridge()
        folder = Path(self.tmp.name) / "Recordings"
        folder.mkdir()
        fid = self.ws.add_watched_folder(str(folder), False)
        self.assertEqual(b.set_folder_authority(fid, True, None)["value"], api.CANCELLED)
        self.assertIn(os.path.realpath(folder), self.asked[0]["detail"])   # from the app
        b.set_folder_authority(fid, False, None)                   # taking it away: no question
        b.set_folder_authority(fid, None, True)                    # cloud downloads: no question
        self.assertEqual(len(self.asked), 1)
        self.answer = True
        self.assertTrue(b.set_folder_authority(fid, True, None)["ok"])
        self.assertEqual(len(self.asked), 2)
        self.ws.policy = policy.PolicySource(reader=lambda: ({"AllowDeleteOriginals": False}, ()))
        self.assertIn("organisation", b.set_folder_authority(fid, True, None)["error"])
        self.assertEqual(len(self.asked), 2)                       # refused before asking

    def test_refused_grants_never_ask(self):
        b = self.bridge()
        self.assertFalse(b.set_folder_authority("f" * 32, True, None)["ok"])   # unknown folder
        self.assertFalse(b.set_folder_authority("../x", True, None)["ok"])     # not an id
        self.assertFalse(b.set_folder_authority("f" * 32, 1, None)["ok"])      # not a yes/no
        self.assertEqual(self.asked, [])

    def test_the_app_refuses_a_grant_nobody_confirmed(self):
        folder = Path(self.tmp.name) / "Recordings"
        folder.mkdir()
        fid = self.ws.add_watched_folder(str(folder), False)
        with self.assertRaisesRegex(WorkspaceError, "confirmation"):
            self.ws.set_folder_authority(fid, True)
        self.ws.set_folder_authority(fid, False)                   # taking it away needs none

    def test_after_a_no_the_same_question_waits_a_moment(self):
        b = self.bridge(decline_pause_s=60)
        self.assertEqual(b.discard_meeting(self.mid)["value"], api.CANCELLED)
        self.answer = True
        self.assertEqual(b.discard_meeting(self.mid)["value"], api.CANCELLED_WAIT)  # not asked
        self.assertEqual(len(self.asked), 1)
        self.assertIsNotNone(self.ws.store.meeting(self.mid))

    def test_one_question_at_a_time(self):
        import threading
        from unittest import mock

        from workspace import shell
        opened, release = threading.Event(), threading.Event()

        def slow_question(window, question, theme):
            opened.set()
            release.wait(10)
            return True
        with mock.patch.object(shell, "_ask", side_effect=slow_question):
            first = {}
            t = threading.Thread(target=lambda: first.update(answer=shell.native_confirm(
                object(), {"title": "First"}, theme="light")))
            t.start()
            self.assertTrue(opened.wait(10))
            self.assertFalse(shell.native_confirm(object(), {"title": "Second"},
                                                  theme="light"))      # not shown
            release.set()
            t.join(10)
        self.assertTrue(first["answer"])
        self.assertFalse(shell._asking.locked())

    def test_without_a_window_nothing_is_confirmed(self):
        from unittest import mock
        b = api.make_api(self.ws)                                  # no window, no test hook
        self.assertEqual(b.discard_meeting(self.mid)["value"], api.CANCELLED)
        self.assertIsNotNone(self.ws.store.meeting(self.mid))
        folder = Path(self.tmp.name) / "Recordings"
        folder.mkdir()
        fid = self.ws.add_watched_folder(str(folder), False)
        self.assertEqual(b.set_folder_authority(fid, True, None)["value"], api.CANCELLED)
        with mock.patch.object(self.ws, "check_can_record"), \
                mock.patch.object(self.ws, "start_recording") as start:
            self.assertEqual(b.start_recording(3, "Mic")["value"], api.CANCELLED)
        start.assert_not_called()

    def test_the_app_checks_the_consent_record(self):
        from unittest import mock

        from workspace import consent
        good = consent.recording_record()
        for bad in ({}, {"confirmed": "yes", "wording": 1, "at": 1.0},
                    {**good, "wording": 999}, "yes"):
            with mock.patch.object(self.ws, "_need_microphone"), \
                    self.assertRaisesRegex(WorkspaceError, "not valid"):
                self.ws.start_recording(3, consent=bad)

    def test_the_bridge_drives_the_recording_indicator(self):
        shown = []

        class Window:                                # stands in for the pywebview window
            pass
        window = Window()
        from unittest import mock

        from workspace import shell
        api.make_api(self.ws, lambda: window, confirm=lambda question: True)
        with mock.patch.object(shell, "show_recording",
                               side_effect=lambda w, on: shown.append((w, on))):
            self.ws._recording = object()             # stands in for an open recording
            self.ws._indicate()
            self.ws._indicate()                       # already shown: not repeated
            self.ws._recording = None
            self.ws._indicate()
        self.assertEqual(shown, [(window, True), (window, False)])


def js_function(source, name):
    """The body of a top-level JavaScript function in source, by its braces
    (the functions read here have none unbalanced in strings). The body
    starts after the parameters (which may hold a destructuring default)."""
    start = source.index(") {", source.index(f"function {name}(")) + 2
    depth = 0
    for i in range(start, len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            return source[start + 1:i]
    raise ValueError(f"function {name} has no end")


class PageSearchTests(unittest.TestCase):
    """The transcript list redrawn with a search open (a chunk committed while
    a long meeting is open, tools/workspace_ui_check.py "growing transcript").
    Since every passage is sealed (#111, schema 2), search opens each one and
    takes longer than drawing the rows. The page used to draw the new rows and
    only then start the search, so the rows appeared with the old list's
    count ("No matches") and highlights until it returned; the real-window
    check read the count then. The matches are now found before the list is
    redrawn and shown with it, in the same task. With searches slower, two can
    overlap, so only the latest request's answer is shown (a sequence number),
    and the latest decides whether to jump to the first match. Read from the
    page's source: the tests do not run JavaScript."""

    @classmethod
    def setUpClass(cls):
        page = (Path(__file__).resolve().parents[1] / "workspace" / "ui" / "app.js")
        cls.page = page.read_text(encoding="utf-8")

    def function(self, name):
        """A function's body in the page; a failure (not an error) if missing."""
        self.assertTrue(f"function {name}(" in self.page, f"the page has no {name}()")
        return js_function(self.page, name)

    def assertInOrder(self, text, *parts):
        """Each part in text, after the one before it."""
        at = 0
        for part in parts:
            self.assertIn(part, text[at:], f"{part!r} missing, or out of order")
            at += text[at:].index(part) + len(part)

    def test_a_redraw_shows_its_matches_with_the_rows(self):
        body = self.function("renderTranscript")
        # The stop check comes before the search (nothing is searched for a
        # redraw that will not draw), the search before the rows are drawn,
        # and the matches are shown in the same task as the rows.
        self.assertInOrder(body, "if (stopped())", "const seq = query ? newSearch() : 0;",
                           'call("search", mid, query, version)', "if (stopped())",
                           "list.replaceChildren(",
                           "if (query && seq === searchSeq) showHits(query, hits, takeJump());",
                           "else markHits();")
        after = body[body.index("list.replaceChildren("):]
        self.assertNotIn("await", after, "something is awaited between the rows and their matches")
        self.assertNotIn("await", self.function("showHits"))
        self.assertNotIn("await", self.function("markHits"))
        # The redraw never starts a second search alongside a typed one.
        self.assertNotIn("runSearch(", body)

    def test_only_the_latest_search_request_is_shown(self):
        body = self.function("runSearch")
        self.assertInOrder(body, "const seq = newSearch(jump);", 'call("search", mid, q, version)',
                           "if (seq !== searchSeq) return;", "showHits(q, hits, takeJump());")
        # Not by comparing the query: an older request for the same text that
        # answers last would pass that.
        self.assertNotIn("!== q", body)
        self.assertIn("return ++searchSeq;", self.function("newSearch"))
        self.assertIn("return ++searchSeq;", self.function("dropSearches"))
        # Typing and opening another meeting or version drop older answers.
        self.assertRegex(self.page, r'\$\("m-search"\)\.addEventListener\("input", \(\) => \{\s*'
                                    r"dropSearches\(\);")
        self.assertIn("dropSearches();", self.function("openMeeting"))
        self.assertIn("dropSearches();", self.function("viewVersion"))

    def test_the_latest_request_decides_the_jump(self):
        # A typed search asks for the jump; whichever request answers last
        # (the typed one, or a redraw's for the same text) takes it.
        self.assertIn("if (jump) searchJump = true;", self.function("newSearch"))
        self.assertIn("searchJump = false;", self.function("takeJump"))
        self.assertIn("searchJump = false;", self.function("dropSearches"))
        # Without a jump, the current match stays current while it matches.
        self.assertInOrder(self.function("showHits"),
                           "const current = state.hit >= 0 ? state.hits[state.hit] : undefined;",
                           "state.hit = jump ? -1 : hits.indexOf(current);",
                           "if (jump) nextHit(1);")

if __name__ == "__main__":
    unittest.main()
