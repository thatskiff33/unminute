# tests/test_launch.py
# The launcher pass and the workspace lock (#111 B5a); the first-start check,
# the full check before a migration, the "update waiting" message,
# finish-only mode, the --health fields and removing unused graphics card
# components (#111 B5b).
#   python tools/run_tests.py
#
# The launcher (workspace/launch.py, update.launch_decision and read_slots)
# decides, in machine mode only, which installed version opens a user's
# workspace, and runs the first-start check there and, since the owner's
# decision of 23 Oct 2026, in a per-user install too (PerUserCheckTests);
# the workspace lock (workspace/instance.py) keeps a second
# Windows session of the same user out. Synthetic installs and workspaces
# only; nothing here starts a real app or reads the user's workspace.

import contextlib
import errno
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import keyprotect  # noqa: E402

from workspace import diag, instance, launch, launchcheck, update  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av", "cryptography"))

MACHINE = lambda: {"version": "x", "mode": "machine"}        # noqa: E731
PER_USER = lambda: {"version": "x", "mode": "self-service"}  # noqa: E731


def record(version="3.0", previous=None, slots=None):
    """A current.json format 2 record as read_slots returns it."""
    slots = slots or {version: {"schema": 3, "seq": 9}}
    return {"format": 2, "version": version, "previous": previous, "slots": slots}


class DecisionTests(unittest.TestCase):
    """update.launch_decision: every row of the design's table, with the
    owner's choices of 21 Oct 2026 (strict downgrade, B5-8; bounded disk,
    B5-3, so an older format may have no version left)."""

    SLOTS = {"3.0": {"schema": 3, "seq": 9}, "2.1": {"schema": 2, "seq": 8},
             "2.0": {"schema": 2, "seq": 5}, "old": {"schema": None, "seq": 7}}

    def decide(self, workspace, blockers=False, slots=None, version="3.0"):
        return update.launch_decision(workspace, blockers,
                                      record(version, slots=slots or self.SLOTS))

    def test_no_workspace_or_the_same_format_opens_the_current_version(self):
        self.assertEqual(self.decide(None), ("3.0", "current", None))
        self.assertEqual(self.decide(3), ("3.0", "current", None))

    def test_an_older_empty_workspace_goes_to_the_current_version(self):
        self.assertEqual(self.decide(2, blockers=False), ("3.0", "current", None))

    def test_an_older_workspace_with_blockers_goes_to_the_newest_version_of_its_format(self):
        # 2.1 has the higher seq; versions are never compared by name.
        self.assertEqual(self.decide(2, blockers=True), ("2.1", "compat", None))
        slots = dict(self.SLOTS, **{"2.0": {"schema": 2, "seq": 10}})
        self.assertEqual(self.decide(2, blockers=True, slots=slots), ("2.0", "compat", None))

    def test_two_format_changes_behind_with_no_version_of_its_format_is_refused(self):
        # Bounded disk (B5-3): the version kept for format 1 was replaced at the
        # next change, so nothing may open this workspace.
        self.assertEqual(self.decide(1, blockers=True), (None, None, "blocked"))
        slots = dict(self.SLOTS, **{"1.9": {"schema": 1, "seq": 2}})
        self.assertEqual(self.decide(1, blockers=True, slots=slots), ("1.9", "compat", None))

    def test_a_workspace_from_a_newer_version_is_refused_even_if_that_version_is_kept(self):
        # Strict withdrawal (B5-8): never handed to a newer version.
        slots = dict(self.SLOTS, **{"4.0": {"schema": 4, "seq": 11}})
        self.assertEqual(self.decide(4, slots=slots), (None, None, "too_new"))
        self.assertEqual(self.decide(4), (None, None, "too_new"))

    def test_an_unreadable_workspace_opens_the_current_version_never_an_older_one(self):
        for found in (update.UNREADABLE, "2", 2.0, True, 0, -1):
            with self.subTest(found=found):
                self.assertEqual(self.decide(found, blockers=True), ("3.0", "current", None))

    def test_a_version_without_a_storage_format_is_never_a_target(self):
        # B5-9: "old" states no format, even if it is a release of a preview.
        slots = {"3.0": {"schema": 3, "seq": 9}, "0.5.0-preview.3": {"schema": None, "seq": 8}}
        self.assertEqual(self.decide(1, blockers=True, slots=slots), (None, None, "blocked"))
        self.assertIsNone(update.newest_of_schema(slots, None))

    def test_a_current_version_of_unknown_format_opens_as_before(self):
        slots = {"3.0": {"schema": None, "seq": 9}, "2.1": {"schema": 2, "seq": 8}}
        self.assertEqual(self.decide(2, blockers=True, slots=slots), ("3.0", "current", None))


