# tests/test_consistency.py
# Single source of truth (docs/RELEASE_STANDARD.md, section 1). Where one fact
# still has more than one copy, these tests tie the copies together, so changing
# one without the other fails here rather than on a user's machine. When a copy
# is removed in favour of a generated value, delete its test.

import ast
import json
import re
import subprocess
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
        # Handlers added after the literal (one mode's only, as the transcribing
        # mode's edit, #127) count too: handlers.name = ... or handlers["name"] = ...
        added = {dotted or quoted for dotted, quoted in re.findall(
            r"\bhandlers(?:\.(\w+)|\[\s*[\"'](\w+)[\"']\s*\])\s*=(?!=)", fixtures)}
        self.assertIn("edit", added, "the added-handler pattern no longer matches fixtures.js")
        served |= added
        self.assertEqual(set(), served - self.allowed,
                         "tools/design_check/fixtures.js serves methods the app does not expose")

    def test_locked_methods_match_agents_md_and_the_docs(self):
        # #111 C4a, I3: while locked the page reaches only LOCKED_METHODS (the
        # user's decision, 21 Oct 2026). AGENTS.md names both lists, and the
        # docs' copies of the locked list equal the code's, so widening it
        # shows in the docs and the PR (AGENTS.md, E2).
        from workspace import api
        locked = api.LOCKED_METHODS
        self.assertLessEqual(locked, self.allowed, "LOCKED_METHODS outside API_METHODS")
        self.assertNotIn("continue_despite_damage", locked)
        text = read("AGENTS.md")
        start = text.index("- **I3**")
        i3 = text[start:text.index("\n- **", start + 1)]
        for name in ("API_METHODS", "LOCKED_METHODS"):
            self.assertIn(f"`{name}`", i3, f"AGENTS.md I3 does not name {name}")
        for doc in ("docs/ENCRYPTION.md", "docs/WORKSPACE_DESIGN.md"):
            listed = one(r"`api\.LOCKED_METHODS` \(([^)]*)\)", read(doc),
                         f"LOCKED_METHODS list in {doc}")
            self.assertEqual(locked, set(re.findall(r"`(\w+)`", listed)),
                             f"{doc} lists different locked methods from workspace/api.py")


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

    def test_whisper_gpu_component_matches_the_ctranslate2_pin(self):
        # #248: the cuBLAS component is the one CTranslate2's Windows build
        # loads. Upgrading CTranslate2 means checking that again (its DLL's
        # imports and its build's CUDA version) and re-validating on a GPU.
        from workspace import gpu
        pinned = one(r"^ctranslate2==(\S+)", read("requirements.txt"), "ctranslate2 pin")
        self.assertEqual(pinned, gpu.WHISPER_CTRANSLATE2,
                         "requirements.txt moved CTranslate2: check the cuBLAS it loads and "
                         "update workspace/gpu.py's Whisper component with it")


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

    def test_no_release_is_prepared_until_phase_c_is_complete(self):
        # #111 C2b-2: Phase C ships as one release. Until C7 sets
        # PHASE_C_COMPLETE, VERSION stays what it was when C2b-2 merged, so a
        # release-prep written by hand (not by tools/changelog.py, which
        # refuses) fails here.
        from workspace import keystore
        if not keystore.PHASE_C_COMPLETE:
            self.assertEqual(read("VERSION").strip(), keystore.PHASE_C_FROZEN_VERSION,
                             "no release can be prepared while Phase C (#111) is not complete")


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
            # One name since the rename (#136): no earlier names to clean up after.
            self.assertNotIn("KnownAppNames", text, script)

    def test_the_install_identity_names_are_pinned(self):
        # K1: the user renamed every one of these to Unminute (#136, 17 Oct 2026);
        # changing one again needs the user's say-so.
        main, store = read("workspace/__main__.py"), read("workspace/store.py")
        self.assertEqual("Unlimit.Unminute", one(r'^APP_ID = "([^"]+)"', main, "APP_ID"))
        self.assertEqual("Unminute/HuggingFaceToken",
                         one(r'^TARGET = "([^"]+)"', read("workspace/credentials.py"), "TARGET"))
        self.assertIn('return local_appdata() / "Unminute" / "workspace"', store)
        self.assertIn('return local_appdata() / "Unminute" / "models"', main)
        self.assertEqual(r"Programs\Unminute",
                         one(r'"(Programs\\[^"]+)"', self.install, "install folder"))
        self.assertEqual(r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Unminute",
                         one(r'^\$RegKey = "([^"]+)"', self.install, "uninstall key"))
        self.assertTrue((ROOT / "unminute.ico").is_file())
        self.assertIn('ICON = Path(__file__).resolve().parents[1] / "unminute.ico"',
                      read("workspace/shell.py"))
        self.assertEqual({r"app\unminute.ico"}, set(re.findall(r'"(app\\[^"]+\.ico)"',
                                                                 self.install)))
        # The data folder the installer and uninstaller keep clear of is the
        # store's and the model cache's parent.
        for script in (self.install, self.uninstall):
            self.assertIn('(Join-Path $env:LOCALAPPDATA "Unminute")', script)

    def test_no_earlier_app_name_is_left_in_shipped_code(self):
        # The app was "Whisper Transcriber", then "Meeting Workspace"; OpenAI's
        # Whisper speech model keeps its own name, so only the app's are listed.
        old = re.compile(r"WhisperWorkspace|WhisperTranscriber|MeetingWorkspace|Meeting Workspace"
                         r"|whisper\.ico|whisper-models|whisper[\\/]+workspace"
                         r"""|["']whisper["']\s*[/,]\s*["']workspace["']"""
                         r"|earlier app|Whisper Transcriber"
                         r"|whisper-(?:gpu-)?worker|WHISPER_TEST_MARKER", re.IGNORECASE)
        # Everything the package ships (APP_FILES, the app, the installer), and the
        # changelog fragments that become its CHANGELOG.md.
        files = [ROOT / "README.md",
                 ROOT / "CHANGELOG.md", ROOT / "LICENSE", ROOT / "tools" / "build_package.py",
                 *(p for p in (ROOT / "workspace").rglob("*")
                   if p.suffix in (".py", ".js", ".html", ".css")),
                 *(ROOT / "packaging").glob("*.ps1"), *(ROOT / "packaging").glob("*.cmd"),
                 *(ROOT / "changelog.d").glob("*.md"),
                 # and the tests that set the worker's test marker, the one test name
                 # the scan looks for (R13b round 2 review)
                 ROOT / "tests" / "fake_worker_engines.py", ROOT / "tests" / "test_workers.py"]
        found = [f"{p.relative_to(ROOT)}:{n}" for p in files
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if old.search(line)]
        self.assertEqual(found, [])

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
    # A Markdown link target, or an HTML src or href inside a Markdown file.
    LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)|(?:src|href)=\"([^\"]+)\"")

    def test_relative_links_resolve(self):
        # Every relative link in the docs reaches a file or folder (R13, #136,
        # moved docs to docs/history/). URLs and in-page anchors are skipped.
        broken = []
        for rel in self.DOCS:
            path = ROOT / rel
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for m in self.LINK.finditer(line):
                    target = m.group(1) or m.group(2)
                    if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                        continue
                    target = target.split("#", 1)[0].split("?", 1)[0]
                    if target and not (path.parent / target).exists():
                        broken.append(f"{rel}:{n}: {target}")
        self.assertEqual(broken, [])

    # Every job in these workflows is a required status check (docs/RELEASING.md,
    # "Protecting main"). ci.yml holds most; the changelog job re-runs when labels
    # change, the audit also runs daily and CodeQL runs weekly, so they keep their
    # own workflows. quality.yml holds report-only jobs; release.yml runs only
    # from a release tag.
    REQUIRED_WORKFLOWS = ("ci.yml", "changelog.yml", "audit.yml", "codeql.yml")
    REPORT_ONLY_WORKFLOWS = ("quality.yml",)
    OTHER_WORKFLOWS = ("release.yml",)

    @staticmethod
    def workflow_jobs(name):
        """The job ids and job names in .github/workflows/<name>."""
        workflow = read(f".github/workflows/{name}")
        jobs = workflow[workflow.index("\njobs:"):]
        ids = re.findall(r"^  ([\w-]+):\s*$", jobs, re.MULTILINE)
        names = re.findall(r"^    name: (.+)$", jobs, re.MULTILINE)
        return ids, [j.strip().strip('"') for j in names]

    @classmethod
    def setUpClass(cls):
        workflow = read(".github/workflows/ci.yml")
        cls.job_ids, cls.jobs = cls.workflow_jobs("ci.yml")
        cls.compile_line = one(r"^\s*run: python -m compileall (.+)$", workflow,
                               "compileall step in ci.yml")

    def test_ci_compile_step_names_real_paths_and_covers_all_python(self):
        # The Syntax check compiles named folders. A glob or a path that matches
        # nothing (the old root *.py after R13c, #136) only makes compileall warn
        # and still pass, so each target must exist, and together they must hold
        # every tracked Python file.
        targets = [a for a in self.compile_line.split() if not a.startswith("-")]
        self.assertTrue(targets, "the compileall step names no targets")
        missing = [t for t in targets if not (ROOT / t).exists()]
        self.assertEqual(missing, [], "ci.yml compiles paths that do not exist")
        files = tracked_files()
        if files is None:                              # outside a checkout: walk the tree
            skip = {".git", "build", "dist", "extracted", ".venv", "venv", "__pycache__"}
            files = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py")
                     if not skip & set(p.relative_to(ROOT).parts)]
        uncovered = [f for f in files if f.endswith(".py")
                     and not any(f == t or f.startswith(t.rstrip("/") + "/") for t in targets)]
        self.assertEqual(uncovered, [], "ci.yml's compile step misses these Python files")

    def test_ci_jobs_found_and_all_named(self):
        self.assertGreaterEqual(len(self.jobs), 5, "the job name pattern no longer matches ci.yml")
        # A job without a name: shows its id as the check name, which the
        # lists below would not know about.
        self.assertEqual(len(self.job_ids), len(self.jobs), "every ci.yml job needs a name:")

    def test_every_workflow_is_classified(self):
        # A new workflow joins one of the lists above, so its jobs are either
        # required or left out on purpose.
        present = {p.name for p in (ROOT / ".github" / "workflows").glob("*.y*ml")}
        known = set(self.REQUIRED_WORKFLOWS + self.REPORT_ONLY_WORKFLOWS + self.OTHER_WORKFLOWS)
        self.assertEqual(present, known, "classify each workflow in DocsTests")

    def test_other_workflow_jobs_found_and_all_named(self):
        for name in self.REQUIRED_WORKFLOWS + self.REPORT_ONLY_WORKFLOWS:
            with self.subTest(workflow=name):
                ids, names = self.workflow_jobs(name)
                self.assertTrue(ids, f"the job pattern no longer matches {name}")
                self.assertEqual(len(ids), len(names), f"every {name} job needs a name:")

    def test_required_checks_list_matches_required_jobs(self):
        line = one(r"^.*Require status checks.*$", read("docs/RELEASING.md"),
                   "'Require status checks' line in docs/RELEASING.md")
        quoted = re.findall(r"`([^`]+)`", line)
        sources = {q.removeprefix(".github/workflows/") for q in quoted
                   if q.startswith(".github/workflows/")}
        listed = {q for q in quoted if not q.startswith(".github/workflows/")}
        self.assertEqual(set(self.REQUIRED_WORKFLOWS), sources,
                         "docs/RELEASING.md names other workflows for the required checks")
        required = [job for name in self.REQUIRED_WORKFLOWS
                    for job in self.workflow_jobs(name)[1]]
        self.assertEqual(len(required), len(set(required)), "two required jobs share a name")
        self.assertEqual(set(required), listed,
                         "docs/RELEASING.md's required checks differ from the jobs in "
                         + ", ".join(self.REQUIRED_WORKFLOWS))

    def test_report_only_jobs_say_so(self):
        # A job outside the required workflows never blocks a merge, so its name
        # says so; a check that should block moves into ci.yml (docs/RELEASING.md).
        for name in self.REPORT_ONLY_WORKFLOWS:
            for job in self.workflow_jobs(name)[1]:
                self.assertTrue(job.endswith("(report only)"),
                                f"{name} job {job!r} is not report-only: move it into "
                                "ci.yml and the required list in docs/RELEASING.md")

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
# Hex also as %23 (a colour written inside an SVG data URI).
COLOUR = re.compile(r"(?:#|%23)[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color"
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
                            "fill: var(--x); mask: url(\"data:image/svg+xml,"
                            "%3Csvg stroke='%2362D098'%3E\"); }")
        self.assertEqual(["white", "#fff", "oklch(", "%2362D098"], found)

    def test_raw_colours_within_ratchet(self):
        colours = raw_colours(read("workspace/ui/app.css"))
        budget = BUDGETS["app_css_raw_colours"]["value"]
        self.assertLessEqual(len(colours), budget,
                             "a colour was added to app.css directly; add it to tokens.css and "
                             "use the variable")


