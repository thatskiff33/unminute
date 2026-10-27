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

    def test_the_design_fixtures_lock_as_the_app_does(self):
        # #111 C4b: the fake bridge's locked scenario (the design check and
        # tools/ui_idle_check.py) answers what the app's gate does.
        from workspace import api, app
        fixtures = read("tools/design_check/fixtures.js")
        listed = one(r"const LOCKED_METHODS = new Set\(\[([^\]]*)\]\)", fixtures,
                     "LOCKED_METHODS in tools/design_check/fixtures.js")
        self.assertEqual(set(api.LOCKED_METHODS), set(re.findall(r'"(\w+)"', listed)))
        text = one(r'const LOCKED_TEXT = "([^"]*)";', fixtures,
                   "LOCKED_TEXT in tools/design_check/fixtures.js")
        self.assertEqual(app.LOCKED_TEXT, text)


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
        # imports and its build's CUDA version) and re-validating on a GPU. The
        # package also leaves out the wheel's cudnn64_9.dll (#176 D2), which holds
        # only while CTranslate2's Windows build has no cuDNN.
        from workspace import gpu
        pinned = one(r"^ctranslate2==(\S+)", read("requirements.txt"), "ctranslate2 pin")
        self.assertEqual(pinned, gpu.WHISPER_CTRANSLATE2,
                         "requirements.txt moved CTranslate2: check the cuBLAS it loads and "
                         "that it still needs no cuDNN (the package leaves cudnn64_9.dll "
                         "out, #176 D2), and update workspace/gpu.py's Whisper component")

    def test_dll_path_variables_were_checked_against_the_ctranslate2_pin(self):
        # #357 (T25): every worker removes the environment variables CTranslate2's
        # __init__ reads to add a DLL folder (4.8.3: HIP_PATH only). The list is
        # right only for the version it was checked against, so moving the pin
        # fails here until someone reads the new __init__ again. Read from the
        # source, as workspace/worker.py imports numpy.
        checked = one(r'^DLL_PATH_CHECKED_CTRANSLATE2 = "([^"]+)"$',
                      read("workspace/worker.py"), "DLL_PATH_CHECKED_CTRANSLATE2")
        pinned = one(r"^ctranslate2==(\S+)", read("requirements.txt"), "ctranslate2 pin")
        self.assertEqual(pinned, checked,
                         "requirements.txt moved CTranslate2: read its __init__ for the "
                         "environment variables it uses to add DLL folders, update "
                         "worker.DLL_PATH_VARIABLES (and T25 in docs/SECURITY_MODEL.md), "
                         "then set worker.DLL_PATH_CHECKED_CTRANSLATE2 to the new version")

    # #427: the PowerShell modules CI uses come from the PowerShell Gallery as .nupkg
    # files pinned by version and SHA-256, and are kept in the Actions cache only under
    # a key of both. (workflow, job id, cache key prefix, version and hash variables)
    GALLERY_PACKAGES = (
        ("ci.yml", "lint-powershell", "psscriptanalyzer", "PSSA_VERSION", "PSSA_SHA256"),
        ("quality.yml", "pester", "pester", "PESTER_VERSION", "PESTER_SHA256"),
    )

    def test_gallery_packages_are_pinned_and_cached_by_version_and_hash(self):
        for workflow, job_id, prefix, version, sha in self.GALLERY_PACKAGES:
            with self.subTest(workflow=workflow, job=job_id):
                job = DocsTests.job_blocks(workflow)[job_id]
                one(rf'^      {version}: "(\d+\.\d+\.\d+)"$', job, f"{job_id}'s {version}")
                one(rf'^      {sha}: "([0-9a-f]{{64}})"$', job, f"{job_id}'s {sha}")
                key = f"key: {prefix}-${{{{ env.{version} }}}}-${{{{ env.{sha} }}}}"
                self.assertEqual(job.count(key), 2, f"{job_id} restores and saves by {key}")
                self.assertEqual(job.count("key:"), 2, f"{job_id} caches something else too")
                # A partial key would restore some other version's package.
                self.assertNotIn("restore-keys", job)
                self.assertNotIn("Install-Module", job)

    def test_pester_cache_key_matches_the_script_pin(self):
        # The pester job's cache key must name the package tools/run_pester.ps1 pins, or
        # the cache holds a package the script throws away and downloads again.
        script = read("tools/run_pester.ps1")
        job = DocsTests.job_blocks("quality.yml")["pester"]
        for variable, name in (("PESTER_VERSION", "PesterVersion"),
                               ("PESTER_SHA256", "PesterSha256")):
            self.assertEqual(one(rf'^      {variable}: "([^"]+)"$', job, variable),
                             one(rf'^\${name} = "([^"]+)"$', script, f"${name}"),
                             f"quality.yml's {variable} differs from tools/run_pester.ps1")


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
        # #111 C2b-2: Phase C ships as one release. Until C7 set
        # PHASE_C_COMPLETE, VERSION stayed what it was when C2b-2 merged, so a
        # release-prep written by hand (not by tools/changelog.py, which
        # refuses) failed here. Kept for a guard that is ever set back.
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

    def test_the_identity_file_pins_the_install_names(self):
        # K1 (#111 B6a): packaging/identity.json mirrors the names, never replaces
        # them, so the names stay literals here and changing the file alone fails.
        # The machine names (B6b) joined K1 with the owner's approval (21 October
        # 2026, confirmed 22 October 2026, #111); install.ps1 -Machine and
        # uninstall.ps1 -Machine use them.
        identity = json.loads(read("packaging/identity.json"))
        self.assertEqual(identity["name"], "Unminute")
        self.assertEqual(identity["earlier_names"], ["Whisper Transcriber", "Meeting Workspace"])
        self.assertEqual(identity["app_id"], "Unlimit.Unminute")
        self.assertEqual(identity["credential"], "Unminute/HuggingFaceToken")
        self.assertEqual(identity["icon"], "unminute.ico")
        self.assertEqual(identity["mutex"], r"Local\Unminute.Workspace")
        self.assertEqual(identity["event_source"], "Unminute")
        self.assertEqual(identity["data_folders"], {
            "root": r"%LOCALAPPDATA%\Unminute",
            "workspace": r"%LOCALAPPDATA%\Unminute\workspace",
            "models": r"%LOCALAPPDATA%\Unminute\models"})
        self.assertEqual(identity["workspace_lock"], "instance.lock")
        self.assertEqual(identity["install_lock"], "install.lock")
        self.assertEqual(identity["install_log"], [r"logs\install.log", r"logs\install.log.1"])
        self.assertEqual(identity["install_root_entries"],
                         ["versions", "current.json", "current.json.tmp", "uninstall.ps1",
                          "logs", "install.lock"])
        self.assertEqual(identity["modes"], {
            "user": {
                "install_folder": r"%LOCALAPPDATA%\Programs\Unminute",
                "uninstall_key": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall"
                                 r"\Unminute",
                "shortcut": "Unminute.lnk"},
            "machine": {
                "install_folder": r"%ProgramFiles%\Unminute",
                "uninstall_key": r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
                                 r"\Unminute",
                "launch_junction": "current",
                "launch_junction_working": ["current.new", "current.old"],
                "staging_folder": "staging-<32 hex digits>",
                "shortcut": "Unminute.lnk",
                "event_source": "Unminute",
                "event_source_key": r"HKLM\SYSTEM\CurrentControlSet\Services\EventLog"
                                    r"\Application\Unminute",
                "event_message_file": "EventLogMessages.dll"}})
        self.assertEqual({script: {mode: sorted(codes) for mode, codes in modes.items()}
                          for script, modes in identity["exit_codes"].items()},
                         {"install.ps1": {"user": ["0", "1", "2", "3", "4", "5", "6"],
                                          "machine": ["0", "1", "2", "3", "4"]},
                          "uninstall.ps1": {"user": ["0", "1", "2", "3", "5", "6"],
                                            "machine": ["0", "1", "2", "3", "4", "5"]}})
        # Repository only: the build copies named files, so it never ships.
        self.assertNotIn("identity.json", read("tools/build_package.py"))

    def test_the_app_and_the_scripts_match_the_identity_file(self):
        identity = json.loads(read("packaging/identity.json"))
        main = read("workspace/__main__.py")
        self.assertEqual(identity["app_id"], one(r'^APP_ID = "([^"]+)"', main, "APP_ID"))
        self.assertEqual(identity["mutex"].replace("\\", "\\\\"),
                         one(r'^MUTEX_NAME = "([^"]+)"', main, "MUTEX_NAME"))
        self.assertEqual(identity["credential"],
                         one(r'^TARGET = "([^"]+)"', read("workspace/credentials.py"), "TARGET"))
        self.assertEqual(identity["event_source"],
                         one(r'^SOURCE = "([^"]+)"', read("workspace/audit.py"), "SOURCE"))
        self.assertEqual(identity["workspace_lock"],
                         one(r'^NAME = "([^"]+)"', read("workspace/instance.py"), "NAME"))
        user = identity["modes"]["user"]
        for script, text in (("install.ps1", self.install), ("uninstall.ps1", self.uninstall)):
            with self.subTest(script=script):
                self.assertEqual(identity["name"],
                                 one(r'^\$AppName = "([^"]+)"', text, "$AppName"))
                self.assertEqual(user["uninstall_key"].replace("HKCU", "HKCU:", 1),
                                 one(r'^\$RegKey = "([^"]+)"', text, "$RegKey"))
                self.assertEqual(user["install_folder"],
                                 "%LOCALAPPDATA%\\" + one(r'"(Programs\\[^"]+)"', text,
                                                          "install folder"))
                entries = one(r'^\$OwnEntries = @\(([^)]+)\)$', text, "$OwnEntries")
                self.assertEqual(identity["install_root_entries"],
                                 re.findall(r'"([^"]+)"', entries))
                self.assertEqual(identity["install_lock"],
                                 one(r'^\$LockName = "([^"]+)"', text, "$LockName"))
                logs = re.findall(r'"([^"]+)"', one(r'^\$LogNames = @\(([^)]+)\)$', text,
                                                    "$LogNames"))
                self.assertEqual(identity["install_log"], [f"logs\\{n}" for n in logs])
                # Every exit code the script can give, in either mode, and no
                # other, is listed.
                used = set(re.findall(r"\bStop-(?:Install|Uninstall) (\d)\b", text))
                used |= set(re.findall(r"(?m)^\s*exit (\d)\b", text))
                codes = identity["exit_codes"][script]
                self.assertEqual(used, set(codes["user"]) | set(codes["machine"]))
                # The machine names (#111 B6b), each a literal in both scripts.
                machine = identity["modes"]["machine"]
                self.assertEqual(machine["uninstall_key"].replace("HKLM", "HKLM:", 1),
                                 one(r'^\$MachineRegKey = "([^"]+)"$', text, "$MachineRegKey"))
                self.assertEqual(machine["event_source_key"].replace("HKLM", "HKLM:", 1),
                                 one(r'^\$EventSourceKey = "([^"]+)"$', text, "$EventSourceKey"))
                self.assertEqual(machine["install_folder"], "%ProgramFiles%\\" + one(
                    r'^\$MachineFolderName = "([^"]+)"$', text, "$MachineFolderName"))
                self.assertEqual(machine["launch_junction"],
                                 one(r'^\$JunctionName = "([^"]+)"$', text, "$JunctionName"))
                self.assertEqual([machine["launch_junction"], *machine["launch_junction_working"]],
                                 re.findall(r'"([^"]+)"', one(r'^\$JunctionNames = @\(([^)]+)\)$',
                                                              text, "$JunctionNames")))
                self.assertEqual(r"^staging-[0-9a-f]{32}\z",
                                 one(r"^\$StagingPattern = '([^']+)'$", text, "$StagingPattern"))
                self.assertIn('"$AppName.lnk"', text)
                self.assertIn('"EventLogMessages.dll"', text)
                # Program Files and the all-users Start menu from Windows' known
                # folders, each set once (the tests redirect them).
                self.assertEqual(one(r'^\$ProgramFilesFolder = (.+)$', text, "$ProgramFilesFolder"),
                                 '[Environment]::GetFolderPath("ProgramFiles")')
                self.assertEqual(one(r'^\$CommonProgramsFolder = (.+)$', text,
                                     "$CommonProgramsFolder"),
                                 '[Environment]::GetFolderPath("CommonPrograms")')
        # The per-user refusal reads that same machine entry, in the 64-bit view.
        self.assertIn("$hive, $subKey = $MachineRegKey -split ':\\\\', 2", self.install)
        self.assertIn("[Microsoft.Win32.RegistryView]::Registry64", self.install)
        from workspace import update
        self.assertEqual(identity["modes"]["machine"]["install_folder"],
                         "%ProgramFiles%\\" + update.MACHINE_FOLDER)
        self.assertEqual(identity["modes"]["machine"]["event_source"], identity["event_source"])
        self.assertEqual(identity["app_id"], one(r'^\$AppId = "([^"]+)"', self.install, "$AppId"))
        self.assertEqual({identity["icon"]}, {Path(p).name for p in re.findall(
            r'"app\\([^"]+\.ico)"', self.install)})

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
    # from a release tag. One exception: a job run in parts by a matrix (the
    # Windows unit tests, #386) is not required itself; the job that needs it
    # and always runs is, and test_matrix_parts_stand_behind_a_required_job
    # holds that.
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

    @staticmethod
    def job_blocks(name):
        """{job id: its text} in .github/workflows/<name>."""
        workflow = read(f".github/workflows/{name}")
        parts = re.split(r"^  ([\w-]+):\s*$", workflow[workflow.index("\njobs:"):],
                         flags=re.MULTILINE)
        return dict(zip(parts[1::2], parts[2::2], strict=True))

    @staticmethod
    def is_matrix_part(job_name):
        """A job name that differs per matrix entry names one part of a job."""
        return "${{ matrix." in job_name

    @classmethod
    def required_jobs(cls, name):
        """The names of the required status checks in .github/workflows/<name>."""
        return [job for job in cls.workflow_jobs(name)[1] if not cls.is_matrix_part(job)]

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
                    for job in self.required_jobs(name)]
        self.assertEqual(len(required), len(set(required)), "two required jobs share a name")
        self.assertEqual(set(required), listed,
                         "docs/RELEASING.md's required checks differ from the jobs in "
                         + ", ".join(self.REQUIRED_WORKFLOWS))

    def test_matrix_parts_stand_behind_a_required_job(self):
        # A matrix job's parts are left out of the required checks only because
        # one required job needs them and always runs: a skipped required job
        # would count as passing, so it must run, and fail, when a part fails.
        found = 0
        for name in self.REQUIRED_WORKFLOWS:
            blocks = self.job_blocks(name)
            for job_id, body in blocks.items():
                job_name = one(r"^    name: (.+)$", body, f"{name} job {job_id}'s name")
                if not self.is_matrix_part(job_name):
                    continue
                found += 1
                with self.subTest(workflow=name, job=job_id):
                    gates = [b for b in blocks.values()
                             if re.search(rf"^    needs: (\[[^\]]*\b)?{re.escape(job_id)}\b", b,
                                          re.MULTILINE)
                             and not self.is_matrix_part(one(r"^    name: (.+)$", b, "a name"))]
                    self.assertEqual(len(gates), 1, f"no one required job needs {job_id}")
                    self.assertIn("    if: ${{ always() }}\n", gates[0])
        self.assertGreaterEqual(found, 1, "the Windows unit tests no longer run in parts")

    # One group per pull request, cancelled by a newer push; every other run in a
    # group of its own (its run id), never cancelled.
    CONCURRENCY = (
        "concurrency:\n"
        "  group: ${{ github.workflow }}-${{ github.event_name == 'pull_request' && "
        "format('pr-{0}', github.event.pull_request.number) || "
        "format('run-{0}', github.run_id) }}\n"
        "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n")

    def test_superseded_pull_request_runs_are_cancelled_and_no_others(self):
        # #395, CI time: a new push to a pull request cancels that pull request's
        # older run in every workflow a pull request starts, so superseded runs
        # stop holding runners. A push to main, a schedule, a manual run or a
        # release is never cancelled, and no job sets a concurrency of its own.
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
        self.assertTrue(workflows)
        for path in workflows:
            text = path.read_text(encoding="utf-8")
            head, jobs = text.split("\njobs:", 1)
            with self.subTest(workflow=path.name):
                if re.search(r"^  pull_request:", head, re.MULTILINE):
                    self.assertIn("\n" + self.CONCURRENCY, head)
                else:
                    self.assertNotRegex(text, r"cancel-in-progress: (?!false\s)")
                self.assertNotRegex(jobs, r"(?m)^\s+concurrency:")

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
        names = [j.lower() for j in self.required_jobs("ci.yml")]
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
           "tests/test_hardening.py": ["EnvironmentTokenTests"],
           "tests/test_download_queue.py": [
               "test_a_queued_speaker_model_gets_its_token_by_pipe",
               "test_the_queue_is_kept_with_model_identifiers_only"]},
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
                                       "test_every_content_method_is_refused_while_locked"],
           "tests/test_drafts.py": [
               "test_the_bridge_method_checks_its_argument_and_returns_text_only",
               "test_the_page_shows_draft_text_as_text_only_and_does_not_announce_it"]},
    "D1": {"tests/test_workspace_core.py": [
        "test_crash_before_commit_resumes_without_loss_or_duplication",
        "test_generated_output_never_overwrites_edits"]},
    "D2": {"tests/test_workspace_core.py": ["test_explicit_pause_survives_restart"]},
    "D3": {"tests/test_hardening.py": ["test_only_one_copy_holds_the_workspace",
                                      "test_location_inside_onedrive_is_refused",
                                      "test_a_data_folder_junction_into_onedrive_is_refused",
                                      "test_a_missing_onedrive_target_spelt_another_way_"
                                      "is_refused"],
           # The workspace lock across Windows sessions (#111 B5a, B5-10).
           "tests/test_launch.py": ["test_a_lock_held_by_another_process_is_refused_and_"
                                    "released_with_it",
                                    "test_a_second_session_is_refused_before_the_store_opens",
                                    "test_a_migration_never_runs_while_another_session_"
                                    "holds_the_lock",
                                    "test_the_app_start_refuses_a_workspace_open_in_another_"
                                    "session"]},
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
                                       "test_the_switch_cannot_be_saved_before_the_app_offers_it",
                                       "test_no_computer_track_opens_without_a_confirmation_that_names_it",
                                       "test_a_confirmation_naming_the_computer_never_starts_the_microphone_alone",
                                       "test_a_switch_changed_while_the_question_is_open_starts_nothing",
                                       "RecordingWordingTests"]},
    "K1": {"tests/test_consistency.py": ["InstallIdentityTests",
                                        "test_the_install_identity_names_are_pinned",
                                        "test_the_identity_file_pins_the_install_names",
                                        "test_the_app_and_the_scripts_match_the_identity_file",
                                        "test_no_earlier_app_name_is_left_in_shipped_code"]},
    "K2": {},
    "K3": {"tests/test_policy.py": ["WorkspacePolicyTests",
                                   "test_a_bad_value_never_grants_a_permission"],
           "tests/test_two_track.py": [
               "test_turned_off_unreadable_or_invalid_policy_refuses_it",
               "test_policy_turned_off_mid_recording_stops_only_the_computer_track",
               "test_an_invalid_policy_value_mid_recording_stops_only_the_computer_track",
               "test_resume_checks_the_policy_again"],
           "tests/test_drafts.py": [
               "test_policy_off_bad_or_unreadable_withholds_drafts",
               "test_the_choice_and_the_computer_policy_apply_mid_recording"],
           "tests/test_download_queue.py": [
               "test_policy_that_turns_downloads_off_empties_the_queue",
               "test_policy_read_when_the_next_one_would_start",
               "test_an_unreadable_policy_grants_nothing_at_the_next_start"]},
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


