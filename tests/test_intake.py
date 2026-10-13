# Watched-folder intake with reconciliation and duplicate detection (#23).
# Synthetic files only; the hasher is real (SHA-256), imports are recorded.
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
EXT = (".wav", ".mp3", ".m4a")
BIG = 1 << 30

if HAVE_DEPS:
    import numpy as np

    from workspace import intake
    from workspace.intake import IntakeError
    from workspace.store import Store


def wav(path, seconds=1.0, seed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((rng.standard_normal(int(16000 * seconds)) * 300).astype("<i2").tobytes())
    return path


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "Recordings"
        self.folder.mkdir()
        self.db = self.root / "ws" / "workspace.db"
        self.store = Store(self.db)
        self.queued = []
        self.now = time.time()
        self.saved_recent = intake.STABLE_RECENT_S
        intake.STABLE_RECENT_S = 0          # simulated clock; recency has its own test
        self.import_ok = True

    def tearDown(self):
        intake.STABLE_RECENT_S = self.saved_recent
        self.store.close()
        self.tmp.cleanup()

    def enqueue(self, path, fid, key, content):
        """Stands in for the app: import, then report back."""
        self.queued.append(Path(path))
        intake.import_finished(self.store, fid, key, content, self.import_ok)

    def scan(self, advance=10, **kw):
        self.now += advance
        return intake.scan(self.store, EXT, BIG, self.enqueue, now=self.now, **kw)

    def settle(self):
        """Two scans: first sees a new file, the second (later) imports it."""
        self.scan()
        return self.scan()

    def test_refuses_broad_or_missing_folders(self):
        for bad in (Path(self.root.anchor), Path(os.environ.get("USERPROFILE", self.root.anchor)),
                    self.root / "missing"):
            with self.assertRaises(IntakeError):
                intake.add_folder(self.store, str(bad))
        intake.add_folder(self.store, str(self.folder))
        with self.assertRaises(IntakeError):
            intake.add_folder(self.store, str(self.folder))       # already watched

    def test_existing_files_only_when_asked(self):
        wav(self.folder / "before.wav")
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        self.assertEqual(self.queued, [])                         # baseline, not imported
        new = wav(self.folder / "after.wav", seed=1)
        self.settle()
        self.assertEqual([p.name for p in self.queued], [new.name])

    def test_include_existing_imports_what_is_there(self):
        wav(self.folder / "before.wav")
        intake.add_folder(self.store, str(self.folder), include_existing=True)
        self.settle()
        self.assertEqual([p.name for p in self.queued], ["before.wav"])

    def test_old_file_copied_in_later_is_new(self):
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        old = wav(self.root / "archive" / "2019-board.wav", seed=2)
        os.utime(old, (time.time() - 5 * 365 * 86400,) * 2)       # five years old
        target = self.folder / old.name
        os.replace(old, target)
        self.settle()
        self.assertEqual([p.name for p in self.queued], [target.name])

    def test_still_copying_file_waits_until_stable(self):
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        f = wav(self.folder / "growing.wav", seconds=1)
        self.scan()
        wav(f, seconds=2)                                         # still being written
        self.scan()
        self.assertEqual(self.queued, [])
        self.scan()
        self.assertEqual(len(self.queued), 1)

    def test_duplicates_are_skipped_but_replacements_imported(self):
        intake.add_folder(self.store, str(self.folder))
        other = self.root / "Other"
        other.mkdir()
        intake.add_folder(self.store, str(other))
        self.settle()
        wav(self.folder / "a.wav", seed=3)
        self.settle()
        self.assertEqual(len(self.queued), 1)
        wav(other / "copy-of-a.wav", seed=3)                      # same content elsewhere
        stats = self.settle()
        self.assertEqual((len(self.queued), stats["duplicates"]), (1, 1))
        wav(self.folder / "a.wav", seconds=2, seed=4)             # same path, new content
        self.settle()
        self.assertEqual(len(self.queued), 2)

    def test_closed_app_catches_up_on_launch(self):
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        self.store.close()
        wav(self.folder / "while-closed.wav", seed=5)             # app not running
        self.store = Store(self.db)
        self.settle()
        self.assertEqual([p.name for p in self.queued], ["while-closed.wav"])

    def test_depth_and_type_limits(self):
        intake.add_folder(self.store, str(self.folder), include_existing=True)
        wav(self.folder / "a" / "b" / "ok.wav", seed=6)
        wav(self.folder / "a" / "b" / "c" / "too-deep.wav", seed=7)
        (self.folder / "notes.txt").write_text("not audio")
        self.settle()
        self.assertEqual([p.name for p in self.queued], ["ok.wav"])

    def test_suppression_data_has_no_names_and_is_bounded(self):
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        wav(self.folder / "Client X board meeting.wav", seed=8)
        self.settle()
        self.store.close()
        raw = b"".join(f.read_bytes() for f in self.db.parent.glob("workspace.db*"))
        self.assertNotIn(b"Client X", raw)
        self.store = Store(self.db)
        self.assertEqual(len(self.store._all("SELECT * FROM intake_content")), 1)
        self.assertIsNone(self.store._one("SELECT content FROM intake_seen "
                                          "WHERE content IS NOT NULL"))
        intake.purge(self.store, now=time.time() + (intake.SUPPRESS_DAYS + 1) * 86400)
        self.assertEqual(self.store._all("SELECT * FROM intake_content"), [])

    def test_failed_import_is_not_suppressed(self):
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        self.import_ok = False
        wav(self.folder / "damaged.wav", seed=30)
        self.settle()
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.store._all("SELECT * FROM intake_content"), [])
        self.settle()
        self.assertEqual(len(self.queued), 1)                # unchanged: not retried endlessly
        self.import_ok = True
        wav(self.folder / "damaged.wav", seconds=2, seed=31) # fixed copy saved over it
        self.settle()
        self.assertEqual(len(self.queued), 2)

    def test_imports_queued_at_close_are_found_again(self):
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        wav(self.folder / "queued.wav", seed=32)
        self.scan()
        self.now += 10
        intake.scan(self.store, EXT, BIG, lambda *a: None, now=self.now)  # queued, app closes
        intake.recover(self.store)                                     # next launch
        self.settle()
        self.assertEqual([p.name for p in self.queued], ["queued.wav"])

    def test_partial_read_never_records_a_baseline(self):
        wav(self.folder / "history.wav", seed=33)
        intake.add_folder(self.store, str(self.folder))
        real = intake._walk
        intake._walk = lambda root, ext, stop=None: ([], False)       # e.g. sync re-initialising
        try:
            self.scan()
        finally:
            intake._walk = real
        self.assertFalse(intake.folders(self.store)[0]["baseline_done"])
        self.settle()
        self.assertEqual(self.queued, [])                           # history not imported

    def test_partial_read_keeps_rows_for_unseen_files(self):
        wav(self.folder / "history.wav", seed=34)
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        real = intake._walk
        intake._walk = lambda root, ext, stop=None: ([], False)
        try:
            self.scan()
        finally:
            intake._walk = real
        self.settle()
        self.assertEqual(self.queued, [])

    @unittest.skipUnless(os.name == "nt", "NTFS file IDs")
    def test_rename_or_move_of_an_old_file_is_not_new(self):
        old = wav(self.folder / "old.wav", seed=35)
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        renamed = old.rename(self.folder / "old renamed.wav")
        self.settle()
        (self.folder / "sub").mkdir()
        renamed.rename(self.folder / "sub" / renamed.name)          # moved into a subfolder
        self.settle()
        self.assertEqual(self.queued, [])

    def test_unreadable_file_is_retried_when_readable(self):
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        wav(self.folder / "locked.wav", seed=36)
        self.scan()

        def denied(path):
            raise PermissionError(13, "Access is denied")
        self.scan(hasher=denied)
        self.assertEqual(self.queued, [])
        self.scan()                                                  # permission fixed
        self.assertEqual(len(self.queued), 1)

    def test_recently_written_file_waits(self):
        intake.STABLE_RECENT_S = 3600
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        f = wav(self.folder / "fresh.wav", seed=37)
        self.settle()
        self.assertEqual(self.queued, [])                           # written moments ago
        os.utime(f, (time.time() - 7200,) * 2)
        self.settle()
        self.assertEqual(len(self.queued), 1)

    def test_unavailable_folder_is_reported_and_rechecked(self):
        fid = intake.add_folder(self.store, str(self.folder))
        self.folder.rename(self.root / "moved")
        stats = self.scan()
        self.assertEqual(stats["errors"], 1)
        [f] = intake.summary(self.store)
        self.assertIn("not available", f["error"])
        (self.root / "moved").rename(self.folder)
        self.scan()
        self.assertIsNone(intake.summary(self.store)[0]["error"])
        intake.set_active(self.store, fid, False)
        wav(self.folder / "paused.wav", seed=9)
        self.settle()
        self.assertEqual(self.queued, [])


