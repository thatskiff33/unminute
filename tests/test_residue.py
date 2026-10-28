# tests/test_residue.py
# What the packaged checks look for after #111 phase C sealed the content
# (tools/residue.py; #111 C7): the finder and its positive control, the files
# allowed after final cleanup (built from the app's constants) and the crash
# dump count, which reads names and sizes only and nothing outside its base
# unless a person asks. Synthetic files only.

import json
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


class WindowFilesTests(TempDir):
    """The window step of tools/packaged_check.py (#418): the WebView2
    profile, the real start's own files, the rest of the redirected
    LOCALAPPDATA and the Event Log entries, from the app's constants."""

    def setUp(self):
        super().setUp()
        self.n = residue.names()
        self.ws = self.root / "Unminute" / "workspace"
        self.ws.mkdir(parents=True)

    def test_the_names_follow_the_app_s_constants(self):
        from workspace import diag, instance, shell
        self.assertEqual(("instance.lock", "launch.json", "webview"),
                         (self.n["lock"], self.n["launch"], self.n["webview"]))
        with mock.patch.object(instance, "NAME", "held.lock"), \
                mock.patch.object(diag, "LAUNCH_FILE", "start.json"), \
                mock.patch.object(shell, "STORAGE_FOLDER", "profile"):
            n = residue.names()
        self.assertEqual(("held.lock", "start.json", "profile"),
                         (n["lock"], n["launch"], n["webview"]))
        # The app makes its profile there (workspace/shell.py storage_folder).
        self.assertEqual(self.ws / self.n["webview"], shell.storage_folder(self.ws).parent)
        source = (ROOT / "workspace" / "shell.py").read_text(encoding="utf-8")
        self.assertEqual(1, source.count('STORAGE_FOLDER = "webview"'))
        self.assertIn("Path(root) / STORAGE_FOLDER", source)

    def test_profile_files_are_named_never_opened(self):
        profile = self.ws / "webview" / ("c" * 32) / "EBWebView" / "Default"
        profile.mkdir(parents=True)
        (profile / "Preferences").write_bytes(b"zebra")
        (self.ws / "settings.json").write_bytes(b"{}")         # not the profile
        with mock.patch("builtins.open", side_effect=AssertionError("opened")):
            files, refused = residue.profile_files(self.ws, self.n)
        self.assertEqual((["webview/" + "c" * 32 + "/EBWebView/Default/Preferences"], []),
                         (files, refused))
        self.assertEqual(([], []), residue.profile_files(self.root / "none", self.n))

    def test_the_profile_s_files_are_not_the_workspace_s_own(self):
        # The list is not widened (E2): a profile file left after final
        # cleanup fails "only the workspace's own files".
        for rel in ("webview/" + "d" * 32 + "/EBWebView/Local State", "webview/x"):
            with self.subTest(rel=rel):
                self.assertFalse(residue.allowed(rel, self.n))

    def test_the_wipe_is_the_app_s_next_start(self):
        from workspace import shell
        for rel in ("a/EBWebView/Default/Cookies", "b/Crashpad/settings.dat"):
            self.put(f"Unminute/workspace/webview/{rel}")
        with mock.patch.object(shell, "storage_folder",
                               wraps=shell.storage_folder) as wipe:
            self.assertEqual({"files_before": 2, "files_left": 0},
                             residue.wipe_profile(self.ws, self.n))
        wipe.assert_called_once_with(self.ws)
        self.assertEqual([], residue.unexpected(self.ws.parent, self.ws, self.n))

    def test_a_profile_the_wipe_cannot_remove_is_counted_and_fails(self):
        from workspace import shell
        self.put("Unminute/workspace/webview/a/EBWebView/Default/Cookies")
        for effect in (lambda root: None, shell.ShellError("in use")):
            with self.subTest(effect=effect), \
                    mock.patch.object(shell, "storage_folder", side_effect=effect):
                self.assertEqual({"files_before": 1, "files_left": 1},
                                 residue.wipe_profile(self.ws, self.n))
        self.assertEqual(["workspace/webview/a/EBWebView/Default/Cookies"],
                         residue.unexpected(self.ws.parent, self.ws, self.n))

    def test_the_start_s_own_files(self):
        from workspace import diag, instance
        self.assertEqual({"instance_lock": False, "launch_record": False},
                         residue.start_files(self.ws, self.n))
        held = instance.acquire(self.ws)
        try:
            (self.ws / diag.LOGS_FOLDER).mkdir()
            (self.ws / diag.LOGS_FOLDER / diag.LAUNCH_FILE).write_text("{}", encoding="utf-8")
            self.assertEqual({"instance_lock": True, "launch_record": True},
                             residue.start_files(self.ws, self.n))
            # Both are the workspace's own files after final cleanup.
            self.assertEqual([], residue.unexpected(self.ws.parent, self.ws, self.n))
        finally:
            held.release()

    def test_the_rest_of_localappdata_is_scanned_and_named(self):
        keep = {"Unminute", "temp", "roaming", "home", "lifecycle.py", "windows.bin"}
        for rel in ("Unminute/workspace/workspace.db", "temp/a", "windows.bin", "lifecycle.py",
                    "Microsoft/EdgeWebView/x.dat", "stray.bin"):
            self.put(rel, b"zebra" if rel == "stray.bin" else b"x")
        rest = residue.rest_of(self.root, keep)
        self.assertEqual([self.root / "Microsoft", self.root / "stray.bin"], rest)
        self.assertEqual(["stray.bin"],
                         residue.find(rest, {"m": b"zebra"}, self.root)["hits"]["m"])
        left = residue.left_in({"TEMP": self.root / "temp"}, self.root, rest=rest)
        self.assertEqual({"TEMP": ["a"], residue.REST: ["Microsoft/EdgeWebView/x.dat",
                                                         "stray.bin"]}, left["files"])
        # Nothing explains a file there: each fails the report.
        self.assertEqual(["LOCALAPPDATA/Microsoft/EdgeWebView/x.dat", "LOCALAPPDATA/stray.bin",
                          "TEMP/a"], left["unexplained"])
        self.assertNotIn(residue.REST, residue.EXPLAINED)
        # Without rest, as before.
        self.assertEqual({"TEMP": ["a"]},
                         residue.left_in({"TEMP": self.root / "temp"}, self.root)["files"])

    def lend(self):
        cache = self.root / "cache"
        weights = self.put("cache/parakeet/v3/rev/model.onnx", b"w" * residue.LINK_FROM)
        record = self.put("cache/parakeet/v3/rev/.verified.json", b'{"ok": true}')
        self.put("cache/whisper/.verified/small-ab.json", b"{}")
        target = self.ws.parent / "models"
        lent, counts = residue.lend_models(cache, target)
        return cache, target, weights, record, lent, counts

    def test_models_are_lent_weights_linked_and_records_copied(self):
        cache, target, weights, record, lent, counts = self.lend()
        self.assertEqual({"linked": 1, "copied": 2, "unreadable": []}, counts)
        self.assertEqual(sorted(["parakeet/v3/rev/model.onnx", "parakeet/v3/rev/.verified.json",
                                 "whisper/.verified/small-ab.json"]), sorted(lent))
        self.assertTrue(os.path.samefile(weights, target / "parakeet/v3/rev/model.onnx"))
        lent_record = target / "parakeet/v3/rev/.verified.json"
        self.assertFalse(os.path.samefile(record, lent_record))
        self.assertEqual(record.stat().st_mtime_ns, lent_record.stat().st_mtime_ns)
        # A record the app rewrites in place is the throwaway copy only.
        lent_record.write_bytes(b'{"ok": false}')
        self.assertEqual(b'{"ok": true}', record.read_bytes())

    def test_a_weight_a_link_cannot_reach_is_copied(self):
        with mock.patch.object(residue.os, "link", side_effect=OSError(17, "another drive")):
            cache, target, weights, _, _, counts = self.lend()
        self.assertEqual({"linked": 0, "copied": 3, "unreadable": []}, counts)
        self.assertFalse(os.path.samefile(weights, target / "parakeet/v3/rev/model.onnx"))

    def test_models_are_taken_back_and_what_was_added_named(self):
        cache, target, weights, record, lent, _ = self.lend()
        (target / "parakeet/v3/rev/.verified.json").write_bytes(b'{"ok": true, "x": 1}')
        (target / "parakeet/v3/rev/stray.bin").write_bytes(b"x")
        back = residue.return_models(target, lent, residue.names())
        self.assertEqual(["parakeet/v3/rev/.verified.json", "parakeet/v3/rev/stray.bin"],
                         back["new"])
        # A verification record is explained (EXPLAINED's model cache
        # entries); anything else is not.
        self.assertEqual(["model_cache/parakeet/v3/rev/stray.bin"], back["unexplained"])
        self.assertEqual((True, []), (back["removed"], back["unreadable"]))
        self.assertFalse(target.exists())
        # The cache's own files are untouched: a link removed is only a link.
        self.assertEqual((b"w" * residue.LINK_FROM, b'{"ok": true}'),
                         (weights.read_bytes(), record.read_bytes()))

    def test_a_models_folder_already_there_is_refused_and_left_alone(self):
        # Fail closed (review of #467): return_models() removes the whole
        # folder, so one that was there before is never lent into, and
        # neither it nor the cache is touched.
        cache = self.root / "cache"
        self.put("cache/parakeet/v3/rev/model.onnx", b"w" * residue.LINK_FROM)
        target = self.ws.parent / "models"
        kept = self.put("Unminute/models/kept.bin", b"k")
        with self.assertRaises(FileExistsError) as caught:
            residue.lend_models(cache, target)
        self.assertNotIn(str(self.root), str(caught.exception))       # P3: no path
        self.assertEqual(["kept.bin"], sorted(p.name for p in target.iterdir()))
        self.assertEqual(b"k", kept.read_bytes())
        # An empty folder, or a file by that name, is refused too.
        kept.unlink()
        with self.assertRaises(FileExistsError):
            residue.lend_models(cache, target)
        target.rmdir()
        target.write_bytes(b"")
        with self.assertRaises(FileExistsError):
            residue.lend_models(cache, target)
        self.assertEqual(b"", target.read_bytes())

    def test_nothing_to_lend_is_nothing_lent(self):
        lent, counts = residue.lend_models(self.root / "none", self.ws.parent / "models")
        self.assertEqual(({}, {"linked": 0, "copied": 0, "unreadable": []}), (lent, counts))

    def test_a_base_that_cannot_be_listed_is_scanned_whole(self):
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            self.assertEqual([self.root], residue.rest_of(self.root, {"Unminute"}))


