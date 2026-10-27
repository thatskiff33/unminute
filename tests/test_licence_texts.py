# Licence texts collected from upstream (#176): the manifest's rules, the build's
# checks of the committed texts, the notices, and the developer-only collector.
# No test here reaches the network: the collector's fetches are faked.

import contextlib
import copy
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.parse
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

    def test_a_malformed_or_explicit_port_is_a_manifest_problem(self):
        # #282 review: urlsplit's port raised ValueError (a traceback, not a
        # manifest error), and port 0 passed because it is falsy.
        for url in ("https://ffmpeg.org:x/releases/LICENSE", "https://ffmpeg.org:0/LICENSE",
                    "https://ffmpeg.org:443/LICENSE", "https://ffmpeg.org:/LICENSE",
                    "https://[ffmpeg.org/LICENSE"):
            with self.subTest(url=url):
                bad = entry(files=[{**entry()["files"][0], "url": url}])
                self.assertIn("the URL", problems_of(bad))
                archive = entry(files=[{"path": "demo-1.0/LICENSE", "sha256": "b" * 64,
                                        "archive": {"url": url, "sha256": "c" * 64,
                                                    "member": "LICENSE"}}])
                self.assertIn("the archive URL", problems_of(archive))
                self.assertFalse(cl.allowed_url(url))

    def test_exact_names_cover_files_a_pattern_cannot_name(self):
        # Owner decision, 22 Oct 2026 (#111, #176): covers may list exact file
        # names as well as patterns. An exact name has no wildcard, so it names
        # one file; it may be short or sit in a folder that starts with a dot.
        own = {k: v for k, v in entry(id="own", covers=["torch/lib/uv.dll",
                                                        "sklearn/.libs/msvcp140.dll",
                                                        "sklearn/.libs/vcomp140.dll"],
                                      files=[], in_wheel=True).items() if k != "source"}
        self.assertEqual(problems_of(own), "")
        self.assertEqual(bp.licence_pattern("sklearn/.libs/vcomp140.dll").pattern,
                         r"sklearn/\.libs/vcomp140\.dll")
        for exact in ("sklearn/.libs/*.dll", "a/.libs/v*.dll", ".libs/x.dll/../y.dll",
                      "sklearn/../x.dll", "sklearn/./x.dll", "sklearn//x.dll",
                      "sklearn/.libs/.dll", "C:/x/uv.dll", "sklearn/.libs/uv.DLL "):
            with self.subTest(exact=exact):
                self.assertIn("not an anchored .dll path", problems_of(entry(covers=[exact])))

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

    def test_linked_into_names_exact_libraries(self):
        # #176 D1: code compiled into a wheel's .pyd or .dll is recorded against
        # each library by its exact path; a wildcard could hide a new library.
        self.assertEqual(problems_of(entry(linked_into=["demo/_core.cp314-win_amd64.pyd",
                                                        "demo/lib/core.dll"])), "")
        for bad in (["demo/*.pyd"], ["demo/core.so"], ["../demo/core.pyd"], ["demo\\core.pyd"],
                    ["/demo/core.pyd"], ["demo/./core.pyd"], [1], "demo/core.pyd"):
            with self.subTest(bad=bad):
                self.assertIn("linked_into", problems_of(entry(linked_into=bad)))
        self.assertIn("at least one library", problems_of(entry(linked_into=[])))
        self.assertIn("listed twice", problems_of(entry(linked_into=["d/a.pyd", "d/a.pyd"])))

    def test_an_in_wheel_entry_may_record_compiled_in_code(self):
        # FreeType in matplotlib: the wheel's own texts carry it, so the entry
        # records where it is compiled in and has no files.
        own = {k: v for k, v in entry(id="ft", linked_into=["demo/ft.pyd"], files=[],
                                      in_wheel=True).items() if k != "source"}
        self.assertEqual(problems_of(own), "")
        self.assertIn("in_wheel entry", problems_of({**own, "linked_into": ["demo/ft.pyd"],
                                                     "files": entry()["files"]}))

    def test_a_crate_names_its_sbom_purl_and_its_library(self):
        crate = entry(linked_into=["demo/_rust.pyd"], sbom="pkg:cargo/serde@1.0.228")
        self.assertEqual(problems_of(crate), "")
        for sbom in ("pkg:cargo/serde", "pkg:pypi/serde@1.0", "pkg:cargo/serde@1.0?x=y", 7):
            with self.subTest(sbom=sbom):
                self.assertIn("sbom must be",
                              problems_of(entry(linked_into=["d/r.pyd"], sbom=sbom)))
        self.assertIn("sbom must be", problems_of(entry(sbom="pkg:cargo/serde@1.0.228")))
        twice = entry(id="other", linked_into=["demo/_rust.pyd"], sbom="pkg:cargo/serde@1.0.228",
                      files=[{**entry()["files"][0], "path": "other-1.0/LICENSE"}])
        self.assertIn("pkg:cargo/serde@1.0.228 is recorded twice", problems_of(crate, twice))

    def test_a_bundle_is_a_short_name_on_an_entry_with_texts(self):
        self.assertEqual(problems_of(entry(bundle="demo-1.0-rust-crates")), "")
        for bundle in ("Demo", "../x", "", "a" * 61, 3):
            with self.subTest(bundle=bundle):
                self.assertIn("bundle must be", problems_of(entry(bundle=bundle)))
        own = {k: v for k, v in entry(covers=["d/x1.dll"], files=[], in_wheel=True,
                                      bundle="b").items() if k != "source"}
        self.assertIn("bundle must be", problems_of(own))
        # The bundle's file may not take a component folder's name.
        folder = entry(version="1.0.txt", files=[{**entry()["files"][0],
                                                   "path": "demo-1.0.txt/LICENSE"}])
        clash = entry(id="other", bundle="demo-1.0",
                      files=[{**entry()["files"][0], "path": "other-1.0/LICENSE"}])
        self.assertIn("is a component folder's name", problems_of(folder, clash))


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

    def test_the_collector_hosts_are_the_ones_the_manifest_uses(self):
        # Least privilege (#282 review): no host the collector may reach that no
        # committed text comes from. Adding a source adds its host here.
        components, _ = bp.licence_texts()
        used = {urllib.parse.urlsplit(f.get("url") or f["archive"]["url"]).hostname
                for c in components for f in c["files"]}
        self.assertEqual(set(bp.LICENCE_HOSTS), used)

    def test_every_allowlisted_dll_has_a_licence_text(self):
        # A release stops on a bundled DLL with no text (#176). The binary
        # allowlist names every DLL the package holds, so a gap shows here
        # before a build does.
        components, _ = bp.licence_texts()
        prefix = f"runtime/{bp.SITE}/"
        dlls = [e["path"][len(prefix):] for e in
                json.loads(bp.ALLOWLIST.read_text(encoding="utf-8"))["binaries"]
                if e["path"].startswith(prefix) and e["path"].lower().endswith(".dll")]
        self.assertTrue(dlls)
        uncovered = [d for d in dlls if not any(bp.licence_pattern(p).fullmatch(d)
                                                for c in components for p in c["covers"])]
        self.assertEqual(uncovered, [])

    def test_the_microsoft_runtime_in_numpy_and_pandas_has_cpythons_conditions(self):
        # Owner decision, 24 Oct 2026 (#176 L8): the msvcp140 copies in
        # numpy.libs and pandas.libs are covered by CPython's "Additional
        # Conditions for this Windows binary build" (PC/crtlicense.txt at tag
        # v3.14.8), from a host the collector already uses.
        components, files = bp.licence_texts()
        dll = "msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"
        url = ("https://raw.githubusercontent.com/python/cpython/"
               "8e6e75d9102e39bed2a2b279203a396741180f12/PC/crtlicense.txt")
        for wheel in ("numpy", "pandas"):
            with self.subTest(wheel=wheel):
                found = [c for c in components if c["wheel"] == wheel
                         and f"{wheel}.libs/{dll}" in c["covers"]]
                self.assertEqual(len(found), 1)
                c = found[0]
                self.assertEqual(c["source"], {"repository": "https://github.com/python/cpython",
                                               "ref": "v3.14.8",
                                               "commit": url.split("/")[5]})
                self.assertEqual([f["url"] for f in c["files"]], [url])
                text = files[c["files"][0]["path"]]
                self.assertIn(b"Additional Conditions for this Windows binary build", text)

    def test_the_compiled_in_components_have_their_texts(self):
        # #176 D1 (owner decision, 19 Oct 2026): code compiled into other libraries,
        # each checked against the locked wheels (27 Oct 2026). The build checks each
        # library is in its wheel; this keeps the inventory from shrinking unseen.
        components, _ = bp.licence_texts()
        inside = {}
        for c in components:
            for lib in c.get("linked_into", []):
                inside.setdefault((bp.canonical(c["wheel"]), lib), set()).add(c["id"])
        expected = {
            ("cryptography", "cryptography/hazmat/bindings/_rust.pyd"): {"openssl"},
            ("torch", "torch/lib/torch_cpu.dll"): {"onemkl-torch"},
            ("ctranslate2", "ctranslate2/ctranslate2.dll"): {
                "onemkl-ct2", "onednn", "spdlog", "cpu-features", "cccl", "cuda-runtime",
                "avx-mathfun", "simd-utils"},
            ("ctranslate2", "ctranslate2/_ext.cp314-win_amd64.pyd"): {"pybind11-ct2"},
            ("matplotlib", "matplotlib/ft2font.cp314-win_amd64.pyd"): {
                "freetype-matplotlib", "harfbuzz-matplotlib", "libraqm-matplotlib",
                "sheenbidi-matplotlib", "agg-matplotlib", "pybind11-matplotlib"},
            ("pyyaml", "yaml/_yaml.cp314-win_amd64.pyd"): {"libyaml"},
            ("cffi", "_cffi_backend.cp314-win_amd64.pyd"): {"libffi-cffi"},
            ("contourpy", "contourpy/_contourpy.cp314-win_amd64.pyd"): {"pybind11-contourpy"},
        }
        for key, ids in expected.items():
            with self.subTest(library=key):
                self.assertLessEqual(ids, inside.get(key, set()))
        # SciPy's wheel leaves out the texts of what it bundles (LICENSES_bundled.txt).
        scipy = {c["id"] for c in components if c["wheel"] == "scipy"}
        self.assertLessEqual({"scipy-bundled", "scipy-boost-math", "scipy-highs", "scipy-xsf",
                              "scipy-unuran", "scipy-cobyqa"}, scipy)

    def test_each_wheels_rust_crates_share_one_bundle(self):
        # The crates' texts go into one file per wheel, so several hundred texts
        # add a handful of files to the package (its package_files budget).
        components, files = bp.licence_texts()
        bundles = {}
        for c in components:
            if "sbom" in c:
                bundles.setdefault(bp.canonical(c["wheel"]), set()).add(c["bundle"])
                self.assertEqual(c["linked_into"], [c["linked_into"][0]])
        self.assertEqual(sorted(bundles), ["cryptography", "hf_xet", "safetensors",
                                           "tokenizers"])
        for wheel, names in bundles.items():
            with self.subTest(wheel=wheel):
                self.assertEqual(len(names), 1)
        crate_texts = sum(len(c["files"]) for c in components if c.get("bundle"))
        self.assertGreater(crate_texts, 500)
        loose = sum(len(c["files"]) for c in components if not c.get("bundle"))
        with tempfile.TemporaryDirectory() as tmp:
            bp.copy_licences(Path(tmp), files, components)
            in_package = sum(1 for p in Path(tmp).rglob("*") if p.is_file())
        self.assertEqual(in_package, loose + 4)

    def test_coverage_is_required_for_a_release(self):
        # Owner decision, 22 Oct 2026 (#111, #176): a release fails if any
        # bundled wheel or native file lacks its licence text. A development
        # build lists the gaps in its notices and on the console instead.
        self.assertIs(bp.LICENCE_COVERAGE_REQUIRED, True)
        self.assertTrue(bp.licence_coverage_required(release=True))
        self.assertFalse(bp.licence_coverage_required(release=False))

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

    def test_a_library_named_in_linked_into_must_be_in_its_wheel(self):
        inside = entry(id="inside", linked_into=["demo/_rust.pyd", "demo/gone.pyd"],
                       files=[{**entry()["files"][0], "path": "inside-1.0/LICENSE"}])
        components = bp.licence_manifest(manifest(inside))
        linked, stale = bp.licence_linked(components, {"demo": ["demo/_rust.pyd"],
                                                       "other": ["demo/gone.pyd"]})
        self.assertEqual([c["id"] for c in linked["demo"]], ["inside"])
        self.assertEqual(stale, ["inside: demo/gone.pyd is not in demo"])


