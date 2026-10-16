# Hash-locked dependencies (#111 B1): the locks are complete and agree with
# their inputs and the build, the subset closure is right, and audit ignores
# expire. No network: tools/lock.py --check (CI) re-resolves against PyPI.
#   python -m unittest discover -s tests -v

import datetime
import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lock = load("lock")
audit = load("audit")
build = load("build_package")


def read(path):
    return (ROOT / path).read_text(encoding="utf-8").replace("\r\n", "\n")


class LockTests(unittest.TestCase):
    def test_every_lock_pins_every_package_with_hashes(self):
        for _, target, _, _ in lock.LOCKS:
            found = lock.entries(read(target))
            self.assertTrue(found, target)
            for name, (block, _) in found.items():
                # An exact pin, with an environment marker in the CI tool locks.
                self.assertRegex(block[0], r"^[A-Za-z0-9][\w.-]*==[^\s;]+( ; [^\\]+)? \\$",
                                 f"{target}: {name} is not an exact pin")
                self.assertTrue(any(re.match(r"^\s+--hash=sha256:[0-9a-f]{64}", line)
                                    for line in block), f"{target}: {name} has no hash")

    def test_every_line_of_a_lock_belongs_to_a_pinned_entry(self):
        # A hand-added line (say "requests>=2") would be installed unhashed or
        # break --require-hashes; only pins, their hashes and notes may appear.
        for _, target, _, _ in lock.LOCKS:
            for line in read(target).split("\n"):
                self.assertRegex(line, r"^(|#.*|[A-Za-z0-9][\w.-]*==\S+.*\\|"
                                       r"    --hash=sha256:[0-9a-f]{64}( \\)?|    # via .*)$",
                                 f"{target}: {line!r}")

    def test_every_lock_records_its_cooldown(self):
        for _, target, _, _ in lock.LOCKS:
            cutoff = lock.recorded_cutoff(read(target))
            self.assertIsNotNone(cutoff, target)
            datetime.datetime.strptime(cutoff, lock.STAMP)

    def test_direct_dependencies_are_in_the_lock_as_pinned(self):
        found = lock.entries(read("requirements.txt"))
        versions = {name: block[0].split("==")[1].split()[0]
                    for name, (block, _) in found.items()}
        for line in read("requirements.in").split("\n"):
            m = re.match(r"^([A-Za-z0-9][\w.-]*)(==|>=)(\S+)", line)
            if not m:
                continue
            name = lock.norm(m.group(1))
            self.assertIn(name, versions, f"{name} is in requirements.in but not the lock")
            if m.group(2) == "==":
                self.assertEqual(versions[name], m.group(3), name)
            else:                                   # a security floor
                got = tuple(int(x) for x in versions[name].split(".")[:3])
                floor = tuple(int(x) for x in m.group(3).split(".")[:3])
                self.assertGreaterEqual(got, floor, name)

    def test_the_vendored_wheel_has_the_same_hash_in_the_lock_and_the_build(self):
        block, _ = lock.entries(read("requirements.txt"))["proxy-tools"]
        digests = {re.search(r"sha256:([0-9a-f]{64})", line).group(1)
                   for line in block if "--hash" in line}
        self.assertEqual(digests, set(build.VENDORED.values()))

    def test_uv_is_pinned_to_the_version_lock_py_needs(self):
        found = lock.entries(read("requirements/ci-lock.txt"))
        self.assertEqual(found["uv"][0][0].split("==")[1].split()[0], lock.pinned_uv())

    def test_subset_brings_its_dependencies(self):
        text = read("requirements.txt")
        names = set(lock.entries(lock.subset(text, ["pywebview"])))
        self.assertTrue({"pywebview", "pythonnet", "clr-loader", "proxy-tools", "bottle",
                         "cffi", "pycparser"} <= names, names)
        self.assertNotIn("torch", names)
        self.assertEqual(set(lock.entries(lock.subset(text, ["numpy"]))), {"numpy"})
        with self.assertRaises(lock.LockError):
            lock.subset(text, ["not-a-package"])

    def test_packaging_wheels_holds_only_the_vendored_wheels(self):
        # A wheel added there would be found by the next re-lock and the build.
        self.assertEqual({p.name for p in (ROOT / "packaging" / "wheels").iterdir()},
                         set(build.VENDORED))

    def test_the_build_installs_only_the_lock_with_hashes(self):
        source = (ROOT / "tools" / "build_package.py").read_text(encoding="utf-8")
        body = source[source.index("def install_wheels"):source.index("def write_pth")]
        for flag in ('"--require-hashes"', '"--no-deps"', '"--only-binary=:all:"'):
            self.assertIn(flag, body)
        self.assertIn('ROOT / "requirements.txt"', body)
        self.assertFalse((ROOT / "workspace" / "requirements.txt").exists())


