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


class TinyParakeetFixture(MachineFixture):
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


class StatusTests(TinyParakeetFixture):
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


def fooled(folder, name="weights.onnx", data=b"\x07\x07" * 500):
    """Other bytes of the same size, with the modification time put back: IT's
    record still matches, so only a full check (or a load) finds it."""
    p = Path(folder) / name
    before = p.stat()
    p.write_bytes(data)
    os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))


class DamagedCopyTests(TinyParakeetFixture):
    """#451 (review of 317a3d4, nit 3): IT's copy that passes the quick checks
    but fails a full check, or fails to load, is kept as failed in memory until
    its files change. Downloads then shows it as damaged with IT's wording, and
    the worker names IT, not Manage downloads, which offers nothing for it."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(ae._machine_failed, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_full_check_failure_is_kept_until_the_files_change(self):
        self.stage_parakeet()
        main = ae.PARAKEET_MODELS[ae.PARAKEET_V2]
        folder = ae.machine_model_dir(main, self.root)
        fooled(folder)
        with machine(self.root), user_models(False):
            self.assertEqual(setup.status(ae.PARAKEET_V2, self.cache)[:2],
                             ("ready", setup.INSTALLED_BY_IT))
            self.assertIn("checksum", ae.check_machine(main, folder, full=True)[0])
            # Kept: no quick check trusts IT's record for it now.
            self.assertFalse(ae.machine_ready(ae.PARAKEET_V2))
            with mock.patch.object(ae, "_sha256", side_effect=AssertionError("hashed")):
                self.assertIn("checksum", ae.check_machine(main, folder)[0])
                self.assertIn("checksum", ae.locate(main, self.cache)[1][0])
            self.assertEqual(setup.status(ae.PARAKEET_V2, self.cache)[:2],
                             ("invalid", setup.IT_COPY_DAMAGED))
            # The speech detector's own copy is still fine, and IT did not
            # install Parakeet v3, so that one is not IT's to repair.
            self.assertFalse(ae.machine_damaged_model(ae.PARAKEET_V3))
            # IT replaces the files (a new modification time and record).
            write_files(folder, GOOD)
            later = time.time() + 5
            for n in GOOD:
                os.utime(folder / n, (later, later))
            write_record(main, folder)
            self.assertTrue(ae.machine_ready(ae.PARAKEET_V2))
            self.assertEqual(setup.status(ae.PARAKEET_V2, self.cache)[:2],
                             ("ready", setup.INSTALLED_BY_IT))

    def test_a_passing_full_check_clears_it(self):
        self.stage_parakeet()
        main = ae.PARAKEET_MODELS[ae.PARAKEET_V2]
        folder = ae.machine_model_dir(main, self.root)
        with machine(self.root):
            ae._machine_failed[str(folder)] = (ae._fingerprint(folder, main), ["x"])
            self.assertFalse(ae.machine_ready(ae.PARAKEET_V2))
            self.assertEqual(ae.check_machine(main, folder, full=True), [])
            self.assertTrue(ae.machine_ready(ae.PARAKEET_V2))

    def test_a_quick_check_failure_shows_it_wording(self):
        self.stage_parakeet()
        (ae.machine_model_dir(ae.SILERO_VAD, self.root) / "weights.onnx").unlink()
        with machine(self.root), user_models(True):
            state, plain, detail = setup.status(ae.PARAKEET_V2, self.cache)
        self.assertEqual((state, plain), ("invalid", setup.IT_COPY_DAMAGED))
        self.assertIn("speech detector", detail)

    def test_check_every_file_keeps_the_failure_for_downloads(self):
        # The Downloads check runs in the window process: its failure is the
        # one Downloads then shows, with nothing written in the machine root.
        self.stage_parakeet()
        fooled(ae.machine_model_dir(ae.PARAKEET_MODELS[ae.PARAKEET_V2], self.root))
        before = listing(self.root)
        downloads = setup.Downloads(self.cache)
        with machine(self.root), user_models(False):
            downloads.state[ae.PARAKEET_V2] = {}
            downloads._verify(ae.PARAKEET_V2)
            self.assertEqual(downloads.state[ae.PARAKEET_V2]["message"],
                             "Some files do not match their checksums. Ask your IT team to "
                             "repair it.")
            self.assertEqual(setup.status(ae.PARAKEET_V2, self.cache)[:2],
                             ("invalid", setup.IT_COPY_DAMAGED))
        self.assertEqual(listing(self.root), before)

    def test_the_window_learns_of_a_workers_failure_from_its_message(self):
        self.stage_parakeet()
        main = ae.PARAKEET_MODELS[ae.PARAKEET_V2]
        with machine(self.root):
            ae.note_machine_damage("ModelLoadError: Parakeet v2 failed to load. Repair it.")
            self.assertTrue(ae.machine_ready(ae.PARAKEET_V2))
            ae.note_machine_damage(f"ModelLoadError: Parakeet v2 failed to load. "
                                   f"{ae.machine_damaged(main)} (RuntimeError)")
            self.assertFalse(ae.machine_ready(ae.PARAKEET_V2))
            self.assertEqual(ae.check_machine(main, ae.machine_model_dir(main, self.root)),
                             [ae.MACHINE_FAILED])
            # Only the copy named: the speech detector, which Parakeet v3
            # shares, is not marked.
            vad = ae.machine_model_dir(ae.SILERO_VAD, self.root)
            self.assertEqual(ae._machine_failure(ae.SILERO_VAD, vad), [])
        self.assertEqual(ae.machine_damaged(ae.SILERO_VAD),
                         "Your IT team's copy of the speech detector is damaged. Ask your IT "
                         "team to repair it.")
        self.assertEqual(ae.machine_damaged(ae.SPEAKER_MODEL),
                         "Your IT team's copy of the speaker identification model is damaged. "
                         "Ask your IT team to repair it.")
        self.assertEqual(ae.machine_damaged(ae.WHISPER_MODELS[ae.WHISPER_LARGE]),
                         "Your IT team's copy of Whisper large-v3 is damaged. Ask your IT team "
                         "to repair it.")

    def fake_onnx(self, fails):
        """onnx_asr and onnxruntime stand-ins: loading a model from a folder in
        fails raises, as the runtime does for a damaged model."""
        import types
        onnx_asr = types.ModuleType("onnx_asr")
        loaded = []

        def load_model(name, folder, **kwargs):
            if Path(folder) in fails:
                raise RuntimeError("synthetic: model is damaged")
            loaded.append(Path(folder))
            return mock.MagicMock()
        onnx_asr.load_model = load_model
        onnx_asr.load_vad = lambda name, folder, **kwargs: mock.MagicMock()
        rt = types.ModuleType("onnxruntime")
        rt.disable_telemetry_events = lambda: None
        rt.SessionOptions = mock.MagicMock
        return mock.patch.dict(sys.modules, onnx_asr=onnx_asr, onnxruntime=rt), loaded

    def test_parakeet_names_it_when_its_copy_fails_to_load(self):
        self.stage_parakeet()
        main = ae.PARAKEET_MODELS[ae.PARAKEET_V2]
        folder = ae.machine_model_dir(main, self.root)
        fooled(folder)
        modules, loaded = self.fake_onnx({folder})
        with modules, machine(self.root), user_models(False), \
                self.assertRaises(ae.ModelLoadError) as caught:
            ae.ParakeetEngine(ae.PARAKEET_V2, self.cache, 1).load()
        message = str(caught.exception)
        # Tried again without IT's copy, which it now keeps as failed: with no
        # copy of the user's allowed, the error names IT.
        self.assertTrue(message.startswith(f"Parakeet v2 cannot be used. "
                                           f"{ae.machine_damaged(main)} ("), message)
        self.assertNotIn("Manage downloads", message)
        self.assertEqual(loaded, [])

    def test_parakeet_falls_back_to_the_users_copy_when_allowed(self):
        self.stage_parakeet()
        main = ae.PARAKEET_MODELS[ae.PARAKEET_V2]
        folder = ae.machine_model_dir(main, self.root)
        fooled(folder)
        write_files(ae.model_dir(main, self.cache), GOOD)
        modules, loaded = self.fake_onnx({folder})
        with modules, machine(self.root), user_models(True):
            ae.ParakeetEngine(ae.PARAKEET_V2, self.cache, 1).load()
        self.assertEqual(loaded, [ae.model_dir(main, self.cache)])

    def test_a_load_failure_of_a_sound_copy_keeps_its_advice(self):
        # The files hash correctly, so the runtime failed, not IT's copy.
        self.stage_parakeet()
        folder = ae.machine_model_dir(ae.PARAKEET_MODELS[ae.PARAKEET_V2], self.root)
        modules, _ = self.fake_onnx({folder})
        with modules, machine(self.root), user_models(False), \
                self.assertRaises(ae.ModelLoadError) as caught:
            ae.ParakeetEngine(ae.PARAKEET_V2, self.cache, 1).load()
        self.assertIn(f"failed to load. {ae.PARAKEET_FIX} (RuntimeError", str(caught.exception))
        with machine(self.root):
            self.assertTrue(ae.machine_ready(ae.PARAKEET_V2))

    def test_whisper_and_the_speaker_model_name_it(self):
        from workspace import engines
        whisper = make_spec(name=ae.WHISPER_TURBO)
        speaker = make_spec(name=ae.SPEAKER_MODEL.name)
        damaged = {**GOOD, "weights.onnx": b"\x09" * 1000}
        with mock.patch.dict(ae.WHISPER_MODELS, {ae.WHISPER_TURBO: whisper}), \
                mock.patch.object(ae, "SPEAKER_MODEL", speaker):
            for spec in (whisper, speaker):
                self.stage(spec, damaged, record=False)
            import types
            fw = types.ModuleType("faster_whisper")
            fw.WhisperModel = mock.MagicMock(side_effect=AssertionError("loaded"))
            with machine(self.root), user_models(False), \
                    mock.patch.dict(sys.modules, faster_whisper=fw):
                with self.assertRaises(ae.ModelLoadError) as caught:
                    engines.load_engine(ae.WHISPER_TURBO, self.cache, 1)
                self.assertEqual(str(caught.exception),
                                 "Whisper large-v3 turbo is not installed or failed "
                                 f"verification. {ae.machine_damaged(whisper)}")
                with self.assertRaises(ae.ModelLoadError) as caught:
                    engines.diarize_audio("pyannote", self.cache, [0.0] * 16, None, 1)
                self.assertEqual(str(caught.exception),
                                 "The speaker identification model is not installed or failed "
                                 f"verification. {ae.machine_damaged(speaker)}")

    def test_whisper_falls_back_when_its_copy_fails_to_load(self):
        from workspace import engines
        whisper = make_spec(name=ae.WHISPER_TURBO)
        with mock.patch.dict(ae.WHISPER_MODELS, {ae.WHISPER_TURBO: whisper}):
            folder = self.stage(whisper)
            fooled(folder)
            write_files(ae.model_dir(whisper, self.cache), GOOD)
            import types
            fw = types.ModuleType("faster_whisper")
            opened = []

            def model(path, **kwargs):
                if Path(path) == folder:
                    raise RuntimeError("synthetic: model is damaged")
                opened.append(Path(path))
                return "model"
            fw.WhisperModel = model
            with machine(self.root), mock.patch.dict(sys.modules, faster_whisper=fw):
                with user_models(True):
                    self.assertEqual(engines._whisper_model(whisper, self.cache, 1), "model")
                self.assertEqual(opened, [ae.model_dir(whisper, self.cache)])
                ae._machine_failed.clear()
                with user_models(False), self.assertRaises(ae.ModelLoadError) as caught:
                    engines._whisper_model(whisper, self.cache, 1)
                self.assertEqual(str(caught.exception),
                                 "Whisper large-v3 turbo is not installed or failed "
                                 f"verification. {ae.machine_damaged(whisper)}")

    def test_downloads_names_repair_only_where_it_is_offered(self):
        invalid = {"state": "invalid", "status": setup.INVALID}
        self.assertEqual(setup.shown_status(invalid, True), setup.INVALID)
        self.assertEqual(setup.shown_status(invalid, False), setup.INVALID_ASK_IT)
        gpu_invalid = {"state": "invalid", "status": "Damaged or incomplete. Repair downloads "
                       "a fresh, verified copy; the damaged copy is kept aside."}
        self.assertEqual(setup.shown_status(gpu_invalid, False), setup.INVALID_ASK_IT)
        it = {"state": "invalid", "status": setup.IT_COPY_DAMAGED}
        for offered in (True, False):
            self.assertEqual(setup.shown_status(it, offered), setup.IT_COPY_DAMAGED)
        for state in ("ready", "missing"):
            card = {"state": state, "status": "Not downloaded yet."}
            self.assertEqual(setup.shown_status(card, False), "Not downloaded yet.")
        self.assertNotIn("Repair", setup.INVALID_ASK_IT + setup.IT_COPY_DAMAGED)

if __name__ == "__main__":
    unittest.main()
