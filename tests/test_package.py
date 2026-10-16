# Package build helpers (#11). No network: the runtime download and wheel
# installation are exercised by tools/build_package.py on a build machine.
#   python -m unittest discover -s tests -v

import json
import sys
import tempfile
import unittest
from pathlib import Path

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

if __name__ == "__main__":
    unittest.main()
