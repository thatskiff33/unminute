# Two-track recording groundwork (#108): microphone and computer audio, with
# fakes and synthetic buffers only (no device is opened, nothing is played).
#   - workspace/loopback.py: mono conversion, the pacer that keeps a silent
#     computer track continuous in time, and source checks;
#   - workspace/recorder.py: the two-track journal (format 2), the computer
#     track's chunks, marks and events, and its rules (silence is not a lost
#     device; a failure or policy stops that track only);
#   - workspace/mixdown.py: drift and offset corrected, resampled, ONE mix;
#   - workspace/app.py: the policy key and the user's switch, and the import
#     of a two-track recording as one meeting.
#   python tools/run_tests.py

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))      # fake_worker_engines

from workspace import consent  # noqa: E402  (stdlib only)

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
RATE = 16000
CRATE = 48000
CONSENT = consent.recording_record(clock=lambda: 1000.0)
ROOT = Path(__file__).resolve().parents[1]

if HAVE_NUMPY:
    import numpy as np

    from workspace import loopback, mixdown, recorder


def tone(n, rate, hz, start=0, amp=8000):
    t = (np.arange(n) + start) / rate
    return (np.sin(2 * np.pi * hz * t) * amp).astype(np.int16)


class FakeMic:
    """Stands in for a sounddevice InputStream; the test pushes audio."""

    def __init__(self, device, rate, callback, finished):
        self.rate, self.callback, self.finished = rate, callback, finished
        self.stopped = self.closed = False
        self.fed = 0

    def start(self):
        pass

    def stop(self):
        self.stopped = True
        self.finished()

    def close(self):
        self.closed = True

    def feed(self, seconds, hz=220):
        n = int(self.rate * recorder.BLOCK_S)
        for _ in range(int(round(seconds / recorder.BLOCK_S))):
            self.callback(tone(n, self.rate, hz, self.fed).reshape(-1, 1), n, None, None)
            self.fed += n


class FakeLoopback:
    """Stands in for loopback.LoopbackStream: the test pushes computer audio
    and device events, as the reader thread would."""

    def __init__(self, source, callback, notify, fail=False):
        self.source, self.callback, self.notify = source, callback, notify
        self.fail, self.started, self.stopped, self.closed = fail, False, False, False
        self.device_name = "Test speakers"
        self.fed = 0

    def start(self):
        if self.fail:
            raise loopback.LoopbackError("no output")
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def feed(self, seconds, hz=1000, silent=False):
        n = int(CRATE * recorder.BLOCK_S)
        for _ in range(int(round(seconds / recorder.BLOCK_S))):
            block = np.zeros(n, np.int16) if silent else tone(n, CRATE, hz, self.fed)
            self.callback(block, time.monotonic() - n / CRATE)
            self.fed += n


def opener(holder, key, cls, **kw):
    def open_(*args):
        holder[key] = cls(*args, **kw)
        return holder[key]
    return open_


# -- loopback: pure helpers ------------------------------------------------------------

