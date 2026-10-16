# tests/test_consistency.py
# Single source of truth (docs/RELEASE_STANDARD.md, section 1). Where one fact
# still has more than one copy, these tests tie the copies together, so changing
# one without the other fails here rather than on a user's machine. When a copy
# is removed in favour of a generated value, delete its test.

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUDGETS = json.loads((ROOT / "tests" / "budgets.json").read_text(encoding="utf-8"))


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def one(pattern, text, what):
    """The single capture of pattern in text, or a failure naming what is missing."""
    found = re.findall(pattern, text, re.MULTILINE)
    if len(found) != 1:
        raise AssertionError(f"expected one {what}, found {len(found)}")
    return found[0]


class BridgeNameTests(unittest.TestCase):
    """The page may call only methods the bridge allowlist exposes."""

    @classmethod
    def setUpClass(cls):
        from workspace import api
        cls.allowed = api.API_METHODS
        cls.page = read("workspace/ui/app.js")

    def test_page_calls_only_allowlisted_methods(self):
        called = set(re.findall(r'\bcall\(\s*"(\w+)"', self.page))
        self.assertGreater(len(called), 20, "the call() pattern no longer matches app.js")
        self.assertEqual(set(), called - self.allowed,
                         "app.js calls bridge methods missing from API_METHODS in workspace/api.py")

    def test_call_is_the_only_dynamic_bridge_access(self):
        # Other than its own definition, every call() names its method with a
        # string literal, so the test above sees every method the page uses.
        dynamic = re.findall(r'(?<!function )\bcall\((?!\s*")', self.page)
        self.assertEqual([], dynamic, "app.js calls call() with a computed method name")
        self.assertEqual(1, self.page.count("pywebview.api"),
                         "app.js reaches the bridge outside call()")

    def test_design_fixtures_serve_only_allowlisted_methods(self):
        fixtures = read("tools/design_check/fixtures.js")
        start = fixtures.index("const handlers = {")
        end = fixtures.index("\n  };", start)
        served = set(re.findall(r"^    (\w+):", fixtures[start:end], re.MULTILINE))
        self.assertGreater(len(served), 10, "the handlers pattern no longer matches fixtures.js")
        self.assertEqual(set(), served - self.allowed,
                         "tools/design_check/fixtures.js serves methods the app does not expose")


class DependencyPinTests(unittest.TestCase):
    def test_gpu_wheels_match_requirement_pins(self):
        # The optional GPU component installs CUDA builds of torch and
        # torchaudio. They must be the same release as the CPU pins, or the
        # speaker worker runs different code on the GPU than on the CPU.
        from workspace import gpu
        requirements = read("requirements.txt")
        for wheel in gpu.WHEELS:
            pinned = one(rf"^{re.escape(wheel['name'])}==(\S+)", requirements,
                         f"{wheel['name']} pin in requirements.txt")
            self.assertEqual(pinned, wheel["version"].split("+")[0],
                             f"workspace/gpu.py ships {wheel['name']} {wheel['version']} "
                             f"but requirements.txt pins {pinned}")