# Planned code work in SECURITY_MODEL.md: a #111 slice (B7) or an issue (#299). Owner,
# IT and Python-version entries in a Planned cell are not code work the app owes.
PLANNED_WORK = re.compile(rf"{SLICE.pattern}|#\d+")


class SecuritySummaryTests(unittest.TestCase):
    """The Summary of docs/SECURITY_MODEL.md lists as "Not protected today, planned"
    exactly the work the threat rows still plan, threat by threat (#409), so a slice
    that lands or a newly planned control cannot leave the Summary out of date."""

    @classmethod
    def setUpClass(cls):
        text = read("docs/SECURITY_MODEL.md")
        cls.summary = section(text, "Summary")
        cls.rows = table_rows(section(text, "Threats and controls"), r"T\d+")
        cls.merged = re.search(r"\(merged so far: ([^)]*)\)", text).group(1)

    @staticmethod
    def add(found, work, threat):
        found.setdefault(work, set()).add(threat)

    def planned_by_rows(self):
        # A Planned cell is "; "-separated entries; code work is an entry that starts
        # "B7:" or "#299:". Anything else (IT:, Owner:, None) is not code work.
        found = {}
        for threat, cells in self.rows.items():
            for entry in cells[4].split("; "):
                head = entry.split(":", 1)[0].strip()
                if ":" in entry and PLANNED_WORK.fullmatch(head):
                    self.add(found, head, threat)
        return found

    def planned_by_summary(self):
        # Each item under the heading ends "(B7: T9, T10)." naming its work and threats.
        block = self.summary.split("- **Not protected today, planned.**", 1)[1]
        block = block.split("\n- **", 1)[0]
        items = [line for line in block.splitlines() if line.startswith("  - ")]
        self.assertTrue(items, "the Summary's planned list no longer matches")
        found = {}
        for item in items:
            match = re.search(r"\(([^()]+): (T\d+(?:, T\d+)*)\)\.$", item)
            self.assertIsNotNone(match, f"a planned item does not end (work: threats): {item}")
            work = PLANNED_WORK.findall(match.group(1))
            self.assertTrue(work, f"a planned item names no slice or issue: {item}")
            for name in work:
                for threat in re.findall(r"T\d+", match.group(2)):
                    self.add(found, name, threat)
        return found

    def test_planned_list_matches_the_rows(self):
        rows = self.planned_by_rows()
        self.assertTrue(rows, "no row plans code work: rewrite the Summary and this test")
        self.assertEqual(self.planned_by_summary(), rows,
                         "the Summary's 'Not protected today, planned' items differ from "
                         "the work the threat rows still plan (slice or issue: threats)")

    def test_planned_work_is_not_listed_as_merged(self):
        # "A0 to A7" style ranges in the merged list count every slice in between.
        merged = set(SLICE.findall(self.merged))
        for letter, first, last in re.findall(r"\b([A-D])(\d+) to \1(\d+)\b", self.merged):
            merged.update(f"{letter}{n}" for n in range(int(first), int(last) + 1))
        self.assertIn("C7", merged, "the merged-slices sentence no longer matches")
        still_planned = set(self.planned_by_rows()) & merged
        self.assertEqual(set(), still_planned,
                         "slices listed as merged are still planned in a threat row")


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

    def test_app_control_guidance_cites_its_sources(self):
        # #304: every Microsoft claim cites a listed Microsoft Learn page, every listed
        # page is cited, and the security model's residual points to the guidance.
        heading = "Stopping per-user copies on managed computers"
        guide = section(self.text, heading)
        body, sources = guide.split("\n### Microsoft sources\n", 1)
        listed = table_rows(sources, r"M\d+")
        self.assertGreaterEqual(len(listed), 10, "the sources table no longer matches")
        self.assertEqual(set(re.findall(r"\bM\d+\b", body)), set(listed),
                         "cited and listed Microsoft sources differ")
        for ref, cells in listed.items():
            with self.subTest(source=ref):
                self.assertRegex(cells[1], r"^\[[^]]+\]\(https://learn\.microsoft\.com/en-us/")
        anchor = heading.lower().replace(" ", "-")
        self.assertIn(anchor, heading_anchors(self.text))
        t24 = table_rows(section(read("docs/SECURITY_MODEL.md"), "Threats and controls"),
                         r"T24")["T24"]
        self.assertIn(f"(IT_DEPLOYMENT.md#{anchor})", t24[5])

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


