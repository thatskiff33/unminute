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

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
RATE = 16000

if HAVE_DEPS:
    import numpy as np

    from workspace import recorder, update
    from workspace.app import Workspace, WorkspaceError


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
        journal = rec.stop()
        self.assertTrue(s.stopped and s.closed)
        self.assertEqual(journal["state"], "stopped")
        self.assertGreaterEqual(len(journal["chunks"]), 3)
        wav, seconds = recorder.assemble_wav(rec.folder, journal)
        self.assertEqual(wav_frames(wav), (s.fed, RATE))
        self.assertEqual(journal["events"], [])

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

    def test_driver_reporting_the_end_counts_as_device_loss(self):
        rec, s = self.start()
        s.feed(1)
        s.finished()
        self.assertTrue(rec.stopped)
        self.assertEqual(rec.stop()["state"], "device_lost")

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
        self.assertEqual(found, [{"index": 1, "name": "Headset microphone", "default": True,
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

    def make(self):
        ws = Workspace(self.root / "ws", self.root / "models", start=False, battery=False)
        ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False)
        ws.open_stream = opener(self.holder)
        return ws

    def tearDown(self):
        self.ws.close()
        self.devices.stop()
        self.tmp.cleanup()

    def test_off_by_default(self):
        self.ws.start()
        with self.assertRaises(WorkspaceError):
            self.ws.start_recording(3)
        self.assertFalse(self.ws.recording_status()["enabled"])

    def test_recording_becomes_a_meeting_and_heavy_work_waits(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        with self.assertRaises(WorkspaceError):
            self.ws.start_recording(99)                 # unknown device: refused
        self.ws.start_recording(3)
        self.holder["stream"].feed(8)
        self.assertIsNone(self.ws._next_work())         # nothing heavy starts meanwhile
        with self.assertRaises(WorkspaceError):
            self.ws.start_recording(3)                  # one at a time
        self.ws.stop_recording()
        self.assertTrue(self.ws.wait_idle(120))
        meetings = self.ws.meetings()
        self.assertEqual(len(meetings), 1)
        self.assertTrue(meetings[0]["title"].startswith("Recording "))
        self.assertEqual(list((self.ws.root / "recordings").iterdir()), [])
        self.assertTrue((self.ws.media / meetings[0]["id"] / "recording.json").is_file())
        self.assertEqual(self.ws.recording_status()["reports"][-1]["seconds"], 8.0)
        self.assertTrue(self.ws.transcript(meetings[0]["id"])["items"])  # then transcribed

    def test_unimported_recording_blocks_schema_updates(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3)
        self.holder["stream"].feed(1)
        kinds = [b["kind"] for b in update.blockers(self.ws.root / "workspace.db")]
        self.assertIn("recordings", kinds)
        self.ws.stop_recording()

    def test_recording_cut_short_by_a_crash_is_imported_at_next_start(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3)
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
        report = self.ws.recording_status()["reports"][-1]
        self.assertEqual(report["seconds"], 11.0)      # all the writer had saved
        gap = [e for e in report["events"] if e["kind"] == "interrupted"]
        self.assertEqual(len(gap), 1)
        self.assertGreater(gap[0]["seconds"], 15)
        self.assertEqual(len(self.ws.meetings()), 1)


if __name__ == "__main__":
    unittest.main()
