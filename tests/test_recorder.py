# Microphone recording (#30) with a fake audio stream: durable chunks, pause,
# overrun and full-queue reporting, device loss, a full disk, crash recovery,
# and the workspace flow (heavy work waits; the recording becomes a meeting).
#   python -m unittest discover -s tests -v

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # fake_worker_engines

from workspace import consent  # noqa: E402  (stdlib only)

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
RATE = 16000

if HAVE_DEPS:
    import numpy as np

    from workspace import recorder, update
    from workspace.app import Workspace, WorkspaceError


# What the bridge passes after the app's own confirmation (#111 A6).
CONSENT = consent.recording_record(clock=lambda: 1000.0)


class FakeStream:
    """Stands in for a sounddevice InputStream; the test pushes audio."""

    def __init__(self, device, rate, callback, finished):
        self.rate, self.callback, self.finished = rate, callback, finished
        self.started = self.stopped = self.closed = False
        self.fed = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.finished()          # like PortAudio: finished_callback runs on stop()

    def close(self):
        self.closed = True

    def feed(self, seconds, overflow=False):
        n = int(self.rate * recorder.BLOCK_S)
        status = type("Status", (), {"input_overflow": overflow})() if overflow else None
        for i in range(int(round(seconds / recorder.BLOCK_S))):
            t = (np.arange(n) + self.fed) / self.rate
            block = (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16).reshape(-1, 1)
            self.callback(block, n, None, status if i == 0 else None)
            self.fed += n


def opener(holder):
    def open_stream(device, rate, callback, finished):
        holder["stream"] = FakeStream(device, rate, callback, finished)
        return holder["stream"]
    return open_stream


