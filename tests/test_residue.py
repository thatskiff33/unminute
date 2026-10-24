# tests/test_residue.py
# What the packaged checks look for after #111 phase C sealed the content
# (tools/residue.py; #111 C7): the finder and its positive control, the files
# allowed after final cleanup (built from the app's constants) and the crash
# dump count, which reads names and sizes only and nothing outside its base
# unless a person asks. Synthetic files only.

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import residue  # noqa: E402


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, rel, data=b"x"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class FindTests(TempDir):
    def test_names_the_files_and_patterns_found_never_the_bytes(self):
        self.put("profile/a.bin", b"head zebra tail")
        self.put("profile/sub/b.bin", b"nothing here")
        out = residue.find([self.root / "profile"], {"m": b"zebra", "w": b"\x01\x02"},
                           self.root)
        self.assertEqual({"m": ["profile/a.bin"]}, out["hits"])
        self.assertEqual(["profile/a.bin", "profile/sub/b.bin"], sorted(out["scanned"]))
        self.assertEqual([], out["unreadable"])

    def test_a_pattern_across_a_read_boundary_is_found(self):
        self.put("profile/big.bin", b"a" * 13 + b"PATTERN" + b"b" * 20)
        with mock.patch.object(residue, "CHUNK", 16):
            out = residue.find([self.root / "profile"], {"p": b"PATTERN"}, self.root)
        self.assertEqual({"p": ["profile/big.bin"]}, out["hits"])

    def test_a_file_that_cannot_be_read_is_reported_not_passed(self):
        locked = self.put("profile/locked.db", b"zebra")
        real_open = open

        def refusing(path, *a, **k):
            if Path(path) == locked:
                raise PermissionError("in use")
            return real_open(path, *a, **k)
        with mock.patch("builtins.open", refusing):
            out = residue.find([self.root / "profile"], {"m": b"zebra"}, self.root)
        self.assertEqual(["profile/locked.db"], out["unreadable"])
        self.assertEqual({}, out["hits"])

    def test_text_is_looked_for_as_utf8_and_utf16(self):
        pats = residue.text_patterns("quokka")
        self.assertEqual({b"quokka", "quokka".encode("utf-16-le")}, set(pats.values()))

    def test_windows_split_evenly(self):
        self.assertEqual({"window_1": b"ab", "window_2": b"cd"},
                         residue.split_windows(b"abcde", 2))


class ControlTests(TempDir):
    PATTERNS = {"marker": b"zebracrossing", "window_1": bytes(range(256)) * 8,
                "window_2": b"\xff\x00" * 900}

    def test_the_finder_finds_every_planted_pattern(self):
        self.assertEqual([], residue.control(self.root, self.PATTERNS))
        self.assertFalse((self.root / residue.CONTROL_FOLDER).exists())   # removed again

    def test_a_finder_that_finds_nothing_fails_the_control(self):
        blind = mock.Mock(return_value={"hits": {}, "unreadable": [], "scanned": []})
        self.assertEqual(list(self.PATTERNS), residue.control(self.root, self.PATTERNS,
                                                              finder=blind))
        # The same finder is asked, over the control file only, outside any profile.
        self.assertEqual([self.root / residue.CONTROL_FOLDER], blind.call_args[0][0])

    def test_a_finder_that_misses_one_names_it(self):
        def partial(roots, patterns, base):
            out = residue.find(roots, patterns, base)
            out["hits"].pop("window_2", None)
            return out
        self.assertEqual(["window_2"], residue.control(self.root, self.PATTERNS,
                                                       finder=partial))


