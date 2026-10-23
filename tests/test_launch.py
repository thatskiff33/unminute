# tests/test_launch.py
# The launcher pass and the workspace lock (#111 B5a).
#   python tools/run_tests.py
#
# The launcher (workspace/launch.py, update.launch_decision and read_slots)
# decides, in machine mode only, which installed version opens a user's
# workspace; the workspace lock (workspace/instance.py) keeps a second
# Windows session of the same user out. Synthetic installs and workspaces
# only; nothing here starts a real app or reads the user's workspace.

import errno
import importlib.util
import json
import os
import re
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

from workspace import instance, launch, update  # noqa: E402

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
        self.assertEqual(rec["slots"], {"3.0": {"schema": 3, "seq": 9},
                                        "2.1": {"schema": 2, "seq": 8},
                                        "0.5.0": {"schema": None, "seq": 1}})
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
        self.assertIn("$Name -notmatch '\\.(partial|old)\\z'", install)

    def test_the_launch_options_match_the_installer_and_the_uninstaller(self):
        install = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        uninstall = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        args = " ".join(update.LAUNCH_ARGS)
        self.assertIn(f'$LaunchArgs = "{args}"', install)
        self.assertIn("$gateArgs = @(" + ", ".join(f'"{a}"' for a in update.LAUNCH_ARGS)
                      + ', "--gate")', install)
        self.assertIn(f'$start.Arguments = "{args} --restore-lid"', uninstall)


class InstallLayout:
    """A synthetic install: <root>\\versions\\<v>\\{app\\workspace, runtime}
    with version.json, and current.json format 2."""

    def __init__(self, base, slots, current, previous=None):
        self.root = Path(base) / "Unminute"
        self.versions = self.root / "versions"
        for name, schema in slots.items():
            self.version(name, schema)
        entries = {}
        for seq, (name, schema) in enumerate(slots.items(), 1):
            entries[name] = {"seq": seq, **({"schema": schema} if schema else {})}
        (self.root / "current.json").write_text(json.dumps(
            {"format": 2, "version": current, "path": str(self.versions / current),
             "previous": previous, "managed": True, "slots": entries}), encoding="utf-8-sig")

    def version(self, name, schema, stated=None):
        slot = self.versions / name
        (slot / "app" / "workspace").mkdir(parents=True, exist_ok=True)
        (slot / "runtime").mkdir(exist_ok=True)
        (slot / "runtime" / "pythonw.exe").write_bytes(b"synthetic")
        info = {"name": "Unminute", "version": stated or name}
        if schema:
            info["schema"] = schema
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

    def popen(self, argv, **kwargs):
        self.started.append((argv, kwargs))

    def run_pass(self, slot="3.0", argv=("workspace",), workspace=(3,), deployment=MACHINE,
                 executable=None):
        with FakeWorkspace(*workspace).patch():
            return launch.start(list(argv), here=self.layout.here(slot),
                                executable=executable or self.layout.exe(slot),
                                popen=self.popen, deployment=deployment,
                                workspace_db=self.base / "workspace.db")

    def assert_started(self, slot, role):
        self.assertEqual(len(self.started), 1, self.started)
        argv, kwargs = self.started[0]
        self.assertEqual(argv, [self.layout.exe(slot), *update.LAUNCH_ARGS, "--slot", role])
        self.assertEqual(kwargs["cwd"], str(self.layout.versions / slot / "app"))

    def test_a_per_user_install_starts_as_before_and_reads_nothing(self):
        def never(db):
            raise AssertionError("the workspace was read")
        with mock.patch.multiple(update, current_schema=never, blockers=never), \
                mock.patch.object(update, "read_slots", side_effect=AssertionError("read")):
            out = launch.start(["workspace"], here=self.layout.here("2.0"),
                               executable=self.layout.exe("2.0"), popen=self.popen,
                               deployment=PER_USER)
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
        self.assertEqual(self.run_pass(workspace=(2, found)), launch.Outcome(exit=0))
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
                                   workspace_db=self.base / "workspace.db")
            self.assertEqual(out, launch.Outcome(role="current"))
        # A real, damaged database file: unreadable, never an older format.
        db = self.base / "workspace.db"
        db.write_bytes(b"not a database" * 100)
        out = launch.start(["workspace"], here=self.layout.here("2.1"),
                           executable=self.layout.exe("2.1"), popen=self.popen,
                           deployment=MACHINE, workspace_db=db)
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


class ImportTests(unittest.TestCase):
    def test_the_pass_and_the_lock_import_only_the_standard_library(self):
        for name in ("launch.py", "instance.py"):
            text = (ROOT / "workspace" / name).read_text(encoding="utf-8")
            imports = re.findall(r"(?m)^\s*(?:from|import) ([\w.]+)", text)
            outside = {i.partition(".")[0] for i in imports} - set(sys.stdlib_module_names)
            self.assertLessEqual(outside, {"workspace"}, name)


if __name__ == "__main__":
    unittest.main()