# Top-level folders whose paths AGENTS.md may name; a backticked path starting
# with one of these must exist. Other backticked text (names, IDs, registry
# keys, commands) is left alone.
REPO_FOLDERS = {".claude", ".github", "changelog.d", "docs", "packaging", "requirements",
                "tests", "tools", "workspace"}
FILE_SUFFIXES = (".py", ".md", ".json", ".toml", ".txt", ".yml", ".ps1", ".cmd", ".ico")
# Named in AGENTS.md because the repo must not have it (see test_claude_reads_agents_md_natively).
ABSENT_ON_PURPOSE = {"CLAUDE.md"}


# The tests that guard each AGENTS.md invariant. AGENTS.md names the files; this
# names the tests, and test_invariant_guards_exist keeps the two in step. An ID
# with no entries is guarded by review only. Renaming or deleting one of these
# tests weakens its invariant (AGENTS.md, E2): say so in the PR.
INVARIANT_GUARDS = {
    "P1": {"tests/test_consistency.py": ["test_no_tracked_media"]},
    "P2": {"tests/test_agent_guard.py": ["FileToolTests", "ShellTests",
                                        "test_configured_command_blocks_through_powershell"]},
    "P3": {"tests/test_workers.py": ["test_error_text_has_no_paths"],
           "tests/test_app.py": ["test_refused_imports_explain_without_paths"],
           "tests/test_diagnostics.py": ["test_markers_never_reach_the_logs", "RecordTests"],
           "tests/test_audit.py": ["test_markers_never_reach_the_audit_log", "FieldRuleTests",
                                   "test_the_mirror_gets_each_record_in_order_without_ids"]},
    "N1": {"tests/test_offline_guard.py": [
        "test_switches_are_forced_off_even_if_the_user_enabled_them", "NetworkGuardTests"],
           "tests/test_workers.py": ["test_worker_cannot_reach_the_network"],
           "tests/test_import_hygiene.py": [],
           "tests/test_drafts.py": ["test_the_draft_worker_cannot_reach_the_network"]},
    "N2": {"tests/test_download_process.py": ["RequestCheckTests"]},
    "N3": {"tests/test_setup.py": ["test_child_gets_the_token_but_not_on_argv_or_env"],
           "tests/test_hardening.py": ["EnvironmentTokenTests"]},
    "I1": {"tests/test_model_integrity.py": ["test_speaker_model_loads_only_the_hash_checked_bytes",
                                             "test_a_code_running_checkpoint_is_refused",
                                             "test_the_allowlist_is_scoped"],
           "tests/test_asr_engines.py": ["test_docs_manifest_matches_code"]},
    "I2": {"tests/test_intake_isolation.py": ["test_lowered_decoder_cannot_write_the_users_files",
                                               "test_the_format_and_decoder_lists_are_unchanged"],
           "tests/test_safe_paths.py": ["FileValidationTests"]},
    "I3": {"tests/test_consistency.py": ["BridgeNameTests",
                                         "test_locked_methods_match_agents_md_and_the_docs"],
           "tests/test_hardening.py": ["test_refuses_the_internet_explorer_fallback",
                                       "test_the_page_opens_no_link_itself",
                                       "test_every_content_method_is_refused_while_locked"]},
    "D1": {"tests/test_workspace_core.py": [
        "test_crash_before_commit_resumes_without_loss_or_duplication",
        "test_generated_output_never_overwrites_edits"]},
    "D2": {"tests/test_workspace_core.py": ["test_explicit_pause_survives_restart"]},
    "D3": {"tests/test_hardening.py": ["test_only_one_copy_holds_the_workspace",
                                      "test_location_inside_onedrive_is_refused",
                                      "test_a_data_folder_junction_into_onedrive_is_refused",
                                      "test_a_missing_onedrive_target_spelt_another_way_"
                                      "is_refused"]},
    "D4": {"tests/test_workers.py": ["test_one_model_at_a_time_in_the_same_worker",
                                    "test_idle_worker_unloads"],
           "tests/test_speakers.py": ["test_failure_keeps_the_transcript_usable"],
           "tests/test_drafts.py": [
               "test_drafts_follow_the_recording_and_go_at_stop_before_processing",
               "test_processing_waits_for_the_draft_worker_after_the_recording_ends",
               "test_the_processing_loop_checks_the_draft_worker_again"],
           "tests/test_gpu.py": ["test_gpu_failure_falls_back_to_the_cpu_and_says_so",
                                 "test_whisper_gpu_failure_falls_back_to_the_cpu_and_says_so",
                                 "test_out_of_graphics_memory_falls_back_without_losing_"
                                 "committed_parts",
                                 "test_a_card_that_cannot_run_the_precision_falls_back_to_"
                                 "the_processor"]},
    "C1": {"tests/test_cleanup.py": ["test_copy_or_export_alone_never_deletes",
                                    "test_intent_survives_a_crash_and_is_retried",
                                    "test_late_worker_result_cannot_recreate_content"]},
    "C2": {"tests/test_holds.py": ["test_release_never_deletes_silently"]},
    "C3": {"tests/test_cleanup.py": [
        "test_original_deleted_only_with_authority_root_and_same_identity"]},
    "C4": {"tests/test_update.py": ["test_workspace_with_content_is_never_migrated"]},
    "R1": {"tests/test_app.py": ["NativeConfirmationTests"],
           "tests/test_recorder.py": ["test_a_recording_needs_the_confirmation_and_keeps_it",
                                      "test_the_indicator_follows_every_way_a_recording_ends"],
           "tests/test_policy.py": ["test_a_bad_value_keeps_the_recording_confirmation"],
           "tests/test_two_track.py": ["test_no_computer_track_opens_until_the_app_allows_it",
                                       "test_the_switch_cannot_be_saved_before_the_app_offers_it"]},
    "K1": {"tests/test_consistency.py": ["InstallIdentityTests",
                                        "test_the_install_identity_names_are_pinned",
                                        "test_no_earlier_app_name_is_left_in_shipped_code"]},
    "K2": {},
    "K3": {"tests/test_policy.py": ["WorkspacePolicyTests",
                                   "test_a_bad_value_never_grants_a_permission"],
           "tests/test_two_track.py": [
               "test_turned_off_unreadable_or_invalid_policy_refuses_it",
               "test_policy_turned_off_mid_recording_stops_only_the_computer_track",
               "test_an_invalid_policy_value_mid_recording_stops_only_the_computer_track",
               "test_resume_checks_the_policy_again"]},
    "E1": {},
    "E2": {},
}