def cyclonedx(*components):
    return json.dumps({"bomFormat": "CycloneDX", "components": list(components)}).encode()


class SbomTests(unittest.TestCase):
    # #176 D1: the Rust crates a wheel's own SBOM lists are compiled into its library.

    def test_the_crates_are_the_sboms_registry_crates_in_use(self):
        data = cyclonedx(
            {"name": "serde", "purl": "pkg:cargo/serde@1.0.228", "scope": "required"},
            {"name": "tokio", "purl": "pkg:cargo/tokio@1.47.1"},                  # no scope
            {"name": "cc", "purl": "pkg:cargo/cc@1.4.0", "scope": "excluded"},      # build tool
            {"name": "own", "purl": "pkg:cargo/own@0.1.0?download_url=file://own"},  # own code
            {"name": "openssl", "purl": "pkg:generic/openssl@4.0.3"},              # not a crate
            {"name": "nested", "purl": "pkg:cargo/outer@2.0.0#sub",
             "components": [{"name": "inner", "purl": "pkg:cargo/inner@3.0.0?x=1"}]},
            {"name": "no purl"})
        self.assertEqual(bp.sbom_crates(data), ["pkg:cargo/inner@3.0.0", "pkg:cargo/outer@2.0.0",
                                                "pkg:cargo/serde@1.0.228",
                                                "pkg:cargo/tokio@1.47.1"])
        self.assertEqual(bp.sbom_crates(b"{}"), [])
        with self.assertRaisesRegex(SystemExit, "SBOM is not JSON"):
            bp.sbom_crates(b"not json")

    def test_every_crate_needs_an_entry_and_every_entry_a_crate(self):
        crate = entry(linked_into=["demo/_rust.pyd"], sbom="pkg:cargo/serde@1.0.228")
        components = bp.licence_manifest(manifest(crate))
        crates = {"demo": ["pkg:cargo/serde@1.0.228", "pkg:cargo/tokio@1.47.1"], "bare": []}
        gaps, stale = bp.licence_sbom_problems(components, crates)
        self.assertEqual(gaps, {"demo": ["pkg:cargo/tokio@1.47.1"]})
        self.assertEqual(stale, [])
        gaps, stale = bp.licence_sbom_problems(components, {"demo": ["pkg:cargo/tokio@1.47.1"]})
        self.assertEqual(stale, ["demo: pkg:cargo/serde@1.0.228 is not in demo's SBOM"])
        self.assertEqual(bp.licence_sbom_problems(components, {}),
                         ({}, ["demo: pkg:cargo/serde@1.0.228 is not in demo's SBOM"]))


