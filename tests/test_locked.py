# Locked mode (#111 C4a; docs/ENCRYPTION.md "Locked"): the app's own start
# when Windows will not unlock the workspace key.
#   - Never plain text, never a write: a locked start and close leave the
#     database (schema, every row, no write-ahead log), settings, templates,
#     media and recordings as they were; logs are only added to.
#   - Each reason (windows with its error number, key, protector, missing,
#     and rows with no schema version) opens locked; no new root key.
#   - Work is held: no job, import, scan, purge, upkeep, recovered recording
#     or interrupted-work marking; recording and imports are refused and
#     Continue anyway does nothing.
#   - The database check (R11) with and without a lock: status() fields.
#   - No lock without content: a new or empty workspace whose key cannot be
#     protected raises and leaves nothing (or schema 1) behind.
#   - keys.json, --status and --health report the state without unlocking.
#   - A start that unlocks runs the work that waited.
# The bridge gate is in tests/test_hardening.py (LockedBridgeTests).
# Synthetic text only, under temporary folders (the test protector refuses
# the real workspace).
#   python tools/run_tests.py

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import keyprotect  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av", "cryptography"))
MARKER = "Zqlockedmarker"
H = "b" * 64
SR = 16000
# 0x8009000B (NTE_BAD_KEY_STATE) as the signed number Windows reports.
BAD_KEY_STATE = -2146893813

if HAVE_DEPS:
    from workspace import app as app_module
    from workspace import cleanup, diag, holds, keystore, media, update
    from workspace.app import LOCKED_TEXT, Workspace, WorkspaceError
    from workspace.store import KeyUnavailable


def database_state(db):
    """Everything the database holds, read without writing: the schema and
    every table's rows (sorted), plus the write-ahead log's size."""
    con = sqlite3.connect(db)
    try:
        master = con.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                             ).fetchall()
        rows = {name: sorted(con.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
                for kind, name, _ in master if kind == "table"}
    finally:
        con.close()
    wal = Path(str(db) + "-wal")
    return master, rows, wal.stat().st_size if wal.exists() else 0


def files_under(folder):
    """{relative path: bytes} for every file under folder ({} if none)."""
    folder = Path(folder)
    if not folder.exists():
        return {}
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*")
            if p.is_file()}