class InstallIdentityTests(unittest.TestCase):
    """Names and codes shared by the app, the build and the installer scripts."""

    @classmethod
    def setUpClass(cls):
        cls.install = read("packaging/install.ps1")
        cls.uninstall = read("packaging/uninstall.ps1")

    def test_taskbar_id_matches_installer(self):
        app_id = one(r'^APP_ID = "([^"]+)"', read("workspace/__main__.py"), "APP_ID")
        shortcut_id = one(r'^\$AppId = "([^"]+)"', self.install, "$AppId in install.ps1")
        self.assertEqual(app_id, shortcut_id,
                         "a mismatch stops pinned shortcuts grouping with the window")

    def test_installer_exit_codes_match_the_gate(self):
        from workspace import update
        self.assertEqual(0, update.ALLOWED)
        for code in (update.WAIT, update.TOO_NEW):
            self.assertRegex(self.install, rf"(?m)^\s*{code} \{{ Stop-Install {code}\b",
                             f"install.ps1 does not pass on gate exit code {code}")
            self.assertRegex(read("docs/PACKAGING.md"), rf"(?m)^\| {code} \|",
                             f"docs/PACKAGING.md does not document exit code {code}")

    def test_app_name_matches_build_and_installers(self):
        built = one(r'"name":\s*"([^"]+)"', read("tools/build_package.py"), "package name")
        for script, text in (("install.ps1", self.install), ("uninstall.ps1", self.uninstall)):
            self.assertEqual(built, one(r'^\$AppName = "([^"]+)"', text, f"$AppName in {script}"))
        names = [one(r"^\$KnownAppNames = (@\(.*\))", text, "$KnownAppNames")
                 for text in (self.install, self.uninstall)]
        self.assertEqual(names[0], names[1],
                         "install.ps1 and uninstall.ps1 list different earlier names")
        self.assertIn(f'"{built}"', names[0])

    def test_install_folder_and_registry_key_match_uninstaller(self):
        for pattern, what in ((r'"(Programs\\[^"]+)"', "install folder"),
                              (r'^\$RegKey = "([^"]+)"', "uninstall registry key")):
            self.assertEqual(one(pattern, self.install, f"{what} in install.ps1"),
                             one(pattern, self.uninstall, f"{what} in uninstall.ps1"))


class DocsTests(unittest.TestCase):
    """Docs link to the source of truth instead of restating facts that change."""

    DOCS = ("README.md", "AGENTS.md", "SECURITY.md", *sorted(
        str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("*.md")))

    @classmethod
    def setUpClass(cls):
        workflow = read(".github/workflows/ci.yml")
        jobs = re.findall(r"^    name: (.+)$", workflow, re.MULTILINE)
        cls.jobs = [j.strip().strip('"') for j in jobs]

    def test_ci_jobs_found(self):
        self.assertGreaterEqual(len(self.jobs), 5, "the job name pattern no longer matches ci.yml")

    def test_required_checks_list_matches_ci_jobs(self):
        line = next(x for x in read("docs/RELEASING.md").splitlines()
                    if "Require status checks" in x)
        listed = set(re.findall(r"`([^`]+)`", line)) - {".github/workflows/ci.yml"}
        self.assertEqual(set(self.jobs), listed,
                         "docs/RELEASING.md's required checks differ from the jobs in ci.yml")

    def test_no_partial_lists_of_ci_jobs(self):
        # A line naming three or more CI jobs is a list of them, and has to name
        # them all. Better still, point to ci.yml or RELEASING.md instead.
        names = [j.lower() for j in self.jobs]
        for doc in self.DOCS:
            for number, line in enumerate(read(doc).splitlines(), 1):
                found = [n for n in names if n in line.lower()]
                if 3 <= len(found) < len(names):
                    self.fail(f"{doc}:{number} lists some CI jobs but not "
                              f"{sorted(set(names) - set(found))}; link to ci.yml instead")

    def test_no_hard_coded_test_counts(self):
        for doc in self.DOCS:
            for number, line in enumerate(read(doc).splitlines(), 1):
                match = re.search(r"\b\d{2,5} (?:unit )?tests\b", line)
                self.assertIsNone(match, f"{doc}:{number} states a test count that will go stale")


class DesignTokenTests(unittest.TestCase):
    def test_raw_colours_within_ratchet(self):
        css = read("workspace/ui/app.css")
        colours = re.findall(r"#[0-9A-Fa-f]{3,8}\b|\b(?:rgba?|hsla?)\(", css)
        budget = BUDGETS["app_css_raw_colours"]["value"]
        self.assertLessEqual(len(colours), budget,
                             "a colour was added to app.css directly; add it to tokens.css and "
                             "use the variable")


if __name__ == "__main__":
    unittest.main()
