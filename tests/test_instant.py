# Instant live words while recording (#105), back end: the streaming engine's
# features and decoding with a stand-in model, the instant-words worker's loop,
# its session (start after the draft worker, restart once, pause while hidden,
# stop), the draft session and workspace wiring (D4's exception during a
# recording only), and the shell telling the workspace of a minimised window.
# Synthetic audio only: tones are "speech", zeros are silence. The real model
# never runs here (CI does not run models): docs/ENGINE_EVALUATION.md and
# tools/live_trial.py --instant measure it on public audio.
#   python tools/run_tests.py --only test_instant

import importlib.util
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))      # fake_worker_engines

from workspace import consent  # noqa: E402  (stdlib only)

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))

if HAVE_DEPS:
    import numpy as np

    from workspace import asr_engines, diag, drafts, engines, instant, instant_engine, recorder
    from workspace.app import Workspace

FAKE = "test:fake_worker_engines:"
SR = 16000


sys.path.append(str(Path(__file__).resolve().parents[1] / "tools"))
import decoder_runtime  # noqa: E402  (tools/decoder_runtime.py)


def setUpModule():
    # #299: a recording's import decodes in an AppContainer that cannot read
    # this Python: a scratch runtime it can read and a throwaway profile.
    decoder_runtime.use()


def tearDownModule():
    decoder_runtime.stop()


def tone(seconds, rate=SR, freq=220.0, amp=0.25):
    t = np.arange(int(round(seconds * rate))) / rate
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)


def silence(seconds, rate=SR):
    return np.zeros(int(round(seconds * rate)), dtype=np.int16)