class RecordTests(unittest.TestCase):
    """update.read_slots: current.json format 2 and its checks."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, data, raw=None):
        (self.root / "current.json").write_text(
            raw if raw is not None else json.dumps(data), encoding="utf-8-sig")

    def good(self):
        return {"format": 2, "version": "3.0", "path": "x", "previous": "2.1", "managed": True,
                "slots": {"3.0": {"schema": 3, "seq": 9, "installed": "2027-02-02T01:00:00Z"},
                          "2.1": {"schema": 2, "seq": 8, "installed": "2027-01-01T01:00:00Z"},
                          "0.5.0": {"seq": 1}}}

    def test_a_good_record_is_read(self):
        self.write(self.good())
        state, rec = update.read_slots(self.root)
        self.assertEqual(state, "ok")
        self.assertEqual(rec["slots"], {
            "3.0": {"schema": 3, "seq": 9, "installed": "2027-02-02T01:00:00Z"},
            "2.1": {"schema": 2, "seq": 8, "installed": "2027-01-01T01:00:00Z"},
            "0.5.0": {"schema": None, "seq": 1, "installed": None}})
        self.assertEqual((rec["version"], rec["previous"]), ("3.0", "2.1"))

    def test_no_file_and_format_1(self):
        self.assertEqual(update.read_slots(self.root), ("missing", None))
        self.write({"version": "0.5.0", "path": "x", "previous": None, "managed": False})
        state, rec = update.read_slots(self.root)
        self.assertEqual((state, rec["format"], rec["version"]), ("format1", 1, "0.5.0"))

    def test_any_bad_value_makes_the_record_damaged(self):
        bad_names = ["..", "..\\x", "C:\\x", "\\\\server\\x", "0.6.0.", "a/b", "1.0.partial",
                     "1.0.old", "", "x" * 65, "1.0\n", " 1.0"]
        cases = []
        for name in bad_names:
            g = self.good()
            g["slots"][name] = {"schema": 2, "seq": 3}
            cases.append((f"slot {name!r}", g))
            g = self.good()
            g["version"] = name
            cases.append((f"version {name!r}", g))
            g = self.good()
            g["previous"] = name
            cases.append((f"previous {name!r}", g))
        for value in (1.5, "1", True, 0, -1, None):
            g = self.good()
            g["slots"]["2.1"]["seq"] = value
            cases.append((f"seq {value!r}", g))
        for value in (1.5, "2", True, 0, -1):
            g = self.good()
            g["slots"]["2.1"]["schema"] = value
            cases.append((f"schema {value!r}", g))
        for key, value in (("format", 3), ("format", "2"), ("format", 2.0), ("format", True),
                           ("slots", []), ("slots", None)):
            g = self.good()
            g[key] = value
            cases.append((f"{key} {value!r}", g))
        g = self.good()
        g["slots"]["2.1"]["seq"] = 9                       # the same seq twice
        cases.append(("duplicate seq", g))
        g = self.good()
        del g["slots"]["3.0"]                              # current not listed
        cases.append(("current missing", g))
        g = self.good()
        g["slots"]["2.1"] = [2, 8]
        cases.append(("entry not an object", g))
        for label, data in cases:
            with self.subTest(label):
                self.write(data)
                self.assertEqual(update.read_slots(self.root), ("damaged", None))
        for label, raw in (("not JSON", "{"), ("a list", "[1]"),
                           ("too large", json.dumps(dict(self.good(), pad="x" * 70000))),
                           ("deeply nested", "[" * 100000)):
            with self.subTest(label):
                self.write(None, raw=raw)
                self.assertEqual(update.read_slots(self.root), ("damaged", None))

    def test_the_name_rules_match_the_installer(self):
        install = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        pattern = re.search(r"(?m)^\$VersionPattern = '\^([^']+)\\z'$", install).group(1)
        self.assertEqual(pattern, update._VERSION_NAME.pattern)
        self.assertIn("-not $Name.EndsWith(\".\")", install)
        self.assertIn("$Name -notmatch '\\.(partial|old|retired)\\z'", install)
        self.assertEqual(update._WORKING.pattern, r"\.(partial|old|retired)\Z")
        for name in ("1.0.0.partial", "1.0.0.old", "1.0.0.retired"):
            self.assertFalse(update.version_name(name), name)

    def test_the_launch_options_match_the_installer_and_the_uninstaller(self):
        install = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        args = " ".join(update.LAUNCH_ARGS)
        for script in (install, uninstall):
            self.assertIn(f'$LaunchArgs = "{args}"', script)
        # Every start uses that one list (#111 B6a).
        self.assertIn('$gateArgs = @($LaunchArgs -split " ") + "--gate"', install)
        self.assertIn('$start.Arguments = "$LaunchArgs --restore-lid"', uninstall)


class InstallLayout:
    """A synthetic install: <root>\\versions\\<v>\\{app\\workspace, runtime}
    with version.json, and current.json format 2."""

    def __init__(self, base, slots, current, previous=None, features=(), gpu=None):
        self.root = Path(base) / "Unminute"
        self.versions = self.root / "versions"
        self.features, self.gpu = list(features), gpu
        for name, schema in slots.items():
            self.version(name, schema)
        self.entries = {}
        for seq, (name, schema) in enumerate(slots.items(), 1):
            self.entries[name] = {"seq": seq, "installed": f"2027-01-{seq:02d}T01:00:00Z",
                                  **({"schema": schema} if schema else {})}
        self.current, self.previous = current, previous
        self.write()

    def write(self):
        (self.root / "current.json").write_text(json.dumps(
            {"format": 2, "version": self.current, "path": str(self.versions / self.current),
             "previous": self.previous, "managed": True, "slots": self.entries}),
            encoding="utf-8-sig")

    def version(self, name, schema, stated=None):
        slot = self.versions / name
        (slot / "app" / "workspace").mkdir(parents=True, exist_ok=True)
        (slot / "runtime").mkdir(exist_ok=True)
        (slot / "runtime" / "pythonw.exe").write_bytes(b"synthetic")
        info = {"name": "Unminute", "version": stated or name, "features": self.features}
        if schema:
            info["schema"] = schema
        if self.gpu is not None:
            info["gpu_components"] = self.gpu.get(name, []) if isinstance(self.gpu, dict) \
                else list(self.gpu)
        (slot / "version.json").write_text(json.dumps(info), encoding="utf-8")

    def here(self, name):
        return self.versions / name / "app" / "workspace" / "launch.py"

    def exe(self, name):
        return str(self.versions / name / "runtime" / "pythonw.exe")


class FakeWorkspace:
    """update.current_schema and blockers for a synthetic workspace."""

    def __init__(self, schema, blockers=()):
        self.schema, self.found = schema, list(blockers)

    def patch(self):
        return mock.patch.multiple(update, current_schema=lambda db: self.schema,
                                   blockers=lambda db: self.found)


class PassTests(unittest.TestCase):
    """launch.start with a synthetic install and workspace."""

    SLOTS = {"2.0": 2, "2.1": 2, "3.0": 3}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.layout = InstallLayout(self.base, self.SLOTS, "3.0", previous="2.1")
        self.started = []
        no_windows(self)

    def popen(self, argv, **kwargs):
        self.started.append((argv, kwargs))

    def run_pass(self, slot="3.0", argv=("workspace",), workspace=(3,), deployment=MACHINE,
                 executable=None, checks=None, show=None, today="2026-10-23"):
        with FakeWorkspace(*workspace).patch():
            return launch.start(list(argv), here=self.layout.here(slot),
                                executable=executable or self.layout.exe(slot),
                                popen=self.popen, deployment=deployment,
                                workspace_db=self.base / "workspace.db",
                                checks=checks or FakeChecks(), notice=FakeNotice,
                                show=show or self.fail, today=today)

    def assert_started(self, slot, role):
        self.assertEqual(len(self.started), 1, self.started)
        argv, kwargs = self.started[0]
        self.assertEqual(argv, [self.layout.exe(slot), *update.LAUNCH_ARGS, "--slot", role])
        self.assertEqual(kwargs["cwd"], str(self.layout.versions / slot / "app"))

    def test_a_per_user_install_makes_no_decision_and_reads_no_workspace(self):
        # Per-user installs keep the installer's gate (B5-1): the version
        # started opens, whatever the workspace. Since the owner's decision
        # of 23 Oct 2026 it reads current.json for its first-start check
        # (PerUserCheckTests); these versions do not have it.
        def never(db):
            raise AssertionError("the workspace was read")
        (self.base / "workspace" / "logs").mkdir(parents=True)
        for slot in ("2.0", "3.0"):
            with self.subTest(slot=slot), \
                    mock.patch.multiple(update, current_schema=never, blockers=never):
                out = launch.start(["workspace"], here=self.layout.here(slot),
                                   executable=self.layout.exe(slot), popen=self.popen,
                                   deployment=PER_USER, checks=FakeChecks(), notice=FakeNotice,
                                   show=self.fail,
                                   workspace_db=self.base / "workspace" / "workspace.db")
                self.assertEqual(out, launch.Outcome())
                self.assertEqual(self.started, [])

    def test_the_current_version_with_its_own_format_opens_here(self):
        for workspace in ((None,), (3,), (2, ())):
            with self.subTest(workspace=workspace):
                self.assertEqual(self.run_pass(workspace=workspace),
                                 launch.Outcome(role="current"))
        self.assertEqual(self.started, [])

    def test_another_version_started_hands_over_to_the_current_one(self):
        self.assertEqual(self.run_pass(slot="2.0"), launch.Outcome(exit=0))
        self.assert_started("3.0", "current")

    def test_blockers_hand_over_to_the_newest_version_of_the_older_format(self):
        found = [{"kind": "meetings", "count": 2, "message": "2 meetings are still here."}]
        shown = []                                       # B5b: the daily message first
        self.assertEqual(self.run_pass(workspace=(2, found), show=shown.append),
                         launch.Outcome(exit=0))
        self.assertEqual(shown, [launch.WAITING.format(version="2.1")])
        self.assert_started("2.1", "compat")

    def test_no_version_of_the_older_format_shows_the_message_with_counts(self):
        found = [{"kind": "meetings", "count": 2, "message": "2 meetings are still here."}]
        out = self.run_pass(workspace=(1, found))
        self.assertEqual(out.exit, 1)
        self.assertEqual(out.message, launch.BLOCKED + " 2 meetings are still here.")
        self.assertEqual(self.started, [])

    def test_a_workspace_from_a_newer_version_is_refused(self):
        self.layout.version("4.0", 4)            # installed, but strict: never a target
        out = self.run_pass(workspace=(4,))
        self.assertEqual((out.exit, out.message), (1, launch.TOO_NEW.format(schema=4)))
        self.assertIn("Ask your IT team.", out.message)
        self.assertEqual(self.started, [])

    def test_any_error_reading_the_workspace_opens_the_current_version(self):
        def damaged(db):
            raise sqlite3.DatabaseError("synthetic")
        for patch in ({"current_schema": damaged}, {"blockers": damaged}):
            with self.subTest(patch=sorted(patch)), mock.patch.multiple(update, **patch):
                out = launch.start(["workspace"], here=self.layout.here("3.0"),
                                   executable=self.layout.exe("3.0"), popen=self.popen,
                                   deployment=MACHINE,
                                   workspace_db=self.base / "workspace.db",
                                   checks=FakeChecks(), notice=FakeNotice, show=self.fail)
            self.assertEqual(out, launch.Outcome(role="current"))
        # A real, damaged database file: unreadable, never an older format.
        db = self.base / "workspace.db"
        db.write_bytes(b"not a database" * 100)
        out = launch.start(["workspace"], here=self.layout.here("2.1"),
                           executable=self.layout.exe("2.1"), popen=self.popen,
                           deployment=MACHINE, workspace_db=db, checks=FakeChecks(),
                           notice=FakeNotice, show=self.fail)
        self.assertEqual(out, launch.Outcome(exit=0))
        self.assert_started("3.0", "current")

    def test_a_target_that_does_not_match_its_record_starts_nothing(self):
        cases = (("another version", lambda lay: lay.version("2.1", 2, "2.2")),
                 ("another format", lambda lay: lay.version("2.1", 3)),
                 ("no format", lambda lay: lay.version("2.1", None)),
                 ("no runtime", lambda lay: os.remove(lay.exe("2.1"))),
                 ("no version file", lambda lay: os.remove(lay.versions / "2.1" / "version.json")))
        found = [{"kind": "meetings", "count": 1, "message": "1 meeting is still here."}]
        for n, (label, spoil) in enumerate(cases):
            with self.subTest(label):
                self.layout = InstallLayout(self.base / f"case{n}", self.SLOTS, "3.0",
                                            previous="2.1")
                spoil(self.layout)
                self.started = []
                out = self.run_pass(workspace=(2, found))
                self.assertEqual((out.exit, out.message), (1, launch.IT_MESSAGE))
                self.assertEqual(self.started, [])

    def test_a_damaged_record_hands_over_to_nothing_else(self):
        (self.layout.root / "current.json").write_text('{"version": "..\\\\x", "format": 2}')
        self.assertEqual(self.run_pass(slot="2.0", workspace=(2, ["x"])),
                         launch.Outcome(role="current"))
        self.assertEqual(self.started, [])

    def test_an_unknown_role_is_refused(self):
        for argv in (["workspace", "--slot"], ["workspace", "--slot", "newer"],
                     ["workspace", "--slot", "--gate"]):
            with self.subTest(argv=argv):
                out = self.run_pass(argv=argv)
                self.assertEqual((out.exit, out.message), (1, launch.IT_MESSAGE))

    def test_a_hand_over_makes_no_second_decision(self):
        out = self.run_pass(slot="2.1", argv=("workspace", "--slot", "compat"), workspace=(3,))
        self.assertEqual(out, launch.Outcome(role="compat"))
        self.assertEqual(self.started, [])

    @unittest.skipUnless(sys.platform == "win32", "junctions")
    def test_a_start_through_a_junction_hands_over_by_the_real_path(self):
        import _winapi
        link = self.layout.root / "current"
        _winapi.CreateJunction(str(self.layout.versions / "3.0"), str(link))
        through = str(link / "runtime" / "pythonw.exe")
        self.assertFalse(launch.is_real(through))
        self.assertTrue(launch.is_real(self.layout.exe("3.0")))
        out = self.run_pass(executable=through)
        self.assertEqual(out, launch.Outcome(exit=0))
        self.assert_started("3.0", "current")
        # Already handed over, and still not the real path: refused, never a loop.
        self.started = []
        out = self.run_pass(argv=("workspace", "--slot", "current"), executable=through)
        self.assertEqual((out.exit, out.message), (1, launch.IT_MESSAGE))
        self.assertEqual(self.started, [])

    @unittest.skipUnless(sys.platform == "win32", "junctions")
    def test_a_target_that_is_a_link_starts_nothing(self):
        import _winapi
        real = self.base / "elsewhere"
        InstallLayout(real, {"2.1": 2}, "2.1")
        target = self.layout.versions / "2.1"
        for p in sorted(target.rglob("*"), reverse=True):
            p.rmdir() if p.is_dir() else p.unlink()
        target.rmdir()
        _winapi.CreateJunction(str(real / "Unminute" / "versions" / "2.1"), str(target))
        self.addCleanup(lambda: os.path.lexists(target) and os.rmdir(target))
        found = [{"kind": "meetings", "count": 1, "message": "1 meeting is still here."}]
        out = self.run_pass(workspace=(2, found))
        self.assertEqual((out.exit, out.message), (1, launch.IT_MESSAGE))
        self.assertEqual(self.started, [])


class FakeChecks:
    """launchcheck.Checks in tests: each version's (result, part) by its
    folder name, ok by default; every call is recorded."""

    def __init__(self, light=None, full=None):
        self.light_results, self.full_results = dict(light or {}), dict(full or {})
        self.calls = []

    @staticmethod
    def _name(exe):
        return Path(exe).parents[1].name

    def light(self, exe, app):
        self.calls.append(("light", self._name(exe)))
        return self.light_results.get(self._name(exe), ("ok", None))

    def full(self, exe, app):
        self.calls.append(("full", self._name(exe)))
        return self.full_results.get(self._name(exe), ("ok", None))


class FakeNotice:
    """launchcheck.Notice in tests: records that it was open."""
    opened = 0

    def __enter__(self):
        FakeNotice.opened += 1
        return self

    def __exit__(self, *exc):
        return False


def no_windows(case):
    """No test may show a real message box or notice: fail if one would."""
    for target, name in ((launch, "_inform"), (launchcheck, "Notice")):
        patcher = mock.patch.object(target, name,
                                    side_effect=AssertionError(f"a real {name} was shown"))
        patcher.start()
        case.addCleanup(patcher.stop)


def descriptor(sddl):
    """A self-relative security descriptor (bytes) from its SDDL text."""
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.ULONG))
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    found, size = ctypes.c_void_p(), wintypes.ULONG()
    if not convert(sddl, 1, ctypes.byref(found), ctypes.byref(size)):
        raise OSError(ctypes.get_last_error(), "SDDL")
    try:
        return ctypes.string_at(found, size.value)
    finally:
        kernel32.LocalFree(found)


class MachineTrustTests(unittest.TestCase):
    """#111 B6b: deployment() reports "machine" only for an install whose
    current.json says so and that passes the machine trust test
    (update.machine_trusted, design 5.2): FOLDERID_ProgramFiles\\Unminute,
    owned by Administrators or SYSTEM, which this process's user cannot
    change. A per-user install that writes "machine" into its own
    current.json gains nothing."""

    ADMINS, USER = "S-1-5-32-544", "S-1-5-21-1-2-3-1001"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.program_files = self.base / "Program Files"
        self.layout = InstallLayout(self.program_files, {"3.0": 3}, "3.0")
        record = json.loads((self.layout.root / "current.json").read_text(encoding="utf-8-sig"))
        record["mode"] = "machine"
        (self.layout.root / "current.json").write_text(json.dumps(record), encoding="utf-8-sig")

    def trusted(self, root=None, *, owner=ADMINS, can_change=False, program_files=None):
        def raise_or(value):
            def answer(path):
                if isinstance(value, Exception):
                    raise value
                return value
            return answer
        return update.machine_trusted(
            root or self.layout.root,
            program_files=program_files or (lambda: str(self.program_files)),
            owner=raise_or(owner), can_change=raise_or(can_change))

    def test_deployment_is_machine_only_when_the_trust_test_passes(self):
        here = self.layout.here("3.0")
        asked = []
        found = update.deployment(here, trusted=lambda root: asked.append(root) or True)
        self.assertEqual(found, {"version": "3.0", "mode": "machine"})
        self.assertEqual(asked, [self.layout.root])
        found = update.deployment(here, trusted=lambda root: False)
        self.assertEqual(found, {"version": "3.0", "mode": "managed", "machine_refused": True})
        self.assertIn("machine_claim_refused", __import__("workspace.diag").diag.KINDS)
        # No machine claim: the trust test is not asked, as before B6b.
        record = json.loads((self.layout.root / "current.json").read_text(encoding="utf-8-sig"))
        del record["mode"]
        (self.layout.root / "current.json").write_text(json.dumps(record), encoding="utf-8-sig")
        found = update.deployment(here, trusted=lambda root: self.fail("asked"))
        self.assertEqual(found, {"version": "3.0", "mode": "managed"})

    def test_deployment_reads_a_record_that_is_not_an_object_as_a_source_checkout(self):
        (self.layout.root / "current.json").write_text("[1]", encoding="utf-8")
        self.assertEqual(update.deployment(self.layout.here("3.0"), trusted=lambda r: True),
                         {"version": None, "mode": "source"})

    def test_the_trust_test_needs_every_condition(self):
        self.assertTrue(self.trusted())
        self.assertTrue(self.trusted(owner="S-1-5-18"))                 # SYSTEM
        for label, kwargs in (
                ("owned by the user", {"owner": self.USER}),
                ("owned by Users", {"owner": "S-1-5-32-545"}),
                ("the user can change it", {"can_change": True}),
                ("the owner cannot be read", {"owner": OSError("denied")}),
                ("the permissions cannot be read", {"can_change": OSError("denied")}),
                # Any error fails the test, not only the expected ones (review
                # of 4c0daf2): a ctypes argument or type error never escapes.
                ("a ctypes argument error", {"owner": __import__("ctypes").ArgumentError("bad")}),
                ("a type error", {"can_change": TypeError("bad")}),
                ("a lookup error", {"owner": KeyError("bad")}),
                ("no Program Files", {"program_files": lambda: None}),
                ("another folder", {"root": self.base / "Local" / "Programs" / "Unminute"}),
                ("a folder inside it", {"root": self.layout.versions}),
                ("another name in Program Files", {"root": self.program_files / "Unminute2"})):
            with self.subTest(label):
                self.assertFalse(self.trusted(**kwargs))

    @unittest.skipUnless(sys.platform == "win32", "junctions")
    def test_a_junction_on_the_way_fails_the_trust_test(self):
        import _winapi
        # Program Files\Unminute is a junction to a folder the user owns.
        elsewhere = self.base / "elsewhere"
        InstallLayout(elsewhere, {"3.0": 3}, "3.0")
        other = self.base / "Other Files"
        other.mkdir()
        _winapi.CreateJunction(str(elsewhere / "Unminute"), str(other / "Unminute"))
        self.addCleanup(lambda: os.rmdir(other / "Unminute"))
        self.assertFalse(self.trusted(other / "Unminute", program_files=lambda: str(other)))
        # The known folder itself reached through a junction.
        alias = self.base / "Alias"
        _winapi.CreateJunction(str(self.program_files), str(alias))
        self.addCleanup(lambda: os.rmdir(alias))
        self.assertFalse(self.trusted(alias / "Unminute", program_files=lambda: str(alias)))
        self.assertTrue(self.trusted())

    @unittest.skipUnless(sys.platform == "win32", "Windows security")
    def test_the_windows_checks(self):
        # Program Files as Windows' known folder gives it (a 64-bit Python).
        self.assertEqual(os.path.normcase(update._program_files()),
                         os.path.normcase(os.environ["ProgramFiles"]))
        # A folder this user made: owned by this user (or, elevated, by
        # Administrators), and this user can change it, so it is never trusted.
        self.assertRegex(update._owner_sid(str(self.base)), r"^S-1-[0-9]+(-[0-9]+)+$")
        self.assertTrue(update._can_change(str(self.base)))
        self.assertFalse(update.machine_trusted(self.layout.root))
        # Windows' own folder is TrustedInstaller's, not Administrators'.
        self.assertNotIn(update._owner_sid(os.environ["SystemRoot"]), update.OWNERS)
        # AccessCheck against this process's token, on descriptors built here.
        everyone_reads = descriptor("O:SYG:SYD:P(A;OICI;FA;;;SY)(A;OICI;0x1200a9;;;WD)")
        everyone_changes = descriptor("O:SYG:SYD:P(A;OICI;FA;;;WD)")
        self.assertFalse(update._grants_change(everyone_reads))
        self.assertTrue(update._grants_change(everyone_changes))
        # install.ps1 -Machine's permissions: only an elevated administrator may
        # change the folder; the app runs without elevation.
        machine = descriptor("O:BAG:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
                             "(A;OICI;0x1200a9;;;BU)(A;OICI;0x1200a9;;;AC)")
        import ctypes
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        self.assertEqual(update._grants_change(machine), elevated)


HOLD = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[2])
    import os
    path = os.path.join(sys.argv[1], "instance.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0)
    print("held", flush=True)
    sys.stdin.readline()
""")


