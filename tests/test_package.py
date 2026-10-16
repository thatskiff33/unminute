# Package build helpers (#11). No network: the runtime download and wheel
# installation are exercised by tools/build_package.py on a build machine.
#   python -m unittest discover -s tests -v

import json
import os
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
        (rt / "python312._pth").write_text("python312.zip\n.\n#import site\n")
        bp.write_pth(rt)
        lines = (rt / "python312._pth").read_text().splitlines()
        self.assertEqual(lines, ["python312.zip", ".", bp.SITE, "..\\app", "import site"])
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
        m = bp.model_manifest()
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
        with mock.patch.object(bp.subprocess, "run", fake_run):
            self.assertEqual(bp.install_wheels(self.root / "lib", self.root / "cache",
                                               self.root / "report.json"), [])
        download, install = calls
        for cmd in calls:
            self.assertEqual(cmd[1:5], ["-m", "pip", "--isolated", "--disable-pip-version-check"])
            for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:"):
                self.assertIn(flag, cmd)
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
        info = site / "tool-1.0.dist-info"
        info.mkdir()
        (info / "RECORD").write_bytes(b"tool/__init__.py,sha256=abc,10\r\n"
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
                mock.patch.object(bp, "git", return_value="1750000000"):
            self.assertEqual(bp.source_date_epoch(), 1750000000)
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": ""}), \
                mock.patch.object(bp, "git", return_value=None), \
                self.assertRaisesRegex(SystemExit, "needs SOURCE_DATE_EPOCH"):
            bp.source_date_epoch()

    def test_a_release_builds_only_the_tagged_commit_from_a_clean_tree(self):
        def fake(status="", head="f" * 40, tag="f" * 40):
            answers = {("status", "--porcelain", "--untracked-files=all"): status,
                       ("rev-parse", "HEAD"): head,
                       ("rev-parse", "--verify", "--quiet", "refs/tags/v0.5.0^{commit}"): tag}
            # Any other question (a branch named v0.5.0, say) has no answer.
            return lambda *args: answers.get(args)
        with mock.patch.object(bp, "git", fake()):
            bp.release_guard("0.5.0")                                       # passes
        for bad in (dict(status="?? workspace/stray.py"), dict(status=" M asr_engines.py"),
                    dict(tag=None), dict(head="0" * 40)):
            with mock.patch.object(bp, "git", fake(**bad)), self.assertRaises(SystemExit):
                bp.release_guard("0.5.0")
        with mock.patch.object(bp, "git", return_value=None), self.assertRaises(SystemExit):
            bp.release_guard("0.5.0")                                       # no git: refused

    def test_a_development_build_says_whether_the_tree_was_clean(self):
        for status, clean in (("", True), (" M x.py", False), (None, None)):
            with mock.patch.object(bp, "git", return_value=status):
                self.assertIs(bp.clean_tree(), clean)

    def test_a_release_cannot_rewrite_the_allowlist(self):
        argv = ["build_package.py", "--release", "--update-binary-allowlist"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(bp, "build") as build, \
                mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            bp.main()
        build.assert_not_called()
        with self.assertRaisesRegex(SystemExit, "cannot rewrite"), \
                mock.patch.object(bp, "check_host"):
            bp.build("0.5.0", self.root, self.root, release=True, update_allowlist=True)

    def test_a_release_packages_the_committed_tree_with_fixed_line_endings(self):
        repo = self.root / "repo"
        files = {"workspace/app.py": "a = 1\nb = 2\n", "workspace/ui/index.html": "<p>\n",
                 "workspace/ui/trial.html": "trial\n", "LICENSE": "MIT\n",
                 "packaging/install.ps1": "Write-Host 1\nWrite-Host 2\n",
                 ".gitattributes": "* text=auto\n*.ps1 text eol=crlf\n",
                 ".gitignore": "*.log\n"}
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_bytes(text.encode())
        # A clean git setup (no user or system settings), then a Windows-style
        # checkout setting that would otherwise turn LF into CRLF.
        empty = self.root / "gitconfig"
        empty.write_text("")
        env = {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"}
        with mock.patch.dict(os.environ, env):
            git(repo, "init", "-q")
            git(repo, "config", "core.autocrlf", "true")
            git(repo, "add", "-A")
            git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                "commit", "-q", "-m", "fixture")
            (repo / "workspace" / "app.py").write_bytes(b"a = 'changed, not committed'\n")
            (repo / "workspace" / "notes.log").write_text("ignored file")
            (repo / "workspace" / "stray.py").write_text("untracked file")
            app = self.root / "out" / "Unminute-0.5.0" / "app"
            with mock.patch.object(bp, "ROOT", repo), \
                    mock.patch.object(bp, "APP_FILES", ["LICENSE"]), \
                    mock.patch.object(bp, "PACKAGED_SCRIPTS", ("install.ps1",)):
                bp.export_app(app)
        shipped = sorted(p.relative_to(app.parent).as_posix()
                         for p in app.parent.rglob("*") if p.is_file())
        self.assertEqual(shipped, ["app/LICENSE", "app/workspace/app.py",
                                   "app/workspace/ui/index.html", "install.ps1"])
        self.assertEqual((app / "workspace" / "app.py").read_bytes(), b"a = 1\nb = 2\n")
        self.assertEqual((app.parent / "install.ps1").read_bytes(),
                         b"Write-Host 1\r\nWrite-Host 2\r\n")


if __name__ == "__main__":
    unittest.main()
