# Installation-aware updates and the empty-workspace migration gate (#26).
#   python -m unittest discover -s tests -v

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None

if HAVE_NUMPY:
    import schema_v1

    from workspace import holds, keystore, store, update
    from workspace.store import MigrationBlocked, SchemaTooNew, Store


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "workspace.db"

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, meeting=False, hold=False, job=False, receipt=False):
        s = Store(self.db, protector=keyprotect.TestProtector())
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
        s = Store(self.db, protector=keyprotect.TestProtector())
        hid = holds.add_hold(s, mid, "audio", "x")
        holds.release_hold(s, hid)
        s.close()
        kinds = {b["kind"] for b in update.blockers(self.db)}
        self.assertEqual(kinds, {"meetings"})


# Folder names a SQLite URI misreads unless the path is escaped (#164): "#"
# starts a fragment, "?" a query, and "%41" is decoded to "A". Windows does not
# allow "?" in a name, so that one runs on Linux only.
AWKWARD = ["a#b", "c%41d", "e%f"] + ([] if os.name == "nt" else ["g?h"])


def synthetic_workspace(db):
    """A synthetic workspace with a meeting, an active hold and a cleanup job."""
    db.parent.mkdir(parents=True, exist_ok=True)
    s = Store(db, protector=keyprotect.TestProtector())
    mid = s.create_meeting("Synthetic")
    holds.add_hold(s, mid, "all", "dispute")
    with s.tx() as con:
        con.execute("INSERT INTO cleanup_jobs (id, meeting_id, phase, state, intent_at) "
                    "VALUES ('j', ?, 'audio', 'failed', 0)", (mid,))
    s.close()


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class GatePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def workspace(self, db):
        synthetic_workspace(db)

    def test_awkward_folder_names_are_read_not_refused(self):
        for name in AWKWARD:
            with self.subTest(folder=name):
                db = Path(self.tmp.name) / name / "workspace.db"
                self.workspace(db)
                before = db.read_bytes()
                self.assertEqual(update.current_schema(db), store.SCHEMA_VERSION)
                kinds = {b["kind"] for b in update.blockers(db)}
                self.assertEqual(kinds, {"holds", "meetings", "cleanup"})
                g = update.gate(db, store.SCHEMA_VERSION)
                self.assertEqual((g["code"], g["kind"]), (update.ALLOWED, "compatible"))
                g = update.gate(db, store.SCHEMA_VERSION + 1)
                self.assertEqual((g["code"], g["kind"]), (update.WAIT, "migration"))
                self.assertEqual(db.read_bytes(), before)        # the gate wrote nothing
                # The gate's connection cannot write. Unescaped, a "#" also
                # dropped "?mode=ro" into the URI's fragment (#164).
                con = update._connect(db)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        con.execute("CREATE TABLE gate_wrote (x)")
                finally:
                    con.close()
                self.assertEqual(db.read_bytes(), before)

    def test_gate_command_reads_a_workspace_under_an_awkward_path(self):
        # The installer's check (python -m workspace --gate), in a child process
        # with LOCALAPPDATA pointing at a synthetic folder, never the real one.
        appdata = Path(self.tmp.name).joinpath(*AWKWARD)
        self.workspace(appdata / "Unminute" / "workspace" / "workspace.db")
        env = dict(os.environ, LOCALAPPDATA=str(appdata))
        root = Path(__file__).resolve().parents[1]
        for schema, code, kind in ((store.SCHEMA_VERSION, update.ALLOWED, "compatible"),
                                   (store.SCHEMA_VERSION + 1, update.WAIT, "migration")):
            with self.subTest(schema=schema):
                r = subprocess.run([sys.executable, "-m", "workspace", "--gate",
                                    "--schema", str(schema)], cwd=root, env=env,
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, code, r.stderr)
                result = json.loads(r.stdout.strip().splitlines()[-1])
                self.assertEqual((result["kind"], result["current_schema"]),
                                 (kind, store.SCHEMA_VERSION))

    def assert_refused_untouched(self, raw):
        def touched(*args, **kwargs):
            raise AssertionError("the gate touched a path that is not local")
        with mock.patch("os.stat", touched), mock.patch("os.path.exists", touched), \
                mock.patch("sqlite3.connect", touched):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                update._connect(raw)
            self.assertNotIn("server", str(caught.exception))      # no path (P3)
            self.assertNotIn("localhost", str(caught.exception))
            self.assertIs(update.current_schema(raw), update.UNREADABLE)
            g = update.gate(raw, store.SCHEMA_VERSION)
            self.assertEqual((g["code"], g["kind"]), (update.WAIT, "unreadable"))
            with self.assertRaises(sqlite3.OperationalError):
                update.blockers(raw)

    def test_a_path_not_on_a_local_drive_is_refused_untouched(self):
        # as_uri() turns \\localhost\C$\x into file://localhost/C%24/x, which
        # SQLite opens as /C$/x on the current drive: a different file. The
        # gate refuses UNC, extended, device and NT-namespace paths before
        # reading anything (no network access), and the update waits.
        paths = ["\\\\localhost\\C$\\x\\workspace.db", "\\\\127.0.0.1\\C$\\x\\workspace.db",
                 "//server/share/x/workspace.db", "\\\\?\\UNC\\server\\share\\x\\workspace.db"]
        if os.name == "nt":
            # Windows passes \??\ straight to the NT namespace, so \??\UNC\
            # reaches the network, though Python reads it as C:\??\... (#164).
            # On Linux these are ordinary relative names.
            paths += ["\\??\\UNC\\server\\share\\x\\workspace.db", "\\??\\C:\\x\\workspace.db",
                      "/??/UNC/server/share/x/workspace.db", "\\\\?\\C:\\x\\workspace.db",
                      "\\\\.\\pipe\\workspace.db", "C:\\x\\NUL"]
        for raw in paths:
            with self.subTest(path=raw):
                self.assert_refused_untouched(raw)

    def test_a_relative_path_under_a_unc_current_folder_is_refused_untouched(self):
        # The path as resolved against the current folder is checked too.
        unc = "\\\\server\\share\\" if os.name == "nt" else "//server/share/"
        with mock.patch("os.path.abspath", lambda p: unc + os.fspath(p)):
            self.assert_refused_untouched("workspace.db")

    def test_a_workspace_whose_existence_cannot_be_told_is_unreadable(self):
        # Only "not found" means no workspace; any other error fails closed.
        db = Path(self.tmp.name) / "w" / "workspace.db"
        self.workspace(db)
        with mock.patch("os.stat", side_effect=PermissionError(13, "Access is denied")):
            self.assertIs(update.current_schema(db), update.UNREADABLE)
            g = update.gate(db, store.SCHEMA_VERSION + 1)
        self.assertEqual((g["code"], g["kind"]), (update.WAIT, "unreadable"))

    def test_blockers_still_reads_a_workspace_whose_existence_cannot_be_told(self):
        # blockers() must not fail open: when os.stat is refused it reads the
        # database (or raises), and never reports an empty workspace.
        db = Path(self.tmp.name) / "w" / "workspace.db"
        self.workspace(db)
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) == "workspace.db":
                raise PermissionError(13, "Access is denied")
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", stat):
            with self.assertRaises(PermissionError):
                os.stat(db)                             # the refusal holds
            kinds = {b["kind"] for b in update.blockers(db)}
        self.assertEqual(kinds, {"holds", "meetings", "cleanup"})