def tracked_files():
    """Tracked paths, or None outside a git checkout."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [f for f in out.split("\0") if f]


class AgentInstructionTests(unittest.TestCase):
    """AGENTS.md stays true: the paths and guard tests it names exist (standard section 1)."""

    @classmethod
    def setUpClass(cls):
        cls.text = read("AGENTS.md")
        start = cls.text.index("\n## Invariants")
        end = cls.text.index("\n## ", start + 1)
        cls.invariants = cls.text[start:end]

    def test_claude_reads_agents_md_natively(self):
        # Claude Code reads AGENTS.md only when there is no CLAUDE.md; a new one
        # would silently stop Claude sessions loading the shared rules.
        for name in ("CLAUDE.md", ".claude/CLAUDE.md"):
            self.assertFalse((ROOT / name).exists(),
                             f"{name} stops Claude Code reading AGENTS.md; put the rule there")

    def test_paths_named_in_agents_md_exist(self):
        missing = []
        for token in set(re.findall(r"`([^`\s]+)`", self.text)):
            first = token.split("/")[0]
            if ("/" in token and first in REPO_FOLDERS) or (
                    "/" not in token and token.endswith(FILE_SUFFIXES)):
                if token not in ABSENT_ON_PURPOSE and not (ROOT / token).exists():
                    missing.append(token)
        self.assertEqual([], sorted(missing), "AGENTS.md names paths that no longer exist")

    def test_invariant_guards_exist(self):
        bullets = re.findall(r"^- \*\*([A-Z]+\d+)\*\*(.*?)(?=^- \*\*|^\S|\Z)", self.invariants,
                             re.MULTILINE | re.DOTALL)
        self.assertGreaterEqual(len(bullets), 10, "the invariant pattern no longer matches")
        ids = [ident for ident, _ in bullets]
        self.assertEqual(len(ids), len(set(ids)), "invariant IDs must be unique")
        self.assertEqual(set(ids), set(INVARIANT_GUARDS),
                         "AGENTS.md invariants and INVARIANT_GUARDS list different IDs")
        for ident, body in bullets:
            with self.subTest(invariant=ident):
                self.assertIn("Guard:", body, f"{ident} names no guard (write 'Guard: review')")
                named = set(re.findall(r"`(tests/[\w/]+\.py)`", body))
                self.assertEqual(named, set(INVARIANT_GUARDS[ident]),
                                 f"{ident}: AGENTS.md and INVARIANT_GUARDS name different files")
                for path, names in INVARIANT_GUARDS[ident].items():
                    source = read(path)
                    for name in names:
                        self.assertRegex(source, rf"(?m)^\s*(def|class) {name}\b",
                                         f"{ident} relies on {name}, not found in {path}")

    def test_no_tracked_media(self):
        files = tracked_files()
        if files is None:
            self.skipTest("not a git checkout")
        tree = ast.parse(read("workspace/app.py"))
        extensions = next(ast.literal_eval(node.value) for node in ast.walk(tree)
                          if isinstance(node, ast.Assign)
                          and any(getattr(t, "id", None) == "AUDIO_EXTENSIONS"
                                  for t in node.targets))
        media = [f for f in files if f.lower().endswith((*extensions, ".f32", ".uma"))]
        self.assertEqual([], media, "recordings stay out of the repo (AGENTS.md, P1); "
                                    "a synthetic fixture needs an agreed exception here")


def section(text, heading):
    """The body of a '## heading' section, up to the next '## ' heading."""
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def table_rows(text, id_pattern):
    """{first cell: cells} for each table row in text whose first cell matches id_pattern."""
    rows = {}
    for line in text.splitlines():
        match = re.match(rf"^\| ({id_pattern}) \|", line)
        if match:
            if match.group(1) in rows:
                raise AssertionError(f"two rows for {match.group(1)}")
            rows[match.group(1)] = [c.strip() for c in line.strip().strip("|").split(" | ")]
    return rows


def heading_anchors(text):
    """GitHub's anchors for the headings in a Markdown file."""
    anchors = set()
    for heading in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        anchors.add(slug)
    return anchors