RELEASE_VERSION = r"v?([0-9]+\.[0-9]+\.[0-9]+(?:-preview\.[0-9]+)?)\b"
# The docs that name the newest release (RELEASE_CHECKLIST.md, "Straight after publishing").
NEWEST_RELEASE_DOCS = ("README.md", "docs/WORKSPACE_GUIDE.md", "docs/IT_DEPLOYMENT.md",
                       "docs/PRIVACY_IMPACT_ASSESSMENT_DRAFT.md")


def changelog_versions():
    """The changelog's version headings, newest first."""
    return re.findall(r"^## v(\S+)", read("CHANGELOG.md"), re.MULTILINE)


class ReleaseMarkerTests(unittest.TestCase):
    """#411: "Not in vX" markers and the "newest release" lines name one release.

    The newest release is the one the docs' "newest release" lines name. It must be
    the newest changelog heading (VERSION, VersionTests), or the one before it
    while a release is prepared but not yet published: tools/changelog.py writes
    the new heading before the tag, and the docs move "straight after publishing"
    (RELEASE_CHECKLIST.md). A marker that names any other version is stale."""

    MARKER = re.compile(r"\bnot in " + RELEASE_VERSION, re.IGNORECASE)
    NEWEST = re.compile(r"\bthe newest release(?:,| is) " + RELEASE_VERSION, re.IGNORECASE)

    def newest_release(self):
        named = {}
        for doc in NEWEST_RELEASE_DOCS:
            found = self.NEWEST.findall(read(doc))
            self.assertTrue(found, f"{doc} no longer names the newest release")
            named.update({v: doc for v in found})
        self.assertEqual(1, len(named), f"the docs name different newest releases: {named}")
        return next(iter(named))

    def test_newest_release_lines_name_a_current_version(self):
        newest = self.newest_release()
        headings = changelog_versions()
        self.assertIn(newest, headings[:2],
                      f"the docs' newest release v{newest} is neither the newest changelog "
                      f"heading v{headings[0]} nor the one before it: set the 'newest release' "
                      "lines when a release is published (RELEASE_CHECKLIST.md)")

    def test_markers_name_the_newest_release(self):
        newest = self.newest_release()
        seen = 0
        for doc in DocsTests.DOCS:
            for line_no, line in enumerate(read(doc).splitlines(), 1):
                for version in self.MARKER.findall(line):
                    seen += 1
                    with self.subTest(doc=doc, line=line_no):
                        self.assertEqual(newest, version,
                                         f"{doc}:{line_no} says 'Not in v{version}', but the "
                                         f"newest release is v{newest}: remove the marker if "
                                         "it shipped, otherwise name the newest release")
        self.assertGreater(seen, 0, "no 'Not in vX' marker found: the pattern no longer "
                                    "matches (IT_DEPLOYMENT.md explains the marker)")

    def test_the_patterns_find_what_the_docs_write(self):
        self.assertEqual(["0.5.0-preview.4", "0.5.0"], self.MARKER.findall(
            "(**Not in v0.5.0-preview.4**) and not in v0.5.0. Not in that release."))
        self.assertEqual(["0.5.0-preview.4", "1.2.3"], self.NEWEST.findall(
            "The newest release, 0.5.0-preview.4 (27 October 2026), ... "
            "The newest release is v1.2.3 (1 May 2027)."))


