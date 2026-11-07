# Joined lines (#561, the owner's decision DEC-100): a sentence split into two
# stored passages is shown, copied and handed off as one line; the stored text
# never changes.
#   - The rule (workspace/joins.py): a passage joins the one before it when that
#     one's text as shown does not end a sentence, the gap is at most 1.0 s, the
#     final speakers match (or neither has one) and neither carries the
#     overlapping-speech mark. The page's own sentence-end test (meeting.js
#     endsSentence, which only decides whether to ask the app again) agrees.
#   - The bridge's transcript reply carries `joined` per item, right for the
#     first item of any page (a one-passage lookback) and for earlier versions,
#     and follows edits (1a), reassignments and merges (2a).
#   - The handoff text joins the same lines, with the first passage's time and
#     speaker; redaction and replaced names apply to the line as shown; leaving
#     out any passage of a line leaves out the whole line (3a).
# Synthetic text only, no models.
#   python tools/run_tests.py --only test_joins

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyprotect  # noqa: E402
import page_scripts  # noqa: E402
from worker_exit import join_workers  # noqa: E402

from workspace import joins  # noqa: E402

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("numpy", "av"))
SR = 16000


def piece(text, start, end, label="SPEAKER_00", overlap=False):
    return {"text": text, "start": start, "end": end, "label": label, "overlap": overlap}


class RuleTests(unittest.TestCase):
    def test_a_boundary_inside_a_sentence_joins(self):
        self.assertTrue(joins.joins_previous(piece("We looked at the", 0, 2),
                                             piece("budget.", 2.3, 3)))

    def test_a_sentence_end_stays_two_lines(self):
        for text in ("Done.", "Done?", "Done!", "Done. ", 'He said "done."', "(Done.)",
                     "Done.\u201d", "It\u2019s done!\u2019 ", "Done.) "):
            with self.subTest(text=text):
                self.assertTrue(joins.ends_sentence(text))
                self.assertFalse(joins.joins_previous(piece(text, 0, 2), piece("next", 2.1, 3)))
        for text in ("Done", "Done,", "12.5 percent", "", None, "Done;", "and..then"):
            with self.subTest(text=text):
                self.assertFalse(joins.ends_sentence(text))

    def test_a_speaker_change_stays_two_lines(self):
        self.assertFalse(joins.joins_previous(piece("Well I", 0, 2, "SPEAKER_00"),
                                              piece("think so.", 2.1, 3, "SPEAKER_01")))
        self.assertFalse(joins.joins_previous(piece("Well I", 0, 2, None),
                                              piece("think so.", 2.1, 3, "SPEAKER_01")))

    def test_both_unlabelled_joins(self):
        self.assertTrue(joins.joins_previous(piece("Well I", 0, 2, None),
                                             piece("think so.", 2.1, 3, None)))

    def test_a_gap_over_one_second_stays_two_lines(self):
        self.assertEqual(1.0, joins.JOIN_GAP_S)
        self.assertTrue(joins.joins_previous(piece("Well I", 0, 2), piece("think", 3.0, 4)))
        self.assertFalse(joins.joins_previous(piece("Well I", 0, 2), piece("think", 3.01, 4)))

    def test_overlapping_speech_stays_two_lines(self):
        self.assertFalse(joins.joins_previous(piece("Well I", 0, 2),
                                              piece("think", 2.1, 3, overlap=True)))
        self.assertFalse(joins.joins_previous(piece("Well I", 0, 2, overlap=True),
                                              piece("think", 2.1, 3)))

    def test_edited_text_that_now_ends_a_sentence_splits(self):
        # The text as shown is what counts (1a): an edit adding a full stop splits.
        self.assertTrue(joins.joins_previous(piece("Well I", 0, 2), piece("think", 2.1, 3)))
        self.assertFalse(joins.joins_previous(piece("Well I.", 0, 2), piece("think", 2.1, 3)))

    def test_lines_group_every_piece_once_in_order(self):
        pieces = [piece("A", 0, 1), piece("b.", 1.1, 2), piece("C", 2.1, 3),
                  piece("d", 5, 6), piece("e", 6.1, 7, "SPEAKER_01")]
        got = joins.lines(pieces)
        self.assertEqual([["A", "b."], ["C"], ["d"], ["e"]],
                         [[p["text"] for p in line] for line in got])
        self.assertEqual(pieces, [p for line in got for p in line])
        self.assertEqual([], joins.lines([]))


