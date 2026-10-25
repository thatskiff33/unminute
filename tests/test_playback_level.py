# Playback loudness (#108, the owner's decision 2(b), 25 Oct 2026): the app
# plays a meeting at a standard level for listening. The gain is worked out
# from a sample of the meeting's decoded audio on its first clip, kept in
# memory for that audio file only, and applied to each clip with the cap and
# the full-scale limit. The saved audio never changes. Synthetic audio and
# fake readers only (no recording, nothing played).
#   python tools/run_tests.py

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
SR = 16000

if HAVE_NUMPY:
    import numpy as np

    from workspace import media


def db(x):
    """RMS level of float samples in dB of full scale."""
    return 10 * np.log10(np.mean(np.square(x, dtype=np.float64)) + 1e-20)


def speech_like(seconds, level_db, seed=1):
    """Noise shaped into syllables (4 a second), scaled to an RMS level."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    x = rng.standard_normal(n) * (0.2 + np.clip(np.sin(2 * np.pi * 4 * t), 0, None))
    x *= 10 ** (level_db / 20) / np.sqrt(np.mean(x ** 2))
    return x.astype(np.float32)


class FakeReader:
    """A meeting's decoded audio in memory, counting what is read."""

    def __init__(self, samples, file_id=b"a" * 32, fail_from=None):
        self.samples, self.file_id, self.fail_from = samples, file_id, fail_from
        self.frames_read = 0
        self.closed = False

    def __len__(self):
        return len(self.samples)

    def read(self, start, end):
        if self.fail_from is not None and end > self.fail_from:
            raise media_damage()
        part = self.samples[max(0, int(start)):int(end)].copy()
        self.frames_read += len(part)
        return part

    def close(self):
        self.closed = True


def media_damage():
    from workspace.audiocrypt import AudioCryptError
    return AudioCryptError("The audio file is damaged.")