def checkpointed(db):
    """Fold the write-ahead log into the file, so a locked start has none."""
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class LockedStartTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ws"
        self.db = self.root / "workspace.db"
        self.open_ws = []
        patcher = mock.patch("workspace.gpu.detected", return_value=None)   # no nvidia-smi
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for ws in self.open_ws:
            try:
                ws.close()
            except Exception:
                pass
        self.tmp.cleanup()

    def workspace(self, protector=None, **kw):
        kw.setdefault("start", False)
        ws = Workspace(self.root, Path(self.tmp.name) / "models", battery=False,
                       protector=protector or keyprotect.TestProtector(), **kw)
        self.open_ws.append(ws)
        return ws

    def close(self, ws):
        self.open_ws.remove(ws)
        ws.close()

    def locked(self, code=BAD_KEY_STATE, **kw):
        return self.workspace(protector=keyprotect.TestProtector(fail=True, code=code),
                              on_unavailable="lock", **kw)

    def fill(self):
        """A schema-2 workspace with content: a sealed meeting from a recording
        (recording_sources), a held meeting, a meeting confirmed for removal
        (cleanup jobs), a recording key, settings, a plain templates.json,
        media files and two recording folders left for the next start."""
        ws = self.workspace()
        s = ws.store
        ids = []
        for n in range(3):
            mid = s.create_meeting(f"{MARKER} meeting {n}")
            sid = s.add_source(mid, original_path=f"C:\\{MARKER}\\{n}.wav", original_sha256=H,
                               original_size=1, original_mtime_ns=0, audio_id=H,
                               samples=SR, sample_rate=SR,
                               recording=("a" * 32, json.dumps({"device": MARKER}))
                               if n == 0 else None)
            s.ensure_version(mid, sid, engine="e", model="m", model_revision="r",
                             precision="p", config={}, chunk_plan=[(0, SR, 0, SR)])
            ids.append(mid)
            folder = self.root / "media" / mid
            folder.mkdir(parents=True)
            (folder / media.AUDIO_FILE).write_bytes(MARKER.encode())   # present, never opened
        s.create_recording_key("c" * 32)
        holds.add_hold(s, ids[1], "all", f"{MARKER} reason")
        cleanup.discard(s, ids[2])
        ws.settings.update(mode="faster")
        self.close(ws)
        (self.root / "templates.json").write_text(json.dumps(
            {"custom-1": {"title": MARKER, "prompt": MARKER, "version": 1, "saved_at": 1.0}}),
            encoding="utf-8")
        live = self.root / "recordings" / ("d" * 32)          # the app closed while recording
        live.mkdir(parents=True)
        (live / "journal.json").write_text(json.dumps({"state": "recording", "rate": SR,
                                                       "frames": SR}), encoding="utf-8")
        gone = self.root / "recordings" / ("e" * 32)          # discarded: deleted at next start
        gone.mkdir()
        (gone / "journal.json").write_text(json.dumps({"state": "stopped"}), encoding="utf-8")
        (gone / "discarded").write_text("Discarded", encoding="utf-8")
        checkpointed(self.db)
        return ids

    def snapshot(self):
        return {"db": database_state(self.db),
                "settings": (self.root / "settings.json").read_bytes(),
                "templates": {p.name: p.read_bytes() for p in self.root.glob("templates.*")},
                "media": files_under(self.root / "media"),
                "recordings": files_under(self.root / "recordings")}

    def events(self, ws, kind):
        return [r for r in diag.shareable_records(ws.log.folder, limit=200) if r["kind"] == kind]

    # -- never plain text, never a write -------------------------------------------

    def test_a_locked_start_reads_nothing_and_writes_nothing(self):
        self.fill()
        before = self.snapshot()
        logs_before = files_under(self.root / "logs")
        ws = self.locked(start=True)
        ws._scan_wake.set()                      # a folder scan pass, at once
        ws._wake.set()                           # and a processing pass
        time.sleep(0.5)
        self.assertIsInstance(ws.store.codec, keystore.LockedCodec)
        self.assertIsNone(ws.store.keyring)
        self.assertEqual(0, ws.store.db.total_changes)
        self.assertEqual(0, ws._imports.unfinished_tasks)       # no recovered recording queued
        self.assertEqual([], ws._recording_reports)
        self.close(ws)
        after = self.snapshot()
        self.assertEqual(before, after)          # schema, every row, no WAL, files
        self.assertEqual(0, after["db"][2])
        # Logs are only added to: each earlier file is the start of its new self.
        logs_after = files_under(self.root / "logs")
        for name, data in logs_before.items():
            if name == diag.KEYS_FILE:
                continue
            self.assertTrue(logs_after[name].startswith(data), name)
        self.assertNotIn(MARKER, json.dumps({k: v.decode("latin-1")
                                             for k, v in logs_after.items()}))

    def test_nothing_is_opened_built_or_converted_while_locked(self):
        # Carried from #277: a real locked start (upkeep and all) builds no
        # templates and leaves both template files byte for byte.
        self.fill()
        ws = self.workspace()
        ws.save_template(None, "Ours", f"Ours {MARKER}")      # converts, then seals
        self.close(ws)
        self.assertFalse((self.root / "templates.json").exists())
        for plain in (False, True):
            with self.subTest(plain_file=plain):
                if plain:
                    (self.root / "templates.json").write_text(
                        json.dumps({"custom-1": {"title": MARKER, "prompt": MARKER,
                                                 "version": 1, "saved_at": 1.0}}),
                        encoding="utf-8")
                before = {p.name: p.read_bytes() for p in self.root.glob("templates.*")}
                with mock.patch("workspace.handoff.open_templates") as opened:
                    ws = self.locked(start=True)
                    for call in (lambda w=ws: w.handoff_templates(),
                                 lambda w=ws: w.templates,
                                 lambda w=ws: w.save_template(None, "x", "y")):
                        with self.assertRaises(KeyUnavailable):
                            call()
                    self.close(ws)
                opened.assert_not_called()
                self.assertEqual(before, {p.name: p.read_bytes()
                                          for p in self.root.glob("templates.*")})
                self.assertEqual([], self.events(ws, "templates_unreadable"))

    # -- each reason ---------------------------------------------------------------

    def crypto_meta(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT * FROM crypto_meta").fetchall()
        finally:
            con.close()

    def raw(self, sql):
        con = sqlite3.connect(self.db)
        try:
            con.execute(sql)
            con.commit()
        finally:
            con.close()
        checkpointed(self.db)

    def assert_locked(self, ws, reason, win_error=None):
        self.assertEqual(reason, ws.locked.reason)
        info = ws.status()["locked"]
        self.assertEqual((reason, win_error, None), (info["reason"], info["win_error"],
                                                     info["damage"]))
        self.assertRegex(info["code"], r"U-[0-9A-F]{6}\Z")
        kept = diag.keys_state(ws.log.folder)
        self.assertEqual(("locked", reason, win_error, info["code"]),
                         (kept["result"], kept["reason"], kept["winerror"], kept["code"]))
        [event] = [e for e in self.events(ws, "key_unavailable") if e["code"] == info["code"]]
        self.assertEqual((reason, win_error), (event["reason"], event.get("winerror")))

    def test_each_reason_opens_locked_and_never_makes_a_new_key(self):
        self.fill()
        key = self.crypto_meta()
        ws = self.locked()
        self.assert_locked(ws, "windows", BAD_KEY_STATE)
        self.close(ws)
        other = keyprotect.TestProtector()
        other.name = "other"
        ws = self.workspace(protector=other, on_unavailable="lock")
        self.assert_locked(ws, "protector")
        self.close(ws)
        self.assertEqual(key, self.crypto_meta())
        self.raw("UPDATE crypto_meta SET check_value = zeroblob(32)")
        ws = self.workspace(on_unavailable="lock")
        self.assert_locked(ws, "key")
        self.close(ws)
        changed = self.crypto_meta()
        self.raw("DELETE FROM crypto_meta")
        ws = self.workspace(on_unavailable="lock")
        self.assert_locked(ws, "missing")
        self.close(ws)
        self.assertEqual([], self.crypto_meta())             # never given a new key
        self.assertNotEqual(key, changed)

    def test_rows_without_a_schema_version_open_locked(self):
        # Carried from #276: rows and no version is content whose key is
        # unknown. It opens locked, query-only, with nothing rebuilt.
        self.fill()
        self.raw("DELETE FROM schema_version")
        before = database_state(self.db)
        ws = self.workspace(on_unavailable="lock")
        self.assert_locked(ws, "missing")
        self.assertEqual(0, ws.store.db.total_changes)
        self.assertEqual(1, ws.store.db.execute("PRAGMA query_only").fetchone()[0])
        self.close(ws)
        self.assertEqual(before, database_state(self.db))
        with self.assertRaises(KeyUnavailable):                  # the default still raises
            self.workspace()

    def test_an_older_locked_database_still_gives_copy_diagnostics(self):
        # Rows with no version from an older build whose tables differ from
        # this version's: the counts cannot be read, so Copy diagnostics says
        # "Workspace: ?" instead of failing, and nothing is built or rebuilt.
        self.fill()
        self.raw("DELETE FROM schema_version")
        self.raw("DROP TABLE cleanup_jobs")
        before = database_state(self.db)
        ws = self.workspace(on_unavailable="lock")
        self.assert_locked(ws, "missing")
        text = ws.diagnostics()
        self.close(ws)
        self.assertIn(f"Key: locked (missing, code {ws._lock_code})", text)
        self.assertIn("Workspace: ?", text)
        self.assertNotIn("Active holds:", text)
        self.assertNotIn(MARKER, text)
        self.assertEqual(before, database_state(self.db))      # no table made again

    def test_an_out_of_range_windows_error_is_shown_nowhere(self):
        # The Windows error number is checked the same way for the page,
        # Copy diagnostics and keys.json: anything but a 32-bit value is left out.
        self.fill()
        ws = self.locked(code=2**40)
        info = ws.status()["locked"]
        text = ws.diagnostics()
        self.close(ws)
        self.assertEqual(("windows", None), (info["reason"], info["win_error"]))
        self.assertIn(f"Key: locked (windows, code {info['code']})", text)
        self.assertNotIn(str(2**40), text)
        self.assertIsNone(diag.keys_state(self.root / "logs")["winerror"])

    def test_a_keys_file_that_cannot_be_written_is_in_the_app_log(self):
        # #311: the app passes its log, so Copy diagnostics shows that
        # keys.json (which --status and --health read) is out of date.
        self.fill()
        real = Path.replace

        def replace(path, target):
            if path.name == diag.KEYS_FILE + ".tmp":
                raise PermissionError(13, "held")
            return real(path, target)

        with mock.patch.object(Path, "replace", replace), \
                mock.patch.object(diag, "STATE_RETRIES_S", ()):
            ws = self.locked()
        self.assertEqual("ok", diag.keys_state(ws.log.folder)["result"])    # the earlier start's
        self.assertEqual([("keys", "PermissionError")],
                         [(e["file"], e["error"]) for e in self.events(ws, "state_unwritten")])
        self.close(ws)

    # -- work is held ----------------------------------------------------------------

    def test_work_waits_and_requests_are_refused(self):
        ids = self.fill()
        spies = {name: mock.patch(target, side_effect=AssertionError(name))
                 for name, target in (
                     ("run_job", "workspace.cleanup.run_job"),
                     ("scan", "workspace.intake.scan"),
                     ("recover", "workspace.intake.recover"),
                     ("purge", "workspace.holds.purge_expired_receipts"),
                     ("compact", "workspace.cleanup.compact_deferred"),
                     ("reconcile", "workspace.app.Workspace._reconcile_imports"),
                     ("interrupted", "workspace.store.Store.mark_interrupted"),
                     ("import", "workspace.app.Workspace._import_recording_folder"))}
        started = {name: p.start() for name, p in spies.items()}
        try:
            ws = self.locked(start=True)
            ws._scan_wake.set()
            ws._wake.set()
            time.sleep(0.5)
            for call in (lambda: ws.start_recording(0, "Mic"),
                         lambda: ws.check_can_record(0, "Mic"),
                         lambda: ws.import_paths([str(self.root / "x.wav")]),
                         ws.scan_now):
                with self.assertRaises(WorkspaceError) as caught:
                    call()
                self.assertEqual(LOCKED_TEXT, str(caught.exception))
            ws.continue_despite_damage()
            self.assertFalse(ws._integrity_ok.is_set())
            status = ws.status()
            self.assertEqual(("Locked", None, 0), (status["message"], status["integrity"],
                                                   status["importing"]))
            self.assertEqual((False, None, []),
                             tuple(ws.recording_status()[k] for k in
                                   ("enabled", "recording", "reports")))
            self.close(ws)
        finally:
            for p in spies.values():
                p.stop()
        for name, spy in started.items():
            self.assertFalse(spy.called, name)
        self.assertEqual(3, len(ids))

    def test_no_audio_key_is_read_or_changed_while_locked(self):
        # C3a (#281) reads a meeting's audio key to transcribe (open_audio),
        # for a speaker pass (audio_ref, the key in the diarize request), for
        # playback and for Transcribe again. While locked none of them runs
        # (design 7.2): the processing thread is held, and a direct call is
        # refused by the store before any file or key is opened. No key row
        # is destroyed or made, and the update gate reads no key. C3b: the
        # sweep of recording keys whose folder is gone (fill() leaves one)
        # and the erase before a recording folder is deleted do not run.
        ids = self.fill()
        before = self.snapshot()
        targets = ("workspace.store.Store.meeting_key", "workspace.store.Store.destroy_keys",
                   "workspace.store.Store.remove_audio_keys",
                   "workspace.store.Store.recording_key",
                   "workspace.store.Store.create_recording_key",
                   "workspace.store.Store.destroy_recording_key",
                   "workspace.store.Store.recording_key_ids",        # C3b's sweep and erase
                   "workspace.media.open_audio", "workspace.media.audio_ref",
                   "workspace.media.AudioReader", "workspace.speakers.run_speakers",
                   "workspace.speakers.record_failure", "workspace.cleanup.run_job",
                   "workspace.intake.deletion_roots")
        patches = [mock.patch(t, side_effect=AssertionError(t)) for t in targets]
        spies = [p.start() for p in patches]
        # The receipt purge's refused write would also end each pass; without
        # it, only the hold keeps the thread from the audio.
        patches.append(mock.patch.object(Workspace, "_maybe_purge"))
        patches[-1].start()
        try:
            ws = self.locked(start=True)
            for _ in range(3):                        # processing passes, at once
                ws._wake.set()
                time.sleep(0.2)
        finally:
            for p in patches:
                p.stop()
        for target, spy in zip(targets, spies, strict=True):
            self.assertFalse(spy.called, target)
        self.assertTrue(ws._audio_available(ws._meeting(ids[0])))   # due, with its audio
        # Called directly (the bridge gate refuses first): the store refuses
        # before the audio file is opened.
        with mock.patch("workspace.media.AudioReader",
                        side_effect=AssertionError("opened")) as reader, \
                mock.patch.object(Workspace, "_speech_states",
                                  side_effect=lambda: {ws.settings["model"]: "ready"}):
            for call in (lambda: ws.clip(ids[0], 0, 1), lambda: ws.retranscribe(ids[0])):
                with self.assertRaises(KeyUnavailable):
                    call()
        reader.assert_not_called()
        # C3b: the erase before a recording folder goes and the sweep of keys
        # whose folder is gone read no key row while locked.
        with mock.patch("workspace.store.Store.recording_key_ids",
                        side_effect=AssertionError("read")) as ids:
            self.assertFalse(ws._forget_recording_key("c" * 32))
            ws._sweep_recording_keys()
        ids.assert_not_called()
        self.close(ws)
        # The update gate on the locked workspace: plain columns only.
        with mock.patch("workspace.dpapi.unprotect", side_effect=AssertionError("dpapi")), \
                mock.patch("workspace.store.Store", side_effect=AssertionError("store")):
            kinds = {b["kind"] for b in update.blockers(self.db)}
        self.assertIn("media", kinds)
        self.assertEqual(before, self.snapshot())

    def test_the_cleanup_count_leaves_out_cleanup_owed_a_new_confirmation(self):
        # Round 2 review nit: a job that a released hold moved to
        # awaiting_confirmation needs the user to confirm again, so its
        # meeting is not "confirmed and not finished". A confirmed job that
        # a hold still blocks (pending) is.
        ids = self.fill()
        ws = self.workspace()
        s = ws.store
        mid = s.create_meeting(f"{MARKER} meeting 3")
        sid = s.add_source(mid, original_path=f"C:\\{MARKER}\\3.wav", original_sha256=H,
                           original_size=1, original_mtime_ns=0, audio_id=H,
                           samples=SR, sample_rate=SR)
        s.ensure_version(mid, sid, engine="e", model="m", model_revision="r",
                         precision="p", config={}, chunk_plan=[(0, SR, 0, SR)])
        cleanup.discard(s, mid)
        hid = holds.add_hold(s, mid, "all", f"{MARKER} reason")
        self.assertTrue(holds.release_hold(s, hid))
        self.assertEqual({mid}, {j["meeting_id"] for j in holds.awaiting_confirmation(s)})
        self.close(ws)
        ws = self.locked()
        text = ws.diagnostics()
        self.close(ws)
        self.assertIn("Cleanup confirmed and not finished: 1", text)     # ids[2] only
        self.assertNotIn(MARKER, text)
        self.assertEqual(3, len(ids))

    def test_a_start_that_unlocks_runs_the_work_that_waited(self):
        self.fill()
        ws = self.locked()
        self.close(ws)
        ran = []
        with mock.patch.object(Workspace, "_run",
                               side_effect=lambda kind, mid, ref: ran.append(kind)), \
                mock.patch.object(Workspace, "_next_work",
                                  side_effect=[("cleanup", "m", "j"), None, None, None]):
            ws = self.workspace(start=True)
            for _ in range(100):
                if ran:
                    break
                time.sleep(0.05)
            self.close(ws)
        self.assertEqual(["cleanup"], ran)
        self.assertEqual("ok", diag.keys_state(self.root / "logs")["result"])
        self.assertIsNone(ws.status()["locked"])

    # -- the database check (R11) with and without a lock ------------------------------

    def test_status_for_each_database_check_and_key(self):
        self.fill()
        for damaged in (False, True):
            for locked in (False, True):
                with self.subTest(damaged=damaged, locked=locked):
                    diag.record_integrity(self.root / "logs", "failed" if damaged else "ok",
                                          "U-ABCDEF" if damaged else None)
                    with mock.patch("workspace.app.quick_check_file", return_value=not damaged):
                        ws = (self.locked if locked else self.workspace)(check_integrity=True)
                    status = ws.status()
                    if locked:
                        self.assertIsNone(status["integrity"])
                        self.assertEqual(ws._integrity["code"] if damaged else None,
                                         status["locked"]["damage"])
                        ws.continue_despite_damage()           # never offered; no effect
                        self.assertFalse(ws._integrity_ok.is_set())
                    else:
                        self.assertIsNone(status["locked"])
                        self.assertEqual(damaged, status["integrity"] is not None)
                    self.close(ws)

    # -- no lock without content -------------------------------------------------------

    def test_a_new_workspace_that_cannot_be_protected_raises_and_leaves_nothing(self):
        from workspace import __main__ as main
        with self.assertRaises(KeyUnavailable) as caught:
            self.locked(code=5)
        self.assertEqual(("windows", 5), (caught.exception.reason, caught.exception.code))
        self.assertFalse(self.db.exists())
        self.assertEqual([], [p.name for p in self.root.iterdir()
                              if p.name.startswith("workspace")])
        with mock.patch("workspace.store.default_root", return_value=self.root):
            text = main._open_failed(caught.exception)
        self.assertRegex(text, r"could not set up its encryption key with Windows "
                               r"\(error 5, code U-[0-9A-F]{6}\)")

    def test_an_empty_schema_1_workspace_that_cannot_be_protected_stays_schema_1(self):
        import schema_v1
        self.root.mkdir(parents=True)
        schema_v1.create(self.db).close()
        before = database_state(self.db)
        with self.assertRaises(KeyUnavailable):
            self.locked()
        self.assertEqual(before, database_state(self.db))
        self.assertEqual(1, update.current_schema(self.db))

    # -- what IT sees --------------------------------------------------------------------

    def test_status_health_and_copy_details_report_without_unlocking(self):
        from workspace import __main__ as main
        self.fill()
        ws = self.locked()
        code = ws.status()["locked"]["code"]
        text = ws.diagnostics()
        self.close(ws)
        self.assertIn(f"Key: locked (windows, Windows error {BAD_KEY_STATE} = 0x8009000B, "
                      f"code {code})", text)
        self.assertIn("Active holds: 1", text)
        # One meeting discarded: two jobs (audio, transcript), one meeting.
        self.assertIn("Cleanup confirmed and not finished: 1", text)
        self.assertNotIn(MARKER, text)
        self.assertNotIn(str(self.root), text)
        # Neither command touches Windows Data Protection, a key or the store.
        with mock.patch("workspace.dpapi.protect", side_effect=AssertionError("dpapi")), \
                mock.patch("workspace.dpapi.unprotect", side_effect=AssertionError("dpapi")), \
                mock.patch("workspace.store.Store", side_effect=AssertionError("store")), \
                mock.patch.object(main, "_app_running", return_value=False):
            report = main.status(self.root)
            self.assertEqual(("locked", "windows", BAD_KEY_STATE, 3),
                             (report["keys"], report["lock_reason"], report["lock_code"],
                              report["meetings"]))
            health = main.health(self.root)
            self.assertEqual(("locked", False), (health["workspace"], health["healthy"]))
            # Ranked before damaged, after unreadable.
            diag.record_integrity(self.root / "logs", "failed", "U-ABCDEF")
            self.assertEqual("locked", main.health(self.root)["workspace"])
            with mock.patch.object(main, "status", return_value={**report, "readable": False}):
                self.assertEqual("unreadable", main.health(self.root)["workspace"])
        diag.record_integrity(self.root / "logs", "ok")
        ws = self.workspace()                                   # unlocks again
        self.assertIn("Key: ok", ws.diagnostics())
        self.close(ws)
        report = main.status(self.root)
        self.assertEqual(("ok", None, None), (report["keys"], report["lock_reason"],
                                              report["lock_code"]))
        with mock.patch.object(main, "_app_running", return_value=False):
            self.assertEqual("ok", main.health(self.root)["workspace"])


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class KeysFileTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "logs"

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_and_only_what_the_app_writes(self):
        self.assertIsNone(diag.keys_state(self.folder))
        diag.record_keys(self.folder, "locked", "windows", BAD_KEY_STATE, "U-ABC123")
        state = diag.keys_state(self.folder)
        self.assertEqual(("locked", "windows", BAD_KEY_STATE, "U-ABC123"),
                         (state["result"], state["reason"], state["winerror"], state["code"]))
        self.assertRegex(state["t"], r"\d{4}-\d\d-\d\dT")
        diag.record_keys(self.folder, "ok")
        self.assertEqual(("ok", None, None, None),
                         tuple(diag.keys_state(self.folder)[k]
                               for k in ("result", "reason", "winerror", "code")))
        stamp = (self.folder / diag.KEYS_FILE).stat().st_mtime_ns
        os.utime(self.folder / diag.KEYS_FILE, ns=(10**18, 10**18))
        diag.record_keys(self.folder, "ok")                    # unchanged: not written again
        self.assertEqual(10**18, (self.folder / diag.KEYS_FILE).stat().st_mtime_ns)
        self.assertNotEqual(10**18, stamp)
        (self.folder / diag.KEYS_FILE).write_text(json.dumps(
            {"result": "locked", "reason": MARKER, "winerror": "Zq", "code": MARKER,
             "t": MARKER}), encoding="ascii")
        self.assertEqual({"result": "locked", "reason": None, "winerror": None, "code": None,
                          "t": None}, diag.keys_state(self.folder))
        for text in ("{", "[]", json.dumps({"result": "maybe"}), "[" * 100000):
            (self.folder / diag.KEYS_FILE).write_text(text, encoding="ascii")
            self.assertIsNone(diag.keys_state(self.folder))
        diag.record_keys(self.folder, "locked", MARKER, 2**40, MARKER)   # refused values
        self.assertEqual((None, None, None), tuple(diag.keys_state(self.folder)[k]
                                                   for k in ("reason", "winerror", "code")))
        with mock.patch("os.fsync", side_effect=OSError("disk")):
            diag.record_keys(self.folder, "locked", "key")       # never raises
        diag.record_keys(Path(self.tmp.name) / "gone" / "logs", "ok")   # nor here

    def test_a_file_held_open_for_a_moment_is_still_written(self):
        # #311: another process (an antivirus or indexer scan, --status)
        # holding keys.json open without sharing deletion makes Windows refuse
        # the rename. Held here for 0.15 s by an ordinary open: the write is
        # tried again and lands, so the next reader sees this start's state.
        diag.record_keys(self.folder, "ok")
        held = open(self.folder / diag.KEYS_FILE, "rb")
        timer = threading.Timer(0.15, held.close)
        timer.start()
        try:
            diag.record_keys(self.folder, "locked", "protector", None, "U-ABC123")
        finally:
            timer.cancel()
            held.close()
        state = diag.keys_state(self.folder)
        self.assertEqual(("locked", "protector", "U-ABC123"),
                         (state["result"], state["reason"], state["code"]))
        self.assertEqual([diag.KEYS_FILE], [p.name for p in self.folder.iterdir()])

    def test_a_write_that_still_fails_is_logged_without_content(self):
        # Held for longer than the retries: the earlier file stays as it was,
        # no temporary file is left, and the log says which file and the
        # exception type only (P3), never the message or a path.
        diag.record_integrity(self.folder, "ok")
        before = (self.folder / diag.INTEGRITY_FILE).read_bytes()
        log = diag.Log(self.folder, "app")
        real = Path.replace
        refused = PermissionError(13, f"{MARKER} held", str(self.folder))

        def replace(path, target):
            if path.name.endswith(".tmp"):
                raise refused
            return real(path, target)

        with mock.patch.object(Path, "replace", replace), \
                mock.patch("workspace.diag.time.sleep") as slept:
            self.assertFalse(diag._write_state(self.folder, diag.INTEGRITY_FILE,
                                               {"result": "failed", "code": "U-ABCDEF"}, log))
            diag.record_keys(self.folder, "locked", "key", log=log)
        self.assertEqual(list(diag.STATE_RETRIES_S) * 2,
                         [c.args[0] for c in slept.call_args_list])
        self.assertLess(sum(diag.STATE_RETRIES_S), 1.0)          # bounded: under a second
        self.assertEqual(before, (self.folder / diag.INTEGRITY_FILE).read_bytes())
        self.assertIsNone(diag.keys_state(self.folder))
        self.assertEqual([], list(self.folder.glob("*.tmp")))
        records = [r for r in diag.shareable_records(self.folder)
                   if r["kind"] == "state_unwritten"]
        self.assertEqual([("integrity", "PermissionError"), ("keys", "PermissionError")],
                         [(r["file"], r["error"]) for r in records])
        text = (self.folder / "app.log").read_text(encoding="ascii")
        self.assertNotIn(MARKER, text)
        self.assertNotIn(self.tmp.name, text)
        # Anything but a file held open (a full disk) is not tried again.
        with mock.patch("os.fsync", side_effect=OSError(28, "full")), \
                mock.patch("workspace.diag.time.sleep") as slept:
            diag.record_keys(self.folder, "ok", log=log)
        self.assertEqual([], slept.call_args_list)
        self.assertEqual("keys", diag.shareable_records(self.folder)[-1]["file"])
        self.assertEqual([], list(self.folder.glob("*.tmp")))


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class EntryPointTests(unittest.TestCase):

    def test_the_app_opens_in_lock_mode_with_its_own_protector(self):
        from workspace import __main__ as main
        seen = {}

        def built(*a, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")
        with mock.patch.object(app_module, "Workspace", side_effect=built), \
                mock.patch.object(main, "_single_instance", return_value=True), \
                mock.patch.object(main, "model_cache", return_value=Path("models")), \
                mock.patch.object(sys, "argv", ["workspace"]), \
                self.assertRaisesRegex(RuntimeError, "stop here"):
            main.main()
        self.assertEqual("lock", seen.get("on_unavailable"))
        self.assertNotIn("protector", seen)


if __name__ == "__main__":
    unittest.main()