# A #111 slice, such as B6, C3, C3a or C2b-2. A lettered slice has no word boundary
# between its digit and its letter, so the letter and part number are matched too.
SLICE = re.compile(r"\b[A-D]\d{1,2}[a-z]?(?:-\d+)?\b")
TEST_FILE = re.compile(r"`(tests/[\w/]+\.py)`")


class ControlMatrixTests(unittest.TestCase):
    """docs/CONTROL_MATRIX.md has one row for each AGENTS.md invariant and each
    SECURITY_MODEL.md threat, names their guards, and carries their planned slices
    (#111 D1), so it cannot drift from either."""

    @classmethod
    def setUpClass(cls):
        cls.text = read("docs/CONTROL_MATRIX.md")
        cls.invariants = table_rows(section(cls.text, "Invariants"), r"[A-Z]\d+")
        cls.threats = table_rows(section(cls.text, "Threats"), r"T\d+")
        model = section(read("docs/SECURITY_MODEL.md"), "Threats and controls")
        cls.model = table_rows(model, r"T\d+")

    def test_one_row_per_invariant(self):
        self.assertEqual(set(self.invariants), set(INVARIANT_GUARDS),
                         "CONTROL_MATRIX.md's invariants differ from AGENTS.md's")

    def test_invariant_guards_match_agents_md(self):
        for ident, cells in self.invariants.items():
            with self.subTest(invariant=ident):
                guard = cells[3]
                self.assertEqual(set(TEST_FILE.findall(guard)), set(INVARIANT_GUARDS[ident]),
                                 f"{ident}: CONTROL_MATRIX.md and INVARIANT_GUARDS name "
                                 "different guard files")
                if not INVARIANT_GUARDS[ident]:
                    self.assertIn("Review", guard, f"{ident} has no guard test: say Review")

    def test_one_row_per_threat(self):
        self.assertGreaterEqual(len(self.model), 30, "the threat row pattern no longer matches")
        self.assertEqual(set(self.threats), set(self.model),
                         "CONTROL_MATRIX.md's threats differ from SECURITY_MODEL.md's")

    def test_every_threat_names_a_guard_or_review(self):
        for ident, cells in self.threats.items():
            with self.subTest(threat=ident):
                self.assertEqual(len(cells), 6, f"{ident}: the row needs six cells")
                guard = cells[3]
                self.assertTrue(TEST_FILE.search(guard) or "Review" in guard,
                                f"{ident} names no guard test and does not say Review")

    def test_planned_slices_are_carried(self):
        # Both ways: a slice SECURITY_MODEL.md plans is in the matrix, and a slice the
        # matrix still shows as planned is still planned there (not delivered or dropped).
        for ident, cells in self.model.items():
            with self.subTest(threat=ident):
                planned = set(SLICE.findall(cells[4]))
                carried = set(SLICE.findall(self.threats.get(ident, [""] * 6)[4]))
                self.assertEqual(set(), planned - carried,
                                 f"{ident}: SECURITY_MODEL.md plans slices the matrix omits")
                self.assertEqual(set(), carried - planned,
                                 f"{ident}: the matrix plans slices SECURITY_MODEL.md does not")

    def test_named_paths_exist(self):
        missing = [token for token in set(re.findall(r"`([^`\s]+)`", self.text))
                   if token.split("/")[0] in REPO_FOLDERS and "/" in token
                   and not (ROOT / token).exists()]
        self.assertEqual([], sorted(missing), "CONTROL_MATRIX.md names paths that do not exist")


