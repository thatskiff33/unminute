# Cleanup hardening (#193, #194, #198), from the #192 security review.
# Synthetic data only.
#   python -m unittest discover -s tests -v

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

if HAVE_DEPS:
    from test_app import fake

    from workspace import cleanup, holds, media
    from workspace.app import Workspace, WorkspaceError
    from workspace.store import StoreError

GOOD = "0123456789abcdef0123456789abcdef"


def sqlite_error(kind, code, message):
    """An sqlite3 error with its result code, as SQLite raises them."""
    err = kind(message)
    err.sqlite_errorcode = code
    return err


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class MeetingIdTests(unittest.TestCase):
    """#194: only the app's own ids become media paths."""

    def test_only_32_lowercase_hex_characters_make_a_path(self):
        root = Path("media-root")
        self.assertEqual(media.meeting_folder(root, GOOD), root / GOOD)
        self.assertEqual(media.audio_path(root, GOOD), root / GOOD / media.AUDIO_FILE)
        for bad in ("..", "../" + GOOD[3:], GOOD.upper(), GOOD[:31], GOOD + "0",
                    GOOD[:30] + "/x", GOOD[:30] + chr(92) + "x", GOOD[:31] + "g",
                    GOOD[:31] + chr(10), GOOD + chr(10), None, 32):
            with self.subTest(bad=bad):
                with self.assertRaises(media.BadMeetingId):
                    media.meeting_folder(root, bad)
                with self.assertRaises(media.BadMeetingId):
                    media.audio_path(root, bad)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class CleanupHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = None

    def tearDown(self):
        if self.ws:
            self.ws.close()
        self.tmp.cleanup()

    def open(self):
        ws = Workspace(self.root / "ws", self.root / "models", start=False,
                       allow_test_engines=True, battery=False, protector=keyprotect.TestProtector())
        ws.settings.update(model=fake("sentences"), speakers=False)
        self.ws = ws
        return ws

    def reopen(self):
        self.ws.close()
        self.ws = None
        return self.open()

    def crafted_meeting(self, ws, mid):
        """A meeting row whose id is not the app's own (a damaged or edited database)."""
        real = ws.store.create_meeting("synthetic crafted")
        with ws.store.tx() as db:
            # Its keys move with it (the foreign keys hold); its title, sealed
            # for the old id, then reads as damaged (#111 C2b-2).
            db.execute("INSERT INTO meetings (id, title_sealed, created_at, updated_at) "
                       "SELECT ?, title_sealed, created_at, updated_at FROM meetings "
                       "WHERE id = ?", (mid, real))
            db.execute("UPDATE content_keys SET meeting_id = ? WHERE meeting_id = ?", (mid, real))
            db.execute("DELETE FROM meetings WHERE id = ?", (real,))

    # -- #194 ------------------------------------------------------------------------

    def test_a_crafted_id_is_never_joined_to_a_path(self):
        ws = self.open()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("synthetic", encoding="utf-8")
        crafted = "../../outside" + "/." * 9 + "/"  # 32 characters: resolves to it
        self.assertEqual(len(crafted), 32)
        self.assertEqual((ws.media / crafted).resolve(), outside.resolve())
        self.crafted_meeting(ws, crafted)
        with self.assertRaisesRegex(WorkspaceError, "not found"):
            ws.discard_meeting(crafted)            # the page cannot reach it
        with ws.store.tx() as db:                  # a job for it, as damage might leave
            jid = cleanup._add_job(db, crafted, cleanup.AUDIO)
        self.assertEqual(cleanup.run_job(ws.store, jid, ws.media), cleanup.JobResult.FAILED)
        job = ws.store._one("SELECT state, error FROM cleanup_jobs WHERE id = ?", (jid,))
        self.assertEqual(job["state"], "failed")
        self.assertIn("Meeting not found", job["error"])
        self.assertTrue((outside / "keep.txt").exists())
        ws = self.reopen()                         # start-up upkeep skips it too
        self.assertTrue((outside / "keep.txt").exists())

    def test_a_crafted_id_in_the_transcript_phase_is_refused_too(self):
        ws = self.open()
        crafted = "../../outside" + "/." * 9 + "/"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("synthetic", encoding="utf-8")
        self.crafted_meeting(ws, crafted)
        with ws.store.tx() as db:
            jid = cleanup._add_job(db, crafted, cleanup.TRANSCRIPT)
        self.assertEqual(cleanup.run_job(ws.store, jid, ws.media), cleanup.JobResult.FAILED)
        self.assertTrue((outside / "keep.txt").exists())

    def test_a_damaged_row_never_breaks_the_meetings_list(self):
        # F1: one row with an id that is not the app's own is listed, not fatal.
        ws = self.open()
        ws.store.create_meeting("synthetic real")
        self.crafted_meeting(ws, "Z" * 32)
        rows = ws.meetings()
        self.assertEqual(len(rows), 2)
        [bad] = [r for r in rows if r["id"] == "Z" * 32]
        self.assertFalse(bad["audio_available"])

    # -- #193 ------------------------------------------------------------------------

    def test_a_released_hold_still_needs_discard(self):
        # F2 (C2): releasing a hold deletes nothing, even at a later start.
        ws = self.open()
        mid = ws.store.create_meeting("synthetic unfinished, held then released")
        (ws.media / mid).mkdir(parents=True)
        (ws.media / mid / "partial.bin").write_bytes(b"synthetic")
        holds.add_hold(ws.store, mid, "all", "synthetic reason")
        hid = ws.store._one("SELECT id FROM holds WHERE meeting_id = ?", (mid,))["id"]
        ws.release_hold(hid)
        ws = self.reopen()                         # a passing start
        self.assertIsNotNone(ws.store.meeting(mid))
        self.assertTrue((ws.media / mid / "partial.bin").exists())
        [row] = [m for m in ws.meetings() if m["id"] == mid]
        self.assertTrue(row["unfinished"])

    def test_a_folder_that_cannot_be_removed_keeps_the_meeting(self):
        # #202 N1: no orphaned folder with decoded audio and no record of it.
        ws = self.open()
        mid = ws.store.create_meeting("synthetic unfinished")
        (ws.media / mid).mkdir(parents=True)
        (ws.media / mid / "audio.uma").write_bytes(b"synthetic")
        with mock.patch("shutil.rmtree", side_effect=PermissionError("locked")):
            self.assertFalse(ws._discard_import(mid))
        self.assertIsNotNone(ws.store.meeting(mid))      # rolled back: listed, retried
        self.assertTrue((ws.media / mid / "audio.uma").exists())
        self.assertTrue(ws._discard_import(mid))         # unlocked later
        self.assertIsNone(ws.store.meeting(mid))
        self.assertFalse((ws.media / mid).exists())

    def test_the_hold_check_inside_the_removal_is_effective(self):
        # F5: the check made under the lock, in the removal's own transaction.
        ws = self.open()
        mid = ws.store.create_meeting("synthetic unfinished, held")
        (ws.media / mid).mkdir(parents=True)
        holds.add_hold(ws.store, mid, "all", "synthetic reason")
        self.assertFalse(ws._discard_import(mid))
        self.assertIsNotNone(ws.store.meeting(mid))
        self.assertTrue((ws.media / mid).exists())
        other = ws.store.create_meeting("synthetic unfinished")
        (ws.media / other).mkdir(parents=True)
        self.assertTrue(ws._discard_import(other))  # F11: the real outcome
        self.assertFalse((ws.media / other).exists())

    def test_a_held_unfinished_import_survives_start_up_and_is_listed(self):
        ws = self.open()
        mid = ws.store.create_meeting("synthetic unfinished, held")
        (ws.media / mid).mkdir(parents=True)
        (ws.media / mid / "partial.bin").write_bytes(b"synthetic")
        holds.add_hold(ws.store, mid, "all", "synthetic reason")
        ws = self.reopen()                         # a passing start: upkeep runs
        self.assertIsNotNone(ws.store.meeting(mid))
        self.assertTrue((ws.media / mid / "partial.bin").exists())
        [row] = [m for m in ws.meetings() if m["id"] == mid]
        self.assertTrue(row["unfinished"])         # "Import did not finish"
        with self.assertRaisesRegex(WorkspaceError, "hold"):
            ws.discard_meeting(mid)
        hid = ws.store._one("SELECT id FROM holds WHERE meeting_id = ?", (mid,))["id"]
        ws.release_hold(hid)
        ws.discard_meeting(mid)                    # removable once released
        ws.start()
        self.assertTrue(ws.wait_idle(60))
        self.assertIsNone(ws.store.meeting(mid))
        self.assertFalse((ws.media / mid).exists())

    def test_an_unheld_unfinished_import_is_still_removed_at_start_up(self):
        ws = self.open()
        mid = ws.store.create_meeting("synthetic unfinished")
        (ws.media / mid).mkdir(parents=True)
        ws = self.reopen()
        self.assertIsNone(ws.store.meeting(mid))
        self.assertFalse((ws.media / mid).exists())

    def test_an_import_in_progress_is_not_listed_as_unfinished(self):
        ws = self.open()
        mid = ws.store.create_meeting("synthetic, importing")
        ws._imports.put(("__recording__", str(self.root / "elsewhere")))
        [row] = [m for m in ws.meetings() if m["id"] == mid]
        self.assertFalse(row["unfinished"])
        ws._imports.get()
        ws._imports.task_done()

    # -- #198 ------------------------------------------------------------------------

    def discarded_meeting(self, ws):
        mid = ws.store.create_meeting("synthetic unfinished")
        (ws.media / mid).mkdir(parents=True)
        ws.discard_meeting(mid)
        return mid

    def failing_compact(self, error):
        def compact(store):
            raise StoreError("Could not compact the database: synthetic") from error
        return mock.patch.object(cleanup, "compact", compact)

    def test_a_damaged_database_defers_compaction_and_finishes_the_job(self):
        ws = self.open()
        mid = self.discarded_meeting(ws)
        damage = sqlite_error(sqlite3.DatabaseError, 11, "database disk image is malformed")
        with self.failing_compact(damage):
            cleanup.resume_pending(ws.store, ws.media)
        self.assertIsNone(ws.store.meeting(mid))
        self.assertEqual(sorted(r["result"] for r in ws.receipts()),
                         ["audio removed", "transcript removed; " + cleanup.DEFERRED_NOTE])
        self.assertEqual(ws.store._one("SELECT COUNT(*) FROM cleanup_jobs")[0], 0)
        marker = ws.store.path.with_name("compact.pending")
        self.assertTrue(marker.exists())
        with mock.patch.object(cleanup, "compact", wraps=cleanup.compact) as compact:
            ws = self.reopen()                     # a start whose check passes
        self.assertEqual(compact.call_count, 1)    # the owed compaction ran
        self.assertFalse(ws.store.path.with_name("compact.pending").exists())

    def test_a_busy_database_still_retries_the_job(self):
        ws = self.open()
        self.discarded_meeting(ws)
        with self.failing_compact(sqlite_error(sqlite3.OperationalError, 5, "database is locked")):
            cleanup.resume_pending(ws.store, ws.media)
        job = ws.store._one("SELECT state FROM cleanup_jobs WHERE phase = 'transcript'")
        self.assertEqual(job["state"], "failed")   # retried, not deferred
        self.assertFalse(ws.store.path.with_name("compact.pending").exists())

    def test_an_error_that_is_not_damage_is_never_deferred(self):
        # F6: a closed connection (or any non-damage error) keeps retrying.
        ws = self.open()
        self.discarded_meeting(ws)
        with self.failing_compact(sqlite_error(sqlite3.ProgrammingError, 21, "closed")):
            cleanup.resume_pending(ws.store, ws.media)
        job = ws.store._one("SELECT state FROM cleanup_jobs WHERE phase = 'transcript'")
        self.assertEqual(job["state"], "failed")
        self.assertFalse(ws.store.path.with_name("compact.pending").exists())

    def test_deferral_needs_the_log_emptied(self):
        # F7: no "removed" receipt while the write-ahead log may hold the rows.
        ws = self.open()
        self.discarded_meeting(ws)
        with self.failing_compact(sqlite_error(sqlite3.DatabaseError, 11, "malformed")), \
                mock.patch.object(cleanup, "_wal_truncated", lambda store: False):
            cleanup.resume_pending(ws.store, ws.media)
        job = ws.store._one("SELECT state FROM cleanup_jobs WHERE phase = 'transcript'")
        self.assertEqual(job["state"], "failed")

    def test_a_planted_link_is_never_written_through(self):
        # F4: an existing marker (here a hard link to another file) is left alone.
        ws = self.open()
        target = self.root / "other.txt"
        target.write_text("synthetic content", encoding="utf-8")
        os.link(target, ws.store.path.with_name("compact.pending"))
        self.discarded_meeting(ws)
        with self.failing_compact(sqlite_error(sqlite3.DatabaseError, 11, "malformed")):
            cleanup.resume_pending(ws.store, ws.media)
        self.assertEqual(target.read_text(encoding="utf-8"), "synthetic content")

    def test_a_marker_that_cannot_be_removed_never_stops_the_app(self):
        # F3: compacted anyway; the marker is kept and tried again next start.
        ws = self.open()
        marker = ws.store.path.with_name("compact.pending")
        marker.write_text("", encoding="ascii")
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            self.assertFalse(cleanup.compact_deferred(ws.store))
            ws = self.reopen()                     # the start-up upkeep runs it too
        self.assertTrue(marker.exists())

    def test_an_owed_compaction_that_fails_again_is_kept(self):
        ws = self.open()
        marker = ws.store.path.with_name("compact.pending")
        marker.write_text("", encoding="ascii")
        with self.failing_compact(sqlite_error(sqlite3.DatabaseError, 11, "malformed")):
            self.assertFalse(cleanup.compact_deferred(ws.store))
        self.assertTrue(marker.exists())
        self.assertTrue(cleanup.compact_deferred(ws.store))
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