class EventLogCountTests(TempDir):
    """residue.event_log_entries (#418): only entries naming a chain of this
    workspace's audit file count; nothing of an entry is returned."""

    class Mirror:
        def __init__(self):
            self.texts = []

        def report(self, event_id, level, text):
            self.texts.append(text)

    def chain(self, folder, records):
        from workspace import audit
        mirror = self.Mirror()
        log = audit.AuditLog(folder, mirror=mirror, strict=True)
        for _ in range(records):
            log.record("damage_continued")
        log.close()
        return mirror.texts

    def xml(self, texts):
        from workspace import audit
        ns = "http://schemas.microsoft.com/win/2004/08/events/event"
        body = "".join(f"<Event xmlns='{ns}'><System><Provider Name='{audit.SOURCE}'/>"
                       f"</System><EventData><Data>{t}</Data></EventData></Event>"
                       for t in texts)
        return f"<Events>{body}</Events>"

    def test_only_this_workspace_s_entries_count(self):
        ours = self.chain(self.root / "ours", 3)
        theirs = self.chain(self.root / "theirs", 4)      # the real app's, say
        self.assertTrue(ours and theirs)
        xml = self.xml(theirs[:2] + ours + theirs[2:] + ["someone else's text"])
        self.assertEqual(len(ours), residue.event_log_entries(
            self.root / "ours", read=lambda source, provider: xml))
        self.assertEqual(0, residue.event_log_entries(
            self.root / "ours", read=lambda source, provider: self.xml(theirs)))
        self.assertEqual(0, residue.event_log_entries(
            self.root / "ours", read=lambda source, provider: self.xml([])))

    def test_a_log_that_cannot_be_read_is_not_a_count(self):
        self.chain(self.root / "ours", 1)

        def refused(source, provider):
            raise OSError("wevtutil could not read the log")
        self.assertIsNone(residue.event_log_entries(self.root / "ours", read=refused))


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
        # The report names each EXPLAINED entry it relied on (the owner's
        # decision, 26 Oct 2026).
        self.assertEqual({"LocalLow: Microsoft/CryptnetUrlCache/.+": 1}, out["explained"])
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

    def test_the_explained_records_are_where_the_app_writes_them(self):
        # Tied to workspace/asr_engines.py's own paths, not a copy of their
        # layout (review of #335): a managed model's marker, and the record
        # kept for a Hugging Face copy outside the cache's own folders.
        from workspace import asr_engines
        n, cache = residue.names(), self.root / "models"
        for name, spec in asr_engines.MANAGED_MODELS.items():
            paths = [asr_engines.model_dir(spec, cache) / asr_engines.VERIFIED_MARKER]
            if spec.hub_repos:
                paths.append(asr_engines._marker_for(spec, cache, self.root / "hub" / name))
            for path in paths:
                rel = path.relative_to(cache).as_posix()
                with self.subTest(model=name, record=rel):
                    self.assertIsNotNone(residue.explain("model_cache", rel, n))

    def test_a_folder_unreadable_before_the_run_is_still_reported(self):
        # Named even when it can be listed again afterwards (review of #335).
        places = {"LocalLow": self.root / "low"}
        self.put("low/a.dat")
        before = (residue.snapshot(places)[0], ["LocalLow/Locked/"])
        out = residue.compare(before, places)
        self.assertEqual(["LocalLow/Locked/"], out["unreadable"])
        self.assertEqual({"LocalLow": []}, out["new"])

    def test_a_file_stat_cannot_read_is_reported_not_skipped(self):
        self.put("cache/a.bin")
        self.put("cache/sub/locked.bin")
        real_stat = os.stat

        def stat(path, *a, **k):
            if os.fspath(path).endswith("locked.bin"):
                raise PermissionError(13, "Access is denied", os.fspath(path))
            return real_stat(path, *a, **k)
        unreadable = []
        with mock.patch.object(residue.os, "stat", stat):
            out = residue.listing(self.root / "cache", unreadable, self.root)
        self.assertEqual({"a.bin"}, set(out))
        self.assertEqual(["cache/sub/locked.bin"], unreadable)

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
        self.assertEqual({}, out["explained"])

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

    def test_a_macronised_name_is_replaced_however_it_is_written(self):
        # Synthetic name. JSON escapes it (T\u0101ne) and a pipe that cannot
        # hold it backslash-escapes it; both used to get through (review of
        # #335).
        home = "C:\\Users\\Tāne.Example"
        env = {"USERPROFILE": home, "USERNAME": "Tāne.Example"}
        base = home + "\\AppData\\Local\\Temp\\umpkg-9z"
        result = {"error": f"PermissionError: {home}\\Downloads\\a.wav; user Tāne.Example",
                  "opened": f"{base}\\Unminute\\x", "share": "D:\\Shares\\Tāne.Example\\y"}
        as_json = json.dumps(result)
        self.assertIn("\\u0101", as_json)
        piped = (f"{home}\\x\nfor TĀNE.EXAMPLE".encode("ascii", "backslashreplace")
                 .decode("ascii"))
        for text in (as_json, piped, f"{home}\\x tāne.example"):
            with self.subTest(text=text):
                out = residue.redact(text, base, env)
                self.assertNotIn("example", out.lower())
                self.assertNotIn("tāne", out.lower())
        decoded = residue.redact_json(json.loads(as_json), base, env)
        self.assertEqual({"error": "PermissionError: <home>\\Downloads\\a.wav; user <user>",
                          "opened": "<base>\\Unminute\\x",
                          "share": "D:\\Shares\\<user>\\y"}, decoded)

    def test_a_short_user_name_is_replaced_only_in_a_path_or_an_error(self):
        env = {"USERPROFILE": "C:\\Users\\ope", "USERNAME": "ope"}
        result = {"opens": "opens", "note": "rope ope", "files": ["D:\\Shares\\ope\\x.wav"],
                  "key_error": "user ope refused; scope", "D:/ope/k": 1}
        out = residue.redact_json(result, None, env)
        self.assertEqual({"opens": "opens", "note": "rope ope",
                          "files": ["D:\\Shares\\<user>\\x.wav"],
                          "key_error": "user <user> refused; scope", "D:/<user>/k": 1}, out)
        # Error text: the name as a word, never inside one.
        self.assertEqual("<user> opens a scope",
                         residue.redact("ope opens a scope", None, env))

    def test_the_name_ends_at_anything_but_a_letter(self):
        # Synthetic name. "_", "-", "." and a digit end the name as a word, so
        # torch's torchinductor_<user> and a library's cache-<user>.bin lose
        # it; a longer word that holds it does not (review of 387f8d3).
        env = {"USERPROFILE": "C:\\Users\\Tāne.Example", "USERNAME": "Tāne.Example"}
        for text, want in (("torchinductor_Tāne.Example", "torchinductor_<user>"),
                           ("cache-TĀNE.EXAMPLE.bin", "cache-<user>.bin"),
                           ("Tāne.Example_cache 2tāne.example3", "<user>_cache 2<user>3"),
                           ("Tāne.Examples", "Tāne.Examples")):
            with self.subTest(text=text):
                self.assertEqual(want, residue.redact(text, None, env))
        # A result's path values too, wherever the name stands in them.
        result = {"temp_files": ["torchinductor_Tāne.Example/x.py",
                                 "cache\\cache-Tāne.Example.bin"],
                  "D:/torchinductor_Tāne.Example": 1}
        self.assertEqual({"temp_files": ["torchinductor_<user>/x.py",
                                         "cache\\cache-<user>.bin"],
                          "D:/torchinductor_<user>": 1},
                         residue.redact_json(result, None, env))

    def test_free_text_in_a_result_is_left_to_the_guard(self):
        # A value that is not a path, under a key that names no error, keeps
        # the name in redact_json(); guarded() then fails the report on it
        # (review of 387f8d3).
        env = {"USERPROFILE": "C:\\Users\\Tāne.Example", "USERNAME": "Tāne.Example"}
        result = {"detail": "denied for Tāne.Example", "opens": "opens"}
        out = residue.redact_json(result, None, env)
        self.assertEqual(result, out)
        self.assertEqual([["detail"]], residue.user_name_at(out, env))
        self.assertFalse(residue.guarded(out, env)["pass"])

    def test_the_short_form_is_replaced_too(self):
        short = "C:\\Users\\PATEXA~1"
        with mock.patch.object(residue, "_short",
                               lambda p: short if str(p) == self.HOME else str(p)):
            out = residue.redact(short + "\\Temp\\x", None, {"USERPROFILE": self.HOME})
        self.assertEqual("<home>\\Temp\\x", out)