class BundleTests(unittest.TestCase):
    def setUp(self):
        two = {"path": "demo-1.0/NOTICE", "sha256": hashlib.sha256(b"no newline").hexdigest(),
               "archive": {"url": "https://files.pythonhosted.org/p/x.whl", "sha256": "c" * 64,
                           "member": "x/NOTICE"}}
        first = entry(bundle="demo-crates", linked_into=["demo/_rust.pyd"],
                      files=entry()["files"] + [two])
        second = entry(id="second", name="Second", bundle="demo-crates",
                       linked_into=["demo/_rust.pyd"],
                       files=[{**entry()["files"][0], "path": "second-1.0/LICENSE"}])
        loose = entry(id="loose", files=[{**entry()["files"][0], "path": "loose-1.0/LICENSE"}])
        self.components = bp.licence_manifest(manifest(first, second, loose))
        self.files = {"demo-1.0/LICENSE": TEXT, "demo-1.0/NOTICE": b"no newline",
                      "second-1.0/LICENSE": TEXT, "loose-1.0/LICENSE": TEXT}

    def test_a_bundle_holds_each_text_unchanged_after_its_header(self):
        text = bp.licence_bundles(self.components, self.files)["demo-crates.txt"]
        self.assertTrue(text.startswith(b"Licence texts collected from each project's own"))
        self.assertIn(b"Demo 1.0 (MIT (metadata))\nFile: demo-1.0/LICENSE\nSource: "
                      b"https://raw.githubusercontent.com/example/demo/" + COMMIT.encode()
                      + b"/LICENSE\nSHA-256: " + hashlib.sha256(TEXT).hexdigest().encode(), text)
        self.assertIn(b"=" * 78 + b"\n" + TEXT, text)
        self.assertIn(b"Source: x/NOTICE in https://files.pythonhosted.org/p/x.whl", text)
        self.assertIn(b"no newline\n", text)                  # a newline before the next header
        # In manifest order, and only the bundle's own components.
        self.assertLess(text.index(b"File: demo-1.0/NOTICE"), text.index(b"File: second-1.0/"))
        self.assertNotIn(b"loose-1.0", text)
        self.assertEqual(bp.licence_package_paths(self.components[1]),
                         ["licences/demo-crates.txt"])
        self.assertEqual(bp.licence_package_paths(self.components[2]),
                         ["licences/loose-1.0/LICENSE"])

    def test_bundled_texts_are_one_file_in_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            bp.copy_licences(Path(tmp), self.files, self.components)
            found = sorted(p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*")
                           if p.is_file())
        self.assertEqual(found, ["licences/demo-crates.txt", "licences/loose-1.0/LICENSE"])


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
                                          "demo/.libs/uv.dll", "demo.libs/libfoo-1-abc.dll",
                                          "demo/_rust.pyd",
                                          "demo-1.0.dist-info/sboms/demo.cdx.json",
                                          "demo-1.0.dist-info/licenses/LICENSE"])):
            info = self.site / f"{dist}.dist-info"
            info.mkdir(parents=True, exist_ok=True)
            for f in files:
                (self.site / f).parent.mkdir(parents=True, exist_ok=True)
                (self.site / f).write_text("x")
            (info / "RECORD").write_text("".join(f"{f},,\n" for f in files)
                                         + f"{dist}.dist-info/RECORD,,\n")
        # demo's own SBOM: one Rust crate compiled into demo/_rust.pyd, a build tool
        # and the project's own crate (neither of those needs an entry).
        (self.site / "demo-1.0.dist-info/sboms/demo.cdx.json").write_bytes(cyclonedx(
            {"name": "serde", "purl": "pkg:cargo/serde@1.0.228", "scope": "required"},
            {"name": "cc", "purl": "pkg:cargo/cc@1.4.0", "scope": "excluded"},
            {"name": "demo", "purl": "pkg:cargo/demo@1.0.0?download_url=file://demo"}))
        self.installed = [
            {"metadata": {"name": "bare", "version": "1.0", "license": "MIT"},
             "download_info": {"url": "https://files.example/bare-1.0-py3-none-any.whl",
                               "archive_info": {"hashes": {"sha256": "a" * 64}}}},
            {"metadata": {"name": "demo", "version": "1.0", "license": "BSD"},
             "download_info": {"url": "https://files.example/demo-1.0-py3-none-any.whl",
                               "archive_info": {"hashes": {"sha256": "b" * 64}}}}]
        lib = f"runtime/{bp.SITE}"
        self.signed = {f"{lib}/demo/core.dll": {"signer": ""},
                       f"{lib}/demo/.libs/uv.dll": {"signer": ""},
                       f"{lib}/demo.libs/libfoo-1-abc.dll": {"signer": ""}}
        own = entry(id="bare", name="Bare", wheel="bare",
                    files=[{**entry()["files"][0], "path": "bare-1.0/LICENSE"}])
        foo = entry(id="libfoo", name="libfoo", version="2.3", covers=["demo.libs/libfoo-1-*.dll"],
                    files=[{**entry()["files"][0], "path": "libfoo-2.3/COPYING"}])
        core = {k: v for k, v in entry(id="demo-core", name="Demo core",
                                       covers=["demo/core.dll", "demo/.libs/uv.dll"],
                                       files=[], in_wheel=True).items() if k != "source"}
        # #176 D1: a crate from the SBOM (bundled), and code the wheel's own texts cover.
        serde = entry(id="demo.serde", name="serde", version="1.0.228",
                      licence="MIT OR Apache-2.0", linked_into=["demo/_rust.pyd"],
                      sbom="pkg:cargo/serde@1.0.228", bundle="demo-1.0-rust-crates",
                      files=[{**entry()["files"][0], "path": "demo.serde-1.0.228/LICENSE-MIT"}])
        inner = {k: v for k, v in entry(id="inner", name="Inner", version="2.0",
                                        linked_into=["demo/_rust.pyd"], files=[],
                                        in_wheel=True).items() if k != "source"}
        self.components = bp.licence_manifest(manifest(own, foo, core, serde, inner))
        bp.copy_licences(self.folder, {"bare-1.0/LICENSE": TEXT, "libfoo-2.3/COPYING": TEXT,
                                       "demo.serde-1.0.228/LICENSE-MIT": TEXT}, self.components)

    def notices(self, components=None, **kw):
        # A development build also lists the gaps on the console; kept in self.console.
        self.console = io.StringIO()
        with contextlib.redirect_stderr(self.console):
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
        self.assertNotIn("missing", text)
        self.assertIn("collected in licences/ from each project's own\nrelease.", text)

    def test_compiled_in_code_is_listed_with_its_texts(self):
        # #176 D1: code from other projects compiled into a wheel's library.
        text = self.notices(required=True)
        lib = f"runtime/{bp.SITE}/demo/_rust.pyd"
        self.assertIn("  Compiled into its libraries:\n"
                      f"    serde 1.0.228 (MIT OR Apache-2.0), in {lib}: "
                      "licences/demo-1.0-rust-crates.txt\n"
                      f"    Inner 2.0 (MIT (metadata)), in {lib}: the demo texts above\n", text)
        self.assertIn('listed under "Compiled into its libraries"', text)
        bundle = (self.folder / "licences" / "demo-1.0-rust-crates.txt").read_bytes()
        self.assertIn(b"File: demo.serde-1.0.228/LICENSE-MIT", bundle)
        self.assertFalse((self.folder / "licences" / "demo.serde-1.0.228").exists())

    def test_a_crate_in_the_sbom_without_a_text_stops_a_release(self):
        less = [c for c in self.components if c["id"] != "demo.serde"]
        text = self.notices(components=less)
        self.assertNotIn("to follow", text)
        self.assertIn("Some licence texts are missing from this development build", text)
        self.assertIn("demo 1.0: pkg:cargo/serde@1.0.228 (in the wheel's SBOM) has no licence "
                      "text recorded", self.console.getvalue())
        with self.assertRaisesRegex(SystemExit, r"pkg:cargo/serde@1\.0\.228 \(in the wheel's"):
            self.notices(components=less, required=True)

    def test_a_stale_library_or_crate_stops_any_build(self):
        for change, why in (({"linked_into": ["demo/_gone.pyd"]}, "demo/_gone.pyd is not in demo"),
                            ({"sbom": "pkg:cargo/serde@9.9.9"},
                             "pkg:cargo/serde@9.9.9 is not in demo's SBOM")):
            with self.subTest(why=why):
                stale = copy.deepcopy(self.components)
                stale[3].update(change)
                with self.assertRaisesRegex(SystemExit, f"is stale:(.|\n)*{why}"):
                    self.notices(components=stale)

    def test_without_texts_the_gaps_are_stated_and_can_stop_the_build(self):
        text = self.notices(components=[])
        self.assertIn("bare 1.0\n  Licence: MIT\n  Texts: none shipped in the wheel", text)
        self.assertNotIn("to follow", text)
        self.assertIn("Some licence texts are missing from this development build; a release\n"
                      "build stops until each has one.", text)
        with self.assertRaisesRegex(SystemExit, "bare 1.0: the wheel ships no licence text"):
            self.notices(components=[], required=True)
        self.notices(required=True)             # complete: passes

    def test_removing_a_covered_file_from_the_manifest_stops_a_required_build(self):
        # The exact name covers the file; take it out of the manifest and a
        # build that requires coverage (a release) stops on that file.
        self.notices(required=True)
        less = copy.deepcopy(self.components)
        less[2]["covers"] = ["demo/core.dll"]
        self.assertIn("missing from this development build", self.notices(components=less))
        self.assertIn(f"a release stops on these:\n  runtime/{bp.SITE}/demo/.libs/uv.dll: "
                      "no licence text recorded", self.console.getvalue())
        with self.assertRaisesRegex(SystemExit, r"demo/\.libs/uv\.dll: no licence text"):
            self.notices(components=less, required=True)

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