@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class LoopbackHelperTests(unittest.TestCase):
    def test_float_stereo_becomes_the_mean_in_16_bit_with_clipping(self):
        frames = np.array([[0.5, -0.5], [1.0, 1.0], [2.0, 2.0], [-3.0, -1.0], [0.25, 0.75]],
                          dtype="<f4")
        out = loopback.to_mono_int16(frames.tobytes(), 2, "float32")
        self.assertEqual(out.dtype, np.int16)
        self.assertEqual(out.tolist(), [0, 32767, 32767, -32768, 16384])
        pcm = np.array([[1000, 3000], [-2000, 0]], dtype="<i2")
        self.assertEqual(loopback.to_mono_int16(pcm.tobytes(), 2, "int16").tolist(),
                         [2000, -1000])
        with self.assertRaises(loopback.LoopbackError):
            loopback.to_mono_int16(b"\0" * 12, 1, "int24")

    def test_audio_that_flows_is_never_padded_and_keeps_its_arrival_times(self):
        p = loopback.Pacer(CRATE, now=100.0)
        out = []
        cb = lambda block, stamp: out.append((len(block), round(stamp, 4)))  # noqa: E731
        # The first audio comes 0.1 s after the start: that lead is filled.
        loopback.deliver(p, [np.ones(480, np.int16)], 100.11, cb)
        self.assertEqual(out, [(4800, 100.0), (480, 100.1)])
        out.clear()
        # Then audio flows: a slightly fast device clock is kept in the stamps
        # (the mix corrects it), and no silence is ever inserted.
        now = 100.11
        for _ in range(100):
            now += 0.05
            loopback.deliver(p, [np.ones(2401, np.int16)], now, cb)
        self.assertEqual({n for n, _ in out}, {2401})
        self.assertAlmostEqual(out[-1][1], now - 2401 / CRATE, places=4)

    def test_silence_while_nothing_plays_is_filled_by_the_clock(self):
        p = loopback.Pacer(CRATE, now=0.0)
        out = []
        cb = lambda block, stamp: out.append((len(block), int(np.count_nonzero(block)),  # noqa: E731
                                             round(stamp, 3)))
        now = 0.0
        for _ in range(40):                       # 2 s with nothing arriving
            now += 0.05
            loopback.deliver(p, [], now, cb)
        self.assertTrue(all(nz == 0 for _, nz, _ in out))
        filled = sum(n for n, _, _ in out)
        self.assertLessEqual(abs(filled - 2.0 * CRATE), loopback.IDLE_PAD_S * CRATE)
        stamps = [s for _, _, s in out]           # contiguous, in order
        for (n, _, s), s2 in zip(out, stamps[1:], strict=False):
            self.assertAlmostEqual(s + n / CRATE, s2, places=3)
        # Audio again: the time since the last filled silence is filled first.
        out.clear()
        loopback.deliver(p, [np.ones(480, np.int16)], now + 0.15, cb)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][1], 0)
        self.assertAlmostEqual(out[0][2] + out[0][0] / CRATE, out[1][2], places=3)

    def test_only_known_sources_are_accepted(self):
        self.assertEqual(loopback.check_source({"kind": "endpoint"}), {"kind": "endpoint"})
        self.assertEqual(loopback.check_source({"kind": "process", "pid": 42}),
                         {"kind": "process", "pid": 42})
        for bad in (None, "endpoint", {"kind": "endpoint", "id": "x"}, {"kind": "process"},
                    {"kind": "process", "pid": True}, {"kind": "process", "pid": -1},
                    {"kind": "process", "pid": "42"}, {"kind": "file", "path": "C:\\x"}):
            with self.assertRaises(loopback.LoopbackError, msg=repr(bad)):
                loopback.check_source(bad)

    def test_loopback_is_not_loaded_by_the_window_process_at_start(self):
        # Imported only when a recording with computer audio starts (and its
        # mix only at import): never by the window modules at start-up.
        probe = ("import sys, workspace.app, workspace.api, workspace.shell\n"
                 "print(sorted(m for m in ('workspace.loopback', 'workspace.mixdown') "
                 "if m in sys.modules))")
        result = subprocess.run([sys.executable, "-B", "-c", probe], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            self.skipTest("the window modules cannot be imported here: "
                          + result.stderr[-300:])
        self.assertEqual(result.stdout.strip().splitlines()[-1], "[]")


# -- mixdown: one mix, lined up ----------------------------------------------------------

def write_track(folder, prefix, samples, chunk_s, rate):
    """Chunk files and their journal entries for a synthetic track."""
    chunks, step = [], int(chunk_s * rate)
    for i, a in enumerate(range(0, len(samples), step)):
        name = f"{prefix}{i:06d}.pcm"
        part = samples[a:a + step].astype("<i2")
        (Path(folder) / name).write_bytes(part.tobytes())
        chunks.append({"file": name, "frames": len(part)})
    return chunks


def peaks(x, rate, threshold):
    """Frames where short pulses are, one per pulse."""
    idx = np.flatnonzero(np.abs(x) > threshold)
    out = []
    for i in idx:
        if not out or i - out[-1][-1] > rate // 10:
            out.append([i])
        else:
            out[-1].append(i)
    return [int(np.mean(group)) for group in out]


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class MixdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.rng = np.random.default_rng(108)

    def journal(self, mic, comp, mic_marks, comp_marks):
        return {"rate": RATE, "format": 2, "marks": mic_marks,
                "chunks": write_track(self.folder, "chunk-", mic, 5, RATE),
                "tracks": {"computer": {"rate": CRATE, "marks": comp_marks,
                                        "chunks": write_track(self.folder, "sys-", comp, 5,
                                                              CRATE)}}}

    def drifting(self, seconds=200, ppm=100.0, offset=0.25):
        """The microphone from t=1000 at its nominal rate; the computer from
        t=1000+offset with a clock ppm fast. Pulses on the computer track at
        known real times, so where they land in the mix shows the alignment."""
        mic = np.zeros(seconds * RATE, np.int16)
        crate_true = CRATE * (1 + ppm * 1e-6)
        n = int((seconds - offset) * crate_true)
        comp = np.zeros(n, np.int16)
        times = [1010.0, 1060.0, 1110.0, 1160.0, 1190.0]
        for t in times:
            g = int((t - 1000.0 - offset) * crate_true)
            comp[g:g + 48] = 20000
        jitter = lambda: float(self.rng.uniform(-0.005, 0.005))  # noqa: E731
        mic_marks = [[f, round(1000.0 + f / RATE + jitter(), 4), 1 if f == 0 else 0]
                     for f in range(0, len(mic), 30 * RATE)]
        comp_marks = [[g, round(1000.0 + offset + g / crate_true + jitter(), 4),
                       1 if g == 0 else 0] for g in range(0, n, 30 * CRATE)]
        return mic, comp, mic_marks, comp_marks, times

    def test_drift_and_offset_are_corrected_to_within_10_ms(self):
        mic, comp, mm, cm, times = self.drifting()
        journal = self.journal(mic, comp, mm, cm)
        notes = []
        out, seconds = mixdown.write_mix(self.folder, journal, self.folder / "mix.wav", notes)
        self.assertEqual(notes, [])
        self.assertAlmostEqual(seconds, 200.0)
        with wave.open(str(out), "rb") as w:
            self.assertEqual((w.getnchannels(), w.getframerate(), w.getnframes()),
                             (1, RATE, len(mic)))
            mix = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        found = peaks(mix, RATE, 3000)
        self.assertEqual(len(found), len(times))
        for frame, t in zip(found, times, strict=True):
            self.assertLess(abs(frame / RATE - (t - 1000.0)), 0.010)

    def test_without_the_drift_fit_the_end_is_out_of_step(self):
        # The same tracks lined up at the nominal rates: 100 ppm over 190 s is
        # about 19 ms by the last pulse, so the fit above is what corrects it.
        mic, comp, mm, cm, times = self.drifting()
        lines = mixdown.nominal(len(comp), CRATE, t0=1000.25)
        reader = mixdown.TrackReader(self.folder, [("c.pcm", len(comp))])
        (self.folder / "c.pcm").write_bytes(comp.astype("<i2").tobytes())
        t = 1000.0 + np.arange(len(mic)) / RATE
        found = peaks(mixdown.computer_at(t, lines, reader), RATE, 3000)
        self.assertGreater(abs(found[-1] / RATE - (times[-1] - 1000.0)), 0.015)

    def test_a_pause_starts_a_new_segment_on_both_tracks(self):
        # 10 s, a 5 s pause, 10 s; the computer track's second part starts
        # 0.5 s after the microphone's (its first block after the resume came later).
        mic = tone(20 * RATE, RATE, 220, amp=1000)
        comp = np.zeros(20 * CRATE, np.int16)
        comp[int(5 * CRATE):int(5 * CRATE) + 48] = 20000           # at 1005.0
        comp[int(14 * CRATE):int(14 * CRATE) + 48] = 20000         # at 1015.5 + 4 = 1019.5
        mic_marks = [[0, 1000.0, 1], [10 * RATE, 1015.0, 1]]
        comp_marks = [[0, 1000.0, 1], [10 * CRATE, 1015.5, 1]]
        journal = self.journal(mic, comp, mic_marks, comp_marks)
        notes = []
        out, _ = mixdown.write_mix(self.folder, journal, self.folder / "mix.wav", notes)
        with wave.open(str(out), "rb") as w:
            mix = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        found = peaks(mix, RATE, 6000)
        self.assertEqual(len(found), 2)
        self.assertLess(abs(found[0] / RATE - 5.0), 0.005)
        self.assertLess(abs(found[1] / RATE - 14.5), 0.005)     # 1019.5 is 4.5 s into part two

    def test_the_mix_is_the_mean_of_the_tracks_and_silence_where_one_has_none(self):
        mic = np.full(4 * RATE, 1000, np.int16)
        comp = np.full(2 * CRATE, 3000, np.int16)                 # the computer stops at 2 s
        journal = self.journal(mic, comp, [[0, 50.0, 1]], [[0, 50.0, 1]])
        out, seconds = mixdown.write_mix(self.folder, journal, self.folder / "m.wav", [])
        with wave.open(str(out), "rb") as w:
            mix = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        self.assertEqual(seconds, 4.0)
        self.assertTrue(np.all(mix[100:2 * RATE - 100] == 2000))  # (1000 + 3000) / 2
        self.assertTrue(np.all(mix[2 * RATE + 100:] == 500))      # 1000 / 2: the computer is silent

    def test_marks_that_cannot_be_used_fall_back_to_start_times_and_say_so(self):
        mic = np.full(2 * RATE, 1000, np.int16)
        comp = np.full(2 * CRATE, 3000, np.int16)
        for mic_marks, comp_marks in (([[0, 5.0, 1], [100, 4.0, 0]], [[0, 5.0, 1]]),  # time back
                                      ([[0, "x", 1]], [[0, 5.0, 1]]),
                                      ([], [[0, 5.0, 1]]),
                                      ([[0, 5.0, 1]], [[0, float("nan"), 1]])):
            notes = []
            journal = self.journal(mic, comp, mic_marks, comp_marks)
            out, seconds = mixdown.write_mix(self.folder, journal, self.folder / "m.wav", notes)
            self.assertEqual(seconds, 2.0)
            self.assertEqual([n["kind"] for n in notes], ["mix_by_start"])
            with wave.open(str(out), "rb") as w:
                mix = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            self.assertTrue(np.all(mix[100:-100] == 2000))

    def test_an_implausible_clock_keeps_the_nominal_rate_and_says_so(self):
        mic = np.zeros(60 * RATE, np.int16)
        comp = np.zeros(60 * CRATE, np.int16)
        mic_marks = [[0, 0.0, 1], [30 * RATE, 30.0, 0]]
        comp_marks = [[0, 0.0, 1], [30 * CRATE, 33.0, 0]]          # 10 % slow: not a clock drift
        notes = []
        mixdown.write_mix(self.folder, self.journal(mic, comp, mic_marks, comp_marks),
                          self.folder / "m.wav", notes)
        self.assertEqual([n["kind"] for n in notes], ["mix_drift"])


# -- the recorder's two tracks ------------------------------------------------------------

@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class TwoTrackRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.holder = {}

    def start(self, fail=False):
        rec = recorder.Recording(
            self.root / "rec", 3, "Test microphone", RATE,
            open_stream=opener(self.holder, "mic", FakeMic),
            computer={"open": opener(self.holder, "comp", FakeLoopback, fail=fail),
                      "source": {"kind": "endpoint"}, "rate": CRATE})
        rec.start()
        self.addCleanup(rec.stop)
        return rec, self.holder["mic"], self.holder["comp"]

    def test_a_microphone_only_journal_is_unchanged(self):
        rec = recorder.Recording(self.root / "mic", 3, "Mic", RATE,
                                 open_stream=opener(self.holder, "mic", FakeMic))
        rec.start()
        self.addCleanup(rec.stop)
        self.holder["mic"].feed(1)
        journal = rec.stop()
        for key in ("format", "tracks", "marks"):
            self.assertNotIn(key, journal)
        self.assertIsNone(rec.status()["computer"])
        self.assertEqual(sorted(p.name for p in rec.folder.iterdir()),
                         ["chunk-000000.pcm", "heartbeat", "journal.json"])

    def test_both_tracks_are_saved_in_durable_chunks_with_a_format_2_journal(self):
        rec, mic, comp = self.start()
        self.assertTrue(comp.started)
        mic.feed(6)
        comp.feed(6)
        time.sleep(0.5)
        status = rec.status()
        self.assertTrue(status["computer"]["on"])
        self.assertGreater(status["computer"]["level_db"], -30)
        journal = rec.stop()
        self.assertTrue(comp.stopped and comp.closed and mic.closed)
        self.assertEqual(journal["format"], 2)
        track = journal["tracks"]["computer"]
        self.assertEqual((track["source"], track["rate"], track["state"], track["device_name"]),
                         ({"kind": "endpoint"}, CRATE, "stopped", "Test speakers"))
        self.assertEqual(track["frames"], comp.fed)
        self.assertEqual(journal["frames"], mic.fed)
        self.assertEqual([c["file"] for c in track["chunks"]], ["sys-000000.pcm", "sys-000001.pcm"])
        self.assertEqual(journal["marks"][0][0::2], [0, 1])       # the first block starts a segment
        self.assertEqual(track["marks"][0][0::2], [0, 1])
        on_disk = json.loads((rec.folder / "journal.json").read_text("utf-8"))
        self.assertEqual(on_disk["tracks"]["computer"]["frames"], comp.fed)
        wav, seconds = recorder.assemble_wav(rec.folder, journal)
        self.assertAlmostEqual(seconds, 6.0)
        with wave.open(str(wav), "rb") as w:
            self.assertEqual((w.getframerate(), w.getnframes()), (RATE, mic.fed))
            mix = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
        spectrum = np.abs(np.fft.rfft(mix[RATE:3 * RATE]))
        freqs = np.fft.rfftfreq(2 * RATE, 1 / RATE)
        level = lambda hz: spectrum[np.argmin(np.abs(freqs - hz))]  # noqa: E731
        self.assertGreater(level(220), 100 * level(600))           # the microphone's tone
        self.assertGreater(level(1000), 100 * level(600))          # and the computer's: one mix

    def test_silence_on_the_computer_track_is_not_a_lost_device(self):
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 0.6), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.1):
            rec, mic, comp = self.start()
            for _ in range(15):               # 1.5 s: the microphone flows, the computer is quiet
                mic.feed(0.1)
                time.sleep(0.1)
            self.assertEqual(rec.status()["state"], "recording")
            self.assertNotIn("device_lost", [e["kind"] for e in rec.journal["events"]])

    def test_computer_audio_never_keeps_a_lost_microphone_going(self):
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 0.6), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.1):
            rec, mic, comp = self.start()
            mic.feed(0.5)
            deadline = time.time() + 5
            while rec.status()["state"] == "recording" and time.time() < deadline:
                comp.feed(0.1)                # the computer flows; the microphone has gone
                time.sleep(0.1)
            self.assertEqual(rec.status()["state"], "device_lost")

    def test_pause_and_resume_cover_both_tracks_and_start_new_segments(self):
        rec, mic, comp = self.start()
        mic.feed(1)
        comp.feed(1)
        rec.pause()
        mic.feed(1)
        comp.feed(1)                          # not recorded while paused
        rec.resume()
        mic.feed(1)
        comp.feed(1)
        journal = rec.stop()
        self.assertEqual(journal["frames"], 2 * RATE)
        self.assertEqual(journal["tracks"]["computer"]["frames"], 2 * CRATE)
        self.assertEqual([m[2] for m in journal["marks"]], [1, 1])
        self.assertEqual([m[0] for m in journal["marks"]], [0, RATE])
        self.assertEqual([m[0] for m in journal["tracks"]["computer"]["marks"]], [0, CRATE])

    def test_device_changes_are_events_and_a_failure_stops_only_that_track(self):
        rec, mic, comp = self.start()
        comp.notify("switched", "Headset")
        comp.notify("no_output", None)
        self.assertEqual(rec.journal["tracks"]["computer"]["device_name"], "Headset")
        comp.notify("failed", None)
        mic.feed(1)
        comp.feed(1)                          # nothing more is kept from it
        self.assertFalse(rec.computer_on)
        self.assertEqual(rec.status()["state"], "recording")
        journal = rec.stop()
        self.assertEqual([e["kind"] for e in journal["events"]],
                         ["computer_switched", "computer_no_output", "computer_failed"])
        self.assertTrue(all("Headset" not in e["note"] for e in journal["events"]))
        self.assertEqual(journal["tracks"]["computer"]["state"], "failed")
        self.assertEqual(journal["tracks"]["computer"]["frames"], 0)
        self.assertEqual(journal["frames"], RATE)

    def test_policy_stops_the_computer_track_and_the_microphone_carries_on(self):
        rec, mic, comp = self.start()
        mic.feed(1)
        comp.feed(1)
        self.assertTrue(rec.stop_computer("Turned off."))
        self.assertTrue(comp.stopped and comp.closed)
        self.assertFalse(rec.stop_computer("Turned off."))         # once
        mic.feed(1)
        journal = rec.stop()
        self.assertEqual(journal["frames"], 2 * RATE)
        self.assertEqual(journal["tracks"]["computer"]["frames"], CRATE)
        self.assertEqual(journal["tracks"]["computer"]["state"], "policy_stop")
        self.assertEqual([e["kind"] for e in journal["events"]], ["computer_policy_stop"])

    def test_if_computer_audio_cannot_start_the_recording_does_not_start(self):
        with self.assertRaisesRegex(recorder.RecorderError, "Computer audio"):
            self.start(fail=True)
        self.assertTrue(self.holder["mic"].closed)
        self.assertFalse((self.root / "rec").exists())

    def test_crash_recovery_includes_unlisted_computer_chunks_and_removal_takes_all(self):
        rec, mic, comp = self.start()
        mic.feed(6)
        comp.feed(6)
        time.sleep(0.6)
        rec._stop.set()                       # the process "dies"
        rec._writer.join(5)
        folder = rec.folder
        (folder / "sys-999999.pcm").write_bytes(np.zeros(CRATE, "<i2").tobytes())
        (folder / "sys-999998.pcm.tmp").write_bytes(b"\x01" * 1001)
        journal = recorder.read_journal(folder)
        files = recorder.track_files(folder, journal["tracks"]["computer"]["chunks"], "sys-")
        self.assertEqual(files[-1], ("sys-999999.pcm", CRATE))
        self.assertNotIn("sys-999998.pcm.tmp", [f for f, _ in files])
        wav, seconds = recorder.assemble_wav(folder, journal)
        self.assertAlmostEqual(seconds, journal["frames"] / RATE)
        recorder.remove_folder(folder)
        self.assertFalse(folder.exists())


