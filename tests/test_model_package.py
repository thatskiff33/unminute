# The model package (#111 B7a): tools/intune/New-ModelPackage.ps1 and
# packaging/models/Install-Models.ps1 against the app's own rules. The record IT's
# script writes must be one the app trusts (asr_engines.check_machine), the names
# and patterns the scripts use must cover every pinned model, and the functions
# Install-Models.ps1 shares with the installer must stay the installer's own.
# The scripts' behaviour is tested by Pester (tests/powershell/Models.Tests.ps1).

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "tools"))
import windows_powershell  # noqa: E402  (Windows PowerShell 5.1 by its full path)
from workspace import asr_engines as ae  # noqa: E402

MODELS_SCRIPT = ROOT / "packaging" / "models" / "Install-Models.ps1"
PACKAGE_SCRIPT = ROOT / "tools" / "intune" / "New-ModelPackage.ps1"


def read(path):
    return Path(path).read_text(encoding="utf-8")


def ps_function(text, name):
    """One function's text, from its "function <name>" line to its closing brace at
    the start of a line (or the same line, for a one-line function)."""
    match = re.search(rf"(?m)^function {re.escape(name)}\b[^\n]*\n?", text)
    if match is None:
        raise AssertionError(f"no function {name}")
    first = match.group(0)
    if first.count("{") and first.count("{") == first.count("}"):
        return first.rstrip("\r\n")
    end = re.compile(r"(?m)^\}\r?$").search(text, match.end())
    return text[match.start():end.end()]


def ps_string(text, name):
    """The value of a top-level "$name = '...'" or "$name = \"...\"" line."""
    match = re.search(rf"""(?m)^\${name} = (?:"([^"]*)"|'([^']*)')\r?$""", text)
    if match is None:
        raise AssertionError(f"no ${name}")
    return match.group(1) if match.group(1) is not None else match.group(2)


class SharedWithTheInstallerTests(unittest.TestCase):
    """Install-Models.ps1 checks the install folder, takes the lock and removes
    folders exactly as install.ps1 does: the same functions, word for word."""

    SHARED = ("Get-FileSha256", "Test-TransientLock", "Invoke-LockRetry", "Test-Link",
              "Remove-OwnFolder", "Remove-FolderTree", "Enter-InstallLock", "Exit-InstallLock",
              "Test-Elevated", "Test-64BitProcess", "Get-ProgramFilesFolder",
              "Get-FolderSecurity", "Get-MachineRoot", "Get-MachineRule",
              "Test-MachineRootSecurity", "Test-AclChanged", "Get-AclChangeCount")

    def test_the_shared_functions_are_the_installers(self):
        installer, models = read(ROOT / "packaging" / "install.ps1"), read(MODELS_SCRIPT)
        for name in self.SHARED:
            with self.subTest(function=name):
                self.assertEqual(ps_function(models, name).replace("\r\n", "\n"),
                                 ps_function(installer, name).replace("\r\n", "\n"))

    def test_the_same_names_and_permissions(self):
        installer, models = read(ROOT / "packaging" / "install.ps1"), read(MODELS_SCRIPT)
        for name in ("MachineFolderName", "LockName", "AdministratorsSid", "SystemSid",
                     "UsersSid", "AppPackagesSid", "PermissionMessage", "ModelsFolderName"):
            with self.subTest(name=name):
                self.assertEqual(ps_string(models, name), ps_string(installer, name))
        self.assertEqual(ps_string(models, "ModelsFolderName"),
                         ps_string(read(ROOT / "packaging" / "uninstall.ps1"), "ModelsFolderName"))
        identity = json.loads(read(ROOT / "packaging" / "identity.json"))
        self.assertEqual(identity["modes"]["machine"]["models_folder"],
                         ps_string(models, "ModelsFolderName"))
        self.assertEqual(identity["modes"]["machine"]["models_record"], ae.MACHINE_RECORD)
        # Program Files from Windows' known folder, set once at the top (the tests
        # redirect it), and nothing else outside the install folder.
        self.assertEqual(re.findall(r"(?m)^\$ProgramFilesFolder = (.+?)\r?$", models),
                         ['[Environment]::GetFolderPath("ProgramFiles")'])
        self.assertNotRegex(models, r"(?i)HKLM:|HKEY_|Registry::|CurrentVersion\\Uninstall")