# The crate mode (#448): synthetic wheels, crates and GitHub trees only. Nothing
# here reaches the network: every fetch goes to a fake that records it.
MIT = b"MIT License\n\nPermission is hereby granted, free of charge...\n"
APACHE = b"Apache License\nVersion 2.0, January 2004\n"
ZLIB = b"zlib License\n\nThis software is provided 'as-is'...\n"
C1, C2 = "1" * 40, "2" * 40


def git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def crate_of(name, version, files, repository=None, commit=C1, where="", vcs=True,
             link=None):
    """A .crate: a gzipped tar of <name>-<version>/ with Cargo.toml, the VCS
    record and the given files; `link` adds a licence file that is a symbolic link."""
    top = f"{name}-{version}/"
    repository = repository or f"https://github.com/example/{name}"
    toml = (f'[package]\nname = "{name}"\nversion = "{version}"\n'
            f'repository = "{repository}"\nlicense = "MIT"\n').encode()
    members = {top + "Cargo.toml": toml, top + "src/lib.rs": b"// code\n",
               top + "src/license.rs": b"// not a licence text\n"}
    if vcs:
        members[top + ".cargo_vcs_info.json"] = json.dumps(
            {"git": {"sha1": commit}, "path_in_vcs": where}).encode()
    members.update({top + rel: data for rel, data in files.items()})
    return tar_of(members, link=top + link if link else None)


