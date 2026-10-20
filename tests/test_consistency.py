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
        files = [ROOT / "asr_engines.py", ROOT / "offline_guard.py", ROOT / "README.md",
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
           "tests/test_diagnostics.py": ["test_markers_never_reach_the_logs", "RecordTests"]},
    "N1": {"tests/test_offline_guard.py": [
        "test_switches_are_forced_off_even_if_the_user_enabled_them", "NetworkGuardTests"],
           "tests/test_workers.py": ["test_worker_cannot_reach_the_network"],
           "tests/test_import_hygiene.py": []},
    "N2": {"tests/test_download_process.py": ["RequestCheckTests"]},
    "N3": {"tests/test_setup.py": ["test_child_gets_the_token_but_not_on_argv_or_env"],
           "tests/test_hardening.py": ["EnvironmentTokenTests"]},
    "I1": {"tests/test_model_integrity.py": ["test_speaker_model_loads_only_the_hash_checked_bytes",
                                             "test_a_code_running_checkpoint_is_refused",
                                             "test_the_allowlist_is_scoped"],
           "tests/test_asr_engines.py": ["test_docs_manifest_matches_code"]},
    "I2": {"tests/test_intake_isolation.py": ["test_lowered_decoder_cannot_write_the_users_files"],
           "tests/test_safe_paths.py": ["FileValidationTests"]},
    "I3": {"tests/test_consistency.py": ["BridgeNameTests"],
           "tests/test_hardening.py": ["test_refuses_the_internet_explorer_fallback"]},
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
           "tests/test_policy.py": ["test_a_bad_value_keeps_the_recording_confirmation"]},
    "K1": {"tests/test_consistency.py": ["InstallIdentityTests",
                                        "test_the_install_identity_names_are_pinned",
                                        "test_no_earlier_app_name_is_left_in_shipped_code"]},
    "K2": {},
    "K3": {"tests/test_policy.py": ["WorkspacePolicyTests",
                                   "test_a_bad_value_never_grants_a_permission"]},
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
        media = [f for f in files if f.lower().endswith((*extensions, ".f32"))]
        self.assertEqual([], media, "recordings stay out of the repo (AGENTS.md, P1); "
                                    "a synthetic fixture needs an agreed exception here")


if __name__ == "__main__":
    unittest.main()