class GuardTests(unittest.TestCase):
    """guarded(): the last step before a packaged check writes its report. A
    report that would carry the Windows user name anywhere fails, naming key
    paths only (P3; review of 387f8d3). Synthetic names only."""

    NAME = "Tāne.Example"
    ENV = {"USERPROFILE": "C:\\Users\\Tāne.Example", "USERNAME": "Tāne.Example"}

    def test_a_clean_report_is_written_as_it_is(self):
        report = {"when": "2026-10-26 20:00", "pass": True, "failed": [],
                  "residual": {"temp_files_left": ["tmpab12.wav"], "opened": "<user>",
                               "home": "<home>\\x", "base": "<base>/y"}}
        self.assertIs(report, residue.guarded(report, self.ENV))
        self.assertEqual([], residue.user_name_at(report, self.ENV))

    def test_the_name_in_any_form_or_place_fails_the_report(self):
        name = self.NAME
        forms = {"plain": f"temp/torchinductor_{name}/x.py",
                 "capitals": f"cache-{name.upper()}.bin",
                 "json": "tail " + json.dumps(name)[1:-1],
                 "backslashreplace": name.encode("ascii", "backslashreplace").decode(),
                 "upper hex": "T\\u0101NE.EXAMPLE",
                 "digit": f"2{name}3"}
        for label, text in forms.items():
            with self.subTest(form=label):
                report = {"when": "w", "pass": True, "failed": [],
                          "residual": {"outside_new": {"LocalLow": ["a", text]}}}
                out = residue.guarded(report, self.ENV)
                self.assertEqual({"when": "w", "pass": False,
                                  "failed": [residue.CARRIES_USER_NAME],
                                  "user_name_at": [["residual", "outside_new", "LocalLow", 1]]},
                                 out)

    def test_a_key_carrying_the_name_is_named_without_it(self):
        report = {"pass": False, "failed": ["no error"],
                  "left_explained": {f"TEMP: torchinductor_{self.NAME}": 1}}
        out = residue.guarded(report, self.ENV)
        self.assertEqual([["left_explained", "<key>"]], out["user_name_at"])
        self.assertEqual([residue.CARRIES_USER_NAME, "no error"], out["failed"])
        self.assertEqual([], residue.user_name_at(out, self.ENV))
        self.assertNotIn("tāne", json.dumps(out, ensure_ascii=False).lower())

    def test_the_profile_folder_name_counts_too(self):
        env = {"USERPROFILE": "C:\\Users\\Pat.Other", "USERNAME": "Pat.Example"}
        self.assertEqual({"Pat.Other", "Pat.Example"}, residue.user_names(env))
        self.assertEqual([["x"]], residue.user_name_at({"x": "pat.other-1"}, env))

    def test_a_short_name_inside_a_word_is_not_the_name(self):
        env = {"USERPROFILE": "C:\\Users\\ope", "USERNAME": "ope"}
        self.assertEqual([], residue.user_name_at({"audio_key": "opens", "rope": "<user>"}, env))
        self.assertEqual([["a"]], residue.user_name_at({"a": "ope_cache"}, env))
        # Too short to look for, as redact() does.
        self.assertEqual([], residue.user_name_at({"a": "jo"}, {"USERNAME": "jo"}))

    def test_a_name_inside_a_hash_or_a_commit_id_is_not_the_name(self):
        # Synthetic names made only of the letters a to f, which turn up
        # inside a SHA-256, a commit id or a model's verification key between
        # two digits (review of 78f17ee, #419).
        for name in ("Dee", "Abe"):
            low = name.lower()
            env = {"USERPROFILE": f"C:\\Users\\{name}", "USERNAME": name}
            core = f"1{low}2"
            sha, commit, key = ((core + "0" * 64)[:64], ("9" * 20 + core + "9" * 40)[:40],
                                ("3" + core + "3" * 16)[:16])
            report = {"evidence": {"package_sha256": sha, "commit": commit,
                                   "folder_differences_listed": [f"changed: app/{sha.upper()}"]},
                      "outside_new": {"model_cache": [f"m/.verified/large-v3-{key}.json"]}}
            with self.subTest(name=name):
                self.assertEqual([], residue.user_name_at(report, env))
                self.assertIs(report, residue.guarded(report, env))
            # The name beside a hash, in a short commit id, or in a run that
            # is not all hex digits (one more letter, or mixed case) is
            # still the name.
            for text in (f"C:/Users/{name}/x", f"{low}_{sha}", f"{sha}/{low}", f"{sha} {name}",
                         f"commit 7{low}3a9", f"x{sha}", sha.replace(low, name),
                         f"{key}{low}"[:-1] + "g"):
                with self.subTest(name=name, text=text):
                    self.assertEqual([["t"]], residue.user_name_at({"t": text}, env))

    def test_the_profile_folder_s_short_name_counts_too(self):
        # Synthetic. A path can carry the 8.3 form of the profile folder's
        # name in place of the long one (review of 78f17ee, #419).
        env = {"USERPROFILE": "C:\\Users\\Pat.Example", "USERNAME": "Pat.Example"}
        short = "C:\\Users\\PATEXA~1"
        with mock.patch.object(residue, "_short",
                               lambda p: short if str(p) == env["USERPROFILE"] else str(p)):
            self.assertEqual({"Pat.Example", "PATEXA~1"}, residue.user_names(env))
            for text in (short + "\\AppData\\Local\\Temp\\x", "c:/users/patexa~1/x",
                         json.dumps(short)):
                with self.subTest(text=text):
                    self.assertEqual([["t"]], residue.user_name_at({"t": text}, env))
                    self.assertFalse(residue.guarded({"t": text}, env)["pass"])
            self.assertEqual([], residue.user_name_at({"t": "XPATEXA~1 or PATEXA~2"}, env))

    def test_a_decomposed_or_url_encoded_name_fails_the_report(self):
        # Synthetic. A macron as a letter and a combining mark (NFD), and a
        # file:// address, used to get through (review of 78f17ee, #419).
        import unicodedata
        from urllib.parse import quote
        nfd = unicodedata.normalize("NFD", self.NAME)
        self.assertNotEqual(nfd, self.NAME)
        forms = {"nfd": f"temp/torchinductor_{nfd}/x.py",
                 "nfd capitals": f"cache-{nfd.upper()}.bin",
                 "nfd json": "tail " + json.dumps(nfd)[1:-1],
                 "nfd backslashreplace": nfd.encode("ascii", "backslashreplace").decode(),
                 "url": "file:///C:/Users/T%C4%81ne.Example/AppData/x",
                 "url small letters": "file:///c:/users/t%c4%81ne.example/x",
                 "url nfd": "file:///C:/Users/" + quote(nfd) + "/x"}
        for label, text in forms.items():
            with self.subTest(form=label):
                self.assertEqual([["t"]], residue.user_name_at({"t": text}, self.ENV))
        # The environment's name decomposed finds the composed one too.
        self.assertEqual([["t"]], residue.user_name_at({"t": f"x/{self.NAME}/y"},
                                                       {"USERNAME": nfd}))
        # A name with a space, as an address writes it.
        env = {"USERPROFILE": "C:\\Users\\Pat Example", "USERNAME": "Pat Example"}
        for text in ("file:///C:/Users/Pat%20Example/x", "q=Pat+Example&x=1"):
            with self.subTest(text=text):
                self.assertEqual([["t"]], residue.user_name_at({"t": text}, env))
        # redact() replaces these forms in error text too.
        self.assertEqual("denied for <user> at <user>",
                         residue.redact(f"denied for {nfd} at T%C4%81ne.Example", None,
                                        self.ENV))

    def test_write_guarded_writes_the_report_or_its_failing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            clean = {"when": "w", "views": {"meetings": {"ok": True}}}
            self.assertIs(clean, residue.write_guarded(path, clean, self.ENV))
            self.assertEqual(clean, json.loads(path.read_text(encoding="utf-8")))
            named = {"when": "w", "error": f"denied for {self.NAME}"}
            written = residue.write_guarded(path, named, self.ENV)
            self.assertIsNot(named, written)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(written, json.loads(text))
            self.assertEqual([residue.CARRIES_USER_NAME], written["failed"])
            self.assertNotIn("tāne", text.lower())
            self.assertNotIn("\\u0101ne", text.lower())


class EveryReportIsGuardedTests(unittest.TestCase):
    """The performance, UI and accessibility reports go through the same name
    guard as the packaged checks' reports, and their errors lose the home
    folder and the user name first (review of 78f17ee, #419)."""

    def test_the_three_window_checks_write_through_the_guard(self):
        for tool, verdict in (("perf_check.py", "named = residue.write_guarded("),
                              ("workspace_ui_check.py", "bad.append(residue.CARRIES_USER_NAME)"),
                              ("accessibility_check.py",
                               "problems.append(residue.CARRIES_USER_NAME)")):
            source = (ROOT / "tools" / tool).read_text(encoding="utf-8")
            with self.subTest(tool=tool):
                self.assertIn("residue.write_guarded(", source)
                self.assertIn(verdict, source)
                # No report written past the guard, and no error copied raw.
                self.assertNotRegex(source, r"write_text\(json\.dumps\((report|written)")
                self.assertNotIn('report["error"] = f"', source)
                self.assertNotIn('report["error"] = results, str(exc)', source)


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
