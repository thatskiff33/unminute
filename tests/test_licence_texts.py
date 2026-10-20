# Licence texts collected from upstream (#176): the manifest's rules, the build's
# checks of the committed texts, the notices, and the developer-only collector.
# No test here reaches the network: the collector's fetches are faked.

import copy
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_package as bp  # noqa: E402
import collect_licences as cl  # noqa: E402

COMMIT = "a" * 40
TEXT = b"Permission is hereby granted, free of charge, to any person...\n"


def entry(**changes):
    """A valid manifest entry for a wheel's own texts, with changes applied."""
    c = {"id": "demo", "name": "Demo", "version": "1.0", "licence": "MIT (metadata)",
         "wheel": "demo", "wheel_version": "1.0", "covers": [],
         "source": {"repository": "https://github.com/example/demo", "ref": "v1.0",
                    "commit": COMMIT},
         "files": [{"path": "demo-1.0/LICENSE",
                    "url": f"https://raw.githubusercontent.com/example/demo/{COMMIT}/LICENSE",
                    "sha256": hashlib.sha256(TEXT).hexdigest()}]}
    c.update(changes)
    return c


def manifest(*entries):
    return json.dumps({"schema": 1, "components": list(entries)})


def problems_of(*entries):
    """The problems licence_manifest reports, as one string ("" when valid)."""
    try:
        bp.licence_manifest(manifest(*entries))
    except SystemExit as exc:
        return str(exc.code)
    return ""


class ManifestRuleTests(unittest.TestCase):
    def test_a_valid_entry_passes(self):
        self.assertEqual(problems_of(entry()), "")
        lib = entry(id="lib", covers=["demo.libs/libfoo-1-*.dll", "demo/bar.dll"],
                    files=[{"path": "lib-1.0/COPYING", "sha256": "b" * 64,
                            "archive": {"url": "https://files.pythonhosted.org/p/x.whl",
                                        "sha256": "c" * 64, "member": "x/COPYING"}}])
        own = {k: v for k, v in entry(id="own", covers=["demo/core.dll"], files=[],
                                      in_wheel=True).items() if k != "source"}
        self.assertEqual(problems_of(entry(), lib, own), "")

    def test_fields_must_be_known_and_complete(self):
        self.assertIn("unknown field extra", problems_of(entry(extra=1)))
        broken = entry()
        del broken["licence"]
        self.assertIn("missing field licence", problems_of(broken))
        self.assertIn("id used twice", problems_of(entry(), entry()))

    def test_a_github_url_names_the_commit_not_a_tag(self):
        tagged = entry(files=[{**entry()["files"][0], "url":
                               "https://raw.githubusercontent.com/example/demo/v1.0/LICENSE"}])
        self.assertIn("must name the source commit", problems_of(tagged))

    def test_urls_are_https_on_collector_hosts_only(self):
        for url in ("http://raw.githubusercontent.com/example/demo/x/LICENSE",
                    "https://example.com/LICENSE",
                    "https://github.com/example/demo/blob/main/LICENSE",   # HTML, not the text
                    "https://user@gitlab.com/x/LICENSE",
                    "https://gitlab.com/x/LICENSE?ref=main"):
            with self.subTest(url=url):
                bad = entry(files=[{**entry()["files"][0], "url": url}])
                self.assertIn("the URL", problems_of(bad))

    def test_covers_patterns_are_anchored_dll_paths(self):
        for pattern in ("av.libs/*.dll", "*.dll", "av.libs/ab*.dll", "../x/libfoo.dll",
                        "av.libs/libfoo-*-*.dll", "av.libs/libfoo.so", "av.libs\\libfoo.dll",
                        "/abs/libfoo.dll", "av.libs/**/libfoo.dll", "av.libs/libfoo?.dll"):
            with self.subTest(pattern=pattern):
                self.assertIn("not an anchored .dll path",
                              problems_of(entry(covers=[pattern])))

    def test_text_paths_are_one_level_under_the_component_folder(self):
        for path in ("other-1.0/LICENSE", "demo-1.0/sub/LICENSE", "demo-1.0/../LICENSE",
                     "LICENSE", "demo-1.0/.hidden", "demo-1.0/" + "x" * 61):
            with self.subTest(path=path):
                bad = entry(files=[{**entry()["files"][0], "path": path}])
                self.assertIn("must be demo-1.0/<file name>", problems_of(bad))

    def test_two_texts_that_differ_only_by_case_are_refused(self):
        first = entry()["files"][0]
        twice = entry(files=[first, {**first, "path": "demo-1.0/license"}])
        self.assertIn("listed twice", problems_of(twice))

    def test_archive_members_and_hashes_are_checked(self):
        for archive, why in (({"url": "https://files.pythonhosted.org/p/x.whl",
                               "sha256": "c" * 64, "member": "../LICENSE"}, "plain relative"),
                             ({"url": "https://files.pythonhosted.org/p/x.whl",
                               "sha256": "short", "member": "LICENSE"}, "published SHA-256"),
                             ({"url": "https://files.pythonhosted.org/p/x.whl",
                               "member": "LICENSE"}, "must hold url, sha256 and member")):
            with self.subTest(why=why):
                bad = entry(files=[{"path": "demo-1.0/LICENSE", "sha256": "b" * 64,
                                    "archive": archive}])
                self.assertIn(why, problems_of(bad))

    def test_in_wheel_entries_name_dlls_and_carry_no_files(self):
        self.assertIn("in_wheel entry", problems_of(entry(in_wheel=True)))
        self.assertIn("in_wheel entry", problems_of(entry(in_wheel=True, covers=["d/x1.dll"])))
        self.assertIn("files must list at least one text", problems_of(entry(files=[])))