class NamesTests(unittest.TestCase):
    """The scripts' names and patterns are the app's, and fit every pinned model."""

    SPECS = [*ae.PARAKEET_MODELS.values(), ae.SILERO_VAD, *ae.WHISPER_MODELS.values(),
             ae.SPEAKER_MODEL]

    def test_record_and_marker_names(self):
        models, package = read(MODELS_SCRIPT), read(PACKAGE_SCRIPT)
        self.assertEqual(ps_string(models, "RecordName"), ae.MACHINE_RECORD)
        self.assertEqual(ps_string(package, "SetupMarker"), ae.VERIFIED_MARKER)
        self.assertEqual(ps_string(package, "Detector"), ae.SILERO_VAD.name)

    def test_every_pinned_model_fits_the_patterns(self):
        for text in (read(MODELS_SCRIPT), read(PACKAGE_SCRIPT)):
            name = ps_string(text, "NamePattern").replace(r"\z", r"\Z")
            revision = ps_string(text, "RevisionPattern").replace(r"\z", r"\Z")
            part = ps_string(text, "PartPattern").replace(r"\z", r"\Z")
            for spec in self.SPECS:
                with self.subTest(model=spec.name):
                    self.assertRegex(spec.family, name)
                    self.assertRegex(spec.folder, name)
                    self.assertRegex(spec.revision, revision)
                    for f in spec.files:
                        for piece in f.name.split("/"):
                            self.assertRegex(piece, part)
                        self.assertNotEqual(f.name.lower(), ae.MACHINE_RECORD)

    def test_both_scripts_are_ascii_with_their_dot_source_guard(self):
        for path in (MODELS_SCRIPT, PACKAGE_SCRIPT):
            with self.subTest(script=path.name):
                data = path.read_bytes()
                self.assertTrue(data.isascii(), "Windows PowerShell 5.1 reads it in the code page")
                self.assertIn(b'if ($MyInvocation.InvocationName -eq ".") { return }', data)


@unittest.skipUnless(sys.platform == "win32", "Windows PowerShell writes the record")
class RecordInteropTests(unittest.TestCase):
    """A record written by Install-Models.ps1's own functions is one the app trusts,
    and only while the files are unchanged. Only those two functions run, copied into
    a scratch script: nothing else of the script, no install folder, no registry."""

    CONTENTS = {"config.json": b'{"m": "one"}', "sub/weights.bin": b"\x01\x02" * 300}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.spec = ae.ModelSpec(
            name="fake-model", folder="fake-model-int8", repo="example/fake", revision="a" * 40,
            files=tuple(ae.ModelFile(n, len(d), hashlib.sha256(d).hexdigest())
                        for n, d in self.CONTENTS.items()))
        self.folder = base / "models" / "parakeet" / self.spec.folder / self.spec.revision
        for name, data in self.CONTENTS.items():
            (self.folder / name).parent.mkdir(parents=True, exist_ok=True)
            (self.folder / name).write_bytes(data)
        model = {"name": self.spec.name, "family": "parakeet", "folder": self.spec.folder,
                 "repository": self.spec.repo, "revision": self.spec.revision,
                 "files": [{"name": f.name, "size": f.size, "sha256": f.sha256}
                           for f in self.spec.files]}
        self.model_file = base / "model.json"
        self.model_file.write_text(json.dumps(model), encoding="utf-8")
        text = read(MODELS_SCRIPT)
        functions = [ps_function(text, n) for n in ("Get-ModelFingerprint", "Write-ModelRecord")]
        for body in functions:     # they reach nothing but the folder they are given
            self.assertNotRegex(
                body, r"(?i)GetFolderPath|HKLM|HKEY_|Remove-|Program|Invoke-|\$env:")
        self.script = base / "write-record.ps1"
        self.script.write_text("\r\n".join([
            '$ErrorActionPreference = "Stop"',
            f'$RecordName = "{ae.MACHINE_RECORD}"', *functions,
            "$model = [IO.File]::ReadAllText($args[0]) | ConvertFrom-Json",
            "Write-ModelRecord $args[1] $model"]) + "\r\n", encoding="ascii")

    def write_record(self):
        done = subprocess.run(
            [windows_powershell.executable(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(self.script), str(self.model_file), str(self.folder)],
            env=windows_powershell.environment(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])

    def test_the_app_trusts_the_record_without_hashing(self):
        self.write_record()
        record = json.loads((self.folder / ae.MACHINE_RECORD).read_text(encoding="utf-8"))
        self.assertEqual(record["files"], ae._fingerprint(self.folder, self.spec))
        root = self.folder.parents[3]
        with mock.patch.object(ae, "_machine_root", [root]), \
                mock.patch.object(ae, "_sha256", side_effect=AssertionError("hashed")):
            self.assertEqual(ae.check_machine(self.spec, self.folder), [])

    def test_a_changed_file_is_hashed_and_refused(self):
        self.write_record()
        path = self.folder / "sub" / "weights.bin"
        path.write_bytes(b"\x07\x07" * 300)
        later = time.time() + 5
        os.utime(path, (later, later))
        with mock.patch.object(ae, "_machine_root", [self.folder.parents[3]]):
            self.assertIn("checksum", ae.check_machine(self.spec, self.folder)[0])


if __name__ == "__main__":
    unittest.main()
