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
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
EXT = (".wav",)

if HAVE_DEPS:
    from test_intake import wav

    from workspace import intake
    from workspace.safe_paths import UnsafePathError
    from workspace.store import Store


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / "OneDrive - Org" / "Recordings"
        self.folder.mkdir(parents=True)
        self.store = Store(self.root / "ws" / "workspace.db")
        self.fid = intake.add_folder(self.store, str(self.folder))
        self.queued, self.now = [], time.time()
        self.scan()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def scan(self, hasher=None):
        self.now += 10
        return intake.scan(self.store, EXT, 1 << 30,
                           lambda p, fid: self.queued.append(Path(p)), now=self.now,
                           hasher=hasher)

    def test_cloud_only_file_waits_without_inventing_progress(self):
        f = wav(self.folder / "cloud.wav")
        real = intake.validate_input_file
        calls = []

        def placeholder(path, ext, size, allow_cloud=False):
            calls.append(allow_cloud)
            if not allow_cloud:
                raise UnsafePathError("The file is in cloud storage and not downloaded to "
                                      "this computer yet.")
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
        self.assertEqual(intake.deletion_roots(self.store), [])   # watching is not authority
        intake.set_delete_originals(self.store, self.fid, True)
        self.assertEqual(intake.deletion_roots(self.store), [os.path.realpath(self.folder)])
        intake.set_delete_originals(self.store, self.fid, False)
        self.assertEqual(intake.deletion_roots(self.store), [])


@unittest.skipUnless(HAVE_DEPS and os.name == "nt", "Windows paths")
class AuthorityFlowTests(unittest.TestCase):
    def run_flow(self, authority, revoke_before_cleanup=False, replace=False):
        from workspace import app
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        folder = root / "Recordings"
        folder.mkdir()
        ws = app.Workspace(root / "ws", root / "models", start=False, battery=False)
        try:
            ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
            fid = ws.add_watched_folder(str(folder), False)
            if authority:
                ws.set_folder_authority(fid, delete_originals=True)
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

    def test_replaced_original_is_never_deleted(self):
        kept, result = self.run_flow(authority=True, replace=True)
        self.assertTrue(kept)
        self.assertIn("changed", result)


if __name__ == "__main__":
    unittest.main()