class CommittedTextsTests(unittest.TestCase):
    def test_the_committed_texts_match_their_manifest_and_the_lock(self):
        # Every text is listed with the hash of its bytes, and was collected for
        # the version the lock pins (#176). A re-lock that moves one fails here.
        components, files = bp.licence_texts()
        self.assertTrue(files)
        locked = {}
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            if "==" in line and not line.startswith((" ", "#")):
                name, version = line.split("==")
                locked[bp.canonical(name)] = version.split()[0]
        self.assertEqual(bp.licence_version_problems(components, locked), [])

    def test_git_keeps_the_texts_byte_for_byte(self):
        # A line-ending conversion would change a text's SHA-256.
        out = subprocess.run(["git", "check-attr", "text", "--",
                              "packaging/licences/demo-1.0/LICENSE"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        self.assertTrue(out.strip().endswith("text: unset"), out)

    def test_the_build_never_imports_the_collector(self):
        source = (ROOT / "tools" / "build_package.py").read_text(encoding="utf-8")
        self.assertNotIn("collect_licences", source.replace("tools/collect_licences.py", ""))


class FileCheckTests(unittest.TestCase):
    def setUp(self):
        self.components = bp.licence_manifest(manifest(entry()))

    def test_unlisted_missing_and_changed_texts_are_reported(self):
        good = {"demo-1.0/LICENSE": TEXT}
        self.assertEqual(bp.licence_file_problems(self.components, good), [])
        self.assertEqual(bp.licence_file_problems(self.components, {}),
                         ["missing: demo-1.0/LICENSE"])
        self.assertEqual(bp.licence_file_problems(self.components,
                                                  {**good, "demo-1.0/EXTRA": b"x"}),
                         ["not in the manifest: demo-1.0/EXTRA"])
        self.assertEqual(bp.licence_file_problems(self.components,
                                                  {"demo-1.0/LICENSE": TEXT + b" "}),
                         ["SHA-256 differs from the manifest: demo-1.0/LICENSE"])

    def test_a_release_reads_the_texts_from_the_commit(self):
        tree = {f"{bp.LICENCES_DIR}/manifest.json": manifest(entry()).encode(),
                f"{bp.LICENCES_DIR}/demo-1.0/LICENSE": TEXT}
        with mock.patch.object(bp, "committed_tree", return_value=tree) as read:
            components, files = bp.licence_texts("f" * 40)
        read.assert_called_once_with("f" * 40, [bp.LICENCES_DIR])
        self.assertEqual(files, {"demo-1.0/LICENSE": TEXT})
        self.assertEqual(components[0]["id"], "demo")
        tree[f"{bp.LICENCES_DIR}/demo-1.0/PLANTED"] = b"x"
        with mock.patch.object(bp, "committed_tree", return_value=tree):
            with self.assertRaisesRegex(SystemExit, "not in the manifest: demo-1.0/PLANTED"):
                bp.licence_texts("f" * 40)

    def test_texts_are_for_the_installed_version(self):
        self.assertEqual(bp.licence_version_problems(self.components, {"demo": "1.0"}), [])
        moved = bp.licence_version_problems(self.components, {"demo": "1.1"})
        self.assertEqual(len(moved), 1)
        self.assertIn("checked for demo 1.0, and the package has 1.1", moved[0])
        self.assertIn("not in the package", bp.licence_version_problems(self.components, {})[0])

    def test_copied_into_the_package_licences_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            bp.copy_licences(Path(tmp), {"demo-1.0/LICENSE": TEXT})
            self.assertEqual((Path(tmp) / "licences" / "demo-1.0" / "LICENSE").read_bytes(),
                             TEXT)


class CoverageTests(unittest.TestCase):
    def test_a_star_stays_inside_the_file_name_and_stale_patterns_are_reported(self):
        lib = entry(id="lib", covers=["demo.libs/libfoo-1-*.dll", "demo/gone.dll"],
                    files=[{**entry()["files"][0], "path": "lib-1.0/LICENSE"}])
        components = bp.licence_manifest(manifest(lib))
        libraries = {"demo": ["demo.libs/libfoo-1-abc.dll", "demo.libs/libfoo-1-x/y.dll",
                              "demo.libs/libbar.dll"],
                     "other": ["demo/gone.dll"]}                     # another wheel's DLL
        covered, stale = bp.licence_coverage(components, libraries)
        self.assertEqual(sorted(covered), ["demo.libs/libfoo-1-abc.dll"])
        self.assertEqual(stale, ["lib: demo/gone.dll matches no DLL in demo"])


class NoticesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "Unminute-0.5.0"
        self.site = self.folder / "runtime" / bp.SITE
        (self.folder / "runtime").mkdir(parents=True)
        (self.folder / "runtime" / "LICENSE.txt").write_text("PSF")
        for dist, files in (("bare-1.0", ["bare/__init__.py"]),
                            ("demo-1.0", ["demo/__init__.py", "demo/core.dll",
                                          "demo.libs/libfoo-1-abc.dll",
                                          "demo-1.0.dist-info/licenses/LICENSE"])):
            info = self.site / f"{dist}.dist-info"
            info.mkdir(parents=True, exist_ok=True)
            for f in files:
                (self.site / f).parent.mkdir(parents=True, exist_ok=True)
                (self.site / f).write_text("x")
            (info / "RECORD").write_text("".join(f"{f},,\n" for f in files)
                                         + f"{dist}.dist-info/RECORD,,\n")
        self.installed = [
            {"metadata": {"name": "bare", "version": "1.0", "license": "MIT"},
             "download_info": {"url": "https://files.example/bare-1.0-py3-none-any.whl",
                               "archive_info": {"hashes": {"sha256": "a" * 64}}}},
            {"metadata": {"name": "demo", "version": "1.0", "license": "BSD"},
             "download_info": {"url": "https://files.example/demo-1.0-py3-none-any.whl",
                               "archive_info": {"hashes": {"sha256": "b" * 64}}}}]
        lib = f"runtime/{bp.SITE}"
        self.signed = {f"{lib}/demo/core.dll": {"signer": ""},
                       f"{lib}/demo.libs/libfoo-1-abc.dll": {"signer": ""}}
        own = entry(id="bare", name="Bare", wheel="bare",
                    files=[{**entry()["files"][0], "path": "bare-1.0/LICENSE"}])
        foo = entry(id="libfoo", name="libfoo", version="2.3", covers=["demo.libs/libfoo-1-*.dll"],
                    files=[{**entry()["files"][0], "path": "libfoo-2.3/COPYING"}])
        core = {k: v for k, v in entry(id="demo-core", name="Demo core", covers=["demo/core.dll"],
                                       files=[], in_wheel=True).items() if k != "source"}
        self.components = bp.licence_manifest(manifest(own, foo, core))
        bp.copy_licences(self.folder, {"bare-1.0/LICENSE": TEXT, "libfoo-2.3/COPYING": TEXT})

    def notices(self, components=None, **kw):
        return bp.notices(self.installed, self.site, self.folder, "0.5.0", self.signed,
                          self.components if components is None else components, **kw)

    def test_collected_texts_are_named_for_wheels_and_dlls(self):
        text = self.notices()
        self.assertIn("bare 1.0\n  Licence: MIT\n  Texts: licences/bare-1.0/LICENSE\n", text)
        self.assertIn(f"    runtime/{bp.SITE}/demo.libs/libfoo-1-abc.dll (unsigned)\n"
                      "      libfoo 2.3 (MIT (metadata)): licences/libfoo-2.3/COPYING", text)
        self.assertIn("      Demo core 1.0 (MIT (metadata)): the demo texts above", text)
        # Nothing is missing, so the notices no longer say texts are to follow.
        self.assertNotIn("to follow", text)
        self.assertIn("collected in\nlicences/ from each project's own release", text)

    def test_without_texts_the_gaps_are_stated_and_can_stop_the_build(self):
        text = self.notices(components=[])
        self.assertIn("bare 1.0\n  Licence: MIT\n  Texts: none shipped in the wheel", text)
        self.assertIn("included yet (to follow, #176)", text)
        with self.assertRaisesRegex(SystemExit, "bare 1.0: the wheel ships no licence text"):
            self.notices(components=[], required=True)
        self.notices(required=True)             # complete: passes

    def test_a_stale_pattern_stops_the_build(self):
        stale = copy.deepcopy(self.components)
        stale[1]["covers"] = ["demo.libs/libgone-*.dll"]
        with self.assertRaisesRegex(SystemExit, "libfoo: demo.libs/libgone-.*matches no DLL"):
            self.notices(components=stale)

    def test_a_named_text_must_be_in_the_package(self):
        (self.folder / "licences" / "libfoo-2.3" / "COPYING").unlink()
        with self.assertRaisesRegex(SystemExit, "licences/libfoo-2.3/COPYING"):
            self.notices()


class FakeResponse(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


def fake_web(pages):
    """An open_url for the collector that serves `pages` ({url: bytes})."""
    calls = []

    def open_url(request, timeout):
        calls.append(request.full_url)
        if request.full_url not in pages:
            raise OSError("not found")
        return FakeResponse(pages[request.full_url], request.full_url)
    open_url.calls = calls
    return open_url


def zip_of(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def tar_of(files, link=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link)
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            t.addfile(info)
    return buf.getvalue()


class CollectorTests(unittest.TestCase):
    URL = f"https://raw.githubusercontent.com/example/demo/{COMMIT}/LICENSE"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def write_manifest(self, *entries):
        (self.folder / "manifest.json").write_text(manifest(*entries), encoding="utf-8")

    def test_fetch_refuses_other_hosts_and_plain_http(self):
        web = fake_web({})
        for url in ("https://example.com/LICENSE", "http://gitlab.com/x/LICENSE"):
            with self.subTest(url=url), self.assertRaisesRegex(cl.CollectError, "collector host"):
                cl.fetch(url, 100, web)
        self.assertEqual(web.calls, [])                 # refused before any request

    def test_a_redirect_off_the_collector_hosts_is_refused(self):
        handler = cl.CheckedRedirects()
        request = mock.Mock(full_url=self.URL)
        with self.assertRaisesRegex(cl.CollectError, "redirects off the collector hosts"):
            handler.redirect_request(request, None, 302, "Found", {},
                                     "https://evil.example/LICENSE")
        # And a response that ended somewhere else (any other route) is refused too.
        def moved(request, timeout):
            return FakeResponse(TEXT, "https://evil.example/LICENSE")
        with self.assertRaisesRegex(cl.CollectError, "off the collector hosts"):
            cl.fetch(self.URL, 1000, moved)

    def test_size_limits_hold(self):
        with self.assertRaisesRegex(cl.CollectError, "larger than 10 bytes"):
            cl.fetch(self.URL, 10, fake_web({self.URL: TEXT}))

    def test_html_binary_and_empty_downloads_are_not_texts(self):
        for data, why in ((b"\n<!DOCTYPE html><html>...", "HTML page"),
                          (b"MZ\x90\x00\x03", "NUL bytes"), (b"  \n", "empty")):
            with self.subTest(why=why), self.assertRaisesRegex(cl.CollectError, why):
                cl.collect_file({"url": self.URL, "path": "demo-1.0/LICENSE"},
                                fake_web({self.URL: data}))

    def test_archive_members_are_read_in_memory_after_the_hash_check(self):
        url = "https://files.pythonhosted.org/p/demo.whl"
        for blob in (zip_of({"demo/LICENSE": TEXT}), tar_of({"demo/LICENSE": TEXT})):
            spec = {"path": "demo-1.0/LICENSE",
                    "archive": {"url": url, "sha256": hashlib.sha256(blob).hexdigest(),
                                "member": "demo/LICENSE"}}
            self.assertEqual(cl.collect_file(spec, fake_web({url: blob})), TEXT)
            spec["archive"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(cl.CollectError, "not the published"):
                cl.collect_file(spec, fake_web({url: blob}))
        self.assertEqual(list(self.folder.iterdir()), [])           # nothing extracted

    def test_only_a_single_regular_member_is_read(self):
        blob = tar_of({}, link="demo/LICENSE")
        with self.assertRaisesRegex(cl.CollectError, "no single regular file"):
            cl.member(blob, "demo/LICENSE")
        with self.assertRaisesRegex(cl.CollectError, "no single file"):
            cl.member(zip_of({"demo/LICENSE.txt": TEXT}), "demo/LICENSE")
        with self.assertRaisesRegex(cl.CollectError, "neither a zip nor a tar"):
            cl.member(b"plain bytes", "demo/LICENSE")

    def test_collect_writes_the_texts_and_their_hashes(self):
        first = entry()
        first["files"][0]["sha256"] = cl.PLACEHOLDER
        self.write_manifest(first)
        lines = []
        changed = cl.collect(self.folder, ["demo"], fake_web({self.URL: TEXT}), lines.append)
        self.assertEqual(changed, 1)
        self.assertEqual((self.folder / "demo-1.0" / "LICENSE").read_bytes(), TEXT)
        saved = json.loads((self.folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["components"][0]["files"][0]["sha256"],
                         hashlib.sha256(TEXT).hexdigest())
        self.assertIn("(new)", lines[0])
        # Collected again unchanged; a changed upstream text is called out.
        self.assertEqual(cl.collect(self.folder, ["demo"], fake_web({self.URL: TEXT}),
                                    lines.append), 0)
        cl.collect(self.folder, ["demo"], fake_web({self.URL: TEXT + b"x\n"}), lines.append)
        self.assertIn("(CHANGED)", lines[-1])

    def test_one_failure_writes_nothing(self):
        other = entry(id="other", files=[{**entry()["files"][0], "path": "other-1.0/LICENSE",
                                          "url": self.URL.replace("LICENSE", "COPYING")}])
        self.write_manifest(entry(), other)
        before = (self.folder / "manifest.json").read_bytes()
        with self.assertRaises(cl.CollectError):
            cl.collect(self.folder, [], fake_web({self.URL: TEXT}), lambda _: None)
        self.assertEqual((self.folder / "manifest.json").read_bytes(), before)
        self.assertFalse((self.folder / "demo-1.0").exists())

    def test_verify_compares_upstream_the_manifest_and_the_file(self):
        self.write_manifest(entry())
        (self.folder / "demo-1.0").mkdir()
        (self.folder / "demo-1.0" / "LICENSE").write_bytes(TEXT)
        out = []
        self.assertEqual(cl.verify(self.folder, [], fake_web({self.URL: TEXT}), out.append), 0)
        self.assertEqual(cl.verify(self.folder, [], fake_web({self.URL: TEXT + b"x"}),
                                   out.append), 1)
        self.assertIn("DIFFERS", out[-1])
        self.assertEqual((self.folder / "demo-1.0" / "LICENSE").read_bytes(), TEXT)

    def test_unknown_ids_are_refused(self):
        self.write_manifest(entry())
        with self.assertRaisesRegex(cl.CollectError, "Not in the manifest: nope"):
            cl.collect(self.folder, ["nope"], fake_web({}), lambda _: None)


if __name__ == "__main__":
    unittest.main()