# -- the app: policy, the switch, the import ------------------------------------------

@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class WorkspaceTwoTrackTests(unittest.TestCase):
    DEVICES = [{"index": 3, "name": "Test microphone", "default": True, "rate": RATE}]

    def setUp(self):
        from workspace import power
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.holder = {}
        devices = mock.patch.object(recorder, "list_devices", lambda: self.DEVICES)
        devices.start()
        self.addCleanup(devices.stop)

        def never(*args, **kwargs):
            raise AssertionError("a test reached the real power scheme")
        for name in ("write", "apply"):
            guard = mock.patch.object(power._PowrProf, name, never)
            guard.start()
            self.addCleanup(guard.stop)
        self.ws = None

    def tearDown(self):
        if self.ws is not None:
            self.ws.close()
        self.tmp.cleanup()

    def make(self, values=None, invalid=()):
        from workspace.app import Workspace
        if self.ws is not None:
            self.ws.close()
        self.ws = Workspace(self.root / "ws", self.root / "models", start=False,
                            allow_test_engines=True, battery=False,
                            policy_reader=lambda: (dict(values or {}), tuple(invalid)),
                            protector=keyprotect.TestProtector())
        self.ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
        self.ws.open_stream = opener(self.holder, "mic", FakeMic)
        self.ws.open_loopback = opener(self.holder, "comp", FakeLoopback)
        return self.ws

    def refused(self, fn, *args):
        from workspace.app import WorkspaceError
        with self.assertRaises(WorkspaceError) as ctx:
            fn(*args)
        return str(ctx.exception)

    def test_not_set_by_it_the_switch_is_allowed_but_off_by_default(self):
        ws = self.make()
        self.assertTrue(ws.managed()["allow"]["AllowComputerAudioRecording"])
        self.assertIs(ws.get_settings()["computer_audio"], False)
        self.assertIn("microphone", self.refused(ws.set_settings, {"computer_audio": True}))
        ws.set_settings({"microphone": True, "computer_audio": True})
        self.assertIs(ws.get_settings()["computer_audio"], True)
        ws.set_settings({"microphone": False})          # never without the microphone
        self.assertIs(ws.settings["computer_audio"], False)

    def test_turned_off_unreadable_or_invalid_policy_refuses_it(self):
        from workspace import policy
        cases = (({"AllowComputerAudioRecording": False}, ()),
                 ({}, ("AllowComputerAudioRecording",)),
                 ({}, policy.UNREADABLE),
                 ({"AllowMicrophoneRecording": False}, ()))
        for values, invalid in cases:
            ws = self.make(values, invalid)
            ws.settings.update(microphone=True, computer_audio=True)   # saved before the policy
            self.assertIn("organisation", self.refused(ws.set_settings,
                                                       {"computer_audio": True}))
            self.assertIs(ws.get_settings()["computer_audio"], False)
            with mock.patch("workspace.app.COMPUTER_AUDIO_STARTS", True):
                self.assertIsNone(ws._computer_track())

    def test_no_computer_track_opens_until_the_app_allows_it(self):
        # Groundwork only: whatever the switch says, the start path opens no
        # computer track until the version 2 confirmation and the page land.
        ws = self.make()
        ws.settings.update(microphone=True, computer_audio=True)
        ws.start()
        ws.start_recording(3, consent=CONSENT)
        self.assertNotIn("comp", self.holder)
        self.assertNotIn("format", recorder.read_journal(ws._recording.folder))
        ws.stop_recording()

    def start_two(self, values=None):
        ws = self.make(values)
        ws.settings.update(microphone=True, computer_audio=True)
        ws.start()
        with mock.patch("workspace.app.COMPUTER_AUDIO_STARTS", True):
            ws.start_recording(3, consent=CONSENT)
        return ws, self.holder["mic"], self.holder["comp"]

    def test_a_two_track_recording_becomes_one_meeting_from_one_mix(self):
        from workspace import media
        ws, mic, comp = self.start_two()
        mic.feed(6)
        comp.feed(6)
        ws.stop_recording()
        self.assertTrue(ws.wait_idle(120))
        meetings = ws.meetings()
        self.assertEqual(len(meetings), 1)                       # one meeting, one transcript
        self.assertEqual(list((ws.root / "recordings").iterdir()), [])
        self.assertEqual(ws.recording_status()["reports"][-1]["seconds"], 6.0)
        reader = media.AudioReader(media.audio_path(ws.media, meetings[0]["id"]))
        try:
            audio = reader.read(16000, 64000)                   # 1 s to 4 s, at 16 kHz
        finally:
            reader.close()
        spectrum = np.abs(np.fft.rfft(audio))
        freqs = np.fft.rfftfreq(len(audio), 1 / 16000)
        level = lambda hz: spectrum[np.argmin(np.abs(freqs - hz))]  # noqa: E731
        self.assertGreater(level(220), 50 * level(600))            # the microphone
        self.assertGreater(level(1000), 50 * level(600))           # and the computer, mixed
        self.assertTrue(ws.transcript(meetings[0]["id"])["items"])

    def test_policy_turned_off_mid_recording_stops_only_the_computer_track(self):
        from workspace import policy
        ws, mic, comp = self.start_two()
        mic.feed(1)
        ws.policy = policy.PolicySource(reader=lambda: ({}, policy.UNREADABLE))
        ws._policy_tick()                       # unreadable: does not cut a track short
        self.assertTrue(ws._recording.computer_on)
        ws.policy = policy.PolicySource(reader=lambda: ({"AllowComputerAudioRecording": False}, ()))
        ws._policy_tick()
        self.assertTrue(comp.stopped and comp.closed)
        self.assertIsNotNone(ws._recording)     # the microphone carries on
        self.assertEqual(ws._recording.status()["state"], "recording")
        kinds = [e["kind"] for e in ws._recording.journal["events"]]
        self.assertEqual(kinds, ["computer_policy_stop"])
        ws.stop_recording()

    def test_resume_checks_the_policy_again(self):
        from workspace import policy
        ws, mic, comp = self.start_two()
        ws.pause_recording()
        ws.policy = policy.PolicySource(reader=lambda: ({"AllowComputerAudioRecording": False}, ()))
        ws.resume_recording()
        self.assertFalse(ws._recording.computer_on)
        self.assertEqual(ws._recording.status()["state"], "recording")
        ws.stop_recording()

    def test_discard_deletes_both_tracks(self):
        ws, mic, comp = self.start_two()
        mic.feed(6)
        comp.feed(6)
        folder = ws._recording.folder
        time.sleep(0.5)
        self.assertTrue(any(folder.glob("sys-*.pcm")))
        ws.discard_recording()
        self.assertFalse(folder.exists())
        self.assertTrue(ws.wait_idle(60))
        self.assertEqual(ws.meetings(), [])

    def test_a_journal_from_a_newer_version_is_kept_not_half_imported(self):
        ws = self.make()
        ws.settings.update(microphone=True)
        folder = ws.root / "recordings" / ("d" * 32)
        folder.mkdir(parents=True)
        (folder / "chunk-000000.pcm").write_bytes(np.zeros(RATE, "<i2").tobytes())
        (folder / "future-000000.pcm").write_bytes(np.zeros(RATE, "<i2").tobytes())
        (folder / "journal.json").write_text(json.dumps({
            "id": folder.name, "format": 3, "rate": RATE, "state": "stopped",
            "chunks": [{"file": "chunk-000000.pcm", "frames": RATE}], "frames": RATE,
            "events": [], "started_at": 1000.0, "last_seen": 1001.0}))
        ws.start()
        self.assertTrue(ws.wait_idle(60))
        self.assertEqual(ws.meetings(), [])
        self.assertTrue((folder / "future-000000.pcm").exists())   # nothing removed
        report = ws.recording_status()["reports"][-1]
        self.assertIn("cannot read", report["note"])
        self.assertEqual(report["pending"], folder.name)


@unittest.skipUnless(HAVE_NUMPY and sys.platform == "win32", "Windows only")
class LoopbackWindowsTests(unittest.TestCase):
    """The ctypes definitions load; nothing is opened (the probe, run by
    hand, opens a stream: tools/capture_probe.py)."""

    def test_the_com_definitions_build(self):
        api = loopback._com()
        self.assertEqual(api.ctypes.sizeof(api.PROPVARIANT), 24 if sys.maxsize > 2 ** 32 else 16)
        self.assertEqual(api.ctypes.sizeof(api.WAVEFORMATEX), 18)
        self.assertEqual(api.ctypes.sizeof(api.ACTIVATION_PARAMS), 12)


if __name__ == "__main__":
    unittest.main()