EXTENDED = "\\\\?\\"


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class GateLongPathTests(unittest.TestCase):
    """A workspace whose path is longer than MAX_PATH (260). With long paths
    not enabled (LongPathsEnabled 0), Windows reports such a path as missing,
    and the gate must not then say "no workspace yet"."""

    def setUp(self):
        self.base = os.path.abspath(tempfile.mkdtemp())
        # Windows needs the extended form to create and remove the long path.
        self.ext = EXTENDED if os.name == "nt" else ""
        d = self.base
        for part in ["d" * 50] * 5:
            d = os.path.join(d, part)
            os.mkdir(self.ext + d)
        self.db = Path(d) / "workspace.db"
        self.assertGreater(len(str(self.db)), 260)
        short = Path(self.base) / "short" / "workspace.db"
        synthetic_workspace(short)
        shutil.copyfile(short, self.ext + str(self.db))

    def tearDown(self):
        shutil.rmtree(self.ext + self.base)

    def check_never_fresh(self):
        # Either read (long paths enabled, or not Windows) or UNREADABLE:
        # never None, which would let a schema-changing update go ahead.
        cur = update.current_schema(self.db)
        self.assertIn(cur, (store.SCHEMA_VERSION, update.UNREADABLE))
        g = update.gate(self.db, store.SCHEMA_VERSION + 1)
        self.assertEqual(g["code"], update.WAIT)
        self.assertIn(g["kind"], ("migration", "unreadable"))

    def test_a_workspace_beyond_max_path_is_never_fresh(self):
        # Fails without the fix on a Windows machine with LongPathsEnabled 0.
        self.check_never_fresh()

    @unittest.skipUnless(os.name == "nt", "Windows MAX_PATH and the extended path form")
    def test_a_workspace_beyond_max_path_is_never_fresh_without_long_paths(self):
        # The same with LongPathsEnabled 0 emulated, so the check holds on a
        # machine (such as CI) that has long paths enabled.
        real_stat, real_exists = os.stat, os.path.exists

        def legacy(path):
            s = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ""
            return len(s) >= 260 and not s.startswith(EXTENDED)

        def stat(path, *args, **kwargs):
            if legacy(path):
                raise FileNotFoundError(2, "The system cannot find the path specified")
            return real_stat(path, *args, **kwargs)

        def exists(path):
            return False if legacy(path) else real_exists(path)

        with mock.patch("os.stat", stat), mock.patch("os.path.exists", exists):
            self.assertFalse(self.db.exists())          # the emulation holds
            self.check_never_fresh()


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
        s = Store(self.db, protector=keyprotect.TestProtector())
        with s.tx() as db:
            db.execute("INSERT INTO receipts VALUES ('r', 0, ?, 'transcript removed')",
                       (time.time(),))
        s.close()
        store.SCHEMA_VERSION = self.saved + 1
        s = Store(self.db, protector=keyprotect.TestProtector())
        try:
            self.assertEqual(s._one("SELECT MAX(version) AS v FROM schema_version")["v"],
                             self.saved + 1)
            self.assertEqual(len(holds.receipts(s)), 1)
        finally:
            s.close()

    def test_watched_folder_history_survives_migration(self):
        # From schema 2 on, values are copied by name: crypto_meta is carried
        # with them, so the sealed folder path and the salt still open (#111).
        s = Store(self.db, protector=keyprotect.TestProtector())
        self.assertTrue(s.add_folder("f", r"C:\Rec", False))
        salt, _ = s.intake_salt()
        with s.tx() as db:
            db.execute("UPDATE watched_folders SET baseline_done = 1")
            db.execute("INSERT INTO intake_seen (folder_id, key, size, mtime_ns, file_id, "
                       "state, first_seen, last_seen) VALUES ('f', 'k', 1, 1, 1, 'baseline', 0, 0)")
        root = s._one("SELECT root FROM crypto_meta")[0]
        s.close()
        store.SCHEMA_VERSION = self.saved + 1
        s = Store(self.db, protector=keyprotect.TestProtector())
        try:
            self.assertEqual(s._one("SELECT state FROM intake_seen")["state"], "baseline")
            [folder] = s.folders()
            self.assertEqual((folder["path"], folder["baseline_done"]), (r"C:\Rec", 1))
            self.assertNotIn("damaged", folder)
            self.assertEqual((salt, False), s.intake_salt())
            self.assertEqual(root, s._one("SELECT root FROM crypto_meta")[0])
            self.assertFalse(s.add_folder("g", "c:\\rec\\", False))    # still the same folder
        finally:
            s.close()

    def test_interrupted_migration_leaves_the_old_workspace_intact(self):
        s = Store(self.db, protector=keyprotect.TestProtector())
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
                Store(self.db, protector=keyprotect.TestProtector())
        finally:
            store._statements = real
        store.SCHEMA_VERSION = self.saved
        s = Store(self.db,
                  protector=keyprotect.TestProtector())   # old version opens it unchanged
        try:
            self.assertEqual(len(holds.receipts(s)), 1)
        finally:
            s.close()

    def test_workspace_with_content_is_never_migrated(self):
        s = Store(self.db, protector=keyprotect.TestProtector())
        mid = s.create_meeting("Keep me")
        holds.add_hold(s, mid, "all", "legal")
        s.close()
        store.SCHEMA_VERSION = self.saved + 1
        with self.assertRaises(MigrationBlocked):
            Store(self.db, protector=keyprotect.TestProtector())
        store.SCHEMA_VERSION = self.saved
        s = Store(self.db,
                  protector=keyprotect.TestProtector())   # still intact, still held
        try:
            self.assertEqual(s.meeting(mid)["title"], "Keep me")
            self.assertEqual(len(holds.active_holds(s, mid)), 1)
        finally:
            s.close()

    def test_older_app_refuses_a_newer_workspace(self):
        store.SCHEMA_VERSION = self.saved + 1
        Store(self.db, protector=keyprotect.TestProtector()).close()
        store.SCHEMA_VERSION = self.saved
        with self.assertRaises(SchemaTooNew):
            Store(self.db, protector=keyprotect.TestProtector())


