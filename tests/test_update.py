# Installation-aware updates and the empty-workspace migration gate (#26).
#   python -m unittest discover -s tests -v

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None

if HAVE_NUMPY:
    from workspace import holds, store, update
    from workspace.store import MigrationBlocked, SchemaTooNew, Store


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "workspace.db"

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, meeting=False, hold=False, job=False, receipt=False):
        s = Store(self.db)
        mid = s.create_meeting("Synthetic") if (meeting or hold or job) else None
        if hold:
            holds.add_hold(s, mid, "all", "dispute")
        with s.tx() as db:
            if job:
                db.execute("INSERT INTO cleanup_jobs (id, meeting_id, phase, state, intent_at) "
                           "VALUES ('j', ?, 'audio', 'failed', 0)", (mid,))
            if receipt:
                db.execute("INSERT INTO receipts VALUES ('r', 0, 1, 'audio removed')")
        s.close()
        return mid

    def test_fresh_and_compatible_updates_go_ahead(self):
        self.assertEqual(update.gate(self.db, 1)["kind"], "fresh")
        self.make(meeting=True, hold=True)
        g = update.gate(self.db, store.SCHEMA_VERSION)       # same schema: keep everything
        self.assertEqual((g["code"], g["kind"]), (update.ALLOWED, "compatible"))

    def test_schema_change_waits_and_explains_without_touching_anything(self):
        self.make(meeting=True, hold=True, job=True)
        before = self.db.read_bytes()
        g = update.gate(self.db, store.SCHEMA_VERSION + 1)
        self.assertEqual(g["code"], update.WAIT)
        kinds = {b["kind"] for b in g["blockers"]}
        self.assertEqual(kinds, {"holds", "meetings", "cleanup"})
        self.assertIn("never expire", g["message"])
        self.assertEqual(self.db.read_bytes(), before)          # read-only check

    def test_receipts_and_configuration_alone_do_not_block(self):
        self.make(receipt=True)
        g = update.gate(self.db, store.SCHEMA_VERSION + 1)
        self.assertEqual((g["code"], g["blockers"]), (update.ALLOWED, []))

    def test_unreadable_workspace_makes_the_update_wait(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.db.write_bytes(b"not a database at all" * 100)
        g = update.gate(self.db, store.SCHEMA_VERSION + 1)
        self.assertEqual((g["code"], g["kind"]), (update.WAIT, "unreadable"))
        import sqlite3
        self.db.unlink()
        con = sqlite3.connect(self.db)                 # content but no schema_version
        con.execute("CREATE TABLE meetings (id TEXT)")
        con.execute("INSERT INTO meetings VALUES ('x')")
        con.commit()
        con.close()
        self.assertEqual(update.gate(self.db, 1)["code"], update.WAIT)

    def test_downgrade_is_refused(self):
        self.make()
        self.assertEqual(update.gate(self.db, store.SCHEMA_VERSION - 1)["code"], update.TOO_NEW)

    def test_released_hold_does_not_block_but_its_meeting_does(self):
        mid = self.make(meeting=True)
        s = Store(self.db)
        hid = holds.add_hold(s, mid, "audio", "x")
        holds.release_hold(s, hid)
        s.close()
        kinds = {b["kind"] for b in update.blockers(self.db)}
        self.assertEqual(kinds, {"meetings"})


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "workspace.db"
        self.saved = store.SCHEMA_VERSION

    def tearDown(self):
        store.SCHEMA_VERSION = self.saved
        self.tmp.cleanup()

    def test_empty_workspace_migrates_keeping_receipts(self):
        s = Store(self.db)
        with s.tx() as db:
            db.execute("INSERT INTO receipts VALUES ('r', 0, ?, 'transcript removed')",
                       (time.time(),))
        s.close()
        store.SCHEMA_VERSION = self.saved + 1
        s = Store(self.db)
        try:
            self.assertEqual(s._one("SELECT MAX(version) AS v FROM schema_version")["v"],
                             self.saved + 1)
            self.assertEqual(len(holds.receipts(s)), 1)
        finally:
            s.close()

    def test_watched_folder_history_survives_migration(self):
        s = Store(self.db)
        with s.tx() as db:
            db.execute("INSERT INTO watched_folders (id, path, created_at, baseline_done) "
                       "VALUES ('f', 'C:/Rec', 0, 1)")
            db.execute("INSERT INTO intake_seen (folder_id, key, size, mtime_ns, file_id, "
                       "state, first_seen, last_seen) VALUES ('f', 'k', 1, 1, 1, 'baseline', 0, 0)")
        s.close()
        store.SCHEMA_VERSION = self.saved + 1
        s = Store(self.db)
        try:
            self.assertEqual(s._one("SELECT state FROM intake_seen")["state"], "baseline")
            row = s._one("SELECT baseline_done FROM watched_folders")
            self.assertEqual(row["baseline_done"], 1)
        finally:
            s.close()

    def test_interrupted_migration_leaves_the_old_workspace_intact(self):
        s = Store(self.db)
        with s.tx() as db:
            db.execute("INSERT INTO receipts VALUES ('r', 0, 1, 'kept')")
        s.close()
        store.SCHEMA_VERSION = self.saved + 1
        real = store._statements

        def crash(script):
            raise KeyboardInterrupt("power cut")
        store._statements = crash
        try:
            with self.assertRaises(KeyboardInterrupt):
                Store(self.db)
        finally:
            store._statements = real
        store.SCHEMA_VERSION = self.saved
        s = Store(self.db)                                 # old version opens it unchanged
        try:
            self.assertEqual(len(holds.receipts(s)), 1)
        finally:
            s.close()

    def test_workspace_with_content_is_never_migrated(self):
        s = Store(self.db)
        mid = s.create_meeting("Keep me")
        holds.add_hold(s, mid, "all", "legal")
        s.close()
        store.SCHEMA_VERSION = self.saved + 1
        with self.assertRaises(MigrationBlocked):
            Store(self.db)
        store.SCHEMA_VERSION = self.saved
        s = Store(self.db)                                     # still intact, still held
        try:
            self.assertEqual(s.meeting(mid)["title"], "Keep me")
            self.assertEqual(len(holds.active_holds(s, mid)), 1)
        finally:
            s.close()

    def test_older_app_refuses_a_newer_workspace(self):
        store.SCHEMA_VERSION = self.saved + 1
        Store(self.db).close()
        store.SCHEMA_VERSION = self.saved
        with self.assertRaises(SchemaTooNew):
            Store(self.db)


class DeploymentTests(unittest.TestCase):
    def test_installed_copy_reports_its_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            here = root / "versions" / "0.2.0" / "app" / "workspace" / "update.py"
            here.parent.mkdir(parents=True)
            (root / "current.json").write_text(json.dumps({"version": "0.2.0", "managed": True}),
                                               encoding="utf-8-sig")       # as PowerShell writes it
            if HAVE_NUMPY:
                self.assertEqual(update.deployment(here), {"version": "0.2.0",
                                                           "mode": "managed"})
                (root / "current.json").write_text(json.dumps({"version": "0.2.0"}))
                self.assertEqual(update.deployment(here)["mode"], "self-service")
                self.assertEqual(update.deployment(root / "x.py")["mode"], "source")


if __name__ == "__main__":
    unittest.main()
