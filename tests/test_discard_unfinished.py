# Discarding an import that never finished (#181): the safety properties,
# from the #192 security review's scenarios. Synthetic audio only.
#   python -m unittest discover -s tests -v

import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402
import profile_elsewhere  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

if HAVE_DEPS:
    from test_app import fake, write_wav

    from workspace import cleanup, intake, media
    from workspace.app import Workspace, WorkspaceError

MEETING_TABLES = ("sources", "versions", "cleanup_jobs", "holds", "reviewed_snapshots",
                  "handoff_options")


sys.path.append(str(Path(__file__).resolve().parents[1] / "tools"))
import decoder_runtime  # noqa: E402  (tools/decoder_runtime.py)


def setUpModule():
    # #299: the decoder runs in an AppContainer that cannot read this Python:
    # decodes here use a scratch runtime it can read and a throwaway profile.
    decoder_runtime.use()


def tearDownModule():
    decoder_runtime.stop()


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class DiscardUnfinishedTests(unittest.TestCase):
    def setUp(self):
        profile_elsewhere.start(self)       # folders in %TEMP%, inside AppData (#111 D2)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = None

    def tearDown(self):
        if self.ws:
            self.ws.close()
        self.tmp.cleanup()

    def open(self, start=False):
        ws = Workspace(self.root / "ws", self.root / "models", start=False,
                       allow_test_engines=True, speaker_model=fake("fake_diarizer"),
                       battery=False, protector=keyprotect.TestProtector())
        ws.settings.update(model=fake("sentences"), speakers=False)
        if start:
            ws.start()
        self.ws = ws
        return ws

    def make_unfinished(self, ws, mid):
        """Remove the version rows, as damage (or a crash before planning) would."""
        with ws.store.tx() as db:
            db.execute("DELETE FROM chunks WHERE version_id IN "
                       "(SELECT id FROM versions WHERE meeting_id = ?)", (mid,))
            db.execute("DELETE FROM versions WHERE meeting_id = ?", (mid,))

    def unfinished_from_watched(self, ws):
        """A real import from a folder with deletion authority, made to look unfinished."""
        folder = self.root / "watched"
        folder.mkdir()
        orig = write_wav(folder / "synthetic-orig.wav", 5)
        fid = intake.add_folder(ws.store, str(folder))
        intake.set_delete_originals(ws.store, fid, True)
        mid = ws._import_one(str(orig), fid)
        row = ws.store._one("SELECT delete_original FROM sources WHERE meeting_id = ?", (mid,))
        self.assertEqual(row[0], 1)                       # authority really granted
        self.make_unfinished(ws, mid)
        return mid, orig

    def leftovers(self, ws, mid):
        return {t: ws.store._one(f"SELECT COUNT(*) FROM {t} WHERE meeting_id = ?", (mid,))[0]
                for t in MEETING_TABLES}

    def test_the_original_stays_and_only_this_meeting_goes(self):
        # C3: authority is withdrawn; another meeting is untouched.
        ws = self.open()
        other = ws._import_one(str(write_wav(self.root / "synthetic-other.wav", 5)))
        mid, orig = self.unfinished_from_watched(ws)
        ws.discard_meeting(mid)
        self.assertEqual(ws.store._one("SELECT delete_original FROM sources WHERE "
                                       "meeting_id = ?", (mid,))[0], 0)
        ws.start()
        self.assertTrue(ws.wait_idle(120))
        self.assertTrue(orig.exists())                    # never deleted
        self.assertIsNone(ws.store.meeting(mid))
        self.assertFalse((ws.media / mid).exists())
        self.assertEqual(set(self.leftovers(ws, mid).values()), {0})
        self.assertIsNotNone(ws.store.meeting(other))
        self.assertTrue(media.audio_path(ws.media, other).is_file())
        self.assertEqual(sorted(r["result"] for r in ws.receipts()),
                         ["audio removed", "transcript removed"])

    def test_holds_still_protect_an_unfinished_meeting(self):
        # C2: a hold refuses Discard; one added afterwards holds the jobs.
        ws = self.open()
        held = ws.store.create_meeting("synthetic unfinished, held")
        ws.add_hold(held, "all", "synthetic reason")
        with self.assertRaisesRegex(WorkspaceError, "hold"):
            ws.discard_meeting(held)
        self.assertEqual(self.leftovers(ws, held)["cleanup_jobs"], 0)
        later = ws.store.create_meeting("synthetic unfinished, held later")
        (ws.media / later).mkdir(parents=True)
        (ws.media / later / "partial.bin").write_bytes(b"synthetic")
        ws.discard_meeting(later)
        ws.add_hold(later, "audio", "synthetic reason")
        ws.start()
        self.assertTrue(ws.wait_idle(60))
        self.assertIsNotNone(ws.store.meeting(later))
        self.assertTrue((ws.media / later / "partial.bin").exists())
        self.assertEqual(ws.receipts(), [])

    def test_an_import_in_progress_is_refused(self):
        # The in-progress import is counted: task_done comes only after it.
        ws = self.open(start=True)
        entered, gate = threading.Event(), threading.Event()
        real = media.decode_to_file

        def slow(src, dst, *a, **k):
            entered.set()
            gate.wait(60)
            return real(src, dst, *a, **k)
        with mock.patch("workspace.media.decode_to_file", slow):
            ws.import_paths([str(write_wav(self.root / "synthetic-in.wav", 5))])
            self.assertTrue(entered.wait(60))
            [m] = ws.store._all("SELECT id FROM meetings")
            try:
                with self.assertRaisesRegex(WorkspaceError, "still being imported"):
                    ws.discard_meeting(m["id"])
                self.assertEqual(self.leftovers(ws, m["id"])["cleanup_jobs"], 0)
            finally:
                gate.set()
            self.assertTrue(ws.wait_idle(120))
        self.assertIsNotNone(ws.store.active_version(m["id"]))
        self.assertIsNone(ws.store.meeting(m["id"])["reviewed_at"])

    def test_discard_during_an_authorised_import_never_deletes_the_original(self):
        # Why the in-progress import must count: an import from a folder with
        # deletion authority sets delete_original after a discard would clear it.
        ws = self.open()                                  # only the intake thread runs
        folder = self.root / "watched"
        folder.mkdir()
        orig = write_wav(folder / "synthetic-orig.wav", 5)
        fid = intake.add_folder(ws.store, str(folder))
        intake.set_delete_originals(ws.store, fid, True)
        t = threading.Thread(target=ws._intake_loop, daemon=True)
        t.start()
        entered, gate = threading.Event(), threading.Event()
        real = media.decode_to_file

        def slow(src, dst, *a, **k):
            entered.set()
            gate.wait(60)
            return real(src, dst, *a, **k)
        with mock.patch("workspace.media.decode_to_file", slow):
            ws._imports.put((str(orig), fid))             # as a folder scan queues it
            self.assertTrue(entered.wait(60))
            [m] = ws.store._all("SELECT id FROM meetings")
            mid = m["id"]
            try:
                with self.assertRaises(WorkspaceError):
                    ws.discard_meeting(mid)
            finally:
                gate.set()
            end = time.monotonic() + 60
            while time.monotonic() < end and ws.store.active_version(mid) is None:
                time.sleep(0.05)
        ws._imports.put(None)
        t.join(15)
        roots = intake.deletion_roots(ws.store, mid)
        cleanup.resume_pending(ws.store, ws.media, original_roots=roots)
        self.assertTrue(orig.exists())

    def test_a_crash_after_discard_is_finished_at_the_next_start(self):
        ws = self.open()
        mid, orig = self.unfinished_from_watched(ws)
        ws.discard_meeting(mid)
        ws.close()
        self.ws = None
        ws = self.open()                                  # start-up upkeep runs at open
        self.assertEqual(ws.store._one("SELECT COUNT(*) FROM versions WHERE meeting_id = ?",
                                       (mid,))[0], 0)     # nothing planned for it again
        ws.start()
        self.assertTrue(ws.wait_idle(120))
        self.assertTrue(orig.exists())
        self.assertIsNone(ws.store.meeting(mid))
        self.assertFalse((ws.media / mid).exists())
        self.assertEqual(set(self.leftovers(ws, mid).values()), {0})
        self.assertEqual(sorted(r["result"] for r in ws.receipts()),
                         ["audio removed", "transcript removed"])


if __name__ == "__main__":
    unittest.main()
