# Machine models (#111 B7a): models IT pre-staged under a machine install's
# Program Files folder come first, are checked only against IT's own record
# (or hashed in full), are never written by the app, and AllowUserModels
# decides, on a machine install only, whether the user's own copies are used.
# Synthetic files only; stdlib only, so these run in CI without any model.

import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace import asr_engines as ae  # noqa: E402
from workspace import policy  # noqa: E402
from workspace import setup  # noqa: E402

GOOD = {"config.json": b'{"model_type": "fake"}', "weights.onnx": b"\x01\x02" * 500}
OTHER = {"config.json": b'{"model_type": "other"}', "weights.onnx": b"\x03\x04" * 400}


def make_spec(contents=GOOD, name="fake-model", revision="a" * 40, hub_repos=()):
    files = tuple(ae.ModelFile(n, len(d), hashlib.sha256(d).hexdigest())
                  for n, d in contents.items())
    return ae.ModelSpec(name=name, folder=name, onnx_asr_name=name, repo="example/fake",
                        revision=revision, files=files, hub_repos=hub_repos)


def write_files(folder, contents):
    folder.mkdir(parents=True, exist_ok=True)
    for n, d in contents.items():
        (folder / n).parent.mkdir(parents=True, exist_ok=True)
        (folder / n).write_bytes(d)


def write_record(spec, where, **changes):
    """IT's record as Install-Models.ps1 writes it."""
    record = {"repo": spec.repo, "revision": spec.revision, "folder": spec.folder, "ok": True,
              "files": ae._fingerprint(Path(where), spec)}
    record.update(changes)
    (Path(where) / ae.MACHINE_RECORD).write_text(json.dumps(record), encoding="utf-8")


def machine(root):
    """This process as a trusted machine install whose folder is root."""
    return mock.patch.object(ae, "_machine_root", [Path(root)])


def user_models(allowed):
    return mock.patch.object(ae, "user_models_allowed", lambda reader=None: allowed)


