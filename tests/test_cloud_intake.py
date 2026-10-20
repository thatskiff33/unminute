# Cloud placeholders and intake-source deletion authority (#24).
# Synthetic files; cloud states are simulated (a real placeholder needs a
# sync client), permission and offline errors are injected.
#   python -m unittest discover -s tests -v

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
EXT = (".wav",)

if HAVE_DEPS:
    from test_intake import wav

    from workspace import intake
    from workspace.safe_paths import CloudPlaceholderError
    from workspace.store import Store


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "OneDrive - Org" / "Recordings"
        self.folder.mkdir(parents=True)
        self.store = Store(self.root / "ws" / "workspace.db", protector=keyprotect.TestProtector())
        self.fid = intake.add_folder(self.store, str(self.folder))
        self.queued, self.now = [], time.time()
        self.saved_recent = intake.STABLE_RECENT_S
        intake.STABLE_RECENT_S = 0
        self.scan()

    def tearDown(self):
        intake.STABLE_RECENT_S = self.saved_recent
        self.store.close()
        self.tmp.cleanup()

    def enqueue(self, path, fid, key, content):
        self.queued.append(Path(path))
        intake.import_finished(self.store, fid, key, content, True)

    def scan(self, hasher=None):
        self.now += 10
        return intake.scan(self.store, EXT, 1 << 30, self.enqueue, now=self.now,
                           hasher=hasher)

    def test_cloud_only_file_waits_without_inventing_progress(self):
        f = wav(self.folder / "cloud.wav")
        real = intake.validate_input_file
        calls = []

        def placeholder(path, ext, size, allow_cloud=False):
            calls.append(allow_cloud)
            if not allow_cloud:
                raise CloudPlaceholderError("The file is in cloud storage and not "
                                            "downloaded to this computer yet.")
            return real(path, ext, size)
        intake.validate_input_file = placeholder
        try:
            self.scan()
            stats = self.scan()
            self.assertEqual((stats["cloud"], self.queued), (1, []))
            [s] = intake.summary(self.store)
            self.assertEqual(s["cloud"], 1)
            self.assertNotIn("%", str(s))                       # no invented percentage
            intake.set_fetch_cloud(self.store, self.fid, True)  # user allows fetching
            self.scan()
            self.assertEqual([p.name for p in self.queued], [f.name])
            self.assertEqual(calls[-1], True)
        finally:
            intake.validate_input_file = real

    def test_paused_sync_or_offline_is_retried(self):
        wav(self.folder / "a.wav")
        self.scan()

        def offline(path):
            raise OSError("The cloud operation was not completed before the time-out")
        stats = self.scan(hasher=offline)
        self.assertEqual((stats["cloud"], self.queued), (1, []))
        self.scan()                                          # sync resumed
        self.assertEqual(len(self.queued), 1)

    def test_unreadable_file_is_reported_not_imported(self):
        wav(self.folder / "locked.wav")
        self.scan()

        def denied(path):
            raise PermissionError(13, "Access is denied")
        self.scan(hasher=denied)
        self.assertEqual(intake.summary(self.store)[0]["unreadable"], 1)
        self.assertEqual(self.queued, [])

    def test_deletion_authority_is_explicit_and_revocable(self):
        self.assertFalse(intake.authority_for(self.store, self.fid))  # watching is not authority
        intake.set_delete_originals(self.store, self.fid, True)
        self.assertTrue(intake.authority_for(self.store, self.fid))
        intake.set_delete_originals(self.store, self.fid, False)
        self.assertFalse(intake.authority_for(self.store, self.fid))