class DeploymentGuideTests(unittest.TestCase):
    """docs/IT_DEPLOYMENT.md: its policy list is the ADMX template's, and its incident
    runbook reaches the withdrawal runbook and --health (#111 D1, folded from #121)."""

    NEW_DOCS = ("docs/IT_DEPLOYMENT.md", "docs/CONTROL_MATRIX.md",
                "docs/PRIVACY_IMPACT_ASSESSMENT_DRAFT.md", "docs/WORKSPACE_GUIDE.md")

    @classmethod
    def setUpClass(cls):
        cls.text = read("docs/IT_DEPLOYMENT.md")

    def test_policy_list_matches_the_admx_template(self):
        import xml.etree.ElementTree as ET
        ns = "{http://schemas.microsoft.com/GroupPolicy/2006/07/PolicyDefinitions}"
        admx = ET.fromstring(read("packaging/policy/Unminute.admx").encode("utf-8"))
        adml = ET.fromstring(read("packaging/policy/en-US/Unminute.adml").encode("utf-8"))
        strings = {s.get("id"): s.text for s in adml.iter(ns + "string")}
        template = {}
        for policy in admx.iter(ns + "policy"):
            label = one(r"^\$\(string\.(\w+)\)$", policy.get("displayName"), "display name")
            template[policy.get("name")] = strings[label]
        self.assertGreaterEqual(len(template), 10, "the ADMX pattern no longer matches")
        rows = table_rows(section(self.text, "Managed policy"), r"`\w+`")
        listed = {name.strip("`"): cells[1] for name, cells in rows.items()}
        self.assertEqual(listed, template,
                         "IT_DEPLOYMENT.md's policy list differs from packaging/policy")

    def test_not_set_column_matches_packaging(self):
        # Most Allow values grant when not set: IT must not read an unset value as off.
        packaging = read("docs/PACKAGING.md").split("### Managed settings (policy)", 1)[1]
        packaging = packaging.split("\n#", 1)[0]
        expected = {name.strip("`"): cells[2]
                    for name, cells in table_rows(packaging, r"`\w+`").items()}
        rows = table_rows(section(self.text, "Managed policy"), r"`\w+`")
        listed = {name.strip("`"): cells[3] for name, cells in rows.items()}
        self.assertGreaterEqual(len(expected), 10, "PACKAGING.md's policy table no longer matches")
        self.assertEqual(listed, expected,
                         "IT_DEPLOYMENT.md's Not set column differs from PACKAGING.md's")

    def test_incident_runbook_links_withdrawal_and_health(self):
        incidents = section(self.text, "Incidents")
        self.assertIn("(RELEASING.md#withdrawing-a-bad-release)", incidents)
        self.assertIn("(#health-monitoring-with---health)", incidents)
        self.assertIn("(PACKAGING.md#managed-deployment-intune)", incidents)

    def test_anchor_links_resolve(self):
        # The repo-wide link test skips anchors; these docs lean on them.
        broken = []
        for doc in self.NEW_DOCS:
            text = read(doc)
            for target, anchor in re.findall(r"\]\(([^)#\s]*)#([^)\s]+)\)", text):
                path = (ROOT / doc).parent / target if target else ROOT / doc
                if path.suffix == ".md" and anchor not in heading_anchors(
                        path.read_text(encoding="utf-8")):
                    broken.append(f"{doc}: {target}#{anchor}")
        self.assertEqual([], broken)


