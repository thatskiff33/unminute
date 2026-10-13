# Speaker naming and attribution corrections (#21). Synthetic data, no models.
#   python -m unittest discover -s tests -v

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
SR = 16000

if HAVE_NUMPY:
    from asr_engines import Segment
    from workspace import speaker_names as sn
    from workspace.speaker_names import NamingError
    from workspace.store import Store


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class SpeakerNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite"
        self.store = Store(self.path)
        mid = self.store.create_meeting("t")
        sid = self.store.add_source(mid, original_path=None, original_sha256="a" * 64,
                                    original_size=1, original_mtime_ns=0, audio_sha256="b" * 64,
                                    samples=60 * SR, sample_rate=SR)
        self.vid = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                             model_revision="1", precision="int8", config={},
                                             chunk_plan=[(0, 60 * SR, 0, 60 * SR)])
        # Twelve 4 s passages; A speaks 0-20 s and 40-60 s, B 20-40 s.
        self.store.commit_chunk(self.vid, 0, [Segment(t, t + 4.0, f"passage {t:.0f}")
                                              for t in range(0, 60, 5)])
        turns = [(0, 20, "SPEAKER_00"), (20, 40, "SPEAKER_01"), (40, 60, "SPEAKER_00")]
        self.add_run(turns)
        self.segs = {r["text"]: r["id"] for r in self.store.transcript(self.vid)}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_run(self, turns):
        rid = self.store.start_speaker_run(self.vid)
        self.assertTrue(self.store.finish_speaker_run(rid, turns, turns))

    def who(self, text):
        view = sn.resolve(self.store, self.vid)
        return view["names"].get(view["labels"][self.segs[text]])

    def test_default_names_follow_first_appearance(self):
        self.assertEqual(self.who("passage 0"), "Speaker 1")
        self.assertEqual(self.who("passage 25"), "Speaker 2")
        self.assertEqual([s["passages"] for s in sn.summary(self.store, self.vid)["speakers"]],
                         [8, 4])

    def test_rename_merge_assign_and_undo_survive_restart(self):
        sn.rename(self.store, self.vid, "SPEAKER_00", "  Aroha   Smith ")
        sn.assign(self.store, self.vid, self.segs["passage 30"], "SPEAKER_00")
        new = sn.assign(self.store, self.vid, self.segs["passage 35"], "new")
        self.assertEqual(new, "NEW_1")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.who("passage 0"), "Aroha Smith")
        self.assertEqual(self.who("passage 30"), "Aroha Smith")
        self.assertEqual(self.who("passage 35"), "Speaker 3")
        sn.merge(self.store, self.vid, "NEW_1", "SPEAKER_01")
        self.assertEqual(self.who("passage 35"), "Speaker 2")
        self.assertEqual(sn.undo(self.store, self.vid), "merge")
        self.assertEqual(self.who("passage 35"), "Speaker 3")
        for _ in range(3):
            sn.undo(self.store, self.vid)
        self.assertEqual(self.who("passage 0"), "Speaker 1")
        self.assertIsNone(sn.undo(self.store, self.vid))
        self.assertFalse(sn.summary(self.store, self.vid)["can_undo"])

    def test_default_numbers_stay_stable_when_correcting(self):
        sn.assign(self.store, self.vid, self.segs["passage 0"], "new")   # earliest passage
        self.assertEqual(self.who("passage 0"), "Speaker 3")
        self.assertEqual(self.who("passage 5"), "Speaker 1")
        self.assertEqual(self.who("passage 25"), "Speaker 2")

    def test_name_follows_a_merge(self):
        sn.rename(self.store, self.vid, "SPEAKER_01", "Tama")
        sn.merge(self.store, self.vid, "SPEAKER_01", "SPEAKER_00")
        self.assertEqual(self.who("passage 25"), "Tama")    # merged person keeps the name
        sn.undo(self.store, self.vid)
        sn.merge(self.store, self.vid, "SPEAKER_00", "SPEAKER_01")
        self.assertEqual(self.who("passage 0"), "Tama")

    def test_machine_attribution_and_text_are_never_changed(self):
        before = self.store.current_speakers(self.vid)
        text = [r["text"] for r in self.store.transcript(self.vid)]
        sn.assign(self.store, self.vid, self.segs["passage 0"], "SPEAKER_01")
        sn.merge(self.store, self.vid, "SPEAKER_00", "SPEAKER_01")
        self.assertEqual(self.store.current_speakers(self.vid), before)
        self.assertEqual([r["text"] for r in self.store.transcript(self.vid)], text)

    def test_refusals(self):
        with self.assertRaises(NamingError):
            sn.rename(self.store, self.vid, "SPEAKER_00", "   ")
        with self.assertRaises(NamingError):
            sn.rename(self.store, self.vid, "SPEAKER_09", "X")
        with self.assertRaises(NamingError):
            sn.merge(self.store, self.vid, "SPEAKER_00", "SPEAKER_00")
        with self.assertRaises(NamingError):
            sn.assign(self.store, self.vid, 10**9, "SPEAKER_00")
        with self.assertRaises(NamingError):
            sn.assign(self.store, self.vid, self.segs["passage 0"], "SPEAKER_07")

    def test_a_new_speaker_pass_starts_without_old_corrections(self):
        sn.rename(self.store, self.vid, "SPEAKER_00", "Aroha")
        self.add_run([(0, 60, "SPEAKER_00")])
        self.assertEqual(self.who("passage 0"), "Speaker 1")

    def test_examples_are_spread_bounded_and_page(self):
        first = sn.examples(self.store, self.vid, "SPEAKER_00")
        self.assertEqual(first["total"], 8)
        starts = [i["start"] for i in first["items"]]
        self.assertEqual(starts, [0.0, 40.0, 55.0])            # spread over the meeting
        for item in first["items"]:
            self.assertEqual(item["end"] - item["start"], 4.0)  # exactly the passage
            self.assertTrue(item["text"].startswith("passage"))
        more = sn.examples(self.store, self.vid, "SPEAKER_00", offset=3)
        self.assertFalse({i["id"] for i in more["items"]} & {i["id"] for i in first["items"]})

    def test_merged_person_takes_the_latest_merged_name(self):
        self.add_run([(0, 20, "SPEAKER_00"), (20, 40, "SPEAKER_01"), (40, 60, "SPEAKER_02")])
        sn.rename(self.store, self.vid, "SPEAKER_00", "X")
        sn.rename(self.store, self.vid, "SPEAKER_01", "Y")
        sn.merge(self.store, self.vid, "SPEAKER_01", "SPEAKER_02")
        sn.merge(self.store, self.vid, "SPEAKER_00", "SPEAKER_02")
        self.assertEqual(self.who("passage 45"), "X")
        sn.undo(self.store, self.vid)
        self.assertEqual(self.who("passage 45"), "Y")

    def test_examples_show_exactly_what_plays_when_only_long_passages_exist(self):
        mid = self.store.create_meeting("long")
        sid = self.store.add_source(mid, original_path=None, original_sha256="c" * 64,
                                    original_size=1, original_mtime_ns=0,
                                    audio_sha256="d" * 64, samples=60 * SR, sample_rate=SR)
        vid = self.store.ensure_version(mid, sid, engine="fake", model="fake",
                                        model_revision="1", precision="int8", config={},
                                        chunk_plan=[(0, 60 * SR, 0, 60 * SR)])
        words = tuple((float(t), t + 0.8, f" w{t}") for t in range(0, 40))
        self.store.commit_chunk(vid, 0, [Segment(0.0, 40.0, "".join(w[2] for w in words).strip(),
                                                 words)])
        rid = self.store.start_speaker_run(vid)
        self.store.finish_speaker_run(rid, [(0, 40, "A")], [(0, 40, "A")])
        [item] = sn.examples(self.store, vid, "A")["items"]
        self.assertLessEqual(item["end"] - item["start"], sn.MAX_CLIP_S)
        self.assertTrue(item["text"].startswith("w0 w1"))
        self.assertIn("w14", item["text"])
        self.assertNotIn("w15", item["text"])             # spoken after the clip ends

    def test_panel_returns_every_speaker_with_examples(self):
        p = sn.panel(self.store, self.vid, {"SPEAKER_00": 3})
        self.assertEqual([s["label"] for s in p["speakers"]], ["SPEAKER_00", "SPEAKER_01"])
        self.assertEqual(p["speakers"][0]["offset"], 3)
        self.assertEqual(len(p["speakers"][1]["examples"]["items"]), 3)

    def test_names_are_removed_with_the_transcript(self):
        from workspace.cleanup import _remove_transcript
        sn.rename(self.store, self.vid, "SPEAKER_00", "Aroha")
        mid = self.store.version(self.vid)["meeting_id"]
        _remove_transcript(self.store, mid, Path(self.tmp.name) / "media")
        self.assertEqual(self.store._one("SELECT COUNT(*) AS n FROM speaker_corrections")["n"], 0)


if __name__ == "__main__":
    unittest.main()
