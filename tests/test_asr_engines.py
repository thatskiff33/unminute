# Model-free tests for asr_engines: sentence splitting, integrity checks and
# install/repair behaviour, using tiny synthetic "model" files. Stdlib only, so
# they run in CI without torch, onnxruntime or any downloads.
#   python -m unittest discover -s tests -v

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asr_engines as ae  # noqa: E402

GOOD = {"config.json": b'{"model_type": "fake"}', "weights.onnx": b"\x01\x02" * 500}


def make_spec(contents=GOOD, name="fake-model"):
    files = tuple(ae.ModelFile(n, len(d), hashlib.sha256(d).hexdigest())
                  for n, d in contents.items())
    return ae.ModelSpec(name=name, folder=name, onnx_asr_name=name,
                        repo="example/fake", revision="0" * 40, files=files)


def write_files(folder, contents):
    folder.mkdir(parents=True, exist_ok=True)
    for n, d in contents.items():
        (folder / n).write_bytes(d)


class FakeDownload:
    """Stands in for snapshot_download; records calls, writes given contents."""

    def __init__(self, contents=GOOD, fail=False):
        self.contents, self.fail, self.calls = contents, fail, 0

    def __call__(self, spec, dest):
        self.calls += 1
        write_files(Path(dest), self.contents)
        if self.fail:
            raise OSError("network down")


class SplitSentencesTests(unittest.TestCase):
    def test_splits_on_sentence_end_with_absolute_times(self):
        tokens = [" Hello", " there", ".", " How", " are", " you", "?"]
        times = [0.0, 0.3, 0.6, 1.0, 1.2, 1.4, 1.6]
        segs = ae.split_sentences(10.0, 12.0, tokens, times, "Hello there. How are you?")
        self.assertEqual(segs, [ae.Segment(10.0, 11.0, "Hello there."),
                                ae.Segment(11.0, 12.0, "How are you?")])

    def test_decimal_point_does_not_split(self):
        tokens = [" It", " was", " 3", ".", "5", " percent", "."]
        times = [0.0, 0.2, 0.4, 0.5, 0.6, 0.7, 1.0]
        segs = ae.split_sentences(0.0, 1.2, tokens, times, "It was 3.5 percent.")
        self.assertEqual([s.text for s in segs], ["It was 3.5 percent."])

    def test_text_without_final_punctuation_is_kept(self):
        segs = ae.split_sentences(5.0, 6.0, [" and", " so"], [0.0, 0.4], "and so")
        self.assertEqual(segs, [ae.Segment(5.0, 6.0, "and so")])

    def test_missing_timings_fall_back_to_one_segment(self):
        segs = ae.split_sentences(1.0, 2.0, [], [], " Whole piece. ")
        self.assertEqual(segs, [ae.Segment(1.0, 2.0, "Whole piece.")])

    def test_empty_text_yields_nothing(self):
        self.assertEqual(ae.split_sentences(0.0, 1.0, [" "], [0.0], "  "), [])


class CheckFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "m"
        self.spec = make_spec()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_folder(self):
        self.assertEqual(ae.check_files(self.spec, self.folder), ["not installed"])

    def test_empty_folder_is_not_ready(self):
        # The original failure mode: a folder existing is not proof of a model.
        self.folder.mkdir()
        problems = ae.check_files(self.spec, self.folder)
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("missing" in p for p in problems))

    def test_wrong_size(self):
        write_files(self.folder, {**GOOD, "weights.onnx": b"short"})
        self.assertIn("wrong size", ae.check_files(self.spec, self.folder)[0])

    def test_same_size_wrong_content_fails_checksum(self):
        write_files(self.folder, {**GOOD, "weights.onnx": b"\x09\x09" * 500})
        self.assertIn("checksum", ae.check_files(self.spec, self.folder)[0])

    def test_valid_files_write_a_marker_then_pass_quickly(self):
        write_files(self.folder, GOOD)
        self.assertEqual(ae.check_files(self.spec, self.folder), [])
        self.assertTrue((self.folder / ae.VERIFIED_MARKER).is_file())
        self.assertEqual(ae.check_files(self.spec, self.folder), [])

    def test_change_after_verification_is_caught(self):
        write_files(self.folder, GOOD)
        self.assertEqual(ae.check_files(self.spec, self.folder), [])
        # Same size, different bytes, newer mtime: the marker must not be trusted.
        p = self.folder / "weights.onnx"
        p.write_bytes(b"\x07\x07" * 500)
        later = time.time() + 5
        os.utime(p, (later, later))
        self.assertIn("checksum", ae.check_files(self.spec, self.folder)[0])

    def test_quick_check_trusts_a_restored_date_but_full_check_does_not(self):
        # The attack the speaker model's full re-hash closes (#111 A4): same
        # size, different bytes, and the old modification time put back.
        write_files(self.folder, GOOD)
        self.assertEqual(ae.check_files(self.spec, self.folder), [])
        p = self.folder / "weights.onnx"
        before = p.stat()
        p.write_bytes(b"\x07\x07" * 500)
        os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(ae.check_files(self.spec, self.folder), [])      # quick: fooled
        self.assertIn("checksum", ae.check_files(self.spec, self.folder, full=True)[0])

    def test_marker_for_another_revision_is_not_trusted(self):
        write_files(self.folder, GOOD)
        ae.check_files(self.spec, self.folder)
        other = dataclasses.replace(self.spec, revision="1" * 40)
        self.assertEqual(ae.check_files(other, self.folder), [])   # re-hashed
        marker = json.loads((self.folder / ae.VERIFIED_MARKER).read_text(encoding="utf-8"))
        self.assertEqual(marker["revision"], "1" * 40)

    def test_malformed_marker_is_ignored(self):
        write_files(self.folder, GOOD)
        (self.folder / ae.VERIFIED_MARKER).write_text("{not json", encoding="utf-8")
        self.assertEqual(ae.check_files(self.spec, self.folder), [])


class InstallModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        self.spec = make_spec()
        self.target = ae.model_dir(self.spec, self.cache)
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def siblings(self, tag):
        return [p for p in self.target.parent.iterdir() if tag in p.name]

    def test_fresh_install_then_repeat_is_a_no_op(self):
        dl = FakeDownload()
        self.assertEqual(ae.install_model(self.spec, self.cache, self.logs.append, dl), "installed")
        self.assertEqual(ae.check_files(self.spec, self.target), [])
        self.assertEqual(ae.install_model(self.spec, self.cache, self.logs.append, dl), "ok")
        self.assertEqual(dl.calls, 1)
        self.assertEqual(self.siblings("~dl-"), [])

    def test_damaged_install_is_kept_aside_and_replaced(self):
        write_files(self.target, {**GOOD, "weights.onnx": b"\x09\x09" * 500})
        dl = FakeDownload()
        self.assertEqual(ae.install_model(self.spec, self.cache, self.logs.append, dl), "repaired")
        self.assertEqual(ae.check_files(self.spec, self.target, full=True), [])
        kept = self.siblings(".invalid-")
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / "weights.onnx").read_bytes(), b"\x09\x09" * 500)
        # Repeating the repair leaves the good install alone.
        self.assertEqual(ae.install_model(self.spec, self.cache, self.logs.append, dl), "ok")
        self.assertEqual(dl.calls, 1)

    def test_empty_folder_is_repaired(self):
        self.target.mkdir(parents=True)
        self.assertEqual(ae.install_model(self.spec, self.cache, self.logs.append,
                                          FakeDownload()), "repaired")

    def test_bad_download_changes_nothing(self):
        write_files(self.target, {"config.json": GOOD["config.json"]})  # incomplete
        bad = FakeDownload({**GOOD, "weights.onnx": b"\x09\x09" * 500})
        with self.assertRaises(ae.ModelLoadError):
            ae.install_model(self.spec, self.cache, self.logs.append, bad)
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["config.json"])
        self.assertEqual(self.siblings("~dl-"), [])
        self.assertEqual(self.siblings(".invalid-"), [])

    def test_interrupted_download_is_cleaned_up_with_actionable_error(self):
        with self.assertRaises(ae.ModelLoadError) as ctx:
            ae.install_model(self.spec, self.cache, self.logs.append, FakeDownload(fail=True))
        self.assertIn("network down", str(ctx.exception))
        self.assertIn("try again", str(ctx.exception))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.siblings("~dl-"), [])

    def test_leftover_staging_and_download_metadata_are_removed(self):
        staging = ae.staging_dir(self.spec, self.cache)
        staging.mkdir(parents=True)
        (staging / "stale.bin").write_bytes(b"from an interrupted run")

        def download(spec, dest):
            meta = Path(dest) / ".cache" / "huggingface" / "download"
            meta.mkdir(parents=True)
            (meta / "weights.onnx.metadata").write_text("x", encoding="utf-8")
            write_files(Path(dest), GOOD)

        self.assertEqual(ae.install_model(self.spec, self.cache, self.logs.append, download),
                         "installed")
        self.assertFalse((self.target / ".cache").exists())
        self.assertFalse((self.target / "stale.bin").exists())
        self.assertFalse(staging.exists())

    def test_staging_path_stays_short(self):
        # Worst real case: huggingface_hub temp file inside the staging folder.
        spec = ae.PARAKEET_MODELS["parakeet-tdt-0.6b-v2"]
        hf_temp = Path(".cache/huggingface/download") / ("x" * 113)
        rel = ae.staging_dir(spec, "C") / hf_temp
        self.assertLess(len(str(rel)), 170)