# Words that take a legal position or name an adviser step.
LEGAL_WORDS = re.compile(r"\b(?:un)?lawful(?:ly|ness)?\b|\b(?:il)?legal(?:ly|ity)?\b"
                         r"|\bPrivacy Act\b|\bCrimes Act\b|\bIPPs?\b|\badvis[eo]rs?\b",
                         re.IGNORECASE)


class PrivacyDraftTests(unittest.TestCase):
    """The privacy impact assessment draft states facts only: the deploying
    organisation decides the legal position (the owner's rule), so the draft says
    it is a draft and takes no position. The other docs stay silent on it too."""

    def test_marked_as_a_draft_with_no_legal_position(self):
        text = read("docs/PRIVACY_IMPACT_ASSESSMENT_DRAFT.md")
        self.assertIn("**Status: draft.**", text.split("\n## ", 1)[0])
        found = LEGAL_WORDS.findall(text)
        self.assertEqual([], found, "the draft takes no legal position and names no "
                                    "adviser step; leave that to the organisation")

    def test_no_doc_takes_a_legal_position_or_names_an_adviser_step(self):
        # AGENTS.md states the rule itself, so it is the one doc allowed the words.
        found = {doc: LEGAL_WORDS.findall(read(doc)) for doc in DocsTests.DOCS
                 if doc != "AGENTS.md"}
        self.assertEqual({}, {doc: words for doc, words in found.items() if words},
                         "docs describe what the app does and stay silent on the legal "
                         "position, with no adviser step (the owner's rule)")


