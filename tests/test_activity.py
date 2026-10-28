# The track-activity file and the "You" suggestion of a two-track recording
# (#108 slice 5): synthetic signals only (noise shaped like turns of speech;
# no device is opened, nothing is played, no real audio).
#   - workspace/activity.py: levels in 10 ms steps, the echo test (laptop
#     speakers: the microphone hears the other side later and quieter, which
#     must never count as "you"), the suggestion, and the sealed file;
#   - workspace/mixdown.py: the levels are kept while the mix is made;
#   - workspace/app.py: the Name speakers panel offers the suggestion only
#     for a two-track meeting, only for an unnamed speaker, and never names
#     anyone itself; the crash-recovery report gives each track's seconds.
#   python tools/run_tests.py --only test_activity

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
RATE = 16000
CRATE = 48000
KEY = bytes(range(32))
MID = "0123456789abcdef0123456789abcdef"

if HAVE_NUMPY:
    import numpy as np

    from workspace import activity, mixdown, recorder


def talk(seconds, rate, turns, seed, amp=6000.0):
    """Noise shaped like speech (syllables at about 4 a second, with the
    short gaps between them) in the given turns [(start_s, end_s)], silence
    elsewhere. int16-scale floats."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    out = np.zeros(n, dtype=np.float64)
    t = np.arange(n) / rate
    envelope = np.clip(0.25 + np.sin(2 * np.pi * 4.1 * t + seed), 0.0, 1.0)
    for a, b in turns:
        i, j = int(a * rate), int(b * rate)
        out[i:j] = rng.standard_normal(j - i) * amp * envelope[i:j]
    return out


def delayed(x, seconds, rate, gain):
    k = int(seconds * rate)
    out = np.zeros_like(x)
    out[k:] = x[:len(x) - k] * gain
    return out


# A 120 s call: "you" (A) on the microphone, the other side (B) on the
# computer, taking turns, with a little talking over each other.
SECONDS = 120
YOU = [(0, 10), (30, 40), (60, 70), (90, 100)]
OTHERS = [(10, 30), (40, 60), (70, 90), (100, 120)]
OVERLAP = [(45, 47)]               # A says something while B is talking


def tracks(echo_gain=0.25, echo_s=0.12, room=None, others=OTHERS):
    """(microphone, computer) at RATE: the microphone hears A (and the room
    speaker, if any), plus B through the laptop speakers (echo_gain, after
    echo_s), plus a low noise floor."""
    rng = np.random.default_rng(7)
    a = talk(SECONDS, RATE, YOU + OVERLAP, seed=1)
    b = talk(SECONDS, RATE, others, seed=2, amp=5000.0)
    mic = a + delayed(b, echo_s, RATE, echo_gain) + rng.standard_normal(len(a)) * 20.0
    if room is not None:
        mic = mic + talk(SECONDS, RATE, room, seed=3)
    return mic, b


def tracked(mic, comp, rate=RATE, block=None):
    t = activity.Tracker(rate, len(mic))
    step = block or len(mic)
    for a in range(0, len(mic), step):
        t.add(a, mic[a:a + step], comp[a:a + step])
    return t.finish()


def share(flag, spans):
    """Own-voice share of the active steps in these spans."""
    own = act = 0
    for a, b in spans:
        part = flag[int(a / activity.HOP_S):int(b / activity.HOP_S)]
        own += np.count_nonzero(part & activity.OWN)
        act += np.count_nonzero(part & (activity.MIC_ON | activity.COMP_ON))
    return own / act


def without_overlap(spans):
    """Passages without talking over each other, as speaker identification
    marks them (speaker_names.spans leaves overlap out)."""
    out = []
    for a, b in spans:
        cut = [(x, y) for x, y in OVERLAP if a < y and x < b]
        if not cut:
            out.append((a, b))
            continue
        x, y = cut[0]
        out += [p for p in ((a, x), (y, b)) if p[1] > p[0]]
    return out


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class EchoAndSuggestionTests(unittest.TestCase):
    def spans(self, **extra):
        return {"SPEAKER_00": YOU, "SPEAKER_01": without_overlap(OTHERS), **extra}

    def test_on_laptop_speakers_the_echo_never_counts_as_you(self):
        # The regression this guards: the microphone is loud while the other
        # side talks (it hears them through the speakers), so "the microphone
        # was active" alone would count them as you. With the echo test, only
        # the person recording is.
        for delay in (0.03, 0.12):
            with self.subTest(delay=delay):
                mic, comp = tracks(echo_s=delay)
                _, _, flag = tracked(mic, comp)
                self.assertGreater(share(flag, YOU), 0.9)
                self.assertLess(share(flag, without_overlap(OTHERS)), 0.1)
                self.assertEqual(activity.suggest(self.spans(), flag), "SPEAKER_00")

    def test_without_the_echo_test_the_other_side_would_look_like_you(self):
        # The control for the test above (E1): with no echo measured, the
        # other side's turns are mostly "own voice" too, and no one (or the
        # wrong one) would be suggested.
        mic, comp = tracks(echo_s=0.03)
        with mock.patch.object(activity, "MIN_ECHO_STEPS", 10 ** 9):
            _, _, flag = tracked(mic, comp)
        self.assertGreater(share(flag, without_overlap(OTHERS)), 0.8)
        self.assertNotEqual(activity.suggest(self.spans(), flag), "SPEAKER_00")

    def test_talking_over_the_other_side_still_counts_as_you(self):
        mic, comp = tracks()
        _, _, flag = tracked(mic, comp)
        # Each 10 ms step is judged alone, so where the echo peaks over a
        # quiet moment of the voice that step is not counted: most are.
        self.assertGreater(share(flag, OVERLAP), 0.5)

    def test_a_louder_echo_and_a_longer_delay_are_measured_too(self):
        for gain, delay in ((0.5, 0.05), (0.35, 0.25), (0.1, 0.18)):
            with self.subTest(gain=gain, delay=delay):
                mic, comp = tracks(echo_gain=gain, echo_s=delay)
                _, _, flag = tracked(mic, comp)
                self.assertLess(share(flag, without_overlap(OTHERS)), 0.2)
                self.assertEqual(activity.suggest(self.spans(), flag), "SPEAKER_00")

    def test_on_a_headset_every_word_on_the_microphone_is_yours(self):
        mic, comp = tracks(echo_gain=0.0)
        _, _, flag = tracked(mic, comp)
        self.assertGreater(share(flag, YOU), 0.9)
        self.assertLess(share(flag, without_overlap(OTHERS)), 0.05)
        self.assertEqual(activity.suggest(self.spans(), flag), "SPEAKER_00")

    def test_two_people_in_the_room_are_both_on_the_microphone_so_no_one_is_suggested(self):
        # A and C both on the microphone: which is "you" cannot be told, so
        # nothing is suggested rather than a guess.
        room = [(20, 30), (50, 60)]
        others = [(10, 20), (40, 50), (70, 90), (100, 120)]
        mic, comp = tracks(room=room, others=others)
        _, _, flag = tracked(mic, comp)
        spans = {"SPEAKER_00": YOU, "SPEAKER_01": others, "SPEAKER_02": room}
        self.assertGreater(share(flag, room), 0.8)
        self.assertIsNone(activity.suggest(spans, flag))

    def test_too_little_own_voice_suggests_no_one(self):
        mic, comp = tracks()
        _, _, flag = tracked(mic, comp)
        self.assertIsNone(activity.suggest({"SPEAKER_00": [(0, 4)],
                                            "SPEAKER_01": without_overlap(OTHERS)}, flag))
        self.assertIsNone(activity.suggest({}, flag))

    def test_a_silent_computer_track_leaves_the_microphone_as_it_is(self):
        mic, comp = tracks(echo_gain=0.0)
        _, _, flag = tracked(mic, np.zeros_like(comp))
        self.assertGreater(share(flag, YOU), 0.9)
        self.assertFalse(np.any(flag & activity.COMP_ON))

    def test_blocks_of_any_size_give_the_same_steps(self):
        mic, comp = tracks()
        whole = tracked(mic, comp)
        for block in (RATE * 10, 4999, 160, 161):
            with self.subTest(block=block):
                parts = tracked(mic, comp, block=block)
                for x, y in zip(whole, parts, strict=True):
                    self.assertTrue(np.array_equal(x, y))

    def test_steps_follow_the_rate_and_the_last_part_step_is_kept(self):
        for rate in (16000, 44100, 48000):
            with self.subTest(rate=rate):
                n = int(rate * 2.345)
                result = tracked(np.full(n, 1000.0), np.zeros(n), rate=rate)
                self.assertEqual(len(result[0]), 235)
                self.assertEqual(int(result[0][-1]), round(20 * np.log10(1000 / 32768)))
                self.assertEqual(int(result[1][-1]), activity.MIN_DB)

    def test_a_jump_inside_the_open_step_keeps_both_parts_of_it(self):
        # #461: a block that starts after a gap but still inside the step
        # left open averages with what that step already had, as one block
        # would (the mix feeds the time line without gaps, so not reachable
        # from it today).
        t = activity.Tracker(RATE, 2 * RATE // 100)           # two steps of 160 frames
        t.add(0, np.full(80, 1000.0), np.zeros(80))
        t.add(100, np.full(60, 4000.0), np.zeros(60))         # 20 frames skipped, step 0 open
        t.add(160, np.full(160, 1000.0), np.zeros(160))
        mic = t.finish()[0]
        power = (80 * 1000.0 ** 2 + 60 * 4000.0 ** 2) / 140 / 32768.0 ** 2
        self.assertEqual(int(mic[0]), round(10 * np.log10(power)))
        self.assertEqual(int(mic[1]), round(20 * np.log10(1000 / 32768)))

    def test_a_mix_not_read_to_its_end_gives_no_result(self):
        t = activity.Tracker(RATE, RATE * 2)
        t.add(0, np.ones(RATE), np.ones(RATE))
        self.assertIsNone(t.finish())


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class ActivityFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        mic, comp = tracks()
        self.result = tracked(mic, comp)

    def test_round_trip(self):
        activity.write(self.folder, KEY, MID, self.result)
        _, mic, comp, flag = activity.read(self.folder, KEY, MID)
        for x, y in zip(self.result, (mic, comp, flag), strict=True):
            self.assertTrue(np.array_equal(x, y))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [activity.FILE])
        size = (self.folder / activity.FILE).stat().st_size
        self.assertLess(size, 3 * SECONDS * 100 + 200)    # three bytes a step

    def test_nothing_plain_is_on_disk(self):
        activity.write(self.folder, KEY, MID, self.result)
        blob = (self.folder / activity.FILE).read_bytes()
        plain = activity.encode(*self.result)
        for part in (plain[:64], plain[-64:], self.result[2][1000:1064].tobytes()):
            self.assertNotIn(part, blob)

    def test_another_meeting_key_or_a_changed_byte_does_not_open(self):
        activity.write(self.folder, KEY, MID, self.result)
        path = self.folder / activity.FILE
        with self.assertRaises(activity.ActivityError):
            activity.read(self.folder, KEY, "f" * 32)                # another meeting's place
        with self.assertRaises(activity.ActivityError):
            activity.read(self.folder, bytes(32), MID)                # another key
        blob = bytearray(path.read_bytes())
        blob[-20] ^= 1
        path.write_bytes(bytes(blob))
        with self.assertRaises(activity.ActivityError):
            activity.read(self.folder, KEY, MID)
        path.write_bytes(b"not an activity file")
        with self.assertRaises(activity.ActivityError):
            activity.read(self.folder, KEY, MID)
        path.unlink()
        with self.assertRaises(OSError):
            activity.read(self.folder, KEY, MID)

    def test_the_sealed_format_is_pinned(self):
        # A known answer built from the raw primitives: the file's id, then a
        # sealed value (version 1, a nonce, AES-256-GCM) under HKDF-SHA256 of
        # the audio key labelled "activity" with (file id, meeting), with the
        # context ("activity", meeting). Changing it makes saved files unreadable.
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        def part(tag, data):
            return tag + len(data).to_bytes(4, "big") + data
        file_id = bytes(range(100, 132))
        info = (b"unminute/v1/activity\0" + part(b"b", file_id) + part(b"s", MID.encode()))
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(KEY)
        aad = (b"\x01" + b"unminute/v1/activity\0" + part(b"s", b"activity")
               + part(b"s", MID.encode()))
        payload = (bytes([1]) + (3).to_bytes(4, "little") + (10).to_bytes(4, "little")
                   + bytes([0xec, 0xd8, 0x81]) + bytes([0x81, 0xe2, 0x81]) + bytes([3, 6, 0]))
        nonce = bytes(12)
        blob = b"UAC1" + file_id + b"\x01" + nonce + AESGCM(key).encrypt(nonce, payload, aad)
        (self.folder / activity.FILE).write_bytes(blob)
        got_id, mic, comp, flag = activity.read(self.folder, KEY, MID)
        self.assertEqual(got_id, file_id)
        self.assertEqual(mic.tolist(), [-20, -40, -127])
        self.assertEqual(comp.tolist(), [-127, -30, -127])
        self.assertEqual(flag.tolist(), [3, 6, 0])

    def test_a_failed_write_leaves_no_file(self):
        with mock.patch.object(activity.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                activity.write(self.folder, KEY, MID, self.result)
        self.assertEqual(list(self.folder.iterdir()), [])
        (self.folder / (activity.FILE + ".partial")).write_bytes(b"left by a crash")
        activity.write(self.folder, KEY, MID, self.result)          # a stale part is replaced
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [activity.FILE])

    def test_an_oversized_file_is_refused_before_it_is_read_whole(self):
        path = self.folder / activity.FILE
        with open(path, "wb") as f:
            f.write(b"UAC1")
            f.truncate(3 * activity.MAX_STEPS + 4096)
        with self.assertRaises(activity.ActivityError):
            activity.read(self.folder, KEY, MID)


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class MixKeepsActivityTests(unittest.TestCase):
    """The levels are kept while the one mix is made from the encrypted
    chunks, on the mix's time line (the microphone's)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / ("a" * 32)
        self.folder.mkdir()

    def write_track(self, track, samples, rate):
        chunks, step = [], 5 * rate
        for i, a in enumerate(range(0, len(samples), step)):
            name = recorder.chunk_name(track, i)
            part = np.clip(np.round(samples[a:a + step]), -32768, 32767).astype("<i2")
            recorder._write_chunk(self.folder / name, part.tobytes(), KEY,
                                  recorder.chunk_context(self.folder.name, track, i), rate)
            chunks.append({"file": name, "frames": len(part)})
        return chunks

    def test_the_mix_leaves_the_levels_and_the_echo_test_finds_you(self):
        mic, b16 = tracks()
        comp = np.repeat(b16, 3)                           # the other side at 48 kHz
        journal = {"rate": RATE, "format": 2,
                   "marks": [[f, 1000.0 + f / RATE, 1 if f == 0 else 0]
                             for f in range(0, len(mic), 30 * RATE)],
                   "chunks": self.write_track(recorder.MICROPHONE, mic, RATE),
                   "tracks": {"computer": {"rate": CRATE,
                                           "marks": [[g, 1000.0 + g / CRATE, 1 if g == 0 else 0]
                                                     for g in range(0, len(comp), 30 * CRATE)],
                                           "chunks": self.write_track(recorder.COMPUTER, comp,
                                                                      CRATE)}}}
        notes = []
        audio = recorder.open_recording(self.folder, journal, KEY, notes)
        self.assertIsNone(audio.activity())                 # not read yet
        b"".join(audio.blocks())
        result = audio.activity()
        self.assertEqual(notes, [])
        self.assertEqual(len(result[2]), SECONDS * 100)
        self.assertEqual(activity.suggest({"SPEAKER_00": YOU,
                                           "SPEAKER_01": without_overlap(OTHERS)}, result[2]),
                         "SPEAKER_00")

    def test_a_microphone_only_recording_has_no_activity(self):
        mic, _ = tracks()
        journal = {"rate": RATE, "chunks": self.write_track(recorder.MICROPHONE, mic[:RATE * 6],
                                                            RATE)}
        audio = recorder.open_recording(self.folder, journal, KEY, [])
        b"".join(audio.blocks())
        self.assertIsNone(audio.activity())

    def test_a_tracking_failure_never_stops_the_mix(self):
        mic, b16 = tracks()
        tracker = mock.Mock(add=mock.Mock(side_effect=MemoryError))
        mix = mixdown.Mix({"rate": RATE, "tracks": {"computer": {"rate": RATE}}},
                          _Array(mic[:RATE * 3]), _Array(b16[:RATE * 3]), [])
        mix.tracker = tracker
        self.assertEqual(len(b"".join(mix.blocks())), RATE * 3 * 2)
        self.assertIsNone(mix.activity())


    def test_a_tracker_that_cannot_be_made_never_stops_the_mix(self):
        # #461: the tracker is made inside a try, as it is fed.
        mic, b16 = tracks()
        with mock.patch.object(activity, "Tracker", side_effect=MemoryError):
            mix = mixdown.Mix({"rate": RATE, "tracks": {"computer": {"rate": RATE}}},
                              _Array(mic[:RATE * 3]), _Array(b16[:RATE * 3]), [])
        self.assertEqual(len(b"".join(mix.blocks())), RATE * 3 * 2)
        self.assertIsNone(mix.activity())