class AllowedFilesTests(TempDir):
    def setUp(self):
        super().setUp()
        self.n = residue.names()

    def test_the_workspace_s_own_files_are_allowed(self):
        for rel in ("workspace.db", "workspace.db-wal", "workspace.db-shm", "settings.json",
                    "settings.unreadable.json", "templates.sealed", "power_restore.json",
                    "compact.pending", "logs/app.log", "logs/app.1.log", "logs/app.2.log",
                    "logs/worker.log", "logs/draft-worker.log", "logs/setup.log",
                    "logs/gpu-worker.1.log", "logs/app.running", "logs/integrity.json",
                    "logs/keys.json", "logs/app.crash", "audit/audit-000001.jsonl",
                    "audit/audit-1000000.jsonl"):
            with self.subTest(rel=rel):
                self.assertTrue(residue.allowed(rel, self.n))

    def test_anything_else_is_not(self):
        for rel in ("stray.bin", "templates.json", "logs/stray.log", "logs/app.10.log",
                    "logs/app.rotating", "media/abc/audio.uma", "media/abc/audio.partial",
                    "recordings/r1/chunk-0001.uma", "recordings/r1/chunk-0002.tmp",
                    "audit/notes.txt", "audit/audit-1.jsonl", "webview/EBWebView/x",
                    "workspace.db.new", "logs/sub/app.log"):
            with self.subTest(rel=rel):
                self.assertFalse(residue.allowed(rel, self.n))

    def test_the_list_follows_the_app_s_constants(self):
        from workspace import diag
        with mock.patch.object(diag, "SETTINGS_FILE", "prefs.json"), \
                mock.patch.object(diag, "LOGS_FOLDER", "diagnostics"), \
                mock.patch.object(diag, "LOG_SUFFIX", ".txt"):
            n = residue.names()
        self.assertTrue(residue.allowed("prefs.json", n))
        self.assertFalse(residue.allowed("settings.json", n))
        self.assertTrue(residue.allowed("diagnostics/app.1.txt", n))
        self.assertFalse(residue.allowed("logs/app.log", n))
        self.assertFalse(residue.allowed("diagnostics/app.log", n))

    def test_the_app_writes_with_those_constants(self):
        # A rename moves the list only if every place that names these files
        # uses the constant: the window, the support options with no window
        # (--status, --health, --gate, --restore-lid, --verify-audit) and the
        # packaged checks that open the workspace themselves (review of #335).
        literals = ('"settings.json"', '"settings.unreadable.json"', '"power_restore.json"',
                    '/ "logs"', '/ "audit"', '"workspace.db', '"app.running"', '"app.log"')
        for rel in ("workspace/app.py", "workspace/__main__.py", "tools/packaged_check.py",
                    "tools/packaged_crash_check.py", "tools/perf_check.py"):
            source = (ROOT / rel).read_text(encoding="utf-8")
            for literal in literals:
                with self.subTest(rel=rel, literal=literal):
                    self.assertNotIn(literal, source)
        diag_source = (ROOT / "workspace" / "diag.py").read_text(encoding="utf-8")
        for constant in ('DB_FILE = "workspace.db"', 'SETTINGS_FILE = "settings.json"',
                         'LOGS_FOLDER = "logs"', 'AUDIT_FOLDER = "audit"'):
            with self.subTest(constant=constant):
                self.assertIn(constant, diag_source)
        power_source = (ROOT / "workspace" / "power.py").read_text(encoding="utf-8")
        self.assertEqual(1, power_source.count('"power_restore.json"'))   # RESTORE_FILE
        after = diag_source.split('CRASH_SUFFIX = ".crash"', 1)[1]
        for literal in ('.log"', '.crash"', '.running"', '/ "logs"', '"workspace.db'):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, after)

    def test_unexpected_lists_strays_and_anything_outside_the_workspace(self):
        profile = self.root / "Unminute"
        ws = profile / "workspace"
        for rel in ("workspace/workspace.db", "workspace/logs/app.log",
                    "workspace/logs/app.1.log", "workspace/stray.bin",
                    "workspace/media/abc/audio.uma", "elsewhere.txt"):
            self.put(f"Unminute/{rel}")
        self.assertEqual(["elsewhere.txt", "workspace/media/abc/audio.uma",
                          "workspace/stray.bin"],
                         sorted(residue.unexpected(profile, ws, self.n)))

    def test_logs_scanned_are_listed(self):
        scanned = ["Unminute/workspace/logs/app.log", "Unminute/workspace/workspace.db",
                   "Unminute/workspace/logs/worker.log"]
        self.assertEqual(["Unminute/workspace/logs/app.log",
                          "Unminute/workspace/logs/worker.log"],
                         residue.logs_scanned(scanned, self.n, "Unminute/workspace"))


