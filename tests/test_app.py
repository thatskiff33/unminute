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
        try:
            r = ws.copy_handoff(mid, "minutes", "combined")
            ws.copy_handoff(mid, "summary", "prompt_only")
        finally:
            clipboard.copy_text = real
        preview = ws.handoff_preview(mid, "minutes")
        self.assertEqual(copied[0], preview["combined"])      # exactly what was previewed
        self.assertEqual(r["chars"], len(preview["combined"]))
        self.assertNotIn("sentence", copied[1])
        out = ws.save_handoff_to(mid, "minutes", str(self.root / "handoff"))
        saved = (self.root / "handoff.md").read_text(encoding="utf-8")
        self.assertEqual((out["name"], saved), ("handoff.md", preview["markdown"]))
        with self.assertRaises(WorkspaceError):
            ws.copy_handoff(mid, "minutes", "everything")
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
