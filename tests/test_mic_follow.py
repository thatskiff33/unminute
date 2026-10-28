# tests/test_mic_follow.py
# The microphone follows Windows mid-recording (#462, the owner's decisions of
# 28 Oct 2026): a new Windows default microphone is followed, a microphone
# that is lost moves to the one Windows now uses, and the person may choose
# another on the card, which stops following the default. Only a recording
# that started on the Windows default follows a new default; one started on
# a microphone the person chose stays on it, and if that one is lost it moves
# to the default and follows it from then on (the owner's refinement of 28
# Oct 2026, DEC-080). The recording
# carries on in the same meeting; the short gap is saved as silence and
# reported as a gap; nothing committed is lost or saved twice (D1); a pause
# stays paused (D2); managed policy is read before every switch (K3); events
# name no device (P3); the mix lines a switch up as after a resume.
#
# Windows is a fake here (FakeWindows: its default microphone and the
# microphones PortAudio would list), and so is the audio stream, as in
# tests/test_recorder.py. Real Bluetooth and USB microphones are checked by
# hand at #189's sound sitting.
#   python tools/run_tests.py --only test_mic_follow

import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # keyprotect, fake engines
import keyprotect  # noqa: E402

from workspace import consent  # noqa: E402  (stdlib only)

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
RATE = 16000
CRATE = 48000
KEY = bytes(range(32))
LAPTOP, HEADSET, USB = "Laptop microphone", "Headset microphone", "USB microphone"
INDEX = {LAPTOP: 3, HEADSET: 5, USB: 7}           # PortAudio's numbers for them
CONSENT = consent.recording_record(clock=lambda: 1000.0)

if HAVE_DEPS:
    import numpy as np

    from workspace import api, mixdown, recorder
    from workspace.app import Workspace, WorkspaceError


def tone(n, start=0, rate=RATE, hz=220):
    """What a FakeStream sends: its 220 Hz tone from sample start."""
    t = (np.arange(n) + start) / rate
    return (np.sin(2 * np.pi * hz * t) * 8000).astype(np.int16)


class FakeStream:
    """A sounddevice InputStream on one device; the test pushes audio."""

    def __init__(self, device, rate, callback, finished):
        self.device, self.rate, self.callback, self.finished = device, rate, callback, finished
        self.started = self.stopped = self.closed = False
        self.fed = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.finished()          # like PortAudio: finished_callback runs on stop()

    def close(self):
        self.closed = True

    def feed(self, seconds):
        n = int(self.rate * recorder.BLOCK_S)
        for _ in range(int(round(seconds / recorder.BLOCK_S))):
            self.callback(tone(n, self.fed, self.rate).reshape(-1, 1), n, None, None)
            self.fed += n


class FakeWindows:
    """Windows' microphones as the recorder sees them: the default (a fake
    of WASAPI's default capture device), the microphones PortAudio lists
    (find_input's shape), and the streams opened. A test changes the
    default, or connects and disconnects microphones."""

    def __init__(self, default=LAPTOP, present=(LAPTOP, HEADSET, USB)):
        self.default = default               # a name, or None: no microphone
        self.present = set(present)
        self.streams = []
        self.allowed = True
        self.watch_threads, self.close_threads = [], []
        self.lock = threading.Lock()

    def watch(self):
        outer = self

        class Watch:
            def __init__(self):
                outer.watch_threads.append(threading.get_ident())

            def default(self):
                name = outer.default
                return None if name is None else {"id": "{0.0.1}." + name, "name": name}

            def close(self):
                outer.close_threads.append(threading.get_ident())
        return Watch()

    def resolve(self, name, default=False):
        if name in self.present:
            return {"index": INDEX[name], "name": name, "label": name, "rate": RATE}
        if default and self.default in self.present:
            return {"index": INDEX[self.default], "name": self.default,
                    "label": self.default, "rate": RATE}
        return None

    def open_stream(self, device, rate, callback, finished):
        if device not in {INDEX[n] for n in self.present}:
            raise OSError("no such device")         # PortAudio cannot open it
        stream = FakeStream(device, rate, callback, finished)
        with self.lock:
            self.streams.append(stream)
        return stream

    def follow(self):
        return {"watch": self.watch, "resolve": self.resolve, "allowed": lambda: self.allowed}

    @property
    def stream(self):
        return self.streams[-1]


class Flicker:
    """A watch whose second check reports the headset, the rest the laptop."""

    def __init__(self, watch, seen):
        self.watch, self.seen = watch, seen

    def default(self):
        self.seen.append(1)
        return ({"id": "{0.0.1}." + HEADSET, "name": HEADSET} if len(self.seen) == 2
                else self.watch.default())

    def close(self):
        self.watch.close()


class HeldSecondSighting:
    """A watch that holds its second sighting of the headset as the default
    until the test lets it go: the follow thread has then read `following`
    and is part-way through a check when the person chooses on the card."""

    def __init__(self):
        self.sightings = 0
        self.holding, self.go = threading.Event(), threading.Event()

    def wrap(self, watch):
        outer = self

        class Held:
            def default(self):
                found = watch.default()
                if found and found["name"] == HEADSET:
                    outer.sightings += 1
                    if outer.sightings == 2:
                        outer.holding.set()
                        outer.go.wait(10)
                return found

            def close(self):
                watch.close()
        return Held()


