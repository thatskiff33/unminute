# Microphone recording (#30) with a fake audio stream: durable chunks, pause,
# overrun and full-queue reporting, device loss, a full disk, crash recovery,
# and the workspace flow (heavy work waits; the recording becomes a meeting).
# Since #111 C3b every chunk is encrypted under the recording's key, the
# recording reaches the decoder over its stdin, and no plain audio is written.
#   python -m unittest discover -s tests -v

import importlib.util
import json
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
RATE = 16000

if HAVE_DEPS:
    import numpy as np

    from workspace import recorder, update
    from workspace.app import Workspace, WorkspaceError


# What the bridge passes after the app's own confirmation (#111 A6).
CONSENT = consent.recording_record(clock=lambda: 1000.0)
KEY = bytes(range(32))           # a recording's key in the recorder's own tests


def saved(folder, journal, key=KEY):
    """What a saved recording holds, as the import streams it (int16 at the
    journal's rate), its length in seconds and the problems reported."""
    notes = []
    audio = recorder.open_recording(folder, journal, key, notes)
    pcm = np.frombuffer(b"".join(audio.blocks()), dtype="<i2")
    return pcm, audio.seconds, notes


def files_holding(root, needle):
    """Every file under root whose bytes contain needle (names only)."""
    found = []
    for path in Path(root).rglob("*"):
        try:
            if path.is_file() and needle in path.read_bytes():
                found.append(path.name)
        except OSError:
            pass
    return found


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


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # A clean-up, not tearDown: clean-ups run last first, so every
        # recording a test started is stopped before its folder is removed.
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.holder = {}

    def started(self, rec):
        # #269: stopped even when an assertion fails, so no recorder thread
        # outlives the test (stop() is safe to call again after a test's own).
        rec.start()
        self.addCleanup(rec.stop)
        return rec

    def start(self):
        rec = self.started(recorder.Recording(self.root / "rec", 3, "Test microphone", RATE,
                                              open_stream=opener(self.holder), key=KEY))
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
        pcm, seconds, notes = saved(rec.folder, journal)
        self.assertEqual((len(pcm), seconds, notes), (s.fed, s.fed / RATE, []))
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
        _, seconds, _ = saved(rec.folder, journal)
        self.assertAlmostEqual(seconds, 3.0, places=1)
        self.assertEqual([e["kind"] for e in journal["events"]], ["paused", "resumed"])

    def test_overrun_and_full_queue_are_reported_never_silent(self):
        rec = recorder.Recording(self.root / "rec", 0, "Mic", RATE, open_stream=opener({}),
                                 key=KEY)
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
        _, seconds, _ = saved(rec.folder, journal)
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
                                     open_stream=opener({}), key=KEY)
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
        rec = self.started(recorder.Recording(self.root / "rec", 3, "Test microphone", RATE,
                                              open_stream=opener(self.holder),
                                              tick=lambda: time.monotonic() + self.asleep[0],
                                              key=KEY))
        return rec, self.holder["stream"]

    def wait_stopped(self, rec, seconds=10):
        deadline = time.time() + seconds
        while not rec.stopped and time.time() < deadline:
            time.sleep(0.05)

    # #269: wait for what a background thread does, up to a bound, rather than
    # for a fixed time, so a slow runner does not fail a test that is right.
    # Each wait fails the test if it runs out, so none passes without testing.
    def wait_for(self, condition, seconds=10):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        return condition()

    def wait_event(self, rec, kind, seconds=10):
        self.assertTrue(self.wait_for(
            lambda: kind in [e["kind"] for e in rec.journal["events"]], seconds),
            f"no {kind!r} event within {seconds} s")

    def wait_first_beat(self, rec, seconds=10):
        # The writer's first pass (its silence check) writes the heartbeat file.
        self.assertTrue(self.wait_for(lambda: (rec.folder / "heartbeat").exists(), seconds),
                        f"the writer made no first pass within {seconds} s")

    def wait_writer_check(self, rec, seconds=10):
        # The writer notes "last seen" just before each silence check, so a
        # second new note means a whole check ran after this call. A recording
        # that stopped has no more checks to run: the caller asserts on that.
        notes = [rec.journal["last_seen"]]

        def two_new_notes():
            if rec.journal["last_seen"] != notes[-1]:
                notes.append(rec.journal["last_seen"])
            return len(notes) > 2 or rec.stopped
        self.assertTrue(self.wait_for(two_new_notes, seconds),
                        f"the writer ran no silence check within {seconds} s")

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
        _, seconds, _ = saved(rec.folder, journal)
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
        _, seconds, _ = saved(rec.folder, journal)
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
        _, seconds, _ = saved(rec.folder, journal)
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
        real = recorder._write_chunk

        def slow(*args):
            time.sleep(1.0)
            real(*args)
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 0.6), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.1), \
                mock.patch.object(recorder, "_write_chunk", slow):
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
                                 clock=lambda: time.time() + offset[0], key=KEY)
        self.assertIs(rec._tick, time.monotonic)          # the default the app relies on
        # #269: a silence limit well above the real 0.2 s between feeds (a slow
        # runner cannot reach it) and well below each 5 s jump of the clock.
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 2.0), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.1):
            self.started(rec)
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
        # silence check: never "slept". (#269: a 1.5 s limit, so a watchdog
        # beat that a busy runner delays is not taken for a stall.)
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 1.5), \
                mock.patch.object(recorder, "HEARTBEAT_S", 30):
            rec, s = self.start()
            s.feed(1)
            quiet_from = time.monotonic()
            self.wait_first_beat(rec)            # the only silence check for 30 s
            time.sleep(2.5)
            quiet = time.monotonic() - quiet_from    # #269: as long as it really was
            s.feed(1)                            # it comes back
            time.sleep(0.3)
            self.assertFalse(rec.stopped)
        journal = rec.stop()
        self.assertEqual([e["kind"] for e in journal["events"]], ["gap"])   # reported, not sleep
        self.assertAlmostEqual(journal["events"][0]["seconds"], quiet, delta=0.4)
        self.assertEqual(journal["state"], "stopped")

    def test_setting_the_clock_back_does_not_pause_the_silence_check(self):
        offset = [0.0]
        rec = recorder.Recording(self.root / "rec", 3, "Mic", RATE, open_stream=opener(self.holder),
                                 clock=lambda: time.time() + offset[0], key=KEY)
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 0.6), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.1):
            self.started(rec)
            self.holder["stream"].feed(1)
            offset[0] -= 3600                    # an hour back; then the microphone goes
            self.wait_stopped(rec, 5)
        self.assertEqual(rec.stop()["state"], "device_lost")

    def test_the_time_limit_follows_the_monotonic_clock(self):
        # #269: a one-second limit, waited for up to 10 s while audio keeps
        # coming (so only the limit can stop it), not for a fixed 1.5 s. On the
        # wall clock, set back an hour, the limit would never be reached.
        offset = [0.0]
        rec = recorder.Recording(self.root / "rec", 3, "Mic", RATE, open_stream=opener(self.holder),
                                 clock=lambda: time.time() + offset[0], key=KEY)
        with mock.patch.object(recorder, "MAX_HOURS", 1 / 3600), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.1):
            began = time.monotonic()
            self.started(rec)
            offset[0] -= 3600                    # setting the time back does not extend it
            s = self.holder["stream"]
            deadline = began + 10
            while not rec.stopped and time.monotonic() < deadline:
                s.feed(0.1)
                time.sleep(0.1)
            took = time.monotonic() - began
            stopped = rec.stopped
            journal = rec.stop()
        self.assertTrue(stopped)
        self.assertIn("time_limit", [e["kind"] for e in journal["events"]])
        # Not before the limit, and not much after it. Measured 0.1 s late on a
        # quiet laptop and at most 0.3 s with every processor busy; the runner
        # once overran 0.5 s. 3 s allows for that many times over, and a limit
        # four times too long still fails.
        self.assertGreaterEqual(took, 1.0)
        self.assertLessEqual(took, 1.0 + 3.0, f"stopped after {took:.1f} s")

    def test_a_lid_closed_while_recording_is_noted_on_opening(self):
        # (#269: a 1 s note limit, so the brief closing below stays brief on a
        # slow runner.)
        with mock.patch.object(recorder, "LID_NOTE_S", 1.0), \
                mock.patch.object(recorder, "HEARTBEAT_S", 30):
            rec, s = self.start()
            s.feed(1)
            rec.lid_changed(False)
            rec.lid_changed(False)               # repeated: the first closing counts
            time.sleep(1.2)
            s.feed(0.5)
            rec.lid_changed(True)
            rec.lid_changed(True)                # repeated opening: noted once
            rec.lid_changed(False)
            rec.lid_changed(True)                # closed only briefly: not noted
        journal = rec.stop()
        lids = [e for e in journal["events"] if e["kind"] == "lid"]
        self.assertEqual(len(lids), 1)
        self.assertGreaterEqual(lids[0]["seconds"], 1.15)
        self.assertEqual(journal["state"], "stopped")    # the recording carried on

    def test_a_lid_closed_while_paused_is_noted_too(self):
        # M19: the recording marks stay while paused, so it is noted alike.
        with mock.patch.object(recorder, "LID_NOTE_S", 0.1), \
                mock.patch.object(recorder, "HEARTBEAT_S", 30):
            rec, s = self.start()
            s.feed(1)
            rec.pause()
            rec.lid_changed(False)
            time.sleep(0.2)
            rec.lid_changed(True)
        self.assertIn("lid", [e["kind"] for e in rec.stop()["events"]])

    def test_a_lost_device_is_reported_once(self):
        rec = recorder.Recording(self.root / "rec", 0, "Mic", RATE, open_stream=opener({}),
                                 key=KEY)
        rec._device_lost()
        rec._device_lost()                       # the driver and the silence check both
        self.assertEqual([e["kind"] for e in rec.journal["events"]], ["device_lost"])
        import threading                         # and at the same moment
        for n in range(20):
            rec = recorder.Recording(self.root / f"race{n}", 0, "Mic", RATE,
                                     open_stream=opener({}), key=KEY)
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
            self.wait_first_beat(rec)            # the writer's first (silence-checking) pass
            beat_before = rec._alive             # the watchdog's latest beat, before the hold-up
            self.asleep[0] += 5                  # the monotonic clock moved 5 s at once
            s.feed(1)                            # audio straight after: not "slept"
            self.wait_event(rec, "stall")        # the watchdog's next beat
            seen = time.monotonic()
            self.assertFalse(rec.stopped)
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual([e["kind"] for e in journal["events"]], ["stall"])
        # #269: the stall is the 5 s hold-up plus the real time since the
        # watchdog's previous beat (about ALIVE_S; longer on a busy runner).
        stall = journal["events"][0]["seconds"]
        self.assertGreaterEqual(stall, 5.0 - 0.01)
        self.assertLessEqual(stall, 5.0 + (seen - beat_before) + 0.01)

    def test_after_a_hold_up_audio_gets_a_full_silence_period(self):
        # The writer's silence check can run before the first audio after a
        # hold-up: it waits while the beat is late, then counts from the stall.
        # (#269: a 1.5 s limit leaves a slow runner room between the steps.)
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 1.5), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.05):
            rec, s = self.start_ticking()
            s.feed(1)
            time.sleep(0.3)
            self.asleep[0] += 4                  # held up 4 s: more than the 1.5 s limit
            self.wait_event(rec, "stall")        # the watchdog beats
            self.wait_writer_check(rec)          # then the writer checks for silence
            s.feed(1)                            # audio within one silence period
            time.sleep(0.2)
            self.assertFalse(rec.stopped)
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual([e["kind"] for e in journal["events"]], ["stall"])

    def test_the_silence_check_waits_while_the_beat_is_late(self):
        # The same, with the writer certain to check before the watchdog beats
        # (the watchdog is held off): it must not call the microphone lost.
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 1.5), \
                mock.patch.object(recorder, "HEARTBEAT_S", 0.05), \
                mock.patch.object(recorder, "ALIVE_S", 30):
            rec, s = self.start_ticking()
            s.feed(1)
            time.sleep(0.3)
            self.asleep[0] += 4                  # held up 4 s; no beat follows
            self.wait_writer_check(rec)          # a silence check runs (#269: waited for)
            self.wait_writer_check(rec)          # and another
            self.assertFalse(rec.stopped)
            s.feed(1)
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")

    def test_a_microphone_lost_after_a_stall_is_a_lost_microphone(self):
        with mock.patch.object(recorder, "SILENT_DEVICE_S", 1.5), \
                mock.patch.object(recorder, "HEARTBEAT_S", 30):
            rec, s = self.start_ticking()
            s.feed(1)
            self.wait_first_beat(rec)
            self.asleep[0] += 4                  # a stall, not a sleep
            self.wait_event(rec, "stall")        # the watchdog notes it
            s.finished()                         # then the microphone goes
        journal = rec.stop()
        self.assertEqual(journal["state"], "device_lost")
        self.assertEqual([e["kind"] for e in journal["events"]], ["stall", "device_lost"])

    def test_a_hold_up_while_paused_is_not_noted(self):
        with mock.patch.object(recorder, "HEARTBEAT_S", 30):
            rec, s = self.start_ticking()
            s.feed(1)
            rec.pause()
            self.asleep[0] += 5
            time.sleep(0.4)
            rec.resume()
            s.feed(1)
        journal = rec.stop()
        self.assertEqual([e["kind"] for e in journal["events"]], ["paused", "resumed"])

    def test_no_sleep_is_judged_before_the_watchdog_runs(self):
        # A slow microphone start: audio can arrive before the watchdog starts.
        asleep = [0.0]
        rec = recorder.Recording(self.root / "rec", 0, "Mic", RATE, open_stream=opener({}),
                                 tick=lambda: time.monotonic() + asleep[0], key=KEY)
        asleep[0] += 600
        rec._callback(np.zeros((1600, 1), dtype=np.int16), 1600, None, None)
        self.assertEqual(rec.journal["state"], "recording")

    def test_a_journal_locked_for_a_moment_is_read_again(self):
        # #148: a file another process holds for a moment (Windows sharing
        # violation) is retried; damaged JSON is not, and a lasting lock gives up.
        folder = self.root / "rec"
        folder.mkdir()
        (folder / "journal.json").write_text('{"state": "recording"}', encoding="utf-8")
        real = Path.read_text
        calls = []

        def locked_twice(path, *args, **kwargs):
            calls.append(path.name)
            if len(calls) <= 2:
                raise PermissionError("in use by another process")
            return real(path, *args, **kwargs)
        with mock.patch.object(recorder, "JOURNAL_READ_WAIT_S", 0.01):
            with mock.patch.object(Path, "read_text", locked_twice):
                self.assertEqual(recorder.read_journal(folder), {"state": "recording"})
            self.assertEqual(len(calls), 3)
            held = mock.Mock(side_effect=PermissionError("held"))
            with mock.patch.object(Path, "read_text", held):
                self.assertIsNone(recorder.read_journal(folder))
            self.assertEqual(held.call_count, recorder.JOURNAL_READ_TRIES)
            missing = mock.Mock(side_effect=FileNotFoundError("gone"))
            with mock.patch.object(Path, "read_text", missing):
                self.assertIsNone(recorder.read_journal(folder))
            self.assertEqual(missing.call_count, 1)          # only a lock is retried
        (folder / "journal.json").write_text("{damaged", encoding="utf-8")
        calls.clear()
        with mock.patch.object(Path, "read_text", lambda p, *a, **k: calls.append(1) or "{x"):
            self.assertIsNone(recorder.read_journal(folder))
        self.assertEqual(len(calls), 1)

    def test_full_disk_keeps_earlier_chunks_and_reports_the_loss(self):
        real = recorder._write_chunk
        calls = {"n": 0}

        def failing(*args):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError(28, "No space left on device")
            return real(*args)

        with mock.patch.object(recorder, "_write_chunk", failing):
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
        _, seconds, _ = saved(rec.folder, journal)
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
        # a temporary file is not, and is deleted (never opened: #111 C3b).
        n = len(journal["chunks"])
        recorder._write_chunk(folder / recorder.chunk_name(recorder.MICROPHONE, n),
                              np.zeros(RATE, "<i2").tobytes(), KEY,
                              recorder.chunk_context(folder.name, recorder.MICROPHONE, n), RATE)
        (folder / "chunk-999998.uma.tmp").write_bytes(b"\x01" * 1001)
        (folder / "heartbeat").write_text(str(journal["started_at"] + 20))
        found = recorder.unfinished(self.root)
        self.assertEqual([f for f, _ in found], [folder])
        _, seconds, notes = saved(folder, found[0][1])
        self.assertEqual(notes, [])
        self.assertFalse((folder / "chunk-999998.uma.tmp").exists())
        self.assertAlmostEqual(seconds, 12.0, places=1)    # 11 s fed, plus the extra chunk
        missing = recorder.missing_tail_seconds(folder, found[0][1], seconds)
        self.assertAlmostEqual(missing, 8.0, places=0)


