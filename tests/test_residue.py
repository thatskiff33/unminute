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

    def test_a_folder_that_cannot_be_listed_is_reported_not_skipped(self):
        # os.walk skips such a folder in silence unless told otherwise; what
        # is in it cannot be shown free of the patterns (review of #335).
        self.put("profile/a.bin", b"nothing")
        self.put("profile/denied/b.bin", b"zebra")
        refused = self.root / "profile" / "denied"
        real_scandir = os.scandir

        def scandir(path=".", *a, **k):
            if Path(os.fspath(path)) == refused:
                raise PermissionError(13, "Access is denied", os.fspath(path))
            return real_scandir(path, *a, **k)
        unreadable = []
        with mock.patch.object(os, "scandir", scandir):
            out = residue.find([self.root / "profile"], {"m": b"zebra"}, self.root)
            listed = residue.listing(self.root / "profile", unreadable, self.root)
            stray = residue.unexpected(self.root / "profile", self.root / "profile",
                                       residue.names())
        self.assertEqual(["profile/denied/"], out["unreadable"])
        self.assertEqual({}, out["hits"])
        self.assertEqual(["a.bin"], sorted(listed))
        self.assertEqual(["profile/denied/"], unreadable)
        self.assertIn("denied/", stray)

    def test_a_missing_root_holds_nothing(self):
        out = residue.find([self.root / "nowhere"], {"m": b"zebra"}, self.root)
        self.assertEqual(([], []), (out["scanned"], out["unreadable"]))

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

    def test_the_first_pattern_straddles_a_read_boundary(self):
        # So a finder that reads in pieces without overlap fails the control.
        seen = {}

        def reading(roots, patterns, base):
            path = next(Path(roots[0]).iterdir())
            data = path.read_bytes()
            first = next(iter(patterns.values()))
            seen["at"], seen["size"] = data.index(first), len(first)
            return residue.find(roots, patterns, base)
        with mock.patch.object(residue, "CHUNK", 4096):
            self.assertEqual([], residue.control(self.root, self.PATTERNS, finder=reading))
        self.assertLess(seen["at"], 4096)
        self.assertGreater(seen["at"] + seen["size"], 4096)

    def test_a_finder_without_overlap_fails_the_control(self):
        def no_overlap(roots, patterns, base):
            hits = {}
            for path in Path(roots[0]).iterdir():
                data = path.read_bytes()
                for i in range(0, len(data), residue.CHUNK):
                    for label, p in patterns.items():
                        if p in data[i:i + residue.CHUNK]:
                            hits[label] = [path.name]
            return {"hits": hits, "unreadable": [], "scanned": []}
        with mock.patch.object(residue, "CHUNK", 4096):
            missed = residue.control(self.root, self.PATTERNS, finder=no_overlap)
        self.assertIn("marker", missed)        # the first, placed across the boundary

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

    def test_the_real_start_s_files_and_the_download_queue_are_allowed(self):
        # The workspace lock, the launcher's record and the download queue
        # with its temporary file (review of #335), from their constants.
        for rel in ("instance.lock", "logs/launch.json", "download_queue.json",
                    "download_queue.tmp"):
            with self.subTest(rel=rel):
                self.assertTrue(residue.allowed(rel, self.n))
        from workspace import diag, instance, setup
        with mock.patch.object(instance, "NAME", "held.lock"), \
                mock.patch.object(diag, "LAUNCH_FILE", "start.json"), \
                mock.patch.object(setup, "QUEUE_FILE", "queue.json"):
            n = residue.names()
        for rel, ok in (("held.lock", True), ("instance.lock", False),
                        ("logs/start.json", True), ("logs/launch.json", False),
                        ("queue.json", True), ("queue.tmp", True),
                        ("download_queue.json", False)):
            with self.subTest(rel=rel):
                self.assertEqual(ok, residue.allowed(rel, n))

    def test_a_real_workspace_lock_leaves_only_allowed_files(self):
        # The lock the app's own start takes (instance.acquire) is one of
        # the workspace's own files.
        from workspace import instance
        ws = self.root / "Unminute" / "workspace"
        ws.mkdir(parents=True)
        held = instance.acquire(ws)
        try:
            self.assertEqual([], residue.unexpected(self.root / "Unminute", ws, self.n))
        finally:
            held.release()

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
        # packaged checks that open the workspace themselves (review of #335);
        # offboarding and the launcher pass, which read the workspace with no
        # window; the durable-core check; and the places that collect a
        # worker's or the download process's crash file (rebase onto main,
        # 26 Oct 2026: a search of the whole tree, review round 3 of #335).
        # The workspace lock and the launcher's record too, now on the list
        # (review of #335; offboarding named the lock).
        literals = ('"settings.json"', '"settings.unreadable.json"', '"power_restore.json"',
                    '/ "logs"', '/ "audit"', '"workspace.db', '"app.running"', '"app.log"',
                    '.crash"', '"instance.lock"', '"launch.json"')
        for rel in ("workspace/app.py", "workspace/__main__.py", "workspace/offboard.py",
                    "workspace/launch.py", "workspace/worker.py", "workspace/drafts.py",
                    "workspace/setup.py", "tools/packaged_check.py",
                    "tools/packaged_crash_check.py", "tools/perf_check.py",
                    "tools/durable_core_check.py"):
            source = (ROOT / rel).read_text(encoding="utf-8")
            for literal in literals:
                with self.subTest(rel=rel, literal=literal):
                    self.assertFalse(literal in source, f"{literal} named in {rel}")
        # Where each name is defined: once, in diag.py (the lid's record in
        # power.py), and nowhere after the definitions.
        from workspace import diag, power
        diag_source = (ROOT / "workspace" / "diag.py").read_text(encoding="utf-8")
        for name, value in (("DB_FILE", "workspace.db"), ("SETTINGS_FILE", "settings.json"),
                            ("SETTINGS_UNREADABLE_FILE", "settings.unreadable.json"),
                            ("LOGS_FOLDER", "logs"), ("AUDIT_FOLDER", "audit"),
                            ("LOG_SUFFIX", ".log"), ("RUNNING_SUFFIX", ".running"),
                            ("CRASH_SUFFIX", ".crash")):
            with self.subTest(constant=name):
                self.assertEqual(value, getattr(diag, name))
                self.assertEqual(1, diag_source.count(f'\n{name} = "{value}"'))
        power_source = (ROOT / "workspace" / "power.py").read_text(encoding="utf-8")
        self.assertEqual(1, power_source.count('"power_restore.json"'))   # RESTORE_FILE
        self.assertEqual("power_restore.json", power.RESTORE_FILE)
        after = diag_source.split('CRASH_SUFFIX = ".crash"', 1)[1]
        for literal in ('.log"', '.crash"', '.running"', '/ "logs"', '"workspace.db'):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, after)
        # residue.dumps() counts crash files without importing the app.
        self.assertEqual(diag.CRASH_SUFFIX, residue.CRASH_SUFFIX)

    def test_the_powershell_scripts_name_the_same_files(self):
        # PowerShell cannot import the constants, so its copies are tied to
        # them here: a rename that missed the uninstaller would quietly skip
        # the lid's restore, or the note that the workspace was kept (review
        # round 2 of #335).
        from workspace import diag, power
        for rel, needles in (
                ("packaging/uninstall.ps1",
                 (f'"workspace", "{power.RESTORE_FILE}")',
                  f'"Unminute\\workspace\\{diag.DB_FILE}"')),
                ("tools/import_dialog_check.ps1",
                 (f"'Unminute\\workspace\\{diag.SETTINGS_FILE}'",
                  f"'Unminute\\workspace\\{diag.DB_FILE}'"))):
            source = (ROOT / rel).read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(rel=rel, needle=needle):
                    self.assertIn(needle, source)
        with mock.patch.object(power, "RESTORE_FILE", "lid.json"):
            source = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
            self.assertNotIn(f'"workspace", "{power.RESTORE_FILE}")', source)

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

    def test_a_dump_folder_that_cannot_be_read_is_named_not_counted_as_none(self):
        (self.root / "WER").mkdir()
        refused = self.root / "WER"
        real_scandir = os.scandir

        def scandir(path=".", *a, **k):
            if Path(os.fspath(path)) == refused:
                raise PermissionError(13, "Access is denied", os.fspath(path))
            return real_scandir(path, *a, **k)
        with mock.patch.object(os, "scandir", scandir):
            out = residue.system_dumps(time.time() - 60,
                                       folders=[refused, self.root / "missing"])
        self.assertEqual(0, out["new_files"])
        self.assertEqual(2, out["folders"])
        self.assertEqual(0, out["folders_read"])
        self.assertEqual(1, len(out["unreadable"]))
        self.assertTrue(out["unreadable"][0].endswith("/WER"))
        self.assertEqual(1, len(out["missing"]))
        # The label never holds the whole path (the user's profile).
        self.assertNotIn(self.root.as_posix(), out["unreadable"][0])

    def test_the_system_folders_come_from_the_real_profile(self):
        folders = residue.system_dump_folders({"LOCALAPPDATA": r"C:\Users\u\AppData\Local",
                                               "ProgramData": r"C:\ProgramData"})
        self.assertIn(Path(r"C:\Users\u\AppData\Local") / "CrashDumps", folders)
        self.assertEqual(5, len(folders))


