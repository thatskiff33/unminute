# Tests for workspace.safe_paths and the bridge's argument validation.
# Stdlib only for paths; the bridge tests need numpy and skip without it.
# The junction test creates a real NTFS junction (no admin needed) on Windows.
#   python -m unittest discover -s tests -v

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace import safe_paths as sp  # noqa: E402

AUDIO = (".wav", ".mp3", ".m4a")


class RawPathTests(unittest.TestCase):
    def test_refuses_relative_unc_device_and_nul(self):
        for bad in ("meeting.wav", "..\\x.wav", "\\\\server\\share\\a.wav",
                    "//server/share/a.wav", "\\\\?\\C:\\a.wav", "\\\\.\\PhysicalDrive0",
                    # mixed-separator UNC (found in review): would contact a server
                    "/\\localhost\\c$\\Windows\\win.ini", "\\/localhost/c$/Windows/win.ini",
                    # rooted without a drive, drive-relative, alternate data stream
                    "\\Windows\\win.ini", "C:foo.wav", "C:\\a.wav:hidden.wav",
                    "C:\\a.wav::$DATA", "C:\\a\x00.wav", "", None, 42):
            with self.assertRaises(sp.UnsafePathError, msg=repr(bad)):
                sp.check_raw(bad)


@unittest.skipUnless(os.name == "nt", "the workspace is Windows-only (drive-letter paths)")
class FileValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "allowed"
        self.root.mkdir()
        self.outside = Path(self.tmp.name) / "outside"
        self.outside.mkdir()
        (self.root / "ok.wav").write_bytes(b"RIFF" + b"\0" * 100)
        (self.outside / "secret.wav").write_bytes(b"RIFF" + b"\0" * 100)

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_a_normal_file_inside_root(self):
        got = sp.validate_input_file(self.root / "ok.wav", AUDIO, 10**6, roots=[self.root])
        self.assertEqual(got, Path(os.path.realpath(self.root / "ok.wav")))

    def test_refuses_dot_dot_escape(self):
        sneaky = self.root / ".." / "outside" / "secret.wav"
        with self.assertRaises(sp.UnsafePathError):
            sp.validate_input_file(sneaky, AUDIO, 10**6, roots=[self.root])

    @unittest.skipUnless(os.name == "nt", "NTFS junctions are Windows-only")
    def test_refuses_junction_escape(self):
        link = self.root / "looks-inside"
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.outside)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        try:
            with self.assertRaises(sp.UnsafePathError) as ctx:
                sp.validate_input_file(link / "secret.wav", AUDIO, 10**6, roots=[self.root])
            self.assertIn("outside the folders", str(ctx.exception))
        finally:
            os.rmdir(link)   # removes the junction only, never its target

    def test_symbolic_link_on_the_way_is_refused(self):
        link = self.root / "link.wav"
        try:
            os.symlink(self.outside / "secret.wav", link)
        except OSError:
            self.skipTest("creating symlinks needs Developer Mode or admin on Windows")
        with self.assertRaises(sp.UnsafePathError) as ctx:
            sp.validate_input_file(link, AUDIO, 10**6)
        self.assertIn("symbolic link", str(ctx.exception))
        # allow_network (managed policy) lifts the link and drive rules only
        self.assertTrue(sp.validate_input_file(link, AUDIO, 10**6, allow_network=True))

    def test_mapped_network_drive_is_refused_before_resolving(self):
        from unittest import mock
        with mock.patch.object(sp, "_drive_type", return_value=sp.DRIVE_REMOTE), \
                mock.patch.object(sp.os.path, "realpath") as realpath:
            with self.assertRaises(sp.UnsafePathError) as ctx:
                sp.validate_input_file(self.root / "ok.wav", AUDIO, 10**6)
            realpath.assert_not_called()              # nothing contacted the server
        self.assertIn("network drives", str(ctx.exception))

    def test_a_path_that_resolves_to_a_network_share_is_refused(self):
        from unittest import mock
        with mock.patch.object(sp.os.path, "realpath",
                               return_value="\\\\server\\share\\ok.wav"):
            with self.assertRaises(sp.UnsafePathError) as ctx:
                sp.validate_input_file(self.root / "ok.wav", AUDIO, 10**6)
        self.assertIn("network drives", str(ctx.exception))

    def test_junction_to_a_local_folder_is_still_allowed(self):
        link = self.root / "via-junction"
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.outside)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        try:
            got = sp.validate_input_file(link / "secret.wav", AUDIO, 10**6)
            self.assertEqual(got, Path(os.path.realpath(self.outside / "secret.wav")))
        finally:
            os.rmdir(link)

    def test_refuses_wrong_type_empty_large_and_directories(self):
        (self.root / "notes.exe").write_bytes(b"MZ")
        (self.root / "empty.wav").write_bytes(b"")
        cases = [(self.root / "notes.exe", "not supported"),
                 (self.root / "empty.wav", "empty"),
                 (self.root, "regular files"),
                 (self.root / "missing.wav", "could not be found")]
        for path, msg in cases:
            with self.assertRaises(sp.UnsafePathError) as ctx:
                sp.validate_input_file(path, AUDIO, 10**6, roots=[self.root])
            self.assertIn(msg, str(ctx.exception))
        with self.assertRaises(sp.UnsafePathError):
            sp.validate_input_file(self.root / "ok.wav", AUDIO, 10, roots=[self.root])


@unittest.skipUnless(importlib.util.find_spec("webview"), "pywebview not installed")
class DispatchGuardTests(unittest.TestCase):
    """pywebview resolves page calls with getattr over any dotted name; the
    guard must refuse everything except the exact allowed names."""

    def test_only_exact_allowed_names_reach_pywebview(self):
        import webview.util as util

        from workspace import shell
        seen = []
        original = util.js_bridge_call
        util.js_bridge_call = lambda w, name, p, i: seen.append(name)
        try:
            shell.install_dispatch_guard({"clip", "ping", "transcript"})
            for name in ("__init__", "__class__.__init__", "_audio.tofile",
                         "clip.__globals__.clear", "clip.__call__", "pywebviewStateUpdate",
                         "pywebviewMoveWindow", "pywebviewEventHandler", None, 7):
                util.js_bridge_call(None, name, [], "id")
            for name in ("clip", "ping", "transcript", "pywebviewAsyncCallback"):
                util.js_bridge_call(None, name, [], "id")
        finally:
            util.js_bridge_call = original
        self.assertEqual(seen, ["clip", "ping", "transcript", "pywebviewAsyncCallback"])
        self.assertIn("_audio.tofile", shell.blocked_calls)

    def test_navigation_allowlist(self):
        from workspace import shell
        base = shell.UI_DIR.as_uri()
        self.assertTrue(shell._allowed(base + "/index.html"))
        self.assertTrue(shell._allowed(base.replace("%20", " ") + "/index.html"))
        for bad in ("https://example.com/", "about:blank", "data:text/html,x",
                    base + "-evil/index.html", "file:///C:/Windows/win.ini"):
            self.assertFalse(shell._allowed(bad), bad)


if __name__ == "__main__":
    unittest.main()
