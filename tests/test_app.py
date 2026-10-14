# The workspace controller and its page API (#20), end to end with a real
# worker process, fake engines and synthetic audio (no models, no UI).
#   python -m unittest discover -s tests -v

import importlib.util
import os
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
SR = 16000

if HAVE_DEPS:
    import numpy as np

    from workspace import api
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
        ws = Workspace(self.root / "ws", self.root / "models", start=False,
                       speaker_model=fake("fake_diarizer"), battery=False)
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
        old = ws.store._one("SELECT text FROM edits WHERE segment_id = ?", (seg["id"],))
        self.assertEqual(old["text"], "Human correction")    # machine history untouched

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
        ws = Workspace(self.root / "ws", self.root / "models", start=False, battery=True)
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

    def test_models_can_be_removed_unless_still_needed(self):
        import asr_engines
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

        import asr_engines
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
        import asr_engines
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
        snap = ws.store._one("SELECT text FROM reviewed_snapshots WHERE meeting_id = ?", (mid,))
        self.assertIn("Aroha: ", snap["text"])
        with self.assertRaises(WorkspaceError):
            ws.rename_speaker(mid, a["label"], "Late change")
        ws.confirm_final_saved(mid)
        self.assertTrue(ws.wait_idle(60))
        self.assertEqual(ws.store._one("SELECT COUNT(*) AS n FROM speaker_corrections")["n"], 0)

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
        with ws.store.tx() as db:            # about 4 hours of short passages
            db.executemany("INSERT INTO segments (version_id, chunk_idx, start_s, end_s, text) "
                           "VALUES (?, 0, ?, ?, ?)",
                           [(vid, i * 3.0, i * 3.0 + 2.5, f"passage {i} with some words")
                            for i in range(5000)])
        t = time.perf_counter()
        page = ws.transcript(mid, 4000, 1000)
        hits = ws.search(mid, "passage 4999")
        elapsed = time.perf_counter() - t
        self.assertEqual((page["total"], len(page["items"])), (5000, 1000))
        self.assertEqual(hits, [ws.transcript(mid, 4999, 1)["items"][0]["id"]])
        self.assertLess(elapsed, 2.0)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False, battery=False)
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
        self.assertFalse(self.api.clip(self.mid, 0, 60)["ok"])
        self.assertFalse(self.api.clip(self.mid, "0", 2)["ok"])

    def test_import_dialog_without_window_is_refused(self):
        self.assertFalse(self.api.import_dialog()["ok"])

    def test_hold_ids_are_validated(self):
        for bad in ("../x", 5, None, "a" * 33):
            self.assertFalse(self.api.release_hold(bad)["ok"])
        self.assertTrue(self.api.add_hold(self.mid, "all", "reason")["ok"])
        self.assertEqual(len(new_id()), 32)


if __name__ == "__main__":
    unittest.main()