class LockHolder:
    """Another process holding workspace\\instance.lock, standing in for the
    app open in another Windows session of the same user. It locks the file
    with the platform's own call, not workspace/instance.py."""

    def __init__(self, root):
        self.proc = subprocess.Popen([sys.executable, "-c", HOLD, str(root), str(ROOT)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        line = self.proc.stdout.readline().strip()
        if line != "held":
            self.close()
            raise AssertionError("the holder did not take the lock")

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait(timeout=30)
        self.proc.stdout.close()


class LockTests(unittest.TestCase):
    """The workspace lock (B5-10): one app per workspace across every session."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "workspace"
        self.root.mkdir()

    def hold(self):
        holder = LockHolder(self.root)
        self.addCleanup(holder.close)
        return holder

    def test_a_lock_held_by_another_process_is_refused_and_released_with_it(self):
        holder = self.hold()
        with self.assertRaises(instance.Held) as caught:
            instance.acquire(self.root)
        self.assertEqual(str(caught.exception), instance.MESSAGE)
        holder.close()
        held = instance.acquire(self.root)
        held.release()
        held.release()                               # twice is harmless
        self.assertEqual((self.root / instance.NAME).read_bytes(), b"")

    def test_only_a_held_lock_is_reported_as_another_session(self):
        # Round 1 review (P3): a lock call that fails for any other reason is
        # not "open in another session"; it stays an OSError, which the app's
        # start turns into its IT message, and the file is closed either way.
        for code in (errno.EACCES, errno.EAGAIN):
            with self.subTest(errno=code), \
                    mock.patch.object(instance, "_lock", side_effect=OSError(code, "held")), \
                    self.assertRaises(instance.Held):
                instance.acquire(self.root)
        for code in (errno.EIO, errno.EBADF, errno.ENOLCK):
            with self.subTest(errno=code), \
                    mock.patch.object(instance, "_lock", side_effect=OSError(code, "other")), \
                    self.assertRaises(OSError) as caught:
                instance.acquire(self.root)
            self.assertNotIsInstance(caught.exception, instance.Held)
            self.assertEqual(caught.exception.errno, code)
        (self.root / instance.NAME).unlink()         # every handle was closed
        instance.acquire(self.root).release()

    def test_another_process_sees_the_lock_this_one_holds(self):
        probe = textwrap.dedent(f"""
            import sys; sys.path.insert(0, {str(ROOT)!r})
            from workspace import instance
            try:
                instance.acquire({str(self.root)!r}).release(); print("taken")
            except instance.Held:
                print("held")
        """)
        held = instance.acquire(self.root)
        try:
            r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.stdout.strip(), "held", r.stderr)
        finally:
            held.release()
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.stdout.strip(), "taken", r.stderr)


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class WorkspaceLockTests(unittest.TestCase):
    """The app's own start holds the lock before anything reads or migrates
    the workspace (D3)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "workspace"
        self.root.mkdir()
        self.models = Path(tmp.name) / "models"

    def open(self, **kw):
        from workspace.app import Workspace
        return Workspace(self.root, self.models, start=False, battery=False,
                         protector=keyprotect.TestProtector(), **kw)

    def test_a_second_session_is_refused_before_the_store_opens(self):
        holder = LockHolder(self.root)
        self.addCleanup(holder.close)
        with mock.patch("workspace.app.Store", side_effect=AssertionError("store built")) \
                as built, self.assertRaises(instance.Held):
            self.open(instance_lock=True)
        built.assert_not_called()
        self.assertFalse((self.root / "workspace.db").exists())

    def test_a_migration_never_runs_while_another_session_holds_the_lock(self):
        import schema_v1
        db = schema_v1.create(self.root / "workspace.db")   # an older, empty workspace
        db.close()
        holder = LockHolder(self.root)
        self.addCleanup(holder.close)
        with self.assertRaises(instance.Held):
            self.open(instance_lock=True)
        self.assertEqual(update.current_schema(self.root / "workspace.db"), 1)
        holder.close()
        ws = self.open(instance_lock=True)                   # now it migrates
        try:
            from workspace.store import SCHEMA_VERSION
            self.assertEqual(update.current_schema(self.root / "workspace.db"), SCHEMA_VERSION)
            with self.assertRaisesRegex(AssertionError, "did not take the lock"):
                LockHolder(self.root)                        # held while the app is open
        finally:
            ws.close()
        instance.acquire(self.root).release()                # closing let it go

    def test_a_lock_that_fails_for_another_reason_stops_the_start_naming_it(self):
        # Round 1 review (P3): an I/O error on the lock is not reported as the
        # app being open in another session, and the store never opens.
        from workspace.store import StoreError
        with mock.patch.object(instance, "_lock", side_effect=OSError(errno.EIO, "I/O")), \
                mock.patch("workspace.app.Store", side_effect=AssertionError("store built")) \
                as built, self.assertRaises(StoreError) as caught:
            self.open(instance_lock=True)
        built.assert_not_called()
        self.assertIn("Ask your IT team.", str(caught.exception))
        self.assertNotIn("another Windows session", str(caught.exception))
        self.assertFalse((self.root / "workspace.db").exists())

    def test_tests_and_tools_that_do_not_ask_take_no_lock(self):
        ws = self.open()
        try:
            self.assertFalse((self.root / instance.NAME).exists())
        finally:
            ws.close()

    def test_the_app_start_refuses_a_workspace_open_in_another_session(self):
        # The D3 gap (#111 B5-10): the single-instance mutex is per session,
        # so a start in another session of the same user does not see it
        # (_single_instance answers as if nothing were open). The workspace
        # lock refuses that start before the store opens.
        from workspace import __main__ as main
        from workspace import app as app_module
        from workspace import diag
        holder = LockHolder(self.root)
        self.addCleanup(holder.close)
        real = app_module.Workspace

        def here(*a, **kw):                  # the app's own call, on this test's folder
            kw.pop("cache_dir", None)
            return real(self.root, self.models, start=False, battery=False,
                        protector=keyprotect.TestProtector(), **kw)
        with mock.patch.object(app_module, "Workspace", side_effect=here), \
                mock.patch.object(main, "_single_instance", return_value=True), \
                mock.patch.object(main, "model_cache", return_value=self.models), \
                mock.patch.object(main, "_message") as shown, \
                mock.patch.object(diag, "install", side_effect=RuntimeError("opened")), \
                mock.patch.object(sys, "argv", ["workspace"]):
            self.assertEqual(main.main(), 0)
        shown.assert_called_once_with(instance.MESSAGE)
        self.assertFalse((self.root / "workspace.db").exists())


class CheckTests(unittest.TestCase):
    """B5b: the light check at a version's first start, its fault classes and
    fallback, the full check before a migration, the daily "update waiting"
    message and logs\\launch.json (the owner's decisions B5-2, B5-5, B5-6)."""

    # 2.9 and 3.0 share the current format; 2.1 is the older one.
    SLOTS = {"2.1": 2, "2.9": 3, "3.0": 3}
    BLOCKERS = [{"kind": "meetings", "count": 1, "message": "1 meeting is still here."}]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.layout = InstallLayout(self.base, self.SLOTS, "3.0", previous="2.9",
                                    features=["gate", "launch-gate", launchcheck.FEATURE])
        self.ws = self.base / "workspace"
        (self.ws / "logs").mkdir(parents=True)
        self.started, self.shown = [], []
        FakeNotice.opened = 0
        no_windows(self)

    def run_pass(self, checks, workspace=(3,), slot="3.0", argv=("workspace",),
                 today="2026-10-23"):
        self.started, self.shown = [], []
        with FakeWorkspace(*workspace).patch():
            return launch.start(list(argv), here=self.layout.here(slot),
                                executable=self.layout.exe(slot),
                                popen=lambda argv, **kw: self.started.append(argv),
                                deployment=MACHINE, workspace_db=self.ws / "workspace.db",
                                checks=checks, notice=FakeNotice, show=self.shown.append,
                                today=today)

    def started_as(self):
        self.assertEqual(len(self.started), 1, self.started)
        argv = self.started[0]
        return Path(argv[0]).parents[1].name, argv[-1]

    def record(self):
        return json.loads((self.ws / "logs" / "launch.json").read_text(encoding="ascii"))

    def test_the_first_start_runs_the_light_check_once_and_records_it(self):
        checks = FakeChecks()
        self.assertEqual(self.run_pass(checks), launch.Outcome(role="current"))
        self.assertEqual(checks.calls, [("light", "3.0")])
        entry = self.record()["slots"]["3.0"]
        self.assertEqual((entry["result"], entry["installed"]), ("ok", "2027-01-03T01:00:00Z"))
        self.assertEqual((self.record()["last_role"], self.record()["last_check"]),
                         ("current", "ok"))
        checks = FakeChecks()
        # A pass read from the record makes no incident code (nothing would use it).
        with mock.patch.object(diag, "new_code", side_effect=AssertionError("a code made")):
            self.assertEqual(self.run_pass(checks), launch.Outcome(role="current"))
        self.assertEqual(checks.calls, [])                 # a pass is not repeated
        self.assertEqual(self.shown, [])

    def test_a_slot_fault_falls_back_to_the_newest_other_version_of_its_format(self):
        checks = FakeChecks(light={"3.0": ("slot_fault", "webview")})
        self.assertEqual(self.run_pass(checks), launch.Outcome(exit=0))
        self.assertEqual(self.started_as(), ("2.9", "fallback"))
        self.assertEqual(checks.calls, [("light", "3.0"), ("light", "2.9")])
        rec = self.record()
        code = rec["slots"]["3.0"]["code"]
        self.assertRegex(code, r"\AU-[0-9A-F]{6}\Z")
        self.assertEqual((rec["slots"]["3.0"]["result"], rec["last_role"], rec["last_check"]),
                         ("slot_fault", "fallback", "slot_fault"))
        self.assertEqual(self.shown, [launch.FALLBACK.format(code=code, version="2.9")])
        # Not tried again, and the message not shown again, until a repair.
        checks = FakeChecks()
        self.assertEqual(self.run_pass(checks), launch.Outcome(exit=0))
        self.assertEqual(self.started_as(), ("2.9", "fallback"))
        self.assertEqual((checks.calls, self.shown), ([], []))
        self.layout.entries["3.0"]["installed"] = "2027-03-01T01:00:00Z"    # repaired
        self.layout.write()
        checks = FakeChecks()
        self.assertEqual(self.run_pass(checks), launch.Outcome(role="current"))
        self.assertEqual(checks.calls, [("light", "3.0")])
        self.assertEqual(self.record()["last_check"], "ok")

    def test_an_environment_fault_never_falls_back_and_is_checked_at_every_start(self):
        for _ in range(2):
            checks = FakeChecks(light={"3.0": ("env_fault", "dpapi")})
            # The current version opens and shows its own locked screen.
            self.assertEqual(self.run_pass(checks), launch.Outcome(role="current"))
            self.assertEqual(checks.calls, [("light", "3.0")])
            self.assertEqual((self.started, self.shown), ([], []))
            self.assertEqual(self.record()["last_check"], "env_fault")

    def test_with_no_version_to_fall_back_to_nothing_opens(self):
        del self.layout.entries["2.9"]
        self.layout.write()
        checks = FakeChecks(light={"3.0": ("slot_fault", "import")})
        out = self.run_pass(checks)
        code = self.record()["slots"]["3.0"]["code"]
        self.assertEqual(out, launch.Outcome(exit=1, message=launch.FAILED.format(code=code)))
        self.assertIn("Ask your IT team.", out.message)
        self.assertEqual(self.started, [])

    def test_a_fallback_never_targets_an_older_format(self):
        # 2.1 is installed, but with no workspace (or the same format) a
        # fallback to it would make or keep a workspace the update cannot take.
        del self.layout.entries["2.9"]
        self.layout.write()
        for workspace in ((None,), (3,), (2, ())):
            with self.subTest(workspace=workspace):
                checks = FakeChecks(light={"3.0": ("slot_fault", "import")})
                self.assertEqual(self.run_pass(checks, workspace=workspace).exit, 1)
                self.assertNotIn(("light", "2.1"), checks.calls)
                self.assertEqual(self.started, [])

    def test_the_full_check_runs_before_a_migration_whatever_the_record_says(self):
        # A forged record claiming every check passed changes nothing.
        (self.ws / "logs" / "launch.json").write_text(json.dumps(
            {"slots": {"3.0": {"installed": "2027-01-03T01:00:00Z", "result": "ok"}},
             "last_role": "current", "last_check": "ok"}), encoding="ascii")
        checks = FakeChecks()
        self.assertEqual(self.run_pass(checks, workspace=(2, ())), launch.Outcome(role="current"))
        self.assertEqual(checks.calls, [("full", "3.0")])
        self.assertEqual(FakeNotice.opened, 1)
        checks = FakeChecks()
        self.run_pass(checks, workspace=(2, ()))
        self.assertEqual(checks.calls, [("full", "3.0")])   # every migrating start
        # No migration: no full check.
        checks = FakeChecks()
        self.run_pass(checks, workspace=(3,))
        self.assertEqual(checks.calls, [])

    def test_a_failed_full_check_never_migrates(self):
        never = mock.patch("workspace.store.Store", side_effect=AssertionError("store built"))
        with never:
            checks = FakeChecks(full={"3.0": ("env_fault", "dpapi")})
            out = self.run_pass(checks, workspace=(2, ()))
            code = re.search(r"code (U-[0-9A-F]{6})", out.message).group(1)
            self.assertEqual(out, launch.Outcome(exit=1,
                                                 message=launch.NOT_READY.format(code=code)))
            self.assertEqual(self.started, [])
            # A slot fault falls back to another version of the current
            # format, which gets its own full check before it migrates.
            checks = FakeChecks(full={"3.0": ("slot_fault", "self_test")})
            self.assertEqual(self.run_pass(checks, workspace=(2, ())), launch.Outcome(exit=0))
            self.assertEqual(self.started_as(), ("2.9", "fallback"))
            # 3.0's light check passed at the first start above: not repeated.
            self.assertEqual(checks.calls, [("full", "3.0"), ("light", "2.9"),
                                            ("full", "2.9")])
            checks = FakeChecks(full={"3.0": ("slot_fault", "self_test"),
                                      "2.9": ("slot_fault", "hung")})
            self.assertEqual(self.run_pass(checks, workspace=(2, ())).exit, 1)
            self.assertEqual(self.started, [])

    def test_the_update_waiting_message_is_shown_once_a_day_before_the_hand_over(self):
        checks = FakeChecks()
        self.assertEqual(self.run_pass(checks, workspace=(2, self.BLOCKERS)),
                         launch.Outcome(exit=0))
        self.assertEqual(self.started_as(), ("2.1", "compat"))
        self.assertEqual(self.shown, [launch.WAITING.format(version="2.1")])
        self.assertEqual(checks.calls, [("light", "2.1")])    # the version that will open
        self.assertEqual(self.record()["waiting_shown"], "2026-10-23")
        self.assertEqual(self.record()["last_role"], "compat")
        self.run_pass(FakeChecks(), workspace=(2, self.BLOCKERS))
        self.assertEqual(self.shown, [])                     # the same day
        self.run_pass(FakeChecks(), workspace=(2, self.BLOCKERS), today="2026-10-24")
        self.assertEqual(self.shown, [launch.WAITING.format(version="2.1")])

    def test_a_fallback_from_compat_stays_finish_only_with_the_waiting_message(self):
        # Two kept versions of the older format: 2.0 (newest, seq 4) fails its
        # check, so 2.1 opens the same older workspace. It must still be
        # compat (finish-only, B5-7) and the waiting message still shown (B5-6).
        self.layout.version("2.0", 2)
        self.layout.entries["2.0"] = {"seq": 4, "installed": "2027-01-04T01:00:00Z",
                                      "schema": 2}
        self.layout.write()
        checks = FakeChecks(light={"2.0": ("slot_fault", "import")})
        self.assertEqual(self.run_pass(checks, workspace=(2, self.BLOCKERS)),
                         launch.Outcome(exit=0))
        self.assertEqual(checks.calls, [("light", "2.0"), ("light", "2.1")])
        self.assertEqual(self.started_as(), ("2.1", "compat"))
        rec = self.record()
        code = rec["slots"]["2.0"]["code"]
        self.assertEqual(self.shown, [launch.FALLBACK.format(code=code, version="2.1"),
                                      launch.WAITING.format(version="2.1")])
        self.assertEqual((rec["last_role"], rec["last_check"]), ("compat", "slot_fault"))
        self.assertEqual(launchcheck.health_state(self.ws / "logs"),
                         {"slot": "compat", "update_waiting": True, "self_check": "slot_fault"})
        # Never the current format's versions, and never a migration.
        self.assertNotIn(("full", "3.0"), checks.calls)

    def test_a_version_from_before_b5b_is_never_started_with_the_check(self):
        for name, schema in self.SLOTS.items():
            self.layout.features = ["gate", "launch-gate"]
            self.layout.version(name, schema)
        checks = FakeChecks()
        self.assertEqual(self.run_pass(checks), launch.Outcome(role="current"))
        self.assertEqual(checks.calls, [])
        self.assertIsNone(self.record()["last_check"])

    def test_a_hand_over_start_checks_nothing(self):
        checks = FakeChecks(light={"2.1": ("slot_fault", "import")})
        out = self.run_pass(checks, slot="2.1", argv=("workspace", "--slot", "compat"))
        self.assertEqual(out, launch.Outcome(role="compat"))
        self.assertEqual(checks.calls, [])

    def test_the_record_is_written_in_full_or_not_at_all(self):
        self.run_pass(FakeChecks())
        before = (self.ws / "logs" / "launch.json").read_bytes()
        with mock.patch.object(Path, "replace", side_effect=OSError("synthetic")), \
                mock.patch.object(diag, "STATE_RETRIES_S", ()):
            self.run_pass(FakeChecks(light={"3.0": ("env_fault", "dpapi")}))
        self.assertEqual((self.ws / "logs" / "launch.json").read_bytes(), before)
        self.assertEqual([p.name for p in (self.ws / "logs").glob("launch.json*")],
                         ["launch.json"])

    def test_a_damaged_record_reads_as_none_and_holds_no_content(self):
        for raw in ("{", "[1]", json.dumps({"slots": {"..\\x": {"result": "ok"}},
                                            "last_role": "newer", "last_check": "fine",
                                            "waiting_shown": "C:\\x"})):
            with self.subTest(raw=raw[:20]):
                (self.ws / "logs" / "launch.json").write_text(raw, encoding="ascii")
                book = launchcheck.Book(self.ws / "logs")
                self.assertEqual((book.slots, book.last_role, book.last_check,
                                  book.waiting_shown), ({}, None, None, None))
                self.assertEqual(launchcheck.health_state(self.ws / "logs"),
                                 {"slot": "none", "update_waiting": False,
                                  "self_check": "unknown"})


class PerUserCheckTests(unittest.TestCase):
    """The light first-start check in a per-user install too (the owner's
    decision of 23 Oct 2026): the version started is checked once per
    install, a slot fault falls back to another kept version of the same
    format, and nothing else changes: no workspace is read, no decision,
    no full check and no "update waiting" message."""

    # 2.9 (the previous version) shares 3.0's format; 2.1 is older.
    SLOTS = {"2.1": 2, "2.9": 3, "3.0": 3}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.layout = InstallLayout(self.base, self.SLOTS, "3.0", previous="2.9",
                                    features=["gate", "launch-gate", launchcheck.FEATURE])
        self.ws = self.base / "workspace"
        (self.ws / "logs").mkdir(parents=True)
        FakeNotice.opened = 0
        no_windows(self)

    def run_pass(self, checks, slot="3.0", argv=("workspace",), deployment=PER_USER):
        self.started, self.shown = [], []

        def never(db):
            raise AssertionError("the workspace was read")
        with mock.patch.multiple(update, current_schema=never, blockers=never):
            return launch.start(list(argv), here=self.layout.here(slot),
                                executable=self.layout.exe(slot),
                                popen=lambda argv, **kw: self.started.append(argv),
                                deployment=deployment, workspace_db=self.ws / "workspace.db",
                                checks=checks, notice=FakeNotice, show=self.shown.append,
                                today="2026-10-23")

    def record(self):
        return json.loads((self.ws / "logs" / "launch.json").read_text(encoding="ascii"))

    def test_the_first_start_is_checked_once_and_opens_here(self):
        for _ in range(2):
            checks = FakeChecks()
            self.assertEqual(self.run_pass(checks), launch.Outcome())
            self.assertEqual((self.started, self.shown), ([], []))
        self.assertEqual(checks.calls, [])                  # the second start: recorded
        rec = self.record()
        self.assertEqual((rec["slots"]["3.0"]["result"], rec["slots"]["3.0"]["code"]),
                         ("ok", None))
        # An ordinary per-user start has no role: --health's slot stays none.
        self.assertEqual(launchcheck.health_state(self.ws / "logs"),
                         {"slot": "none", "update_waiting": False, "self_check": "ok"})
        # A repair or reinstall checks it again.
        self.layout.entries["3.0"]["installed"] = "2027-03-01T01:00:00Z"
        self.layout.write()
        checks = FakeChecks()
        self.assertEqual(self.run_pass(checks), launch.Outcome())
        self.assertEqual(checks.calls, [("light", "3.0")])

    def test_a_slot_fault_falls_back_to_the_previous_version_of_its_format(self):
        checks = FakeChecks(light={"3.0": ("slot_fault", "guard")})
        self.assertEqual(self.run_pass(checks), launch.Outcome(exit=0))
        self.assertEqual(checks.calls, [("light", "3.0"), ("light", "2.9")])
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0], [self.layout.exe("2.9"), *update.LAUNCH_ARGS,
                                           "--slot", "fallback"])
        code = self.record()["slots"]["3.0"]["code"]
        self.assertEqual(self.shown, [launch.FALLBACK.format(code=code, version="2.9")])
        self.assertEqual(launchcheck.health_state(self.ws / "logs"),
                         {"slot": "fallback", "update_waiting": False,
                          "self_check": "slot_fault"})
        # The fallback version, started with --slot, checks nothing again.
        checks = FakeChecks()
        self.assertEqual(self.run_pass(checks, slot="2.9", argv=("workspace", "--slot",
                                                                  "fallback")),
                         launch.Outcome(role="fallback"))
        self.assertEqual((checks.calls, self.started), ([], []))

    def test_never_a_fallback_to_an_older_format(self):
        del self.layout.entries["2.9"]
        self.layout.write()
        checks = FakeChecks(light={"3.0": ("slot_fault", "webview")})
        out = self.run_pass(checks)
        code = self.record()["slots"]["3.0"]["code"]
        self.assertEqual(out, launch.Outcome(exit=1, message=launch.FAILED.format(code=code)))
        self.assertNotIn(("light", "2.1"), checks.calls)
        self.assertEqual(self.started, [])

    def test_an_environment_fault_opens_here_and_is_checked_at_every_start(self):
        for _ in range(2):
            checks = FakeChecks(light={"3.0": ("env_fault", "dpapi")})
            self.assertEqual(self.run_pass(checks), launch.Outcome())
            self.assertEqual(checks.calls, [("light", "3.0")])
            self.assertEqual((self.started, self.shown), ([], []))
        self.assertEqual(self.record()["last_check"], "env_fault")

    def test_no_full_check_and_no_waiting_message(self):
        # Even a forged record saying the last start was compat changes nothing.
        (self.ws / "logs" / "launch.json").write_text(json.dumps(
            {"last_role": "compat", "waiting_shown": "2026-10-01"}), encoding="ascii")
        checks = FakeChecks()
        self.assertEqual(self.run_pass(checks), launch.Outcome())
        self.assertEqual(checks.calls, [("light", "3.0")])
        self.assertEqual((FakeNotice.opened, self.shown), (0, []))

    def test_with_no_workspace_yet_nothing_is_made_and_the_check_runs_again(self):
        # A new profile's first start: the workspace does not exist yet, and
        # the launcher never makes it (D3, C1; only the app's first start
        # does), so nothing is recorded and the next start checks again
        # (review of 836d308, nit 2).
        shutil.rmtree(self.ws)
        for fault in (None, ("slot_fault", "guard")):
            for _ in range(2):
                checks = FakeChecks(light={"3.0": fault} if fault else None)
                out = self.run_pass(checks)
                self.assertEqual(checks.calls[0], ("light", "3.0"))
                self.assertEqual(out, launch.Outcome(exit=0) if fault else launch.Outcome())
                self.assertEqual(len(self.shown), 1 if fault else 0)    # each time
                self.assertFalse(self.ws.exists())
        # Once the app has made the workspace, the result is kept.
        self.ws.mkdir()
        for calls in ([("light", "3.0")], []):
            checks = FakeChecks()
            self.assertEqual(self.run_pass(checks), launch.Outcome())
            self.assertEqual(checks.calls, calls)
        self.assertEqual(self.record()["slots"]["3.0"]["result"], "ok")

    def test_what_cannot_be_checked_starts_as_before(self):
        def damaged():
            (self.layout.root / "current.json").write_text("{", encoding="utf-8")

        def unlisted():
            del self.layout.entries["3.0"]
            self.layout.current = "2.9"
            self.layout.write()

        def mismatched():
            self.layout.version("3.0", 3, stated="3.1")

        def no_runtime():
            (self.layout.versions / "3.0" / "runtime" / "pythonw.exe").unlink()
        for name, change in (("damaged", damaged), ("unlisted", unlisted),
                             ("mismatched", mismatched), ("no_runtime", no_runtime)):
            with self.subTest(name):
                self.setUp()
                change()
                checks = FakeChecks(light={"3.0": ("slot_fault", "import")})
                self.assertEqual(self.run_pass(checks), launch.Outcome())
                self.assertEqual((checks.calls, self.started, self.shown), ([], [], []))
        # Not inside a versions folder (a source checkout's layout), and a
        # source checkout itself: nothing is read.
        with mock.patch.object(update, "read_slots", side_effect=AssertionError("read")):
            out = launch.start(["workspace"], here=self.base / "src" / "workspace" / "x.py",
                               executable=sys.executable, deployment=PER_USER,
                               workspace_db=self.ws / "workspace.db", checks=FakeChecks())
            self.assertEqual(out, launch.Outcome())
            checks = FakeChecks(light={"3.0": ("slot_fault", "import")})
            self.assertEqual(self.run_pass(checks, deployment=lambda: {"version": None,
                                                                        "mode": "source"}),
                             launch.Outcome())
            self.assertEqual(checks.calls, [])