class ReviewFixTests(unittest.TestCase):
    """Behaviour added after independent review of PR #37."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        self.spec = make_spec()
        self.target = ae.model_dir(self.spec, self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def test_join_tokens_handles_bare_space_tokens(self):
        self.assertEqual(ae.join_tokens([" Hello", " ", ",", " world", " ", "."]),
                         "Hello, world.")
        self.assertEqual(ae.join_tokens([" were", " $", "412", ",", "000"]), "were $412,000")

    def test_checksum_failure_is_recorded_and_cleared_by_new_files(self):
        write_files(self.target, {**GOOD, "weights.onnx": b"\x09\x09" * 500})
        self.assertTrue(ae.check_files(self.spec, self.target, full=True))
        self.assertIn("checksum", ae._recorded_failure(self.spec, self.target)[0])
        # Quick checks keep failing (no silent "ready") until the files change.
        self.assertTrue(ae.check_files(self.spec, self.target))
        write_files(self.target, GOOD)
        later = time.time() + 5
        os.utime(self.target / "weights.onnx", (later, later))
        self.assertEqual(ae._recorded_failure(self.spec, self.target), [])
        self.assertEqual(ae.check_files(self.spec, self.target), [])

    def test_failed_final_move_restores_old_folder_and_reuses_download(self):
        write_files(self.target, {**GOOD, "weights.onnx": b"\x09\x09" * 500})
        real_rename = Path.rename
        calls = {"n": 0}

        def flaky_rename(self_path, dest):
            if self_path.name.startswith("~dl-") and calls["n"] == 0:
                calls["n"] += 1
                raise PermissionError("file in use")
            return real_rename(self_path, dest)

        dl = FakeDownload()
        Path.rename = flaky_rename
        try:
            with self.assertRaises(ae.ModelLoadError):
                ae.install_model(self.spec, self.cache, print, dl)
        finally:
            Path.rename = real_rename
        self.assertTrue(self.target.exists())                     # old folder back
        self.assertTrue(ae.staging_dir(self.spec, self.cache).exists())
        self.assertEqual(ae.install_model(self.spec, self.cache, print, dl), "repaired")
        self.assertEqual(dl.calls, 1)                              # download reused
        self.assertEqual(ae.check_files(self.spec, self.target, full=True), [])

    def test_without_a_downloader_it_only_verifies(self):
        # There is no in-process downloader: a missing model needs the
        # app's download process, and nothing is written meanwhile.
        with self.assertRaises(ae.ModelLoadError) as ctx:
            ae.install_model(self.spec, self.cache, lambda message: None)
        self.assertIn("Setup", str(ctx.exception))
        self.assertFalse(self.target.exists())
        self.assertFalse(ae.staging_dir(self.spec, self.cache).exists())
        self.assertFalse(hasattr(ae, "_download"))

    def test_only_the_manifest_command_remains(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            ae._main(["ensure", "--present", "--cache", str(self.cache)])


def lock_error(winerror):
    """The error Windows gives a folder rename while a file in it is open:
    access denied (5) or a sharing violation (32). The code is set by hand so
    the test runs on any system."""
    exc = PermissionError(13, "Access is denied")
    exc.winerror = winerror
    return exc


class FakeClock:
    """Stands in for the retry's clock and sleep, so no test waits."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class LockRetryTests(unittest.TestCase):
    """#249: moving a verified model into place waits out a file in use for a
    moment (antivirus scanning the new files), as the installer does (#239)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.spec = make_spec()
        self.target = ae.model_dir(self.spec, self.cache)
        self.staging = ae.staging_dir(self.spec, self.cache)
        self.clock = FakeClock()
        for name, value in (("_lock_clock", self.clock), ("_lock_sleep", self.clock.sleep)):
            patcher = mock.patch.object(ae, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []

    def lock(self, refusals):
        """Refuse renames of the folders named in `refusals` ({name: (error,
        times)}, times None for always); every other rename is real."""
        real = Path.rename
        left = {name: times for name, (_, times) in refusals.items()}

        def rename(path, dest):
            name = Path(path).name
            self.calls.append(name)
            if name in refusals and (left[name] is None or left[name] > 0):
                if left[name] is not None:
                    left[name] -= 1
                raise refusals[name][0]
            return real(path, dest)

        patcher = mock.patch.object(Path, "rename", rename)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def damaged_target(self):
        write_files(self.target, {**GOOD, "weights.onnx": b"\x09\x09" * 500})

    def kept(self):
        return [p for p in self.target.parent.iterdir() if ".invalid-" in p.name]

    def test_a_file_in_use_for_a_moment_is_waited_for(self):
        self.damaged_target()
        self.lock({self.target.name: (lock_error(5), 2),
                   self.staging.name: (lock_error(32), 3)})
        dl = FakeDownload()
        self.assertEqual(ae.install_model(self.spec, self.cache, lambda m: None, dl), "repaired")
        self.assertEqual(ae.check_files(self.spec, self.target, full=True), [])
        self.assertEqual(len(self.kept()), 1)
        self.assertEqual((self.kept()[0] / "weights.onnx").read_bytes(), b"\x09\x09" * 500)
        self.assertFalse(self.staging.exists())
        # The same two folders each time: the damaged copy aside, then the download in.
        self.assertEqual(self.calls, [self.target.name] * 3 + [self.staging.name] * 4)
        self.assertEqual(self.clock.sleeps, [0.25, 0.5, 0.25, 0.5, 1.0])   # per rename

    def test_a_file_still_in_use_fails_after_30_seconds_with_the_same_message(self):
        self.damaged_target()
        patcher = self.lock({self.staging.name: (lock_error(32), None)})
        dl = FakeDownload()
        with self.assertRaises(ae.ModelLoadError) as ctx:
            ae.install_model(self.spec, self.cache, lambda m: None, dl)
        self.assertIn("Could not move the verified", str(ctx.exception))
        self.assertIn("Wait a minute and try the download again", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, PermissionError)
        # Bounded: 30 seconds from the first refusal, pauses doubling to 4 s.
        self.assertAlmostEqual(sum(self.clock.sleeps), ae.LOCK_RETRY_SECONDS)
        self.assertAlmostEqual(self.clock.now - 1000.0, ae.LOCK_RETRY_SECONDS)
        self.assertEqual(self.clock.sleeps[:6], [0.25, 0.5, 1.0, 2.0, 4.0, 4.0])
        self.assertLessEqual(max(self.clock.sleeps), 4.0)
        # Today's handling: the damaged folder is put back, the download kept.
        self.assertEqual((self.target / "weights.onnx").read_bytes(), b"\x09\x09" * 500)
        self.assertEqual(self.kept(), [])
        self.assertEqual(ae.check_files(self.spec, self.staging, full=True), [])
        patcher.stop()
        self.assertEqual(ae.install_model(self.spec, self.cache, lambda m: None, dl), "repaired")
        self.assertEqual(dl.calls, 1)                              # download reused

    def test_the_damaged_copy_still_in_use_fails_after_30_seconds_as_today(self):
        self.damaged_target()
        self.lock({self.target.name: (lock_error(5), None)})
        with self.assertRaises(PermissionError) as ctx:
            ae.install_model(self.spec, self.cache, lambda m: None, FakeDownload())
        self.assertEqual(ctx.exception.winerror, 5)
        self.assertAlmostEqual(sum(self.clock.sleeps), ae.LOCK_RETRY_SECONDS)
        self.assertNotIn(self.staging.name, self.calls)            # never moved in
        self.assertEqual((self.target / "weights.onnx").read_bytes(), b"\x09\x09" * 500)
        self.assertEqual(self.kept(), [])

    def test_any_other_error_fails_at_once(self):
        errors = {"exists (183)": FileExistsError(17, "exists"),
                  "no Windows code": PermissionError(13, "denied"),
                  "not found (2)": FileNotFoundError(2, "gone"),
                  "lock code on a plain value": ValueError("winerror 5")}
        errors["exists (183)"].winerror = 183
        errors["not found (2)"].winerror = 2
        for label_, error in errors.items():
            with self.subTest(label_):
                self.calls.clear()
                self.clock.sleeps.clear()
                patcher = self.lock({"x": (error, None)})
                src = self.cache / "x"
                src.mkdir(exist_ok=True)
                with self.assertRaises(type(error)):
                    ae.rename_with_retry(src, self.cache / "y")
                patcher.stop()
                self.assertEqual(self.calls, ["x"])
                self.assertEqual(self.clock.sleeps, [])
                self.assertTrue(src.is_dir())

    def test_any_other_error_moving_the_download_in_fails_at_once(self):
        self.damaged_target()
        error = FileExistsError(17, "exists")
        error.winerror = 183
        self.lock({self.staging.name: (error, None)})
        with self.assertRaises(ae.ModelLoadError):
            ae.install_model(self.spec, self.cache, lambda m: None, FakeDownload())
        self.assertEqual(self.calls.count(self.staging.name), 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertTrue(self.target.is_dir())                      # put back, as today

    @unittest.skipUnless(os.name == "nt", "Windows file locking")
    def test_a_real_open_file_is_waited_for(self):
        # A file held open in the download stops Windows renaming the folder;
        # the first pause closes it, as a finished virus scan would.
        held = []
        test = self

        class Holding(FakeDownload):
            def __call__(self, spec, dest):
                super().__call__(spec, dest)
                held.append(open(Path(dest) / "weights.onnx", "rb"))
                test.addCleanup(held[0].close)

        first_error = []
        real_rename = Path.rename

        def rename(path, dest):
            try:
                return real_rename(path, dest)
            except OSError as exc:
                first_error.append(exc)
                raise

        def release(seconds):
            held[0].close()
            self.clock.sleep(seconds)

        with mock.patch.object(ae, "_lock_sleep", release), \
                mock.patch.object(Path, "rename", rename):
            self.assertEqual(ae.install_model(self.spec, self.cache, lambda m: None,
                                              Holding()), "installed")
        self.assertTrue(first_error)
        self.assertIn(first_error[0].winerror, (5, 32))           # what Windows really says
        self.assertEqual(self.clock.sleeps, [0.25])
        self.assertEqual(ae.check_files(self.spec, self.target, full=True), [])


class ParakeetStatusTests(unittest.TestCase):
    def test_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = ae.RECOMMENDED_PARAKEET
            self.assertEqual(ae.parakeet_status(name, tmp)[0], "missing")
            ae.model_dir(ae.PARAKEET_MODELS[name], tmp).mkdir(parents=True)
            state, detail = ae.parakeet_status(name, tmp)
            self.assertEqual(state, "invalid")
            self.assertIn("encoder-model.int8.onnx is missing", detail)
            self.assertIn("speech detector", detail)


class ManifestTests(unittest.TestCase):
    def test_pinned_manifests_are_complete(self):
        for spec in (*ae.PARAKEET_MODELS.values(), ae.SILERO_VAD):
            self.assertRegex(spec.revision, r"^[0-9a-f]{40}$")
            names = {f.name for f in spec.files}
            self.assertIn("config.json", names)
            for f in spec.files:
                self.assertRegex(f.sha256, r"^[0-9a-f]{64}$")
                self.assertGreater(f.size, 0)
        for spec in ae.PARAKEET_MODELS.values():
            self.assertEqual(spec.quantization, "int8")
            self.assertTrue({"encoder-model.int8.onnx", "decoder_joint-model.int8.onnx",
                             "vocab.txt"} <= {f.name for f in spec.files})


class EngineTests(unittest.TestCase):
    def test_whisper_engine_maps_segments_and_keeps_settings(self):
        class Word:
            start, end, word = 0.1, 0.4, " Hi."

        class Seg:
            def __init__(self, s, e, t):
                self.start, self.end, self.text, self.words = s, e, t, [Word()]

        class Info:
            duration = 42.0

        class FakeWhisper:
            def transcribe(self, audio, **kw):
                self.kw = kw
                return iter([Seg(0.0, 1.0, " Hi. ")]), Info()

        fw = FakeWhisper()
        engine = ae.WhisperEngine(fw, "large-v3-turbo", "int8", "en")
        segs, duration = engine.transcribe("x.wav")
        self.assertEqual(list(segs), [ae.Segment(0.0, 1.0, "Hi.", ((0.1, 0.4, " Hi."),))])
        self.assertEqual(duration, 42.0)
        self.assertEqual(fw.kw, {"language": "en", "vad_filter": True, "beam_size": 5,
                                 "word_timestamps": True})

    def test_parakeet_refuses_to_load_incomplete_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            ae.model_dir(ae.PARAKEET_MODELS[ae.RECOMMENDED_PARAKEET], tmp).mkdir(parents=True)
            engine = ae.ParakeetEngine(ae.RECOMMENDED_PARAKEET, tmp, 2, decode_audio=None)
            with self.assertRaises(ae.ModelLoadError) as ctx:
                engine.load()
            self.assertIn("Manage downloads", str(ctx.exception))

    def test_parakeet_vad_bridges_pauses_up_to_one_second(self):
        # #107: onnx-asr's default (100 ms of silence ends a piece) cut meetings
        # into pieces too short for Parakeet. The load must pass the 1 s pause
        # bridge and keep the 20 s cap. ONNX is stubbed; the load is real.
        calls = []

        class FakeAsr:
            def with_vad(self, vad, **kw):
                calls.append(("with_vad", vad, kw))
                return self

            def with_timestamps(self):
                calls.append(("with_timestamps",))
                return self

        vad = object()
        onnx_asr = types.ModuleType("onnx_asr")
        onnx_asr.load_model = lambda *a, **k: FakeAsr()
        onnx_asr.load_vad = lambda *a, **k: vad

        class Options:
            def add_session_config_entry(self, *a):
                pass

        onnxruntime = types.ModuleType("onnxruntime")
        onnxruntime.disable_telemetry_events = lambda: None
        onnxruntime.SessionOptions = Options
        with mock.patch.dict(sys.modules, onnx_asr=onnx_asr, onnxruntime=onnxruntime), \
                mock.patch.object(ae, "check_files", return_value=[]):
            engine = ae.ParakeetEngine(ae.RECOMMENDED_PARAKEET, ".", 2).load()
        self.assertIsInstance(engine._asr, FakeAsr)
        self.assertEqual(calls, [
            ("with_vad", vad, {"batch_size": 1, "max_speech_duration_s": 20.0,
                               "min_silence_duration_ms": 1000}),
            ("with_timestamps",)])


# --- Whisper model integrity (the "expected 128 mel bands, got 80" failure) ---

def preproc(feature_size=128, raw=None):
    if raw is not None:
        return raw
    return json.dumps({"chunk_length": 30, "feature_size": feature_size,
                       "hop_length": 160, "n_fft": 400, "sampling_rate": 16000}).encode()


W_GOOD = {"config.json": b'{"num_mel_bins": 128}', "model.bin": b"\x05" * 2000,
          "preprocessor_config.json": preproc(128)}


def whisper_spec(contents=W_GOOD, n_mels=128, name="fake-whisper"):
    base = make_spec(contents, name)
    return dataclasses.replace(base, onnx_asr_name=None, family="whisper", n_mels=n_mels,
                               hub_repos=("org/fake-whisper",))


def hub_snapshot(cache, rev="a" * 40):
    return Path(cache) / "models--org--fake-whisper" / "snapshots" / rev


def tree(folder):
    """{relative path: bytes} for every file under folder (to prove it is untouched)."""
    return {str(p.relative_to(folder)): p.read_bytes()
            for p in Path(folder).rglob("*") if p.is_file()}


class WhisperIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        self.spec = whisper_spec()
        ae.WHISPER_MODELS[self.spec.name] = self.spec   # registered for status/CLI helpers
        ae.MANAGED_MODELS[self.spec.name] = self.spec
        self.logs = []

    def tearDown(self):
        ae.WHISPER_MODELS.pop(self.spec.name, None)
        ae.MANAGED_MODELS.pop(self.spec.name, None)
        self.tmp.cleanup()

    def status(self):
        return ae.hub_model_status(self.spec.name, self.cache)

    def test_missing(self):
        self.assertEqual(self.status(), ("missing", "not installed"))

    def test_empty_hub_folder_is_not_ready(self):
        # The original bug: a cache folder existing was treated as "downloaded".
        hub_snapshot(self.cache).mkdir(parents=True)
        state, detail = self.status()
        self.assertEqual(state, "invalid")
        self.assertIn("model.bin is missing", detail)

    def test_missing_preprocessor_config(self):
        write_files(hub_snapshot(self.cache),
                    {k: v for k, v in W_GOOD.items() if k != "preprocessor_config.json"})
        state, detail = self.status()
        self.assertEqual(state, "invalid")
        self.assertIn("preprocessor_config.json is missing", detail)

    def test_malformed_preprocessor_config(self):
        write_files(hub_snapshot(self.cache),
                    {**W_GOOD, "preprocessor_config.json": b'{"feature_size": 12'})
        self.assertIn("not valid JSON", self.status()[1])

    def test_preprocessor_config_without_feature_size(self):
        write_files(hub_snapshot(self.cache),
                    {**W_GOOD, "preprocessor_config.json": b'{"sampling_rate": 16000}'})
        self.assertIn("has no feature_size", self.status()[1])

    def test_preprocessor_config_with_wrong_feature_size(self):
        write_files(hub_snapshot(self.cache),
                    {**W_GOOD, "preprocessor_config.json": preproc(80)})
        self.assertIn("feature_size 80, but fake-whisper needs 128", self.status()[1])

    def test_same_size_valid_looking_config_caught_by_checksum(self):
        # Still valid JSON with feature_size 128 and the same size, but other
        # bytes: only the checksum can tell, and it must.
        bad = W_GOOD["preprocessor_config.json"].replace(b"16000", b"16001")
        self.assertEqual(len(bad), len(W_GOOD["preprocessor_config.json"]))
        write_files(hub_snapshot(self.cache), {**W_GOOD, "preprocessor_config.json": bad})
        self.assertEqual(self.status(), ("ready", ""))              # quick check passes
        self.assertIsNone(ae.find_verified(self.spec, self.cache))
        self.assertIn("preprocessor_config.json does not match", self.status()[1])

    def test_valid_hub_cache_is_used_without_writing_to_it(self):
        snap = hub_snapshot(self.cache)
        write_files(snap, W_GOOD)
        before = tree(snap.parents[1])
        self.assertEqual(self.status(), ("ready", ""))
        self.assertEqual(ae.find_verified(self.spec, self.cache, full=True), snap)
        self.assertEqual(tree(snap.parents[1]), before)            # hub cache untouched
        self.assertTrue(any((self.cache / "whisper" / ".verified").iterdir()))
        dl = FakeDownload(W_GOOD)
        self.assertEqual(ae.install_model(self.spec, self.cache, self.logs.append, dl), "ok")
        self.assertEqual(dl.calls, 0)

    def test_broken_hub_cache_is_kept_and_a_verified_copy_installed(self):
        snap = hub_snapshot(self.cache)
        write_files(snap, {**W_GOOD, "preprocessor_config.json": preproc(80)})
        before = tree(snap.parents[1])
        dl = FakeDownload(W_GOOD)
        self.assertEqual(ae.install_model(self.spec, self.cache, self.logs.append, dl),
                         "installed")
        self.assertEqual(tree(snap.parents[1]), before)            # preserved for diagnosis
        self.assertTrue(any("left untouched" in m for m in self.logs))
        managed = ae.model_dir(self.spec, self.cache)
        self.assertEqual(ae.find_verified(self.spec, self.cache), managed)
        self.assertEqual(self.status(), ("ready", ""))
        # Repeat repair: nothing more to do, no second download.
        self.assertEqual(ae.install_model(self.spec, self.cache, self.logs.append, dl), "ok")
        self.assertEqual(dl.calls, 1)

    def test_checksum_failure_in_hub_cache_shows_needs_repair(self):
        # Right sizes, wrong bytes: quick checks pass until a full check fails.
        snap = hub_snapshot(self.cache)
        write_files(snap, {**W_GOOD, "model.bin": b"\x06" * 2000})
        before = tree(snap.parents[1])
        self.assertEqual(self.status(), ("ready", ""))
        self.assertIsNone(ae.find_verified(self.spec, self.cache, full=True))
        state, detail = self.status()
        self.assertEqual(state, "invalid")
        self.assertIn("model.bin does not match the expected checksum", detail)
        self.assertEqual(tree(snap.parents[1]), before)            # nothing written there

    def test_known_bad_copy_is_skipped_not_rehashed(self):
        managed = ae.model_dir(self.spec, self.cache)
        write_files(managed, {**W_GOOD, "model.bin": b"\x06" * 2000})   # bad, sizes ok
        write_files(hub_snapshot(self.cache), W_GOOD)                    # good
        self.assertTrue(ae.check_files(self.spec, managed, full=True))  # fails, recorded
        hashed = []
        real = ae._sha256
        ae._sha256 = lambda p, *a: (hashed.append(Path(p).parent.name), real(p, *a))[1]
        try:
            found = ae.find_verified(self.spec, self.cache)
        finally:
            ae._sha256 = real
        self.assertEqual(found, hub_snapshot(self.cache))
        self.assertNotIn(managed.name, hashed)

    def test_install_dispatches_whisper(self):
        dl = FakeDownload(W_GOOD)
        self.assertEqual(ae.install(self.spec.name, self.cache, self.logs.append, dl),
                         {self.spec.name: "installed"})

    def test_other_models_are_checked_against_their_own_width(self):
        # Not every model needs 128 bands: an 80-band model validates as 80.
        spec80 = whisper_spec({**W_GOOD, "preprocessor_config.json": preproc(80)},
                              n_mels=80, name="fake-80")
        write_files(ae.model_dir(spec80, self.cache), {**W_GOOD,
                                                       "preprocessor_config.json": preproc(80)})
        self.assertEqual(ae.check_files(spec80, ae.model_dir(spec80, self.cache)), [])


class FeatureWidthTests(unittest.TestCase):
    """feature_width_problem() against stand-ins for WhisperModel's attributes."""

    @staticmethod
    def model(produced, expected):
        class Arr:
            shape = (produced, 201)

        class FE:
            mel_filters = Arr()

        class CT2:
            n_mels = expected

        class M:
            feature_extractor = FE()
            model = CT2()
        return M()

    def test_turbo_with_128_is_fine(self):
        self.assertIsNone(ae.feature_width_problem(self.model(128, 128),
                                                   ae.WHISPER_MODELS["large-v3-turbo"]))

    def test_fallback_to_80_is_caught(self):
        msg = ae.feature_width_problem(self.model(80, 128), ae.WHISPER_MODELS["large-v3-turbo"])
        self.assertIn("expects 128 mel bands but its preprocessing settings produce 80", msg)

    def test_model_reporting_unexpected_width_is_caught(self):
        msg = ae.feature_width_problem(self.model(80, 80), ae.WHISPER_MODELS["large-v3"])
        self.assertIn("should use 128", msg)

    def test_unpinned_80_band_model_is_not_forced_to_128(self):
        spec = dataclasses.replace(ae.WHISPER_MODELS["large-v3"], n_mels=None)
        self.assertIsNone(ae.feature_width_problem(self.model(80, 80), spec))