class AccessibilityTargetTests(unittest.TestCase):
    """The accessibility target is WCAG 2.2 AA (owner decision, 21 Oct 2026, #121),
    and no doc names another WCAG version."""

    def test_the_standard_names_wcag_2_2_aa(self):
        criterion = one(r"^\*\*Criterion\.\*\* (WCAG [^\n]*)$",
                        section(read("docs/RELEASE_STANDARD.md"), "Part A. The standard")
                        .split("### 12. Accessibility", 1)[1].split("\n### ", 1)[0],
                        "accessibility criterion")
        self.assertTrue(criterion.startswith("WCAG 2.2 AA"), criterion)

    def test_the_tool_extension_gates_the_next_release(self):
        # Owner decision, 22 Oct 2026: #298 (the automatable WCAG 2.2 AA criteria in
        # tools/accessibility_check.py) is a gate for the next release. The wording is
        # the post-#298 text (the checks are in the tool; the gate still stands).
        standard = read("docs/RELEASE_STANDARD.md")
        wcag = standard.split("### Accessibility against WCAG 2.2 AA", 1)[1].split("\n### ", 1)[0]
        self.assertIn("These checks (#298) are a gate for the next release "
                      "(owner decision, 22 October 2026).", wcag)
        before = section(read("docs/RELEASING.md"), "Before cutting a release")
        self.assertRegex(before, r"(?m)^\d+\. \*\*The accessibility check covers WCAG 2\.2 AA "
                                 r"where it can \(#298\)\.\*\* .*gate for the next release")

    def test_no_doc_names_another_wcag_version(self):
        for doc in DocsTests.DOCS:
            with self.subTest(doc=doc):
                versions = set(re.findall(r"WCAG (\d\.\d)", read(doc)))
                self.assertLessEqual(versions, {"2.2"}, f"{doc} names another WCAG version")


if __name__ == "__main__":
    unittest.main()