def sbom_of(crates):
    """A CycloneDX SBOM naming these crates ({purl: .crate bytes or a SHA-256}),
    plus a build tool and a local crate, which need no entry."""
    def sha(v):
        return v if isinstance(v, str) else hashlib.sha256(v).hexdigest()
    return json.dumps({"bomFormat": "CycloneDX", "components": [
        {"purl": purl, "scope": "required", "hashes": [{"alg": "SHA-256", "content": sha(v)}],
         "licenses": [{"expression": "MIT OR Apache-2.0"}]} for purl, v in crates.items()] + [
        {"purl": "pkg:cargo/cc@1.0.0", "scope": "excluded",
         "hashes": [{"alg": "SHA-256", "content": "e" * 64}]},
        {"purl": "pkg:cargo/demo-core@1.0.0?download_url=file://core", "scope": "required"}]})


def tree_of(owner, repo, commit, files, links=()):
    """The GitHub API's recursive tree at a commit, as JSON bytes."""
    tree = [{"path": p, "mode": "100644", "type": "blob", "sha": git_blob(d)}
            for p, d in files.items()]
    tree += [{"path": p, "mode": "120000", "type": "blob", "sha": "f" * 40} for p in links]
    return json.dumps({"sha": "0" * 40, "truncated": False, "tree": tree,
                       "url": f"https://api.github.com/repos/{owner}/{repo}/git/trees/"
                              f"{commit}"}).encode()


def crate_url(name, version):
    return f"https://static.crates.io/crates/{name}/{name}-{version}.crate"


def tree_url(owner, repo, commit):
    return f"https://api.github.com/repos/{owner}/{repo}/git/trees/{commit}?recursive=1"