class DumpTests(TempDir):
    def test_dumps_and_crash_files_are_counted(self):
        self.put("crash/CrashDumps/python.exe.123.dmp", b"MDMP")
        self.put("crash/Unminute/workspace/logs/app.crash", b"Fatal Python error")
        self.put("crash/Unminute/workspace/logs/worker.crash", b"")    # opened, not used
        self.put("crash/Unminute/workspace/logs/app.log")
        self.assertEqual({"dumps": 1, "crash_files": 1}, residue.dumps(self.root))

    def test_no_dump_is_ever_opened(self):
        self.put("x/a.dmp", b"MDMP")
        self.put("x/app.crash", b"Fatal")
        with mock.patch("builtins.open", side_effect=AssertionError("opened")), \
                mock.patch("io.open", side_effect=AssertionError("opened")):
            self.assertEqual({"dumps": 1, "crash_files": 1}, residue.dumps(self.root))

    def test_only_the_base_is_listed(self):
        self.put("x/y/a.dmp", b"MDMP")
        listed = []
        real_scandir = os.scandir

        def recording(path=".", *a, **k):
            listed.append(Path(os.fspath(path)))
            return real_scandir(path, *a, **k)
        with mock.patch.object(os, "scandir", recording):
            self.assertEqual({"dumps": 1, "crash_files": 0}, residue.dumps(self.root))
        self.assertTrue(listed)
        for path in listed:
            with self.subTest(path=path):
                self.assertTrue(path == self.root or self.root in path.parents, path)

    def test_system_folders_are_counted_by_name_and_time_only(self):
        since = time.time() - 60
        self.put("CrashDumps/python.exe.1.dmp", b"MDMP")
        self.put("CrashDumps/pythonw.exe.2.dmp", b"MDMP")
        self.put("CrashDumps/otherapp.exe.3.dmp", b"MDMP")
        old = self.put("CrashDumps/python.exe.4.dmp", b"MDMP")
        os.utime(old, (since - 3600, since - 3600))
        (self.root / "WER" / "AppCrash_msedgewebview2.exe_ab12").mkdir(parents=True)
        with mock.patch("builtins.open", side_effect=AssertionError("opened")):
            out = residue.system_dumps(since, folders=[self.root / "CrashDumps",
                                                       self.root / "WER",
                                                       self.root / "missing"])
        self.assertEqual(3, out["new_files"])
        self.assertEqual(3, out["folders"])

    def test_the_system_folders_come_from_the_real_profile(self):
        folders = residue.system_dump_folders({"LOCALAPPDATA": r"C:\Users\u\AppData\Local",
                                               "ProgramData": r"C:\ProgramData"})
        self.assertIn(Path(r"C:\Users\u\AppData\Local") / "CrashDumps", folders)
        self.assertEqual(5, len(folders))


class ToolsUseTheSameFinderTests(unittest.TestCase):
    def test_both_packaged_checks_scan_with_residue(self):
        for tool in ("packaged_check.py", "packaged_crash_check.py"):
            source = (ROOT / "tools" / tool).read_text(encoding="utf-8")
            with self.subTest(tool=tool):
                self.assertIn("residue.find(", source)
                self.assertIn("residue.control(", source)
                self.assertIn("residue.unexpected(", source)
                self.assertIn("residue.dumps(", source)
                self.assertNotIn("read_bytes() in", source)    # no second, plainer scanner


if __name__ == "__main__":
    unittest.main()
