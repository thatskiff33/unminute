# Schema 2 (#111 C2b-2; docs/ENCRYPTION.md): the workspace's key, sealed
# content, fingerprints, damaged values and the erase points.
#   - Opening: a new workspace keeps one protected root key and nothing is on
#     disk if it cannot be protected; a key that cannot be used is refused
#     (KeyUnavailable, with its reason), never replaced and never plain text,
#     and nothing is written; lock mode opens read-only.
#   - The test protector is refused for the real workspace.
#   - Sealing: values open only in their own row and parent; row ids never
#     come back; the stored formats are pinned (known answers).
#   - The store's lock: content is sealed and opened only under it, and each
#     accessor fetches and opens under one hold.
#   - Damaged values: shown as a placeholder, refused for anything produced
#     from them; cleanup and discard still finish; the runner stops without
#     counting a failure.
#   - Erase points: every deletion of a meeting removes its keys and its
#     recording source; audio cleanup removes the audio key.
# Synthetic text only.
#   python tools/run_tests.py

import hashlib
import importlib.util
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import keyprotect  # noqa: E402
import profile_elsewhere  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av", "cryptography"))
SR = 16000
H = "a" * 64

if HAVE_DEPS:
    from workspace import cleanup, diag, handoff, holds, intake, keystore, runner, store, update
    from workspace import speaker_names, vault
    from workspace.dpapi import DpapiError
    from workspace.store import DamagedContent, KeyMissing, KeyUnavailable, Store, StoreError


def open_store(path, **kwargs):
    kwargs.setdefault("protector", keyprotect.TestProtector())
    return Store(path, **kwargs)


def passage(start, end, text, words=()):
    return types.SimpleNamespace(start=start, end=end, text=text, words=words)


def meeting_with_text(s, title="synthetic standup", texts=("first passage", "second passage"),
                      original_path=None, recording=None):
    """A meeting with a source, a version and committed passages. Returns
    (mid, sid, vid)."""
    mid = s.create_meeting(title)
    total = 60 * SR
    sid = s.add_source(mid, original_path=original_path, original_sha256=H, original_size=1,
                       original_mtime_ns=0, audio_id="b" * 64, samples=total, sample_rate=SR,
                       recording=recording)
    vid = s.ensure_version(mid, sid, engine="fake", model="fake", model_revision="1",
                           precision="int8", config={}, chunk_plan=[(0, total, 0, total)])
    assert s.commit_chunk(vid, 0, [passage(i * 5.0, i * 5.0 + 4, t, [(i * 5.0, i * 5.0 + 1, t)])
                                   for i, t in enumerate(texts)])
    return mid, sid, vid


def flip(blob):
    """The same sealed value with one byte of its ciphertext changed."""
    data = bytearray(blob)
    data[-1] ^= 1
    return bytes(data)


def state_of(path):
    """What a database holds, to show that opening it wrote nothing: its
    schema, every table's row count, and the size of its write-ahead log."""
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        master = con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        counts = {name: con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                  for kind, name, _ in master if kind == "table"}
    finally:
        con.close()
    wal = Path(str(path) + "-wal")
    return master, counts, wal.stat().st_size if wal.exists() else 0