class SpeakerModelAndManifestDocTests(unittest.TestCase):
    def test_speaker_model_is_pinned_gated_and_managed(self):
        s = ae.SPEAKER_MODEL
        self.assertTrue(s.gated)
        self.assertIn(s.name, ae.MANAGED_MODELS)
        self.assertEqual({f.name for f in s.files},
                         {"config.yaml", "embedding/pytorch_model.bin", "plda/plda.npz",
                          "plda/xvec_transform.npz", "segmentation/pytorch_model.bin"})
        for f in s.files:
            self.assertRegex(f.sha256, r"^[0-9a-f]{64}$")

    def test_damaged_speaker_cache_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = (Path(tmp) / "models--pyannote--speaker-diarization-community-1"
                    / "snapshots" / ae.SPEAKER_MODEL.revision)
            (snap / "embedding").mkdir(parents=True)
            (snap / "config.yaml").write_text("pipeline: {}", encoding="utf-8")
            state, detail = ae.hub_model_status(ae.SPEAKER_MODEL.name, tmp)
            self.assertEqual(state, "invalid")
            self.assertIn("embedding/pytorch_model.bin is missing", detail)

    def test_docs_manifest_matches_code(self):
        doc = Path(__file__).resolve().parents[1] / "docs" / "MODEL_MANIFEST.md"
        self.assertEqual(doc.read_text(encoding="utf-8").replace("\r\n", "\n"),
                         ae.manifest_markdown(),
                         "Regenerate: python asr_engines.py manifest --out docs/MODEL_MANIFEST.md")

    def test_gated_repair_without_token_says_so_and_downloads_nothing(self):
        # A token in the environment is never used: the caller says it has one.
        dl = FakeDownload()
        saved = os.environ.get("HF_TOKEN")
        os.environ["HF_TOKEN"] = "hf_example"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ae.ModelLoadError) as ctx:
                    ae.install_model(ae.SPEAKER_MODEL, tmp, print, dl)
            self.assertIn("Hugging Face token", str(ctx.exception))
            self.assertEqual(dl.calls, 0)
        finally:
            if saved is None:
                os.environ.pop("HF_TOKEN", None)
            else:
                os.environ["HF_TOKEN"] = saved


class WhisperManifestTests(unittest.TestCase):
    def test_supported_whisper_models_are_pinned_to_128_bands(self):
        for spec in ae.WHISPER_MODELS.values():
            self.assertEqual(spec.family, "whisper")
            self.assertEqual(spec.n_mels, 128)
            names = {f.name for f in spec.files}
            self.assertTrue({"model.bin", "config.json", "preprocessor_config.json",
                             "tokenizer.json", "vocabulary.json"} <= names)
            self.assertTrue(spec.hub_repos)


if __name__ == "__main__":
    unittest.main()