def listing(folder):
    """Every path under folder, with each file's size and modification time."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns)
    return out


class MachineFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "Program Files" / "Unminute"
        self.cache = base / "profile" / "models"
        self.cache.mkdir(parents=True)
        self.spec = make_spec()
        self.mfolder = ae.machine_model_dir(self.spec, self.root)
        self.ufolder = ae.model_dir(self.spec, self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, spec=None, contents=GOOD, record=True):
        spec = spec or self.spec
        folder = ae.machine_model_dir(spec, self.root)
        write_files(folder, contents)
        if record:
            write_record(spec, folder)
        return folder


class LookupTests(MachineFixture):
    def test_the_machine_copy_comes_first(self):
        self.stage()
        write_files(self.ufolder, GOOD)
        with machine(self.root), user_models(True):
            self.assertEqual(ae.hub_candidates(self.spec, self.cache),
                             [self.mfolder, self.ufolder])
            self.assertEqual(ae.find_verified(self.spec, self.cache), self.mfolder)
            self.assertEqual(ae.locate(self.spec, self.cache), (self.mfolder, []))

    def test_the_path_holds_family_folder_and_revision(self):
        self.assertEqual(self.mfolder,
                         self.root / "models" / "parakeet" / "fake-model" / ("a" * 40))

    def test_a_kept_versions_revision_sits_beside_the_current_one(self):
        older = make_spec(OTHER, revision="b" * 40)
        self.stage()
        self.stage(older, OTHER)
        with machine(self.root), user_models(False):
            self.assertEqual(ae.find_verified(self.spec, self.cache), self.mfolder)
            self.assertEqual(ae.find_verified(older, self.cache),
                             ae.machine_model_dir(older, self.root))

    def test_user_copies_are_used_only_when_allowed(self):
        spec = make_spec(hub_repos=("example/fake",))
        write_files(ae.model_dir(spec, self.cache), GOOD)
        snap = self.cache / "models--example--fake" / "snapshots" / "abc"
        write_files(snap, GOOD)
        with machine(self.root), user_models(True):
            self.assertEqual(ae.hub_candidates(spec, self.cache),
                             [ae.model_dir(spec, self.cache), snap])
        with machine(self.root), user_models(False):
            # Older Hugging Face cache copies count as the user's too.
            self.assertEqual(ae.hub_candidates(spec, self.cache), [])
            self.assertIsNone(ae.find_verified(spec, self.cache))
            self.assertIsNone(ae.read_verified(spec, self.cache))
            self.assertIsNone(ae._quick_status(spec, self.cache))

    def test_no_machine_copy_without_a_trusted_machine_install(self):
        self.stage()
        write_files(self.ufolder, GOOD)
        with mock.patch.object(ae, "_machine_root", [None]):
            self.assertEqual(ae.hub_candidates(self.spec, self.cache), [self.ufolder])

    def test_a_damaged_machine_copy_falls_back_to_the_users_when_allowed(self):
        self.stage(contents={**GOOD, "weights.onnx": b"\x09\x09" * 500}, record=False)
        write_files(self.ufolder, GOOD)
        with machine(self.root), user_models(True):
            self.assertEqual(ae.find_verified(self.spec, self.cache), self.ufolder)
        with machine(self.root), user_models(False):
            folder, problems = ae.locate(self.spec, self.cache)
            self.assertIsNone(folder)
            self.assertIn("checksum", problems[0])


class RecordTests(MachineFixture):
    def test_a_matching_record_is_trusted_without_hashing(self):
        self.stage()
        with machine(self.root), mock.patch.object(ae, "_sha256",
                                                   side_effect=AssertionError("hashed")):
            self.assertEqual(ae.check_machine(self.spec, self.mfolder), [])

    def test_no_record_means_a_full_hash_and_nothing_written(self):
        self.stage(record=False)
        with machine(self.root):
            self.assertEqual(ae.check_machine(self.spec, self.mfolder), [])
        self.assertFalse((self.mfolder / ae.MACHINE_RECORD).exists())
        self.assertFalse((self.mfolder / ae.VERIFIED_MARKER).exists())

    def test_a_record_is_trusted_only_when_everything_matches(self):
        changes = [{"revision": "c" * 40}, {"repo": "example/other"}, {"folder": "other"},
                   {"ok": False}, {"ok": "true"}]
        for change in changes:
            with self.subTest(change=change):
                # Same size, other bytes: only a full hash finds it.
                self.stage(contents={**GOOD, "weights.onnx": b"\x07\x07" * 500}, record=False)
                write_record(self.spec, self.mfolder, **change)
                with machine(self.root):
                    self.assertIn("checksum", ae.check_machine(self.spec, self.mfolder)[0])

    def test_a_changed_file_is_hashed_in_full(self):
        self.stage()
        p = self.mfolder / "weights.onnx"
        p.write_bytes(b"\x07\x07" * 500)
        later = time.time() + 5
        os.utime(p, (later, later))
        with machine(self.root):
            self.assertIn("checksum", ae.check_machine(self.spec, self.mfolder)[0])

    def test_full_and_keep_always_hash(self):
        self.stage()
        p = self.mfolder / "weights.onnx"
        before = p.stat()
        p.write_bytes(b"\x07\x07" * 500)
        os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))     # the record is fooled
        with machine(self.root):
            self.assertEqual(ae.check_machine(self.spec, self.mfolder), [])
            self.assertIn("checksum", ae.check_machine(self.spec, self.mfolder, full=True)[0])
            self.assertIn("checksum", ae.check_machine(self.spec, self.mfolder, keep={})[0])

    def test_read_verified_returns_the_hashed_bytes_of_the_machine_copy(self):
        self.stage()
        with machine(self.root), user_models(False):
            self.assertEqual(ae.read_verified(self.spec, self.cache), GOOD)

    def test_the_users_records_are_never_read_for_a_machine_copy(self):
        # A failure record a user planted for the machine copy's path, and a
        # record in the user's cache, change nothing (critique B7-7).
        self.stage()
        key = ae._marker_for(self.spec, self.cache, self.mfolder)
        key.parent.mkdir(parents=True)
        key.write_text(json.dumps({"ok": False, "revision": self.spec.revision,
                                   "files": ae._fingerprint(self.mfolder, self.spec),
                                   "problems": ["planted"]}), encoding="utf-8")
        read = []
        real = ae._read_marker

        def spy(path):
            read.append(Path(path))
            return real(path)
        with machine(self.root), user_models(False), mock.patch.object(ae, "_read_marker", spy):
            self.assertEqual(ae.find_verified(self.spec, self.cache), self.mfolder)
            self.assertEqual(ae._quick_status(self.spec, self.cache), [])
            self.assertEqual(ae.locate(self.spec, self.cache), (self.mfolder, []))
        self.assertTrue(read)
        self.assertTrue(all(p == self.mfolder / ae.MACHINE_RECORD for p in read), read)


class NeverWrittenTests(MachineFixture):
    def test_nothing_in_the_machine_root_is_written(self):
        self.stage(record=False)             # no record: every check hashes in full
        before = listing(self.root)

        def refuse(*args, **kwargs):
            raise AssertionError("a write")
        with machine(self.root), user_models(False), \
                mock.patch.object(Path, "write_text", refuse), \
                mock.patch.object(Path, "write_bytes", refuse), \
                mock.patch.object(Path, "mkdir", refuse), \
                mock.patch.object(Path, "rename", refuse):
            self.assertEqual(ae.find_verified(self.spec, self.cache, full=True), self.mfolder)
            self.assertEqual(ae.locate(self.spec, self.cache), (self.mfolder, []))
            self.assertEqual(ae.read_verified(self.spec, self.cache), GOOD)
            self.assertEqual(ae._quick_status(self.spec, self.cache), [])
        self.assertEqual(listing(self.root), before)

    def test_install_and_remove_refuse_the_machine_root(self):
        inside = self.root / "models"
        inside.mkdir(parents=True)
        with machine(self.root):
            with self.assertRaisesRegex(ae.ModelLoadError, "IT team"):
                ae.install_model(self.spec, inside, log=lambda m: None)
            with self.assertRaises(setup.RemovalRefused):
                setup.remove(ae.PARAKEET_V2, inside)
            self.assertFalse(ae.in_machine_root(self.cache))


class LinkTests(MachineFixture):
    def test_a_link_anywhere_refuses_the_copy(self):
        self.stage()
        places = [self.root / "models", self.mfolder.parent.parent, self.mfolder.parent,
                  self.mfolder, self.mfolder / "weights.onnx", self.mfolder / ae.MACHINE_RECORD]
        for place in places:
            with self.subTest(place=place.name):
                with machine(self.root), user_models(False), \
                        mock.patch.object(ae, "is_link", lambda p, place=place: Path(p) == place):
                    self.assertEqual(ae.check_machine(self.spec, self.mfolder), [ae.MACHINE_LINK])
                    self.assertIsNone(ae.find_verified(self.spec, self.cache))
                    self.assertEqual(ae._quick_status(self.spec, self.cache), [ae.MACHINE_LINK])
                    self.assertFalse(ae.machine_ready(ae.PARAKEET_V2))

    @unittest.skipUnless(sys.platform == "win32", "junctions are Windows only")
    def test_a_real_junction_at_the_revision_is_refused(self):
        import _winapi
        elsewhere = Path(self.tmp.name) / "elsewhere"
        write_files(elsewhere, GOOD)
        write_record(self.spec, elsewhere)
        self.mfolder.parent.mkdir(parents=True)
        _winapi.CreateJunction(str(elsewhere), str(self.mfolder))
        with machine(self.root), user_models(False):
            self.assertEqual(ae.check_machine(self.spec, self.mfolder), [ae.MACHINE_LINK])
            self.assertIsNone(ae.find_verified(self.spec, self.cache))


class MachineRootTests(unittest.TestCase):
    def tree(self, record):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "Unminute"
        app_file = root / "versions" / "1.0.0" / "app" / "workspace" / "update.py"
        app_file.parent.mkdir(parents=True)
        app_file.write_text("", encoding="utf-8")
        (root / "current.json").write_text(json.dumps(record), encoding="utf-8")
        return root, app_file

    def test_a_per_user_install_claiming_machine_mode_has_no_machine_root(self):
        # The real trust test: a folder in the user's profile is not Program Files.
        root, app_file = self.tree({"version": "1.0.0", "mode": "machine"})
        self.assertIsNone(ae.find_machine_root(app_file))

    def test_only_a_trusted_machine_install_has_one(self):
        root, app_file = self.tree({"version": "1.0.0", "mode": "machine", "managed": True})
        self.assertEqual(ae.find_machine_root(app_file, trusted=lambda r: True), root.resolve())
        self.assertIsNone(ae.find_machine_root(app_file, trusted=lambda r: False))
        root, app_file = self.tree({"version": "1.0.0", "managed": True})
        self.assertIsNone(ae.find_machine_root(app_file, trusted=lambda r: True))

    def test_any_error_means_none(self):
        with mock.patch("workspace.update.deployment", side_effect=RuntimeError("x")):
            self.assertIsNone(ae.find_machine_root())

    def test_a_source_checkout_has_none(self):
        self.assertIsNone(ae.find_machine_root())


class AllowUserModelsTests(unittest.TestCase):
    def test_read_only_on_a_machine_install(self):
        def reader():
            raise AssertionError("read on a per-user install")
        with mock.patch.object(ae, "_machine_root", [None]):
            self.assertTrue(ae.user_models_allowed(reader))

    def test_on_a_machine_install(self):
        cases = [(({}, ()), True),
                 (({"AllowUserModels": True}, ()), True),
                 (({"AllowUserModels": False}, ()), False),
                 (({}, ("AllowUserModels",)), False),         # invalid: withheld
                 (({}, policy.UNREADABLE), False)]            # unreadable: nothing granted
        with mock.patch.object(ae, "_machine_root", [Path("C:/Program Files/Unminute")]):
            for read, allowed in cases:
                with self.subTest(read=read):
                    self.assertIs(ae.user_models_allowed(lambda read=read: read), allowed)

    def test_policy_spec(self):
        self.assertEqual(policy.SPEC["AllowUserModels"], ("bool", True, True))
        self.assertIn("AllowUserModels", policy.UNREADABLE)


class StatusTests(MachineFixture):
    def setUp(self):
        super().setUp()
        # Tiny stand-ins for Parakeet v2, v3 and the speech detector, so no
        # file of the real sizes is written.
        tiny = {n: dataclasses.replace(make_spec(name=n), folder=s.folder)
                for n, s in ae.PARAKEET_MODELS.items()}
        vad = dataclasses.replace(make_spec(OTHER, name="silero-vad"), folder="silero-vad")
        for patch in (mock.patch.dict(ae.PARAKEET_MODELS, tiny),
                      mock.patch.dict(ae.MANAGED_MODELS, tiny),
                      mock.patch.object(ae, "SILERO_VAD", vad)):
            patch.start()
            self.addCleanup(patch.stop)

    def stage_parakeet(self, name=ae.PARAKEET_V2):
        self.stage(ae.PARAKEET_MODELS[name])
        self.stage(ae.SILERO_VAD, OTHER)

    def test_setup_shows_a_machine_copy_as_installed_by_it(self):
        self.stage_parakeet()
        with machine(self.root), user_models(False):
            self.assertTrue(ae.machine_ready(ae.PARAKEET_V2))
            self.assertEqual(setup.status(ae.PARAKEET_V2, self.cache),
                             ("ready", setup.INSTALLED_BY_IT, ""))
            self.assertEqual(ae.parakeet_status(ae.PARAKEET_V2, self.cache), ("ready", ""))
            self.assertEqual(setup.removal_targets(ae.PARAKEET_V2, self.cache), [])
            self.assertEqual(setup.status(ae.PARAKEET_V3, self.cache)[0], "missing")

    def test_without_its_speech_detector_parakeet_is_not_installed_by_it(self):
        self.stage_parakeet()
        import shutil
        shutil.rmtree(ae.machine_model_dir(ae.SILERO_VAD, self.root))
        with machine(self.root), user_models(False):
            self.assertFalse(ae.machine_ready(ae.PARAKEET_V2))
            state, detail = ae.parakeet_status(ae.PARAKEET_V2, self.cache)
            self.assertEqual(state, "invalid")
            self.assertIn("speech detector: not installed", detail)

    def test_the_users_copy_can_still_be_removed(self):
        self.stage_parakeet()
        write_files(ae.model_dir(ae.PARAKEET_MODELS[ae.PARAKEET_V3], self.cache), GOOD)
        with machine(self.root), user_models(True):
            self.assertEqual(setup.removal_targets(ae.PARAKEET_V3, self.cache),
                             [ae.model_dir(ae.PARAKEET_MODELS[ae.PARAKEET_V3], self.cache)])

    def test_status_of_a_spec_with_other_revision_ignores_the_staged_one(self):
        self.stage()
        other = dataclasses.replace(self.spec, revision="d" * 40)
        with machine(self.root), user_models(False):
            self.assertIsNone(ae._quick_status(other, self.cache))


if __name__ == "__main__":
    unittest.main()
