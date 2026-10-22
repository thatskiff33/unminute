# Live drafts while recording (#104), back end: the phrase splitter, the
# resampler, the draft worker with fake engines, the recorder tap, and the
# workspace lifecycle (off by default, policy, battery, every way a recording
# ends, D4). Synthetic audio only: tone bursts are "speech", zeros are silence.
#   python tools/run_tests.py

import importlib.util
import math
import os
import sys
import tempfile
import threading
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

    from workspace import drafts, policy, recorder, resources
    from workspace.app import Workspace, WorkspaceError

CONSENT = consent.recording_record(clock=lambda: 1000.0)
# drafts.ON_PAGE as the module ships it, read before any test patches it.
SHIPPED_ON_PAGE = drafts.ON_PAGE if HAVE_DEPS else None
FAKE = "test:fake_worker_engines:"


def tone(seconds, rate=16000, freq=220.0, amp=0.25):
    t = np.arange(int(round(seconds * rate))) / rate
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)


def silence(seconds, rate=16000):
    return np.zeros(int(round(seconds * rate)), dtype=np.int16)


def wait_for(check, timeout=60.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(step)
    return bool(check())


def loudness(window):
    """A fake speech score: 1 for a tone, 0 for silence."""
    return 1.0 if float(np.sqrt(np.mean(np.square(window)))) > 0.02 else 0.0


# -- the pure parts -----------------------------------------------------------------

@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class SplitterTests(unittest.TestCase):
    def split(self, audio, score=loudness, flush=True):
        """Phrases [(start_s, end_s, audio)] from 16 kHz float audio."""
        s = drafts.PhraseSplitter()
        out = []
        whole = len(audio) // drafts.WINDOW * drafts.WINDOW
        for i in range(0, whole, drafts.WINDOW):
            w = audio[i:i + drafts.WINDOW]
            out += s.push(w, score(w))
        if flush:
            out += s.flush()
        return [(a / 16000, (a + len(x)) / 16000, x) for a, x in out]

    @staticmethod
    def f(pcm):
        return pcm.astype(np.float32) / 32768.0

    def test_a_phrase_ends_after_silence_with_pre_roll_and_a_short_tail(self):
        audio = self.f(np.concatenate([silence(1.0), tone(2.0), silence(1.5), tone(1.0),
                                       silence(1.0)]))
        phrases = self.split(audio, flush=False)
        self.assertEqual(len(phrases), 2)
        (s1, e1, _), (s2, e2, _) = phrases
        w = drafts.WINDOW_S
        self.assertAlmostEqual(s1, 1.0 - drafts._windows(drafts.PRE_ROLL_S) * w, delta=w)
        self.assertAlmostEqual(e1, 3.0 + drafts._windows(drafts.TAIL_S) * w, delta=w)
        self.assertGreater(s2, e1)                       # never the same audio twice
        self.assertAlmostEqual(s2, 4.5 - drafts._windows(drafts.PRE_ROLL_S) * w, delta=w)
        self.assertAlmostEqual(e2, 5.5 + drafts._windows(drafts.TAIL_S) * w, delta=w)

    def test_unbroken_speech_is_cut_at_the_quietest_window_near_the_limit(self):
        w = drafts.WINDOW_S
        # A quieter moment on a window boundary within the search span, 13 windows
        # before the cut: 9.6 s at 10 s, 7.584 s at 8 s. The plain tone's own energy
        # ripple must not put the cut there too, or the check proves nothing.
        before_cut = 13                                  # windows; inside the 47-window search
        dip_at = (drafts._windows(drafts.MAX_PHRASE_S) - before_cut) * w
        expected_end = dip_at + w                        # the quiet window ends the phrase

        def split_with_dip(scale):
            speech = tone(20.0).astype(np.float32)
            a, b = round(dip_at * 16000), round((dip_at + w) * 16000)
            speech[a:b] *= scale
            return self.split(self.f(speech.astype(np.int16)))

        # Control: with no dip, the cut lands well away from the expected end.
        self.assertNotAlmostEqual(split_with_dip(1.0)[0][1], expected_end, delta=2 * w)
        phrases = split_with_dip(0.3)                    # still speech, but the quietest
        first_end = phrases[0][1]
        longest = drafts._windows(drafts.MAX_PHRASE_S) * w   # whole windows
        self.assertLessEqual(phrases[0][1] - phrases[0][0], longest + 1e-6)
        self.assertAlmostEqual(first_end, expected_end, delta=2 * w)
        for (_, e, _), (s, _, _) in zip(phrases, phrases[1:], strict=False):
            self.assertAlmostEqual(e, s, places=6)      # cut, not lost: back to back
        self.assertAlmostEqual(phrases[-1][1], 20.0, delta=drafts.WINDOW_S)

    def test_unbroken_speech_is_cut_at_about_10_seconds_the_owners_decision(self):
        # The owner's decision of 24 October 2026 (#104), from the 6, 8 and 10 s
        # trial in #347. Changing the cut is the owner's call, not a tuning step.
        self.assertEqual(drafts.MAX_PHRASE_S, 10.0)
        phrases = self.split(self.f(tone(25.0)))
        length = phrases[0][1] - phrases[0][0]
        # 10 s is 312.5 windows of 32 ms, so the cut falls at 313 windows at most (10.016 s).
        longest = drafts._windows(drafts.MAX_PHRASE_S) * drafts.WINDOW_S
        self.assertLessEqual(length, longest + 1e-6)
        self.assertGreaterEqual(length, 10.0 - drafts.CUT_SEARCH_S - drafts.WINDOW_S)

    def test_short_blips_are_dropped(self):
        audio = self.f(np.concatenate([silence(1.0), tone(0.1), silence(2.0)]))
        self.assertEqual(self.split(audio), [])

    def test_flush_ends_the_phrase_in_progress(self):
        audio = self.f(np.concatenate([silence(0.5), tone(1.5)]))
        self.assertEqual(self.split(audio, flush=False), [])
        [(s, e, _)] = self.split(audio)
        self.assertAlmostEqual(e, 2.0, delta=drafts.WINDOW_S)

    def test_every_sample_is_in_at_most_one_phrase_and_speech_is_never_lost(self):
        rng = np.random.default_rng(7)
        parts, speech = [], []
        t = 0.0
        for _ in range(30):
            n = float(rng.uniform(0.2, drafts.MAX_PHRASE_S + 1.0))   # some longer than the cut
            loud = bool(rng.integers(0, 2))
            parts.append(tone(n) if loud else silence(n))
            if loud:
                speech.append((t, t + n))
            t += n
        audio = self.f(np.concatenate(parts))
        phrases = self.split(audio)
        covered = np.zeros(len(audio), dtype=np.int32)
        for s, e, x in phrases:
            a = int(round(s * 16000))
            self.assertEqual(len(x), int(round(e * 16000)) - a)
            np.testing.assert_array_equal(x, audio[a:a + len(x)])   # the audio itself
            covered[a:a + len(x)] += 1
        self.assertLessEqual(int(covered.max()), 1)
        whole = len(audio) // drafts.WINDOW * drafts.WINDOW
        for s, e in speech:
            if e - s < drafts.MIN_SPEECH_S + 2 * drafts.WINDOW_S:
                continue                                  # a blip may be dropped
            a = int(math.ceil(s * 16000 / drafts.WINDOW)) * drafts.WINDOW
            b = min(whole, int(e * 16000) // drafts.WINDOW * drafts.WINDOW)
            self.assertTrue(covered[a:b].all(), (s, e))


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class ResamplerTests(unittest.TestCase):
    def test_a_tone_keeps_its_pitch_and_length_at_any_device_rate(self):
        for rate in (48000, 44100, 32000, 22050, 16000):
            with self.subTest(rate=rate):
                pcm = tone(2.0, rate=rate, freq=1000.0)
                r = drafts.Resampler(rate)
                out = np.concatenate([r.feed(pcm[i:i + rate // 10])
                                      for i in range(0, len(pcm), rate // 10)])
                self.assertLessEqual(abs(len(out) - 32000), 2)
                spec = np.abs(np.fft.rfft(out[1000:17000]))
                peak = np.fft.rfftfreq(16000, 1 / 16000)[int(np.argmax(spec))]
                self.assertLess(abs(peak - 1000.0), 2.0)

    def test_block_by_block_matches_all_at_once(self):
        pcm = tone(1.0, rate=44100, freq=440.0)
        whole = drafts.Resampler(44100).feed(pcm)
        r = drafts.Resampler(44100)
        parts = np.concatenate([r.feed(pcm[i:i + 4410]) for i in range(0, len(pcm), 4410)])
        n = min(len(whole), len(parts))
        np.testing.assert_allclose(parts[:n], whole[:n], atol=1e-5)

    def test_high_tones_are_filtered_not_folded_into_speech(self):
        pcm = tone(1.0, rate=48000, freq=12000.0)       # above 16 kHz audio's 8 kHz limit
        out = drafts.Resampler(48000).feed(pcm)
        self.assertLess(float(np.sqrt(np.mean(np.square(out[200:])))), 0.01)

    def test_refuses_impossible_rates(self):
        for rate in (0, 100, 10 ** 6, 44100.0):
            with self.assertRaises(ValueError):
                drafts.Resampler(rate)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class PipelineTests(unittest.TestCase):
    def test_times_follow_the_recording_across_a_gap(self):
        p = drafts.DraftPipeline(48000, loudness)
        out = p.feed(0, np.concatenate([tone(1.0, 48000), silence(1.0, 48000)]))
        self.assertEqual(len(out), 1)
        self.assertLess(out[0][0], 0.05)
        # Draft audio lost (or drafts paused on battery): the next batch starts
        # later in the recording, and its phrase is placed there.
        out = p.feed(48000 * 30, tone(1.0, 48000))
        out += p.feed(48000 * 31, silence(1.0, 48000))
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0][0], 30.0, delta=0.05)

    def test_a_gap_ends_the_phrase_in_progress(self):
        p = drafts.DraftPipeline(16000, loudness)
        self.assertEqual(p.feed(0, tone(1.0)), [])
        out = p.feed(16000 * 10, silence(0.1))
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0][1], 1.0, delta=0.05)


# -- the draft worker process, with fake engines ----------------------------------------

@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def session(self, engine, rate=16000, **kw):
        s = drafts.DraftSession("rec1", rate, self.root, model=FAKE + engine,
                                allow_test_engines=True, **kw)
        self.addCleanup(s.stop)
        self.assertTrue(s.start())
        return s

    @staticmethod
    def play(s, pcm, start=0, rate=16000):
        """Hands pcm to the tap in 0.1 s blocks, as the recorder's writer does."""
        n = rate // 10
        for i in range(0, len(pcm), n):
            s.tap(start + i, pcm[i:i + n])
        return start + len(pcm)

    def speech(self, bursts=2):
        return np.concatenate([np.concatenate([tone(1.5), silence(1.0)])
                               for _ in range(bursts)])

    def phrases(self, s):
        return [p for p in s.snapshot()["phrases"] if p["kind"] == "phrase"]

    def test_phrases_arrive_in_order_with_their_times(self):
        s = self.session("draft_phrases")
        self.play(s, self.speech(3))
        self.assertTrue(wait_for(lambda: len(self.phrases(s)) == 3))
        got = self.phrases(s)
        self.assertEqual([p["text"].split()[1] for p in got], ["1", "2", "3"])
        for i, p in enumerate(got):
            self.assertAlmostEqual(p["start"], 2.5 * i, delta=0.4)
            self.assertLess(p["start"], p["end"])
        self.assertEqual(s.state, drafts.RUNNING)
        snap = s.snapshot(since=2)
        self.assertEqual((snap["since"], len(snap["phrases"]), snap["count"]), (2, 1, 3))

    def test_the_worker_runs_below_normal_priority(self):
        if sys.platform != "win32":
            self.skipTest("Windows priority classes")
        s = self.session("draft_phrases")
        self.assertTrue(wait_for(lambda: s.state == drafts.RUNNING))
        self.assertEqual(resources.get_priority(s.pid), resources.BELOW_NORMAL_PRIORITY_CLASS)

    def test_stop_ends_the_worker_and_forgets_the_text(self):
        s = self.session("draft_phrases")
        self.play(s, self.speech(1))
        self.assertTrue(wait_for(lambda: self.phrases(s)))
        pid = s.pid
        proc = s._proc
        s.stop()
        self.assertFalse(proc.is_alive())
        self.assertIsNone(s.pid)
        self.assertEqual(s.snapshot()["phrases"], [])
        self.assertEqual(s.state, drafts.STOPPED)
        self.assertTrue(pid)
        s.tap(0, tone(0.1))                          # after a stop: ignored, never raises
        self.assertFalse(s.start())                  # and never started again

    def test_a_second_stop_returns_only_once_the_worker_has_exited(self):
        # D4: the policy thread and the page can end drafts at the same time;
        # whichever returns second must not return while the worker still runs.
        s = self.session("draft_phrases")
        self.assertTrue(wait_for(lambda: s.state == drafts.RUNNING))
        proc = s._proc
        real_end = s._end_process

        def slow_end(*args, **kwargs):
            time.sleep(1.0)
            real_end(*args, **kwargs)
        s._end_process = slow_end
        first = threading.Thread(target=s.stop)
        first.start()
        self.assertTrue(wait_for(lambda: s._stopped))
        s.stop()
        self.assertFalse(proc.is_alive())
        first.join(10)

    def test_a_late_result_after_stop_is_dropped(self):
        s = drafts.DraftSession("rec1", 16000, self.root)
        late = ("phrase", 0.0, 1.0, "late words")

        class Conn:
            """The late result, then the pipe closes."""
            def __init__(self):
                self.sent = False

            def poll(self, timeout):
                return True

            def recv(self):
                if self.sent:
                    raise EOFError
                self.sent = True
                return late

            def close(self):
                pass
        conn = Conn()
        s._conn = conn
        s._stopped = True
        s._read_loop(mock.Mock(is_alive=lambda: True), conn)
        self.assertEqual(s.snapshot()["phrases"], [])
        conn = Conn()
        s._stopped = False
        s._conn = object()                           # replaced by a restarted worker
        s._read_loop(mock.Mock(is_alive=lambda: True), conn)
        self.assertEqual(s.snapshot()["phrases"], [])

    def test_the_tap_never_blocks_and_a_full_tap_loses_draft_audio_only(self):
        s = drafts.DraftSession("rec1", 16000, self.root)
        s._feeding = True                            # never started: nothing drains it
        block = tone(0.1)
        began = time.perf_counter()
        for i in range(int(drafts.TAP_S / 0.1) + 50):
            s.tap(i * 1600, block)
        self.assertLess(time.perf_counter() - began, 1.0)
        self.assertAlmostEqual(s.dropped_s, 5.0, delta=0.01)

    def test_a_slow_worker_skips_the_oldest_and_feeding_never_waits(self):
        s = self.session("draft_slow")
        self.assertTrue(wait_for(lambda: s.state == drafts.RUNNING))
        pcm = np.concatenate([np.concatenate([tone(1.0), silence(0.8)]) for _ in range(30)])
        began = time.perf_counter()
        self.play(s, pcm)
        self.assertLess(time.perf_counter() - began, 1.0)
        self.assertTrue(wait_for(lambda: s.skipped_s > 0, timeout=30))
        skipped = [p for p in s.snapshot()["phrases"] if p["kind"] == "skipped"]
        self.assertTrue(skipped and all(p["seconds"] > 0 for p in skipped))
        self.assertEqual(s.dropped_s, 0.0)

    def test_a_crashed_worker_is_restarted_once(self):
        marker = self.root / "crashed"
        with mock.patch.dict(os.environ, {"UNMINUTE_TEST_MARKER": str(marker)}):
            s = self.session("draft_crash_once")
            end = self.play(s, self.speech(1))
            self.assertTrue(wait_for(lambda: s.restarts == 1 and s.state == drafts.RUNNING))
            self.play(s, self.speech(1), start=end + 16000)
            self.assertTrue(wait_for(lambda: self.phrases(s)))
        self.assertTrue(marker.exists())
        self.assertGreater(self.phrases(s)[0]["start"], 2.0)   # placed after the gap

    def test_audio_during_a_restart_waits_for_the_new_worker(self):
        # Found by a full local run: audio that arrived while the crashed
        # worker was being replaced was dropped, not held for the new one.
        marker = self.root / "crashed"
        with mock.patch.dict(os.environ, {"UNMINUTE_TEST_MARKER": str(marker)}):
            s = self.session("draft_crash_once")
            real_start = s._start_process

            def slow_start():
                time.sleep(1.0)
                real_start()
            s._start_process = slow_start
            end = self.play(s, self.speech(1))
            self.assertTrue(wait_for(lambda: marker.exists() and s._conn is None))
            self.play(s, self.speech(1), start=end + 16000)     # while it restarts
            self.assertTrue(wait_for(lambda: self.phrases(s), timeout=30))
        self.assertEqual(s.restarts, 1)
        self.assertGreater(self.phrases(s)[0]["start"], 3.0)    # the audio played meanwhile

    def test_a_second_crash_ends_drafts(self):
        s = self.session("draft_crash_always")
        end = self.play(s, self.speech(1))
        self.assertTrue(wait_for(lambda: s.restarts == 1 and s.state == drafts.RUNNING))
        self.play(s, self.speech(1), start=end)
        self.assertTrue(wait_for(lambda: s.state == drafts.FAILED))
        self.assertTrue(wait_for(lambda: s.pid is None))
        self.assertEqual(s.restarts, 1)

    def test_a_model_that_will_not_load_is_unavailable_and_not_retried(self):
        s = self.session("draft_load_fails")
        self.assertTrue(wait_for(lambda: s.state == drafts.UNAVAILABLE))
        self.assertTrue(wait_for(lambda: s.pid is None))
        self.assertEqual(s.restarts, 0)
        self.assertEqual(s.snapshot()["note"], drafts.NOTES[drafts.UNAVAILABLE])

    def test_suspend_keeps_the_text_and_resume_carries_on(self):
        s = self.session("draft_phrases")
        end = self.play(s, self.speech(1))
        self.assertTrue(wait_for(lambda: len(self.phrases(s)) == 1))
        s.suspend()
        self.assertEqual(s.state, drafts.BATTERY)
        self.assertIsNone(s.pid)
        self.assertEqual(len(self.phrases(s)), 1)
        self.play(s, self.speech(1), start=end)      # on battery: not drafted
        s.resume()
        self.play(s, self.speech(1), start=end + 5 * 16000)
        self.assertTrue(wait_for(lambda: len(self.phrases(s)) == 2))
        self.assertGreater(self.phrases(s)[1]["start"], 7.0)

    # -- round 1 review of PR #290 ------------------------------------------------

    def test_the_draft_worker_cannot_reach_the_network(self):
        # N1: the draft worker is its own process, so it is guarded on its own.
        s = self.session("draft_network")
        self.play(s, self.speech(1))
        self.assertTrue(wait_for(lambda: self.phrases(s), timeout=30))
        text = self.phrases(s)[0]["text"]
        self.assertIn("blocked NetworkBlockedError", text)
        self.assertIn("weights=1", text)                 # weights-only loading too

    def test_an_error_while_scoring_ends_drafts_with_an_incident(self):
        from workspace import diag
        log = diag.Log(self.root / "logs", "app")
        s = self.session("draft_scoring_fails", log=log)
        self.play(s, self.speech(1))
        self.assertTrue(wait_for(lambda: s.state == drafts.FAILED, timeout=30))
        self.assertTrue(wait_for(lambda: s.pid is None))
        self.assertEqual((s.reason, s.restarts), ("error", 0))    # not taken for a crash
        records = diag.shareable_records(self.root / "logs")
        self.assertTrue(any(r.get("kind") == "incident" and r.get("op") == "draft"
                            for r in records))

    def test_the_draft_worker_limits_its_crash_file_as_it_goes(self):
        # Run in this process with a pipe stand-in: the loop starts the crash
        # file afresh past its limit (#278), as the main worker does.
        from fake_worker_engines import draft_phrases

        class Conn:
            def __init__(self, msgs):
                self.msgs, self.sent = list(msgs), []

            def recv(self):
                if not self.msgs:
                    raise EOFError
                return self.msgs.pop(0)

            def send(self, msg):
                self.sent.append(msg)
        pcm = tone(0.5).astype("<i2").tobytes()
        conn = Conn([("start", 16000)] + [("audio", i * 8000, pcm) for i in range(4)])
        log = mock.Mock()
        drafts.serve(conn, lambda: draft_phrases(1), log)
        self.assertGreaterEqual(log.limit_crash_file.call_count, 4)

    def test_the_draft_worker_closes_its_crash_file_before_it_ends(self):
        from workspace import worker
        log = mock.Mock()
        with mock.patch.object(worker, "_start_up", return_value=(log, "cpu")), \
                mock.patch.object(drafts, "serve") as serve:
            worker.draft_main(None, str(self.root), FAKE + "draft_phrases", 1, True, None)
        serve.assert_called_once()
        log.stop_crash_capture.assert_called_once()

    def test_an_ended_draft_worker_is_waited_for_as_long_as_the_main_worker(self):
        from workspace import worker
        s = drafts.DraftSession("rec1", 16000, self.root)
        proc = mock.Mock(is_alive=lambda: True)
        s._end_process(proc, None)
        proc.terminate.assert_called_once()
        proc.join.assert_called_once_with(worker.END_WAIT_S)

    def test_load_time_must_be_a_finite_number(self):
        s = drafts.DraftSession("rec1", 16000, self.root)
        for bad in (float("nan"), float("inf"), True, "1.0"):
            with self.subTest(load_s=bad), s._lock:
                s._take(("ready", "engine", bad))
                self.assertIsNone(s.load_s)
        with s._lock:
            s._take(("ready", "engine", 1.25))
        self.assertEqual(s.load_s, 1.25)

    def test_the_logged_phrase_count_leaves_out_skipped_notes(self):
        log = mock.Mock()
        s = drafts.DraftSession("rec1", 16000, self.root, log=log)
        with s._lock:
            s._take(("phrase", 0.0, 1.0, "one"))
            s._take(("skipped", 4.0))
            s._take(("phrase", 5.0, 6.0, "two"))
        s.stop()
        [(args, kwargs)] = [c for c in log.write.call_args_list if c.args[:1] == ("drafts",)]
        self.assertEqual(kwargs["phrases"], 2)
        self.assertEqual(kwargs["skipped_s"], 4.0)


# -- the recorder tap ---------------------------------------------------------------

class FakeStream:
    def __init__(self, device, rate, callback, finished):
        self.rate, self.callback, self.finished = rate, callback, finished
        self.stopped = self.closed = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True
        self.finished()

    def close(self):
        self.closed = True

    def play(self, pcm):
        """0.1 s blocks through the callback, pausing so the writer keeps up."""
        n = int(self.rate * recorder.BLOCK_S)
        for i, k in enumerate(range(0, len(pcm) - n + 1, n)):
            self.callback(pcm[k:k + n].reshape(-1, 1), n, None, None)
            if i % 50 == 49:
                time.sleep(0.15)


def opener(holder):
    def open_stream(device, rate, callback, finished):
        holder["stream"] = FakeStream(device, rate, callback, finished)
        return holder["stream"]
    return open_stream


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class TapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.holder = {}
        self.rec = recorder.Recording(Path(self.tmp.name) / "rec", 3, "Test microphone", 16000,
                                      open_stream=opener(self.holder), key=bytes(range(32)))

    def test_the_tap_gets_every_block_with_its_place_in_the_recording(self):
        seen = []
        self.rec.tap = lambda start, block: seen.append((start, len(block)))
        self.rec.start()
        self.addCleanup(self.rec.stop)
        self.holder["stream"].play(tone(3.0))
        journal = self.rec.stop()
        self.assertEqual(sum(n for _, n in seen), journal["frames"])
        self.assertEqual([s for s, _ in seen], list(range(0, journal["frames"], 1600)))

    def test_a_failing_tap_is_turned_off_and_the_recording_is_untouched(self):
        def broken(start, block):
            raise RuntimeError("draft trouble")
        self.rec.tap = broken
        self.rec.start()
        self.addCleanup(self.rec.stop)
        self.holder["stream"].play(tone(3.0))
        journal = self.rec.stop()
        self.assertIsNone(self.rec.tap)
        self.assertEqual(journal["frames"], 48000)        # every frame saved
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual(journal["events"], [])           # nothing in the journal

    def test_a_slow_tap_never_reaches_the_audio_callback(self):
        # The callback only queues; the tap runs on the writer thread.
        threads = set()
        self.rec.tap = lambda start, block: threads.add(threading.current_thread().name)
        self.rec.start()
        self.addCleanup(self.rec.stop)
        self.holder["stream"].play(tone(0.5))
        self.rec.stop()
        self.assertEqual(threads, {"recorder-writer"})

    def test_the_tap_gets_the_block_itself_not_the_queued_tuple(self):
        # Since #287 the writer's queue holds (block, stamp, segment). A tap
        # handed the tuple would fail and be turned off: drafts would stop
        # with nothing said. It gets the 16-bit block and stays on.
        blocks = []

        def strict(start, block):
            if not (isinstance(block, np.ndarray) and block.dtype == np.int16
                    and block.ndim == 1):
                raise TypeError("not an audio block")
            blocks.append(len(block))
        self.rec.tap = strict
        self.rec.start()
        self.addCleanup(self.rec.stop)
        self.holder["stream"].play(tone(1.0))
        journal = self.rec.stop()
        self.assertIs(self.rec.tap, strict)               # never turned off
        self.assertEqual(blocks, [1600] * 10)
        self.assertEqual(sum(blocks), journal["frames"])

    def test_with_computer_audio_the_tap_follows_the_microphone_only(self):
        # The owner's decision of 22 Oct 2026: drafts use the microphone
        # track until the page part adds the choice of a live mix.
        class Loopback:
            device_name = "Test speakers"

            def __init__(self, source, callback, notify):
                self.callback = callback

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                pass

        held = {}

        def open_loopback(source, callback, notify):
            held["comp"] = Loopback(source, callback, notify)
            return held["comp"]
        rec = recorder.Recording(Path(self.tmp.name) / "two", 3, "Test microphone", 16000,
                                 open_stream=opener(self.holder),
                                 computer={"open": open_loopback,
                                           "source": {"kind": "endpoint"}, "rate": 48000},
                                 key=bytes(range(32)))
        seen = []
        rec.tap = lambda start, block: seen.append((start, len(block)))
        rec.start()
        self.addCleanup(rec.stop)
        comp = tone(1.0, 48000, freq=1000.0)
        for k in range(0, 48000, 4800):
            held["comp"].callback(comp[k:k + 4800], time.monotonic())
        self.holder["stream"].play(tone(1.0))
        journal = rec.stop()
        self.assertEqual(journal["tracks"]["computer"]["frames"], 48000)   # saved, not tapped
        self.assertEqual(seen, [(i * 1600, 1600) for i in range(10)])
        self.assertEqual(journal["frames"], 16000)
        # The tap takes the audio in memory; on disk both tracks are sealed
        # (#111 C3b): no file holds either track's samples in the clear.
        for needle in (tone(1.0)[800:832].astype("<i2").tobytes(),
                       comp[800:832].astype("<i2").tobytes()):
            self.assertEqual([p.name for p in Path(self.tmp.name).rglob("*")
                              if p.is_file() and needle in p.read_bytes()], [])

    def test_a_pause_marker_is_not_counted_as_queued_audio(self):
        # The marker that ends the draft's phrase at a pause takes a place in
        # the writer's queue but holds no audio: event times ignore it.
        inside, gate = threading.Event(), threading.Event()

        def held(start, block):
            if block is not None and start == 0:
                inside.set()
                gate.wait(10)                             # the writer waits here
        self.rec.tap = held
        self.rec.start()
        self.addCleanup(self.rec.stop)
        self.addCleanup(gate.set)
        self.holder["stream"].play(tone(1.0))
        self.assertTrue(inside.wait(10))
        self.rec.pause()                                  # the marker is queued
        self.rec.resume()                                 # no audio in between
        gate.set()
        journal = self.rec.stop()
        at = {e["kind"]: e["at_s"] for e in journal["events"]}
        self.assertEqual(at["resumed"], at["paused"])


# -- the workspace: off by default, policy, battery, every way a recording ends -----

class DraftWorkspaceCase(unittest.TestCase):
    """A workspace with fake engines and a fake microphone (no tests here)."""
    RATE = 48000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.holder = {}
        self.policy = ({}, ())
        devices = [{"index": 3, "name": "Test microphone", "default": True, "rate": self.RATE}]
        patches = [mock.patch.object(recorder, "list_devices", lambda: devices),
                   # The draft worker takes a moment to start: no feeding meanwhile
                   # must not count as a lost microphone.
                   mock.patch.object(recorder, "SILENT_DEVICE_S", 3600.0)]
        from workspace import power

        def never(*args, **kwargs):
            raise AssertionError("a test reached the real power scheme")
        patches += [mock.patch.object(power._PowrProf, name, never) for name in ("write", "apply")]
        # As the page part will set it; the tests of the refusal before then
        # set it back to False.
        patches.append(mock.patch.object(drafts, "ON_PAGE", True))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ws = self.make()

    def make(self, **kw):
        kw.setdefault("protector", keyprotect.TestProtector())
        ws = Workspace(self.root / "ws", self.root / "models", start=False,
                       allow_test_engines=True, battery=False,
                       policy_reader=lambda: self.policy, **kw)
        ws.settings.update(model=FAKE + "sentences", speakers=False, microphone=True)
        ws.draft_model = FAKE + "draft_phrases"
        ws.open_stream = opener(self.holder)
        return ws

    def tearDown(self):
        self.ws.close()
        self.tmp.cleanup()

    def record(self, drafts_on=True):
        if drafts_on:
            self.ws.settings.update(live_drafts=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        return self.ws._recording

    def speech(self, bursts):
        return np.concatenate([np.concatenate([tone(1.5, self.RATE), silence(1.0, self.RATE)])
                               for _ in range(bursts)])

    def running(self):
        s = self.ws._drafts
        return s is not None and s.state == drafts.RUNNING

    def draft_phrases(self):
        return [p for p in self.ws.recording_draft()["phrases"] if p["kind"] == "phrase"]

    def set_policy(self, values=None, invalid=()):
        self.policy = (values or {}, invalid)
        self.ws.policy.refresh()


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class WorkspaceDraftTests(DraftWorkspaceCase):
    # -- off means off ----------------------------------------------------------

    def test_off_by_default_no_worker_and_no_model(self):
        self.assertFalse(self.ws.settings["live_drafts"])
        with mock.patch.object(drafts, "DraftSession") as made:
            rec = self.record(drafts_on=False)
            self.holder["stream"].play(self.speech(2))
            for _ in range(3):
                self.ws._policy_tick()
            self.assertIsNone(self.ws._drafts)
            self.assertIsNone(rec.tap)
            self.assertEqual(self.ws.recording_draft()["state"], "off")
            self.ws.stop_recording()
        made.assert_not_called()

    def test_policy_off_bad_or_unreadable_withholds_drafts(self):
        for values, invalid in (({"AllowLiveDrafts": False}, ()),
                                ({}, ("AllowLiveDrafts",)),
                                ({}, policy.UNREADABLE)):
            with self.subTest(values=values, invalid=invalid):
                self.set_policy(values, invalid)
                self.assertFalse(self.ws.get_settings()["live_drafts"])
                with self.assertRaisesRegex(WorkspaceError, "turned off by your organisation"):
                    self.ws.set_settings({"live_drafts": True})
        self.set_policy()
        self.ws.settings.update(live_drafts=True)        # on before IT turned it off
        self.set_policy({"AllowLiveDrafts": False})
        with mock.patch.object(drafts, "DraftSession") as made:
            self.record(drafts_on=False)
            self.ws._policy_tick()
            self.assertIsNone(self.ws._drafts)
            self.assertEqual(self.ws.recording_draft()["state"], "managed")
            self.ws.stop_recording()
        made.assert_not_called()

    def test_the_switch_cannot_be_turned_on_before_the_page_shows_drafts(self):
        # Fail closed until the page part (slice 3): no bridge call turns on
        # a worker whose text nobody can see. Turning it off always works.
        with mock.patch.object(drafts, "ON_PAGE", False):
            with self.assertRaisesRegex(WorkspaceError, "not available in this version"):
                self.ws.set_settings({"live_drafts": True})
            self.assertFalse(self.ws.settings["live_drafts"])
            self.ws.set_settings({"live_drafts": False})
        self.ws.set_settings({"live_drafts": True})
        self.assertTrue(self.ws.settings["live_drafts"])

    def test_a_hand_edited_settings_file_cannot_start_drafts_before_the_page_shows_them(self):
        # The settings file says on (as a hand edit could leave it), but no
        # draft worker starts and no audio is tapped until the page part.
        with mock.patch.object(drafts, "ON_PAGE", False), \
                mock.patch.object(drafts, "DraftSession") as made:
            rec = self.record()
            self.holder["stream"].play(self.speech(1))
            for _ in range(3):
                self.ws._policy_tick()
            self.assertIsNone(self.ws._drafts)
            self.assertIsNone(rec.tap)
            self.assertEqual(self.ws.recording_draft()["state"], "off")
            self.ws.stop_recording()
        made.assert_not_called()

    def test_on_battery_drafts_wait_unless_battery_processing_is_allowed(self):
        self.ws._battery = True
        with mock.patch.object(drafts, "DraftSession") as made:
            self.record()
            self.ws._policy_tick()
            self.assertIsNone(self.ws._drafts)
            self.assertEqual(self.ws.recording_draft()["state"], "battery")
        made.assert_not_called()
        self.ws.settings.update(battery_override=True)
        self.ws._policy_tick()
        self.assertTrue(wait_for(self.running))

    def test_parakeet_v2_not_downloaded_means_no_worker(self):
        from workspace import asr_engines
        self.ws.draft_model = asr_engines.PARAKEET_V2          # the real one, not installed
        with mock.patch.object(drafts, "DraftSession") as made:
            self.record()
            self.ws._policy_tick()
            self.ws._policy_tick()
        made.assert_not_called()
        self.assertIsNone(self.ws._drafts)
        self.assertEqual(self.ws.recording_draft()["state"], "unavailable")

    # -- on -------------------------------------------------------------------------

    def test_drafts_follow_the_recording_and_go_at_stop_before_processing(self):
        order = []
        real_shutdown, real_start = self.ws.handle.shutdown, drafts.DraftSession.start

        def shutdown():
            order.append("main worker shut down")
            real_shutdown()

        def start(session):
            order.append("draft worker started")
            return real_start(session)
        with mock.patch.object(self.ws.handle, "shutdown", side_effect=shutdown), \
                mock.patch.object(drafts.DraftSession, "start", start):
            rec = self.record()
            self.assertTrue(wait_for(self.running))
        # D4: the main worker lets go of any model before the draft model loads.
        self.assertEqual(order[:2], ["main worker shut down", "draft worker started"])
        session = self.ws._drafts
        self.assertEqual(rec.tap, session.tap)
        self.holder["stream"].play(self.speech(3))
        self.assertTrue(wait_for(lambda: len(self.draft_phrases()) == 3))
        got = self.draft_phrases()
        for i, p in enumerate(got):
            self.assertAlmostEqual(p["start"], 2.5 * i, delta=0.4)
        self.assertIsNone(self.ws._next_work())          # heavy processing waits
        # Memory only: the recording folder holds what it always did.
        names = {p.name for p in rec.folder.iterdir()}
        self.assertLessEqual(names, {"journal.json", "heartbeat"} | {
            n for n in names if n.startswith("chunk-") and n.endswith(".uma")})
        proc = session._proc
        self.ws.stop_recording()
        # The draft worker has ended and its text is gone before the import.
        self.assertIsNone(self.ws._drafts)
        self.assertFalse(proc.is_alive())
        self.assertEqual(session.snapshot()["phrases"], [])
        with self.assertRaises(WorkspaceError):
            self.ws.recording_draft()
        self.assertTrue(self.ws.wait_idle(120))
        [m] = self.ws.meetings()
        self.assertTrue(self.ws.transcript(m["id"])["items"])   # then transcribed as usual

    def test_the_final_transcript_is_the_same_with_drafts_on_or_off(self):
        texts = []
        for on in (False, True):
            self.ws.close()
            self.root = Path(self.tmp.name) / ("on" if on else "off")
            self.ws = self.make()
            self.record(drafts_on=on)
            if on:
                self.assertTrue(wait_for(self.running))
            self.holder["stream"].play(self.speech(6))
            if on:
                self.assertTrue(wait_for(lambda: len(self.draft_phrases()) == 6))
            self.ws.stop_recording()
            self.assertTrue(self.ws.wait_idle(120))
            [m] = self.ws.meetings()
            texts.append([i["text"] for i in self.ws.transcript(m["id"])["items"]])
        self.assertTrue(texts[0])
        self.assertEqual(texts[0], texts[1])

    def test_pause_ends_the_phrase_in_progress(self):
        self.record()
        self.assertTrue(wait_for(self.running))
        self.holder["stream"].play(tone(2.0, self.RATE))        # no silence after it
        time.sleep(0.5)
        self.assertEqual(self.draft_phrases(), [])
        self.ws.pause_recording()
        self.assertTrue(wait_for(lambda: len(self.draft_phrases()) == 1))
        self.ws.resume_recording()
        self.ws.stop_recording()

    def test_policy_or_the_switch_turned_off_mid_recording_stops_and_clears(self):
        rec = self.record()
        self.assertTrue(wait_for(self.running))
        self.holder["stream"].play(self.speech(1))
        self.assertTrue(wait_for(lambda: self.draft_phrases()))
        proc = self.ws._drafts._proc
        self.set_policy({"AllowLiveDrafts": False})
        self.ws._policy_tick()
        self.assertIsNone(self.ws._drafts)
        self.assertFalse(proc.is_alive())
        self.assertEqual(self.ws.recording_draft()["phrases"], [])
        self.assertIs(self.ws._recording, rec)                 # the recording carries on
        self.set_policy({}, policy.UNREADABLE)                 # unreadable: still nothing
        self.ws._policy_tick()
        self.assertIsNone(self.ws._drafts)
        self.set_policy()                                      # allowed again: back on
        self.ws._policy_tick()
        self.assertTrue(wait_for(self.running))
        proc = self.ws._drafts._proc
        self.ws.set_settings({"live_drafts": False})           # the user's switch
        self.ws._policy_tick()
        self.assertIsNone(self.ws._drafts)
        self.assertFalse(proc.is_alive())
        self.assertEqual(self.ws.recording_draft()["state"], "off")
        self.ws.stop_recording()

    def test_battery_pauses_drafts_and_mains_power_resumes_them(self):
        self.record()
        self.assertTrue(wait_for(self.running))
        self.holder["stream"].play(self.speech(1))
        self.assertTrue(wait_for(lambda: len(self.draft_phrases()) == 1))
        self.ws._battery = True
        self.ws._policy_tick()
        session = self.ws._drafts
        self.assertEqual(session.state, drafts.BATTERY)
        self.assertIsNone(session.pid)                          # memory freed
        self.assertEqual(len(self.draft_phrases()), 1)          # the text so far is kept
        self.assertEqual(self.ws.recording_draft()["note"], drafts.NOTES[drafts.BATTERY])
        self.holder["stream"].play(self.speech(1))              # not drafted on battery
        self.ws._battery = False
        self.ws._policy_tick()
        self.assertTrue(wait_for(self.running))
        self.holder["stream"].play(self.speech(1))
        self.assertTrue(wait_for(lambda: len(self.draft_phrases()) == 2))
        self.assertGreater(self.draft_phrases()[1]["start"], 4.5)   # placed in the recording
        self.ws.stop_recording()

    def test_discard_and_a_recording_that_stops_itself_end_drafts(self):
        for ending in ("discard", "device lost", "policy stop"):
            with self.subTest(ending=ending):
                self.set_policy()
                if self.ws._threads:
                    self.ws.start_recording(3, consent=CONSENT)
                else:
                    self.record()
                rec = self.ws._recording
                self.assertTrue(wait_for(self.running))
                proc = self.ws._drafts._proc
                if ending == "discard":
                    self.ws.discard_recording()
                elif ending == "device lost":
                    rec._device_lost()
                else:
                    self.set_policy({"AllowMicrophoneRecording": False})
                    self.ws._policy_tick()
                self.assertTrue(wait_for(lambda: self.ws._recording is None
                                         and self.ws._drafts is None, timeout=30))
                self.assertFalse(proc.is_alive())
        self.set_policy()

    def test_a_model_download_or_removal_ends_drafts_for_the_recording(self):
        self.record()
        self.assertTrue(wait_for(self.running))
        proc = self.ws._drafts._proc
        self.ws._before_model_change()                  # setup.Downloads' before_install
        self.assertIsNone(self.ws._drafts)
        self.assertFalse(proc.is_alive())
        self.ws._policy_tick()                          # not started again for this recording
        self.assertIsNone(self.ws._drafts)
        self.assertEqual(self.ws.recording_draft()["state"], "ended")
        self.ws.stop_recording()
        self.assertIs(self.ws.downloads.before_install.__func__,
                      Workspace._before_model_change)

    def test_closing_the_app_ends_the_draft_worker(self):
        self.record()
        self.assertTrue(wait_for(self.running))
        proc = self.ws._drafts._proc
        self.ws.close()
        self.assertFalse(proc.is_alive())
        self.ws = self.make()                           # tearDown closes this one

    def test_draft_text_never_reaches_the_logs(self):
        from fake_worker_engines import MARKER
        for engine in ("draft_marker", "draft_raises_marker"):
            with self.subTest(engine=engine):
                self.ws.draft_model = FAKE + engine
                if self.ws._threads:
                    self.ws.start_recording(3, consent=CONSENT)
                else:
                    self.record()
                self.assertTrue(wait_for(self.running))
                self.holder["stream"].play(self.speech(2))
                if engine == "draft_marker":
                    self.assertTrue(wait_for(lambda: len(self.draft_phrases()) == 2))
                    self.assertIn(MARKER, self.draft_phrases()[0]["text"])
                else:
                    self.assertTrue(wait_for(lambda: self.ws._drafts.skipped_s > 0))
                self.ws.stop_recording()
                self.assertTrue(self.ws.wait_idle(120))
        logs = self.ws.root / "logs"
        self.assertTrue((logs / "draft-worker.log").exists())   # incidents were written
        for path in logs.iterdir():
            self.assertNotIn(MARKER.lower(), path.read_text("utf-8", errors="replace").lower(),
                             path.name)
        from workspace import diag
        records = diag.shareable_records(logs)
        self.assertTrue(any(r.get("kind") == "drafts" for r in records))
        self.assertTrue(any(r.get("op") == "draft" for r in records))   # the incidents

    # -- round 1 review of PR #290 ------------------------------------------------
    # These run the policy tick and processing checks by hand (the app's threads
    # are not started), so each step happens exactly when the test says.

    def hold_shutdown(self):
        """The first main-worker shutdown (the one _start_drafts makes) waits
        until released; returns (entered, release)."""
        entered, release = threading.Event(), threading.Event()
        real, first = self.ws.handle.shutdown, [True]

        def shutdown():
            if first[0]:
                first[0] = False
                entered.set()
                release.wait(30)
            real()
        patch = mock.patch.object(self.ws.handle, "shutdown", side_effect=shutdown)
        patch.start()
        self.addCleanup(patch.stop)
        return entered, release

    def tick_in_background(self):
        t = threading.Thread(target=self.ws._policy_tick, daemon=True)
        t.start()
        return t

    def test_processing_waits_for_the_draft_worker_after_the_recording_ends(self):
        # D4: between the recording ending and its draft worker exiting, only
        # the draft session holds processing back.
        ws = self.ws
        ws.start_recording(3, consent=CONSENT)            # drafts off: a meeting to process
        self.holder["stream"].play(self.speech(1))
        ws.stop_recording()
        _, folder = ws._imports.get_nowait()
        ws._import_recording_folder(Path(folder))
        job = ws._next_work()
        self.assertEqual(job[0], "transcribe")
        ws.settings.update(live_drafts=True)
        ws.start_recording(3, consent=CONSENT)
        ws._policy_tick()
        self.assertTrue(wait_for(self.running))
        session = ws._drafts
        entered, release = threading.Event(), threading.Event()
        real_end = session._end_process

        def held_end(*args, **kwargs):
            entered.set()
            release.wait(30)
            real_end(*args, **kwargs)
        session._end_process = held_end
        stopper = threading.Thread(target=ws.stop_recording)
        stopper.start()
        try:
            self.assertTrue(entered.wait(30))
            self.assertIsNone(ws._recording)                 # the recording has ended...
            self.assertIsNone(ws._next_work())               # ...its draft worker has not
        finally:
            release.set()
            stopper.join(60)
        self.assertIsNone(ws._drafts)
        self.assertEqual(ws._next_work(), job)               # then processing carries on

    def test_the_processing_loop_checks_the_draft_worker_again(self):
        ws = self.ws
        ws._integrity_ok.set()
        ran = []
        ws._drafts = object()                       # a draft session not yet ended
        try:
            with mock.patch.object(ws, "_next_work", return_value=("transcribe", "m", "v")), \
                    mock.patch.object(ws, "_maybe_purge"), \
                    mock.patch.object(ws, "_run", side_effect=lambda *a: ran.append(a)):
                loop = threading.Thread(target=ws._process_loop, daemon=True)
                loop.start()
                ws._wake.set()
                time.sleep(1.0)
                ws._closing.set()
                ws._wake.set()
                loop.join(10)
        finally:
            ws._drafts = None
        self.assertEqual(ran, [])

    def test_the_first_words_are_drafted_while_the_main_worker_lets_go(self):
        ws = self.ws
        entered, release = self.hold_shutdown()
        ws.settings.update(live_drafts=True)
        ws.start_recording(3, consent=CONSENT)
        ticker = self.tick_in_background()
        try:
            self.assertTrue(entered.wait(30))
            self.holder["stream"].play(self.speech(1))     # while the main worker lets go
        finally:
            release.set()
            ticker.join(60)
        self.assertTrue(wait_for(lambda: len(self.draft_phrases()) == 1, timeout=30))
        self.assertAlmostEqual(self.draft_phrases()[0]["start"], 0.0, delta=0.4)
        ws.stop_recording()

    def test_a_download_that_begins_as_drafts_start_ends_them(self):
        ws = self.ws
        entered, release = self.hold_shutdown()
        started = []
        real_start = drafts.DraftSession.start

        def start(session):
            started.append(real_start(session))
            return started[-1]
        ws.settings.update(live_drafts=True)
        with mock.patch.object(drafts.DraftSession, "start", start):
            ws.start_recording(3, consent=CONSENT)
            ticker = self.tick_in_background()
            try:
                self.assertTrue(entered.wait(30))
                ws._before_model_change()                  # setup.Downloads' before_install
            finally:
                release.set()
                ticker.join(60)
            self.assertIsNone(ws._drafts)
            self.assertFalse(any(started))                 # no draft worker ever started
            ws._policy_tick()
            self.assertIsNone(ws._drafts)
        self.assertEqual(ws.recording_draft()["state"], "ended")
        ws.stop_recording()

    def test_a_download_that_begins_just_after_the_tick_checks_is_not_missed(self):
        # The download ends drafts between the tick's checks and the start:
        # _start_drafts checks again under the lock, so no session is made.
        ws = self.ws
        ws.settings.update(live_drafts=True)
        real_ready = ws._draft_model_ready

        def ready():
            ok = real_ready()
            ws._before_model_change()                     # setup.Downloads' before_install
            return ok
        with mock.patch.object(drafts, "DraftSession") as made, \
                mock.patch.object(ws, "_draft_model_ready", ready):
            ws.start_recording(3, consent=CONSENT)
            ws._policy_tick()
            self.assertIsNone(ws._drafts)
            ws.stop_recording()
        made.assert_not_called()

    def test_drafts_wait_while_a_model_is_downloaded_or_removed(self):
        ws = self.ws
        ws.settings.update(live_drafts=True)
        with mock.patch.object(drafts, "DraftSession") as made:
            ws.start_recording(3, consent=CONSENT)
            with mock.patch.object(ws.downloads, "busy", return_value=True):
                ws._policy_tick()
            self.assertEqual(ws.recording_draft()["state"], "busy")
            ws._removing = "some model"
            try:
                ws._policy_tick()
            finally:
                ws._removing = None
            self.assertEqual(ws.recording_draft()["state"], "busy")
        made.assert_not_called()
        ws._policy_tick()                                   # done: drafts start
        self.assertTrue(wait_for(self.running))
        ws.stop_recording()

    def test_pause_ends_the_phrase_after_the_audio_queued_before_it(self):
        self.record()
        self.assertTrue(wait_for(self.running))
        rec = self.ws._recording
        real, gate, first = rec.tap, threading.Event(), [True]

        def held(start, block):                           # the writer falls behind
            if first[0]:
                first[0] = False
                gate.wait(30)
            real(start, block)
        rec.tap = held
        self.holder["stream"].play(tone(2.0, self.RATE))    # queued behind the first block
        self.ws.pause_recording()
        gate.set()
        self.assertTrue(wait_for(lambda: len(self.draft_phrases()) == 1, timeout=30))
        self.assertAlmostEqual(self.draft_phrases()[0]["end"], 2.0, delta=0.3)
        self.ws.resume_recording()
        self.ws.stop_recording()

    def test_stop_ends_capture_before_the_drafts(self):
        rec = self.record()
        self.assertTrue(wait_for(self.running))
        order, session = [], self.ws._drafts
        real_rec_stop, real_session_stop = rec.stop, session.stop

        def rec_stop():
            order.append("recording")
            return real_rec_stop()

        def session_stop():
            order.append("drafts")
            real_session_stop()
        rec.stop, session.stop = rec_stop, session_stop
        self.ws.stop_recording()
        self.assertEqual(order, ["recording", "drafts"])

    def test_a_recording_that_cannot_be_saved_still_ends_its_drafts(self):
        rec = self.record()
        self.assertTrue(wait_for(self.running))
        proc = self.ws._drafts._proc
        real_rec_stop = rec.stop
        rec.stop = mock.Mock(side_effect=recorder.RecorderError("Saving did not finish."))
        try:
            self.ws.stop_recording()
            self.assertIsNone(self.ws._drafts)
            self.assertFalse(proc.is_alive())
        finally:
            real_rec_stop()


# -- the page part (#104 slice 3): the live mix, the bridge, the recording card ----------

@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class LiveMixTests(unittest.TestCase):
    """The draft's live mix of the microphone and the computer track, in the
    draft worker (the owner's decision of 22 Oct 2026)."""

    def test_the_mix_is_the_mean_of_both_tracks_at_the_microphone_rate(self):
        mix = drafts.LiveMix(16000)
        voice = tone(1.0, 16000, freq=200.0)
        other = tone(1.0, 48000, freq=300.0, amp=0.2)
        self.assertEqual(mix.computer(0, 48000, other), [])     # waits for the microphone
        [(start, out)] = mix.mic(0, voice)                      # covered: on at once
        self.assertEqual((start, len(out)), (0, 16000))
        resampled = np.interp(np.arange(16000) * 3.0, np.arange(48000), other.astype(float))
        expected = (voice.astype(float) + resampled) / 2
        self.assertLessEqual(float(np.max(np.abs(out - expected))), 1.0)
        self.assertEqual(out.dtype, np.int16)

    def test_microphone_audio_waits_for_the_computer_then_goes_on_alone(self):
        mix = drafts.LiveMix(16000)
        half = 8000
        self.assertEqual(mix.computer(0, 16000, silence(0.5)), [])
        self.assertEqual([s for s, _ in mix.mic(0, tone(0.5))], [0])
        late = tone(0.5, freq=330.0)
        self.assertEqual(mix.mic(half, late), [])               # its computer audio may come
        self.assertEqual(mix.mic(2 * half, tone(0.5)), [])       # still within MIX_HOLD_S
        [(start, out)] = mix.mic(3 * half, tone(0.5))            # waited long enough
        self.assertEqual(start, half)
        np.testing.assert_array_equal(out, late)                 # alone and unchanged
        self.assertEqual([s for s, _ in mix.flush()], [2 * half, 3 * half])

    def test_off_lets_the_held_audio_go_and_stops_waiting(self):
        mix = drafts.LiveMix(16000)
        mix.computer(0, 16000, silence(0.2))
        self.assertEqual(mix.mic(16000, tone(0.5)), [])
        self.assertEqual([s for s, _ in mix.off()], [16000])
        self.assertFalse(mix.mixing)
        [(start, out)] = mix.mic(24000, tone(0.5))               # straight on
        self.assertEqual(start, 24000)

    def test_computer_audio_far_from_the_microphone_is_not_used(self):
        mix = drafts.LiveMix(16000)
        mix.mic(0, tone(0.5))
        self.assertEqual(mix.computer(16000 * 20, 16000, tone(0.5)), [])
        self.assertFalse(mix.mixing)
        self.assertEqual(len(mix.mic(8000, tone(0.5))), 1)

    def test_the_worker_refuses_computer_audio_out_of_shape(self):
        # serve() reads only messages in a known shape; others are ignored.
        for bad in ((True, 48000, b"\0\0"), (0, 1, b"\0\0"), (0, 48000, "pcm"),
                    (0, 48000, b""), (0.5, 48000, b"\0\0"),
                    (0, 48000, b"\0" * (4 * drafts.MAX_BATCH_FRAMES))):
            with self.subTest(bad=repr(bad)[:40]):
                ok = (drafts._whole(bad[0], -drafts.MAX_FRAME, drafts.MAX_FRAME)
                      and drafts._whole(bad[1], 8000, drafts.MAX_RATE)
                      and drafts._sized(bad[2]))
                self.assertFalse(ok)
        self.assertTrue(drafts._whole(-5, -drafts.MAX_FRAME, drafts.MAX_FRAME)
                        and drafts._whole(48000, 8000, drafts.MAX_RATE)
                        and drafts._sized(b"\0\0"))


class _Loopback:
    device_name = "Test speakers"

    def __init__(self, source, callback, notify):
        self.callback = callback

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class ComputerTapTests(unittest.TestCase):
    """The recorder hands computer blocks to the draft's live mix, placed on
    the microphone's time line, from its writer thread."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.holder, self.held = {}, {}
        self.now = [100.0]

        def open_loopback(source, callback, notify):
            self.held["comp"] = _Loopback(source, callback, notify)
            return self.held["comp"]
        self.rec = recorder.Recording(Path(self.tmp.name) / "two", 3, "Test microphone", 16000,
                                      open_stream=opener(self.holder),
                                      tick=lambda: self.now[0],
                                      computer={"open": open_loopback,
                                                "source": {"kind": "endpoint"}, "rate": 48000},
                                      key=bytes(range(32)))
        self.mic, self.comp = [], []
        self.rec.tap = lambda start, block: self.mic.append(start)
        self.rec.tap_computer = lambda at, block: self.comp.append((at, len(block)))

    def mic_block(self):
        self.now[0] += 0.1
        n = len(self.mic)
        self.holder["stream"].callback(silence(0.1).reshape(-1, 1), 1600, None, None)
        self.assertTrue(wait_for(lambda: len(self.mic) == n + 1, timeout=10))

    def comp_block(self, stamp):
        self.held["comp"].callback(tone(0.05, 48000), stamp)

    def test_computer_blocks_are_placed_on_the_microphone_time_line(self):
        self.rec.start()
        self.addCleanup(self.rec.stop)
        self.comp_block(self.now[0])                    # before any microphone block: not used
        time.sleep(0.5)
        self.assertEqual(self.comp, [])
        for i in range(5):
            self.mic_block()                             # its stamp: now - 0.1
            self.comp_block(self.now[0] - 0.1 + 0.03)   # 30 ms after that block began
            self.assertTrue(wait_for(lambda i=i: len(self.comp) == i + 1, timeout=10))
        self.assertEqual(self.comp, [(i * 1600 + 480, 2400) for i in range(5)])
        self.rec.pause()
        self.rec.resume()
        self.comp_block(self.now[0] + 0.5)              # after the pause, before the microphone
        time.sleep(0.5)
        self.assertEqual(len(self.comp), 5)
        journal = self.rec.stop()
        self.assertEqual(journal["tracks"]["computer"]["frames"], 7 * 2400)   # all saved

    def test_a_failing_computer_tap_is_turned_off_and_the_recording_is_untouched(self):
        def broken(at, block):
            raise RuntimeError("draft trouble")
        self.rec.tap_computer = broken
        self.rec.start()
        self.addCleanup(self.rec.stop)
        self.mic_block()
        self.comp_block(self.now[0] - 0.1)
        self.assertTrue(wait_for(lambda: self.rec.tap_computer is None, timeout=10))
        self.mic_block()
        journal = self.rec.stop()
        self.assertEqual((journal["frames"], journal["tracks"]["computer"]["frames"]),
                         (3200, 2400))
        self.assertEqual(journal["events"], [])


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class PagePartTests(DraftWorkspaceCase):
    """Slice 3: the setting for what the draft follows, the live mix in the
    app, the bridge method and the recording card's status (no text)."""

    def computer_audio(self, choice):
        from workspace import app as app_module
        patch = mock.patch.object(app_module, "COMPUTER_AUDIO_STARTS", True)
        patch.start()
        self.addCleanup(patch.stop)
        self.held = {}

        def open_loopback(source, callback, notify):
            self.held["comp"] = _Loopback(source, callback, notify)
            return self.held["comp"]
        self.ws.open_loopback = open_loopback
        self.ws.settings.update(computer_audio=True, live_drafts_audio=choice)

    def play_both(self, mic_pcm, comp_pcm):
        """Both tracks in real time, 0.1 s at a time, as the devices would."""
        stream, comp = self.holder["stream"], self.held["comp"]
        n, c = self.RATE // 10, 4800
        for i in range(len(mic_pcm) // n):
            now = time.monotonic()
            stream.callback(mic_pcm[i * n:(i + 1) * n].reshape(-1, 1), n, None, None)
            comp.callback(comp_pcm[i * c:(i + 1) * c], now - 0.1)
            time.sleep(0.1)

    def test_the_setting_defaults_to_the_microphone_and_takes_two_choices(self):
        self.assertEqual(self.ws.settings["live_drafts_audio"], "microphone")
        self.assertEqual(self.ws.set_settings({"live_drafts_audio": "mix"})["live_drafts_audio"],
                         "mix")
        for bad in ("both", "", None, 1, True):
            with self.assertRaisesRegex(WorkspaceError, "Choose the microphone"):
                self.ws.set_settings({"live_drafts_audio": bad})
        self.assertEqual(self.ws.settings["live_drafts_audio"], "mix")
        model = self.ws.get_settings()["live_drafts_model"]
        self.assertEqual(set(model), {"state", "label", "offered"})

    def test_the_switch_can_be_turned_on_now_the_page_shows_drafts(self):
        # As shipped, not as the set-up patches it for the other tests.
        self.assertIs(SHIPPED_ON_PAGE, True)
        with mock.patch.object(drafts, "ON_PAGE", SHIPPED_ON_PAGE):
            self.ws.set_settings({"live_drafts": True})
        self.assertTrue(self.ws.settings["live_drafts"])

    def test_with_the_mix_chosen_the_draft_hears_the_computer_audio(self):
        # Silence at the microphone, speech from the computer: only the mix
        # drafts it. The microphone choice drafts nothing.
        # Two bursts, then silence for long enough that the mix (which waits
        # up to MIX_HOLD_S for the computer's audio) lets the last phrase end.
        speech = np.concatenate([tone(1.5, 48000, freq=300.0), silence(1.0, 48000),
                                 tone(1.5, 48000, freq=300.0), silence(2.5, 48000)])
        for choice, expected in (("mix", 2), ("microphone", 0)):
            with self.subTest(choice=choice):
                self.ws.close()
                self.root = Path(self.tmp.name) / choice
                self.ws = self.make()
                self.computer_audio(choice)
                rec = self.record()
                self.assertTrue(wait_for(self.running))
                session = self.ws._drafts
                self.assertEqual(session.mixing, choice == "mix")
                self.assertEqual(rec.tap_computer == session.tap_computer, choice == "mix")
                self.play_both(silence(6.5, self.RATE), speech)
                if expected:
                    self.assertTrue(wait_for(
                        lambda n=expected: len(self.draft_phrases()) == n))
                else:
                    time.sleep(2.0)
                self.assertEqual(len(self.draft_phrases()), expected)
                self.ws.stop_recording()
                self.assertTrue(self.ws.wait_idle(120))

    def test_the_choice_and_the_computer_policy_apply_mid_recording(self):
        self.computer_audio("microphone")
        rec = self.record()
        self.assertTrue(wait_for(self.running))
        session = self.ws._drafts
        self.assertIsNone(rec.tap_computer)
        self.ws.set_settings({"live_drafts_audio": "mix"})
        self.ws._policy_tick()
        self.assertTrue(session.mixing)
        self.assertEqual(rec.tap_computer, session.tap_computer)
        self.ws.set_settings({"live_drafts_audio": "microphone"})
        self.ws._policy_tick()
        self.assertFalse(session.mixing)
        self.assertIsNone(rec.tap_computer)
        self.ws.set_settings({"live_drafts_audio": "mix"})
        self.ws._policy_tick()
        self.assertTrue(session.mixing)
        # IT turns computer audio off: that track stops, and the draft follows
        # the microphone again (K3).
        self.set_policy({"AllowComputerAudioRecording": False})
        self.ws._policy_tick()
        self.ws._policy_tick()
        self.assertFalse(rec.computer_on)
        self.assertFalse(session.mixing)
        self.assertIsNone(rec.tap_computer)
        self.assertIs(self.ws._drafts, session)                 # drafts carry on
        self.ws.stop_recording()
        self.assertIsNone(rec.tap_computer)

    def test_without_computer_audio_the_mix_choice_changes_nothing(self):
        self.ws.settings.update(live_drafts_audio="mix")
        rec = self.record()
        self.assertTrue(wait_for(self.running))
        self.ws._policy_tick()
        self.assertFalse(self.ws._drafts.mixing)
        self.assertIsNone(rec.tap_computer)
        self.ws.stop_recording()

    def test_recording_status_carries_the_draft_state_and_counts_never_text(self):
        self.assertIsNone(self.ws.recording_status()["draft"])          # nothing recorded
        self.record(drafts_on=False)
        self.ws._policy_tick()
        self.assertIsNone(self.ws.recording_status()["draft"])          # the switch is off
        self.set_policy({"AllowLiveDrafts": False})
        self.ws.settings.update(live_drafts=True)
        self.ws._policy_tick()
        draft = self.ws.recording_status()["draft"]
        self.assertEqual(draft["state"], "managed")
        self.assertEqual(draft["note"], drafts.REASONS["managed"])
        self.set_policy()
        self.ws._policy_tick()
        self.assertTrue(wait_for(self.running))
        self.holder["stream"].play(self.speech(2))
        self.assertTrue(wait_for(lambda: self.ws.recording_status()["draft"]["count"] == 2))
        draft = self.ws.recording_status()["draft"]
        self.assertEqual(set(draft), {"state", "note", "count", "generation", "mix"})
        self.assertEqual(draft["state"], drafts.RUNNING)
        for value in draft.values():                                     # no phrase text
            self.assertNotIn("phrase", str(value))
        self.ws.stop_recording()
        self.assertIsNone(self.ws.recording_status()["draft"])

    def test_the_bridge_method_checks_its_argument_and_returns_text_only(self):
        from workspace import api
        self.assertIn("recording_draft", api.API_METHODS)
        self.assertNotIn("recording_draft", api.LOCKED_METHODS)      # nothing records then
        page = api.make_api(self.ws)
        self.assertEqual(page.recording_draft(0),
                         {"ok": False, "error": "Nothing is being recorded."})
        self.record()
        self.assertTrue(wait_for(self.running))
        for bad in (-1, True, 1.5, "0", None, [0], 10 ** 8, float("nan")):
            with self.subTest(bad=bad):
                self.assertEqual(page.recording_draft(bad),
                                 {"ok": False, "error": "Out of range."})
        self.holder["stream"].play(self.speech(2))
        self.assertTrue(wait_for(lambda: len(self.draft_phrases()) == 2))
        got = page.recording_draft(1)
        self.assertTrue(got["ok"])
        self.assertEqual((got["value"]["since"], len(got["value"]["phrases"])), (1, 1))
        [phrase] = got["value"]["phrases"]
        self.assertEqual(set(phrase), {"kind", "text", "start", "end"})
        self.assertIsInstance(phrase["text"], str)
        self.ws.stop_recording()

    def test_the_page_shows_draft_text_as_text_only_and_does_not_announce_it(self):
        ui = Path(__file__).resolve().parents[1] / "workspace" / "ui"
        html = (ui / "index.html").read_text(encoding="utf-8")
        js = (ui / "app.js").read_text(encoding="utf-8")
        import re

        def tag(element_id):
            return re.search(r'<[^<>]*id="' + element_id + r'"[^<>]*>', html).group(0)
        self.assertIn('aria-live="off"', tag("rec-draft-box"))       # not read out as it grows
        self.assertIn('role="status"', tag("rec-draft-status"))      # its state is
        self.assertIn("Hide it before sharing your screen.", html)   # the owner's line (T31)
        body = js[js.index("function draftLine"):js.index("function toggleDraft")]
        self.assertNotRegex(body, r"innerHTML|outerHTML|insertAdjacentHTML|\.html\(")
        self.assertIn("textContent: p.text", body)
        self.assertIn("user-select: none", (ui / "app.css").read_text(encoding="utf-8"))



if __name__ == "__main__":
    unittest.main()
