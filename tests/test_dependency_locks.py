# Hash-locked dependencies (#111 B1): the locks are complete and agree with
# their inputs and the build, the subset closure is right, and audit ignores
# expire. No network: tools/lock.py --check (CI) re-resolves against PyPI.
#   python -m unittest discover -s tests -v

import datetime
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

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

    def test_every_lock_records_its_cooldown(self):
        for _, target, _, _ in lock.LOCKS:
            cutoff = lock.recorded_cutoff(read(target))
            self.assertIsNotNone(cutoff, target)
            datetime.date.fromisoformat(cutoff)

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
        with self.assertRaises(SystemExit):
            lock.subset(text, ["not-a-package"])

    def test_the_build_installs_only_the_lock_with_hashes(self):
        source = (ROOT / "tools" / "build_package.py").read_text(encoding="utf-8")
        body = source[source.index("def install_wheels"):source.index("def write_pth")]
        for flag in ('"--require-hashes"', '"--no-deps"', '"--only-binary=:all:"'):
            self.assertIn(flag, body)
        self.assertIn('ROOT / "requirements.txt"', body)
        self.assertFalse((ROOT / "workspace" / "requirements.txt").exists())


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
        targets = {target for _, target, _, _ in lock.LOCKS}
        self.assertTrue(targets <= set(audit.LOCKS))
        for path in audit.LOCKS:
            self.assertTrue((ROOT / path).is_file(), path)


if __name__ == "__main__":
    unittest.main()
