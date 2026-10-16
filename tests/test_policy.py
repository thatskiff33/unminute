# Managed policy (#111 slice A5): parsing, fail-safe handling of bad values,
# and refresh. The registry itself is read only on Windows; the parsing rules
# are tested with winreg's type constants.
#   python -m unittest discover -s tests -v

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace import policy  # noqa: E402


class PolicyTests(unittest.TestCase):
    def test_unmanaged_values_use_the_app_default(self):
        p = policy.Policy()
        self.assertTrue(p.get("AllowModelDownloads"))
        self.assertFalse(p.get("AllowNetworkSources"))
        self.assertEqual(p.get("ClipboardClearMinutes"), 0)
        self.assertFalse(p.managed("AllowModelDownloads"))

    def test_set_values_win(self):
        p = policy.Policy({"AllowModelDownloads": False, "AllowNetworkSources": True,
                           "ClipboardClearMinutes": 5})
        self.assertFalse(p.get("AllowModelDownloads"))
        self.assertTrue(p.get("AllowNetworkSources"))
        self.assertEqual(p.get("ClipboardClearMinutes"), 5)
        self.assertTrue(p.managed("ClipboardClearMinutes"))

    def test_a_bad_value_never_grants_a_permission(self):
        p = policy.Policy(invalid=("AllowModelDownloads", "AllowNetworkSources",
                                   "ClipboardClearMinutes"))
        self.assertFalse(p.get("AllowModelDownloads"))     # default True, withheld
        self.assertFalse(p.get("AllowNetworkSources"))
        self.assertEqual(p.get("ClipboardClearMinutes"), 0)  # not a permission: default
        self.assertEqual(p.summary()["invalid"],
                         ["AllowModelDownloads", "AllowNetworkSources",
                          "ClipboardClearMinutes"])

    def test_source_caches_and_refreshes(self):
        calls, now = [], [0.0]

        def reader():
            calls.append(1)
            return {"AllowModelDownloads": len(calls) == 1}, ()
        src = policy.PolicySource(reader=reader, refresh_s=60, clock=lambda: now[0])
        self.assertTrue(src.current().get("AllowModelDownloads"))
        now[0] = 30
        self.assertTrue(src.current().get("AllowModelDownloads"))   # cached
        now[0] = 61
        self.assertFalse(src.current().get("AllowModelDownloads"))  # read again
        self.assertEqual(len(calls), 2)
        src.refresh()
        self.assertEqual(len(calls), 3)


@unittest.skipUnless(sys.platform == "win32", "Windows registry types")
class ParseTests(unittest.TestCase):
    def test_types_and_ranges(self):
        import winreg
        self.assertIs(policy._parse("AllowWatchedFolders", 0, winreg.REG_DWORD), False)
        self.assertIs(policy._parse("AllowWatchedFolders", 1, winreg.REG_DWORD), True)
        self.assertEqual(policy._parse("ClipboardClearMinutes", 15, winreg.REG_DWORD), 15)
        for name, value, kind in (("AllowWatchedFolders", 2, winreg.REG_DWORD),
                                  ("AllowWatchedFolders", "1", winreg.REG_SZ),
                                  ("ClipboardClearMinutes", 99999, winreg.REG_DWORD),
                                  ("HandoffDefaultFolder", "relative\\path", winreg.REG_SZ),
                                  ("HandoffDefaultFolder", "\\\\server\\share\\Clients",
                                   winreg.REG_SZ),
                                  ("HandoffDefaultFolder", "%NO_SUCH_VAR%\\x",
                                   winreg.REG_EXPAND_SZ),
                                  ("HandoffDefaultFolder", 5, winreg.REG_DWORD)):
            with self.assertRaises(ValueError, msg=(name, value)):
                policy._parse(name, value, kind)

    def test_folder_expands_environment_variables(self):
        import os
        import winreg
        got = policy._parse("HandoffDefaultFolder", "%USERPROFILE%\\Handoffs",
                            winreg.REG_EXPAND_SZ)
        self.assertEqual(got, os.path.join(os.environ["USERPROFILE"], "Handoffs"))

    def test_reading_without_a_policy_key_is_empty(self):
        values, invalid = policy.read_registry()     # dev machines have no policy key
        self.assertIsInstance(values, dict)
        self.assertIsInstance(invalid, tuple)