class OtherProtector(keyprotect.TestProtector):
    __test__ = False
    name = "other"


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class OpeningTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ws" / "workspace.db"

    def tearDown(self):
        self.tmp.cleanup()

    def made(self):
        s = open_store(self.db)
        mid = s.create_meeting("synthetic title")
        s.start_attempt(*self._plan(s, mid))
        s.close()
        return mid

    @staticmethod
    def _plan(s, mid):
        sid = s.add_source(mid, original_path=None, original_sha256=H, original_size=1,
                           original_mtime_ns=0, audio_id=H, samples=SR, sample_rate=SR)
        vid = s.ensure_version(mid, sid, engine="e", model="m", model_revision="r",
                               precision="p", config={}, chunk_plan=[(0, SR, 0, SR)])
        return vid, 0

    def assert_refused(self, reason, protector=None, mode="raise"):
        before = state_of(self.db)
        with self.assertRaises(KeyUnavailable) as caught:
            open_store(self.db, protector=protector or keyprotect.TestProtector(),
                       on_unavailable=mode)
        self.assertEqual(reason, caught.exception.reason)
        self.assertEqual(KeyUnavailable.MESSAGE, str(caught.exception))     # no path or value
        self.assertEqual(before, state_of(self.db))                         # nothing written
        return caught.exception

    def test_a_new_workspace_keeps_one_key_and_opens_again(self):
        mid = self.made()
        con = sqlite3.connect(self.db)
        try:
            rows = con.execute("SELECT id, format, protector FROM crypto_meta").fetchall()
            self.assertEqual([(1, 1, "test")], rows)
            self.assertEqual(2, con.execute("SELECT MAX(version) FROM schema_version")
                             .fetchone()[0])
            keys = con.execute("SELECT purpose FROM content_keys WHERE meeting_id = ? "
                               "ORDER BY purpose", (mid,)).fetchall()
            self.assertEqual([("audio",), ("text",)], keys)
        finally:
            con.close()
        s = open_store(self.db)
        try:
            self.assertEqual("synthetic title", s.meeting(mid)["title"])
        finally:
            s.close()

    def test_a_key_that_cannot_be_protected_leaves_no_workspace(self):
        for mode in ("raise", "lock"):
            with self.subTest(mode=mode):
                with self.assertRaises(KeyUnavailable) as caught:
                    open_store(self.db, protector=keyprotect.TestProtector(fail=True),
                               on_unavailable=mode)
                self.assertEqual(("windows", 13),
                                 (caught.exception.reason, caught.exception.code))
                self.assertEqual([], sorted(p.name for p in self.db.parent.iterdir()))
                self.assertIsNone(update.current_schema(self.db))
        open_store(self.db).close()                  # a working protector then succeeds
        self.assertEqual(2, update.current_schema(self.db))

    def test_a_creation_cut_short_leaves_no_workspace_and_starts_again(self):
        with mock.patch.object(store, "_install_new", side_effect=KeyboardInterrupt("cut")):
            with self.assertRaises(KeyboardInterrupt):
                open_store(self.db)
        self.assertEqual([], sorted(p.name for p in self.db.parent.iterdir()))
        # A start killed between the build and the rename leaves the new file
        # (and perhaps its journal): the next start removes them and builds afresh.
        (self.db.parent / "workspace.db.new").write_bytes(b"left by a start that was killed")
        (self.db.parent / "workspace.db.new-journal").write_bytes(b"x")
        s = open_store(self.db)
        s.close()
        self.assertFalse((self.db.parent / "workspace.db.new").exists())
        self.assertFalse((self.db.parent / "workspace.db.new-journal").exists())

    def test_a_stub_is_built_afresh_but_rows_without_a_version_are_refused(self):
        self.db.parent.mkdir(parents=True)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE meetings (id TEXT)")       # from an older build, empty
        con.close()
        s = open_store(self.db)
        try:
            self.assertEqual(1, s._one("SELECT COUNT(*) FROM crypto_meta")[0])
            self.assertEqual(2, s._one("SELECT MAX(version) FROM schema_version")[0])
        finally:
            s.close()
        other = Path(self.tmp.name) / "rows" / "workspace.db"
        other.parent.mkdir()
        con = sqlite3.connect(other)
        con.execute("CREATE TABLE meetings (id TEXT)")
        con.execute("INSERT INTO meetings VALUES ('synthetic')")
        con.commit()
        con.close()
        before = other.read_bytes()
        self.db = other
        self.assert_refused("missing")
        self.assertEqual(before, other.read_bytes())

    def test_a_changed_check_value_is_refused(self):
        self.made()
        con = sqlite3.connect(self.db)
        con.execute("UPDATE crypto_meta SET check_value = ?", (b"\0" * 32,))
        con.commit()
        con.close()
        self.assert_refused("key")

    def test_another_protectors_workspace_is_refused(self):
        self.made()
        self.assert_refused("protector", protector=OtherProtector())

    def test_a_workspace_without_its_key_is_refused_and_never_given_a_new_one(self):
        self.made()
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM crypto_meta")
        con.commit()
        con.close()
        self.assert_refused("missing")
        s = open_store(self.db, on_unavailable="lock")       # or opened locked, never given one
        try:
            self.assertEqual("missing", s.locked.reason)
        finally:
            s.close()
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(0, con.execute("SELECT COUNT(*) FROM crypto_meta").fetchone()[0])
        finally:
            con.close()

    def test_lock_mode_opens_read_only_and_writes_nothing(self):
        mid = self.made()
        before = state_of(self.db)
        s = open_store(self.db, protector=keyprotect.TestProtector(fail=True),
                       on_unavailable="lock")
        try:
            self.assertEqual(("windows", 13), (s.locked.reason, s.locked.code))
            self.assertIsNone(s.keyring)
            # Plain reads work.
            self.assertEqual("new", s.meeting_state(mid)["phase"])
            self.assertEqual(1, len(s.meetings(content=False)))
            self.assertEqual(0, s._one("SELECT COUNT(*) FROM holds")[0])
            # Nothing is opened, sealed or written.
            for call in (lambda: s.meeting(mid), lambda: s.tx().__enter__(),
                         lambda: s.create_meeting("x"), lambda: s.fingerprint("original", H),
                         lambda: s.meeting_key(mid, "text")):
                with self.assertRaises(KeyUnavailable):
                    call()
            with s._lock:
                for call in (s._seal, s._unseal):
                    with self.assertRaises(KeyUnavailable):
                        call(mid, "meetings", "title", (mid,), "x")
            # mark_interrupted did not run: the attempt is still running.
            self.assertEqual("running", s._one("SELECT result FROM attempts")[0])
            self.assertEqual(0, s.db.total_changes)
        finally:
            s.close()
        self.assertEqual(before, state_of(self.db))

    def test_there_is_no_plain_path(self):
        # Without Windows Data Protection (as on Linux, or a profile that
        # cannot use it), the default protector refuses: no workspace is made
        # and nothing is ever stored as plain text.
        self.assertFalse(hasattr(store, "PlainCodec"))
        self.assertNotIn("codec", inspect.signature(Store).parameters)
        from workspace import app
        self.assertNotIn("codec", inspect.signature(app.Workspace).parameters)
        failing = DpapiError("Protecting the workspace key needs Windows.", 0)
        with mock.patch("workspace.dpapi.protect", side_effect=failing):
            with self.assertRaises(KeyUnavailable) as caught:
                Store(self.db)
        self.assertEqual("windows", caught.exception.reason)
        self.assertEqual([], list(self.db.parent.iterdir()))

    def local(self):
        """LOCALAPPDATA in the temporary folder: (environment, its workspace)."""
        base = Path(self.tmp.name) / "LocalAppData"
        return {"LOCALAPPDATA": str(base)}, base / "Unminute" / "workspace"

    def test_the_test_protector_is_refused_for_the_real_workspace(self):
        env, real = self.local()
        with mock.patch.dict(os.environ, env):
            for protector in (keyprotect.TestProtector(), OtherProtector()):
                with self.subTest(protector=protector.name):
                    with self.assertRaises(StoreError) as caught:
                        Store(real / "workspace.db", protector=protector)
                    self.assertEqual(store.PROTECTOR_REFUSED, str(caught.exception))
            # Refused by the store even when the protector would allow it.
            permissive = OtherProtector()
            permissive.allows = lambda path: True
            with self.assertRaises(StoreError):
                Store(real / "workspace.db", protector=permissive)
            self.assertFalse(real.exists())                  # nothing touched
            # Elsewhere, the test protector works.
            open_store(self.db).close()

    def test_a_protector_named_dpapi_is_the_only_one_for_the_real_workspace(self):
        env, real = self.local()

        class Named(keyprotect.TestProtector):
            __test__ = False
            name = "dpapi"

            def allows(self, path):
                return True
        with mock.patch.dict(os.environ, env):
            Store(real / "workspace.db", protector=Named()).close()   # the name check passes

    def test_the_real_workspace_is_guarded_wherever_its_folder_is_refused(self):
        # default_root() refuses a redirected, synced, network or broken
        # folder, but a real workspace may still be there on disk: only a
        # missing LOCALAPPDATA (Linux CI) lets the test protector through.
        env, real = self.local()
        refused = StoreError(store.REDIRECTED["onedrive"])
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(store, "default_root", side_effect=refused):
            self.assertTrue(store.under_workspace_root(real / "workspace.db"))
            with self.assertRaises(StoreError) as caught:
                Store(real / "workspace.db", protector=keyprotect.TestProtector())
            self.assertEqual(store.PROTECTOR_REFUSED, str(caught.exception))
        self.assertFalse(real.exists())
        without = {k: v for k, v in os.environ.items() if k != "LOCALAPPDATA"}
        with mock.patch.dict(os.environ, without, clear=True):
            self.assertFalse(store.under_workspace_root(real / "workspace.db"))

    def test_replacing_the_default_protector_does_not_open_the_real_workspace(self):
        # The store's own check compares with a constant, not with the
        # default protector class, which a test may replace.
        env, real = self.local()

        class Permissive(keyprotect.TestProtector):
            __test__ = False

            def allows(self, path):
                return True
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(keystore, "DpapiProtector", Permissive):
            with self.assertRaises(StoreError) as caught:
                Store(real / "workspace.db", protector=None)       # the (replaced) default
            self.assertEqual(store.PROTECTOR_REFUSED, str(caught.exception))
        self.assertFalse(real.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows Data Protection")
    def test_windows_data_protection_round_trip(self):
        s = Store(self.db)                                  # the app's own protector
        mid = s.create_meeting("synthetic title")
        s.close()
        s = Store(self.db)
        try:
            self.assertEqual("synthetic title", s.meeting(mid)["title"])
        finally:
            s.close()
        self.assert_refused("protector")                    # the test protector cannot open it
        other = Path(self.tmp.name) / "other" / "workspace.db"
        open_store(other).close()
        with self.assertRaises(KeyUnavailable) as caught:
            Store(other)                                    # nor DPAPI a test workspace
        self.assertEqual("protector", caught.exception.reason)
        r = subprocess.run([sys.executable, "-B", "-c",
                            "from workspace.__main__ import dpapi_round_trip; "
                            "print(dpapi_round_trip())"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual("(True, 'dpapi round trip')", r.stdout.strip().splitlines()[-1],
                         r.stderr)


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class SealingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = open_store(Path(self.tmp.name) / "workspace.db")

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def raw(self, sql, args=()):
        """A raw change, as someone who can write the file could make."""
        self.s.db.execute(sql, args)

    def test_a_value_opens_only_in_its_own_row(self):
        mid, _, vid = meeting_with_text(self.s, texts=("first passage", "second passage",
                                                       "third passage"))
        a, b, c = (r["id"] for r in self.s.transcript(vid))
        blob_a = self.s._one("SELECT text_sealed FROM segments WHERE id = ?", (a,))[0]
        blob_b = self.s._one("SELECT text_sealed FROM segments WHERE id = ?", (b,))[0]
        self.raw("UPDATE segments SET text_sealed = ? WHERE id = ?", (blob_b, a))
        self.raw("UPDATE segments SET text_sealed = ? WHERE id = ?", (blob_a, b))
        rows = {r["id"]: r for r in self.s.transcript(vid)}
        for sid in (a, b):
            self.assertEqual(store.DAMAGED_TEXT, rows[sid]["text"])
            self.assertIn("text", rows[sid]["damaged"])
        self.assertEqual("third passage", rows[c]["text"])
        # An edit's history moved to another passage does not open there, and
        # undo refuses and changes nothing (D1).
        self.s.save_edit(c, "edited third")
        self.s.save_edit(c, "edited third again")      # its history holds the first edit
        hid = self.s._one("SELECT MAX(id) FROM edit_history")[0]
        self.raw("PRAGMA foreign_keys=OFF")
        self.raw("UPDATE edit_history SET segment_id = ? WHERE id = ?", (a, hid))
        self.raw("PRAGMA foreign_keys=ON")
        before = self.s.transcript(vid)
        with self.assertRaises(DamagedContent):
            self.s.undo_edit(vid)
        self.assertEqual(before, self.s.transcript(vid))

    def test_a_first_edits_history_opens_only_for_its_own_passage(self):
        # A passage's first edit has no "before": its entry is still bound to
        # its passage, so moved to another one, undo refuses and the other
        # passage's edit stays (D1).
        mid, _, vid = meeting_with_text(self.s, texts=("alpha", "beta"))
        a, b = (r["id"] for r in self.s.transcript(vid))
        self.s.save_edit(a, "edited alpha")
        self.s.save_edit(b, "edited beta")              # its entry: before NULL
        hid = self.s._one("SELECT MAX(id) FROM edit_history")[0]
        self.assertIsNone(self.s._one("SELECT before_sealed FROM edit_history WHERE id = ?",
                                      (hid,))[0])
        self.raw("PRAGMA foreign_keys=OFF")
        self.raw("UPDATE edit_history SET segment_id = ? WHERE id = ?", (a, hid))
        self.raw("PRAGMA foreign_keys=ON")
        before = self.s.transcript(vid)
        with self.assertRaises(DamagedContent):
            self.s.undo_edit(vid)
        self.assertEqual(before, self.s.transcript(vid))
        self.assertEqual("edited alpha", self.s.passage(a)["text"])
        # An entry with neither side cannot be bound to anything: refused too.
        self.raw("UPDATE edit_history SET segment_id = ?, after_sealed = NULL WHERE id = ?",
                 (b, hid))
        with self.assertRaises(DamagedContent):
            self.s.undo_edit(vid)
        self.assertEqual(before, self.s.transcript(vid))

    def test_row_ids_never_come_back(self):
        mid, _, vid = meeting_with_text(self.s)
        first = self.s.transcript(vid)[0]["id"]
        self.s.save_edit(first, "edit one")
        h1 = self.s._one("SELECT MAX(id) FROM edit_history")[0]
        self.assertEqual(first, self.s.undo_edit(vid))
        self.s.save_edit(first, "edit two")
        self.assertGreater(self.s._one("SELECT MAX(id) FROM edit_history")[0], h1)
        rid = self.s.start_speaker_run(vid)
        self.assertTrue(self.s.finish_speaker_run(rid, [(0.0, 9.0, "SPEAKER_00")],
                                                  [(0.0, 9.0, "SPEAKER_00")]))
        speaker_names.rename(self.s, vid, "SPEAKER_00", "Synthetic One")
        c1 = self.s._one("SELECT MAX(id) FROM speaker_corrections")[0]
        self.assertEqual("name", speaker_names.undo(self.s, vid))
        speaker_names.rename(self.s, vid, "SPEAKER_00", "Synthetic Two")
        self.assertGreater(self.s._one("SELECT MAX(id) FROM speaker_corrections")[0], c1)
        top = self.s._one("SELECT MAX(id) FROM segments")[0]
        with self.s.tx() as db:
            self.s.delete_meeting_content(db, mid)
        self.assertIsNone(self.s._one("SELECT MAX(id) FROM segments")[0])
        _, _, vid2 = meeting_with_text(self.s, title="synthetic later")
        self.assertGreater(min(r["id"] for r in self.s.transcript(vid2)), top)

    def test_the_stored_formats_are_pinned(self):
        # Known answers (the C1 practice): a refactor cannot silently change
        # where a value is bound, or a fingerprint.
        mid = "0123456789abcdef0123456789abcdef"
        rows = {"meetings": (mid,), "reviewed_snapshots": (mid,), "sources": ("s1",),
                "segments": (7, "v1"), "edits": (7,), "edit_history": (3, 7),
                "speaker_runs": ("r1", "v1"), "speaker_corrections": (5, "r1"),
                "handoff_options": (mid,), "holds": ("h1",), "recording_sources": (mid,),
                "watched_folders": ("f1",), "intake_meta": ("salt",)}
        got = {}
        for table, cols in store.CONTENT_COLUMNS.items():
            meeting = None if table in ("watched_folders", "intake_meta") else mid
            for physical in cols.values():
                ctx = keystore.context(meeting, table, physical, rows[table])
                got[(table, physical)] = hashlib.sha256(vault.encode("cell", ctx)).hexdigest()
        self.assertEqual(KNOWN_CONTEXTS, got)
        self.assertEqual(("meeting", mid, "recording_sources", "info_sealed", mid),
                         keystore.context(mid, "recording_sources", "info_sealed", (mid,)))
        keyring = vault.Keyring(bytes(range(32)))
        self.assertEqual(KNOWN_FINGERPRINTS, {
            "original": keystore.fingerprint(keyring, "original", "00" * 32),
            "snapshot": keystore.fingerprint(keyring, "snapshot", "00" * 32),
            "folder": keystore.folder_fingerprint(
                keyring, keystore.stored_folder_identity("C:\\Rec\\"))})
        self.assertEqual("756e6d696e7574652f76312f6b65790073000000097265636f7264696e6773000000"
                         "057265632d31", vault.encode("key", ("recording", "rec-1")).hex())
        with self.assertRaises(ValueError):
            keystore.fingerprint(keyring, "original", "A" * 64)      # lower-case hex only

    def test_content_is_sealed_and_opened_only_under_the_store_lock(self):
        mid, _, vid = meeting_with_text(self.s)
        blob = self.s._one("SELECT title_sealed FROM meetings")[0]
        for call, value in ((self.s._seal, "x"), (self.s._unseal, blob)):
            with self.assertRaisesRegex(RuntimeError, "store lock not held"):
                call(mid, "meetings", "title", (mid,), value)
        with self.s._lock:
            self.assertEqual("synthetic standup",
                             self.s._unseal(mid, "meetings", "title", (mid,), blob))

        # Each accessor fetches and opens under one hold of the lock (a
        # cleanup cannot delete the keys in between).
        hid = holds.add_hold(self.s, mid, "audio", "synthetic reason")
        self.s.add_folder("f1", r"C:\Rec", False)
        self.s.intake_salt()
        rid = self.s.start_speaker_run(vid)
        self.s.finish_speaker_run(rid, [(0.0, 9.0, "S0")], [(0.0, 9.0, "S0")])
        sid = self.s.sources(mid)[0]["id"]
        seg = self.s.passage_ids(vid)[0]
        calls = {"meeting": lambda: self.s.meeting(mid), "meetings": self.s.meetings,
                 "source": lambda: self.s.source(sid), "sources": lambda: self.s.sources(mid),
                 "transcript": lambda: self.s.transcript(vid),
                 "transcript_page": lambda: self.s.transcript_page(vid, 0, 1),
                 "passage": lambda: self.s.passage(seg),
                 "speaker_runs": lambda: self.s.speaker_runs(vid),
                 "current_run": lambda: self.s.current_run(vid),
                 "corrections": lambda: self.s.corrections(rid), "holds": self.s.holds,
                 "handoff_options": lambda: self.s.handoff_options(mid),
                 "snapshot": lambda: self.s.snapshot(mid), "folders": self.s.folders,
                 "search": lambda: self.s.search(vid, "first"),
                 "recording_info": lambda: self.s.recording_info(mid),
                 "intake_salt": self.s.intake_salt,
                 # Fetched and unwrapped under one hold (#111 C3a reads audio with it).
                 "meeting_key": lambda: self.s.meeting_key(mid, "audio")}
        for name, call in calls.items():
            with self.subTest(accessor=name):
                before = self.s._lock.acquisitions
                call()
                self.assertEqual(1, self.s._lock.acquisitions - before)
        self.assertTrue(hid)

    def test_the_key_cache_follows_commits(self):
        mid = self.s.create_meeting("synthetic one")
        self.assertIn(mid, self.s._wrapped)
        known = dict(self.s._wrapped)
        with self.assertRaises(TypeError):
            self.s.create_meeting(1234)                 # fails inside its transaction
        self.assertEqual(known, self.s._wrapped)        # its key never reached the cache
        with self.s.tx() as db:
            self.s.delete_meeting_row(db, mid)
        self.assertNotIn(mid, self.s._wrapped)
        self.assertEqual({}, self.s._sealers)
        with self.s._lock, self.assertRaises(KeyMissing):
            self.s._seal(mid, "meetings", "title", (mid,), "x")


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class DamagedValueTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = open_store(self.root / "workspace.db")
        self.log = diag.Log(self.root / "logs", "app")
        self.s.log = self.log
        profile_elsewhere.start(self)       # folders in %TEMP%, inside AppData (#111 D2)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def damage(self, table, column, where="1", args=()):
        """Change one byte of a sealed value, raw."""
        for rowid, blob in self.s.db.execute(f"SELECT rowid, {column} FROM {table} "
                                             f"WHERE {where}", args).fetchall():
            self.s.db.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                              (flip(blob), rowid))

    def events(self):
        if not self.log.path.exists():
            return []
        return [json.loads(line) for line in self.log.path.read_text("ascii").splitlines()]

    def test_a_damaged_title_is_shown_as_a_placeholder_and_the_meeting_can_go(self):
        mid, _, vid = meeting_with_text(self.s, title="MARKER-TITLE synthetic")
        self.damage("meetings", "title_sealed")
        [m] = self.s.meetings()
        self.assertEqual((store.DAMAGED_TEXT, ["title"]), (m["title"], m["damaged"]))
        self.assertEqual(store.DAMAGED_TEXT, holds.unfinished_work(self.s)[0]["title"])
        # One content-free event per (table, column): no value in the log.
        found = [e for e in self.events() if e["kind"] == "damaged_value"]
        self.assertTrue(found)
        self.assertEqual({("meetings", "title")}, {(e["table"], e["column"]) for e in found})
        self.assertNotIn("MARKER", self.log.path.read_text("ascii"))
        # Discarding it still works: deleting rows and keys needs no content.
        for jid in cleanup.discard(self.s, mid):
            self.assertEqual(cleanup.JobResult.DONE, cleanup.run_job(self.s, jid,
                                                                     self.root / "media"))
        self.assertIsNone(self.s.meeting_state(mid))
        self.assertEqual(0, self.s._one("SELECT COUNT(*) FROM content_keys")[0])

    def test_nothing_is_produced_from_damaged_text(self):
        mid, _, vid = meeting_with_text(self.s)
        a, b = self.s.passage_ids(vid)
        self.s.save_edit(a, "edited by hand")
        self.damage("edits", "text_sealed")
        self.assertEqual(store.DAMAGED_TEXT, self.s.passage(a)["text"])     # shown as such
        with self.assertRaises(DamagedContent):
            self.s.save_edit(a, "a new edit")               # never replaces an unreadable edit
        self.assertEqual(1, self.s.edit_count(vid))
        with self.assertRaisesRegex(cleanup.CleanupError, "could not be read"):
            cleanup.confirm_transcript_reviewed(self.s, mid)
        self.assertIsNone(self.s.snapshot(mid))
        self.assertIsNone(self.s.meeting_state(mid)["reviewed_at"])
        self.assertEqual(0, self.s._one("SELECT COUNT(*) FROM cleanup_jobs")[0])
        with self.s.tx() as db:                               # reviewed: a plain column
            db.execute("UPDATE meetings SET reviewed_at = 1")
        template = {"id": "t", "title": "T", "prompt": "Summarise.", "version": 1}
        with self.assertRaises(DamagedContent):
            handoff.build(self.s, mid, template)
        self.assertEqual([b], self.s.search(vid, "second"))
        self.assertEqual([], self.s.search(vid, "edited"))  # a damaged passage is skipped
        # Search names the value it tried: the edit, not the generated text.
        noted = {(e["table"], e["column"]) for e in self.events()
                 if e["kind"] == "damaged_value"}
        self.assertIn(("edits", "text"), noted)
        self.assertNotIn(("segments", "text"), noted)

    def test_only_what_goes_out_is_strict(self):
        # Word timings, a meeting's note and other notes the app writes never
        # go out: damaged, they do not refuse an edit or the handoff.
        mid, _, vid = meeting_with_text(self.s)
        a, _b = self.s.passage_ids(vid)
        self.s.set_phase(mid, "transcribed", "synthetic note")
        self.damage("segments", "words_sealed")
        self.damage("meetings", "last_error_sealed")
        self.assertTrue(self.s.save_edit(a, "edited by hand"))
        self.assertEqual("edited by hand", self.s.passage(a)["text"])
        with self.s.tx() as db:                               # reviewed: a plain column
            db.execute("UPDATE meetings SET reviewed_at = 1")
        template = {"id": "t", "title": "T", "prompt": "Summarise.", "version": 1}
        built = handoff.build(self.s, mid, template)
        self.assertIn("edited by hand", json.dumps(built))
        # The title does go out: damaged, the handoff is refused.
        self.damage("meetings", "title_sealed")
        with self.assertRaises(DamagedContent):
            handoff.build(self.s, mid, template)

    def test_a_changed_fingerprint_is_no_match(self):
        self.assertFalse(self.s.same_fingerprint("original", H, "é" * 64))
        self.assertFalse(self.s.same_fingerprint("original", H, None))
        self.assertTrue(self.s.same_fingerprint("original", H,
                                                self.s.fingerprint("original", H)))

    def test_speaker_names_built_on_a_damaged_name_are_refused(self):
        mid, _, vid = meeting_with_text(self.s)
        rid = self.s.start_speaker_run(vid)
        self.s.finish_speaker_run(rid, [(0.0, 9.0, "SPEAKER_00")], [(0.0, 9.0, "SPEAKER_00")])
        speaker_names.rename(self.s, vid, "SPEAKER_00", "Synthetic Name")
        self.damage("speaker_corrections", "value_sealed")
        self.assertIn(store.DAMAGED_TEXT,
                      speaker_names.resolve(self.s, vid)["names"].values())   # shown as such
        for op in (lambda: speaker_names.rename(self.s, vid, "SPEAKER_00", "Other"),
                   lambda: speaker_names.undo(self.s, vid)):
            with self.assertRaises(DamagedContent):
                op()
        self.assertEqual(1, len(self.s.corrections(rid)))

    def test_cleanup_keeps_an_original_whose_location_cannot_be_read(self):
        folder = self.root / "Recordings"
        folder.mkdir()
        original = folder / "synthetic.wav"
        original.write_bytes(b"synthetic original")
        mid, sid, vid = meeting_with_text(self.s, original_path=str(original))
        with self.s.tx() as db:
            db.execute("UPDATE sources SET delete_original = 1")
        self.damage("sources", "original_path_sealed")
        jid = cleanup.confirm_transcript_reviewed(self.s, mid)
        self.assertEqual(cleanup.JobResult.DONE,
                         cleanup.run_job(self.s, jid, self.root / "media",
                                         original_roots=[folder]))
        self.assertTrue(original.exists())
        receipt = self.s._one("SELECT result FROM receipts")[0]
        self.assertIn("Original kept: its location could not be read.", receipt)

    def test_a_folder_whose_path_cannot_be_read_is_skipped_and_never_a_deletion_root(self):
        mid, sid, vid = meeting_with_text(self.s)
        folder = self.root / "Recordings"
        folder.mkdir()
        self.s.add_folder("f1", str(folder), False)
        intake.set_delete_originals(self.s, "f1", True)
        self.s.set_source_intake(sid, "f1", may_delete=True)
        self.damage("watched_folders", "path_sealed")
        self.assertEqual([], intake.deletion_roots(self.s, mid))
        stats = intake.scan(self.s, (".wav",), 1 << 30, lambda *a: None)
        self.assertEqual(1, stats["errors"])
        self.assertEqual(intake.FOLDER_UNREADABLE_NOTE, self.s.folders()[0]["last_error"])

    def test_the_runner_stops_on_a_key_that_does_not_open_without_counting_a_failure(self):
        mid, _, vid = meeting_with_text(self.s)
        with self.s.tx() as db:
            db.execute("UPDATE chunks SET status = 'pending'")
            db.execute("DELETE FROM segments")
        self.damage("content_keys", "wrapped", "purpose = 'text'")
        self.s.close()
        self.s = open_store(self.root / "workspace.db")       # the key is read at open

        class Reader:
            def __len__(self):
                return 60 * SR

            def read(self, start, end):
                import numpy as np
                return np.zeros(end - start, dtype=np.float32)

        class Engine:
            device, precision = "cpu", "int8"

            def transcribe(self, audio):
                from workspace.asr_engines import Segment
                return [Segment(1.0, 2.0, "synthetic words", ())], len(audio) / SR

        for _ in range(runner.MAX_ATTEMPTS + 1):
            self.assertEqual(runner.RunResult.STORE_ERROR,
                             runner.run_version(self.s, mid, vid, Reader(), Engine()))
            with self.s.tx() as db:                   # as "Try again" would
                db.execute("UPDATE meetings SET phase = 'new'")
        self.assertEqual(0, self.s._one("SELECT COUNT(*) FROM segments")[0])   # rolled back
        self.assertEqual({"stopped"}, {r[0] for r in self.s._all("SELECT result FROM attempts")})
        self.assertEqual(0, self.s.chunks(vid)[0]["failures"])
        # The note cannot be sealed under that key: the phase is still saved,
        # with no note, and nothing is stored plain or under another key.
        self.assertEqual(runner.RunResult.STORE_ERROR,
                         runner.run_version(self.s, mid, vid, Reader(), Engine()))
        self.assertEqual(("error", None), tuple(self.s._one(
            "SELECT phase, last_error_sealed FROM meetings WHERE id = ?", (mid,))))
        # A meeting in error with no note, whose key will not open, shows the
        # fixed wording, worked out when it is shown.
        other, _, _ = meeting_with_text(self.s, title="synthetic fine")
        self.s.set_phase(other, "error")                   # no note, key opens
        self.s.close()
        from workspace.app import Workspace
        ws = Workspace(self.root, self.root / "models", start=False, allow_test_engines=True,
                       battery=False, policy_reader=lambda: ({}, ()),
                       protector=keyprotect.TestProtector())
        try:
            shown = {m["id"]: m["error"] for m in ws.meetings()}
            self.assertEqual({mid: runner.DAMAGED_NOTE, other: None}, shown)
            self.assertEqual(runner.DAMAGED_NOTE, ws.detail(mid)["error"])
            self.assertIsNone(ws.detail(other)["error"])
        finally:
            ws.close()
        self.s = open_store(self.root / "workspace.db")
        self.assertIsNone(self.s._one("SELECT last_error_sealed FROM meetings WHERE id = ?",
                                      (mid,))[0])

    def test_a_lost_intake_record_starts_again_without_importing_anything(self):
        folder = self.root / "Recordings"
        folder.mkdir()
        for name in ("one.wav", "two.wav"):
            (folder / name).write_bytes(b"RIFF synthetic " + name.encode())
        self.s.add_folder("f1", str(folder), False)
        queued = []
        enqueue = lambda *a: queued.append(a)              # noqa: E731
        intake.scan(self.s, (".wav",), 1 << 30, enqueue)   # baseline
        self.assertEqual(2, self.s._one("SELECT COUNT(*) FROM intake_seen")[0])
        for how in ("damaged", "missing"):
            with self.subTest(how=how):
                if how == "damaged":
                    self.damage("intake_meta", "v_sealed")
                else:
                    self.s.db.execute("DELETE FROM intake_meta")
                stats = intake.scan(self.s, (".wav",), 1 << 30, enqueue)
                self.assertEqual(store.SALT_RESTARTED_NOTE, stats["notice"])
                self.assertEqual(0, stats["queued"])
                self.assertEqual(store.SALT_RESTARTED_NOTE, self.s.folders()[0]["last_error"])
                self.assertEqual({"baseline"}, {r[0] for r in self.s._all(
                    "SELECT state FROM intake_seen")})
                self.assertEqual([], queued)
                for _ in range(2):              # later scans keep the folder's note
                    later = intake.scan(self.s, (".wav",), 1 << 30, enqueue)
                    self.assertNotIn("notice", later)
                    self.assertEqual(store.SALT_RESTARTED_NOTE,
                                     self.s.folders()[0]["last_error"])
                # Another note comes first, and the restart note stays after it.
                folder.rename(self.root / "away")             # offline for a scan
                intake.scan(self.s, (".wav",), 1 << 30, enqueue)
                note = self.s.folders()[0]["last_error"]
                self.assertTrue(note.startswith("The folder is not available"), note)
                self.assertTrue(note.endswith(store.SALT_RESTARTED_NOTE), note)
                (self.root / "away").rename(folder)
                intake.scan(self.s, (".wav",), 1 << 30, enqueue)
                self.assertEqual(store.SALT_RESTARTED_NOTE, self.s.folders()[0]["last_error"])
                # A failed import keeps it; the folder's next successful one ends it.
                key = self.s._one("SELECT key FROM intake_seen LIMIT 1")[0]
                intake.import_finished(self.s, "f1", key, "c" * 64, ok=False)
                self.assertEqual(store.SALT_RESTARTED_NOTE, self.s.folders()[0]["last_error"])
                fp = self.s.fingerprint("original", "c" * 64)
                intake.import_finished(self.s, "f1", key, fp, ok=True)
                self.assertIsNone(self.s.folders()[0]["last_error"])
                intake.scan(self.s, (".wav",), 1 << 30, enqueue)
                self.assertIsNone(self.s.folders()[0]["last_error"])


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class ErasePointTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = open_store(self.root / "workspace.db")

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def leftovers(self, mid):
        q = self.s._one
        return (q("SELECT COUNT(*) FROM content_keys WHERE meeting_id = ?", (mid,))[0],
                q("SELECT COUNT(*) FROM recording_sources WHERE meeting_id = ?", (mid,))[0])

    def test_cleanup_destroys_the_keys_at_each_checkpoint(self):
        mid, _, vid = meeting_with_text(self.s, recording=("rec-1", '{"device": "x"}'))
        title = self.s._one("SELECT title_sealed FROM meetings WHERE id = ?", (mid,))[0]
        jid = cleanup.confirm_transcript_reviewed(self.s, mid)
        self.assertEqual(cleanup.JobResult.DONE, cleanup.run_job(self.s, jid, self.root / "m"))
        self.assertEqual([("text",)], [tuple(r) for r in self.s._all(
            "SELECT purpose FROM content_keys WHERE meeting_id = ?", (mid,))])
        self.assertEqual((1, 0), self.leftovers(mid))          # audio key and recording gone
        self.assertEqual("synthetic standup", self.s.meeting(mid)["title"])   # text key kept
        with self.assertRaises(KeyMissing):
            self.s.meeting_key(mid, "audio")
        jid = cleanup.confirm_final_saved(self.s, mid)
        self.assertEqual(cleanup.JobResult.DONE, cleanup.run_job(self.s, jid, self.root / "m"))
        self.assertEqual((0, 0), self.leftovers(mid))
        # A copy of a value sealed before cleanup can no longer be opened.
        with self.s._lock, self.assertRaises(KeyMissing):
            self.s._unseal(mid, "meetings", "title", (mid,), title)

    def test_every_deletion_of_a_meeting_removes_its_keys_and_recording(self):
        # The foreign keys are on: a path that missed a table would fail.
        self.assertEqual(1, self.s._one("PRAGMA foreign_keys")[0])
        rec = lambda n: (f"rec-{n}", '{"device": "x"}')      # noqa: E731
        # Transcript cleanup and discard.
        mid, _, _ = meeting_with_text(self.s, recording=rec(1))
        for jid in cleanup.discard(self.s, mid):
            cleanup.run_job(self.s, jid, self.root / "m")
        self.assertEqual((0, 0), self.leftovers(mid))
        # An unfinished import (no version yet).
        mid = self.s.create_meeting("synthetic unfinished")
        self.s.add_source(mid, original_path=None, original_sha256=H, original_size=1,
                          original_mtime_ns=0, audio_id=H, samples=SR, sample_rate=SR,
                          recording=rec(2))
        self.assertTrue(self.s.discard_unfinished_import(mid, lambda: None))
        self.assertEqual((0, 0), self.leftovers(mid))
        # A meeting that never got a source (its recording row put in place raw).
        mid = self.s.create_meeting("synthetic new")
        with self.s.tx() as db:
            db.execute("INSERT INTO recording_sources (meeting_id, recording_id, info_sealed) "
                       "VALUES (?, 'rec-3', ?)",
                       (mid, self.s._seal(mid, "recording_sources", "info", (mid,), "{}")))
        self.s.delete_new_meeting(mid)
        self.assertEqual((0, 0), self.leftovers(mid))
        self.assertEqual(0, self.s._one("SELECT COUNT(*) FROM meetings")[0])

    def test_recording_keys_are_made_and_destroyed(self):
        key = self.s.create_recording_key("rec-1")
        self.assertEqual(key, self.s.recording_key("rec-1"))
        self.assertTrue(self.s.destroy_recording_key("rec-1"))
        self.assertEqual(0, self.s._one("SELECT COUNT(*) FROM recording_keys")[0])
        with self.assertRaises(KeyMissing):
            self.s.recording_key("rec-1")

    def test_the_erase_checkpoint_waits_for_a_reader(self):
        self.s.create_recording_key("rec-1")
        reader = sqlite3.connect(self.root / "workspace.db", check_same_thread=False)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM receipts").fetchone()  # holds a read snapshot
            with self.s.tx() as db:
                db.execute("DELETE FROM recording_keys")
            with mock.patch.object(Store, "ERASE_RETRIES_S", (0.01, 0.01)):
                self.assertFalse(self.s.erase_checkpoint())             # the reader stays
            timer = threading.Timer(0.05, reader.rollback)
            timer.start()
            with mock.patch.object(Store, "ERASE_RETRIES_S", (0.2, 0.2, 0.2)):
                self.assertTrue(self.s.erase_checkpoint())              # once it has gone
            timer.join()
        finally:
            reader.close()


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class FolderIdentityTests(unittest.TestCase):

    def setUp(self):
        profile_elsewhere.start(self)       # folders in %TEMP%, inside AppData (#111 D2)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = open_store(self.root / "workspace.db")

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_case_and_a_trailing_separator_are_the_same_folder(self):
        self.assertTrue(self.s.add_folder("f1", r"C:\Rec", False))
        for other in (r"c:\rec", "c:\\rec\\", r"C:\REC"):
            with self.subTest(other=other):
                self.assertFalse(self.s.add_folder(store.new_id(), other, False))
        self.assertTrue(self.s.add_folder("f2", r"C:\Rec2", False))

    @unittest.skipUnless(sys.platform == "win32", "junctions and short names are Windows's")
    def test_a_junction_or_short_name_is_the_same_folder(self):
        import _winapi
        import ctypes
        folder = self.root / "Synthetic recordings folder"
        folder.mkdir()
        junction = self.root / "jn"
        _winapi.CreateJunction(str(folder), str(junction))
        try:
            self.assertEqual(intake.folder_identity(folder), intake.folder_identity(junction))
            intake.add_folder(self.s, str(folder))
            with self.assertRaises(intake.IntakeError):
                intake.add_folder(self.s, str(junction))
            buf = ctypes.create_unicode_buffer(512)
            ctypes.windll.kernel32.GetShortPathNameW(str(folder), buf, 512)
            if buf.value and buf.value != str(folder):        # short names may be off
                self.assertEqual(intake.folder_identity(folder), intake.folder_identity(buf.value))
                with self.assertRaises(intake.IntakeError):
                    intake.add_folder(self.s, buf.value)
        finally:
            os.rmdir(junction)


# Known answers: the SHA-256 of each content column's context bytes
# (vault.encode("cell", keystore.context(...))) for the rows in
# test_the_stored_formats_are_pinned, and fingerprints from the root key
# bytes(range(32)). Changing any of them changes the stored format.
KNOWN_CONTEXTS = {
    ("meetings", "title_sealed"):
        "ff93342f6d8127d3280a2342348a2ecc2ac018662bb8b458b997770840180d91",
    ("meetings", "last_error_sealed"):
        "a633cb9c60949a01934b072094818fee2b17a863dc93644d2f295bc838347fc6",
    ("reviewed_snapshots", "text_sealed"):
        "c8c85a4153a00d064b9739fe089e18069794d51c31a861ad16ad62370200112b",
    ("sources", "original_path_sealed"):
        "05c62c0fad289b1f0a927c7528f7184b4b6de25c7c07338cf6185099c3946b9e",
    ("segments", "text_sealed"):
        "1864cdc877ec6162ebd57731aa04a40ea1378d1db339c10ee0a99945a694daef",
    ("segments", "words_sealed"):
        "c61113bce50d6a6cd3a1203e01a1ad3f2986265bd147751421c9686d09271581",
    ("edits", "text_sealed"):
        "6813dc5dba0e010a1bd7c339d99414eb47b578e560dced06494a687a7c9647a1",
    ("edit_history", "before_sealed"):
        "c38409a7ceeaac75b31d8a24a354441cdfd8707f0f55537d0e1f1a69a7617108",
    ("edit_history", "after_sealed"):
        "d07138ebe77f2bb69cbc147173ca6501fcbb0eadbed15afb89f71080ddd63ae3",
    ("speaker_runs", "error_sealed"):
        "4a4d670addcd034598103fb49c9bef1fd4dbe9fa518f2f1e213202cd08eb7be0",
    ("speaker_corrections", "value_sealed"):
        "9e5c20bf36e6f8ad4f5d0f3be49c6d9e89bc7eb6c8d8767014256b2b8e7eefaa",
    ("handoff_options", "omitted_sealed"):
        "4f97bfb053b7164ed95903af92c2d80440ede7e5b66ad83c72634b2acbe7f275",
    ("handoff_options", "redactions_sealed"):
        "b2c106d072ccdf985b4efec0d3e05d15da702d3b53d90aeadba4589ae005be0e",
    ("holds", "reason_sealed"):
        "352c814f97bfbe424e29aae03594421420381bee3315f06ea638667059b54485",
    ("recording_sources", "info_sealed"):
        "f6e51c507da78cdd9786fb3556806b787904875c10abcc6d356ba402510ad4e5",
    ("watched_folders", "path_sealed"):
        "49a4921d5565d8c9ea0a2df7893778d757d3d2b9c27320acf21e0a1ca44119b4",
    ("intake_meta", "v_sealed"):
        "07dd776e6c816aed76d4f9e6fc136447d38e575d19c57ca32c6cb59177179abc",
}
KNOWN_FINGERPRINTS = {
    "original": "d5f29e40ff048299424a600ecc05ae2a799c1c3bd9dae8a35bfce92fc3ea176a",
    "snapshot": "a622e005eea2318291a98e06fa02ea238954becbc82cb050dd5c01d1c0c12c29",
    "folder": "2d81b6e0bee10b243abe0332fc21cdaa6c2786a2ad1440e882aeb0561e97d027",
}


if __name__ == "__main__":
    unittest.main()