def gain_db(samples):
    return 20 * np.log10(media.playback_gain(FakeReader(samples)))


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class PlaybackGainTests(unittest.TestCase):
    def test_a_quiet_meeting_is_raised_but_no_more_than_the_cap(self):
        quiet = speech_like(60, -30.0)
        self.assertAlmostEqual(gain_db(quiet), media.PLAYBACK_TARGET_DB + 30.0, delta=0.5)
        very_quiet = speech_like(60, -45.0)
        self.assertAlmostEqual(gain_db(very_quiet), media.PLAYBACK_MAX_GAIN_DB, delta=0.01)

    def test_a_loud_meeting_is_lowered_to_the_standard_level(self):
        loud = speech_like(60, -8.0)
        self.assertAlmostEqual(gain_db(loud), media.PLAYBACK_TARGET_DB + 8.0, delta=0.5)
        out = media.apply_playback_gain(loud, media.playback_gain(FakeReader(loud)))
        self.assertAlmostEqual(db(out), media.PLAYBACK_TARGET_DB, delta=0.5)

    def test_an_already_normal_meeting_is_left_about_as_it_is(self):
        normal = speech_like(60, media.PLAYBACK_TARGET_DB)
        self.assertLess(abs(gain_db(normal)), 0.5)

    def test_long_silences_do_not_make_the_speech_too_loud(self):
        # 10% speech at -30 dB, the rest near silence: the level is the
        # speech's (gated), not the average with the silence.
        speech = speech_like(30, -30.0)
        quiet = (np.random.default_rng(2).standard_normal(270 * SR) * 1e-4).astype(np.float32)
        meeting = np.concatenate([quiet[:135 * SR], speech, quiet[135 * SR:]])
        self.assertAlmostEqual(gain_db(meeting), media.PLAYBACK_TARGET_DB + 30.0, delta=1.5)

    def test_silence_is_played_as_it_is(self):
        self.assertEqual(media.playback_gain(FakeReader(np.zeros(60 * SR, np.float32))), 1.0)
        self.assertEqual(media.playback_gain(FakeReader(np.zeros(0, np.float32))), 1.0)

    def test_no_sample_goes_past_full_scale(self):
        quiet = speech_like(20, -35.0)
        quiet[5 * SR] = 0.9                                       # one loud click
        gain = media.playback_gain(FakeReader(quiet))
        out = media.apply_playback_gain(quiet, gain)
        self.assertEqual(out.dtype, np.float32)
        self.assertLessEqual(float(np.abs(out).max()), 1.0)
        self.assertGreater(float(out[5 * SR]), 0.9)               # limited, not removed
        ordinary = np.abs(quiet * gain) < 0.5                     # below the knee: exact gain
        self.assertTrue(np.allclose(out[ordinary], quiet[ordinary] * gain, atol=1e-6))
        self.assertIs(media.apply_playback_gain(quiet, 1.0), quiet)   # nothing to do

    def test_each_piece_read_is_one_encrypted_block(self):
        # So the sample decrypts about 1 block in PLAYBACK_SAMPLE_EVERY.
        from workspace import audiocrypt
        self.assertEqual(media.PLAYBACK_PIECE, audiocrypt.FRAMES_PER_BLOCK)

    def test_a_long_meeting_is_measured_from_a_sample_of_its_audio(self):
        reader = FakeReader(speech_like(1800, -25.0))             # 30 minutes
        media.playback_gain(reader)
        self.assertLess(reader.frames_read, 0.05 * len(reader))
        self.assertGreater(reader.frames_read, 0)
        short = FakeReader(speech_like(40, -25.0))                # under a minute: all of it
        media.playback_gain(short)
        self.assertEqual(short.frames_read, len(short))


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class ClipLevelTests(unittest.TestCase):
    """Workspace.clip with a fake meeting: the gain is worked out once per
    audio file, applied to every clip, and never shown or kept."""

    def setUp(self):
        from workspace import power
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        def never(*args, **kwargs):
            raise AssertionError("a test reached the real power scheme")
        for name in ("write", "apply"):
            guard = mock.patch.object(power._PowrProf, name, never)
            guard.start()
            self.addCleanup(guard.stop)
        self.ws = Workspace(root / "ws", root / "models", start=False, allow_test_engines=True,
                            battery=False, protector=keyprotect.TestProtector())
        self.addCleanup(self.ws.close)
        self.readers = []
        self.audio = speech_like(120, -32.0)
        self.file_id = b"a" * 32
        self.fail_from = None
        for name, value in (("_meeting", lambda mid, content=False: {"id": mid}),
                            ("_audio_available", lambda m: True),
                            ("_user_audio", self.reader)):
            patch = mock.patch.object(self.ws, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def reader(self, mid):
        r = FakeReader(self.audio, self.file_id, self.fail_from)
        self.readers.append(r)
        return r

    def test_every_clip_gets_the_meetings_gain(self):
        gain = media.playback_gain(FakeReader(self.audio))
        for start in (0.0, 30.0, 90.0):
            out = self.ws.clip("m1", start, start + 10.0)
            raw = self.audio[int(start * SR):int((start + 10) * SR)]
            self.assertAlmostEqual(db(out) - db(raw), 20 * np.log10(gain), delta=0.2)
        self.assertTrue(all(r.closed for r in self.readers))

    def test_the_gain_is_worked_out_once_per_audio_file(self):
        with mock.patch.object(media, "playback_gain", wraps=media.playback_gain) as measured:
            self.ws.clip("m1", 0.0, 5.0)
            self.ws.clip("m1", 5.0, 10.0)
            self.assertEqual(measured.call_count, 1)
            self.file_id = b"b" * 32                              # the meeting's audio changed
            self.audio = speech_like(120, -12.0)
            out = self.ws.clip("m1", 0.0, 5.0)
            self.assertEqual(measured.call_count, 2)
        self.assertAlmostEqual(db(out), media.PLAYBACK_TARGET_DB, delta=1.0)   # not stale

    def test_audio_that_cannot_be_measured_plays_as_it_is(self):
        # A damaged block where the sample is read, but not in this clip:
        # the clip still plays, unchanged; nothing about it is said.
        self.fail_from = 60 * SR
        out = self.ws.clip("m1", 0.0, 5.0)
        self.assertTrue(np.array_equal(out, self.audio[:5 * SR]))

    def test_the_saved_audio_is_never_changed(self):
        before = self.audio.copy()
        self.ws.clip("m1", 0.0, 10.0)
        self.assertTrue(np.array_equal(self.audio, before))


if __name__ == "__main__":
    unittest.main()