class PageRuleTests(unittest.TestCase):
    """The page's endsSentence (workspace/ui/meeting.js) only decides whether an
    edit could have changed a join, and then asks the app; it must agree with
    the app's rule, or an edit that changes a join would not be followed."""

    CASES = ["Done.", "Done?", "Done!", "Done. ", 'He said "done."', "(Done.)",
             "Done.\u201d", "It\u2019s done!\u2019 ", "Done.) ", "Done.\u00bb", "Done",
             "Done,", "12.5 percent", "", "Done;", "and..then", "Wait.\n", "x ]", "?"]

    def test_the_page_agrees_with_the_app(self):
        got = page_scripts.run(
            f"console.log(JSON.stringify({json.dumps(self.CASES)}.map(endsSentence)));")
        self.assertEqual([joins.ends_sentence(c) for c in self.CASES], got)


def add_meeting(store, rows, turns, model="fake", title="synthetic"):
    """A meeting (or, given its id as title, a new version of it) with rows
    (start, end, text) and speaker turns [(start, end, label)]. Returns
    (mid, vid, ids in transcript order, source id)."""
    from workspace.asr_engines import Segment
    total = 60 * SR
    if isinstance(title, tuple):
        mid, sid = title
    else:
        mid = store.create_meeting(title)
        sid = store.add_source(mid, original_path=None, original_sha256="a" * 64,
                               original_size=1, original_mtime_ns=0, audio_id="b" * 64,
                               samples=total, sample_rate=SR)
    vid = store.ensure_version(mid, sid, engine="fake", model=model, model_revision="1",
                               precision="int8", config={}, chunk_plan=[(0, total, 0, total)])
    assert store.commit_chunk(vid, 0, [Segment(a, b, t) for a, b, t in rows])
    rid = store.start_speaker_run(vid)
    assert store.finish_speaker_run(rid, turns, turns)
    return mid, vid, [r["id"] for r in store.transcript(vid)], sid


ROWS = [(0.0, 2.0, "We looked at the"),          # 0
        (2.3, 4.0, "budget for the garden."),    # 1 joins 0
        (4.5, 6.0, "Then the lease"),            # 2 (0 ends a sentence)
        (6.2, 8.0, "was signed"),                # 3 joins 2
        (9.5, 11.0, "by the owner and"),         # 4 (a gap of 1.5 s)
        (11.4, 13.0, "the tenant"),              # 5 (another speaker)
        (13.3, 15.0, "too.")]                    # 6 joins 5
TURNS = [(0.0, 11.2, "SPEAKER_00"), (11.2, 20.0, "SPEAKER_01")]
JOINED = [False, True, False, True, False, False, True]


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class TranscriptReplyTests(unittest.TestCase):
    def setUp(self):
        from workspace.app import Workspace
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = Workspace(root / "ws", root / "models", start=False, battery=False,
                            allow_test_engines=True, protector=keyprotect.TestProtector())
        self.mid, self.vid, self.ids, self.sid = add_meeting(self.ws.store, ROWS, TURNS)

    def tearDown(self):
        try:
            self.ws.close()
            join_workers(self)
        finally:
            self.tmp.cleanup()

    def joined(self, start=0, count=500, version=None):
        return [i["joined"] for i in self.ws.transcript(self.mid, start, count, version)["items"]]

    def test_each_item_says_whether_it_joins_the_one_before(self):
        page = self.ws.transcript(self.mid)
        self.assertEqual(self.ids, [i["id"] for i in page["items"]])
        self.assertEqual(JOINED, [i["joined"] for i in page["items"]])
        # The stored text is unchanged: each item is still its own passage.
        self.assertEqual([t for _, _, t in ROWS], [i["text"] for i in page["items"]])

    def test_the_first_item_of_any_page_is_right(self):
        for count in range(1, len(ROWS) + 1):
            got = []
            for start in range(0, len(ROWS), count):
                page = self.ws.transcript(self.mid, start, count)
                self.assertEqual((len(ROWS), start), (page["total"], page["start"]))
                got += [i["joined"] for i in page["items"]]
            self.assertEqual(JOINED, got, f"pages of {count}")
        self.assertEqual([True], self.joined(1, 1))
        self.assertEqual([], self.joined(len(ROWS), 5))
        self.assertEqual([], self.joined(len(ROWS) + 3, 5))

    def test_an_edit_that_ends_or_unends_a_sentence_follows(self):
        self.ws.edit(self.mid, self.ids[0], "We looked at it.")
        self.assertEqual([False, False], self.joined(0, 2))
        self.assertEqual([False], self.joined(1, 1))
        self.ws.undo(self.mid)
        self.assertEqual([True], self.joined(1, 1))
        self.ws.edit(self.mid, self.ids[1], "budget for the garden and")   # no longer ends
        self.assertEqual([True, True], self.joined(1, 2))

    def test_a_reassigned_passage_splits_and_a_merge_joins(self):
        from workspace import speaker_names
        speaker_names.assign(self.ws.store, self.vid, self.ids[1], "SPEAKER_01")
        self.assertEqual([False, False], self.joined(1, 2))
        speaker_names.undo(self.ws.store, self.vid)
        self.assertEqual([True], self.joined(1, 1))
        # The final speaker counts: merged into one person, 4 and 5 join.
        self.assertEqual([False], self.joined(5, 1))
        speaker_names.merge(self.ws.store, self.vid, "SPEAKER_01", "SPEAKER_00")
        self.assertEqual([True, True], self.joined(5, 2))

    def test_an_earlier_version_has_its_own_joins(self):
        newer = [(0.0, 2.0, "Fresh words."), (2.1, 3.0, "Another one")]
        add_meeting(self.ws.store, newer, TURNS, model="fake2", title=(self.mid, self.sid))
        self.assertEqual([False, False], self.joined())
        self.assertEqual(JOINED, self.joined(version=0))
        self.assertEqual([True, False], self.joined(3, 2, version=0))