def crate_web(pages, refuse=None):
    """An open_url for the crate mode that serves pages and records each request;
    refuse: {url: HTTP status} answered with that error."""
    requests = []

    def open_url(request, timeout):
        requests.append(request)
        if refuse and request.full_url in refuse:
            raise urllib.error.HTTPError(request.full_url, refuse[request.full_url], "No",
                                         {}, None)
        if request.full_url not in pages:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        return FakeResponse(pages[request.full_url], request.full_url)
    open_url.requests = requests
    return open_url


class CrateModeTests(unittest.TestCase):
    WHEEL = "demo-1.0-cp310-abi3-win_amd64.whl"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        # alpha: texts at its repository's root, one of them Markdown; the crate
        # spells its repository in lower case with ".git".
        self.alpha = crate_of("alpha", "1.0.0", {"LICENSE-MIT": MIT, "LICENSE.md": ZLIB},
                              repository="https://github.com/example/alpha.git")
        # beta: a workspace member whose LICENSE-APACHE is a link to the root's text
        # in its repository, and which holds the same text twice.
        self.beta = crate_of("beta", "2.0.0", {"LICENSE-APACHE": APACHE,
                                               "runtime/LICENSE-APACHE": APACHE,
                                               "src/tables/LICENSE-ZLIB": ZLIB},
                             repository="https://github.com/example/workspace", commit=C2,
                             where="crates/beta")
        # gamma: records no commit; an entry made by hand for it is kept.
        self.gamma = crate_of("gamma", "1.0.0", {"LICENSE": MIT}, vcs=False)
        self.pages = {
            crate_url("alpha", "1.0.0"): self.alpha,
            crate_url("beta", "2.0.0"): self.beta,
            crate_url("gamma", "1.0.0"): self.gamma,
            crate_url("delta", "0.1.0"): b"not the crate the SBOM names",
            tree_url("example", "alpha", C1): tree_of("Example", "alpha", C1,
                                                      {"LICENSE-MIT": MIT, "LICENSE.md": ZLIB}),
            tree_url("example", "workspace", C2): tree_of(
                "example", "workspace", C2,
                {"LICENSE-APACHE": APACHE, "crates/beta/src/tables/LICENSE-ZLIB": ZLIB},
                links=["crates/beta/LICENSE-APACHE"]),
        }
        sbom = sbom_of({"pkg:cargo/alpha@1.0.0": self.alpha, "pkg:cargo/beta@2.0.0": self.beta,
                        "pkg:cargo/gamma@1.0.0": self.gamma, "pkg:cargo/delta@0.1.0": "d" * 64})
        self.wheel = self.folder / self.WHEEL
        self.wheel.write_bytes(zip_of({"demo-1.0.dist-info/sboms/demo.cyclonedx.json": sbom,
                                       "demo/_demo.pyd": b"MZ"}))
        self.lock = ("demo==1.0 \\\n    --hash=sha256:"
                     + hashlib.sha256(self.wheel.read_bytes()).hexdigest() + "\n")
        self.kept = entry(id="demo.gamma", name="gamma", version="1.0.0", wheel_version="0.9",
                          linked_into=["demo/_demo.pyd"], sbom="pkg:cargo/gamma@1.0.0",
                          bundle="demo-0.9-rust-crates", note="Made by hand.",
                          files=[{**entry()["files"][0], "path": "demo.gamma-1.0.0/LICENSE"}])
        self.gone = entry(id="demo.omega", name="omega", version="3.0", wheel_version="0.9",
                          linked_into=["demo/_demo.pyd"], sbom="pkg:cargo/omega@3.0",
                          bundle="demo-0.9-rust-crates",
                          files=[{**entry()["files"][0], "path": "demo.omega-3.0/LICENSE"}])
        self.other = entry()
        (self.folder / "manifest.json").write_text(
            manifest(self.other, self.kept, self.gone), encoding="utf-8")

    def run_mode(self, web, token=None):
        out = []
        gaps = cl.crate_entries(self.folder, self.wheel, self.lock, web, token, out.append)
        data = json.loads((self.folder / "manifest.json").read_text(encoding="utf-8"))
        return gaps, {c["id"]: c for c in data["components"]}, out

    def test_makes_each_crates_entry_from_the_crate_and_its_commit(self):
        web = crate_web(self.pages)
        gaps, entries, out = self.run_mode(web)
        alpha = entries["demo.alpha"]
        self.assertEqual(alpha["source"], {"repository": "https://github.com/Example/alpha",
                                           "ref": "alpha 1.0.0 (crates.io)", "commit": C1})
        self.assertEqual((alpha["wheel"], alpha["wheel_version"], alpha["linked_into"],
                          alpha["sbom"], alpha["bundle"], alpha["licence"], alpha["covers"]),
                         ("demo", "1.0", ["demo/_demo.pyd"], "pkg:cargo/alpha@1.0.0",
                          "demo-1.0-rust-crates", "MIT OR Apache-2.0", []))
        raw = f"https://raw.githubusercontent.com/Example/alpha/{C1}/"
        # Each file's hash is the crate's own copy's, so collecting it later says
        # "unchanged" only when the text is byte-identical.
        self.assertEqual(alpha["files"], [
            {"path": "demo.alpha-1.0.0/LICENSE-MIT", "url": raw + "LICENSE-MIT",
             "sha256": hashlib.sha256(MIT).hexdigest()},
            {"path": "demo.alpha-1.0.0/LICENSE.md.txt", "url": raw + "LICENSE.md",
             "sha256": hashlib.sha256(ZLIB).hexdigest()}])
        # A workspace member's link goes to the root's text; a text held twice is
        # listed once; a text in a sub-folder keeps its folders in its name.
        raw = f"https://raw.githubusercontent.com/example/workspace/{C2}/"
        self.assertEqual([(f["path"], f["url"]) for f in entries["demo.beta"]["files"]], [
            ("demo.beta-2.0.0/LICENSE-APACHE", raw + "LICENSE-APACHE"),
            ("demo.beta-2.0.0/src.tables.LICENSE-ZLIB",
             raw + "crates/beta/src/tables/LICENSE-ZLIB")])
        # An entry made by hand for a crate the mode cannot make is kept, for the
        # new wheel version; a crate with no entry is a gap; a crate the SBOM no
        # longer names loses its entry, and its text is named for removal.
        self.assertEqual(entries["demo.gamma"], {**self.kept, "wheel_version": "1.0",
                                                 "bundle": "demo-1.0-rust-crates"})
        self.assertEqual(gaps, 1)
        self.assertNotIn("demo.omega", entries)
        self.assertIn(self.other["id"], entries)
        text = "\n".join(out)
        self.assertIn("pkg:cargo/delta@0.1.0: NO ENTRY", text)
        self.assertIn("does not match the SBOM's SHA-256", text)
        self.assertIn("gamma@1.0.0: kept the entry made by hand (gamma 1.0.0: the crate "
                      "records no commit.)", text)
        self.assertIn("remove it: packaging/licences/demo.omega-3.0/LICENSE", text)
        # The build accepts what was written.
        bp.licence_manifest((self.folder / "manifest.json").read_text(encoding="utf-8"))
        # Only the crate hosts were reached, one tree per repository and commit, and
        # never a build tool or a local crate; no text was fetched.
        hosts = {urllib.parse.urlsplit(r.full_url).hostname for r in web.requests}
        self.assertEqual(hosts, set(cl.CRATE_HOSTS))
        self.assertEqual(sorted(r.full_url for r in web.requests),
                         sorted([crate_url("alpha", "1.0.0"), crate_url("beta", "2.0.0"),
                                 crate_url("gamma", "1.0.0"), crate_url("delta", "0.1.0"),
                                 tree_url("example", "alpha", C1),
                                 tree_url("example", "workspace", C2)]))

    def test_collecting_what_it_made_says_unchanged_for_the_crates_own_texts(self):
        # Step 4: the texts come from a collector host, and a byte-identical one
        # says "unchanged"; one that differs says "CHANGED".
        self.run_mode(crate_web(self.pages))
        raw = {f"https://raw.githubusercontent.com/Example/alpha/{C1}/LICENSE-MIT": MIT,
               f"https://raw.githubusercontent.com/Example/alpha/{C1}/LICENSE.md": ZLIB}
        out = []
        self.assertEqual(cl.collect(self.folder, ["demo.alpha"], fake_web(raw), out.append), 0)
        self.assertTrue(all(line.endswith("(unchanged)") for line in out), out)
        raw[f"https://raw.githubusercontent.com/Example/alpha/{C1}/LICENSE-MIT"] = MIT + b"x"
        cl.collect(self.folder, ["demo.alpha"], fake_web(raw), out.append)
        self.assertIn("(CHANGED)", "\n".join(out))

    def test_a_text_no_file_at_the_commit_has_is_a_gap(self):
        # Windows line endings in the crate, for example: no blob matches.
        self.pages[tree_url("example", "alpha", C1)] = tree_of(
            "example", "alpha", C1, {"LICENSE-MIT": MIT.replace(b"\n", b"\r\n"),
                                     "LICENSE.md": ZLIB})
        gaps, entries, out = self.run_mode(crate_web(self.pages))
        self.assertEqual(gaps, 2)
        self.assertNotIn("demo.alpha", entries)
        self.assertIn(f"no file at example/alpha {C1} has the bytes of its LICENSE-MIT",
                      "\n".join(out))

    def test_a_linked_or_binary_licence_file_is_a_gap(self):
        for crate, why in ((crate_of("alpha", "1.0.0", {}, link="LICENSE"), "is a link"),
                           (crate_of("alpha", "1.0.0", {"COPYING": b"\0\1"}), "NUL bytes"),
                           (crate_of("alpha", "1.0.0", {}), "has no licence file")):
            with self.subTest(why=why):
                pages = {**self.pages, crate_url("alpha", "1.0.0"): crate}
                sbom = sbom_of({"pkg:cargo/alpha@1.0.0": crate})
                self.wheel.write_bytes(zip_of({"demo-1.0.dist-info/sboms/x.json": sbom,
                                               "demo/_demo.pyd": b"MZ"}))
                self.lock = ("demo==1.0 --hash=sha256:"
                             + hashlib.sha256(self.wheel.read_bytes()).hexdigest() + "\n")
                gaps, _, out = self.run_mode(crate_web(pages))
                self.assertEqual(gaps, 1)
                self.assertIn(why, "\n".join(out))

    def test_the_wheel_must_be_a_file_the_lock_pins(self):
        before = (self.folder / "manifest.json").read_bytes()
        for lock, why in (("demo==1.0 --hash=sha256:" + "0" * 64 + "\n", "SHA-256 differs"),
                          ("demo==2.0 --hash=sha256:" + "0" * 64 + "\n", "not in the lock")):
            with self.subTest(why=why), self.assertRaisesRegex(cl.CollectError, why):
                cl.crate_entries(self.folder, self.wheel, lock, crate_web(self.pages), None,
                                 lambda _: None)
        self.assertEqual((self.folder / "manifest.json").read_bytes(), before)

    def test_githubs_rate_limit_stops_the_run_and_writes_nothing(self):
        before = (self.folder / "manifest.json").read_bytes()
        web = crate_web(self.pages, refuse={tree_url("example", "alpha", C1): 403})
        with self.assertRaisesRegex(cl.CollectError, "set GITHUB_TOKEN"):
            self.run_mode(web)
        self.assertEqual((self.folder / "manifest.json").read_bytes(), before)

    def test_the_sbom_crates_are_the_ones_the_build_counts(self):
        sbom = sbom_of({"pkg:cargo/alpha@1.0.0": "a" * 64, "pkg:cargo/beta@2.0.0": "b" * 64})
        self.assertEqual(sorted(cl.sbom_crate_hashes(sbom)), bp.sbom_crates(sbom))
        self.assertEqual(cl.sbom_crate_hashes(sbom)["pkg:cargo/alpha@1.0.0"],
                         ("a" * 64, "MIT OR Apache-2.0"))

    def test_the_lock_is_read_with_its_continuation_lines(self):
        lock = ("# via x\nHF_Xet==1.6.0 \\\n    --hash=sha256:" + "a" * 64 + " \\\n"
                "    --hash=sha256:" + "b" * 64 + "\n    # via y\nother==2 --hash=sha256:"
                + "c" * 64 + "\n")
        self.assertEqual(cl.locked_hashes(lock), {("hf_xet", "1.6.0"): {"a" * 64, "b" * 64},
                                                  ("other", "2"): {"c" * 64}})

    def test_repository_urls_are_github_owner_and_name_only(self):
        self.assertEqual(cl.github_repository("https://github.com/o/r.git"), ("o", "r"))
        self.assertEqual(cl.github_repository("https://github.com/o/r/tree/main/x"), ("o", "r"))
        for url in ("https://gitlab.com/o/r", "http://github.com/o/r", "https://github.com/o",
                    "https://github.com/o/..", "https://user@github.com/o/r"):
            with self.subTest(url=url):
                self.assertIsNone(cl.github_repository(url))