class ChildCheckTests(unittest.TestCase):
    """The checks' own processes: what run_light and run_full make of what
    the child says, and the light check's fault classes."""

    def runner(self, code=0, out="", raises=None):
        def run(argv, **kw):
            self.argv, self.kw = argv, kw
            if raises:
                raise raises
            return subprocess.CompletedProcess(argv, code, out, "")
        return run

    def test_the_light_check_runs_the_versions_own_runtime_with_a_limit(self):
        result = launchcheck.run_light("C:\\v\\runtime\\pythonw.exe", "C:\\v\\app",
                                       self.runner(0, 'noise\n{"result": "ok", "part": null}\n'))
        self.assertEqual(result, ("ok", None))
        self.assertEqual(self.argv, ["C:\\v\\runtime\\pythonw.exe", *update.LAUNCH_ARGS,
                                     "--launch-check"])
        self.assertEqual((self.kw["cwd"], self.kw["timeout"]),
                         ("C:\\v\\app", launchcheck.LIGHT_LIMIT_S))

    def test_what_the_light_check_reports(self):
        cases = (
            (self.runner(1, '{"result": "env_fault", "part": "dpapi"}'), ("env_fault", "dpapi")),
            (self.runner(1, '{"result": "slot_fault", "part": "guard"}'), ("slot_fault", "guard")),
            (self.runner(raises=subprocess.TimeoutExpired("x", 30)), ("slot_fault", "hung")),
            (self.runner(raises=OSError("no runtime")), ("slot_fault", "hung")),
            (self.runner(0, ""), ("slot_fault", "no_answer")),
            (self.runner(0, "Traceback (most recent call last)"), ("slot_fault", "no_answer")),
            (self.runner(0, '{"result": "fine"}'), ("slot_fault", "no_answer")),
            (self.runner(0, '{"result": "ok", "part": "C:\\\\x"}'), ("slot_fault", "no_answer")),
            (self.runner(3, '{"result": "ok", "part": null}'), ("slot_fault", "no_answer")),
        )
        for runner, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(launchcheck.run_light("x", "y", runner), expected)

    def test_a_light_check_past_its_limit_is_run_once_more(self):
        # A first run after an install can be slow while the antivirus scans
        # the new files (review of 836d308): one hang is run again at once,
        # and only a second hang is a slot fault. Nothing else is repeated.
        ok = subprocess.CompletedProcess([], 0, '{"result": "ok", "part": null}', "")
        hang = subprocess.TimeoutExpired("x", launchcheck.LIGHT_LIMIT_S)
        cases = (((hang, ok), ("ok", None), 2),
                 ((hang, hang), ("slot_fault", "hung"), 2),
                 ((hang, OSError("no runtime")), ("slot_fault", "hung"), 2),
                 ((OSError("no runtime"), ok), ("slot_fault", "hung"), 1),
                 ((subprocess.CompletedProcess([], 0, "", ""), ok),
                  ("slot_fault", "no_answer"), 1))
        for answers, expected, runs in cases:
            calls = []

            def run(argv, answers=answers, calls=calls, **kw):
                calls.append((argv, kw["timeout"]))
                answer = answers[len(calls) - 1]
                if isinstance(answer, BaseException):
                    raise answer
                return answer
            with self.subTest(expected=expected, runs=runs):
                self.assertEqual(launchcheck.run_light("x", "y", run), expected)
                self.assertEqual(len(calls), runs)
                self.assertTrue(all(c == calls[0] for c in calls))   # the same check, limit
        # The full check (180 s, behind a notice) is not repeated.
        calls = []

        def hung(argv, **kw):
            calls.append(argv)
            raise subprocess.TimeoutExpired("x", launchcheck.FULL_LIMIT_S)
        self.assertEqual(launchcheck.run_full("x", "y", hung), ("slot_fault", "hung"))
        self.assertEqual(len(calls), 1)

    def test_what_the_full_check_reports(self):
        ok = "ok      import numpy\nok      dpapi round trip\nPASS\n"
        dpapi = "ok      import numpy\nFAILED  dpapi round trip: -2146893813\nFAIL\n"
        both = "FAILED  import torch: OSError: x\nFAILED  dpapi round trip: 5\nFAIL\n"
        cases = ((self.runner(0, ok), ("ok", None)),
                 (self.runner(1, dpapi), ("env_fault", "dpapi")),
                 (self.runner(1, both), ("slot_fault", "self_test")),
                 (self.runner(1, "FAIL\n"), ("slot_fault", "self_test")),
                 (self.runner(0, ""), ("slot_fault", "self_test")),
                 (self.runner(raises=subprocess.TimeoutExpired("x", 180)), ("slot_fault", "hung")))
        for runner, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(launchcheck.run_full("x", "y", runner), expected)
        launchcheck.run_full("x", "y", self.runner(0, ok))
        self.assertEqual(self.argv[-1], "--self-test")
        self.assertEqual(self.kw["timeout"], launchcheck.FULL_LIMIT_S)

    def test_the_fault_classes(self):
        good = lambda: (True, "dpapi round trip")              # noqa: E731
        bad = mock.Mock(return_value=(False, "dpapi round trip: 5"))
        with mock.patch("importlib.import_module"), \
                mock.patch.object(launchcheck, "_guard_holds", return_value=True):
            self.assertEqual(launchcheck.light_check(good), {"result": "ok", "part": None})
            self.assertEqual(launchcheck.light_check(bad), {"result": "env_fault",
                                                            "part": "dpapi"})
        for failing, part in (("workspace.app", "import"),
                              ("webview.platforms.edgechromium", "webview")):
            def imp(name, failing=failing):
                if name == failing:
                    raise ImportError("synthetic")
            bad.reset_mock()
            with self.subTest(part=part), mock.patch("importlib.import_module", side_effect=imp):
                self.assertEqual(launchcheck.light_check(bad),
                                 {"result": "slot_fault", "part": part})
                bad.assert_not_called()                  # a slot fault is reported first
        with mock.patch("importlib.import_module"), \
                mock.patch.object(launchcheck, "_guard_holds", return_value=False):
            self.assertEqual(launchcheck.light_check(bad), {"result": "slot_fault",
                                                            "part": "guard"})

    @unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
    def test_the_self_test_adds_no_dll_folder_named_by_the_environment(self):
        # #357 (T25), review of 96cc8ee: the launcher now starts --self-test on
        # its own before a migration, with the user's environment, and it
        # imports CTranslate2, which adds %HIP_PATH%\bin to the DLL search path
        # when it loads, and PyTorch and TorchCodec, which add folders named by
        # PYTHONUSERBASE, NVTOOLSEXT_PATH and CUDA_PATH_V* and the folder of an
        # ffmpeg on PATH (#358). As in every worker, the self-test clears them
        # before importing any library; if it cannot, it imports nothing and
        # fails.
        import io

        from workspace import __main__ as main
        from workspace import speaker_load, worker
        seen = {}
        names = worker.DLL_PATH_VARIABLES + ("CUDA_PATH_V13_0",)

        def imp(name):
            seen[name] = {v: os.environ.get(v) for v in names}
            seen[name]["ffmpeg"] = shutil.which("ffmpeg")
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        Path(scratch.name, "ffmpeg.exe").write_bytes(b"")
        hip = {name: "C:\\synthetic-hip" for name in names}
        hip["PATH"] = os.pathsep.join([scratch.name, os.environ.get("PATH", "")])
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, hip))
            stack.enter_context(mock.patch("importlib.import_module", side_effect=imp))
            stack.enter_context(mock.patch.object(main.offline_guard,
                                                  "disable_onnxruntime_telemetry"))
            stack.enter_context(mock.patch.object(speaker_load, "self_check", return_value=[]))
            stack.enter_context(mock.patch.object(main, "dpapi_round_trip",
                                                  return_value=(True, "dpapi round trip")))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            main.self_test()
        self.assertIn("ctranslate2", seen)
        self.assertIn("faster_whisper", seen)
        self.assertIn("torch", seen)
        self.assertIn("pyannote.audio", seen)
        nothing = {v: None for v in names + ("ffmpeg",)}
        self.assertEqual({name: env for name, env in seen.items() if env != nothing}, {})
        # The clearing cannot be loaded: nothing is imported and the self-test fails.
        seen.clear()
        out = io.StringIO()
        with mock.patch.dict(os.environ, hip), \
                mock.patch.dict(sys.modules, {"workspace.worker": None}), \
                mock.patch("importlib.import_module", side_effect=imp), \
                contextlib.redirect_stdout(out):
            self.assertEqual(main.self_test(), 1)
        self.assertEqual(seen, {})
        self.assertEqual(out.getvalue().splitlines()[-1], "FAIL")

    # PyTorch reads the user base and ProgramFiles only on Windows, where
    # sysconfig always has a user base (on Linux it can be None).
    @unittest.skipUnless(sys.platform == "win32" and HAVE_DEPS, "Windows app dependencies")
    def test_the_self_test_clears_the_user_base_and_sets_program_files_first(self):
        # #358 (T25): PyTorch reads the user base from sysconfig, which keeps
        # the first one it finds, PYTHONUSERBASE included, and builds its
        # default NVTOOLSEXT and CUDA folders from ProgramFiles, which Windows
        # sets for a 64-bit process from ProgramW6432. Started as the launcher
        # starts it (python -B -m workspace --self-test), the self-test clears
        # that variable before anything in the app reads sysconfig, and sets
        # ProgramFiles to the real Program Files folder, so no library it
        # imports sees the user base or the Program Files folder the
        # environment names. Library imports are recorded instead of run, in
        # a fresh process.
        from workspace import update
        code = textwrap.dedent("""
            import json, os, sys, sysconfig
            from unittest import mock
            from workspace import __main__ as main, speaker_load
            seen = []
            sys.argv = ["workspace", "--self-test"]
            with mock.patch("importlib.import_module",
                            side_effect=lambda name: seen.append(
                                [sysconfig.get_config_var("userbase"),
                                 os.environ.get("ProgramFiles")])), \\
                    mock.patch.object(main.offline_guard, "disable_onnxruntime_telemetry"), \\
                    mock.patch.object(speaker_load, "self_check", return_value=[]), \\
                    mock.patch.object(main, "dpapi_round_trip", return_value=(True, "dpapi")):
                main.main()
            print(json.dumps(seen))
            """)
        with tempfile.TemporaryDirectory() as scratch:
            userbase, pfiles = os.path.join(scratch, "userbase"), os.path.join(scratch, "pf")
            env = {k: v for k, v in os.environ.items()
                   if k.upper() not in ("PROGRAMFILES", "PROGRAMW6432")}
            env.update(PYTHONUSERBASE=userbase, ProgramFiles=pfiles, ProgramW6432=pfiles)
            r = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(ROOT), env=env,
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        seen = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(seen)
        real = update._program_files()
        self.assertNotIn(os.path.normcase(userbase),
                         {os.path.normcase(u) for u, _ in seen if u})
        self.assertEqual({p for _, p in seen}, {real})

    @unittest.skipUnless(sys.platform == "win32" and HAVE_DEPS
                         and importlib.util.find_spec("webview"), "Windows app dependencies")
    def test_the_switch_prints_one_result_and_opens_nothing(self):
        # The real child, as the launcher starts it, without a window, a
        # database or a key (no workspace is named: it has none to open).
        r = subprocess.run([sys.executable, "-B", "-m", "workspace", "--launch-check"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        answer = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(answer, {"result": "ok", "part": None}, r.stderr[-2000:])
        self.assertEqual(r.returncode, 0)


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class HealthTests(unittest.TestCase):
    """--health's launcher fields (#111 B5b): states only; a fallback or a
    fault needs attention, an update waiting does not."""

    def test_the_launchers_fields(self):
        from workspace import __main__ as main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            (root / "logs").mkdir(parents=True)

            def health(**state):
                if state:
                    (root / "logs" / "launch.json").write_text(json.dumps(state),
                                                               encoding="ascii")
                with mock.patch.object(main, "_system_account", return_value=False), \
                        mock.patch.object(main, "_app_running", return_value=False):
                    report = main.health(root)
                return (report["slot"], report["update_waiting"], report["self_check"],
                        report["healthy"])
            self.assertEqual(health(), ("none", False, "unknown", True))      # per-user
            self.assertEqual(health(last_role="current", last_check="ok"),
                             ("current", False, "ok", True))
            self.assertEqual(health(last_role="compat", last_check="ok"),
                             ("compat", True, "ok", True))                  # workload
            self.assertEqual(health(last_role="fallback", last_check="slot_fault"),
                             ("fallback", False, "slot_fault", False))
            self.assertEqual(health(last_role="current", last_check="env_fault"),
                             ("current", False, "env_fault", False))
            self.assertEqual(health(last_role="newer", last_check="C:\\x"),
                             ("none", False, "unknown", True))


@unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
class FinishOnlyTests(unittest.TestCase):
    """Finish-only mode (#111 B5b, the owner's decision B5-7): a version
    opened as compat refuses new imports, intake and recordings, in the
    bridge and in the Workspace; review, editing, handoff, cleanup and holds
    work."""

    def setUp(self):
        import profile_elsewhere
        from test_app import fake, write_wav
        profile_elsewhere.start(self)       # folders in %TEMP%, inside AppData (#111 D2)

        from workspace import api
        from workspace.app import Workspace
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ws = Workspace(self.root / "ws", self.root / "models", start=False,
                            allow_test_engines=True, battery=False, finish_only=True,
                            protector=keyprotect.TestProtector())
        self.addCleanup(self.ws.close)
        self.ws.settings.update(model=fake("sentences"), speakers=False)
        self.ws._import_one(write_wav(self.root / "a.wav", 20))   # made before the update
        self.mid = self.ws.meetings()[0]["id"]
        # Processing goes on in finish-only mode: a meeting is transcribed.
        job = self.ws._next_work()
        for _ in range(20):
            if not job:
                break
            self.ws._run(*job)
            job = self.ws._next_work()
        self.wav = write_wav(self.root / "b.wav", 5)
        self.api = api.make_api(self.ws, confirm=lambda question: True, decline_pause_s=0)

    def test_new_meetings_are_refused_by_the_bridge_without_calling_the_workspace(self):
        from workspace import api
        from workspace.app import FINISH_ONLY_TEXT
        self.assertEqual(api.FINISH_ONLY_REFUSED,
                         {"import_dialog", "start_recording", "scan_now"})
        self.assertTrue(api.FINISH_ONLY_REFUSED <= api.API_METHODS - api.LOCKED_METHODS)
        refused = {"ok": False, "error": FINISH_ONLY_TEXT, "kind": "info"}
        with mock.patch.object(self.ws, "import_paths") as imported, \
                mock.patch.object(self.ws, "start_recording") as recorded, \
                mock.patch.object(self.ws, "check_can_record") as checked:
            self.assertEqual(self.api.import_dialog(), refused)
            self.assertEqual(self.api.start_recording(0, "Microphone"), refused)
            self.assertEqual(self.api.scan_now(), refused)
        imported.assert_not_called()
        recorded.assert_not_called()
        checked.assert_not_called()
        self.assertTrue(self.api.status()["value"]["finish_only"])

    def test_the_workspace_refuses_them_itself(self):
        from workspace.app import FINISH_ONLY_TEXT, WorkspaceError
        for call in (lambda: self.ws.import_paths([str(self.wav)]),
                     lambda: self.ws.start_recording(0, "Microphone", consent=None),
                     lambda: self.ws.check_can_record(0, "Microphone"),
                     self.ws.scan_now):
            with self.assertRaises(WorkspaceError) as caught:
                call()
            self.assertEqual(str(caught.exception), FINISH_ONLY_TEXT)
        self.assertEqual(self.ws._imports.unfinished_tasks, 0)

    @unittest.skipUnless(importlib.util.find_spec("webview"), "pywebview not installed")
    def test_a_drop_imports_nothing(self):
        from workspace import api

        class Events:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self
        window = mock.Mock()
        window._drop_enabled = False
        window.dom.document.events.dragover = Events()
        window.dom.document.events.drop = drops = Events()
        with mock.patch.object(self.ws, "import_paths") as imported:
            api.enable_drop(window, self.ws)
            drops.handlers[0].callback(
                {"dataTransfer": {"files": [{"pywebviewFullPath": str(self.wav)}]}})
        imported.assert_not_called()

    def test_watched_folders_stay_configured_and_are_not_scanned(self):
        from workspace import intake
        folder = self.root / "watched"
        folder.mkdir()
        fid = self.ws.add_watched_folder(str(folder), True)      # a setting: kept
        self.assertTrue(fid)
        with mock.patch.object(intake, "scan", side_effect=AssertionError("scanned")):
            self.ws._scan_folders()
        self.assertEqual(self.ws.last_scan, {"finish_only": True})
        self.assertEqual(len(self.ws.watched_folders()["folders"]), 1)

    def test_review_editing_handoff_cleanup_and_holds_work(self):
        page = self.api
        first = page.transcript(self.mid)["value"]["items"][0]
        self.assertTrue(page.edit(self.mid, first["id"], "Edited while finishing")["ok"])
        self.assertTrue(page.rename(self.mid, "Finishing")["ok"])
        hold = page.add_hold(self.mid, "all", "Synthetic reason")
        self.assertTrue(hold["ok"], hold)
        self.assertTrue(page.release_hold(hold["value"])["ok"])
        self.assertTrue(page.confirm_reviewed(self.mid)["ok"])
        tid = page.handoff_templates()["value"][0]["id"]
        self.assertTrue(page.handoff_preview(self.mid, tid)["ok"])
        self.assertTrue(page.confirm_final_saved(self.mid)["ok"])
        self.assertTrue(page.settings()["ok"])

    def test_the_page_shows_the_banner_and_hides_record_and_import(self):
        from workspace.app import FINISH_ONLY_BANNER
        js = (ROOT / "workspace" / "ui" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "workspace" / "ui" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "workspace" / "ui" / "app.css").read_text(encoding="utf-8")
        text = re.search(r'const FINISH_ONLY_TEXT = "([^"]*)" \+\s*"([^"]*)";', js)
        self.assertEqual(text.group(1) + text.group(2), FINISH_ONLY_BANNER)
        self.assertIn('id="finish-only"', html)
        self.assertIn("renderFinishOnly(s.finish_only)", js)
        self.assertIn(".start.finish-only { display: none; }", css)
        # Settings' "Check now" for watched folders (scan_now) is not offered either.
        self.assertIn('id="s-scan"', html)
        self.assertIn('$("s-scan").hidden = on;', js)


class GpuUpkeepTests(unittest.TestCase):
    """Unused graphics card components (#111 B5b, the owner's decision B5-4):
    removed when no installed version names them and this user has one
    install; links refused; never while busy; current version only."""

    def setUp(self):
        from workspace import gpu
        self.gpu = gpu
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.cache = self.base / "models"
        self.folders = {}
        for name in ("torch-2.13.0-cu130", "torch-2.13.0-cu130.invalid-0a1b2c",
                     "torch-2.13.0-cu130.partial", gpu.VERSION, gpu.WHISPER_VERSION,
                     "torch-2.14.1-cu130.invalid-00ff", "cublas-12.6-cu12"):
            folder = self.cache / "gpu" / name
            (folder / "site").mkdir(parents=True)
            (folder / "site" / "lib.dll").write_bytes(b"x" * 1000)
            self.folders[name] = folder

    def layout(self, gpu_lists):
        return InstallLayout(self.base, {"0.9": 2, "1.0": 2}, "1.0", previous="0.9",
                             gpu=gpu_lists)

    def test_the_keep_list_is_every_installed_versions_components(self):
        g = self.gpu
        lay = self.layout({"0.9": ["torch-2.13.0-cu130", "cublas-12.6-cu12"],
                           "1.0": [g.VERSION, g.WHISPER_VERSION]})
        self.assertEqual(update.gpu_keep(lay.root), {"torch-2.13.0-cu130", "cublas-12.6-cu12",
                                                     g.VERSION, g.WHISPER_VERSION})
        for bad in ([], None, ["..\\x"], "torch", [1]):
            with self.subTest(bad=bad):
                lay = self.layout({"0.9": bad, "1.0": [g.VERSION]})
                if bad is None:
                    info = json.loads((lay.versions / "0.9" / "version.json").read_text())
                    info.pop("gpu_components")
                    (lay.versions / "0.9" / "version.json").write_text(json.dumps(info))
                self.assertIsNone(update.gpu_keep(lay.root))     # keep everything
        (lay.root / "current.json").write_text('{"version": "1.0"}')     # format 1
        self.assertIsNone(update.gpu_keep(lay.root))

    def test_superseded_copies_are_removed_and_the_rest_kept(self):
        g = self.gpu
        log = mock.Mock()
        removed = g.remove_superseded(self.cache, {g.VERSION, g.WHISPER_VERSION}, log=log)
        self.assertEqual(sorted(v for v, _ in removed),
                         ["cublas-12.6-cu12", "torch-2.13.0-cu130", "torch-2.13.0-cu130",
                          "torch-2.13.0-cu130"])
        self.assertTrue(all(size == 1000 for _, size in removed))
        left = sorted(p.name for p in (self.cache / "gpu").iterdir())
        self.assertEqual(left, sorted([g.VERSION, g.WHISPER_VERSION,
                                       "torch-2.14.1-cu130.invalid-00ff"]))
        for call in log.write.call_args_list:
            self.assertEqual(call.args[0], "gpu_removed")
            self.assertEqual(set(call.kwargs), {"component", "bytes"})

    @unittest.skipUnless(sys.platform == "win32", "junctions")
    def test_a_link_is_never_followed(self):
        import _winapi
        g = self.gpu
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sentinel.txt").write_text("keep")
        # A junction named like an old component, and one inside an old copy.
        _winapi.CreateJunction(str(outside), str(self.cache / "gpu" / "torch-1.0-cu118"))
        _winapi.CreateJunction(str(outside), str(self.folders["cublas-12.6-cu12"] / "site" / "x"))
        g.remove_superseded(self.cache, {g.VERSION, g.WHISPER_VERSION})
        self.assertEqual((outside / "sentinel.txt").read_text(), "keep")
        self.assertTrue(os.path.lexists(self.cache / "gpu" / "torch-1.0-cu118"))
        self.assertFalse(self.folders["cublas-12.6-cu12"].exists())
        os.rmdir(self.cache / "gpu" / "torch-1.0-cu118")
        # A link at the gpu folder itself: nothing at all.
        moved = self.base / "real-gpu"
        (self.cache / "gpu").rename(moved)
        _winapi.CreateJunction(str(moved), str(self.cache / "gpu"))
        self.addCleanup(lambda: os.path.lexists(self.cache / "gpu")
                        and os.rmdir(self.cache / "gpu"))
        (moved / "torch-0.1-cu100").mkdir()
        self.assertEqual(g.remove_superseded(self.cache, {g.VERSION}), [])
        self.assertTrue((moved / "torch-0.1-cu100").is_dir())

    def test_the_other_install_is_looked_for_in_the_other_hive(self):
        if sys.platform != "win32":
            self.assertIsNone(update.other_install("self-service"))
            return
        import winreg
        seen = []

        def opener(found):
            def open_key(hive, key, reserved, access):
                seen.append((hive, key, access))
                if found is True:
                    return mock.Mock()
                raise found
            return open_key
        self.assertTrue(update.other_install("self-service", opener(True)))
        self.assertEqual(seen[-1], (winreg.HKEY_LOCAL_MACHINE, update.UNINSTALL_KEY,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY))
        self.assertFalse(update.other_install("machine", opener(FileNotFoundError())))
        self.assertEqual(seen[-1][0], winreg.HKEY_CURRENT_USER)
        self.assertIsNone(update.other_install("managed", opener(PermissionError())))
        self.assertEqual(update.UNINSTALL_KEY,
                         r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute")

    @unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
    def test_the_workspace_removes_only_with_one_install_and_when_idle(self):
        from workspace.app import Workspace
        g = self.gpu
        lay = self.layout({"0.9": [g.VERSION], "1.0": [g.VERSION, g.WHISPER_VERSION]})
        ws = Workspace(self.base / "ws", self.cache, start=False, battery=False,
                       protector=keyprotect.TestProtector())
        self.addCleanup(ws.close)
        old = self.folders["torch-2.13.0-cu130"]
        for other in (True, None):                    # another install, or cannot tell
            with mock.patch.object(update, "other_install", return_value=other) as asked:
                self.assertEqual(ws.remove_unused_gpu(lay.root, "self-service"), [])
            asked.assert_called_once_with("self-service")
            self.assertTrue(old.is_dir())
        with mock.patch.object(ws.downloads, "busy", return_value=True):
            self.assertEqual(ws.remove_unused_gpu(lay.root, "self-service", False), [])
        ws._gpu_pass = True
        self.assertEqual(ws.remove_unused_gpu(lay.root, "self-service", False), [])
        ws._gpu_pass = False
        self.assertTrue(old.is_dir())
        lay2 = self.layout({"0.9": None, "1.0": [g.VERSION]})          # unknown: keep
        info = json.loads((lay2.versions / "0.9" / "version.json").read_text())
        info.pop("gpu_components", None)
        (lay2.versions / "0.9" / "version.json").write_text(json.dumps(info))
        self.assertEqual(ws.remove_unused_gpu(lay2.root, "self-service", False), [])
        self.assertTrue(old.is_dir())
        lay = self.layout({"0.9": [g.VERSION], "1.0": [g.VERSION, g.WHISPER_VERSION]})
        removed = ws.remove_unused_gpu(lay.root, "self-service", False)
        self.assertIn("torch-2.13.0-cu130", [v for v, _ in removed])
        self.assertFalse(old.exists())
        self.assertTrue(self.folders[g.VERSION].is_dir())
        logged = (self.base / "ws" / "logs" / "app.log").read_text(encoding="ascii")
        self.assertIn('"kind":"gpu_removed"', logged)
        self.assertIn('"why":"other_install"', logged)
        self.assertNotIn(str(self.base), logged)

    @unittest.skipUnless(HAVE_DEPS, "numpy/av/cryptography not installed")
    def test_only_the_current_version_starts_the_upkeep_and_compat_is_finish_only(self):
        # The app's own start (__main__.main) as each role, up to the window.
        from workspace import __main__ as main
        from workspace import app as app_module
        from workspace import diag, shell
        real = app_module.Workspace
        made = []

        def here(*a, **kw):                   # the app's own call, on this test's folder
            kw.pop("cache_dir", None)
            made.append(real(self.base / "ws", self.cache, start=False, battery=False,
                             protector=keyprotect.TestProtector(), **kw))
            return made[-1]
        for role, upkeep in ((None, True), ("current", True), ("compat", False),
                             ("fallback", False)):
            with self.subTest(role=role), contextlib.ExitStack() as stack:
                for target, name, kw in (
                        (app_module, "Workspace", {"side_effect": here}),
                        (launch, "start", {"return_value": launch.Outcome(role=role)}),
                        (main, "_single_instance", {"return_value": True}),
                        (main, "model_cache", {"return_value": self.cache}),
                        (diag, "install", {"return_value": False}),
                        (diag, "summarise_output", {}),
                        (shell, "create_window", {"side_effect": RuntimeError("the window")}),
                        (sys, "argv", {"new": ["workspace"]})):
                    stack.enter_context(mock.patch.object(target, name, **kw))
                started = stack.enter_context(mock.patch.object(real, "start_gpu_upkeep"))
                try:
                    with self.assertRaisesRegex(RuntimeError, "the window"):
                        main.main()
                    # A compat start opens the workspace for finishing only (B5-7).
                    self.assertEqual(made[-1].finish_only, role == "compat")
                finally:
                    made.pop().close()
                self.assertEqual(started.called, upkeep)
                self.assertEqual(made, [])
        # A locked workspace starts none.
        ws = mock.Mock(locked=RuntimeError("locked"))
        with mock.patch("threading.Thread") as thread:
            real.start_gpu_upkeep(ws)
        thread.assert_not_called()


class ImportTests(unittest.TestCase):
    def test_the_pass_and_the_lock_import_only_the_standard_library(self):
        for name in ("launch.py", "instance.py", "launchcheck.py"):
            text = (ROOT / "workspace" / name).read_text(encoding="utf-8")
            imports = re.findall(r"(?m)^\s*(?:from|import) ([\w.]+)", text)
            outside = {i.partition(".")[0] for i in imports} - set(sys.stdlib_module_names)
            self.assertLessEqual(outside, {"workspace"}, name)


if __name__ == "__main__":
    unittest.main()