@unittest.skipUnless(sys.platform == "win32", "Windows clipboard")
class ClipboardClearTests(unittest.TestCase):
    def test_clears_only_the_apps_own_copy(self):
        from workspace import clipboard
        try:
            previous = clipboard.read_text()
        except clipboard.ClipboardError:
            self.skipTest("clipboard busy")
        try:
            ours = clipboard.copy_text("handoff text")
            self.assertTrue(clipboard.clear_if_unchanged(ours))
            self.assertEqual(clipboard.read_text(), "")
            ours = clipboard.copy_text("handoff text")
            clipboard.copy_text("something the user copied since")
            self.assertFalse(clipboard.clear_if_unchanged(ours))
            self.assertEqual(clipboard.read_text(), "something the user copied since")
        finally:
            clipboard.copy_text(previous or "")       # put the user's text back


class AdmxTests(unittest.TestCase):
    """The ADMX template IT imports into Intune matches the values the app reads."""

    def test_template_covers_every_policy_value_with_text(self):
        import re
        import xml.etree.ElementTree as ET
        root = Path(__file__).resolve().parents[1] / "packaging" / "policy"
        ns = "{http://schemas.microsoft.com/GroupPolicy/2006/07/PolicyDefinitions}"
        admx_text = (root / "Unminute.admx").read_text(encoding="utf-8")
        admx = ET.fromstring(admx_text.encode("utf-8"))
        adml = ET.parse(root / "en-US" / "Unminute.adml").getroot()
        names = {p.get("name") for p in admx.iter(ns + "policy")}
        self.assertEqual(names, set(policy.SPEC))
        for p in admx.iter(ns + "policy"):
            self.assertEqual(p.get("key"), policy.POLICY_KEY)
            self.assertEqual(p.get("class"), "Machine")
        strings = {s.get("id") for s in adml.iter(ns + "string")}
        presentations = {s.get("id") for s in adml.iter(ns + "presentation")}
        self.assertLessEqual(set(re.findall(r"\$\(string\.(\w+)\)", admx_text)), strings)
        self.assertLessEqual(set(re.findall(r"\$\(presentation\.(\w+)\)", admx_text)),
                             presentations)


import importlib.util as _ilu  # noqa: E402

