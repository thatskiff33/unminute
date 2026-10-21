# Watched-folder intake with reconciliation and duplicate detection (#23).
# Synthetic files only; the hasher is real (SHA-256), imports are recorded.
#   python -m unittest discover -s tests -v

import contextlib
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
EXT = (".wav", ".mp3", ".m4a")
EXTENDED = "\\\\?\\"                 # a Windows extended-length path prefix
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
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "Recordings"
        self.folder.mkdir()
        self.db = self.root / "ws" / "workspace.db"
        self.store = Store(self.db, protector=keyprotect.TestProtector())
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

    def test_refuses_folders_that_hold_the_profile_or_app_data(self):
        # #111 D2 (red-team part 2, finding 6): the folder holding every
        # profile, and AppData with the app's own data folder, could be watched.
        profile = self.root / "Users" / "someone"
        appdata = profile / "AppData"
        local = appdata / "Local"
        for d in (local / "Unminute" / "workspace", local / "Temp" / "Recordings",
                  appdata / "Roaming", appdata / "LocalLow", profile / "Documents" / "Recordings"):
            d.mkdir(parents=True)
        env = {"USERPROFILE": str(profile), "APPDATA": str(appdata / "Roaming"),
               "LOCALAPPDATA": str(local)}
        with mock.patch.dict(os.environ, env):
            for bad in (self.root / "Users", profile, appdata, local, appdata / "Roaming",
                        appdata / "LocalLow", local / "Unminute", local / "Unminute" / "workspace"):
                with self.assertRaises(IntakeError, msg=str(bad)):
                    intake.add_folder(self.store, str(bad))
            for good in (local / "Temp" / "Recordings", profile / "Documents" / "Recordings"):
                intake.add_folder(self.store, str(good))
        # A local app data folder redirected elsewhere: what holds it is refused too.
        data = self.root / "Data"
        (data / "Local" / "Unminute").mkdir(parents=True)
        (data / "Shared").mkdir()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(data / "Local")}):
            for bad in (data, data / "Local", data / "Local" / "Unminute"):
                with self.assertRaises(IntakeError, msg=str(bad)):
                    intake.add_folder(self.store, str(bad))
            intake.add_folder(self.store, str(data / "Shared"))
        self.assertEqual(len(intake.folders(self.store)), 3)

    def test_only_a_duplicate_is_called_already_watched(self):
        # Any other failure (here a full disk) is not "already watched" (#111 C2a).
        from workspace.store import StoreError
        other = self.root / "Other"
        other.mkdir()
        with mock.patch.object(self.store, "tx", side_effect=StoreError("disk is full")):
            with self.assertRaises(StoreError) as caught:
                intake.add_folder(self.store, str(other))
        self.assertNotIsInstance(caught.exception, IntakeError)
        self.assertEqual([], intake.folders(self.store))

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
        self.store = Store(self.db, protector=keyprotect.TestProtector())
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
        self.store = Store(self.db, protector=keyprotect.TestProtector())
        self.assertEqual(len(self.store._all("SELECT * FROM intake_content")), 1)
        self.assertIsNone(self.store._one("SELECT content_fp FROM intake_seen "
                                          "WHERE content_fp IS NOT NULL"))
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
        intake._walk = lambda root, ext, stop=None: ([], False, [])       # e.g. sync re-initialising
        try:
            self.scan()
        finally:
            intake._walk = real
        self.assertFalse(intake.folders(self.store)[0]["baseline_done"])
        self.settle()
        self.assertEqual(self.queued, [])                           # history not imported

    def long_paths(self, limit):
        """Act as Windows without long paths turned on: a plain path of
        `limit` characters or more cannot be listed or read, while its
        extended form can. The same whatever this computer's setting is."""
        real_scandir, real_stat = os.scandir, os.stat

        def plain(path):
            text = os.fspath(path)
            if not isinstance(text, str):
                return path
            if text.startswith(EXTENDED):
                return text[len(EXTENDED):]
            if len(text) >= limit:
                raise FileNotFoundError(3, "The system cannot find the path specified")
            return text

        def scandir(path="."):
            return real_scandir(plain(path))

        def stat(path, *args, **kw):
            return real_stat(path if isinstance(path, int) else plain(path), *args, **kw)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(os, "scandir", scandir))
        stack.enter_context(mock.patch.object(os, "stat", stat))
        stack.enter_context(mock.patch.object(intake, "_LONG_PATHS", True, create=True))
        return stack

    def test_a_path_too_long_for_windows_does_not_stop_the_baseline(self):
        # #111 D2 (red-team part 2, finding 5): one folder past MAX_PATH made
        # the walk incomplete, so the baseline was never recorded and nothing
        # was ever imported from the folder.
        wav(self.folder / "history.wav", seed=40)
        deep = self.folder / ("d" * 30) / ("e" * 30)
        old = wav(deep / "deep history.wav", seed=41)
        intake.add_folder(self.store, str(self.folder))
        limit = len(str(deep))
        with self.long_paths(limit):
            self.settle()
        [f] = intake.folders(self.store)
        self.assertTrue(f["baseline_done"])
        self.assertIsNone(f["last_error"])
        self.assertEqual(self.queued, [])                         # a baseline imports nothing
        wav(self.folder / "new.wav", seed=42)
        wav(deep / "new deep.wav", seed=43)
        with self.long_paths(limit):
            self.settle()
        self.assertEqual([p.name for p in self.queued], ["new.wav"])
        self.assertEqual(intake.folders(self.store)[0]["last_error"], intake._too_long_note(1))
        # Moved to a shorter path: the old recording is still not new, and the
        # new one is imported.
        old.rename(self.folder / old.name)
        (deep / "new deep.wav").rename(self.folder / "new deep.wav")
        with self.long_paths(limit):
            self.settle()
        self.assertEqual(sorted(p.name for p in self.queued), ["new deep.wav", "new.wav"])
        self.assertIsNone(intake.folders(self.store)[0]["last_error"])

    def test_a_real_long_path_does_not_stop_the_baseline(self):
        # The same on this computer's file system, whatever its long path
        # setting: with long paths off, the deep file is listed by the
        # extended form and counted; with them on, it is an ordinary file.
        top = self.folder / ("e" * 120)
        deep = top / ("f" * 120)
        os.makedirs(EXTENDED + str(deep))
        try:
            with open(EXTENDED + str(deep / "deep history.wav"), "wb") as f:
                f.write(b"RIFF")
            wav(self.folder / "history.wav", seed=44)
            intake.add_folder(self.store, str(self.folder))
            self.settle()
            self.assertTrue(intake.folders(self.store)[0]["baseline_done"])
            wav(self.folder / "new.wav", seed=45)
            self.settle()
            self.assertEqual([p.name for p in self.queued], ["new.wav"])
        finally:
            shutil.rmtree(EXTENDED + str(top))

    def count_transactions(self):
        """Patch the store so each transaction is counted (R7: each one is a
        disk flush with synchronous=FULL)."""
        real, count = self.store.tx, []

        def counting():
            count.append(1)
            return real()
        return mock.patch.object(self.store, "tx", new=counting), count

    def test_unchanged_files_share_one_transaction_per_folder(self):
        intake.add_folder(self.store, str(self.folder))
        for i in range(200):
            (self.folder / f"old {i}.wav").write_bytes(b"synthetic")
        patch, count = self.count_transactions()
        with patch:
            self.scan()                                             # baseline of 200
        self.assertLessEqual(len(count), 3)             # + the salt, made by the first scan
        count.clear()
        with patch:
            self.scan()                                             # nothing new
        self.assertLessEqual(len(count), 2)             # the folder, the purge (was 202)
        self.assertEqual(self.queued, [])

    def test_closing_part_way_through_a_baseline_finishes_it_next_time(self):
        intake.add_folder(self.store, str(self.folder))
        for i in range(20):
            (self.folder / f"old {i}.wav").write_bytes(b"synthetic")
        calls = []

        def stop():                     # three checks before the files, then one each
            calls.append(1)
            return len(calls) > 13
        self.scan(stop=stop)
        self.assertFalse(intake.folders(self.store)[0]["baseline_done"])
        self.assertEqual(self.states(), {"baseline": 10})          # what was seen is kept
        self.settle()
        self.assertTrue(intake.folders(self.store)[0]["baseline_done"])
        self.assertEqual(self.queued, [])           # files not reached were not "new"

    def states(self):
        return {r["state"]: r["n"] for r in self.store._all(
            "SELECT state, COUNT(*) AS n FROM intake_seen GROUP BY state")}

    def test_same_content_twice_in_one_scan_is_imported_once(self):
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        wav(self.folder / "a.wav", seed=38)
        wav(self.folder / "b.wav", seed=38)
        self.scan()
        queued = []                     # the app imports later: nothing reported back
        self.now += 10
        stats = intake.scan(self.store, EXT, BIG, lambda *a: queued.append(a), now=self.now)
        self.assertEqual((len(queued), stats["duplicates"]), (1, 0))
        self.assertEqual(stats["waiting"], 1)          # the copy waits for that import
        (_, fid, key, content), = queued
        intake.import_finished(self.store, fid, key, content, True)
        self.now += 10
        stats = intake.scan(self.store, EXT, BIG, lambda *a: queued.append(a), now=self.now)
        self.assertEqual((len(queued), stats["duplicates"]), (1, 1))     # done: a duplicate

    # -- the same recording in two folders (#152) --------------------------------

    def two_folders_lined_up(self):
        """The same recording in two watched folders; the first folder's copy
        is lined up for import (not yet imported). Returns the folder ids and
        what was lined up."""
        other = self.root / "OneDrive copy"
        other.mkdir()
        first = intake.add_folder(self.store, str(self.folder))
        second = intake.add_folder(self.store, str(other))
        self.settle()
        wav(self.folder / "meeting.wav", seed=70)
        wav(other / "meeting.wav", seed=70)
        self.scan()
        lined_up = []
        self.now += 10
        intake.scan(self.store, EXT, BIG, lambda *a: lined_up.append(a), now=self.now)
        self.assertEqual(len(lined_up), 1)
        return first, second, lined_up[0]

    def test_a_copy_in_another_folder_is_imported_when_the_first_is_paused(self):
        first, second, (path, fid, key, content) = self.two_folders_lined_up()
        intake.set_active(self.store, fid, False)
        intake.import_held(self.store, fid, key)             # its import comes up: held
        self.settle()
        self.assertEqual(len(self.queued), 1)                # from the folder still on
        self.assertNotEqual(self.queued[0].parent, Path(path).parent)
        intake.set_active(self.store, fid, True)
        self.settle()
        self.assertEqual(len(self.queued), 1)                # imported once overall

    def test_a_copy_in_another_folder_is_imported_when_the_first_is_removed(self):
        first, second, (path, fid, key, content) = self.two_folders_lined_up()
        intake.remove_folder(self.store, fid)
        intake.import_held(self.store, fid, key)             # nothing left to hold
        self.settle()
        self.assertEqual(len(self.queued), 1)

    def test_a_copy_in_another_folder_is_imported_when_the_first_fails(self):
        first, second, (path, fid, key, content) = self.two_folders_lined_up()
        intake.import_finished(self.store, fid, key, content, False)    # damaged, say
        self.settle()
        self.assertEqual(len(self.queued), 1)

    def test_a_folder_paused_and_resumed_at_once_is_not_marked_scanned(self):
        # Paused just before a file is read, resumed before the scan ends: the
        # folder still counts as not scanned (its rows were not all written).
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        wav(self.folder / "new.wav", seed=71)
        self.scan()
        before = intake.folders(self.store)[0]["last_scan_at"]
        with mock.patch.object(intake, "watching", return_value=False):   # paused...
            self.scan()                                                  # ...and back on
        self.assertEqual(intake.folders(self.store)[0]["last_scan_at"], before)
        self.assertEqual(self.queued, [])

    def test_turned_off_during_the_walk_is_not_an_unreadable_folder(self):
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        calls = []

        def stop():                     # the second check is inside the walk
            calls.append(1)
            return len(calls) >= 2
        self.scan(stop=stop)
        self.assertIsNone(intake.summary(self.store)[0]["error"])

    def test_an_import_that_finishes_during_the_scan_is_recorded(self):
        # Its row is written before the import is queued, so the import's
        # report (here at once, inside enqueue) is not overwritten afterwards.
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        wav(self.folder / "new.wav", seed=39)
        self.settle()
        row = self.store._one("SELECT state, content_fp FROM intake_seen")
        self.assertEqual((row["state"], row["content_fp"]), ("imported", None))

    def test_a_file_gone_since_the_walk_does_not_stop_the_scan(self):
        from workspace.media import sha256_file
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        a, b = wav(self.folder / "a.wav", seed=40), wav(self.folder / "b.wav", seed=41)
        self.scan()

        def hasher(path):               # the other file is deleted while one is hashed
            other = b if Path(path).name == "a.wav" else a
            other.unlink(missing_ok=True)
            return sha256_file(path)
        self.scan(hasher=hasher)
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(intake.folders(self.store)[0]["last_scan_at"], self.now)  # finished

    def test_a_folder_removed_during_a_scan_leaves_nothing_behind(self):
        from workspace.media import sha256_file
        fid = intake.add_folder(self.store, str(self.folder))
        self.settle()
        for i in range(3):
            wav(self.folder / f"new {i}.wav", seed=50 + i)
        self.scan()

        def hasher(path):               # the user removes the folder meanwhile
            intake.remove_folder(self.store, fid)
            return sha256_file(path)
        self.scan(hasher=hasher)
        self.assertEqual(self.queued, [])                           # nothing imported
        self.assertEqual(self.states(), {})                         # no rows left behind

    # -- pausing a folder (#152): nothing more from it until it is turned on ------

    def test_a_folder_paused_during_a_scan_imports_nothing_more_until_resumed(self):
        from workspace.media import sha256_file
        fid = intake.add_folder(self.store, str(self.folder))
        self.settle()                                               # a finished baseline
        for i in range(3):
            wav(self.folder / f"new {i}.wav", seed=60 + i)
        self.scan()

        def hasher(path):               # the user pauses the folder meanwhile
            intake.set_active(self.store, fid, False)
            return sha256_file(path)
        self.scan(hasher=hasher)
        self.assertEqual(self.queued, [])
        wav(self.folder / "while paused.wav", seed=63)              # arrives while paused
        self.settle()
        self.assertEqual(self.queued, [])                           # a paused folder is skipped
        intake.set_active(self.store, fid, True)
        self.settle()
        self.assertEqual(sorted(p.name for p in self.queued),
                         ["new 0.wav", "new 1.wav", "new 2.wav", "while paused.wav"])

    def test_a_paused_folder_hashes_nothing_more(self):
        # Duplicates are never lined up for import, so the pause must be
        # noticed before the next file is read, not only at the import.
        from workspace.media import sha256_file
        fid = intake.add_folder(self.store, str(self.folder))
        self.settle()
        for i in range(3):
            wav(self.folder / f"copy {i}.wav", seed=65)            # the same recording
        intake.remember_content(self.store, sha256_file(self.folder / "copy 0.wav"))
        self.scan()
        hashed = []

        def hasher(path):
            hashed.append(path)
            intake.set_active(self.store, fid, False)
            return sha256_file(path)
        self.scan(hasher=hasher)
        self.assertEqual(len(hashed), 1)

    def test_pausing_an_unfinished_baseline_does_it_again_when_resumed(self):
        fid = intake.add_folder(self.store, str(self.folder))       # without existing files
        for i in range(20):
            (self.folder / f"old {i}.wav").write_bytes(b"synthetic")
        calls = []

        def stop():                     # paused part-way through the first check
            calls.append(1)
            if len(calls) == 12:
                intake.set_active(self.store, fid, False)
            return False
        self.scan(stop=stop)
        self.assertFalse(intake.folders(self.store)[0]["baseline_done"])
        intake.set_active(self.store, fid, True)
        self.settle()
        self.assertTrue(intake.folders(self.store)[0]["baseline_done"])
        self.assertEqual(self.queued, [])                           # existing files were not new
        self.assertEqual(self.states(), {"baseline": 20})

    def test_an_import_held_back_waits_for_the_next_scan(self):
        fid = intake.add_folder(self.store, str(self.folder))
        self.settle()
        wav(self.folder / "lined up.wav", seed=64)
        self.scan()
        queued = []
        self.now += 10
        intake.scan(self.store, EXT, BIG, lambda *a: queued.append(a), now=self.now)
        [(_, _, key, _)] = queued                                   # lined up, not yet imported
        intake.import_held(self.store, fid, key)                    # the folder was paused
        row = self.store._one("SELECT state, content_fp FROM intake_seen WHERE key = ?", (key,))
        self.assertEqual((row["state"], row["content_fp"]), ("waiting", None))
        self.scan()
        self.assertEqual([p.name for p in self.queued], ["lined up.wav"])

    def test_partial_read_keeps_rows_for_unseen_files(self):
        wav(self.folder / "history.wav", seed=34)
        intake.add_folder(self.store, str(self.folder))
        self.settle()
        real = intake._walk
        intake._walk = lambda root, ext, stop=None: ([], False, [])
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
            ws = app.Workspace(root / "ws", root / "models", start=False,
                               allow_test_engines=True, battery=False,
                               protector=keyprotect.TestProtector())
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