def marker(start=800, n=32):
    """Bytes the fake stream's audio holds as plain PCM (samples start to
    start + n of its 220 Hz tone): found in a file only if audio was
    written in the clear."""
    t = (np.arange(n) + start) / RATE
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2").tobytes()


# Known answer for test_a_chunk_has_a_known_answer (key 0..31, file id 32..63,
# context chunk_context("0123456789abcdef" * 2, "microphone", 7), samples 0..3).
CHUNK_KAT_SHA256 = "c517bc557e291378789b04e9dde95e350bc2256abd7aee345005528d56b43bc1"


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class EncryptedChunkTests(unittest.TestCase):
    """#111 C3b: every chunk is its own encrypted file under the recording's
    key, bound to its recording, track and place; nothing plain is written."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.holder = {}

    def start(self, name="rec"):
        rec = recorder.Recording(self.root / name, 3, "Mic", RATE,
                                 open_stream=opener(self.holder), key=KEY)
        rec.start()
        self.addCleanup(rec.stop)
        return rec, self.holder["stream"]

    def wait_for(self, condition, seconds=10):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        return condition()

    def test_chunks_are_encrypted_files_and_no_plain_audio_is_written(self):
        rec, s = self.start()
        s.feed(6)
        self.assertTrue(self.wait_for(lambda: (rec.folder / "chunk-000000.uma").exists()))
        self.assertEqual(files_holding(self.root, marker()), [])      # while recording
        journal = rec.stop()
        names = sorted(p.name for p in rec.folder.iterdir())
        self.assertEqual(names, ["chunk-000000.uma", "chunk-000001.uma", "heartbeat",
                                 "journal.json"])
        for name in names[:2]:
            self.assertEqual((rec.folder / name).read_bytes()[:4], b"UMA1")
        self.assertEqual(files_holding(self.root, marker()), [])      # after stop
        self.assertEqual(files_holding(self.root, KEY), [])            # no key in any file
        pcm, seconds, notes = saved(rec.folder, journal)               # yet it is all there
        self.assertIn(marker(), pcm.tobytes())
        self.assertEqual((len(pcm), notes), (s.fed, []))
        self.assertEqual(list(self.root.rglob("recording.wav")), [])
        self.assertEqual(files_holding(self.root, marker()), [])      # reading wrote nothing

    def test_a_chunk_has_a_known_answer(self):
        # Pins how a chunk's context goes into its file key (with the header,
        # nonces and associated data): a change fails here, not on saved
        # recordings. Fixed key, file id and samples.
        from workspace import audiocrypt
        self.assertEqual(recorder.chunk_context("r1", recorder.COMPUTER, 3),
                         ("recording", "r1", "computer", 3))
        self.assertEqual(recorder.chunk_name(recorder.COMPUTER, 3), "sys-000003.uma")
        path = self.root / "chunk-000007.uma"
        with mock.patch.object(audiocrypt.secrets, "token_bytes",
                               return_value=bytes(range(32, 64))):
            recorder._write_chunk(path, np.arange(4, dtype="<i2").tobytes(), KEY,
                                  recorder.chunk_context("0123456789abcdef" * 2,
                                                         recorder.MICROPHONE, 7), RATE)
        import hashlib
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), CHUNK_KAT_SHA256)

    def test_a_stale_temporary_file_does_not_stop_the_first_chunk(self):
        rec, s = self.start()
        (rec.folder / "chunk-000000.uma.tmp").write_bytes(b"\x01" * 1001)   # an earlier try
        s.feed(6)
        journal = rec.stop()
        self.assertEqual((journal["state"], journal["events"]), ("stopped", []))
        self.assertEqual(len(journal["chunks"]), 2)
        self.assertEqual(len(saved(rec.folder, journal)[0]), s.fed)
        self.assertEqual(list(rec.folder.glob("*.tmp")), [])

    def test_a_retry_writes_a_new_file_with_a_new_id(self):
        # No nonce is used twice: a failed write is never resumed; the retry
        # starts a new file, with a new random id and so a new key.
        from workspace import audiocrypt
        ids, calls = [], {"n": 0}
        real_init, real_write = audiocrypt.AudioWriter.__init__, audiocrypt.AudioWriter.write

        def init(self, *args, **kwargs):
            real_init(self, *args, **kwargs)
            ids.append(self.file_id)

        def write(self, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(13, "held by another program")
            return real_write(self, data)
        with mock.patch.object(audiocrypt.AudioWriter, "__init__", init), \
                mock.patch.object(audiocrypt.AudioWriter, "write", write), \
                mock.patch.object(recorder, "_retrying", _retry_now):
            rec, s = self.start()
            s.feed(6)
            journal = rec.stop()
        self.assertEqual(journal["events"], [])
        self.assertEqual(len(ids), 3)                     # two tries for the first chunk
        with audiocrypt.AudioReader(rec.folder / "chunk-000000.uma", KEY,
                                    recorder.chunk_context(rec.folder.name,
                                                           recorder.MICROPHONE, 0)) as r:
            self.assertEqual(r.file_id, ids[1])
            self.assertNotEqual(r.file_id, ids[0])
        self.assertEqual(len(saved(rec.folder, journal)[0]), s.fed)

    def test_a_damaged_or_swapped_chunk_becomes_silence_and_is_reported(self):
        rec, s = self.start()
        s.feed(16)
        journal = rec.stop()
        self.assertEqual([c["frames"] for c in journal["chunks"]],
                         [5 * RATE, 5 * RATE, 5 * RATE, RATE])
        f = rec.folder
        (f / "chunk-000001.uma").write_bytes((f / "chunk-000002.uma").read_bytes())  # swapped
        data = bytearray((f / "chunk-000003.uma").read_bytes())
        data[len(data) // 2] ^= 1                                                     # damaged
        (f / "chunk-000003.uma").write_bytes(bytes(data))
        pcm, seconds, notes = saved(f, journal)
        self.assertEqual(len(pcm), s.fed)                 # later times still match
        self.assertTrue(pcm[: 5 * RATE].any())
        self.assertFalse(pcm[5 * RATE: 10 * RATE].any())  # silence of its listed length
        self.assertTrue(pcm[10 * RATE: 15 * RATE].any())
        self.assertFalse(pcm[15 * RATE:].any())
        self.assertEqual([(n["kind"], n["at_s"], n["seconds"]) for n in notes],
                         [("damaged", 5.0, 5.0), ("damaged", 15.0, 1.0)])
        self.assertIn("could not be read", notes[0]["note"])

    def test_chunks_are_never_taken_out_of_order_or_from_elsewhere(self):
        a, s = self.start("a")
        s.feed(11)
        journal = a.stop()
        b, s2 = self.start("b")
        s2.feed(1)
        journal_b = b.stop()
        # A chunk of another recording (same key: the context differs), a
        # chunk of the other track, a reordered journal and a name that
        # points outside the folder: none is ever read as this place's audio.
        (b.folder / "chunk-000000.uma").write_bytes((a.folder / "chunk-000000.uma").read_bytes())
        self.assertFalse(saved(b.folder, journal_b)[0].any())
        sys_chunk = a.folder / "sys-000000.uma"
        recorder._write_chunk(sys_chunk, np.full(RATE, 1000, "<i2").tobytes(), KEY,
                              recorder.chunk_context(a.folder.name, recorder.COMPUTER, 0), RATE)
        (b.folder / "chunk-000000.uma").write_bytes(sys_chunk.read_bytes())
        self.assertFalse(saved(b.folder, journal_b)[0].any())
        sys_chunk.unlink()
        reordered = dict(journal, chunks=[journal["chunks"][1], journal["chunks"][0],
                                          journal["chunks"][2]])
        pcm, _, notes = saved(a.folder, reordered)
        self.assertFalse(pcm[: 10 * RATE].any())
        self.assertEqual([n["at_s"] for n in notes], [0.0, 5.0])
        (self.root / "outside.uma").write_bytes((a.folder / "chunk-000000.uma").read_bytes())
        escaped = dict(journal, chunks=[{"file": "..\\outside.uma", "frames": 5 * RATE}])
        pcm, _, notes = saved(a.folder, escaped)
        self.assertFalse(pcm[: 5 * RATE].any())
        self.assertEqual(notes[0]["kind"], "damaged")

    def test_no_plain_writer_is_left_in_the_recording_path(self):
        # Static: the recorder, the mixer and the import write no WAV and no
        # raw PCM (the plain writer is for the journal alone), and the
        # recorder never opens an unfinished file (allow_incomplete).
        import re
        root = Path(__file__).resolve().parents[1] / "workspace"
        found = []
        for name in ("recorder.py", "mixdown.py", "media.py", "app.py"):
            text = (root / name).read_text(encoding="utf-8")
            if name != "app.py" and re.search(r"\bimport wave\b|\bwave\.open\b", text):
                found.append((name, "wave"))
            for call in re.findall(r"(?<!def )_write_atomic\(([^,]*),", text):
                if "journal.json" not in call:
                    found.append((name, call.strip()))
            if re.search(r"allow_incomplete\s*=", text):
                found.append((name, "allow_incomplete"))
        self.assertEqual(found, [])

    def test_a_wrong_key_reads_nothing(self):
        rec, s = self.start()
        s.feed(6)
        journal = rec.stop()
        audio = recorder.open_recording(rec.folder, journal, bytes(32), [])
        self.assertFalse(audio.readable)


def _retry_now(fn, attempts=3):
    """recorder._retrying without its wait."""
    for i in range(attempts):
        try:
            return fn()
        except OSError:
            if i == attempts - 1:
                raise


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
        # No test may change the real power scheme (#188): any write or apply
        # through the real API fails the test instead.
        from workspace import power

        def never(*args, **kwargs):
            raise AssertionError("a test reached the real power scheme")
        self.power_guard = [mock.patch.object(power._PowrProf, name, never)
                            for name in ("write", "apply")]
        for guard in self.power_guard:
            guard.start()
        self.addCleanup(lambda: [guard.stop() for guard in self.power_guard])
        self.ws = self.make()

    def make(self, **kw):
        kw.setdefault("protector", keyprotect.TestProtector())
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
        info = json.loads(self.ws.store.recording_info(m["id"]))   # sealed (#111 C3b)
        self.assertEqual(info["consent"], CONSENT)
        self.assertEqual(info["sources"], ["microphone"])
        self.assertFalse((self.ws.media / m["id"] / "recording.json").exists())

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

    def fake_lid(self, ws, ac=1, dc=1, managed=False):
        """A fake power scheme and lid watch: tests never change the real one."""
        from test_power import FakeScheme

        from workspace import power
        scheme = FakeScheme(ac=ac, dc=dc)
        ws.lid = power.LidSwitch(ws.root / "power_restore.json", api=scheme,
                                 managed=lambda: managed)
        watch = type("Watch", (), {"on": 0, "start": lambda s: setattr(s, "on", s.on + 1),
                                   "stop": lambda s: setattr(s, "on", s.on - 1)})()
        ws.lid_watch = watch
        return scheme, watch

    def test_the_lid_action_is_do_nothing_exactly_while_recording(self):
        from workspace import policy
        self.ws.settings.update(microphone=True)
        self.ws.start()
        scheme, watch = self.fake_lid(self.ws, ac=1, dc=2)
        self.ws.start_recording(3, consent=CONSENT)      # setting off: nothing changes
        self.assertEqual((scheme.values, watch.on), ({True: 1, False: 2}, 1))
        self.assertFalse(self.ws.recording_status()["lid_override"])
        self.ws.stop_recording()
        self.assertEqual(watch.on, 0)
        self.ws.set_settings({"lid_override": True})
        seen = []
        self.ws.recording_indicator = lambda on: seen.append(dict(scheme.values))
        self.ws.start_recording(3, consent=CONSENT)
        self.assertTrue(self.ws.recording_status()["lid_override"])
        self.ws.stop_recording()                         # stopped
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.discard_recording()                      # discarded
        self.ws.start_recording(3, consent=CONSENT)
        self.ws._recording._device_lost()                # stops itself
        deadline = time.time() + 10
        while self.ws._recording is not None and time.time() < deadline:
            time.sleep(0.05)
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.policy = policy.PolicySource(               # IT turns recording off
            reader=lambda: ({"AllowMicrophoneRecording": False}, ()))
        self.ws._policy_tick()
        self.ws.policy = policy.PolicySource(reader=lambda: ({}, ()))
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.close()                                  # saved at close
        do_nothing, original = {True: 0, False: 0}, {True: 1, False: 2}
        self.assertEqual(seen, [do_nothing, original] * 5)
        self.assertFalse((self.ws.root / "power_restore.json").exists())

    def test_turning_the_lid_setting_off_or_a_policy_puts_it_back_at_once(self):
        from workspace import policy
        self.ws.settings.update(microphone=True, lid_override=True)
        self.ws.start()
        scheme, _ = self.fake_lid(self.ws)
        self.ws.start_recording(3, consent=CONSENT)
        self.assertEqual(scheme.values, {True: 0, False: 0})
        self.ws.set_settings({"lid_override": False})    # mid-recording
        self.assertEqual(scheme.values, {True: 1, False: 1})
        self.assertFalse(self.ws.recording_status()["lid_override"])
        self.ws.stop_recording()
        self.ws.set_settings({"lid_override": True})
        self.ws.start_recording(3, consent=CONSENT)
        self.ws.policy = policy.PolicySource(               # IT turns it off mid-recording
            reader=lambda: ({"AllowLidOverride": False}, ()))
        self.ws._policy_tick()
        self.assertEqual(scheme.values, {True: 1, False: 1})
        with self.assertRaisesRegex(WorkspaceError, "organisation"):
            self.ws.set_settings({"lid_override": True})
        self.assertFalse(self.ws.get_settings()["lid_override"])
        self.ws.stop_recording()
        self.ws.start_recording(3, consent=CONSENT)      # policy off: never set
        self.assertEqual(scheme.writes[-1], (False, 1))
        self.ws.stop_recording()

    def test_a_lid_managed_by_the_organisation_is_left_alone(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        scheme, _ = self.fake_lid(self.ws, managed=True)
        with self.assertRaisesRegex(WorkspaceError, "IT team"):
            self.ws.set_settings({"lid_override": True})
        s = self.ws.get_settings()
        self.assertEqual((s["lid_override"], s["lid_managed"]), (False, True))
        self.ws.settings.update(lid_override=True)       # saved before IT took it over
        self.ws.start_recording(3, consent=CONSENT)
        self.assertEqual(scheme.writes, [])
        self.assertFalse(self.ws.recording_status()["lid_override"])
        self.ws.stop_recording()

    def test_a_lid_left_by_a_crash_is_put_back_before_anything_else(self):
        # M6 and F4: the restore runs at construction, before the database.
        from test_power import FakeScheme

        from workspace import power
        root = self.root / "crashed"
        root.mkdir()
        scheme = FakeScheme(ac=1, dc=2)
        sw = power.LidSwitch(root / "power_restore.json", api=scheme, managed=lambda: False)
        sw.override()                                    # the app died here
        with mock.patch("workspace.app.Store", side_effect=RuntimeError("damaged")):
            with self.assertRaises(RuntimeError):        # even a start that fails later
                Workspace(root, self.root / "models", start=False, lid_switch=sw,
                          protector=keyprotect.TestProtector())
        self.assertEqual(scheme.values, {True: 1, False: 2})
        self.assertFalse((root / "power_restore.json").exists())

    def test_a_lid_error_never_stops_a_recording_ending(self):
        # F1: the recorder is always told to stop.
        self.ws.settings.update(microphone=True, lid_override=True)
        self.ws.start()
        self.fake_lid(self.ws)

        def broken():
            raise PermissionError("locked")
        self.ws.lid.restore = broken
        self.ws.start_recording(3, consent=CONSENT)
        rec = self.ws._recording
        self.ws.stop_recording()
        self.assertTrue(rec.stopped)
        self.assertIsNone(self.ws._recording)

    def test_an_unreadable_policy_grants_no_lid_change(self):
        # M11 and M12: withheld at the start, and put back on the tick.
        from workspace import policy
        self.ws.settings.update(microphone=True, lid_override=True)
        self.ws.start()
        scheme, _ = self.fake_lid(self.ws)
        self.ws.policy = policy.PolicySource(reader=lambda: ({}, ()))
        self.ws.start_recording(3, consent=CONSENT)
        self.assertEqual(scheme.values, {True: 0, False: 0})
        unreadable = policy.PolicySource(reader=lambda: ({}, policy.UNREADABLE))
        self.assertTrue(unreadable.current().unreadable)   # a key that cannot be read at all
        self.ws.policy = unreadable
        self.ws._policy_tick()
        self.assertEqual(scheme.values, {True: 1, False: 1})
        self.ws.policy = policy.PolicySource(reader=lambda: ({}, ()))
        self.ws.stop_recording()
        writes = len(scheme.writes)
        self.ws.policy = policy.PolicySource(              # a bad value: withheld, never set
            reader=lambda: ({}, ("AllowLidOverride",)))
        self.ws.start_recording(3, consent=CONSENT)
        self.assertEqual(len(scheme.writes), writes)
        self.ws.stop_recording()

    def test_the_idle_retry_never_undoes_a_recording_that_has_started(self):
        # R1: the retry runs under the lock that sets the lid for a recording.
        self.ws.settings.update(microphone=True, lid_override=True)
        self.ws.start()
        scheme, _ = self.fake_lid(self.ws)
        self.ws.start_recording(3, consent=CONSENT)
        self.ws._lid_retry_at = float("-inf")
        with mock.patch.object(self.ws, "_capturing", lambda: False):   # the racing read
            self.ws._policy_tick()
        self.assertEqual(scheme.values, {True: 0, False: 0})
        self.assertTrue(self.ws.recording_status()["lid_override"])
        self.ws.stop_recording()
        self.assertEqual(scheme.values, {True: 1, False: 1})

    def test_a_damaged_record_is_left_and_the_user_told_once(self):
        # The user's decision on #188: leave the lid as it is, say so once.
        from test_power import FakeScheme

        from workspace import power
        root = self.root / "lost"
        root.mkdir()
        (root / "power_restore.json").write_text("{damaged", encoding="utf-8")
        scheme = FakeScheme(ac=0, dc=0)
        sw = power.LidSwitch(root / "power_restore.json", api=scheme, managed=lambda: False)
        ws = Workspace(root, self.root / "models", start=False, lid_switch=sw,
                       protector=keyprotect.TestProtector())
        try:
            ws._policy_tick()
            lids = [r for r in ws.recording_status()["reports"] if r.get("lid")]
            self.assertEqual(len(lids), 1)
            self.assertIn("could not put your lid setting back", lids[0]["note"])
            self.assertEqual(scheme.writes, [])          # never guessed
            ws._policy_tick()
            self.assertEqual(len([r for r in ws.recording_status()["reports"]
                                  if r.get("lid")]), 1)  # told once
        finally:
            ws.close()

    def test_a_failed_restore_is_retried_while_idle(self):
        # F6: a restore that fails at the end of a recording is not silent.
        self.ws.settings.update(microphone=True, lid_override=True)
        self.ws.start()
        scheme, _ = self.fake_lid(self.ws, ac=1, dc=2)
        self.ws.start_recording(3, consent=CONSENT)
        scheme.write_rc = 5                              # Windows refuses for now
        self.ws.stop_recording()
        self.assertTrue(self.ws.lid.active)
        self.assertTrue(any(r.get("lid") for r in self.ws.recording_status()["reports"]))
        scheme.write_rc = 0
        self.ws._lid_retry_at = float("-inf")
        held, real = [], self.ws._capturing

        def checked():                                   # R1/T1: decided under the lock
            held.append(self.ws._indicator_lock.locked())
            return real()
        with mock.patch.object(self.ws, "_capturing", checked):
            self.ws._policy_tick()
        self.assertTrue(held and all(held), held)       # every read held the lock
        self.assertEqual(scheme.values, {True: 1, False: 2})
        self.assertFalse(self.ws.lid.active)

    def test_the_card_says_when_the_lid_setting_could_not_be_changed(self):
        self.ws.settings.update(microphone=True, lid_override=True)
        self.ws.start()
        self.fake_lid(self.ws, managed=True)
        self.ws.start_recording(3, consent=CONSENT)
        status = self.ws.recording_status()
        self.assertEqual((status["lid_override"], status["lid_refused"]), (False, "managed"))
        self.ws.stop_recording()
        self.assertIsNone(self.ws.recording_status()["lid_refused"])

    def test_lid_changes_reach_the_recording(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.fake_lid(self.ws)
        self.ws._lid_changed(False)                      # not recording: ignored
        with mock.patch.object(recorder, "LID_NOTE_S", 0.1):
            self.ws.start_recording(3, consent=CONSENT)
            self.holder["stream"].feed(1)
            self.ws._lid_changed(False)
            time.sleep(0.2)
            self.ws._lid_changed(True)
            kinds = [e["kind"] for e in self.ws.recording_status()["recording"]["events"]]
            self.ws.stop_recording()
        self.assertIn("lid", kinds)

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
        self.assertIsNotNone(self.ws.store.recording_info(meetings[0]["id"]))
        self.assertEqual(self.ws.store.recording_key_ids(), [])     # erased with the import
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
        # Let go first (#148): with the handle still set, the app's recorder-watch
        # thread would "finish" a stopped recording in the gap, saving it as
        # stopped, so the next start would not see a crash to recover.
        self.ws._recording = None
        rec._stop.set()                                # the app closes without stopping it
        rec._writer.join(5)
        journal = json.loads((rec.folder / "journal.json").read_text())
        journal["state"] = "recording"
        (rec.folder / "journal.json").write_text(json.dumps(journal))
        self.ws.close()
        self.ws = self.make()
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(60))
        waiting = self.ws.recording_status()["reports"][-1]
        # Self-explaining (#148): a failure shows which report came instead.
        self.assertTrue(waiting.get("recovered"), waiting)
        locked = mock.patch("shutil.rmtree", lambda *a, **k: None)   # a file is locked
        with locked, self.assertRaises(WorkspaceError):               # and it says so
            self.ws.delete_pending_recording(waiting["id"])
        self.assertTrue(recorder.is_discarded(rec.folder))      # so it is never offered again
        self.assertEqual(self.ws.store.recording_key_ids(), [])  # its key went first (C3b)
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
        # Let go first (#148): with the handle still set, the app's recorder-watch
        # thread would "finish" a stopped recording in the gap, saving it as
        # stopped, so the next start would not see a crash to recover.
        self.ws._recording = None
        rec._stop.set()                                # the process "dies"
        rec._writer.join(5)
        journal = json.loads((rec.folder / "journal.json").read_text())
        journal["state"] = "recording"
        (rec.folder / "journal.json").write_text(json.dumps(journal))
        (rec.folder / "heartbeat").write_text(str(journal["started_at"] + 30))
        self.ws.close()
        self.ws = self.make()
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(120))
        waiting = self.ws.recording_status()["reports"][-1]   # not transcribed until the user says
        self.assertTrue(waiting.get("recovered"), waiting)   # self-explaining (#148)
        self.assertEqual(self.ws.meetings(), [])
        self.ws.retry_recording_import(waiting["id"])        # Keep and transcribe
        self.assertTrue(self.ws.wait_idle(120))
        report = self.ws.recording_status()["reports"][-1]
        self.assertEqual(report["seconds"], 11.0)      # all the writer had saved
        gap = [e for e in report["events"] if e["kind"] == "interrupted"]
        self.assertEqual(len(gap), 1)
        self.assertGreater(gap[0]["seconds"], 15)
        self.assertEqual(len(self.ws.meetings()), 1)

    # -- #111 C3b: encrypted chunks, the recording over stdin, crypto-erase --------------

    def wait_for(self, condition, seconds=10):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        return condition()

    def wrapped_key(self, rid):
        """The recording key's wrapped bytes, checkpointed into the main
        database file, so a test can look for them in the files later."""
        row = self.ws.store._one("SELECT wrapped FROM recording_keys WHERE recording_id = ?",
                                 (rid,))
        with self.ws.store._lock:
            self.ws.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wrapped = bytes(row["wrapped"])
        self.assertIn(wrapped, (self.ws.root / "workspace.db").read_bytes())
        return wrapped

    def in_database_files(self, needle):
        found = []
        for name in ("workspace.db", "workspace.db-wal"):
            path = self.ws.root / name
            if path.exists() and needle in path.read_bytes():
                found.append(name)
        return found

    def test_no_plain_audio_ever_and_the_meeting_holds_what_was_recorded(self):
        # End to end through the real recorder path (a fake input device), the
        # real sandboxed decoder fed over its stdin, and the encrypted import.
        from workspace import media
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        stream, folder = self.holder["stream"], self.ws._recording.folder
        stream.feed(7)
        self.assertTrue(self.wait_for(lambda: (folder / "chunk-000000.uma").exists()))
        scans = [files_holding(self.ws.root, marker())]               # while recording
        real = recorder.RecordingAudio.blocks

        def blocks(audio):
            for i, block in enumerate(real(audio)):
                if i == 0:                                            # while the decoder is fed
                    scans.append(files_holding(self.ws.root, marker()))
                    scans.append([p.name for p in self.ws.root.rglob("recording.wav")])
                yield block
        with mock.patch.object(recorder.RecordingAudio, "blocks", blocks):
            self.ws.stop_recording()
            self.assertTrue(self.ws.wait_idle(120))
        scans.append(files_holding(self.ws.root, marker()))           # after the import
        self.assertEqual(scans, [[]] * 4)
        [m] = self.ws.meetings()
        reader = media.open_audio(self.ws.store, self.ws.media, m["id"])
        try:
            decoded = reader.read(0, len(reader))
        finally:
            reader.close()
        t = np.arange(stream.fed) / RATE
        fed = (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16) / 32768.0
        self.assertEqual(len(decoded), stream.fed)                    # 16 kHz in, 16 kHz out
        self.assertLess(float(np.max(np.abs(decoded - fed))), 1e-3)
        self.assertTrue(self.ws.transcript(m["id"])["items"])

    def test_the_recording_key_is_erased_with_the_import(self):
        from workspace.store import KeyMissing
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        rid = self.ws._recording.folder.name
        self.holder["stream"].feed(3)
        wrapped = self.wrapped_key(rid)
        self.assertEqual(self.ws.store.recording_key_ids(), [rid])
        self.ws.stop_recording()
        self.assertTrue(self.ws.wait_idle(120))
        self.assertEqual(len(self.ws.meetings()), 1)
        self.assertEqual(self.in_database_files(wrapped), [])       # not in the log either
        with self.assertRaises(KeyMissing):
            self.ws.store.recording_key(rid)
        self.assertTrue(self.ws.store.recording_imported(rid))

    def test_a_discard_erases_the_key_before_any_file_is_deleted(self):
        from workspace import audiocrypt
        from workspace.store import KeyMissing
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws.start_recording(3, consent=CONSENT)
        folder = self.ws._recording.folder
        self.holder["stream"].feed(6)
        self.assertTrue(self.wait_for(lambda: (folder / "chunk-000000.uma").exists()))
        wrapped = self.wrapped_key(folder.name)
        seen = {}

        def stops_after_one(f):                     # a file is held: removal stops
            seen["keys"] = self.ws.store.recording_key_ids()
            seen["database"] = self.in_database_files(wrapped)
            (Path(f) / "chunk-000000.uma").unlink()
            raise OSError(32, "in use")
        with mock.patch.object(recorder, "remove_folder", stops_after_one):
            with self.assertRaises(WorkspaceError):
                self.ws.discard_recording()
        self.assertEqual(seen, {"keys": [], "database": []})       # before the first file went
        with self.assertRaises(KeyMissing):
            self.ws.store.recording_key(folder.name)
        left = folder / "chunk-000001.uma"
        self.assertTrue(left.exists())
        with self.assertRaises(audiocrypt.AudioCryptError):        # not with any key it had
            audiocrypt.AudioReader(left, bytes(32), recorder.chunk_context(
                folder.name, recorder.MICROPHONE, 1))
        self.ws.close()
        self.ws = self.make()                                      # the next start deletes it
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(60))
        self.assertFalse(folder.exists())
        self.assertEqual(self.ws.meetings(), [])

    def test_keys_without_a_folder_go_and_folders_without_their_key_stay(self):
        from workspace.store import KeyMissing
        rec_root = self.ws.root / "recordings"
        orphan = "f" * 32                      # a crash between the key and the folder
        self.ws.store.create_recording_key(orphan)
        keyless, wrong = rec_root / ("e" * 32), rec_root / ("d" * 32)
        self.ws.store.create_recording_key(wrong.name)    # not the key it was saved under
        for folder in (keyless, wrong):
            rec = recorder.Recording(folder, 3, "Mic", RATE, open_stream=opener(self.holder),
                                     key=KEY)
            rec.start()
            self.holder["stream"].feed(2)
            rec.stop()
        self.ws.close()
        self.ws = self.make()
        self.ws.settings.update(microphone=True)
        self.assertEqual(self.ws.store.recording_key_ids(), [wrong.name])   # the orphan went
        self.ws.start()
        self.assertTrue(self.ws.wait_idle(60))
        self.assertEqual(self.ws.meetings(), [])
        self.assertTrue(keyless.is_dir() and wrong.is_dir())             # never deleted
        reports = self.ws.recording_status()["reports"]
        self.assertEqual(sorted(r["pending"] for r in reports), [wrong.name, keyless.name])
        for r in reports:
            self.assertIn("could not be read", r["note"])
        self.assertEqual(self.ws.store.recording_key_ids(), [wrong.name])   # kept with it
        with self.assertRaises(KeyMissing):
            self.ws.store.recording_key(keyless.name)
        for r in reports:                                                 # the user's Delete
            self.ws.delete_pending_recording(r["id"])
        self.assertEqual(self.ws.store.recording_key_ids(), [])
        self.assertEqual(list(rec_root.iterdir()), [])

    def test_a_failure_after_the_import_leaves_no_meeting_and_no_rows(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()
        self.ws._plan_version = mock.Mock(side_effect=RuntimeError("planning failed"))
        self.ws.start_recording(3, consent=CONSENT)
        rid = self.ws._recording.folder.name
        self.holder["stream"].feed(3)
        self.ws.stop_recording()
        self.assertTrue(self.ws.wait_idle(120))
        self.assertEqual(self.ws.meetings(), [])
        q = self.ws.store._one
        self.assertEqual([q(f"SELECT COUNT(*) FROM {t}")[0] for t in
                          ("meetings", "sources", "content_keys", "recording_sources",
                           "recording_keys")], [0, 0, 0, 0, 0])
        report = self.ws.recording_status()["reports"][-1]
        self.assertEqual(report["pending"], rid)                  # reported; its key went
        del self.ws._plan_version
        self.ws.retry_recording_import(report["id"])
        self.assertTrue(self.ws.wait_idle(60))
        self.assertIn("could not be read", self.ws.recording_status()["reports"][-1]["note"])
        self.assertEqual(self.ws.meetings(), [])

    def test_a_start_that_fails_leaves_no_key(self):
        self.ws.settings.update(microphone=True)
        self.ws.start()

        def broken(device, rate, callback, finished):
            raise OSError("the device went")
        self.ws.open_stream = broken
        with self.assertRaises(WorkspaceError):
            self.ws.start_recording(3, consent=CONSENT)
        self.assertEqual(self.ws.store.recording_key_ids(), [])
        self.assertFalse(any((self.ws.root / "recordings").glob("*")))


if __name__ == "__main__":
    unittest.main()