class FirstReadFails:
    """A watch whose first read of the default fails, the rest as Windows."""

    def __init__(self, watch):
        self.watch, self.reads = watch, 0

    def default(self):
        self.reads += 1
        if self.reads == 1:
            raise OSError("Windows audio is not ready yet.")
        return self.watch.default()

    def close(self):
        self.watch.close()


class HeldLossRead:
    """A watch that holds its second read of the default until the test
    lets it go. On a recording that follows no default, the first read is
    the starting point and the second is the follow thread's read for a lost
    microphone: the thread has seen the loss and is part-way through it."""

    def __init__(self, watch):
        self.watch, self.reads = watch, 0
        self.holding, self.go = threading.Event(), threading.Event()

    def default(self):
        self.reads += 1
        if self.reads == 2:
            self.holding.set()
            self.go.wait(10)
        return self.watch.default()

    def close(self):
        self.watch.close()


class FakeLoopback:
    """Computer audio, pushed by the test (as tests/test_two_track.py)."""

    def __init__(self, source, callback, notify):
        self.callback, self.device_name, self.fed = callback, "Test speakers", 0

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass

    def feed(self, seconds):
        n = int(CRATE * recorder.BLOCK_S)
        for _ in range(int(round(seconds / recorder.BLOCK_S))):
            self.callback(tone(n, self.fed, CRATE, 1000), time.monotonic() - n / CRATE)
            self.fed += n


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    return condition()


FAST = {"DEFAULT_CHECK_S": 0.05, "FOLLOW_TICK_S": 0.02, "SILENT_DEVICE_S": 0.6,
        "HEARTBEAT_S": 0.1}


