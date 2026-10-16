# Managed policy (#111 slice A5): parsing, fail-safe handling of bad values,
# and refresh. The registry itself is read only on Windows; the parsing rules
# are tested with winreg's type constants.
#   python -m unittest discover -s tests -v

import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
