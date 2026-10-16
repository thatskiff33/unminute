# Package build helpers (#11). No network: the runtime download and wheel
# installation are exercised by tools/build_package.py on a build machine.
#   python -m unittest discover -s tests -v

import json
import os
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

    def test_deep_licences_are_flattened_and_indexed(self):
        site = self.root / bp.SITE
        deep = site / "torch-9.dist-info" / "licenses" / ("x" * 60) / ("y" * 60) / "LICENSE"
        deep.parent.mkdir(parents=True)
        deep.write_text("licence text")
        shallow = site / "numpy-2.dist-info" / "licenses" / "LICENSE.txt"
        shallow.parent.mkdir(parents=True)
        shallow.write_text("kept in place")
        bp.flatten_licences(site)
        flat = site / "torch-9.dist-info" / "licenses" / "flat"
        self.assertEqual((flat / "000-LICENSE").read_text(), "licence text")
        self.assertIn(("x" * 60), (flat / "INDEX.txt").read_text())
        self.assertFalse((site / "torch-9.dist-info" / "licenses" / ("x" * 60)).exists())
        self.assertTrue(shallow.exists())

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
                         "MIT OR Apache-2.0")
        self.assertEqual(bp.licence_of({"classifier": [
            "License :: OSI Approved :: BSD License", "Programming Language :: Python"]}),
            "BSD License")
        self.assertEqual(bp.licence_of({"license": "MIT"}), "MIT")
        self.assertEqual(bp.licence_of({"license": "Copyright ...\n" * 40}),
                         "see the licence texts")       # a whole licence text: not a name
        self.assertEqual(bp.licence_of({}), "see the licence texts")

    def test_the_bill_of_materials_names_each_wheel_and_its_hash(self):
        installed = [report_entry("NumPy", "2.4.6", "numpy-2.4.6-cp312-cp312-win_amd64.whl",
                                  "a" * 64, license_expression="BSD-3-Clause"),
                     report_entry("av", "17.1.0", "av-17.1.0-cp312-cp312-win_amd64.whl",
                                  "b" * 64, license="BSD")]
        bom = bp.sbom(installed, "Unminute", "0.5.0", "c" * 40, 1760000000, "d" * 64)
        self.assertEqual(bom["bomFormat"], "CycloneDX")
        self.assertEqual(bom["metadata"]["component"]["version"], "0.5.0")
        names = [c["name"] for c in bom["components"]]
        self.assertEqual(names, [bp.RUNTIME_NAME, "av", "NumPy"])          # runtime, then sorted
        numpy = bom["components"][2]
        self.assertEqual(numpy["purl"], "pkg:pypi/numpy@2.4.6")
        self.assertEqual(numpy["hashes"], [{"alg": "SHA-256", "content": "a" * 64}])
        self.assertEqual(numpy["properties"][0]["value"],
                         "numpy-2.4.6-cp312-cp312-win_amd64.whl")         # no build paths
        self.assertNotIn("C:/build", json.dumps(bom))
        again = bp.sbom(list(reversed(installed)), "Unminute", "0.5.0", "c" * 40, 1760000000,
                        "d" * 64)
        self.assertEqual(json.dumps(bom), json.dumps(again))             # reproducible
        other = bp.sbom(installed, "Unminute", "0.5.0", "e" * 40, 1760000000, "d" * 64)
        self.assertNotEqual(bom["serialNumber"], other["serialNumber"])

    def test_notices_list_every_component_and_its_licence_texts(self):
        site = self.root / bp.SITE

        def wheel(dist, *files):
            info = site / f"{dist}.dist-info"
            info.mkdir(parents=True)
            (info / "RECORD").write_text("".join(f"{f},sha256=x,1\n" for f in files)
                                         + f"{dist}.dist-info/RECORD,,\n")
        wheel("numpy-2.4.6", "numpy/__init__.py", "numpy-2.4.6.dist-info/licenses/LICENSE.txt")
        wheel("onnxruntime-1.30.0", "onnxruntime/__init__.py", "onnxruntime/LICENSE",
              "onnxruntime/ThirdPartyNotices.txt")                  # beside the code
        wheel("av-17.1.0", "av/__init__.py", "av.libs/avcodec-62-abc.dll",
              "av.libs/libmp3lame-0-def.dll", "av-17.1.0.dist-info/licenses/LICENSE.txt")
        wheel("bare-1.0", "bare/__init__.py")
        text = bp.notices([report_entry("numpy", "2.4.6", "n.whl", "a" * 64,
                                        license_expression="BSD-3-Clause"),
                           report_entry("onnxruntime", "1.30.0", "o.whl", "c" * 64,
                                        license="MIT License"),
                           report_entry("av", "17.1.0", "a.whl", "d" * 64,
                                        license_expression="BSD-3-Clause"),
                           report_entry("bare", "1.0", "b.whl", "b" * 64, license="MIT")],
                          site, "0.5.0")
        self.assertIn("numpy 2.4.6\n  Licence: BSD-3-Clause", text)
        self.assertIn(f"runtime/{bp.SITE}/numpy-2.4.6.dist-info/licenses/LICENSE.txt", text)
        self.assertIn(f"runtime/{bp.SITE}/onnxruntime/ThirdPartyNotices.txt", text)
        self.assertIn("avcodec-62-abc.dll, libmp3lame-0-def.dll", text)
        self.assertIn("bare 1.0\n  Licence: MIT\n  Texts: none shipped in the wheel", text)
        self.assertIn("PSF-2.0", text)

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

    def test_binaries_must_match_the_allowlist_exactly(self):
        folder = self.root / "pkg"
        for rel in ("runtime/python.exe", "runtime/lib/x/_core.pyd", "runtime/lib/x/data.txt"):
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(b"x")
        found = bp.binaries(folder)
        self.assertEqual(found, ["runtime/lib/x/_core.pyd", "runtime/python.exe"])
        allow = self.root / "allow.json"
        self.assertEqual(bp.check_binaries(found, allow, update=True), [])
        self.assertEqual(bp.check_binaries(found, allow), [])
        (folder / "runtime" / "lib" / "x" / "new.dll").write_bytes(b"x")
        (folder / "runtime" / "python.exe").unlink()
        problems = bp.check_binaries(bp.binaries(folder), allow)
        self.assertEqual(problems, ["not allowlisted: runtime/lib/x/new.dll",
                                    "allowlisted but missing: runtime/python.exe"])

    def test_launchers_and_their_record_lines_go(self):
        # Launchers embed the build machine's path, so their hashes in RECORD
        # differ between builds; everything outside the package folder goes.
        site = self.root / bp.SITE
        (site / "bin").mkdir(parents=True)
        (site / "bin" / "tool.exe").write_bytes(b"MZ")
        info = site / "tool-1.0.dist-info"
        info.mkdir()
        (info / "RECORD").write_bytes(b"tool/__init__.py,sha256=abc,10\r\n"
                                      b"../../bin/tool.exe,sha256=xyz,2\r\n"
                                      b"../../share/man/tool.1,sha256=def,3\r\n"
                                      b"tool-1.0.dist-info/RECORD,,\r\n")
        bp.drop_launchers(site)
        self.assertFalse((site / "bin").exists())
        self.assertEqual((info / "RECORD").read_bytes(),
                         b"tool/__init__.py,sha256=abc,10\r\ntool-1.0.dist-info/RECORD,,\r\n")

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
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": ""}), \
                mock.patch.object(bp, "git", return_value="1750000000"):
            self.assertEqual(bp.source_date_epoch(), 1750000000)
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": ""}), \
                mock.patch.object(bp, "git", return_value=None), \
                self.assertRaises(SystemExit):
            bp.source_date_epoch()

    def test_a_release_builds_only_the_tagged_commit_from_a_clean_tree(self):
        def fake(status="", head="f" * 40, tag="f" * 40):
            answers = {("status", "--porcelain", "--untracked-files=all"): status,
                       ("rev-parse", "HEAD"): head, ("rev-parse", "v0.5.0^{commit}"): tag}
            return lambda *args: answers[args]
        with mock.patch.object(bp, "git", fake()):
            bp.release_guard("0.5.0")                                       # passes
        for bad in (dict(status="?? workspace/stray.py"), dict(status=" M asr_engines.py"),
                    dict(tag=None), dict(head="0" * 40)):
            with mock.patch.object(bp, "git", fake(**bad)), self.assertRaises(SystemExit):
                bp.release_guard("0.5.0")
        with mock.patch.object(bp, "git", return_value=None), self.assertRaises(SystemExit):
            bp.release_guard("0.5.0")                                       # no git: refused


if __name__ == "__main__":
    unittest.main()