class _Array:
    def __init__(self, samples):
        self.samples = np.asarray(samples, dtype=np.float32)
        self.frames = len(self.samples)

    def read(self, a, b):
        return self.samples[a:b].copy()


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class WorkspaceSuggestionTests(unittest.TestCase):
    """The Name speakers panel (Workspace.speaker_panel): the suggestion is
    the label only, for a two-track meeting with its file, for a speaker
    still unnamed; nothing is renamed by it."""

    def setUp(self):
        from workspace import media
        from workspace.app import Workspace
        from workspace.asr_engines import Segment
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False, allow_test_engines=True,
                            battery=False, policy_reader=lambda: ({}, ()),
                            protector=keyprotect.TestProtector())
        self.addCleanup(self.ws.close)
        store = self.ws.store
        self.mid = mid = store.create_meeting("Synthetic call")
        sid = store.add_source(mid, original_path=None, original_sha256="a" * 64,
                               original_size=1, original_mtime_ns=0, audio_id="b" * 64,
                               samples=SECONDS * RATE, sample_rate=RATE)
        self.vid = store.ensure_version(mid, sid, engine="fake", model="fake",
                                        model_revision="1", precision="int8", config={},
                                        chunk_plan=[(0, SECONDS * RATE, 0, SECONDS * RATE)])
        store.commit_chunk(self.vid, 0, [Segment(float(a), float(b), f"passage {a}")
                                         for a, b in sorted(YOU + OTHERS)])
        turns = sorted([(a, b, "SPEAKER_00") for a, b in YOU]
                       + [(a, b, "SPEAKER_01") for a, b in OTHERS])
        rid = store.start_speaker_run(self.vid)
        self.assertTrue(store.finish_speaker_run(rid, turns, turns))
        self.folder = media.meeting_folder(self.ws.media, mid)
        self.folder.mkdir(parents=True)
        mic, comp = tracks()
        activity.write(self.folder, store.meeting_key(mid, "audio"), mid, tracked(mic, comp))

    def test_the_speaker_mostly_on_the_microphone_is_suggested_and_nothing_is_named(self):
        panel = self.ws.speaker_panel(self.mid)
        self.assertEqual(panel["you"], "SPEAKER_00")
        self.assertEqual([sp["name"] for sp in panel["speakers"]], ["Speaker 1", "Speaker 2"])
        self.assertFalse(panel["can_undo"])                  # no correction was made

    def test_once_named_or_once_someone_is_you_it_is_not_offered(self):
        self.ws.rename_speaker(self.mid, "SPEAKER_00", "Aroha")
        self.assertIsNone(self.ws.speaker_panel(self.mid)["you"])
        self.ws.undo_speakers(self.mid)
        self.ws.rename_speaker(self.mid, "SPEAKER_01", "you")
        self.assertIsNone(self.ws.speaker_panel(self.mid)["you"])
        self.ws.undo_speakers(self.mid)
        self.ws.rename_speaker(self.mid, "SPEAKER_00", "You")    # what Use 'You' does
        self.assertIsNone(self.ws.speaker_panel(self.mid)["you"])

    def test_no_file_a_damaged_file_or_no_audio_key_offers_nothing(self):
        from workspace.store import KeyMissing
        path = self.folder / activity.FILE
        blob = path.read_bytes()
        path.unlink()
        self.assertIsNone(self.ws.speaker_panel(self.mid)["you"])     # an import, or mic only
        path.write_bytes(blob[:-1] + bytes([blob[-1] ^ 1]))
        self.assertIsNone(self.ws.speaker_panel(self.mid)["you"])
        path.write_bytes(blob)
        self.assertEqual(self.ws.speaker_panel(self.mid)["you"], "SPEAKER_00")
        with mock.patch.object(self.ws.store, "meeting_key", side_effect=KeyMissing()):
            os.utime(path, ns=(1, 1))                        # not from the memory kept
            self.assertIsNone(self.ws.speaker_panel(self.mid)["you"])

    def test_the_flags_are_kept_in_memory_for_a_few_meetings(self):
        self.ws.speaker_panel(self.mid)
        with mock.patch.object(activity, "read", side_effect=AssertionError("read again")):
            self.assertEqual(self.ws.speaker_panel(self.mid)["you"], "SPEAKER_00")
        (self.folder / activity.FILE).unlink()                # audio cleanup removes the folder
        self.assertIsNone(self.ws.speaker_panel(self.mid)["you"])
        self.assertNotIn(self.mid, self.ws._activity_cache)


    def test_flags_kept_in_memory_go_once_the_audio_key_is_destroyed(self):
        # #461: audio cleanup destroys the audio key first; if removing the
        # folder then fails (a locked file), the file is still there with
        # the same size and time, and the flags kept in memory must not
        # bring the suggestion back.
        self.assertEqual(self.ws.speaker_panel(self.mid)["you"], "SPEAKER_00")
        with self.ws.store.tx() as db:
            self.ws.store.remove_audio_keys(db, self.mid)
        self.assertTrue((self.folder / activity.FILE).exists())
        self.assertIsNone(self.ws.speaker_panel(self.mid)["you"])
        self.assertNotIn(self.mid, self.ws._activity_cache)

    def test_the_panel_resolves_the_speakers_once(self):
        # #461: the suggestion uses the panel's own resolve of the speakers.
        from workspace import speaker_names
        with mock.patch.object(speaker_names, "resolve",
                               side_effect=speaker_names.resolve) as resolve:
            self.assertEqual(self.ws.speaker_panel(self.mid)["you"], "SPEAKER_00")
        self.assertEqual(resolve.call_count, 1)


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class WriteFailureTraceTests(unittest.TestCase):
    """#461: a track-activity file not written leaves a content-free
    record in the app log (P3: the stage and an exception type name only),
    so support can tell it from a call where no one qualified."""

    def setUp(self):
        from workspace import diag
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.folder = root / MID
        self.folder.mkdir()
        self.log = diag.Log(root / "logs", "app")
        self.store = mock.Mock(log=self.log)
        self.store.meeting_key.return_value = KEY
        mic, comp = tracks()
        self.result = tracked(mic, comp)

    def records(self):
        import json
        path = self.log.path
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]

    def audio(self, result=None, two_track=True, fails=None):
        def get():
            if fails is not None:
                raise fails
            return result
        return mock.Mock(activity=get, two_track=two_track)

    def test_a_write_that_fails_is_recorded_without_its_message(self):
        from workspace import media
        marker = r"C:\Users\Aroha Marker\private"
        with mock.patch.object(activity, "write", side_effect=PermissionError(marker)):
            media._write_activity(self.store, self.folder, MID, self.audio(self.result))
        records = self.records()
        self.assertEqual([{k: v for k, v in r.items() if k != "t"} for r in records],
                         [{"kind": "activity_unwritten", "stage": "write",
                           "error": "PermissionError"}])
        self.assertNotIn("Marker", self.log.path.read_text(encoding="ascii"))
        self.assertFalse((self.folder / activity.FILE).exists())

    def test_no_audio_key_is_recorded_as_a_write_failure(self):
        from workspace import media
        from workspace.store import KeyMissing
        self.store.meeting_key.side_effect = KeyMissing()
        media._write_activity(self.store, self.folder, MID, self.audio(self.result))
        self.assertEqual([(r["kind"], r["stage"], r["error"]) for r in self.records()],
                         [("activity_unwritten", "write", "KeyMissing")])

    def test_a_two_track_mix_with_no_levels_is_recorded(self):
        from workspace import media
        media._write_activity(self.store, self.folder, MID, self.audio(None))
        media._write_activity(self.store, self.folder, MID,
                              self.audio(fails=MemoryError("x")))
        self.assertEqual([(r["kind"], r["stage"], r.get("error")) for r in self.records()],
                         [("activity_unwritten", "tracking", None),
                          ("activity_unwritten", "tracking", "MemoryError")])

    def test_a_microphone_only_recording_or_a_written_file_records_nothing(self):
        from workspace import media
        media._write_activity(self.store, self.folder, MID, self.audio(None, two_track=False))
        media._write_activity(self.store, self.folder, MID, self.audio(self.result))
        self.assertEqual(self.records(), [])
        self.assertTrue((self.folder / activity.FILE).exists())


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class RecoveredReportTests(unittest.TestCase):
    """The crash-recovery report gives each track's saved seconds (counts
    only, P3)."""

    def test_each_tracks_seconds_from_the_journal(self):
        from workspace.app import _computer_seconds
        two = {"format": 2, "rate": RATE, "frames": 10 * RATE,
               "tracks": {"computer": {"rate": CRATE, "frames": 9 * CRATE + CRATE // 2}}}
        self.assertEqual(_computer_seconds(two), {"computer_seconds": 9.5})
        self.assertEqual(_computer_seconds({"rate": RATE, "frames": RATE}), {})
        self.assertEqual(_computer_seconds({"format": 2, "tracks": {}}), {})
        for bad in ({"rate": 0, "frames": 5}, {"rate": True, "frames": 5},
                    {"rate": CRATE, "frames": -1}, {"rate": CRATE, "frames": "5"}):
            with self.subTest(track=bad):
                self.assertEqual(_computer_seconds({"format": 2, "tracks": {"computer": bad}}),
                                 {"computer_seconds": 0})


if __name__ == "__main__":
    unittest.main()