def wav_frames(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes(), w.getframerate()


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.holder = {}

    def tearDown(self):
        self.tmp.cleanup()

    def start(self):
        rec = recorder.Recording(self.root / "rec", 3, "Test microphone", RATE,
                                 open_stream=opener(self.holder))
        rec.start()
        return rec, self.holder["stream"]

    def test_every_frame_is_saved_in_durable_chunks(self):
        rec, s = self.start()
        s.feed(12.3)
        time.sleep(0.5)
        self.assertGreater(rec.level_db, -30)          # a level for the meter
        status = rec.status()                          # recent blocks as [number, level]
        numbers = [n for n, _ in status["levels"]]
        self.assertEqual(numbers, list(range(numbers[0], numbers[0] + len(numbers))))
        self.assertLessEqual(len(numbers), 50)
        self.assertEqual(rec.status()["levels"], status["levels"])   # reading does not use them up
        viz = status["viz"]                            # display data for the latest block
        self.assertEqual(len(viz["bands"]), recorder.VIZ_BANDS)
        self.assertEqual(len(viz["wave"]), recorder.VIZ_WAVE_POINTS)
        self.assertLess(abs(viz["pitch_hz"] - 220), 15)   # the 220 Hz test tone
        self.assertGreater(max(viz["bands"]), -20)
        for rate in (16000, 44100, 48000):                # any device rate, short blocks, silence
            for n in (rate // 10, 100, 40):
                v = recorder.visual(np.zeros(n, dtype=np.int16), rate)
                self.assertEqual(len(v["bands"]), recorder.VIZ_BANDS)
                self.assertEqual(v["pitch_hz"], 0)
        journal = rec.stop()
        self.assertTrue(s.stopped and s.closed)
        self.assertIsNone(rec.status()["viz"])            # nothing of the sound kept after stop
        self.assertEqual(journal["state"], "stopped")
        self.assertGreaterEqual(len(journal["chunks"]), 3)
        wav, seconds = recorder.assemble_wav(rec.folder, journal)
        self.assertEqual(wav_frames(wav), (s.fed, RATE))
        self.assertEqual(journal["events"], [])

    def test_display_data_is_worked_out_only_when_asked_for(self):
        # docs/RELEASE_STANDARD.md section 3: no FFT for blocks nobody looks at.
        calls = []
        real = recorder.visual

        def counting(block, rate):
            calls.append(len(block))
            return real(block, rate)
        with mock.patch.object(recorder, "visual", counting):
            rec, s = self.start()
            s.feed(3)                                 # 30 blocks, none displayed
            self.assertEqual(calls, [])
            first = rec.status()["viz"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(rec.status()["viz"], first)   # same block: not worked out again
            self.assertEqual(len(calls), 1)
            s.feed(0.1)
            rec.status()
            self.assertEqual(len(calls), 2)
            rec.pause()
            s.feed(0.5)
            self.assertIsNone(rec.status()["viz"])    # nothing to draw while paused
            rec.stop()
        self.assertEqual(len(calls), 2)

    def test_pause_keeps_nothing_and_says_so(self):
        rec, s = self.start()
        s.feed(2)
        rec.pause()
        s.feed(3)                    # not recorded while paused
        rec.resume()
        s.feed(1)
        journal = rec.stop()
        _, seconds = recorder.assemble_wav(rec.folder, journal)
        self.assertAlmostEqual(seconds, 3.0, places=1)
        self.assertEqual([e["kind"] for e in journal["events"]], ["paused", "resumed"])

    def test_overrun_and_full_queue_are_reported_never_silent(self):
        rec = recorder.Recording(self.root / "rec", 0, "Mic", RATE, open_stream=opener({}))
        rec._queue.maxsize = 2                       # writer not started: queue fills
        block = np.zeros((1600, 1), dtype=np.int16)
        status = type("S", (), {"input_overflow": True})()
        rec._callback(block, 1600, None, status)
        rec._callback(block, 1600, None, None)
        rec._callback(block, 1600, None, None)      # no room: dropped, with its length
        kinds = [(e["kind"], e["seconds"]) for e in rec.journal["events"]]
        self.assertEqual(kinds, [("overrun", None), ("dropped", 0.1)])

    def test_lost_device_stops_and_keeps_what_was_saved(self):
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 0.6), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.1):
            rec, s = self.start()
            s.feed(6)
            deadline = time.time() + 10           # then nothing: the device is gone
            while not rec.stopped and time.time() < deadline:
                time.sleep(0.1)
        journal = rec.stop()
        self.assertEqual(journal["state"], "device_lost")
        self.assertIn("did not switch", journal["events"][-1]["note"])
        _, seconds = recorder.assemble_wav(rec.folder, journal)
        self.assertAlmostEqual(seconds, 6.0, places=1)

    def test_an_overrun_at_the_moment_of_disconnection_is_part_of_it(self):
        rec, s = self.start()
        s.feed(2)
        s.feed(0.1, overflow=True)            # what Windows reports as the device goes
        s.finished()
        events = rec.stop()["events"]
        self.assertEqual([e["kind"] for e in events], ["device_lost"])
        self.assertIn("fraction of a second", events[0]["note"])

    def test_overruns_are_placed_by_captured_audio_even_when_saving_is_behind(self):
        # The writer is not started, so everything stays queued, as when saving
        # falls behind. An overrun 3 s before the device goes is its own event;
        # one at the moment it goes is part of the disconnection.
        block = np.zeros((1600, 1), dtype=np.int16)
        overflow = type("S", (), {"input_overflow": True})()
        for before, expected in ((0, ["overrun", "device_lost"]), (30, ["device_lost"])):
            rec = recorder.Recording(self.root / f"rec{before}", 0, "Mic", RATE,
                                     open_stream=opener({}))
            for _ in range(before):                  # blocks before the overrun
                rec._callback(block, 1600, None, None)
            rec._callback(block, 1600, None, overflow)
            for _ in range(30 - before):
                rec._callback(block, 1600, None, None)
            rec._device_lost()
            self.assertEqual([e["kind"] for e in rec.journal["events"]], expected, before)
            self.assertIsNone(rec._display_block)    # nothing kept after the device went

    def test_an_earlier_overrun_is_still_reported_on_its_own(self):
        rec, s = self.start()
        s.feed(0.1, overflow=True)
        s.feed(3)
        s.finished()
        kinds = [e["kind"] for e in rec.stop()["events"]]
        self.assertEqual(kinds, ["overrun", "device_lost"])

    def test_driver_reporting_the_end_counts_as_device_loss(self):
        rec, s = self.start()
        s.feed(1)
        s.finished()
        self.assertTrue(rec.stopped)
        self.assertEqual(rec.stop()["state"], "device_lost")

    # Sleep, as Windows presents it to the app: the monotonic clock jumps
    # forward (it counts time asleep) while every thread was frozen.

    def start_ticking(self):
        self.asleep = [0.0]
        rec = recorder.Recording(self.root / "rec", 3, "Test microphone", RATE,
                                 open_stream=opener(self.holder),
                                 tick=lambda: time.monotonic() + self.asleep[0])
        rec.start()
        return rec, self.holder["stream"]

    def wait_stopped(self, rec, seconds=10):
        deadline = time.time() + seconds
        while not rec.stopped and time.time() < deadline:
            time.sleep(0.05)

    def test_sleep_is_reported_as_sleep_not_a_lost_microphone(self):
        rec, s = self.start_ticking()
        s.feed(2)
        time.sleep(0.3)
        self.asleep[0] += 600                    # ten minutes asleep; nothing after
        self.wait_stopped(rec)                   # the watchdog notices on its next beat
        journal = rec.stop()
        self.assertEqual(journal["state"], "slept")
        self.assertEqual([e["kind"] for e in journal["events"]], ["sleep"])
        self.assertIn("went to sleep", journal["events"][0]["note"])
        _, seconds = recorder.assemble_wav(rec.folder, journal)
        self.assertAlmostEqual(seconds, 2.0, places=1)    # everything before it is kept

    def test_audio_resuming_after_sleep_is_not_joined_on(self):
        # The driver delivers audio before the watchdog's first beat after the
        # wake (and before any silence check): the callback alone must notice.
        with mock.patch.object(recorder, "ALIVE_S", 30), \
                mock.patch.object(recorder, "HEARTBEAT_S", 30):
            rec, s = self.start_ticking()
            s.feed(2)
            self.asleep[0] += 600
            s.feed(2)
            self.assertTrue(rec.stopped)
        journal = rec.stop()
        self.assertEqual(journal["state"], "slept")
        _, seconds = recorder.assemble_wav(rec.folder, journal)
        self.assertAlmostEqual(seconds, 2.0, places=1)    # only the audio before the sleep

    def test_driver_ending_the_stream_after_sleep_is_sleep(self):
        with mock.patch.object(recorder, "ALIVE_S", 30):  # before the watchdog's beat
            rec, s = self.start_ticking()
            s.feed(1)
            self.asleep[0] += 600
            s.finished()
            self.assertEqual(rec.journal["state"], "slept")
        self.assertEqual(rec.stop()["state"], "slept")

    def test_driver_ending_the_stream_after_sleep_while_paused_is_sleep(self):
        rec, s = self.start_ticking()
        s.feed(1)
        rec.pause()
        self.asleep[0] += 600
        time.sleep(0.5)                          # the watchdog has seen the freeze
        self.assertFalse(rec.stopped)
        s.finished()                             # then the driver ends the stream
        self.assertEqual(rec.stop()["state"], "slept")

    def test_a_sleep_while_paused_leaves_it_paused(self):
        # Nothing is being recorded, so nothing is lost: it stays paused and
        # carries on in the same recording when resumed.
        rec, s = self.start_ticking()
        s.feed(1)
        rec.pause()
        self.asleep[0] += 600
        time.sleep(0.5)
        s.feed(0.5)                              # paused: not recorded
        self.assertFalse(rec.stopped)
        rec.resume()
        s.feed(1)
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        _, seconds = recorder.assemble_wav(rec.folder, journal)
        self.assertAlmostEqual(seconds, 2.0, places=1)

    def test_sleep_closes_the_stream_at_once(self):
        rec, s = self.start_ticking()
        s.feed(1)
        self.asleep[0] += 600
        self.wait_stopped(rec)
        deadline = time.time() + 5               # closed by the writer, before stop()
        while not s.closed and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(rec.journal["state"], "slept")
        self.assertTrue(s.stopped and s.closed)
        rec.stop()

    def test_a_slow_save_is_not_mistaken_for_sleep(self):
        # A microphone lost while a chunk takes longer than the silence limit
        # to save is a lost microphone: a slow save does not delay the watchdog.
        real = recorder._write_atomic

        def slow(path, data):
            if path.suffix == ".pcm":
                time.sleep(1.0)
            real(path, data)
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 0.6), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.1), \
                mock.patch.object(recorder, "_write_atomic", slow):
            rec, s = self.start()
            s.feed(6)                        # the 5 s chunk is saved slowly
            self.wait_stopped(rec)
        journal = rec.stop()
        self.assertEqual(journal["state"], "device_lost")

    def test_changing_the_clock_does_not_end_a_recording(self):
        # The wall clock jumps (the user or Windows time service sets it); the
        # audio never stops. Journal times follow the wall clock; gaps do not.
        offset = [0.0]
        rec = recorder.Recording(self.root / "rec", 3, "Mic", RATE, open_stream=opener(self.holder),
                                 clock=lambda: time.time() + offset[0])
        self.assertIs(rec._tick, time.monotonic)          # the default the app relies on
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 0.6), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.1):
            rec.start()
            s = self.holder["stream"]
            for _ in range(10):
                s.feed(0.2)
                offset[0] += 5.0                 # forward 5 s each time
                time.sleep(0.2)
            self.assertFalse(rec.stopped)
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual(journal["events"], [])

    def test_a_microphone_that_stalls_is_not_sleep(self):
        # No audio for longer than the limit while the app keeps running (a
        # Bluetooth headset reconnecting), and it comes back before the next
        # silence check: never "slept".
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 0.6), \
                mock.patch.object(recorder, "HEARTBEAT_S", 30):
            rec, s = self.start()
            s.feed(1)
            time.sleep(1.2)
            s.feed(1)                            # it comes back
            time.sleep(0.3)
            self.assertFalse(rec.stopped)
        journal = rec.stop()
        self.assertNotIn("sleep", [e["kind"] for e in journal["events"]])
        self.assertEqual(journal["state"], "stopped")

    def test_a_lost_device_is_reported_once(self):
        rec = recorder.Recording(self.root / "rec", 0, "Mic", RATE, open_stream=opener({}))
        rec._device_lost()
        rec._device_lost()                       # the driver and the silence check both
        self.assertEqual([e["kind"] for e in rec.journal["events"]], ["device_lost"])
        import threading                         # and at the same moment
        for n in range(20):
            rec = recorder.Recording(self.root / f"race{n}", 0, "Mic", RATE,
                                     open_stream=opener({}))
            gate = threading.Barrier(6)

            def lose(r=rec, g=gate):
                g.wait()
                r._device_lost()
            threads = [threading.Thread(target=lose) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual([e["kind"] for e in rec.journal["events"]], ["device_lost"])

    def test_a_hold_up_short_of_sleep_is_a_stall_not_sleep(self):
        # The app is held up for 5 s (heavy paging, a busy computer) without
        # sleeping: the gap is noted with its length and the recording goes on.
        with mock.patch.object(recorder, "HEARTBEAT_S", 30):
            rec, s = self.start_ticking()
            s.feed(1)
            time.sleep(0.3)                      # the writer's first (silence-checking) pass
            self.asleep[0] += 5                  # the monotonic clock moved 5 s at once
            s.feed(1)                            # audio straight after: not "slept"
            time.sleep(0.5)                      # the watchdog's next beat
            self.assertFalse(rec.stopped)
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual([e["kind"] for e in journal["events"]], ["stall"])
        self.assertAlmostEqual(journal["events"][0]["seconds"], 5.0, delta=0.5)

    def test_no_sleep_is_judged_before_the_watchdog_runs(self):
        # A slow microphone start: audio can arrive before the watchdog starts.
        asleep = [0.0]
        rec = recorder.Recording(self.root / "rec", 0, "Mic", RATE, open_stream=opener({}),
                                 tick=lambda: time.monotonic() + asleep[0])
        asleep[0] += 600
        rec._callback(np.zeros((1600, 1), dtype=np.int16), 1600, None, None)
        self.assertEqual(rec.journal["state"], "recording")

    def test_full_disk_keeps_earlier_chunks_and_reports_the_loss(self):
        real = recorder._write_atomic
        calls = {"n": 0}

        def failing(path, data):
            if path.suffix == ".pcm":
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError(28, "No space left on device")
            return real(path, data)

        with mock.patch.object(recorder, "_write_atomic", failing):
            rec, s = self.start()
            s.feed(12)
            deadline = time.time() + 10
            while not rec.stopped and time.time() < deadline:
                time.sleep(0.1)
            journal = rec.stop()
        self.assertEqual(journal["state"], "failed")
        last = journal["events"][-1]
        self.assertEqual(last["kind"], "disk_full")
        self.assertGreater(last["seconds"], 0)
        self.assertEqual(len(journal["chunks"]), 1)        # the chunk saved before is kept
        _, seconds = recorder.assemble_wav(rec.folder, journal)
        self.assertEqual(seconds, recorder.CHUNK_S)

    def test_crash_recovery_finds_saved_audio_and_estimates_the_gap(self):
        rec, s = self.start()
        s.feed(11)
        time.sleep(0.6)               # chunks written; then the process "dies"
        rec._stop.set()               # stop the writer without the orderly stop()
        rec._writer.join(5)
        folder = rec.folder
        journal = json.loads((folder / "journal.json").read_text())
        journal["state"] = "recording"                # as a crash leaves it
        (folder / "journal.json").write_text(json.dumps(journal))
        # A chunk renamed into place but not yet in the journal is complete;
        # a temporary file is not.
        (folder / "chunk-999999.pcm").write_bytes(np.zeros(RATE, "<i2").tobytes())
        (folder / "chunk-999998.pcm.tmp").write_bytes(b"\x01" * 1001)
        (folder / "heartbeat").write_text(str(journal["started_at"] + 20))
        found = recorder.unfinished(self.root)
        self.assertEqual([f for f, _ in found], [folder])
        wav, seconds = recorder.assemble_wav(folder, found[0][1])
        self.assertAlmostEqual(seconds, 12.0, places=1)    # 11 s fed, plus the extra chunk
        missing = recorder.missing_tail_seconds(folder, found[0][1], seconds)
        self.assertAlmostEqual(missing, 8.0, places=0)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class DeviceListTests(unittest.TestCase):
    def fake_sd(self, devices, default_in):
        sd = type(sys)("sounddevice")
        sd.query_devices = lambda: devices
        sd.query_hostapis = lambda: [{"name": "MME"}, {"name": "Windows WASAPI"}]
        sd.default = type("D", (), {"device": (default_in, 0)})()
        return sd

    def test_only_real_mme_inputs_are_offered(self):
        devices = [
            {"name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2},
            {"name": "Headset microphone", "hostapi": 0, "max_input_channels": 1,
             "default_samplerate": 48000},
            {"name": "Headset microphone", "hostapi": 1, "max_input_channels": 1},
            {"name": "Speakers", "hostapi": 0, "max_input_channels": 0}]
        with mock.patch.dict(sys.modules, {"sounddevice": self.fake_sd(devices, 1)}):
            found = recorder.list_devices()
        # The Sound Mapper follows the Windows default and could switch device.
        self.assertEqual(found, [{"index": 1, "name": "Headset microphone",
                                  "label": "Headset microphone", "default": True,
                                  "rate": 48000}])

    def test_a_broken_audio_library_is_an_error_not_no_microphone(self):
        with mock.patch.dict(sys.modules, {"sounddevice": None}):   # import fails
            with self.assertRaises(recorder.RecorderError):
                recorder.list_devices()


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class WorkspaceRecordingTests(unittest.TestCase):
    DEVICES = [{"index": 3, "name": "Test microphone", "default": True, "rate": RATE}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.holder = {}
        self.devices = mock.patch.object(recorder, "list_devices", lambda: self.DEVICES)
        self.devices.start()
        self.ws = self.make()

    def make(self, **kw):
        ws = Workspace(self.root / "ws", self.root / "models", start=False,
                       allow_test_engines=True, battery=False, **kw)
        ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
        ws.open_stream = opener(self.holder)
        return ws

    def tearDown(self):
        self.ws.close()
        self.devices.stop()
        self.tmp.cleanup()

    def test_a_recording_needs_the_confirmation_and_keeps_it(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        with self.assertRaisesRegex(WorkspaceError, "confirmation"):
            self.ws.start_recording(3)                  # the bridge asks; nothing else may
        shown = []
        self.ws.recording_indicator = shown.append
        self.ws.start_recording(3, consent=CONSENT)
        folder = self.ws._recording.folder
        self.assertEqual(recorder.read_journal(folder)["consent"], CONSENT)   # also after a crash
        self.assertEqual(shown, [True])
        self.holder["stream"].feed(4)
        self.ws.stop_recording()
        self.assertEqual(shown, [True, False])
        self.assertTrue(self.ws.wait_idle(120))
        [m] = self.ws.meetings()                       # kept with the imported meeting
        sidecar = json.loads((self.ws.media / m["id"] / "recording.json").read_text("utf-8"))
        self.assertEqual(sidecar["consent"], CONSENT)

    def test_policy_can_turn_the_confirmation_off(self):
        self.ws.close()
        self.ws = self.make(policy_reader=lambda: ({"RequireRecordingConsent": False}, ()))
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.assertFalse(self.ws.consent_required())
        self.ws.start_recording(3)
        self.assertNotIn("consent", recorder.read_journal(self.ws._recording.folder))
        self.ws.stop_recording()

    def test_the_indicator_follows_every_way_a_recording_ends(self):
        from workspace import policy
        self.ws.settings.update(microphone=True)
        self.ws.start()
        shown = []
        self.ws.recording_indicator = shown.append
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.discard_recording()                     # discarded
        self.ws.start_recording(3, consent=CONSENT)
        self.ws._recording._device_lost()               # stops itself (microphone lost)
        deadline = time.time() + 10
        while self.ws._recording is not None and time.time() < deadline:
            time.sleep(0.05)
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.policy = policy.PolicySource(               # IT turns recording off
            reader=lambda: ({"AllowMicrophoneRecording": False}, ()))
        self.ws._policy_tick()
        self.ws.policy = policy.PolicySource(reader=lambda: ({}, ()))
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.close()                                 # saved at close
        self.assertEqual(shown, [True, False] * 4)

        def broken(on):
            raise RuntimeError("window gone")
        self.ws = self.make()
        self.ws.settings.update(microphone=True)
        self.ws.recording_indicator = broken            # never stops a recording
        self.ws.start_recording(3, consent=CONSENT)
        self.assertIsNotNone(self.ws._recording)
        later = []
        self.ws.recording_indicator = later.append      # the next change is applied anyway
        self.ws.stop_recording()
        self.assertEqual(later, [False])

    def test_the_computer_is_kept_awake_exactly_while_recording(self):
        from test_power import FakeKernel32

        from workspace import power, policy
        api = FakeKernel32()
        self.ws.keep_awake = power.KeepAwake(api)
        self.ws.settings.update(microphone=True)
        self.ws.start()
        seen = []
        self.ws.recording_indicator = lambda on: seen.append((on, api.active))
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.stop_recording()                        # stopped
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.discard_recording()                     # discarded
        self.ws.start_recording(3, consent=CONSENT)
        self.ws._recording._device_lost()               # stops itself
        deadline = time.time() + 10
        while self.ws._recording is not None and time.time() < deadline:
            time.sleep(0.05)
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.policy = policy.PolicySource(               # IT turns recording off
            reader=lambda: ({"AllowMicrophoneRecording": False}, ()))
        self.ws._policy_tick()
        self.ws.policy = policy.PolicySource(reader=lambda: ({}, ()))
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.close()                                 # saved at close
        self.assertEqual(seen, [(True, 1), (False, 0)] * 5)
        self.assertEqual(api.open, 0)

        refused = FakeKernel32(set_ok=0)                # Windows says no: recording goes on
        self.ws = self.make()
        self.ws.keep_awake = power.KeepAwake(refused)
        self.ws.settings.update(microphone=True)
        self.ws.start_recording(3, consent=CONSENT)
        self.assertIsNotNone(self.ws._recording)
        self.ws.stop_recording()

    def test_off_by_default(self):
        self.ws.start()
        with self.assertRaises(WorkspaceError):
            self.ws.start_recording(3, consent=CONSENT)
        self.assertFalse(self.ws.recording_status()["enabled"])

    def test_recording_becomes_a_meeting_and_heavy_work_waits(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        with self.assertRaises(WorkspaceError):
            self.ws.start_recording(99, consent=CONSENT)                 # unknown device: refused
        self.ws.start_recording(3, consent=CONSENT)
        self.holder["stream"].feed(8)
        self.assertIsNone(self.ws._next_work())         # nothing heavy starts meanwhile
        with self.assertRaises(WorkspaceError):
            self.ws.start_recording(3, consent=CONSENT)                  # one at a time
        self.ws.stop_recording()
        self.assertTrue(self.ws.wait_idle(120))
        meetings = self.ws.meetings()
        self.assertEqual(len(meetings), 1)
        self.assertTrue(meetings[0]["title"].startswith("Recording "))
        self.assertEqual(list((self.ws.root / "recordings").iterdir()), [])
        self.assertTrue((self.ws.media / meetings[0]["id"] / "recording.json").is_file())
        self.assertEqual(self.ws.recording_status()["reports"][-1]["seconds"], 8.0)
        self.assertTrue(self.ws.transcript(meetings[0]["id"])["items"])  # then transcribed

    def test_discarding_keeps_nothing_and_makes_no_meeting(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        self.holder["stream"].feed(3)
        self.ws.pause_recording()                       # can be discarded while paused too
        self.ws.discard_recording()
        self.assertTrue(self.holder["stream"].stopped and self.holder["stream"].closed)
        self.assertIsNone(self.ws.recording_status()["recording"])
        self.assertEqual(list((self.ws.root / "recordings").iterdir()), [])
        time.sleep(1.0)                                 # the watcher must not import it
        self.assertTrue(self.ws.wait_idle(60))
        self.assertEqual(self.ws.meetings(), [])
        with self.assertRaises(WorkspaceError):
            self.ws.discard_recording()                 # nothing left to discard

    def test_a_discard_that_cannot_delete_yet_is_never_imported(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        self.holder["stream"].feed(3)
        real = recorder.remove_folder
        with mock.patch.object(recorder, "remove_folder", side_effect=OSError("in use")):
            with self.assertRaises(WorkspaceError):
                self.ws.discard_recording()
        report = self.ws.recording_status()["reports"][-1]
        self.assertTrue(report["discarded"])
        with self.assertRaises(WorkspaceError):
            self.ws.retry_recording_import(report["id"])   # no Try again for a discard
        folder = self.ws.root / "recordings" / report["pending"]
        self.assertTrue(recorder.is_discarded(folder))
        self.ws.close()
        self.ws = self.make()                              # the next start deletes it
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(60))
        self.assertFalse(folder.exists())
        self.assertEqual(self.ws.meetings(), [])
        self.assertIs(recorder.remove_folder, real)

    def test_a_recovered_recording_can_be_deleted_instead(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        self.holder["stream"].feed(2)
        rec = self.ws._recording
        time.sleep(0.6)
        rec._stop.set()                                # the app closes without stopping it
        rec._writer.join(5)
        journal = json.loads((rec.folder / "journal.json").read_text())
        journal["state"] = "recording"
        (rec.folder / "journal.json").write_text(json.dumps(journal))
        self.ws._recording = None
        self.ws.close()
        self.ws = self.make()
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(60))
        waiting = self.ws.recording_status()["reports"][-1]
        self.assertTrue(waiting["recovered"])
        locked = mock.patch("shutil.rmtree", lambda *a, **k: None)   # a file is locked
        with locked, self.assertRaises(WorkspaceError):               # and it says so
            self.ws.delete_pending_recording(waiting["id"])
        self.assertTrue(recorder.is_discarded(rec.folder))      # so it is never offered again
        self.ws.close()
        self.ws = self.make()
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(60))
        self.assertFalse(rec.folder.exists())
        self.assertEqual(self.ws.recording_status()["reports"], [])
        self.assertEqual(self.ws.meetings(), [])
        self.assertTrue(self.ws.recording_active() is False)

    def test_full_microphone_names_are_shown(self):
        full = ["Logitech C920 Microphone (HD Pro Webcam C920)",
                "Laptop Inbuilt MIcrophone (2- Intel Smart Sound Technology for Digital "
                "Microphones)"]
        self.assertEqual(recorder._full_name("Logitech C920 Microphone (HD Pr", full), full[0])
        self.assertEqual(recorder._full_name("Laptop Inbuilt MIcrophone (2- I", full), full[1])
        self.assertEqual(recorder._full_name("USB Mic", full), "USB Mic")
        twins = ["Headset Microphone (Jabra Evolve 65)", "Headset Microphone (Jabra Evolve2 85)"]
        self.assertEqual(recorder._full_name("Headset Microphone (Jabra Evol", twins),
                         "Headset Microphone (Jabra Evol")   # ambiguous: the name as listed
        self.assertEqual(recorder._clean_name("Intel\ufffd Smart"), "Intel Smart")

    def test_recording_display_setting(self):
        self.assertEqual(self.ws.recording_status()["display"], "flow")
        self.ws.settings.update(recording_display="spectrum")
        self.assertEqual(self.ws.recording_status()["display"], "spectrum")
        with self.assertRaises(WorkspaceError):
            self.ws.settings.update(recording_display="bars")

    def test_unimported_recording_blocks_schema_updates(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        self.holder["stream"].feed(1)
        kinds = [b["kind"] for b in update.blockers(self.ws.root / "workspace.db")]
        self.assertIn("recordings", kinds)
        self.ws.stop_recording()

    def test_two_starts_at_once_give_one_recording(self):
        import threading
        self.ws.settings.update(microphone=True)
        self.ws.start()
        results, barrier = [], threading.Barrier(2)

        def go():
            barrier.wait()
            try:
                self.ws.start_recording(3, consent=CONSENT)
                results.append("started")
            except WorkspaceError:
                results.append("refused")
        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["refused", "started"])
        self.assertEqual(len(list((self.ws.root / "recordings").iterdir())), 1)
        self.ws.stop_recording()

    def test_heavy_work_is_refused_while_a_recording_is_opening(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws._rec_starting = True            # between the check and the stream opening
        self.assertIsNone(self.ws._next_work())
        self.ws._rec_starting = False

    def test_a_changed_device_list_is_refused_not_opened(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        with self.assertRaises(WorkspaceError) as e:
            self.ws.start_recording(3, "Another microphone", consent=CONSENT)
        self.assertIn("changed", str(e.exception))
        self.assertFalse((self.ws.root / "recordings").exists()
                         and any((self.ws.root / "recordings").iterdir()))

    def test_imported_or_empty_folders_are_removed_never_imported_twice(self):
        self.ws.settings.update(microphone=True)
        rec_root = self.ws.root / "recordings"
        done = rec_root / ("a" * 32)
        done.mkdir(parents=True)
        (done / "journal.json").write_text(json.dumps({"state": "imported", "meeting": "x"}))
        (done / "recording.wav").write_bytes(b"RIFF")
        empty = rec_root / ("b" * 32)                  # removal stopped at the very end
        empty.mkdir()
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(60))
        self.assertEqual(list(rec_root.iterdir()), [])
        self.assertEqual(self.ws.meetings(), [])

    def test_unreadable_journal_keeps_the_audio_until_the_user_decides(self):
        self.ws.settings.update(microphone=True)
        folder = self.ws.root / "recordings" / ("c" * 32)
        folder.mkdir(parents=True)
        (folder / "journal.json").write_text("{ damaged by a power cut")
        (folder / "chunk-000000.pcm").write_bytes(b"\x01\x00" * 8000)
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(60))
        self.assertTrue(folder.is_dir())                 # never deleted silently
        rep = self.ws.recording_status()["reports"][-1]
        self.assertIn("could not be read", rep["note"])
        self.ws.delete_pending_recording(rep["id"])      # the user's explicit choice
        self.assertFalse(folder.exists())

    def test_a_meeting_made_from_the_folder_prevents_a_second_import(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        self.holder["stream"].feed(3)
        rec = self.ws._recording
        real = recorder._write_atomic

        def no_journal_mark(path, data):              # marking the journal fails
            if path.name == "journal.json" and b'"imported"' in data:
                raise OSError(5, "Access is denied")
            return real(path, data)
        with mock.patch.object(recorder, "_write_atomic", no_journal_mark), \
                mock.patch.object(recorder, "remove_folder", lambda f: None):
            self.ws.stop_recording()
            self.assertTrue(self.ws.wait_idle(120))
        self.assertTrue(rec.folder.is_dir())             # left behind, journal not marked
        self.ws._imports.put(("__recording__", str(rec.folder)))   # e.g. the next start
        self.assertTrue(self.ws.wait_idle(120))
        self.assertEqual(len(self.ws.meetings()), 1)     # no duplicate
        self.assertFalse(rec.folder.exists())

    def test_a_failed_import_is_reported_kept_and_can_be_retried(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        self.holder["stream"].feed(3)
        real = self.ws._import_one
        self.ws._import_one = mock.Mock(side_effect=OSError(28, "No space left on device"))
        self.ws.stop_recording()
        self.assertTrue(self.ws.wait_idle(60))
        rep = self.ws.recording_status()["reports"][-1]
        self.assertIn("disk is full", rep["note"])
        self.assertTrue(rep["pending"])
        self.assertEqual(len(list((self.ws.root / "recordings").iterdir())), 1)
        self.ws._import_one = real
        self.ws.retry_recording_import(rep["id"])
        self.assertTrue(self.ws.wait_idle(120))
        self.assertEqual(len(self.ws.meetings()), 1)
        self.assertEqual(list((self.ws.root / "recordings").iterdir()), [])
        ids = [r["id"] for r in self.ws.recording_status()["reports"]]
        self.ws.discard_recording_report(ids[-1])
        self.assertNotIn(ids[-1], [r["id"] for r in self.ws.recording_status()["reports"]])

    def test_recording_cut_short_by_a_crash_is_imported_at_next_start(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        self.holder["stream"].feed(11)
        rec = self.ws._recording
        time.sleep(0.6)
        rec._stop.set()                                # the process "dies"
        rec._writer.join(5)
        journal = json.loads((rec.folder / "journal.json").read_text())
        journal["state"] = "recording"
        (rec.folder / "journal.json").write_text(json.dumps(journal))
        (rec.folder / "heartbeat").write_text(str(journal["started_at"] + 30))
        self.ws._recording = None
        self.ws.close()
        self.ws = self.make()
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(120))
        waiting = self.ws.recording_status()["reports"][-1]   # not transcribed until the user says
        self.assertTrue(waiting["recovered"])
        self.assertEqual(self.ws.meetings(), [])
        self.ws.retry_recording_import(waiting["id"])        # Keep and transcribe
        self.assertTrue(self.ws.wait_idle(120))
        report = self.ws.recording_status()["reports"][-1]
        self.assertEqual(report["seconds"], 11.0)      # all the writer had saved
        gap = [e for e in report["events"] if e["kind"] == "interrupted"]
        self.assertEqual(len(gap), 1)
        self.assertGreater(gap[0]["seconds"], 15)
        self.assertEqual(len(self.ws.meetings()), 1)


if __name__ == "__main__":
    unittest.main()