@unittest.skipUnless(HAVE_DEPS, "numpy/av not installed")
class HandoffJoinTests(unittest.TestCase):
    ROWS = [(0.0, 4.0, "We agreed the budget of"),           # 0
            (4.5, 8.0, "40,000 dollars with Aroha"),        # 1 joins 0
            (8.2, 10.0, "Smith present."),                   # 2 joins 1
            (12.0, 16.0, "The car park issue is still open."),   # 3, Speaker 2
            (16.5, 18.0, "We will")]                         # 4, Speaker 2 again
    TURNS = [(0.0, 11.0, "SPEAKER_00"), (11.0, 30.0, "SPEAKER_01")]

    def setUp(self):
        from workspace import handoff, speaker_names
        from workspace.store import Store
        self.handoff = handoff
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / "db.sqlite", protector=keyprotect.TestProtector())
        self.mid, self.vid, self.ids, _ = add_meeting(self.store, self.ROWS, self.TURNS,
                                                      title="Board meeting")
        speaker_names.rename(self.store, self.vid, "SPEAKER_00", "Aroha Smith")
        with self.store.tx() as db:
            db.execute("UPDATE meetings SET reviewed_at = ?", (time.time(),))
        self.template = {"id": "synthetic", "title": "Synthetic", "prompt": "Summarise.",
                         "version": 1}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def body(self, anonymise=False):
        out = self.handoff.build(self.store, self.mid, self.template, anonymise)
        md = out["markdown"]
        lines = md[md.index("## Transcript"):].split("\n")[2:]
        return out, [x for x in lines if x.startswith("[")]

    def test_a_joined_line_has_the_first_passages_time_and_speaker(self):
        out, lines = self.body()
        self.assertEqual(
            ["[0:00] **Aroha Smith:** We agreed the budget of 40,000 dollars with Aroha Smith "
             "present.",
             "[0:12] **Speaker 2:** The car park issue is still open.",
             "[0:16] **Speaker 2:** We will"], lines)
        self.assertEqual(0, out["omitted"])
        self.assertEqual(self.handoff.digest(out["combined"]), out["digests"]["combined"])
        # Display only: the stored passages are as they were.
        self.assertEqual([t for _, _, t in self.ROWS],
                         [r["text"] for r in self.store.transcript(self.vid)])

    def test_redaction_and_replaced_names_apply_to_each_passage_and_across_them(self):
        self.handoff.set_options(self.store, self.mid, [], ["budget", "Aroha Smith", "car park"])
        out, lines = self.body()
        R = self.handoff.REDACTED
        self.assertEqual(f"[0:00] **{R}:** We agreed the {R} of 40,000 dollars with {R} present.",
                         lines[0])
        self.assertEqual(f"[0:12] **Speaker 2:** The {R} issue is still open.", lines[1])
        # The name once (counted once for the label) and three in the text.
        self.assertEqual(4, out["redactions"])
        self.handoff.set_options(self.store, self.mid, [], [])
        _, lines = self.body(anonymise=True)
        self.assertEqual("[0:00] **Speaker 1:** We agreed the budget of 40,000 dollars with "
                         "Speaker 1 present.", lines[0])

    def test_leaving_out_one_passage_of_a_line_leaves_out_the_whole_line(self):
        self.handoff.set_options(self.store, self.mid, [self.ids[1]], [])
        out, lines = self.body()
        self.assertEqual(["[0:12] **Speaker 2:** The car park issue is still open.",
                          "[0:16] **Speaker 2:** We will"], lines)
        self.assertEqual(1, out["omitted"])
        self.assertIn("- For this handoff, 1 passage was left out", out["markdown"])
        text = out["combined"]
        for words in ("We agreed", "40,000", "present"):
            self.assertNotIn(words, text)
        # Every passage of the line, as the page sends them, is one line left out.
        self.handoff.set_options(self.store, self.mid, self.ids[:3] + [self.ids[4]], [])
        out, lines = self.body()
        self.assertEqual(["[0:12] **Speaker 2:** The car park issue is still open."], lines)
        self.assertEqual(2, out["omitted"])


if __name__ == "__main__":
    unittest.main()