class CheckTests(unittest.TestCase):
    """--check takes every hash afresh from the index (#111 B1 review): uv is
    given the lock's versions only, as constraints, and writes into an empty
    file, so a hash edited into the lock can never be carried through."""

    def run_check(self, tamper=None):
        targets = {source: target for source, target, _, _ in lock.LOCKS}
        calls = []
        real_read = Path.read_text

        def fake_run(cmd, **kw):
            if cmd[-1] == "--version":
                return subprocess.CompletedProcess(cmd, 0, f"uv {lock.pinned_uv()} (test)\n")
            out = Path(cmd[cmd.index("-o") + 1])
            constraints = Path(cmd[cmd.index("-c") + 1]) if "-c" in cmd else None
            calls.append({"empty": not out.exists(), "refresh": "--refresh" in cmd,
                          "pins": constraints.read_text(encoding="utf-8") if constraints
                          else None, "target": targets[cmd[cmd.index("compile") + 1]]})
            # What uv writes: the index's hashes (here, the committed, untampered
            # ones), with the constraints file noted in each "# via" line.
            fresh = lock.body(real_read(ROOT / calls[-1]["target"], encoding="utf-8"))
            fresh = re.sub(r"^(\s+# via .*)$", lambda m: f"{m.group(1)}, -c {constraints}",
                           fresh, flags=re.M)
            out.write_text(fresh, encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0)

        def read_text(path, *a, **kw):
            text = real_read(path, *a, **kw)
            return tamper(path, text) if tamper else text
        with mock.patch.object(lock.subprocess, "run", fake_run), \
                mock.patch.object(Path, "read_text", read_text):
            code = lock.main(["--check"])
        return code, calls

    def test_versions_only_in_and_an_empty_output(self):
        code, calls = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual({c["target"] for c in calls}, {t for _, t, _, _ in lock.LOCKS})
        for c in calls:
            self.assertTrue(c["empty"], c["target"])            # no hash carried in
            self.assertTrue(c["refresh"], c["target"])
            self.assertEqual(c["pins"], lock.pins(read(c["target"])))
            self.assertNotIn("--hash", c["pins"])

    def test_an_edited_hash_fails(self):
        def tamper(path, text):
            if path.name == "requirements.txt" and path.parent == ROOT:
                return re.sub(r"(--hash=sha256:)[0-9a-f]{64}", r"\g<1>" + "0" * 64, text,
                              count=1)
            return text
        code, _ = self.run_check(tamper)
        self.assertEqual(code, 1)

    def test_a_later_cut_off_than_the_cooldown_allows_fails(self):
        def tamper(path, text):
            if path.name == "requirements.txt" and path.parent == ROOT:
                return re.sub(r"(published before )\S+", r"\g<1>2999-01-01T00:00:00Z", text,
                              count=1)
            return text
        code, _ = self.run_check(tamper)
        self.assertEqual(code, 1)


class AuditIgnoreTests(unittest.TestCase):
    TODAY = datetime.date(2026, 10, 17)

    def entry(self, **over):
        e = {"id": "GHSA-xxxx-xxxx-xxxx", "package": "example", "reason": "not reachable",
             "until": "2026-11-01"}
        e.update(over)
        return e

    def test_a_current_entry_is_ignored(self):
        self.assertEqual(audit.active_ignores([self.entry()], self.TODAY),
                         ["GHSA-xxxx-xxxx-xxxx"])

    def test_expired_far_off_or_incomplete_entries_fail(self):
        for bad in (self.entry(until="2026-10-16"), self.entry(until="2027-02-01"),
                    self.entry(reason=""), {"id": "X", "package": "p", "until": "2026-11-01"},
                    self.entry(until="soon")):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                audit.active_ignores([bad], self.TODAY)

    def test_the_ignore_file_is_well_formed(self):
        entries = json.loads(read("requirements/audit-ignore.json"))
        self.assertIsInstance(entries, list)
        for e in entries:
            self.assertEqual(set(e), set(audit.FIELDS))

    def test_every_lock_is_audited(self):
        locks = {"requirements.txt"} | {f"requirements/{p.name}"
                                         for p in (ROOT / "requirements").glob("*.txt")}
        self.assertEqual(set(audit.LOCKS), locks)
        self.assertEqual({target for _, target, _, _ in lock.LOCKS}, locks)
        self.assertIn("--strict", (ROOT / "tools" / "audit.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