HAVE_DEPS = all(_ilu.find_spec(m) for m in ("numpy", "av"))


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class WorkspacePolicyTests(unittest.TestCase):
    """Each policy value is enforced by the app, whatever the page shows."""

    def open(self, **values):
        import tempfile
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                            allow_test_engines=True, policy_reader=lambda: (values, ()))
        return self.ws

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def refused(self, fn, *args):
        from workspace.app import WorkspaceError
        with self.assertRaises(WorkspaceError) as ctx:
            fn(*args)
        return str(ctx.exception)

    def test_downloads_off(self):
        ws = self.open(AllowModelDownloads=False)
        self.assertIn("Downloads are turned off", self.refused(ws.download_model,
                                                              "parakeet-tdt-0.6b-v2"))
        self.assertFalse(ws.setup_state()["managed"]["allow"]["AllowModelDownloads"])

    def test_gpu_component_off(self):
        from workspace import gpu
        ws = self.open(AllowGpuComponent=False)
        self.assertIn("graphics card", self.refused(ws.download_model, gpu.COMPONENT))
        self.assertIn("organisation", self.refused(ws.set_settings, {"gpu_speakers": True}))
        ws.settings.update(gpu_speakers=True, mode="faster")
        with mock.patch.object(gpu, "verified_for_use", return_value=True):
            self.assertEqual(ws._speaker_device(), ("cpu", None))
        self.assertFalse(ws.get_settings()["gpu_speakers"])     # shown as off

    def test_microphone_off(self):
        ws = self.open(AllowMicrophoneRecording=False)
        ws.settings.update(microphone=True)                      # saved before the policy
        self.assertIn("organisation", self.refused(ws.microphone_devices))
        self.assertIn("organisation", self.refused(ws.set_settings, {"microphone": True}))
        self.assertFalse(ws.get_settings()["microphone"])

    def test_watched_folders_and_deletion_authority_off(self):
        ws = self.open(AllowWatchedFolders=False, AllowDeleteOriginals=False)
        self.assertIn("Watched folders", self.refused(ws.add_watched_folder, "C:\\x", False))
        self.assertIn("Deleting original", self.refused(
            ws.set_folder_authority, "a" * 32, True, None))
        seen = {}
        with mock.patch("workspace.cleanup.run_job",
                        side_effect=lambda *a, **kw: seen.update(kw)), \
                mock.patch("workspace.intake.deletion_roots", return_value=["C:\\rec"]):
            ws._run("cleanup", "m" * 32, "job")
        self.assertEqual(seen["original_roots"], [])             # originals are kept

    def test_clipboard_is_cleared_later_only_by_policy(self):
        from workspace import clipboard
        ws = self.open(ClipboardClearMinutes=5)
        out = {"combined": "text", "long": False}
        with mock.patch.object(ws, "_checked", return_value=out), \
                mock.patch.object(clipboard, "copy_text", return_value=42), \
                mock.patch.object(clipboard, "clear_if_unchanged") as clear:
            self.assertEqual(ws.copy_handoff("m" * 32, "t", "combined", "x")["clears_in_minutes"],
                             5)
            clear.assert_not_called()
            ws._flush_clipboard_clears()                         # closing runs pending clears
            clear.assert_called_once_with(42)

    def test_handoff_file_name_and_folder(self):
        import tempfile
        from workspace import handoff
        ws = self.open(HandoffFilenameIncludesTitle=True,
                       HandoffDefaultFolder=tempfile.gettempdir())
        mid = ws.store.create_meeting("Client: budget review")
        built = {"filename": "Meeting transcript 2026-10-16 10-00.md"}
        with mock.patch.object(handoff, "build", return_value=built), \
                mock.patch.object(ws.templates, "get", return_value={}), \
                mock.patch.object(handoff, "options", return_value={}):
            self.assertEqual(ws.handoff_preview(mid, "t")["filename"],
                             "Client  budget review - transcript.md")
            self.assertEqual(ws.handoff_preview(mid, "t", True)["filename"],
                             "Meeting transcript 2026-10-16 10-00.md")   # anonymised: no title
        self.assertEqual(ws.handoff_folder(), tempfile.gettempdir())

    def test_network_sources_only_by_policy(self):
        from workspace import app as app_module
        for values, expected in (({}, False), ({"AllowNetworkSources": True}, True)):
            if expected:                                     # second pass: a fresh workspace
                self.ws.close()
                self.tmp.cleanup()
            ws = self.open(**values)
            with mock.patch.object(app_module, "validate_input_file",
                                   side_effect=app_module.UnsafePathError("no")) as check:
                ws.import_paths(["Z:\\meeting.wav"])
            self.assertIs(check.call_args.kwargs["allow_network"], expected)

    def test_without_policy_nothing_changes(self):
        ws = self.open()
        managed = ws.get_settings()["managed"]
        self.assertEqual(managed["managed"], [])
        self.assertTrue(all(v for k, v in managed["allow"].items()
                            if k not in ("AllowNetworkSources", "HandoffFilenameIncludesTitle")))


if __name__ == "__main__":
    unittest.main()