def css_colour(value):
    """(r, g, b, alpha) from a tokens.css colour: #RRGGBB, rgba() or transparent."""
    value = value.strip().lower()
    if value == "transparent":
        return (0, 0, 0, 0.0)
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5)) + (1.0,)
    m = re.fullmatch(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)", value)
    if not m:
        raise AssertionError(f"unexpected colour in tokens.css: {value}")
    return (int(m[1]), int(m[2]), int(m[3]), float(m[4]))


def shell_colour(value):
    """(r, g, b, alpha) from a shell._THEMES colour: "#RRGGBB" or (r, g, b, alpha)."""
    if isinstance(value, str):
        return css_colour(value)
    r, g, b, a = value
    return (r, g, b, float(a))


def theme_tokens():
    """{"light": {token: colour}, "dark": {token: colour}} from tokens.css: :root,
    then the dark theme's overrides inside @media (prefers-color-scheme: dark)."""
    css = re.sub(r"/\*.*?\*/", "", read("workspace/ui/tokens.css"), flags=re.DOTALL)
    dark_at = css.index("@media (prefers-color-scheme: dark)")
    pattern = r"(--[\w-]+)\s*:\s*([^;]+);"
    light = dict(re.findall(pattern, css[css.index(":root {"):dark_at]))
    dark = {**light, **dict(re.findall(pattern, css[dark_at:]))}
    return {"light": light, "dark": dark}