@unittest.skipUnless(HAVE_DEPS and os.name == "nt", "Windows paths")
class WorkspaceIntakeTests(unittest.TestCase):
    def test_new_recording_in_a_watched_folder_becomes_a_meeting(self):
        from workspace import app
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Recordings"
            folder.mkdir()
            saved = (intake.SCAN_INTERVAL_S, intake.STABLE_S, intake.STABLE_RECENT_S)
            intake.SCAN_INTERVAL_S, intake.STABLE_S, intake.STABLE_RECENT_S = 1, 0, 0
            ws = app.Workspace(root / "ws", root / "models", start=False, battery=False)
            try:
                ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
                ws.add_watched_folder(str(folder), False)
                ws.start()
                time.sleep(2.5)                                  # baseline scan done
                wav(folder / "Synthetic standup.wav", seconds=8, seed=11)
                end = time.time() + 60
                while time.time() < end and not ws.meetings():
                    time.sleep(0.5)
                self.assertTrue(ws.wait_idle(60))
                [m] = ws.meetings()
                self.assertEqual(m["title"], "Synthetic standup")
                self.assertEqual(ws.watched_folders()["folders"][0]["imported"], 1)
            finally:
                intake.SCAN_INTERVAL_S, intake.STABLE_S, intake.STABLE_RECENT_S = saved
                ws.close()


if __name__ == "__main__":
    unittest.main()