def wait_for(check, timeout=60.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(step)
    return bool(check())


# -- the engine's pure parts -----------------------------------------------------------

@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class FeatureTests(unittest.TestCase):
    def test_frames_come_as_soon_as_their_samples_have_arrived(self):
        # Frame f covers samples f * 160 - 120 to f * 160 + 279: the first
        # needs 280 samples, then one more every 160.
        fb = instant_engine.Fbank()
        self.assertEqual(fb.accept(np.zeros(279, np.float32)).shape, (0, 128))
        self.assertEqual(fb.accept(np.zeros(1, np.float32)).shape, (1, 128))
        self.assertEqual(fb.accept(np.zeros(159, np.float32)).shape, (0, 128))
        self.assertEqual(fb.accept(np.zeros(1, np.float32)).shape, (1, 128))

    def test_streaming_gives_the_same_frames_as_all_at_once(self):
        rng = np.random.default_rng(5)
        audio = (rng.standard_normal(SR * 2) * 0.1).astype(np.float32)
        whole = instant_engine.Fbank().accept(audio)
        fb = instant_engine.Fbank()
        parts = [fb.accept(audio[i:i + 1234]) for i in range(0, len(audio), 1234)]
        np.testing.assert_allclose(np.concatenate(parts), whole, rtol=1e-5, atol=1e-5)
        self.assertEqual(len(whole), (len(audio) - 280) // 160 + 1)

    def test_a_tone_peaks_in_its_own_mel_band(self):
        t = np.arange(SR) / SR
        audio = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        frames = instant_engine.Fbank().accept(audio)
        banks = instant_engine.mel_banks(128)
        self.assertEqual(int(np.argmax(frames[50])), int(np.argmax(banks[:, 32])))  # bin 32: 1 kHz

    def test_silence_is_the_log_guard(self):
        frames = instant_engine.Fbank().accept(np.zeros(SR, np.float32))
        np.testing.assert_allclose(frames, math.log(instant_engine.LOG_GUARD), rtol=1e-6)

    def test_mel_banks_are_slaney_normalised_triangles(self):
        banks = instant_engine.mel_banks(128)
        self.assertEqual(banks.shape, (128, 257))
        self.assertTrue((banks >= 0).all())
        self.assertTrue((banks.max(axis=1) > 0).all())     # no empty band

    def test_tokens_keep_the_word_marker_as_a_space_and_drop_special_ones(self):
        data = "<unk> 0\n▁the 1\ning 2\n<blk> 3\n".encode()
        self.assertEqual(instant_engine.read_tokens(data), {1: " the", 2: "ing"})


class FakeModel:
    """A stand-in for the three networks: window 65, shift 56, 8 times
    subsampling, blank 9. join() gives the scripted token for each encoder
    frame in turn (one per call until blank)."""

    window, shift, subsampling, blank, feat_dim = 65, 56, 8, 9, 128
    pred_hidden, pred_layers = 4, 2
    cache_shapes = ((1, 2, 3, 4), (1, 2, 4, 3))

    def __init__(self, script):
        self.script = list(script)            # per encoder frame: [token ids]
        self.tokens = {1: " one", 2: "ish", 3: " two"}
        self.encoded, self.predicted = [], []
        self.frame = -1
        self.emitted = 0

    def encode(self, feats, ch, t, ln):
        self.encoded.append((feats.shape, float(ch.sum()), int(ln[0])))
        n = 7
        enc = np.zeros((1, 2, n), np.float32)
        enc[0, 0, :] = np.arange(n) + len(self.encoded) * 100
        return enc, np.array([n]), ch + 1, t + 1, ln + 1

    def predict(self, token, state):
        self.predicted.append((token, float(state[0].sum())))
        out = np.full((1, 4, 1), token, np.float32)
        return out, (state[0] + 1, state[1] + 1)

    def join(self, enc_frame, dec_out):
        frame = int(enc_frame[0, 0, 0])
        if frame != self.frame:
            self.frame, self.emitted = frame, 0
        index = (frame // 100 - 1) * 7 + frame % 100
        todo = self.script[index] if index < len(self.script) else []
        if self.emitted < len(todo):
            self.emitted += 1
            return todo[self.emitted - 1]
        return self.blank


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class StreamTests(unittest.TestCase):
    def test_tokens_carry_their_encoder_frame_time_and_the_caches_go_on(self):
        script = [[1], [], [2], [3]] + [[]] * 3 + [[1, 2]]
        m = FakeModel(script)
        s = instant_engine.Stream(m)
        # 65 frames need 280 + 64 * 160 samples; then 56 more frames a chunk.
        first = 280 + 64 * 160
        self.assertEqual(s.accept(np.zeros(first - 1, np.float32)), [])
        out = s.accept(np.zeros(1, np.float32))
        # Encoder frame t: t * 8 feature frames of 10 ms.
        self.assertEqual(out, [(0.0, " one"), (0.16, "ish"), (0.24, " two")])
        self.assertEqual(m.encoded[0], ((65, 128), 0.0, 0))
        out = s.accept(np.zeros(56 * 160, np.float32))
        self.assertEqual(out, [(0.56, " one"), (0.56, "ish")])   # two tokens, one frame
        # The second call got the caches the first returned.
        self.assertEqual(m.encoded[1][1:], (float(np.prod((1, 2, 3, 4))), 1))

    def test_the_prediction_network_continues_from_its_returned_state(self):
        m = FakeModel([[1], [2]])
        s = instant_engine.Stream(m)
        s.accept(np.zeros(280 + 64 * 160, np.float32))
        # Blank at the start from a zero state, then each token from the
        # state returned with the output before it.
        self.assertEqual(m.predicted, [(9, 0.0), (1, 8.0), (2, 16.0)])

    def test_one_frame_gives_at_most_max_symbols_tokens(self):
        m = FakeModel([[1] * 20])
        s = instant_engine.Stream(m)
        out = s.accept(np.zeros(280 + 64 * 160, np.float32))
        self.assertEqual(len(out), instant_engine.MAX_SYMBOLS)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class EngineLoadTests(unittest.TestCase):
    def test_a_missing_model_does_not_load_and_names_no_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(asr_engines.ModelLoadError) as ctx:
                instant_engine.InstantEngine(tmp).load()
            self.assertIn("Nemotron streaming cannot be used for instant words",
                          str(ctx.exception))
            self.assertNotIn(tmp, str(ctx.exception))

    def test_the_model_loads_only_the_verified_bytes(self):
        # I1: the four files are read once and hashed in full (read_verified),
        # and the sessions are built from those same bytes, never from a path,
        # so no file is opened again after its check; the bytes are then let go.
        made = []

        class Session:
            def __init__(self, data, options, providers):
                self._model_bytes = data
                made.append((data, providers))

            def get_modelmeta(self):
                meta = {"window_size": "65", "chunk_shift": "56", "subsampling_factor": "8",
                        "vocab_size": "1024", "feat_dim": "128", "pred_hidden": "640",
                        "pred_rnn_layers": "2"}
                for kind, dims in (("channel", (24, 70, 1024)), ("time", (24, 1024, 8))):
                    for i, d in enumerate(dims, 1):
                        meta[f"cache_last_{kind}_dim{i}"] = str(d)
                return mock.Mock(custom_metadata_map=meta)

            def get_inputs(self):
                return [mock.Mock(name=n) for n in ("a", "b", "c", "d")]

        rt = mock.Mock(InferenceSession=Session, SessionOptions=mock.Mock,
                       ExecutionMode=mock.Mock(ORT_SEQUENTIAL=0))
        blobs = {"encoder.int8.onnx": b"E", "decoder.int8.onnx": b"D",
                 "joiner.int8.onnx": b"J", "tokens.txt": "▁a 0\n".encode()}
        with mock.patch.dict(sys.modules, {"onnxruntime": rt}), \
                mock.patch.object(asr_engines, "read_verified",
                                  return_value=dict(blobs)) as read:
            engine = instant_engine.InstantEngine("cache").load()
        read.assert_called_once_with(asr_engines.INSTANT_MODEL, "cache")
        self.assertEqual([d for d, _ in made], [b"E", b"D", b"J"])
        self.assertTrue(all(p == ["CPUExecutionProvider"] for _, p in made))
        self.assertTrue(all(s._model_bytes is None
                            for s in (engine._enc, engine._dec, engine._join)))
        rt.disable_telemetry_events.assert_called_once_with()
        self.assertEqual(engine.tokens, {0: " a"})

    def test_only_the_pinned_model_or_a_test_engine_loads(self):
        with self.assertRaisesRegex(asr_engines.ModelLoadError, "their own model"):
            engines.load_instant_engine(asr_engines.PARAKEET_V2, "x", 1)
        with self.assertRaisesRegex(asr_engines.ModelLoadError, "Unknown speech model"):
            engines.load_instant_engine(FAKE + "instant_words", "x", 1)   # tests only

    def test_the_model_bytes_are_let_go(self):
        # onnxruntime 1.30's InferenceSession keeps the bytes it was built from
        # (_model_bytes) for a retry on another provider; about 0.6 GB here.
        class Session:
            _model_bytes = b"x" * 10
        s = Session()
        instant_engine._release(s)
        self.assertIsNone(s._model_bytes)
        instant_engine._release(object())          # a later runtime without it: nothing
        if importlib.util.find_spec("onnxruntime"):
            import inspect

            from onnxruntime.capi import onnxruntime_inference_collection as oic
            # If this fails, a new onnxruntime renamed it: check _release again.
            self.assertIn("self._model_bytes = path_or_bytes", inspect.getsource(oic))

    def test_one_thread_and_no_onnxruntime_in_the_window(self):
        self.assertEqual((instant.THREADS, instant_engine.THREADS), (1, 1))
        self.assertNotIn("onnxruntime", instant_engine.__dict__)


# -- the worker's loop, in this process with a pipe stand-in ---------------------------

class Conn:
    def __init__(self, messages):
        self.messages, self.sent = list(messages), []

    def recv(self):
        if not self.messages:
            raise EOFError
        return self.messages.pop(0)

    def send(self, msg):
        self.sent.append(msg)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class ServeTests(unittest.TestCase):
    def serve(self, messages, engine=None):
        import fake_worker_engines as few
        engine = engine or few.instant_words(1)
        conn = Conn([("start", SR), *messages])
        instant.serve(conn, lambda: engine)
        return conn.sent, engine

    def test_words_carry_their_place_in_the_recording(self):
        pcm = tone(1.0).tobytes()
        sent, engine = self.serve([("audio", 0, pcm), ("audio", SR, pcm),
                                   ("audio", 10 * SR, pcm)])    # after a gap: a fresh stream
        self.assertEqual(sent[0][0], "ready")
        words = [w for m in sent if m[0] == "tokens" for w in m[1]]
        self.assertEqual([p for _, p in words], [" w1s1", " w2s1", " w3s1", " w4s1",
                                                 " w1s2", " w2s2"])
        self.assertEqual([t for t, _ in words][-2:], [10.4, 10.9])
        self.assertEqual(engine.streams, 2)

    def test_a_load_failure_says_so_and_ends(self):
        def fail():
            raise RuntimeError("model files failed verification")
        conn = Conn([("start", SR), ("audio", 0, tone(1).tobytes())])
        instant.serve(conn, fail)
        self.assertEqual([m[0] for m in conn.sent], ["error"])
        self.assertEqual(conn.sent[0][3], "load")

    def test_a_decoding_error_ends_it_with_its_own_op(self):
        import fake_worker_engines as few
        sent, _ = self.serve([("audio", 0, tone(1).tobytes()), ("audio", SR, tone(1).tobytes())],
                             few.instant_raises(1))
        self.assertEqual(sent[-1][0], "error")
        self.assertEqual(sent[-1][3], "instant")

    def test_bad_messages_are_ignored(self):
        sent, engine = self.serve([("audio", -1, b"xx"), ("audio", 0, b""), ("nonsense",), "x",
                                   ("computer", 0, 5, b"xx")])
        self.assertEqual([m[0] for m in sent], ["ready"])
        self.assertEqual(engine.fed, 0)

    def test_a_wrong_start_loads_nothing(self):
        loaded = []
        instant.serve(Conn([("start", 5)]), lambda: loaded.append(1))
        self.assertEqual(loaded, [])

    def test_pieces_are_plain_bounded_text(self):
        self.assertEqual(instant.clean_piece(" a\x00b\nc"), " abc")
        self.assertEqual(len(instant.clean_piece("x" * 500)), instant.MAX_PIECE)


# -- the session, with a real worker process running a fake engine ---------------------

@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def session(self, engine="instant_words", start=True, **kw):
        s = instant.InstantSession(SR, self.root, model=FAKE + engine, allow_test_engines=True,
                                   **kw)
        self.addCleanup(s.stop)
        if start:
            self.assertTrue(s.start())
        return s

    @staticmethod
    def play(s, pcm, start=0):
        for i in range(0, len(pcm), SR // 2):
            s.put(("audio", start + i, pcm[i:i + SR // 2].tobytes()))
        return start + len(pcm)

    def test_words_arrive_with_their_times(self):
        s = self.session()
        self.assertTrue(wait_for(lambda: s.state == instant.RUNNING))
        self.play(s, tone(2.0))
        self.assertTrue(wait_for(lambda: s.word_count == 4))
        self.assertEqual(s.words_after(0.0), "w1s1 w2s1 w3s1 w4s1")
        self.assertEqual(s.words_after(1.0), "w3s1 w4s1")      # the draft has replaced the rest

    def test_the_worker_runs_below_normal_priority(self):
        if sys.platform != "win32":
            self.skipTest("Windows priority classes")
        from workspace import resources
        s = self.session()
        self.assertTrue(wait_for(lambda: s.state == instant.RUNNING))
        self.assertEqual(resources.get_priority(s.pid), resources.BELOW_NORMAL_PRIORITY_CLASS)

    def test_the_worker_cannot_reach_the_network(self):
        # N1: its own process, guarded on its own, with weights-only loading.
        s = self.session("instant_network")
        self.assertTrue(wait_for(lambda: s.state == instant.RUNNING))
        self.play(s, tone(0.5))
        self.assertTrue(wait_for(lambda: s.word_count == 1, timeout=30))
        word = s.words_after(0.0)
        self.assertIn("blockedNetworkBlockedError", word)
        self.assertIn("weights1", word)

    def test_a_crashed_worker_is_restarted_once_then_stops(self):
        marker = self.root / "crashed"
        with mock.patch.dict(os.environ, {"UNMINUTE_TEST_INSTANT_MARKER": str(marker)}):
            s = self.session("instant_crash_once")
            self.assertTrue(wait_for(lambda: s.state == instant.RUNNING))
            first = s.pid
            self.play(s, tone(0.5))
            self.assertTrue(wait_for(lambda: s.restarts == 1 and s.state == instant.RUNNING))
            self.assertNotEqual(s.pid, first)
            self.play(s, tone(0.5), start=SR)
            self.assertTrue(wait_for(lambda: s.word_count >= 1))
        s2 = self.session("instant_crash_always")
        self.assertTrue(wait_for(lambda: s2.state == instant.RUNNING))
        for k in range(4):
            self.play(s2, tone(0.5), start=k * SR)
            time.sleep(0.5)
        self.assertTrue(wait_for(lambda: s2.state == instant.FAILED, timeout=60))
        self.assertEqual((s2.restarts, s2.reason, s2.pid), (1, "crashed", None))
        self.play(s2, tone(0.5), start=10 * SR)                 # after it stopped: let go
        self.assertEqual(len(s2._queue), 0)

    def test_a_model_that_will_not_load_is_unavailable_not_restarted(self):
        s = self.session("instant_load_fails")
        self.assertTrue(wait_for(lambda: s.state == instant.UNAVAILABLE))
        self.assertTrue(wait_for(lambda: s.pid is None))
        self.assertEqual((s.restarts, s.reason), (0, "load"))

    def test_hidden_sends_nothing_and_shown_starts_a_fresh_stream(self):
        fed = self.root / "fed.txt"
        with mock.patch.dict(os.environ, {"UNMINUTE_TEST_INSTANT_FED": str(fed)}):
            s = self.session("instant_counts_audio")
            self.assertTrue(wait_for(lambda: s.state == instant.RUNNING))
            at = self.play(s, tone(1.0))
            self.assertTrue(wait_for(lambda: fed.exists() and fed.read_text() == str(SR)))
            s.set_visible(False)
            at = self.play(s, tone(3.0), start=at)
            time.sleep(1.0)
            self.assertEqual(fed.read_text(), str(SR))              # nothing reached it
            self.assertAlmostEqual(s.hidden_s, 3.0)
            self.assertEqual(s.dropped_s, 0.0)                      # not counted as lost
            s.set_visible(True)
            self.play(s, tone(1.0), start=at)
            self.assertTrue(wait_for(lambda: fed.read_text() == str(2 * SR)))
            self.assertTrue(wait_for(lambda: s.word_count == 4))
            self.assertIn("w1s2", s.words_after(0.0))               # a fresh stream

    def test_audio_waiting_beyond_the_queue_limit_goes_oldest_first(self):
        s = self.session(start=False)                 # not started: nothing is sent
        for k in range(20):
            s.put(("audio", k * SR // 2, tone(0.5).tobytes()))
        self.assertLessEqual(s._queued_s, instant.QUEUE_S)
        self.assertAlmostEqual(s.dropped_s, 10.0 - s._queued_s)
        self.assertEqual(s._queue[-1][0][1], 19 * SR // 2)          # the newest kept

    def test_stop_ends_the_worker_forgets_the_words_and_logs_counts_only(self):
        log = diag.Log(self.root / "logs", "app")
        s = self.session(log=log)
        self.assertTrue(wait_for(lambda: s.state == instant.RUNNING))
        self.play(s, tone(1.0))
        self.assertTrue(wait_for(lambda: s.word_count == 2))
        proc = s._proc
        s.stop()
        self.assertFalse(proc.is_alive())
        self.assertEqual((s.state, s.pid, s.words_after(0.0)), (instant.STOPPED, None, ""))
        self.assertFalse(s.start())
        records = [r for r in diag.shareable_records(self.root / "logs")
                   if r.get("kind") == "instant"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["words"], 2)
        self.assertNotIn("w1s1", (self.root / "logs" / "app.log").read_text(encoding="utf-8"))

    def test_suspend_ends_the_worker_and_start_brings_it_back(self):
        s = self.session()
        self.assertTrue(wait_for(lambda: s.state == instant.RUNNING))
        self.play(s, tone(0.5))
        self.assertTrue(wait_for(lambda: s.word_count == 1))
        proc = s._proc
        s.suspend()
        self.assertFalse(proc.is_alive())
        self.assertEqual((s.state, s.words_after(0.0)), (instant.BATTERY, "w1s1"))
        self.assertTrue(s.start())
        self.assertTrue(wait_for(lambda: s.state == instant.RUNNING))

    def test_old_words_are_let_go(self):
        s = instant.InstantSession(SR, self.root)
        with s._lock:
            for k in range(instant.MAX_WORDS + 50):
                s._add(k * 0.01, f" w{k}")
            self.assertEqual(len(s._words), instant.MAX_WORDS)
            s._add(1000.0, " late")
        self.assertEqual(s.words_after(0.0), "late")
        with s._lock:
            s._add(1000.1, "r")                        # no space: the same word goes on
        self.assertEqual(s.words_after(0.0), "later")

    def test_bad_replies_are_ignored(self):
        s = instant.InstantSession(SR, self.root)
        with s._lock:
            s._take(("tokens", [(math.nan, " a"), (1.0, 5), ("x", " b"), (True, " c"),
                                (2.0, " d")]))
            s._take(("tokens", "x"))
            s._take(())
        self.assertEqual(s.words_after(0.0), "d")


# -- the draft session's wiring ---------------------------------------------------------

class InstantStandIn:
    """Records what the draft session asks of its instant words."""

    def __init__(self):
        self.calls, self.msgs = [], []

    def start(self):
        self.calls.append("start")

    def put(self, msg):
        self.msgs.append(msg)

    def stop(self):
        self.calls.append("stop")

    def suspend(self):
        self.calls.append("suspend")

    def set_visible(self, visible):
        self.calls.append(("visible", visible))

    def words_after(self, after):
        return f"after {after}"


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class DraftSessionWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_instant_words_start_after_the_draft_worker_is_ready_and_end_with_it(self):
        # D4's exception: the two models load one at a time, and both have
        # ended when the draft session's stop returns.
        stand_in = InstantStandIn()
        s = drafts.DraftSession("rec1", SR, self.root, model=FAKE + "draft_phrases",
                                allow_test_engines=True, instant=stand_in)
        self.addCleanup(s.stop)
        self.assertEqual(stand_in.calls, [])
        self.assertTrue(s.start())
        self.assertTrue(wait_for(lambda: s.state == drafts.RUNNING))
        self.assertEqual(stand_in.calls, ["start"])
        pcm = np.concatenate([tone(1.5), silence(1.0)])
        for i in range(0, len(pcm), SR // 10):
            s.tap(i, pcm[i:i + SR // 10])
        self.assertTrue(wait_for(lambda: [p for p in s.snapshot()["phrases"]]))
        # Every message the draft worker got went to instant words too.
        self.assertTrue(wait_for(lambda: sum(len(m[2]) // 2 for m in stand_in.msgs
                                             if m[0] == "audio") == len(pcm)))
        # A block at a time, not 0.5 s batches, so the first piece is not held back.
        self.assertTrue(all(len(m[2]) // 2 <= SR // 10 for m in stand_in.msgs
                            if m[0] == "audio"))
        end = [p for p in s.snapshot()["phrases"] if p["kind"] == "phrase"][0]["end"]
        self.assertEqual(s.instant_words(), f"after {end}")
        s.set_visible(False)
        s.suspend()
        s.stop()
        self.assertEqual(stand_in.calls, ["start", ("visible", False), "suspend", "stop"])

    def test_without_instant_words_nothing_changes(self):
        s = drafts.DraftSession("rec1", SR, self.root)
        self.assertEqual(s.instant_words(), "")
        s.set_visible(False)                       # nothing to tell


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class WorkspaceTests(unittest.TestCase):
    RATE = 48000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.holder = {}
        devices = [{"index": 3, "name": "Test microphone", "default": True, "rate": self.RATE}]
        from workspace import power

        def never(*args, **kwargs):
            raise AssertionError("a test reached the real power scheme")
        import test_drafts
        patches = [mock.patch.object(recorder, "list_devices", lambda: devices),
                   mock.patch.object(recorder, "SILENT_DEVICE_S", 3600.0),
                   mock.patch.object(drafts, "ON_PAGE", True)]
        patches += [mock.patch.object(power._PowrProf, name, never) for name in ("write", "apply")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ws = Workspace(self.root / "ws", self.root / "models", start=False,
                            allow_test_engines=True, battery=False,
                            policy_reader=lambda: ({}, ()),
                            protector=keyprotect.TestProtector())
        self.ws.settings.update(model=FAKE + "sentences", speakers=False, microphone=True,
                                live_drafts=True)
        self.ws.draft_model = FAKE + "draft_phrases"
        self.ws.instant_model = FAKE + "instant_words"
        self.ws.open_stream = test_drafts.opener(self.holder)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.ws.close)

    def record(self):
        self.ws.start()
        self.ws.start_recording(3, consent=consent.recording_record(
            sources=self.ws.recording_sources(), clock=lambda: 1000.0))
        return self.ws._recording

    def speech(self, seconds):
        return tone(seconds, self.RATE)

    def test_off_until_enabled(self):
        self.assertFalse(instant.ENABLED)
        self.record()
        self.ws._policy_tick()
        self.assertTrue(wait_for(lambda: self.ws._drafts is not None))
        self.assertIsNone(self.ws._drafts.instant)
        self.ws.stop_recording()

    def test_not_without_its_model(self):
        self.ws.instant_model = instant.INSTANT_MODEL         # not downloaded here
        with mock.patch.object(instant, "ENABLED", True):
            self.record()
            self.ws._policy_tick()
            self.assertTrue(wait_for(lambda: self.ws._drafts is not None))
            self.assertIsNone(self.ws._drafts.instant)
            self.ws.stop_recording()

    def test_both_workers_run_while_recording_and_end_before_processing(self):
        with mock.patch.object(instant, "ENABLED", True):
            self.record()
            self.ws._policy_tick()
            session = self.ws._drafts
            self.assertIsNotNone(session.instant)
            self.assertTrue(wait_for(lambda: session.instant.state == instant.RUNNING))
            self.holder["stream"].play(self.speech(2.0))
            self.assertTrue(wait_for(lambda: session.instant.word_count >= 3))
            procs = (session._proc, session.instant._proc)
            # Minimised: instant words pause; shown again: they carry on.
            self.ws.window_minimised(True)
            self.assertFalse(session.instant.visible)
            self.ws._policy_tick()
            self.assertFalse(session.instant.visible)
            self.ws.window_minimised(False)
            self.assertTrue(session.instant.visible)
            self.ws.stop_recording()
            # D4: both ended before the recording's processing may start.
            self.assertIsNone(self.ws._drafts)
            self.assertFalse(any(p.is_alive() for p in procs))

    def test_a_session_started_while_minimised_starts_paused(self):
        self.ws.window_minimised(True)
        with mock.patch.object(instant, "ENABLED", True):
            self.record()
            self.ws._policy_tick()
            self.assertFalse(self.ws._drafts.instant.visible)
            self.ws.stop_recording()

    def test_window_minimised_never_raises_without_a_recording(self):
        self.ws.window_minimised(True)
        self.ws.window_minimised(False)


# -- the shell and diagnostics --------------------------------------------------------

class ShellTests(unittest.TestCase):
    def test_the_workspace_is_told_of_each_change_and_an_error_changes_nothing(self):
        from workspace import shell

        class WebView:
            def __init__(self):
                self.calls = []

            def __getattr__(self, name):
                return lambda *a: self.calls.append(name)
        told = []
        state = shell.Minimise(WebView(), lambda: True, lambda s, fn: None, told.append)
        state.changed(True)
        state.changed(True)                        # no change: not told again
        state.changed(False)
        self.assertEqual(told, [True, False])

        def fails(minimised):
            raise RuntimeError("x")
        view = WebView()
        state = shell.Minimise(view, lambda: True, lambda s, fn: None, fails)
        state.changed(True)
        self.assertEqual(view.calls, ["hide", "low_memory"])


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class DiagnosticsTests(unittest.TestCase):
    def test_its_log_and_process_names_are_known_and_no_text_field_is(self):
        self.assertTrue(diag.LOG_NAME.fullmatch(instant.LOG_NAME))
        self.assertEqual(diag.FIELDS["process"](instant.LOG_NAME), instant.LOG_NAME)
        self.assertEqual(diag.FIELDS["op"]("instant"), "instant")
        self.assertIn("instant", diag.KINDS)
        self.assertIn("words", diag.NUMBERS)


if __name__ == "__main__":
    unittest.main()