@unittest.skipUnless(HAVE_DEPS and os.name == "nt", "Windows paths")
class AuthorityFlowTests(unittest.TestCase):
    def run_flow(self, authority, revoke_before_cleanup=False, replace=False):
        from workspace import app
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        folder = root / "Recordings"
        folder.mkdir()
        ws = app.Workspace(root / "ws", root / "models", start=False,
                           allow_test_engines=True, battery=False,
                           protector=keyprotect.TestProtector())
        try:
            ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
            fid = ws.add_watched_folder(str(folder), False)
            if authority:
                ws.set_folder_authority(fid, delete_originals=True, confirmed=True)
            original = wav(folder / "Synthetic.wav", seconds=6, seed=21)
            ws._import_one(original, fid)
            mid = ws.meetings()[0]["id"]
            ws.start()
            self.assertTrue(ws.wait_idle(60))
            if revoke_before_cleanup:
                ws.set_folder_authority(fid, delete_originals=False)
            if replace:
                wav(original, seconds=7, seed=22)            # a different file, same name
            ws.confirm_reviewed(mid)
            self.assertTrue(ws.wait_idle(60))
            return original.exists(), ws.receipts()[-1]["result"]
        finally:
            ws.close()
            tmp.cleanup()

    def test_original_deleted_only_with_folder_authority(self):
        self.assertEqual(self.run_flow(authority=False)[0], True)
        kept, _ = self.run_flow(authority=True)
        self.assertFalse(kept)

    def test_revoked_authority_keeps_the_original(self):
        kept, result = self.run_flow(authority=True, revoke_before_cleanup=True)
        self.assertTrue(kept)
        self.assertIn("not in a folder", result)

    def test_authority_belongs_to_the_folder_the_file_came_from(self):
        from workspace import app
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        parent = root / "All recordings"
        child = parent / "Board"
        child.mkdir(parents=True)
        ws = app.Workspace(root / "ws", root / "models", start=False,
                           allow_test_engines=True, battery=False,
                           protector=keyprotect.TestProtector())
        try:
            ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
            pid = ws.add_watched_folder(str(parent), False)
            cid = ws.add_watched_folder(str(child), False)
            ws.set_folder_authority(pid, delete_originals=True, confirmed=True)    # parent allowed
            original = wav(child / "Board.wav", seconds=6, seed=23)
            ws._import_one(original, cid)                           # came from the child
            mid = ws.meetings()[0]["id"]
            [src] = ws.store.sources(mid)
            self.assertIsNone(src["original_path"])                 # not even recorded
            ws.start()
            self.assertTrue(ws.wait_idle(60))
            ws.confirm_reviewed(mid)
            self.assertTrue(ws.wait_idle(60))
            self.assertTrue(original.exists())
        finally:
            ws.close()
            tmp.cleanup()

    def test_replaced_original_is_never_deleted(self):
        kept, result = self.run_flow(authority=True, replace=True)
        self.assertTrue(kept)
        self.assertIn("changed", result)


@unittest.skipUnless(HAVE_DEPS and os.name == "nt", "Windows paths")
class SyncedSourceTests(unittest.TestCase):
    """#219: the workspace is refused in OneDrive (D3), but recordings are not.
    Teams saves recordings to OneDrive, so one there must still import by hand
    and through a watched folder, with the app opening its default workspace
    while the OneDrive variables name the synced folder."""

    def test_a_recording_in_onedrive_still_imports_and_is_watched(self):
        from unittest import mock

        from workspace import app
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "Local"
            local.mkdir()
            synced = root / "OneDrive - Synthetic Firm"
            recordings = synced / "Recordings"
            teams = wav(recordings / "Synthetic Teams call.wav", seconds=6, seed=31)
            env = {"LOCALAPPDATA": str(local), "OneDrive": str(synced),
                   "OneDriveCommercial": str(synced)}
            saved = (intake.SCAN_INTERVAL_S, intake.STABLE_S, intake.STABLE_RECENT_S)
            intake.SCAN_INTERVAL_S, intake.STABLE_S, intake.STABLE_RECENT_S = 1, 0, 0
            ws = None
            try:
                with mock.patch.dict(os.environ, env):
                    # The default workspace, with the app's own protector
                    # (Windows Data Protection): the test one is refused there.
                    ws = app.Workspace(cache_dir=root / "models", start=False,
                                       allow_test_engines=True, battery=False)
                    self.assertEqual(ws.root, local / "Unminute" / "workspace")
                    ws.settings.update(model="test:fake_worker_engines:sentences",
                                       speakers=False)
                    # By hand (the file dialog or a drop).
                    self.assertEqual(ws.import_paths([str(teams)]),
                                     {"accepted": 1, "refused": []})
                    # Watched: what is there already is the baseline; a new one imports.
                    ws.add_watched_folder(str(recordings), False)
                    ws.start()
                    time.sleep(2.5)                              # baseline scan done
                    wav(recordings / "Synthetic standup.wav", seconds=8, seed=32)
                    end = time.time() + 60
                    while time.time() < end and len(ws.meetings()) < 2:
                        time.sleep(0.5)
                    self.assertTrue(ws.wait_idle(60))
                    self.assertEqual(sorted(m["title"] for m in ws.meetings()),
                                     ["Synthetic Teams call", "Synthetic standup"])
                    [folder] = ws.watched_folders()["folders"]
                    self.assertEqual(folder["imported"], 1)
                    self.assertFalse(folder["error"])
            finally:
                intake.SCAN_INTERVAL_S, intake.STABLE_S, intake.STABLE_RECENT_S = saved
                if ws is not None:
                    ws.close()


if __name__ == "__main__":
    unittest.main()
