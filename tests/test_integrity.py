"""Settings format and the database check after an unclean close (R11, #132).

Synthetic data only: settings files written here, and databases filled with
made-up meeting titles, then damaged on purpose.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace import diag, store  # noqa: E402
from workspace.app import DEFAULT_SETTINGS, SETTINGS_VERSION, Settings, Workspace  # noqa: E402
from workspace.store import Store, StoreError, quick_check_file  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
SR = 16000


def damage(db_path, table="meetings"):
    """Overwrite a table's root page: the database still opens, and SQLite's
    quick check reports the damage."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page = con.execute("PRAGMA page_size").fetchone()[0]
        root = con.execute("SELECT rootpage FROM sqlite_master WHERE name = ?",
                           (table,)).fetchone()[0]
    finally:
        con.close()
    with open(db_path, "r+b") as f:
        f.seek((root - 1) * page)
        f.write(b"\x0d\x00\xff\xff" * 16)       # a leaf page header pointing nowhere


def write_wav(path, seconds):
    import numpy as np
    rng = np.random.default_rng(1)
    pcm = (rng.standard_normal(int(seconds * SR)) * 300).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


def until(condition, timeout=20.0):
    """Wait for a condition (never a fixed sleep); False on timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


class WatchedEvent(threading.Event):
    """The hold's Event, counting who asked: a thread blocked on wait() or a
    loop that checked is_set() proves it reached the hold."""

    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()
        self.checks = 0

    def wait(self, timeout=None):
        self.waiting.set()
        return super().wait(timeout)

    def is_set(self):
        self.checks += 1
        return super().is_set()


class SettingsFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_settings_a_newer_version_added_survive_a_save(self):
        self.write({"mode": "faster", "theme": "dark", "folders": {"a": [1, 2]}})
        s = Settings(self.path)
        self.assertEqual((s["mode"], s.repaired, s.unreadable), ("faster", [], False))
        s.update(speakers=False)
        saved = self.saved()
        self.assertEqual((saved["theme"], saved["folders"]), ("dark", {"a": [1, 2]}))
        self.assertEqual((saved["mode"], saved["speakers"]), ("faster", False))
        self.assertEqual(saved["settings_version"], SETTINGS_VERSION)
        self.assertEqual(Settings(self.path)["speakers"], False)

    def test_a_file_from_before_the_version_loads(self):
        self.write({"mode": "faster", "onboarded": True})     # no settings_version: before R11
        s = Settings(self.path)
        self.assertEqual((s["mode"], s["onboarded"], s.repaired), ("faster", True, []))

    def test_a_newer_versions_value_is_kept_and_reported(self):
        self.write({"settings_version": SETTINGS_VERSION + 1, "mode": "balanced"})
        s = Settings(self.path)
        self.assertEqual((s["mode"], s.repaired), (DEFAULT_SETTINGS["mode"], ["mode"]))
        s.update(speakers=False)
        saved = self.saved()
        self.assertEqual((saved["mode"], saved["settings_version"]),
                         ("balanced", SETTINGS_VERSION + 1))   # the newer version gets it back
        s.update(mode="faster")                                 # the user's choice replaces it
        self.assertEqual((self.saved()["mode"], s.repaired), ("faster", []))

    def test_a_bad_value_from_this_version_is_replaced(self):
        self.write({"settings_version": SETTINGS_VERSION, "mode": "bogus", "speakers": "yes"})
        s = Settings(self.path)
        self.assertEqual(sorted(s.repaired), ["mode", "speakers"])
        s.update(onboarded=True)
        saved = self.saved()
        self.assertEqual((saved["mode"], saved["speakers"]),
                         (DEFAULT_SETTINGS["mode"], DEFAULT_SETTINGS["speakers"]))

    def test_an_unreadable_file_means_all_defaults_and_says_so(self):
        for text in ("{not json", "[1, 2]", "{}" + " " * 70000, "[" * 50000):
            self.path.write_text(text, encoding="utf-8")
            s = Settings(self.path)
            self.assertTrue(s.unreadable, text[:10])
            self.assertEqual(s.values, DEFAULT_SETTINGS)
        # Nesting that loads but cannot be written back (Python 3.12's indented
        # encoder stops near 1,000 levels; 3.14's writes anything that loads).
        deep = '{"x": ' + "[" * 1200 + "]" * 1200 + "}"
        try:
            json.dumps(json.loads(deep), indent=2)
            writable = True
        except RecursionError:
            writable = False
        self.path.write_text(deep, encoding="utf-8")
        self.assertEqual(Settings(self.path).unreadable, not writable)
        self.assertFalse(Settings(Path(self.tmp.name) / "none.json").unreadable)   # first run
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mode": "faster"}).encode())
        self.assertEqual(Settings(self.path)["mode"], "faster")   # saved by Windows PowerShell

    def test_an_unreadable_file_is_kept_aside_before_the_first_save(self):
        self.path.write_text('{"mode": "faster", "theme": ', encoding="utf-8")   # cut short
        s = Settings(self.path)
        s.update(onboarded=True)
        self.assertFalse(s.unreadable)
        aside = self.path.with_name("settings.unreadable.json")
        self.assertEqual(aside.read_text(encoding="utf-8"), '{"mode": "faster", "theme": ')
        s.update(speakers=False)                       # only once
        self.assertEqual(aside.read_text(encoding="utf-8"), '{"mode": "faster", "theme": ')
        self.assertFalse(Settings(self.path).unreadable)

    def test_a_failed_save_still_leaves_the_unreadable_file_in_place(self):
        self.path.write_text('{"mode": "faster", "theme": ', encoding="utf-8")
        s = Settings(self.path)
        with mock.patch("os.fsync", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            s.update(onboarded=True)
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"mode": "faster", "theme": ')
        self.assertTrue(Settings(self.path).unreadable)          # still said at the next start

    def test_a_file_in_use_at_start_is_read_again_never_replaced(self):
        self.write({"mode": "faster", "microphone": True})
        real_open = open

        def in_use(path, *a, **k):
            if Path(path) == self.path and "r" in (a[0] if a else k.get("mode", "r")):
                raise PermissionError("in use by another program")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", in_use), mock.patch("time.sleep"):
            s = Settings(self.path)
            self.assertTrue(s.unreadable)                 # Settings says so meanwhile
            with self.assertRaisesRegex(Exception, "cannot open its settings file"):
                s.update(speakers=False)                  # still in use: nothing replaced
        self.assertEqual(self.saved(), {"mode": "faster", "microphone": True})
        s.update(speakers=False)                          # readable now: read, then saved
        saved = self.saved()
        self.assertEqual((saved["mode"], saved["microphone"], saved["speakers"]),
                         ("faster", True, False))
        self.assertEqual((s.unreadable, s["mode"]), (False, "faster"))
        self.assertFalse(self.path.with_name("settings.unreadable.json").exists())

    def test_a_moment_when_the_file_cannot_be_read_is_tried_again(self):
        self.write({"mode": "faster"})
        real_open, calls = open, []

        def flaky(path, *a, **k):
            if Path(path) == self.path and not calls:
                calls.append(1)
                raise PermissionError("held by a scan")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", flaky), mock.patch("time.sleep"):
            s = Settings(self.path)
        self.assertEqual((s["mode"], s.unreadable), ("faster", False))

    def test_kept_values_never_make_the_file_unreadable(self):
        nested = {"k": [[{"a": i, "b": [i, [i, [i]]]} for i in range(400)]]}
        self.path.write_text(json.dumps({"extra": nested}, separators=(",", ":")),
                             encoding="utf-8")
        self.assertLess(self.path.stat().st_size, 64 * 1024)
        s = Settings(self.path)
        s.update(mode="faster")
        self.assertLessEqual(self.path.stat().st_size, 64 * 1024)   # written compactly
        again = Settings(self.path)
        self.assertEqual((again.unreadable, again._kept["extra"]), (False, nested))

    def test_kept_values_that_cannot_fit_are_dropped_not_the_settings(self):
        self.path.write_text(json.dumps({"extra": "汉" * 20000}, ensure_ascii=False),
                             encoding="utf-8")                  # about 60 KB as UTF-8
        s = Settings(self.path)
        self.assertFalse(s.unreadable)
        s.update(mode="faster")                                 # escaped, it would be 120 KB
        self.assertLessEqual(self.path.stat().st_size, 64 * 1024)
        self.assertNotIn("extra", s._kept)                     # dropped in memory too
        again = Settings(self.path)
        self.assertEqual((again.unreadable, again["mode"], "extra" in again._kept),
                         (False, "faster", False))

    def test_the_new_file_reaches_the_disk_before_it_replaces_the_old(self):
        s = Settings(self.path)
        order = []
        real_fsync, real_replace = os.fsync, Path.replace
        with mock.patch("os.fsync", side_effect=lambda fd: (order.append("fsync"),
                                                             real_fsync(fd))), \
                mock.patch.object(Path, "replace", autospec=True,
                                  side_effect=lambda p, t: (order.append("replace"),
                                                            real_replace(p, t))[1]):
            s.update(mode="faster")
        self.assertEqual(order, ["fsync", "replace"])
        self.assertEqual(self.saved()["mode"], "faster")

    def test_the_app_uses_new_values_only_once_they_are_saved(self):
        s = Settings(self.path)
        with mock.patch("os.fsync", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            s.update(mode="faster")
        self.assertEqual(s["mode"], DEFAULT_SETTINGS["mode"])

    def test_saves_run_one_at_a_time(self):
        s = Settings(self.path)
        errors = []

        def save(i):
            try:
                s.update(mode="faster" if i % 2 else "quiet", speakers=bool(i % 3))
            except Exception as exc:              # noqa: BLE001 (any failure counts)
                errors.append(exc)
        threads = [threading.Thread(target=save, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.saved()["mode"], s["mode"])        # the file matches memory


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ws"
        self.ws = None

    def tearDown(self):
        if self.ws:
            self.ws.close()
        diag.set_current(None)
        self.tmp.cleanup()

    def open(self, **kw):
        self.ws = Workspace(self.root, Path(self.tmp.name) / "models", start=False,
                            allow_test_engines=True, battery=False,
                            policy_reader=lambda: ({}, ()), **kw)
        return self.ws

    def unclean(self, meetings=0, prepare=None):
        """A workspace whose app last ended without closing."""
        ws = self.open()
        for i in range(meetings):
            ws.store.create_meeting(f"Synthetic meeting {i}")
        if prepare:
            prepare(ws)
        ws.log.open_run()                       # never closed: the marker stays
        ws.close()
        self.ws = None

    def checks(self):
        return [r for r in diag.shareable_records(self.root / "logs")
                if r["kind"] == "integrity"]

    def test_no_check_after_a_clean_close(self):
        ws = self.open()
        ws.log.open_run()
        ws.log.close_run()
        ws.close()
        with mock.patch("workspace.app.quick_check_file", side_effect=AssertionError("checked")):
            ws = self.open(check_integrity=True)
        self.assertTrue(ws._integrity_ok.is_set())
        self.assertIsNone(ws.status()["integrity"])
        self.assertIsNone(diag.integrity_state(ws.log.folder))

    def test_tests_and_tools_never_check(self):
        self.unclean()
        with mock.patch("workspace.app.quick_check_file", side_effect=AssertionError("checked")):
            ws = self.open()                    # check_integrity defaults to False
        self.assertTrue(ws._integrity_ok.is_set())

    def test_a_sound_database_after_an_unclean_close_opens_as_usual(self):
        self.unclean(meetings=3)
        ws = self.open(check_integrity=True)
        self.assertTrue(ws._integrity_ok.is_set())
        self.assertIsNone(ws.status()["integrity"])
        self.assertEqual(diag.integrity_state(ws.log.folder)["result"], "ok")
        self.assertEqual([r["result"] for r in self.checks()], ["ok"])

    def test_damage_is_found_before_the_workspace_opens_and_kept(self):
        self.unclean(meetings=200)
        damage(self.root / "workspace.db")
        ws = self.open(check_integrity=True)
        notice = ws.status()["integrity"]
        self.assertEqual(notice["state"], "failed")
        self.assertRegex(notice["code"], r"^U-[0-9A-F]{6}$")
        self.assertFalse(ws._integrity_ok.is_set())
        self.assertIn("On hold", ws.status()["message"])
        state = diag.integrity_state(ws.log.folder)
        self.assertEqual((state["result"], state["code"]), ("failed", notice["code"]))
        self.assertIn("Database check: failed", ws.diagnostics())
        self.assertIn(notice["code"], ws.diagnostics())
        ws.continue_despite_damage()
        self.assertTrue(ws._integrity_ok.is_set())
        self.assertIsNone(ws.status()["integrity"])
        self.assertEqual([r["result"] for r in self.checks()], ["failed", "continued"])
        ws.log.close_run()                       # this time the app closes cleanly
        ws.close()
        ws = self.open(check_integrity=True)     # still checked: the damage stays
        self.assertFalse(ws.log.running_path.exists())
        self.assertEqual(ws.status()["integrity"]["state"], "failed")

    def test_nothing_that_can_delete_runs_before_the_hold_is_released(self):
        def interrupted_import(ws):
            mid = ws.store.create_meeting("Synthetic interrupted import")   # no version yet
            (ws.media / mid).mkdir(parents=True)
            (ws.media / mid / "note.txt").write_text("synthetic", encoding="utf-8")
            self.mid = mid
        self.unclean(meetings=200, prepare=interrupted_import)
        damage(self.root / "workspace.db", table="receipts")
        with mock.patch.object(Store, "mark_interrupted", autospec=True) as marked:
            ws = self.open(check_integrity=True)
            self.assertEqual(ws._integrity["state"], "failed")
            self.assertFalse(marked.called)          # opened without marking interrupted work
            self.assertTrue((ws.media / self.mid).exists())   # upkeep waited: nothing removed
            self.assertIsNotNone(ws.store.meeting(self.mid))
            with mock.patch.object(ws, "_reconcile_imports",
                                   wraps=ws._reconcile_imports) as reconciled:
                ws.continue_despite_damage()
            self.assertTrue(marked.called)
            reconciled.assert_called_once_with(False)            # the upkeep ran on Continue
        self.assertTrue((ws.media / self.mid).exists())        # but removed nothing that run
        self.assertIsNotNone(ws.store.meeting(self.mid))

    def test_after_continue_anyway_an_unfinished_import_can_be_discarded(self):
        # #181: the Continue anyway run keeps it (by design); Discard removes it.
        def interrupted_import(ws):
            mid = ws.store.create_meeting("Synthetic interrupted import")   # no version
            (ws.media / mid).mkdir(parents=True)
            (ws.media / mid / "note.txt").write_text("synthetic", encoding="utf-8")
            self.mid = mid
        self.unclean(meetings=5, prepare=interrupted_import)
        damage(self.root / "workspace.db", table="intake_content")   # cleanup never reads it
        ws = self.open(check_integrity=True)
        self.assertEqual(ws._integrity["state"], "failed")
        ws.continue_despite_damage()
        self.assertIsNotNone(ws.store.meeting(self.mid))
        ws.discard_meeting(self.mid)
        ws.start()
        self.assertTrue(ws.wait_idle(60))
        self.assertIsNone(ws.store.meeting(self.mid))
        self.assertFalse((ws.media / self.mid).exists())
        # The real end state on a damaged database: the audio job finishes; the
        # transcript job removed the rows and files but its final compaction
        # fails, so it stays failed and is retried (#198).
        self.assertEqual([r["result"] for r in ws.receipts()], ["audio removed"])
        job = ws.store._one("SELECT state, error FROM cleanup_jobs WHERE meeting_id = ? "
                            "AND phase = 'transcript'", (self.mid,))
        self.assertEqual(job["state"], "failed")
        self.assertIn("compact", job["error"])

    def test_an_unfinished_import_is_removed_only_when_discard_is_allowed(self):
        ws = self.open()
        mid = ws.store.create_meeting("Synthetic interrupted import")   # no source, no audio
        (ws.media / mid).mkdir(parents=True)
        ws._reconcile_imports(discard=False)
        self.assertTrue((ws.media / mid).exists())
        ws._reconcile_imports()
        self.assertFalse((ws.media / mid).exists())

    def test_no_migration_while_the_check_holds_work(self):
        db = Path(self.tmp.name) / "older.db"
        Store(db).close()
        con = sqlite3.connect(db)
        con.execute("DELETE FROM schema_version")
        con.execute("INSERT INTO schema_version VALUES (?)", (store.SCHEMA_VERSION - 1,))
        con.commit()
        con.close()
        with self.assertRaisesRegex(store.MigrationBlocked, "found damage"):
            Store(db, housekeeping=False)

    def test_a_database_error_never_discards_an_import(self):
        ws = self.open()
        ws.store.create_meeting("Synthetic interrupted import")      # no version yet
        with mock.patch.object(ws.store, "_one", return_value={"id": "s1"}), \
                mock.patch("workspace.media.audio_path",
                           return_value=mock.Mock(is_file=lambda: True)), \
                mock.patch.object(ws, "_plan_version", side_effect=StoreError("malformed")), \
                mock.patch.object(ws, "_discard_import") as discard:
            ws._reconcile_imports()
        self.assertFalse(discard.called)        # kept, to be tried again at the next start
        with mock.patch.object(ws.store, "_one", return_value={"id": "s1"}), \
                mock.patch("workspace.media.audio_path",
                           return_value=mock.Mock(is_file=lambda: True)), \
                mock.patch.object(ws, "_plan_version", side_effect=ValueError("bad audio")), \
                mock.patch.object(ws, "_discard_import") as discard:
            ws._reconcile_imports()
        self.assertTrue(discard.called)         # a broken import is still removed

    def test_continue_does_nothing_without_damage(self):
        ws = self.open()
        ws.continue_despite_damage()
        self.assertTrue(ws._integrity_ok.is_set())
        self.assertIsNone(ws._integrity["state"])
        self.assertEqual(self.checks(), [])

    def test_only_damage_codes_count_as_damage(self):
        good = Path(self.tmp.name) / "fine.db"
        Store(good).close()
        self.assertTrue(quick_check_file(good))
        junk = Path(self.tmp.name) / "junk.db"
        junk.write_bytes(b"\x00" * 4096 + b"not a database" * 100)
        self.assertFalse(quick_check_file(junk))

        def failing(code):
            exc = sqlite3.OperationalError("synthetic")
            exc.sqlite_errorcode = code
            return mock.patch("sqlite3.connect", side_effect=exc)
        with failing(5):                                    # SQLITE_BUSY: not damage
            self.assertIsNone(quick_check_file(good))
        with failing(7):                                    # SQLITE_NOMEM
            self.assertIsNone(quick_check_file(good))
        for code in (10, 11, 26, 10 | (3 << 8)):            # IOERR, CORRUPT, NOTADB, IOERR_WRITE
            with failing(code), mock.patch("workspace.store.time.sleep"):
                self.assertIs(quick_check_file(good), False, code)

    def test_an_io_error_is_tried_once_more_before_it_counts(self):
        good = Path(self.tmp.name) / "fine.db"
        Store(good).close()
        real = sqlite3.connect
        calls = []

        def locked_once(*a, **k):                 # another program held part of the file
            calls.append(1)
            if len(calls) == 1:
                exc = sqlite3.OperationalError("disk I/O error")
                exc.sqlite_errorcode = 10
                raise exc
            return real(*a, **k)
        with mock.patch("sqlite3.connect", side_effect=locked_once), \
                mock.patch("workspace.store.time.sleep") as waited:
            self.assertIs(quick_check_file(good), True)
        self.assertEqual(len(calls), 2)
        waited.assert_called_once_with(1.0)

    def test_a_check_that_cannot_run_holds_only_what_was_held(self):
        self.unclean()
        with mock.patch("workspace.app.quick_check_file", return_value=None):
            ws = self.open(check_integrity=True)
        self.assertTrue(ws._integrity_ok.is_set())
        self.assertEqual([r["result"] for r in self.checks()], ["skipped"])
        self.assertEqual(diag.integrity_state(ws.log.folder)["result"], "skipped")   # kept
        ws.log.close_run()                       # the next start closes cleanly...
        ws.close()
        with mock.patch("workspace.app.quick_check_file", return_value=True) as checked:
            ws = self.open(check_integrity=True)
        self.assertTrue(checked.called)          # ...and still checks: it was skipped
        self.assertEqual(diag.integrity_state(ws.log.folder)["result"], "ok")
        ws.close()
        diag.record_integrity(self.root / "logs", "failed", "U-ABCDEF")   # damage found before
        with mock.patch("workspace.app.quick_check_file", return_value=None):
            ws = self.open(check_integrity=True)
        self.assertEqual(ws.status()["integrity"], {"state": "failed", "code": "U-ABCDEF"})

    def test_status_and_health_report_the_last_check(self):
        from workspace import __main__ as main
        ws = self.open()
        self.assertEqual(main.status(self.root)["integrity"], "unchecked")
        (ws.recordings / "r1").mkdir(parents=True)
        self.assertEqual(main.status(self.root)["recordings"], 1)
        diag.record_integrity(ws.log.folder, "ok")
        self.assertEqual(main.status(self.root)["integrity"], "ok")
        diag.record_integrity(ws.log.folder, "failed", "U-ABCDEF")
        report = main.status(self.root)
        self.assertEqual((report["integrity"], report["readable"]), ("failed", True))
        with mock.patch.object(main, "_app_running", return_value=False):
            report = main.health(self.root)
        self.assertEqual((report["workspace"], report["healthy"]), ("damaged", False))

    def test_status_says_when_it_cannot_read_the_database(self):
        from workspace import __main__ as main
        ws = self.open()
        ws.close()
        self.ws = None
        db = self.root / "workspace.db"
        db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)   # damaged beyond opening
        for name in ("workspace.db-wal", "workspace.db-shm"):
            (self.root / name).unlink(missing_ok=True)
        report = main.status(self.root)
        self.assertFalse(report["readable"])
        with mock.patch.object(main, "_app_running", return_value=False):
            self.assertEqual(main.health(self.root)["workspace"], "unreadable")

    def test_a_workspace_too_damaged_to_open_says_so_with_a_code(self):
        from workspace import __main__ as main
        self.root.mkdir(parents=True)
        db = self.root / "workspace.db"
        db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
        with mock.patch("workspace.store.default_root", return_value=self.root):
            text = main._open_failed(sqlite3.DatabaseError("file is not a database"))
            self.assertIn("found damage", text)
            code = diag.integrity_state(self.root / "logs")["code"]
            self.assertIn(code, text)
            self.assertIn(code, main._open_failed(StoreError("again")))   # the same code
        self.assertEqual([r["result"] for r in self.checks()], ["failed"])   # recorded once
        db.unlink()
        Store(db).close()                                    # sound: the store's own message
        with mock.patch("workspace.store.default_root", return_value=self.root):
            self.assertEqual(main._open_failed(StoreError("Newer schema")), "Newer schema")
            with mock.patch("workspace.store.quick_check_file", return_value=None):
                text = main._open_failed(sqlite3.DatabaseError("x"))
        code = text.split("code ")[1].rstrip(").")
        self.assertRegex(text, r"could not open its workspace \(code U-[0-9A-F]{6}\)")
        opened = [r for r in diag.shareable_records(self.root / "logs")
                  if r["kind"] == "incident" and r.get("area") == "open"]
        self.assertEqual([(r["code"], r["error"]) for r in opened], [(code, "DatabaseError")])

    def test_the_app_checks_before_diagnostics_replace_the_run_marker(self):
        from workspace import __main__ as main
        from workspace import app as app_module
        order = []

        def built(*a, **kw):
            order.append(("workspace", kw.get("check_integrity")))
            return mock.Mock(log=mock.Mock())

        def installed(*a, **kw):
            order.append(("install", None))
            raise RuntimeError("stop here")
        with mock.patch.object(app_module, "Workspace", side_effect=built), \
                mock.patch.object(main, "_single_instance", return_value=True), \
                mock.patch.object(main, "model_cache", return_value=self.root / "models"), \
                mock.patch.object(diag, "install", side_effect=installed), \
                mock.patch.object(sys, "argv", ["workspace"]), \
                self.assertRaisesRegex(RuntimeError, "stop here"):
            main.main()
        self.assertEqual(order, [("workspace", True), ("install", None)])
        for error in (sqlite3.DatabaseError("file is not a database"), StoreError("refused")):
            with mock.patch.object(app_module, "Workspace", side_effect=error), \
                    mock.patch.object(main, "_single_instance", return_value=True), \
                    mock.patch.object(main, "model_cache", return_value=self.root / "models"), \
                    mock.patch.object(main, "_open_failed", return_value="explained") as why, \
                    mock.patch.object(main, "_message") as shown, \
                    mock.patch.object(sys, "argv", ["workspace"]):
                self.assertEqual(main.main(), 1)                 # never a silent exit
            why.assert_called_once_with(error)
            shown.assert_called_once_with("explained")

    def test_the_kept_result_is_only_what_the_app_writes(self):
        folder = Path(self.tmp.name) / "logs"
        folder.mkdir()
        (folder / diag.INTEGRITY_FILE).write_text(json.dumps(
            {"result": "failed", "code": "Zqmarker", "t": "Zqmarker"}), encoding="ascii")
        self.assertEqual(diag.integrity_state(folder),
                         {"result": "failed", "code": None, "t": None})
        for text in ("{", "[]", json.dumps({"result": "maybe"}), "[" * 100000):
            (folder / diag.INTEGRITY_FILE).write_text(text, encoding="ascii")
            self.assertIsNone(diag.integrity_state(folder))
        with mock.patch("os.fsync", side_effect=OSError("disk")):
            diag.record_integrity(folder, "ok")              # never raises
        diag.record_integrity(Path(self.tmp.name) / "gone" / "logs", "ok")   # nor here

    def test_repaired_settings_reach_the_page(self):
        from workspace import gpu
        self.root.mkdir(parents=True)
        (self.root / "settings.json").write_text(json.dumps({"mode": "bogus"}), encoding="utf-8")
        ws = self.open()
        with mock.patch.object(gpu, "detected", return_value=None):
            self.assertEqual(ws.get_settings()["repaired"], {"keys": ["mode"], "all": False})
            ws.set_settings({"mode": "faster"})
            self.assertEqual(ws.get_settings()["repaired"], {"keys": [], "all": False})

    def test_the_bridge_offers_continue_and_the_recovery_page(self):
        from workspace import api, setup
        self.assertIn("continue_despite_damage", api.API_METHODS)
        self.assertEqual(setup.HELP_LINKS["recovery"],
                         "https://github.com/thatskiff33/unminute/blob/main/docs/"
                         "WORKSPACE_GUIDE.md#if-unminute-finds-damage")
        self.unclean()
        with mock.patch("workspace.app.quick_check_file", return_value=False):
            ws = self.open(check_integrity=True)
        page = api.make_api(ws)
        with mock.patch("webbrowser.open") as browser:
            self.assertTrue(page.open_help("recovery")["ok"])
        browser.assert_called_once_with(setup.HELP_LINKS["recovery"])
        self.assertTrue(page.continue_despite_damage()["ok"])
        self.assertTrue(ws._integrity_ok.is_set())

    def test_scans_and_the_receipt_purge_wait_while_held(self):
        self.unclean()
        with mock.patch("workspace.app.quick_check_file", return_value=False):
            ws = self.open(check_integrity=True)
        ws._integrity_ok = watched = WatchedEvent()
        with mock.patch.object(ws, "_scan_folders") as scan, \
                mock.patch("workspace.holds.purge_expired_receipts") as purge:
            ws.start()
            ws.scan_now()
            self.assertTrue(until(lambda: watched.checks >= 2))   # both loops reached the hold
            self.assertFalse(scan.called)
            self.assertFalse(purge.called)
            ws.continue_despite_damage()
            self.assertTrue(until(lambda: scan.called and purge.called))

    def test_a_recording_recovered_after_a_crash_still_asks_while_held(self):
        def recording_cut_short(ws):
            folder = ws.recordings / "synthetic-recording"
            folder.mkdir(parents=True)
            (folder / "chunk-000001.pcm").write_bytes(b"\x00\x01" * SR)
            (folder / "journal.json").write_text(json.dumps(
                {"state": "recording", "rate": SR, "frames": SR, "started_at": time.time(),
                 "chunks": []}), encoding="utf-8")
        self.unclean(prepare=recording_cut_short)
        with mock.patch("workspace.app.quick_check_file", return_value=False):
            ws = self.open(check_integrity=True)
        ws.start()
        self.assertTrue(until(lambda: ws.recording_status()["reports"]))
        self.assertTrue(ws.recording_status()["reports"][-1]["recovered"])
        self.assertFalse(ws._integrity_ok.is_set())          # still held

    def test_a_recovered_recording_that_would_import_waits(self):
        def stopped_recording(ws):
            folder = ws.recordings / "synthetic-stopped"
            folder.mkdir(parents=True)
            (folder / "chunk-000001.pcm").write_bytes(b"\x00\x01" * SR)
            (folder / "journal.json").write_text(json.dumps(
                {"state": "stopped", "rate": SR, "frames": SR, "started_at": time.time(),
                 "chunks": [{"file": "chunk-000001.pcm"}]}), encoding="utf-8")
        self.unclean(prepare=stopped_recording)
        with mock.patch("workspace.app.quick_check_file", return_value=False):
            ws = self.open(check_integrity=True)
        ws._integrity_ok = watched = WatchedEvent()
        ws.start()
        self.assertTrue(watched.waiting.wait(20))            # held: importing writes
        self.assertEqual((ws.recording_status()["reports"], ws._import_errors), ([], []))
        self.assertTrue((ws.recordings / "synthetic-stopped").exists())


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class HoldTests(unittest.TestCase):
    """Damage holds transcription, imports and cleanup until Continue anyway."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.rec = write_wav(self.base / "synthetic-standup.wav", 20)
        self.kw = dict(allow_test_engines=True, battery=False, policy_reader=lambda: ({}, ()))
        self.ws = None

    def tearDown(self):
        if self.ws:
            self.ws.close()
        self.tmp.cleanup()

    def reopen_held(self):
        with mock.patch("workspace.app.quick_check_file", return_value=False):
            ws = self.ws = Workspace(self.base / "ws", self.base / "models", start=False,
                                     check_integrity=True, **self.kw)
        ws._integrity_ok = WatchedEvent()
        return ws

    def test_transcription_and_imports_wait_until_continue(self):
        first = Workspace(self.base / "ws", self.base / "models", start=False, **self.kw)
        first.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
        first._import_one(self.rec)
        first.log.open_run()                    # ends without closing
        first.close()
        ws = self.reopen_held()
        with mock.patch.object(ws, "_import_one", wraps=ws._import_one) as imported:
            ws.start()
            ws.import_paths([str(self.rec)])
            self.assertTrue(ws._integrity_ok.waiting.wait(20))   # the intake is at the hold
            self.assertFalse(imported.called)
            self.assertEqual([m["progress"] for m in ws.meetings()], [0])
            status = ws.status()
            self.assertEqual(status["importing"], 0)              # no fast polling while held
            self.assertIn("1 import waiting", status["message"])
            ws.continue_despite_damage()
            self.assertTrue(ws.wait_idle(120))
        self.assertEqual([m["progress"] for m in ws.meetings()], [100, 100])

    def test_cleanup_waits_too(self):
        ws = Workspace(self.base / "ws", self.base / "models", **self.kw)
        ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
        ws._import_one(self.rec)
        self.assertTrue(ws.wait_idle(120))
        mid = ws.meetings()[0]["id"]
        ws.close()
        ws = Workspace(self.base / "ws", self.base / "models", start=False, **self.kw)
        ws.confirm_reviewed(mid)                # lined up; nothing runs it yet
        ws.log.open_run()                       # then the app ends without closing
        ws.close()
        ws = self.reopen_held()
        ws.start()
        self.assertTrue(until(lambda: ws._integrity_ok.checks >= 1))   # processing looked
        self.assertTrue(ws.detail(mid)["audio_available"])    # held: holds read from damage
        ws.continue_despite_damage()
        self.assertTrue(ws.wait_idle(60))
        self.assertFalse(ws.detail(mid)["audio_available"])   # removed once released

    def test_closing_while_held_does_not_wait_for_the_hold(self):
        first = Workspace(self.base / "ws", self.base / "models", start=False, **self.kw)
        first.log.open_run()
        first.close()
        ws = self.reopen_held()
        ws.start()
        ws.import_paths([str(self.rec)])        # waits on the hold
        self.assertTrue(ws._integrity_ok.waiting.wait(20))
        started = time.monotonic()
        ws.close()
        self.ws = None
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