class DialogColourTests(unittest.TestCase):
    """#411: the confirmations the app draws itself (workspace/shell.py, _THEMES)
    use the page's colours (workspace/ui/tokens.css). Read from the source, so no
    window library is imported."""

    # _THEMES key: the token, or (background, text) tokens. The confirmation's
    # icons follow the page's .c-icon (--sp1) and .c-icon.danger.
    SINGLE = {"card": "--card", "text": "--text", "muted": "--muted", "soft": "--soft",
              "line": "--control-line", "focus": "--accent"}
    PAIRS = {"primary": ("--primary", "--primary-fg"),
             "secondary": ("--secondary", "--secondary-fg"),
             "danger": ("--danger", "--danger-fg"), "record": ("--record", "--record-fg")}
    ICONS = {"tick": ("--sp1", "--sp1-fg"), "mic": ("--sp1", "--sp1-fg"),
             "bin": ("--danger", "--danger-fg")}

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(read("workspace/shell.py"))
        values = [node.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(getattr(t, "id", None) == "_THEMES" for t in node.targets)]
        assert len(values) == 1, "expected one _THEMES assignment in workspace/shell.py"
        cls.themes = ast.literal_eval(values[0])
        cls.tokens = theme_tokens()

    def drawn(self, theme, token):
        """A token's colour as the dialog draws it: a transparent one shows the card."""
        tokens = self.tokens[theme]
        colour = css_colour(tokens[token])
        return css_colour(tokens["--card"]) if colour[3] == 0 else colour

    def test_every_key_is_tied_to_a_token(self):
        for theme, colours in self.themes.items():
            with self.subTest(theme=theme):
                self.assertEqual(set(self.SINGLE) | set(self.PAIRS)
                                 | {"scrim", "scrim_opacity", "icon"}, set(colours))
                self.assertEqual(set(self.ICONS), set(colours["icon"]))
        self.assertEqual({"light", "dark"}, set(self.themes))

    def test_dialog_colours_match_the_page_tokens(self):
        for theme, colours in self.themes.items():
            for key, token in self.SINGLE.items():
                with self.subTest(theme=theme, key=key):
                    self.assertEqual(self.drawn(theme, token), shell_colour(colours[key]),
                                     f"shell._THEMES[{theme!r}][{key!r}] differs from {token}")
            pairs = [(k, v, colours[k]) for k, v in self.PAIRS.items()]
            pairs += [(f"icon {k}", v, colours["icon"][k]) for k, v in self.ICONS.items()]
            for key, tokens, values in pairs:
                for token, value in zip(tokens, values, strict=True):
                    with self.subTest(theme=theme, key=key, token=token):
                        self.assertEqual(self.drawn(theme, token), shell_colour(value),
                                         f"shell._THEMES[{theme!r}] {key} differs from {token}")
            with self.subTest(theme=theme, key="scrim"):
                r, g, b, _ = shell_colour(colours["scrim"])
                self.assertEqual(css_colour(self.tokens[theme]["--scrim"]),
                                 (r, g, b, float(colours["scrim_opacity"])))

    def test_the_colour_readers(self):
        self.assertEqual((20, 41, 102, 1.0), css_colour("#142966"))
        self.assertEqual((5, 12, 33, 0.6), css_colour("rgba(5, 12, 33, 0.6)"))
        self.assertEqual((0, 0, 0, 0.0), css_colour("transparent"))
        self.assertEqual((255, 255, 255, 0.74), shell_colour((255, 255, 255, 0.74)))
        tokens = theme_tokens()
        self.assertEqual("#142966", tokens["light"]["--primary"])
        self.assertEqual("#B4DDF6", tokens["dark"]["--primary"])
        self.assertEqual("#B3261E", tokens["dark"]["--danger"], "inherited from :root")


if __name__ == "__main__":
    unittest.main()