class OutsideTests(TempDir):
    """Folders outside the throwaway profile (review of #335; the owner's
    decision, 26 Oct 2026): the real ones compared by name, size and time
    only, never opened; anything new that nothing explains fails."""

    def test_listing_reads_names_sizes_and_times_never_contents(self):
        self.put("cache/a.bin", b"abc")
        self.put("cache/sub/b.bin", b"de")
        with mock.patch("builtins.open", side_effect=AssertionError("opened")), \
                mock.patch("io.open", side_effect=AssertionError("opened")):
            out = residue.listing(self.root / "cache", [])
        self.assertEqual({"a.bin", "sub/b.bin"}, set(out))
        self.assertEqual(3, out["a.bin"][0])

    def test_new_and_changed_files_are_named(self):
        places = {"model_cache": self.root / "cache", "LocalLow": self.root / "low"}
        self.put("cache/kept.bin", b"same")
        changed = self.put("cache/changed.bin", b"one")
        self.put("low/old.dat", b"x")
        before = residue.snapshot(places)
        changed.write_bytes(b"two!")
        self.put("cache/new.bin")
        self.put("low/Microsoft/CryptnetUrlCache/Content/ABC", b"cert")
        self.put("low/SomeApp/new.dat")
        out = residue.compare(before, places, residue.names())
        self.assertEqual({"model_cache": ["changed.bin", "new.bin"],
                          "LocalLow": ["Microsoft/CryptnetUrlCache/Content/ABC",
                                       "SomeApp/new.dat"]}, out["new"])
        self.assertEqual(["LocalLow/SomeApp/new.dat", "model_cache/changed.bin",
                          "model_cache/new.bin"], out["unexplained"])
        self.assertEqual([], out["unreadable"])
        # An earlier comparison's names are kept (made, then removed again).
        later = residue.compare(before, places, residue.names(),
                                earlier={"new": {"LocalLow": ["Gone/x.dat"]},
                                         "unreadable": []})
        self.assertIn("Gone/x.dat", later["new"]["LocalLow"])
        self.assertIn("LocalLow/Gone/x.dat", later["unexplained"])

    def test_model_verification_records_are_explained(self):
        from workspace import asr_engines
        n = residue.names()
        for name in (f"whisper/small/{asr_engines.VERIFIED_MARKER}",
                     asr_engines.VERIFIED_MARKER, "pyannote/.verified/seg-abc.json"):
            with self.subTest(name=name):
                self.assertIsNotNone(residue.explain("model_cache", name, n))
        for name in ("whisper/small/model.bin", "pyannote/.verified/seg.bin",
                     "notes.txt"):
            with self.subTest(name=name):
                self.assertIsNone(residue.explain("model_cache", name, n))
        # Nothing is expected in the redirected folders.
        for place in ("TEMP", "APPDATA", "USERPROFILE"):
            self.assertIsNone(residue.explain(place, "x.tmp", n))

    def test_left_in_names_every_file_in_the_redirected_folders(self):
        places = {"TEMP": self.root / "temp", "APPDATA": self.root / "roaming",
                  "USERPROFILE": self.root / "home"}
        for folder in places.values():
            folder.mkdir()
        self.put("temp/tmpab12.wav")
        self.put("home/.cache/lib/x.bin")
        out = residue.left_in(places, self.root)
        self.assertEqual({"TEMP": ["tmpab12.wav"], "APPDATA": [],
                          "USERPROFILE": [".cache/lib/x.bin"]}, out["files"])
        self.assertEqual(["TEMP/tmpab12.wav", "USERPROFILE/.cache/lib/x.bin"],
                         out["unexplained"])

    def test_the_real_places_are_locallow_and_the_model_cache(self):
        places = residue.real_places("models", {"USERPROFILE": "profile"})
        self.assertEqual({"model_cache": Path("models"),
                          "LocalLow": Path("profile") / "AppData" / "LocalLow"}, places)