class _Recording(unittest.TestCase):
    """A recording on FakeWindows' microphones, with the follow thread fast."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.win = FakeWindows()
        for key, value in FAST.items():
            patch = mock.patch.object(recorder, key, value)
            patch.start()
            self.addCleanup(patch.stop)

    def start(self, name=LAPTOP, computer=None, **follow):
        """A recording on name, which starts on the Windows default (and so
        follows it, DEC-080) when it is FakeWindows' default, as the app tells
        the recorder; a test may say otherwise with on_default."""
        follow = {**self.win.follow(), "on_default": name == self.win.default, **follow}
        rec = recorder.Recording(self.root / "rec", INDEX[name], name, RATE,
                                 open_stream=self.win.open_stream, key=KEY,
                                 follow=follow, computer=computer)
        rec.start()
        self.addCleanup(rec.stop)
        return rec

    def kinds(self, rec):
        return [e["kind"] for e in rec.journal["events"]]

    def check_saved(self, rec, journal, before, after=None):
        """D1: the saved track is the first stream's first `before` samples,
        then (when `after` is given) silence for the gap and the next
        stream's first `after` samples: nothing lost, nothing twice. Returns
        the silence's length in frames."""
        notes = []
        audio = recorder.open_recording(rec.folder, journal, KEY, notes)
        pcm = np.frombuffer(b"".join(audio.blocks()), dtype="<i2")
        self.assertEqual([], notes)
        self.assertEqual(journal["frames"], len(pcm))
        np.testing.assert_array_equal(pcm[:before], tone(before))
        if after is None:
            self.assertEqual(len(pcm), before)
            return 0
        np.testing.assert_array_equal(pcm[len(pcm) - after:], tone(after))
        gap = pcm[before:len(pcm) - after]
        self.assertFalse(np.any(gap), "the gap is not silence")
        return len(gap)

    def assert_no_names(self, rec):
        for event in rec.journal["events"]:
            for name in (LAPTOP, HEADSET, USB):
                self.assertNotIn(name, event["note"], "an event names a device (P3)")


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class FollowTests(_Recording):
    def test_a_new_windows_default_is_followed_and_the_gap_is_saved_as_silence(self):
        rec = self.start()
        first = self.win.stream
        first.feed(2)
        self.assertTrue(wait_for(lambda: rec.journal["frames"] + rec._buffered == first.fed))
        self.win.default = HEADSET                   # a headset connects and becomes the default
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 2), "it did not switch")
        second = self.win.stream
        self.assertEqual(second.device, INDEX[HEADSET])
        self.assertTrue(second.started and first.stopped and first.closed)
        time.sleep(0.3)                               # the switch's gap, then the headset's audio
        second.feed(2)
        first.feed(1)                                 # the old stream is no longer recorded
        status = rec.status()
        self.assertEqual((status["device"], status["switched"], status["following"]),
                         (HEADSET, True, True))
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual((journal["device"], journal["device_name"]), (INDEX[HEADSET], HEADSET))
        self.assertEqual(self.kinds(rec), ["mic_switched", "gap"])
        gap = journal["events"][1]
        fill = self.check_saved(rec, journal, 2 * RATE, 2 * RATE)
        self.assertGreater(fill, 0.2 * RATE)
        self.assertLess(fill, 5 * RATE)
        self.assertAlmostEqual(gap["seconds"], fill / RATE, places=2)
        self.assertEqual(gap["at_s"], 2.0)            # where the silence starts
        self.assertIn("switched", gap["note"])
        self.assert_no_names(rec)

    def test_a_default_that_flickers_for_one_check_is_not_followed(self):
        # Windows may change its default more than once while a headset
        # connects: only a default seen at two checks in a row is followed.
        seen = []
        rec = self.start(watch=lambda: Flicker(self.win.watch(), seen))
        self.assertTrue(wait_for(lambda: len(seen) >= 5))
        self.assertEqual(len(self.win.streams), 1, "a one-check flicker was followed")
        self.assertEqual(self.kinds(rec), [])

    def test_the_microphone_already_recording_becoming_the_default_is_no_switch(self):
        # A following recording (on_default: PortAudio's default at Start,
        # while Windows' own read names the laptop, as in N1 below) whose
        # microphone then becomes Windows' default: nothing to switch to.
        # (B1 of the review of 325a564: without on_default it followed
        # nothing under DEC-080 and passed whatever _follow_default did.)
        rec = self.start(HEADSET, on_default=True)
        self.assertTrue(rec.status()["following"])
        self.assertTrue(wait_for(lambda: rec._baseline is not None))   # Windows' first read
        self.win.default = HEADSET
        self.assertTrue(wait_for(lambda: rec._baseline == "{0.0.1}." + HEADSET),
                        "the new default was never taken as the starting point")
        time.sleep(0.3)
        self.assertEqual(len(self.win.streams), 1)
        self.assertEqual(self.kinds(rec), [])

    def test_a_lost_microphone_moves_to_the_one_windows_now_uses(self):
        self.win.default = HEADSET
        rec = self.start(HEADSET)
        first = self.win.stream
        first.feed(1.5)
        self.assertTrue(wait_for(lambda: rec.journal["frames"] + rec._buffered == first.fed))
        self.win.present.discard(HEADSET)             # the headset's battery runs out
        self.win.default = LAPTOP
        overflow = type("S", (), {"input_overflow": True})()
        first.callback(np.zeros((1600, 1), np.int16), 1600, None, overflow)
        first.finished()                              # the driver ends the stream
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 2), "it did not move")
        second = self.win.stream
        self.assertEqual(second.device, INDEX[LAPTOP])
        time.sleep(0.2)
        second.feed(1.5)
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual(journal["device_name"], LAPTOP)
        # The overrun as it disconnected is part of the move, not its own event.
        self.assertEqual(self.kinds(rec), ["mic_moved", "gap"])
        self.assertIn("fraction of a second", journal["events"][0]["note"])
        # The block fed with the overrun is silence too, so the check holds.
        self.check_saved(rec, journal, int(1.5 * RATE), int(1.5 * RATE))
        self.assert_no_names(rec)

    def test_a_lost_microphone_with_none_to_take_stops_the_recording(self):
        with mock.patch.object(recorder, "SWITCH_WAIT_S", 0.5):
            rec = self.start()
            first = self.win.stream
            first.feed(1)
            self.win.present.clear()
            self.win.default = None
            started = time.monotonic()
            first.finished()
            self.assertTrue(wait_for(lambda: rec.stopped), "it never stopped")
            self.assertGreaterEqual(time.monotonic() - started, 0.45)
        journal = rec.stop()
        self.assertEqual(journal["state"], "device_lost")
        self.assertEqual(self.kinds(rec), ["device_lost"])
        self.assertIn("no other microphone", journal["events"][0]["note"])
        self.check_saved(rec, journal, RATE)

    def test_a_microphone_that_comes_back_in_time_carries_on(self):
        with mock.patch.object(recorder, "SWITCH_WAIT_S", 5.0):
            rec = self.start()
            first = self.win.stream
            first.feed(1)
            self.win.present.clear()
            self.win.default = None
            first.finished()
            time.sleep(0.4)
            self.assertFalse(rec.stopped)
            self.win.present.add(LAPTOP)              # plugged back in
            self.win.default = LAPTOP
            self.assertTrue(wait_for(lambda: len(self.win.streams) == 2))
            self.win.stream.feed(1)
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual(self.kinds(rec), ["mic_moved", "gap"])
        self.assertGreater(self.check_saved(rec, journal, RATE, RATE), 0.3 * RATE)

    def test_a_microphone_that_opens_but_stays_silent_cannot_hold_the_recording_open(self):
        with mock.patch.object(recorder, "SWITCH_WAIT_S", 1.5):
            rec = self.start()
            first = self.win.stream
            first.feed(1)
            self.win.present.discard(LAPTOP)
            self.win.default = USB                    # opens, but never gives a block
            first.finished()
            self.assertTrue(wait_for(lambda: rec.stopped, 15), "a silent microphone kept it open")
        self.assertEqual(rec.stop()["state"], "device_lost")

    def test_choosing_a_microphone_switches_and_stops_following_the_default(self):
        rec = self.start()
        self.win.stream.feed(1)
        rec.choose(HEADSET)
        self.assertEqual(self.win.stream.device, INDEX[HEADSET])
        self.assertFalse(rec.status()["following"])
        self.win.stream.feed(1)
        self.win.default = USB                        # no longer followed
        time.sleep(0.5)
        self.assertEqual(len(self.win.streams), 2)
        rec.choose(HEADSET)                           # the one recording: nothing happens
        self.assertEqual(len(self.win.streams), 2)
        journal = rec.stop()
        self.assertEqual(self.kinds(rec)[0], "mic_chosen")
        self.assertEqual(journal["device_name"], HEADSET)
        self.assert_no_names(rec)

    def test_a_choice_made_while_a_new_default_is_checked_wins(self):
        # B1 of the review of 69063a4: the follow thread had read `following`
        # and was checking a new default when the person chose on the card.
        held = HeldSecondSighting()
        rec = self.start(watch=lambda: held.wrap(self.win.watch()))
        self.addCleanup(held.go.set)
        self.win.stream.feed(1)
        self.win.default = HEADSET
        self.assertTrue(held.holding.wait(10), "the second check never came")
        rec.choose(USB)
        held.go.set()
        time.sleep(0.5)                               # the held check finishes
        self.assertEqual(self.win.stream.device, INDEX[USB])
        self.assertEqual(len(self.win.streams), 2)
        self.assertFalse(rec.status()["following"])
        journal = rec.stop()
        self.assertEqual(journal["device_name"], USB)
        self.assertNotIn("mic_switched", self.kinds(rec))

    def test_choosing_the_microphone_recording_during_a_follow_switch_wins(self):
        # N6 of the review of 46753be: the follow switch to a new default had
        # passed its check and was opening the headset when the person chose
        # the microphone still recording. The choice must still win.
        inside, go = threading.Event(), threading.Event()
        self.addCleanup(go.set)

        def resolve(name, default=False):
            if name == HEADSET and not inside.is_set():
                inside.set()                          # the follow switch, under its lock
                go.wait(10)
            return self.win.resolve(name, default=default)

        rec = self.start(resolve=resolve)
        self.win.stream.feed(1)
        self.win.default = HEADSET
        self.assertTrue(inside.wait(10), "the follow switch never started")
        chooser = threading.Thread(target=rec.choose, args=(LAPTOP,))
        chooser.start()
        chooser.join(0.5)                             # it waits for the switch under way
        go.set()
        chooser.join(10)
        self.assertFalse(chooser.is_alive())
        self.assertTrue(wait_for(lambda: "mic_switched" in self.kinds(rec)))
        status = rec.status()
        self.assertEqual(status["device"], LAPTOP, "the follow switch won over the choice")
        self.assertFalse(status["following"])
        journal = rec.stop()
        self.assertEqual(journal["device_name"], LAPTOP)
        self.assertEqual([k for k in self.kinds(rec) if k != "gap"],
                         ["mic_switched", "mic_chosen"])

    def test_a_first_read_that_fails_does_not_count_the_default_as_new(self):
        # N1 of the review of 69063a4: the first good read is the starting
        # point, so a recording on another microphone is not moved to it.
        # (A recording PortAudio started on as the default, while Windows'
        # own first good read names another: it follows, DEC-080.)
        self.start(USB, on_default=True, watch=lambda: FirstReadFails(self.win.watch()))
        self.win.stream.feed(1)
        time.sleep(0.5)                               # several good reads of the laptop
        self.assertEqual(len(self.win.streams), 1)    # still the USB microphone
        self.win.default = HEADSET                    # a real change is still followed
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 2), "it did not switch")
        self.assertEqual(self.win.stream.device, INDEX[HEADSET])

    def test_choosing_after_the_recording_stopped_says_so(self):
        # N3 of the review of 69063a4.
        rec = self.start()
        self.win.stream.feed(1)
        rec.stop()
        with self.assertRaises(recorder.RecorderError) as caught:
            rec.choose(HEADSET)
        self.assertEqual(str(caught.exception), "The recording has stopped.")
        self.assertEqual(len(self.win.streams), 1)

    def test_a_chosen_microphone_that_will_not_open_leaves_the_same_one_recording(self):
        rec = self.start()
        self.win.stream.feed(1)
        self.win.present.discard(USB)                 # gone between the list and the choice
        with self.assertRaises(recorder.RecorderError) as caught:
            rec.choose(USB)
        self.assertIn("carries on from the same microphone", str(caught.exception))
        self.assertNotIn(USB, str(caught.exception))
        self.assertEqual(self.win.stream.device, INDEX[LAPTOP])
        time.sleep(0.2)
        self.win.stream.feed(1)
        journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual(journal["device_name"], LAPTOP)
        self.assertEqual(self.kinds(rec), ["mic_switch_failed", "gap"])
        self.check_saved(rec, journal, RATE, RATE)

    def test_policy_is_read_before_every_switch(self):
        rec = self.start()
        self.win.allowed = False                      # K3: managed policy now says no
        self.win.default = HEADSET
        time.sleep(0.5)
        self.assertEqual(len(self.win.streams), 1)
        self.assertEqual(self.kinds(rec), [])
        self.win.allowed = True
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 2))

    def test_a_policy_that_cannot_be_read_allows_no_switch(self):
        def unreadable():
            raise OSError("the policy key cannot be read")
        self.start(allowed=unreadable)
        self.win.default = HEADSET
        time.sleep(0.5)
        self.assertEqual(len(self.win.streams), 1)

    def test_a_pause_stays_paused_across_a_switch_and_paused_time_is_not_filled(self):
        rec = self.start()
        first = self.win.stream
        first.feed(1)
        rec.pause()
        self.win.default = HEADSET
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 2))
        second = self.win.stream
        time.sleep(0.3)
        second.feed(1)                                # paused: not recorded
        self.assertEqual(rec.status()["state"], "paused")       # D2
        time.sleep(0.3)
        rec.resume()
        second.feed(1)
        journal = rec.stop()
        self.assertEqual(journal["frames"], 2 * RATE)
        self.assertEqual(self.kinds(rec), ["paused", "mic_switched", "resumed"])
        notes = []
        audio = recorder.open_recording(rec.folder, journal, KEY, notes)
        pcm = np.frombuffer(b"".join(audio.blocks()), dtype="<i2")
        np.testing.assert_array_equal(pcm[:RATE], tone(RATE))
        np.testing.assert_array_equal(pcm[RATE:], tone(RATE, RATE))   # its second second

    def test_the_watcher_is_made_and_closed_on_the_follow_thread(self):
        rec = self.start()
        self.assertTrue(wait_for(lambda: self.win.watch_threads))
        rec.stop()
        self.assertEqual(self.win.watch_threads, self.win.close_threads)
        self.assertNotEqual(self.win.watch_threads[0], threading.get_ident())
        self.assertFalse(rec._follower.is_alive())

    def test_a_watcher_that_fails_still_lets_a_lost_microphone_stop(self):
        def broken():
            raise OSError("Windows audio is not available.")
        with mock.patch.object(recorder, "SWITCH_WAIT_S", 0.3):
            rec = self.start(watch=broken)
            self.win.stream.feed(1)
            self.win.stream.finished()
            self.assertTrue(wait_for(lambda: rec.stopped))
        self.assertEqual(rec.stop()["state"], "device_lost")

    def test_without_follow_a_lost_microphone_still_stops_at_once(self):
        rec = recorder.Recording(self.root / "rec", INDEX[LAPTOP], LAPTOP, RATE,
                                 open_stream=self.win.open_stream, key=KEY)
        rec.start()
        self.addCleanup(rec.stop)
        self.win.stream.feed(1)
        self.win.stream.finished()
        self.assertTrue(rec.stopped)
        self.assertIn("did not switch", rec.stop()["events"][-1]["note"])

    def test_live_drafts_follow_the_new_microphone(self):
        rec = self.start()
        taken = []
        rec.tap = lambda start, block: taken.append((start, None if block is None
                                                     else len(block)))
        self.win.stream.feed(1)
        self.assertTrue(wait_for(lambda: sum(n or 0 for _, n in taken) == RATE))
        self.win.default = HEADSET
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 2))
        time.sleep(0.2)
        self.win.stream.feed(1)
        journal = rec.stop()
        fill = journal["frames"] - 2 * RATE
        # The draft's phrase ends at the switch, then carries on with the new
        # microphone after the silence (a jump in frames starts a new phrase).
        ends = [i for i, (_, n) in enumerate(taken) if n is None]
        self.assertEqual(len(ends), 1)
        after = taken[ends[0] + 1:]
        self.assertEqual(after[0][0], RATE + fill)
        self.assertEqual(sum(n for _, n in after), RATE)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class StartedOnTests(_Recording):
    """DEC-080 (the owner's refinement of 28 Oct 2026): only a recording that
    started on the Windows default follows a new default. One started on a
    microphone the person chose stays on it; if that one is lost, it moves to
    the default and follows it from then on. A choice on the card still stops
    following for the rest of the recording."""

    def test_a_recording_started_on_a_chosen_microphone_stays_on_it(self):
        rec = self.start(USB)                         # chosen before Record (the laptop is default)
        self.assertFalse(rec.status()["following"])
        self.win.stream.feed(1)
        self.win.default = HEADSET                    # a headset connects and becomes the default
        time.sleep(0.5)                               # many checks
        self.assertEqual(len(self.win.streams), 1, "it left the microphone chosen")
        self.assertEqual(self.kinds(rec), [])
        journal = rec.stop()
        self.assertEqual(journal["device_name"], USB)
        self.check_saved(rec, journal, RATE)

    def test_a_chosen_microphone_that_is_lost_moves_to_the_default_and_follows_it(self):
        rec = self.start(USB)
        first = self.win.stream
        first.feed(1)
        self.assertTrue(wait_for(lambda: rec.journal["frames"] + rec._buffered == first.fed))
        self.win.present.discard(USB)                 # unplugged
        first.finished()
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 2), "it did not move")
        self.assertEqual(self.win.stream.device, INDEX[LAPTOP])      # the Windows default
        self.assertTrue(rec.status()["following"])
        time.sleep(0.2)
        self.win.stream.feed(1)
        self.win.default = HEADSET                    # followed from now on
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 3),
                        "the new default was not followed after the move")
        self.assertEqual(self.win.stream.device, INDEX[HEADSET])
        journal = rec.stop()
        self.assertEqual(journal["device_name"], HEADSET)
        self.assertEqual([k for k in self.kinds(rec) if k != "gap"],
                         ["mic_moved", "mic_switched"])
        self.assert_no_names(rec)

    def test_a_card_choice_that_is_lost_moves_but_follows_no_default(self):
        # DEC-079: a choice on the card stops following for the rest of the
        # recording, as before; a loss still moves it to the default.
        rec = self.start()
        self.win.stream.feed(1)
        rec.choose(USB)
        self.win.present.discard(USB)
        self.win.stream.finished()
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 3), "it did not move")
        self.assertEqual(self.win.stream.device, INDEX[LAPTOP])
        self.assertFalse(rec.status()["following"])
        self.win.default = HEADSET
        time.sleep(0.5)
        self.assertEqual(len(self.win.streams), 3)
        self.assertEqual(rec.stop()["device_name"], LAPTOP)

    def test_choosing_the_lost_microphone_while_it_is_lost_opens_it_again(self):
        # N8 of the review of e836d54: the microphone recording, chosen while
        # it is lost, was reported done with no microphone recording.
        with mock.patch.object(recorder, "SWITCH_WAIT_S", 5.0):
            rec = self.start()
            first = self.win.stream
            first.feed(1)
            self.win.default = None                   # Windows offers no other for now
            first.finished()                          # the driver ended it; it is still listed
            self.assertTrue(wait_for(lambda: first.closed), "the loss was not handled")
            rec.choose(LAPTOP)
            self.assertEqual(len(self.win.streams), 2, "reported done without opening it")
            second = self.win.stream
            self.assertEqual(second.device, INDEX[LAPTOP])
            self.assertTrue(second.started)
            self.assertIsNone(rec._lost_at)
            time.sleep(0.2)
            second.feed(1)
            journal = rec.stop()
        self.assertEqual(journal["state"], "stopped")
        self.assertEqual([k for k in self.kinds(rec) if k != "gap"], ["mic_chosen"])
        self.check_saved(rec, journal, RATE, RATE)

    def test_choosing_the_lost_microphone_that_will_not_open_says_so(self):
        with mock.patch.object(recorder, "SWITCH_WAIT_S", 5.0):
            rec = self.start()
            first = self.win.stream
            first.feed(1)
            self.win.default = None
            self.win.present.discard(LAPTOP)          # gone, though the card still listed it
            first.finished()
            self.assertTrue(wait_for(lambda: first.closed), "the loss was not handled")
            with self.assertRaises(recorder.RecorderError) as caught:
                rec.choose(LAPTOP)
            self.assertIn("waiting for a microphone", str(caught.exception))
            self.assertNotIn(LAPTOP, str(caught.exception))
            self.assertEqual(len(self.win.streams), 1)
            self.assertFalse(rec.stopped)
            self.assertIsNotNone(rec._lost_at)

    def test_a_choice_that_opens_a_microphone_during_a_move_is_not_undone(self):
        # Recorder thread safety: the follow thread had seen the loss and was
        # reading Windows' default when the person chose on the card. The
        # choice opened a microphone, so the move must neither close it nor
        # go elsewhere.
        held = []

        def watch():
            held.append(HeldLossRead(self.win.watch()))
            return held[0]
        with mock.patch.object(recorder, "SWITCH_WAIT_S", 5.0):
            rec = self.start(USB, watch=watch)        # follows no default: reads only for a loss
            self.assertTrue(wait_for(lambda: held and held[0].reads == 1))
            self.addCleanup(held[0].go.set)
            first = self.win.stream
            first.feed(1)
            self.win.present.discard(USB)
            first.finished()
            self.assertTrue(held[0].holding.wait(10), "the follow thread never read the default")
            rec.choose(HEADSET)
            held[0].go.set()
            time.sleep(0.5)                           # the held move finishes
            self.assertEqual(self.win.stream.device, INDEX[HEADSET])
            self.assertFalse(self.win.stream.closed, "the move closed the microphone chosen")
            self.assertEqual(len(self.win.streams), 2)
            self.assertNotIn("mic_moved", self.kinds(rec))
            self.assertFalse(rec.status()["following"])
            self.assertEqual(rec.stop()["device_name"], HEADSET)

    def test_a_choice_made_after_the_move_is_checked_is_not_undone(self):
        # Nit 1 of the review of 325a564: the follow thread had passed
        # _recover's checks and let go of the switch lock, and was about to
        # move to the default, when the person chose on the card. _switch's
        # own check (a move with nothing lost any more) must stop the move.
        inside, go = threading.Event(), threading.Event()
        self.addCleanup(go.set)
        with mock.patch.object(recorder, "SWITCH_WAIT_S", 5.0):
            rec = self.start(USB)                     # follows no default
            switch = rec._switch

            def held_switch(name, kind, default=False):
                if kind == "mic_moved" and not inside.is_set():
                    inside.set()                      # between _recover and _switch's lock
                    go.wait(10)
                return switch(name, kind, default=default)
            rec._switch = held_switch
            first = self.win.stream
            first.feed(1)
            self.win.present.discard(USB)
            first.finished()
            self.assertTrue(inside.wait(10), "the move never started")
            self.assertTrue(first.closed)             # _recover closed the lost stream
            rec.choose(HEADSET)
            go.set()
            time.sleep(0.5)                           # the held move finishes
            self.assertEqual(self.win.stream.device, INDEX[HEADSET])
            self.assertFalse(self.win.stream.closed, "the move closed the microphone chosen")
            self.assertEqual(len(self.win.streams), 2)
            self.assertNotIn("mic_moved", self.kinds(rec))
            self.assertFalse(rec.status()["following"])
            self.assertEqual(rec.stop()["device_name"], HEADSET)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class FollowMixTests(_Recording):
    """The mix (#108) lines a switch up as after a resume: the microphone's
    marks start a segment at the new microphone's first block."""

    def test_a_switch_starts_a_new_segment_and_the_mix_needs_no_fallback(self):
        holder = {}

        def open_loopback(*args):
            holder["comp"] = FakeLoopback(*args)
            return holder["comp"]
        rec = self.start(computer={"open": open_loopback, "source": {"kind": "all"},
                                   "rate": CRATE})
        for _ in range(10):
            self.win.stream.feed(0.1)
            holder["comp"].feed(0.1)
            time.sleep(0.1)
        self.win.default = HEADSET
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 2))
        for _ in range(10):
            self.win.stream.feed(0.1)
            holder["comp"].feed(0.1)
            time.sleep(0.1)
        journal = rec.stop()
        starts = [m[0] for m in journal["marks"] if m[2] == 1]
        fill = journal["frames"] - 2 * RATE
        self.assertEqual(starts, [0, RATE + fill])
        lines, fallback = mixdown.segments(journal["marks"], journal["frames"], RATE)
        self.assertEqual((len(lines), fallback), (2, False))
        notes = []
        audio = recorder.open_recording(rec.folder, journal, KEY, notes)
        pcm = np.frombuffer(b"".join(audio.blocks()), dtype="<i2")
        self.assertEqual(len(pcm), journal["frames"])
        self.assertNotIn("mix_drift", [n["kind"] for n in notes])
        # The "You" suggestion's levels come from the one microphone track,
        # both microphones together (#458).
        self.assertIsNotNone(audio.activity())


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
@unittest.skipUnless(os.name == "nt", "imports use Windows path rules (the app is Windows-only)")
class WorkspaceSwitchTests(unittest.TestCase):
    """The card's choice through the app and the bridge (#462)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.win = FakeWindows()

        def devices():                       # what PortAudio lists, from the fake Windows
            return [{"index": INDEX[n], "name": n, "label": n, "default": n == self.win.default,
                     "rate": RATE} for n in (LAPTOP, HEADSET, USB) if n in self.win.present]
        patches = [mock.patch.object(recorder, "list_devices", devices)]
        patches += [mock.patch.object(recorder, k, v) for k, v in FAST.items()]
        from workspace import power

        def never(*args, **kwargs):
            raise AssertionError("a test reached the real power scheme")
        patches += [mock.patch.object(power._PowrProf, n, never) for n in ("write", "apply")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.ws = Workspace(self.root / "ws", self.root / "models", start=False,
                            allow_test_engines=True, battery=False,
                            protector=keyprotect.TestProtector())
        self.addCleanup(self.ws.close)
        self.ws.settings.update(model="test:fake_worker_engines:sentences", speakers=False,
                                microphone=True)
        self.ws.open_stream = self.win.open_stream
        self.ws.watch_microphones = self.win.watch
        self.ws.list_microphones = lambda: [
            {"id": "{0.0.1}." + n, "name": n, "default": n == self.win.default}
            for n in (LAPTOP, HEADSET, USB) if n in self.win.present]
        self.ws.start()

    def started(self):
        self.ws.start_recording(INDEX[LAPTOP], LAPTOP, consent=CONSENT)
        self.addCleanup(lambda: self.ws._recording and self.ws.discard_recording())
        return self.ws._recording

    def test_while_recording_the_list_is_what_windows_has_with_the_one_recording(self):
        with self.assertRaisesRegex(WorkspaceError, "Nothing is being recorded"):
            self.ws.switch_microphone(0, LAPTOP)
        self.started()
        listed = self.ws.microphone_devices()
        self.assertEqual([(d["index"], d["name"], d["default"], d["current"]) for d in listed],
                         [(0, LAPTOP, True, True), (1, HEADSET, False, False),
                          (2, USB, False, False)])

    def test_the_card_switches_the_microphone_and_the_app_follows_no_default(self):
        rec = self.started()
        self.ws.microphone_devices()
        status = self.ws.switch_microphone(1, HEADSET)
        self.assertEqual((status["device"], status["switched"], status["following"]),
                         (HEADSET, True, False))
        self.assertEqual(self.win.stream.device, INDEX[HEADSET])
        self.assertEqual([d["current"] for d in self.ws.microphone_devices()],
                         [False, True, False])
        self.assertIs(self.ws._recording, rec)        # the same recording

    def test_a_changed_or_gone_entry_and_policy_are_refused(self):
        self.started()
        self.ws.microphone_devices()
        with self.assertRaisesRegex(WorkspaceError, "has changed"):
            self.ws.switch_microphone(1, USB)          # not the name at that place
        with self.assertRaisesRegex(WorkspaceError, "has changed"):
            self.ws.switch_microphone(9, USB)
        self.win.present.discard(USB)
        with self.assertRaisesRegex(WorkspaceError, "no longer connected"):
            self.ws.switch_microphone(2, USB)
        with mock.patch.object(self.ws, "_allowed",
                               lambda name: name != "AllowMicrophoneRecording"):
            with self.assertRaisesRegex(WorkspaceError, "organisation"):
                self.ws.switch_microphone(1, HEADSET)
        self.assertEqual(self.win.stream.device, INDEX[LAPTOP])
        self.assertEqual(len(self.win.streams), 1)

    def test_a_recording_started_on_a_chosen_microphone_follows_no_default(self):
        # DEC-080: the app tells the recorder whether it started on the default.
        self.ws.start_recording(INDEX[USB], USB, consent=CONSENT)
        self.addCleanup(lambda: self.ws._recording and self.ws.discard_recording())
        self.assertFalse(self.ws.recording_status()["recording"]["following"])
        self.win.default = HEADSET
        time.sleep(0.5)
        self.assertEqual(len(self.win.streams), 1)
        self.assertEqual(self.ws.recording_status()["recording"]["device"], USB)

    def test_the_recording_follows_the_windows_default_through_the_app(self):
        rec = self.started()
        self.assertTrue(self.ws.recording_status()["recording"]["following"])
        self.win.default = HEADSET
        self.assertTrue(wait_for(lambda: len(self.win.streams) == 2))
        self.assertEqual(self.ws.recording_status()["recording"]["device"], HEADSET)
        self.assertIs(self.ws._recording, rec)

    def test_the_bridge_checks_the_arguments(self):
        self.started()
        self.ws.microphone_devices()
        bridge = api.make_api(self.ws)
        self.assertFalse(bridge.switch_microphone("1", HEADSET)["ok"])
        self.assertFalse(bridge.switch_microphone(1, 5)["ok"])
        self.assertFalse(bridge.switch_microphone(1, "x" * 121)["ok"])
        self.assertFalse(bridge.switch_microphone(-1, HEADSET)["ok"])
        self.assertEqual(len(self.win.streams), 1)
        answer = bridge.switch_microphone(1, HEADSET)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["value"]["device"], HEADSET)
        self.assertIn("switch_microphone", api.API_METHODS)
        self.assertNotIn("switch_microphone", api.LOCKED_METHODS)


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class FindInputTests(unittest.TestCase):
    """Windows' name for a microphone to PortAudio's entry (#462)."""

    def test_full_names_and_names_cut_at_31_characters_are_found(self):
        long_name = "Headset Microphone (Jabra Evolve2 85)"
        devices = [{"index": 1, "name": "Laptop microphone", "label": "Laptop microphone",
                    "default": True, "rate": RATE},
                   {"index": 2, "name": long_name[:31], "label": long_name, "default": False,
                    "rate": RATE}]
        with mock.patch.object(recorder, "list_devices", lambda: devices):
            self.assertEqual(recorder.find_input(long_name)["index"], 2)
            self.assertEqual(recorder.find_input("Laptop microphone")["index"], 1)
            self.assertIsNone(recorder.find_input("USB microphone"))
            self.assertEqual(recorder.find_input("USB microphone", default=True)["index"], 1)

    def test_two_that_could_match_are_never_guessed(self):
        twins = [{"index": i, "name": "Headset Microphone (Jabra Evolv", "label":
                  "Headset Microphone (Jabra Evolv", "default": False, "rate": RATE}
                 for i in (1, 2)]
        with mock.patch.object(recorder, "list_devices", lambda: twins):
            self.assertIsNone(recorder.find_input("Headset Microphone (Jabra Evolve 65)"))
        twins[1]["default"] = True
        with mock.patch.object(recorder, "list_devices", lambda: twins):
            self.assertEqual(
                recorder.find_input("Headset Microphone (Jabra Evolve 65)")["index"], 2)

    def test_a_broken_audio_library_finds_nothing(self):
        def broken():
            raise recorder.RecorderError("no audio library")
        with mock.patch.object(recorder, "list_devices", broken):
            self.assertIsNone(recorder.find_input("Laptop microphone", default=True))


if __name__ == "__main__":
    unittest.main()