H_A, H_B = "a" * 64, "b" * 64


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SchemaOneMigrationTests(unittest.TestCase):
    """A real schema-1 workspace (tests/schema_v1.py) migrated to schema 2
    (#111 C2b-2): its configuration is sealed and fingerprinted, nothing it
    knew is forgotten, and no plain value is left in the files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "ws" / "workspace.db"
        self.db.parent.mkdir()
        self.folder = self.root / "Recordings"
        self.folder.mkdir()
        self.salt = "5a17" * 8
        self.files = []
        for name in ("old one.wav", "old two.wav"):
            (self.folder / name).write_bytes(b"RIFF synthetic " + name.encode())
            self.files.append(self.folder / name)

    def tearDown(self):
        self.tmp.cleanup()

    def schema_1(self, folders=None, meeting=False):
        """A schema-1 workspace with a watched folder whose files were all
        seen (baseline), the plain intake salt, one imported content hash and a
        receipt. Returns the plain path of the folder."""
        from workspace import intake
        path = os.path.realpath(self.folder)
        db = schema_v1.create(self.db)
        folders = folders or [("f1", path, 1.0, 0, 0)]
        db.executemany("INSERT INTO watched_folders (id, path, created_at, baseline_done, "
                       "delete_originals, fetch_cloud) VALUES (?, ?, ?, 1, ?, ?)", folders)
        for fid, *_ in folders:
            for f in self.files:
                st = f.stat()
                db.execute("INSERT INTO intake_seen (folder_id, key, size, mtime_ns, file_id, "
                           "state, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, 'baseline', "
                           "0, 0)", (fid, intake._key(self.salt.encode(), f), st.st_size,
                                     st.st_mtime_ns, getattr(st, "st_ino", 0)))
        db.execute("INSERT INTO intake_meta (k, v) VALUES ('salt', ?)", (self.salt,))
        db.execute("INSERT INTO intake_content (sha256, imported_at) VALUES (?, ?)",
                   (H_A, time.time()))
        db.execute("INSERT INTO intake_content (sha256, imported_at) VALUES ('not a hash', 1)")
        db.execute("INSERT INTO receipts VALUES ('r', 0, 1, 'transcript removed')")
        if meeting:
            db.execute("INSERT INTO meetings (id, title, created_at, updated_at) "
                       "VALUES ('m', 'Keep me', 0, 0)")
        db.close()
        return path

    def plain(self):
        """The schema-1 rows, read raw (the database must still be schema 1)."""
        con = sqlite3.connect(self.db)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
            return {"version": con.execute("SELECT MAX(version) FROM schema_version").fetchone(),
                    "crypto_meta": "crypto_meta" in names,
                    "folders": con.execute("SELECT id, path FROM watched_folders").fetchall(),
                    "salt": con.execute("SELECT v FROM intake_meta").fetchall(),
                    "content": con.execute("SELECT sha256 FROM intake_content "
                                           "ORDER BY sha256").fetchall()}
        finally:
            con.close()

    def open(self, **kwargs):
        kwargs.setdefault("protector", keyprotect.TestProtector())
        return Store(self.db, **kwargs)

    def test_configuration_is_sealed_and_nothing_known_is_forgotten(self):
        from workspace import intake
        path = self.schema_1()
        s = self.open()
        try:
            self.assertEqual(2, s._one("SELECT MAX(version) FROM schema_version")[0])
            [folder] = s.folders()
            self.assertEqual(path, folder["path"])
            self.assertEqual(s._one("SELECT path_fp FROM watched_folders")[0],
                             folder["path_fp"])
            # The same folder, in another case or with a trailing separator,
            # is already watched.
            for other in (path, path.upper() + "\\", path.lower()):
                with self.subTest(other=other):
                    self.assertFalse(s.add_folder(store.new_id(), other, False))
            # Content imported before is still known (by fingerprint only).
            self.assertTrue(intake.already_imported(s, s.fingerprint("original", H_A)))
            self.assertFalse(intake.already_imported(s, s.fingerprint("original", H_B)))
            self.assertEqual(1, s._one("SELECT COUNT(*) FROM intake_content")[0])
            self.assertEqual(1, len(holds.receipts(s)))
            # The salt is kept, sealed: the files already seen are not new.
            self.assertEqual((self.salt.encode(), False), s.intake_salt())
            if os.name != "nt":
                return                    # scanning uses Windows path rules (below)
            new = self.folder / "new.wav"
            new.write_bytes(b"RIFF synthetic new")
            queued = []
            later = time.time() + 1000

            def scan(now):
                return intake.scan(s, (".wav",), 1 << 30,
                                   lambda *a: queued.append(Path(a[0]).name), now=now)
            first = scan(later)
            self.assertEqual((0, 1), (first["queued"], first["waiting"]))
            scan(later + 10)
            self.assertEqual(["new.wav"], queued)
        finally:
            s.close()

    def test_folders_listed_twice_are_merged_with_the_least_authority(self):
        path = os.path.realpath(self.folder)
        self.schema_1(folders=[("f1", path, 1.0, 1, 1), ("f2", path.upper() + "\\", 2.0, 0, 1)])
        s = self.open()
        try:
            [folder] = s.folders()
            self.assertEqual(("f1", path, 0, 1), (folder["id"], folder["path"],
                                                  folder["delete_originals"],
                                                  folder["fetch_cloud"]))
            self.assertEqual(folder["last_error"], keystore.MERGED_NOTE)
            self.assertEqual({"f1"}, {r[0] for r in s._all("SELECT folder_id FROM intake_seen")})
        finally:
            s.close()

    def test_no_plain_value_is_left_in_the_files(self):
        path = self.schema_1()
        # Values removed earlier without secure_delete, on pages already free
        # before the migration (more than it reuses), which dropping the
        # tables does not overwrite: only compacting the file removes them.
        removed = r"C:\Removed synthetic folder"
        con = sqlite3.connect(self.db)
        con.execute("PRAGMA secure_delete=OFF")
        con.execute("CREATE TABLE old_rows (x TEXT)")
        con.executemany("INSERT INTO old_rows VALUES (?)",
                        [(f"{removed} {i} " + "x" * 2000,) for i in range(300)])
        con.commit()
        con.execute("DROP TABLE old_rows")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        before = b"".join(p.read_bytes() for p in self.db.parent.iterdir()
                          if p.name.startswith("workspace.db"))
        self.assertIn(removed.encode(), before)                 # the control
        self.open().close()
        raw = b"".join(p.read_bytes() for p in self.db.parent.iterdir()
                       if p.name.startswith("workspace.db"))
        for plain in (path, self.salt, H_A, removed):
            with self.subTest(plain=plain):
                self.assertNotIn(plain.encode(), raw)
        self.assertFalse((self.db.parent / "compact.pending").exists())

    def test_an_interrupted_migration_leaves_schema_1(self):
        self.schema_1()
        before = self.plain()
        real_statements, real_transform = store._statements, keystore.transform_from_1

        def crash(*args):
            raise KeyboardInterrupt("power cut")
        for where in ("statements", "transforms"):
            with self.subTest(crash=where):
                if where == "statements":
                    store._statements = crash
                else:
                    keystore.transform_from_1 = crash
                try:
                    with self.assertRaises(KeyboardInterrupt):
                        self.open()
                finally:
                    store._statements, keystore.transform_from_1 = (real_statements,
                                                                    real_transform)
                self.assertEqual(before, self.plain())
                self.assertEqual((1,), before["version"])
                self.assertFalse(before["crypto_meta"])
        s = self.open()                       # and it migrates once the crash is gone
        s.close()

    def test_a_key_that_cannot_be_protected_leaves_schema_1(self):
        self.schema_1()
        before = self.plain()
        for mode in ("raise", "lock"):
            with self.subTest(mode=mode), self.assertRaises(store.KeyUnavailable) as caught:
                self.open(protector=keyprotect.TestProtector(fail=True), on_unavailable=mode)
            self.assertEqual(("windows", 13), (caught.exception.reason, caught.exception.code))
            self.assertEqual(before, self.plain())
        self.assertFalse((self.db.parent / "compact.pending").exists())

    def test_a_schema_1_workspace_with_content_is_never_migrated(self):
        self.schema_1(meeting=True)
        before = self.plain()
        with self.assertRaises(MigrationBlocked):
            self.open()
        self.assertEqual(before, self.plain())

    def test_status_and_gate_need_no_key(self):
        # A schema-2 workspace made with the test protector: the app's own
        # --status and --gate (Windows Data Protection, which cannot open it)
        # read plain counts only.
        appdata = self.root / "appdata"
        db = appdata / "Unminute" / "workspace" / "workspace.db"
        db.parent.mkdir(parents=True)
        s = Store(db, protector=keyprotect.TestProtector())
        mid = s.create_meeting("Synthetic")
        holds.add_hold(s, mid, "all", "synthetic")
        s.close()
        env = dict(os.environ, LOCALAPPDATA=str(appdata))
        root = Path(__file__).resolve().parents[1]
        r = subprocess.run([sys.executable, "-B", "-m", "workspace", "--status"], cwd=root,
                           env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr)
        status = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual((1, 1, 2, True), (status["meetings"], status["holds"],
                                           status["schema"], status["readable"]))
        r = subprocess.run([sys.executable, "-B", "-m", "workspace", "--gate", "--schema", "3"],
                           cwd=root, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(update.WAIT, r.returncode, r.stderr)
        self.assertEqual({"holds", "meetings"},
                         {b["kind"] for b in json.loads(r.stdout.strip().splitlines()[-1])
                          ["blockers"]})


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SchemaTwoGateTests(unittest.TestCase):
    """What a schema-2 workspace holds that blocks a storage-format change
    (#111 C2b-2): the root key and configuration never do; a key or
    recording row left without its meeting, or meeting audio left in the
    media folder, does, and nothing is deleted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "workspace.db"
        s = Store(self.db, protector=keyprotect.TestProtector())
        s.add_folder("f", r"C:\Rec", False)
        s.intake_salt()
        with s.tx() as db:
            db.execute("INSERT INTO receipts VALUES ('r', 0, 1, 'audio removed')")
        self.store = s

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def kinds(self):
        return {b["kind"] for b in update.blockers(self.db)}

    def test_the_key_and_configuration_alone_do_not_block(self):
        self.assertEqual(set(), self.kinds())
        g = update.gate(self.db, store.SCHEMA_VERSION + 1)
        self.assertEqual((update.ALLOWED, "migration"), (g["code"], g["kind"]))

    def test_leftover_keys_and_audio_make_the_update_wait(self):
        s = self.store
        mid = s.create_meeting("synthetic")
        s.db.execute("PRAGMA foreign_keys=OFF")   # a key row left without its meeting
        s.db.execute("DELETE FROM meetings WHERE id = ?", (mid,))
        s.db.execute("PRAGMA foreign_keys=ON")
        self.assertEqual({"content"}, self.kinds())
        s.db.execute("DELETE FROM content_keys")
        self.assertEqual(set(), self.kinds())
        s.create_recording_key("rec-1")
        self.assertEqual({"content"}, self.kinds())
        s.destroy_recording_key("rec-1")
        self.assertEqual(set(), self.kinds())
        audio = self.db.parent / "media" / store.new_id()
        audio.mkdir(parents=True)
        (self.db.parent / "media" / store.new_id()).mkdir()       # empty: nothing to keep
        self.assertEqual(set(), self.kinds())
        (audio / "audio.f32").write_bytes(b"\0" * 64)
        found = update.blockers(self.db)
        self.assertEqual([("media", 1, "Meeting audio files are still in the workspace.")],
                         [(b["kind"], b["count"], b["message"]) for b in found])
        g = update.gate(self.db, store.SCHEMA_VERSION + 1)
        self.assertEqual(update.WAIT, g["code"])
        self.assertTrue((audio / "audio.f32").exists())           # never deleted


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
