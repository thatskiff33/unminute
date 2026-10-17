# Package build helpers (#11). No network: the runtime download and wheel
# installation are exercised by tools/build_package.py on a build machine.
#   python -m unittest discover -s tests -v

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import build_package as bp  # noqa: E402


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pth_isolates_the_runtime(self):
        rt = self.root / "runtime"
        rt.mkdir()
        pth = rt / f"python{bp.PYTHON_TAG}._pth"
        pth.write_text(f"python{bp.PYTHON_TAG}.zip\n.\n#import site\n")
        bp.write_pth(rt)
        lines = pth.read_text().splitlines()
        self.assertEqual(lines, [f"python{bp.PYTHON_TAG}.zip", ".", bp.SITE, "..\\app",
                                 "import site"])
        self.assertIn("addsitedir", (rt / "sitecustomize.py").read_text())  # .pth files work

    def test_deep_licences_are_flattened_indexed_and_recorded(self):
        site = self.root / bp.SITE
        rel = f"torch-9.dist-info/licenses/{'x' * 60}/{'y' * 60}/LICENSE"
        deep = site / rel
        deep.parent.mkdir(parents=True)
        deep.write_text("licence text")
        (site / "torch-9.dist-info" / "RECORD").write_bytes(
            f"torch/__init__.py,sha256=a,1\r\n{rel},sha256=b,12\r\n"
            "torch-9.dist-info/RECORD,,\r\n".encode())
        (site / "torch").mkdir()
        (site / "torch" / "__init__.py").write_text("")
        shallow = site / "numpy-2.dist-info" / "licenses" / "LICENSE.txt"
        shallow.parent.mkdir(parents=True)
        shallow.write_text("kept in place")
        bp.flatten_licences(site)
        flat = site / "torch-9.dist-info" / "licenses" / "flat"
        self.assertEqual((flat / "000-LICENSE").read_text(), "licence text")
        self.assertIn(("x" * 60), (flat / "INDEX.txt").read_text())
        self.assertFalse((site / "torch-9.dist-info" / "licenses" / ("x" * 60)).exists())
        self.assertTrue(shallow.exists())
        # RECORD follows the move, and lists the index, so the notices find both.
        self.assertEqual((site / "torch-9.dist-info" / "RECORD").read_bytes(),
                         b"torch/__init__.py,sha256=a,1\r\n"
                         b"torch-9.dist-info/licenses/flat/000-LICENSE,sha256=b,12\r\n"
                         b"torch-9.dist-info/RECORD,,\r\n"
                         b"torch-9.dist-info/licenses/flat/INDEX.txt,,\r\n")
        bp.sync_records(site)                                   # every row is a real file
        self.assertEqual(bp.licence_files(site, "torch"), [
            f"runtime/{bp.SITE}/torch-9.dist-info/licenses/flat/000-LICENSE",
            f"runtime/{bp.SITE}/torch-9.dist-info/licenses/flat/INDEX.txt"])

    def test_manifest_lists_every_file_with_size_and_hash(self):
        (self.root / "a").mkdir()
        (self.root / "a" / "f.txt").write_bytes(b"abc")
        self.assertEqual(bp.manifest(self.root), 1)
        m = json.loads((self.root / "package-manifest.json").read_text())
        self.assertEqual(m["a/f.txt"], [3, bp.sha256(self.root / "a" / "f.txt")])

    def test_runtime_pin_is_a_sha256(self):
        self.assertRegex(bp.EMBED_SHA256, r"^[0-9a-f]{64}$")
        self.assertIn(bp.PYTHON_VERSION, bp.EMBED_URL)

    def test_the_version_comes_from_the_version_file(self):
        # #131: VERSION is the one source; --version may repeat it, never change it.
        (self.root / "VERSION").write_text("0.5.0\n", encoding="utf-8")
        self.assertEqual("0.5.0", bp.release_version(root=self.root))
        self.assertEqual("0.5.0", bp.release_version("0.5.0", root=self.root))
        with self.assertRaisesRegex(SystemExit, "does not match VERSION"):
            bp.release_version("0.5.1", root=self.root)

    def test_a_version_file_is_read_safely(self):
        (self.root / "VERSION").write_bytes(b"\xef\xbb\xbf0.5.0-preview.2\r\n")   # BOM, CRLF
        self.assertEqual("0.5.0-preview.2", bp.release_version(root=self.root))
        for bad in ("v0.5.0", "0.5", "0.5.0-rc1", "10000.10000.10000"):   # the last is 17 chars
            (self.root / "VERSION").write_text(bad + "\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "not X.Y.Z"):
                bp.release_version(root=self.root)
        (self.root / "VERSION").unlink()
        with self.assertRaisesRegex(SystemExit, "Cannot read"):
            bp.release_version(root=self.root)


def report_entry(name, version, wheel, digest, **meta):
    """One "install" entry as pip's --report writes it."""
    return {"metadata": {"name": name, "version": version, **meta},
            "download_info": {"url": f"file:///C:/build/cache/wheels/{wheel}",
                              "archive_info": {"hashes": {"sha256": digest}}}}


def pe_bytes():
    """The smallest header is_native() takes for a Windows executable."""
    head = bytearray(64)
    head[:2] = b"MZ"
    head[0x3C:0x40] = (64).to_bytes(4, "little")
    return bytes(head) + b"PE\0\0" + bytes(16)


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class ReproducibleBuildTests(unittest.TestCase):
    """#111 B2: the same commit and lock give the same package, and its bill of
    materials, notices and binary list say what is in it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_licences_come_from_the_wheel_metadata(self):
        self.assertEqual(bp.licence_of({"license_expression": "MIT OR Apache-2.0"}),
                         ("expression", "MIT OR Apache-2.0"))
        self.assertEqual(bp.licence_of({"classifier": [
            "License :: OSI Approved :: BSD License", "Programming Language :: Python"]}),
            ("name", "BSD License"))
        self.assertEqual(bp.licence_of({"classifier": [
            "License :: OSI Approved :: MIT License",
            "License :: OSI Approved :: Apache Software License"]}),
            ("name", "MIT License; Apache Software License"))   # not an OR: unknown relation
        self.assertEqual(bp.licence_of({"license": "MIT"}), ("name", "MIT"))
        self.assertEqual(bp.licence_of({"license": "Copyright ...\n" * 40}),
                         (None, None))                  # a whole licence text: not a name
        self.assertEqual(bp.licence_of({}), (None, None))

    def test_the_bill_of_materials_names_each_wheel_and_its_hash(self):
        installed = [report_entry("NumPy", "2.4.6", "numpy-2.4.6-cp312-cp312-win_amd64.whl",
                                  "a" * 64, license_expression="BSD-3-Clause"),
                     report_entry("av", "17.1.0", "av-17.1.0-cp312-cp312-win_amd64.whl",
                                  "b" * 64, license="BSD"),
                     report_entry("torch", "2.9.0+cu128", "torch.whl", "c" * 64)]
        tools = [("python", "3.12.10"), ("pip", "25.2")]
        bom = bp.sbom(installed, "Unminute", "0.5.0", "c" * 40, 1760000000, "d" * 64,
                      tools=tools)
        self.assertEqual(bom["bomFormat"], "CycloneDX")
        self.assertEqual(bom["metadata"]["component"]["version"], "0.5.0")
        names = [c["name"] for c in bom["components"]]
        self.assertEqual(names, [bp.RUNTIME_NAME, "av", "NumPy", "torch"])  # runtime, sorted
        av, numpy, torch = bom["components"][1:]
        self.assertEqual(numpy["purl"], "pkg:pypi/numpy@2.4.6")
        self.assertEqual(torch["purl"], "pkg:pypi/torch@2.9.0%2Bcu128")    # encoded
        self.assertEqual(numpy["hashes"], [{"alg": "SHA-256", "content": "a" * 64}])
        self.assertEqual(numpy["licenses"], [{"expression": "BSD-3-Clause"}])
        self.assertEqual(av["licenses"], [{"license": {"name": "BSD"}}])
        self.assertNotIn("licenses", torch)                     # none stated: none claimed
        self.assertEqual(numpy["properties"][0]["value"],
                         "numpy-2.4.6-cp312-cp312-win_amd64.whl")         # no build paths
        self.assertNotIn("C:/build", json.dumps(bom))
        self.assertEqual([(t["name"], t.get("version")) for t
                          in bom["metadata"]["tools"]["components"]],
                         [(bp.BUILDER_NAME, None), ("python", "3.12.10"), ("pip", "25.2")])
        self.assertIn({"name": "unminute:build", "value": "development"},
                      bom["metadata"]["component"]["properties"])
        again = bp.sbom(list(reversed(installed)), "Unminute", "0.5.0", "c" * 40, 1760000000,
                        "d" * 64, tools=tools)
        self.assertEqual(json.dumps(bom), json.dumps(again))             # reproducible
        other = bp.sbom(installed, "Unminute", "0.5.0", "e" * 40, 1760000000, "d" * 64)
        self.assertNotEqual(bom["serialNumber"], other["serialNumber"])
        release = bp.sbom(installed, "Unminute", "0.5.0", "c" * 40, 1760000000, "d" * 64,
                          release=True)
        self.assertNotEqual(bom["serialNumber"], release["serialNumber"])
        self.assertIn({"name": "unminute:build", "value": "release"},
                      release["metadata"]["component"]["properties"])

    def test_a_package_without_a_recorded_hash_stops_the_build(self):
        entry = report_entry("numpy", "2.4.6", "n.whl", "")
        with self.assertRaisesRegex(SystemExit, "no SHA-256 for numpy"):
            bp.packages([entry])

    def _wheel(self, site, dist, *files, record=True):
        """A wheel installed in site: its files and RECORD (CSV)."""
        info = site / f"{dist}.dist-info"
        info.mkdir(parents=True)
        for f in files:
            (site / f).parent.mkdir(parents=True, exist_ok=True)
            (site / f).write_bytes(b"x")
        if record:
            (info / "RECORD").write_text("".join(f"{f},sha256=x,1\n" for f in files)
                                         + f"{dist}.dist-info/RECORD,,\n")

    def test_notices_list_every_component_its_texts_and_native_libraries(self):
        folder = self.root / "Unminute-0.5.0"
        site = folder / "runtime" / bp.SITE
        (folder / "runtime").mkdir(parents=True)
        (folder / "runtime" / "LICENSE.txt").write_text("PSF")
        self._wheel(site, "numpy-2.4.6", "numpy/__init__.py",
                    "numpy-2.4.6.dist-info/licenses/LICENSE.txt",
                    "numpy-2.4.6.dist-info/licenses/vendor/README")   # all of licenses/
        self._wheel(site, "onnxruntime-1.30.0", "onnxruntime/__init__.py",
                    "onnxruntime/LICENSE", "onnxruntime/ThirdPartyNotices.txt",
                    "onnxruntime/capi/onnxruntime.dll")         # beside the code
        self._wheel(site, "av-17.1.0", "av/__init__.py", "av.libs/avcodec-62-abc.dll",
                    "av.libs/libmp3lame-0-def.dll", "av-17.1.0.dist-info/licenses/LICENSE.txt")
        self._wheel(site, "bare-1.0", "bare/__init__.py")
        installed = [report_entry("numpy", "2.4.6", "n.whl", "a" * 64,
                                  license_expression="BSD-3-Clause"),
                     report_entry("onnxruntime", "1.30.0", "o.whl", "c" * 64,
                                  license="MIT License"),
                     report_entry("av", "17.1.0", "a.whl", "d" * 64,
                                  license_expression="BSD-3-Clause"),
                     report_entry("bare", "1.0", "b.whl", "b" * 64)]
        lib = f"runtime/{bp.SITE}"
        signed = {f"{lib}/av.libs/avcodec-62-abc.dll":
                  {"signer": 'CN="Example, Ltd", O=Example', "thumbprint": "", "timestamped": 0},
                  f"{lib}/onnxruntime/capi/onnxruntime.dll":
                  {"signer": "CN=Microsoft Corporation, O=Microsoft Corporation"}}
        # Every DLL must have a signature result; one without stops the build.
        with self.assertRaisesRegex(SystemExit, "libmp3lame-0-def.dll is not in the signature"):
            bp.notices(installed, site, folder, "0.5.0", signed)
        signed[f"{lib}/av.libs/libmp3lame-0-def.dll"] = {"signer": ""}
        text = bp.notices(installed, site, folder, "0.5.0", signed)
        self.assertIn("numpy 2.4.6\n  Licence: BSD-3-Clause", text)
        self.assertIn(f"{lib}/numpy-2.4.6.dist-info/licenses/LICENSE.txt", text)
        self.assertIn(f"{lib}/numpy-2.4.6.dist-info/licenses/vendor/README", text)
        self.assertIn(f"{lib}/onnxruntime/ThirdPartyNotices.txt", text)
        self.assertIn(f"    {lib}/onnxruntime/capi/onnxruntime.dll "
                      "(signed by Microsoft Corporation)", text)       # any folder, not .libs
        self.assertIn(f"    {lib}/av.libs/avcodec-62-abc.dll (signed by Example, Ltd)", text)
        self.assertIn(f"    {lib}/av.libs/libmp3lame-0-def.dll (unsigned)", text)
        self.assertIn("bare 1.0\n  Licence: not stated in the metadata (see the texts)\n"
                      "  Texts: none shipped in the wheel", text)
        self.assertIn("PSF-2.0", text)
        # A text the notices name must be in the package.
        (site / "onnxruntime" / "LICENSE").unlink()
        with self.assertRaisesRegex(SystemExit, "onnxruntime/LICENSE"):
            bp.notices(installed, site, folder, "0.5.0", signed)

    def test_a_package_without_a_dist_info_folder_stops_the_build(self):
        site = self.root / bp.SITE
        site.mkdir()
        with self.assertRaisesRegex(SystemExit, "No dist-info folder for numpy"):
            bp.wheel_files(site, "numpy")

    def test_the_model_manifest_has_every_pinned_file(self):
        import asr_engines as ae
        packaged = self.root / "app" / "asr_engines.py"            # the file being packaged
        packaged.parent.mkdir()
        packaged.write_bytes((bp.ROOT / "asr_engines.py").read_bytes())
        m = bp.model_manifest(packaged)
        # Nothing is written beside it: a .pyc would ship, with the time it was made.
        self.assertEqual([p.name for p in packaged.parent.iterdir()], ["asr_engines.py"])
        self.assertEqual({x["name"] for x in m["models"]},
                         {*ae.PARAKEET_MODELS, ae.SILERO_VAD.name, *ae.WHISPER_MODELS,
                          ae.SPEAKER_MODEL.name})
        for model in m["models"]:
            self.assertTrue(model["files"])
            for f in model["files"]:
                self.assertRegex(f["sha256"], r"^[0-9a-f]{64}$")

    def test_executables_are_found_by_suffix_or_by_header(self):
        folder = self.root / "pkg"
        files = {"runtime/python.exe": b"x", "runtime/lib/x/_core.pyd": b"x",
                 "runtime/lib/x/data.txt": b"MZ not a program",
                 "runtime/lib/x/payload.bin": pe_bytes(),           # renamed Windows binary
                 "runtime/lib/x/libfoo.so.1": b"\x7fELF" + bytes(60)}
        for rel, data in files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(data)
        self.assertEqual(bp.binaries(folder), ["runtime/lib/x/_core.pyd",
                                               "runtime/lib/x/libfoo.so.1",
                                               "runtime/lib/x/payload.bin",
                                               "runtime/python.exe"])

    def test_binaries_must_match_the_allowlist_and_keep_their_signer(self):
        psf = "CN=Python Software Foundation, O=Python Software Foundation"
        signed = {"runtime/python.exe": {"signer": psf},
                  "runtime/lib/x/_core.pyd": {"signer": ""}}
        allow = self.root / "allow.json"
        self.assertEqual(bp.check_binaries(signed, allow, update=True), [])
        self.assertEqual([e["path"] for e in json.loads(allow.read_text())["binaries"]],
                         ["runtime/lib/x/_core.pyd", "runtime/python.exe"])    # sorted
        self.assertEqual(bp.check_binaries(signed, allow), [])
        changed = {"runtime/lib/x/_core.pyd": {"signer": "CN=Someone Else"},
                   "runtime/lib/x/new.dll": {"signer": ""}}
        self.assertEqual(bp.check_binaries(changed, allow), [
            "not allowlisted: runtime/lib/x/new.dll",
            "allowlisted but missing: runtime/python.exe",
            "signer changed: runtime/lib/x/_core.pyd (unsigned -> CN=Someone Else)"])

    def test_the_committed_allowlist_is_sorted_unique_and_names_signers(self):
        entries = json.loads(bp.ALLOWLIST.read_text(encoding="utf-8"))["binaries"]
        paths = [e["path"] for e in entries]
        self.assertEqual(paths, sorted(paths, key=str.lower))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertTrue(all(set(e) == {"path", "signer"} for e in entries))
        signer = {e["path"]: e["signer"] for e in entries}
        self.assertIn("Python Software Foundation", signer["runtime/python.exe"])

    def test_signatures_are_read_with_paths_outside_the_command(self):
        folder = self.root / "it's here"                  # a quote in the folder name
        paths = ["runtime/python.exe", "runtime/lib/a.dll"]
        seen = {"status": "NotSigned"}

        def fake_powershell(script, **files):
            seen["script"], seen["list"] = script, Path(files["list"]).read_text("utf-8")
            Path(files["out"]).write_text(json.dumps([
                {"status": "Valid", "signer": "CN=PSF", "thumbprint": "AB", "timestamped": True},
                {"status": seen["status"], "signer": "", "thumbprint": "",
                 "timestamped": False}]),
                encoding="utf-8-sig")
            return subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(bp, "powershell", fake_powershell):
            result = bp.signers(folder, paths)
        self.assertNotIn(str(folder), seen["script"])
        self.assertIn("$env:UNMINUTE_LIST", seen["script"])
        self.assertEqual(seen["list"].splitlines(),
                         [f"{folder}\\runtime\\python.exe", f"{folder}\\runtime\\lib\\a.dll"])
        self.assertEqual(result["runtime/python.exe"]["signer"], "CN=PSF")
        for seen["status"] in ("HashMismatch", "UnknownError", "NotTrusted", ""):
            with mock.patch.object(bp, "powershell", fake_powershell), \
                    self.assertRaisesRegex(SystemExit, "bad or unreadable signature"):
                bp.signers(folder, paths)

    def test_a_signature_from_the_machines_catalog_is_refused(self):
        # Get-AuthenticodeSignature falls back to the system catalogs: such a
        # signer belongs to this machine, not to the file that ships.
        def fake_powershell(script, **files):
            Path(files["out"]).write_text(json.dumps([
                {"status": "Valid", "signer": "CN=Microsoft Windows", "thumbprint": "AB",
                 "timestamped": True, "type": "Catalog"}]), encoding="utf-8-sig")
            return subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(bp, "powershell", fake_powershell), \
                self.assertRaisesRegex(SystemExit, "catalog"):
            bp.signers(self.root, ["runtime/msvcp140.dll"])

    def test_the_inventory_records_facts_not_verdicts(self):
        folder = self.root / "pkg"
        (folder / "runtime").mkdir(parents=True)
        (folder / "runtime" / "python.exe").write_bytes(b"abc")
        (folder / "runtime" / "x.pyd").write_bytes(b"de")
        inv = bp.binary_inventory(folder, {
            "runtime/python.exe": {"status": "Valid", "signer": "CN=PSF",
                                   "thumbprint": "AB12", "timestamped": True},
            "runtime/x.pyd": {"status": "NotSigned", "signer": "", "thumbprint": "",
                              "timestamped": False}})
        self.assertEqual(inv["binaries"], [
            {"path": "runtime/python.exe", "size": 3,
             "sha256": bp.sha256(folder / "runtime" / "python.exe"), "signed": True,
             "signer": "CN=PSF", "thumbprint": "AB12", "timestamped": True},
            {"path": "runtime/x.pyd", "size": 2, "sha256": bp.sha256(folder / "runtime" / "x.pyd"),
             "signed": False, "signer": "", "thumbprint": "", "timestamped": False}])
        self.assertNotIn("status", json.dumps(inv))     # "Valid" depends on the build machine

    def test_wheels_are_installed_from_the_lock_only_with_pip_isolated(self):
        calls = []

        def fake_run(cmd, check):
            calls.append(cmd)
            if "install" in cmd:
                Path(cmd[cmd.index("--report") + 1]).write_text('{"install": []}')
            return subprocess.CompletedProcess(cmd, 0)
        lock = self.root / "committed-requirements.txt"
        with mock.patch.object(bp.subprocess, "run", fake_run):
            self.assertEqual(bp.install_wheels(self.root / "lib", self.root / "cache",
                                               self.root / "report.json", lock), [])
        download, install = calls
        for cmd in calls:
            self.assertEqual(cmd[1:6], ["-I", "-m", "pip", "--isolated",
                                        "--disable-pip-version-check"])
            for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:"):
                self.assertIn(flag, cmd)
            self.assertEqual(cmd[cmd.index("-r") + 1], str(lock))     # the lock it was given
        self.assertIn("--no-index", install)
        self.assertIn("--no-compile", install)

    def test_record_lists_only_what_ships(self):
        # Launchers embed the build machine's path, so their hashes in RECORD
        # differ between builds; rows outside the package folder and rows for
        # removed files go. RECORD is CSV: a quoted name with a comma survives.
        site = self.root / bp.SITE
        (site / "bin").mkdir(parents=True)
        (site / "bin" / "tool.exe").write_bytes(b"MZ")
        (site / "tool").mkdir()
        (site / "tool" / "__init__.py").write_text("")
        (site / "tool" / "a,b.txt").write_text("")
        (self.root / "outside.txt").write_text("exists, but not in the package folder")
        info = site / "tool-1.0.dist-info"
        info.mkdir()
        (info / "RECORD").write_bytes(b"tool/__init__.py,sha256=abc,10\r\n"
                                      b"../outside.txt,sha256=mno,5\r\n"
                                      b"../../bin/tool.exe,sha256=xyz,2\r\n"
                                      b"../../share/man/tool.1,sha256=def,3\r\n"
                                      b"tool/pruned.dll,sha256=ghi,4\r\n"
                                      b'"tool/a,b.txt",sha256=jkl,0\r\n'
                                      b"tool-1.0.dist-info/RECORD,,\r\n")
        bp.drop_launchers(site)
        bp.sync_records(site)
        self.assertFalse((site / "bin").exists())
        self.assertEqual((info / "RECORD").read_bytes(),
                         b'tool/__init__.py,sha256=abc,10\r\n"tool/a,b.txt",sha256=jkl,0\r\n'
                         b"tool-1.0.dist-info/RECORD,,\r\n")

    def test_the_zip_is_the_same_whatever_the_files_times(self):
        folder = self.root / "Unminute-0.5.0"
        for rel in ("b.txt", "a/c.txt", "a/B.dll"):
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(rel.encode())
        first, second = self.root / "one.zip", self.root / "two.zip"
        bp.write_zip(folder, first, 1760000000)
        later = time.time() + 3600
        for q in folder.rglob("*"):
            os.utime(q, (later, later))
        bp.write_zip(folder, second, 1760000000)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as z:
            names = z.namelist()
            self.assertEqual(names, sorted(names))
            self.assertEqual({i.date_time for i in z.infolist()},
                             {(2025, 10, 9, 8, 53, 20)})            # 1760000000 in UTC
            self.assertTrue(all(n.startswith("Unminute-0.5.0/") for n in names))

    def test_the_time_stamp_comes_from_source_date_epoch_or_the_commit(self):
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1760000000"}):
            self.assertEqual(bp.source_date_epoch(), 1760000000)
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "5"}):
            self.assertEqual(bp.source_date_epoch(), bp.ZIP_EPOCH_MIN)     # zip's earliest
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "tomorrow"}), \
                self.assertRaisesRegex(SystemExit, "whole seconds"):
            bp.source_date_epoch()
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": ""}), \
                mock.patch.object(bp, "git", return_value="1750000000") as asked:
            self.assertEqual(bp.source_date_epoch("f" * 40), 1750000000)
        asked.assert_called_once_with("log", "-1", "--format=%ct", "f" * 40)  # that commit
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": ""}), \
                mock.patch.object(bp, "git", return_value=None), \
                self.assertRaisesRegex(SystemExit, "needs SOURCE_DATE_EPOCH"):
            bp.source_date_epoch()

    def test_a_release_builds_only_the_tagged_commit_from_a_clean_tree(self):
        def fake(status="", head="f" * 40, tag="f" * 40, flags="H asr_engines.py\nH VERSION"):
            answers = {("status", "--porcelain", "--untracked-files=all"): status,
                       ("ls-files", "-v"): flags, ("rev-parse", "HEAD"): head,
                       ("rev-parse", "--verify", "--quiet", "refs/tags/v0.5.0^{commit}"): tag}
            # Any other question (a branch named v0.5.0, say) has no answer.
            return lambda *args: answers.get(args)
        stored = mock.patch.object(bp, "committed", return_value=b"0.5.0\r\n")
        with mock.patch.object(bp, "git", fake()), stored:
            self.assertEqual(bp.release_guard("0.5.0"), "f" * 40)          # the commit
        for bad in (dict(status="?? workspace/stray.py"), dict(status=" M asr_engines.py"),
                    dict(tag=None), dict(head="0" * 40),
                    dict(flags="h requirements.txt"),                       # assume-unchanged
                    dict(flags="S packaging/binary-allowlist.json")):       # skip-worktree
            with mock.patch.object(bp, "git", fake(**bad)), stored, \
                    self.assertRaises(SystemExit):
                bp.release_guard("0.5.0")
        with mock.patch.object(bp, "git", fake()), \
                mock.patch.object(bp, "committed", return_value=b"0.4.9\n"), \
                self.assertRaisesRegex(SystemExit, "VERSION in commit"):
            bp.release_guard("0.5.0")                   # the commit's VERSION is the one
        with mock.patch.object(bp, "git", return_value=None), self.assertRaises(SystemExit):
            bp.release_guard("0.5.0")                                       # no git: refused

    def test_a_development_build_says_whether_the_tree_was_clean(self):
        for status, flags, clean in (("", "H a.py", True), (" M x.py", "H a.py", False),
                                     ("", "h a.py", False),          # hidden edit
                                     (None, None, None)):
            answers = {("status", "--porcelain", "--untracked-files=all"): status,
                       ("ls-files", "-v"): flags}
            with mock.patch.object(bp, "git", lambda *args, answers=answers: answers[args]):
                self.assertIs(bp.clean_tree(), clean)

    def test_the_build_script_and_vendored_wheel_must_match_the_commit(self):
        repo, sha = self._repo()
        with self._patched(repo):
            bp.same_as_commit(sha, "LICENSE")
            (repo / "LICENSE").write_bytes(b"MIT\r\n")             # line endings only
            bp.same_as_commit(sha, "LICENSE")
            with self.assertRaisesRegex(SystemExit, "workspace/app.py differs from commit"):
                bp.same_as_commit(sha, "workspace/app.py")          # changed in the folder

    def test_nothing_is_written_if_head_moves_during_the_build(self):
        with mock.patch.object(bp, "release_guard", return_value="f" * 40):
            bp.unchanged_since("0.5.0", "f" * 40)
        with mock.patch.object(bp, "release_guard", return_value="e" * 40), \
                self.assertRaisesRegex(SystemExit, "nothing was written"):
            bp.unchanged_since("0.5.0", "f" * 40)
        # build() runs the check, and the script and wheel check, before it writes.
        source = Path(bp.__file__).read_text(encoding="utf-8")
        body = source[source.index("def build("):source.index("def release_version")]
        self.assertLess(body.index("unchanged_since(version, sha)"), body.index("write_zip("))
        self.assertLess(body.index('same_as_commit(sha, path)'), body.index("install_wheels("))
        self.assertIn('for path in ("tools/build_package.py",', body)

    def test_a_release_cannot_rewrite_the_allowlist(self):
        argv = ["build_package.py", "--release", "--update-binary-allowlist"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(bp, "build") as build, \
                mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            bp.main()
        build.assert_not_called()
        with self.assertRaisesRegex(SystemExit, "cannot rewrite"), \
                mock.patch.object(bp, "check_host"):
            bp.build("0.5.0", self.root, self.root, release=True, update_allowlist=True)

    def test_crlf_files_match_the_gitattributes(self):
        rules = (bp.ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
        crlf = {line.split()[0].removeprefix("*") for line in rules if "eol=crlf" in line}
        self.assertEqual(crlf, set(bp.CRLF_SUFFIXES))

    def _repo(self):
        """A committed fixture repo with a clean git setup (no user or system
        settings), then local state git status cannot see or that git would
        apply on export: a Windows checkout setting, info/attributes and a
        smudge filter."""
        repo = self.root / "repo"
        files = {"workspace/app.py": "a = 1\nb = 2\n", "workspace/ui/index.html": "<p>\n",
                 "workspace/ui/trial.html": "trial\n", "workspace/consent.py": "c = 1\n",
                 "LICENSE": "MIT\n", "packaging/install.ps1": "Write-Host 1\nWrite-Host 2\n",
                 ".gitattributes": "* text=auto\n*.ps1 text eol=crlf\n", ".gitignore": "*.log\n"}
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_bytes(text.encode())
        empty = self.root / "gitconfig"
        empty.write_text("")
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(empty),
                                           "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        git(repo, "init", "-q")
        git(repo, "config", "core.autocrlf", "true")
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-q", "-m", "fixture")
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
        info = repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("workspace/consent.py export-ignore\n*.py eol=crlf\n"
                                         "*.html filter=swap\n")
        git(repo, "config", "filter.swap.smudge", "echo swapped")
        (repo / "workspace" / "app.py").write_bytes(b"a = 'changed, not committed'\n")
        (repo / "workspace" / "notes.log").write_text("ignored file")
        (repo / "workspace" / "stray.py").write_text("untracked file")
        return repo, sha

    def _patched(self, repo):
        stack = mock.patch.multiple(bp, ROOT=repo, APP_FILES=["LICENSE"],
                                    PACKAGED_SCRIPTS=("install.ps1",))
        return stack

    def test_a_release_packages_the_commit_as_stored(self):
        repo, sha = self._repo()
        # HEAD moves on after the commit was checked: the export keeps to it.
        (repo / "workspace" / "later.py").write_text("x = 2\n")
        git(repo, "add", "workspace/later.py")
        git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-q", "-m", "later")
        app = self.root / "out" / "Unminute-0.5.0" / "app"
        with self._patched(repo):
            bp.export_app(app, sha)
        shipped = sorted(p.relative_to(app.parent).as_posix()
                         for p in app.parent.rglob("*") if p.is_file())
        self.assertEqual(shipped, ["app/LICENSE", "app/workspace/app.py",
                                   "app/workspace/consent.py", "app/workspace/ui/index.html",
                                   "install.ps1"])
        self.assertEqual((app / "workspace" / "app.py").read_bytes(), b"a = 1\nb = 2\n")
        self.assertEqual((app / "workspace" / "ui" / "index.html").read_bytes(), b"<p>\n")
        self.assertEqual((app.parent / "install.ps1").read_bytes(),
                         b"Write-Host 1\r\nWrite-Host 2\r\n")

    def test_a_release_refuses_a_link_or_a_missing_path_in_the_commit(self):
        repo, sha = self._repo()
        with self._patched(repo):
            with self.assertRaisesRegex(SystemExit, "README.md is not in commit"), \
                    mock.patch.object(bp, "APP_FILES", ["README.md"]):
                bp.export_app(self.root / "a" / "app", sha)
            blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                  input=b"../../etc", capture_output=True, check=True)
            git(repo, "update-index", "--add", "--cacheinfo",
                f"120000,{blob.stdout.decode().strip()},workspace/link")
            git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                "commit", "-q", "-m", "link")
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            with self.assertRaisesRegex(SystemExit, "workspace/link .*not a regular file"):
                bp.export_app(self.root / "b" / "app", head)

    def test_a_release_refuses_a_name_windows_reads_as_another_path(self):
        # git keeps "..\..\x" as one name; Windows would write it two folders up.
        repo, _ = self._repo()

        def run(*args, data=None):
            return subprocess.run(["git", "-C", str(repo), *args], input=data, check=True,
                                  capture_output=True).stdout.decode().strip()
        blob = run("hash-object", "-w", "--stdin", data=b"PWNED")
        for bad in ("..\\..\\runtime\\sitecustomize.py", "C:x.py", "a\x01.py", "NUL.py",
                    "com1", "trail.", "space ", "CONOUT$", "lpt\u00b9.txt"):
            workspace = run("mktree", data=f"100644 blob {blob}\t{bad}\n".encode())
            packaging = run("mktree", data=f"100644 blob {blob}\tinstall.ps1\n".encode())
            root = run("mktree", data=(f"100644 blob {blob}\tLICENSE\n"
                                       f"040000 tree {packaging}\tpackaging\n"
                                       f"040000 tree {workspace}\tworkspace\n").encode())
            sha = run("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                      "commit-tree", root, "-m", "crafted")
            with self._patched(repo), self.assertRaisesRegex(SystemExit, "Unexpected path"):
                bp.export_app(self.root / "c" / "Unminute-0.5.0" / "app", sha)
            self.assertFalse((self.root / "c" / "runtime").exists())

    def test_a_release_must_run_isolated(self):
        with mock.patch.object(bp.sys, "flags", mock.Mock(isolated=0)), \
                mock.patch.object(bp, "check_host"), \
                mock.patch.object(bp, "release_guard") as guard, \
                self.assertRaisesRegex(SystemExit, "python -I"):
            bp.build("0.5.0", self.root, self.root, release=True)
        guard.assert_not_called()

    def test_a_development_build_leaves_ignored_files_out(self):
        repo, _ = self._repo()
        (repo / "packaging" / "install.cmd").write_text("x")
        app = self.root / "dev" / "Unminute-0.5.0" / "app"
        with self._patched(repo):
            bp.copy_app(app)
        shipped = sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file())
        self.assertEqual(shipped, ["LICENSE", "workspace/app.py", "workspace/consent.py",
                                   "workspace/stray.py", "workspace/ui/index.html"])
        self.assertEqual((app / "workspace" / "app.py").read_bytes(),
                         b"a = 'changed, not committed'\n")        # as it is in the folder

class PrunedBuildTests(unittest.TestCase):
    """#111 B2: what the app never uses is left out; every module ships
    precompiled; the package is held to its size and file-count budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _site(self, base="a"):
        runtime = self.root / base / "runtime"
        site = runtime / bp.SITE
        files = ["jinja2/__init__.py", "jinja2/tests.py",             # a module, not a test suite
                 "numpy/__init__.py", "numpy/testing/__init__.py",    # used at run time
                 "numpy/tests/test_core.py", "scipy/io/test/data.mat",
                 "numpy/_core/include/numpy/ndarrayobject.h", "torch/lib/torch_cpu.lib",
                 "torch/lib/torch_cpu.dll", "torch/bin/protoc.exe", "torch/__init__.pyi",
                 "av/filter/loudnorm_impl.h", "cffi/_cffi_include.h",
                 "clr_loader/ffi/dlls/x86/ClrLoader.dll", "clr_loader/ffi/dlls/amd64/ClrLoader.dll",
                 "webview/lib/runtimes/win-x86/native/WebView2Loader.dll",
                 "webview/lib/runtimes/win-arm64/native/WebView2Loader.dll",
                 "webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
                 "webview/lib/WebBrowserInterop.x86.dll",
                 "torch-9.dist-info/licenses/third_party/x/test/LICENSE",  # licences stay
                 "torch-9.dist-info/licenses/third_party/y/include/z.h",
                 "pyannote/audio/data/config.yaml", "certifi/cacert.pem",   # run-time data
                 "matplotlib/mpl-data/matplotlibrc", "onnx_asr/models.json"]
        for rel in files:
            (site / rel).parent.mkdir(parents=True, exist_ok=True)
            (site / rel).write_bytes(b"x")
        (runtime / "libtommath.dll").write_bytes(b"x")
        for dist, owned in (("grpcio-1.0", ["grpc/__init__.py", "grpc/_cython/cygrpc.pyd"]),
                            ("pyannoteai_sdk-0.4.0", ["pyannoteai/sdk/__init__.py"]),
                            ("setuptools-83.0.0", ["setuptools/__init__.py",
                                                   "_distutils_hack/__init__.py",
                                                   "distutils-precedence.pth"])):
            info = site / f"{dist}.dist-info"
            info.mkdir()
            for rel in owned:
                (site / rel).parent.mkdir(parents=True, exist_ok=True)
                (site / rel).write_bytes(b"x")
            (info / "RECORD").write_text("".join(f"{r},sha256=x,1\n" for r in owned)
                                         + f"{dist}.dist-info/RECORD,,\n")
        return runtime, site

    def test_what_the_app_never_uses_is_left_out(self):
        runtime, site = self._site()
        removed = bp.prune(site, runtime)
        self.assertEqual(removed, {"grpcio", "pyannoteai-sdk", "setuptools"})
        left = sorted(p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file())
        self.assertEqual(left, [
            "certifi/cacert.pem",
            "clr_loader/ffi/dlls/amd64/ClrLoader.dll", "jinja2/__init__.py", "jinja2/tests.py",
            "matplotlib/mpl-data/matplotlibrc",
            "numpy/__init__.py", "numpy/testing/__init__.py", "onnx_asr/models.json",
            "pyannote/audio/data/config.yaml",
            "torch-9.dist-info/licenses/third_party/x/test/LICENSE",
            "torch-9.dist-info/licenses/third_party/y/include/z.h",
            "torch/lib/torch_cpu.dll",
            # pywebview looks every architecture's folder up when imported (#111 B2 review).
            "webview/lib/runtimes/win-arm64/native/WebView2Loader.dll",
            "webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
            "webview/lib/runtimes/win-x86/native/WebView2Loader.dll"])
        self.assertFalse((runtime / "libtommath.dll").exists())
        self.assertFalse((site / "torch" / "bin").exists())        # empty folders go too

    def test_a_stale_prune_entry_stops_the_build(self):
        runtime, site = self._site()
        (site / "torch" / "bin" / "protoc.exe").unlink()
        with self.assertRaisesRegex(SystemExit, "torch/bin/protoc.exe is no longer"):
            bp.prune(site, runtime)
        runtime, site = self._site("b")
        shutil.rmtree(site / "grpcio-1.0.dist-info")
        with self.assertRaisesRegex(SystemExit, "No dist-info folder for grpcio"):
            bp.prune(site, runtime)
        runtime, site = self._site("c")
        (runtime / "libtommath.dll").unlink()
        with self.assertRaisesRegex(SystemExit, "libtommath.dll is no longer"):
            bp.prune(site, runtime)

    def test_record_paths_outside_the_folder_are_never_followed(self):
        for rel in ("../evil.dll", "a/../../evil.dll", "/etc/evil", "\\\\server\\x",
                    "C:/Windows/evil.dll", "C:evil.dll", ""):
            self.assertFalse(bp.inside(rel), rel)
        for rel in ("pkg/mod.py", "pkg-1.0.dist-info/RECORD", "a..b/c.py"):
            self.assertTrue(bp.inside(rel), rel)
        runtime, site = self._site()
        outside = self.root / "a" / "keep-me.txt"
        outside.write_text("not the package's")
        info = site / "setuptools-83.0.0.dist-info"
        (info / "RECORD").write_text("../keep-me.txt,,\nsetuptools/__init__.py,,\n"
                                     + str(outside).replace("\\", "/") + ",,\n")
        bp.prune(site, runtime)
        self.assertTrue(outside.exists())

    def test_every_module_is_precompiled_the_same_way_twice(self):
        builds = []
        for n in (1, 2):
            folder = self.root / f"build{n}" / "Unminute-0.5.0"
            for rel, text in ((f"runtime/{bp.SITE}/pkg/__init__.py", "X = {'a', 'b', 'c'}\n"),
                              ("app/workspace/mod.py", "def f():\n    return 1\n"),
                              ("runtime/sitecustomize.py", "import site\n")):
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                (folder / rel).write_text(text)
            bp.precompile(folder, python=sys.executable)
            pycs = sorted(p for p in folder.rglob("*.pyc"))
            self.assertEqual(len(pycs), 3)
            builds.append({p.relative_to(folder).as_posix(): p.read_bytes() for p in pycs})
        self.assertEqual(builds[0], builds[1])               # same bytes in a different folder
        for name, data in builds[0].items():
            self.assertEqual(int.from_bytes(data[4:8], "little"), 1, name)  # unchecked-hash
            self.assertNotIn(b"build1", data)                # no build folder in the bytes
            self.assertNotIn(str(self.root).encode(), data)

    def test_precompiling_is_one_process_with_a_fixed_hash_seed(self):
        # Parallel workers (compileall -j) made two release builds differ in a
        # .pyc's marshal flags; the hash seed orders sets of constants.
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs["env"]))
            return subprocess.CompletedProcess(cmd, 0, "0 1\n", "")
        with mock.patch.object(bp.subprocess, "run", fake_run), \
                mock.patch.dict(os.environ, {"PYTHONHASHSEED": "random", "PYTHONOPTIMIZE": "2"}):
            bp.precompile(self.root / "Unminute-0.5.0")
        self.assertEqual(len(calls), 4)       # the seed check, packages, app, sitecustomize
        self.assertIn("hash_randomization", calls[0][0][-1])
        for n, (cmd, env) in enumerate(calls):
            self.assertTrue(cmd[0].endswith("runtime\\python.exe")
                            or cmd[0].endswith("runtime/python.exe"), cmd[0])   # its own Python
            self.assertEqual(cmd[1:4], ["-S", "-s", "-B"])     # -s: no user site-packages
            self.assertEqual(env["PYTHONHASHSEED"], "0")
            self.assertNotIn("PYTHONOPTIMIZE", env)          # no other PYTHON* setting
            if n:                                           # the compile runs
                self.assertIn("-f", cmd)                    # rewrite, never keep a stale .pyc
                self.assertFalse([a for a in cmd if a.startswith(("-j", "--workers"))], cmd)
                self.assertEqual(cmd[cmd.index("--invalidation-mode") + 1], "unchecked-hash")

    def test_a_compile_run_without_a_fixed_seed_stops_the_build(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "1 1\n", "")   # hash randomised
        with mock.patch.object(bp.subprocess, "run", fake_run), \
                self.assertRaisesRegex(SystemExit, "fixed hash seed"):
            bp.precompile(self.root / "Unminute-0.5.0")

    def test_a_missing_or_leaky_pyc_stops_the_build(self):
        folder = self.root / "Unminute-0.5.0"
        mod = folder / "app" / "workspace" / "mod.py"
        mod.parent.mkdir(parents=True)
        mod.write_text("X = 1\n")
        (folder / "runtime" / bp.SITE).mkdir(parents=True)
        with self.assertRaisesRegex(SystemExit, "no .pyc: app/workspace/mod.py"):
            bp.check_precompiled(folder)
        pyc = mod.parent / "__pycache__" / f"mod.cpython-{bp.PYTHON_TAG}.pyc"
        pyc.parent.mkdir()
        pyc.write_bytes(b"\0\0\0\0" + (3).to_bytes(4, "little") + bytes(8))
        with self.assertRaisesRegex(SystemExit, "not unchecked-hash"):
            bp.check_precompiled(folder)
        pyc.write_bytes(b"\0\0\0\0" + (1).to_bytes(4, "little") + bytes(8)
                        + str(folder).encode())
        with self.assertRaisesRegex(SystemExit, "build path inside"):
            bp.check_precompiled(folder)
        pyc.write_bytes(b"\0\0\0\0" + (1).to_bytes(4, "little") + bytes(8))
        bp.check_precompiled(folder)                       # now it passes
        (folder / "runtime" / "sitecustomize.py").write_text("import site\n")
        with self.assertRaisesRegex(SystemExit, "no .pyc: runtime/sitecustomize.py"):
            bp.check_precompiled(folder)

    def test_the_build_runs_each_step_in_order(self):
        source = Path(bp.__file__).read_text(encoding="utf-8")
        body = source[source.index("def build("):source.index("def release_version")]
        order = ["removed = prune(site, runtime)", "sync_records(site)",
                 "precompile(folder)", "sbom(", "write_zip(", "enforce_budget("]
        at = [body.index(step) for step in order]
        self.assertEqual(at, sorted(at), order)
        self.assertIn("build_tools(), removed)", body)       # the SBOM records what went
        self.assertIn('source_date_epoch(sha or "HEAD")', body)    # the commit's own time
        self.assertIn("budgets = committed(sha, BUDGETS)", body)   # a release obeys the commit
        pre = source[source.index("def precompile("):source.index("def check_precompiled(")]
        self.assertIn("\n    check_precompiled(folder)\n", pre)    # the leak and flag guard runs

    def test_the_self_test_imports_the_window_module_pruning_could_break(self):
        main = (bp.ROOT / "workspace" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn('"webview.platforms.edgechromium"', main)

    def test_an_over_budget_zip_is_deleted_and_the_build_stops(self):
        zpath = self.root / "Unminute-0.5.0.zip"
        zpath.write_bytes(b"x" * 200_000)
        budgets = json.dumps({"package_zip_mb": {"value": 0.1}, "package_files": {"value": 50}})
        with self.assertRaisesRegex(SystemExit, "package_zip_mb: 0.2 is over"):
            bp.enforce_budget(zpath, 10, budgets)
        self.assertFalse(zpath.exists())
        zpath.write_bytes(b"x" * 50_000)
        self.assertEqual(bp.enforce_budget(zpath, 50, budgets),
                         {"package_zip_mb": 0.1, "package_files": 50})
        self.assertTrue(zpath.exists())

    def test_a_strip_warning_stops_the_build(self):
        # compileall only warns when -s does not match, and keeps the build path.
        def fake_run(cmd, **kwargs):
            out = "0 1\n" if "-c" in cmd else "not a valid prefix\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        with mock.patch.object(bp.subprocess, "run", fake_run), \
                self.assertRaisesRegex(SystemExit, "Precompiling"):
            bp.precompile(self.root / "Unminute-0.5.0")

    def test_the_bill_of_materials_records_removed_packages(self):
        bom = bp.sbom([], "Unminute", "0.5.0", "c" * 40, 1760000000, "d" * 64,
                      removed={"setuptools", "grpcio"})
        self.assertIn({"name": "unminute:removed", "value": "grpcio, setuptools"},
                      bom["metadata"]["component"]["properties"])

    def test_the_package_is_held_to_its_budget(self):
        budgets = json.dumps({"package_zip_mb": {"value": 100.0}, "package_files": {"value": 50}})
        self.assertEqual(bp.package_budget(99_949_999, 50, budgets),
                         ([], {"package_zip_mb": 99.9, "package_files": 50}))
        problems, _ = bp.package_budget(100_100_000, 51, budgets)
        self.assertEqual(len(problems), 2)
        self.assertIn("package_zip_mb: 100.1 is over its budget of 100.0", problems[0])
        committed = json.loads((bp.ROOT / bp.BUDGETS).read_text(encoding="utf-8"))
        for key in ("package_zip_mb", "package_files"):
            self.assertGreaterEqual(committed[key]["value"], committed[key]["measured"])


if __name__ == "__main__":
    unittest.main()