class RedactTests(TempDir):
    HOME = "C:\\Users\\Pat.Example"
    ENV = {"USERPROFILE": HOME, "USERNAME": "Pat.Example"}

    def test_the_base_the_home_folder_and_the_user_name_are_replaced(self):
        base = self.HOME + "\\Temp\\umpkg-1x2y"
        text = "\n".join([
            f'File "{base}\\lifecycle.py", line 3',
            base.replace("\\", "/") + "/temp/a.wav",
            repr(base + "\\x"),
            "c:\\users\\pat.example\\Downloads\\clip.wav",
            "account pat.example refused"])
        out = residue.redact(text, base, self.ENV)
        self.assertNotIn("pat.example", out.lower())
        self.assertNotIn("umpkg-1x2y", out)
        self.assertIn("<base>\\lifecycle.py", out)
        self.assertIn("<base>/temp/a.wav", out)
        self.assertIn("'<base>\\\\x'", out)
        self.assertIn("<home>\\Downloads\\clip.wav", out)
        self.assertIn("account <user> refused", out)

    def test_nothing_to_replace_is_left_as_it_is(self):
        self.assertEqual("plain error", residue.redact("plain error", None, {}))
        self.assertEqual("", residue.redact("", "x", self.ENV))
        self.assertIsNone(residue.redact(None, "x", self.ENV))

    def test_the_short_form_is_replaced_too(self):
        short = "C:\\Users\\PATEXA~1"
        with mock.patch.object(residue, "_short",
                               lambda p: short if str(p) == self.HOME else str(p)):
            out = residue.redact(short + "\\Temp\\x", None, {"USERPROFILE": self.HOME})
        self.assertEqual("<home>\\Temp\\x", out)


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
                # No listing that skips a folder it cannot read (review of #335).
                for listing in ("profile.rglob(", "temp.rglob(", '"profile"].rglob('):
                    self.assertNotIn(listing, source)


if __name__ == "__main__":
    unittest.main()