class CrateHostTests(unittest.TestCase):
    """Owner decision DEC-075 (#448): the crate hosts are for the crate mode only."""

    def test_the_crate_hosts_are_not_collector_hosts(self):
        self.assertEqual(set(cl.CRATE_HOSTS), {"static.crates.io", "api.github.com"})
        self.assertFalse(set(cl.CRATE_HOSTS) & set(bp.LICENCE_HOSTS))
        web = fake_web({})
        for host in cl.CRATE_HOSTS:
            url = f"https://{host}/x"
            with self.subTest(host=host):
                self.assertIsNone(bp.licence_url_parts(url))
                self.assertFalse(cl.allowed_url(url))
                with self.assertRaisesRegex(cl.CollectError, "collector host"):
                    cl.fetch(url, 100, web)
                self.assertRegex(bp.licence_url_problem(url, COMMIT), "collector host")
        self.assertEqual(web.calls, [])

    def test_the_build_and_the_app_never_name_the_crate_hosts(self):
        # Only tools/collect_licences.py may; the build never imports it (above).
        code = [ROOT / "tools" / "build_package.py"]
        code += sorted((ROOT / "workspace").rglob("*.py"))
        code += [p for p in sorted((ROOT / "packaging").rglob("*"))
                 if p.suffix.lower() in (".ps1", ".psm1", ".cmd", ".py")]
        self.assertGreater(len(code), 10)
        for path in code:
            text = path.read_text(encoding="utf-8", errors="replace")
            for name in (*cl.CRATE_HOSTS, "CRATE_HOSTS", "crate_fetch"):
                with self.subTest(path=path.relative_to(ROOT).as_posix(), name=name):
                    self.assertNotIn(name, text)

    def test_the_crate_mode_reaches_only_the_crate_hosts(self):
        web = crate_web({})
        for url in ("https://raw.githubusercontent.com/o/r/" + COMMIT + "/LICENSE",
                    "https://files.pythonhosted.org/x.whl", "http://static.crates.io/x",
                    "https://static.crates.io:8443/x", "https://u@api.github.com/x",
                    "https://example.com/x"):
            with self.subTest(url=url), self.assertRaisesRegex(cl.CollectError, "crate host"):
                cl.crate_fetch(url, 100, web)
        self.assertEqual(web.requests, [])
        with self.assertRaisesRegex(cl.CollectError, "redirects off the crate hosts"):
            cl.CrateRedirects().redirect_request(
                mock.Mock(full_url="https://static.crates.io/x"), None, 302, "Found", {},
                "https://raw.githubusercontent.com/o/r/x")

        def moved(request, timeout):
            return FakeResponse(b"x", "https://example.com/x")
        with self.assertRaisesRegex(cl.CollectError, "off the crate hosts"):
            cl.crate_fetch("https://static.crates.io/x", 100, moved)

    def test_the_token_goes_to_the_github_api_only_and_never_across_a_redirect(self):
        web = crate_web({"https://static.crates.io/x": b"crate",
                         "https://api.github.com/repos/o/r": b"{}"})
        cl.crate_fetch("https://static.crates.io/x", 100, web, token="secret-token")
        cl.crate_fetch("https://api.github.com/repos/o/r", 100, web, token="secret-token")
        crate, api = web.requests
        self.assertIsNone(crate.get_header("Authorization"))
        self.assertEqual(api.unredirected_hdrs, {"Authorization": "Bearer secret-token"})
        moved = cl.CrateRedirects().redirect_request(
            api, None, 301, "Moved", {}, "https://api.github.com/repositories/1")
        self.assertFalse(moved.has_header("Authorization"))
        # No error names it.
        web = crate_web({}, refuse={"https://api.github.com/repos/o/r": 500})
        with self.assertRaises(cl.CollectError) as caught:
            cl.crate_fetch("https://api.github.com/repos/o/r", 100, web, token="secret-token")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_crates_takes_a_wheel_not_ids(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cl.main(["--crates", "x.whl", "demo"])


if __name__ == "__main__":
    unittest.main()
