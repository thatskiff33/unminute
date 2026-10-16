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
        dynamic = re.findall(r'(?<!function )(?<![\w.])call\((?!\s*")', self.page)
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


class VersionTests(unittest.TestCase):
    def test_version_file_matches_the_newest_changelog_heading(self):
        # #131: VERSION is the one source of the version. tools/changelog.py sets
        # it together with the changelog section, the build reads it, and the
        # Release workflow refuses a tag that differs.
        version = read("VERSION").strip()
        self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+(-preview\.[0-9]+)?$")
        newest = one(r"\A(?:.*\n)*?## v(\S+)", read("CHANGELOG.md"), "changelog version heading")
        self.assertEqual(newest, version,
                         "VERSION and the newest changelog heading differ; tools/changelog.py "
                         "sets both when a release is prepared")


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
        self.assertRegex(self.install, rf"(?m)^\s*{update.ALLOWED} \{{ Write-Step \$message",
                         "install.ps1 does not continue on the gate's 'allowed' exit code")
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


def markdown_docs():
    """Every Markdown file except the changelog and its fragments, which record history."""
    skip = {".git", "node_modules", "changelog.d", "build", "dist", "extracted",
            ".venv", "venv", "env", "__pycache__"}
    return sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*.md")
                  if not skip & set(p.relative_to(ROOT).parts) and p.name != "CHANGELOG.md")


class DocsTests(unittest.TestCase):
    """Docs link to the source of truth instead of restating facts that change."""

    DOCS = markdown_docs()

    @classmethod
    def setUpClass(cls):
        workflow = read(".github/workflows/ci.yml")
        jobs = workflow[workflow.index("\njobs:"):]
        cls.job_ids = re.findall(r"^  ([\w-]+):\s*$", jobs, re.MULTILINE)
        names = re.findall(r"^    name: (.+)$", jobs, re.MULTILINE)
        cls.jobs = [j.strip().strip('"') for j in names]

    def test_ci_jobs_found_and_all_named(self):
        self.assertGreaterEqual(len(self.jobs), 5, "the job name pattern no longer matches ci.yml")
        # A job without a name: shows its id as the check name, which the
        # lists below would not know about.
        self.assertEqual(len(self.job_ids), len(self.jobs), "every ci.yml job needs a name:")

    def test_required_checks_list_matches_ci_jobs(self):
        line = one(r"^.*Require status checks.*$", read("docs/RELEASING.md"),
                   "'Require status checks' line in docs/RELEASING.md")
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
        # Not a version or issue number ("Python 3.12 unit tests", "#123 tests").
        count = re.compile(r"(?<![\w.#])\d{2,5}\+?\s+(?:[\w-]+\s+){0,2}tests?\b",
                           re.IGNORECASE)
        for doc in self.DOCS:
            text = " ".join(read(doc).split())          # counts can wrap onto the next line
            match = count.search(text)
            self.assertIsNone(match, f"{doc} states a test count that will go stale: "
                                     f"{match and match.group(0)!r}")


# CSS named colours (CSS Color Module Level 4), so "white" counts like #FFFFFF.
NAMED_COLOURS = set("""aliceblue antiquewhite aqua aquamarine azure beige bisque black
blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse chocolate coral
cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen
darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon
darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink
deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro
ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory
khaki lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen
lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen
magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream
mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen
steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow
yellowgreen""".split())
COLOUR = re.compile(r"#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color"
                    r"|color-mix)\(|(?<![\w-])(" + "|".join(sorted(NAMED_COLOURS)) + r")(?![\w-])",
                    re.IGNORECASE)


def raw_colours(css):
    """Colour literals in declaration values (not selectors, not comments)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    values = re.findall(r":\s*([^;{}]+)[;}]", css)
    return [m.group(0) for v in values for m in COLOUR.finditer(v)]


class DesignTokenTests(unittest.TestCase):
    def test_colour_pattern(self):
        found = raw_colours("/* #133 */ .red, .a:hover { color: white; background: #fff; "
                            "border: 1px solid oklch(0.5 0.1 200); white-space: nowrap; "
                            "fill: var(--x); }")
        self.assertEqual(["white", "#fff", "oklch("], found)

    def test_raw_colours_within_ratchet(self):
        colours = raw_colours(read("workspace/ui/app.css"))
        budget = BUDGETS["app_css_raw_colours"]["value"]
        self.assertLessEqual(len(colours), budget,
                             "a colour was added to app.css directly; add it to tokens.css and "
                             "use the variable")


if __name__ == "__main__":
    unittest.main()
