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
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workspace import diag  # noqa: E402
from workspace.app import DEFAULT_SETTINGS, SETTINGS_VERSION, Settings, Workspace  # noqa: E402
from workspace.store import Store  # noqa: E402

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
        for text in ("{not json", "[1, 2]", "{}" + " " * 70000, "[" * 50000):  # under the cap
            self.path.write_text(text, encoding="utf-8")
            s = Settings(self.path)
            self.assertTrue(s.unreadable, text[:10])
            self.assertEqual(s.values, DEFAULT_SETTINGS)
        s.update(mode="faster")
        self.assertFalse(s.unreadable)
        self.assertFalse(Settings(self.path).unreadable)
        self.assertFalse(Settings(Path(self.tmp.name) / "none.json").unreadable)   # first run
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mode": "faster"}).encode())
        self.assertEqual(Settings(self.path)["mode"], "faster")   # saved by Windows PowerShell

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

    def unclean(self, meetings=0):
        """A workspace whose app last ended without closing."""
        ws = self.open()
        for i in range(meetings):
            ws.store.create_meeting(f"Synthetic meeting {i}")
        ws.log.open_run()                       # never closed: the marker stays
        ws.close()
        self.ws = None

    def test_no_check_after_a_clean_close(self):
        ws = self.open()
        ws.log.open_run()
        ws.log.close_run()
        ws.close()
        with mock.patch.object(Store, "quick_check", side_effect=AssertionError("checked")):
            ws = self.open(check_integrity=True)
            ws.start()
        self.assertTrue(ws._integrity_ok.is_set())
        self.assertIsNone(ws.status()["integrity"])
        self.assertIsNone(diag.integrity_state(ws.log.folder))

    def test_tests_and_tools_never_check(self):
        self.unclean()
        ws = self.open()                        # check_integrity defaults to False
        self.assertTrue(ws._integrity_ok.is_set())

    def test_a_sound_database_after_an_unclean_close_lets_work_start(self):
        self.unclean(meetings=3)
        ws = self.open(check_integrity=True)
        self.assertFalse(ws._integrity_ok.is_set())        # held until the check is done
        ws._check_integrity()
        self.assertTrue(ws._integrity_ok.is_set())
        self.assertIsNone(ws.status()["integrity"])
        self.assertEqual(diag.integrity_state(ws.log.folder)["result"], "ok")
        checks = [r for r in diag.shareable_records(ws.log.folder) if r["kind"] == "integrity"]
        self.assertEqual([r["result"] for r in checks], ["ok"])

    def test_damage_is_found_shown_with_a_code_and_kept_for_the_next_start(self):
        self.unclean(meetings=200)
        damage(self.root / "workspace.db")
        ws = self.open(check_integrity=True)
        ws._check_integrity()
        notice = ws.status()["integrity"]
        self.assertEqual(notice["state"], "failed")
        self.assertRegex(notice["code"], r"^U-[0-9A-F]{6}$")
        self.assertFalse(ws._integrity_ok.is_set())
        state = diag.integrity_state(ws.log.folder)
        self.assertEqual((state["result"], state["code"]), ("failed", notice["code"]))
        self.assertIn("Database check: failed", ws.diagnostics())
        self.assertIn(notice["code"], ws.diagnostics())
        ws.continue_despite_damage()
        self.assertTrue(ws._integrity_ok.is_set())
        self.assertIsNone(ws.status()["integrity"])
        ws.log.close_run()                       # this time the app closes cleanly
        ws.close()
        ws = self.open(check_integrity=True)     # still checked: the damage stays
        self.assertFalse(ws.log.running_path.exists())
        self.assertFalse(ws._integrity_ok.is_set())
        ws._check_integrity()
        self.assertEqual(ws.status()["integrity"]["state"], "failed")

    def test_continue_does_nothing_without_damage(self):
        self.unclean()
        ws = self.open(check_integrity=True)
        ws.continue_despite_damage()            # still checking: no effect
        self.assertFalse(ws._integrity_ok.is_set())
        self.assertEqual(ws._integrity["state"], "checking")

    def test_the_check_reports_damage_it_cannot_even_read(self):
        store = Store(Path(self.tmp.name) / "fine.db")
        try:
            self.assertTrue(store.quick_check())
            with mock.patch.object(store, "db") as db:
                db.execute.side_effect = sqlite3.DatabaseError("database disk image is malformed")
                self.assertFalse(store.quick_check())
                db.execute.side_effect = None
                db.execute.return_value.fetchall.return_value = [("*** in database main ***",)]
                self.assertFalse(store.quick_check())
        finally:
            store.close()

    def test_status_and_health_report_the_last_check(self):
        from workspace import __main__ as main
        ws = self.open()
        self.assertEqual(main.status(self.root)["integrity"], "unchecked")
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

    def test_the_bridge_offers_continue(self):
        from workspace import api
        self.assertIn("continue_despite_damage", api.API_METHODS)
        self.unclean()
        ws = self.open(check_integrity=True)
        with mock.patch.object(ws.store, "quick_check", return_value=False):
            ws._check_integrity()
        page = api.make_api(ws)
        self.assertTrue(page.continue_despite_damage()["ok"])
        self.assertTrue(ws._integrity_ok.is_set())


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class HoldTests(unittest.TestCase):
    """Damage holds transcription and imports until Continue anyway; cleanup is not held."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.rec = write_wav(self.base / "synthetic-standup.wav", 20)
        self.ws = None

    def tearDown(self):
        if self.ws:
            self.ws.close()
        self.tmp.cleanup()

    def held_workspace(self):
        first = Workspace(self.base / "ws", self.base / "models", start=False,
                          allow_test_engines=True, battery=False,
                          policy_reader=lambda: ({}, ()))
        first.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
        first._import_one(self.rec)
        first.log.open_run()                    # ends without closing
        first.close()
        ws = self.ws = Workspace(self.base / "ws", self.base / "models", start=False,
                                 allow_test_engines=True, battery=False,
                                 policy_reader=lambda: ({}, ()), check_integrity=True)
        with mock.patch.object(ws.store, "quick_check", return_value=False):
            ws._check_integrity()
        return ws

    def test_cleanup_the_user_confirmed_still_runs(self):
        kw = dict(allow_test_engines=True, battery=False, policy_reader=lambda: ({}, ()))
        ws = Workspace(self.base / "ws", self.base / "models", **kw)
        ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
        ws._import_one(self.rec)
        self.assertTrue(ws.wait_idle(120))
        mid = ws.meetings()[0]["id"]
        ws.close()
        ws = Workspace(self.base / "ws", self.base / "models", start=False, **kw)
        ws.confirm_reviewed(mid)                # lined up; nothing runs it yet
        ws.log.open_run()                       # then the app ends without closing
        ws.close()
        ws = self.ws = Workspace(self.base / "ws", self.base / "models", start=False,
                                 check_integrity=True, **kw)
        with mock.patch.object(ws.store, "quick_check", return_value=False):
            ws._check_integrity()
        self.assertTrue(ws.detail(mid)["audio_available"])
        ws.start()
        self.assertTrue(ws.wait_idle(60))
        self.assertFalse(ws.detail(mid)["audio_available"])   # removed, as the user asked
        self.assertEqual(ws.status()["integrity"]["state"], "failed")   # still on hold

    def test_transcription_and_imports_wait_until_continue(self):
        ws = self.held_workspace()
        ws.start()
        ws.import_paths([str(self.rec)])
        time.sleep(1.5)
        self.assertEqual([m["progress"] for m in ws.meetings()], [0])   # nothing new either
        self.assertIn("On hold", ws.status()["message"])
        ws.continue_despite_damage()
        self.assertTrue(ws.wait_idle(120))
        self.assertEqual([m["progress"] for m in ws.meetings()], [100, 100])

    def test_closing_while_held_does_not_wait_for_the_hold(self):
        ws = self.held_workspace()
        ws.start()
        ws.import_paths([str(self.rec)])        # waits on the hold
        time.sleep(0.5)
        started = time.monotonic()
        ws.close()
        self.ws = None
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
